"""Regression coverage for native API boundaries and the machine protocol."""

from unittest.mock import Mock

import pytest
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema

from collab.models import AuditEvent, IdempotencyRecord, WebhookDelivery, WebhookSubscription
from collab.tests.test_agent_security import service
from collab.tests.test_blind_mode import CONFIG, client_for, env, result  # noqa: F401
from organizations.models import OrganizationMember
from projects.models import Project
from session_policy.models import SessionTimeoutPolicy
from tasks.models import Task

pytestmark = pytest.mark.django_db


def test_annotation_activity_does_not_restore_a_revoked_account(env, monkeypatch):
    from collab import services
    from users.models import User

    original = services.on_annotation_created

    def revoke_after_submission(annotation, user):
        original(annotation, user)
        # The request still holds its previously authenticated user instance.
        User.objects.filter(pk=user.pk).update(is_active=False)

    monkeypatch.setattr(services, 'on_annotation_created', revoke_after_submission)
    response = client_for(env['a']).post(
        f'/api/tasks/{env["task"].pk}/annotations/', {'result': result('a')}, format='json'
    )
    assert response.status_code == 201
    env['a'].refresh_from_db()
    assert env['a'].is_active is False


def test_service_authentication_populates_native_user_organization_and_fsm_context(env, monkeypatch):
    from collab.authentication import ServiceTokenAuthentication
    from core.current_request import CurrentContext
    from rest_framework.test import APIRequestFactory

    _, account, token = service(env, ['progress:read'])
    CurrentContext.clear()
    monkeypatch.setattr('core.feature_flags.flag_set', lambda flag, user=None, **kwargs: getattr(user, 'pk', None) == account.user_id)
    try:
        request = APIRequestFactory().get('/api/projects/', HTTP_AUTHORIZATION='Service ' + token)
        user, identity = ServiceTokenAuthentication().authenticate(request)
        assert user.pk == account.user_id and identity.pk == account.pk
        assert CurrentContext.get_user().pk == account.user_id
        assert CurrentContext.get_organization_id() == env['project'].organization_id
        assert CurrentContext.is_fsm_enabled()
    finally:
        CurrentContext.clear()


@pytest.mark.parametrize('invalid_result', [['not an object'], [None], 'not valid JSON'])
def test_malformed_native_annotation_results_return_validation_error_without_persisting(env, invalid_result):
    from tasks.models import Annotation

    bot, _, _ = service(env, ['annotations:write'])
    response = bot.post(f'/api/tasks/{env["task"].pk}/annotations/', {'result': invalid_result}, format='json', HTTP_IDEMPOTENCY_KEY='invalid-result')
    assert response.status_code == 400, response.data
    assert not Annotation.objects.filter(task=env['task']).exists()
    assert not IdempotencyRecord.objects.filter(key='invalid-result').exists()
    corrected = bot.post(f'/api/tasks/{env["task"].pk}/annotations/', {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='invalid-result')
    assert corrected.status_code == 201, corrected.data


def test_review_idempotency_distinguishes_revision_headers(env):
    from collab.models import AnnotationReview
    from collab.tests.test_review_agent import submitted, three_annotators

    annotation, _, _ = three_annotators(env)
    submitted(env)
    caller = client_for(env['qa'])
    payload = {'annotation': annotation.pk, 'decision': 'ACCEPT'}
    headers = {'HTTP_IDEMPOTENCY_KEY': 'one-review-revision', 'HTTP_IF_MATCH': '"1"'}
    first = caller.post('/api/extensions/v1/reviews', payload, format='json', **headers)
    replay = caller.post('/api/extensions/v1/reviews', payload, format='json', **headers)
    assert first.status_code == replay.status_code == 201, (first.data, replay.data)
    assert first.data == replay.data and replay['Idempotent-Replay'] == 'true'
    changed = caller.post('/api/extensions/v1/reviews', payload, format='json', **{**headers, 'HTTP_IF_MATCH': '"2"'})
    assert changed.status_code == 409, changed.data
    assert AnnotationReview.objects.filter(annotation=annotation).count() == 1


def test_idempotency_fingerprints_without_revision_headers_remain_compatible(env):
    import hashlib
    import json

    bot, account, _ = service(env, ['projects:write'])
    payload = {'title': 'Compatible receipt', 'label_config': CONFIG}
    first = bot.post('/api/extensions/v1/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='old-protocol')
    assert first.status_code == 201, first.data
    old_digest = hashlib.sha256(json.dumps({'data': payload, 'files': {}, 'query': []}, sort_keys=True, default=str).encode()).hexdigest()
    receipt = IdempotencyRecord.objects.get(user=account.user, key='old-protocol')
    assert receipt.request_hash == old_digest
    replay = bot.post('/api/extensions/v1/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='old-protocol')
    assert replay.status_code == 201 and replay.data == first.data


@pytest.mark.parametrize('machine', [False, True])
def test_source_edit_cannot_forge_completion_counters_or_collaborative_overlap(env, machine):
    from collab.api import apply_policy
    from collab.tests.test_blind_mode import annotate

    apply_policy(env['project'], {'required_annotations': 3}, partial=True)
    annotate(env, env['a'], 'a')
    caller = service(env, ['tasks:write'])[0] if machine else client_for(env['admin'])
    env['task'].refresh_from_db()
    original_inner_id = env['task'].inner_id
    original_updated_by = env['task'].updated_by_id
    payload = {
        'data': {'text': 'Legitimate edited source'}, 'meta': {'source_checked': True},
        'is_labeled': True, 'total_annotations': 999, 'cancelled_annotations': 777,
        'total_predictions': 888, 'comment_count': 55, 'unresolved_comment_count': 44,
        'comment_authors': [env['b'].pk], 'updated_by': env['b'].pk,
        'overlap': 1, 'inner_id': 999,
    }
    response = caller.patch(f'/api/tasks/{env["task"].pk}/', payload, format='json')
    assert response.status_code == 200, response.data
    env['task'].refresh_from_db()
    assert env['task'].data == payload['data'] and env['task'].meta == payload['meta']
    assert not env['task'].is_labeled and env['task'].overlap == 3
    assert env['task'].total_annotations == 1 and env['task'].cancelled_annotations == 0
    assert env['task'].total_predictions == env['task'].comment_count == env['task'].unresolved_comment_count == 0
    assert list(env['task'].comment_authors.all()) == []
    assert env['task'].inner_id == original_inner_id and env['task'].updated_by_id == original_updated_by


def test_legacy_per_task_overlap_configuration_remains_supported(env):
    caller = service(env, ['tasks:write'])[0]
    response = caller.patch(f'/api/tasks/{env["task"].pk}/', {'overlap': 2}, format='json')
    assert response.status_code == 200, response.data
    env['task'].refresh_from_db()
    assert env['task'].overlap == 2


@pytest.mark.parametrize('nested', [False, True])
def test_source_task_creation_cannot_seed_forged_counts(env, nested):
    from collab.api import apply_policy

    apply_policy(env['project'], {'required_annotations': 3}, partial=True)
    caller = service(env, ['imports:write'])[0]
    url = f'/api/projects/{env["project"].pk}/tasks/' if nested else '/api/tasks/'
    response = caller.post(url, {'project': env['project'].pk, 'data': {'text': 'Valid new source'},
                                'is_labeled': True, 'total_annotations': 100, 'overlap': 1, 'comment_count': 30},
                           format='json', HTTP_IDEMPOTENCY_KEY='source-computed-fields')
    assert response.status_code == 201, response.data
    created = Task.objects.get(pk=response.data['id'])
    assert not created.is_labeled and created.total_annotations == created.comment_count == 0
    assert created.overlap == 3


@pytest.mark.parametrize('other_organization', [False, True])
@pytest.mark.parametrize('operation', ['patch', 'create', 'nested-create', 'import'])
def test_task_upload_reference_requires_the_same_project(env, other_organization, operation):
    from django.core.files.base import ContentFile
    from data_import.models import FileUpload
    from organizations.models import Organization
    from users.models import User

    owner = env['admin']
    org = env['project'].organization
    if other_organization:
        owner = User.objects.create(username='upload-boundary-owner', email='upload-boundary-owner@example.test')
        org = Organization.create_organization(created_by=owner, title='Unrelated uploads')
    other = Project.objects.create(title='Foreign source files', organization=org, created_by=owner, label_config=CONFIG)
    foreign = FileUpload.objects.create(project=other, user=owner, file=ContentFile(b'private foreign source', name='foreign-source.txt'))
    own = FileUpload.objects.create(project=env['project'], user=env['admin'], file=ContentFile(b'authorized source', name='own-source.txt'))
    caller = service(env, ['tasks:write', 'imports:write'])[0]
    if operation == 'patch':
        url = f'/api/tasks/{env["task"].pk}/'
        response = caller.patch(url, {'file_upload': foreign.pk}, format='json')
        assert response.status_code == 400, response.data
        env['task'].refresh_from_db()
        assert env['task'].file_upload_id is None
        accepted = caller.patch(url, {'file_upload': own.pk}, format='json')
        assert accepted.status_code == 200, accepted.data
        env['task'].refresh_from_db()
        assert env['task'].file_upload_id == own.pk
    elif operation == 'import':
        url = f'/api/projects/{env["project"].pk}/import'
        body = {'data': {'text': 'Valid source'}, 'file_upload_id': foreign.pk}
        response = caller.post(url, [body], format='json', HTTP_IDEMPOTENCY_KEY='upload-boundary')
        assert response.status_code == 400, response.data
        accepted = caller.post(url, [{**body, 'file_upload_id': own.pk}], format='json', HTTP_IDEMPOTENCY_KEY='upload-boundary')
        assert accepted.status_code == 201, accepted.data
        assert Task.objects.filter(project=env['project'], file_upload=own).exists()
    else:
        url = '/api/tasks/' if operation == 'create' else f'/api/projects/{env["project"].pk}/tasks/'
        body = {'project': env['project'].pk, 'data': {'text': 'Valid source'}, 'file_upload': foreign.pk}
        response = caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='upload-boundary')
        assert response.status_code == 400, response.data
        accepted = caller.post(url, {**body, 'file_upload': own.pk}, format='json', HTTP_IDEMPOTENCY_KEY='upload-boundary')
        assert accepted.status_code == 201, accepted.data
        assert Task.objects.get(pk=accepted.data['id']).file_upload_id == own.pk


@pytest.mark.parametrize('role', ['a', 'qa'])
def test_only_admins_change_organization_session_policy(env, role):
    policy, _ = SessionTimeoutPolicy.objects.get_or_create(organization=env['project'].organization)
    original = policy.max_session_age
    response = client_for(env[role]).patch('/api/session-policy/', {'max_session_age': 1}, format='json')
    assert response.status_code == 403
    policy.refresh_from_db()
    assert policy.max_session_age == original
    admin = client_for(env['admin'])
    assert admin.patch('/api/session-policy/', {'max_session_age': 60}, format='json').status_code == 200
    policy.refresh_from_db()
    assert policy.max_session_age == 60
    assert client_for(env[role]).get('/api/session-policy/').status_code == 200


def test_machine_scopes_never_grant_global_session_administration(env):
    bot, _, _ = service(env, ['progress:read', 'projects:write', 'policy:write'])
    assert bot.patch('/api/session-policy/', {'max_session_age': 1}, format='json').status_code == 403
    assert bot.get('/api/session-policy/').status_code == 403
    assert not SessionTimeoutPolicy.objects.filter(organization=env['project'].organization, max_session_age=1).exists()


def test_removed_member_cannot_read_or_change_session_policy(env):
    OrganizationMember.objects.filter(user=env['a'], organization=env['project'].organization).update(deleted_at=timezone.now())
    caller = client_for(env['a'])
    assert caller.get('/api/session-policy/').status_code == 403
    assert caller.patch('/api/session-policy/', {'max_session_age': 1}, format='json').status_code == 403


def test_native_project_task_create_requires_machine_key_and_replays_once(env):
    bot, account, _ = service(env, ['imports:write'])
    url = f'/api/projects/{env["project"].pk}/tasks/'
    payload = {'data': {'text': 'Imported one time'}}
    assert bot.post(url, payload, format='json').status_code == 400
    first = bot.post(url, payload, format='json', HTTP_IDEMPOTENCY_KEY='one-project-task')
    replay = bot.post(url, payload, format='json', HTTP_IDEMPOTENCY_KEY='one-project-task')
    assert first.status_code == replay.status_code == 201, (first.data, replay.data)
    assert first.data == replay.data
    assert replay['Idempotent-Replay'] == 'true'
    assert Task.objects.filter(project=env['project'], data=payload['data']).count() == 1
    assert IdempotencyRecord.objects.filter(user=account.user, key='one-project-task').count() == 1
    changed = bot.post(url, {'data': {'text': 'Different payload'}}, format='json', HTTP_IDEMPOTENCY_KEY='one-project-task')
    assert changed.status_code == 409
    account.projects.clear()
    assert bot.post(url, payload, format='json', HTTP_IDEMPOTENCY_KEY='one-project-task').status_code in (403, 404)


def test_native_project_task_create_checks_scope_and_project(env):
    other = Project.objects.create(title='Outside machine scope', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG)
    bot, _, _ = service(env, ['imports:write'])
    assert bot.post(f'/api/projects/{other.pk}/tasks/', {'data': {'text': 'Denied'}}, format='json', HTTP_IDEMPOTENCY_KEY='foreign').status_code in (403, 404)
    reader, _, _ = service(env, ['projects:read'])
    assert reader.post(f'/api/projects/{env["project"].pk}/tasks/', {'data': {'text': 'Denied'}}, format='json', HTTP_IDEMPOTENCY_KEY='read-only').status_code == 403
    assert not Task.objects.filter(data={'text': 'Denied'}).exists()
    # Existing human integrations may still create tasks without a replay key.
    human = client_for(env['admin']).post(f'/api/projects/{env["project"].pk}/tasks/', {'data': {'text': 'Human source'}}, format='json')
    assert human.status_code == 201


def test_native_policy_fields_require_both_machine_scopes(env):
    admin = client_for(env['admin'])
    assert admin.patch(f'/api/extensions/v1/projects/{env["project"].pk}/policy', {'required_annotations': 2}, format='json').status_code == 200
    bot, account, _ = service(env, ['projects:write'])
    url = f'/api/projects/{env["project"].pk}/'
    assert bot.patch(url, {'maximum_annotations': 3}, format='json').status_code == 403
    assert bot.patch(url, {'overlap_cohort_percentage': 50}, format='json').status_code == 403
    env['project'].refresh_from_db()
    assert env['project'].maximum_annotations == 2
    assert bot.patch(url, {'description': 'Allowed project configuration'}, format='json').status_code == 200
    account.scopes = ['projects:write', 'policy:write']
    account.save(update_fields=['scopes'])
    assert bot.patch(url, {'maximum_annotations': 3}, format='json').status_code == 200
    assert bot.patch(url, {'overlap_cohort_percentage': 50}, format='json').status_code == 400
    env['project'].refresh_from_db()
    assert env['project'].maximum_annotations == env['project'].annotation_policy.required_annotations == 3


@pytest.mark.parametrize('native', [False, True])
def test_explicit_policy_on_creation_requires_scope_but_defaults_remain_available(env, native):
    bot, account, _ = service(env, ['projects:write'])
    url = '/api/projects/' if native else '/api/extensions/v1/projects'
    policy_field = {'maximum_annotations': 3} if native else {'policy': {'required_annotations': 3}}
    explicit = {'title': 'Configured by machine', 'label_config': CONFIG, **policy_field}
    assert bot.post(url, explicit, format='json', HTTP_IDEMPOTENCY_KEY='explicit-policy').status_code == 403
    assert not Project.objects.filter(title=explicit['title']).exists()
    default = bot.post(url, {'title': 'Default private campaign', 'label_config': CONFIG}, format='json', HTTP_IDEMPOTENCY_KEY='default-policy')
    assert default.status_code == 201, default.data
    assert Project.objects.get(pk=default.data['id']).annotation_policy.required_annotations == 2
    account.scopes = ['projects:write', 'policy:write']
    account.save(update_fields=['scopes'])
    created = bot.post(url, explicit, format='json', HTTP_IDEMPOTENCY_KEY='explicit-policy')
    assert created.status_code == 201, created.data
    assert Project.objects.get(pk=created.data['id']).annotation_policy.required_annotations == 3
    account.scopes = ['projects:write']
    account.save(update_fields=['scopes'])
    assert bot.post(url, explicit, format='json', HTTP_IDEMPOTENCY_KEY='explicit-policy').status_code == 403


def test_native_publication_queues_event_once_and_audits(env):
    Project.objects.filter(pk=env['project'].pk).update(is_published=False)
    sub = WebhookSubscription.objects.create(project=env['project'], url='https://example.test/hooks', secret='test-secret', events=['project.published'])
    admin = client_for(env['admin'])
    url = f'/api/projects/{env["project"].pk}/'
    assert admin.patch(url, {'is_published': True}, format='json').status_code == 200
    assert admin.patch(url, {'is_published': True}, format='json').status_code == 200
    assert WebhookDelivery.objects.filter(subscription=sub, event='project.published').count() == 1
    assert AuditEvent.objects.filter(project=env['project'], action='project.publish').count() == 1


def test_native_publication_rolls_back_if_outbox_cannot_be_written(env, monkeypatch):
    Project.objects.filter(pk=env['project'].pk).update(is_published=False)
    monkeypatch.setattr('collab.webhooks.emit', Mock(side_effect=RuntimeError('outbox unavailable')))
    response = client_for(env['admin']).patch(f'/api/projects/{env["project"].pk}/', {'is_published': True}, format='json')
    assert response.status_code == 500
    env['project'].refresh_from_db()
    assert not env['project'].is_published
    assert not AuditEvent.objects.filter(action='project.publish').exists()


def test_global_openapi_validates_and_exposes_native_machine_contracts():
    schema = SchemaGenerator().get_schema(public=True)
    validate_schema(schema)
    task_create = schema['paths']['/api/projects/{id}/tasks/']['post']
    assert any(p['name'] == 'Idempotency-Key' for p in task_create['parameters'])
    assert 'requestBody' in task_create
    review = schema['paths']['/api/annotations/{id}/review']['post']
    assert {'Idempotency-Key', 'If-Match'} <= {p['name'] for p in review['parameters']}
    assert '409' in review['responses']
    assert 'ETag' in review['responses']['200']['headers']
    def request_properties(path):
        request = schema['paths'][path]['post']['requestBody']['content']['application/json']['schema']
        return schema['components']['schemas'][request['$ref'].rsplit('/', 1)[-1]]['properties']
    assert {'url', 'events'} <= set(request_properties('/api/extensions/v1/projects/{id}/webhooks'))
    assert {'url', 'project', 'actions', 'headers', 'send_payload'} <= set(request_properties('/api/webhooks/'))
