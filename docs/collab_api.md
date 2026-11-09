# API collaborative v1

Le contrat OpenAPI est disponible dans `/docs/api/schema/`, avec Swagger et ReDoc dans `/docs/api/schema/swagger-ui/` et `/docs/api/schema/redoc/`. Les routes d’extension commencent par `/api/extensions/v1`, sans slash final.

## Comptes de service

Un administrateur humain crée un compte avec `POST /api/extensions/v1/service-accounts` :

```json
{
  "name": "Agent de campagne",
  "scopes": ["projects:write", "policy:read", "policy:write", "imports:write", "assignments:read", "assignments:write", "progress:read", "results:read"],
  "project_ids": [],
  "all_projects": false
}
```

La réponse renvoie le jeton une seule fois. Le serveur conserve son empreinte SHA-256. Utiliser l’en-tête `Authorization: Service <token>`. Les préfixes `Bearer` et `Token` sont également acceptés pour ces jetons. Cette authentification dédiée continue de fonctionner lorsque les jetons historiques Label Studio sont désactivés.

Le compte reçoit un rôle technique QA, sans autorité administrative implicite : les scopes sont obligatoires pour chaque opération. Il ne peut ni administrer d’autres comptes de service ni consulter le journal d’audit. Une liste vide de projets signifie **aucun projet existant autorisé** ; `all_projects: true` donne explicitement accès aux projets de l’organisation active. Un agent autorisé à créer un projet reçoit automatiquement l’accès à ce projet.

`GET /service-accounts/{id}` permet d’inspecter un compte. `PATCH` accepte `name`, `scopes`, `project_ids`, `all_projects`, `active` et `rotate_token`. La rotation ou la réactivation d’un compte révoqué renvoie un nouveau jeton ; l’ancien cesse de fonctionner. `DELETE /service-accounts/{id}` révoque immédiatement le jeton et désactive l’identité.

| Scope | Actions autorisées |
|---|---|
| `projects:read` | Lecture des projets natifs autorisés |
| `projects:write` | Création, configuration et publication de projets ; la politique explicite exige aussi `policy:write` |
| `projects:delete` | Suppression de projets autorisés |
| `policy:read`, `policy:write` | Lecture ou modification de la politique collaborative |
| `imports:write` | Import de tâches dans les projets autorisés |
| `tasks:read` | Données sources et propres résultats du compte, sans résultats d’autrui |
| `tasks:write` | Modification des données sources des tâches autorisées |
| `tasks:delete` | Suppression de tâches/fichiers autorisés |
| `annotations:write` | Création et correction de ses annotations |
| `annotations:delete` | Suppression d’annotations autorisée par le workflow |
| `predictions:delete` | Suppression explicite de prédictions ; la suppression d'une version exige aussi `projects:write` |
| `assignments:read`, `assignments:write` | Consultation ou distribution/transitions des affectations |
| `reviews:read`, `reviews:write` | Lecture des comparaisons ou décisions de revue |
| `progress:read` | Progression et file de revue résumée, sans contenu des annotations |
| `results:read` | Résultats, historiques et exports autorisés |
| `webhooks:write` | Abonnements, état des livraisons et nouvelles tentatives |

Le scope historique `native` est accepté pour faciliter la reconfiguration mais ne donne aucun droit. Les routes natives sont soumises aux mêmes restrictions de projet et à des scopes précis. Une route native non explicitement prise en charge est refusée aux comptes de service.

L'édition d'une source conserve les écritures sur `data` et `meta`. Les états de complétude, compteurs d'annotations/prédictions/commentaires, auteurs de commentaires, identifiant interne et métadonnées de mise à jour sont gérés par le serveur. Une valeur `overlap` fournie pour une tâche collaborative est ignorée : le nombre requis provient de la politique du projet. Les références `file_upload` et `file_upload_id` doivent appartenir au projet cible, y compris lors d'un import en lot.

Les routes natives destructrices sont réservées aux administrateurs humains ou aux comptes de service disposant de toutes les permissions ci-dessous, dans les projets autorisés. Elles sont refusées sur un projet archivé.

| Route | Scopes cumulés pour un compte de service |
|---|---|
| `POST /api/projects/{id}/reimport` | `imports:write`, `tasks:delete`, `annotations:delete` ; cette opération remplace les tâches des fichiers sélectionnés et supprime leurs annotations |
| `DELETE /api/projects/{id}/file-uploads` | `projects:write`, `tasks:delete` |
| `DELETE /api/projects/{id}/model-versions/` | `projects:write`, `predictions:delete` |

Un ancien agent configuré seulement pour importer ou modifier un projet reçoit désormais 403 sur ces suppressions. Le nouveau scope `predictions:delete` doit être accordé explicitement. Aucun changement de structure de base n'est nécessaire pour ces scopes.

Les scopes se cumulent lorsqu'une requête couvre plusieurs actions : créer un projet avec un objet `policy`, ou fournir explicitement `maximum_annotations` / `overlap_cohort_percentage` à la création native, exige `projects:write` **et** `policy:write`. Modifier ces deux champs sur un projet collaboratif existant exige également les deux scopes. Les projets collaboratifs exigent `overlap_cohort_percentage: 100` pour appliquer le nombre requis à chaque tâche. Omettre la politique permet de créer un nouveau projet avec les valeurs confidentielles par défaut grâce à `projects:write` seul. Les paramètres de session de l'organisation restent administrés par un administrateur humain ; les scopes de projet n'autorisent pas leur modification.

## Créer et exploiter une campagne

1. `POST /api/extensions/v1/projects`, avec `Idempotency-Key: campaign-001`, crée le projet :

Les nouveaux projets créés par API utilisent par défaut deux auteurs, le mode aveugle, l’affectation manuelle et la revue manuelle. Une politique partielle conserve ces valeurs pour les champs omis. Un administrateur peut les modifier explicitement. Les projets historiques sans politique conservent leur fonctionnement antérieur.

```json
{
  "title": "Documents administratifs",
  "label_config": "<View><Text name=\"text\" value=\"$text\"/><Choices name=\"class\" toName=\"text\"><Choice value=\"Courrier\"/><Choice value=\"Facture\"/></Choices></View>",
  "description": "Campagne de classification",
  "expert_instruction": "Lire le document puis choisir sa catégorie.",
  "member_ids": [12, 13, 14],
  "policy": {
    "required_annotations": 3,
    "blind_mode": true,
    "assignment_mode": "hybrid",
    "review_mode": "manual",
    "max_active_tasks_per_user": 20,
    "assignment_ttl_hours": 48,
    "anonymous_review": true,
    "agreement_threshold": 0.8
  },
  "publish": false
}
```

2. `POST /api/projects/{id}/import`, avec `Idempotency-Key: campaign-001-import-001`, réutilise l’import natif Label Studio. Envoyer par exemple `[{"text":"Document A"},{"text":"Document B"}]`. Le jeton doit avoir `imports:write`.
3. `POST /api/extensions/v1/projects/{id}/assignments/distribute` distribue les tâches en mode automatique/hybride. Le corps optionnel `{"limit":1000}` limite le nombre de tâches traitées.
4. L’affectation manuelle utilise `POST /projects/{id}/assignments/bulk` avec `task_ids`, `user_ids` et éventuellement `expires_at`. Les utilisateurs doivent être actifs et autorisés dans le projet ; les plafonds de charge s’appliquent à tous les modes.
5. `POST /projects/{id}/publish` publie le projet.
6. `GET /projects/{id}/progress` expose les comptes par état, la charge et les désaccords. `agreement_pending_tasks` identifie les tâches historiques dont le cache d’accord attend encore la maintenance.
7. `GET /projects/{id}/results?only=accepted` récupère les résultats acceptés ; `only=final` renvoie les résultats arbitrés. Le nombre total et la pagination portent sur les tâches réellement éligibles au filtre.

Les imports de comptes de service exigent une clé d’idempotence. Pour un compte humain, la clé d’import est optionnelle. Une même clé et le même contenu renvoient la réponse précédente (`Idempotent-Replay: true`), sans nouvel import ni nouveau projet. Une clé réutilisée avec un autre contenu renvoie 409. La mutation et son reçu sont enregistrés dans une seule transaction ; une interruption avant validation de la transaction laisse la requête rejouable. Les clés sont isolées par identité, organisation et opération. Conserver les clés côté agent avant la première tentative et les réutiliser après une erreur réseau.

La précondition `If-Match` fait partie de ce contenu : réutiliser une clé avec une autre révision renvoie 409. Une nouvelle décision sur une nouvelle révision utilise donc une nouvelle clé.

`POST /api/projects/{id}/import/predictions` applique aussi ce protocole : une clé est obligatoire pour les comptes de service, facultative pour les humains. Les reçus des prédictions sont séparés de ceux des imports de tâches ; une même clé peut identifier ces deux opérations distinctes sans collision. Une importation de prédictions interrompue par une erreur de lot est annulée avec son reçu avant nouvelle tentative lorsque la clé est utilisée.

La création native de projet, la création de tâche via `/api/tasks/` ou `/api/projects/{id}/tasks/`, la création d’annotation, les décisions de revue, les annotations finales et les abonnements webhook utilisent également une clé obligatoire pour les comptes de service, facultative pour les humains. Les créations et rotations de comptes de service acceptent une clé humaine : un replay renvoie le même identifiant avec `token_shown_once: true`, sans renvoyer ni conserver le jeton en clair dans le reçu. Si le premier jeton a été perdu, demander sa rotation avec une nouvelle clé.

## Affectations et revue

| Route | Usage |
|---|---|
| `GET /my-assignments?project={id}&status=IN_PROGRESS` | Propres affectations visibles |
| `POST /assignments/{id}/start`, `/skip`, `/release` | Démarrer, ignorer ou abandonner sa tâche |
| `POST /assignments/{id}/cancel/staff` | Annuler une affectation en tant que superviseur |
| `POST /assignments/{id}/reassign/staff` | Réaffecter ; `user_id` obligatoire |
| `GET /projects/{id}/assignments/summary` | États globaux et charge par utilisateur |
| `GET /projects/{id}/review-queue?status=pending` | Tâches en attente de revue |
| `GET /projects/{id}/review-queue?status=disagreement` | Désaccords selon le seuil du projet |
| `GET /projects/{id}/review-queue?status=accepted` ou `rejected` | Tâches avec annotations acceptées ou rejetées |
| `GET /tasks/{id}/review` | Source, comparaisons, décisions et versions |
| `POST /reviews` | Décider pour une annotation |
| `GET /my-reviews?project={id}` | Retours privés sur ses propres annotations |
| `POST /tasks/{id}/final` | Créer ou réviser le résultat arbitré |

Un auteur peut fournir `version` dans les actions d’affectation : une version périmée renvoie 409. Chaque réponse d’affectation donne la version courante. Une correction d’annotation utilise sa révision courante via `If-Match` ou le champ `revision`, conformément à l’API native.

Une décision de revue accepte `annotation`, `decision` (`ACCEPT`, `REJECT`, `REQUEST_CORRECTION`, `REFERENCE`), `comment` et la `revision` examinée. Sur un projet collaboratif, fournir cette révision dans le corps ou dans `If-Match` est obligatoire : une valeur absente ou périmée renvoie 409. La route native de revue applique le même contrôle et renvoie la révision courante dans `ETag`. Rejet et correction exigent un motif. La revue et l’arbitrage sont refusés avant le nombre requis d’auteurs distincts. Les versions précédentes et les décisions sont conservées ; l’annotation corrigée repasse en revue. Les motifs restent privés pour les annotateurs et sont accessibles aux superviseurs autorisés.

Le paramètre `anonymous=true` et la politique `anonymous_review` remplacent les auteurs par des alias dans les réponses de comparaison. Les exports enrichis contiennent décisions, versions, arbitrage et détails d’accord ; les exports natifs conservent leur format Label Studio.

## Webhooks durables

`POST /projects/{id}/webhooks` accepte une URL HTTP(S) et une liste d’événements : `annotation.submitted`, `annotation.resubmitted`, `task.ready_for_review`, `review.decided`, `assignment.expired`, `project.published`, `task.arbitrated`, `project.archived`, `project.restored`. `task.arbitrated` signale la création ou la révision du résultat final. Le secret est renvoyé une seule fois.

Un replay idempotent renvoie le même abonnement avec `secret_shown_once: true`, sans secret. Les reçus ne conservent pas la clé de signature ; un ancien reçu contenant cette clé est expurgé lors de son replay. Si la première réponse a été perdue, supprimer l'ancien abonnement et en créer un autre avec une nouvelle clé d'idempotence.

La publication par `PATCH /api/projects/{id}/` émet aussi `project.published`. Répéter une requête qui laisse le projet déjà publié ne crée pas un nouvel événement.

Chaque événement est enregistré dans une boîte d’envoi transactionnelle. Une livraison interrompue est reprise après expiration de son bail. Les échecs sont réessayés avec un délai croissant, jusqu’à huit tentatives ; les échecs définitifs restent consultables et journalisés.

`GET /projects/{id}/webhook-deliveries` liste les livraisons sans exposer leur secret. `POST /projects/{id}/webhook-deliveries/{delivery_id}/retry` remet un échec en attente. Une livraison déjà réussie ou en cours ne peut pas être relancée.

Le corps est `{"event":"review.decided","data":{...}}`. Vérifier `X-Collab-Signature` en calculant HMAC-SHA256 sur les octets UTF-8 de `X-Collab-Timestamp + "."`, suivis des octets bruts du corps. Comparer les signatures en temps constant, rejeter les timestamps trop anciens et dédupliquer `X-Collab-Delivery` : le protocole garantit une livraison **au moins une fois**, pas une seule fois.

Le serveur revérifie la résolution DNS à chaque tentative puis se connecte directement à l’IP validée, en conservant la vérification TLS du nom d’origine. Les redirections et proxys d’environnement ne sont pas suivis. Les destinations privées, locales et réservées sont refusées. `COLLAB_WEBHOOK_ALLOW_PRIVATE=true` est réservé aux destinations internes explicitement autorisées par l’exploitant.

Exécuter régulièrement la maintenance dans le même environnement que l’application :

```sh
python manage.py collab_maintenance
```

Le worker de maintenance doit rester actif dans le déploiement Docker. Les options `--agreement-limit` et `--recompute-agreement` permettent la reprise et la reconstruction du cache d’accord des tâches historiques par lots. La boîte d’envoi survit au redémarrage du serveur et du worker.

## Validation, erreurs et audit

`POST /projects/{id}/archive` et `POST /projects/{id}/restore`, avec un corps vide, archivent ou restaurent une campagne. Ces actions sont réservées aux administrateurs humains et aux comptes de service autorisés avec `projects:write`. L’archivage conserve les sources, résultats, versions et accès de lecture autorisés ; les nouvelles mutations sont refusées jusqu’à restauration. `is_archived` est un champ de lecture seule : il se modifie par ces routes dédiées. Les reçus idempotents revérifient aussi les accès et l’état d’archivage avant d’être renvoyés.

Les listes utilisent `{count,next,previous,results}`, avec `limit` de 1 à 500 (100 par défaut) et `offset` positif ou nul. Les entrées inconnues, identifiants mal formés, expirations passées et seuils non finis sont rejetés. Les erreurs d’extension utilisent `{code,detail,errors}` ; les erreurs internes ne transmettent pas de trace technique au client.

`COLLAB_API_THROTTLE` limite les appels par identité (défaut `600/min`). Les événements métier et les mutations des API natives sont journalisés sans corps de requête, query string, en-tête d’authentification ni secret. `GET /audit?project={id}&action=...` est réservé aux administrateurs humains. L’organisation de l’événement est conservée indépendamment de l’organisation active ultérieure de son auteur et de la suppression du projet.

Après mise à jour, appliquer les migrations avant de démarrer les workers. Les comptes historiques sans liste de projets perdent l’accès implicite à toute l’organisation : un administrateur doit définir `project_ids` ou choisir explicitement `all_projects=true`. Remplacer l’ancien scope `native` par les scopes précis nécessaires. Les jetons historiques de comptes de service sont migrés vers une empreinte ; privilégier ensuite une rotation pour obtenir un jeton dédié.
