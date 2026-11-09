from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('webhooks', '0004_auto_20221221_1101'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='webhook', name='approved_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='approved_native_webhooks', to=settings.AUTH_USER_MODEL,
                help_text='Administrator or scoped service account that approved full payload delivery.',
            ),
        ),
    ]
