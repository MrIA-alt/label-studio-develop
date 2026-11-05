from datetime import timedelta

import pytest
from collab import services
from collab.models import ProjectAnnotationPolicy, TaskAssignment
from django.utils import timezone
from organizations.models import OrganizationMember
from tasks.models import Task

from .test_blind_mode import client_for, env, make_user, set_policy  # noqa: F401

pytestmark = pytest.mark.django_db


def base(env):
    return f"/api/extensions/v1/projects/{env['project'].id}/assignments"


def make_tasks(env, n):
    return [Task.objects.create(project=env['project'], data={'text': str(i)}) for i in range(n)]


def test_auto_distribution_gives_n_distinct_annotators_balanced(env):
    c = make_user(env['project'].organization, 'c@x.io', OrganizationMember.Role.LABELLER)
    make_tasks(env, 5)  # 6 tasks total
    set_policy(env['project'], assignment_mode='automatic', required_annotations=2)
    resp = client_for(env['admin']).post(f'{base(env)}/distribute', {}, format='json')
    assert resp.status_code == 200
    assert resp.json()['created'] == 12
    for task in Task.objects.filter(project=env['project']):
        users = list(task.assignments.values_list('user_id', flat=True))
        assert len(users) == len(set(users)) == 2
    loads = [TaskAssignment.objects.filter(user=u).count() for u in (env['a'], env['b'], c)]
    assert max(loads) - min(loads) <= 1
    # QA is never auto-assigned
    assert not TaskAssignment.objects.filter(user=env['qa']).exists()
    # idempotent
    assert client_for(env['admin']).post(f'{base(env)}/distribute', {}, format='json').json()['created'] == 0


def test_auto_distribution_respects_cap(env):
    make_tasks(env, 3)
    set_policy(env['project'], assignment_mode='automatic', required_annotations=2, max_active_tasks_per_user=1)
    created = services.distribute_project(env['project'])
    assert created == 2  # two labellers, one active task each
    assert TaskAssignment.objects.filter(user=env['a']).count() == 1


def test_inactive_users_excluded(env):
    env['b'].is_active = False
    env['b'].save()
    set_policy(env['project'], assignment_mode='automatic', required_annotations=2)
    services.distribute_project(env['project'])
    assert not TaskAssignment.objects.filter(user=env['b']).exists()


def test_skipped_task_goes_to_someone_else(env):
    c = make_user(env['project'].organization, 'c@x.io', OrganizationMember.Role.LABELLER)
    set_policy(env['project'], assignment_mode='automatic', required_annotations=1)
    services.distribute_project(env['project'])
    first = TaskAssignment.objects.get(task=env['task'])
    owner = first.user
    resp = client_for(owner).post(f'/api/extensions/v1/assignments/{first.id}/skip')
    assert resp.status_code == 200
    assert resp.json()['status'] == 'SKIPPED'
    services.distribute_project(env['project'])
    second = TaskAssignment.objects.get(task=env['task'], status='ASSIGNED')
    assert second.user_id != owner.pk and second.user_id in (env['a'].pk, env['b'].pk, c.pk)


def test_expiry_then_redistribution(env):
    set_policy(env['project'], assignment_mode='automatic', required_annotations=1, assignment_ttl_hours=1)
    services.distribute_project(env['project'])
    assignment = TaskAssignment.objects.get(task=env['task'])
    assert assignment.expires_at is not None
    TaskAssignment.objects.filter(pk=assignment.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert services.expire_overdue() == 1
    assert TaskAssignment.objects.get(pk=assignment.pk).status == 'EXPIRED'
    services.distribute_project(env['project'])
    assert TaskAssignment.objects.filter(task=env['task'], status='ASSIGNED').count() == 1


def test_owner_actions_only_on_own_assignment(env):
    set_policy(env['project'], assignment_mode='manual')
    assignment, _ = services.assign_task(env['task'], env['a'])
    url = f'/api/extensions/v1/assignments/{assignment.id}/start'
    assert client_for(env['b']).post(url).status_code == 404
    resp = client_for(env['a']).post(url)
    assert resp.status_code == 200 and resp.json()['status'] == 'IN_PROGRESS'
    # invalid transition
    assert client_for(env['a']).post(f'/api/extensions/v1/assignments/{assignment.id}/skip').status_code == 400


def test_staff_reassign_and_cancel(env):
    set_policy(env['project'], assignment_mode='hybrid')
    assignment, _ = services.assign_task(env['task'], env['a'])
    url = f'/api/extensions/v1/assignments/{assignment.id}/reassign/staff'
    assert client_for(env['a']).post(url, {'user_id': env['b'].pk}, format='json').status_code == 403
    resp = client_for(env['qa']).post(url, {'user_id': env['b'].pk}, format='json')
    assert resp.status_code == 200 and resp.json()['user'] == env['b'].pk
    assert TaskAssignment.objects.get(pk=assignment.pk).status == 'CANCELLED'
    new_id = resp.json()['id']
    resp = client_for(env['qa']).post(f'/api/extensions/v1/assignments/{new_id}/cancel/staff')
    assert resp.status_code == 200 and resp.json()['status'] == 'CANCELLED'


def test_task_state_not_complete_after_first_submission(env):
    set_policy(env['project'], assignment_mode='manual', required_annotations=3)
    for user in (env['a'], env['b']):
        services.assign_task(env['task'], user)
    first = TaskAssignment.objects.get(task=env['task'], user=env['a'])
    services.transition(first, 'SUBMITTED')
    summary = client_for(env['qa']).get(f'{base(env)}/summary').json()
    assert summary['tasks_by_state'] == {'IN_PROGRESS': 1}
    assert client_for(env['a']).get(f'{base(env)}/summary').status_code == 403


def test_distribute_requires_auto_mode(env):
    set_policy(env['project'], assignment_mode='manual')
    assert client_for(env['admin']).post(f'{base(env)}/distribute', {}, format='json').status_code == 400
