"""Destructive native routes require explicit rights and retry safely."""

import pytest
from django.core.files.base import ContentFile

from collab.models import IdempotencyRecord, ProjectAnnotationPolicy, WebhookSubscription
from collab.tests.test_agent_security import service
from collab.tests.test_blind_mode import CONFIG, annotate, client_for, env, result  # noqa: F401
from data_import.models import FileUpload
from projects.models import Project
from tasks.models import Annotation, Prediction, Task

pytestmark = pytest.mark.django_db
REIMPORT_SCOPES = ['imports:write', 'tasks:delete', 'annotations:delete']
FILE_DELETE_SCOPES = ['projects:write', 'tasks:delete']
PREDICTION_DELETE_SCOPES = ['projects:write', 'predictions:delete']


@pytest.mark.django_db(transaction=True)
def test_idempotency_user_lock_allows_actor_foreign_keys_in_other_transactions(env):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from django.db import close_old_connections, connection, connections
    from django.http import QueryDict
    from rest_framework.response import Response
    from collab.idempotency import run_idempotent
    from collab.models import AuditEvent

    if connection.vendor != 'postgresql':
        pytest.skip('Requires PostgreSQL row/FK lock semantics.')
    actor_id, org_id, project_id = env['admin'].pk, env['project'].organization_id, env['project'].pk
    request = SimpleNamespace(user=env['admin'], headers={'Idempotency-Key': 'actor-fk'}, data={}, FILES={}, query_params=QueryDict(''))
    executor = ThreadPoolExecutor(max_workers=1)

    def record_other_transaction():
        close_old_connections()
        try:
            return AuditEvent.objects.create(actor_id=actor_id, organization_id=org_id, project_id=project_id, action='test.actor_fk').pk
        finally:
            connections['default'].close()

    def callback():
        future = executor.submit(record_other_transaction)
        future.result(timeout=10)  # FOR UPDATE on User would block this deferred FK check until rollback.
        return Response({'recorded': True}, status=201)

    try:
        assert run_idempotent(request, 'actor-fk-test', callback).status_code == 201
    finally:
        # A regression timeout exits/rolls back run_idempotent first, releasing
        # the User lock before waiting for the worker's transaction to finish.
        executor.shutdown(wait=True)
    assert AuditEvent.objects.filter(action='test.actor_fk').count() == 1


def uploaded_source(env):
    upload = FileUpload.objects.create(
        project=env['project'], user=env['admin'],
        file=ContentFile(b'[{"text":"reloaded source"}]', name='source.json'),
    )
    Task.objects.filter(pk=env['task'].pk).update(file_upload=upload)
    return upload


def annotated_source(env):
    upload = uploaded_source(env)
    return upload, annotate(env, env['a'], 'a')


def assert_original(env, annotation):
    assert Task.objects.filter(pk=env['task'].pk, data={'text': 'hello'}).exists()
    assert Annotation.objects.filter(pk=annotation.pk).exists()


@pytest.mark.parametrize('role', ['a', 'qa'])
def test_human_reimport_is_admin_only(env, role):
    upload, annotation = annotated_source(env)
    response = client_for(env[role]).post(f'/api/projects/{env["project"].pk}/reimport',
                                         {'file_upload_ids': [upload.pk]}, format='json')
    assert response.status_code == 403
    assert_original(env, annotation)


@pytest.mark.parametrize('scopes', [
    ['imports:write'], ['imports:write', 'tasks:delete'], ['imports:write', 'annotations:delete'],
    ['tasks:delete', 'annotations:delete'],
])
def test_reimport_requires_every_destructive_service_scope(env, scopes):
    upload, annotation = annotated_source(env)
    caller, _, _ = service(env, scopes)
    response = caller.post(f'/api/projects/{env["project"].pk}/reimport', {'file_upload_ids': [upload.pk]},
                           format='json', HTTP_IDEMPOTENCY_KEY='denied-reimport')
    assert response.status_code == 403
    assert_original(env, annotation)
    assert not IdempotencyRecord.objects.filter(key='denied-reimport').exists()


@pytest.mark.parametrize('machine', [False, True])
def test_explicitly_authorized_reimport_replaces_sources_and_replays_once(env, machine):
    upload, annotation = annotated_source(env)
    caller = service(env, REIMPORT_SCOPES)[0] if machine else client_for(env['admin'])
    url, body = f'/api/projects/{env["project"].pk}/reimport', {'file_upload_ids': [upload.pk]}
    if machine:
        assert caller.post(url, body, format='json').status_code == 400
        assert_original(env, annotation)
    first = caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='authorized-reimport')
    replay = caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='authorized-reimport')
    assert first.status_code == replay.status_code == 201, (first.data, replay.data)
    assert first.json() == replay.json()
    assert Task.objects.get(project=env['project']).data == {'text': 'reloaded source'}
    assert not Annotation.objects.filter(pk=annotation.pk).exists()
    assert IdempotencyRecord.objects.filter(key='authorized-reimport').count() == 1


def test_reimport_does_not_bypass_project_revocation_or_archival(env):
    upload, annotation = annotated_source(env)
    caller, account, _ = service(env, REIMPORT_SCOPES)
    url, body = f'/api/projects/{env["project"].pk}/reimport', {'file_upload_ids': [upload.pk]}
    account.projects.clear()
    assert caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='restricted-reimport').status_code in (403, 404)
    account.projects.add(env['project'])
    ProjectAnnotationPolicy.objects.create(project=env['project'], is_archived=True)
    assert caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='restricted-reimport').status_code == 403
    assert_original(env, annotation)


def test_reimport_rejects_unknown_uploads_before_replacing_any_task(env):
    upload, annotation = annotated_source(env)
    caller, _, _ = service(env, REIMPORT_SCOPES)
    response = caller.post(f'/api/projects/{env["project"].pk}/reimport',
                           {'file_upload_ids': [upload.pk, 9999999]}, format='json',
                           HTTP_IDEMPOTENCY_KEY='unknown-source-reimport')
    assert response.status_code == 400
    assert_original(env, annotation)


@pytest.mark.parametrize('role', ['a', 'qa'])
def test_bulk_upload_delete_is_admin_only_for_humans(env, role):
    upload = uploaded_source(env)
    response = client_for(env[role]).delete(f'/api/projects/{env["project"].pk}/file-uploads', {}, format='json')
    assert response.status_code == 403
    assert FileUpload.objects.filter(pk=upload.pk).exists()


@pytest.mark.parametrize('scopes', [['projects:write'], ['tasks:delete']])
def test_bulk_upload_delete_requires_both_service_scopes(env, scopes):
    upload = uploaded_source(env)
    caller, _, _ = service(env, scopes)
    assert caller.delete(f'/api/projects/{env["project"].pk}/file-uploads', {}, format='json').status_code == 403
    assert FileUpload.objects.filter(pk=upload.pk).exists()


@pytest.mark.parametrize('machine', [False, True])
def test_authorized_bulk_upload_delete_preserves_task_and_annotation(env, machine):
    upload, annotation = annotated_source(env)
    other = FileUpload.objects.create(project=env['project'], user=env['admin'], file=ContentFile(b'other', name='other.txt'))
    caller = service(env, FILE_DELETE_SCOPES)[0] if machine else client_for(env['admin'])
    response = caller.delete(f'/api/projects/{env["project"].pk}/file-uploads', {'file_upload_ids': [upload.pk]}, format='json')
    assert response.status_code == 200, response.data
    assert not FileUpload.objects.filter(pk=upload.pk).exists()
    assert FileUpload.objects.filter(pk=other.pk).exists()
    env['task'].refresh_from_db()
    assert env['task'].file_upload_id is None
    assert Annotation.objects.filter(pk=annotation.pk).exists()


def test_bulk_upload_delete_stays_inside_project_scope_and_respects_archival(env):
    upload = uploaded_source(env)
    caller, account, _ = service(env, FILE_DELETE_SCOPES)
    url = f'/api/projects/{env["project"].pk}/file-uploads'
    account.projects.clear()
    assert caller.delete(url, {}, format='json').status_code in (403, 404)
    account.projects.add(env['project'])
    ProjectAnnotationPolicy.objects.create(project=env['project'], is_archived=True)
    assert caller.delete(url, {}, format='json').status_code == 403
    assert FileUpload.objects.filter(pk=upload.pk).exists()


@pytest.mark.parametrize('body', [{'file_upload_ids': 'all'}, []])
def test_invalid_bulk_upload_delete_body_does_not_delete(env, body):
    upload = uploaded_source(env)
    response = client_for(env['admin']).delete(f'/api/projects/{env["project"].pk}/file-uploads', body, format='json')
    assert response.status_code == 400
    assert FileUpload.objects.filter(pk=upload.pk).exists()


def prediction_body(env, version='agent'):
    return [{'task': env['task'].pk, 'result': result('a'), 'model_version': version}]


@pytest.mark.parametrize('memory_efficient', [False, True])
def test_prediction_import_requires_machine_key_and_deduplicates(env, monkeypatch, memory_efficient):
    import data_import.api as api

    flag = api.flag_set
    monkeypatch.setattr(api, 'flag_set', lambda name, *a, **kw: memory_efficient if name == 'fflag_fix_back_4620_memory_efficient_predictions_import_08012025_short' else flag(name, *a, **kw))
    caller, account, _ = service(env, ['imports:write'])
    url, body = f'/api/projects/{env["project"].pk}/import/predictions', prediction_body(env)
    assert caller.post(url, body, format='json').status_code == 400
    assert not Prediction.objects.filter(project=env['project']).exists()
    first = caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='predictions-once')
    replay = caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='predictions-once')
    assert first.status_code == replay.status_code == 201, (first.data, replay.data)
    assert first.data == replay.data and replay['Idempotent-Replay'] == 'true'
    assert Prediction.objects.filter(task=env['task']).count() == 1
    changed = caller.post(url, prediction_body(env, 'changed'), format='json', HTTP_IDEMPOTENCY_KEY='predictions-once')
    assert changed.status_code == 409
    assert IdempotencyRecord.objects.filter(user=account.user, key='predictions-once').count() == 1
    account.projects.clear()
    assert caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='predictions-once').status_code in (403, 404)


def test_prediction_import_bad_later_batch_rolls_back_and_can_retry(env, monkeypatch, settings):
    import data_import.api as api

    flag = api.flag_set
    monkeypatch.setattr(api, 'flag_set', lambda name, *a, **kw: True if name == 'fflag_fix_back_4620_memory_efficient_predictions_import_08012025_short' else flag(name, *a, **kw))
    settings.PREDICTION_IMPORT_BATCH_SIZE = 1
    caller, _, _ = service(env, ['imports:write'])
    url = f'/api/projects/{env["project"].pk}/import/predictions'
    bad = [*prediction_body(env), {'task': 9999999, 'result': result('a')}]
    response = caller.post(url, bad, format='json', HTTP_IDEMPOTENCY_KEY='prediction-batches')
    assert response.status_code == 400, response.data
    assert not Prediction.objects.filter(project=env['project']).exists()
    assert not IdempotencyRecord.objects.filter(key='prediction-batches').exists()
    good = [*prediction_body(env), *prediction_body(env, 'second')]
    assert caller.post(url, good, format='json', HTTP_IDEMPOTENCY_KEY='prediction-batches').status_code == 201
    assert Prediction.objects.filter(project=env['project']).count() == 2


def test_prediction_import_has_a_distinct_idempotency_namespace(env):
    caller, account, _ = service(env, ['imports:write'])
    body = [{**prediction_body(env)[0], 'text': 'Also a valid flat source task'}]
    tasks_url = f'/api/projects/{env["project"].pk}/import'
    predictions_url = tasks_url + '/predictions'
    source = caller.post(tasks_url, body, format='json', HTTP_IDEMPOTENCY_KEY='same-key-different-operation')
    prediction = caller.post(predictions_url, body, format='json', HTTP_IDEMPOTENCY_KEY='same-key-different-operation')
    assert source.status_code == prediction.status_code == 201, (source.data, prediction.data)
    assert source.data['task_count'] == 1 and prediction.data['created'] == 1
    assert Task.objects.filter(project=env['project']).count() == 2
    assert Prediction.objects.filter(project=env['project']).count() == 1
    assert IdempotencyRecord.objects.filter(user=account.user, key='same-key-different-operation').count() == 2
    assert caller.post(tasks_url, body, format='json', HTTP_IDEMPOTENCY_KEY='same-key-different-operation').data == source.data
    assert caller.post(predictions_url, body, format='json', HTTP_IDEMPOTENCY_KEY='same-key-different-operation').data == prediction.data


def test_prediction_import_preserves_optional_human_keys_and_validates_body(env):
    caller = client_for(env['admin'])
    url = f'/api/projects/{env["project"].pk}/import/predictions'
    assert caller.post(url, prediction_body(env), format='json').status_code == 201
    assert caller.post(url, {'unexpected': 'object'}, format='json').status_code == 400
    assert Prediction.objects.filter(project=env['project']).count() == 1


def model_predictions(env):
    first = Prediction.objects.create(task=env['task'], project=env['project'], result=result('a'), model_version='remove')
    second = Prediction.objects.create(task=env['task'], project=env['project'], result=result('a'), model_version='keep')
    return first, second


@pytest.mark.parametrize('caller_kind', ['a', 'qa', 'service'])
def test_model_version_static_read_does_not_grant_live_backend_configuration(env, caller_kind):
    caller = service(env, ['projects:read'])[0] if caller_kind == 'service' else client_for(env[caller_kind])
    url = f'/api/projects/{env["project"].pk}/model-versions/'
    assert caller.get(url, {'extended': True}).status_code == 200
    assert caller.get(url, {'extended': True, 'include_live_models': True}).status_code == 403
    assert client_for(env['admin']).get(url, {'extended': True, 'include_live_models': True}).status_code == 200


@pytest.mark.parametrize('role', ['a', 'qa'])
def test_model_version_deletion_is_admin_only_for_humans(env, role):
    removed, kept = model_predictions(env)
    response = client_for(env[role]).delete(f'/api/projects/{env["project"].pk}/model-versions/', {'model_version': 'remove'}, format='json')
    assert response.status_code == 403
    assert Prediction.objects.filter(pk__in=[removed.pk, kept.pk]).count() == 2


@pytest.mark.parametrize('scopes', [['projects:write'], ['predictions:delete'], ['projects:write', 'annotations:delete']])
def test_model_version_deletion_requires_specific_service_permissions(env, scopes):
    removed, kept = model_predictions(env)
    caller, _, _ = service(env, scopes)
    response = caller.delete(f'/api/projects/{env["project"].pk}/model-versions/', {'model_version': 'remove'}, format='json')
    assert response.status_code == 403
    assert Prediction.objects.filter(pk__in=[removed.pk, kept.pk]).count() == 2


@pytest.mark.parametrize('machine', [False, True])
def test_authorized_model_version_deletion_only_removes_that_version(env, machine):
    removed, kept = model_predictions(env)
    caller = service(env, PREDICTION_DELETE_SCOPES)[0] if machine else client_for(env['admin'])
    response = caller.delete(f'/api/projects/{env["project"].pk}/model-versions/', {'model_version': 'remove'}, format='json')
    assert response.status_code == 200 and response.data['deleted_predictions'] == 1
    assert not Prediction.objects.filter(pk=removed.pk).exists() and Prediction.objects.filter(pk=kept.pk).exists()


def test_model_version_deletion_respects_project_scope_and_archival(env):
    removed, _ = model_predictions(env)
    caller, account, _ = service(env, PREDICTION_DELETE_SCOPES)
    url = f'/api/projects/{env["project"].pk}/model-versions/'
    account.projects.clear()
    assert caller.delete(url, {'model_version': 'remove'}, format='json').status_code in (403, 404)
    account.projects.add(env['project'])
    ProjectAnnotationPolicy.objects.create(project=env['project'], is_archived=True)
    assert caller.delete(url, {'model_version': 'remove'}, format='json').status_code == 403
    assert Prediction.objects.filter(pk=removed.pk).exists()


def test_invalid_model_version_deletion_body_is_validated(env):
    removed, _ = model_predictions(env)
    response = client_for(env['admin']).delete(f'/api/projects/{env["project"].pk}/model-versions/', [], format='json')
    assert response.status_code == 400
    assert Prediction.objects.filter(pk=removed.pk).exists()


def test_webhook_replay_redacts_and_repairs_legacy_secret_receipts(env, monkeypatch):
    from collab import webhooks

    monkeypatch.setattr(webhooks, 'validate_target_url', lambda _: None)
    monkeypatch.setattr(webhooks, 'new_secret', lambda: 'synthetic-signing-key')
    caller = client_for(env['admin'])
    url = f'/api/extensions/v1/projects/{env["project"].pk}/webhooks'
    body = {'url': 'https://example.test/fixture', 'events': ['review.decided']}
    first = caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='legacy-webhook')
    assert first.status_code == 201 and first.data['secret'] == 'synthetic-signing-key'
    receipt = IdempotencyRecord.objects.get(key='legacy-webhook')
    assert 'secret' not in receipt.response_body
    receipt.response_body = dict(first.data)  # Simulate a receipt persisted before redaction was introduced.
    receipt.save(update_fields=['response_body'])
    replay = caller.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY='legacy-webhook')
    assert replay.status_code == 201 and replay.data['id'] == first.data['id']
    assert 'secret' not in replay.data and replay.data['secret_shown_once'] is True
    receipt.refresh_from_db()
    assert 'secret' not in receipt.response_body
    assert WebhookSubscription.objects.count() == 1
