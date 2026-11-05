"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
import logging   # noqa: I001
from typing import Optional

from pydantic import BaseModel, ConfigDict

import rules

logger = logging.getLogger(__name__)


class AllPermissions(BaseModel):
    model_config = ConfigDict(protected_namespaces=('__.*__', '_.*'))

    organizations_create: str = 'organizations.create'
    organizations_view: str = 'organizations.view'
    organizations_change: str = 'organizations.change'
    organizations_delete: str = 'organizations.delete'
    organizations_invite: str = 'organizations.invite'
    projects_create: str = 'projects.create'
    projects_view: str = 'projects.view'
    projects_change: str = 'projects.change'
    projects_delete: str = 'projects.delete'
    projects_reset_cache: str = 'projects.reset_cache'
    tasks_create: str = 'tasks.create'
    tasks_view: str = 'tasks.view'
    tasks_change: str = 'tasks.change'
    tasks_delete: str = 'tasks.delete'
    views_reset: str = 'views.reset'
    annotations_create: str = 'annotations.create'
    annotations_view: str = 'annotations.view'
    annotations_change: str = 'annotations.change'
    annotations_delete: str = 'annotations.delete'
    actions_perform: str = 'actions.perform'
    predictions_any: str = 'predictions.any'
    avatar_any: str = 'avatar.any'
    labels_create: str = 'labels.create'
    labels_view: str = 'labels.view'
    labels_change: str = 'labels.change'
    labels_delete: str = 'labels.delete'
    models_create: str = 'models.create'
    models_view: str = 'models.view'
    models_change: str = 'models.change'
    models_delete: str = 'models.delete'
    model_provider_connection_create: str = 'model_provider_connection.create'
    model_provider_connection_view: str = 'model_provider_connection.view'
    model_provider_connection_change: str = 'model_provider_connection.change'
    model_provider_connection_delete: str = 'model_provider_connection.delete'
    webhooks_view: str = 'webhooks.view'
    webhooks_change: str = 'webhooks.change'
    users_token_any: str = 'users.token.any'

    storages_view: str = 'storages.view'
    storages_change: str = 'storages.change'
    storages_sync: str = 'storages.sync'

    views_view: str = 'views.view'
    views_create: str = 'views.create'
    views_change: str = 'views.change'
    views_delete: str = 'views.delete'


all_permissions = AllPermissions()


class ViewClassPermission(BaseModel):
    model_config = ConfigDict(frozen=True)

    GET: Optional[str] = None
    PATCH: Optional[str] = None
    PUT: Optional[str] = None
    DELETE: Optional[str] = None
    POST: Optional[str] = None


# ---------------------------------------------------------------------------
# Role-based permission mappings
# Keys are OrganizationMember.Role values; values are sets of permission
# strings that the role is allowed to perform.
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[str, set[str]] = {
    'admin': {
        # Projects
        all_permissions.projects_create,
        all_permissions.projects_view,
        all_permissions.projects_change,
        all_permissions.projects_delete,
        all_permissions.projects_reset_cache,
        # Tasks
        all_permissions.tasks_create,
        all_permissions.tasks_view,
        all_permissions.tasks_change,
        all_permissions.tasks_delete,
        # Annotations (label + review + approve)
        all_permissions.annotations_create,
        all_permissions.annotations_view,
        all_permissions.annotations_change,
        all_permissions.annotations_delete,
        # Labels / templates
        all_permissions.labels_create,
        all_permissions.labels_view,
        all_permissions.labels_change,
        all_permissions.labels_delete,
        # Organizations / users
        all_permissions.organizations_create,
        all_permissions.organizations_view,
        all_permissions.organizations_change,
        all_permissions.organizations_delete,
        all_permissions.organizations_invite,
        # Data views
        all_permissions.views_view,
        all_permissions.views_create,
        all_permissions.views_change,
        all_permissions.views_delete,
        all_permissions.views_reset,
        # Actions
        all_permissions.actions_perform,
        # Predictions / ML
        all_permissions.predictions_any,
        all_permissions.models_create,
        all_permissions.models_view,
        all_permissions.models_change,
        all_permissions.models_delete,
        all_permissions.model_provider_connection_create,
        all_permissions.model_provider_connection_view,
        all_permissions.model_provider_connection_change,
        all_permissions.model_provider_connection_delete,
        # Storages
        all_permissions.storages_view,
        all_permissions.storages_change,
        all_permissions.storages_sync,
        # Webhooks
        all_permissions.webhooks_view,
        all_permissions.webhooks_change,
        # Avatar / token
        all_permissions.avatar_any,
        all_permissions.users_token_any,
    },
    'qa': {
        # View tasks
        all_permissions.tasks_view,
        # Review / approve / reject annotations (no creating new annotations)
        all_permissions.annotations_view,
        all_permissions.annotations_change,
        all_permissions.annotations_delete,
        # Labels – view only
        all_permissions.labels_view,
        # Projects – view only + change for member assignment
        all_permissions.projects_view,
        all_permissions.projects_change,
        # Data views – view + create (Data Manager needs these)
        all_permissions.views_view,
        all_permissions.views_create,
        all_permissions.views_change,
        # Organizations – view only (needed for account settings / JWT)
        all_permissions.organizations_view,
        # Predictions / ML – view
        all_permissions.predictions_any,
        all_permissions.models_view,
        all_permissions.model_provider_connection_view,
        # Avatar / token
        all_permissions.avatar_any,
        all_permissions.users_token_any,
    },
    'labeller': {
        # View all tasks
        all_permissions.tasks_view,
        # Label tasks (create + update annotations)
        all_permissions.annotations_create,
        all_permissions.annotations_view,
        all_permissions.annotations_change,
        # Projects – view only
        all_permissions.projects_view,
        # Labels – view only (needed for label links in labeling interface)
        all_permissions.labels_view,
        # Data views – view + create (Data Manager creates a default view on first load)
        all_permissions.views_view,
        all_permissions.views_create,
        all_permissions.views_change,
        # Organizations – view only (needed for account settings / JWT settings)
        all_permissions.organizations_view,
        # Predictions – view
        all_permissions.predictions_any,
        all_permissions.models_view,
        all_permissions.model_provider_connection_view,
        # Avatar / token
        all_permissions.avatar_any,
        all_permissions.users_token_any,
    },
}


def role_has_permission(role: str, permission: str) -> bool:
    """Check whether a given role has a specific permission."""
    perms = ROLE_PERMISSIONS.get(role)
    if perms is None:
        return False
    return permission in perms


def make_perm(name, pred, overwrite=False):
    if rules.perm_exists(name):
        if overwrite:
            rules.remove_perm(name)
        else:
            return
    rules.add_perm(name, pred)


for _, permission_name in all_permissions:
    make_perm(permission_name, rules.is_authenticated)
