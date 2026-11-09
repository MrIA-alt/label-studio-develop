import pytest

from collab.tests.test_agent_security import service
from collab.tests.test_blind_mode import CONFIG, annotate, client_for, env, result  # noqa: F401
from projects.models import Project
from tasks.models import Annotation, Task

pytestmark = pytest.mark.django_db


def test_service_read_scope_does_not_grant_writes_to_another_authors_legacy_annotation(env):
    annotation = annotate(env, env['a'], 'a')
    bot, account, _ = service(env, ['results:read', 'annotations:write', 'annotations:delete'])
    url = f'/api/annotations/{annotation.pk}/'
    assert bot.get(url).status_code == 200
    assert bot.patch(url, {'result': result('b'), 'revision': 1}, format='json').status_code == 403
    assert bot.delete(url).status_code == 403
    assert bot.post(f'/api/annotations/{annotation.pk}/convert-to-draft').status_code == 403
    annotation.refresh_from_db()
    assert annotation.result == result('a') and annotation.completed_by_id == env['a'].pk
    own = Annotation.objects.create(project=env['project'], task=env['task'], completed_by=account.user, result=result('a'))
    assert bot.patch(f'/api/annotations/{own.pk}/', {'result': result('b'), 'revision': 1}, format='json').status_code == 200


def test_native_task_create_checks_the_target_project_not_only_action_scope(env):
    other = Project.objects.create(title='Outside scope', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG)
    bot, _, _ = service(env, ['imports:write'])
    assert bot.post('/api/tasks/', {'project': other.pk, 'data': {'text': 'injected'}}, format='json', HTTP_IDEMPOTENCY_KEY='foreign-task').status_code in (403, 404)
    assert not Task.objects.filter(project=other).exists()
    created = bot.post('/api/tasks/', {'project': env['project'].pk, 'data': {'text': 'authorized'}}, format='json', HTTP_IDEMPOTENCY_KEY='own-task')
    assert created.status_code == 201


def test_task_project_cannot_be_changed_through_a_query_parameter_precedence_trick(env):
    other = Project.objects.create(title='Other', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG)
    bot, _, _ = service(env, ['tasks:write'])
    url = f'/api/tasks/{env["task"].pk}/?project={env["project"].pk}'
    response = bot.patch(url, {'project': other.pk}, format='json')
    assert response.status_code in (400, 403, 404)
    env['task'].refresh_from_db()
    assert env['task'].project_id == env['project'].pk
    updated = bot.patch(url, {'data': {'text': 'corrected source'}}, format='json')
    assert updated.status_code == 200
    env['task'].refresh_from_db()
    assert env['task'].data == {'text': 'corrected source'}
