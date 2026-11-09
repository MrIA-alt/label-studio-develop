"""Assignment operations. All mutations are transactional and safe under concurrent requests."""

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from collab.models import ProjectAnnotationPolicy, TaskAssignment
from collab.lifecycle import ProjectArchived, ensure_project_writable


def lock_project(project_id):
    """Serialize project changes while allowing concurrent foreign-key inserts on PostgreSQL."""
    from projects.models import Project

    return Project.objects.select_for_update(
        no_key=connection.features.has_select_for_no_key_update
    ).get(pk=project_id)


def assign_task(task, user, assigned_by=None, expires_at=None):
    """Assign `task` to `user`. Returns (assignment, created); never creates a duplicate active assignment."""
    try:
        with transaction.atomic():
            # All assignment modes share this lock, eligibility and workload validation.
            lock_project(task.project_id)
            ensure_project_writable(task.project)
            existing = TaskAssignment.objects.filter(
                task=task, user=user, status__in=TaskAssignment.VISIBLE_STATUSES
            ).first()
            if existing and existing.status in (TaskAssignment.Status.SUBMITTED, TaskAssignment.Status.CLOSED):
                from tasks.models import Annotation

                if not Annotation.objects.filter(
                    task=task, completed_by=user, was_cancelled=False, ground_truth=False
                ).exists():
                    # A bulk deletion can bypass the annotation endpoint's assignment cleanup.
                    TaskAssignment.objects.filter(pk=existing.pk, version=existing.version).update(
                        status=TaskAssignment.Status.CANCELLED, version=existing.version + 1
                    )
                    existing = None
            if existing:
                return existing, False
            if not eligible_users(task.project).filter(pk=user.pk).exists():
                raise ValidationError({'user': 'This user is not an active annotator of this project.'})
            policy = ProjectAnnotationPolicy.objects.filter(project_id=task.project_id).first()
            if policy and policy.max_active_tasks_per_user:
                load = TaskAssignment.objects.filter(project_id=task.project_id, user=user, status__in=LOAD_STATUSES).count()
                if load >= policy.max_active_tasks_per_user:
                    raise ValidationError({'user': 'The active assignment limit has been reached.'})
            if expires_at is None and policy and policy.assignment_ttl_hours:
                expires_at = timezone.now() + timedelta(hours=policy.assignment_ttl_hours)
            if expires_at is not None and expires_at <= timezone.now():
                raise ValidationError({'expires_at': 'Expiration must be in the future.'})
            assignment = TaskAssignment.objects.create(
                task=task, project_id=task.project_id, user=user, assigned_by=assigned_by, expires_at=expires_at
            )
        return assignment, True
    except IntegrityError:
        # lost a race against a concurrent request: the other row is the one to return
        return TaskAssignment.objects.get(task=task, user=user, status__in=TaskAssignment.ACTIVE_STATUSES), False


class AssignmentVersionConflict(APIException):
    status_code = 409
    default_detail = 'The assignment changed. Reload its current version.'
    default_code = 'version_conflict'


@transaction.atomic
def transition(assignment, new_status, expected_version=None):
    """Move an assignment to `new_status` with optimistic locking on `version`."""
    ensure_project_writable(assignment.project)
    from tasks.models import Annotation, Task

    Task.objects.select_for_update().get(pk=assignment.task_id)
    new_status = TaskAssignment.Status(new_status)
    previous_status = assignment.status
    if expected_version is not None:
        try:
            expected_version = int(expected_version)
        except (ValueError, TypeError):
            raise ValidationError({'version': 'Version must be a positive integer.'})
        if expected_version != assignment.version:
            raise AssignmentVersionConflict()
    if new_status not in TaskAssignment.ALLOWED_TRANSITIONS[TaskAssignment.Status(assignment.status)]:
        raise ValidationError({'detail': f'Transition {assignment.status} -> {new_status} is not allowed.'})
    fields = {'status': new_status, 'version': assignment.version + 1}
    now = timezone.now()
    if new_status == TaskAssignment.Status.REWORK_REQUIRED:
        # Review time does not consume the author's next correction window.
        fields['expires_at'] = None
    if new_status == TaskAssignment.Status.IN_PROGRESS and previous_status == TaskAssignment.Status.REWORK_REQUIRED:
        policy = ProjectAnnotationPolicy.objects.filter(project_id=assignment.project_id).first()
        fields['expires_at'] = (
            now + timedelta(hours=policy.assignment_ttl_hours) if policy and policy.assignment_ttl_hours else None
        )
        fields['started_at'] = now
    elif new_status == TaskAssignment.Status.IN_PROGRESS and assignment.started_at is None:
        fields['started_at'] = now
    if new_status == TaskAssignment.Status.SUBMITTED:
        fields['submitted_at'] = now
    updated = TaskAssignment.objects.filter(pk=assignment.pk, version=assignment.version).update(**fields)
    if not updated:
        raise AssignmentVersionConflict()
    if new_status in (TaskAssignment.Status.SKIPPED, TaskAssignment.Status.CANCELLED, TaskAssignment.Status.EXPIRED):
        # Keep rejected originals and their review history, but stop counting abandoned corrections as submissions.
        from django.db.models import Q

        abandoned = Annotation.objects.filter(
            task_id=assignment.task_id, completed_by_id=assignment.user_id,
            was_cancelled=False, ground_truth=False,
        ).filter(
            Q(task_assignment=assignment)
            | Q(task_assignment__isnull=True, review_status=Annotation.REVIEW_REJECTED)
        )
        for annotation in abandoned:
            annotation.was_cancelled = True
            annotation.save(update_fields=['was_cancelled'])
    assignment.refresh_from_db()
    return assignment


def get_active_assignment(task, user):
    return TaskAssignment.objects.filter(task=task, user=user, status__in=TaskAssignment.ACTIVE_STATUSES).first()


def on_annotation_created(annotation, user):
    """Link a new annotation to the author's active assignment and mark it submitted (or skipped)."""
    ensure_project_writable(annotation.project)
    assignment = get_active_assignment(annotation.task, user)
    policy = ProjectAnnotationPolicy.objects.filter(project_id=annotation.project_id).first()
    if assignment is None:
        if policy and policy.assignment_mode != ProjectAnnotationPolicy.AssignmentMode.OPEN:
            raise ValidationError({'detail': 'No active assignment is available for this submission.'})
        if not annotation.was_cancelled:
            emit_submission(annotation)
        return
    if assignment.status == TaskAssignment.Status.SUBMITTED:
        raise ValidationError({'detail': 'This assignment has already been submitted. Update its annotation instead.'})
    if not annotation.was_cancelled:
        from django.db.models import Q

        original = type(annotation).objects.filter(
            task_id=annotation.task_id, completed_by_id=assignment.user_id,
            was_cancelled=False, ground_truth=False,
        ).exclude(pk=annotation.pk).filter(
            Q(task_assignment=assignment) | Q(task_assignment__isnull=True)
        )
        if original.exists():
            raise ValidationError({
                'detail': 'An original annotation already exists for this assignment. '
                'Update it using PATCH and its current revision.'
            })
    type(annotation).objects.filter(pk=annotation.pk).update(task_assignment=assignment)
    annotation.task_assignment = assignment
    target = TaskAssignment.Status.SKIPPED if annotation.was_cancelled else TaskAssignment.Status.SUBMITTED
    if not annotation.was_cancelled and assignment.status in (
        TaskAssignment.Status.ASSIGNED, TaskAssignment.Status.REWORK_REQUIRED
    ):
        transition(assignment, TaskAssignment.Status.IN_PROGRESS)
    transition(assignment, target)
    if target == TaskAssignment.Status.SUBMITTED:
        emit_submission(annotation)


def emit_submission(annotation):
    from collab import reviews, webhooks
    from tasks.models import Annotation

    project = annotation.project
    webhooks.emit(project, 'annotation.submitted', {'task': annotation.task_id, 'annotation': annotation.pk})
    if not reviews.eligible_tasks(project).filter(pk=annotation.task_id).exists():
        return
    previous_authors = reviews.countable(
        Annotation.objects.filter(task_id=annotation.task_id).exclude(pk=annotation.pk)
    ).values('completed_by_id').distinct().count()
    if previous_authors < reviews.required_annotations(project):
        webhooks.emit(project, 'task.ready_for_review', {'task': annotation.task_id})


@transaction.atomic
def delete_annotation(annotation):
    """Delete an authorized annotation and release its now-empty submitted assignment."""
    from tasks.models import Annotation, Task

    lock_project(annotation.project_id)
    Task.objects.select_for_update().get(pk=annotation.task_id)
    ensure_project_writable(annotation.project)
    annotation = Annotation.objects.select_for_update().get(pk=annotation.pk)
    task_id, user_id = annotation.task_id, annotation.completed_by_id
    annotation.delete()
    if user_id is not None and not Annotation.objects.filter(
        task_id=task_id, completed_by_id=user_id, was_cancelled=False, ground_truth=False
    ).exists():
        from django.db.models import F

        TaskAssignment.objects.filter(
            task_id=task_id, user_id=user_id,
            status__in=(TaskAssignment.Status.SUBMITTED, TaskAssignment.Status.CLOSED),
        ).update(status=TaskAssignment.Status.CANCELLED, version=F('version') + 1)


LOAD_STATUSES = (
    TaskAssignment.Status.ASSIGNED,
    TaskAssignment.Status.IN_PROGRESS,
    TaskAssignment.Status.REWORK_REQUIRED,
)


def eligible_users(project):
    """Active labellers of the project's organization (restricted to enabled project members when any are defined)."""
    from organizations.models import OrganizationMember
    from projects.models import ProjectMember
    from users.models import User
    from collab.models import ServiceAccount
    from collab.permissions import service_project_ids

    memberships = OrganizationMember.objects.filter(
        organization_id=project.organization_id,
        deleted_at__isnull=True,
    )
    ids = set(memberships.filter(role=OrganizationMember.Role.LABELLER).values_list('user_id', flat=True))
    for account in ServiceAccount.objects.filter(user_id__in=memberships.values('user_id'), active=True).select_related('user'):
        allowed = service_project_ids(account.user)
        if 'annotations:write' in account.scopes and (allowed is None or project.pk in allowed):
            ids.add(account.user_id)
    users = User.objects.filter(pk__in=ids, is_active=True)
    members = ProjectMember.objects.filter(project=project)
    if members.exists():
        users = users.filter(pk__in=members.filter(enabled=True).values('user_id'))
    return users


def _reclaim_ineligible_assignments(project, eligible_ids, assigned_by=None):
    """Release unfinished work whose owner can no longer annotate this project.

    Submitted/closed work and valid existing originals remain intact. Task locking
    also lets a submission that finished while we waited retain its occupied slot.
    """
    from collab import audit
    from tasks.models import Annotation, Task

    assignments = TaskAssignment.objects.filter(project=project, status__in=LOAD_STATUSES).exclude(
        user_id__in=eligible_ids
    )
    for assignment in assignments:
        Task.objects.select_for_update().get(pk=assignment.task_id)
        assignment.refresh_from_db()
        if assignment.status not in LOAD_STATUSES:
            continue
        original = Annotation.objects.filter(
            task_id=assignment.task_id, completed_by_id=assignment.user_id,
            was_cancelled=False, ground_truth=False,
        ).exclude(review_status=Annotation.REVIEW_REJECTED)
        if original.exists():
            continue
        transition(assignment, TaskAssignment.Status.CANCELLED, expected_version=assignment.version)
        audit.record(
            assigned_by, 'assignment.reclaimed', project, 'assignment', assignment.pk, reason='ineligible'
        )


def distribute_project(project, limit=None, assigned_by=None):
    """Automatically assign tasks to eligible annotators until each has `required_annotations` slots.

    Least-loaded users are picked first, `max_active_tasks_per_user` is honoured, and users who already worked on,
    skipped or lost a task are never given the same task again. Returns the number of assignments created.
    """
    from tasks.models import Annotation, Task

    created = 0
    with transaction.atomic():
        lock_project(project.pk)
        ensure_project_writable(project)
        # row lock serialises concurrent distributions of the same project (no-op on SQLite)
        policy = ProjectAnnotationPolicy.objects.select_for_update().filter(project=project).first()
        if policy is None or policy.assignment_mode not in (
            ProjectAnnotationPolicy.AssignmentMode.AUTOMATIC,
            ProjectAnnotationPolicy.AssignmentMode.HYBRID,
        ):
            return 0
        required = policy.required_annotations
        cap = policy.max_active_tasks_per_user
        users = list(eligible_users(project))
        _reclaim_ineligible_assignments(project, {user.pk for user in users}, assigned_by=assigned_by)
        if not users:
            return 0
        load = {u.pk: 0 for u in users}
        for row in (
            TaskAssignment.objects.filter(project=project, user__in=users, status__in=LOAD_STATUSES)
            .values('user_id')
            .annotate(n=Count('id'))
        ):
            load[row['user_id']] = row['n']

        # Fetch only identifiers once. Distribution must not issue several queries per source task.
        occupied, excluded_by_task = {}, {}
        for task_id, user_id, status in TaskAssignment.objects.filter(project=project).values_list(
            'task_id', 'user_id', 'status'
        ).iterator():
            excluded_by_task.setdefault(task_id, set()).add(user_id)
            # Finished assignments occupy a slot only while their original annotation exists.
            if status in LOAD_STATUSES:
                occupied.setdefault(task_id, set()).add(user_id)
        for task_id, user_id, cancelled, ground_truth in Annotation.objects.filter(project=project).values_list(
            'task_id', 'completed_by_id', 'was_cancelled', 'ground_truth'
        ).iterator():
            excluded_by_task.setdefault(task_id, set()).add(user_id)
            if user_id is not None and not cancelled and not ground_truth:
                occupied.setdefault(task_id, set()).add(user_id)
        expires_at = timezone.now() + timedelta(hours=policy.assignment_ttl_hours) if policy.assignment_ttl_hours else None
        pending = []

        def flush():
            if pending:
                TaskAssignment.objects.bulk_create(pending, batch_size=1000)
                pending.clear()

        tasks = Task.objects.filter(project=project).order_by('id').values_list('id', flat=True)
        for task_id in tasks.iterator():
            missing = required - len(occupied.get(task_id, ()))
            if missing <= 0:
                continue
            excluded = excluded_by_task.get(task_id, ())
            candidates = sorted(
                (u for u in users if u.pk not in excluded and (cap is None or load[u.pk] < cap)),
                key=lambda u: (load[u.pk], u.pk),
            )
            for user in candidates[:missing]:
                if limit is not None and created >= limit:
                    flush()
                    return created
                pending.append(TaskAssignment(
                    task_id=task_id, project=project, user=user, assigned_by=assigned_by, expires_at=expires_at
                ))
                created += 1
                load[user.pk] += 1
                if len(pending) >= 1000:
                    flush()
            if limit and created >= limit:
                break
            if cap is not None and all(n >= cap for n in load.values()):
                break
        flush()
    return created


def expire_overdue(now=None, project=None):
    """Move overdue ASSIGNED/IN_PROGRESS assignments to EXPIRED. Safe to run repeatedly (e.g. after a restart)."""
    now = now or timezone.now()
    count = 0
    overdue = TaskAssignment.objects.filter(
        status__in=(TaskAssignment.Status.ASSIGNED, TaskAssignment.Status.IN_PROGRESS), expires_at__lt=now
    ).exclude(project__annotation_policy__is_archived=True)
    if project is not None:
        overdue = overdue.filter(project=project)
    for assignment in overdue:
        try:
            with transaction.atomic():
                transition(assignment, TaskAssignment.Status.EXPIRED)
                from collab import webhooks

                webhooks.emit(
                    assignment.project, 'assignment.expired', {'task': assignment.task_id, 'user': assignment.user_id}
                )
                count += 1
        except (ValidationError, AssignmentVersionConflict, ProjectArchived):
            continue
    return count


def reassign(assignment, new_user, assigned_by=None, expected_version=None):
    """Cancel `assignment` and give the task to `new_user` (manual/hybrid re-assignment)."""
    if assignment.user_id == new_user.pk:
        raise ValidationError({'detail': 'The task is already assigned to this user.'})
    with transaction.atomic():
        lock_project(assignment.project_id)
        transition(assignment, TaskAssignment.Status.CANCELLED, expected_version=expected_version)
        return assign_task(assignment.task, new_user, assigned_by=assigned_by)[0]


def task_global_state(task_id, required, counts=None):
    """Task-level state computed from its assignments, independent from any single assignment."""
    if counts is None:
        from collab import reviews
        from tasks.models import Annotation

        counts = dict(
            TaskAssignment.objects.filter(task_id=task_id).values_list('status').annotate(n=Count('user_id', distinct=True))
        )
        counts['_done_count'] = reviews.countable(Annotation.objects.filter(task_id=task_id)).values(
            'completed_by_id'
        ).distinct().count()
    S = TaskAssignment.Status
    done = counts.get('_done_count', counts.get(S.SUBMITTED, 0) + counts.get(S.CLOSED, 0))
    if counts.get(S.REWORK_REQUIRED, 0):
        return 'REWORK'
    if done >= required:
        return 'COMPLETE'
    if done or counts.get(S.IN_PROGRESS, 0) or counts.get(S.SUBMITTED, 0) or counts.get(S.CLOSED, 0):
        return 'IN_PROGRESS'
    if counts.get(S.ASSIGNED, 0):
        return 'ASSIGNED'
    return 'UNASSIGNED'
