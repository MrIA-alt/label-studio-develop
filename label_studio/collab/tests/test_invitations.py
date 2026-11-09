from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.utils import timezone
from organizations.models import Organization, OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.test import APIClient
from rest_framework.exceptions import APIException, ValidationError
from tasks.models import Task
from users.models import User

from collab import invitations
from collab.models import ProjectAnnotationPolicy, ProjectInvitation, TaskAssignment
from collab.microsoft_mail import Microsoft365Backend

pytestmark = pytest.mark.django_db
PASSWORD = 'Synthetic!Invitation2026'


@pytest.fixture
def env(settings):
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.HOSTNAME = 'https://annotations.example.test'
    settings.DEFAULT_FROM_EMAIL = 'annotations@example.test'
    admin = User.objects.create_user('invite-admin@example.test', PASSWORD, username='invite-admin')
    org = Organization.create_organization(created_by=admin, title='Invitation tests')
    admin.active_organization = org
    admin.save(update_fields=['active_organization'])
    project = Project.objects.create(title='Campaign', organization=org, created_by=admin)
    ProjectAnnotationPolicy.objects.create(project=project, assignment_mode='manual', blind_mode=True, assignment_ttl_hours=1)
    task = Task.objects.create(project=project, data={'text': 'Test'})
    return admin, org, project, task


def reserve(env, email='invitee@example.test'):
    admin, org, project, task = env
    return invitations.reserve(project, [email], [task.pk], admin)[0]


def deliver():
    assert invitations.drain_invitations()['sent'] == 1
    return mail.outbox[-1].body.split('#token=', 1)[1].split()[0]


def test_reservation_delivery_activation_and_single_use(env):
    row = reserve(env)
    assert not User.objects.filter(email=row.email).exists()
    assert not TaskAssignment.objects.filter(task=env[3]).exists()
    token = deliver()
    row.refresh_from_db()
    assert row.token_digest == invitations.digest(token) and token not in row.token_digest
    assert mail.outbox[-1].to == [row.email]
    user, project = invitations.accept(token, AnonymousUser(), PASSWORD, PASSWORD)
    assert user.check_password(PASSWORD) and user.active_organization_id == env[1].pk
    assert OrganizationMember.objects.get(user=user, organization=env[1]).role == 'labeller'
    assignment = TaskAssignment.objects.get(user=user, task=env[3])
    assert assignment.expires_at > timezone.now() + timedelta(minutes=59)
    assert project.pk == env[2].pk
    with pytest.raises(ValidationError):
        invitations.accept(token, user)


def test_repeated_reservation_deduplicates_and_preserves_existing_members(env):
    user = User.objects.create_user('existing@example.test', PASSWORD, username='existing', active_organization=env[1])
    env[1].add_user(user, role='labeller')
    first = reserve(env)
    assert reserve(env).pk == first.pk
    assert ProjectInvitation.objects.count() == 1
    assert first.tasks.count() == 1
    assert ProjectMember.objects.filter(project=env[2], user=user, enabled=True).exists()
    deliver()
    assert invitations.drain_invitations()['sent'] == 0


def test_existing_account_requires_its_own_login_and_password_is_preserved(env):
    user = User.objects.create_user('invitee@example.test', PASSWORD, username='existing', active_organization=env[1])
    env[1].add_user(user, role='labeller')
    reserve(env)
    token = deliver()
    with pytest.raises(ValidationError):
        invitations.accept(token, AnonymousUser(), 'Different!Password123', 'Different!Password123')
    with pytest.raises(ValidationError):
        invitations.accept(token, env[0])
    invitations.accept(token, user)
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


@pytest.mark.parametrize('change', ['expiry', 'cancel', 'archive', 'creator_revoked'])
def test_invalid_or_revoked_invitation_cannot_create_account(env, change):
    row = reserve(env)
    token = deliver()
    if change == 'expiry':
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save()
    elif change == 'cancel':
        row.status = 'cancelled'
        row.save()
    elif change == 'archive':
        ProjectAnnotationPolicy.objects.filter(project=env[2]).update(is_archived=True)
    else:
        OrganizationMember.objects.filter(user=env[0], organization=env[1]).update(role='labeller')
    with pytest.raises(APIException):
        invitations.accept(token, AnonymousUser(), PASSWORD, PASSWORD)
    assert not User.objects.filter(email=row.email).exists()
    assert TaskAssignment.objects.count() == 0


def test_project_membership_revocation_after_invitation_is_respected(env):
    user = User.objects.create_user('invitee@example.test', PASSWORD, username='existing', active_organization=env[1])
    env[1].add_user(user, role='labeller')
    row = reserve(env)
    assert row.had_project_membership
    token = deliver()
    ProjectMember.objects.filter(project=env[2], user=user).update(enabled=False)
    with pytest.raises(ValidationError):
        invitations.accept(token, user)


def test_activation_rolls_back_when_workload_cap_is_exceeded(env):
    row = reserve(env)
    token = deliver()
    ProjectAnnotationPolicy.objects.filter(project=env[2]).update(max_active_tasks_per_user=1)
    task = Task.objects.create(project=env[2], data={'text': 'Second'})
    row.tasks.add(task)
    with pytest.raises(ValidationError):
        invitations.accept(token, AnonymousUser(), PASSWORD, PASSWORD)
    assert not User.objects.filter(email=row.email).exists()
    assert TaskAssignment.objects.count() == 0
    row.refresh_from_db()
    assert row.status == 'pending'


def test_mail_failures_are_sanitized_and_respect_backoff(env):
    row = reserve(env)
    with patch('collab.invitations.send_mail', side_effect=RuntimeError('private SMTP credential')) as send:
        assert invitations.drain_invitations()['failed'] == 1
        assert invitations.drain_invitations()['failed'] == 0
        assert send.call_count == 1
    row.refresh_from_db()
    assert row.error_code == 'smtp_error' and row.attempts == 1
    assert row.token_digest == '' and row.next_attempt_at > timezone.now()


def test_unconfigured_or_console_mail_never_marks_delivery_sent(env, settings):
    row = reserve(env)
    settings.EMAIL_BACKEND = 'django.core.mail.backends.dummy.EmailBackend'
    assert invitations.drain_invitations()['sent'] == 0
    row.refresh_from_db()
    assert row.delivery_status == 'blocked' and row.attempts == 0
    settings.EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    assert not invitations.smtp_ready()


def test_admin_api_validates_scopes_tasks_and_never_returns_tokens(env):
    client = APIClient()
    client.force_authenticate(user=env[0])
    url = f'/api/extensions/v1/projects/{env[2].pk}/invitations'
    response = client.post(url, {'emails': ['INVITEE@example.test', 'invitee@example.test'], 'task_ids': [env[3].pk]}, format='json')
    assert response.status_code == 201
    assert len(response.data) == 1 and response.data[0]['email'] == 'invitee@example.test'
    assert 'token' not in str(response.data)
    token = deliver()
    response = client.get(url)
    assert response.status_code == 200 and token not in str(response.data)
    other = Project.objects.create(title='Other', organization=env[1], created_by=env[0])
    foreign = Task.objects.create(project=other, data={})
    assert client.post(url, {'emails': ['other@example.test'], 'task_ids': [foreign.pk]}, format='json').status_code == 400
    actor = User.objects.create_user('qa@example.test', PASSWORD, username='qa', active_organization=env[1])
    env[1].add_user(actor, role='qa')
    client.force_authenticate(user=actor)
    assert client.get(url).status_code == 403
    assert client.post(url, {'emails': ['a@example.test'], 'task_ids': [env[3].pk]}, format='json').status_code == 403


def test_resend_invalidates_old_link_and_cancel_blocks_acceptance(env):
    row = reserve(env)
    token = deliver()
    client = APIClient()
    client.force_authenticate(user=env[0])
    url = f'/api/extensions/v1/projects/{env[2].pk}/invitations/{row.pk}'
    assert client.post(url + '/resend', {}, format='json').status_code == 200
    with pytest.raises(ValidationError):
        invitations.accept(token, AnonymousUser(), PASSWORD, PASSWORD)
    replacement = deliver()
    assert replacement != token
    assert client.post(url + '/cancel', {}, format='json').status_code == 200
    with pytest.raises(ValidationError):
        invitations.accept(replacement, AnonymousUser(), PASSWORD, PASSWORD)


def test_public_activation_uses_csrf_and_native_login(env):
    row = reserve(env)
    token = deliver()
    client = Client(enforce_csrf_checks=True)
    path = '/user/invitation/'
    page = client.get(path)
    assert page.status_code == 200
    assert page['Referrer-Policy'] == 'same-origin'
    payload = {'token': token, 'password': PASSWORD, 'confirmation': PASSWORD}
    assert client.post(path, payload).status_code == 403
    response = client.post(path, payload, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
    assert response.status_code == 200 and b'Ouvrir mes' in response.content
    assert client.get('/api/current-user/whoami').json()['email'] == row.email


def test_microsoft_oauth_uses_tls_and_never_basic_auth(settings):
    settings.M365_TENANT_ID = '11111111-1111-1111-1111-111111111111'
    settings.M365_CLIENT_ID = 'application'
    settings.M365_CLIENT_SECRET = 'synthetic-secret'
    settings.EMAIL_HOST_USER = 'annotations@example.test'
    backend = Microsoft365Backend()
    connection = Mock()
    def opened():
        backend.connection = connection
        return True
    response = Mock(status_code=200)
    response.json.return_value = {'access_token': 'synthetic-oauth-token'}
    with patch('collab.microsoft_mail.requests.post', return_value=response) as post, \
            patch('django.core.mail.backends.smtp.EmailBackend.open', side_effect=opened):
        assert backend.open()
    assert backend.use_tls and not backend.use_ssl and not backend.username
    connection.login.assert_not_called()
    assert [call[0] for call in connection.mock_calls][:2] == ['ehlo_or_helo_if_needed', 'auth']
    mechanism, auth = connection.auth.call_args.args
    assert mechanism == 'XOAUTH2' and 'Bearer synthetic-oauth-token' in auth()
    assert auth(b'error') == ''
    assert post.call_args.kwargs['data']['scope'] == 'https://outlook.office365.com/.default'


def test_microsoft_errors_do_not_expose_provider_response(settings):
    settings.M365_TENANT_ID = '11111111-1111-1111-1111-111111111111'
    settings.M365_CLIENT_ID = 'application'
    settings.M365_CLIENT_SECRET = 'synthetic-secret'
    settings.EMAIL_HOST_USER = 'annotations@example.test'
    with patch('collab.microsoft_mail.requests.post', side_effect=RuntimeError('synthetic-secret private provider data')):
        with pytest.raises(ImproperlyConfigured) as error:
            Microsoft365Backend().open()
    assert 'synthetic-secret' not in str(error.value)


def test_real_smtp_transport_delivers_activation_message(env, settings):
    import socketserver
    from threading import Thread
    from email.parser import BytesParser
    from email.policy import default

    received = []

    class SMTP(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'220 localhost test SMTP\r\n')
            while True:
                command = self.rfile.readline()
                if not command:
                    break
                if command.upper().startswith(b'DATA'):
                    self.wfile.write(b'354 Send message\r\n')
                    body = []
                    while True:
                        line = self.rfile.readline()
                        if line in (b'.\r\n', b''):
                            break
                        body.append(line)
                    received.append(b''.join(body))
                    self.wfile.write(b'250 Accepted\r\n')
                elif command.upper().startswith(b'QUIT'):
                    self.wfile.write(b'221 Bye\r\n')
                    break
                else:
                    self.wfile.write(b'250 OK\r\n')

    with socketserver.TCPServer(('127.0.0.1', 0), SMTP) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        settings.EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
        settings.EMAIL_HOST = '127.0.0.1'
        settings.EMAIL_PORT = server.server_address[1]
        settings.EMAIL_HOST_USER = ''
        settings.EMAIL_HOST_PASSWORD = ''
        settings.EMAIL_USE_TLS = False
        settings.EMAIL_USE_SSL = False
        try:
            row = reserve(env)
            assert invitations.drain_invitations()['sent'] == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert len(received) == 1
    message = BytesParser(policy=default).parsebytes(received[0])
    assert str(message['To']) == row.email
    body = message.get_content()
    token = body.split('#token=', 1)[1].split()[0]
    row.refresh_from_db()
    assert row.token_digest == invitations.digest(token)
    assert '/user/invitation/#token=' in body


def test_invitation_cookie_api_requires_csrf(env):
    client = Client(enforce_csrf_checks=True)
    page = client.get('/user/login/')
    assert page.status_code == 200
    csrf = client.cookies['csrftoken'].value
    response = client.post('/user/login/', {'email': env[0].email, 'password': PASSWORD,
                                         'csrfmiddlewaretoken': csrf}, HTTP_REFERER='http://testserver/user/login/')
    assert response.status_code == 302
    url = f'/api/extensions/v1/projects/{env[2].pk}/invitations'
    payload = {'emails': ['invitee@example.test'], 'task_ids': [env[3].pk]}
    assert client.post(url, payload, content_type='application/json').status_code == 403
    assert client.post(url, payload, content_type='application/json',
                       HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value).status_code == 201
