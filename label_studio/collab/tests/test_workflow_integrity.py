import pytest
from collab import consensus, reviews, services
from collab.models import AnnotationRevision, TaskAssignment
from collab.tests.test_blind_mode import annotate, client_for, env, result, set_policy  # noqa: F401
from collab.tests.test_blind_mode import make_user
from collab.tests.test_review_agent import submitted, three_annotators
from projects.models import ProjectMember
from tasks.models import Annotation, Task, bulk_update_stats_project_tasks
from rest_framework.exceptions import ValidationError
from organizations.models import OrganizationMember

pytestmark = pytest.mark.django_db
EXT = '/api/extensions/v1'


def test_invalid_correction_rolls_back_review_and_history(env):
    _, annotation, _ = three_annotators(env)
    submitted(env)
    reviews.apply_review(annotation, env['qa'], 'REJECT', 'Fix the class', expected_revision=annotation.revision)
    response = client_for(env['b']).patch(
        f'/api/annotations/{annotation.pk}/', {'result': {}, 'revision': 1}, format='json'
    )
    assert response.status_code == 400
    annotation.refresh_from_db()
    assert annotation.review_status == Annotation.REVIEW_REJECTED
    assert annotation.review_comment == 'Fix the class'
    assert annotation.revision == 1
    assert not AnnotationRevision.objects.filter(annotation=annotation).exists()


def test_revision_required_and_stale_update_rejected_without_data_loss(env):
    set_policy(env['project'], blind_mode=True)
    annotation = annotate(env, env['a'], 'a')
    client = client_for(env['a'])
    url = f'/api/annotations/{annotation.pk}/'
    assert client.patch(url, {'result': result('b')}, format='json').status_code == 409
    response = client.patch(url, {'result': result('b')}, format='json', HTTP_IF_MATCH='"1"')
    assert response.status_code == 200
    assert response.json()['revision'] == 2 and response['ETag'] == '"2"'
    assert client.patch(url, {'result': result('a'), 'revision': 1}, format='json').status_code == 409
    annotation.refresh_from_db()
    assert annotation.result == result('b')
    assert annotation.revision == 2
    assert AnnotationRevision.objects.get(annotation=annotation).result == result('a')


@pytest.mark.parametrize('field,value', [('review_status', 'accepted'), ('ground_truth', True), ('task_assignment', 1)])
def test_labeller_cannot_forge_workflow_fields(env, field, value):
    response = client_for(env['a']).post(
        f"/api/tasks/{env['task'].pk}/annotations/", {'result': result('a'), field: value}, format='json'
    )
    assert response.status_code == 403
    assert not Annotation.objects.exists()


def test_manual_assignment_limit_and_membership_are_common_invariants(env):
    set_policy(env['project'], assignment_mode='manual', max_active_tasks_per_user=1)
    services.assign_task(env['task'], env['a'])
    other = Task.objects.create(project=env['project'], data={'text': 'other'})
    with pytest.raises(ValidationError, match='limit'):
        services.assign_task(other, env['a'])
    ProjectMember.objects.create(project=env['project'], user=env['b'], enabled=False)
    with pytest.raises(ValidationError, match='active annotator'):
        services.assign_task(other, env['b'])
    assert TaskAssignment.objects.count() == 1


def test_review_waits_for_n_independent_submissions(env):
    set_policy(env['project'], required_annotations=3, review_mode='manual')
    annotation = annotate(env, env['a'], 'a')
    with pytest.raises(ValidationError, match='required independent'):
        reviews.apply_review(annotation, env['qa'], 'ACCEPT', expected_revision=annotation.revision)
    assert not annotation.reviews.exists()


def test_native_submit_and_skip_drive_assignment_states(env):
    set_policy(env['project'], assignment_mode='manual', required_annotations=2)
    a, _ = services.assign_task(env['task'], env['a'])
    b, _ = services.assign_task(env['task'], env['b'])
    services.transition(b, 'IN_PROGRESS')
    url = f"/api/tasks/{env['task'].pk}/annotations/"
    assert client_for(env['a']).post(url, {'result': result('a')}, format='json').status_code == 201
    assert client_for(env['b']).post(url, {'result': [], 'was_cancelled': True}, format='json').status_code == 201
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.status == 'SUBMITTED' and a.started_at is not None
    assert b.status == 'SKIPPED'
    assert client_for(env['a']).post(url, {'result': result('b')}, format='json').status_code == 400
    assert Annotation.objects.filter(completed_by=env['a']).count() == 1


def test_html_consensus_uses_offsets_and_labels():
    def html(label):
        return [{'from_name': 'h', 'type': 'hypertextlabels', 'value': {
            'start': '/p[1]/text()[1]', 'end': '/p[1]/text()[1]', 'startOffset': 0, 'endOffset': 5,
            'hypertextlabels': [label],
        }}]
    assert consensus.compute_agreement({'A': html('x'), 'B': html('x')})['score'] == 1
    assert consensus.compute_agreement({'A': html('x'), 'B': html('y')})['score'] == 0


def test_assignment_summary_counts_each_annotator(env):
    three_annotators(env)
    submitted(env)
    response = client_for(env['qa']).get(f"{EXT}/projects/{env['project'].pk}/assignments/summary")
    assert response.status_code == 200
    assert response.json()['tasks_by_state'] == {'COMPLETE': 1}


def test_three_real_annotators_submit_independent_native_results(env):
    c = make_user(env['project'].organization, 'c@x.io', OrganizationMember.Role.LABELLER)
    policy = client_for(env['admin']).put(
        f"{EXT}/projects/{env['project'].pk}/policy",
        {'required_annotations': 3, 'blind_mode': True, 'assignment_mode': 'manual', 'review_mode': 'manual'},
        format='json',
    )
    assert policy.status_code == 200
    for i, user in enumerate((env['a'], env['b'], c)):
        services.assign_task(env['task'], user)
        response = client_for(user).post(
            f"/api/tasks/{env['task'].pk}/annotations/", {'result': result('a' if i == 0 else 'b')}, format='json'
        )
        assert response.status_code == 201
        assert response.json()['revision'] == 1 and response.json()['readonly'] is True
        env['task'].refresh_from_db()
        assert env['task'].is_labeled is (i == 2)
        visible = client_for(user).get(f"/api/tasks/{env['task'].pk}/annotations/").json()
        assert len(visible) == 1 and visible[0]['completed_by'] == user.pk
    assert Annotation.objects.filter(task=env['task']).count() == 3
    task_review = client_for(env['qa']).get(f"{EXT}/tasks/{env['task'].pk}/review")
    assert task_review.status_code == 200 and len(task_review.json()['annotations']) == 3


def test_completion_and_bulk_recalculation_require_distinct_original_authors(env):
    set_policy(env['project'], required_annotations=3)
    annotate(env, env['a'], 'a')
    annotate(env, env['a'], 'b')
    annotate(env, env['b'], 'b')
    Annotation.objects.create(
        task=env['task'], project=env['project'], completed_by=env['qa'], result=result('a'), ground_truth=True
    )
    bulk_update_stats_project_tasks(Task.objects.filter(pk=env['task'].pk), env['project'])
    env['task'].refresh_from_db()
    assert env['task'].is_labeled is False
    assert env['task'].has_lock(env['a']) is False
    c = make_user(env['project'].organization, 'c@x.io', OrganizationMember.Role.LABELLER)
    annotate(env, c, 'a')
    env['task'].refresh_from_db()
    assert env['task'].is_labeled is True


def test_agreement_cache_updates_after_change_and_delete(env):
    set_policy(env['project'], required_annotations=2)
    annotate(env, env['a'], 'a')
    annotation = annotate(env, env['b'], 'a')
    env['task'].refresh_from_db()
    assert env['task'].precomputed_agreement == 1.0
    annotation.result = result('b')
    annotation.save(update_fields=['result'])
    env['task'].refresh_from_db()
    assert env['task'].precomputed_agreement == 0.0
    annotation.delete()
    env['task'].refresh_from_db()
    assert env['task'].precomputed_agreement is None


def test_native_submission_replay_does_not_duplicate_annotation(env):
    set_policy(env['project'], assignment_mode='manual')
    services.assign_task(env['task'], env['a'])
    url = f"/api/tasks/{env['task'].pk}/annotations/"
    client = client_for(env['a'])
    first = client.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='submission-1')
    replay = client.post(url, {'result': result('a')}, format='json', HTTP_IDEMPOTENCY_KEY='submission-1')
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert Annotation.objects.filter(task=env['task'], completed_by=env['a']).count() == 1


@pytest.mark.parametrize('field', ['completed_by', 'review_status', 'ground_truth', 'was_cancelled'])
def test_qa_cannot_take_ownership_or_mutate_original_metadata(env, field):
    set_policy(env['project'], blind_mode=True)
    annotation = annotate(env, env['a'], 'a')
    values = {'completed_by': env['qa'].pk, 'review_status': 'accepted', 'ground_truth': True, 'was_cancelled': True}
    response = client_for(env['qa']).patch(
        f'/api/annotations/{annotation.pk}/', {'revision': 1, field: values[field]}, format='json',
    )
    assert response.status_code == 403
    annotation.refresh_from_db()
    assert annotation.completed_by_id == env['a'].pk
    assert annotation.review_status == 'pending'
    assert annotation.ground_truth is False and annotation.was_cancelled is False
    assert annotation.revision == 1


def test_admin_can_correct_results_but_cannot_silently_move_annotation_ownership(env):
    set_policy(env['project'], blind_mode=True)
    annotation = annotate(env, env['a'], 'a')
    client = client_for(env['admin'])
    url = f'/api/annotations/{annotation.pk}/'
    assert client.patch(url, {'revision': 1, 'completed_by': env['admin'].pk}, format='json').status_code == 403
    assert client.patch(url, {'revision': 1, 'result': result('b')}, format='json').status_code == 200
    annotation.refresh_from_db()
    assert annotation.completed_by_id == env['a'].pk and annotation.result == result('b')


def test_reopening_skipped_open_task_preserves_original_and_resumes_one_draft(env):
    set_policy(env['project'], blind_mode=True, assignment_mode='open', review_mode='none')
    annotation = Annotation.objects.create(project=env['project'], task=env['task'], completed_by=env['a'], result=[], was_cancelled=True)
    client = client_for(env['a'])
    url = f'/api/annotations/{annotation.pk}/convert-to-draft'
    first = client.post(url)
    replay = client.post(url)
    assert first.status_code == replay.status_code == 201
    assert first.json()['id'] == replay.json()['id']
    assert first.json()['retained_annotation_id'] == annotation.pk
    assert Annotation.objects.filter(pk=annotation.pk, was_cancelled=True).exists()


def test_review_and_expiration_roll_back_when_outbox_persistence_fails(env, monkeypatch):
    from datetime import timedelta
    from django.utils import timezone
    annotation, _, _ = three_annotators(env)
    submitted(env)
    monkeypatch.setattr('collab.webhooks.emit', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('outbox unavailable')))
    with pytest.raises(RuntimeError, match='outbox unavailable'):
        reviews.apply_review(annotation, env['qa'], 'ACCEPT', expected_revision=annotation.revision)
    annotation.refresh_from_db()
    assert annotation.review_status == 'pending' and not annotation.reviews.exists()
    other = Task.objects.create(project=env['project'], data={'text': 'expiry test'})
    assignment, _ = services.assign_task(other, env['a'])
    TaskAssignment.objects.filter(pk=assignment.pk).update(expires_at=timezone.now() - timedelta(hours=1))
    with pytest.raises(RuntimeError, match='outbox unavailable'):
        services.expire_overdue()
    assignment.refresh_from_db()
    assert assignment.status == 'ASSIGNED'
