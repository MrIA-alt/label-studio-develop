"""Server-side confidentiality rules for collaborative annotation.

Every endpoint exposing annotations, drafts or annotation-derived task statistics must go through these helpers.
A user is "restricted" when its organization role is neither admin nor QA. Restrictions only apply to projects whose
policy enables them, so projects without a policy keep the native Label Studio behaviour.
"""

from types import SimpleNamespace

from organizations.models import OrganizationMember

from collab.models import ProjectAnnotationPolicy, TaskAssignment

UNRESTRICTED_ROLES = (OrganizationMember.Role.ADMIN, OrganizationMember.Role.QA)

# Data Manager columns that do not depend on other users' work. Anything else is dropped from filters/ordering
# for restricted users, since filtering or sorting on a hidden value would leak it.
SAFE_DM_FIELDS = {'id', 'inner_id', 'created_at', 'file_upload', 'storage_filename', 'total_predictions'}
SAFE_DM_PREFIXES = ('data.', 'predictions_')

# Task fields derived from every annotator's work (or from comments / review state).
ANNOTATION_DERIVED_TASK_FIELDS = (
    'annotators',
    'annotations_results',
    'annotations_ids',
    'total_annotations',
    'cancelled_annotations',
    'avg_lead_time',
    'completed_at',
    'reviewed',
    'updated_by',
    'is_labeled',
    'draft_exists',
    'comment_count',
    'unresolved_comment_count',
    'comment_authors',
    'last_comment_updated_at',
    'state',
)
HIDDEN_PREFIXES = ('agreement', 'consensus')


def get_policy(project):
    """Return the project's policy or None. Cached on the project instance when one is given."""
    if project is None:
        return None
    if isinstance(project, int):
        return ProjectAnnotationPolicy.objects.filter(project_id=project).first()
    if not hasattr(project, '_collab_policy'):
        project._collab_policy = ProjectAnnotationPolicy.objects.filter(project_id=project.pk).first()
    return project._collab_policy


def _role(user):
    org = getattr(user, 'active_organization', None)
    cache = getattr(user, '_collab_role_cache', None)
    if cache is None or cache[0] != getattr(org, 'pk', None):
        cache = (getattr(org, 'pk', None), user.get_organization_role(org) if org else None)
        user._collab_role_cache = cache
    return cache[1]


def is_restricted_user(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return True
    return _role(user) not in UNRESTRICTED_ROLES


def is_blind_for(user, project):
    """True if `user` must only see its own annotations/drafts in `project`."""
    policy = get_policy(project)
    return bool(policy and policy.blind_mode and is_restricted_user(user))


def is_assignment_restricted_for(user, project):
    """True if `user` only has access to tasks it has been assigned in `project`."""
    policy = get_policy(project)
    return bool(
        policy and policy.assignment_mode != ProjectAnnotationPolicy.AssignmentMode.OPEN and is_restricted_user(user)
    )


def has_visible_assignment(user, task):
    return TaskAssignment.objects.filter(
        task_id=task.pk, user=user, status__in=TaskAssignment.VISIBLE_STATUSES
    ).exists()


def can_access_task(user, task):
    if is_assignment_restricted_for(user, task.project):
        return has_visible_assignment(user, task)
    return True


def can_access_annotation(user, annotation):
    if not can_access_task(user, annotation.task):
        return False
    if is_blind_for(user, annotation.project):
        return annotation.completed_by_id == user.pk
    return True


def can_access_draft(user, draft):
    if draft.task_id and not can_access_task(user, draft.task):
        return False
    project = draft.task.project if draft.task_id else None
    if project is not None and is_blind_for(user, project):
        return draft.user_id == user.pk
    return True


def scope_annotations(queryset, user, project):
    if is_blind_for(user, project):
        return queryset.filter(completed_by=user)
    return queryset


def scope_drafts(queryset, user, project):
    if is_blind_for(user, project):
        return queryset.filter(user=user)
    return queryset


def scope_tasks(queryset, user, project):
    """Restrict a task queryset to the tasks assigned to `user` when the project requires it."""
    if is_assignment_restricted_for(user, project):
        assigned = TaskAssignment.objects.filter(
            user=user, status__in=TaskAssignment.VISIBLE_STATUSES
        ).values('task_id')
        return queryset.filter(pk__in=assigned)
    return queryset


def own_annotations(annotations, user):
    """Python-side filter for already-prefetched annotations."""
    return [a for a in annotations if a.completed_by_id == user.pk]


def _is_safe_dm_field(name):
    return name in SAFE_DM_FIELDS or name.startswith(SAFE_DM_PREFIXES)


def sanitize_prepare_params(prepare_params):
    """Return a copy of Data Manager prepare params without filters/ordering on annotation-derived columns."""
    if prepare_params is None:
        return prepare_params

    params = prepare_params.model_copy() if hasattr(prepare_params, 'model_copy') else prepare_params.copy()

    ordering = [o for o in (params.ordering or []) if _is_safe_dm_field(o.replace('tasks:', '').lstrip('-+'))]
    params.ordering = ordering

    if params.filters:
        items = []
        for item in params.filters.items:
            if not _is_safe_dm_field(item.filter.replace('filter:tasks:', '')):
                continue
            child = item.child_filter
            if child is not None and not _is_safe_dm_field(child.filter.replace('filter:tasks:', '')):
                item = item.model_copy(update={'child_filter': None}) if hasattr(item, 'model_copy') else item
            items.append(item)
        params.filters = params.filters.model_copy(update={'items': items})
    return params


def scrub_task_representation(ret, task, user, pretty_results=None):
    """Replace every annotation-derived value in a serialized task by values computed from `user`'s own work."""
    own = own_annotations(task.annotations.all(), user)
    done = [a for a in own if not a.was_cancelled]
    cancelled = [a for a in own if a.was_cancelled]

    if 'annotations' in ret:
        ret['annotations'] = [a for a in ret['annotations'] if _annotation_owner_id(a) == user.pk]

    replacements = {
        'annotators': [user.pk] if own else [],
        'annotations_ids': ','.join(str(a.pk) for a in own),
        'total_annotations': len(done),
        'cancelled_annotations': len(cancelled),
        'is_labeled': bool(done),
        'avg_lead_time': (sum(a.lead_time or 0 for a in own) / len(own)) if own else None,
        'completed_at': max((a.created_at for a in own), default=None),
        'reviewed': None,
        'updated_by': [],
        'draft_exists': task.drafts.filter(user=user).exists() if 'draft_exists' in ret else False,
        'comment_count': 0,
        'unresolved_comment_count': 0,
        'comment_authors': [],
        'last_comment_updated_at': None,
    }
    if pretty_results is not None:
        replacements['annotations_results'] = pretty_results(
            SimpleNamespace(annotations_results=[a.result for a in own])
        )
    else:
        replacements['annotations_results'] = ''

    for key in ANNOTATION_DERIVED_TASK_FIELDS:
        if key not in ret:
            continue
        if key in replacements:
            ret[key] = replacements[key]
        else:
            ret.pop(key, None)
    for key in [k for k in ret if k.startswith(HIDDEN_PREFIXES)]:
        ret.pop(key)
    return ret


def _annotation_owner_id(serialized_annotation):
    owner = serialized_annotation.get('completed_by')
    return owner.get('id') if isinstance(owner, dict) else owner
