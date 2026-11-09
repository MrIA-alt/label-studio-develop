"""Protect lazy construction of the native task-read query."""

import pytest
from tasks.models import Task


@pytest.mark.django_db
def test_preparing_without_computed_columns_does_not_execute_sql(django_assert_num_queries):
    # Selection and pagination are applied after this construction step. A
    # first-row probe would query a whole project before either is available.
    with django_assert_num_queries(0):
        query = Task.prepared.annotate_queryset(Task.objects.all(), fields_for_evaluation=[])
    assert query.model is Task
