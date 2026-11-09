# Vérifier sauvegarde, restauration et redémarrage

Le pilote `label_studio/collab/tests/operational_client.py` s'exécute côté hôte Linux/WSL avec Python standard et Docker CLI. Il utilise uniquement le projet Compose isolé `label-studio-collab`. L'image applicative n'a pas besoin d'être reconstruite : le pilote transmet ses sondes Python au conteneur via l'entrée standard.

Exécuter d'abord le scénario HTTP `acceptance_client` et attendre sa réussite complète avec cent tâches. Suspendre les écritures de la campagne pendant la vérification de sauvegarde. Les bases de benchmark et les campagnes d'autres organisations peuvent rester présentes ; le dump sauvegarde la base complète, tandis que les empreintes de vérification portent sur la campagne synthétique d'acceptation.

## Transport webhook réel

Cette phase indépendante peut précéder la sauvegarde et ne redémarre aucun service :

```sh
COLLAB_ACCEPTANCE_TESTS=1 python3 label_studio/collab/tests/operational_client.py webhook-verify \
  --env-file /chemin/prive/env \
  --report /tmp/collab-operational-results/webhooks.json
```

Un récepteur HTTP temporaire, lié uniquement à `127.0.0.1` dans le processus de test, répond d'abord 503 puis 200. Le pilote vérifie réellement les signatures HMAC, timestamps, corps et identifiants de livraison, le délai de reprise, l'état `pending` puis `sent` et les deux tentatives. Il commence par vérifier que la politique par défaut refuse cette destination privée avant tout appel réseau.

La dérogation `COLLAB_WEBHOOK_ALLOW_PRIVATE=True` est limitée au processus synthétique pendant les deux requêtes ; la configuration de l'application reste inchangée. Le secret de signature n'est pas imprimé et est remplacé avant validation de la transaction. Les lignes de la fixture restent invisibles au worker jusqu'à leur validation, après désactivation de l'abonnement. La phase exige qu'aucun autre abonnement collaboratif actif ne soit présent afin de ne pas traiter des livraisons tierces. Elle vérifie le transport et les reprises réels sur loopback, sans prétendre exercer un service distant ni la chaîne TLS d'une destination HTTPS externe.

## Sauvegarde et restauration dans une copie

Depuis la racine du dépôt, avec le fichier privé du déploiement isolé :

```sh
mkdir -m 700 /tmp/collab-operational-results
COLLAB_ACCEPTANCE_TESTS=1 python3 label_studio/collab/tests/operational_client.py backup-restore \
  --env-file /chemin/prive/env \
  --report /tmp/collab-operational-results/backup-restore.json
```

Cette phase n'arrête aucun service. Elle crée un marqueur privé dans `app-data` et une livraison webhook volontairement inactive, sans appel vers une destination externe. Elle vérifie une session administrateur réelle, puis produit un dump PostgreSQL et une archive du volume `app-data`. Les sauvegardes contiennent des secrets et des données : le répertoire est privé, les fichiers ont le mode `0600`, et leur contenu n'est jamais imprimé.

La restauration utilise exclusivement une nouvelle base `collab_restore_<UUID>`, sans `--clean`, sans remplacement de la base `collab`, et un nouveau répertoire sous `/tmp` pour les fichiers. Le pilote vérifie explicitement le nom de la connexion Django de la copie. Il compare les compteurs et empreintes des utilisateurs, rôles, accès aux projets, politiques, affectations, annotations, décisions, versions, arbitrages, comptes de service et scopes, reçus d'idempotence, audit, états FSM et outbox. Les mots de passe de fixture sont vérifiés dans la copie sans être affichés. Les octets des fichiers téléversés et le marqueur du volume sont également comparés.

Une modification concurrente des données d'acceptation fait échouer la vérification ; elle ne déclenche aucune restauration de la base source. La base et les fichiers restaurés sont conservés pour inspection. Leur nom exact et les chemins des sauvegardes figurent dans le rapport privé. Aucun effacement de base n'est exécuté par le pilote.

## Redémarrage coordonné

Cette phase provoque une interruption de l'application. L'exécuter après la fin des tests ou sessions qui l'utilisent :

```sh
COLLAB_ACCEPTANCE_TESTS=1 python3 label_studio/collab/tests/operational_client.py restart-verify \
  --env-file /chemin/prive/env \
  --checkpoint /tmp/collab-operational-results/backup-restore.json \
  --report /tmp/collab-operational-results/restart.json
```

Le pilote prend une nouvelle empreinte avant le redémarrage : des changements réalisés depuis la sauvegarde précédente sont donc conservés. Il redémarre uniquement `app` et `maintenance`, attend leurs healthchecks ainsi qu'un nouveau cycle effectif du worker, puis compare les données et le marqueur. Une session signée créée avant le redémarrage doit encore permettre de lire la tâche source et les résultats via Nginx. PostgreSQL n'est jamais arrêté.

## Interpréter les preuves

Seul un rapport JSON contenant `success: true`, les contrôles réussis et les dates de début/fin constitue une exécution validée. La sortie standard contient uniquement les noms des contrôles, l'identifiant du projet synthétique et le chemin du rapport. Un fichier existant n'est jamais écrasé.

Si la fixture n'utilise que des images `data:` sans téléversement, le contrôle des médias HTTP porte explicitement la valeur `not verified`; la sauvegarde/restauration et la persistance du marqueur `app-data` restent vérifiables. Avec des fichiers téléversés locaux dans la fixture, leurs octets doivent correspondre aux réponses HTTP authentifiées et aux fichiers restaurés. Cette procédure ne prétend pas valider la restauration d'un stockage S3 externe ni un arrêt brutal du serveur PostgreSQL.
