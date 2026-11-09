"""Hashed service tokens, independent of organization legacy/JWT token settings."""

import hashlib
import secrets

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed


def token_digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def issue_service_token(account):
    from rest_framework.authtoken.models import Token

    token = 'ls_svc_' + secrets.token_urlsafe(48)
    account.token_digest = token_digest(token)
    account.save(update_fields=['token_digest'])
    # A migrated legacy credential must not survive rotation through the fallback authenticator.
    Token.objects.filter(user_id=account.user_id).delete()
    return token


class ServiceTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() not in (b'service', b'token', b'bearer'):
            return None
        if len(parts) != 2:
            if parts[0].lower() == b'service':
                raise AuthenticationFailed('Invalid service token header.')
            return None
        try:
            token = parts[1].decode('ascii')
        except UnicodeError:
            raise AuthenticationFailed('Invalid service token.')
        from collab.models import ServiceAccount

        account = ServiceAccount.objects.select_related('user').filter(token_digest=token_digest(token)).first()
        if account is None:
            if parts[0].lower() == b'service' or token.startswith('ls_svc_'):
                raise AuthenticationFailed('Invalid service token.')
            return None
        if not account.active or not account.user.is_active:
            raise AuthenticationFailed('Service account is inactive.')
        from organizations.models import OrganizationMember

        if not OrganizationMember.objects.filter(
            user=account.user, organization_id=account.user.active_organization_id, deleted_at__isnull=True
        ).exists():
            raise AuthenticationFailed('Service account has no active organization membership.')
        from core.current_request import CurrentContext

        CurrentContext.set_user(account.user)
        return account.user, account

    def authenticate_header(self, request):
        return 'Service'


class ServiceTokenScheme(OpenApiAuthenticationExtension):
    target_class = 'collab.authentication.ServiceTokenAuthentication'
    name = 'ServiceToken'

    def get_security_definition(self, auto_schema):
        return {'type': 'apiKey', 'in': 'header', 'name': 'Authorization', 'description': 'Service ls_svc_<token>'}
