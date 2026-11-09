# Validation du fork collaboratif

**Mise à jour du 9 novembre :** les invitations par mail et l’image `8fe8f04…` ont leur [rapport de validation séparé](collab_invitations_validation.md). Les résultats et le livrable du 6 novembre ci-dessous conservent leur provenance historique.

Rapport du 6 novembre 2026. L'image fonctionnelle cible est `sha256:99d727d4c102f3f1f99d1fc05715604e0258eec3da577d37febe790f609b1360`. Elle intègre les corrections de focus et de rendu QA ainsi que les lectures de projets/politiques/tâches déjà testées. Les exécutions antérieures sur `c0b780251af7…` et `b4323cc91245…` gardent leur provenance propre ; elles ne sont pas présentées comme de nouvelles exécutions sur 99d727d.

**L'image est fonctionnelle et déployable pour le périmètre demandé.** La suite backend finale, les 35 contrôles HTTP sur un déploiement vierge, le parcours navigateur renforcé et les vérifications de sauvegarde/restauration et de redémarrage ont réussi. Aucun défaut fonctionnel identifié ne reste ouvert sur les parcours validés de multi-annotation, revue et correction. Les limites de couverture des variantes natives sont détaillées dans la matrice.

À la demande explicite de l'utilisateur, la livraison vise une image fonctionnelle et déployable, prioritairement pour la multi-annotation, la revue et la correction. Les plugins JavaScript FR-015/016 et TEST-007 ainsi que le travail de performance sont exclus. Le seuil initial p95 < 500 ms n'a pas été atteint par les mesures conservées ; il ne constitue plus un gate de cette livraison. Aucun nouveau profilage, benchmark ou tuning n'est poursuivi.

Ce rapport distingue les exécutions terminées des vérifications encore en attente. Les résultats de plusieurs exécutions ne sont pas additionnés pour produire un total artificiel.

## Vérifications sur l'état de livraison

| Contrôle | Résultat et portée |
|---|---|
| Construction Docker | Image cible construite ; digest immuable ci-dessus |
| Déploiement vierge final (99d727d) | Réussi : projet Compose UUID, nouveaux identifiants synthétiques, volumes PostgreSQL/app/static vierges, profil standard 2/1/2 ; migration sortie 0 et quatre healthchecks sains ; environnement jetable nettoyé, application principale inchangée |
| Provenance de l'image cible | Digest immuable vérifié ; six fichiers de tests identiques au workspace ; contenu source Canvas de l'image contrôlé et captures finales inspectées |
| Présence des tests corrigés dans l'image cible | Les cinq fichiers historiques Tavern/fixtures et le nouveau fichier de tests ACL sont identiques au workspace par SHA-256 ; aucun overlay de test ou de production |
| Suite fonctionnelle finale (99d727d) | **631 réussis, 55 ignorés, 1 désélectionné en 454,02 s ; sortie 0, zéro erreur/échec**. Seul `test_upload_duration[tasks0-201-1000-30]` est désélectionné ; son volet fonctionnel a réussi à 6,780 s ; aucun autre test de données, confidentialité ou permissions retiré |
| Suite complète PostgreSQL en conteneurs jetables (c0b780) | 624 réussis, 55 ignorés, 1 échec en 669,97 s ; import de 1 000 tâches à 209,46 s pour un seuil de 30 s ; le même test réussit ensuite isolément sous profilage en 20,30 s, puis sans instrumentation sur b432 en 6,780 s. Le critère de performance est exclu de cette livraison sur demande |
| Cohérence des migrations (c0b780) | `makemigrations --check --dry-run` : sortie 0, aucune migration manquante |
| OpenAPI global (c0b780) | `spectacular --validate` : sortie 0, zéro erreur, 74 avertissements dont 64 uniques |
| Maintenance des accords (c0b780) | Recalcul ciblé terminé sans échec, `agreements=8` |
| Compatibilité Tavern SQLite | 5 réussis, 1 ignoré, 4 désélectionnés en 55,69 s ; sortie 0 |
| Compatibilité Tavern PostgreSQL | 5 réussis, 1 ignoré, 4 désélectionnés en 59,22 s ; sortie 0 |
| Scénario HTTP complet d'acceptation | 35 contrôles sur 35 réussis sur le déploiement vierge 99d727d, après 35/35 sur c0b780 ; 100 tâches, 300 affectations et trois comptes réels avec CSRF |
| Parcours navigateur renforcé (99d727d) | **5 tests sur 5 réussis en 158 s, zéro ignoré/pending** ; choix A/B/C cochés, NER et Stamp rendus, correction privée, POST et GET final avec trois résultats non vides profondément égaux à la correction choisie, originaux et snapshot préservés, archivage/restauration |
| Sauvegarde/restauration dans une copie | Réussie, sortie 0 : source préservée, données/permissions et médias HTTP/octets identiques dans la copie ; FSM vide dans cette fixture, préservation non vide couverte séparément par les tests de migration |
| Persistance après redémarrage de l'application et du worker | Réussie : healthchecks et nouveau cycle worker, session préexistante, tâche/résultats et deux médias HTTP ; 100 tâches, 300 affectations et outbox pending préservés |
| Lectures natives et Data Manager | 127 tests SQLite réussis, zéro échec/erreur/ignoré, avant reconstruction de 99d727d ; JUnit et journal conservés |
| ACL de projets et lecture de politique | 53 tests PostgreSQL ciblés réussis avec deux sources montées en lecture seule ; ancien rôle actif conservé même avec doublons, révocation et scopes frais ; changements inclus dans l'image cible |
| Livrable Docker autonome | `dist/label-studio-collab-2026-11-06-99d727d/` prêt ; archive app/PostgreSQL de 578 532 448 octets chargée avec `docker load`, sortie 0 et IDs validés ; configuration Compose valide ; générateur d'environnement testé en mode 0600 avec refus d'écrasement |

Les deux exécutions Tavern ciblées utilisent le backend de l'image précédente `3a543cca…f971f187` et les cinq fichiers de tests finaux montés en lecture seule. Dans chaque moteur, seul le cas propre à l'autre moteur est ignoré. Les quatre désélections correspondent aux autres cas du fichier, hors sélection ciblée ; elles ne constituent pas des succès. La suite complète sur c0b780 confirme leur intégration sans montage, mais reste en échec sur le seuil de durée d'import.

L'unique échec de la suite est `tests/test_tasks_upload.py::test_upload_duration[tasks0-201-1000-30]`, ligne 271. La réponse HTTP 201, les 1 000 tâches et leur état `is_labeled` sont vérifiés avant l'assertion de durée ; celle-ci échoue sur `209.46136021614075 < 30`. Le volume et le seuil restent inchangés.

Le même cas passe ensuite sur l'image inchangée dans un nouveau PostgreSQL tmpfs, sous `cProfile` et chronométrage SQL : 20,30 s pour l'appel du test, dont 7,16 s pour 6 062 requêtes. Les 2 000 écritures d'états FSM restent présentes. Les principaux coûts sont les synchronisations du projet effectuées par les hooks individuels et les contrôles frais de projet archivé ; aucun cache de permissions ni hook n'a été supprimé. L'échec de 209 s n'est pas reproduit isolément ; une mesure non instrumentée au repos reste à effectuer pour qualifier cette variabilité. Ce succès ciblé ne transforme pas rétroactivement la suite initiale en une exécution entièrement verte.

Le même test, non instrumenté et sans overlay, réussit ensuite sur b4323cc avec un import à **6,780 s**, sans modifier le seuil de 30 s ni les 1 000 tâches. Une opération de redémarrage a été annoncée pendant cette fenêtre : ce résultat n'est donc pas présenté comme une mesure garantie au repos. La suite c0b780 initiale reste conservée telle quelle.

Les 55 ignorés se répartissent en 36 cas associés à `HTX-868`, 17 combinaisons de paramètres déclarées sans objet par les tests natifs, une variante SQLite et un cas ignoré explicitement. Ils ne sont pas comptés comme réussis.

La preuve de redémarrage porte sur l'application et la maintenance entre 15:26:05 et 15:28:32 UTC : même base et même volume, session signée préexistante toujours utilisable, huit annotations, huit revues, deux révisions et deux arbitrages préservés. La fixture ne contient aucun état FSM natif : cette exécution prouve seulement la préservation de cet ensemble vide ; les états non vides sont couverts séparément par les tests dédiés. Aucun arrêt brutal de PostgreSQL n'a été simulé.

Les fixtures historiques choisissent explicitement le fonctionnement Community sans politique collaborative. Elles ne changent aucune permission ni valeur par défaut de production. Les assertions de résultats, auteurs, filtres, compteurs et durées sont conservées. Le contrat de colonnes vérifie aussi `valueType`, l'archivage et les compteurs de file ; un mot de passe source synthétique est configuré puis son absence est vérifiée dans les réponses PATCH et GET.

La sélection des cas PostgreSQL reconnaît désormais `DJANGO_DB=postgresql` aussi bien que l'ancien alias `default`. L'attente d'ordre des tâches correspond au tri observé dans le PostgreSQL du Compose de tests ; aucune assertion de contenu n'a été supprimée.

L'acceptation HTTP finale sur le déploiement vierge 99d727d vérifie trois soumissions concurrentes, les réponses et brouillons personnels, le refus des accès directs aux annotations étrangères et des exports interdits. Elle couvre l'anonymat en revue, le rejet privé, une correction en révision 2, le conflit 409 sur version périmée, l'historique et la préservation des originaux lors de l'arbitrage. Les médias PNG sont lus après authentification par les trois comptes ; l'accès anonyme et la pièce jointe privée d'un autre auteur sont refusés. Le parcours agent vérifie l'idempotence, les scopes, la révocation, l'archivage et la restauration du projet.

## Matrice de conformité

« Conforme » signifie que le comportement décrit a une preuve d'exécution dans le périmètre indiqué. « Partiel » signale une partie vérifiée et un contrôle restant explicitement identifié ; ce statut ne transforme pas un contrôle inexécuté en défaut de code. « Absent » signifie non livré et « non vérifié » signifie qu'aucune exécution probante n'est disponible. Les plugins exclus ne sont pas comptés comme une conformité ni comme un défaut de cette livraison.

La base locale est l'import `e229849` complété par les modifications de développement. Le Lot 0 a comparé cinq fichiers de référence au pin Scicom `b027cd253bd820a7da40682e231fefd84c217528` : contenus identiques après normalisation des fins de ligne. Cette preuve identifie le RBAC et la revue réutilisés ; elle ne prétend pas reconstituer une filiation Git absente de l'historique local. Voir [l'audit du Lot 0](collab_lot0_audit.md).

| Exigence | Statut | Preuve et limite |
|---|---|---|
| FR-001 — Affectation multiple | Conforme | Acceptation HTTP : trois auteurs distincts, une tâche source, trois résultats ; tests de plage N, unicité et concurrence des affectations dans la suite backend |
| FR-002 — Confidentialité stricte | Conforme | Sessions annotateur réelles, refus d'ID étranger, brouillons et rejet privés, export refusé, médias autorisés/refusés ; tests backend des agrégats, Data Manager, historique, erreurs, organisations et projets |
| FR-003 — Annotations simultanées | Conforme | Trois soumissions HTTP concurrentes ; transactions PostgreSQL pour sauvegardes indépendantes et conflit 409 sur une même révision dans `test_concurrency.py` |
| FR-004 — Révision personnelle | Conforme | Acceptation : motif privé, correction en révision 2, ancienne version et annotations des autres auteurs conservées ; tests de retour en revue et de verrouillage |
| FR-005 — Modes d'affectation | Conforme | Tests backend manuel/automatique/hybride, équilibre, plafonds, inactifs, expiration et redistribution ; état des affectations préservé après redémarrage, worker repris ; expiration/distribution et redémarrage sont des preuves distinctes |
| FR-006 — États indépendants | Conforme | Tests des transitions, de l'expiration, du lien entre soumission native et affectation, et de la complétude seulement après N auteurs ; affectations persistées dans le parcours HTTP |
| FR-007 — Outils texte/image conservés | Partiel | Classification, NER, boîtes et sources multimodales exercés par HTTP, annotations/imports/exports natifs testés ; pas de parcours navigateur final couvrant tous les outils relations, polygones, segmentation et points |
| FR-008 — Interface personnalisable | Partiel | Configurations déclaratives natives, labels, consignes et contrôles texte/image utilisés par le parcours agent ; validation finale des personnalisations visuelles, raccourcis et panneaux non exhaustive |
| FR-009 — Revue et arbitrage | Conforme | Acceptation backend et Cypress renforcé 99d727d : seuil, décisions, motif, correction privée, régions rendues et annotation finale non vide ; POST/GET final profondément égaux à la correction choisie, trois originaux et snapshot conservés |
| FR-010 — Consensus et désaccords | Conforme | Métriques exécutées sur classification, labels, passages et IoU, dont rectangles tournés et invalides ; cache d'accord recalculé (`agreements=8`) ; scores avancés hors première version manuelle permise par la spécification |
| FR-011 — Anonymat en revue | Conforme | Réponse QA anonyme vérifiée par HTTP/backend ; comparaison anonyme avec choix/NER/rectangle effectivement rendus dans le Cypress renforcé 99d727d |
| FR-012 — Projet créé par agent | Conforme | Compte de service réel : projet configuré, N=3, mode aveugle, 100 tâches, membres, 300 affectations, publication et lecture des résultats autorisés sans interface web |
| FR-013 — API native et extension v1 | Conforme | Parcours HTTP natif/extension réussi et contrats OpenAPI validés ; les deux routes proposées pour les plugins sont exclues avec FR-015/016 |
| FR-014 — Exigences API | Conforme | Authentification, intersection scopes/projets, révocation, validation, pagination, erreurs, idempotence, audit, quotas et webhooks testés ; contrats OpenAPI validés dans la suite fonctionnelle 99d727d ; performance exclue sur demande |
| FR-017 — Espace annotateur | Partiel | HTTP réel et Cypress final texte/NER/rectangle : sauvegarde, rechargement, Next et correction privée réussis ; les raccourcis et panneaux natifs ne sont pas tous parcourus dans la campagne navigateur |
| FR-018 — Espace superviseur | Partiel | Files, progression, désaccords et décisions couverts par backend ; revue/comparaison/arbitrage visuels validés dans le Cypress renforcé ; filtres et actions groupées UI non exhaustivement parcourus |
| FR-019 — Espace administrateur | Partiel | Gestion/API des projets, membres, politique, distribution, agent et audit testée ; archivage/restauration réussi dans Cypress ; autres formulaires administratifs non exhaustivement parcourus ; plugins exclus |

| Acceptation demandée | Statut | Preuve et limite |
|---|---|---|
| TEST-001 — Plusieurs annotateurs | Conforme | Trois sessions réelles et trois soumissions concurrentes dans l'acceptation finale ; résultats distincts conservés |
| TEST-002 — Confidentialité API | Conforme | Véritables comptes annotateur, réponses personnelles, accès direct étranger et export refusés ; brouillons, commentaires, Data Manager et historique couverts par les tests backend |
| TEST-003 — Revue | Conforme | Trois soumissions, QA autorisé, acceptation/rejet, motif privé et correction isolée exercés par le scénario HTTP final |
| TEST-004 — API agent | Conforme | Parcours sans interface réalisé avec 100 tâches plutôt que les dix minimales, N=3, mode aveugle, affectations, publication et résultats selon permissions |
| TEST-005 — Permissions | Conforme | Matrice des rôles et scopes, séparation des projets/organisations, refus des actions administratives et destructrices non autorisées ; activation de plugin exclue |
| TEST-006 — Concurrence | Conforme | Transactions PostgreSQL réelles : affectation unique, deux sauvegardes indépendantes, conflit sur même révision ; complétées par trois soumissions HTTP simultanées |
| TEST-008 — Compatibilité/migration | Conforme | `MigrationExecutor` migre projets, sources, annotations, brouillons, rôles et revue historiques ; accès, revue native et export JSON vérifiés après migration ; scénario séparé agents/reçus ; démarrage vierge 99d727d avec migrations, healthchecks et35/35HTTP ; tous les formats externes optionnels ne sont pas couverts |

## Preuves complémentaires exécutées

| Contrôle | Résultat observé | Limite de portée |
|---|---|---|
| Permissions destructrices natives, idempotence et secret webhook | 68 tests SQLite réussis ; puis 38 tests PostgreSQL réussis en 97,43 s | Exécutions ciblées avant la suite complète de l'image cible |
| Verrou d'idempotence PostgreSQL | Une transaction concurrente enregistre un événement d'audit avec le même acteur pendant le verrou ; test réussi | Vérifie les verrous User/FK, sans simuler un incident matériel |
| Transport webhook HTTP réel | Récepteur loopback : première réponse 503, seconde 200 ; HMAC, timestamp, corps, identifiant de livraison, backoff et deux tentatives vérifiés | Dérogation aux adresses privées limitée au processus synthétique ; ne valide pas une destination tierce ni son TLS |
| Refus des destinations webhook privées par défaut | Rejet avant tout appel réseau, vérifié dans le même pilote | Aucun changement de configuration de l'application |
| Schéma OpenAPI global | Génération et validation finales exécutées avec sortie 0, zéro erreur ; 74 avertissements, 64 uniques | Les avertissements restent explicitement documentés dans [la validation OpenAPI](collab_openapi_validation.md) |

Le contrôle des permissions destructrices couvre le réimport de sources, la suppression d'uploads et la suppression de versions de prédictions : droits humains, intersection des scopes, projet autorisé, révocation et archivage. Il couvre aussi les imports de prédictions sur les deux implémentations natives, la clé obligatoire pour les services, le replay sans doublon, les conflits, le rollback et les namespaces distincts. Les reçus webhook nouveaux et historiques sont expurgés du secret de signature.

### HTTPS

Le fichier `docker-compose.collab.tls.yml` a passé `docker compose config --quiet`. Une CA privée et un certificat `localhost` jetables ont ensuite été utilisés par un Nginx séparé, lié exclusivement à `127.0.0.1:18443`, sur l'image `3a543cca…f971f187` :

- `curl --cacert` renvoie HTTP 200 et `ssl_verify_result=0` pour `/health/`, avec `status=UP` ;
- TLS 1.3 et `TLS_AES_256_GCM_SHA384` sont négociés ;
- la CA non reconnue et un nom d'hôte incorrect sont rejetés, code curl 60 ;
- le clone est supprimé après le test ; les identités, images, dates de démarrage et compteurs de redémarrage des quatre services actifs restent identiques.

Le binaire Nginx, sa configuration et les deux scripts d'entrypoint ont ensuite été comparés par SHA-256 avec c0b780 puis avec l'image cible b4323cc : ils sont identiques, version Nginx 1.22.1. Le test réseau n'a pas été répété sur ces deux digests. Aucun magasin de confiance du système, `HOSTNAME` global ou service actif n'a été modifié. Cette preuve valide le composant TLS local ; elle ne remplace pas le provisionnement d'un certificat reconnu par les clients du déploiement réel.

## Provenance des preuves

Les rapports privés ne conservent aucun identifiant de l'application dans le dépôt. Les derniers tests de régression utilisent exclusivement les secrets synthétiques du Compose de tests ; aucun fichier d'environnement applicatif n'est chargé.

| Exécution | Emplacement privé de la preuve |
|---|---|
| Suite complète de c0b780 | `/tmp/codex-labelstudio-collab/final-suite-8b3cd6af4436416bab5fa08599fed374/` : manifeste, résumé, journal et JUnit |
| Suite fonctionnelle finale 99d727d | `collab-test-results/backend/final-functional/` : JUnit, manifeste du digest/de l'unique désélection et synthèse ; 631 réussis, 55 ignorés, 1 désélectionné, aucun overlay ; originaux privés sous `/tmp/codex-labelstudio-collab/final-functional-bae9a3b203c645bfb708279fc61ae0f0/` |
| Déploiement vierge et HTTP final 99d727d | `collab-test-results/deployment/fresh-deployment-d9ba440ae11e4bb199d04b158b98fd20.summary.json` : migrations/healthchecks/35 contrôles réussis, nettoyage du seul environnement jetable |
| Copie Windows des résultats backend non sensibles | `collab-test-results/backend/c0b780-final-suite/` : JUnit, résumé, manifeste et triage ; ignoré par Git |
| Profil ciblé de l'import, réussi | `collab-test-results/backend/c0b780-import-profile/` : JUnit, résumé, manifeste et profil agrégé ; preuve privée originale sous `/tmp/codex-labelstudio-collab/import-profile-53a2f47a458d4465bcce2db0d22a02e0/` |
| Import1000 non instrumenté b4323cc, réussi | `/tmp/codex-labelstudio-collab/b432-import-quiet-270977c85a214e03bb40048ae1f5eaf0/` : 6,780 s, fenêtre non garantie sans contention |
| Intégrité de l'image finale | `/tmp/codex-labelstudio-collab/final-image-integrity.json` |
| Tests et composant TLS de b4323cc | `/tmp/codex-labelstudio-collab/b432-test-tls-integrity.json` : cinq fichiers de tests et composant Nginx identiques aux preuves précédentes |
| Acceptation HTTP finale | `/tmp/codex-labelstudio-collab/final-acceptance.json` : 35 contrôles réussis |
| Parcours navigateur normal b4323cc | `collab-test-results/browser/final-b432.log` : 5 tests réussis en 113 s |
| Parcours navigateur renforcé final 99d727d | `collab-test-results/browser/final-functional/summary.json`, `cypress.log` et captures : 5 réussis, zéro ignoré/pending, 158 s ; source Canvas contrôlée |
| ACL/policy optimisées, permissions PostgreSQL ciblées | `/tmp/codex-labelstudio-collab/policy-scope-9260b41b872e47709314195eee75f9a6/` : 53 réussis, deux overlays de sources |
| Lectures natives et Data Manager | `collab-test-results/backend/read-optimization/results.xml` et `pytest.log` : 127 réussis |
| Microprofil de GET policy | `/tmp/codex-labelstudio-collab/policy-component-profile-a71714a8c2e24dd1b4fcd345f180c228/profile.json` : 7 à 5 requêtes ; lecture seule, authentification forcée, connexion conservée, sans concurrence HTTP ; ne prouve pas un p95 sous charge |
| Migrations, OpenAPI et maintenance finaux | `/tmp/codex-labelstudio-collab/final-schema-maintenance.json` |
| Benchmark complet initial, seuil non atteint | `/tmp/codex-labelstudio-collab/final-benchmark-scale-initial.json` |
| Sauvegarde/restauration dans une copie | `/tmp/codex-labelstudio-collab/final-operational-e5bd79cb910d4bed9d2bced5953f0b95/backup-restore.json` |
| Redémarrage et persistance | `collab-test-results/operations/restart.summary.json` : `success=true`, projet synthétique 5 |
| Livrable autonome | `dist/label-studio-collab-2026-11-06-99d727d/` : README, Compose, générateur d'environnement et `images.tar.gz` validé par chargement Docker |
| Contrats Tavern finaux SQLite/PostgreSQL | `/tmp/codex-labelstudio-collab/tavern-native-final-b932298743ce4e01b3189d35c0215317/` : un JUnit par moteur |
| HTTPS | `/tmp/codex-labelstudio-collab/tls-proof-df607b833a5a4723bb9de5f09bd1a1a2/` : résultat et comparaison avec l'image finale |
| Transport webhook réel | `/tmp/codex-labelstudio-collab/operations/webhooks-3f2afbddda2f4613a153432d37bc3885.json` |

Les gates fonctionnels finaux sont terminés avec succès, y compris le Cypress renforcé. Le smoke b4323cc reste dans l'historique avec sa limite initiale de couverture ; il n'est pas utilisé seul pour conclure sur le contenu des annotations finales. Les mesures de performance ci-dessous restent un historique hors périmètre, sans nouveau travail demandé.

## Historique de performance, hors périmètre de livraison

Les mesures complètes antérieures utilisaient 50 clients et 100 000 tâches : deux workers sur c0b780 donnaient un p95 de 2 237 ms pour la politique et 7 687 ms pour une tâche ; 16 workers sur b4323cc donnaient 810 ms et 3 219 ms, sans erreur HTTP. Un probe de diagnostic ultérieur restait au-dessus de 500 ms. Ces chiffres ne valident pas l'objectif initial et ne sont pas un gate de l'image fonctionnelle demandée. Le profil de livraison revient au standard max/min/initial de 2/1/2.

## Reproduction et limites

La suite de régression s'exécute séparément de l'application :

```sh
export COLLAB_TEST_IMAGE=sha256:99d727d4c102f3f1f99d1fc05715604e0258eec3da577d37febe790f609b1360
test_project="collab-validation-$(date +%s)"
evidence_dir="$(mktemp -d /tmp/collab-validation-XXXXXX)"
docker compose -p "$test_project" -f docker-compose.collab.test.yml up -d --wait db
if python3 - "$test_project" "$evidence_dir" <<'PY'
import json, pathlib, subprocess, sys
project, evidence = sys.argv[1:]
base = ['docker', 'compose', '-p', project, '-f', 'docker-compose.collab.test.yml']
configuration = json.loads(subprocess.check_output([*base, 'config', '--format', 'json']))
selection = configuration['services']['tests']['command']
selection = [*selection, '--deselect=tests/test_tasks_upload.py::test_upload_duration[tasks0-201-1000-30]']
with (pathlib.Path(evidence) / 'pytest.log').open('w') as log:
    result = subprocess.run([*base, 'run', '--name', project + '-runner', 'tests', *selection],
                            stdout=log, stderr=subprocess.STDOUT)
raise SystemExit(result.returncode)
PY
then
  test_status=0
else
  test_status=$?
fi
docker cp "$test_project-runner:/tmp/collab-test-results.xml" "$evidence_dir/results.xml"
chmod 600 "$evidence_dir/pytest.log" "$evidence_dir/results.xml"
docker compose -p "$test_project" -f docker-compose.collab.test.yml down --remove-orphans
test "$test_status" -eq 0
```

Choisir un nom de projet Compose unique pour chaque exécution concurrente. Ce Compose ne partage ni base, ni volume média, ni clé de signature avec l'application. La suite complète conserve son JUnit et son journal dans un répertoire privé avant le nettoyage du seul projet de tests. Les tests de migration rétablissent le dernier schéma avant le nettoyage des fixtures.

Le script conserve la sélection complète déclarée dans le Compose avant d'ajouter l'unique désélection : passer seulement une option à `docker compose run tests` remplacerait sa commande au lieu de la compléter.

Les protocoles des autres gates sont décrits dans [le déploiement](collab_deployment.md), [les opérations](collab_operational_checks.md) et [le benchmark](collab_performance.md). Les tests d'acceptation HTTP et navigateur doivent utiliser les mêmes comptes synthétiques et la même instance. Les sessions et CSRF sont réels dans ces parcours ; les fixtures unitaires utilisent le hasher rapide prévu par le projet.

La livraison fonctionnelle est validée dans le périmètre demandé. Performance et plugins sont hors périmètre sur demande de l'utilisateur ; aucun objectif chiffré de performance n'est déclaré atteint. Les variantes natives et interactions non parcourues exhaustivement restent des limites de validation explicites, sans défaut fonctionnel ouvert identifié.
