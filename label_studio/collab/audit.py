"""Audit trail of administrative operations."""

from collab.models import AuditEvent


def record(request_or_user, action, project=None, resource_type='', resource_id='', **detail):
    user = getattr(request_or_user, 'user', request_or_user)
    is_service = hasattr(user, 'collab_service_account')
    return AuditEvent.objects.create(
        actor=user if getattr(user, 'pk', None) else None,
        actor_type=AuditEvent.ActorType.SERVICE if is_service else AuditEvent.ActorType.USER,
        project=project,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id != '' else '',
        detail=detail,
    )
