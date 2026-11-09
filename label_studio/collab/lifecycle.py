"""Archive campaigns without deleting their sources, annotations or history."""
from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from collab.models import ProjectAnnotationPolicy


class ProjectArchived(PermissionDenied):
    default_detail = 'This project is archived. Restore it before changing its work.'
    default_code = 'project_archived'


def is_archived(project):
    pk = project if isinstance(project, int) else project.pk
    # Check current persistent state rather than a policy cached before archival.
    return ProjectAnnotationPolicy.objects.filter(project_id=pk, is_archived=True).exists()


def ensure_project_writable(project):
    if project is not None and is_archived(project):
        raise ProjectArchived()


@transaction.atomic
def set_archived(project, actor, archived):
    from collab import audit, webhooks
    from collab.services import lock_project

    project = lock_project(project.pk)
    # Legacy projects acquire lifecycle state without changing their visibility
    # or native distribution/review behaviour.
    defaults = {
        'required_annotations': project.maximum_annotations or 1,
        'blind_mode': False, 'assignment_mode': 'open', 'review_mode': 'none',
    }
    policy = ProjectAnnotationPolicy.objects.filter(project=project).first()
    if policy is None:
        policy = ProjectAnnotationPolicy(project=project, **defaults)
        if not archived:
            return policy
        policy.save()
    if policy.is_archived != archived:
        policy.is_archived = archived
        policy.save(update_fields=['is_archived', 'updated_at'])
        event = 'project.archived' if archived else 'project.restored'
        audit.record(actor, event, project, 'project', project.pk)
        webhooks.emit(project, event, {'is_archived': archived})
    return policy
