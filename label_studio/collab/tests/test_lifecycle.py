from datetime import timedelta

import pytest
from collab import services
from collab.models import AuditEvent, ProjectAnnotationPolicy, ServiceAccount, TaskAssignment, WebhookDelivery, WebhookSubscription
from django.utils import timezone
from projects.models import Project
from tasks.models import Annotation, Task

from .test_blind_mode import annotate, client_for, env, make_user, result, set_policy  # noqa: F401

pytestmark = pytest.mark.django_db
EXT = '/api/extensions/v1'


def lifecycle(env, action, user=None):
    return client_for(user or env['admin']).post(f'{EXT}/projects/{env["project"].pk}/{action}', {}, format='json')


def test_legacy_archive_preserves_reads_exports_and_emits_once(env):
    annotation = annotate(env, env['a'], 'a')
    assert lifecycle(env, 'restore').status_code == 200
    assert not ProjectAnnotationPolicy.objects.filter(project=env['project']).exists()
    WebhookSubscription.objects.create(
        project=env['project'], url='https://example.test/hooks', secret='test-secret',
        events=['project.archived', 'project.restored'], created_by=env['admin'],
    )
    before = env['project'].maximum_annotations
    response = lifecycle(env, 'archive')
    assert response.status_code == 200, response.data
    assert response.json()['is_archived']
    policy = ProjectAnnotationPolicy.objects.get(project=env['project'])
    assert not policy.blind_mode and policy.assignment_mode == 'open' and policy.review_mode == 'none'
    assert policy.required_annotations == before
    assert lifecycle(env, 'archive').status_code == 200
    assert AuditEvent.objects.filter(action='project.archived').count() == 1
    assert WebhookDelivery.objects.filter(event='project.archived').count() == 1
    client = client_for(env['a'])
    assert client.get(f'/api/tasks/{env["task"].pk}/').status_code == 200
    detail = client.get(f'/api/annotations/{annotation.pk}/')
    assert detail.status_code == 200 and detail.json()['readonly']
    assert detail.json()['result'] == result('a')
    assert client.get(f'/api/projects/{env["project"].pk}/').json()['is_archived']
    exported = client_for(env['admin']).get(f'/api/projects/{env["project"].pk}/export', {'exportType': 'JSON'})
    assert exported.status_code == 200, getattr(exported, 'data', None)
    b''.join(exported.streaming_content)
    assert lifecycle(env, 'restore').status_code == 200
    assert WebhookDelivery.objects.filter(event='project.restored').count() == 1
    response = client.patch(f'/api/annotations/{annotation.pk}/', {'result': result('b'), 'revision': 1}, format='json')
    assert response.status_code == 200, response.data


def test_archive_blocks_work_mutations_and_restore_reopens_them(env):
    set_policy(env['project'], required_annotations=1, blind_mode=True, assignment_mode='manual', review_mode='manual')
    assignment, _ = services.assign_task(env['task'], env['a'], assigned_by=env['admin'])
    created = client_for(env['a']).post(f'/api/tasks/{env["task"].pk}/annotations/', {'result': result('a')}, format='json')
    assert created.status_code == 201, created.data
    annotation = Annotation.objects.get(pk=created.json()['id'])
    second = Task.objects.create(project=env['project'], data={'text': 'second'})
    pending, _ = services.assign_task(second, env['a'])
    assert lifecycle(env, 'archive').status_code == 200
    for user, method, url, body in [
        ('a', 'post', f'/api/tasks/{second.pk}/annotations/', {'result': result('a')}),
        ('a', 'patch', f'/api/annotations/{annotation.pk}/', {'result': result('b'), 'revision': 1}),
        ('a', 'post', f'/api/tasks/{second.pk}/drafts', {'result': []}),
        ('qa', 'post', f'{EXT}/reviews', {'annotation': annotation.pk, 'decision': 'ACCEPT'}),
        ('qa', 'post', f'{EXT}/tasks/{env["task"].pk}/final', {'result': result('b')}),
        ('admin', 'post', f'{EXT}/projects/{env["project"].pk}/assignments/distribute', {}),
        ('admin', 'post', f'{EXT}/projects/{env["project"].pk}/assignments/bulk', {'task_ids': [second.pk], 'user_ids': [env['b'].pk]}),
        ('a', 'post', f'{EXT}/assignments/{pending.pk}/start', {}),
        ('admin', 'post', f'/api/projects/{env["project"].pk}/import', [{'data': {'text': 'blocked'}}]),
        ('admin', 'post', '/api/tasks/', {'project': env['project'].pk, 'data': {'text': 'blocked'}}),
        ('admin', 'patch', f'/api/tasks/{second.pk}/', {'data': {'text': 'blocked'}}),
        ('admin', 'patch', f'{EXT}/projects/{env["project"].pk}/policy', {'is_archived': False}),
    ]:
        response = getattr(client_for(env[user]), method)(url, body, format='json')
        assert response.status_code == 403, (url, response.data)
    assert client_for(env['a']).get(f'/api/projects/{env["project"].pk}/next/').status_code == 403
    assert Annotation.objects.filter(project=env['project']).count() == 1
    assert Task.objects.filter(project=env['project']).count() == 2
    state = client_for(env['a']).get('/api/dm/project/', {'project': env['project'].pk}).json()
    assert not state['can_manage_tasks'] and not state['can_manage_annotations']
    assert lifecycle(env, 'restore').status_code == 200
    assert client_for(env['a']).post(f'{EXT}/assignments/{pending.pk}/start', {}, format='json').status_code == 200
    assert client_for(env['admin']).patch(f'/api/tasks/{second.pk}/', {'data': {'text': 'restored'}}, format='json').status_code == 200


def test_archive_permissions_service_scopes_and_tenant_boundary(env):
    from organizations.models import Organization, OrganizationMember
    from users.models import User

    assert lifecycle(env, 'archive', env['qa']).status_code == 403
    assert lifecycle(env, 'restore', env['a']).status_code == 403
    outsider = User.objects.create(username='lifecycle-outsider', email='lifecycle-outsider@x.io')
    org = Organization.create_organization(created_by=outsider, title='Other organisation')
    outsider.active_organization = org
    outsider.save()
    assert lifecycle(env, 'archive', outsider).status_code in (403, 404)
    bot = make_user(env['project'].organization, 'lifecycle-bot@x.io', OrganizationMember.Role.QA)
    account = ServiceAccount.objects.create(user=bot, name='lifecycle bot', scopes=['projects:write'])
    account.projects.add(env['project'])
    assert lifecycle(env, 'archive', bot).status_code == 200
    account.scopes = ['projects:read']
    account.save()
    assert lifecycle(env, 'restore', User.objects.get(pk=bot.pk)).status_code == 403
    assert lifecycle(env, 'restore').status_code == 200
    # The general policy endpoint cannot silently become an archival endpoint.
    response = client_for(env['admin']).patch(f'{EXT}/projects/{env["project"].pk}/policy', {'is_archived': True}, format='json')
    assert response.status_code == 200 and not response.json()['is_archived']


def test_archived_assignments_do_not_expire_and_project_can_be_deleted(env):
    set_policy(env['project'], assignment_mode='automatic', required_annotations=1)
    assignment, _ = services.assign_task(env['task'], env['a'])
    TaskAssignment.objects.filter(pk=assignment.pk).update(expires_at=timezone.now() - timedelta(hours=1))
    assert lifecycle(env, 'archive').status_code == 200
    assert services.expire_overdue() == 0
    assignment.refresh_from_db()
    assert assignment.status == TaskAssignment.Status.ASSIGNED
    response = client_for(env['admin']).delete(f'/api/projects/{env["project"].pk}/')
    assert response.status_code == 204, getattr(response, 'data', None)
    assert not Project.objects.filter(pk=env['project'].pk).exists()
