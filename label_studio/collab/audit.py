"""Audit trail without request payloads, credentials or mutable organization ownership."""

import logging
import re

from collab.models import AuditEvent

logger = logging.getLogger(__name__)


def record(request_or_user, action, project=None, resource_type='', resource_id='', **detail):
    user = getattr(request_or_user, 'user', request_or_user)
    is_service = hasattr(user, 'collab_service_account')
    return AuditEvent.objects.create(
        actor=user if getattr(user, 'pk', None) else None,
        actor_type=AuditEvent.ActorType.SERVICE if is_service else AuditEvent.ActorType.USER,
        organization_id=project.organization_id if project else getattr(user, 'active_organization_id', None),
        project=project,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id != '' else '',
        detail=detail,
    )


class CollabAuditMiddleware:
    """Cover native mutations as well as extension business events; never store bodies or queries."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, 'user', None)
        if (
            request.method not in ('GET', 'HEAD', 'OPTIONS')
            and request.path.startswith('/api/')
            and user is not None and user.is_authenticated
            and (response.status_code < 400 or response.status_code in (401, 403))
        ):
            try:
                from projects.models import Project
                match = re.match(r'^/api/(?:extensions/v1/)?projects/(\d+)(?:/|$)', request.path)
                project = Project.objects.filter(pk=match.group(1), organization_id=user.active_organization_id).first() if match else None
                record(
                    user, 'api.denied' if response.status_code in (401, 403) else f'api.{request.method.lower()}',
                    project, 'api', '', path=request.path, status=response.status_code,
                )
            except Exception:
                logger.exception('Could not persist administrative audit event.')
        return response
