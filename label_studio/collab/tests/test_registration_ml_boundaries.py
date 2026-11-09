"""Regression tests for registration and native ML administrative boundaries."""

import json
from urllib.parse import urlencode

import pytest
from collab.tests.test_blind_mode import annotate, client_for, env, set_policy  # noqa: F401
from ml.api_connector import MLApi, MLApiResult
from ml.models import MLBackend
from rest_framework.test import APIClient
from users.models import User

pytestmark = pytest.mark.django_db


def register(email, invite=None):
    client = APIClient(enforce_csrf_checks=True)
    query = {'enforce_csrf_checks': 'true'}
    if invite:
        query['token'] = invite
    path = '/user/signup/?' + urlencode(query)
    assert client.get(path).status_code == 200
    response = client.post(
        path,
        {
            'email': email,
            'password': 'Isolated-Registration-2026!m4X9',
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
        },
        HTTP_REFERER='http://testserver' + path,
    )
    return client, response


def test_only_initial_registration_bootstraps_administration(settings):
    settings.DISABLE_SIGNUP_WITHOUT_LINK = False
    _, first = register('first-owner@example.test')
    assert first.status_code == 302
    owner = User.objects.get(email='first-owner@example.test')
    assert owner.get_organization_role() == 'admin'
    _, second = register('second-member@example.test')
    assert second.status_code == 302
    member = User.objects.get(email='second-member@example.test')
    assert member.active_organization_id == owner.active_organization_id
    assert member.get_organization_role() == 'labeller'


def test_open_registration_cannot_read_existing_blind_results(env, settings):
    settings.DISABLE_SIGNUP_WITHOUT_LINK = False
    set_policy(env['project'], blind_mode=True)
    annotation = annotate(env, env['b'], 'b')
    client, response = register('new-labeller@example.test')
    assert response.status_code == 302
    assert User.objects.get(email='new-labeller@example.test').get_organization_role() == 'labeller'
    assert client.get(f'/api/annotations/{annotation.pk}/').status_code == 403
    assert client.post('/api/projects/', {'title': 'Forbidden'}, format='json').status_code == 403


def test_invitation_only_registration_still_requires_a_valid_invitation(env, settings):
    settings.DISABLE_SIGNUP_WITHOUT_LINK = True
    _, rejected = register('uninvited@example.test')
    assert rejected.status_code == 403
    assert not User.objects.filter(email='uninvited@example.test').exists()
    _, accepted = register('invited@example.test', env['project'].organization.token)
    assert accepted.status_code == 302
    assert User.objects.get(email='invited@example.test').get_organization_role() == 'labeller'


def test_registration_requires_csrf_even_when_open(env, settings):
    settings.DISABLE_SIGNUP_WITHOUT_LINK = False
    client = APIClient(enforce_csrf_checks=True)
    path = '/user/signup/?enforce_csrf_checks=true'
    assert client.get(path).status_code == 200
    response = client.post(path, {
        'email': 'missing-csrf@example.test', 'password': 'Isolated-Registration-2026!m4X9',
    })
    assert response.status_code == 403
    assert not User.objects.filter(email='missing-csrf@example.test').exists()


@pytest.fixture
def ml_transport(monkeypatch):
    calls = []

    def request(self, endpoint, request=None, **kwargs):
        calls.append((endpoint, request))
        return MLApiResult(response={'results': [], 'model_version': 'test'})

    monkeypatch.setattr(MLApi, '_request', request)
    monkeypatch.setattr('ml.serializers.validate_upload_url', lambda *args, **kwargs: None)
    return calls


def test_ml_setup_does_not_delegate_the_project_owner_credential(env, ml_transport):
    response = client_for(env['admin']).post(
        '/api/ml/',
        {'project': env['project'].pk, 'url': 'https://ml.example.test', 'title': 'Trusted ML'},
        format='json',
    )
    assert response.status_code == 201
    setups = [body for endpoint, body in ml_transport if endpoint == 'setup']
    assert setups
    assert all('access_token' not in body for body in setups)


@pytest.mark.parametrize('scopes', [['projects:write'], ['projects:write', 'results:read']])
def test_machine_project_scopes_do_not_delegate_ml_configuration_or_diagnostics(env, ml_transport, scopes):
    created = client_for(env['admin']).post(
        '/api/extensions/v1/service-accounts',
        {'name': 'Project agent', 'scopes': scopes, 'project_ids': [env['project'].pk]},
        format='json',
    )
    assert created.status_code == 201
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Service ' + created.data['token'])
    backend = MLBackend.objects.create(project=env['project'], url='https://ml.example.test')
    assert client.post(
        '/api/ml/', {'project': env['project'].pk, 'url': 'https://new.example.test'}, format='json'
    ).status_code == 403
    assert client.patch(f'/api/ml/{backend.pk}', {'url': 'https://new.example.test'}, format='json').status_code == 403
    assert client.delete(f'/api/ml/{backend.pk}').status_code == 403
    assert client.post(f'/api/ml/{backend.pk}/train', {}, format='json').status_code == 403
    assert client.post(f'/api/ml/{backend.pk}/predict/test?random=true', {}, format='json').status_code == 403
    assert ml_transport == []


@pytest.mark.parametrize('role', ['a', 'qa'])
def test_ml_debug_and_training_are_not_reviewer_or_labeller_actions(env, ml_transport, role):
    backend = MLBackend.objects.create(project=env['project'], url='https://ml.example.test')
    client = client_for(env[role])
    assert client.post(f'/api/ml/{backend.pk}/predict/test?random=true', {}, format='json').status_code == 403
    assert client.post(f'/api/ml/{backend.pk}/train', {}, format='json').status_code == 403
    assert ml_transport == []


def test_admin_ml_debug_keeps_predictions_but_does_not_echo_source_credentials(env, ml_transport):
    env['project'].task_data_login = 'synthetic-source-login'
    env['project'].task_data_password = 'synthetic-source-password'
    env['project'].save(update_fields=['task_data_login', 'task_data_password'])
    backend = MLBackend.objects.create(project=env['project'], url='https://ml.example.test')
    annotation = annotate(env, env['b'], 'b')
    response = client_for(env['admin']).post(f'/api/ml/{backend.pk}/predict/test?random=true', {}, format='json')
    assert response.status_code == 200
    assert annotation.pk in {item['id'] for item in response.data['task']['annotations']}
    assert 'synthetic-source-login' not in json.dumps(response.data)
    assert 'synthetic-source-password' not in json.dumps(response.data)
    sent = next(body for endpoint, body in ml_transport if endpoint == 'predict')
    assert sent['params']['password'] == 'synthetic-source-password'


@pytest.mark.parametrize('role', ['a', 'qa', 'service'])
def test_editor_ml_discovery_has_no_configuration_secrets_or_external_effects(env, ml_transport, role):
    backend = MLBackend.objects.create(
        project=env['project'], url='https://ml.example.test/private-path', is_interactive=True,
        basic_auth_user='synthetic-backend-login', basic_auth_pass='synthetic-backend-password',
        extra_params={'credential': 'synthetic-backend-key'},
    )
    if role == 'service':
        account = client_for(env['admin']).post('/api/extensions/v1/service-accounts', {
            'name': 'Metadata reader', 'scopes': ['projects:read'], 'project_ids': [env['project'].pk],
        }, format='json')
        assert account.status_code == 201
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Service ' + account.data['token'])
    else:
        client = client_for(env[role])
    listing = client.get(f'/api/ml/?project={env["project"].pk}')
    detail = client.get(f'/api/ml/{backend.pk}')
    assert listing.status_code == detail.status_code == 200
    expected = {'id', 'title', 'is_interactive', 'state', 'model_version'}
    assert set(listing.data[0]) == set(detail.data) == expected
    assert listing.data[0]['id'] == backend.pk
    assert listing.data[0]['is_interactive'] is True
    assert ml_transport == []


def test_admin_can_read_archived_ml_configuration_without_refreshing_backend(env, ml_transport):
    from collab.models import ProjectAnnotationPolicy

    backend = MLBackend.objects.create(project=env['project'], url='https://ml.example.test')
    ProjectAnnotationPolicy.objects.create(project=env['project'], is_archived=True)
    client = client_for(env['admin'])
    assert client.get(f'/api/ml/?project={env["project"].pk}').status_code == 200
    detail = client.get(f'/api/ml/{backend.pk}')
    assert detail.status_code == 200 and detail.data['url'] == backend.url
    assert ml_transport == []
