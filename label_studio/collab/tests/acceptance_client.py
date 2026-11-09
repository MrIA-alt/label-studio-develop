"""Exercise the running application through session/CSRF and real service-token HTTP authentication.

Run only against an isolated acceptance database:
  COLLAB_ACCEPTANCE_TESTS=1 python -m collab.tests.acceptance_client --base-url http://nginx:8085
The fixture credentials are saved with mode 0600 in BASE_DATA_DIR for browser tests, never printed.
"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import uuid

import requests

CONFIG = '''<View><Header value="Document classification"/><Text name="text" value="$text"/>
<Choices name="category" toName="text" choice="single"><Choice value="a"/><Choice value="b"/><Choice value="c"/></Choices>
<Labels name="entities" toName="text"><Label value="Name"/><Label value="Date"/></Labels>
<Image name="image" value="$image"/><RectangleLabels name="boxes" toName="image"><Label value="Stamp"/><Label value="Signature"/></RectangleLabels></View>'''

TASK_COUNT = 100
DOCUMENT_TEXT = 'Alice Example signed a document on 2026-11-06.'


def result(choice):
    date_start = DOCUMENT_TEXT.index('2026-11-06')
    return [
        {'id': 'category', 'from_name': 'category', 'to_name': 'text', 'type': 'choices', 'value': {'choices': [choice]}},
        {'id': 'person', 'from_name': 'entities', 'to_name': 'text', 'type': 'labels',
         'value': {'start': 0, 'end': 13, 'text': 'Alice Example', 'labels': ['Name']}},
        {'id': 'date', 'from_name': 'entities', 'to_name': 'text', 'type': 'labels',
         'value': {'start': date_start, 'end': date_start + 10, 'text': '2026-11-06', 'labels': ['Date']}},
        {'id': 'stamp', 'from_name': 'boxes', 'to_name': 'image', 'type': 'rectanglelabels',
         'original_width': 640, 'original_height': 400, 'image_rotation': 0,
         'value': {'x': 65.625, 'y': 55, 'width': 25, 'height': 25, 'rotation': 0, 'rectanglelabels': ['Stamp']}},
        {'id': 'signature', 'from_name': 'boxes', 'to_name': 'image', 'type': 'rectanglelabels',
         'original_width': 640, 'original_height': 400, 'image_rotation': 0,
         'value': {'x': 15, 'y': 70, 'width': 35, 'height': 15, 'rotation': 0, 'rectanglelabels': ['Signature']}},
    ]


def document_image():
    """A deterministic, visible document fixture with no external media dependency."""
    from PIL import Image, ImageDraw

    image = Image.new('RGB', (640, 400), 'white')
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((16, 16, 623, 383), outline='black', width=2)
    drawing.text((45, 45), 'Administrative document', fill='black')
    drawing.text((45, 95), DOCUMENT_TEXT, fill='black')
    drawing.text((100, 260), 'Signature', fill='black')
    drawing.line([(100, 320), (150, 285), (190, 315), (240, 280), (300, 315)], fill='navy', width=3)
    drawing.rectangle((420, 220, 580, 320), outline='darkred', width=4)
    drawing.text((465, 260), 'STAMP', fill='darkred')
    output = BytesIO()
    image.save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')


def main():
    if os.environ.get('COLLAB_ACCEPTANCE_TESTS') != '1':
        raise SystemExit('Use an isolated database and explicitly set COLLAB_ACCEPTANCE_TESTS=1.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    options = parser.parse_args()
    base = options.base_url.rstrip('/')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.label_studio')
    import django

    django.setup()
    from django.conf import settings
    from django.core.files.base import ContentFile
    from data_import.models import FileUpload
    from organizations.models import Organization, OrganizationMember
    from projects.models import Project
    from tasks.models import Annotation
    from users.models import User

    run = uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(32)
    users = {}
    for name in ('admin', 'qa', 'alice', 'bob', 'charlie'):
        email = f'collab-{run}-{name}@example.test'
        user = User.objects.create(username=email, email=email, first_name=name.title())
        user.set_password(password)
        user.save()
        users[name] = user
    org = Organization.create_organization(created_by=users['admin'], title=f'Acceptance {run}')
    for name, user in users.items():
        user.active_organization = org
        user.save(update_fields=['active_organization'])
        if name != 'admin':
            org.add_user(user, role=OrganizationMember.Role.QA if name == 'qa' else OrganizationMember.Role.LABELLER)
    checks = []

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def request(session, method, path, *, data=None, expected=(200,), key=None):
        headers = {'Referer': base + '/'}
        csrf = session.cookies.get('csrftoken')
        if csrf:
            headers['X-CSRFToken'] = csrf
        if key:
            headers['Idempotency-Key'] = key
        response = session.request(method, base + path, json=data, headers=headers, timeout=45)
        if response.status_code not in expected:
            raise AssertionError(f'{method} {path}: HTTP {response.status_code}; expected {expected}')
        return response.json() if response.content else None

    def login(name):
        session = requests.Session()
        response = session.get(base + '/user/login/', timeout=45)
        response.raise_for_status()
        csrf = session.cookies.get('csrftoken')
        response = session.post(base + '/user/login/', data={
            'email': users[name].email, 'password': password, 'csrfmiddlewaretoken': csrf,
        }, headers={'Referer': base + '/user/login/'}, timeout=45)
        response.raise_for_status()
        me = request(session, 'GET', '/api/current-user/whoami')
        check(me['id'] == users[name].pk, f'{name}: real session authentication')
        return session

    admin, qa = login('admin'), login('qa')
    account = request(admin, 'POST', '/api/extensions/v1/service-accounts', data={
        'name': f'Acceptance agent {run}', 'project_ids': [], 'all_projects': False,
        'scopes': ['projects:read', 'projects:write', 'policy:read', 'policy:write', 'imports:write',
                   'tasks:read', 'assignments:read', 'assignments:write', 'progress:read', 'results:read'],
    }, expected=(201,), key=f'{run}-agent')
    bot = requests.Session()
    bot.headers['Authorization'] = 'Service ' + account['token']
    payload = {
        'title': f'Collaborative acceptance {run}', 'label_config': CONFIG,
        'member_ids': [users[name].pk for name in ('qa', 'alice', 'bob', 'charlie')],
        'policy': {'required_annotations': 3, 'blind_mode': True, 'assignment_mode': 'manual',
                   'review_mode': 'manual', 'anonymous_review': True, 'max_active_tasks_per_user': TASK_COUNT},
    }
    created = request(bot, 'POST', '/api/extensions/v1/projects', data=payload, expected=(201,), key=f'{run}-project')
    replay = request(bot, 'POST', '/api/extensions/v1/projects', data=payload, expected=(201,), key=f'{run}-project')
    project = created['id']
    check(replay['id'] == project, 'project creation is idempotent')
    policy = request(bot, 'GET', f'/api/extensions/v1/projects/{project}/policy')
    check(policy['required_annotations'] == 3 and policy['blind_mode'], 'agent configures three blind annotations')
    # Storage fixtures are local; every task is still imported through the API.
    # The media reference deliberately has no Task.file_upload FK: JSON imports
    # and their referenced image files are separate uploads in native workflows.
    image_bytes = base64.b64decode(document_image().split(',', 1)[1])
    source_upload = FileUpload.objects.create(
        user=users['admin'], project_id=project, file=ContentFile(image_bytes, name='acceptance-document.png'),
    )
    private_upload = FileUpload.objects.create(
        user=users['bob'], project_id=project,
        file=ContentFile(b'Private annotation attachment', name='private-result.txt'),
    )
    image = source_upload.url
    sources = [{'data': {'text': DOCUMENT_TEXT + f' Task {index}.', 'image': image}} for index in range(TASK_COUNT)]
    request(bot, 'POST', f'/api/projects/{project}/import', data=sources, expected=(201,), key=f'{run}-import')
    request(bot, 'POST', f'/api/projects/{project}/import', data=sources, expected=(201,), key=f'{run}-import')
    project_model = Project.objects.get(pk=project)
    task_ids = list(project_model.tasks.order_by('id').values_list('id', flat=True))
    check(len(task_ids) == TASK_COUNT, 'import retry preserves exactly one hundred source tasks')
    bulk = {'task_ids': task_ids, 'user_ids': [users[name].pk for name in ('alice', 'bob', 'charlie')]}
    assigned = request(bot, 'POST', f'/api/extensions/v1/projects/{project}/assignments/bulk', data=bulk, expected=(201,))
    again = request(bot, 'POST', f'/api/extensions/v1/projects/{project}/assignments/bulk', data=bulk, expected=(200, 201))
    check(assigned['created'] == TASK_COUNT * 3 and again['created'] == 0, 'three hundred assignments without duplicates on retry')
    request(bot, 'POST', f'/api/extensions/v1/projects/{project}/publish', data={}, expected=(200,))
    people = {name: login(name) for name in ('alice', 'bob', 'charlie')}
    anonymous_media = requests.get(base + image, timeout=45, allow_redirects=False)
    check(anonymous_media.status_code in (401, 403), 'source media requires authentication')
    for name, session in people.items():
        media = session.get(base + image, timeout=45)
        check(media.status_code == 200 and media.content == image_bytes, f'{name}: assigned source image is readable')
    private_media = people['alice'].get(base + private_upload.url, timeout=45, allow_redirects=False)
    check(private_media.status_code in (403, 404), 'another annotator result attachment is private')
    first = task_ids[0]
    drafts, own_assignments = {}, {}
    for name, session in people.items():
        page = request(session, 'GET', f'/api/extensions/v1/my-assignments?project={project}')
        check(page['count'] == TASK_COUNT and {row['user'] for row in page['results']} == {users[name].pk}, f'{name}: only own assignments')
        assignment = next(row for row in page['results'] if row['task'] == first)
        own_assignments[name] = assignment
        request(session, 'POST', f"/api/extensions/v1/assignments/{assignment['id']}/start", data={'version': assignment['version']})
        drafts[name] = request(session, 'POST', f'/api/tasks/{first}/drafts', data={'result': result(name[0])}, expected=(201,))
    request(people['alice'], 'GET', f"/api/drafts/{drafts['bob']['id']}/", expected=(403, 404))
    check(True, 'another annotator draft is inaccessible')

    def submit(item):
        name, choice = item
        return name, request(people[name], 'POST', f'/api/tasks/{first}/annotations/', data={
            'result': result(choice), 'draft_id': drafts[name]['id'],
        }, expected=(201,))

    with ThreadPoolExecutor(max_workers=3) as executor:
        annotations = dict(executor.map(submit, [('alice', 'a'), ('bob', 'b'), ('charlie', 'c')]))
    check(len({row['id'] for row in annotations.values()}) == 3, 'three concurrent native submissions preserve independent annotations')
    check(all({item['type'] for item in row['result']} == {'choices', 'labels', 'rectanglelabels'} for row in annotations.values()),
          'native text entities, document classification and image boxes survive submission')
    for name, session in people.items():
        task = request(session, 'GET', f'/api/tasks/{first}/')
        check({row['id'] for row in task['annotations']} == {annotations[name]['id']}, f'{name}: task contains only own annotation')
    request(people['alice'], 'GET', f"/api/annotations/{annotations['bob']['id']}/", expected=(403, 404))
    request(people['alice'], 'GET', f'/api/projects/{project}/export', expected=(403, 404))
    check(True, 'direct foreign annotation and export denied to annotator')
    view = request(qa, 'GET', f'/api/extensions/v1/tasks/{first}/review')
    check(view['review_ready'] and len(view['annotations']) == 3 and view['anonymous'], 'QA compares three anonymous annotations')
    request(qa, 'POST', '/api/extensions/v1/reviews', data={
        'annotation': annotations['alice']['id'], 'revision': annotations['alice']['revision'],
        'decision': 'REJECT', 'comment': 'Please correct the document category.',
    }, expected=(201,))
    alice_feedback = request(people['alice'], 'GET', f'/api/extensions/v1/my-reviews?task={first}')
    bob_feedback = request(people['bob'], 'GET', f'/api/extensions/v1/my-reviews?task={first}')
    check(alice_feedback['count'] == 1 and bob_feedback['count'] == 0, 'rejection feedback is private to its recipient')
    corrected = request(people['alice'], 'PATCH', f"/api/annotations/{annotations['alice']['id']}/", data={
        'result': result('b'), 'revision': annotations['alice']['revision'],
    })
    check(corrected['revision'] == 2, 'personal correction advances revision')
    request(people['alice'], 'PATCH', f"/api/annotations/{annotations['alice']['id']}/", data={
        'result': result('c'), 'revision': 1,
    }, expected=(409,))
    for name in people:
        request(qa, 'POST', '/api/extensions/v1/reviews', data={
            'annotation': annotations[name]['id'], 'revision': corrected['revision'] if name == 'alice' else annotations[name]['revision'],
            'decision': 'ACCEPT', 'comment': '',
        }, expected=(201,))
    request(qa, 'POST', f'/api/extensions/v1/tasks/{first}/final', data={'result': result('b')}, expected=(201,))
    check(Annotation.objects.filter(task_id=first, ground_truth=False, was_cancelled=False).count() == 3,
          'arbitration preserves the three originals')
    view = request(qa, 'GET', f'/api/extensions/v1/tasks/{first}/review')
    check(any(row['revisions'] for row in view['annotations']), 'original rejected version remains in history')
    progress = request(bot, 'GET', f'/api/extensions/v1/projects/{project}/progress')
    check(progress['tasks_by_state'].get('COMPLETE', 0) == 1, 'global completion requires the independent submissions')
    results = request(bot, 'GET', f'/api/extensions/v1/projects/{project}/results')
    check(results['count'] == TASK_COUNT and any(row.get('final_result') for row in results['results']), 'authorized agent retrieves enriched final results')
    request(people['alice'], 'POST', '/api/webhooks/', data={'project': project, 'url': 'https://example.test/hook'}, expected=(403,))
    check(True, 'annotator cannot install an exfiltration webhook')
    low = request(admin, 'POST', '/api/extensions/v1/service-accounts', data={
        'name': f'Progress only {run}', 'project_ids': [project], 'scopes': ['progress:read'],
    }, expected=(201,), key=f'{run}-low-agent')
    reader = requests.Session()
    reader.headers['Authorization'] = 'Service ' + low['token']
    request(reader, 'GET', f'/api/extensions/v1/projects/{project}/progress')
    request(reader, 'GET', f'/api/extensions/v1/tasks/{first}/review', expected=(403,))
    request(reader, 'GET', f'/api/extensions/v1/projects/{project}/results', expected=(403,))
    request(admin, 'DELETE', f"/api/extensions/v1/service-accounts/{low['id']}", expected=(204,))
    request(reader, 'GET', f'/api/extensions/v1/projects/{project}/progress', expected=(401, 403))
    check(True, 'real service tokens enforce scopes and immediate revocation')
    request(qa, 'POST', f'/api/extensions/v1/projects/{project}/archive', data={}, expected=(403,))
    archived = request(admin, 'POST', f'/api/extensions/v1/projects/{project}/archive', data={})
    check(archived['is_archived'] is True, 'administrator archives the campaign')
    request(bot, 'GET', f'/api/extensions/v1/projects/{project}/results')
    next_assignment = request(people['alice'], 'GET', f'/api/extensions/v1/my-assignments?project={project}&status=ASSIGNED')['results'][0]
    request(people['alice'], 'POST', f"/api/extensions/v1/assignments/{next_assignment['id']}/start", data={}, expected=(403,))
    restored = request(admin, 'POST', f'/api/extensions/v1/projects/{project}/restore', data={})
    check(restored['is_archived'] is False, 'restoration resumes work without losing results')
    fixture = {'project': project, 'organization': org.pk, 'password': password, 'task_ids': task_ids,
               'media': {'url': source_upload.url, 'file': source_upload.file.name},
               'annotation_ids': {name: row['id'] for name, row in annotations.items()}, 'users': {
        name: {'id': user.pk, 'email': user.email} for name, user in users.items()
    }}
    path = Path(settings.BASE_DATA_DIR) / 'collab-acceptance-credentials.json'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, 'fchmod'):
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(fixture, output)
    print(json.dumps({'run': run, 'project': project, 'checks_passed': len(checks), 'checks': checks}, indent=2))


if __name__ == '__main__':
    main()
