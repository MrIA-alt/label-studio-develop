"""A successful mutation and its replay receipt are committed together."""

import hashlib
import json
from collections.abc import Mapping
from functools import wraps

from django.db import connection, transaction
from rest_framework.exceptions import ValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


def authorize_resource(request, operation, resource_id=None):
    """Recheck current object access before consulting a cached successful response."""
    from django.shortcuts import get_object_or_404
    from rest_framework.exceptions import PermissionDenied
    from collab import privacy
    from collab.permissions import get_service_account
    from projects.models import Project
    from tasks.models import Annotation, Task

    if operation.startswith('service-account-'):
        if get_service_account(request.user) is not None or request.user.get_organization_role() != 'admin':
            raise PermissionDenied('Only organization administrators manage service accounts.')
        if resource_id is not None:
            from collab.models import ServiceAccount
            get_object_or_404(ServiceAccount, pk=resource_id, user__active_organization_id=request.user.active_organization_id)
        return
    project = task = annotation = None
    if operation in ('annotation-create', 'final-annotation'):
        task = get_object_or_404(Task, pk=resource_id)
        project = task.project
    elif operation == 'native-review':
        annotation = get_object_or_404(Annotation, pk=resource_id)
        project = annotation.project
    elif operation == 'review-create':
        if not isinstance(request.data, dict):
            raise ValidationError({'detail': 'Expected an annotation review object.'})
        annotation_id = request.data.get('annotation')
        try:
            annotation_id = int(annotation_id)
        except (TypeError, ValueError):
            raise ValidationError({'annotation': 'A valid annotation ID is required.'})
        annotation = get_object_or_404(Annotation, pk=annotation_id)
        project = annotation.project
    elif operation == 'task-create':
        if not isinstance(request.data, dict):
            raise ValidationError({'detail': 'Expected a task object.'})
        try:
            project_id = int(request.data.get('project'))
        except (TypeError, ValueError):
            raise ValidationError({'project': 'A valid project ID is required.'})
        project = get_object_or_404(Project, pk=project_id)
    elif operation in ('webhook-create', 'import', 'project-task-create'):
        project = get_object_or_404(Project, pk=resource_id)
    elif operation in ('project-create', 'native-project-create') and resource_id is not None:
        project = get_object_or_404(Project, pk=resource_id)
    if project is not None and not privacy.can_access_project(request.user, project):
        raise PermissionDenied('This resource is not available.')
    if project is not None and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        from collab.lifecycle import ensure_project_writable
        ensure_project_writable(project)
    if operation == 'annotation-create' and task is not None and not privacy.can_access_task(request.user, task):
        raise PermissionDenied('This resource is not available.')
    if operation == 'annotation-create' and task is not None:
        policy = privacy.get_policy(project)
        if policy and policy.assignment_mode != 'open' and not privacy.has_visible_assignment(request.user, task):
            raise PermissionDenied('This assignment is not available.')
    if operation == 'native-review' and annotation is not None and not privacy.can_access_annotation(request.user, annotation):
        raise PermissionDenied('This resource is not available.')


def request_hash(request):
    files = {}
    file_items = request.FILES.lists() if hasattr(request.FILES, 'lists') else (
        (name, [uploaded]) for name, uploaded in request.FILES.items()
    )
    for name, uploads in file_items:
        files[name] = []
        for uploaded in uploads:
            digest = hashlib.sha256()
            for chunk in uploaded.chunks():
                digest.update(chunk)
            uploaded.seek(0)
            files[name].append({'name': uploaded.name, 'sha256': digest.hexdigest()})
    data = request.data
    if files:
        data = {key: value for key, value in data.lists() if key not in files} if hasattr(data, 'lists') else {key: value for key, value in data.items() if key not in files}
    body = {'data': data, 'files': files, 'query': sorted(request.query_params.lists())}
    # Preconditions identify the revision being changed. Preserve fingerprints
    # of existing clients that did not send a revision header.
    if 'If-Match' in request.headers:
        body['if_match'] = request.headers['If-Match']
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def run_idempotent(request, endpoint, callback, required=True, receipt_transform=None):
    from collab.models import IdempotencyRecord
    from users.models import User

    key = request.headers.get('Idempotency-Key', '').strip()
    if not key and not required:
        return callback()
    if not key or len(key) > 128:
        raise ValidationError({'detail': 'A valid Idempotency-Key header (max 128 chars) is required.'})
    endpoint = f'{request.user.active_organization_id}:{endpoint}'
    if len(endpoint) > 64:
        raise ValueError('Idempotency endpoint is too long.')
    digest = request_hash(request)
    with transaction.atomic():
        # Serialize this caller's receipts without blocking FK KEY SHARE locks
        # from other transactions recording the same actor in audit/history.
        User.objects.select_for_update(no_key=connection.features.has_select_for_no_key_update).get(pk=request.user.pk)
        record = IdempotencyRecord.objects.filter(user=request.user, key=key, endpoint=endpoint).first()
        if record:
            if endpoint.endswith((':project-create', ':native-project-create')) and isinstance(record.response_body, dict):
                authorize_resource(request, endpoint.split(':', 1)[1], record.response_body.get('id'))
            if endpoint.endswith(':service-account-create') and isinstance(record.response_body, dict):
                authorize_resource(request, 'service-account-create', record.response_body.get('id'))
            legacy_digest = hashlib.sha256(json.dumps(request.data, sort_keys=True, default=str).encode()).hexdigest()
            if record.request_hash != digest and not (endpoint.endswith(':project-create') and record.request_hash == legacy_digest):
                return Response({'code': 'idempotency_conflict', 'detail': 'Idempotency-Key reused with a different payload.', 'errors': {}}, status=409)
            if receipt_transform:
                replay_body = receipt_transform(record.response_body)
                if replay_body != record.response_body:
                    # Sanitize receipts written before one-time credentials were
                    # redacted, as well as the response sent by this replay.
                    record.response_body = replay_body
                    record.save(update_fields=['response_body'])
            return Response(record.response_body, status=record.response_status, headers={'Idempotent-Replay': 'true'})
        response = callback()
        if response.status_code >= 400:
            transaction.set_rollback(True)
            return response
        body = json.loads(JSONRenderer().render(response.data))
        if receipt_transform:
            body = receipt_transform(body)
        IdempotencyRecord.objects.create(
            user=request.user, key=key, endpoint=endpoint, request_hash=digest,
            response_status=response.status_code, response_body=body,
        )
        return response


def idempotent_import(method):
    """Native imports accept optional keys for people and require them for machine identities."""
    @wraps(method)
    def wrapped(view, request, *args, **kwargs):
        from collab.permissions import get_service_account
        project_id = kwargs.get('pk') or kwargs.get('project_id')
        authorize_resource(request, 'import', project_id)
        operation = getattr(view, 'idempotency_operation', 'import')
        return run_idempotent(
            request, f'{operation}:{project_id}', lambda: method(view, request, *args, **kwargs),
            required=get_service_account(request.user) is not None,
        )
    return wrapped


def idempotent_project_create(method):
    @wraps(method)
    def wrapped(view, request, *args, **kwargs):
        from collab.permissions import get_service_account, has_service_scope
        from rest_framework.exceptions import PermissionDenied

        if (
            get_service_account(request.user) is not None
            and isinstance(request.data, Mapping)
            and {'maximum_annotations', 'overlap_cohort_percentage'}.intersection(request.data)
            and not has_service_scope(request.user, 'policy:write')
        ):
            raise PermissionDenied('Explicit policy configuration requires policy:write.')
        return run_idempotent(
            request, 'native-project-create', lambda: method(view, request, *args, **kwargs),
            required=get_service_account(request.user) is not None,
        )
    return wrapped


def idempotent_mutation(operation, hide_token=False, redact_fields=()):
    """Use a caller key for non-naturally-idempotent creates; never persist service credentials."""
    def decorate(method):
        credentials = set(redact_fields) | ({'token'} if hide_token else set())

        @wraps(method)
        def wrapped(view, request, *args, **kwargs):
            from collab.permissions import get_service_account
            resource_id = kwargs.get('pk')
            authorize_resource(request, operation, resource_id)
            endpoint = f'{operation}:{resource_id}' if resource_id is not None else operation
            def safe_receipt(body):
                return {
                    **{key: value for key, value in body.items() if key not in credentials},
                    **{f'{key}_shown_once': True for key in credentials if key in body},
                }
            return run_idempotent(
                request, endpoint, lambda: method(view, request, *args, **kwargs),
                required=get_service_account(request.user) is not None,
                receipt_transform=safe_receipt if credentials else None,
            )
        return wrapped
    return decorate
