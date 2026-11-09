from collab import privacy
from rest_framework.exceptions import PermissionDenied
from tasks.models import AnnotationDraft
from tasks.serializers import AnnotationDraftSerializer


class InteractiveMixin:
    def to_representation(self, task):
        user = self.context.get('user')
        if user is not None and not privacy.can_access_task(user, task):
            raise PermissionDenied('The task is not accessible.')
        drafts = AnnotationDraft.objects.filter(task=task, user=user)
        drafts_ser = AnnotationDraftSerializer(drafts, many=True, default=[], read_only=True).data
        data = super().to_representation(task)
        data['drafts'] = drafts_ser
        if user is not None:
            if privacy.is_blind_for(user, task.project):
                privacy.scrub_task_representation(data, task, user)
            policy = privacy.get_policy(task.project)
            if policy and not policy.allow_predictions and (
                privacy.is_restricted_user(user) or privacy.is_blind_for(user, task.project)
            ):
                raise PermissionDenied('Predictions are disabled for this campaign.')
        return data
