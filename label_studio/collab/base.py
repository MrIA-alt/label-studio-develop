"""Base class of every /api/extensions/v1 view (kept apart from permissions.py to avoid a DRF settings import cycle)."""

from django.conf import settings
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView


class ExtensionThrottle(UserRateThrottle):
    scope = 'collab_extension'

    def get_rate(self):
        return getattr(settings, 'COLLAB_API_THROTTLE', '600/min')


class ExtensionView(APIView):
    """Machine identities need the declared action scope; undeclared actions fail closed."""

    collab_extension = True
    required_scopes = {}
    throttle_classes = [ExtensionThrottle]

    def scope_for(self, method):
        return self.required_scopes.get('GET' if method == 'HEAD' else method)

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if response.status_code >= 500:
            response.data = {'code': 'internal_error', 'detail': 'Internal server error.', 'errors': None}
            return response
        original = response.data
        if isinstance(original, dict) and isinstance(original.get('detail'), str):
            detail = original['detail']
        else:
            detail = 'Request validation failed.' if response.status_code == 400 else 'Request failed.'
        response.data = {'code': getattr(exc, 'default_code', 'error'), 'detail': detail, 'errors': original}
        return response
