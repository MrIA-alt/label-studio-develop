import logging

from rest_framework.permissions import SAFE_METHODS, BasePermission

from core.permissions import ViewClassPermission, role_has_permission

logger = logging.getLogger(__name__)


class HasObjectPermission(BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.has_permission(request.user)


class MemberHasOwnerPermission(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method not in SAFE_METHODS and not request.user.own_organization:
            return False

        return obj.has_permission(request.user)


class RoleBasedPermission(BasePermission):
    """
    Checks the requesting user's organization role against the
    ``permission_required`` attribute declared on the view.

    ``permission_required`` can be:
      - a plain permission string  (e.g. ``all_permissions.projects_view``)
      - a ``ViewClassPermission`` instance that maps HTTP methods to
        permission strings.

    If the view does not declare ``permission_required``, access is allowed
    (falls through to other permission classes such as IsAuthenticated).
    """

    message = 'You do not have permission to perform this action based on your role.'

    def has_permission(self, request, view):
        perm_required = getattr(view, 'permission_required', None)
        if perm_required is None:
            return True

        # Resolve the permission string for this HTTP method
        if isinstance(perm_required, ViewClassPermission):
            permission = getattr(perm_required, request.method, None)
            if permission is None:
                return True
        else:
            permission = perm_required

        user = request.user
        if not user or not user.is_authenticated:
            return False

        role = user.get_organization_role()
        if role is None:
            return False

        allowed = role_has_permission(role, permission)
        if not allowed:
            logger.warning(
                'RoleBasedPermission denied: user=%s role=%s permission=%s method=%s path=%s view=%s',
                user, role, permission, request.method, request.path, view.__class__.__name__,
            )
        return allowed
