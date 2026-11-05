from organizations.models import OrganizationMember


class UserMixin:
    @property
    def is_annotator(self):
        return False

    def is_project_annotator(self, project):
        return False

    def has_permission(self, user):
        return OrganizationMember.objects.filter(
            user=user, organization=user.active_organization, deleted_at__isnull=True
        ).exists()

    def get_organization_role(self, org=None):
        """Return the user's role string in the given organization (or active org).

        Returns None if the user is not a member.
        """
        org = org or getattr(self, 'active_organization', None)
        if org is None:
            return None
        try:
            member = OrganizationMember.objects.get(
                user=self, organization=org, deleted_at__isnull=True,
            )
            return member.role
        except OrganizationMember.DoesNotExist:
            return None

    def is_organization_admin(self, org_pk=None):
        """Check if the user has admin role in the given organization."""
        from organizations.models import Organization

        if org_pk is not None:
            try:
                org = Organization.objects.get(pk=org_pk)
            except Organization.DoesNotExist:
                return False
        else:
            org = getattr(self, 'active_organization', None)

        return self.get_organization_role(org) == OrganizationMember.Role.ADMIN
