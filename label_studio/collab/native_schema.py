"""Typed contracts for native endpoints whose APIView implementations do not expose serializers."""

from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import serializers

from projects.serializers import ProjectSerializer
from tasks.serializers import AnnotationDraftSerializer


class AnnotationConversionResponse(AnnotationDraftSerializer):
    retained_annotation_id = serializers.IntegerField(required=False, read_only=True)


class ProjectStateResponse(ProjectSerializer):
    can_delete_tasks = serializers.BooleanField(read_only=True)
    can_manage_annotations = serializers.BooleanField(read_only=True)
    can_manage_tasks = serializers.BooleanField(read_only=True)
    source_syncing = serializers.BooleanField(read_only=True)
    target_syncing = serializers.BooleanField(read_only=True)
    task_count = serializers.IntegerField(read_only=True)
    annotation_count = serializers.IntegerField(read_only=True)
    config_has_control_tags = serializers.BooleanField(read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = [*ProjectSerializer.Meta.fields,
            'can_delete_tasks', 'can_manage_annotations', 'can_manage_tasks', 'source_syncing', 'target_syncing',
            'task_count', 'annotation_count', 'config_has_control_tags',
        ]
        fields = list(dict.fromkeys(fields))


class LabelBulkUpdateResponse(serializers.Serializer):
    annotations_updated = serializers.IntegerField(min_value=0)


class MicrophoneUploadInput(serializers.Serializer):
    audio = serializers.FileField(help_text='Recorded audio file in a multipart/form-data request.')


class MicrophoneUploadResponse(serializers.Serializer):
    url = serializers.CharField(help_text='Serveable Label Studio upload URL, which may be relative.')
    storage_path = serializers.CharField(allow_null=True)


class MicrophoneUploadMissingFile(serializers.Serializer):
    error = serializers.CharField()


def install_native_view_contracts():
    from collab.schema import NativeErrorResponse
    from data_import.api import MicrophoneUploadAPI
    from data_manager.api import ProjectStateAPI
    from labels_manager.api import LabelBulkUpdateAPI
    from labels_manager.serializers import LabelBulkUpdateSerializer
    from organizations.api import OrganizationResetTokenAPI
    from organizations.serializers import OrganizationInviteSerializer
    from projects.api import ProjectMemberDetailAPI
    from projects.serializers import ProjectMemberSerializer
    from tasks.api import AnnotationConvertAPI

    # These attributes are metadata only: the implementations already build the same serializers explicitly.
    AnnotationConvertAPI.serializer_class = AnnotationConversionResponse
    ProjectStateAPI.serializer_class = ProjectStateResponse
    OrganizationResetTokenAPI.serializer_class = OrganizationInviteSerializer
    LabelBulkUpdateAPI.serializer_class = LabelBulkUpdateSerializer
    ProjectMemberDetailAPI.serializer_class = ProjectMemberSerializer
    MicrophoneUploadAPI.serializer_class = MicrophoneUploadInput

    extend_schema_view(post=extend_schema(
        request=None, responses={201: AnnotationConversionResponse, 400: NativeErrorResponse, 403: NativeErrorResponse},
        description='Convert an editable annotation into a personal draft. Collaborative skipped annotations retain their original and return retained_annotation_id; submitted collaborative annotations require the correction workflow.',
    ))(AnnotationConvertAPI)
    extend_schema_view(get=extend_schema(
        responses={200: ProjectStateResponse},
        parameters=[OpenApiParameter('project', int, description='Authorized project ID.')],
    ))(ProjectStateAPI)
    extend_schema_view(post=extend_schema(
        request=None, responses={200: OrganizationInviteSerializer, 201: OrganizationInviteSerializer},
    ))(OrganizationResetTokenAPI)
    extend_schema_view(post=extend_schema(
        request=LabelBulkUpdateSerializer,
        responses={200: LabelBulkUpdateResponse, 400: NativeErrorResponse, 403: NativeErrorResponse},
    ))(LabelBulkUpdateAPI)
    extend_schema_view(delete=extend_schema(
        request=None, responses={204: None, 403: NativeErrorResponse, 404: NativeErrorResponse},
        parameters=[
            OpenApiParameter('id', int, location=OpenApiParameter.PATH, description='Authorized project ID.'),
            OpenApiParameter('user_pk', int, location=OpenApiParameter.PATH, description='Member user ID.'),
        ],
    ))(ProjectMemberDetailAPI)
    extend_schema_view(post=extend_schema(
        request=MicrophoneUploadInput,
        responses={201: MicrophoneUploadResponse, 400: MicrophoneUploadMissingFile, 403: NativeErrorResponse, 404: NativeErrorResponse},
    ))(MicrophoneUploadAPI)
