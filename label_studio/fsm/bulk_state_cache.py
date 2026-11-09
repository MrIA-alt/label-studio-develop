"""Transaction-local state reads for entities inserted by the same bulk import."""

from asyncio import current_task
from contextlib import contextmanager
from contextvars import ContextVar
from threading import get_ident

from django.db import DEFAULT_DB_ALIAS, connections

_new_states = ContextVar('fsm_new_bulk_states', default=None)
_database = ContextVar('fsm_new_bulk_state_database', default=None)
_owner = ContextVar('fsm_new_bulk_state_owner', default=None)
CACHE_MISS = object()


def _execution_owner():
    try:
        task = current_task()
    except RuntimeError:
        task = None
    return get_ident(), task


def _current_states():
    # Copied async/thread contexts must not read another connection's unpublished rows.
    return _new_states.get() if _owner.get() == _execution_owner() else None


@contextmanager
def cache_new_entity_states(using=DEFAULT_DB_ALIAS):
    """Cache only unpublished rows created through ``bulk_create_with_initial_states``.

    The scope ends before its surrounding transaction commits or rolls back. Existing
    entities, including the project's own state, always retain their database reads.
    """
    if not connections[using].in_atomic_block:
        raise RuntimeError('New entity state caching requires an enclosing transaction.')
    states = {}
    token = _new_states.set(states)
    database_token = _database.set(using)
    owner_token = _owner.set(_execution_owner())

    def invalidate_on_rollback(execute, sql, params, many, context):
        # A caught inner savepoint rollback need not exit this outer import scope.
        # Forget cached reads rather than retaining a state record rolled back in SQL.
        if str(sql).lstrip().upper().startswith('ROLLBACK'):
            states.clear()
        return execute(sql, params, many, context)

    try:
        with connections[using].execute_wrapper(invalidate_on_rollback):
            yield
    finally:
        _new_states.reset(token)
        _database.reset(database_token)
        _owner.reset(owner_token)


def bulk_create_with_initial_states(manager, entities, batch_size=None):
    """Insert fresh entities and register their initially absent FSM states.

    Conflict-ignoring and upsert modes are intentionally unsupported: a successfully
    inserted row must be new before an absent state can be assumed without querying.
    """
    from fsm.registry import get_state_model_for_entity

    entities = list(entities)
    if any(not entity._state.adding for entity in entities):
        raise ValueError('Initial state caching only supports newly constructed entities.')
    states = _current_states()
    if states is not None and manager.db != _database.get():
        raise ValueError('Bulk state registration must use the scope database.')
    if states is not None and not connections[manager.db].in_atomic_block:
        raise RuntimeError('Bulk state registration requires an enclosing transaction.')
    created = manager.bulk_create(entities, batch_size=batch_size)
    if states is not None:
        for entity in created:
            state_model = get_state_model_for_entity(entity)
            if state_model is not None:
                states[(state_model, entity._state.db, entity.pk)] = None
    return created


def get_cached_initial_state(state_model, entity):
    states = _current_states()
    if states is None:
        return CACHE_MISS
    return states.get((state_model, entity._state.db or DEFAULT_DB_ALIAS, entity.pk), CACHE_MISS)


def remember_initial_state(record):
    states = _current_states()
    if states is None:
        return
    entity_id = getattr(record, record._get_entity_field_name() + '_id')
    key = (type(record), record._state.db or DEFAULT_DB_ALIAS, entity_id)
    if key in states:
        current = states[key]
        if current is None or record.pk >= current.pk:
            states[key] = record
