"""Seed synthetic mail invitations through real HTTP in a disposable deployment.

Only run with COLLAB_ACCEPTANCE_TESTS=1. The maintenance worker must use
EMAIL_HOST=app, EMAIL_PORT=1025, EMAIL_USE_TLS=false in this isolated stack.
The private fixture is consumed by the invitation browser acceptance tests.
"""

import json
import os
from pathlib import Path
import secrets
import socketserver
from threading import Thread
import time
from email.parser import BytesParser
from email.policy import default

import requests


def main():
    if os.environ.get('COLLAB_ACCEPTANCE_TESTS') != '1':
        raise SystemExit('Use only an explicitly isolated acceptance deployment.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.label_studio')
    import django
    django.setup()
    from django.conf import settings
    from collab.models import ProjectAnnotationPolicy
    from organizations.models import Organization
    from projects.models import Project
    from tasks.models import Task
    from users.models import User

    run = secrets.token_hex(5)
    password = secrets.token_urlsafe(24)
    users = {}
    for name in ('admin', 'bob'):
        email = f'invite-{run}-{name}@example.test'
        users[name] = User.objects.create_user(email, password, username=email)
    org = Organization.create_organization(created_by=users['admin'], title='Isolated invitations')
    org.add_user(users['bob'], role='labeller')
    for user in users.values():
        user.active_organization = org
        user.save(update_fields=['active_organization'])
    project = Project.objects.create(title='Invitation acceptance', organization=org, created_by=users['admin'],
        label_config='<View><Text name="text" value="$text"/><Choices name="category" toName="text"><Choice value="a"/><Choice value="b"/></Choices></View>')
    ProjectAnnotationPolicy.objects.create(project=project, assignment_mode='manual', blind_mode=True, required_annotations=2)
    task = Task.objects.create(project=project, data={'text': 'Synthetic invitation document'})
    emails = {'alice': f'invite-{run}-alice@example.test', 'bob': users['bob'].email}
    captured = {}

    class SMTP(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(15)
            self.wfile.write(b'220 localhost acceptance SMTP\r\n')
            while command := self.rfile.readline():
                if command.upper().startswith(b'DATA'):
                    self.wfile.write(b'354 Send message\r\n')
                    body = []
                    while (line := self.rfile.readline()) not in (b'.\r\n', b''):
                        body.append(line)
                    message = BytesParser(policy=default).parsebytes(b''.join(body))
                    if str(message['To']) in emails.values():
                        captured[str(message['To'])] = message.get_content().split('#token=', 1)[1].split()[0]
                    self.wfile.write(b'250 Accepted\r\n')
                elif command.upper().startswith(b'QUIT'):
                    self.wfile.write(b'221 Bye\r\n')
                    break
                else:
                    self.wfile.write(b'250 OK\r\n')

    base = 'http://nginx:8085'
    session = requests.Session()
    session.get(base + '/user/login/', timeout=30).raise_for_status()
    response = session.post(base + '/user/login/', data={
        'email': users['admin'].email, 'password': password,
        'csrfmiddlewaretoken': session.cookies['csrftoken'],
    }, headers={'Referer': base + '/user/login/'}, timeout=30)
    response.raise_for_status()
    assert session.get(base + '/api/current-user/whoami', timeout=30).json()['id'] == users['admin'].pk
    response = session.post(base + f'/api/extensions/v1/projects/{project.pk}/publish', json={},
        headers={'Referer': base + '/', 'X-CSRFToken': session.cookies['csrftoken']}, timeout=30)
    assert response.status_code == 200, f'Campaign publication HTTP {response.status_code}'

    with socketserver.TCPServer(('0.0.0.0', 1025), SMTP) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = session.post(base + f'/api/extensions/v1/projects/{project.pk}/invitations', json={
                'emails': list(emails.values()), 'task_ids': [task.pk],
            }, headers={'Referer': base + '/', 'X-CSRFToken': session.cookies['csrftoken']}, timeout=30)
            assert response.status_code == 201, f'Invitation creation HTTP {response.status_code}'
            deadline = time.monotonic() + 100
            while len(captured) != 2 and time.monotonic() < deadline:
                time.sleep(1)
            assert set(captured) == set(emails.values()), 'Both messages delivered by the maintenance worker'
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert not User.objects.filter(email=emails['alice']).exists()
    fixture = {'project': project.pk, 'task': task.pk, 'password': password,
               'admin': users['admin'].email, 'emails': emails,
               'links': {name: settings.HOSTNAME.rstrip('/') + '/user/invitation/#token=' + captured[email]
                         for name, email in emails.items()}}
    path = Path(settings.BASE_DATA_DIR) / 'invitation-acceptance-fixture.json'
    path.write_text(json.dumps(fixture), encoding='utf-8')
    path.chmod(0o600)
    print('Real HTTP reservation and two maintenance-worker SMTP deliveries passed; private browser fixture saved.')


if __name__ == '__main__':
    main()
