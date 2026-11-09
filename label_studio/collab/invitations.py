"""Persistent email invitations. Credentials only exist in the outbound message."""

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.utils.module_loading import import_string
from django.views.decorators.debug import sensitive_variables
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import ValidationError
from users.models import User

from collab import audit, services
from collab.lifecycle import ensure_project_writable
from collab.models import ProjectInvitation


def digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def smtp_ready():
    if settings.EMAIL_BACKEND == 'django.core.mail.backends.locmem.EmailBackend':
        return True  # Explicit test transport, never logs the activation link.
    if settings.EMAIL_BACKEND == 'collab.microsoft_mail.Microsoft365Backend':
        return all((settings.M365_TENANT_ID, settings.M365_CLIENT_ID, settings.M365_CLIENT_SECRET, settings.EMAIL_HOST_USER))
    try:
        return issubclass(import_string(settings.EMAIL_BACKEND), SMTPBackend)
    except (ImportError, AttributeError, TypeError):
        return False


def _account(email, organization):
    from collab.permissions import get_service_account
    users = list(User.objects.filter(email__iexact=email)[:2])
    if len(users) > 1 or (users and not users[0].is_active):
        raise ValidationError('This address cannot be invited.')
    user = users[0] if users else None
    if user and get_service_account(user) is not None:
        raise ValidationError('This address cannot be invited.')
    member = OrganizationMember.objects.filter(user=user, organization=organization).order_by('pk').first() if user else None
    if member and (member.deleted_at or member.role != OrganizationMember.Role.LABELLER):
        raise ValidationError('Only active labeller accounts can receive annotation invitations.')
    return user, member


@transaction.atomic
def reserve(project, emails, task_ids, actor):
    project = services.lock_project(project.pk)
    ensure_project_writable(project)
    if not hasattr(project, 'annotation_policy') or project.annotation_policy.assignment_mode != 'manual':
        raise ValidationError('Email pre-assignment requires a manual annotation policy.')
    tasks = list(project.tasks.filter(pk__in=task_ids))
    if len(tasks) != len(set(task_ids)):
        raise ValidationError('Every task must belong to this project.')
    cap = project.annotation_policy.max_active_tasks_per_user
    if cap and len(set(task_ids)) > cap:
        raise ValidationError('The reservation exceeds the active task limit per annotator.')
    # Preserve implicit legacy membership when introducing explicit members.
    if not ProjectMember.objects.filter(project=project).exists():
        ids = OrganizationMember.objects.filter(
            organization=project.organization, deleted_at__isnull=True, user__is_active=True,
        ).values_list('user_id', flat=True).distinct()
        ProjectMember.objects.bulk_create([ProjectMember(project=project, user_id=pk) for pk in ids])
    rows = []
    for email in emails:
        user, member = _account(email, project.organization)
        pm = ProjectMember.objects.filter(project=project, user=user).first() if user else None
        if pm and not pm.enabled:
            raise ValidationError('A disabled project membership must be restored before inviting this account.')
        row = ProjectInvitation.objects.filter(project=project, email=email, status='pending').first()
        if row and row.expires_at <= timezone.now():
            row.status = 'cancelled'
            row.token_digest = ''
            row.save(update_fields=['status', 'token_digest'])
            row = None
        if row is None:
            row = ProjectInvitation.objects.create(
                project=project, email=email, invited_user=user, created_by=actor,
                had_org_membership=member is not None, had_project_membership=pm is not None,
                expires_at=timezone.now() + timedelta(days=7),
            )
        previous = set(row.tasks.values_list('pk', flat=True))
        if cap and len(previous | set(task_ids)) > cap:
            raise ValidationError('The reservation exceeds the active task limit per annotator.')
        row.tasks.add(*tasks)
        if previous and not set(task_ids).issubset(previous):
            row.delivery_status = 'pending'
            row.attempts = 0
            row.next_attempt_at = timezone.now()
            row.save(update_fields=['delivery_status', 'attempts', 'next_attempt_at'])
        audit.record(actor, 'invitation.reserve', project, 'invitation', row.pk, tasks=len(task_ids))
        rows.append(row)
    return rows


@sensitive_variables()
def drain_invitations(limit=5):
    sent = failed = 0
    ids = ProjectInvitation.objects.filter(
        status='pending', expires_at__gt=timezone.now(), next_attempt_at__lte=timezone.now(),
        delivery_status__in=['pending', 'failed', 'blocked'], attempts__lt=5,
        project__annotation_policy__is_archived=False,
    ).values_list('pk', flat=True)[:limit]
    for pk in list(ids):
        with transaction.atomic():
            row = ProjectInvitation.objects.select_for_update(of=('self',)).select_related('project').get(pk=pk)
            if (row.status != 'pending' or row.delivery_status == 'sent' or row.expires_at <= timezone.now()
                    or row.next_attempt_at > timezone.now() or row.attempts >= 5):
                continue
            origin = settings.HOSTNAME.rstrip('/')
            valid_origin = urlsplit(origin).scheme in ('http', 'https') and bool(urlsplit(origin).netloc)
            if not smtp_ready() or not valid_origin:
                row.delivery_status = 'blocked'
                row.error_code = 'smtp_not_configured' if not smtp_ready() else 'public_url_not_configured'
                row.next_attempt_at = timezone.now() + timedelta(minutes=1)
            else:
                token = secrets.token_urlsafe(32)
                path = reverse('collab:invitation-accept')
                prefix = urlsplit(origin).path.rstrip('/')
                if prefix and path.startswith(prefix + '/'):
                    path = path[len(prefix):]
                link = origin + path + '#token=' + token
                try:
                    count = send_mail(
                        'Invitation à une campagne d’annotation',
                        f'Vous êtes invité à annoter le projet « {row.project.title} » avec l’adresse {row.email}.\n\n'
                        f'Activez votre accès : {link}\n\nCe lien personnel expire le {row.expires_at:%Y-%m-%d %H:%M UTC}.\n'
                        'Si vous avez déjà un compte, connectez-vous à ce compte avant d’accepter.\n',
                        settings.DEFAULT_FROM_EMAIL, [row.email], fail_silently=False,
                    )
                    if count != 1:
                        raise RuntimeError('Mail not delivered')
                except Exception:
                    # Never persist SMTP exception text (it may contain credentials).
                    row.delivery_status = 'failed'
                    row.error_code = 'smtp_error'
                    failed += 1
                else:
                    row.token_digest = digest(token)
                    row.delivery_status = 'sent'
                    row.error_code = ''
                    sent += 1
                row.attempts += 1
                row.next_attempt_at = timezone.now() + timedelta(seconds=min(3600, 60 * 2 ** row.attempts))
            row.save(update_fields=['token_digest', 'delivery_status', 'error_code', 'attempts', 'next_attempt_at'])
    return {'sent': sent, 'failed': failed}


@sensitive_variables('token', 'password', 'confirmation')
@transaction.atomic
def accept(token, current_user, password='', confirmation=''):
    candidate = ProjectInvitation.objects.filter(token_digest=digest(token)).first() if token else None
    if candidate is None:
        raise ValidationError('Invitation invalide, expirée ou déjà utilisée.')
    project = services.lock_project(candidate.project_id)
    ensure_project_writable(project)
    row = ProjectInvitation.objects.select_for_update().get(pk=candidate.pk)
    if row.status != 'pending' or row.expires_at <= timezone.now() or row.token_digest != digest(token):
        raise ValidationError('Invitation invalide, expirée ou déjà utilisée.')
    authorities = list(OrganizationMember.objects.filter(
        user_id=row.created_by_id, user__is_active=True, organization_id=project.organization_id,
        deleted_at__isnull=True,
    ).order_by('pk').values_list('role', flat=True)[:2])
    if authorities != [OrganizationMember.Role.ADMIN]:
        raise ValidationError('Cette invitation n’est plus autorisée. Contactez l’administrateur.')
    if not hasattr(project, 'annotation_policy') or project.annotation_policy.assignment_mode != 'manual':
        raise ValidationError('La politique de ce projet a changé. Contactez l’administrateur.')
    user, member = _account(row.email, project.organization)
    if row.invited_user_id and (user is None or user.pk != row.invited_user_id):
        raise ValidationError('Ce compte ne correspond plus à l’invitation.')
    if user:
        if not current_user.is_authenticated or current_user.pk != user.pk:
            raise ValidationError('Connectez-vous avec le compte invité avant d’accepter.')
    else:
        if current_user.is_authenticated:
            raise ValidationError('Déconnectez-vous avant de créer le compte invité.')
        if password != confirmation or len(password) < 12:
            raise ValidationError('Choisissez deux mots de passe identiques d’au moins 12 caractères.')
        try:
            validate_password(password, User(email=row.email))
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages)
        user = User.objects.create_user(row.email, password, username=row.email)
    pm = ProjectMember.objects.filter(project=project, user=user).first()
    if (row.had_org_membership and member is None) or (row.had_project_membership and pm is None) or (pm and not pm.enabled):
        raise ValidationError('Votre accès a été révoqué. Contactez l’administrateur.')
    if member is None:
        OrganizationMember.objects.create(organization=project.organization, user=user, role=OrganizationMember.Role.LABELLER)
    if pm is None:
        ProjectMember.objects.create(project=project, user=user, enabled=True)
    user.active_organization_id = project.organization_id
    user.save(update_fields=['active_organization'])
    tasks = list(row.tasks.all())
    if not tasks:
        raise ValidationError('Les tâches réservées ont été supprimées. Contactez l’administrateur.')
    if any(t.project_id != project.pk for t in tasks):
        raise ValidationError('Une tâche réservée ne fait plus partie du projet.')
    for task in tasks:
        services.assign_task(task, user, assigned_by=row.created_by)
    row.status = 'accepted'
    row.accepted_at = timezone.now()
    row.token_digest = ''
    row.save(update_fields=['status', 'accepted_at', 'token_digest'])
    audit.record(user, 'invitation.accept', project, 'invitation', row.pk, tasks=len(tasks))
    return user, project
