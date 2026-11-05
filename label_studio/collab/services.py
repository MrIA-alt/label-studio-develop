"""Assignment operations. All mutations are transactional and safe under concurrent requests."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from collab.models import ProjectAnnotationPolicy, TaskAssignment


def assign_task(task, user, assigned_by=None, expires_at=None):
    """Assign `task` to `user`. Returns (assignment, created); never creates a duplicate active assignment."""
    existing = TaskAssignment.objects.filter(
        task=task, user=user, status__in=TaskAssignment.ACTIVE_STATUSES
    ).first()
    if existing:
        return existing, False
    if expires_at is None:
        policy = ProjectAnnotationPolicy.objects.filter(project_id=task.project_id).first()
        if policy and policy.assignment_ttl_hours:
            expires_at = timezone.now() + timedelta(hours=policy.assignment_ttl_hours)
    try:
        with transaction.atomic():
            assignment = TaskAssignment.objects.create(
                task=task, project_id=task.project_id, user=user, assigned_by=assigned_by, expires_at=expires_at
            )
        return assignment, True
    except IntegrityError:
        # lost a race against a concurrent request: the other row is the one to return
        return TaskAssignment.objects.get(task=task, user=user, status__in=TaskAssignment.ACTIVE_STATUSES), False


def transition(assignment, new_status):
    """Move an assignment to `new_status` with optimistic locking on `version`."""
    new_status = TaskAssignment.Status(new_status)
    if new_status not in TaskAssignment.ALLOWED_TRANSITIONS[TaskAssignment.Status(assignment.status)]:
        raise ValidationError({'detail': f'Transition {assignment.status} -> {new_status} is not allowed.'})
    fields = {'status': new_status, 'version': assignment.version + 1}
    now = timezone.now()
    if new_status == TaskAssignment.Status.IN_PROGRESS and assignment.started_at is None:
        fields['started_at'] = now
    if new_status == TaskAssignment.Status.SUBMITTED:
        fields['submitted_at'] = now
    updated = TaskAssignment.objects.filter(pk=assignment.pk, version=assignment.version).update(**fields)
    if not updated:
        raise ValidationError({'detail': 'The assignment was modified concurrently, please retry.'})
    assignment.refresh_from_db()
    return assignment


def get_active_assignment(task, user):
    return TaskAssignment.objects.filter(task=task, user=user, status__in=TaskAssignment.ACTIVE_STATUSES).first()


def on_annotation_created(annotation, user):
    """Link a new annotation to the author's active assignment and mark it submitted (or skipped)."""
    assignment = get_active_assignment(annotation.task, user)
    if assignment is None:
        return
    type(annotation).objects.filter(pk=annotation.pk).update(task_assignment=assignment)
    annotation.task_assignment = assignment
    target = TaskAssignment.Status.SKIPPED if annotation.was_cancelled else TaskAssignment.Status.SUBMITTED
    try:
        transition(assignment, target)
    except ValidationError:
        return
    if target == TaskAssignment.Status.SUBMITTED:
        from collab import reviews, webhooks

        project = annotation.project
        webhooks.emit(project, 'annotation.submitted', {'task': annotation.task_id, 'annotation': annotation.pk})
        if reviews.eligible_tasks(project).filter(pk=annotation.task_id).exists():
            webhooks.emit(project, 'task.ready_for_review', {'task': annotation.task_id})


LOAD_STATUSES = (
    TaskAssignment.Status.ASSIGNED,
    TaskAssignment.Status.IN_PROGRESS,
    TaskAssignment.Status.REWORK_REQUIRED,
)
# Statuses counting toward the N independent annotations a task needs.
SLOT_STATUSES = TaskAssignment.ACTIVE_STATUSES + (TaskAssignment.Status.CLOSED,)


def eligible_users(project):
    """Active labellers of the project's organization (restricted to enabled project members when any are defined)."""
    from organizations.models import OrganizationMember
    from projects.models import ProjectMember
    from users.models import User

    ids = OrganizationMember.objects.filter(
        organization_id=project.organization_id,
        deleted_at__isnull=True,
        role=OrganizationMember.Role.LABELLER,
    ).values('user_id')
    users = User.objects.filter(pk__in=ids, is_active=True)
    members = ProjectMember.objects.filter(project=project)
    if members.exists():
        users = users.filter(pk__in=members.filter(enabled=True).values('user_id'))
    return users


def distribute_project(project, limit=None, assigned_by=None):
    """Automatically assign tasks to eligible annotators until each has `required_annotations` slots.

    Least-loaded users are picked first, `max_active_tasks_per_user` is honoured, and users who already worked on,
    skipped or lost a task are never given the same task again. Returns the number of assignments created.
    """
    from tasks.models import Annotation, Task

    created = 0
    with transaction.atomic():
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
        if not users:
            return 0
        load = {u.pk: 0 for u in users}
        for row in (
            TaskAssignment.objects.filter(project=project, user__in=users, status__in=LOAD_STATUSES)
            .values('user_id')
            .annotate(n=Count('id'))
        ):
            load[row['user_id']] = row['n']

        slots = dict(
            TaskAssignment.objects.filter(project=project, status__in=SLOT_STATUSES)
            .values_list('task_id')
            .annotate(n=Count('id'))
        )
        tasks = Task.objects.filter(project=project).order_by('id').values_list('id', flat=True)
        for task_id in tasks.iterator():
            missing = required - slots.get(task_id, 0)
            if missing <= 0:
                continue
            excluded = set(TaskAssignment.objects.filter(task_id=task_id).values_list('user_id', flat=True))
            excluded |= set(Annotation.objects.filter(task_id=task_id).values_list('completed_by_id', flat=True))
            candidates = sorted(
                (u for u in users if u.pk not in excluded and (cap is None or load[u.pk] < cap)),
                key=lambda u: (load[u.pk], u.pk),
            )
            for user in candidates[:missing]:
                _, was_created = assign_task(Task(pk=task_id, project=project), user, assigned_by=assigned_by)
                if was_created:
                    created += 1
                    load[user.pk] += 1
            if limit and created >= limit:
                break
    return created


def expire_overdue(now=None):
    """Move overdue ASSIGNED/IN_PROGRESS assignments to EXPIRED. Safe to run repeatedly (e.g. after a restart)."""
    now = now or timezone.now()
    count = 0
    overdue = TaskAssignment.objects.filter(
        status__in=(TaskAssignment.Status.ASSIGNED, TaskAssignment.Status.IN_PROGRESS), expires_at__lt=now
    )
    for assignment in overdue:
        try:
            transition(assignment, TaskAssignment.Status.EXPIRED)
            count += 1
            from collab import webhooks

            webhooks.emit(
                assignment.project, 'assignment.expired', {'task': assignment.task_id, 'user': assignment.user_id}
            )
        except ValidationError:
            continue
    return count


def reassign(assignment, new_user, assigned_by=None):
    """Cancel `assignment` and give the task to `new_user` (manual/hybrid re-assignment)."""
    if assignment.user_id == new_user.pk:
        raise ValidationError({'detail': 'The task is already assigned to this user.'})
    with transaction.atomic():
        transition(assignment, TaskAssignment.Status.CANCELLED)
        return assign_task(assignment.task, new_user, assigned_by=assigned_by)[0]


def task_global_state(task_id, required, counts=None):
    """Task-level state computed from its assignments, independent from any single assignment."""
    if counts is None:
        counts = dict(
            TaskAssignment.objects.filter(task_id=task_id).values_list('status').annotate(n=Count('id'))
        )
    S = TaskAssignment.Status
    done = counts.get(S.SUBMITTED, 0) + counts.get(S.CLOSED, 0)
    if done >= required:
        return 'COMPLETE'
    if counts.get(S.REWORK_REQUIRED, 0):
        return 'REWORK'
    if done or counts.get(S.IN_PROGRESS, 0):
        return 'IN_PROGRESS'
    if counts.get(S.ASSIGNED, 0):
        return 'ASSIGNED'
    return 'UNASSIGNED'
