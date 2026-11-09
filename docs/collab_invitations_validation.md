# Validation des invitations — 9 novembre 2026

Image lancée : `label-studio-collab:2026-11-09`, identifiant immuable `sha256:8fe8f047cea232d5378c802c14d3ae2fea5ca4dd5019be338c72c9376977a499`, Linux amd64.

La nouvelle fonctionnalité permet à un administrateur de réserver des tâches à des adresses mail, de suivre les envois, de renouveler ou révoquer les invitations. Un nouvel utilisateur choisit son mot de passe à l’activation ; un utilisateur existant doit se connecter au compte invité. Les tâches sont affectées à l’activation. Le mode manuel est requis. Voir [la configuration SMTP et Microsoft 365](collab_invitations.md).

## Preuves et provenance

| Contrôle | Résultat |
|---|---|
| Suite fonctionnelle PostgreSQL sur l’image intermédiaire `e458bec…` | 648 réussis, 55 ignorés, 1 désélectionné, sortie 0, 715,79 s. La sélection historique de non-régression a été rejouée avec les invitations ; `test_read_performance.py` et le seuil `test_upload_duration` sont exclus conformément au périmètre demandé. |
| Tests PostgreSQL ciblés sur l’image finale `8fe8f04…` | 31 réussis, zéro échec/ignoré, 69,74 s : invitations, concurrence et compatibilité des migrations. Ils incluent la correction du dialogue EHLO après STARTTLS et la politique de référent du formulaire. |
| Interface React | 19 réussis, cinq suites : Administration, CSRF des invitations et autres composants de campagne. |
| Migration | `makemigrations --check --dry-run` : sortie 0 sur l’image finale ; migration `collab.0006_projectinvitation` appliquée sur l’instance locale. |
| OpenAPI | `spectacular --validate` : sortie 0 sur l’image finale. |
| HTTP et SMTP réels isolés sur l’image finale | Réservation via session administrateur et CSRF, publication du projet, deux mails acceptés par un serveur SMTP local via le worker périodique ; compte neuf absent avant activation. Aucune adresse réelle ni infrastructure Microsoft utilisée. |
| Navigateur sur l’image finale, sans remplacement de code | Trois réussis, zéro échec/ignoré, huit secondes : nouveau compte et tâche réservée ; mauvais compte refusé puis déconnexion/connexion au compte existant avec conservation du lien ; invitation depuis l’interface administrateur avec CSRF. |
| Provenance des sources | Les onze fichiers applicatifs/tests sélectionnés, dont le service d’invitation, le backend Microsoft, la vue et le template d’activation, sont identiques octet pour octet au workspace dans l’image finale. |
| Mise à jour locale | Sauvegarde PostgreSQL créée et archive reconnue par `pg_restore --list` avant migration. PostgreSQL, application, Nginx et worker sains après démarrage ; `/health/` et `/user/invitation/` renvoient HTTP 200. |
| Préservation des données | Comptages inchangés après mise à jour : 9 projets, 81 utilisateurs, 100 333 tâches, 43 annotations et 300 996 affectations. Ce contrôle ne remplace pas une comparaison octet par octet de la base. |

La suite complète de 648 cas n’est pas présentée comme une nouvelle exécution sur l’image finale : les changements ultérieurs ont été vérifiés par les tests ciblés et le parcours navigateur. Les résultats des exécutions ne sont pas additionnés.

Les premiers essais navigateur ont révélé un refus CSRF : `no-referrer` pouvait produire un en-tête `Origin: null` lors d’une soumission de formulaire. La page utilise désormais `same-origin`, garde les contrôles CSRF et ne transmet pas de référent aux sites externes. Le lien d’invitation reste dans un fragment, puis dans le formulaire ; le secret n’est pas ajouté à la chaîne de requête ni à une réponse API administrative.

Le téléchargement de l’installateur Poetry dans la recette complète a rencontré un certificat non reconnu. La construction a réutilisé les dépendances Python validées de l’image du 6 novembre, avec égalité obligatoire de `pyproject.toml` et `poetry.lock`, puis reconstruit le frontend et le code. Une couche finale ajoute la correction de formulaire et son test. Aucun contrôle TLS n’a été désactivé.

## Limites

L’envoi Microsoft 365 est **non vérifié dans un tenant réel**. Le dialogue OAuth/XOAUTH2 et le traitement des erreurs sont testés avec un fournisseur simulé ; l’activation, les droits Exchange et la délivrance effective nécessitent la configuration du compte provisoire. Sur l’instance locale, le backend mail reste désactivé tant que cette configuration n’est pas fournie.

Les essais utilisent des volumes et identifiants synthétiques distincts de la campagne de démonstration. Le helper SMTP est copié dans `/tmp` de l’instance jetable ; aucun fichier applicatif de l’image finale n’est remplacé pendant la dernière acceptation. Les secrets de test et liens reçus ne sont pas inclus dans ce rapport. Les plugins JavaScript et le travail de performance restent hors périmètre.

L’archive portable du 6 novembre reste inchangée : elle ne contient pas les invitations. Cette mise à jour est disponible comme image Docker locale datée ci-dessus.
