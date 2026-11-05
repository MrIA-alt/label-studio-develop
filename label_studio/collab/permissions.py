"""Service-account scopes, throttling and the base class of every /api/extensions/v1 view."""

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


def get_service_account(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    try:
        return user.collab_service_account
    except Exception:
        return None


class ServiceAccountPermission(BasePermission):
    """Global gate: service accounts only reach extension views holding the needed scope, or native ones with `native`."""

    def has_permission(self, request, view):
        account = get_service_account(request.user)
        if account is None:
            return True
        if not account.active:
            return False
        scopes = set(account.scopes or [])
        if getattr(view, 'collab_extension', False):
            required = view.scope_for(request.method)
            return required is None or required in scopes
        return 'native' in scopes


def ensure_project_allowed(user, project):
    """Service accounts bound to a project list may only touch those projects."""
    account = get_service_account(user)
    if account is not None and account.projects.exists() and not account.projects.filter(pk=project.pk).exists():
        raise PermissionDenied('This service account is not allowed on this project.')
