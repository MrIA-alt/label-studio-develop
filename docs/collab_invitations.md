# Invitations par mail et pré-affectation

Dans **Campaign → Administration**, choisir le mode **Manual**, configurer le nombre d’annotations requises, sélectionner les tâches dans **Choose tasks**, puis saisir les adresses dans **Invite annotators by email**. Espaces, virgules et points-virgules séparent les adresses. Cette opération est réservée aux administrateurs humains.

Les tâches sources ne sont pas copiées. Une invitation conserve les identifiants réservés avant que son destinataire ait un compte. À l’activation, un nouvel utilisateur choisit son mot de passe et devient annotateur du projet ; un utilisateur existant doit d’abord se connecter au compte correspondant à l’adresse invitée. Aucun mot de passe existant n’est réinitialisé. Les affectations et leur éventuel délai d’expiration commencent à l’activation. La pré-affectation par mail exige le mode manuel pour éviter qu’une distribution automatique occupe les places réservées.

Le lien expire après sept jours, ne s’utilise qu’une fois et ne figure jamais dans une réponse de l’API d’administration. Le secret est transmis dans le fragment du lien, puis envoyé par formulaire protégé contre CSRF ; seul son hash est conservé dans la base. L’archivage, la révocation de l’invitation, du membre ou de son administrateur émetteur empêchent l’activation. Le rôle d’un compte existant n’est pas élevé.

## Envoi automatique

Le service `maintenance` doit rester actif. Il envoie cinq invitations par passage et retente les erreurs avec délai croissant, au maximum cinq tentatives. Les états `pending`, `sent`, `failed`, `blocked` sont visibles dans l’administration. `sent` signifie que le serveur SMTP a accepté le message ; une adresse invalide ou un filtrage antispam peut encore produire un rejet ultérieur. **Refresh invitation status** actualise l’affichage. **Resend email** renouvelle l’expiration et invalide le précédent lien. **Cancel invitation** libère la réservation.

Sans transport configuré, les invitations restent bloquées avec un message explicite. Le backend console est refusé pour ne pas écrire de liens d’activation dans les logs. Conserver les secrets dans le fichier d’environnement privé du serveur ; ne pas les ajouter au dépôt ou à l’image Docker.

Exemple SMTP classique, à adapter à votre serveur :

```dotenv
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.org
EMAIL_PORT=587
EMAIL_USE_TLS=true
EMAIL_USE_SSL=false
EMAIL_HOST_USER=annotations@example.org
EMAIL_HOST_PASSWORD=<secret-fourni-hors-git>
DEFAULT_FROM_EMAIL=annotations@example.org
```

Configurer `LABEL_STUDIO_HOST` avec l’URL réellement accessible aux annotateurs, en HTTPS pour une installation exposée. Après modification du fichier privé, recréer `app`, `nginx` et `maintenance` avec le Compose pour appliquer les variables.

## Microsoft 365 et compte dédié

Procédure vérifiée dans la documentation Microsoft le 9 novembre 2026, pour une boîte professionnelle **Microsoft 365 / Exchange Online**, par exemple `annotations@entreprise.fr`. Elle configure le backend OAuth2 déjà inclus dans l’image. Prévoir un administrateur disposant des droits Entra et Exchange nécessaires.

### 1. Enregistrer l’application dans Entra

Dans [Microsoft Entra](https://entra.microsoft.com), ouvrir **Entra ID → App registrations / Inscriptions d’applications → New registration / Nouvelle inscription**. Nommer l’application `Label Studio Invitations`, choisir les comptes de votre organisation uniquement, puis enregistrer. Relever **Directory (tenant) ID** et **Application (client) ID** dans Overview. Ce parcours serveur ne nécessite pas d’URI de redirection. Voir [l’inscription d’application Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app).

Dans **Certificates & secrets → Client secrets → New client secret**, créer un secret à courte durée pour l’essai provisoire. Conserver sa **Value / Valeur**, avec sa date d’expiration, dans un emplacement privé : le Secret ID ne permet pas l’authentification. La valeur n’est affichée qu’à la création. Le backend actuel utilise ce secret client ; la configuration par certificat n’est pas implémentée. Voir [la gestion des identifiants Entra](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-credentials).

### 2. Autoriser l’envoi et enregistrer l’identité Exchange

Dans **API permissions → Add a permission → APIs my organization uses → Office 365 Exchange Online → Application permissions**, ajouter `SMTP.SendAsApp`, puis faire accorder le consentement administrateur. Ensuite, dans **Enterprise applications / Applications d’entreprise**, ouvrir l’instance de cette application et relever son **Object ID**. Ce dernier diffère de l’Object ID affiché dans App registrations. La procédure suit le [flux SMTP avec identifiants d’application de Microsoft](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth#use-client-credentials-grant-flow-to-authenticate-smtp-imap-and-pop-connections).

L’administrateur exécute dans PowerShell, en remplaçant les quatre valeurs d’exemple :

```powershell
# Une fois, si le module n’est pas installé :
Install-Module ExchangeOnlineManagement -Scope CurrentUser
Import-Module ExchangeOnlineManagement

$mailTenant = 'TENANT-ID'
$mailClient = 'CLIENT-ID'
$mailEnterpriseObject = 'OBJECT-ID-APPLICATION-ENTREPRISE'
$mailSender = 'annotations@entreprise.fr'

Connect-ExchangeOnline -Organization $mailTenant

# Une fois par application ; récupérer l’entrée existante lors d’une reprise.
$mailApp = Get-ServicePrincipal | Where-Object { $_.AppId -eq $mailClient }
if (-not $mailApp) {
    New-ServicePrincipal -AppId $mailClient -ObjectId $mailEnterpriseObject -DisplayName 'Label Studio Invitations'
    $mailApp = Get-ServicePrincipal | Where-Object { $_.AppId -eq $mailClient }
}
if (@($mailApp).Count -ne 1) {
    throw 'Vérifier le principal Exchange avant de poursuivre.'
}

Add-MailboxPermission -Identity $mailSender -User $mailApp.Identity -AccessRights FullAccess
Add-RecipientPermission -Identity $mailSender -Trustee $mailApp.Identity -AccessRights SendAs
```

Ces droits ciblent la seule boîte d’expédition. Le droit **SendAs** est décrit dans [Add-RecipientPermission](https://learn.microsoft.com/en-us/powershell/module/exchangepowershell/add-recipientpermission?view=exchange-ps).

### 3. Activer SMTP AUTH pour cette boîte

L’administrateur vérifie et, si autorisé par la politique de son tenant, active SMTP AUTH sur la boîte :

```powershell
Get-CASMailbox -Identity $mailSender | Format-List SmtpClientAuthenticationDisabled
Set-CASMailbox -Identity $mailSender -SmtpClientAuthenticationDisabled $false
Get-CASMailbox -Identity $mailSender | Format-List SmtpClientAuthenticationDisabled
```

La valeur finale attendue est `False`. Dans le centre Microsoft 365, le même réglage est accessible par **Users → Active users → compte → Mail → Manage email apps → Authenticated SMTP**. Les politiques du tenant, notamment Security Defaults, peuvent bloquer SMTP AUTH : faire valider ce point par l’administrateur avant toute modification globale. Voir [les règles d’activation SMTP AUTH de Microsoft](https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission).

### 4. Configurer l’instance Label Studio

Ajouter les valeurs dans le **fichier d’environnement privé déjà utilisé par votre Compose**, en conservant les autres paramètres et secrets existants :

```dotenv
EMAIL_BACKEND=collab.microsoft_mail.Microsoft365Backend
EMAIL_HOST_USER=annotations@example.org
DEFAULT_FROM_EMAIL=annotations@example.org
M365_TENANT_ID=<identifiant-du-tenant>
M365_CLIENT_ID=<identifiant-de-application>
M365_CLIENT_SECRET='VALEUR-DU-SECRET-CLIENT'
LABEL_STUDIO_HOST=https://annotations.entreprise.fr
```

`EMAIL_HOST_USER` et `DEFAULT_FROM_EMAIL` doivent désigner la boîte autorisée à l’étape 2. `M365_CLIENT_SECRET` contient la valeur du secret de l’application Entra. Aucun mot de passe de la boîte n’est utilisé par ce backend. Ne pas transmettre cette valeur dans le chat, le dépôt ou une capture.

Sur l’instance locale lancée le 9 novembre, le fichier privé est `/tmp/codex-labelstudio-collab/env` **dans Ubuntu-22.04 / WSL**. Sur un autre serveur, utiliser le fichier privé de ce serveur. `LABEL_STUDIO_HOST` doit être l’URL accessible aux destinataires ; `http://localhost:18080` convient uniquement aux essais depuis ce même poste.

Depuis le dépôt, dans Ubuntu / WSL, appliquer les variables sans reconstruire l’image :

```sh
docker compose --env-file /tmp/codex-labelstudio-collab/env \
  -f docker-compose.collab.yml up -d --no-build --force-recreate app nginx maintenance
docker compose --env-file /tmp/codex-labelstudio-collab/env \
  -f docker-compose.collab.yml ps
```

Le backend impose `smtp.office365.com:587`, STARTTLS et SASL XOAUTH2, avec le scope `https://outlook.office365.com/.default`. Le serveur doit pouvoir joindre Microsoft en HTTPS sur 443 et en SMTP sur 587. Les erreurs publiques ne contiennent ni réponse OAuth brute ni secret.

### 5. Vérifier une invitation

Dans **Campaign → Administration**, choisir le mode Manual, sélectionner une tâche de test et inviter une adresse que vous contrôlez. Le worker traite l’envoi au passage suivant, normalement sous trente secondes hors reprises. Cliquer **Refresh invitation status** et vérifier `sent`, puis la réception du message, le lien d’activation et la tâche visible après activation.

`sent` confirme l’acceptation SMTP, pas la livraison finale. En cas d’absence, consulter les indésirables puis le suivi de message Exchange. `blocked` indique une configuration manquante ; `failed` avec `smtp_error` demande de vérifier secret/expiration, consentement, Object ID d’entreprise, droits sur la boîte, SMTP AUTH et accès réseau. Après correction, **Resend email** relance l’envoi et remplace le lien précédent.

L’envoi dans un tenant Microsoft réel doit être vérifié après configuration de votre application et de la boîte. Les tests locaux utilisent uniquement des comptes synthétiques, un serveur SMTP de boucle locale et un échange OAuth simulé.

## API

- `GET /api/extensions/v1/projects/{id}/invitations` : liste paginée et indicateur `mail_configured`, sans jeton.
- `POST /api/extensions/v1/projects/{id}/invitations` : `{"emails":["alice@example.org"],"task_ids":[42,43]}`. Les répétitions ne créent pas de réservation en double.
- `POST /api/extensions/v1/projects/{id}/invitations/{invitation_id}/resend` ou `/cancel` : corps vide.
- `/user/invitation/` : formulaire public d’activation à partir du lien personnel. Un POST avec cookie exige le jeton CSRF, comme les mutations d’invitations.

Le scope API d’un compte de service Label Studio ne lui accorde pas le droit d’inviter des personnes. Le compte expéditeur Microsoft est distinct de cette identité d’agent.

## Construction de la mise à jour

Le Dockerfile principal reste la recette de construction complète. Si ses téléchargements de dépendances sont indisponibles, `deploy/Dockerfile.collab-update` permet une mise à jour depuis l’image validée du 6 novembre, déjà chargée localement :

```sh
docker build -f deploy/Dockerfile.collab-update \
  --build-arg COLLAB_BASE_IMAGE=label-studio-collab:2026-11-06 \
  -t label-studio-collab:local .
```

Vérifier que la base correspond à `sha256:99d727d4c102f3f1f99d1fc05715604e0258eec3da577d37febe790f609b1360`. Cette recette reconstruit le frontend et remplace le code applicatif ; elle refuse de continuer si `pyproject.toml` ou `poetry.lock` diffèrent de la base. Elle ne télécharge pas de nouvelles dépendances Python et ne désactive pas les contrôles TLS. Exécuter la migration `collab.0006_projectinvitation` avant de redémarrer l’application et le worker. La configuration Microsoft et ses secrets restent externes à l’image.
