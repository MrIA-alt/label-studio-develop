# Lot 0 – Audit du fork (Label Studio Community 1.23.0, Apache-2.0)

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

## Résiduel / non traité
- Compteurs de labels du `ProjectSummary`, `updated_at` des tâches (fuite temporelle mineure).
- Plugins/hooks tiers, validation sur instance Docker non réalisées.

## Activation
`ProjectAnnotationPolicy` (par projet) : `blind_mode`, `assignment_mode`, `required_annotations`. Défaut = comportement Label Studio natif.
