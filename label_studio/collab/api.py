import hashlib
import json

from core.permissions import ViewClassPermission, all_permissions
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from projects.models import Project, ProjectMember
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from tasks.models import Annotation, Task
from users.models import User

from collab import audit, privacy, reviews, services, webhooks
from collab.models import (
    AnnotationReview,
    AuditEvent,
    IdempotencyRecord,
    ProjectAnnotationPolicy,
    ServiceAccount,
    TaskAssignment,
    WebhookSubscription,
)
from collab.base import ExtensionView
from collab.permissions import ensure_project_allowed, get_service_account


class ExtensionPagination(LimitOffsetPagination):
    default_limit = 100
    max_limit = 500


def paginate(request, items, serialize):
    paginator = ExtensionPagination()
    page = paginator.paginate_queryset(items, request)
    return paginator.get_paginated_response(serialize(page))


class PolicySerializer(serializers.ModelSerializer):
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
        )


class AssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskAssignment
        fields = ('id', 'task', 'project', 'user', 'status', 'assigned_at', 'started_at', 'submitted_at', 'expires_at')


class BulkAssignSerializer(serializers.Serializer):
    task_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=5000)
    user_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=500)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)


class ReviewInputSerializer(serializers.Serializer):
    annotation = serializers.IntegerField()
    decision = serializers.ChoiceField(choices=AnnotationReview.Decision.choices)
    comment = serializers.CharField(required=False, allow_blank=True, default='', max_length=5000)


class FinalAnnotationSerializer(serializers.Serializer):
    result = serializers.ListField(child=serializers.DictField())


def _project_for(request, pk):
    project = get_object_or_404(Project.objects.for_user(request.user), pk=pk)
    ensure_project_allowed(request.user, project)
    return project


def _is_policy_admin(user):
    return get_service_account(user) is not None or (
        not privacy.is_restricted_user(user) and user.get_organization_role() == 'admin'
    )


def apply_policy(project, data, partial=False):
    policy = privacy.get_policy(project) or ProjectAnnotationPolicy(project=project)
    ser = PolicySerializer(policy, data=data, partial=partial)
    ser.is_valid(raise_exception=True)
    policy = ser.save(project=project)
    if project.maximum_annotations != policy.required_annotations:
        project.maximum_annotations = policy.required_annotations
        project.save(update_fields=['maximum_annotations'])
    return policy


class ProjectPolicyAPI(ExtensionView):
    """GET: any project member. PUT/PATCH: organization admins and authorised service accounts."""

    required_scopes = {'PUT': 'policy:write', 'PATCH': 'policy:write'}
    permission_required = ViewClassPermission(
        GET=all_permissions.projects_view,
        PUT=all_permissions.projects_change,
        PATCH=all_permissions.projects_change,
    )

    def get(self, request, pk):
        project = _project_for(request, pk)
        policy = privacy.get_policy(project)
        if policy is None:
            policy = ProjectAnnotationPolicy(project=project, required_annotations=project.maximum_annotations or 1)
        return Response(PolicySerializer(policy).data)

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

    def get(self, request):
        qs = TaskAssignment.objects.filter(
            user=request.user,
            status__in=TaskAssignment.VISIBLE_STATUSES,
            project__organization=request.user.active_organization,
        ).order_by('id')
        project_id = request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        return paginate(request, qs, lambda page: AssignmentSerializer(page, many=True).data)


def _assignment_for_owner(request, pk):
    return get_object_or_404(
        TaskAssignment, pk=pk, user=request.user, project__organization=request.user.active_organization
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

    ACTIONS = {
        'start': TaskAssignment.Status.IN_PROGRESS,
        'skip': TaskAssignment.Status.SKIPPED,
        'release': TaskAssignment.Status.CANCELLED,
    }

    def post(self, request, pk, action):
        target = self.ACTIONS.get(action)
        if target is None:
            raise Http404
        assignment = _assignment_for_owner(request, pk)
        return Response(AssignmentSerializer(services.transition(assignment, target)).data)


class AssignmentStaffActionAPI(ExtensionView):
    """Supervisor actions: cancel or reassign an assignment (admin and QA)."""

    required_scopes = {'POST': 'assignments:write'}
    permission_required = all_permissions.projects_change

    def post(self, request, pk, action):
        assignment = _assignment_for_staff(request, pk)
        if action == 'cancel':
            result = services.transition(assignment, TaskAssignment.Status.CANCELLED)
        elif action == 'reassign':
            user = get_object_or_404(
                User.objects.filter(organizations=assignment.project.organization).distinct(),
                pk=request.data.get('user_id') or 0,
            )
            result = services.reassign(assignment, user, assigned_by=request.user)
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
        services.expire_overdue()
        limit = request.data.get('limit')
        created = services.distribute_project(project, limit=int(limit) if limit else None, assigned_by=request.user)
        audit.record(request, 'assignments.distribute', project, 'project', project.pk, created=created)
        return Response({'created': created})


def _assignment_stats(project):
    required = reviews.required_annotations(project)
    rows = TaskAssignment.objects.filter(project=project).values('task_id', 'user_id', 'status').annotate(n=Count('id'))
    per_task, workload = {}, {}
    for row in rows:
        per_task.setdefault(row['task_id'], {})[row['status']] = row['n']
        user_counts = workload.setdefault(row['user_id'], {})
        user_counts[row['status']] = user_counts.get(row['status'], 0) + row['n']
    states = {}
    for task_id in Task.objects.filter(project=project).values_list('id', flat=True):
        state = services.task_global_state(task_id, required, per_task.get(task_id, {}))
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
        flagged = [
            t.pk
            for t in eligible.order_by('id')[:2000]
            if reviews.build_review_view(t)['agreement']['disagreement']
        ]
        data['disagreement_tasks'] = len(flagged)
        data['disagreement_task_ids'] = flagged[:200]
        return Response(data)


class ReviewQueueAPI(ExtensionView):
    """Tasks that reached the required number of annotations. `?status=pending|disagreement|all`."""

    required_scopes = {'GET': 'progress:read'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        project = _project_for(request, pk)
        flt = request.query_params.get('status', 'pending')
        if flt not in ('pending', 'disagreement', 'all'):
            raise serializers.ValidationError({'status': 'Use pending, disagreement or all.'})
        anonymous = privacy_anonymous(project, request)
        tasks = reviews.eligible_tasks(project).order_by('id')
        if flt == 'pending':
            tasks = tasks.filter(
                annotations__review_status=Annotation.REVIEW_PENDING,
                annotations__was_cancelled=False,
                annotations__ground_truth=False,
            ).distinct()

        def serialize(page):
            return [_queue_item(t, anonymous) for t in page]

        if flt == 'disagreement':
            flagged = [t for t in tasks[:2000] if reviews.build_review_view(t)['agreement']['disagreement']]
            return paginate(request, flagged, serialize)
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

    required_scopes = {'GET': 'progress:read'}
    permission_required = all_permissions.projects_change

    def get(self, request, pk):
        task = get_object_or_404(Task.objects.filter(project__in=Project.objects.for_user(request.user)), pk=pk)
        ensure_project_allowed(request.user, task.project)
        return Response(reviews.build_review_view(task, anonymous=privacy_anonymous(task.project, request)))


class ReviewAPI(ExtensionView):
    """Record a review decision: ACCEPT, REJECT / REQUEST_CORRECTION (comment required) or REFERENCE."""

    required_scopes = {'POST': 'reviews:write'}
    permission_required = all_permissions.annotations_change

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
            annotation, request.user, ser.validated_data['decision'], ser.validated_data['comment']
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

    def get(self, request):
        qs = AnnotationReview.objects.filter(annotation__completed_by=request.user).exclude(
            decision=AnnotationReview.Decision.REFERENCE
        )
        if request.query_params.get('task'):
            qs = qs.filter(annotation__task_id=request.query_params['task'])

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
                            {k: a[k] for k in ('id', 'annotator', 'result', 'review_status', 'revision')}
                            for a in annotations
                        ],
                        'final_result': final,
                        'agreement': view['agreement']['score'],
                    }
                )
            return out

        return paginate(request, tasks, serialize)


class ProjectCreateSerializer(serializers.Serializer):
    title = serializers.CharField(min_length=settings.PROJECT_TITLE_MIN_LEN, max_length=settings.PROJECT_TITLE_MAX_LEN)
    description = serializers.CharField(required=False, allow_blank=True, default='', max_length=10000)
    expert_instruction = serializers.CharField(required=False, allow_blank=True, default='', max_length=50000)
    label_config = serializers.CharField(max_length=1000000)
    policy = PolicySerializer(required=False)
    member_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list, max_length=500)
    publish = serializers.BooleanField(required=False, default=False)

    def validate_label_config(self, value):
        try:
            Project.validate_label_config(value)
        except Exception as exc:
            raise serializers.ValidationError(str(getattr(exc, 'detail', exc)))
        return value


def _request_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


class ProjectCreateAPI(ExtensionView):
    """Create a fully configured project in one idempotent call (header `Idempotency-Key` is required)."""

    required_scopes = {'POST': 'projects:write'}
    permission_required = all_permissions.projects_create

    def post(self, request):
        key = request.headers.get('Idempotency-Key', '').strip()
        if not key or len(key) > 128:
            raise serializers.ValidationError({'detail': 'A valid Idempotency-Key header (max 128 chars) is required.'})
        body_hash = _request_hash(request.data)
        record, created = IdempotencyRecord.objects.get_or_create(
            user=request.user, key=key, endpoint='project-create', defaults={'request_hash': body_hash}
        )
        if not created:
            if record.request_hash != body_hash:
                return Response({'detail': 'Idempotency-Key reused with a different payload.'}, status=409)
            if record.response_body is None:
                return Response({'detail': 'A request with this key is still in progress.'}, status=409)
            return Response(record.response_body, status=record.response_status, headers={'Idempotent-Replay': 'true'})

        try:
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
                if data.get('policy'):
                    apply_policy(project, data['policy'])
                for user in members:
                    ProjectMember.objects.get_or_create(project=project, user=user)
                if data['publish']:
                    project.is_published = True
                    project.save(update_fields=['is_published', 'is_draft'])
                audit.record(request, 'project.create', project, 'project', project.pk, idempotency_key=key)
            body = {'id': project.pk, 'title': project.title, 'is_published': project.is_published}
            record.response_status, record.response_body = 201, body
            record.save(update_fields=['response_status', 'response_body'])
            return Response(body, status=201)
        except Exception:
            # a failed attempt must not block a corrected retry with the same key
            record.delete()
            raise


class ProjectPublishAPI(ExtensionView):
    required_scopes = {'POST': 'projects:write'}
    permission_required = all_permissions.projects_change

    def post(self, request, pk):
        project = _project_for(request, pk)
        if not project.is_published:
            project.is_published = True
            project.save(update_fields=['is_published', 'is_draft'])
            audit.record(request, 'project.publish', project, 'project', project.pk)
            webhooks.emit(project, 'project.published', {})
        return Response({'id': project.pk, 'is_published': True})


class WebhookSerializer(serializers.Serializer):
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


class ServiceAccountSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(child=serializers.ChoiceField(choices=ServiceAccount.SCOPES), allow_empty=False)
    project_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    role = serializers.ChoiceField(choices=['admin', 'qa'], default='admin')


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
                {'id': a.pk, 'user': a.user_id, 'name': a.name, 'scopes': a.scopes, 'active': a.active} for a in page
            ],
        )

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
                user=user, name=data['name'], scopes=sorted(set(data['scopes'])), created_by=request.user
            )
            account.projects.set(projects)
            token = user.reset_token()
            audit.record(request, 'service_account.create', None, 'service_account', account.pk, scopes=account.scopes)
        return Response(
            {'id': account.pk, 'user': user.pk, 'name': account.name, 'scopes': account.scopes, 'token': token.key},
            status=201,
        )


class ServiceAccountDetailAPI(ServiceAccountsAPI):
    def delete(self, request, pk):
        self._check(request)
        account = get_object_or_404(
            ServiceAccount.objects.filter(user__active_organization=request.user.active_organization), pk=pk
        )
        account.active = False
        account.save(update_fields=['active'])
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
        qs = AuditEvent.objects.filter(
            Q(project__organization=org) | Q(project__isnull=True, actor__active_organization=org)
        )
        if request.query_params.get('project'):
            qs = qs.filter(project_id=request.query_params['project'])
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
