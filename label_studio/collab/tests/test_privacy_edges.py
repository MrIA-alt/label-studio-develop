"""Exercise native membership, ML and FSM paths at campaign privacy boundaries."""

from types import SimpleNamespace

import pytest
from collab.models import ServiceAccount
from collab.services import assign_task
from core.current_request import CurrentContext
from fsm.state_choices import TaskStateChoices
from fsm.state_manager import StateManager
from ml.models import MLBackend
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Task

from .test_blind_mode import CONFIG, annotate, client_for, env, make_user, result, set_policy  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_fsm_cache():
    # Rolled-back SQLite tests reuse entity IDs; runtime cache entries do not
    # participate in those rollbacks and must not stand in for persisted states.
    from fsm.state_manager import get_fsm_cache

    get_fsm_cache().clear()
    yield
    get_fsm_cache().clear()


def hidden_project(env):
    project = Project.objects.create(
        title='Hidden campaign', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG
    )
    ProjectMember.objects.create(project=project, user=env['b'], enabled=True)
    task = Task.objects.create(project=project, data={'text': 'hidden source'})
    Annotation.objects.create(task=task, project=project, completed_by=env['b'], result=result('b'))
    return project


def test_member_detail_and_list_do_not_disclose_hidden_campaign_or_blind_contributions(env):
    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    assign_task(env['task'], env['a'])
    annotate(env, env['b'], 'b')
    hidden = hidden_project(env)
    client = client_for(env['a'])
    assert client.get(f'/api/projects/{hidden.pk}/').status_code in (403, 404)
    base = f'/api/organizations/{env["project"].organization_id}/memberships'
    detail = client.get(f'{base}/{env["b"].pk}/?contributed_to_projects=true')
    assert detail.status_code == 200
    assert detail.data['annotations_count'] == detail.data['contributed_projects_count'] == 0
    assert detail.data['contributed_to_projects'] == []
    assert all(p['id'] != hidden.pk for p in detail.data['created_projects'])
    listing = client.get(f'{base}?contributed_to_projects=true')
    assert listing.status_code == 200
    bob = next(m for m in listing.data['results'] if m['user']['id'] == env['b'].pk)
    assert bob['contributed_to_projects'] == bob['user']['contributed_to_projects'] == []
    assert hidden.title not in str(listing.data)


@pytest.mark.parametrize('role,expected', [('a', 1), ('qa', 2), ('admin', 2)])
def test_membership_counts_preserve_own_blind_and_authorized_reviewer_access(env, role, expected):
    set_policy(env['project'], blind_mode=True)
    annotate(env, env['a'], 'a')
    annotate(env, env['b'], 'b')
    client = client_for(env[role])
    own = client.get(f'/api/organizations/{env["project"].organization_id}/memberships/{env["a"].pk}/')
    other = client.get(f'/api/organizations/{env["project"].organization_id}/memberships/{env["b"].pk}/')
    assert own.status_code == other.status_code == 200
    assert own.data['annotations_count'] + other.data['annotations_count'] == expected


def test_legacy_member_counts_still_include_authorized_organization_projects(env):
    annotate(env, env['b'], 'b')
    response = client_for(env['a']).get(
        f'/api/organizations/{env["project"].organization_id}/memberships/{env["b"].pk}/?contributed_to_projects=true'
    )
    assert response.status_code == 200
    assert response.data['annotations_count'] == 1
    assert response.data['contributed_to_projects'] == [{'id': env['project'].pk, 'title': env['project'].title}]


@pytest.fixture
def echo_backend(env, monkeypatch):
    backend = MLBackend.objects.create(project=env['project'], url='https://ml.example.test', is_interactive=True)
    calls = []

    def predict(self, **kwargs):
        calls.append(kwargs['tasks'])
        return SimpleNamespace(is_error=False, response={'results': kwargs['tasks']})

    monkeypatch.setattr('ml.models.MLApi.make_predictions', predict)
    return backend, calls


def test_interactive_predictions_reject_unassigned_tasks_before_external_call(env, echo_backend):
    set_policy(env['project'], blind_mode=True, assignment_mode='manual', allow_predictions=True)
    assign_task(env['task'], env['b'])
    annotate(env, env['b'], 'b')
    backend, calls = echo_backend
    client = client_for(env['a'])
    assert client.get(f'/api/tasks/{env["task"].pk}/').status_code in (403, 404)
    response = client.post(
        f'/api/ml/{backend.pk}/interactive-annotating', {'task': str(env['task'].pk), 'context': {}}, format='json'
    )
    assert response.status_code in (403, 404)
    assert calls == []


@pytest.mark.parametrize('role', ['a', 'qa', 'admin'])
def test_interactive_serializer_obeys_blind_boundary_and_keeps_reviewers_authorized(env, echo_backend, role):
    set_policy(env['project'], blind_mode=True, assignment_mode='manual', allow_predictions=True)
    assign_task(env['task'], env['a'])
    a = annotate(env, env['a'], 'a')
    b = annotate(env, env['b'], 'b')
    backend, calls = echo_backend
    response = client_for(env[role]).post(
        f'/api/ml/{backend.pk}/interactive-annotating', {'task': env['task'].pk, 'context': {}}, format='json'
    )
    assert response.status_code == 200, response.data
    expected = {a.pk} if role == 'a' else {a.pk, b.pk}
    assert {annotation['id'] for annotation in response.data['data']['annotations']} == expected
    assert {annotation['id'] for annotation in calls[0][0]['annotations']} == expected


@pytest.mark.parametrize('role', ['a', 'qa', 'admin'])
def test_prediction_opt_out_is_enforced_for_annotators_without_blocking_reviewers(env, echo_backend, role):
    set_policy(env['project'], blind_mode=True, allow_predictions=False)
    backend, calls = echo_backend
    response = client_for(env[role]).post(
        f'/api/ml/{backend.pk}/interactive-annotating', {'task': env['task'].pk, 'context': {}}, format='json'
    )
    assert response.status_code == (403 if role == 'a' else 200), response.data
    assert len(calls) == (0 if role == 'a' else 1)


def test_source_only_service_cannot_send_other_authors_results_to_interactive_backend(env, echo_backend):
    set_policy(env['project'], blind_mode=True, allow_predictions=True)
    annotate(env, env['b'], 'b')
    bot = make_user(env['project'].organization, 'ml-source-reader@x.io', 'qa')
    account = ServiceAccount.objects.create(user=bot, name='source reader', scopes=['tasks:read', 'tasks:write'])
    account.projects.add(env['project'])
    backend, calls = echo_backend
    client = client_for(bot)
    response = client.post(
        f'/api/ml/{backend.pk}/interactive-annotating', {'task': env['task'].pk, 'context': {}}, format='json'
    )
    assert response.status_code == 200, response.data
    assert response.data['data']['annotations'] == calls[0][0]['annotations'] == []
    assert response.data['data']['total_annotations'] == 0
    account.scopes = ['progress:read']
    account.save()
    bot.refresh_from_db()
    assert client_for(bot).post(
        f'/api/ml/{backend.pk}/interactive-annotating', {'task': env['task'].pk, 'context': {}}, format='json'
    ).status_code == 403


@pytest.fixture
def configure_backend(monkeypatch):
    from ml.serializers import MLBackendSerializer

    calls = []
    monkeypatch.setattr('ml.serializers.validate_upload_url', lambda *args, **kwargs: None)
    monkeypatch.setattr(MLBackendSerializer, '_validate_healthcheck', lambda self, attrs: calls.append('health'))
    monkeypatch.setattr(MLBackendSerializer, '_validate_setup', lambda self, attrs: calls.append('setup'))
    monkeypatch.setattr(MLBackend, 'update_state', lambda self: None)
    return calls


def test_ml_configuration_is_admin_only_and_project_scoped_before_network_calls(env, configure_backend):
    from organizations.models import Organization
    from users.models import User

    owner = User.objects.create(username='foreign-ml-owner', email='foreign-ml@x.io')
    organization = Organization.create_organization(created_by=owner, title='Foreign ML organization')
    foreign = Project.objects.create(organization=organization, created_by=owner, title='Foreign ML campaign')
    payload = {'project': env['project'].pk, 'url': 'https://ml.example.test', 'title': 'Configured ML'}
    assert client_for(env['qa']).post('/api/ml/', payload, format='json').status_code == 403
    assert configure_backend == []
    assert client_for(env['admin']).post('/api/ml/', {**payload, 'project': foreign.pk}, format='json').status_code == 403
    assert configure_backend == []
    created = client_for(env['admin']).post('/api/ml/', payload, format='json')
    assert created.status_code == 201, created.data
    assert configure_backend == ['health', 'setup']
    identifier = created.data['id']
    assert client_for(env['qa']).patch(f'/api/ml/{identifier}', {'title': 'QA changed'}, format='json').status_code == 403
    assert client_for(env['qa']).delete(f'/api/ml/{identifier}').status_code == 403
    moved = client_for(env['admin']).patch(f'/api/ml/{identifier}', {'project': foreign.pk}, format='json')
    assert moved.status_code in (400, 403)
    assert MLBackend.objects.get(pk=identifier).project_id == env['project'].pk


def test_service_project_editing_does_not_delegate_external_ml_configuration(env, configure_backend):
    allowed = env['project']
    hidden = hidden_project(env)
    bot = make_user(allowed.organization, 'ml-configurator@x.io', 'qa')
    account = ServiceAccount.objects.create(user=bot, name='ML configurator', scopes=['projects:write'])
    account.projects.add(allowed)
    payload = {'project': allowed.pk, 'url': 'https://ml.example.test', 'title': 'Agent ML'}
    assert client_for(bot).post('/api/ml/', {**payload, 'project': hidden.pk}, format='json').status_code == 403
    assert configure_backend == []
    response = client_for(bot).post('/api/ml/', payload, format='json')
    assert response.status_code == 403, response.data
    assert configure_backend == []
    account.scopes = ['projects:read']
    account.save()
    bot.refresh_from_db()
    assert client_for(bot).post('/api/ml/', payload, format='json').status_code == 403


@pytest.mark.parametrize('role', ['a', 'qa', 'admin'])
def test_global_fsm_histories_cannot_be_used_as_blind_activity_oracles(env, role):
    set_policy(env['project'], blind_mode=True, required_annotations=2)
    CurrentContext.set_user(env['b'])
    annotate(env, env['b'], 'b')
    client = client_for(env[role])
    url = f'/api/fsm/entities/task/{env["task"].pk}/history'
    response = client.get(url)
    assert response.status_code == 200, response.data
    if role == 'a':
        assert response.data['count'] == 0
        filtered = client.get(url, {'state': 'IN_PROGRESS'})
        assert filtered.data['count'] == 0
    else:
        assert response.data['count'] > 0
    project = client.get(f'/api/fsm/entities/project/{env["project"].pk}/history')
    assert project.status_code == 200
    if role == 'a':
        assert project.data['count'] == 0


def test_native_fsm_waits_for_distinct_non_cancelled_authors_and_reopens_on_update(env):
    set_policy(env['project'], blind_mode=True, required_annotations=3)
    CurrentContext.set_user(env['admin'])
    third = make_user(env['project'].organization, 'third-fsm@x.io', 'labeller')
    annotate(env, env['a'], 'a')
    annotate(env, env['a'], 'a')  # imported historical duplicate must not count twice
    Annotation.objects.create(task=env['task'], project=env['project'], completed_by=env['b'], was_cancelled=True)
    Annotation.objects.create(task=env['task'], project=env['project'], completed_by=third, ground_truth=True)
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.IN_PROGRESS
    assert client_for(env['admin']).post(
        f'/api/fsm/entities/task/{env["task"].pk}/transition/', {'transition_name': 'task_completed'}, format='json'
    ).status_code == 400
    CurrentContext.set_user(env['admin'])
    annotate(env, env['b'], 'b')
    last = Annotation.objects.create(
        task=env['task'], project=env['project'], completed_by=third, result=result('a')
    )
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.COMPLETED
    last.result = None
    last.save()
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.IN_PROGRESS


def test_policy_threshold_resynchronization_preserves_native_states_and_respects_disabled_flag(env):
    from fsm.functions import sync_collaborative_task_states
    from fsm.state_models import TaskState
    from tasks.models import bulk_update_stats_project_tasks

    policy = set_policy(env['project'], blind_mode=True, required_annotations=1)
    CurrentContext.set_user(env['admin'])
    annotate(env, env['a'], 'a')
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.COMPLETED
    policy.required_annotations = 2
    policy.save()
    bulk_update_stats_project_tasks(env['project'].tasks.all(), env['project'])
    sync_collaborative_task_states(env['project'], user=env['admin'])
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.IN_PROGRESS
    policy.required_annotations = 1
    policy.save()
    bulk_update_stats_project_tasks(env['project'].tasks.all(), env['project'])
    sync_collaborative_task_states(env['project'], user=env['admin'])
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.COMPLETED
    count = TaskState.objects.count()
    CurrentContext.set_fsm_disabled(True)
    try:
        sync_collaborative_task_states(env['project'], user=env['admin'])
        assert TaskState.objects.count() == count
    finally:
        CurrentContext.set_fsm_disabled(False)


def test_fsm_resynchronization_uses_persisted_state_after_transaction_rollback(env):
    from django.db import transaction
    from fsm.functions import sync_collaborative_task_state

    set_policy(env['project'], blind_mode=True, required_annotations=2)
    CurrentContext.set_user(env['admin'])
    annotate(env, env['a'], 'a')
    with pytest.raises(ValueError):
        with transaction.atomic():
            annotate(env, env['b'], 'b')
            assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.COMPLETED
            raise ValueError('rollback the second author')
    sync_collaborative_task_state(env['task'], user=env['admin'])
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.IN_PROGRESS


def test_labeller_cannot_manually_override_native_fsm_states(env):
    set_policy(env['project'], blind_mode=True, required_annotations=2)
    response = client_for(env['a']).post(
        f'/api/fsm/entities/task/{env["task"].pk}/transition/', {'transition_name': 'task_completed'}, format='json'
    )
    assert response.status_code == 403


def test_legacy_fsm_creation_and_history_are_preserved(env):
    CurrentContext.set_user(env['a'])
    annotate(env, env['b'], 'b')
    assert StateManager.get_current_state_value(env['task']) == TaskStateChoices.COMPLETED
    response = client_for(env['a']).get(f'/api/fsm/entities/task/{env["task"].pk}/history')
    assert response.status_code == 200
    assert response.data['count'] > 0


def authenticated_service(env, scopes, name='fsm-service'):
    from collab.authentication import issue_service_token
    from rest_framework.test import APIClient

    bot = make_user(env['project'].organization, name + '@x.io', 'qa')
    account = ServiceAccount.objects.create(user=bot, name=name, scopes=scopes)
    account.projects.add(env['project'])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Service ' + issue_service_token(account))
    return bot, client


def global_state(env, entity_name):
    from fsm.state_models import ProjectState, TaskState

    fields = {
        'organization_id': env['project'].organization_id,
        'state': 'IN_PROGRESS',
        'previous_state': 'CREATED',
        'triggered_by': env['b'],
        'context_data': {'private_review': 'Do not disclose', 'total_annotations': 1},
        'reason': 'Private previous event',
    }
    if entity_name == 'task':
        return TaskState.objects.create(task=env['task'], project_id=env['project'].pk, **fields)
    return ProjectState.objects.create(project=env['project'], **fields)


@pytest.mark.parametrize('entity_name', ['task', 'project'])
@pytest.mark.parametrize('complete', [False, True])
def test_source_only_service_cannot_use_global_fsm_transition_responses_as_an_oracle(env, entity_name, complete):
    set_policy(env['project'], blind_mode=True, required_annotations=2)
    CurrentContext.set_user(env['admin'])
    annotate(env, env['b'], 'b')
    if complete:
        annotate(env, env['a'], 'a')
    record = global_state(env, entity_name)
    scope = 'tasks:write' if entity_name == 'task' else 'projects:write'
    _, client = authenticated_service(env, [scope, 'tasks:read', 'projects:read'])
    entity = env['task'] if entity_name == 'task' else env['project']
    before = type(record).objects.count()
    response = client.post(
        f'/api/fsm/entities/{entity_name}/{entity.pk}/transition/',
        {'transition_name': 'task_in_progress' if entity_name == 'task' else 'project_in_progress'},
        format='json',
    )
    assert response.status_code == 403, response.data
    assert 'new_state' not in response.data and 'state_record' not in response.data
    assert 'Do not disclose' not in str(response.data)
    assert type(record).objects.count() == before
    history = client.get(f'/api/fsm/entities/{entity_name}/{entity.pk}/history')
    assert history.status_code == 200 and history.data['count'] == 0


def test_authorized_results_service_keeps_native_transition_results(env):
    set_policy(env['project'], blind_mode=True, required_annotations=2)
    CurrentContext.set_user(env['admin'])
    annotate(env, env['a'], 'a')
    record = global_state(env, 'task')
    _, client = authenticated_service(env, ['tasks:write', 'results:read'])
    response = client.post(
        f'/api/fsm/entities/task/{env["task"].pk}/transition/',
        {'transition_name': 'task_in_progress'}, format='json',
    )
    assert response.status_code == 200, response.data
    assert response.data['new_state'] == 'IN_PROGRESS'
    assert response.data['state_record']['id'] == str(record.pk)
    assert response.data['state_record']['context_data']['private_review'] == 'Do not disclose'


@pytest.mark.parametrize('entity_name', ['task', 'project'])
def test_shared_state_serializers_redact_global_states_even_outside_history_views(env, entity_name):
    from fsm.serializers import FSMTransitionExecuteResponseSerializer, StateModelSerializer

    set_policy(env['project'], blind_mode=True)
    record = global_state(env, entity_name)
    bot, _ = authenticated_service(env, ['tasks:write', 'projects:write'])
    request = SimpleNamespace(user=bot)
    assert StateModelSerializer(record, context={'request': request}).data == {}
    response = FSMTransitionExecuteResponseSerializer(
        {'success': True, 'new_state': record.state, 'state_record': record}, context={'request': request}
    ).data
    assert response == {'success': True, 'new_state': None, 'state_record': None}


def test_blind_service_can_read_own_annotation_history_but_not_another_authors(env):
    from fsm.serializers import StateModelSerializer
    from fsm.state_models import AnnotationState

    set_policy(env['project'], blind_mode=True)
    bot, client = authenticated_service(env, ['annotations:write'])
    CurrentContext.set_user(env['admin'])
    own = Annotation.objects.create(task=env['task'], project=env['project'], completed_by=bot, result=result('a'))
    other = annotate(env, env['b'], 'b')
    record = AnnotationState.objects.filter(annotation=own).first()
    response = client.get(f'/api/fsm/entities/annotation/{own.pk}/history')
    assert response.status_code == 200, response.data
    assert response.data['count'] > 0
    assert StateModelSerializer(record, context={'request': SimpleNamespace(user=bot)}).data['id'] == str(record.pk)
    assert client.get(f'/api/fsm/entities/annotation/{other.pk}/history').status_code in (403, 404)


@pytest.mark.parametrize('import_file', [False, True])
@pytest.mark.parametrize('nested', [False, True])
def test_assigned_json_import_sources_can_fetch_separate_image_uploads(env, import_file, nested):
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile

    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    image = FileUpload.objects.create(
        project=env['project'], user=env['admin'], file=SimpleUploadedFile('source image.png', b'Image source')
    )
    document = FileUpload.objects.create(
        project=env['project'], user=env['admin'], file=SimpleUploadedFile('sources.json', b'[{"secret":"private"}]')
    )
    env['task'].file_upload = document if import_file else None
    env['task'].data = (
        {'text': 'hello', 'assets': [{'image': image.url}]} if nested else {'text': 'hello', 'image': image.url}
    )
    env['task'].save()
    assert client_for(env['a']).get(image.url).status_code == 403
    assign_task(env['task'], env['a'])
    response = client_for(env['a']).get(image.url)
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'Image source'
    assert client_for(env['b']).get(image.url).status_code == 403
    assert client_for(env['a']).get(document.url).status_code == 403


@pytest.mark.parametrize('placement', ['annotation', 'draft', 'source_key', 'source_substring'])
def test_result_media_and_non_exact_source_mentions_do_not_grant_upload_access(env, placement):
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile
    from tasks.models import AnnotationDraft

    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    assign_task(env['task'], env['a'])
    private = FileUpload.objects.create(
        project=env['project'], user=env['b'], file=SimpleUploadedFile('private.png', b'Private annotation media')
    )
    if placement == 'annotation':
        Annotation.objects.create(
            task=env['task'], project=env['project'], completed_by=env['b'],
            result=[{'type': 'image', 'value': {'image': private.url}}],
        )
    elif placement == 'draft':
        AnnotationDraft.objects.create(task=env['task'], user=env['b'], result=[{'image': private.url}])
    else:
        env['task'].data = {private.url: 'source key'} if placement == 'source_key' else {'text': 'Text ' + private.url}
        env['task'].save()
    assert client_for(env['a']).get(private.url).status_code == 403


def test_source_upload_references_do_not_cross_project_boundaries(env):
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile

    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    assign_task(env['task'], env['a'])
    foreign = hidden_project(env)
    private = FileUpload.objects.create(
        project=foreign, user=env['b'], file=SimpleUploadedFile('hidden.png', b'Other project media')
    )
    env['task'].data = {'text': 'hello', 'image': private.url}
    env['task'].save()
    assert client_for(env['a']).get(private.url).status_code == 403


@pytest.mark.parametrize('origin,expected', [('configured', 200), ('foreign', 403)])
def test_source_media_absolute_urls_require_the_configured_server_origin(env, settings, origin, expected):
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.HOSTNAME = 'https://labels.example.test'
    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    assign_task(env['task'], env['a'])
    image = FileUpload.objects.create(
        project=env['project'], user=env['admin'], file=SimpleUploadedFile('absolute.png', b'Absolute source')
    )
    host = settings.HOSTNAME if origin == 'configured' else 'https://foreign.example.test'
    env['task'].data = {'text': 'hello', 'image': host + image.url}
    env['task'].save()
    response = client_for(env['a']).get(image.url)
    assert response.status_code == expected
    if expected == 200:
        assert b''.join(response.streaming_content) == b'Absolute source'


def test_source_media_absolute_urls_keep_the_configured_script_prefix_once(env, settings):
    from collab.privacy import can_access_upload
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.HOSTNAME = 'https://labels.example.test/campaigns'
    settings.FORCE_SCRIPT_NAME = '/campaigns'
    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    assign_task(env['task'], env['a'])
    image = FileUpload.objects.create(
        project=env['project'], user=env['admin'], file=SimpleUploadedFile('prefix.png', b'Prefixed source')
    )
    env['task'].data = {'image': 'https://labels.example.test' + image.url}
    env['task'].save()
    assert can_access_upload(env['a'], image)


def test_shared_text_import_is_downloadable_only_when_all_its_source_tasks_are_visible(env):
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile

    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    contents = b'Visible first source\nPrivate second source\n'
    document = FileUpload.objects.create(
        project=env['project'], user=env['admin'], file=SimpleUploadedFile('shared.txt', contents)
    )
    env['task'].file_upload = document
    env['task'].data = {'text': 'Visible first source'}
    env['task'].save()
    second = Task.objects.create(project=env['project'], file_upload=document, data={'text': 'Private second source'})
    assign_task(env['task'], env['a'])
    client = client_for(env['a'])
    response = client.get(document.url)
    assert response.status_code == 403
    assert 'Private second source' not in str(response.data)
    assign_task(second, env['a'])
    response = client.get(document.url)
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == contents


def test_explicit_whole_document_source_uri_allows_access_without_all_manifest_tasks(env):
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile

    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    document = FileUpload.objects.create(
        project=env['project'], user=env['admin'], file=SimpleUploadedFile('whole-document.txt', b'Whole document source')
    )
    env['task'].file_upload = document
    env['task'].data = {'text': 'hello', 'document': document.url}
    env['task'].save()
    Task.objects.create(project=env['project'], file_upload=document, data={'text': 'Another view of the document'})
    assign_task(env['task'], env['a'])
    response = client_for(env['a']).get(document.url)
    assert response.status_code == 200
    assert b''.join(response.streaming_content) == b'Whole document source'


def test_shared_file_shortcut_checks_task_visibility_across_projects(env):
    from data_import.models import FileUpload
    from django.core.files.uploadedfile import SimpleUploadedFile

    set_policy(env['project'], blind_mode=True, assignment_mode='manual')
    document = FileUpload.objects.create(
        project=env['project'], user=env['admin'], file=SimpleUploadedFile('shared-projects.txt', b'Two project sources')
    )
    env['task'].file_upload = document
    env['task'].save()
    foreign = hidden_project(env)
    Task.objects.create(project=foreign, file_upload=document, data={'text': 'Hidden project source'})
    assign_task(env['task'], env['a'])
    assert client_for(env['a']).get(document.url).status_code == 403
