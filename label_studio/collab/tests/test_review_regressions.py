from datetime import timedelta

import pytest
from django.utils import timezone

from collab import consensus, reviews, services
from collab.models import AnnotationReview, AuditEvent, TaskAssignment, WebhookDelivery, WebhookSubscription
from collab.tests.test_blind_mode import annotate, client_for, env, make_user, result, set_policy  # noqa: F401
from collab.tests.test_review_agent import submitted, three_annotators
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from tasks.models import Annotation

pytestmark = pytest.mark.django_db
EXT = '/api/extensions/v1'


@pytest.mark.parametrize('native', [False, True])
def test_review_requires_the_revision_displayed_to_the_supervisor(env, native):
    _, annotation, _ = three_annotators(env)
    submitted(env)
    qa = client_for(env['qa'])
    url = f'/api/annotations/{annotation.pk}/review' if native else f'{EXT}/reviews'

    def body(decision, revision=None):
        payload = {'status': decision.lower()} if native else {'annotation': annotation.pk, 'decision': decision}
        if native:
            payload['status'] = 'accepted' if decision == 'ACCEPT' else 'rejected'
        payload['comment'] = 'Correct the selected class'
        if revision is not None:
            payload['revision'] = revision
        return payload

    assert qa.post(url, body('REJECT'), format='json').status_code == 409
    assert not annotation.reviews.exists()
    assert qa.post(url, body('REJECT', 1), format='json').status_code == (200 if native else 201)
    corrected = client_for(env['b']).patch(
        f'/api/annotations/{annotation.pk}/', {'revision': 1, 'result': result('a')}, format='json'
    )
    assert corrected.status_code == 200
    assert qa.post(url, body('ACCEPT', 1), format='json').status_code == 409
    annotation.refresh_from_db()
    assert annotation.revision == 2 and annotation.review_status == Annotation.REVIEW_PENDING
    assert annotation.reviews.count() == 1
    accepted = qa.post(url, body('ACCEPT'), format='json', HTTP_IF_MATCH='"2"')
    assert accepted.status_code == (200 if native else 201)
    assert accepted.json()['revision'] == 2


@pytest.mark.parametrize('native', [False, True])
def test_legacy_review_without_policy_remains_compatible(env, native):
    annotation = annotate(env, env['a'], 'a')
    url = f'/api/annotations/{annotation.pk}/review' if native else f'{EXT}/reviews'
    payload = {'status': 'accepted'} if native else {'annotation': annotation.pk, 'decision': 'ACCEPT'}
    assert client_for(env['qa']).post(url, payload, format='json').status_code == (200 if native else 201)


@pytest.mark.parametrize('action', ['skip', 'release'])
def test_abandoned_correction_keeps_original_and_frees_a_distribution_slot(env, action):
    _, annotation, _ = three_annotators(env)
    submitted(env)
    replacement = make_user(env['project'].organization, 'replacement@x.io', OrganizationMember.Role.LABELLER)
    set_policy(env['project'], assignment_mode='automatic')
    original_result = annotation.result
    reviews.apply_review(annotation, env['qa'], 'REJECT', 'Correct this class', expected_revision=1)
    assignment = TaskAssignment.objects.get(task=env['task'], user=env['b'])
    response = client_for(env['b']).post(f'{EXT}/assignments/{assignment.pk}/{action}', {}, format='json')
    assert response.status_code == 200
    annotation.refresh_from_db()
    assert annotation.was_cancelled and annotation.result == original_result
    assert annotation.reviews.get().comment == 'Correct this class'
    assert Annotation.objects.filter(pk=annotation.pk).exists()
    env['task'].refresh_from_db()
    assert not env['task'].is_labeled
    assert services.distribute_project(env['project']) == 1
    assert TaskAssignment.objects.filter(task=env['task'], user=replacement, status='ASSIGNED').exists()


@pytest.mark.parametrize('native_delete', [False, True])
def test_deleted_submission_does_not_leave_a_ghost_distribution_slot(env, native_delete):
    _, annotation, _ = three_annotators(env)
    submitted(env)
    replacement = make_user(env['project'].organization, 'replacement@x.io', OrganizationMember.Role.LABELLER)
    set_policy(env['project'], assignment_mode='automatic')
    assignment = TaskAssignment.objects.get(task=env['task'], user=env['b'])
    if native_delete:
        assert client_for(env['admin']).delete(f'/api/annotations/{annotation.pk}/').status_code == 204
        assignment.refresh_from_db()
        assert assignment.status == 'CANCELLED'
    else:
        # Bulk deletes may bypass the native endpoint; finished rows must still not reserve an empty slot.
        annotation.delete()
    assert not reviews.eligible_tasks(env['project']).exists()
    progress = client_for(env['qa']).get(f'{EXT}/projects/{env["project"].pk}/progress').json()
    assert progress['tasks_by_state'] != {'COMPLETE': 1}
    assert services.distribute_project(env['project']) == 1
    assert TaskAssignment.objects.filter(task=env['task'], user=replacement, status='ASSIGNED').exists()


def test_open_submissions_emit_events_and_advance_global_progress(env):
    set_policy(env['project'], assignment_mode='open', required_annotations=2, blind_mode=True)
    WebhookSubscription.objects.create(
        project=env['project'], url='https://example.test/hooks', secret='test-secret',
        events=['annotation.submitted', 'task.ready_for_review'], created_by=env['admin'],
    )
    url = f'/api/tasks/{env["task"].pk}/annotations/'
    assert client_for(env['a']).post(url, {'result': result('a')}, format='json').status_code == 201
    progress_url = f'{EXT}/projects/{env["project"].pk}/progress'
    assert client_for(env['qa']).get(progress_url).json()['tasks_by_state'] == {'IN_PROGRESS': 1}
    assert not WebhookDelivery.objects.filter(event='task.ready_for_review').exists()
    assert client_for(env['b']).post(url, {'result': result('b')}, format='json').status_code == 201
    progress = client_for(env['qa']).get(progress_url).json()
    assert progress['tasks_by_state'] == {'COMPLETE': 1}
    assert progress['review_ready_tasks'] == 1
    assert WebhookDelivery.objects.filter(event='annotation.submitted').count() == 2
    assert WebhookDelivery.objects.filter(event='task.ready_for_review').count() == 1
    assert not TaskAssignment.objects.exists()
    assert client_for(env['a']).post(url, {'result': result('b')}, format='json').status_code == 201
    assert WebhookDelivery.objects.filter(event='annotation.submitted').count() == 3
    assert WebhookDelivery.objects.filter(event='task.ready_for_review').count() == 1


def test_manual_assignment_can_replace_a_submission_removed_by_bulk_delete(env):
    _, annotation, _ = three_annotators(env)
    submitted(env)
    set_policy(env['project'], assignment_mode='manual')
    old = TaskAssignment.objects.get(task=env['task'], user=env['b'])
    annotation.delete()
    replacement, created = services.assign_task(env['task'], env['b'], assigned_by=env['qa'])
    assert created and replacement.pk != old.pk and replacement.status == 'ASSIGNED'
    old.refresh_from_db()
    assert old.status == 'CANCELLED'


def test_late_review_grants_a_fresh_correction_window(env):
    _, annotation, _ = three_annotators(env)
    submitted(env)
    set_policy(env['project'], assignment_mode='manual', assignment_ttl_hours=2)
    assignment = TaskAssignment.objects.get(task=env['task'], user=env['b'])
    TaskAssignment.objects.filter(pk=assignment.pk).update(expires_at=timezone.now() - timedelta(hours=1))
    reviews.apply_review(annotation, env['qa'], 'REJECT', 'Correct this class', expected_revision=1)
    assignment.refresh_from_db()
    assert assignment.expires_at is None
    started = client_for(env['b']).post(f'{EXT}/assignments/{assignment.pk}/start', {}, format='json')
    assert started.status_code == 200
    assignment.refresh_from_db()
    assert assignment.expires_at > timezone.now() + timedelta(hours=1, minutes=59)
    assert services.expire_overdue() == 0
    corrected = client_for(env['b']).patch(
        f'/api/annotations/{annotation.pk}/', {'revision': 1, 'result': result('a')}, format='json'
    )
    assert corrected.status_code == 200
    accepted = client_for(env['qa']).post(
        f'{EXT}/reviews', {'annotation': annotation.pk, 'revision': 2, 'decision': 'ACCEPT'}, format='json'
    )
    assert accepted.status_code == 201, accepted.data
    assert AnnotationReview.objects.filter(annotation=annotation).count() == 2


def test_policy_quorum_changes_recalculate_distinct_author_completion(env):
    annotate(env, env['a'], 'a')
    annotate(env, env['a'], 'b')
    annotate(env, env['b'], 'b')
    url = f'{EXT}/projects/{env["project"].pk}/policy'
    admin = client_for(env['admin'])
    for required, complete in ((3, False), (2, True), (1, True)):
        changed = admin.patch(url, {'required_annotations': required}, format='json')
        assert changed.status_code == 200, changed.data
        env['task'].refresh_from_db()
        assert env['task'].is_labeled is complete
        assert env['task'].overlap == required


def test_rework_cannot_create_another_original_and_must_patch_the_existing_revision(env):
    set_policy(env['project'], required_annotations=1, assignment_mode='manual', review_mode='manual', blind_mode=True)
    services.assign_task(env['task'], env['a'])
    client = client_for(env['a'])
    url = f'/api/tasks/{env["task"].pk}/annotations/'
    submitted_annotation = client.post(url, {'result': result('b')}, format='json')
    assert submitted_annotation.status_code == 201
    annotation = Annotation.objects.get(pk=submitted_annotation.json()['id'])
    reviews.apply_review(annotation, env['qa'], 'REJECT', 'Correct this class', expected_revision=1)
    replacement = client.post(url, {'result': result('a')}, format='json')
    assert replacement.status_code == 400
    assert 'PATCH' in str(replacement.data)
    assert Annotation.objects.filter(task=env['task']).count() == 1
    annotation.refresh_from_db()
    assert annotation.review_status == Annotation.REVIEW_REJECTED and annotation.revision == 1
    assert not annotation.revisions.exists()
    corrected = client.patch(
        f'/api/annotations/{annotation.pk}/', {'revision': 1, 'result': result('a')}, format='json'
    )
    assert corrected.status_code == 200
    assert Annotation.objects.filter(task=env['task']).count() == 1
    assert annotation.revisions.get().result == result('b')
    reviews.apply_review(annotation, env['qa'], 'ACCEPT', expected_revision=2)
    assert TaskAssignment.objects.get(task=env['task'], user=env['a']).status == 'CLOSED'


@pytest.mark.parametrize('status', ['ASSIGNED', 'IN_PROGRESS', 'REWORK_REQUIRED'])
def test_new_result_cannot_replace_a_legacy_original_of_the_same_assignment(env, status):
    set_policy(env['project'], required_annotations=1, assignment_mode='manual', review_mode='manual', blind_mode=True)
    assignment, _ = services.assign_task(env['task'], env['a'])
    annotation = annotate(env, env['a'], 'b')
    TaskAssignment.objects.filter(pk=assignment.pk).update(status=status)
    response = client_for(env['a']).post(
        f'/api/tasks/{env["task"].pk}/annotations/', {'result': result('a')}, format='json'
    )
    assert response.status_code == 400
    assert Annotation.objects.filter(task=env['task']).count() == 1
    annotation.refresh_from_db()
    assert annotation.result == result('b') and annotation.revision == 1


@pytest.mark.parametrize('revocation', ['inactive', 'deleted_membership', 'disabled_project_member', 'qa_role'])
def test_distribution_replaces_unfinished_work_of_ineligible_annotators(env, revocation):
    set_policy(env['project'], required_annotations=1, assignment_mode='automatic')
    for user in (env['a'], env['b'], env['qa']):
        ProjectMember.objects.create(project=env['project'], user=user, enabled=True)
    old, _ = services.assign_task(env['task'], env['a'])
    if revocation == 'inactive':
        env['a'].is_active = False
        env['a'].save(update_fields=['is_active'])
    elif revocation == 'deleted_membership':
        OrganizationMember.objects.filter(user=env['a']).update(deleted_at=timezone.now())
    elif revocation == 'disabled_project_member':
        ProjectMember.objects.filter(project=env['project'], user=env['a']).update(enabled=False)
    else:
        OrganizationMember.objects.filter(user=env['a']).update(role=OrganizationMember.Role.QA)
    assert services.distribute_project(env['project'], assigned_by=env['qa']) == 1
    old.refresh_from_db()
    assert old.status == 'CANCELLED' and old.version == 2
    assert TaskAssignment.objects.filter(task=env['task'], user=env['b'], status='ASSIGNED').exists()
    assert not Annotation.objects.exists()
    assert AuditEvent.objects.filter(action='assignment.reclaimed', resource_id=str(old.pk)).exists()


@pytest.mark.parametrize('accepted', [False, True])
def test_distribution_preserves_submitted_results_after_the_author_becomes_inactive(env, accepted):
    set_policy(env['project'], required_annotations=1, assignment_mode='automatic', review_mode='manual')
    services.assign_task(env['task'], env['a'])
    response = client_for(env['a']).post(
        f'/api/tasks/{env["task"].pk}/annotations/', {'result': result('a')}, format='json'
    )
    annotation = Annotation.objects.get(pk=response.json()['id'])
    if accepted:
        reviews.apply_review(annotation, env['qa'], 'ACCEPT', expected_revision=1)
    env['a'].is_active = False
    env['a'].save(update_fields=['is_active'])
    assert services.distribute_project(env['project']) == 0
    annotation.refresh_from_db()
    assert not annotation.was_cancelled and annotation.result == result('a')
    assert annotation.review_status == ('accepted' if accepted else 'pending')
    assert TaskAssignment.objects.get(task=env['task'], user=env['a']).status == ('CLOSED' if accepted else 'SUBMITTED')


def test_distribution_preserves_rejected_history_when_reclaiming_a_correction(env):
    set_policy(env['project'], required_annotations=1, assignment_mode='automatic', review_mode='manual')
    services.assign_task(env['task'], env['a'])
    response = client_for(env['a']).post(
        f'/api/tasks/{env["task"].pk}/annotations/', {'result': result('b')}, format='json'
    )
    annotation = Annotation.objects.get(pk=response.json()['id'])
    reviews.apply_review(annotation, env['qa'], 'REJECT', 'Keep this feedback', expected_revision=1)
    env['a'].is_active = False
    env['a'].save(update_fields=['is_active'])
    assert services.distribute_project(env['project']) == 1
    annotation.refresh_from_db()
    assert annotation.was_cancelled and annotation.result == result('b')
    assert annotation.reviews.get().comment == 'Keep this feedback'
    assert TaskAssignment.objects.get(task=env['task'], user=env['a']).status == 'CANCELLED'


@pytest.mark.parametrize('status', ['ASSIGNED', 'IN_PROGRESS', 'REWORK_REQUIRED'])
def test_reclaim_does_not_cancel_valid_legacy_results_even_with_a_stale_assignment_status(env, status):
    set_policy(env['project'], required_annotations=1, assignment_mode='automatic')
    assignment, _ = services.assign_task(env['task'], env['a'])
    annotation = annotate(env, env['a'], 'a')
    Annotation.objects.filter(pk=annotation.pk).update(task_assignment=assignment)
    TaskAssignment.objects.filter(pk=assignment.pk).update(status=status)
    env['a'].is_active = False
    env['a'].save(update_fields=['is_active'])
    assert services.distribute_project(env['project']) == 0
    annotation.refresh_from_db()
    assert not annotation.was_cancelled and annotation.result == result('a')
    assignment.refresh_from_db()
    assert assignment.status == status


def test_rotated_rectangle_iou_uses_the_exported_top_left_anchor():
    horizontal = {'x': 20, 'y': 30, 'width': 20, 'height': 10, 'rotation': 0}
    different = {**horizontal, 'rotation': 90}
    equivalent = {'x': 40, 'y': 30, 'width': 10, 'height': 20, 'rotation': 90}
    assert consensus.bbox_iou(horizontal, different) == pytest.approx(0)
    assert consensus.bbox_iou(horizontal, equivalent) == pytest.approx(1)
    assert consensus.bbox_iou(equivalent, horizontal) == pytest.approx(1)
    diamond = {'x': 50, 'y': 50 - 10 * 2**0.5, 'width': 20, 'height': 20, 'rotation': 45}
    square = {'x': 40, 'y': 40, 'width': 20, 'height': 20}
    assert consensus.bbox_iou(square, diamond) == pytest.approx(1 / 2**0.5)


def test_rotated_rectangle_consensus_retains_the_original_image_aspect():
    def region(value):
        return [{
            'from_name': 'box', 'to_name': 'image', 'type': 'rectanglelabels',
            'original_width': 2000, 'original_height': 1000,
            'value': {**value, 'rectanglelabels': ['Stamp']},
        }]

    horizontal = region({'x': 20, 'y': 30, 'width': 20, 'height': 10, 'rotation': 0})
    equivalent = region({'x': 40, 'y': 30, 'width': 5, 'height': 40, 'rotation': 90})
    assert consensus.compute_agreement({'A': horizontal, 'B': equivalent})['score'] == pytest.approx(1)


@pytest.mark.parametrize('invalid', [
    {'width': 0}, {'width': -1}, {'width': None}, {'height': 'invalid'}, {'x': float('nan')}, {'rotation': float('inf')},
])
def test_invalid_or_degenerate_rectangles_do_not_break_consensus(invalid):
    valid = {'x': 20, 'y': 30, 'width': 20, 'height': 10}
    assert consensus.bbox_iou({**valid, **invalid}, valid) == 0
