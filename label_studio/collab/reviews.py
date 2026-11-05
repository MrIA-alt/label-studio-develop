"""Review workflow: decisions, personal corrections with history, arbitration and the supervisor view of a task."""

import string

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from collab import consensus, webhooks
from collab.models import (
    AnnotationReview,
    AnnotationRevision,
    ProjectAnnotationPolicy,
    TaskArbitration,
    TaskAssignment,
)
from collab.privacy import get_policy

D = AnnotationReview.Decision
S = TaskAssignment.Status


def required_annotations(project):
    policy = get_policy(project)
    return policy.required_annotations if policy else (project.maximum_annotations or 1)


def countable(annotations):
    return annotations.filter(was_cancelled=False, ground_truth=False)


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
    return annotation.revisions.count() + 1


def _assignment_of(annotation):
    if annotation.task_assignment_id:
        return annotation.task_assignment
    return TaskAssignment.objects.filter(
        task_id=annotation.task_id, user_id=annotation.completed_by_id, status__in=TaskAssignment.ACTIVE_STATUSES
    ).first()


def apply_review(annotation, reviewer, decision, comment=''):
    """Record a review decision, update native review fields and drive the author's assignment state."""
    from collab import services

    decision = D(decision)
    comment = (comment or '').strip()
    if decision in (D.REJECT, D.REQUEST_CORRECTION) and not comment:
        raise ValidationError({'comment': 'A comment is required to reject or request a correction.'})
    with transaction.atomic():
        assignment = _assignment_of(annotation)
        if assignment is not None and assignment.status == S.CLOSED and decision != D.REFERENCE:
            raise ValidationError({'detail': 'This annotation is already closed.'})
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
    policy = get_policy(annotation.project)
    if not policy or policy.review_mode != ProjectAnnotationPolicy.ReviewMode.MANUAL:
        return True
    assignment = _assignment_of(annotation) or annotation.task_assignment
    return assignment is None or assignment.status not in (S.SUBMITTED, S.CLOSED)


def snapshot_before_update(annotation, user, new_result):
    """Keep the previous version when the author corrects an annotation that went through review."""
    reviewed = annotation.review_status != annotation.REVIEW_PENDING or annotation.reviews.exists()
    if not reviewed or new_result is None or new_result == annotation.result:
        return False
    AnnotationRevision.objects.create(
        annotation=annotation, revision=current_revision(annotation), result=annotation.result, created_by=user
    )
    return True


def after_author_update(annotation, user):
    """A corrected annotation goes back to review."""
    from collab import services

    assignment = annotation.task_assignment or TaskAssignment.objects.filter(
        task_id=annotation.task_id, user=user, status=S.REWORK_REQUIRED
    ).first()
    if assignment is not None and assignment.status == S.REWORK_REQUIRED:
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
                {'decision': r.decision, 'comment': r.comment, 'reviewer': r.reviewer_id, 'created_at': r.created_at}
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
            final.save(update_fields=['result'])
        arbitration.final_annotation = final
        arbitration.decided_by = user
        arbitration.save()
    return final
