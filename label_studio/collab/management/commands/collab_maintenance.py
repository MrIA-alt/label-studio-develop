from django.core.management.base import BaseCommand

from collab import reviews, services, webhooks, invitations
from collab.models import ProjectAnnotationPolicy


class Command(BaseCommand):
    help = 'Expire overdue assignments and redistribute tasks of automatic/hybrid projects. Safe to run periodically.'

    def add_arguments(self, parser):
        parser.add_argument('--agreement-limit', type=int, default=500)
        parser.add_argument('--recompute-agreement', action='store_true')

    def handle(self, *args, **options):
        expired = services.expire_overdue()
        created = 0
        policies = ProjectAnnotationPolicy.objects.filter(is_archived=False, assignment_mode__in=['automatic', 'hybrid']).select_related(
            'project'
        )
        for policy in policies:
            created += services.distribute_project(policy.project)
        refreshed = 0
        for policy in ProjectAnnotationPolicy.objects.filter(is_archived=False).select_related('project').order_by('pk'):
            tasks = reviews.eligible_tasks(policy.project).filter(n_annotators__gte=2).order_by('id')
            if not options['recompute_agreement']:
                tasks = tasks.filter(precomputed_agreement__isnull=True)
            ids = tasks.values_list('id', flat=True)[:max(0, options['agreement_limit'] - refreshed)]
            for task_id in ids:
                reviews.refresh_task_agreement(task_id)
                refreshed += 1
            if refreshed >= options['agreement_limit']:
                break
        deliveries = webhooks.drain_outbox(limit=100)
        invitations.drain_invitations(limit=5)
        self.stdout.write(f'expired={expired} created={created} agreements={refreshed} webhooks={deliveries}')
