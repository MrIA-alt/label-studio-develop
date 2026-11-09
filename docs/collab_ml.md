# Backends ML et comptes de service

La configuration d'une destination ML, son entraînement et son diagnostic relèvent des administrateurs humains. Les scopes génériques de projet ne délèguent pas ces actions : un compte disposant de `projects:write`, même accompagné de `results:read`, reçoit HTTP 403 avant tout appel externe. Les superviseurs continuent de consulter les prédictions autorisées ; les annotateurs utilisent les prédictions interactives selon leur affectation et la politique du projet.

La découverte des backends par l'éditeur renvoie aux annotateurs, superviseurs et comptes de service lecteurs uniquement `id`, `title`, `is_interactive`, `state` et `model_version`, sans appel externe ni mise à jour d'état. Les adresses et paramètres du backend restent administratifs. La consultation d'un projet archivé utilise son état ML enregistré sans contacter le backend.

L'appel natif `setup` conserve le projet, le schéma, l'adresse de Label Studio et les paramètres supplémentaires. Il ne transmet plus automatiquement le jeton personnel du propriétaire. Un backend qui récupère des médias privés doit être configuré explicitement, côté backend, avec un compte de service limité aux projets nécessaires et aux scopes `tasks:read` et, si nécessaire, `projects:read`. Son client HTTP peut utiliser `Authorization: Service <jeton>`. Aucun identifiant n'est incorporé dans une URL ou un exemple de configuration versionné.

Les identifiants de source configurés par l'administrateur restent transmis au backend approuvé pour son accès aux sources. Le diagnostic HTTP ne les renvoie pas dans les paramètres affichés au navigateur. Les annotations et données de tâche passent par les sérialiseurs et les contrôles d'accès de l'appelant.

Pour l'inscription, seul le premier propriétaire d'une organisation initiale reçoit le rôle administrateur. Toute inscription suivante, avec invitation ou en inscription ouverte, crée un annotateur. Les administrateurs attribuent ensuite les projets et, si nécessaire, changent son rôle par l'API de membres. `DISABLE_SIGNUP_WITHOUT_LINK=true` impose les invitations après le provisionnement initial.
