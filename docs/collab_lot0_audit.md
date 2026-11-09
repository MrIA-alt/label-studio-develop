# Lot 0 – Audit de la base Scicom (Label Studio Community 1.23.0, Apache-2.0)

## Identification vérifiée de la base

Le 6 novembre 2026, le dépôt public cible [Scicom-AI-Enterprise-Organization/label-studio-rbac](https://github.com/Scicom-AI-Enterprise-Organization/label-studio-rbac) a été consulté directement via le connecteur GitHub. Sa branche `develop` pointe sur [b027cd253bd820a7da40682e231fefd84c217528](https://github.com/Scicom-AI-Enterprise-Organization/label-studio-rbac/commit/b027cd253bd820a7da40682e231fefd84c217528). Son `pyproject.toml` déclare la version 1.23.0 et la licence Apache-2.0.

La comparaison du contenu de ces cinq fichiers à l'import local `e229849`, après normalisation des fins de ligne, est identique :

| Fichier | Composant identifié |
|---|---|
| `pyproject.toml` | Version, licence et dépendances Python |
| `label_studio/core/permissions.py` | Matrice des trois rôles Scicom |
| `label_studio/core/api_permissions.py` | Contrôle serveur des permissions par rôle |
| `label_studio/organizations/models.py` | Membres, rôles et appartenance à l'organisation |
| `label_studio/tasks/migrations/0061_annotation_review_fields.py` | Champs de revue QA historiques |

Les composants RBAC et QA Scicom sont donc réutilisés dans cette base. Le dépôt local conserve un seul commit d'import initial : cette comparaison établit le contenu réutilisé, sans inventer une filiation Git que son historique ne contient pas. La branche maintenue est `codex/collaborative-completion`.

Les ajouts collaboratifs n'introduisent aucune nouvelle dépendance dans `pyproject.toml`, `poetry.lock`, `web/package.json` ou `web/yarn.lock`. Le calcul des rectangles tournés utilise la bibliothèque standard Python. Les fichiers `LICENSE`, `NOTICE` et les licences natives sont conservés. Les changements de distributions ou de dépendances lors d'une mise à jour doivent être revérifiés avant livraison.

Pour suivre l'upstream, comparer d'abord les modifications à ce commit identifié, exécuter les tests de permissions, de compatibilité et de migration dans une base isolée, puis reconstruire et vérifier l'image Docker avant mise à jour. Ne pas remplacer les composants RBAC par les seules permissions Community.

## Existant
- RBAC présent : `core/permissions.py` (`ROLE_PERMISSIONS`) et `core/api_permissions.py` (`RoleBasedPermission`, `HasObjectPermission`). Rôles : `admin`, `qa`, `labeller`.
- Aucun modèle d'assignation, de politique, de review ni d'audit en base native.

## Surfaces exposant les annotations d'autrui (corrigées dans le Lot 1)
| Surface | Correction |
|---|---|
| `GET /api/tasks/{id}/` (annotations expand) | `scrub_task_representation` |
| `GET/POST /api/tasks/{id}/annotations/`, `/api/annotations/{id}/` | `can_access_annotation`, `scope_annotations`, `completed_by` forcé |
| Brouillons (`/api/tasks/{id}/drafts/`, `/api/drafts/`) | `can_access_draft`, `scope_drafts` |
| Data Manager (`/api/tasks/`) : agrégats, filtres, tris | scrub + `sanitize_prepare_params` (liste blanche) + `scope_tasks` |
| `/api/projects/{id}/tasks/`, next task | scrub / `scope_tasks` / annotations filtrées |
| `/api/projects/{id}/annotators/` | limité à soi-même |
| Agreement de tâche | refusé en mode aveugle |
| Review d'annotation (`AnnotationReviewAPI`) | refusé aux labellers (trou préexistant) |
| Exports | déjà réservés à `projects_change` (non accessible aux labellers) |

## État courant après intégration
- Les compteurs de `ProjectSummary`, dates dérivées, filtres, accès directs, historiques FSM et contributions des membres suivent les règles serveur de confidentialité.
- La préannotation ML vérifie l'affectation avant tout appel externe et ne transmet que les résultats autorisés. La configuration ML relève de l'administration du projet.
- Les médias référencés dans les données sources restent lisibles par les utilisateurs autorisés, même lorsque le fichier JSON d'import et l'image sont deux téléversements distincts. Les fichiers contenant des résultats privés ne deviennent pas accessibles par cette association.
- Le worker persistant, la configuration Compose et les trois espaces React sont présents. Leur validation effective est suivie dans [collab_validation.md](collab_validation.md).
- Les plugins JavaScript restent hors périmètre et ne sont pas livrés.

## Activation
Les nouveaux projets API reçoivent une politique privée : deux annotations, mode aveugle, affectation manuelle et revue manuelle. Les projets historiques sans politique conservent le comportement natif. Les tests de compatibilité sélectionnent explicitement ce dernier mode ; ils ne changent pas les valeurs par défaut de l'application.

## Addendum - Revue, Lot 3 (API agent)

- Revue (FR-009/010/011/004): `AnnotationReview` (historique append-only), `AnnotationRevision` (versions corrigées), `TaskArbitration` (référence / annotation finale = `ground_truth`). Le motif de rejet n'est visible que de l'auteur (`GET /api/extensions/v1/my-reviews`). Sous `review_mode=manual`, l'annotation soumise est verrouillée jusqu'à une demande de correction. `anonymous_review` remplace les identités par « Annotateur A/B… ». Consensus extensible : `collab/consensus.py` (`@metric`), seuil `agreement_threshold`.
- Lot 3 : comptes de service avec scopes par action et restriction explicite par projet. Le scope historique `native` n'accorde aucun droit. La configuration d'une politique requiert `policy:write`, y compris lorsqu'elle passe par la route native de projet. Les créations utilisent `Idempotency-Key`, les webhooks sont signés HMAC-SHA256 et leur transport contrôle les destinations SSRF. Les événements sont persistés dans une outbox transactionnelle avec reprises. Les routes natives réutilisées et les extensions ont des contrats OpenAPI typés ; la validation réelle du document global est décrite dans [collab_openapi_validation.md](collab_openapi_validation.md).
- Listes paginées : `{count, next, previous, results}` (défaut 100, max 500).
- La revue d'une annotation collaborative exige sa révision courante (`revision` ou `If-Match`). Une décision portant sur une ancienne version échoue avec HTTP 409, sans accepter la correction à l'insu du superviseur.
- Le pilote HTTP d'acceptation couvre cent tâches multimodales et trois sessions d'annotation. Les parcours navigateur et les vérifications de sauvegarde/redémarrage disposent de pilotes distincts. Seuls les résultats effectivement exécutés du rapport de validation font foi.
