import logging

import pytest
from rest_framework.exceptions import APIException, ValidationError

from core.utils.common import custom_exception_handler


@pytest.mark.parametrize('exception', [RuntimeError('synthetic-private-token'), APIException('synthetic-private-token')])
def test_production_errors_do_not_return_or_log_exception_values(exception, settings, caplog, monkeypatch):
    settings.DEBUG = False
    settings.DEBUG_MODAL_EXCEPTIONS = True
    monkeypatch.setattr('core.utils.common.sentry_sdk_loaded', False)
    with caplog.at_level(logging.ERROR):
        response = custom_exception_handler(exception, {})
    assert response.status_code == 500
    assert response.data['code'] == 'internal_error'
    assert response.data['exc_info'] is None
    assert 'synthetic-private-token' not in str(response.data)
    assert 'synthetic-private-token' not in caplog.text
    assert str(response.data['id']) in caplog.text


def test_validation_errors_keep_actionable_fields(settings, monkeypatch):
    settings.DEBUG = False
    monkeypatch.setattr('core.utils.common.sentry_sdk_loaded', False)
    response = custom_exception_handler(ValidationError({'required_annotations': ['Must be at most 10.']}), {})
    assert response.status_code == 400
    assert response.data['validation_errors']['required_annotations'] == ['Must be at most 10.']
