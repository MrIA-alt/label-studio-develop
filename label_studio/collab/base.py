"""Base class of every /api/extensions/v1 view (kept apart from permissions.py to avoid a DRF settings import cycle)."""

from django.conf import settings
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView


class ExtensionThrottle(UserRateThrottle):
    scope = 'collab_extension'

    def get_rate(self):
        return getattr(settings, 'COLLAB_API_THROTTLE', '600/min')


class ExtensionView(APIView):
    """`required_scopes` maps HTTP methods to the scope demanded from service accounts (None = no scope needed)."""

    collab_extension = True
    required_scopes = {}
    throttle_classes = [ExtensionThrottle]

    def scope_for(self, method):
        return self.required_scopes.get(method)
