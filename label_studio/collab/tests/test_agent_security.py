import pytest
from rest_framework.test import APIClient

from collab.models import ServiceAccount
from collab.tests.test_blind_mode import CONFIG, client_for, env, annotate  # noqa: F401
from projects.models import Project

pytestmark = pytest.mark.django_db
EXT = '/api/extensions/v1'


def service(env, scopes, project_ids=None):
    response = client_for(env['admin']).post(
        f'{EXT}/service-accounts',
        {'name': 'Agent', 'scopes': scopes, 'project_ids': [env['project'].pk] if project_ids is None else project_ids}, format='json',
    )
    assert response.status_code == 201, response.data
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Service ' + response.data['token'])
    return client, ServiceAccount.objects.get(pk=response.data['id']), response.data['token']


def test_service_token_authenticates_with_legacy_tokens_disabled_and_revokes(env):
    org = env['project'].organization
    org.jwt.legacy_api_tokens_enabled = False
    org.jwt.save()
    bot, account, token = service(env, ['progress:read'])
    assert account.user.get_organization_role() == 'qa'
    assert token not in account.token_digest
    assert bot.get(f"{EXT}/projects/{env['project'].pk}/progress").status_code == 200
    assert client_for(env['admin']).delete(f'{EXT}/service-accounts/{account.pk}').status_code == 204
    assert bot.get(f"{EXT}/projects/{env['project'].pk}/progress").status_code == 401


def test_progress_scope_cannot_read_annotations_or_reviews(env):
    annotate(env, env['a'], 'a')
    bot, _, _ = service(env, ['progress:read'])
    assert bot.get(f"{EXT}/projects/{env['project'].pk}/progress").status_code == 200
    assert bot.get(f"{EXT}/projects/{env['project'].pk}/results").status_code == 403
    assert bot.get(f"{EXT}/tasks/{env['task'].pk}/review").status_code == 403
    assert bot.get(f"/api/tasks/{env['task'].pk}/").status_code == 403


def test_native_project_scope_is_enforced_and_deletion_requires_explicit_scope(env):
    other = Project.objects.create(
        title='Unrelated', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG,
    )
    bot, account, _ = service(env, ['projects:read', 'projects:write'], [env['project'].pk])
    assert bot.get(f"/api/projects/{env['project'].pk}/").status_code == 200
    assert bot.get(f'/api/projects/{other.pk}/').status_code in (403, 404)
    assert bot.delete(f"/api/projects/{env['project'].pk}/").status_code == 403
    account.scopes = ['projects:delete']
    account.save()
    assert bot.delete(f'/api/projects/{other.pk}/').status_code in (403, 404)
    assert Project.objects.filter(pk=other.pk).exists()


def test_legacy_native_scope_is_not_an_administrative_wildcard(env):
    bot, _, _ = service(env, ['native'])
    assert bot.get('/api/projects/').status_code == 403
    assert bot.delete(f"/api/projects/{env['project'].pk}/").status_code == 403
    assert bot.post('/api/webhooks/', {'project': env['project'].pk, 'url': 'https://example.com'}, format='json').status_code == 403


def test_no_service_account_can_create_another_machine_identity(env):
    bot, _, _ = service(env, ['projects:write', 'policy:write'])
    assert bot.post(f'{EXT}/service-accounts', {'name': 'nested', 'scopes': ['projects:write']}, format='json').status_code == 403


def test_service_account_request_cannot_request_admin_role(env):
    response = client_for(env['admin']).post(
        f'{EXT}/service-accounts', {'name': 'admin bot', 'scopes': ['progress:read'], 'role': 'admin'}, format='json'
    )
    assert response.status_code == 400


def test_empty_project_list_is_not_organization_wide_and_admin_can_update_permissions(env):
    bot, account, old_token = service(env, ['progress:read'], [])
    url = f"{EXT}/projects/{env['project'].pk}/progress"
    assert bot.get(url).status_code in (403, 404)
    admin = client_for(env['admin'])
    changed = admin.patch(
        f'{EXT}/service-accounts/{account.pk}',
        {'project_ids': [env['project'].pk], 'rotate_token': True}, format='json',
    )
    assert changed.status_code == 200 and changed.data['token'] != old_token
    assert bot.get(url).status_code == 401
    bot.credentials(HTTP_AUTHORIZATION='Service ' + changed.data['token'])
    assert bot.get(url).status_code == 200
    assert admin.get(f'{EXT}/service-accounts/{account.pk}').data['all_projects'] is False


def test_native_mutations_are_audited_without_request_body_or_query(env):
    from collab.models import AuditEvent
    response = client_for(env['admin']).patch(
        f"/api/projects/{env['project'].pk}/?token=never-store-this", {'description': 'private-description'}, format='json'
    )
    assert response.status_code == 200
    event = AuditEvent.objects.get(action='api.patch')
    assert event.organization_id == env['project'].organization_id
    assert 'never-store-this' not in str(event.detail)
    assert 'private-description' not in str(event.detail)


def test_audit_organization_survives_project_deletion_and_actor_switch(env):
    from collab import audit
    from organizations.models import Organization
    from users.models import User
    event = audit.record(env['admin'], 'project.test', env['project'], 'project', env['project'].pk)
    original_org_id = env['project'].organization_id
    env['project'].delete()
    owner = User.objects.create(email='other-owner@example.com', username='other-owner')
    other = Organization.create_organization(created_by=owner, title='Other organization')
    other.add_user(env['admin'], role='admin')
    env['admin'].active_organization = other
    env['admin'].save(update_fields=['active_organization'])
    event.refresh_from_db()
    assert event.project_id is None and event.organization_id == original_org_id
    response = client_for(env['admin']).get(f'{EXT}/audit')
    assert response.status_code == 200
    assert event.pk not in [row['id'] for row in response.data['results']]


def test_import_scope_does_not_grant_task_mutation_or_bulk_deletion(env):
    bot, _, _ = service(env, ['imports:write', 'projects:write'])
    assert bot.patch(f"/api/tasks/{env['task'].pk}/", {'data': {'text': 'changed'}}, format='json').status_code == 403
    assert bot.post(f"/api/dm/actions/?project={env['project'].pk}&id=delete_tasks", {}, format='json').status_code == 403
    assert bot.post(f"/api/dm/actions/?project={env['project'].pk}&id=delete_tasks_annotations", {}, format='json').status_code == 403


def test_rotation_removes_migrated_legacy_token_fallback(env):
    from rest_framework.authtoken.models import Token
    from collab.authentication import token_digest
    bot, account, _ = service(env, ['progress:read'])
    legacy = Token.objects.create(user=account.user)
    account.token_digest = token_digest(legacy.key)
    account.save(update_fields=['token_digest'])
    org = env['project'].organization
    org.jwt.legacy_api_tokens_enabled = True
    org.jwt.save()
    bot.credentials(HTTP_AUTHORIZATION='Token ' + legacy.key)
    url = f"{EXT}/projects/{env['project'].pk}/progress"
    assert bot.get(url).status_code == 200
    rotated = client_for(env['admin']).patch(
        f'{EXT}/service-accounts/{account.pk}', {'rotate_token': True}, format='json'
    )
    assert rotated.status_code == 200
    assert not Token.objects.filter(user=account.user).exists()
    assert bot.get(url).status_code == 401


def test_annotation_writer_cannot_review_through_native_route_or_convert_without_both_scopes(env):
    from tasks.models import Annotation
    bot, account, _ = service(env, ['annotations:write'])
    own = annotate(env, account.user, 'a')
    assert bot.post(f'/api/annotations/{own.pk}/review', {'status': 'accepted'}, format='json').status_code == 403
    url = f'/api/annotations/{own.pk}/convert-to-draft'
    assert bot.post(url, {}, format='json').status_code == 403
    account.scopes = ['annotations:delete']
    account.save(update_fields=['scopes'])
    assert bot.post(url, {}, format='json').status_code == 403
    assert Annotation.objects.filter(pk=own.pk).exists()


def test_annotation_creation_receipt_does_not_bypass_revoked_project_access(env):
    from collab.tests.test_blind_mode import result
    bot, account, _ = service(env, ['annotations:write'])
    url = f"/api/tasks/{env['task'].pk}/annotations/"
    first = bot.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='private-annotation')
    assert first.status_code == 201, first.data
    account.projects.clear()
    replay = bot.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='private-annotation')
    assert replay.status_code in (403, 404)
    assert 'result' not in replay.data


def test_project_creation_receipt_is_hidden_after_created_project_access_is_revoked(env):
    bot, account, _ = service(env, ['projects:write'], [])
    payload = {'title': 'Private new campaign', 'label_config': CONFIG}
    first = bot.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='private-project')
    assert first.status_code == 201, first.data
    account.projects.clear()
    replay = bot.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='private-project')
    assert replay.status_code in (403, 404)
    assert 'title' not in replay.data
