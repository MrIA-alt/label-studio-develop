"""Server-side confidentiality rules for collaborative annotation.

Every endpoint exposing annotations, drafts or annotation-derived task statistics must go through these helpers.
A user is "restricted" when its organization role is neither admin nor QA. Restrictions only apply to projects whose
policy enables them, so projects without a policy keep the native Label Studio behaviour.
"""

from types import SimpleNamespace

from organizations.models import OrganizationMember
from django.db.models import Exists, OuterRef, Q, Subquery

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


def scope_projects(queryset, user):
    """Apply the same tenant, membership and service-account boundary to every caller.

    Projects without explicit memberships retain the Community organisation-wide access.
    Once memberships exist, QA and labellers need an enabled membership.
    """
    from collab.permissions import get_service_account, scope_queryset_for_service
    from projects.models import ProjectMember

    if user is None or not getattr(user, 'is_authenticated', False) or not user.is_active:
        return queryset.none()
    org_id = getattr(user, 'active_organization_id', None)
    membership = OrganizationMember.objects.filter(
        user_id=user.pk, organization_id=org_id, deleted_at__isnull=True
    ).order_by('pk')
    account = get_service_account(user)
    if account is not None and not account.active:
        return queryset.none()
    # Keep the same oldest-active-membership semantics as first(), including
    # historical duplicate rows, while checking the role inside the SQL that
    # reads the project. No permission decision survives this query.
    queryset = queryset.alias(
        _collab_member_role=Subquery(membership.values('role')[:1]),
    ).filter(organization_id=org_id, _collab_member_role__isnull=False)
    if account is None:
        members = ProjectMember.objects.filter(project_id=OuterRef('pk'))
        queryset = queryset.alias(
            _collab_has_members=Exists(members),
            _collab_is_member=Exists(members.filter(user_id=user.pk, enabled=True)),
        ).filter(
            Q(_collab_member_role=OrganizationMember.Role.ADMIN)
            | Q(_collab_has_members=False)
            | Q(_collab_is_member=True)
        )
    return scope_queryset_for_service(queryset, user, project_field='pk')


def can_access_project(user, project):
    from projects.models import Project

    return project is not None and scope_projects(Project.objects.filter(pk=project.pk), user).exists()


def can_access_upload(user, upload):
    """Source media follows task access; another annotator's uploaded result stays private."""
    from pathlib import PurePosixPath
    from tasks.models import Task

    if upload is None or not can_access_project(user, upload.project):
        return False
    if (not is_restricted_user(user) and not is_blind_for(user, upload.project)) or upload.user_id == user.pk:
        return True
    if is_blind_for(user, upload.project) and PurePosixPath(upload.file.name).suffix.lower() in {
        '.json', '.jsonl', '.ndjson', '.csv', '.tsv', '.zip', '.parquet'
    }:
        return False
    # The project boundary was checked above. On a manual campaign, drive the
    # JSON lookup from this user's indexed assignment set instead of all tasks.
    sources = Task.objects.filter(project_id=upload.project_id)
    assigned_only = is_assignment_restricted_for(user, upload.project)
    if assigned_only:
        visible = TaskAssignment.objects.filter(
            project_id=upload.project_id, user=user, status__in=TaskAssignment.VISIBLE_STATUSES
        ).values('task_id')
        sources = sources.filter(pk__in=visible)
    if sources.filter(file_upload_id=upload.pk).exists():
        linked = Task.objects.filter(file_upload_id=upload.pk)
        # A manifest can create several source tasks. Knowing one of its rows
        # does not authorize downloading the remaining, unassigned source rows.
        visible_linked = scope_tasks(linked, user)
        if not linked.exclude(pk__in=visible_linked.values('pk')).exists():
            return True
    return source_uri_tasks(sources, upload.url).exists()


def source_uri_tasks(tasks, uri):
    """Find exact string values in source JSON, without loading tasks into Python.

    JSON imports may own Task.file_upload while the source image/audio is a
    separate upload referenced by Task.data. Object keys and substrings are not
    source references; annotation and draft JSON are deliberately never searched.
    """
    import json
    from urllib.parse import urlsplit

    from django.conf import settings
    from django.db import connections
    from django.db.models import BooleanField
    from django.db.models.expressions import RawSQL

    connection = connections[tasks.db]
    uris = [uri]
    configured = urlsplit(settings.HOSTNAME or '')
    if uri.startswith('/') and not uri.startswith('//') and configured.scheme in ('http', 'https') and configured.netloc:
        base_path = configured.path.rstrip('/')
        prefix = '' if base_path and uri.startswith(base_path + '/') else base_path
        uris.append(f'{configured.scheme}://{configured.netloc}{prefix}{uri}')
    column = connection.ops.quote_name(tasks.model._meta.db_table) + '.' + connection.ops.quote_name('data')
    if connection.vendor == 'postgresql':
        condition = RawSQL(
            f'jsonb_path_exists({column}, %s::jsonpath, %s::jsonb)',
            ('$.** ? (@.type() == "string" && @ == $uris[*])', json.dumps({'uris': uris})),
            output_field=BooleanField(),
        )
    elif connection.vendor == 'sqlite':
        condition = RawSQL(
            f"EXISTS (SELECT 1 FROM json_tree({column}) WHERE type = 'text' AND value IN ({', '.join(['%s'] * len(uris))}))",
            tuple(uris), output_field=BooleanField(),
        )
    else:
        # The collaborative deployment targets PostgreSQL, with SQLite for
        # isolated tests; other backends retain file_upload-based authorization.
        return tasks.none()
    return tasks.alias(_collab_source_uri=condition).filter(_collab_source_uri=True)


def is_blind_for(user, project):
    """True if `user` must only see its own annotations/drafts in `project`."""
    from collab.permissions import get_service_account, has_service_scope

    if get_service_account(user) is not None and not (
        has_service_scope(user, 'results:read') or has_service_scope(user, 'reviews:read')
    ):
        return True
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
    if not can_access_project(user, task.project):
        return False
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


def scope_tasks(queryset, user, project=None):
    """Restrict a task queryset to the tasks assigned to `user` when the project requires it."""
    from projects.models import Project

    accessible = scope_projects(Project.objects.all(), user).values('pk')
    queryset = queryset.filter(project_id__in=accessible)
    if is_restricted_user(user):
        restricted_projects = ProjectAnnotationPolicy.objects.exclude(
            assignment_mode=ProjectAnnotationPolicy.AssignmentMode.OPEN
        ).values('project_id')
        assigned = TaskAssignment.objects.filter(
            user=user, status__in=TaskAssignment.VISIBLE_STATUSES
        ).values('task_id')
        queryset = queryset.filter(~Q(project_id__in=restricted_projects) | Q(pk__in=assigned))
    return queryset


def scope_task_annotations(queryset, user):
    """Scope aggregate counts across a mix of blind and ordinary projects."""
    blind_projects = ProjectAnnotationPolicy.objects.filter(blind_mode=True).values('project_id')
    from collab.permissions import get_service_account, has_service_scope

    if get_service_account(user) is not None and not (
        has_service_scope(user, 'results:read') or has_service_scope(user, 'reviews:read')
    ):
        return queryset.filter(completed_by=user)
    if is_restricted_user(user):
        queryset = queryset.filter(Q(completed_by=user) | ~Q(project_id__in=blind_projects))
    return queryset


def own_annotations(annotations, user):
    """Python-side filter for already-prefetched annotations."""
    return [a for a in annotations if a.completed_by_id == user.pk]


def _is_safe_dm_field(name):
    return name in SAFE_DM_FIELDS or name.startswith(SAFE_DM_PREFIXES)


def sanitize_prepare_params(prepare_params, allow_predictions=True):
    """Return a copy of Data Manager prepare params without filters/ordering on annotation-derived columns."""
    if prepare_params is None:
        return prepare_params

    params = prepare_params.model_copy() if hasattr(prepare_params, 'model_copy') else prepare_params.copy()

    def safe(name):
        return _is_safe_dm_field(name) and (
            allow_predictions or (not name.startswith('predictions_') and name != 'total_predictions')
        )

    ordering = [o for o in (params.ordering or []) if safe(o.replace('tasks:', '').lstrip('-+'))]
    params.ordering = ordering

    if params.filters:
        items = []
        for item in params.filters.items:
            if not safe(item.filter.replace('filter:tasks:', '')):
                continue
            child = item.child_filter
            if child is not None and not safe(child.filter.replace('filter:tasks:', '')):
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
        'updated_at': task.created_at,
    }
    if 'updated_at' in ret:
        ret['updated_at'] = task.created_at.isoformat()
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
