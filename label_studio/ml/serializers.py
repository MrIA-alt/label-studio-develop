"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
from core.utils.io import validate_upload_url
from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from ml.models import MLBackend, MLBackendAuth
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied


def can_manage_ml(user):
    from collab.permissions import get_service_account

    return bool(
        getattr(user, 'is_authenticated', False) and user.is_active
        and get_service_account(user) is None and user.get_organization_role() == 'admin'
    )


def ensure_ml_management(user, project=None):
    from collab.lifecycle import ensure_project_writable
    from collab.privacy import can_access_project

    # A destination receiving source credentials must be chosen by an
    # administrator. Generic project editing never delegates that trust.
    if not can_manage_ml(user):
        raise PermissionDenied('Only organization administrators can manage ML backends.')
    if project is not None:
        if not can_access_project(user, project):
            raise PermissionDenied('The project is not accessible.')
        ensure_project_writable(project)


class MLBackendSummarySerializer(serializers.ModelSerializer):
    """Editor discovery without credentials, backend addresses or configuration."""

    class Meta:
        model = MLBackend
        fields = ('id', 'title', 'is_interactive', 'state', 'model_version')
        read_only_fields = fields


class MLBackendSerializer(serializers.ModelSerializer):
    """
    Serializer for MLBackend model.
    """

    readable_state = serializers.SerializerMethodField()
    basic_auth_pass = serializers.CharField(write_only=True, required=False, allow_null=True, allow_blank=True)
    basic_auth_pass_is_set = serializers.SerializerMethodField()

    @extend_schema_field(bool)
    def get_basic_auth_pass_is_set(self, obj):
        return bool(obj.basic_auth_pass)

    def get_readable_state(self, obj):
        return obj.get_state_display()

    def validate_basic_auth_pass(self, value):
        # Checks if the new password and old password are non-existent.
        if not value:
            if not self.instance.basic_auth_pass:
                raise serializers.ValidationError('Authentication password is required for Basic Authentication.')
            else:
                # If user is not changing the password, return the old password.
                return self.instance.basic_auth_pass
        return value

    def validate_url(self, value):
        validate_upload_url(value, block_local_urls=settings.ML_BLOCK_LOCAL_IP)

        return value

    def _validate_authentication(self, attrs):
        if attrs.get('auth_method') == MLBackendAuth.BASIC_AUTH:
            required_fields = ['basic_auth_user', 'basic_auth_pass']

            if any(field not in attrs for field in required_fields):
                raise serializers.ValidationError(
                    'Authentication username and password is required for Basic Authentication.'
                )

    def _validate_healthcheck(self, attrs):
        healthcheck_response = MLBackend.healthcheck_(**attrs)

        if healthcheck_response.is_error:
            if healthcheck_response.status_code == 401:
                message = (
                    'Able to connect to ML Server, but authentication parameters were '
                    'either not provided or are incorrect.'
                )
            else:
                message = (
                    f"Can't connect to ML backend {attrs['url']}, health check failed. "
                    'Make sure it is up and your firewall is properly configured. '
                    f'<a href="https://labelstud.io/guide/ml.html">Learn more</a> '
                    f'about how to set up an ML backend. Additional info: {healthcheck_response.error_message}'
                )

            raise serializers.ValidationError(message)

    def _validate_setup(self, attrs):
        setup_response = MLBackend.setup_(**attrs)

        if setup_response.is_error:
            message = (
                f"Successfully connected to {attrs['url']} but it doesn't look like a valid ML backend. "
                f'Reason: {setup_response.error_message}.\n'
                'Check the ML backend server console logs to check the status.'
                'There might be something wrong with your model or it might be incompatible with the current labeling configuration.'
            )

            raise serializers.ValidationError(message)

    def validate(self, attrs):
        attrs = super().validate(attrs)

        request = self.context.get('request')
        if request is not None:
            project = attrs.get('project') or (self.instance.project if self.instance is not None else None)
            ensure_ml_management(request.user, project)
            if self.instance is not None and project is not None and project.pk != self.instance.project_id:
                raise serializers.ValidationError({'project': 'An ML backend cannot be moved to another project.'})

        self._validate_authentication(attrs)
        self._validate_healthcheck(attrs)
        self._validate_setup(attrs)

        return attrs

    class Meta:
        model = MLBackend
        fields = [
            'id',
            'state',
            'readable_state',
            'is_interactive',
            'url',
            'error_message',
            'title',
            'auth_method',
            'basic_auth_user',
            'basic_auth_pass',
            'basic_auth_pass_is_set',
            'description',
            'extra_params',
            'model_version',
            'timeout',
            'created_at',
            'updated_at',
            'auto_update',
            'project',
        ]


class MLInteractiveAnnotatingRequest(serializers.Serializer):
    """
    Serializer for ML interactive annotating request.
    """

    task = serializers.IntegerField(help_text='ID of task to annotate', required=True)
    context = serializers.JSONField(help_text='Context for ML model', allow_null=True, default=None)
