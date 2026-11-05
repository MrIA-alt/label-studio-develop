from django.db import migrations, models


def set_admin_for_org_creators(apps, schema_editor):
    """Set the 'admin' role for organization creators."""
    OrganizationMember = apps.get_model('organizations', 'OrganizationMember')
    Organization = apps.get_model('organizations', 'Organization')

    for org in Organization.objects.all():
        if org.created_by_id is not None:
            OrganizationMember.objects.filter(
                organization=org, user_id=org.created_by_id, deleted_at__isnull=True
            ).update(role='admin')


class Migration(migrations.Migration):

    dependencies = [
        ('organizations', '0006_alter_organizationmember_deleted_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationmember',
            name='role',
            field=models.CharField(
                choices=[('admin', 'Admin'), ('qa', 'QA (Supervisor)'), ('labeller', 'Labeller (Agent)')],
                default='labeller',
                help_text='The role of this member within the organization.',
                max_length=20,
                verbose_name='role',
            ),
        ),
        migrations.RunPython(set_admin_for_org_creators, migrations.RunPython.noop),
    ]
