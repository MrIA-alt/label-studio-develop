"""Explicit action and project permissions for machine identities."""

from core.permissions import ViewClassPermission
from django.core.exceptions import ObjectDoesNotExist
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission


def get_service_account(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    try:
        return user.collab_service_account
    except ObjectDoesNotExist:
        return None


def service_project_ids(user):
    account = get_service_account(user)
    if account is None:
        return None
    if account.all_projects:
        return None
    return set(account.projects.values_list('pk', flat=True))


def scope_queryset_for_service(queryset, user, project_field='project_id'):
    project_ids = service_project_ids(user)
    return queryset if project_ids is None else queryset.filter(**{f'{project_field}__in': project_ids})


def has_service_scope(user, scope):
    account = get_service_account(user)
    return bool(account and account.active and scope in (account.scopes or []))


def _native_scope(request, view):
    """Map native resource operations to narrow scopes; unknown endpoints fail closed."""
    permission = getattr(view, 'permission_required', None)
    if isinstance(permission, ViewClassPermission):
        permission = getattr(permission, 'GET' if request.method == 'HEAD' else request.method, None)
    if permission is None:
        return None
    resource, _, operation = permission.partition('.')
    reading = request.method in SAFE_METHODS
    if view.__class__.__name__ == 'ReImportAPI' and request.method == 'POST':
        return frozenset(('imports:write', 'tasks:delete', 'annotations:delete'))
    if view.__class__.__name__ == 'FileUploadListAPI' and request.method == 'DELETE':
        return frozenset(('projects:write', 'tasks:delete'))
    if view.__class__.__name__ == 'ProjectModelVersions' and request.method == 'DELETE':
        return frozenset(('projects:write', 'predictions:delete'))
    if view.__class__.__name__ == 'AnnotationReviewAPI' and not reading:
        return 'reviews:write'
    if view.__class__.__name__ == 'AnnotationConvertAPI' and not reading:
        return frozenset(('annotations:delete', 'annotations:write'))
    if view.__class__.__name__ == 'ProjectMemberListAPI' and reading:
        return ('assignments:read', 'reviews:read')
    if view.__class__.__name__ == 'ProjectActionsAPI' and not reading:
        return {
            'next_task': 'tasks:read',
            'delete_tasks': 'tasks:delete',
            'delete_tasks_annotations': 'annotations:delete',
        }.get(request.query_params.get('id'))
    if view.__class__.__name__ == 'ProjectTaskListAPI' and request.method == 'POST':
        return 'imports:write'
    if view.__class__.__module__.startswith('data_export'):
        return 'results:read'
    if view.__class__.__module__.startswith('data_import') and request.method == 'POST' and view.__class__.__name__ in (
        'ImportAPI', 'ReImportAPI', 'TasksBulkCreateAPI', 'ImportPredictionsAPI',
    ):
        return 'imports:write'
    if view.__class__.__module__.startswith('data_import') and reading:
        return ('tasks:read', 'results:read', 'reviews:read') if view.__class__.__name__ in ('UploadedFileResponse', 'DownloadStorageData') else 'results:read'
    if resource == 'projects':
        return 'projects:read' if reading else ('projects:delete' if operation == 'delete' else 'projects:write')
    if resource == 'tasks':
        return ('tasks:read', 'results:read', 'reviews:read') if reading else (
            'tasks:delete' if operation == 'delete' else ('imports:write' if operation == 'create' else 'tasks:write')
        )
    if resource == 'annotations':
        return ('results:read', 'reviews:read', 'annotations:write') if reading else ('annotations:delete' if operation == 'delete' else 'annotations:write')
    if resource == 'webhooks':
        return 'webhooks:write'
    if resource in ('views', 'labels', 'predictions') and reading:
        return 'tasks:read'
    return None


class ServiceAccountPermission(BasePermission):
    """Scopes are authoritative for service accounts; the legacy `native` scope grants no rights."""

    def has_permission(self, request, view):
        account = get_service_account(request.user)
        if account is None:
            return True
        if not account.active or not request.user.is_active:
            return False
        required = view.scope_for(request.method) if getattr(view, 'collab_extension', False) else _native_scope(request, view)
        if isinstance(required, frozenset):
            return bool(required) and required.issubset(set(account.scopes or []))
        alternatives = required if isinstance(required, (tuple, list)) else (required,)
        return any(scope and scope in (account.scopes or []) for scope in alternatives)

    def has_object_permission(self, request, view, obj):
        if get_service_account(request.user) is None:
            return True
        project = obj if obj.__class__.__name__ == 'Project' else getattr(obj, 'project', None)
        if project is not None:
            ensure_project_allowed(request.user, project)
        return True


def ensure_project_allowed(user, project):
    """Service accounts bound to a project list may only touch those projects."""
    account = get_service_account(user)
    if account is None:
        return
    if not account.active or project.organization_id != user.active_organization_id:
        raise PermissionDenied('This service account is not allowed on this project.')
    ids = service_project_ids(user)
    if ids is not None and project.pk not in ids:
        raise PermissionDenied('This service account is not allowed on this project.')
