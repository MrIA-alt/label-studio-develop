class TaskMixin:
    def has_permission(self, user: 'User') -> bool:  # noqa: F821
        """Called by Task#has_permission"""
        from collab.privacy import can_access_task

        return can_access_task(user, self)

    def _get_is_labeled_value(self) -> bool:
        from collab.privacy import get_policy

        policy = get_policy(self.project)
        if policy is not None:
            n = self.annotations.filter(
                was_cancelled=False, ground_truth=False, completed_by__isnull=False, result__isnull=False
            ).values('completed_by').distinct().count()
            return n >= policy.required_annotations
        n = self.completed_annotations.values('completed_by').distinct().count()
        return n >= self.overlap

    def update_is_labeled(self, *args, **kwargs) -> None:
        self.is_labeled = self._get_is_labeled_value()

    @classmethod
    def post_process_bulk_update_stats(cls, tasks) -> None:
        pass

    def before_delete_actions(self):
        """
        Actions to execute before task deletion
        """
        pass

    @staticmethod
    def after_bulk_delete_actions(tasks_ids, project):
        """
        Actions to execute after bulk task deletion
        """
        pass

    def get_rejected_query(self):
        pass

    def can_be_skipped(self) -> bool:
        return True


class AnnotationMixin:
    def has_permission(self, user: 'User') -> bool:  # noqa: F821
        """Called by Annotation#has_permission"""
        from collab.privacy import can_access_annotation

        return can_access_annotation(user, self)
