from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from test_api import MemoryStore

from app.config import Settings
from app.email_verification import EmailVerificationService
from app.service import RegistrationService


class FakeAuth:
    class UserNotFoundError(Exception):
        pass

    class EmailAlreadyExistsError(Exception):
        pass

    def __init__(self, existing=True, verified=False, disabled=False):
        self.user = SimpleNamespace(uid="user-1", email="a@example.com",
                                    email_verified=verified, disabled=disabled)
        self.existing = existing
        self.created = []
        self.tokens = []

    def get_user_by_email(self, email, **kwargs):
        if not self.existing:
            raise self.UserNotFoundError()
        return self.user

    def create_user(self, **kwargs):
        self.created.append(kwargs)
        self.existing = True
        return self.user

    def get_user(self, uid, **kwargs):
        assert uid == self.user.uid
        return self.user

    def create_custom_token(self, uid, **kwargs):
        self.tokens.append(uid)
        return b"private-custom-token"


@pytest.fixture
def setup():
    store = MemoryStore()
    service = RegistrationService(store, Settings(
        _env_file=None, firebase_web_api_key="test-key", frontend_url="https://rsvp.example/",
    ), lambda: datetime(2026, 10, 1, tzinfo=UTC))
    auth = FakeAuth()
    calls = []

    def post(url, **kwargs):
        action = url.rsplit(":", 1)[1]
        calls.append((action, kwargs["json"]))
        data = {
            "signInWithCustomToken": {"idToken": "private-id-token", "refreshToken": "private-refresh"},
            "sendOobCode": {"email": auth.user.email},
            "resetPassword": {"requestType": "VERIFY_EMAIL", "email": auth.user.email},
            "update": {"localId": auth.user.uid},
        }[action]
        if action == "update":
            auth.user.email_verified = True
        return httpx.Response(200, json=data, request=httpx.Request("POST", url))

    verifier = EmailVerificationService(service, auth, lambda: "app", post)
    return verifier, auth, calls, store


@pytest.mark.parametrize("existing,verified", [(False, False), (True, False), (True, True)])
def test_delivery_uses_standard_verification_and_never_exposes_session(setup, existing, verified):
    verifier, auth, calls, _ = setup
    auth.existing, auth.user.email_verified = existing, verified
    result = verifier.request("a@example.com", "a" * 64, "ip")
    assert result == {"sent": True}
    assert [action for action, _ in calls] == ["signInWithCustomToken", "sendOobCode"]
    delivery = calls[1][1]
    assert delivery["requestType"] == "VERIFY_EMAIL"
    assert delivery["idToken"] == "private-id-token"
    assert delivery["continueUrl"] == "https://rsvp.example/?auth=callback&pending=" + "a" * 64
    assert auth.user.email_verified == verified
    assert all("password" not in entry for entry in auth.created)


def test_completion_requires_code_and_issues_session_only_after_consuming_proof(setup):
    verifier, auth, calls, store = setup
    result = verifier.complete("one-time-code", "ip")
    assert result == {"email": "a@example.com", "customToken": "private-custom-token"}
    assert [action for action, _ in calls] == ["resetPassword", "update"]
    assert auth.tokens == ["user-1"]
    assert any(path.startswith("rsvpEmailVerificationProofs/") for path in store.records)
    with pytest.raises(HTTPException, match="already been used"):
        verifier.complete("one-time-code", "ip")
    assert len(auth.tokens) == 1


def test_other_email_action_types_cannot_issue_sessions(setup):
    verifier, auth, _, _ = setup
    verifier.firebase_request = lambda *args: {"requestType": "PASSWORD_RESET", "email": "a@example.com"}
    with pytest.raises(HTTPException, match="email address verification"):
        verifier.complete("reset-code", "ip")
    assert auth.tokens == []


@pytest.mark.parametrize("disabled,mismatch", [(True, False), (False, True)])
def test_disabled_or_mismatched_identity_cannot_issue_session(setup, disabled, mismatch):
    verifier, auth, _, _ = setup
    auth.user.disabled = disabled
    def request(action, body):
        if action == "resetPassword":
            return {"requestType": "VERIFY_EMAIL", "email": "other@example.com" if mismatch else "a@example.com"}
        auth.user.email_verified = True
        return {"localId": "user-1"}
    verifier.firebase_request = request
    with pytest.raises(HTTPException, match="could not be confirmed"):
        verifier.complete("code", "ip")
    assert auth.tokens == []


def test_failed_code_consumption_never_issues_session(setup):
    verifier, auth, _, _ = setup
    def request(action, body):
        if action == "resetPassword":
            return {"requestType": "VERIFY_EMAIL", "email": "a@example.com"}
        raise HTTPException(400, "Expired code")
    verifier.firebase_request = request
    with pytest.raises(HTTPException, match="Expired"):
        verifier.complete("expired", "ip")
    assert auth.tokens == []


def test_firebase_failures_do_not_expose_tokens_or_request_urls(setup):
    verifier, _, _, _ = setup
    def post(url, **kwargs):
        return httpx.Response(400, json={"error": "private details"}, request=httpx.Request("POST", url))
    verifier.post = post
    with pytest.raises(HTTPException) as error:
        verifier.complete("secret-code", "ip")
    assert error.value.status_code == 400
    assert "secret-code" not in error.value.detail
    assert "private" not in error.value.detail


def test_delivery_rate_limit_applies_before_minting_more_sessions(setup):
    verifier, auth, _, _ = setup
    for _ in range(5):
        verifier.request("a@example.com", None, "ip")
    with pytest.raises(HTTPException) as error:
        verifier.request("a@example.com", None, "ip")
    assert error.value.status_code == 429
    assert len(auth.tokens) == 5
