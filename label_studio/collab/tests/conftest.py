import pytest


@pytest.fixture(autouse=True)
def fast_test_password_hasher(settings):
    # Match the native test suite. HTTP acceptance uses the application's real
    # password configuration; unit fixtures need no costly password derivation.
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
