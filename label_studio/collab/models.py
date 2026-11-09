"""Collaborative annotation: per-project policy and per-user task assignments."""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils import timezone


class ProjectAnnotationPolicy(models.Model):
    class AssignmentMode(models.TextChoices):
        OPEN = 'open', _('Open (native Label Studio distribution)')
        MANUAL = 'manual', _('Manual')
        AUTOMATIC = 'automatic', _('Automatic')
        HYBRID = 'hybrid', _('Hybrid')

    class ReviewMode(models.TextChoices):
        NONE = 'none', _('None')
        MANUAL = 'manual', _('Manual')

    project = models.OneToOneField('projects.Project', on_delete=models.CASCADE, related_name='annotation_policy')
    required_annotations = models.PositiveSmallIntegerField(
        _('required annotations'),
        default=2,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text='Number of independent annotations required per task',
    )
    blind_mode = models.BooleanField(
        _('blind mode'),
        default=False,
        help_text='Annotators only receive their own annotations, drafts and derived task statistics',
    )
    assignment_mode = models.CharField(
        _('assignment mode'), max_length=16, choices=AssignmentMode.choices, default=AssignmentMode.OPEN
    )
    review_mode = models.CharField(_('review mode'), max_length=16, choices=ReviewMode.choices, default=ReviewMode.NONE)
    allow_predictions = models.BooleanField(_('allow predictions'), default=True)
    is_archived = models.BooleanField(_('archived'), default=False)
    max_active_tasks_per_user = models.PositiveIntegerField(
        _('max active tasks per user'),
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text='Upper bound of active assignments (ASSIGNED/IN_PROGRESS/REWORK_REQUIRED) per annotator in this project',
    )
    assignment_ttl_hours = models.PositiveIntegerField(
        _('assignment TTL (hours)'),
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text='Default expiration delay applied to new assignments',
    )
    anonymous_review = models.BooleanField(
        _('anonymous review'),
        default=False,
        help_text='Reviewers see "Annotateur A/B/..." instead of annotator identities',
    )
    agreement_threshold = models.FloatField(
        _('agreement threshold'),
        default=0.8,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text='Tasks whose agreement score is below this value are flagged as disagreements',
    )
    created_at = models.DateTimeField(_('created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('updated at'), auto_now=True)

    def __str__(self):
        return f'Policy(project={self.project_id}, blind={self.blind_mode}, mode={self.assignment_mode})'


class TaskAssignment(models.Model):
    class Status(models.TextChoices):
        ASSIGNED = 'ASSIGNED', _('Assigned')
        IN_PROGRESS = 'IN_PROGRESS', _('In progress')
        SUBMITTED = 'SUBMITTED', _('Submitted')
        REWORK_REQUIRED = 'REWORK_REQUIRED', _('Rework required')
        CLOSED = 'CLOSED', _('Closed')
        SKIPPED = 'SKIPPED', _('Skipped')
        EXPIRED = 'EXPIRED', _('Expired')
        CANCELLED = 'CANCELLED', _('Cancelled')

    # Statuses holding the (task, user) slot: a user can't be assigned twice while one of these is set.
    ACTIVE_STATUSES = (Status.ASSIGNED, Status.IN_PROGRESS, Status.SUBMITTED, Status.REWORK_REQUIRED)
    # Statuses giving the user access to the task.
    VISIBLE_STATUSES = ACTIVE_STATUSES + (Status.CLOSED,)

    ALLOWED_TRANSITIONS = {
        Status.ASSIGNED: {Status.IN_PROGRESS, Status.SUBMITTED, Status.SKIPPED, Status.CANCELLED, Status.EXPIRED},
        Status.IN_PROGRESS: {Status.SUBMITTED, Status.EXPIRED, Status.CANCELLED, Status.SKIPPED},
        Status.SUBMITTED: {Status.REWORK_REQUIRED, Status.CLOSED},
        Status.REWORK_REQUIRED: {Status.IN_PROGRESS, Status.SUBMITTED, Status.CANCELLED, Status.SKIPPED},
        Status.CLOSED: set(),
        Status.SKIPPED: set(),
        Status.EXPIRED: set(),
        Status.CANCELLED: set(),
    }

    task = models.ForeignKey('tasks.Task', on_delete=models.CASCADE, related_name='assignments')
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='task_assignments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='task_assignments')
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ASSIGNED, db_index=True)
    assigned_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1, help_text='Incremented on each update (optimistic locking)')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['task', 'user'],
                condition=Q(status__in=['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REWORK_REQUIRED']),
                name='collab_unique_active_assignment',
            )
        ]
        indexes = [models.Index(fields=['project', 'user', 'status'], name='collab_assign_proj_user_idx')]

    def __str__(self):
        return f'Assignment(task={self.task_id}, user={self.user_id}, {self.status})'


class ProjectInvitation(models.Model):
    """Email-owned reservation; no login or assignment is created before acceptance."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='invitations')
    email = models.EmailField()
    tasks = models.ManyToManyField('tasks.Task', related_name='pending_invitations')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    invited_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    had_org_membership = models.BooleanField(default=False)
    had_project_membership = models.BooleanField(default=False)
    status = models.CharField(max_length=16, default='pending', choices=[
        ('pending', 'Pending'), ('accepted', 'Accepted'), ('cancelled', 'Cancelled'),
    ])
    token_digest = models.CharField(max_length=64, blank=True, default='', db_index=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True)
    delivery_status = models.CharField(max_length=16, default='pending', choices=[
        ('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed'), ('blocked', 'SMTP not configured'),
    ])
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    error_code = models.CharField(max_length=32, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(
            fields=['project', 'email'], condition=Q(status='pending'), name='collab_pending_invite_unique',
        )]


class AnnotationReview(models.Model):
    """Append-only history of review decisions (the native Annotation.review_* fields hold the latest one only)."""

    class Decision(models.TextChoices):
        ACCEPT = 'ACCEPT', _('Accept')
        REJECT = 'REJECT', _('Reject')
        REQUEST_CORRECTION = 'REQUEST_CORRECTION', _('Request correction')
        REFERENCE = 'REFERENCE', _('Selected as reference')

    annotation = models.ForeignKey('tasks.Annotation', on_delete=models.CASCADE, related_name='reviews')
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    decision = models.CharField(max_length=24, choices=Decision.choices)
    comment = models.TextField(blank=True, default='')
    revision = models.PositiveIntegerField(default=1, help_text='Annotation revision the decision applies to')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class AnnotationRevision(models.Model):
    """Previous versions of an annotation result, kept when its author corrects it after a review."""

    annotation = models.ForeignKey('tasks.Annotation', on_delete=models.CASCADE, related_name='revisions')
    revision = models.PositiveIntegerField()
    result = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['revision']
        constraints = [models.UniqueConstraint(fields=['annotation', 'revision'], name='collab_unique_revision')]


class TaskArbitration(models.Model):
    task = models.OneToOneField('tasks.Task', on_delete=models.CASCADE, related_name='arbitration')
    reference_annotation = models.ForeignKey(
        'tasks.Annotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    final_annotation = models.ForeignKey(
        'tasks.Annotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    updated_at = models.DateTimeField(auto_now=True)


class AuditEvent(models.Model):
    class ActorType(models.TextChoices):
        USER = 'user', _('User')
        SERVICE = 'service', _('Service account')

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    organization = models.ForeignKey(
        'organizations.Organization', on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    actor_type = models.CharField(max_length=16, choices=ActorType.choices, default=ActorType.USER)
    project = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    action = models.CharField(max_length=64, db_index=True)
    resource_type = models.CharField(max_length=64, blank=True, default='')
    resource_id = models.CharField(max_length=64, blank=True, default='')
    detail = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['organization', 'timestamp'], name='collab_audit_org_time_idx')]


class ServiceAccount(models.Model):
    """Machine identity (e.g. an AI agent) with explicit scopes, optionally restricted to some projects."""

    SCOPES = (
        'projects:read',
        'projects:write',
        'projects:delete',
        'policy:read',
        'policy:write',
        'imports:write',
        'tasks:read',
        'tasks:write',
        'tasks:delete',
        'annotations:write',
        'annotations:delete',
        'predictions:delete',
        'assignments:read',
        'assignments:write',
        'reviews:read',
        'reviews:write',
        'progress:read',
        'results:read',
        'webhooks:write',
        'native',
    )

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='collab_service_account')
    name = models.CharField(max_length=100)
    scopes = models.JSONField(default=list)
    projects = models.ManyToManyField('projects.Project', blank=True, related_name='+')
    all_projects = models.BooleanField(default=False, help_text='Explicit organization-wide project authorization')
    active = models.BooleanField(default=True)
    token_digest = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)


class WebhookSubscription(models.Model):
    EVENTS = (
        'project.archived',
        'project.restored',
        'annotation.submitted',
        'annotation.resubmitted',
        'task.ready_for_review',
        'review.decided',
        'assignment.expired',
        'project.published',
        'task.arbitrated',
    )

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='collab_webhooks')
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=128)
    events = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)


class IdempotencyRecord(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=128)
    endpoint = models.CharField(max_length=64)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'key', 'endpoint'], name='collab_unique_idempotency')]


class WebhookDelivery(models.Model):
    """Transactional outbox. Claimed rows are retried after their lease expires following a worker crash."""

    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        SENDING = 'sending', _('Sending')
        SENT = 'sent', _('Sent')
        FAILED = 'failed', _('Failed')

    subscription = models.ForeignKey(WebhookSubscription, on_delete=models.CASCADE, related_name='deliveries')
    event = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=160, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'available_at'], name='collab_outbox_ready_idx')]
