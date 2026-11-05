import pytest
from collab.models import ProjectAnnotationPolicy
from organizations.models import Organization, OrganizationMember
from projects.models import Project
from rest_framework.test import APIClient
from tasks.models import Annotation, Task
from users.models import User

pytestmark = pytest.mark.django_db

CONFIG = '<View><Text name="t" value="$text"/><Choices name="c" toName="t"><Choice value="a"/><Choice value="b"/></Choices></View>'


def make_user(org, email, role):
    user = User.objects.create(email=email, username=email)
    user.set_password('pw')
    user.active_organization = org
    user.save()
    if org.created_by_id != user.pk:
        org.add_user(user, role=role)
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def result(choice):
    return [{'from_name': 'c', 'to_name': 't', 'type': 'choices', 'value': {'choices': [choice]}}]


@pytest.fixture
def env():
    admin = User.objects.create(email='admin@x.io', username='admin@x.io')
    org = Organization.create_organization(created_by=admin, title='Org')
    admin.active_organization = org
    admin.save()
    a = make_user(org, 'a@x.io', OrganizationMember.Role.LABELLER)
    b = make_user(org, 'b@x.io', OrganizationMember.Role.LABELLER)
    qa = make_user(org, 'qa@x.io', OrganizationMember.Role.QA)
    project = Project.objects.create(title='P', organization=org, created_by=admin, label_config=CONFIG)
    task = Task.objects.create(project=project, data={'text': 'hello'})
    return dict(admin=admin, a=a, b=b, qa=qa, project=project, task=task)


def set_policy(project, **kwargs):
    return ProjectAnnotationPolicy.objects.update_or_create(project=project, defaults=kwargs)[0]


def annotate(env, user, choice):
    return Annotation.objects.create(
        task=env['task'], project=env['project'], completed_by=user, result=result(choice)
    )


def test_blind_annotations_list_only_own(env):
    set_policy(env['project'], blind_mode=True)
    ann_a, ann_b = annotate(env, env['a'], 'a'), annotate(env, env['b'], 'b')
    resp = client_for(env['a']).get(f"/api/tasks/{env['task'].id}/annotations/")
    assert resp.status_code == 200
    assert [x['id'] for x in resp.json()] == [ann_a.id]


def test_blind_cannot_read_other_annotation(env):
    set_policy(env['project'], blind_mode=True)
    ann_b = annotate(env, env['b'], 'b')
    resp = client_for(env['a']).get(f'/api/annotations/{ann_b.id}/')
    assert resp.status_code in (403, 404)


def test_blind_task_detail_hides_other_annotations(env):
    set_policy(env['project'], blind_mode=True)
    annotate(env, env['a'], 'a')
    annotate(env, env['b'], 'b')
    resp = client_for(env['a']).get(f"/api/tasks/{env['task'].id}/")
    assert resp.status_code == 200
    body = resp.json()
    assert {x['completed_by'] if isinstance(x['completed_by'], int) else x['completed_by']['id'] for x in body['annotations']} == {
        env['a'].pk
    }


def test_blind_data_manager_hides_aggregates(env):
    set_policy(env['project'], blind_mode=True)
    annotate(env, env['b'], 'b')
    resp = client_for(env['a']).get(f"/api/tasks/?project={env['project'].id}")
    assert resp.status_code == 200
    item = resp.json()['tasks'][0]
    assert item['total_annotations'] == 0
    assert not item['annotators']
    assert not item['annotations_ids']


def test_blind_filter_oracle_is_neutralised(env):
    set_policy(env['project'], blind_mode=True)
    annotate(env, env['b'], 'b')
    flt = '{"filters":{"conjunction":"and","items":[{"filter":"filter:tasks:total_annotations","operator":"greater","type":"Number","value":0}]}}'
    resp = client_for(env['a']).get(f"/api/tasks/?project={env['project'].id}&filters={flt}")
    assert resp.status_code == 200
    assert resp.json()['total'] == 1  # filter ignored: the task is still listed for A


def test_blind_annotators_endpoint(env):
    set_policy(env['project'], blind_mode=True)
    annotate(env, env['b'], 'b')
    resp = client_for(env['a']).get(f"/api/projects/{env['project'].id}/annotators/")
    assert resp.status_code == 200
    assert resp.json() == []


def test_labeller_cannot_force_completed_by(env):
    set_policy(env['project'], blind_mode=True)
    resp = client_for(env['a']).post(
        f"/api/tasks/{env['task'].id}/annotations/",
        {'result': result('a'), 'completed_by': env['b'].pk},
        format='json',
    )
    assert resp.status_code == 201
    assert Annotation.objects.get(pk=resp.json()['id']).completed_by_id == env['a'].pk


def test_labeller_cannot_review(env):
    ann_b = annotate(env, env['b'], 'b')
    resp = client_for(env['a']).post(f'/api/annotation-reviews/', {'annotation': ann_b.id, 'accepted': True}, format='json')
    assert resp.status_code in (403, 404, 405)


def test_qa_and_admin_see_everything(env):
    set_policy(env['project'], blind_mode=True)
    annotate(env, env['a'], 'a')
    annotate(env, env['b'], 'b')
    for user in (env['qa'], env['admin']):
        resp = client_for(user).get(f"/api/tasks/{env['task'].id}/annotations/")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


def test_legacy_project_unchanged(env):
    annotate(env, env['a'], 'a')
    annotate(env, env['b'], 'b')
    resp = client_for(env['a']).get(f"/api/tasks/{env['task'].id}/annotations/")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_manual_assignment_restricts_tasks(env):
    from collab import services

    set_policy(env['project'], assignment_mode='manual')
    other = Task.objects.create(project=env['project'], data={'text': 'other'})
    services.assign_task(env['task'], env['a'], assigned_by=env['admin'])
    resp = client_for(env['a']).get(f"/api/tasks/?project={env['project'].id}")
    assert resp.status_code == 200
    assert [t['id'] for t in resp.json()['tasks']] == [env['task'].id]
    assert client_for(env['a']).get(f'/api/tasks/{other.id}/').status_code in (403, 404)
