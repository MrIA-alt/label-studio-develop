"""Fresh project ACLs retain legacy role selection without extra roundtrips."""

from types import SimpleNamespace

import pytest
from django.http import Http404
from django.utils import timezone

from collab import privacy
from collab.api import _project_for
from collab.models import ProjectAnnotationPolicy
from collab.permissions import get_service_account
from collab.tests.test_agent_security import service
from collab.tests.test_blind_mode import client_for, env  # noqa: F401
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember

pytestmark = pytest.mark.django_db


def scoped_project(env, user):
    return privacy.scope_projects(Project.objects.filter(pk=env['project'].pk), user)


def test_duplicate_memberships_keep_the_oldest_active_role(env):
    ProjectMember.objects.create(project=env['project'], user=env['admin'])
    first = OrganizationMember.objects.get(user=env['a'], organization=env['project'].organization)
    OrganizationMember.objects.create(
        user=env['a'], organization=env['project'].organization, role=OrganizationMember.Role.ADMIN,
    )
    # A later duplicate admin row must not turn the first labeller into an admin.
    assert not scoped_project(env, env['a']).exists()
    first.deleted_at = timezone.now()
    first.save(update_fields=['deleted_at'])
    assert scoped_project(env, env['a']).exists()


def test_duplicate_memberships_do_not_hide_an_older_admin_role(env):
    ProjectMember.objects.create(project=env['project'], user=env['a'])
    OrganizationMember.objects.create(
        user=env['admin'], organization=env['project'].organization, role=OrganizationMember.Role.LABELLER,
    )
    assert scoped_project(env, env['admin']).exists()


def test_project_acl_reads_role_and_membership_again_after_revocation(env):
    ProjectMember.objects.create(project=env['project'], user=env['a'])
    assert scoped_project(env, env['admin']).exists()
    members = OrganizationMember.objects.filter(user=env['admin'], organization=env['project'].organization)
    members.update(role=OrganizationMember.Role.LABELLER)
    assert not scoped_project(env, env['admin']).exists()
    members.update(role=OrganizationMember.Role.ADMIN, deleted_at=timezone.now())
    assert not scoped_project(env, env['admin']).exists()


def test_policy_and_project_acl_are_read_together_without_global_caching(env, django_assert_num_queries):
    ProjectAnnotationPolicy.objects.create(project=env['project'], required_annotations=3)
    user = env['a']
    get_service_account(user)  # Isolate the project/policy query from identity discovery.
    request = SimpleNamespace(user=user, method='GET')
    with django_assert_num_queries(1):
        project = _project_for(request, env['project'].pk, with_policy=True)
        assert project.annotation_policy.required_annotations == 3
    ProjectAnnotationPolicy.objects.filter(project=env['project']).update(required_annotations=4)
    with django_assert_num_queries(1):
        assert _project_for(request, env['project'].pk, with_policy=True).annotation_policy.required_annotations == 4
    OrganizationMember.objects.filter(user=user, organization=env['project'].organization).update(deleted_at=timezone.now())
    with pytest.raises(Http404):
        _project_for(request, env['project'].pk, with_policy=True)


def test_policy_get_keeps_legacy_defaults_and_fresh_values(env):
    url = f'/api/extensions/v1/projects/{env["project"].pk}/policy'
    caller = client_for(env['a'])
    assert caller.get(url).json()['required_annotations'] == env['project'].maximum_annotations
    ProjectAnnotationPolicy.objects.create(project=env['project'], required_annotations=3)
    assert caller.get(url).json()['required_annotations'] == 3
    ProjectAnnotationPolicy.objects.filter(project=env['project']).update(required_annotations=4)
    assert caller.get(url).json()['required_annotations'] == 4


def test_policy_get_keeps_service_scopes_and_project_revocation(env):
    url = f'/api/extensions/v1/projects/{env["project"].pk}/policy'
    denied, _, _ = service(env, ['projects:read'])
    assert denied.get(url).status_code == 403
    allowed, account, _ = service(env, ['policy:read'])
    assert allowed.get(url).status_code == 200
    account.projects.clear()
    assert allowed.get(url).status_code in (403, 404)
