"""Exercise backup/restore and restart recovery of an isolated Docker acceptance instance.

Run this module with the host's Python and Docker CLI; no host Django installation is needed.
The same source is streamed into the application container for database and media checks.
Only databases generated as ``collab_restore_<32 hex digits>`` can be restore targets.
Restarting app/maintenance is a separate, explicitly selected phase.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import time
import uuid


PROJECT = 'label-studio-collab'
SOURCE_DB = 'collab'
JSON_MARKER = 'COLLAB_OPERATIONAL_JSON='


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def private_json(path, value):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(value, output, indent=2, sort_keys=True, default=str)


def check_run(run_id):
    require(bool(re.fullmatch(r'[0-9a-f]{32}', run_id or '')), 'Invalid operational run identifier.')


def check_restore_database(name):
    require(bool(re.fullmatch(r'collab_restore_[0-9a-f]{32}', name or '')), 'Restore target must be a generated disposable database.')
    require(name != SOURCE_DB, 'The application database cannot be a restore target.')


def verify_webhook_transport(run_id):
    """Real loopback HTTP retry/signature test; rows stay invisible to the worker until disabled."""
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from django.db import transaction
    from django.test import override_settings
    from django.utils import timezone as django_timezone
    from rest_framework.exceptions import ValidationError
    from collab import webhooks
    from collab.models import WebhookDelivery, WebhookSubscription
    from organizations.models import Organization
    from projects.models import Project
    from users.models import User

    require(not WebhookSubscription.objects.filter(active=True).exists(), 'Wait until other active webhook subscriptions are disabled before the isolated transport probe.')
    secret, received = secrets.token_hex(32), []
    expected = {}

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            timestamp = self.headers.get('X-Collab-Timestamp', '')
            signature = self.headers.get('X-Collab-Signature', '')
            computed = 'sha256=' + hmac.new(secret.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
            received.append({
                'signature': hmac.compare_digest(signature, computed),
                'timestamp': timestamp.isdecimal() and abs(time.time() - int(timestamp)) < 30,
                'body': json.loads(raw) == expected['body'],
                'event': self.headers.get('X-Collab-Event') == 'review.decided',
                'delivery': self.headers.get('X-Collab-Delivery') == str(expected['delivery']),
            })
            self.send_response(503 if len(received) == 1 else 200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/fixture'
    try:
        with override_settings(COLLAB_WEBHOOK_ALLOW_PRIVATE=False):
            try:
                webhooks.validate_target_url(url)
            except ValidationError:
                pass
            else:
                raise RuntimeError('Private webhook target was not rejected under the default policy.')
        require(not received, 'Default-policy validation contacted the private target.')
        with transaction.atomic():
            owner = User.objects.create(username='operational-' + run_id, email='operational-' + run_id + '@example.test')
            owner.set_unusable_password()
            owner.save(update_fields=['password'])
            org = Organization.create_organization(created_by=owner, title='Operational webhook ' + run_id)
            project = Project.objects.create(title='Webhook probe ' + run_id, organization=org, created_by=owner)
            subscription = WebhookSubscription.objects.create(project=project, url=url, secret=secret, events=['review.decided'])
            payload = {'project': project.pk, 'probe': run_id}
            delivery = WebhookDelivery.objects.create(subscription=subscription, event='review.decided', payload=payload, available_at=django_timezone.now())
            expected.update(body={'event': 'review.decided', 'data': payload}, delivery=delivery.pk)
            with override_settings(COLLAB_WEBHOOK_ALLOW_PRIVATE=True):
                require(webhooks.drain_outbox(limit=1)['sent'] == 0, 'First receiver response must schedule a retry.')
                delivery.refresh_from_db()
                wait_seconds = (delivery.available_at - django_timezone.now()).total_seconds()
                require(delivery.status == 'pending' and delivery.attempts == 1 and 0 < wait_seconds <= 3, '503 did not persist the expected retry/backoff.')
                require(len(received) == 1, 'First delivery did not reach the real HTTP receiver once.')
                webhooks.drain_outbox(limit=1)
                require(len(received) == 1, 'The outbox retried before its backoff elapsed.')
                time.sleep(10)
                require(webhooks.drain_outbox(limit=1)['sent'] == 1, 'The scheduled retry did not succeed over HTTP.')
            delivery.refresh_from_db()
            require(delivery.status == 'sent' and delivery.attempts == 2 and len(received) == 2, 'The outbox did not retain the successful second attempt.')
            require(all(all(values.values()) for values in received), 'Received webhook signature, timestamp, payload or delivery identity was invalid.')
            subscription.active = False
            subscription.secret = secrets.token_hex(32)  # Discard the signing key used by the transient receiver.
            subscription.save(update_fields=['active', 'secret'])
            result = {'project': project.pk, 'delivery': delivery.pk, 'attempts': delivery.attempts,
                      'requests_received': len(received), 'http_statuses': [503, 200], 'backoff_seconds_observed': round(wait_seconds, 3),
                      'default_private_target_rejected': True, 'signature_timestamp_body_verified': True,
                      'subscription_disabled_before_commit': True,
                      'limitation': 'Private-target override was enabled only in this synthetic process; application configuration was unchanged.'}
        return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def inside(options):
    """Execute using the container's installed application and existing private environment."""
    check_run(options.run_id)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.label_studio')
    import django

    django.setup()
    from django.conf import settings
    from django.db import connection, transaction
    from django.db.models import Count
    from django.utils import timezone as django_timezone
    from collab.models import (
        AnnotationReview, AnnotationRevision, AuditEvent, IdempotencyRecord, ProjectAnnotationPolicy,
        ServiceAccount, TaskArbitration, TaskAssignment, WebhookDelivery, WebhookSubscription,
    )
    from data_import.models import FileUpload
    from fsm.state_models import AnnotationState, ProjectState, TaskState
    from organizations.models import OrganizationMember
    from projects.models import Project, ProjectMember
    from tasks.models import Annotation, AnnotationDraft, Task
    from users.models import User

    require(connection.vendor == 'postgresql', 'Operational acceptance requires PostgreSQL.')
    require(connection.settings_dict['NAME'] == options.expected_db, 'Database configuration does not match the requested source/restore target.')
    if connection.settings_dict['NAME'] != SOURCE_DB:
        check_restore_database(connection.settings_dict['NAME'])
    if options.action == '_webhooks':
        require(connection.settings_dict['NAME'] == SOURCE_DB, 'Run the transport fixture only in the isolated application database.')
        return verify_webhook_transport(options.run_id)
    base = Path(settings.BASE_DATA_DIR).resolve()
    fixture = json.loads((base / 'collab-acceptance-credentials.json').read_text(encoding='utf-8'))
    project = Project.objects.select_related('organization').get(pk=fixture['project'])
    require(project.title.startswith('Collaborative acceptance '), 'Select the synthetic acceptance project.')
    require(project.organization.title.startswith('Acceptance '), 'Select the synthetic acceptance organization.')
    require(len(fixture['task_ids']) >= 100, 'Complete the one-hundred-task HTTP acceptance scenario first.')
    require(project.organization_id == fixture['organization'], 'Acceptance organization does not match.')
    probe_path = base / f'.collab-operational-{options.run_id}.json'
    marker_path = base / f'.collab-operational-{options.run_id}.bin'
    archive_path = Path('/tmp') / f'collab-operational-{options.run_id}-app-data.tar.gz'
    restore_root = Path('/tmp') / f'collab-operational-{options.run_id}-restored'

    def session():
        import requests

        caller = requests.Session()
        if probe_path.exists():
            caller.cookies.update(json.loads(probe_path.read_text(encoding='utf-8'))['cookies'])
        else:
            response = caller.get(options.base_url + '/user/login/', timeout=30)
            require(response.status_code == 200, 'Acceptance login page failed.')
            response = caller.post(options.base_url + '/user/login/', data={
                'email': fixture['users']['admin']['email'], 'password': fixture['password'],
                'csrfmiddlewaretoken': caller.cookies.get('csrftoken'),
            }, headers={'Referer': options.base_url + '/user/login/'}, timeout=30)
            require(response.status_code == 200, 'Acceptance session login failed.')
        response = caller.get(options.base_url + '/api/current-user/whoami', timeout=30)
        require(response.status_code == 200 and response.json()['id'] == fixture['users']['admin']['id'], 'Persisted acceptance session is invalid.')
        return caller

    def http_read():
        caller = session()
        task = caller.get(options.base_url + f'/api/tasks/{fixture["task_ids"][0]}/', timeout=30)
        require(task.status_code == 200, 'Source task could not be read over HTTP.')
        results = caller.get(options.base_url + f'/api/extensions/v1/projects/{project.pk}/results?limit=500', timeout=30)
        require(results.status_code == 200 and results.json()['count'] >= 100, 'Authorized results could not be read over HTTP.')
        media_checked = 0
        from urllib.parse import urlsplit

        for upload in FileUpload.objects.filter(project=project).order_by('pk'):
            path = urlsplit(upload.url).path
            require(path.startswith('/data/'), 'Operational media checks accept only local upload URLs.')
            response = caller.get(options.base_url + path, timeout=30)
            require(response.status_code == 200, 'Authorized source media could not be read over HTTP.')
            with upload.file.open('rb') as source:
                expected = hashlib.sha256(source.read()).hexdigest()
            require(hashlib.sha256(response.content).hexdigest() == expected, 'HTTP media differs from persisted source bytes.')
            media_checked += 1
        return {
            'persisted_session_valid': True, 'task_read': True, 'results_tasks': results.json()['count'],
            'media_files_read': media_checked,
            'media_http': 'verified' if media_checked else 'not verified: acceptance sources contain no uploaded files',
        }

    if options.action == '_checkpoint':
        require(connection.settings_dict['NAME'] == SOURCE_DB, 'Create checkpoints only in the isolated application database.')
        require(not probe_path.exists() and not marker_path.exists(), 'Checkpoint already exists; use a new run identifier.')
        caller = session()
        with transaction.atomic():
            subscription = WebhookSubscription.objects.create(
                project=project, url='https://example.invalid/operational', secret=secrets.token_hex(32),
                events=['project.published'], active=False, created_by_id=fixture['users']['admin']['id'],
            )
            delivery = WebhookDelivery.objects.create(
                subscription=subscription, event='project.published', payload={'operational_run': options.run_id},
                status=WebhookDelivery.Status.PENDING, available_at=django_timezone.now(),
            )
        descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as marker:
            marker.write(secrets.token_bytes(64))
        private_json(probe_path, {
            'project': project.pk, 'subscription': subscription.pk, 'delivery': delivery.pk,
            'cookies': caller.cookies.get_dict(),
        })
        return {'project': project.pk, 'marker_sha256': sha_file(marker_path), 'delivery': delivery.pk}

    if options.action == '_archive':
        require(connection.settings_dict['NAME'] == SOURCE_DB, 'Archive the isolated source volume only.')
        require(not archive_path.exists(), 'Archive already exists.')
        with tarfile.open(archive_path, 'x:gz') as archive:
            archive.add(base, arcname='app-data')
        archive_path.chmod(0o600)
        return {'path': str(archive_path), 'sha256': sha_file(archive_path), 'bytes': archive_path.stat().st_size}

    if options.action == '_extract':
        require(not restore_root.exists(), 'Disposable media restore directory already exists.')
        input_archive = Path('/tmp') / f'collab-operational-{options.run_id}-restore.tar.gz'
        restore_root.mkdir(mode=0o700)
        with tarfile.open(input_archive, 'r:gz') as archive:
            archive.extractall(restore_root, filter='data')
        return {'base_data_dir': str(restore_root / 'app-data'), 'archive_sha256': sha_file(input_archive)}

    require(options.action == '_snapshot', 'Unknown container operation.')
    probe = json.loads(probe_path.read_text(encoding='utf-8'))
    require(probe['project'] == project.pk, 'Checkpoint project does not match.')
    http = http_read() if connection.settings_dict['NAME'] == SOURCE_DB else None
    user_ids = list(OrganizationMember.objects.filter(organization=project.organization).values_list('user_id', flat=True))
    annotation_ids = Annotation.objects.filter(project=project).values('pk')
    tables = {}

    def table(name, queryset, fields=None):
        rows = list(queryset.order_by('pk').values(*(fields or [])))
        tables[name] = {'count': len(rows), 'sha256': digest(rows)}

    table('organization', type(project.organization).objects.filter(pk=project.organization_id))
    table('users', User.objects.filter(pk__in=user_ids), ('id', 'email', 'username', 'password', 'is_active', 'active_organization_id'))
    table('roles', OrganizationMember.objects.filter(organization=project.organization))
    table('project', Project.objects.filter(pk=project.pk))
    table('project_members', ProjectMember.objects.filter(project=project))
    table('policy', ProjectAnnotationPolicy.objects.filter(project=project))
    table('tasks', Task.objects.filter(project=project))
    table('annotations', Annotation.objects.filter(project=project))
    table('drafts', AnnotationDraft.objects.filter(task__project=project))
    table('assignments', TaskAssignment.objects.filter(project=project))
    table('reviews', AnnotationReview.objects.filter(annotation_id__in=annotation_ids))
    table('revisions', AnnotationRevision.objects.filter(annotation_id__in=annotation_ids))
    table('arbitration', TaskArbitration.objects.filter(task__project=project))
    table('audit', AuditEvent.objects.filter(organization=project.organization))
    table('service_accounts', ServiceAccount.objects.filter(user_id__in=user_ids))
    table('service_projects', ServiceAccount.projects.through.objects.filter(serviceaccount__user_id__in=user_ids))
    table('idempotency', IdempotencyRecord.objects.filter(user_id__in=user_ids))
    table('webhooks', WebhookSubscription.objects.filter(project=project))
    table('deliveries', WebhookDelivery.objects.filter(subscription__project=project))
    table('uploads', FileUpload.objects.filter(project=project))
    for model in (ProjectState, TaskState, AnnotationState):
        table(model._meta.label_lower, model.objects.filter(organization_id=project.organization_id))
    media = []
    for upload in FileUpload.objects.filter(project=project).order_by('pk'):
        with upload.file.open('rb') as source:
            contents = source.read()
        media.append({'id': upload.pk, 'bytes': len(contents), 'sha256': hashlib.sha256(contents).hexdigest()})
    require(Task.objects.filter(project=project).count() == len(fixture['task_ids']), 'Acceptance source task count changed.')
    for user in fixture['users'].values():
        require(User.objects.get(pk=user['id']).check_password(fixture['password']), 'Restored fixture credentials are invalid.')
    policy = ProjectAnnotationPolicy.objects.get(project=project)
    require(policy.required_annotations == 3 and policy.blind_mode, 'Acceptance annotation policy is invalid.')
    delivery = WebhookDelivery.objects.get(pk=probe['delivery'], subscription_id=probe['subscription'])
    require(delivery.status == WebhookDelivery.Status.PENDING and not delivery.subscription.active, 'Dormant outbox checkpoint was lost or delivered.')
    roles = dict(OrganizationMember.objects.filter(organization=project.organization).values_list('role').annotate(n=Count('pk')))
    require({'admin', 'qa', 'labeller'} <= set(roles), 'Acceptance roles are incomplete.')
    require(tables['reviews']['count'] > 0 and tables['revisions']['count'] > 0 and tables['audit']['count'] > 0, 'Complete acceptance review/history/audit before backup.')
    require(tables['arbitration']['count'] > 0 and tables['service_accounts']['count'] > 0, 'Complete acceptance arbitration and service workflow before backup.')
    return {
        'project': project.pk, 'tables': tables, 'media': media, 'roles': roles,
        'marker_sha256': sha_file(marker_path), 'pending_delivery': delivery.pk, 'http': http,
    }


class Docker:
    def __init__(self, options):
        self.options = options
        self.compose = ['compose', '--project-name', PROJECT, '--env-file', options.env_file, '-f', options.compose_file]
        self.source = Path(__file__).read_text(encoding='utf-8')

    def run(self, arguments, *, stdin=None):
        result = subprocess.run(['docker', *arguments], input=stdin, text=True, capture_output=True)
        if result.returncode:
            # Surface only our concise probe failures and structural Python
            # errors. Never print captured startup logs, SQL errors or payloads.
            for line in (result.stderr + '\n' + result.stdout).splitlines():
                if re.match(r'^Operational acceptance failed: (RuntimeError|AttributeError|TypeError|KeyError|FieldError): ', line):
                    raise RuntimeError(line.removeprefix('Operational acceptance failed: '))
            exception_types = re.findall(r'\b([A-Za-z_]+(?:Error|Exception)):', result.stderr)
            locations = re.findall(r'File "([^"]+)", line ([0-9]+)', result.stderr)
            location = (Path(locations[-1][0]).name + ':' + locations[-1][1]) if locations else 'no Python frame'
            kind = exception_types[-1] if exception_types else 'no Python exception'
            raise RuntimeError(f'Docker {arguments[0]} failed, exit={result.returncode}, {kind}, {location}; captured values withheld.')
        return result.stdout.strip()

    def container(self, service):
        identifier = self.run([*self.compose, 'ps', '--all', '-q', service])
        require(bool(re.fullmatch('[0-9a-f]{12,64}', identifier)), f'Exactly one running {service} container is required.')
        details = json.loads(self.run(['inspect', identifier]))[0]
        require(details['Config']['Labels'].get('com.docker.compose.project') == PROJECT, 'Unexpected Compose project.')
        return identifier

    def probe(self, action, run_id, *, database=None, base_data=None, service='app'):
        arguments = ['exec', '-i', '-w', '/label-studio/label_studio', '-e', 'COLLAB_ACCEPTANCE_TESTS=1']
        if database:
            check_restore_database(database)
            arguments += ['-e', 'POSTGRE_NAME=' + database]
        if base_data:
            arguments += ['-e', 'BASE_DATA_DIR=' + base_data, '-e', 'LABEL_STUDIO_BASE_DATA_DIR=' + base_data]
        arguments += [self.container(service), 'python', '-', action, '--inside', '--run-id', run_id,
                      '--base-url', self.options.base_url, '--expected-db', database or SOURCE_DB]
        output = self.run(arguments, stdin=self.source)
        records = [line[len(JSON_MARKER):] for line in output.splitlines() if line.startswith(JSON_MARKER)]
        require(len(records) == 1, 'Container operation did not produce one structured result.')
        return json.loads(records[0])

    def healthy(self, service):
        identifier = self.container(service)
        details = json.loads(self.run(['inspect', identifier]))[0]
        return details['State'].get('Health', {}).get('Status') == 'healthy'

    def started(self, service):
        return json.loads(self.run(['inspect', self.container(service)]))[0]['State']['StartedAt']

    def worker_marker(self):
        return int(self.run(['exec', self.container('maintenance'), 'python', '-c', "from pathlib import Path; print(Path('/tmp/collab-worker-health').stat().st_mtime_ns)"]))

    def copy_archive_to_app(self, source, destination):
        require(bool(re.fullmatch(r'/tmp/collab-operational-[0-9a-f]{32}-restore\.tar\.gz', destination)), 'Unexpected disposable archive path.')
        container = self.container('app')
        self.run(['cp', str(source), container + ':' + destination])
        uid = self.run(['exec', container, 'id', '-u'])
        gid = self.run(['exec', container, 'id', '-g'])
        require(uid.isdecimal() and gid.isdecimal(), 'Cannot determine application file ownership.')
        # docker cp creates root-owned files; preserve 0600 while allowing the
        # application's unprivileged user to read this disposable backup copy.
        self.run(['exec', '--user', '0', container, 'chown', uid + ':' + gid, destination])


def comparable(snapshot):
    return {key: value for key, value in snapshot.items() if key != 'http'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup-restore', 'restart-verify', 'webhook-verify', '_checkpoint', '_archive', '_extract', '_snapshot', '_webhooks'])
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    parser.add_argument('--expected-db', default=SOURCE_DB, help=argparse.SUPPRESS)
    parser.add_argument('--compose-file', default='docker-compose.collab.yml')
    parser.add_argument('--env-file')
    parser.add_argument('--base-url', default='http://nginx:8085', help='Application URL reachable from the application container.')
    parser.add_argument('--report', help='New private JSON report path; existing reports are never overwritten.')
    parser.add_argument('--checkpoint', help='Successful backup-restore report used by restart-verify.')
    options = parser.parse_args()
    require(os.environ.get('COLLAB_ACCEPTANCE_TESTS') == '1', 'Set COLLAB_ACCEPTANCE_TESTS=1 for the isolated acceptance instance.')
    if options.inside:
        require(options.action.startswith('_'), 'Internal mode accepts only container probes.')
        print(JSON_MARKER + json.dumps(inside(options), sort_keys=True, default=str))
        return
    require(not options.action.startswith('_'), 'Container probes require internal mode.')
    require(options.env_file and Path(options.env_file).is_file(), 'Select the private isolated Compose environment file.')
    require(options.report and not Path(options.report).exists(), 'Choose a new report path.')
    client = Docker(options)
    for service in ('db', 'app', 'maintenance', 'nginx'):
        require(client.healthy(service), f'{service} must be healthy before operational checks.')
    report = {'action': options.action, 'started_at': datetime.now(timezone.utc).isoformat(), 'checks': []}
    if options.action == 'webhook-verify':
        run_id = uuid.uuid4().hex
        report.update(run=run_id, webhook=client.probe('_webhooks', run_id))
        report['checks'] = ['default private-target policy rejected before HTTP', 'real loopback HTTP returned 503 then 200',
                            'HMAC/timestamp/body/delivery identity verified on both requests', 'pending/backoff/sent state and two attempts persisted',
                            'fixture subscription disabled before transaction commit; no application configuration changed']
    elif options.action == 'backup-restore':
        run_id = uuid.uuid4().hex
        restore_db = 'collab_restore_' + run_id
        check_restore_database(restore_db)
        report.update(run=run_id, restored_database=restore_db)
        client.probe('_checkpoint', run_id)
        before = client.probe('_snapshot', run_id)
        db_container = client.container('db')
        dump_path = '/tmp/collab-operational-' + run_id + '.dump'
        client.run(['exec', db_container, 'pg_dump', '--username=collab', '--dbname=' + SOURCE_DB, '--format=custom', '--file=' + dump_path])
        client.run(['exec', db_container, 'chmod', '600', dump_path])
        archive = client.probe('_archive', run_id)
        artifacts = Path(options.report).resolve().parent / ('collab-operational-' + run_id)
        artifacts.mkdir(mode=0o700)
        dump_file, media_file = artifacts / 'database.dump', artifacts / 'app-data.tar.gz'
        client.run(['cp', db_container + ':' + dump_path, str(dump_file)])
        client.run(['cp', client.container('app') + ':' + archive['path'], str(media_file)])
        dump_file.chmod(0o600)
        media_file.chmod(0o600)
        require(sha_file(media_file) == archive['sha256'], 'The copied media archive is damaged.')
        after_dump = client.probe('_snapshot', run_id)
        require(comparable(before) == comparable(after_dump), 'Source data changed during backup; pause acceptance writes and rerun.')
        restore_input = '/tmp/collab-operational-' + run_id + '-restore.dump'
        client.run(['cp', str(dump_file), db_container + ':' + restore_input])
        client.run(['exec', db_container, 'createdb', '--username=collab', '--maintenance-db=postgres', restore_db])
        client.run(['exec', db_container, 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '--username=collab', '--dbname=' + restore_db, restore_input])
        client.copy_archive_to_app(media_file, '/tmp/collab-operational-' + run_id + '-restore.tar.gz')
        extracted = client.probe('_extract', run_id)
        restored = client.probe('_snapshot', run_id, database=restore_db, base_data=extracted['base_data_dir'])
        require(comparable(before) == comparable(restored), 'Restored database or media differs from the source snapshot.')
        source_after = client.probe('_snapshot', run_id)
        require(comparable(before) == comparable(source_after), 'The source changed during restore verification.')
        report.update(snapshot=before, artifacts={
            'database': {'path': str(dump_file), 'sha256': sha_file(dump_file), 'bytes': dump_file.stat().st_size},
            'app_data': {'path': str(media_file), 'sha256': sha_file(media_file), 'bytes': media_file.stat().st_size},
        })
        report['checks'] = ['pg_dump completed', 'backup artifacts copied and private', 'restored into new disposable database',
                            'source database preserved', 'roles/scopes/policy/results/history/audit/FSM restored identically',
                            'app-data marker and existing uploaded media restored identically', 'dormant outbox job preserved']
    else:
        require(options.checkpoint and Path(options.checkpoint).is_file(), 'Select a successful backup-restore report.')
        checkpoint = json.loads(Path(options.checkpoint).read_text(encoding='utf-8'))
        require(checkpoint.get('action') == 'backup-restore' and checkpoint.get('success') is True, 'A successful backup-restore checkpoint is required.')
        run_id = checkpoint['run']
        check_run(run_id)
        before = client.probe('_snapshot', run_id)
        starts = {service: client.started(service) for service in ('app', 'maintenance')}
        worker_marker = client.worker_marker()
        client.run([*client.compose, 'restart', 'app', 'maintenance'])
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if all(client.healthy(service) for service in ('app', 'maintenance', 'nginx')) and client.worker_marker() > worker_marker:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Application/worker did not become healthy with fresh maintenance activity after restart.')
        require(all(client.started(service) != starts[service] for service in starts), 'Both application and maintenance must actually restart.')
        after = client.probe('_snapshot', run_id)
        require(comparable(before) == comparable(after), 'Persistent data changed or disappeared during restart.')
        worker_after = client.probe('_snapshot', run_id, service='maintenance')
        require(comparable(after) == comparable(worker_after), 'The worker does not see the same persistent database and application volume.')
        report.update(run=run_id, snapshot=after)
        report['checks'] = ['app and maintenance restarted', 'healthchecks recovered', 'worker completed a fresh maintenance cycle',
                            'worker reads the same persistent database and app-data volume',
                            'persistent data and dormant outbox job preserved', 'existing signed session/source/results remain usable over HTTP']
    report.update(success=True, completed_at=datetime.now(timezone.utc).isoformat())
    private_json(options.report, report)
    summary = {'success': True, 'action': options.action, 'checks': report['checks'], 'report': options.report}
    if 'snapshot' in report:
        summary.update(project=report['snapshot']['project'], media_http=report['snapshot']['http']['media_http'])
    else:
        summary['project'] = report['webhook']['project']
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never emit command output, captured application startup logs, cookies or backup contents.
        print(f'Operational acceptance failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
