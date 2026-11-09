"""TEST-008: migrate an existing RBAC/QA installation without losing its data.

The schema is always restored before pytest flushes this isolated test database.
This test runs against isolated SQLite and PostgreSQL test databases.
"""
import hashlib
import json

import pytest
from core.current_request import CurrentContext
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


def applied_state_apps():
    loader = MigrationExecutor(connection).loader
    applied = loader.applied_migrations
    # Include independent apps at their actually applied versions as well as the
    # rolled-back tasks/collab branch (important for non-null user columns).
    leaves = [
        key for key, node in loader.graph.node_map.items()
        if key in applied and not any(child.key in applied for child in node.children)
    ]
    return loader.project_state(leaves).apps


@pytest.mark.django_db(transaction=True)
def test_existing_projects_annotations_rbac_reviews_and_exports_survive_migration():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    historical = [
        ('collab', None),
        ('tasks', '0061_annotation_review_fields'),
        ('webhooks', '0004_auto_20221221_1101'),
    ]
    CurrentContext.clear()
    try:
        executor.migrate(historical)
        apps = applied_state_apps()
        OldUser = apps.get_model('users', 'User')
        OldOrg = apps.get_model('organizations', 'Organization')
        OldMember = apps.get_model('organizations', 'OrganizationMember')
        OldProject = apps.get_model('projects', 'Project')
        OldProjectMember = apps.get_model('projects', 'ProjectMember')
        OldSummary = apps.get_model('projects', 'ProjectSummary')
        OldTask = apps.get_model('tasks', 'Task')
        OldAnnotation = apps.get_model('tasks', 'Annotation')
        OldDraft = apps.get_model('tasks', 'AnnotationDraft')
        OldWebhook = apps.get_model('webhooks', 'Webhook')
        OldToken = apps.get_model('authtoken', 'Token')

        admin = OldUser.objects.create(username='migration-admin', email='migration-admin@x.io', password='!')
        author = OldUser.objects.create(username='migration-author', email='migration-author@x.io', password='!')
        qa = OldUser.objects.create(username='migration-qa', email='migration-qa@x.io', password='!')
        # Historical model creation bypasses the runtime signal that provisions
        # the owner's native token used by the standard export converter.
        OldToken.objects.create(user_id=admin.pk, key='migration-owner-token'.ljust(40, '0'))
        org = OldOrg.objects.create(title='Existing organisation', created_by_id=admin.pk)
        for user, role in ((admin, 'admin'), (author, 'labeller'), (qa, 'qa')):
            OldMember.objects.create(organization_id=org.pk, user_id=user.pk, role=role)
            OldUser.objects.filter(pk=user.pk).update(active_organization_id=org.pk)
        config = '<View><Text name="text" value="$text"/><Choices name="label" toName="text"><Choice value="existing"/></Choices></View>'
        project = OldProject.objects.create(
            title='Existing campaign', organization_id=org.pk, created_by_id=admin.pk,
            label_config=config, maximum_annotations=2,
        )
        for user in (author, qa):
            OldProjectMember.objects.create(project_id=project.pk, user_id=user.pk, enabled=True)
        OldSummary.objects.create(project_id=project.pk)
        source = {'text': 'Original document', 'image': 'https://example.test/source.png'}
        task = OldTask.objects.create(project_id=project.pk, data=source, overlap=2)
        original = [{'from_name': 'label', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['existing']}}]
        annotation = OldAnnotation.objects.create(
            task_id=task.pk, project_id=project.pk, completed_by_id=author.pk, result=original,
            review_status='rejected', review_comment='Original QA feedback', reviewed_by_id=qa.pk,
            reviewed_at=timezone.now(),
        )
        OldAnnotation.objects.create(
            task_id=task.pk, project_id=project.pk, completed_by_id=admin.pk, result=original,
        )
        draft = OldDraft.objects.create(task_id=task.pk, user_id=author.pk, result=original)
        hook = OldWebhook.objects.create(
            organization_id=org.pk, project_id=project.pk, url='https://example.test/legacy-webhook',
        )

        MigrationExecutor(connection).migrate(latest)

        from collab.models import ProjectAnnotationPolicy, TaskAssignment
        from organizations.models import OrganizationMember
        from projects.models import Project
        from rest_framework.test import APIClient
        from tasks.models import Annotation, AnnotationDraft, Task
        from users.models import User
        from webhooks.models import Webhook

        migrated = Annotation.objects.get(pk=annotation.pk)
        assert migrated.result == original
        assert migrated.completed_by_id == author.pk
        assert migrated.review_status == 'rejected'
        assert migrated.review_comment == 'Original QA feedback'
        assert migrated.reviewed_by_id == qa.pk
        assert migrated.revision == 1
        assert migrated.task_assignment_id is None
        assert AnnotationDraft.objects.get(pk=draft.pk).result == original
        assert Task.objects.get(pk=task.pk).data == source
        assert Project.objects.get(pk=project.pk).label_config == config
        assert not ProjectAnnotationPolicy.objects.filter(project_id=project.pk).exists()
        assert not TaskAssignment.objects.filter(project_id=project.pk).exists()
        assert Webhook.objects.get(pk=hook.pk).approved_by_id is None
        assert set(OrganizationMember.objects.filter(organization_id=org.pk).values_list('role', flat=True)) == {
            'admin', 'qa', 'labeller'
        }

        client = APIClient()
        client.force_authenticate(user=User.objects.get(pk=author.pk))
        assert client.get(f'/api/projects/{project.pk}/').status_code == 200
        assert client.post('/api/projects/', {'title': 'Forbidden'}, format='json').status_code == 403
        assert client.get(f'/api/annotations/{annotation.pk}/').json()['result'] == original

        client.force_authenticate(user=User.objects.get(pk=qa.pk))
        reviewed = client.post(f'/api/annotations/{annotation.pk}/review', {'status': 'accepted'}, format='json')
        assert reviewed.status_code == 200
        migrated.refresh_from_db()
        assert migrated.review_status == 'accepted'
        assert migrated.result == original

        client.force_authenticate(user=User.objects.get(pk=admin.pk))
        response = client.get(f'/api/projects/{project.pk}/export', {'exportType': 'JSON', 'download_all_tasks': 'true'})
        assert response.status_code == 200, getattr(response, 'data', None)
        exported = json.loads(b''.join(response.streaming_content))
        response.close()
        assert exported[0]['data'] == source
        assert exported[0]['annotations'][0]['result'] == original
    finally:
        CurrentContext.clear()
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_existing_agent_tokens_audit_and_retry_receipts_survive_upgrade():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    CurrentContext.clear()
    try:
        executor.migrate([('collab', '0003_reviews_service_accounts')])
        apps = applied_state_apps()
        OldUser = apps.get_model('users', 'User')
        OldOrg = apps.get_model('organizations', 'Organization')
        OldMember = apps.get_model('organizations', 'OrganizationMember')
        OldProject = apps.get_model('projects', 'Project')
        OldAccount = apps.get_model('collab', 'ServiceAccount')
        OldToken = apps.get_model('authtoken', 'Token')
        OldAudit = apps.get_model('collab', 'AuditEvent')
        OldReceipt = apps.get_model('collab', 'IdempotencyRecord')
        OldHook = apps.get_model('collab', 'WebhookSubscription')
        admin = OldUser.objects.create(username='upgrade-owner', email='upgrade-owner@x.io', password='!')
        agent = OldUser.objects.create(username='upgrade-agent', email='upgrade-agent@x.io', password='!')
        org = OldOrg.objects.create(title='Existing agents', created_by_id=admin.pk)
        for user, role in ((admin, 'admin'), (agent, 'qa')):
            OldMember.objects.create(organization_id=org.pk, user_id=user.pk, role=role)
            OldUser.objects.filter(pk=user.pk).update(active_organization_id=org.pk)
        project = OldProject.objects.create(title='Existing generated project', organization_id=org.pk, created_by_id=agent.pk)
        account = OldAccount.objects.create(user_id=agent.pk, name='Existing agent', scopes=['projects:write', 'native'])
        account.projects.add(project)
        credential = 'synthetic-migration-agent-token'.ljust(40, '0')
        OldToken.objects.create(user_id=agent.pk, key=credential)
        event = OldAudit.objects.create(
            actor_id=agent.pk, actor_type='service', project_id=project.pk,
            action='project.create', detail={'idempotency_key': 'unfinished-with-project'},
        )
        OldReceipt.objects.create(
            user_id=agent.pk, endpoint='project-create', key='finished', request_hash='a' * 64,
            response_status=201, response_body={'id': project.pk, 'title': project.title, 'is_published': False},
        )
        OldReceipt.objects.create(user_id=agent.pk, endpoint='project-create', key='unfinished-with-project', request_hash='b' * 64)
        OldReceipt.objects.create(user_id=agent.pk, endpoint='project-create', key='unfinished-without-project', request_hash='c' * 64)
        hook = OldHook.objects.create(
            project_id=project.pk, created_by_id=admin.pk, url='https://example.test/existing-hook',
            secret='synthetic-migration-hook-secret', events=['project.published'],
        )

        MigrationExecutor(connection).migrate(latest)

        from collab.models import AuditEvent, IdempotencyRecord, ServiceAccount, WebhookDelivery, WebhookSubscription

        migrated = ServiceAccount.objects.get(pk=account.pk)
        assert migrated.token_digest == hashlib.sha256(credential.encode()).hexdigest()
        assert migrated.scopes == ['projects:write', 'native']
        assert not migrated.all_projects
        assert list(migrated.projects.values_list('pk', flat=True)) == [project.pk]
        assert AuditEvent.objects.get(pk=event.pk).organization_id == org.pk
        receipts = IdempotencyRecord.objects.filter(user_id=agent.pk)
        assert receipts.count() == 2
        for receipt in receipts:
            assert receipt.endpoint == f'{org.pk}:project-create'
            assert receipt.response_status == 201 and receipt.response_body['id'] == project.pk
        assert not receipts.filter(key='unfinished-without-project').exists()
        migrated_hook = WebhookSubscription.objects.get(pk=hook.pk)
        assert migrated_hook.secret == 'synthetic-migration-hook-secret'
        assert migrated_hook.events == ['project.published'] and migrated_hook.active
        assert not WebhookDelivery.objects.exists()
    finally:
        CurrentContext.clear()
        MigrationExecutor(connection).migrate(latest)
