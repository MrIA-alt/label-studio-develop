import json

import pytest
from collab import consensus, webhooks
from collab.models import (
    AnnotationReview,
    AnnotationRevision,
    AuditEvent,
    ServiceAccount,
    TaskAssignment,
    WebhookSubscription,
)
from collab.tests.test_blind_mode import CONFIG, annotate, client_for, env, result, set_policy  # noqa: F401
from projects.models import Project
from tasks.models import Annotation, Task

pytestmark = pytest.mark.django_db

EXT = '/api/extensions/v1'


def three_annotators(env):
    from collab.tests.test_blind_mode import make_user
    from organizations.models import OrganizationMember

    c = make_user(env['project'].organization, 'c@x.io', OrganizationMember.Role.LABELLER)
    env['c'] = c
    set_policy(env['project'], blind_mode=True, required_annotations=3, review_mode='manual')
    for u in (env['a'], env['b'], c):
        TaskAssignment.objects.create(task=env['task'], project=env['project'], user=u)
    return [annotate(env, u, ch) for u, ch in ((env['a'], 'a'), (env['b'], 'b'), (c, 'a'))]


def submitted(env):
    TaskAssignment.objects.update(status=TaskAssignment.Status.SUBMITTED)


def test_review_flow_reject_visible_only_to_author_and_correction_history(env):
    ann_a, ann_b, ann_c = three_annotators(env)
    submitted(env)
    qa = client_for(env['qa'])

    view = qa.get(f"{EXT}/tasks/{env['task'].id}/review").json()
    assert len(view['annotations']) == 3
    assert view['agreement']['disagreement'] is True

    assert qa.post(f'{EXT}/reviews', {'annotation': ann_a.id, 'revision': ann_a.revision, 'decision': 'ACCEPT'}, format='json').status_code == 201
    no_comment = qa.post(f'{EXT}/reviews', {'annotation': ann_b.id, 'revision': ann_b.revision, 'decision': 'REJECT'}, format='json')
    assert no_comment.status_code == 400
    ok = qa.post(
        f'{EXT}/reviews', {'annotation': ann_b.id, 'revision': ann_b.revision, 'decision': 'REJECT', 'comment': 'wrong class'}, format='json'
    )
    assert ok.status_code == 201

    # only the author sees the comment; the reviewed content of others stays hidden
    mine = client_for(env['b']).get(f'{EXT}/my-reviews').json()
    assert [r['comment'] for r in mine['results']] == ['wrong class']
    assert 'reviewer' not in mine['results'][0]
    assert client_for(env['c']).get(f'{EXT}/my-reviews').json()['count'] == 0
    assert 'wrong class' not in json.dumps(client_for(env['c']).get(f"/api/tasks/{env['task'].id}/annotations/").json())

    # author is locked while submitted, free to correct once rejected, others untouched
    states = {a.user_id: a.status for a in TaskAssignment.objects.all()}
    assert states[env['b'].pk] == TaskAssignment.Status.REWORK_REQUIRED
    assert states[env['a'].pk] == TaskAssignment.Status.CLOSED
    locked = client_for(env['a']).patch(f'/api/annotations/{ann_a.id}/', {'result': result('b'), 'revision': ann_a.revision}, format='json')
    assert locked.status_code == 403

    fixed = client_for(env['b']).patch(f'/api/annotations/{ann_b.id}/', {'result': result('a'), 'revision': ann_b.revision}, format='json')
    assert fixed.status_code == 200
    assert AnnotationRevision.objects.filter(annotation=ann_b).count() == 1
    assert TaskAssignment.objects.get(user=env['b']).status == TaskAssignment.Status.SUBMITTED
    ann_a.refresh_from_db()
    assert ann_a.review_status == Annotation.REVIEW_ACCEPTED
    assert AnnotationReview.objects.filter(annotation=ann_b).count() == 1  # history is append-only


def test_labeller_cannot_review(env):
    ann_a, _, _ = three_annotators(env)
    resp = client_for(env['a']).post(f'{EXT}/reviews', {'annotation': ann_a.id, 'decision': 'ACCEPT'}, format='json')
    assert resp.status_code == 403


def test_anonymous_review_and_final_annotation(env):
    three_annotators(env)
    set_policy(env['project'], anonymous_review=True)
    qa = client_for(env['qa'])
    view = qa.get(f"{EXT}/tasks/{env['task'].id}/review").json()
    assert {a['annotator'] for a in view['annotations']} == {'Annotateur A', 'Annotateur B', 'Annotateur C'}
    assert 'annotator_alias' not in view['annotations'][0]

    resp = qa.post(f"{EXT}/tasks/{env['task'].id}/final", {'result': result('a')}, format='json')
    assert resp.status_code == 201
    final = Annotation.objects.get(pk=resp.json()['final_annotation'])
    assert final.ground_truth is True
    again = qa.post(f"{EXT}/tasks/{env['task'].id}/final", {'result': result('b')}, format='json')
    assert again.json()['final_annotation'] == final.pk
    queue = qa.get(f"{EXT}/projects/{env['project'].id}/review-queue?status=all").json()
    assert queue['results'][0]['final_annotation'] == final.pk


def test_consensus_metrics():
    box = lambda x, y, w, h: [  # noqa: E731
        {'from_name': 'r', 'to_name': 'i', 'type': 'rectanglelabels',
         'value': {'x': x, 'y': y, 'width': w, 'height': h, 'rectanglelabels': ['cat']}}
    ]
    same = consensus.compute_agreement({'A': box(0, 0, 10, 10), 'B': box(0, 0, 10, 10)})
    assert same['score'] == 1.0
    half = consensus.compute_agreement({'A': box(0, 0, 10, 10), 'B': box(5, 0, 10, 10)})
    assert 0.3 < half['score'] < 0.4  # IoU = 50 / 150
    diff = consensus.compute_agreement({'A': result('a'), 'B': result('b')})
    assert diff['score'] == 0.0


def test_service_account_scopes_project_restriction_and_revocation(env):
    admin = client_for(env['admin'])
    resp = admin.post(f'{EXT}/service-accounts', {'name': 'bot', 'scopes': ['progress:read'], 'project_ids': [env['project'].pk]}, format='json')
    assert resp.status_code == 201
    assert resp.json()['token']
    svc = ServiceAccount.objects.get(pk=resp.json()['id'])
    bot = client_for(svc.user)
    pid = env['project'].id

    assert bot.get(f'{EXT}/projects/{pid}/progress').status_code == 200
    assert bot.get(f'{EXT}/projects/{pid}/results').status_code == 403  # missing scope
    assert bot.get('/api/projects/').status_code == 403  # no native scope
    assert bot.post(f'{EXT}/service-accounts', {'name': 'x', 'scopes': ['native']}, format='json').status_code == 403

    other = Project.objects.create(
        title='Other', organization=env['project'].organization, created_by=env['admin'], label_config=CONFIG
    )
    svc.projects.set([env['project']])
    assert bot.get(f'{EXT}/projects/{other.id}/progress').status_code in (403, 404)

    assert admin.delete(f'{EXT}/service-accounts/{svc.pk}').status_code == 204
    from users.models import User

    assert client_for(User.objects.get(pk=svc.user_id)).get(f'{EXT}/projects/{pid}/progress').status_code in (401, 403)
    actions = set(AuditEvent.objects.values_list('action', flat=True))
    assert {'service_account.create', 'service_account.revoke'} <= actions
    assert admin.get(f'{EXT}/audit').status_code == 200
    assert client_for(env['qa']).get(f'{EXT}/audit').status_code == 403


def test_agent_end_to_end_project_creation_import_progress_results(env):
    admin = client_for(env['admin'])
    scopes = ['projects:write', 'policy:write', 'assignments:read', 'assignments:write', 'progress:read',
              'results:read', 'projects:read', 'policy:read', 'imports:write']
    created = admin.post(f'{EXT}/service-accounts', {'name': 'agent', 'scopes': scopes}, format='json').json()
    bot = client_for(ServiceAccount.objects.get(pk=created['id']).user)

    payload = {
        'title': 'Agent project',
        'label_config': CONFIG,
        'policy': {'required_annotations': 3, 'blind_mode': True, 'assignment_mode': 'manual'},
        'member_ids': [env['a'].pk, env['b'].pk],
        'publish': True,
    }
    assert bot.post(f'{EXT}/projects', payload, format='json').status_code == 400  # missing Idempotency-Key
    first = bot.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='k1')
    assert first.status_code == 201
    replay = bot.post(f'{EXT}/projects', payload, format='json', HTTP_IDEMPOTENCY_KEY='k1')
    assert replay.status_code == 201 and replay.json() == first.json()
    assert Project.objects.filter(title='Agent project').count() == 1
    conflict = bot.post(f'{EXT}/projects', {**payload, 'title': 'Different'}, format='json', HTTP_IDEMPOTENCY_KEY='k1')
    assert conflict.status_code == 409

    pid = first.json()['id']
    project = Project.objects.get(pk=pid)
    assert project.is_published
    assert project.annotation_policy.blind_mode is True

    imp = bot.post(f'/api/projects/{pid}/import', [{'text': f't{i}'} for i in range(10)], format='json', HTTP_IDEMPOTENCY_KEY='import-agent-e2e')
    assert imp.status_code == 201, imp.content
    assert Task.objects.filter(project=project).count() == 10

    from collab.tests.test_blind_mode import make_user
    from organizations.models import OrganizationMember

    c = make_user(project.organization, 'c@x.io', OrganizationMember.Role.LABELLER)
    from projects.models import ProjectMember

    ProjectMember.objects.create(project=project, user=c)
    task_ids = list(Task.objects.filter(project=project).values_list('id', flat=True))
    bulk = bot.post(
        f'{EXT}/projects/{pid}/assignments/bulk',
        {'task_ids': task_ids, 'user_ids': [env['a'].pk, env['b'].pk, c.pk]},
        format='json',
    )
    assert bulk.status_code == 201
    assert bulk.json()['created'] == 30

    progress = bot.get(f'{EXT}/projects/{pid}/progress')
    assert progress.status_code == 200
    assert progress.json()['annotations']['total'] == 0
    results = bot.get(f'{EXT}/projects/{pid}/results?only=accepted')
    assert results.status_code == 200
    assert results.json()['count'] == 0


def test_webhook_signature_ssrf_and_failure_audit(env, settings, monkeypatch):
    admin = client_for(env['admin'])
    pid = env['project'].id
    bad = admin.post(
        f'{EXT}/projects/{pid}/webhooks', {'url': 'http://127.0.0.1/hook', 'events': ['review.decided']}, format='json'
    )
    assert bad.status_code == 400

    settings.COLLAB_WEBHOOK_ALLOW_PRIVATE = True
    monkeypatch.setattr(webhooks.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('127.0.0.1', 80))])
    good = admin.post(
        f'{EXT}/projects/{pid}/webhooks',
        {'url': 'http://hooks.example.test/in', 'events': ['review.decided']},
        format='json',
    )
    assert good.status_code == 201
    secret = good.json()['secret']
    sub = WebhookSubscription.objects.get(pk=good.json()['id'])
    assert 'secret' not in admin.get(f'{EXT}/projects/{pid}/webhooks').json()['results'][0]

    posted = []
    def successful_post(url, body, headers):
        posted.append((body, headers))
        return 200
    monkeypatch.setattr(webhooks, '_post_once', successful_post)
    assert webhooks.deliver(sub, 'review.decided', {'task': 1}) is True
    body, headers = posted[0]
    ts = headers['X-Collab-Timestamp']
    assert headers['X-Collab-Signature'] == 'sha256=' + webhooks.sign(secret, ts, body)

    monkeypatch.setattr(webhooks, '_post_once', lambda *_: 500)
    monkeypatch.setattr(webhooks.time, 'sleep', lambda *_: None)
    assert webhooks.deliver(sub, 'review.decided', {'task': 1}) is False
    assert AuditEvent.objects.filter(action='webhook.delivery_failed').exists()


def test_throttle(env, settings):
    from collab.base import ExtensionThrottle
    from django.core.cache import cache

    cache.clear()
    settings.COLLAB_API_THROTTLE = '2/min'
    pid = env['project'].id
    client = client_for(env['admin'])
    codes = [client.get(f'{EXT}/projects/{pid}/progress').status_code for _ in range(3)]
    assert codes[-1] == 429
    assert ExtensionThrottle.scope == 'collab_extension'
    cache.clear()
