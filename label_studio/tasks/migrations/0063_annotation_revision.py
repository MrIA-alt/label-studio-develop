from django.db import migrations, models
from django.db.models import Max


def initialize_revisions(apps, schema_editor):
    Annotation = apps.get_model('tasks', 'Annotation')
    Revision = apps.get_model('collab', 'AnnotationRevision')
    for row in Revision.objects.values('annotation_id').annotate(latest=Max('revision')).iterator():
        Annotation.objects.filter(pk=row['annotation_id']).update(revision=row['latest'] + 1)


class Migration(migrations.Migration):
    dependencies = [('tasks', '0062_annotation_task_assignment'), ('collab', '0003_reviews_service_accounts')]
    operations = [
        migrations.AddField(
            model_name='annotation', name='revision',
            field=models.PositiveIntegerField(default=1, help_text='Optimistic version of the annotation'),
        ),
        migrations.RunPython(initialize_revisions, migrations.RunPython.noop),
    ]
