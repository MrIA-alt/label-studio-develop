"""Signed webhooks. Payloads are signed with HMAC-SHA256 over `{timestamp}.{body}` using the subscription secret."""

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from datetime import timedelta
import time
from urllib.parse import urlparse

import urllib3
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from collab.models import AuditEvent, WebhookDelivery, WebhookSubscription

logger = logging.getLogger(__name__)

TIMEOUT = 5
RETRIES = 3
MAX_DELIVERY_ATTEMPTS = 8


def resolve_target(url):
    """Resolve once and return only validated IPs; the transport connects to that exact address."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError({'url': 'Only http(s) URLs are allowed.'})
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError):
        raise ValidationError({'url': 'Host cannot be resolved.'})
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not getattr(settings, 'COLLAB_WEBHOOK_ALLOW_PRIVATE', False) and not ip.is_global:
            raise ValidationError({'url': 'Target host is not allowed.'})
        addresses.append(str(ip))
    if not addresses:
        raise ValidationError({'url': 'Host cannot be resolved.'})
    return parsed, port, sorted(set(addresses))


def validate_target_url(url):
    resolve_target(url)


def _post_once(url, body, headers):
    """No proxy, redirect or second DNS lookup; preserve TLS SNI and hostname verification."""
    parsed, port, addresses = resolve_target(url)
    hostname = parsed.hostname
    host_header = f'[{hostname}]' if ':' in hostname else hostname
    if port != (443 if parsed.scheme == 'https' else 80):
        host_header += f':{port}'
    headers = {**headers, 'Host': host_header}
    if parsed.scheme == 'https':
        pool = urllib3.HTTPSConnectionPool(
            addresses[0], port=port, server_hostname=hostname, assert_hostname=hostname, cert_reqs='CERT_REQUIRED'
        )
    else:
        pool = urllib3.HTTPConnectionPool(addresses[0], port=port)
    response = None
    try:
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        response = pool.urlopen(
            'POST', path, body=body, headers=headers, timeout=TIMEOUT,
            retries=False, redirect=False, preload_content=False,
        )
        return response.status
    finally:
        if response is not None:
            response.close()
        pool.close()


def new_secret():
    return secrets.token_hex(32)


def sign(secret, timestamp, body):
    return hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()


def deliver(subscription, event, payload, delivery_id=None, retries=RETRIES):
    """Send one event; retries with backoff and records a final failure in the audit trail. Returns success."""
    body = json.dumps({'event': event, 'data': payload}, sort_keys=True, default=str).encode()
    last_error = None
    for attempt in range(retries):
        timestamp = str(int(time.time()))
        headers = {
            'Content-Type': 'application/json',
            'X-Collab-Event': event,
            'X-Collab-Timestamp': timestamp,
            'X-Collab-Signature': 'sha256=' + sign(subscription.secret, timestamp, body),
        }
        if delivery_id is not None:
            headers['X-Collab-Delivery'] = str(delivery_id)
        try:
            status = _post_once(subscription.url, body, headers)
            if 200 <= status < 300:
                return True
            last_error = f'HTTP {status}'
        except (urllib3.exceptions.HTTPError, OSError, ValidationError) as exc:
            last_error = type(exc).__name__
        if attempt < retries - 1:
            time.sleep(0.5 * 2**attempt)
    if delivery_id is not None:
        return False
    AuditEvent.objects.create(
        actor_type=AuditEvent.ActorType.USER,
        project_id=subscription.project_id,
        organization_id=subscription.project.organization_id,
        action='webhook.delivery_failed',
        resource_type='webhook',
        resource_id=str(subscription.pk),
        detail={'event': event, 'error': last_error},
    )
    return False


def emit(project, event, payload):
    """Persist deliveries in the same transaction as their originating business mutation."""
    subs = [
        s for s in WebhookSubscription.objects.filter(project=project, active=True) if event in (s.events or [])
    ]
    if not subs:
        return
    payload = json.loads(json.dumps({'project': project.pk, **payload}, default=str))
    WebhookDelivery.objects.bulk_create([
        WebhookDelivery(subscription=sub, event=event, payload=payload, available_at=timezone.now()) for sub in subs
    ])


def drain_outbox(limit=100):
    """Lease durable jobs, retry transient failures and recover jobs interrupted by a restart."""
    sent = failed = 0
    for _ in range(limit):
        now = timezone.now()
        with transaction.atomic():
            ready = WebhookDelivery.objects.filter(subscription__active=True).filter(
                Q(status=WebhookDelivery.Status.PENDING, available_at__lte=now)
                | Q(status=WebhookDelivery.Status.SENDING, lease_until__lte=now)
            ).order_by('id')
            from django.db import connection
            lock_options = {'skip_locked': True} if connection.features.has_select_for_update_skip_locked else {}
            job = ready.select_for_update(**lock_options).first()
            if job is None:
                break
            job.status = WebhookDelivery.Status.SENDING
            job.lease_until = now + timedelta(seconds=60)
            job.attempts += 1
            job.save(update_fields=['status', 'lease_until', 'attempts'])
        ok = deliver(job.subscription, job.event, job.payload, delivery_id=job.pk, retries=1)
        with transaction.atomic():
            current = WebhookDelivery.objects.select_for_update().filter(pk=job.pk).first()
            if current is None:
                continue  # Subscription removed while the request was in flight.
            if current.status != WebhookDelivery.Status.SENDING or current.attempts != job.attempts:
                continue
            current.lease_until = None
            current.last_error = '' if ok else 'Delivery failed; inspect destination and retry.'
            if ok:
                current.status = WebhookDelivery.Status.SENT
                current.delivered_at = timezone.now()
                sent += 1
            elif current.attempts >= MAX_DELIVERY_ATTEMPTS:
                current.status = WebhookDelivery.Status.FAILED
                failed += 1
                AuditEvent.objects.create(
                    project_id=current.subscription.project_id, action='webhook.delivery_failed',
                    organization_id=current.subscription.project.organization_id,
                    resource_type='webhook_delivery', resource_id=str(current.pk), detail={'event': current.event},
                )
            else:
                current.status = WebhookDelivery.Status.PENDING
                current.available_at = timezone.now() + timedelta(seconds=min(3600, 2 ** current.attempts))
            current.save()
    return {'sent': sent, 'failed': failed, 'pending': WebhookDelivery.objects.filter(status='pending').count()}


dispatch_pending = drain_outbox
