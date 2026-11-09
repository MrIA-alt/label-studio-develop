from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('collab', '0004_agent_reliability')]

    operations = [migrations.AddField(
        model_name='projectannotationpolicy', name='is_archived',
        field=models.BooleanField(default=False, verbose_name='archived'),
    )]
