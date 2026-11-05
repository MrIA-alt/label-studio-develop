"""Signed webhooks. Payloads are signed with HMAC-SHA256 over `{timestamp}.{body}` using the subscription secret."""

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ValidationError

from collab.models import AuditEvent, WebhookSubscription

logger = logging.getLogger(__name__)

TIMEOUT = 5
RETRIES = 3


def validate_target_url(url):
    """Reject non-http(s) URLs and private/loopback targets (SSRF) unless COLLAB_WEBHOOK_ALLOW_PRIVATE is set."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValidationError({'url': 'Only http(s) URLs are allowed.'})
    if getattr(settings, 'COLLAB_WEBHOOK_ALLOW_PRIVATE', False):
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror:
        raise ValidationError({'url': 'Host cannot be resolved.'})
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValidationError({'url': 'Target host is not allowed.'})


def new_secret():
    return secrets.token_hex(32)


def sign(secret, timestamp, body):
    return hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()


def deliver(subscription, event, payload):
    """Send one event; retries with backoff and records a final failure in the audit trail. Returns success."""
    body = json.dumps({'event': event, 'data': payload}, sort_keys=True, default=str).encode()
    last_error = None
    for attempt in range(RETRIES):
        timestamp = str(int(time.time()))
        headers = {
            'Content-Type': 'application/json',
            'X-Collab-Event': event,
            'X-Collab-Timestamp': timestamp,
            'X-Collab-Signature': 'sha256=' + sign(subscription.secret, timestamp, body),
        }
        try:
            response = requests.post(
                subscription.url, data=body, headers=headers, timeout=TIMEOUT, allow_redirects=False
            )
            if response.status_code < 300:
                return True
            last_error = f'HTTP {response.status_code}'
        except requests.RequestException as exc:
            last_error = type(exc).__name__
        if attempt < RETRIES - 1:
            time.sleep(0.5 * 2**attempt)
    AuditEvent.objects.create(
        actor_type=AuditEvent.ActorType.USER,
        project_id=subscription.project_id,
        action='webhook.delivery_failed',
        resource_type='webhook',
        resource_id=str(subscription.pk),
        detail={'event': event, 'error': last_error},
    )
    return False


def _dispatch(subscriptions, event, payload):
    for sub in subscriptions:
        deliver(sub, event, payload)


def emit(project, event, payload):
    """Queue `event` for every active subscription of `project`, after the current transaction commits."""
    subs = [
        s for s in WebhookSubscription.objects.filter(project=project, active=True) if event in (s.events or [])
    ]
    if not subs:
        return
    payload = {'project': project.pk, **payload}

    def run():
        threading.Thread(target=_dispatch, args=(subs, event, payload), daemon=True).start()

    transaction.on_commit(run)
