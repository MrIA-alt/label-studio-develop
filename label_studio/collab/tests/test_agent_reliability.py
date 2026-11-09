from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from collab import webhooks
from collab.models import AnnotationReview, AnnotationRevision, IdempotencyRecord, ServiceAccount, WebhookDelivery, WebhookSubscription
from collab.tests.test_agent_security import EXT, service
from collab.tests.test_blind_mode import CONFIG, env  # noqa: F401
from projects.models import Project
from tasks.models import Task

pytestmark = pytest.mark.django_db


def test_project_and_idempotency_receipt_roll_back_together(env, monkeypatch):
    bot, _, _ = service(env, ['projects:write'])
    payload = {'title': 'Atomic agent project', 'label_config': CONFIG}
    original = IdempotencyRecord.objects.create
    monkeypatch.setattr(IdempotencyRecord.objects, 'create', Mock(side_effect=RuntimeError('receipt write failed')))
    response = bot.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='atomic')
    assert response.status_code == 500
    assert 'receipt write failed' not in str(response.data)
    assert not Project.objects.filter(title=payload['title']).exists()
    assert not IdempotencyRecord.objects.filter(key='atomic').exists()
    monkeypatch.setattr(IdempotencyRecord.objects, 'create', original)
    first = bot.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='atomic')
    replay = bot.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='atomic')
    assert first.status_code == replay.status_code == 201
    assert first.data == replay.data
    assert Project.objects.filter(title=payload['title']).count() == 1


def test_native_import_replays_without_duplicate_tasks(env):
    bot, _, _ = service(env, ['imports:write'], [env['project'].pk])
    url = f"/api/projects/{env['project'].pk}/import"
    payload = [{'text': 'Imported once'}]
    assert bot.post(url, payload, format='json').status_code == 400
    first = bot.post(url, payload, format='json', HTTP_IDEMPOTENCY_KEY='import-once')
    assert first.status_code == 201, first.data
    replay = bot.post(url, payload, format='json', HTTP_IDEMPOTENCY_KEY='import-once')
    assert replay.status_code == first.status_code and replay.data == first.data
    assert Task.objects.filter(project=env['project'], data={'text': 'Imported once'}).count() == 1
    conflict = bot.post(url, [{'text': 'Changed'}], format='json', HTTP_IDEMPOTENCY_KEY='import-once')
    assert conflict.status_code == 409


def subscription(env):
    return WebhookSubscription.objects.create(
        project=env['project'], url='https://hooks.example.com/events', secret='secret', events=['review.decided'],
    )


def test_webhook_dns_is_rechecked_for_delivery_and_internal_target_is_rejected(env, monkeypatch):
    sub = subscription(env)
    monkeypatch.setattr(webhooks.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('93.184.216.34', 443))])
    webhooks.validate_target_url(sub.url)
    monkeypatch.setattr(webhooks.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValidationError):
        webhooks._post_once(sub.url, b'{}', {})


def test_pinned_https_transport_uses_validated_ip_and_original_sni(monkeypatch):
    calls = {}
    monkeypatch.setattr(webhooks.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('93.184.216.34', 443))])
    pool = Mock()
    pool.urlopen.return_value = SimpleNamespace(status=204, close=lambda: None)
    def factory(host, **kwargs):
        calls.update(host=host, **kwargs)
        return pool
    monkeypatch.setattr(webhooks.urllib3, 'HTTPSConnectionPool', factory)
    assert webhooks._post_once('https://hooks.example.com/events?q=1', b'{}', {}) == 204
    assert calls == {
        'host': '93.184.216.34', 'port': 443, 'server_hostname': 'hooks.example.com',
        'assert_hostname': 'hooks.example.com', 'cert_reqs': 'CERT_REQUIRED',
    }
    assert pool.urlopen.call_args.kwargs['headers']['Host'] == 'hooks.example.com'
    assert pool.urlopen.call_args.kwargs['redirect'] is False


def test_outbox_survives_worker_restart_retries_and_records_signature(env, monkeypatch):
    sub = subscription(env)
    webhooks.emit(env['project'], 'review.decided', {'annotation': 123})
    job = WebhookDelivery.objects.get(subscription=sub)
    # Simulate a worker dying after claiming the row.
    job.status = WebhookDelivery.Status.SENDING
    job.lease_until = timezone.now() - timedelta(seconds=1)
    job.save()
    posted = []
    def post(url, body, headers):
        posted.append((body, headers))
        return 503 if len(posted) == 1 else 204
    monkeypatch.setattr(webhooks, '_post_once', post)
    assert webhooks.drain_outbox()['sent'] == 0
    job.refresh_from_db()
    assert job.status == WebhookDelivery.Status.PENDING
    WebhookDelivery.objects.filter(pk=job.pk).update(available_at=timezone.now() - timedelta(seconds=1))
    assert webhooks.drain_outbox()['sent'] == 1
    job.refresh_from_db()
    assert job.status == WebhookDelivery.Status.SENT and job.attempts == 2
    body, headers = posted[-1]
    assert headers['X-Collab-Delivery'] == str(job.pk)
    assert headers['X-Collab-Signature'] == 'sha256=' + webhooks.sign(sub.secret, headers['X-Collab-Timestamp'], body)


def test_outbox_is_rolled_back_with_business_transaction(env):
    from django.db import transaction
    subscription(env)
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            webhooks.emit(env['project'], 'review.decided', {})
            raise RuntimeError('business mutation rolled back')
    assert not WebhookDelivery.objects.exists()


def test_extension_openapi_has_typed_bodies_responses_and_unique_operations():
    from drf_spectacular.generators import SchemaGenerator
    from collab.urls import urlpatterns
    schema = SchemaGenerator(patterns=urlpatterns).get_schema(public=True)
    operations = []
    for path, methods in schema['paths'].items():
        for method, operation in methods.items():
            if method not in ('get', 'post', 'put', 'patch', 'delete'):
                continue
            operations.append(operation['operationId'])
            assert operation['responses'], (path, method)
    assert len(operations) == len(set(operations))
    create = schema['paths'][f'{EXT}/projects']['post']
    create_ref = create['requestBody']['content']['application/json']['schema']['$ref'].rsplit('/', 1)[-1]
    assert {'title', 'label_config', 'policy', 'member_ids'} <= set(schema['components']['schemas'][create_ref]['properties'])
    detail = schema['paths'][f'{EXT}/tasks/{{id}}/review']['get']
    detail_ref = detail['responses']['200']['content']['application/json']['schema']['$ref'].rsplit('/', 1)[-1]
    assert {'task', 'annotations', 'agreement', 'arbitration'} <= set(schema['components']['schemas'][detail_ref]['properties'])
    assert 'reviews:read' in detail['description']


def test_failed_webhook_is_visible_and_can_be_explicitly_retried(env):
    from collab.tests.test_blind_mode import client_for
    sub = subscription(env)
    job = WebhookDelivery.objects.create(
        subscription=sub, event='review.decided', payload={}, status=WebhookDelivery.Status.FAILED,
        attempts=8, available_at=timezone.now(), last_error='Unavailable',
    )
    admin = client_for(env['admin'])
    url = f"{EXT}/projects/{env['project'].pk}/webhook-deliveries"
    listed = admin.get(url)
    assert listed.status_code == 200 and listed.data['count'] == 1
    assert 'secret' not in listed.data['results'][0]
    assert admin.post(f'{url}/{job.pk}/retry', {}, format='json').status_code == 200
    job.refresh_from_db()
    assert job.status == WebhookDelivery.Status.PENDING and job.attempts == 0


def test_invalid_pagination_and_distribution_body_return_structured_validation_errors(env):
    from collab.tests.test_blind_mode import client_for, set_policy
    admin = client_for(env['admin'])
    response = admin.get(f'{EXT}/my-assignments?limit=not-a-number')
    assert response.status_code == 400 and response.data['code'] == 'invalid'
    assert admin.get(f'{EXT}/my-reviews?project=not-an-id').status_code == 400
    set_policy(env['project'], assignment_mode='automatic')
    invalid = admin.post(f"{EXT}/projects/{env['project'].pk}/assignments/distribute", {'limit': 'bad'}, format='json')
    assert invalid.status_code == 400


def test_service_account_creation_retry_keeps_identity_without_persisting_or_replaying_token(env):
    from collab.tests.test_blind_mode import client_for
    admin = client_for(env['admin'])
    payload = {'name': 'One machine', 'scopes': ['progress:read'], 'project_ids': [env['project'].pk]}
    first = admin.post(f'{EXT}/service-accounts', payload, format='json', HTTP_IDEMPOTENCY_KEY='one-machine')
    assert first.status_code == 201, first.data
    replay = admin.post(f'{EXT}/service-accounts', payload, format='json', HTTP_IDEMPOTENCY_KEY='one-machine')
    assert replay.status_code == 201 and replay.data['id'] == first.data['id']
    assert replay.data['token_shown_once'] is True and 'token' not in replay.data
    assert ServiceAccount.objects.filter(name=payload['name']).count() == 1
    receipt = IdempotencyRecord.objects.get(key='one-machine')
    assert first.data['token'] not in str(receipt.response_body)


def test_review_and_final_retries_do_not_duplicate_decisions_or_revisions(env):
    from collab.tests.test_blind_mode import client_for, result
    from collab.tests.test_review_agent import three_annotators, submitted
    annotation, _, _ = three_annotators(env)
    submitted(env)
    qa = client_for(env['qa'])
    payload = {'annotation': annotation.pk, 'revision': annotation.revision, 'decision': 'ACCEPT'}
    first = qa.post(f'{EXT}/reviews', payload, format='json', HTTP_IDEMPOTENCY_KEY='review-once')
    again = qa.post(f'{EXT}/reviews', payload, format='json', HTTP_IDEMPOTENCY_KEY='review-once')
    assert first.status_code == again.status_code == 201
    assert first.data == again.data and AnnotationReview.objects.filter(annotation=annotation).count() == 1
    url = f"{EXT}/tasks/{env['task'].pk}/final"
    final = qa.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='final-once')
    replay = qa.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='final-once')
    assert final.status_code == replay.status_code == 201 and final.data == replay.data
    assert not AnnotationRevision.objects.filter(annotation_id=final.data['final_annotation']).exists()


def test_webhook_creation_retry_avoids_duplicate_subscriptions(env, monkeypatch):
    from collab.tests.test_blind_mode import client_for
    monkeypatch.setattr(webhooks.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('93.184.216.34', 443))])
    admin = client_for(env['admin'])
    payload = {'url': 'https://hooks.example.com/events', 'events': ['review.decided']}
    url = f"{EXT}/projects/{env['project'].pk}/webhooks"
    first = admin.post(url, payload, format='json', HTTP_IDEMPOTENCY_KEY='hook-once')
    replay = admin.post(url, payload, format='json', HTTP_IDEMPOTENCY_KEY='hook-once')
    assert first.status_code == replay.status_code == 201
    assert first.data['id'] == replay.data['id']
    assert first.data['secret'] and 'secret' not in replay.data
    assert replay.data['secret_shown_once'] is True
    assert 'secret' not in IdempotencyRecord.objects.get(key='hook-once').response_body
    assert WebhookSubscription.objects.count() == 1


def test_new_extension_projects_are_confidential_by_default_and_partial_policy_keeps_safe_defaults(env):
    from collab.tests.test_blind_mode import client_for
    admin = client_for(env['admin'])
    for number, policy in enumerate((None, {'required_annotations': 3})):
        payload = {'title': f'Confidential project {number}', 'label_config': CONFIG}
        if policy is not None:
            payload['policy'] = policy
        response = admin.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY=f'safe-default-{number}')
        assert response.status_code == 201, response.data
        project = Project.objects.get(pk=response.data['id'])
        assert project.annotation_policy.blind_mode is True
        assert project.annotation_policy.assignment_mode == 'manual'
        assert project.annotation_policy.review_mode == 'manual'
        assert project.maximum_annotations == (3 if policy else 2)


def test_publish_rolls_back_business_state_and_audit_when_outbox_write_fails(env, monkeypatch):
    from collab.models import AuditEvent
    from collab.tests.test_blind_mode import client_for
    Project.objects.filter(pk=env['project'].pk).update(is_published=False)
    monkeypatch.setattr(webhooks, 'emit', Mock(side_effect=RuntimeError('outbox write failed')))
    response = client_for(env['admin']).post(f"{EXT}/projects/{env['project'].pk}/publish", {}, format='json')
    assert response.status_code == 500
    env['project'].refresh_from_db()
    assert env['project'].is_published is False
    assert not AuditEvent.objects.filter(action='project.publish').exists()


def test_final_arbitration_event_is_subscribable_and_queued_once_for_retries(env):
    from collab.tests.test_blind_mode import client_for, result
    from collab.tests.test_review_agent import three_annotators, submitted
    three_annotators(env)
    submitted(env)
    sub = subscription(env)
    sub.events = ['task.arbitrated']
    sub.save(update_fields=['events'])
    qa = client_for(env['qa'])
    url = f"{EXT}/tasks/{env['task'].pk}/final"
    first = qa.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='arbitrate-once')
    replay = qa.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='arbitrate-once')
    assert first.status_code == replay.status_code == 201
    assert 'task.arbitrated' in WebhookSubscription.EVENTS
    job = WebhookDelivery.objects.get(subscription=sub)
    assert job.event == 'task.arbitrated'


def test_native_openapi_preserves_bodies_and_documents_idempotency_and_revision_headers():
    from django.urls import path
    from drf_spectacular.generators import SchemaGenerator
    from collab.schema import install_native_contracts
    from projects.api import ProjectListAPI
    from data_import.api import ImportAPI
    from tasks.api import AnnotationAPI, AnnotationsListAPI, TaskListAPI
    install_native_contracts()
    schema = SchemaGenerator(patterns=[
        path('api/projects/', ProjectListAPI.as_view()),
        path('api/projects/<int:pk>/import', ImportAPI.as_view()),
        path('api/tasks/', TaskListAPI.as_view()),
        path('api/tasks/<int:pk>/annotations/', AnnotationsListAPI.as_view()),
        path('api/annotations/<int:pk>/', AnnotationAPI.as_view()),
    ]).get_schema(public=True)
    for url in ('/api/projects/', '/api/projects/{id}/import', '/api/tasks/', '/api/tasks/{id}/annotations/'):
        operation = schema['paths'][url]['post']
        assert any(param['name'] == 'Idempotency-Key' and param['in'] == 'header' for param in operation.get('parameters', []))
        assert operation.get('requestBody'), url
    detail = schema['paths']['/api/annotations/{id}/']
    assert any(param['name'] == 'If-Match' for param in detail['patch']['parameters'])
    assert 'ETag' in detail['patch']['responses']['200']['headers']
    assert '409' in detail['patch']['responses']


def test_native_serializer_inference_gaps_have_explicit_typed_contracts():
    from django.urls import path
    from drf_spectacular.generators import SchemaGenerator
    from collab.native_schema import install_native_view_contracts
    from data_import.api import MicrophoneUploadAPI
    from data_manager.api import ProjectStateAPI
    from labels_manager.api import LabelBulkUpdateAPI
    from organizations.api import OrganizationResetTokenAPI
    from projects.api import ProjectMemberDetailAPI
    from tasks.api import AnnotationConvertAPI
    install_native_view_contracts()
    schema = SchemaGenerator(patterns=[
        path('api/annotations/<int:pk>/convert-to-draft', AnnotationConvertAPI.as_view()),
        path('api/dm/project/', ProjectStateAPI.as_view()),
        path('api/invite/reset-token', OrganizationResetTokenAPI.as_view()),
        path('api/labels/bulk', LabelBulkUpdateAPI.as_view()),
        path('api/projects/<int:pk>/members/<int:user_pk>/', ProjectMemberDetailAPI.as_view()),
        path('api/projects/<int:pk>/microphone-upload', MicrophoneUploadAPI.as_view()),
    ]).get_schema(public=True)
    cases = [
        ('/api/annotations/{id}/convert-to-draft', 'post', '201'),
        ('/api/dm/project/', 'get', '200'),
        ('/api/invite/reset-token', 'post', '201'),
        ('/api/labels/bulk', 'post', '200'),
        ('/api/projects/{id}/microphone-upload', 'post', '201'),
    ]
    for url, method, status in cases:
        assert schema['paths'][url][method]['responses'][status]['content']['application/json']['schema'].get('$ref')
    audio = schema['paths']['/api/projects/{id}/microphone-upload']['post']
    assert 'multipart/form-data' in audio['requestBody']['content']
    assert '204' in schema['paths']['/api/projects/{id}/members/{user_pk}/']['delete']['responses']
