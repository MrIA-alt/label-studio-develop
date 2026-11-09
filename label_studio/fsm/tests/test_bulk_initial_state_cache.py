import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from uuid import UUID

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from collab.tests.test_blind_mode import env  # noqa: F401
from core.current_request import CurrentContext
from fsm.bulk_state_cache import (
    CACHE_MISS,
    bulk_create_with_initial_states,
    cache_new_entity_states,
    get_cached_initial_state,
)
from fsm.state_models import AnnotationState, ProjectState, TaskState
from tasks.models import Task
from tasks.serializers import TaskWithAnnotationsSerializer

pytestmark = pytest.mark.django_db(transaction=True)


def test_cache_only_reads_newly_inserted_entities_and_tracks_each_state_write(env, django_assert_num_queries):
    existing = env['task']
    TaskState.objects.create(task=existing, project_id=env['project'].pk, state='CREATED')
    with transaction.atomic(), cache_new_entity_states():
        task = bulk_create_with_initial_states(
            Task.objects, [Task(project=env['project'], data={'text': 'new'})]
        )[0]
        with django_assert_num_queries(0):
            assert TaskState.get_current_state(task) is None
        with django_assert_num_queries(1):
            assert TaskState.get_current_state_value(existing) == 'CREATED'
        state = TaskState.objects.create(task=task, project_id=env['project'].pk, state='IN_PROGRESS')
        with django_assert_num_queries(0):
            assert TaskState.get_current_state(task).pk == state.pk
            assert TaskState.get_current_state_value(task) == 'IN_PROGRESS'
        TaskState.objects.create(id=UUID(int=1), task=task, project_id=env['project'].pk, state='CREATED')
        with django_assert_num_queries(0):
            assert TaskState.get_current_state_value(task) == 'IN_PROGRESS'
        assert Context().run(get_cached_initial_state, TaskState, task) is CACHE_MISS
        inherited = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(inherited.run, get_cached_initial_state, TaskState, task).result() is CACHE_MISS

        async def another_task():
            return get_cached_initial_state(TaskState, task)

        assert asyncio.run(another_task()) is CACHE_MISS
        TaskState.objects.create(task=existing, project_id=env['project'].pk, state='COMPLETED')
        with django_assert_num_queries(1):
            assert TaskState.get_current_state_value(existing) == 'COMPLETED'
        with pytest.raises(ValueError, match='newly constructed'):
            bulk_create_with_initial_states(Task.objects, [existing])
    with django_assert_num_queries(1):
        assert TaskState.get_current_state_value(task) == 'IN_PROGRESS'


def test_rollback_discards_the_cache_before_an_entity_identifier_can_be_reused(env, django_assert_num_queries):
    with pytest.raises(RuntimeError, match='roll back'):
        with transaction.atomic(), cache_new_entity_states():
            task = bulk_create_with_initial_states(
                Task.objects, [Task(project=env['project'], data={'text': 'rolled back'})]
            )[0]
            task_id = task.pk
            TaskState.objects.create(task=task, project_id=env['project'].pk, state='COMPLETED')
            assert TaskState.get_current_state_value(task) == 'COMPLETED'
            raise RuntimeError('roll back')
    replacement = Task.objects.create(id=task_id, project=env['project'], data={'text': 'replacement'})
    with django_assert_num_queries(1):
        assert TaskState.get_current_state_value(replacement) is None
    with pytest.raises(RuntimeError, match='enclosing transaction'):
        with cache_new_entity_states():
            pass


def test_caught_savepoint_rollback_invalidates_uncommitted_state_reads(env, django_assert_num_queries):
    with transaction.atomic(), cache_new_entity_states():
        task = bulk_create_with_initial_states(
            Task.objects, [Task(project=env['project'], data={'text': 'new'})]
        )[0]
        with pytest.raises(RuntimeError, match='inner rollback'):
            with transaction.atomic():
                TaskState.objects.create(task=task, project_id=env['project'].pk, state='COMPLETED')
                assert TaskState.get_current_state_value(task) == 'COMPLETED'
                raise RuntimeError('inner rollback')
        with django_assert_num_queries(1):
            assert TaskState.get_current_state_value(task) is None
        TaskState.objects.create(task=task, project_id=env['project'].pk, state='CREATED')
        assert TaskState.get_current_state_value(task) == 'CREATED'
    assert list(TaskState.objects.filter(task=task).values_list('state', flat=True)) == ['CREATED']


def test_bulk_import_preserves_all_fsm_history_and_hooks_without_repeated_state_reads(env):
    CurrentContext.set_user(env['admin'])
    CurrentContext.set('fsm_enabled_cached', True)
    serializer = TaskWithAnnotationsSerializer(
        data=[{'data': {'text': f'document {index}'}, 'annotations': [{'result': []}]} for index in range(10)],
        many=True, context={'project': env['project'], 'user': env['admin']},
    )
    serializer.is_valid(raise_exception=True)
    with CaptureQueriesContext(connection) as queries:
        tasks = serializer.save()
    current_state_reads = [
        entry['sql'] for entry in queries.captured_queries
        if entry['sql'].lstrip().upper().startswith('SELECT')
        and ('FROM "fsm_taskstate"' in entry['sql'] or 'FROM "fsm_annotationstate"' in entry['sql'])
    ]
    assert not current_state_reads
    assert TaskState.objects.filter(task__in=tasks, state='COMPLETED').count() == 10
    assert AnnotationState.objects.filter(task_id__in=[task.pk for task in tasks], state='CREATED').count() == 10
    assert set(TaskState.objects.filter(task__in=tasks).values_list('triggered_by_id', flat=True)) == {env['admin'].pk}
    # The fixture's original task remains unlabeled alongside the ten completed imports.
    assert ProjectState.get_current_state_value(env['project']) == 'IN_PROGRESS'
    assert all(task.annotations.count() == 1 for task in tasks)
