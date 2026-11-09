# Déployer et vérifier le fork collaboratif

Cette configuration utilise PostgreSQL, Nginx, une migration contrôlée avant démarrage et un worker de maintenance. Aucun service SaaS n'est nécessaire. Les plugins JavaScript ne font pas partie de cette livraison.

## Démarrage Docker Compose

Créer un fichier `.env.collab` privé, ignoré par Git. Le modèle `.env.collab.example` décrit les variables. Génération initiale possible avec Python, sans afficher les secrets :

```python
import os
import secrets

descriptor = os.open('.env.collab', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
    output.write('POSTGRE_PASSWORD=' + secrets.token_urlsafe(32) + '\n')
    output.write('SECRET_KEY=' + secrets.token_urlsafe(64) + '\n')
    output.write('LABEL_STUDIO_HOST=http://localhost:18080\n')
```

```sh
docker compose --env-file .env.collab -f docker-compose.collab.yml up -d --build
docker compose --env-file .env.collab -f docker-compose.collab.yml ps
```

L'application de développement est accessible sur `http://localhost:18080`. Les ports sont liés à l'interface locale. Pour une installation exposée sur un serveur, terminer HTTPS au reverse proxy et définir `LABEL_STUDIO_HOST` avec l'origine HTTPS correspondante. Conserver la même `SECRET_KEY` entre les redémarrages.

Le fichier optionnel `docker-compose.collab.tls.yml` active directement le support TLS natif de Nginx sur le port local 18443. Fournir un répertoire privé externe contenant `cert.pem` (certificat et chaîne) et `key.pem` (clé privée), lisibles par l'utilisateur 1001 du conteneur sans rendre la clé publique. Définir `COLLAB_TLS_DIR` avec ce chemin absolu et `LABEL_STUDIO_HOST=https://<hôte>:18443` dans le fichier d'environnement, puis démarrer avec les deux fichiers Compose :

```sh
docker compose --env-file .env.collab \
  -f docker-compose.collab.yml -f docker-compose.collab.tls.yml up -d
```

Utiliser un certificat reconnu par les clients de l'installation. Une autorité privée peut être fournie explicitement aux outils de vérification avec `curl --cacert /chemin/ca.pem https://<hôte>:18443/health/`. Les identifiants, certificats et clés réels restent hors Git. Le port HTTP du fichier de base reste lié à l'interface locale pour les healthchecks et le diagnostic ; une exposition externe doit passer par la terminaison HTTPS choisie.

Le service `migrate` termine avant le démarrage de l'application. `maintenance` expire les affectations, redistribue les projets automatiques/hybrides, complète le cache d'accord et traite l'outbox des webhooks toutes les trente secondes. Ses opérations sont reprenables après redémarrage ; le travail et les tentatives sont conservés dans PostgreSQL.

Les healthchecks vérifient PostgreSQL, l'application, Nginx et l'activité du worker. Pour examiner une erreur :

```sh
docker compose --env-file .env.collab -f docker-compose.collab.yml logs --tail=100 app maintenance
```

## Parcours utilisateurs

Une carte projet ouvre l'espace **Campaign**. **My tasks** affiche les seules affectations personnelles et les retours de revue. L'action **Annotate** démarre l'affectation puis ouvre le moteur natif : texte, image, brouillons, sauvegarde et raccourcis sont conservés. Une soumission sous revue manuelle est verrouillée jusqu'à une demande de correction. Un conflit de version conserve les modifications à l'écran et demande de recharger avant une nouvelle sauvegarde.

**Supervision** présente progression, désaccords, résultats acceptés/rejetés, comparaison côte à côte, historique, actions groupées et arbitrage avec les outils natifs. Un rejet ou une demande de correction nécessite un motif. Les annotations originales restent distinctes de l'annotation finale.

**Administration** configure la politique, les membres, la distribution, la publication et les comptes de service. Les nouveaux projets créés par API sont privés par défaut : deux annotations, mode aveugle, affectation manuelle et revue manuelle. Les projets historiques sans politique conservent leur fonctionnement jusqu'à activation explicite du workflow collaboratif.

La pré-affectation par adresses mail et les invitations automatiques sont décrites dans [le guide des invitations](collab_invitations.md), avec les paramètres SMTP classiques et Microsoft 365 OAuth2. Le worker effectue les envois ; sans configuration mail, l’interface indique que les invitations restent en attente.

Les comptes de service reçoivent un jeton affiché une seule fois, indépendant des anciens jetons Label Studio. La liste de projets est explicite : une liste vide n'autorise aucun projet existant ; `all_projects=true` doit être accordé explicitement par un administrateur. La création autorisée d'un nouveau projet ajoute ce projet à la portée du compte. Voir [le contrat API](collab_api.md).

Seul le premier propriétaire initialise le rôle administrateur ; les inscriptions suivantes créent des annotateurs. La configuration des destinations ML et leur diagnostic restent administratifs. Le backend ML ne reçoit pas le jeton du propriétaire. Voir [la configuration ML et les inscriptions](collab_ml.md).

## Sauvegarde, restauration et mise à jour

Sauvegarder ensemble PostgreSQL, le volume `app-data` (médias et données persistées), ainsi que le fichier d'environnement conservé dans un emplacement privé. Une sauvegarde PostgreSQL peut être créée dans le conteneur puis copiée :

```sh
docker compose --env-file .env.collab -f docker-compose.collab.yml exec db pg_dump -U collab -d collab -Fc -f /tmp/collab.dump
docker compose --env-file .env.collab -f docker-compose.collab.yml cp db:/tmp/collab.dump ./collab.dump
```

Pour restaurer, arrêter les écritures (`app` et `maintenance`), restaurer le dump avec `pg_restore` dans une base dédiée, restaurer le volume des médias puis exécuter `migrate` avec la configuration restaurée. Vérifier d'abord cette procédure dans un environnement séparé ; ne pas appliquer une restauration sur une base en cours d'utilisation.

Pour mettre à jour : sauvegarder, reconstruire l'image, arrêter `app` et `maintenance`, exécuter `docker compose ... run --rm migrate`, puis redémarrer les services. Les migrations ajoutent leurs structures ; les projets, résultats et rôles historiques sont préservés. Les anciens webhooks natifs de projets aveugles sans approbation explicite transmettent uniquement des identifiants jusqu'à réapprobation administrative.

## Tests reproductibles

Les tests utilisent un environnement distinct. Ajouter `COLLAB_TEST_DEPS=true` dans le fichier d'environnement de construction pour inclure les dépendances de test. La suite Python s'exécute ensuite avec `docker-compose.collab.test.yml`, sans passer le fichier d'environnement applicatif : base PostgreSQL, stockage temporaire et identifiants sont entièrement jetables. Cela évite qu'une erreur d'un ancien test affiche les secrets de l'application dans son journal.

```sh
docker compose -f docker-compose.collab.test.yml up --abort-on-container-exit --exit-code-from tests
mkdir -p collab-test-results/backend
docker compose -f docker-compose.collab.test.yml cp tests:/tmp/collab-test-results.xml ./collab-test-results/backend/results.xml
docker compose -f docker-compose.collab.test.yml down --volumes

docker compose --env-file .env.collab -f docker-compose.collab.yml exec -T -w /label-studio/label_studio -e COLLAB_ACCEPTANCE_TESTS=1 app python -m collab.tests.acceptance_client --base-url http://nginx:8085
```

Copier le rapport JUnit même si la suite échoue, puis arrêter ce projet Compose de tests uniquement. `COLLAB_TEST_IMAGE` permet de choisir un digest d'image immuable. La commande `down` ci-dessus cible le projet `label-studio-collab-tests`, qui ne partage aucun volume avec l'application.

Le scénario HTTP crée uniquement des comptes et données synthétiques dans l'instance de développement isolée : un agent crée un projet, importe cent tâches texte/image, affecte trois annotateurs et publie. Trois sessions réelles soumettent simultanément ; les contrôles vérifient confidentialité, brouillons, rejet privé, correction versionnée, arbitrage, export autorisé, scopes et révocation. Les identifiants de fixture nécessaires aux tests navigateur sont enregistrés avec permissions privées dans `BASE_DATA_DIR/collab-acceptance-credentials.json`, sans être imprimés.

Le pilote hôte de [vérification opérationnelle](collab_operational_checks.md) sauvegarde la base et `app-data`, restaure dans une base et un répertoire jetables, puis vérifie séparément la persistance après un redémarrage coordonné de l'application et du worker. Il ne restaure jamais la base principale.

Les tests de concurrence nécessitent PostgreSQL. La compatibilité des migrations est exercée par `test_migration_compatibility.py`. Le benchmark de cinquante utilisateurs et cent mille tâches est décrit dans [collab_performance.md](collab_performance.md). Les résultats effectivement obtenus et les vérifications restantes sont consignés dans le rapport de validation de la livraison.
