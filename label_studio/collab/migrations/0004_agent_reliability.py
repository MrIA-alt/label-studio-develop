import hashlib

from django.db import migrations, models
import django.db.models.deletion


def migrate_tokens(apps, schema_editor):
    Account = apps.get_model('collab', 'ServiceAccount')
    Token = apps.get_model('authtoken', 'Token')
    for account in Account.objects.all().iterator():
        token = Token.objects.filter(user_id=account.user_id).first()
        if token:
            account.token_digest = hashlib.sha256(token.key.encode()).hexdigest()
            account.save(update_fields=['token_digest'])


def migrate_audit_organizations(apps, schema_editor):
    Event = apps.get_model('collab', 'AuditEvent')
    for event in Event.objects.select_related('project', 'actor').all().iterator():
        event.organization_id = event.project.organization_id if event.project else (event.actor.active_organization_id if event.actor else None)
        event.save(update_fields=['organization'])


def migrate_idempotency_receipts(apps, schema_editor):
    Record = apps.get_model('collab', 'IdempotencyRecord')
    Project = apps.get_model('projects', 'Project')
    Event = apps.get_model('collab', 'AuditEvent')
    for record in Record.objects.filter(endpoint='project-create').select_related('user').iterator():
        body = record.response_body or {}
        project = Project.objects.filter(pk=body.get('id')).first() if body.get('id') else None
        if record.response_body is None:
            event = Event.objects.filter(actor_id=record.user_id, action='project.create', detail__idempotency_key=record.key).first()
            if event is None:
                # Old implementation committed the project and its audit together; neither exists, so retry is safe.
                record.delete()
                continue
            project = Project.objects.filter(pk=event.project_id).first()
            record.response_status = 201 if project else 410
            record.response_body = (
                {'id': project.pk, 'title': project.title, 'is_published': project.is_published}
                if project else {'detail': 'The original project no longer exists.'}
            )
        org_id = project.organization_id if project else record.user.active_organization_id
        endpoint = f'{org_id}:project-create'
        if Record.objects.filter(user_id=record.user_id, key=record.key, endpoint=endpoint).exists():
            record.delete()
            continue
        record.endpoint = endpoint
        record.save(update_fields=['endpoint', 'response_status', 'response_body'])


class Migration(migrations.Migration):
    dependencies = [('collab', '0003_reviews_service_accounts'), ('authtoken', '0004_alter_tokenproxy_options')]

    operations = [
        migrations.AddField(
            model_name='auditevent', name='organization',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='organizations.organization'),
        ),
        migrations.AddIndex(
            model_name='auditevent', index=models.Index(fields=['organization', 'timestamp'], name='collab_audit_org_time_idx'),
        ),
        migrations.RunPython(migrate_audit_organizations, migrations.RunPython.noop),
        migrations.RunPython(migrate_idempotency_receipts, migrations.RunPython.noop),
        migrations.AddField(
            model_name='serviceaccount', name='all_projects',
            field=models.BooleanField(default=False, help_text='Explicit organization-wide project authorization'),
        ),
        migrations.AddField(
            model_name='serviceaccount', name='token_digest',
            field=models.CharField(blank=True, editable=False, max_length=64, null=True, unique=True),
        ),
        migrations.RunPython(migrate_tokens, migrations.RunPython.noop),
        migrations.CreateModel(
            name='WebhookDelivery',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event', models.CharField(max_length=64)),
                ('payload', models.JSONField(default=dict)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('sending', 'Sending'), ('sent', 'Sent'), ('failed', 'Failed')], db_index=True, default='pending', max_length=12)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('available_at', models.DateTimeField(db_index=True)),
                ('lease_until', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.CharField(blank=True, default='', max_length=160)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
                ('subscription', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deliveries', to='collab.webhooksubscription')),
            ],
            options={'indexes': [models.Index(fields=['status', 'available_at'], name='collab_outbox_ready_idx')]},
        ),
    ]
