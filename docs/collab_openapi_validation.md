# Validation des contrats OpenAPI collaboratifs

La génération et la validation du schéma global ont été exécutées avec `python manage.py spectacular --validate --format openapi-json --file <fichier temporaire>`, sans exclusion supplémentaire de routes ni suppression des diagnostics. Les tests de contrat utilisent également `SchemaGenerator` sur les routes d’extension et sur le parcours natif utilisé par les agents, puis `validate_schema` sur le document global.

## Parcours collaboratif vérifié

- Les requêtes et réponses des routes `/api/extensions/v1/` disposent de sérialiseurs typés, de paramètres de pagination/filtrage et de scopes documentés. Les identifiants d’opération sont uniques.
- Les créations natives de projets, imports, tâches, annotations et décisions de revue documentent `Idempotency-Key`, obligatoire pour les comptes de service et facultatif pour les humains.
- Les modifications d’annotation documentent `If-Match`, la révision `ETag` retournée et le conflit HTTP 409. Les sérialiseurs, exemples et réponses natives existants sont conservés.
- Les tests vérifient que les corps de requête natifs restent présents et que les paramètres métier ne sont pas remplacés par un corps générique.

## Génération globale après correction

La validation finale sur l'image `c0b780251af7…` se termine avec **74 avertissements (64 uniques), zéro erreur et un code de sortie 0**. La preuve est conservée dans `/tmp/codex-labelstudio-collab/final-schema-maintenance.json`. Aucun endpoint public supplémentaire n'a été exclu et aucun diagnostic n'a été masqué. La création de tâche via `POST /api/projects/{id}/tasks/` est désormais également documentée, avec son en-tête d'idempotence.

Les listes vides `required: []` des contrats natifs d’annotations, de modèles et de stockage ont été supprimées : l’absence de ce mot-clé exprime correctement qu’aucune propriété n’est obligatoire en OpenAPI 3.0. La génération seule avait laissé passer ces documents invalides. Les entrées des webhooks collaboratifs portent un nom de composant distinct (`CollaborationWebhookRequest`) pour conserver aussi le contrat complet des webhooks natifs.

Les six vues natives qui produisaient initialement 20 erreurs de déduction ont reçu des contrats explicites dans `collab/native_schema.py` :

| Vue native | Contrat complété |
|---|---|
| `tasks.api.AnnotationConvertAPI` | Brouillon natif et identifiant de l’annotation conservée en workflow collaboratif |
| `data_manager.api.ProjectStateAPI` | Projet natif, permissions d’interface et compteurs filtrés |
| `organizations.api.OrganizationResetTokenAPI` | Corps vide et réponse typée du nouveau lien d’invitation |
| `labels_manager.api.LabelBulkUpdateAPI` | Entrée de mise à jour des labels et nombre d’annotations modifiées |
| `projects.api.ProjectMemberDetailAPI` | Identifiants entiers, réponse 204 sans corps et erreurs de permission |
| `data_import.api.MicrophoneUploadAPI` | Fichier audio multipart et URL de lecture renvoyée |

Les avertissements restants concernent principalement des annotations de type non explicitées dans des champs natifs et des noms d’énumérations partagés. Ils n’empêchent pas de produire un document OpenAPI valide. `--fail-on-warn` reste volontairement en échec tant que ces avertissements ne sont pas corrigés.

Les tests couvrent les schémas des six vues, les corps multipart, les réponses sans contenu, les en-têtes d’idempotence et de concurrence ainsi que les contrats typés de l’API collaborative. Le parcours agent est documenté dans [collab_api.md](collab_api.md).
