"""Microsoft 365 SMTP OAuth2 client credentials, with no interactive login."""

import re

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.smtp import EmailBackend
from django.views.decorators.debug import sensitive_variables


class Microsoft365Backend(EmailBackend):
    def __init__(self, *args, **kwargs):
        # Django must not attempt basic authentication with the mailbox password.
        kwargs.update(host='smtp.office365.com', port=587, username='', password='', use_tls=True, use_ssl=False)
        super().__init__(*args, **kwargs)

    @sensitive_variables()
    def open(self):
        if self.connection:
            return False
        tenant = settings.M365_TENANT_ID
        if not re.fullmatch(r'[0-9a-fA-F-]{36}', tenant) or not all((
            settings.M365_CLIENT_ID, settings.M365_CLIENT_SECRET, settings.EMAIL_HOST_USER,
        )):
            raise ImproperlyConfigured('Microsoft 365 mail configuration is incomplete.')
        try:
            response = requests.post(
                f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token',
                data={'client_id': settings.M365_CLIENT_ID, 'client_secret': settings.M365_CLIENT_SECRET,
                      'grant_type': 'client_credentials', 'scope': 'https://outlook.office365.com/.default'},
                timeout=10, allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError('Token request failed')
            token = response.json()['access_token']
            if not isinstance(token, str) or not token or any(c in token for c in ('\x01', '\r', '\n')):
                raise ValueError('Invalid access token')
            mailbox = settings.EMAIL_HOST_USER
            if any(c in mailbox for c in ('\x01', '\r', '\n')):
                raise ValueError('Invalid mailbox')
            opened = super().open()
            if not self.connection:
                raise ValueError('SMTP connection failed')
            # STARTTLS resets the SMTP greeting. Unlike login(), auth() does
            # not send EHLO itself; Exchange requires it before AUTH XOAUTH2.
            self.connection.ehlo_or_helo_if_needed()
            self.connection.auth('XOAUTH2', lambda challenge=None: (
                f'user={mailbox}\x01auth=Bearer {token}\x01\x01' if challenge is None else ''
            ))
            return opened
        except Exception:
            self.close()
            if not self.fail_silently:
                raise ImproperlyConfigured('Microsoft 365 mail authentication failed. Check application and mailbox permissions.') from None
            return False
