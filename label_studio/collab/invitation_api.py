from datetime import timedelta
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from collab import audit, invitations, services
from collab.api import StrictInput, _project_for, paginate
from collab.base import ExtensionView
from collab.lifecycle import ensure_project_writable
from collab.models import ProjectInvitation
from collab.permissions import get_service_account


class InvitationInput(StrictInput, serializers.Serializer):
    emails = serializers.ListField(child=serializers.EmailField(), allow_empty=False, max_length=100)
    task_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=500)

    def validate_emails(self, value):
        return sorted({email.strip().lower() for email in value})


class InvitationOutput(serializers.ModelSerializer):
    task_ids = serializers.PrimaryKeyRelatedField(source='tasks', many=True, read_only=True)
    expired = serializers.SerializerMethodField()

    def get_expired(self, obj) -> bool:
        return obj.status == 'pending' and obj.expires_at <= timezone.now()

    class Meta:
        model = ProjectInvitation
        fields = ('id', 'email', 'status', 'task_ids', 'expires_at', 'expired', 'delivery_status', 'attempts', 'error_code')


def _admin(request):
    if get_service_account(request.user) is not None or request.user.get_organization_role() != 'admin':
        raise PermissionDenied('Only organization administrators can invite annotators.')


class InvitationAdminView(ExtensionView):
    @classmethod
    def as_view(cls, **kwargs):
        view = super().as_view(**kwargs)
        view._dont_enforce_csrf_checks = False
        return view


class ProjectInvitationsAPI(InvitationAdminView):
    @extend_schema(responses=InvitationOutput(many=True), description='Administrator-only invitations with reserved task IDs. No activation tokens are returned.')
    def get(self, request, pk):
        _admin(request)
        project = _project_for(request, pk)
        response = paginate(request, ProjectInvitation.objects.filter(project=project).prefetch_related('tasks'),
                            lambda rows: InvitationOutput(rows, many=True).data)
        response.data['mail_configured'] = invitations.smtp_ready()
        return response

    @extend_schema(request=InvitationInput, responses={201: InvitationOutput(many=True)})
    def post(self, request, pk):
        _admin(request)
        project = _project_for(request, pk)
        serializer = InvitationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        rows = invitations.reserve(project, actor=request.user, **serializer.validated_data)
        # Delivery is durable and performed by the maintenance worker after commit.
        return Response(InvitationOutput(rows, many=True).data, status=201)


class ProjectInvitationActionAPI(InvitationAdminView):
    @extend_schema(request=None, responses=InvitationOutput, description='Cancel or resend a pending invitation. Resending replaces the previous link after successful delivery.')
    @transaction.atomic
    def post(self, request, pk, invitation_id, action):
        _admin(request)
        project = _project_for(request, pk)
        services.lock_project(project.pk)
        ensure_project_writable(project)
        row = get_object_or_404(ProjectInvitation.objects.select_for_update(), pk=invitation_id, project=project)
        if request.data or row.status != 'pending' or action not in ('cancel', 'resend'):
            raise serializers.ValidationError('Select a pending invitation and an empty request body.')
        if action == 'cancel':
            row.status = 'cancelled'
            row.token_digest = ''
        else:
            row.token_digest = ''
            row.created_by = request.user
            row.delivery_status = 'pending'
            row.attempts = 0
            row.next_attempt_at = timezone.now()
            row.expires_at = timezone.now() + timedelta(days=7)
        row.save()
        audit.record(request, 'invitation.' + action, project, 'invitation', row.pk)
        return Response(InvitationOutput(row).data)
