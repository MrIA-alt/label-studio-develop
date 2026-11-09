# Mesure de performance du fork collaboratif

Le benchmark mesure les réponses HTTP d'une instance Docker isolée. Il ne démontre aucun objectif avant son exécution. Le seuil initial est **p95 strictement inférieur à 500 ms**, sans erreur, pour les deux lectures simples : politique d'un projet et tâche native affectée.

## Jeu de données et protocole

- Par défaut : **50 identités annotateur distinctes**, **100 000 tâches sources**, deux projets avec membres explicitement autorisés, mode aveugle et affectation manuelle.
- Chaque tâche possède `overlap=3` et trois affectations à des utilisateurs distincts, soit 300 000 affectations. En mode réduit, N vaut `min(3, --users)`. Aucune annotation n'est créée.
- Chaque exécution crée une organisation et des noms de fixture avec UUID uniques. Elle ne modifie et ne supprime aucune donnée préexistante. Les tâches et affectations sont créées par lots ORM ; les métadonnées de sources sont initialisées explicitement. Ce chargement est une fixture, pas une mesure d'import ou de distribution.
- Chaque client se connecte réellement par `/user/login/`, reçoit ses cookies de session/CSRF, puis vérifie son identité par HTTP. Aucun `force_authenticate` n'est utilisé. Les mots de passe aléatoires et cookies ne sont ni affichés ni enregistrés dans le résultat.
- Après un appel de chauffe par client et par endpoint, 50 clients commencent chaque phase ensemble. Chaque client effectue par défaut 20 lectures de politique, puis 20 lectures de tâches qui lui sont affectées. Les projets et tâches échantillonnées alternent. Les mesures de chauffe restent visibles séparément dans `warmup`, sans entrer dans le seuil des phases mesurées.
- Les durées couvrent la requête HTTP jusqu'à réception du corps, hors décodage JSON et validation du contenu. La génération de fixtures, le hachage des mots de passe, la connexion et la chauffe sont exclus. Les erreurs HTTP, réseau et de contenu sont comptées séparément et font échouer le seuil.

## Exécution

Utiliser exclusivement une instance de développement jetable. Le module refuse de démarrer sans `COLLAB_ACCEPTANCE_TESTS=1`. Le processus doit partager la base configurée de l'instance HTTP visée ; la vérification des identités échoue si ce n'est pas le cas.

Depuis le dépôt, dans l'environnement Docker dédié déjà démarré :

```sh
docker compose --env-file /tmp/codex-labelstudio-collab/env \
  -f docker-compose.collab.yml exec \
  -w /label-studio/label_studio -e COLLAB_ACCEPTANCE_TESTS=1 app \
  python -m collab.tests.benchmark_client \
  --base-url http://nginx:8085 --tasks 100000 --users 50 --requests 20
```

Une première vérification réduite évite de générer immédiatement le volume complet :

```sh
docker compose --env-file /tmp/codex-labelstudio-collab/env \
  -f docker-compose.collab.yml exec \
  -w /label-studio/label_studio -e COLLAB_ACCEPTANCE_TESTS=1 app \
  python -m collab.tests.benchmark_client \
  --base-url http://nginx:8085 --tasks 30 --users 3 --requests 5
```

`--requests` indique le nombre d'appels **par client et par endpoint**. `--timeout` vaut 45 secondes par défaut. `--threshold-ms` permet une exploration avec un autre seuil, mais un seuil supérieur à 500 ne valide jamais l'objectif d'acceptation.

Les résultats JSON sont conservés dans le volume persistant à `/label-studio/data/collab-benchmarks/<UUID>.json`. `--output` accepte un autre nouveau fichier ; un fichier existant est refusé. La console affiche son chemin et les résultats agrégés. Les snapshots intermédiaires permettent de distinguer un jeu de données créé d'une mesure terminée. Les données de fixture restent disponibles ; aucun nettoyage automatique n'est exécuté.

Le code de sortie vaut 0 si les deux mesures respectent le seuil choisi sans erreur, 1 si une mesure échoue, 2 si la préparation/connexion échoue. Un test réduit peut renvoyer 0 tout en ayant `profile: "smoke"` et `acceptance_goal_met: false` : seule une exécution d'au moins 50 clients et 100 000 tâches peut valider l'objectif complet.

## Lecture et comparaison des résultats

Le JSON contient les p50/p95/max des succès, les durées incluant les erreurs, les statuts HTTP, les catégories d'erreur, le débit, les échantillons bruts anonymes et la taille effective du jeu de données. Il indique les versions applicative/Python/Django, les empreintes SHA-256 des fichiers backend concernés, le backend PostgreSQL, les paramètres uWSGI non secrets et les limites cgroup visibles depuis le client conteneurisé. Il ne contient aucune identité, adresse email, donnée source, URL de base, valeur de secret ou cookie.

Le client s'exécute ici dans le conteneur applicatif et partage ses ressources : noter cette contention dans toute conclusion. Les lectures utilisent des sources texte courtes sans annotation ; elles ne mesurent ni un téléchargement d'image volumineuse, ni la comparaison d'une tâche avec de nombreuses versions, ni le temps de rendu React. Le nombre de tâches déjà présentes avant la fixture est enregistré : les exécutions successives ajoutent des données et ne représentent pas une base vierge identique.

Si p95 dépasse 500 ms, conserver le JSON avant de modifier les paramètres, puis mesurer une seule modification à la fois :

1. **Erreurs ou 429 :** résoudre d'abord authentification, affectations ou saturation du quota. Une réponse rapide en erreur ne constitue pas un gain de performance.
2. **Politique rapide, tâche lente :** profiler la sérialisation native et les requêtes par tâche ; examiner les calculs FSM, les préchargements et les accès répétés à la politique, aux membres et à l'affectation. Ajouter les index seulement après un `EXPLAIN (ANALYZE, BUFFERS)` sur les requêtes réellement lentes.
3. **Les deux endpoints lents sous concurrence :** comparer CPU, file d'attente uWSGI et connexions PostgreSQL. Tester un nombre de workers adapté aux CPU/RAM disponibles, puis refaire exactement le même profil. Le réglage de deux workers du Compose de développement n'est pas une garantie de capacité à 50 clients.
4. **Chauffe ou première lecture dominante :** séparer les mesures à froid et à chaud et conserver les deux ; ne pas supprimer les échecs ou échantillons lents du calcul.

La progression et la file des désaccords utilisent le cache scalaire `Task.precomputed_agreement` ; les métriques détaillées ne sont calculées que pour les tâches affichées. Après migration de données historiques, exécuter régulièrement `python manage.py collab_maintenance` pour remplir les valeurs manquantes par lots. Une nouvelle implémentation de métrique nécessite un recalcul explicite, par exemple `collab_maintenance --recompute-agreement --agreement-limit 100000` sur la base de développement isolée. Ce benchmark ne mesure pas encore ces endpoints agrégés : ils doivent faire l'objet d'une mesure séparée avant toute conclusion sur leur p95.
