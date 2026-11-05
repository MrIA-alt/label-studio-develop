from django.core.management.base import BaseCommand

from collab import services
from collab.models import ProjectAnnotationPolicy


class Command(BaseCommand):
    help = 'Expire overdue assignments and redistribute tasks of automatic/hybrid projects. Safe to run periodically.'

    def handle(self, *args, **options):
        expired = services.expire_overdue()
        created = 0
        policies = ProjectAnnotationPolicy.objects.filter(assignment_mode__in=['automatic', 'hybrid']).select_related(
            'project'
        )
        for policy in policies:
            created += services.distribute_project(policy.project)
        self.stdout.write(f'expired={expired} created={created}')
