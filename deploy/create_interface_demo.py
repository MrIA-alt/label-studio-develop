"""Create synthetic import files and an additive UI demonstration campaign.

Run in the application container with its normal Django settings:
  PYTHONPATH=/label-studio/label_studio python /tmp/create_interface_demo.py \
    --owner-email mcarlesso@alteca.fr --output /label-studio/data/interface-demo
Never edits existing projects or resets an existing account's password.
"""

import argparse
import base64
import csv
from datetime import timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import uuid
import zipfile


CONFIG_TEXT = '''<View>
  <Header value="Classer la demande et repérer la personne et la date"/>
  <Text name="text" value="$text"/>
  <Choices name="category" toName="text" choice="single" required="true">
    <Choice value="Technique"/><Choice value="Facturation"/><Choice value="Accès"/>
  </Choices>
  <Labels name="entities" toName="text">
    <Label value="Personne" background="#f5a623"/>
    <Label value="Date" background="#4a90e2"/>
  </Labels>
</View>'''
CONFIG_IMAGE = CONFIG_TEXT.replace('</View>', '''
  <Header value="Encadrer le cachet et la signature sur le document fictif"/>
  <Image name="image" value="$image"/>
  <RectangleLabels name="boxes" toName="image">
    <Label value="Cachet" background="#e85d75"/>
    <Label value="Signature" background="#45b69c"/>
  </RectangleLabels>
</View>''')

NAMES = ['Camille Martin', 'Alex Dupont', 'Noa Bernard', 'Lou Petit', 'Sacha Leroy']
SUBJECTS = [
    ('Technique', 'le logiciel se ferme au lancement'),
    ('Facturation', 'une facture apparaît deux fois dans son espace'),
    ('Accès', 'son compte est bloqué après un changement de mot de passe'),
    ('Technique', 'le téléchargement du document reste bloqué'),
    ('Facturation', 'le montant du devis diffère du montant facturé'),
    ('Accès', 'une invitation à un projet ne donne pas accès aux tâches'),
]
SCENARIOS = {
    19: 'Brouillon personnel Alice', 20: 'Deux brouillons indépendants',
    21: 'Trois soumissions en accord, à examiner',
    22: 'Trois catégories différentes, désaccord à arbitrer',
    23: 'Correction demandée à Alice, commentaire privé',
    24: 'Annotation de Bob rejetée, reprise possible',
    25: 'Trois annotations acceptées',
    26: 'Correction Alice soumise en révision 2, historique conservé',
    27: 'Arbitrage final enregistré, trois originaux conservés',
    28: 'Affectation Alice expirée, autres annotateurs disponibles',
    29: 'Alice a passé la tâche, Bob annulé, Charlie disponible',
    30: 'Deux soumissions seulement : revue encore indisponible',
}


def document(index, person, date):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (900, 540), '#fffdf7')
    draw = ImageDraw.Draw(image)
    try:
        normal = ImageFont.truetype('DejaVuSans.ttf', 24)
        title = ImageFont.truetype('DejaVuSans.ttf', 32)
    except OSError:
        normal = ImageFont.load_default(size=24)
        title = ImageFont.load_default(size=32)
    draw.rounded_rectangle((20, 20, 879, 519), radius=12, outline='#34495e', width=3)
    draw.text((55, 48), 'DOCUMENT FICTIF - TEST ANNOTATION', fill='#19324d', font=title)
    draw.text((55, 115), f'Reference : DEMO-{index:03}', fill='black', font=normal)
    draw.text((55, 165), f'Demandeur : {person}', fill='black', font=normal)
    draw.text((55, 215), f'Date : {date}', fill='black', font=normal)
    draw.text((55, 275), 'Ces donnees sont entierement synthetiques.', fill='#555555', font=normal)
    draw.rectangle((625, 310, 835, 430), outline='#ad3140', width=5)
    draw.text((660, 350), 'CACHEt TEST'.upper(), fill='#ad3140', font=normal)
    draw.text((120, 360), 'Signature fictive', fill='#285078', font=normal)
    draw.line([(120, 465), (165, 415), (205, 460), (260, 405), (310, 455), (385, 425), (440, 460)], fill='#285078', width=5)
    buf = BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


def result(row, choice=None):
    text = row['text']
    return [
        {'id': 'category', 'from_name': 'category', 'to_name': 'text', 'type': 'choices',
         'value': {'choices': [choice or row['expected']]}},
        {'id': 'person', 'from_name': 'entities', 'to_name': 'text', 'type': 'labels',
         'value': {'start': 0, 'end': len(row['person']), 'text': row['person'], 'labels': ['Personne']}},
        {'id': 'date', 'from_name': 'entities', 'to_name': 'text', 'type': 'labels',
         'value': {'start': text.index(row['date']), 'end': text.index(row['date']) + len(row['date']),
                   'text': row['date'], 'labels': ['Date']}},
        *[{'id': label.lower(), 'from_name': 'boxes', 'to_name': 'image', 'type': 'rectanglelabels',
           'original_width': 900, 'original_height': 540, 'image_rotation': 0,
           'value': {'x': x, 'y': y, 'width': width, 'height': height, 'rotation': 0, 'rectanglelabels': [label]}}
          for label, x, y, width, height in [('Cachet', 69.44, 57.41, 23.33, 22.22), ('Signature', 13.33, 73.15, 35.56, 16.67)]],
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner-email', required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    if options.output.exists():
        raise SystemExit('Output already exists; preserving the previous demonstration.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.label_studio')
    import django
    django.setup()
    from django.core.files.base import ContentFile
    from django.db import transaction
    from django.utils import timezone
    from collab import services, reviews
    from collab.models import AnnotationRevision, TaskArbitration, TaskAssignment
    from data_import.models import FileUpload
    from organizations.models import OrganizationMember
    from projects.models import Project
    from rest_framework.test import APIClient
    from tasks.models import Annotation, AnnotationDraft, Task
    from users.models import User

    owner = User.objects.get(email__iexact=options.owner_email)
    if not owner.is_active or not owner.is_organization_admin():
        raise SystemExit('The owner must be an active organization administrator.')
    run = uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(22)
    output = options.output
    output.mkdir(parents=True)
    (output / 'images').mkdir()
    date = timezone.localdate().isoformat()
    rows = []
    for index in range(1, 31):
        expected, subject = SUBJECTS[(index - 1) % len(SUBJECTS)]
        person = NAMES[(index - 1) % len(NAMES)]
        text = f'{person} signale que {subject} le {date}. Dossier de démonstration DEMO-{index:03}.'
        png = document(index, person, date)
        (output / 'images' / f'document-{index:03}.png').write_bytes(png)
        rows.append({'index': index, 'person': person, 'text': text, 'date': date, 'expected': expected,
                     'png': png, 'image': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')})
    (output / '01-configuration-texte.xml').write_text(CONFIG_TEXT, encoding='utf-8')
    (output / '02-configuration-texte-image.xml').write_text(CONFIG_IMAGE, encoding='utf-8')
    (output / '01-taches-texte.json').write_text(json.dumps([
        {'data': {'demo_id': f'DEMO-{r["index"]:03}', 'text': r['text']}} for r in rows
    ], ensure_ascii=False, indent=2), encoding='utf-8')
    (output / '02-taches-texte-image.json').write_text(json.dumps([
        {'data': {'demo_id': f'DEMO-{r["index"]:03}', 'text': r['text'], 'image': r['image']}} for r in rows
    ], ensure_ascii=False), encoding='utf-8')
    with (output / '01-taches-texte.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['demo_id', 'text'])
        writer.writerows((f'DEMO-{r["index"]:03}', r['text']) for r in rows)
    with (output / 'corrige-administrateur.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['demo_id', 'categorie_attendue', 'personne', 'date'])
        writer.writerows((f'DEMO-{r["index"]:03}', r['expected'], r['person'], r['date']) for r in rows)

    clients = {}
    def client(user):
        if user.pk not in clients:
            api = APIClient(HTTP_HOST='localhost:18080')
            api.force_authenticate(user=user)
            clients[user.pk] = api
        return clients[user.pk]

    def api(user, method, path, data=None, expected=200, **headers):
        response = getattr(client(user), method)(path, data=data, format='json', **headers)
        if response.status_code != expected:
            raise RuntimeError(f'{method.upper()} {path}: {response.status_code}; expected {expected}; '
                               f'{getattr(response, "data", {})}')
        return getattr(response, 'data', None)

    people = {}
    with transaction.atomic():
        for name in ('alice', 'bob', 'charlie', 'superviseur'):
            email = f'demo-interface-{run}-{name}@example.test'
            person = User.objects.create_user(email, password, username=email, first_name=name.title(),
                                               active_organization=owner.active_organization)
            OrganizationMember.objects.create(user=person, organization=owner.active_organization,
                role='qa' if name == 'superviseur' else 'labeller')
            people[name] = person
        created = api(owner, 'post', '/api/extensions/v1/projects', {
            'title': f'Démo interface complète — {date} — {run}',
            'description': 'Données entièrement synthétiques pour parcourir les interfaces. DEMO-001 à 018 sont vierges ; DEMO-019 à 030 illustrent les différents états. Kit et guide disponibles dans le dossier demo-interface du dépôt.',
            'expert_instruction': 'Classer la demande (Technique, Facturation ou Accès), sélectionner la personne et la date dans le texte, puis encadrer le cachet et la signature fictifs dans le document.',
            'label_config': CONFIG_IMAGE, 'member_ids': [u.pk for u in people.values()],
            'policy': {'required_annotations': 3, 'blind_mode': True, 'assignment_mode': 'manual',
                'review_mode': 'manual', 'anonymous_review': True, 'allow_predictions': False,
                'max_active_tasks_per_user': 40, 'assignment_ttl_hours': 72},
            'publish': True,
        }, expected=201, HTTP_IDEMPOTENCY_KEY=f'interface-demo-{run}')
        project = Project.objects.get(pk=created['id'])
        sources = []
        for row in rows:
            upload = FileUpload.objects.create(user=owner, project=project,
                file=ContentFile(row['png'], name=f'demo-{run}-{row["index"]:03}.png'))
            sources.append({'data': {'demo_id': f'DEMO-{row["index"]:03}', 'text': row['text'], 'image': upload.url}})
        api(owner, 'post', f'/api/projects/{project.pk}/import', sources, expected=201)
        tasks = list(Task.objects.filter(project=project).order_by('id'))
        assert len(tasks) == 30
        annotators = [people[name] for name in ('alice', 'bob', 'charlie')]
        assigned = api(owner, 'post', f'/api/extensions/v1/projects/{project.pk}/assignments/bulk', {
            'task_ids': [t.pk for t in tasks], 'user_ids': [u.pk for u in annotators],
        }, expected=201)
        assert TaskAssignment.objects.filter(project=project).count() == 90
        for index, names in ((19, ('alice',)), (20, ('alice', 'bob'))):
            for name in names:
                user, task = people[name], tasks[index - 1]
                assignment = TaskAssignment.objects.get(task=task, user=user)
                api(user, 'post', f'/api/extensions/v1/assignments/{assignment.pk}/start', {})
                api(user, 'post', f'/api/tasks/{task.pk}/drafts', {'result': result(rows[index - 1])}, expected=201)
        annotations = {}
        for index in (*range(21, 28), 30):
            for position, name in enumerate(('alice', 'bob', 'charlie')):
                if index == 30 and name == 'charlie':
                    continue
                choice = ['Technique', 'Facturation', 'Accès'][position] if index in (22, 27) else None
                if index == 26 and name == 'alice':
                    choice = 'Accès' if rows[index - 1]['expected'] != 'Accès' else 'Technique'
                task = tasks[index - 1]
                annotations[index, name] = api(people[name], 'post', f'/api/tasks/{task.pk}/annotations/',
                    {'result': result(rows[index - 1], choice), 'lead_time': 12.5}, expected=201)
        qa = people['superviseur']
        def review(index, name, decision, comment=''):
            ann = Annotation.objects.get(pk=annotations[index, name]['id'])
            api(qa, 'post', '/api/extensions/v1/reviews', {
                'annotation': ann.pk, 'revision': ann.revision, 'decision': decision, 'comment': comment,
            }, expected=201)
        review(23, 'alice', 'REQUEST_CORRECTION', 'Vérifier la catégorie puis ajuster le cadre du cachet. Ce retour est réservé à Alice.')
        review(24, 'bob', 'REJECT', 'Reprendre la sélection de la date et la catégorie. Ce retour est réservé à Bob.')
        for name in ('alice', 'bob', 'charlie'):
            review(25, name, 'ACCEPT')
        review(26, 'alice', 'REQUEST_CORRECTION', 'La catégorie choisie ne correspond pas à la demande. Merci de la corriger.')
        original = annotations[26, 'alice']
        api(people['alice'], 'patch', f'/api/annotations/{original["id"]}/', {
            'revision': original['revision'], 'result': result(rows[25]),
        })
        api(qa, 'post', f'/api/extensions/v1/tasks/{tasks[26].pk}/final', {'result': result(rows[26])}, expected=201)
        expired = TaskAssignment.objects.get(task=tasks[27], user=people['alice'])
        expired.expires_at = timezone.now() - timedelta(hours=1)
        expired.save(update_fields=['expires_at'])
        services.transition(expired, TaskAssignment.Status.EXPIRED)
        services.transition(TaskAssignment.objects.get(task=tasks[28], user=people['alice']), TaskAssignment.Status.SKIPPED)
        services.transition(TaskAssignment.objects.get(task=tasks[28], user=people['bob']), TaskAssignment.Status.CANCELLED)
        for task in tasks:
            reviews.refresh_task_agreement(task.pk)
        assert AnnotationDraft.objects.filter(task__project=project).count() == 3
        assert AnnotationRevision.objects.filter(annotation__project=project).count() == 1
        assert TaskArbitration.objects.filter(task__project=project, final_annotation__isnull=False).count() == 1
        assert Annotation.objects.filter(project=project, ground_truth=False).count() == 23
        alice_view = api(people['alice'], 'get', f'/api/tasks/{tasks[21].pk}/')
        assert len(alice_view['annotations']) == 1
        assert api(people['alice'], 'get', f'/api/extensions/v1/my-reviews?task={tasks[22].pk}')['count'] == 1
        assert api(people['bob'], 'get', f'/api/extensions/v1/my-reviews?task={tasks[22].pk}')['count'] == 0
        progress = api(owner, 'get', f'/api/extensions/v1/projects/{project.pk}/progress')

    scenario_rows = [{'demo_id': f'DEMO-{i:03}', 'task_id': task.pk,
                      'scenario': SCENARIOS.get(i, 'Tâche vierge à annoter par trois comptes')}
                     for i, task in enumerate(tasks, 1)]
    (output / 'campagne.json').write_text(json.dumps({
        'project': project.pk, 'url': f'http://localhost:18080/projects/{project.pk}/collaboration',
        'tasks': scenario_rows, 'users': {n: {'email': u.email, 'role': u.get_organization_role()} for n, u in people.items()},
        'progress': progress,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    guide = f'''# Kit de test de l’interface collaborative

Données synthétiques uniquement. Campagne prête : http://localhost:18080/projects/{project.pk}/collaboration
Votre compte {owner.email} conserve son rôle administrateur. Les quatre nouveaux comptes de test sont listés dans campagne.json ; leur mot de passe est dans le fichier privé séparé indiqué à la livraison.

## Créer votre propre projet

1. Dans Projects, cliquer Create et donner un nom au projet.
2. Importer 01-taches-texte.json OU 01-taches-texte.csv (choisir un seul format, sinon les tâches seront doublées).
3. Dans la configuration de labellisation, coller 01-configuration-texte.xml puis enregistrer.
4. Pour tester texte + image, créer un autre projet avec 02-taches-texte-image.json et 02-configuration-texte-image.xml. Les images sont intégrées au JSON : aucun site externe n’est requis.
5. Dans Campaign / Administration, sélectionner les membres de test, régler 3 annotations, Manual, mode aveugle et revue manuelle ; affecter les tâches puis publier le projet.
6. Le fichier corrige-administrateur.csv propose les catégories et entités attendues ; ne pas l’importer comme tâches.

## Parcourir la campagne déjà préparée

Se connecter avec votre compte pour Administration et Supervision. Ouvrir un autre profil de navigateur pour Alice, Bob et Charlie : My tasks contient leurs affectations personnelles. Le superviseur a le rôle QA. Le nom DEMO-xxx est dans les données source et le tableau ci-dessous donne le véritable numéro de tâche.

| Exemple | Tâche | Parcours à essayer |
|---|---|---|
'''
    guide += ''.join(f'| {r["demo_id"]} | #{r["task_id"]} | {r["scenario"]} |\n' for r in scenario_rows)
    guide += '''
## Parcours manuels

- Sur DEMO-001, annoter le même document avec Alice, Bob et Charlie, puis comparer les trois réponses dans Supervision. Chaque annotateur doit retrouver uniquement sa propre réponse.
- Sur DEMO-019/020, reprendre les brouillons. Modifier la catégorie, les entités et les rectangles, puis vérifier la sauvegarde et la soumission.
- Sur DEMO-023, Alice voit une demande de correction. Corriger puis soumettre ; revenir en supervision pour vérifier l’historique et accepter. Bob ne doit pas voir le commentaire destiné à Alice.
- Sur DEMO-022, examiner le désaccord, choisir une référence ou arbitrer un résultat final. DEMO-027 montre déjà un arbitrage conservant les trois originaux.
- Examiner les filtres de statut, la pagination (30 tâches), la progression, l’historique et les exports depuis le compte administrateur.
- Dans Administration, créer/révoquer un compte de service limité à ce projet si vous souhaitez essayer l’API agent. Aucun jeton d’agent n’est précréé dans ce kit.
- Vous pouvez tester les invitations avec une adresse contrôlée, mais la réception réelle attend la validation et la configuration Microsoft 365. Aucun mail n’a été envoyé pour créer cette démo.
- Archiver puis restaurer UNIQUEMENT cette campagne de démonstration pour vérifier le passage en lecture seule.

Les exemples ne changent aucun ancien projet. Ils servent aux parcours texte/image du fork ; ils ne prétendent pas couvrir tous les templates natifs de Label Studio. Les rôles QA et annotateur sont des rôles d’organisation ; utiliser ces comptes uniquement sur cette instance de test. Les fichiers d’import ne contiennent aucune annotation préremplie ; les états avancés sont présents uniquement dans la campagne préparée.
'''
    (output / 'GUIDE.md').write_text(guide, encoding='utf-8')
    private = output.parent / (output.name + '-acces-prive.txt')
    descriptor = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write('COMPTES SYNTHÉTIQUES — INSTANCE LOCALE UNIQUEMENT\nhttp://localhost:18080/user/login/\n\n')
        for name, user in people.items():
            handle.write(f'{name} ({user.get_organization_role()}) : {user.email}\n')
        handle.write('\nMot de passe commun de ces quatre comptes de test : ' + password + '\n')
        handle.write('Ne pas publier ce fichier. Le compte personnel administrateur est inchangé.\n')
    archive = output.parent / (output.name + '.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                bundle.write(path, path.relative_to(output))
    print(json.dumps({'project': project.pk, 'tasks': len(tasks), 'assignments': 90, 'drafts': 3,
        'original_annotations': 23, 'final_annotations': 1, 'revisions': 1,
        'kit': str(output), 'archive': str(archive), 'private_access': str(private)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
