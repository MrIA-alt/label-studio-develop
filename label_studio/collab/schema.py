"""Explicit OpenAPI contracts for the versioned collaboration API."""

from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import serializers


class ErrorResponse(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()
    errors = serializers.JSONField()


class NativeErrorResponse(serializers.Serializer):
    id = serializers.UUIDField()
    status_code = serializers.IntegerField()
    version = serializers.CharField()
    detail = serializers.CharField()
    exc_info = serializers.CharField(allow_null=True)
    validation_errors = serializers.JSONField(required=False)
    display_context = serializers.JSONField(required=False)


class NativeReviewResponse(serializers.Serializer):
    id = serializers.IntegerField()
    revision = serializers.IntegerField(min_value=1)
    review_status = serializers.CharField()
    review_comment = serializers.CharField(allow_blank=True, allow_null=True)
    reviewed_by = serializers.IntegerField(allow_null=True)
    reviewed_at = serializers.DateTimeField()


class ProjectResponse(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField(required=False)
    is_published = serializers.BooleanField()


class CountResponse(serializers.Serializer):
    created = serializers.IntegerField()
    already_assigned = serializers.IntegerField(required=False)


class Workload(serializers.Serializer):
    user = serializers.IntegerField()
    by_status = serializers.DictField(child=serializers.IntegerField())


class AssignmentSummary(serializers.Serializer):
    required_annotations = serializers.IntegerField()
    tasks_by_state = serializers.DictField(child=serializers.IntegerField())
    workload = Workload(many=True)


class ProgressResponse(AssignmentSummary):
    annotations = serializers.DictField(child=serializers.IntegerField())
    review_ready_tasks = serializers.IntegerField()
    tasks_with_final_annotation = serializers.IntegerField()
    disagreement_tasks = serializers.IntegerField()
    agreement_pending_tasks = serializers.IntegerField()
    disagreement_task_ids = serializers.ListField(child=serializers.IntegerField())


class QueueItem(serializers.Serializer):
    task = serializers.IntegerField()
    annotations = serializers.IntegerField()
    pending = serializers.IntegerField()
    agreement = serializers.FloatField(allow_null=True)
    disagreement = serializers.BooleanField()
    final_annotation = serializers.IntegerField(allow_null=True)


class ReviewDecision(serializers.Serializer):
    decision = serializers.CharField()
    comment = serializers.CharField()
    reviewer = serializers.IntegerField(allow_null=True)
    created_at = serializers.DateTimeField()


class Revision(serializers.Serializer):
    revision = serializers.IntegerField()
    result = serializers.ListField(child=serializers.JSONField(), allow_null=True)
    created_at = serializers.DateTimeField()


class AnnotationResult(serializers.Serializer):
    id = serializers.IntegerField()
    annotator = serializers.JSONField(help_text='Numeric author ID, or anonymous alias when enabled.')
    revision = serializers.IntegerField()
    result = serializers.ListField(child=serializers.JSONField(), allow_null=True)
    review_status = serializers.CharField()
    assignment_status = serializers.CharField(allow_null=True, required=False)
    annotator_alias = serializers.CharField(required=False)
    reviews = ReviewDecision(many=True)
    revisions = Revision(many=True)


class Agreement(serializers.Serializer):
    score = serializers.FloatField(allow_null=True)
    disagreement = serializers.BooleanField()
    threshold = serializers.FloatField()
    by_control = serializers.JSONField(required=False)
    per_label = serializers.JSONField(required=False)


class Arbitration(serializers.Serializer):
    reference_annotation = serializers.IntegerField(allow_null=True)
    final_annotation = serializers.IntegerField(allow_null=True)


class TaskSource(serializers.Serializer):
    id = serializers.IntegerField()
    data = serializers.DictField(child=serializers.JSONField())


class TaskReviewResponse(serializers.Serializer):
    task = TaskSource()
    required_annotations = serializers.IntegerField()
    review_ready = serializers.BooleanField()
    anonymous = serializers.BooleanField()
    annotations = AnnotationResult(many=True)
    agreement = Agreement()
    arbitration = Arbitration()


class ResultItem(serializers.Serializer):
    task = serializers.IntegerField()
    data = serializers.DictField(child=serializers.JSONField())
    annotations = AnnotationResult(many=True)
    final_result = serializers.ListField(child=serializers.JSONField(), allow_null=True)
    agreement = serializers.FloatField(allow_null=True)
    agreement_details = Agreement()
    arbitration = Arbitration()


class OwnReview(serializers.Serializer):
    annotation = serializers.IntegerField()
    task = serializers.IntegerField()
    decision = serializers.CharField()
    comment = serializers.CharField()
    revision = serializers.IntegerField()
    created_at = serializers.DateTimeField()


class ReviewResponse(serializers.Serializer):
    id = serializers.IntegerField()
    annotation = serializers.IntegerField()
    decision = serializers.CharField()
    revision = serializers.IntegerField()


class FinalResponse(serializers.Serializer):
    task = serializers.IntegerField()
    final_annotation = serializers.IntegerField()


class AssignmentAction(serializers.Serializer):
    version = serializers.IntegerField(min_value=1, required=False)


class StaffAssignmentAction(AssignmentAction):
    user_id = serializers.IntegerField(required=False, min_value=1, help_text='Required for reassign.')


class WebhookResponse(serializers.Serializer):
    id = serializers.IntegerField()
    url = serializers.URLField()
    events = serializers.ListField(child=serializers.CharField())
    active = serializers.BooleanField(required=False)
    secret = serializers.CharField(required=False, help_text='Returned once, on creation only.')
    secret_shown_once = serializers.BooleanField(required=False, help_text='An idempotent replay does not return the signing secret again.')


class ServiceAccountResponse(serializers.Serializer):
    id = serializers.IntegerField()
    user = serializers.IntegerField(required=False)
    name = serializers.CharField()
    scopes = serializers.ListField(child=serializers.CharField())
    active = serializers.BooleanField(required=False)
    all_projects = serializers.BooleanField()
    project_ids = serializers.ListField(child=serializers.IntegerField())
    token = serializers.CharField(required=False, help_text='Returned once. Authorization: Service <token>')
    token_shown_once = serializers.BooleanField(required=False, help_text='An idempotent replay never returns the credential again; rotate it if lost.')


class AuditResponse(serializers.Serializer):
    id = serializers.IntegerField()
    actor = serializers.IntegerField(allow_null=True)
    actor_type = serializers.CharField()
    project = serializers.IntegerField(allow_null=True)
    action = serializers.CharField()
    resource_type = serializers.CharField()
    resource_id = serializers.CharField()
    detail = serializers.DictField(child=serializers.JSONField())
    timestamp = serializers.DateTimeField()


class WebhookDeliveryResponse(serializers.Serializer):
    id = serializers.IntegerField()
    subscription = serializers.IntegerField(required=False)
    event = serializers.CharField(required=False)
    status = serializers.CharField()
    attempts = serializers.IntegerField(required=False)
    available_at = serializers.DateTimeField(required=False)
    delivered_at = serializers.DateTimeField(allow_null=True, required=False)
    last_error = serializers.CharField(required=False)


def page(name, item):
    return type(name, (serializers.Serializer,), {
        'count': serializers.IntegerField(), 'next': serializers.URLField(allow_null=True),
        'previous': serializers.URLField(allow_null=True), 'results': item(many=True),
    })


def install_schema(api):
    list_params = [
        OpenApiParameter('limit', int, description='Page size: 1–500; default 100.'),
        OpenApiParameter('offset', int, description='Zero-based offset.'),
    ]
    project_param = OpenApiParameter('project', int, description='Filter by authorized project ID.')
    anonymous = OpenApiParameter('anonymous', bool, description='Hide annotator identities in review responses.')
    def endpoint(request=None, response=None, paginated=False, parameters=(), code=200):
        return extend_schema(
            tags=['Collaboration'], request=request,
            responses={code: response, 400: ErrorResponse, 401: ErrorResponse, 403: ErrorResponse, 404: ErrorResponse, 409: ErrorResponse, 429: ErrorResponse},
            parameters=[*(list_params if paginated else []), *parameters],
        )
    assignment_page = page('AssignmentPage', api['AssignmentSerializer'])
    specs = {
        'ProjectArchiveAPI': {'post': endpoint(response=api['PolicySerializer'])},
        'ProjectRestoreAPI': {'post': endpoint(response=api['PolicySerializer'])},
        'ProjectPolicyAPI': {'get': endpoint(response=api['PolicySerializer']), 'put': endpoint(api['PolicySerializer'], api['PolicySerializer']), 'patch': endpoint(api['PolicySerializer'], api['PolicySerializer'])},
        'BulkAssignmentAPI': {'get': endpoint(response=assignment_page, paginated=True), 'post': endpoint(api['BulkAssignSerializer'], CountResponse, code=201)},
        'MyAssignmentsAPI': {'get': endpoint(response=assignment_page, paginated=True, parameters=[project_param, OpenApiParameter('status', str)])},
        'AssignmentActionAPI': {'post': endpoint(AssignmentAction, api['AssignmentSerializer'])},
        'AssignmentStaffActionAPI': {'post': endpoint(StaffAssignmentAction, api['AssignmentSerializer'])},
        'DistributeAPI': {'post': endpoint(api['DistributionInputSerializer'], CountResponse)},
        'AssignmentSummaryAPI': {'get': endpoint(response=AssignmentSummary)},
        'ProgressAPI': {'get': endpoint(response=ProgressResponse)},
        'ReviewQueueAPI': {'get': endpoint(response=page('ReviewQueuePage', QueueItem), paginated=True, parameters=[OpenApiParameter('status', str, enum=['pending', 'disagreement', 'accepted', 'rejected', 'all']), anonymous])},
        'TaskReviewAPI': {'get': endpoint(response=TaskReviewResponse, parameters=[anonymous])},
        'ReviewAPI': {'post': endpoint(api['ReviewInputSerializer'], ReviewResponse, code=201)},
        'FinalAnnotationAPI': {'post': endpoint(api['FinalAnnotationSerializer'], FinalResponse, code=201)},
        'MyReviewsAPI': {'get': endpoint(response=page('OwnReviewsPage', OwnReview), paginated=True, parameters=[project_param, OpenApiParameter('task', int)])},
        'ResultsAPI': {'get': endpoint(response=page('ResultsPage', ResultItem), paginated=True, parameters=[OpenApiParameter('only', str, enum=['all', 'accepted', 'final']), anonymous])},
        'ProjectCreateAPI': {'post': endpoint(api['ProjectCreateSerializer'], ProjectResponse, code=201, parameters=[OpenApiParameter('Idempotency-Key', str, location=OpenApiParameter.HEADER, required=True)])},
        'ProjectPublishAPI': {'post': endpoint(response=ProjectResponse)},
        'ProjectWebhooksAPI': {'get': endpoint(response=page('WebhookPage', WebhookResponse), paginated=True), 'post': endpoint(api['WebhookSerializer'], WebhookResponse, code=201)},
        'ProjectWebhookDetailAPI': {'delete': endpoint(code=204)},
        'WebhookDeliveriesAPI': {'get': endpoint(response=page('WebhookDeliveryPage', WebhookDeliveryResponse), paginated=True)},
        'WebhookDeliveryRetryAPI': {'post': endpoint(response=WebhookDeliveryResponse)},
        'ServiceAccountsAPI': {'get': endpoint(response=page('ServiceAccountPage', ServiceAccountResponse), paginated=True), 'post': endpoint(api['ServiceAccountSerializer'], ServiceAccountResponse, code=201)},
        'ServiceAccountDetailAPI': {'get': endpoint(response=ServiceAccountResponse), 'patch': endpoint(api['ServiceAccountSerializer'], ServiceAccountResponse), 'delete': endpoint(code=204)},
        'AuditAPI': {'get': endpoint(response=page('AuditPage', AuditResponse), paginated=True, parameters=[project_param, OpenApiParameter('action', str)])},
    }
    for name, methods in specs.items():
        extend_schema_view(**methods)(api[name])
        scope_docs = {}
        for method in methods:
            scope = api[name].required_scopes.get(method.upper())
            parameters = []
            if (name, method) in {
                ('ReviewAPI', 'post'), ('FinalAnnotationAPI', 'post'), ('ProjectWebhooksAPI', 'post'),
                ('ServiceAccountsAPI', 'post'), ('ServiceAccountDetailAPI', 'patch'),
            }:
                parameters.append(OpenApiParameter(
                    'Idempotency-Key', str, location=OpenApiParameter.HEADER,
                    description='Required for service-account creates; optional for humans. Reuse after a network failure.',
                ))
            if (name, method) == ('ReviewAPI', 'post'):
                parameters.append(OpenApiParameter(
                    'If-Match', str, location=OpenApiParameter.HEADER,
                    description='Annotation revision being reviewed. Collaborative reviews require this header or revision in the body; a missing or stale revision returns 409.',
                ))
            scope_docs[method] = extend_schema(
                operation_id=f'collab_{name.removesuffix("API")}_{method}',
                parameters=parameters,
                description=(api[name].__doc__ or '') + (
                    f'\n\nService-account scope: `{scope}`. Project restrictions also apply.'
                    if scope else '\n\nHuman organization administrators only.'
                ),
            )
        extend_schema_view(**scope_docs)(api[name])
    install_native_contracts()


def install_native_contracts():
    """Add collaboration protocol headers while retaining native serializers, examples and responses."""
    from projects.api import ProjectListAPI, ProjectTaskListAPI
    from data_import.api import ImportAPI, ReImportAPI, TasksBulkCreateAPI
    from tasks.api import AnnotationAPI, AnnotationReviewAPI, AnnotationsListAPI, TaskListAPI

    key = OpenApiParameter(
        'Idempotency-Key', str, location=OpenApiParameter.HEADER,
        description='Required for service-account creates/imports; optional for humans. Reuse the same key and body after a network failure.',
    )
    for view in (ProjectListAPI, ProjectTaskListAPI, ImportAPI, ReImportAPI, TasksBulkCreateAPI, TaskListAPI, AnnotationsListAPI, AnnotationReviewAPI):
        extend_schema_view(post=extend_schema(parameters=[key]))(view)
    match = OpenApiParameter(
        'If-Match', str, location=OpenApiParameter.HEADER,
        description='Current annotation revision, for example "2". Required for collaborative updates unless revision is supplied in the JSON body. A missing or stale revision returns 409.',
    )
    etag = OpenApiParameter(
        'ETag', str, location=OpenApiParameter.HEADER, response=[200],
        description='Quoted annotation revision for the next If-Match update.',
    )
    extend_schema_view(
        get=extend_schema(parameters=[etag]),
        patch=extend_schema(parameters=[match, etag]),
        put=extend_schema(parameters=[match, etag]),
    )(AnnotationAPI)
    extend_schema_view(post=extend_schema(
        parameters=[match, etag],
        responses={200: NativeReviewResponse, 400: NativeErrorResponse, 403: NativeErrorResponse, 409: NativeErrorResponse},
        description='Review an annotation revision. Collaborative reviews require If-Match or revision in the request body; stale or missing revisions return 409. Rejections require a comment.',
    ))(AnnotationReviewAPI)
    # Extend the existing response map, including its native examples, instead of replacing it.
    class ConcurrencyResponses:
        def get_response_serializers(self):
            original = super().get_response_serializers()
            responses = dict(original) if isinstance(original, dict) else {200: original}
            responses[409] = NativeErrorResponse
            return responses
    for name in ('patch', 'put'):
        method = getattr(AnnotationAPI, name)
        original_schema = method.kwargs['schema']
        if not getattr(original_schema, 'collab_concurrency_responses', False):
            method.kwargs['schema'] = type(
                'CollaborativeAnnotationConcurrencySchema', (ConcurrencyResponses, original_schema),
                {'collab_concurrency_responses': True},
            )
    from collab.native_schema import install_native_view_contracts
    install_native_view_contracts()
