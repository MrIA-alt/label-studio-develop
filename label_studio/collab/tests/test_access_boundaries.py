"""Exercise native routes using real organisation roles and separate tenants."""
import json

import pytest
from collab import privacy
from collab.models import ServiceAccount
from data_import.models import FileUpload
from data_manager.prepare_params import PrepareParams
from organizations.models import Organization, OrganizationMember
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Task
from users.models import User
from webhooks.models import Webhook
from webhooks.utils import run_webhook_sync

from .test_blind_mode import CONFIG, annotate, client_for, env, make_user, set_policy  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def other(env):
    owner = User.objects.create(email='other-owner@x.io', username='other-owner@x.io')
    org = Organization.create_organization(created_by=owner, title='Other tenant')
    owner.active_organization = org
    owner.save()
    project = Project.objects.create(title='Other project', organization=org, created_by=owner, label_config=CONFIG)
    task = Task.objects.create(project=project, data={'text': 'private source'})
    annotation = Annotation.objects.create(task=task, project=project, completed_by=owner, result=[])
    return dict(org=org, project=project, task=task, annotation=annotation, owner=owner)


@pytest.mark.parametrize('role', ['a', 'qa', 'admin'])
def test_native_routes_do_not_cross_tenants(env, other, role):
    client = client_for(env[role])
    urls = (
        f"/api/tasks/?project={other['project'].pk}&fields=all",
        f"/api/tasks/{other['task'].pk}/",
        f"/api/annotations/{other['annotation'].pk}/",
        f"/api/projects/{other['project'].pk}/summary/",
        f"/api/projects/{other['project'].pk}/next/",
    )
    for url in urls:
        assert client.get(url).status_code in (403, 404), url
    assert client.patch(f"/api/annotations/{other['annotation'].pk}/", {'result': []}, format='json').status_code in (403, 404)


def test_explicit_membership_and_revocation_apply_to_native_and_extension(env):
    project = env['project']
    # A project without memberships keeps legacy organisation access.
    assert client_for(env['a']).get(f'/api/projects/{project.pk}/').status_code == 200
    member = ProjectMember.objects.create(project=project, user=env['a'], enabled=True)
    assert client_for(env['a']).get(f'/api/projects/{project.pk}/').status_code == 200
    assert client_for(env['b']).get(f'/api/projects/{project.pk}/').status_code in (403, 404)
    assert client_for(env['qa']).get(f'/api/tasks/?project={project.pk}').status_code in (403, 404)
    member.enabled = False
    member.save()
    assert client_for(env['a']).get(f'/api/tasks/{env["task"].pk}/').status_code in (403, 404)
    assert client_for(env['a']).get(f'/api/extensions/v1/projects/{project.pk}/policy').status_code in (403, 404)
    assert client_for(env['admin']).get(f'/api/projects/{project.pk}/').status_code == 200


def test_disabled_org_member_cannot_use_stale_session(env):
    OrganizationMember.objects.filter(user=env['a'], organization=env['project'].organization).update(deleted_at=env['project'].created_at)
    assert client_for(env['a']).get(f'/api/tasks/?project={env["project"].pk}').status_code in (403, 404)


def test_removing_last_member_does_not_reopen_project_to_organisation(env):
    member = ProjectMember.objects.create(project=env['project'], user=env['a'], enabled=True)
    response = client_for(env['admin']).delete(f'/api/projects/{env["project"].pk}/members/{env["a"].pk}/')
    assert response.status_code == 204
    member.refresh_from_db()
    assert not member.enabled
    assert client_for(env['a']).get(f'/api/projects/{env["project"].pk}/').status_code in (403, 404)
    assert client_for(env['b']).get(f'/api/projects/{env["project"].pk}/').status_code in (403, 404)


def test_new_native_project_starts_with_explicit_access(env):
    response = client_for(env['admin']).post('/api/projects/', {'title': 'New private campaign'}, format='json')
    assert response.status_code == 201
    pk = response.json()['id']
    assert ProjectMember.objects.filter(project_id=pk, user=env['admin'], enabled=True).exists()
    assert client_for(env['a']).get(f'/api/projects/{pk}/').status_code in (403, 404)
    policy = Project.objects.get(pk=pk).annotation_policy
    assert policy.required_annotations == 2
    assert policy.blind_mode
    assert policy.assignment_mode == 'manual'
    assert policy.review_mode == 'manual'


def test_native_annotation_count_setting_stays_in_sync_with_policy(env):
    client = client_for(env['admin'])
    response = client.post('/api/projects/', {'title': 'Three authors', 'maximum_annotations': 3}, format='json')
    assert response.status_code == 201
    project = Project.objects.get(pk=response.json()['id'])
    assert project.maximum_annotations == project.annotation_policy.required_annotations == 3
    assert client.patch(f'/api/projects/{project.pk}/', {'maximum_annotations': 4}, format='json').status_code == 200
    project.refresh_from_db()
    assert project.maximum_annotations == project.annotation_policy.required_annotations == 4
    assert client.post('/api/projects/', {'title': 'Invalid', 'maximum_annotations': 11}, format='json').status_code == 400


@pytest.mark.parametrize('action', ['delete_tasks', 'delete_tasks_annotations', 'delete_tasks_predictions'])
def test_labeller_cannot_bypass_role_through_data_manager_actions(env, action):
    annotation = annotate(env, env['b'], 'b')
    response = client_for(env['a']).post(
        f'/api/dm/actions/?project={env["project"].pk}&id={action}',
        {'selectedItems': {'all': True, 'excluded': []}}, format='json',
    )
    assert response.status_code == 403
    assert Task.objects.filter(pk=env['task'].pk).exists()
    assert Annotation.objects.filter(pk=annotation.pk).exists()


def test_blind_totals_summary_and_actual_filter_query_hide_other_work(env):
    set_policy(env['project'], blind_mode=True)
    annotate(env, env['b'], 'b')
    client = client_for(env['a'])
    data = client.get(f'/api/tasks/?project={env["project"].pk}').json()
    assert data['total_annotations'] == 0
    manager_state = client.get('/api/dm/project/', {'project': env['project'].pk}).json()
    assert manager_state['annotation_count'] == 0
    assert manager_state['task_count'] == 1
    assert not manager_state['can_delete_tasks']
    assert not manager_state['can_manage_annotations']
    query = json.dumps({'filters': {'conjunction': 'and', 'items': [
        {'filter': 'filter:tasks:total_annotations', 'operator': 'greater', 'type': 'Number', 'value': 0}
    ]}})
    response = client.get('/api/tasks/', {'project': env['project'].pk, 'query': query})
    assert response.status_code == 200
    assert response.json()['total'] == 1
    summary = client.get(f'/api/projects/{env["project"].pk}/summary/').json()
    assert summary['created_labels'] == {}
    assert summary['created_labels_drafts'] == {}
    project = client.get(f'/api/projects/{env["project"].pk}/').json()
    assert 'total_annotations_number' not in project


def test_multi_project_manager_enforces_each_assignment_policy(env):
    from collab.services import assign_task

    second = Project.objects.create(title='P2', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG)
    another_task = Task.objects.create(project=second, data={'text': 'second'})
    set_policy(env['project'], assignment_mode='manual', blind_mode=True)
    set_policy(second, assignment_mode='manual', blind_mode=True)
    assign_task(env['task'], env['a'])
    request = type('Request', (), {'user': env['a']})()
    params = PrepareParams(project=[env['project'].pk, second.pk], request=request)
    ids = list(Task.prepared.only_filtered(params).values_list('pk', flat=True))
    assert ids == [env['task'].pk]
    assert another_task.pk not in ids


def test_project_secrets_and_admin_mutations_are_not_available_to_labeller_or_qa(env):
    project = env['project']
    project.task_data_login = 'source-login'
    project.task_data_password = 'private-password'
    project.save()
    for role in ('a', 'qa', 'admin'):
        response = client_for(env[role]).get(f'/api/projects/{project.pk}/')
        assert response.status_code == 200
        assert 'task_data_password' not in response.json()
    assert 'task_data_login' not in client_for(env['a']).get(f'/api/projects/{project.pk}/').json()
    assert client_for(env['qa']).patch(f'/api/projects/{project.pk}/', {'title': 'changed'}, format='json').status_code == 403
    assert client_for(env['qa']).get(f'/api/projects/{project.pk}/members/').status_code == 200
    assert client_for(env['a']).get(f'/api/projects/{project.pk}/members/').status_code == 403
    assert client_for(env['qa']).post(f'/api/projects/{project.pk}/members/', {'user_id': env['b'].pk}, format='json').status_code == 403


def test_file_mutation_requires_admin_and_file_access_respects_tenant(env, other):
    upload = FileUpload.objects.create(project=other['project'], user=other['owner'], file='upload/private.png')
    for role in ('a', 'qa', 'admin'):
        client = client_for(env[role])
        assert client.get(f'/api/import/file-upload/{upload.pk}').status_code in (403, 404)
        assert client.delete(f'/api/import/file-upload/{upload.pk}').status_code in (403, 404)
    own = FileUpload.objects.create(project=env['project'], user=env['admin'], file='upload/source.png')
    assert client_for(env['a']).delete(f'/api/import/file-upload/{own.pk}').status_code == 403
    assert FileUpload.objects.filter(pk=own.pk).exists()


def test_data_manager_cannot_create_views_in_another_tenant(env, other):
    from data_manager.models import View

    response = client_for(env['a']).post('/api/dm/views/', {
        'project': other['project'].pk, 'data': {'title': 'Unauthorized view'},
    }, format='json')
    assert response.status_code == 400
    assert not View.objects.filter(project=other['project']).exists()


def test_source_upload_and_private_result_upload_have_separate_access(env):
    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    source = FileUpload.objects.create(project=env['project'], user=env['admin'], file='upload/source.png')
    result = FileUpload.objects.create(project=env['project'], user=env['b'], file='upload/recording.wav')
    env['task'].file_upload = source
    env['task'].save()
    assert not privacy.can_access_upload(env['a'], source)
    from collab.services import assign_task
    assign_task(env['task'], env['a'])
    assert privacy.can_access_upload(env['a'], source)
    assert not privacy.can_access_upload(env['a'], result)
    assert privacy.can_access_upload(env['b'], result)


def test_storage_resolver_only_serves_assigned_task_source_values(env, monkeypatch):
    from types import SimpleNamespace
    from collab.services import assign_task

    env['task'].data = {'text': 'hello', 'image': 's3://bucket/source.png'}
    env['task'].save()
    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    assign_task(env['task'], env['a'])
    monkeypatch.setattr('io_storages.proxy_api.get_storage_by_url', lambda *_: SimpleNamespace(presign=True))
    monkeypatch.setattr(Task, 'resolve_storage_uri', lambda *_: {'url': 'https://media.example.test/source.png'})
    client = client_for(env['a'])
    task_url = f'/tasks/{env["task"].pk}/resolve/'
    assert client.get(task_url, {'fileuri': 's3://bucket/source.png'}).status_code == 303
    assert client.get(task_url, {'fileuri': 's3://bucket/another.png'}).status_code == 403
    assert client.get(f'/projects/{env["project"].pk}/resolve/', {'fileuri': 's3://bucket/source.png'}).status_code == 403


def test_service_account_project_list_applies_to_native_routes(env):
    bot = make_user(env['project'].organization, 'scoped-bot@x.io', OrganizationMember.Role.QA)
    account = ServiceAccount.objects.create(user=bot, name='reader', scopes=['results:read', 'projects:read'])
    account.projects.add(env['project'])
    other_project = Project.objects.create(title='Restricted', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG)
    other_task = Task.objects.create(project=other_project, data={'text': 'hidden'})
    assert client_for(bot).get(f'/api/tasks/{env["task"].pk}/').status_code == 200
    assert client_for(bot).get(f'/api/tasks/{other_task.pk}/').status_code in (403, 404)
    assert client_for(bot).get(f'/api/tasks/?project={other_project.pk}').status_code in (403, 404)


def test_source_only_service_never_receives_other_annotations_or_counts(env):
    annotate(env, env['b'], 'b')
    bot = make_user(env['project'].organization, 'source-only@x.io', OrganizationMember.Role.QA)
    account = ServiceAccount.objects.create(user=bot, name='source reader', scopes=['tasks:read', 'projects:read'])
    account.projects.add(env['project'])
    response = client_for(bot).get(f'/api/tasks/?project={env["project"].pk}&fields=all')
    assert response.status_code == 200
    data = response.json()
    assert data['total_annotations'] == 0
    assert data['tasks'][0]['annotations'] == []
    assert data['tasks'][0]['total_annotations'] == 0
    assert client_for(bot).get(f'/api/annotations/{env["task"].annotations.first().pk}/').status_code == 403


def test_service_import_cannot_fabricate_other_annotators_submissions(env):
    bot = make_user(env['project'].organization, 'source-importer@x.io', OrganizationMember.Role.QA)
    account = ServiceAccount.objects.create(user=bot, name='source importer', scopes=['imports:write'])
    account.projects.add(env['project'])
    before = Task.objects.filter(project=env['project']).count()
    response = client_for(bot).post(f'/api/projects/{env["project"].pk}/import', [{
        'data': {'text': 'forged result'},
        'annotations': [{'result': [], 'completed_by': env['b'].pk}],
    }], format='json', HTTP_IDEMPOTENCY_KEY='source-import-no-results')
    assert response.status_code == 400, response.data
    assert Task.objects.filter(project=env['project']).count() == before


def test_native_webhooks_require_admin_and_explicit_approval(env, monkeypatch):
    monkeypatch.setattr('webhooks.api.validate_target_url', lambda _: None)
    url = 'https://hooks.example.test/native'
    payload = {'url': url, 'project': env['project'].pk, 'send_payload': True}
    assert client_for(env['a']).post('/api/webhooks/', payload, format='json').status_code == 403
    assert client_for(env['qa']).post('/api/webhooks/', payload, format='json').status_code == 403
    response = client_for(env['admin']).post('/api/webhooks/', payload, format='json')
    assert response.status_code == 201
    hook = Webhook.objects.get(pk=response.json()['id'])
    assert hook.approved_by_id == env['admin'].pk
    set_policy(env['project'], blind_mode=True)
    deliveries = []
    monkeypatch.setattr('collab.webhooks._post_once', lambda url, body, headers: deliveries.append(json.loads(body)) or 200)
    private = {'annotation': {'id': 12, 'result': [{'secret': 'private'}]}}
    run_webhook_sync(hook, 'ANNOTATION_CREATED', private)
    assert deliveries[-1]['annotation']['result']
    hook.approved_by = None
    hook.save()
    run_webhook_sync(hook, 'ANNOTATION_CREATED', private)
    assert deliveries[-1]['annotation'] == {'id': 12}
