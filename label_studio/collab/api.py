import math
from collections.abc import Mapping

from core.permissions import ViewClassPermission, all_permissions
from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema_serializer
from projects.models import Project, ProjectMember
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from tasks.models import Annotation, Task
from users.models import User

from collab import audit, privacy, reviews, services, webhooks
from collab.authentication import issue_service_token
from collab.base import ExtensionView
from collab.idempotency import idempotent_mutation, run_idempotent
from collab.models import (
    AnnotationReview,
    AuditEvent,
    ProjectAnnotationPolicy,
    ServiceAccount,
    TaskAssignment,
    WebhookDelivery,
    WebhookSubscription,
)
from collab.permissions import ensure_project_allowed, get_service_account, has_service_scope
from collab.lifecycle import ensure_project_writable, set_archived


class ExtensionPagination(LimitOffsetPagination):
    default_limit = 100
    max_limit = 500

    def get_limit(self, request):
        if 'limit' in request.query_params:
            serializers.IntegerField(min_value=1, max_value=self.max_limit).run_validation(request.query_params['limit'])
        return super().get_limit(request)

    def get_offset(self, request):
        if 'offset' in request.query_params:
            serializers.IntegerField(min_value=0).run_validation(request.query_params['offset'])
        return super().get_offset(request)


def paginate(request, items, serialize):
    paginator = ExtensionPagination()
    page = paginator.paginate_queryset(items, request)
    return paginator.get_paginated_response(serialize(page))


def _query_id(request, name):
    value = request.query_params.get(name)
    if value is None:
        return None
    try:
        return serializers.IntegerField(min_value=1).run_validation(value)
    except serializers.ValidationError as exc:
        raise serializers.ValidationError({name: exc.detail})


class StrictInput:
    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({name: ['Unknown field.'] for name in sorted(unknown)})
        return super().to_internal_value(data)


class PolicySerializer(StrictInput, serializers.ModelSerializer):
    def validate_agreement_threshold(self, value):
        if not math.isfinite(value):
            raise serializers.ValidationError('Use a finite agreement threshold.')
        return value

    class Meta:
        model = ProjectAnnotationPolicy
        fields = (
            'required_annotations',
            'blind_mode',
            'assignment_mode',
            'review_mode',
            'allow_predictions',
            'max_active_tasks_per_user',
            'assignment_ttl_hours',
            'anonymous_review',
            'agreement_threshold',
            'is_archived',
        )
        read_only_fields = ('is_archived',)


class AssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskAssignment
        fields = ('id', 'task', 'project', 'user', 'status', 'assigned_at', 'started_at', 'submitted_at', 'expires_at', 'version')


class DistributionInputSerializer(StrictInput, serializers.Serializer):
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100000)


class AssignmentActionInputSerializer(StrictInput, serializers.Serializer):
    version = serializers.IntegerField(required=False, min_value=1)


class AssignmentStaffInputSerializer(AssignmentActionInputSerializer):
    user_id = serializers.IntegerField(required=False, min_value=1)


class BulkAssignSerializer(StrictInput, serializers.Serializer):
    task_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=5000)
    user_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=500)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_expires_at(self, value):
        if value and value <= timezone.now():
            raise serializers.ValidationError('Expiration must be in the future.')
        return value


class ReviewInputSerializer(StrictInput, serializers.Serializer):
    annotation = serializers.IntegerField()
    revision = serializers.IntegerField(
        required=False, min_value=1,
        help_text='Displayed annotation revision. Required for collaborative projects unless If-Match is supplied.',
    )
    decision = serializers.ChoiceField(choices=AnnotationReview.Decision.choices)
    comment = serializers.CharField(required=False, allow_blank=True, default='', max_length=5000)


class FinalAnnotationSerializer(StrictInput, serializers.Serializer):
    result = serializers.ListField(child=serializers.DictField())


def _project_for(request, pk, writable=True, with_policy=False):
    projects = Project.objects.for_user(request.user)
    if with_policy:
        projects = projects.select_related('annotation_policy')
    project = get_object_or_404(projects, pk=pk)
    ensure_project_allowed(request.user, project)
    if writable and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        ensure_project_writable(project)
    return project


def _is_policy_admin(user):
    return get_service_account(user) is not None or (
        not privacy.is_restricted_user(user) and user.get_organization_role() == 'admin'
    )


@transaction.atomic
def apply_policy(project, data, partial=False):
    # Use the same project-first lock order as archival and distribution.
    current = services.lock_project(project.pk)
    ensure_project_writable(current)
    project.maximum_annotations = current.maximum_annotations
    project.overlap_cohort_percentage = current.overlap_cohort_percentage
    policy = ProjectAnnotationPolicy.objects.filter(project=project).first() or ProjectAnnotationPolicy(project=project)
    ser = PolicySerializer(policy, data=data, partial=partial)
    ser.is_valid(raise_exception=True)
    policy = ser.save(project=project)
    project._collab_policy = policy
    if project.maximum_annotations != policy.required_annotations or project.overlap_cohort_percentage != 100:
        project.maximum_annotations = policy.required_annotations
        project.overlap_cohort_percentage = 100
        project.save(update_fields=['maximum_annotations', 'overlap_cohort_percentage'])
    project.tasks.update(overlap=policy.required_annotations)
    from tasks.models import bulk_update_stats_project_tasks
    bulk_update_stats_project_tasks(project.tasks.all(), project)
    from core.current_request import CurrentContext
    from fsm.functions import sync_collaborative_task_states

    sync_collaborative_task_states(project, user=CurrentContext.get_user())
    return policy


class ProjectArchiveAPI(ExtensionView):
    """Archive a campaign, preserving authorized read access and existing results."""
    required_scopes = {'POST': 'projects:write'}
    permission_required = all_permissions.projects_create
    archived = True

    def post(self, request, pk):
        project = _project_for(request, pk, writable=False)
        if not _is_policy_admin(request.user):
            raise PermissionDenied('Only administrators can archive or restore a project.')
        if request.data:
            raise serializers.ValidationError({'detail': 'This operation accepts an empty body.'})
        return Response(PolicySerializer(set_archived(project, request, self.archived)).data)


class ProjectRestoreAPI(ProjectArchiveAPI):
    """Restore a campaign and allow its annotation workflow to continue."""
    archived = False


class ProjectPolicyAPI(ExtensionView):
    """GET: any project member. PUT/PATCH: organization admins and authorised service accounts."""

    required_scopes = {'GET': 'policy:read', 'PUT': 'policy:write', 'PATCH': 'policy:write'}
    permission_required = ViewClassPermission(
        GET=all_permissions.projects_view,
        PUT=all_permissions.projects_change,
        PATCH=all_permissions.projects_change,
    )

    def get(self, request, pk):
        project = _project_for(request, pk, with_policy=True)
        try:
            policy = project.annotation_policy
        except ProjectAnnotationPolicy.DoesNotExist:
            policy = None
        if policy is None:
            policy = ProjectAnnotationPolicy(project=project, required_annotations=project.maximum_annotations or 1)
        return Response(PolicySerializer(policy).data)

    @transaction.atomic
    def _update(self, request, pk, partial):
        if not _is_policy_admin(request.user):
            raise PermissionDenied('Only administrators can change the annotation policy.')
        project = _project_for(request, pk)
        policy = apply_policy(project, request.data, partial=partial)
        audit.record(request, 'policy.update', project, 'project', project.pk, **PolicySerializer(policy).data)
        return Response(PolicySerializer(policy).data)

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)


class BulkAssignmentAPI(ExtensionView):
    """Assign tasks to annotators. Admin and QA only (projects_change)."""

    required_scopes = {'GET': 'assignments:read', 'POST': 'assignments:write'}
    permission_required = ViewClassPermission(
        GET=all_permissions.projects_change,
        POST=all_permissions.projects_change,
    )

    def get(self, request, pk):
        project = _project_for(request, pk)
        qs = TaskAssignment.objects.filter(project=project).order_by('id')
        return paginate(request, qs, lambda page: AssignmentSerializer(page, many=True).data)

    @transaction.atomic
    def post(self, request, pk):
        project = _project_for(request, pk)
        ser = BulkAssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        tasks = list(Task.objects.filter(project=project, pk__in=data['task_ids']))
        users = list(User.objects.filter(pk__in=data['user_ids'], organizations=project.organization).distinct())
        if len(tasks) != len(set(data['task_ids'])) or len(users) != len(set(data['user_ids'])):
            raise serializers.ValidationError({'detail': 'Unknown task or user for this project.'})
        created = existing = 0
        for task in tasks:
            for user in users:
                _, was_created = services.assign_task(
                    task, user, assigned_by=request.user, expires_at=data.get('expires_at')
                )
                created += was_created
                existing += not was_created
        audit.record(request, 'assignments.bulk', project, 'project', project.pk, created=created, existing=existing)
        return Response({'created': created, 'already_assigned': existing}, status=201 if created else 200)


class MyAssignmentsAPI(ExtensionView):
    """The caller's own assignments only."""

    permission_required = all_permissions.tasks_view
    required_scopes = {'GET': 'assignments:read'}

    def get(self, request):
        qs = TaskAssignment.objects.filter(
            user=request.user,
            project__in=Project.objects.for_user(request.user),
        ).order_by('id')
        project_id = _query_id(request, 'project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        if request.query_params.get('status'):
            value = request.query_params['status']
            if value not in TaskAssignment.Status.values:
                raise serializers.ValidationError({'status': 'Unknown assignment status.'})
            qs = qs.filter(status=value)
        return paginate(request, qs, lambda page: AssignmentSerializer(page, many=True).data)


def _assignment_for_owner(request, pk):
    return get_object_or_404(
        TaskAssignment, pk=pk, user=request.user, project__in=Project.objects.for_user(request.user)
    )


def _assignment_for_staff(request, pk):
    assignment = get_object_or_404(
        TaskAssignment.objects.filter(project__in=Project.objects.for_user(request.user)), pk=pk
    )
    ensure_project_allowed(request.user, assignment.project)
    return assignment


class AssignmentActionAPI(ExtensionView):
    """Annotator actions on their own assignment: start, skip, release."""

    permission_required = all_permissions.tasks_view
    required_scopes = {'POST': 'assignments:write'}

    ACTIONS = {
        'start': TaskAssignment.Status.IN_PROGRESS,
        'skip': TaskAssignment.Status.SKIPPED,
        'release': TaskAssignment.Status.CANCELLED,
    }

    def post(self, request, pk, action):
        target = self.ACTIONS.get(action)
        if target is None:
            raise Http404
        ser = AssignmentActionInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        assignment = _assignment_for_owner(request, pk)
        return Response(AssignmentSerializer(services.transition(assignment, target, expected_version=ser.validated_data.get('version'))).data)


class AssignmentStaffActionAPI(ExtensionView):
    """Supervisor actions: cancel or reassign an assignment (admin and QA)."""

    required_scopes = {'POST': 'assignments:write'}
    permission_required = all_permissions.projects_change

    def post(self, request, pk, action):
        assignment = _assignment_for_staff(request, pk)
        ser = AssignmentStaffInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        version = ser.validated_data.get('version')
        if action == 'cancel':
            result = services.transition(assignment, TaskAssignment.Status.CANCELLED, expected_version=version)
        elif action == 'reassign':
            if 'user_id' not in ser.validated_data:
                raise serializers.ValidationError({'user_id': 'This field is required for reassign.'})
            user = get_object_or_404(
                User.objects.filter(organizations=assignment.project.organization).distinct(),
                pk=ser.validated_data['user_id'],
            )
            result = services.reassign(assignment, user, assigned_by=request.user, expected_version=version)
        else:
            raise Http404
        audit.record(request, f'assignment.{action}', assignment.project, 'assignment', assignment.pk)
        return Response(AssignmentSerializer(result).data)


class DistributeAPI(ExtensionView):
    """Run the automatic distribution of a project (automatic/hybrid modes)."""

    required_scopes = {'POST': 'assignments:write'}
    permission_required = all_permissions.projects_change

    def post(self, request, pk):
        project = _project_for(request, pk)
        policy = privacy.get_policy(project)
        if policy is None or policy.assignment_mode not in ('automatic', 'hybrid'):
            raise serializers.ValidationError({'detail': 'Project is not in automatic or hybrid assignment mode.'})
        services.expire_overdue(project=project)
        ser = DistributionInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        created = services.distribute_project(project, limit=ser.validated_data.get('limit'), assigned_by=request.user)
        audit.record(request, 'assignments.distribute', project, 'project', project.pk, created=created)
        return Response({'created': created})


def _assignment_stats(project):
    required = reviews.required_annotations(project)
    rows = TaskAssignment.objects.filter(project=project).values('task_id', 'user_id', 'status').annotate(n=Count('id'))
    per_task, workload = {}, {}
    for row in rows:
        task_counts = per_task.setdefault(row['task_id'], {})
        task_counts[row['status']] = task_counts.get(row['status'], 0) + row['n']
        user_counts = workload.setdefault(row['user_id'], {})
        user_counts[row['status']] = user_counts.get(row['status'], 0) + row['n']
    completed = reviews.countable(Annotation.objects.filter(project=project)).values('task_id').annotate(
        n=Count('completed_by_id', distinct=True)
    )
    actual_done = {row['task_id']: row['n'] for row in completed}
    states = {}
    for task_id in Task.objects.filter(project=project).values_list('id', flat=True):
        counts = per_task.get(task_id, {})
        counts['_done_count'] = actual_done.get(task_id, 0)
        state = services.task_global_state(task_id, required, counts)
        states[state] = states.get(state, 0) + 1
    return {
        'required_annotations': required,
        'tasks_by_state': states,
        'workload': [{'user': uid, 'by_status': counts} for uid, counts in sorted(workload.items())],
    }


class AssignmentSummaryAPI(ExtensionView):
    """Supervisor dashboard data: progress per task state and workload per annotator."""

    required_scopes = {'GET': 'progress:read'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        return Response(_assignment_stats(_project_for(request, pk)))


class ProgressAPI(ExtensionView):
    """Project progress: assignment states, review status counts and disagreements."""

    required_scopes = {'GET': 'progress:read'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        project = _project_for(request, pk)
        data = _assignment_stats(project)
        annotations = reviews.countable(Annotation.objects.filter(project=project))
        data['annotations'] = {
            'total': annotations.count(),
            **{
                s: annotations.filter(review_status=s).count()
                for s in (Annotation.REVIEW_PENDING, Annotation.REVIEW_ACCEPTED, Annotation.REVIEW_REJECTED)
            },
        }
        eligible = reviews.eligible_tasks(project)
        data['review_ready_tasks'] = eligible.count()
        data['tasks_with_final_annotation'] = Task.objects.filter(
            project=project, arbitration__final_annotation__isnull=False
        ).count()
        policy = privacy.get_policy(project)
        flagged = eligible.filter(precomputed_agreement__lt=policy.agreement_threshold if policy else 0.8)
        data['disagreement_tasks'] = flagged.count()
        data['agreement_pending_tasks'] = eligible.filter(precomputed_agreement__isnull=True).count()
        data['disagreement_task_ids'] = list(flagged.order_by('id').values_list('pk', flat=True)[:200])
        return Response(data)


class ReviewQueueAPI(ExtensionView):
    """Tasks that reached the required number of annotations. `?status=pending|disagreement|all`."""

    required_scopes = {'GET': 'progress:read'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        project = _project_for(request, pk)
        flt = request.query_params.get('status', 'pending')
        if flt not in ('pending', 'disagreement', 'accepted', 'rejected', 'all'):
            raise serializers.ValidationError({'status': 'Use pending, disagreement, accepted, rejected or all.'})
        anonymous = privacy_anonymous(project, request)
        tasks = reviews.eligible_tasks(project).order_by('id')
        if flt == 'pending':
            tasks = tasks.filter(
                annotations__review_status=Annotation.REVIEW_PENDING,
                annotations__was_cancelled=False,
                annotations__ground_truth=False,
            ).distinct()
        elif flt in ('accepted', 'rejected'):
            tasks = Task.objects.filter(project=project, annotations__review_status=flt).distinct().order_by('id')

        def serialize(page):
            return [_queue_item(t, anonymous) for t in page]

        if flt == 'disagreement':
            policy = privacy.get_policy(project)
            tasks = tasks.filter(precomputed_agreement__lt=policy.agreement_threshold if policy else 0.8)
        return paginate(request, tasks, serialize)


def privacy_anonymous(project, request):
    policy = privacy.get_policy(project)
    return bool(request.query_params.get('anonymous') in ('1', 'true') or (policy and policy.anonymous_review))


def _queue_item(task, anonymous):
    view = reviews.build_review_view(task, anonymous=anonymous)
    return {
        'task': task.pk,
        'annotations': len(view['annotations']),
        'pending': sum(1 for a in view['annotations'] if a['review_status'] == Annotation.REVIEW_PENDING),
        'agreement': view['agreement']['score'],
        'disagreement': view['agreement']['disagreement'],
        'final_annotation': view['arbitration']['final_annotation'],
    }


class TaskReviewAPI(ExtensionView):
    """Supervisor view of one task: annotations, differences, history. `?anonymous=1` hides annotator identities."""

    required_scopes = {'GET': 'reviews:read'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        task = get_object_or_404(Task.objects.filter(project__in=Project.objects.for_user(request.user)), pk=pk)
        ensure_project_allowed(request.user, task.project)
        return Response(reviews.build_review_view(task, anonymous=privacy_anonymous(task.project, request)))


class ReviewAPI(ExtensionView):
    """Record a review decision: ACCEPT, REJECT / REQUEST_CORRECTION (comment required) or REFERENCE."""

    required_scopes = {'POST': 'reviews:write'}
    permission_required = all_permissions.annotations_change

    @idempotent_mutation('review-create')
    def post(self, request):
        if privacy.is_restricted_user(request.user):
            raise PermissionDenied('Only QA and admin users can review annotations.')
        ser = ReviewInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        annotation = get_object_or_404(
            Annotation.objects.filter(project__in=Project.objects.for_user(request.user)),
            pk=ser.validated_data['annotation'],
        )
        ensure_project_allowed(request.user, annotation.project)
        review = reviews.apply_review(
            annotation, request.user, ser.validated_data['decision'], ser.validated_data['comment'],
            expected_revision=request.headers.get('If-Match', ser.validated_data.get('revision')),
        )
        audit.record(
            request, 'review.decide', annotation.project, 'annotation', annotation.pk, decision=review.decision
        )
        return Response(
            {'id': review.pk, 'annotation': annotation.pk, 'decision': review.decision, 'revision': review.revision},
            status=201,
        )


class FinalAnnotationAPI(ExtensionView):
    """Create or replace the arbitrated final annotation of a task (stored as ground truth)."""

    required_scopes = {'POST': 'reviews:write'}
    permission_required = all_permissions.annotations_change

    @idempotent_mutation('final-annotation')
    def post(self, request, pk):
        if privacy.is_restricted_user(request.user):
            raise PermissionDenied('Only QA and admin users can arbitrate.')
        task = get_object_or_404(Task.objects.filter(project__in=Project.objects.for_user(request.user)), pk=pk)
        ensure_project_allowed(request.user, task.project)
        ser = FinalAnnotationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        final = reviews.create_final_annotation(task, request.user, ser.validated_data['result'])
        audit.record(request, 'review.final_annotation', task.project, 'task', task.pk, annotation=final.pk)
        return Response({'task': task.pk, 'final_annotation': final.pk}, status=201)


class MyReviewsAPI(ExtensionView):
    """Review feedback (decision + comment) on the caller's own annotations. Never exposes anybody else's."""

    permission_required = all_permissions.tasks_view
    required_scopes = {'GET': 'reviews:read'}

    def get(self, request):
        qs = AnnotationReview.objects.filter(annotation__completed_by=request.user).exclude(
            decision=AnnotationReview.Decision.REFERENCE
        )
        qs = qs.filter(annotation__project__in=Project.objects.for_user(request.user))
        project_id, task_id = _query_id(request, 'project'), _query_id(request, 'task')
        if project_id:
            qs = qs.filter(annotation__project_id=project_id)
        if task_id:
            qs = qs.filter(annotation__task_id=task_id)

        def serialize(page):
            return [
                {
                    'annotation': r.annotation_id,
                    'task': r.annotation.task_id,
                    'decision': r.decision,
                    'comment': r.comment,
                    'revision': r.revision,
                    'created_at': r.created_at,
                }
                for r in page
            ]

        return paginate(request, qs.select_related('annotation').order_by('id'), serialize)


class ResultsAPI(ExtensionView):
    """Annotation results of a project (staff / authorised service accounts). Honors anonymous review."""

    required_scopes = {'GET': 'results:read'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        project = _project_for(request, pk)
        only = request.query_params.get('only', 'all')
        if only not in ('all', 'accepted', 'final'):
            raise serializers.ValidationError({'only': 'Use all, accepted or final.'})
        anonymous = privacy_anonymous(project, request)
        tasks = Task.objects.filter(project=project).order_by('id')
        if only == 'accepted':
            tasks = tasks.filter(
                annotations__review_status=Annotation.REVIEW_ACCEPTED,
                annotations__ground_truth=False, annotations__was_cancelled=False,
            ).distinct()
        elif only == 'final':
            tasks = tasks.filter(arbitration__final_annotation__isnull=False)

        def serialize(page):
            out = []
            for task in page:
                view = reviews.build_review_view(task, anonymous=anonymous)
                annotations = view['annotations']
                if only == 'accepted':
                    annotations = [a for a in annotations if a['review_status'] == Annotation.REVIEW_ACCEPTED]
                final = None
                if view['arbitration']['final_annotation']:
                    final = Annotation.objects.filter(pk=view['arbitration']['final_annotation']).values_list(
                        'result', flat=True
                    ).first()
                if only == 'final' and final is None:
                    continue
                out.append(
                    {
                        'task': task.pk,
                        'data': task.data,
                        'annotations': [] if only == 'final' else [
                            {k: a[k] for k in ('id', 'annotator', 'result', 'review_status', 'revision', 'reviews', 'revisions')}
                            for a in annotations
                        ],
                        'final_result': final,
                        'agreement': view['agreement']['score'],
                        'agreement_details': view['agreement'],
                        'arbitration': view['arbitration'],
                    }
                )
            return out

        return paginate(request, tasks, serialize)


class ProjectCreateSerializer(StrictInput, serializers.Serializer):
    title = serializers.CharField(min_length=settings.PROJECT_TITLE_MIN_LEN, max_length=settings.PROJECT_TITLE_MAX_LEN)
    description = serializers.CharField(required=False, allow_blank=True, default='', max_length=10000)
    expert_instruction = serializers.CharField(required=False, allow_blank=True, default='', max_length=50000)
    label_config = serializers.CharField(max_length=1000000)
    policy = PolicySerializer(
        required=False,
        help_text='Explicit policy configuration requires policy:write for service accounts; omit it to use the confidential defaults.',
    )
    member_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list, max_length=500)
    publish = serializers.BooleanField(required=False, default=False)

    def validate_label_config(self, value):
        try:
            Project.validate_label_config(value)
        except Exception as exc:
            raise serializers.ValidationError(str(getattr(exc, 'detail', exc)))
        return value


class ProjectCreateAPI(ExtensionView):
    """Create a fully configured project in one idempotent call (header `Idempotency-Key` is required)."""

    required_scopes = {'POST': 'projects:write'}
    permission_required = all_permissions.projects_create

    def post(self, request):
        if (
            get_service_account(request.user) is not None
            and isinstance(request.data, Mapping) and 'policy' in request.data
            and not has_service_scope(request.user, 'policy:write')
        ):
            raise PermissionDenied('Explicit policy configuration requires policy:write.')

        def create():
            ser = ProjectCreateSerializer(data=request.data)
            ser.is_valid(raise_exception=True)
            data = ser.validated_data
            org = request.user.active_organization
            members = list(User.objects.filter(pk__in=data['member_ids'], organizations=org).distinct())
            if len(members) != len(set(data['member_ids'])):
                raise serializers.ValidationError({'member_ids': 'Unknown user for this organization.'})
            with transaction.atomic():
                project = Project.objects.create(
                    title=data['title'],
                    description=data['description'],
                    expert_instruction=data['expert_instruction'],
                    label_config=data['label_config'],
                    organization=org,
                    created_by=request.user,
                )
                policy_data = {
                    'required_annotations': 2, 'blind_mode': True,
                    'assignment_mode': 'manual', 'review_mode': 'manual',
                    **data.get('policy', {}),
                }
                apply_policy(project, policy_data)
                ProjectMember.objects.get_or_create(project=project, user=request.user)
                for user in members:
                    ProjectMember.objects.get_or_create(project=project, user=user)
                if data['publish']:
                    project.is_published = True
                    project.save(update_fields=['is_published', 'is_draft'])
                account = get_service_account(request.user)
                if account and not account.all_projects:
                    account.projects.add(project)
                audit.record(request, 'project.create', project, 'project', project.pk)
            body = {'id': project.pk, 'title': project.title, 'is_published': project.is_published}
            return Response(body, status=201)
        return run_idempotent(request, 'project-create', create)


class ProjectPublishAPI(ExtensionView):
    required_scopes = {'POST': 'projects:write'}
    permission_required = all_permissions.projects_change

    @transaction.atomic
    def post(self, request, pk):
        project = _project_for(request, pk)
        project = services.lock_project(project.pk)
        ensure_project_writable(project)
        if not project.is_published:
            project.is_published = True
            project.save(update_fields=['is_published', 'is_draft'])
            audit.record(request, 'project.publish', project, 'project', project.pk)
            webhooks.emit(project, 'project.published', {})
        return Response({'id': project.pk, 'is_published': True})


@extend_schema_serializer(component_name='CollaborationWebhook')
class WebhookSerializer(StrictInput, serializers.Serializer):
    url = serializers.URLField(max_length=500)
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=WebhookSubscription.EVENTS), allow_empty=False
    )


class ProjectWebhooksAPI(ExtensionView):
    """Signed webhooks. The signing secret is returned once, at creation."""

    required_scopes = {'GET': 'webhooks:write', 'POST': 'webhooks:write'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        project = _project_for(request, pk)
        qs = WebhookSubscription.objects.filter(project=project).order_by('id')
        return paginate(
            request,
            qs,
            lambda page: [{'id': w.pk, 'url': w.url, 'events': w.events, 'active': w.active} for w in page],
        )

    @transaction.atomic
    @idempotent_mutation('webhook-create', redact_fields=('secret',))
    def post(self, request, pk):
        project = _project_for(request, pk)
        ser = WebhookSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        webhooks.validate_target_url(ser.validated_data['url'])
        secret = webhooks.new_secret()
        sub = WebhookSubscription.objects.create(
            project=project,
            url=ser.validated_data['url'],
            events=sorted(set(ser.validated_data['events'])),
            secret=secret,
            created_by=request.user,
        )
        audit.record(request, 'webhook.create', project, 'webhook', sub.pk)
        return Response({'id': sub.pk, 'url': sub.url, 'events': sub.events, 'secret': secret}, status=201)


class ProjectWebhookDetailAPI(ExtensionView):
    required_scopes = {'DELETE': 'webhooks:write'}
    permission_required = all_permissions.projects_change

    def delete(self, request, pk, webhook_id):
        project = _project_for(request, pk)
        get_object_or_404(WebhookSubscription, pk=webhook_id, project=project).delete()
        audit.record(request, 'webhook.delete', project, 'webhook', webhook_id)
        return Response(status=204)


class WebhookDeliveriesAPI(ExtensionView):
    required_scopes = {'GET': 'webhooks:write'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        project = _project_for(request, pk)
        jobs = WebhookDelivery.objects.filter(subscription__project=project).order_by('-pk')
        return paginate(request, jobs, lambda page: [{
            'id': job.pk, 'subscription': job.subscription_id, 'event': job.event, 'status': job.status,
            'attempts': job.attempts, 'available_at': job.available_at, 'delivered_at': job.delivered_at,
            'last_error': job.last_error,
        } for job in page])


class WebhookDeliveryRetryAPI(ExtensionView):
    required_scopes = {'POST': 'webhooks:write'}
    permission_required = all_permissions.projects_change

    @transaction.atomic
    def post(self, request, pk, delivery_id):
        project = _project_for(request, pk)
        job = get_object_or_404(WebhookDelivery.objects.select_for_update(), pk=delivery_id, subscription__project=project)
        if not job.subscription.active or job.status == WebhookDelivery.Status.SENT:
            raise serializers.ValidationError({'detail': 'Only undelivered active webhooks can be retried.'})
        if job.status == WebhookDelivery.Status.SENDING and job.lease_until and job.lease_until > timezone.now():
            return Response({'code': 'delivery_in_progress', 'detail': 'Delivery is currently in progress.', 'errors': {}}, status=409)
        job.status, job.attempts, job.available_at, job.lease_until = WebhookDelivery.Status.PENDING, 0, timezone.now(), None
        job.save()
        audit.record(request, 'webhook.retry', project, 'webhook_delivery', job.pk)
        return Response({'id': job.pk, 'status': job.status})


class ServiceAccountSerializer(StrictInput, serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(child=serializers.ChoiceField(choices=ServiceAccount.SCOPES), allow_empty=False)
    project_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    all_projects = serializers.BooleanField(required=False, default=False)
    active = serializers.BooleanField(required=False, default=True)
    rotate_token = serializers.BooleanField(required=False, default=False)
    role = serializers.ChoiceField(choices=['qa'], default='qa')

    def validate(self, data):
        if data.get('all_projects') and data.get('project_ids'):
            raise serializers.ValidationError({'project_ids': 'Choose a project list or all_projects, not both.'})
        return data


def _service_data(account):
    return {
        'id': account.pk, 'user': account.user_id, 'name': account.name, 'scopes': account.scopes,
        'project_ids': list(account.projects.order_by('pk').values_list('pk', flat=True)),
        'all_projects': account.all_projects, 'active': account.active,
    }


class ServiceAccountsAPI(ExtensionView):
    """Manage service accounts. Organization admins only; service accounts cannot create service accounts."""

    permission_required = all_permissions.projects_change

    def _check(self, request):
        if get_service_account(request.user) is not None or privacy.is_restricted_user(request.user):
            raise PermissionDenied('Only organization administrators manage service accounts.')
        if request.user.get_organization_role() != 'admin':
            raise PermissionDenied('Only organization administrators manage service accounts.')

    def get(self, request):
        self._check(request)
        qs = ServiceAccount.objects.filter(user__active_organization=request.user.active_organization).order_by('id')
        return paginate(
            request,
            qs,
            lambda page: [
                _service_data(a) for a in page
            ],
        )

    @idempotent_mutation('service-account-create', hide_token=True)
    def post(self, request):
        self._check(request)
        ser = ServiceAccountSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        org = request.user.active_organization
        projects = list(Project.objects.filter(pk__in=data['project_ids'], organization=org))
        if len(projects) != len(set(data['project_ids'])):
            raise serializers.ValidationError({'project_ids': 'Unknown project.'})
        import uuid

        with transaction.atomic():
            username = f'svc-{uuid.uuid4().hex[:12]}'
            user = User.objects.create(email=f'{username}@service.invalid', username=username)
            user.set_unusable_password()
            user.active_organization = org
            user.save()
            org.add_user(user, role=data['role'])
            account = ServiceAccount.objects.create(
                user=user, name=data['name'], scopes=sorted(set(data['scopes'])), created_by=request.user,
                all_projects=data['all_projects'], active=data['active'],
            )
            user.is_active = account.active
            user.save(update_fields=['is_active'])
            account.projects.set(projects)
            token = issue_service_token(account)
            audit.record(request, 'service_account.create', None, 'service_account', account.pk, scopes=account.scopes)
        return Response(
            {**_service_data(account), 'token': token},
            status=201,
        )


class ServiceAccountDetailAPI(ExtensionView):
    permission_required = all_permissions.projects_change
    _check = ServiceAccountsAPI._check

    def _account(self, request, pk):
        self._check(request)
        return get_object_or_404(
            ServiceAccount.objects.filter(user__active_organization=request.user.active_organization), pk=pk
        )

    def get(self, request, pk):
        return Response(_service_data(self._account(request, pk)))

    @transaction.atomic
    @idempotent_mutation('service-account-update', hide_token=True)
    def patch(self, request, pk):
        account = self._account(request, pk)
        ser = ServiceAccountSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if 'project_ids' in data:
            projects = Project.objects.filter(pk__in=data['project_ids'], organization=request.user.active_organization)
            if projects.count() != len(set(data['project_ids'])):
                raise serializers.ValidationError({'project_ids': 'Unknown project.'})
            account.projects.set(projects)
        for field in ('name', 'scopes', 'all_projects', 'active'):
            if field in data:
                setattr(account, field, data[field])
        if account.all_projects:
            account.projects.clear()
        if not account.active:
            account.token_digest = None
        account.save()
        account.user.is_active = account.active
        account.user.save(update_fields=['is_active'])
        token = None
        if account.active and (data.get('rotate_token') or account.token_digest is None):
            token = issue_service_token(account)
        audit.record(request, 'service_account.update', None, 'service_account', account.pk, fields=sorted(data))
        return Response({**_service_data(account), **({'token': token} if token else {})})

    @transaction.atomic
    def delete(self, request, pk):
        self._check(request)
        account = get_object_or_404(
            ServiceAccount.objects.filter(user__active_organization=request.user.active_organization), pk=pk
        )
        account.active = False
        account.token_digest = None
        account.save(update_fields=['active', 'token_digest'])
        account.user.is_active = False
        account.user.save(update_fields=['is_active'])
        from rest_framework.authtoken.models import Token

        Token.objects.filter(user=account.user).delete()
        audit.record(request, 'service_account.revoke', None, 'service_account', account.pk)
        return Response(status=204)


class AuditAPI(ExtensionView):
    """Audit trail. Organization admins only."""

    permission_required = all_permissions.projects_change

    def get(self, request):
        if get_service_account(request.user) is not None or request.user.get_organization_role() != 'admin':
            raise PermissionDenied('Only organization administrators can read the audit trail.')
        org = request.user.active_organization
        qs = AuditEvent.objects.filter(organization=org)
        project_id = _query_id(request, 'project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        if request.query_params.get('action'):
            qs = qs.filter(action=request.query_params['action'])
        return paginate(
            request,
            qs.order_by('-id'),
            lambda page: [
                {
                    'id': e.pk,
                    'actor': e.actor_id,
                    'actor_type': e.actor_type,
                    'project': e.project_id,
                    'action': e.action,
                    'resource_type': e.resource_type,
                    'resource_id': e.resource_id,
                    'detail': e.detail,
                    'timestamp': e.timestamp,
                }
                for e in page
            ],
        )


from collab.schema import install_schema

install_schema(globals())
