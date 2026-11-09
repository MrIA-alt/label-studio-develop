from rest_framework import serializers
from users.serializers import UserSerializer


def state_entity(record, fallback=None):
    field_name = getattr(record, '_get_entity_field_name', lambda: None)()
    if (
        field_name
        and getattr(getattr(fallback, '_meta', None), 'model_name', None) == field_name
        and getattr(record, field_name + '_id', None) == fallback.pk
    ):
        return fallback
    return getattr(record, field_name, fallback) if field_name else fallback


def can_see_entity_state(request, entity):
    """Global task/project state is result data, including transition replies."""
    from collab import privacy

    if request is None:
        return True  # Trusted internal serialization without a client identity.
    user = getattr(request, 'user', None)
    entity_name = getattr(getattr(entity, '_meta', None), 'model_name', None)
    key = (entity_name, getattr(entity, 'pk', None))
    cache = getattr(request, '_fsm_state_visibility', None)
    if cache is None:
        cache = request._fsm_state_visibility = {}
    if key in cache:
        return cache[key]
    if entity_name == 'annotation':
        visible = privacy.can_access_annotation(user, entity)
    elif entity_name == 'task':
        visible = privacy.can_access_task(user, entity) and not privacy.is_blind_for(user, entity.project)
    elif entity_name == 'project':
        visible = privacy.can_access_project(user, entity) and not privacy.is_blind_for(user, entity)
    else:
        visible = False
    cache[key] = visible
    return visible


class TriggeredBySerializer(UserSerializer):
    class Meta(UserSerializer.Meta):
        fields = ['id', 'email']


class StateModelSerializer(serializers.Serializer):
    """
    Serializer for FSM state models.

    Uses Serializer instead of ModelSerializer because BaseState is abstract.
    Works with any concrete state model that inherits from BaseState.
    """

    id = serializers.UUIDField(read_only=True)
    state = serializers.CharField(read_only=True)
    previous_state = serializers.CharField(read_only=True, allow_null=True)
    transition_name = serializers.CharField(read_only=True, allow_null=True)
    triggered_by = TriggeredBySerializer(read_only=True, allow_null=True)
    reason = serializers.CharField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    context_data = serializers.JSONField(read_only=True)

    def to_representation(self, instance):
        """
        Override to exclude triggered_by field for annotators.
        """
        request = self.context.get('request')
        if not can_see_entity_state(request, state_entity(instance, self.context.get('entity'))):
            return {}
        data = super().to_representation(instance)

        if request and hasattr(request, 'user'):
            user = request.user
            if getattr(user, 'is_annotator', False):
                data.pop('triggered_by', None)

        return data


class FSMTransitionExecuteRequestSerializer(serializers.Serializer):
    """
    Request body serializer for executing a manual FSM transition.
    """

    transition_name = serializers.CharField()
    transition_data = serializers.DictField(required=False, allow_null=True)


class FSMTransitionExecuteResponseSerializer(serializers.Serializer):
    """
    Response serializer for manual FSM transition execution.
    """

    success = serializers.BooleanField(read_only=True)
    new_state = serializers.CharField(read_only=True, allow_null=True)
    state_record = StateModelSerializer(read_only=True, allow_null=True)

    def to_representation(self, instance):
        entity = self.context.get('entity') or state_entity(instance.get('state_record'))
        if not can_see_entity_state(self.context.get('request'), entity):
            return {'success': instance['success'], 'new_state': None, 'state_record': None}
        return super().to_representation(instance)
