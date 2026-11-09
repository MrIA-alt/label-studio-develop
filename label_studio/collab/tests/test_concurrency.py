from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, current_thread

import pytest
from collab import services
from collab.models import TaskAssignment
from collab.tests.test_blind_mode import annotate, client_for, env, result, set_policy  # noqa: F401
from collab.tests.test_blind_mode import make_user
from organizations.models import OrganizationMember
from rest_framework.exceptions import ValidationError
from django.db import close_old_connections, connection
from tasks.models import Annotation, Task
from users.models import User

pytestmark = pytest.mark.django_db(transaction=True)


def postgres_only():
    if connection.vendor != 'postgresql':
        pytest.skip('Real concurrent transactions require PostgreSQL, not SQLite database-wide write locks.')


def parallel(functions):
    barrier = Barrier(len(functions))

    def run(fn):
        close_old_connections()
        try:
            barrier.wait(timeout=15)
            return fn()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(run, functions))


def test_simultaneous_assignment_is_unique(env):
    postgres_only()
    set_policy(env['project'], assignment_mode='manual')

    def assign():
        task = Task.objects.get(pk=env['task'].pk)
        user = User.objects.get(pk=env['a'].pk)
        return services.assign_task(task, user)[1]

    assert sorted(parallel([assign, assign])) == [False, True]
    assert TaskAssignment.objects.count() == 1


def test_simultaneous_saves_keep_independent_results(env):
    postgres_only()
    set_policy(env['project'], blind_mode=True)
    a, b = annotate(env, env['a'], 'a'), annotate(env, env['b'], 'a')

    def update(user_id, annotation_id, choice):
        return client_for(User.objects.get(pk=user_id)).patch(
            f'/api/annotations/{annotation_id}/', {'result': result(choice), 'revision': 1}, format='json'
        ).status_code

    assert parallel([lambda: update(env['a'].pk, a.pk, 'b'), lambda: update(env['b'].pk, b.pk, 'a')]) == [200, 200]
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.result == result('b') and b.result == result('a')
    assert a.revision == b.revision == 2


def test_simultaneous_updates_of_one_revision_conflict(env):
    postgres_only()
    set_policy(env['project'], blind_mode=True)
    annotation = annotate(env, env['a'], 'a')

    def update(choice):
        return client_for(User.objects.get(pk=env['a'].pk)).patch(
            f'/api/annotations/{annotation.pk}/', {'result': result(choice), 'revision': 1}, format='json'
        ).status_code

    assert sorted(parallel([lambda: update('a'), lambda: update('b')])) == [200, 409]
    annotation.refresh_from_db()
    assert annotation.revision == 2
    assert annotation.revisions.count() <= 1


def test_three_simultaneous_native_submissions(env):
    postgres_only()
    c = make_user(env['project'].organization, 'c@x.io', OrganizationMember.Role.LABELLER)
    set_policy(env['project'], blind_mode=True, assignment_mode='manual', required_annotations=3)
    users = (env['a'], env['b'], c)
    for user in users:
        services.assign_task(env['task'], user)

    def submit(user_id, choice):
        return client_for(User.objects.get(pk=user_id)).post(
            f"/api/tasks/{env['task'].pk}/annotations/", {'result': result(choice)}, format='json'
        ).status_code

    assert parallel([lambda u=u, i=i: submit(u.pk, 'a' if i == 0 else 'b') for i, u in enumerate(users)]) == [201, 201, 201]
    assert Annotation.objects.filter(task=env['task']).values('completed_by_id').distinct().count() == 3
    assert TaskAssignment.objects.filter(task=env['task'], status='SUBMITTED').count() == 3


def test_concurrent_automatic_distribution_has_n_distinct_slots(env):
    postgres_only()
    set_policy(env['project'], assignment_mode='automatic', required_annotations=2)

    def distribute():
        from projects.models import Project

        return services.distribute_project(Project.objects.get(pk=env['project'].pk))

    assert sorted(parallel([distribute, distribute])) == [0, 2]
    assert TaskAssignment.objects.filter(task=env['task']).values('user_id').distinct().count() == 2


def test_concurrent_manual_assignment_respects_workload_limit(env):
    postgres_only()
    set_policy(env['project'], assignment_mode='manual', max_active_tasks_per_user=1)
    other = Task.objects.create(project=env['project'], data={'text': 'other'})

    def assign(task_id):
        try:
            services.assign_task(Task.objects.get(pk=task_id), User.objects.get(pk=env['a'].pk))
            return 201
        except ValidationError:
            return 400

    assert sorted(parallel([lambda: assign(env['task'].pk), lambda: assign(other.pk)])) == [201, 400]
    assert TaskAssignment.objects.filter(user=env['a']).count() == 1


def test_review_racing_with_correction_never_accepts_an_unseen_revision(env):
    postgres_only()
    from collab import reviews
    from collab.tests.test_review_agent import submitted, three_annotators

    _, annotation, _ = three_annotators(env)
    submitted(env)
    reviews.apply_review(annotation, env['qa'], 'REJECT', 'Correct this class', expected_revision=1)

    def correct():
        return client_for(User.objects.get(pk=env['b'].pk)).patch(
            f'/api/annotations/{annotation.pk}/', {'revision': 1, 'result': result('a')}, format='json'
        ).status_code

    def accept_old_view():
        return client_for(User.objects.get(pk=env['qa'].pk)).post(
            '/api/extensions/v1/reviews',
            {'annotation': annotation.pk, 'revision': 1, 'decision': 'ACCEPT'}, format='json',
        ).status_code

    correction_status, review_status = parallel([correct, accept_old_view])
    assert correction_status == 200
    assert review_status in (400, 409)
    annotation.refresh_from_db()
    assert annotation.revision == 2 and annotation.review_status == Annotation.REVIEW_PENDING
    assert annotation.reviews.count() == 1


def test_reclaim_waiting_for_an_inflight_submission_preserves_its_result(env, monkeypatch):
    postgres_only()
    from django.db import transaction
    from django.db.models import QuerySet

    set_policy(env['project'], assignment_mode='automatic', required_annotations=1)
    services.assign_task(env['task'], env['a'])
    task_held, allow_submission, reclaim_reached = Event(), Event(), Event()
    original_get = QuerySet.get

    def tracked_get(queryset, *args, **kwargs):
        if queryset.model is Task and queryset.query.select_for_update and current_thread().name.startswith('reclaim'):
            reclaim_reached.set()
        return original_get(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, 'get', tracked_get)

    def submit_authorized_work():
        close_old_connections()
        try:
            user = User.objects.get(pk=env['a'].pk)
            with transaction.atomic():
                task = Task.objects.select_for_update().get(pk=env['task'].pk)
                task_held.set()
                assert allow_submission.wait(timeout=20)
                annotation = Annotation.objects.create(task=task, project=task.project, completed_by=user, result=result('a'))
                services.on_annotation_created(annotation, user)
                return annotation.pk
        finally:
            close_old_connections()

    def redistribute():
        from projects.models import Project

        close_old_connections()
        try:
            return services.distribute_project(Project.objects.get(pk=env['project'].pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='submission') as submitter:
        submission = submitter.submit(submit_authorized_work)
        try:
            assert task_held.wait(timeout=20)
            User.objects.filter(pk=env['a'].pk).update(is_active=False)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='reclaim') as distributor:
                redistribution = distributor.submit(redistribute)
                try:
                    assert reclaim_reached.wait(timeout=20)
                finally:
                    allow_submission.set()
                assert redistribution.result(timeout=30) == 0
            annotation_id = submission.result(timeout=30)
        finally:
            allow_submission.set()
    annotation = Annotation.objects.get(pk=annotation_id)
    assert not annotation.was_cancelled and annotation.result == result('a')
    assert TaskAssignment.objects.get(task=env['task'], user=env['a']).status == 'SUBMITTED'
    assert not TaskAssignment.objects.filter(task=env['task'], user=env['b']).exists()


@pytest.mark.parametrize('path', ['/api/projects/', '/api/extensions/v1/projects'])
def test_simultaneous_machine_project_creation_is_idempotent(env, path):
    postgres_only()
    from collab.models import IdempotencyRecord
    from collab.tests.test_blind_mode import CONFIG
    from projects.models import Project
    from rest_framework.test import APIClient

    account = client_for(env['admin']).post('/api/extensions/v1/service-accounts', {
        'name': 'Concurrent project agent', 'scopes': ['projects:write'], 'project_ids': [],
    }, format='json')
    assert account.status_code == 201
    token = account.data['token']
    before = Project.objects.count()

    def create():
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Service ' + token)
        response = client.post(
            path, {'title': 'Concurrent campaign', 'label_config': CONFIG}, format='json',
            HTTP_IDEMPOTENCY_KEY='concurrent-project',
        )
        return response.status_code, response.data.get('id'), response.get('Idempotent-Replay')

    responses = parallel([create, create])
    assert [item[0] for item in responses] == [201, 201]
    assert responses[0][1] == responses[1][1]
    assert sum(item[2] == 'true' for item in responses) == 1
    assert Project.objects.count() == before + 1
    assert IdempotencyRecord.objects.filter(key='concurrent-project').count() == 1


def test_simultaneous_publication_emits_one_event(env, monkeypatch):
    postgres_only()
    from collab import api
    from collab.models import AuditEvent, WebhookDelivery, WebhookSubscription

    project = env['project']
    project.is_published = False
    project.save(update_fields=['is_published'])
    WebhookSubscription.objects.create(
        project=project, url='https://hooks.example.test/events',
        events=['project.published'], secret='synthetic-test-signing-key',
    )
    fetched = Barrier(2)
    original = api._project_for

    def project_for(*args, **kwargs):
        current = original(*args, **kwargs)
        fetched.wait(timeout=15)
        return current

    monkeypatch.setattr(api, '_project_for', project_for)

    def publish():
        return client_for(User.objects.get(pk=env['admin'].pk)).post(
            f'/api/extensions/v1/projects/{project.pk}/publish', {}, format='json'
        ).status_code

    assert parallel([publish, publish]) == [200, 200]
    assert AuditEvent.objects.filter(project=project, action='project.publish').count() == 1
    assert WebhookDelivery.objects.filter(subscription__project=project, event='project.published').count() == 1
