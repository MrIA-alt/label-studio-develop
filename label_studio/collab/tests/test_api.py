import pytest
from collab.models import ProjectAnnotationPolicy, TaskAssignment
from tasks.models import Task

from .test_blind_mode import client_for, env  # noqa: F401

pytestmark = pytest.mark.django_db


def url(env, suffix):
    return f"/api/extensions/v1/projects/{env['project'].id}/{suffix}"


def test_admin_sets_policy_and_syncs_overlap(env):
    resp = client_for(env['admin']).put(
        url(env, 'policy'),
        {'required_annotations': 3, 'blind_mode': True, 'assignment_mode': 'manual'},
        format='json',
    )
    assert resp.status_code == 200, resp.content
    env['project'].refresh_from_db()
    assert env['project'].maximum_annotations == 3
    assert ProjectAnnotationPolicy.objects.get(project=env['project']).blind_mode is True


def test_non_admin_cannot_change_policy(env):
    for user in (env['a'], env['qa']):
        resp = client_for(user).put(url(env, 'policy'), {'blind_mode': False}, format='json')
        assert resp.status_code == 403


def test_policy_get_without_policy_returns_defaults(env):
    resp = client_for(env['a']).get(url(env, 'policy'))
    assert resp.status_code == 200
    assert resp.json()['blind_mode'] is False


def test_invalid_policy_rejected(env):
    resp = client_for(env['admin']).put(
        url(env, 'policy'), {'required_annotations': 99, 'assignment_mode': 'manual'}, format='json'
    )
    assert resp.status_code == 400


def test_bulk_assign_idempotent_and_my_assignments(env):
    task2 = Task.objects.create(project=env['project'], data={'text': 'x'})
    payload = {'task_ids': [env['task'].id, task2.id], 'user_ids': [env['a'].pk, env['b'].pk]}
    resp = client_for(env['qa']).post(url(env, 'assignments/bulk'), payload, format='json')
    assert resp.status_code == 201
    assert resp.json()['created'] == 4
    again = client_for(env['qa']).post(url(env, 'assignments/bulk'), payload, format='json')
    assert again.json() == {'created': 0, 'already_assigned': 4}
    assert TaskAssignment.objects.count() == 4

    mine = client_for(env['a']).get('/api/extensions/v1/my-assignments')
    assert mine.status_code == 200
    assert {x['user'] for x in mine.json()['results']} == {env['a'].pk}
    assert mine.json()['count'] == 2


def test_labeller_cannot_assign(env):
    payload = {'task_ids': [env['task'].id], 'user_ids': [env['a'].pk]}
    resp = client_for(env['a']).post(url(env, 'assignments/bulk'), payload, format='json')
    assert resp.status_code == 403
    assert TaskAssignment.objects.count() == 0


def test_bulk_assign_unknown_task_rejected(env):
    resp = client_for(env['admin']).post(
        url(env, 'assignments/bulk'), {'task_ids': [999999], 'user_ids': [env['a'].pk]}, format='json'
    )
    assert resp.status_code == 400
