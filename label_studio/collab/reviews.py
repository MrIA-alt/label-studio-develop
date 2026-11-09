"""Review workflow: decisions, personal corrections with history, arbitration and the supervisor view of a task."""

import string

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from collab import consensus, webhooks
from collab.models import (
    AnnotationReview,
    AnnotationRevision,
    ProjectAnnotationPolicy,
    TaskArbitration,
    TaskAssignment,
)
from collab.privacy import get_policy
from collab.lifecycle import ensure_project_writable, is_archived

D = AnnotationReview.Decision
S = TaskAssignment.Status


class ReviewVersionConflict(APIException):
    status_code = 409
    default_detail = 'The annotation changed. Reload its current revision before reviewing it.'
    default_code = 'version_conflict'


def required_annotations(project):
    policy = get_policy(project)
    return policy.required_annotations if policy else (project.maximum_annotations or 1)


def countable(annotations):
    return annotations.filter(was_cancelled=False, ground_truth=False, completed_by__isnull=False)


def eligible_tasks(project):
    """Tasks that gathered the required number of independent annotations."""
    from tasks.models import Task

    return (
        Task.objects.filter(project=project)
        .annotate(
            n_annotators=Count(
                'annotations__completed_by',
                distinct=True,
                filter=Q(annotations__was_cancelled=False, annotations__ground_truth=False),
            )
        )
        .filter(n_annotators__gte=required_annotations(project))
    )


def current_revision(annotation):
    return annotation.revision


def refresh_task_agreement(task_id):
    """Keep the native scalar cache current; detailed metrics are calculated only on displayed tasks."""
    from tasks.models import Annotation, Task

    task = Task.objects.filter(pk=task_id).only('id', 'project_id').first()
    if task is None or not ProjectAnnotationPolicy.objects.filter(project_id=task.project_id).exists():
        return
    if is_archived(task.project_id):
        return
    results = dict(countable(Annotation.objects.filter(task_id=task_id)).order_by('id').values_list('completed_by_id', 'result'))
    score = consensus.compute_agreement(results)['score']
    Task.objects.filter(pk=task_id).update(precomputed_agreement=score)


def _assignment_of(annotation):
    if annotation.task_assignment_id:
        return annotation.task_assignment
    return (
        TaskAssignment.objects.filter(
            task_id=annotation.task_id, user_id=annotation.completed_by_id, status__in=TaskAssignment.VISIBLE_STATUSES
        )
        .order_by('-id')
        .first()
    )


@transaction.atomic
def apply_review(annotation, reviewer, decision, comment='', expected_revision=None):
    """Record a review decision, update native review fields and drive the author's assignment state."""
    from collab import services

    decision = D(decision)
    comment = (comment or '').strip()
    if decision in (D.REJECT, D.REQUEST_CORRECTION) and not comment:
        raise ValidationError({'comment': 'A comment is required to reject or request a correction.'})
    with transaction.atomic():
        from tasks.models import Annotation, Task

        ensure_project_writable(annotation.project)
        Task.objects.select_for_update().get(pk=annotation.task_id)
        annotation = Annotation.objects.select_for_update().get(pk=annotation.pk)
        if expected_revision is None and get_policy(annotation.project) is not None:
            raise ReviewVersionConflict('A revision or If-Match header is required to review this annotation.')
        if expected_revision is not None:
            try:
                expected_revision = int(str(expected_revision).strip('"'))
            except (TypeError, ValueError):
                raise ValidationError({'revision': 'Use the integer revision displayed in the review.'})
            if expected_revision != annotation.revision:
                raise ReviewVersionConflict()
        if not eligible_tasks(annotation.project).filter(pk=annotation.task_id).exists():
            raise ValidationError({'detail': 'The task has not received the required independent submissions.'})
        if annotation.was_cancelled or annotation.ground_truth:
            raise ValidationError({'annotation': 'Only submitted original annotations can be reviewed.'})
        assignment = _assignment_of(annotation)
        if assignment is not None and assignment.status == S.CLOSED and decision != D.REFERENCE:
            raise ValidationError({'detail': 'This annotation is already closed.'})
        if assignment is not None and assignment.status != S.SUBMITTED and decision != D.REFERENCE:
            raise ValidationError({'detail': 'Only a submitted assignment can receive a review decision.'})
        review = AnnotationReview.objects.create(
            annotation=annotation,
            reviewer=reviewer,
            decision=decision,
            comment=comment,
            revision=current_revision(annotation),
        )
        if decision == D.REFERENCE:
            arbitration, _ = TaskArbitration.objects.get_or_create(task=annotation.task)
            arbitration.reference_annotation = annotation
            arbitration.decided_by = reviewer
            arbitration.save()
        else:
            annotation.review_status = (
                annotation.REVIEW_ACCEPTED if decision == D.ACCEPT else annotation.REVIEW_REJECTED
            )
            annotation.review_comment = comment
            annotation.reviewed_by = reviewer
            annotation.reviewed_at = timezone.now()
            annotation.save(update_fields=['review_status', 'review_comment', 'reviewed_by', 'reviewed_at'])
            if assignment is not None and assignment.status == S.SUBMITTED:
                target = S.CLOSED if decision == D.ACCEPT else S.REWORK_REQUIRED
                services.transition(assignment, target)
    webhooks.emit(
        annotation.project,
        'review.decided',
        {'task': annotation.task_id, 'annotation': annotation.pk, 'decision': decision.value},
    )
    return review


def author_edit_allowed(annotation, user):
    """Under manual review, a submitted/closed annotation is frozen for its author until rework is requested."""
    if is_archived(annotation.project):
        return False
    policy = get_policy(annotation.project)
    if annotation.was_cancelled:
        return True
    if not policy or policy.review_mode != ProjectAnnotationPolicy.ReviewMode.MANUAL:
        return True
    assignment = _assignment_of(annotation) or annotation.task_assignment
    if assignment is None:
        return annotation.review_status == annotation.REVIEW_REJECTED
    return assignment.status not in (S.SUBMITTED, S.CLOSED)


def snapshot_before_update(annotation, user, new_result):
    """Keep the previous version when the author corrects an annotation that went through review."""
    ensure_project_writable(annotation.project)
    if new_result is None or new_result == annotation.result:
        return False
    AnnotationRevision.objects.create(
        annotation=annotation, revision=current_revision(annotation), result=annotation.result, created_by=user
    )
    return True


def after_author_update(annotation, user):
    """A corrected annotation goes back to review."""
    from collab import services

    assignment = annotation.task_assignment or TaskAssignment.objects.filter(
        task_id=annotation.task_id, user=user, status__in=(S.REWORK_REQUIRED, S.IN_PROGRESS)
    ).first()
    if assignment is not None and assignment.status in (S.REWORK_REQUIRED, S.IN_PROGRESS):
        if annotation.task_assignment_id is None:
            type(annotation).objects.filter(pk=annotation.pk).update(task_assignment=assignment)
            annotation.task_assignment = assignment
        services.transition(assignment, S.SUBMITTED)
        webhooks.emit(annotation.project, 'annotation.resubmitted', {'task': annotation.task_id, 'annotation': annotation.pk})


def alias_for(index):
    letters = string.ascii_uppercase
    return f'Annotateur {letters[index]}' if index < len(letters) else f'Annotateur {index + 1}'


def build_review_view(task, anonymous=False):
    """Supervisor view of a task: annotations, history, differences and arbitration state."""
    policy = get_policy(task.project)
    annotations = list(countable(task.annotations.all()).order_by('id'))
    aliases, order = {}, []
    for a in annotations:
        if a.completed_by_id not in aliases:
            aliases[a.completed_by_id] = alias_for(len(aliases))
            order.append(a.completed_by_id)
    results = {aliases[a.completed_by_id]: a.result or [] for a in annotations}
    threshold = policy.agreement_threshold if policy else 0.8
    agreement = consensus.compute_agreement(results)
    agreement['disagreement'] = agreement['score'] is not None and agreement['score'] < threshold
    agreement['threshold'] = threshold

    items = []
    for a in annotations:
        item = {
            'id': a.pk,
            'annotator': aliases[a.completed_by_id] if anonymous else a.completed_by_id,
            'annotator_alias': aliases[a.completed_by_id],
            'revision': current_revision(a),
            'result': a.result,
            'review_status': a.review_status,
            'assignment_status': a.task_assignment.status if a.task_assignment_id else None,
            'reviews': [
                {'decision': r.decision, 'comment': r.comment, 'created_at': r.created_at,
                 **({} if anonymous else {'reviewer': r.reviewer_id})}
                for r in a.reviews.all()
            ],
            'revisions': [
                {'revision': r.revision, 'result': r.result, 'created_at': r.created_at} for r in a.revisions.all()
            ],
        }
        if anonymous:
            item.pop('annotator_alias')
        items.append(item)
    arbitration = TaskArbitration.objects.filter(task=task).first()
    return {
        'task': {'id': task.pk, 'data': task.data},
        'required_annotations': required_annotations(task.project),
        'review_ready': len(aliases) >= required_annotations(task.project),
        'anonymous': anonymous,
        'annotations': items,
        'agreement': agreement,
        'arbitration': {
            'reference_annotation': arbitration.reference_annotation_id if arbitration else None,
            'final_annotation': arbitration.final_annotation_id if arbitration else None,
        },
    }


def create_final_annotation(task, user, result):
    """Create (or replace, keeping the previous version) the arbitrated final annotation, flagged ground truth."""
    from tasks.models import Annotation

    with transaction.atomic():
        from tasks.models import Task

        ensure_project_writable(task.project)
        Task.objects.select_for_update().get(pk=task.pk)
        if not eligible_tasks(task.project).filter(pk=task.pk).exists():
            raise ValidationError({'detail': 'The task has not received the required independent submissions.'})
        arbitration, _ = TaskArbitration.objects.get_or_create(task=task)
        final = arbitration.final_annotation
        if final is None:
            final = Annotation.objects.create(
                task=task, project=task.project, completed_by=user, result=result, ground_truth=True
            )
            task.ensure_unique_groundtruth(annotation_id=final.pk)
        else:
            AnnotationRevision.objects.create(
                annotation=final, revision=current_revision(final), result=final.result, created_by=user
            )
            final.result = result
            final.revision += 1
            final.save(update_fields=['result', 'revision'])
        arbitration.final_annotation = final
        arbitration.decided_by = user
        arbitration.save()
        webhooks.emit(task.project, 'task.arbitrated', {'task': task.pk, 'annotation': final.pk, 'revision': final.revision})
    return final
