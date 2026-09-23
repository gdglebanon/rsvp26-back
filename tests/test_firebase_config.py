import json
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app import firebase
from app.config import Settings


@pytest.fixture
def firebase_setup(monkeypatch):
    firebase.firebase_app.cache_clear()
    settings = Settings(_env_file=None)
    certificate = Mock(return_value=object())
    initialize = Mock(return_value=object())
    monkeypatch.setattr(firebase, "get_settings", lambda: settings)
    monkeypatch.setattr(firebase.credentials, "Certificate", certificate)
    monkeypatch.setattr(firebase.firebase_admin, "initialize_app", initialize)
    yield settings, certificate, initialize
    firebase.firebase_app.cache_clear()


def test_hosted_credentials_work_without_a_local_key_file(firebase_setup):
    settings, certificate, initialize = firebase_setup
    # The hosted JSON must take precedence over a leftover development file path.
    settings.firebase_credentials_path = "/not-present-on-vercel/key.json"
    data = {"type": "service_account", "project_id": "test-project"}
    settings.firebase_credentials_json = SecretStr(json.dumps(data))
    result = firebase.firebase_app()
    certificate.assert_called_once_with(data)
    assert result is initialize.return_value
    assert firebase.firebase_app() is result
    assert initialize.call_count == 1


@pytest.mark.parametrize("value", ['{"private_key":"sensitive-value"', '["sensitive-value"]'])
def test_invalid_hosted_credentials_do_not_expose_secret_values(firebase_setup, value):
    settings, certificate, initialize = firebase_setup
    settings.firebase_credentials_json = SecretStr(value)
    with pytest.raises(ValueError) as error:
        firebase.firebase_app()
    assert "sensitive-value" not in str(error.value)
    certificate.assert_not_called()
    initialize.assert_not_called()


def test_development_key_file_still_works(firebase_setup):
    settings, certificate, _ = firebase_setup
    settings.firebase_credentials_path = "/local/key.json"
    firebase.firebase_app()
    certificate.assert_called_once_with("/local/key.json")
