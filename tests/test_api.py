from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import current_identity
from app.config import Settings
from app.main import app, get_service, get_settings
from app.models import Identity
from app.service import RegistrationService, digest
from app.worker import OutboxWorker


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.lock = RLock()
        self.legacy_calls = []

    def atomic(self, operation):
        with self.lock:
            staged = deepcopy(self.records)

            class Unit:
                written = False

                def get(self, path):
                    assert not self.written, "Firestore requires all reads before writes"
                    return deepcopy(staged.get(path))

                def set(self, path, data):
                    self.written = True
                    staged[path] = deepcopy(data)

            result = operation(Unit())
            self.records = staged
            return result

    def pending_jobs(self, now):
        return sorted(
            [
                deepcopy(value)
                for key, value in self.records.items()
                if key.startswith("rsvpOutbox/")
                and value["state"] == "pending"
                and value["nextAttemptAt"] <= now
            ],
            key=lambda value: value["nextAttemptAt"],
        )

    def legacy_profile(self, email):
        self.legacy_calls.append(email)
        return {"firstName": "Legacy"}


@pytest.fixture
def env():
    now = [datetime(2026, 10, 1, tzinfo=UTC)]
    settings = Settings(
        _env_file=None,
        registration_deadline="2026-12-31T23:59:59Z",
        ticket_signing_key=SecretStr("test-signing-key-" * 4),
        sheets_webhook_secret=SecretStr("test-webhook-key-" * 4),
    )
    store = MemoryStore()
    service = RegistrationService(store, settings, lambda: now[0])
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, store, service, now
    app.dependency_overrides.clear()


@pytest.fixture
def payload():
    return {
        "email": "attendee@example.com",
        "form": {
            "email": "attendee@example.com",
            "firstName": "Alex",
            "lastName": "Attendee",
            "linkedIn": "https://www.linkedin.com/in/alex",
            "phone": "+961 71 123456",
            "region": "Beirut",
            "specialization": "Backend Developer",
            "activeExpCategories": ["Professional"],
            "expLevels": {"Student": 0, "Professional": 2, "Manager / Team Lead": 0},
            "status": "student",
            "company": "Example",
            "university": "",
            "referral": "Friends / Colleagues",
            "attendedBefore": 3,
            "takeaways": ["Networking"],
            "techInterests": ["Cloud"],
            "attendanceType": "full_day",
            "secretCode": "",
        },
    }


def login(email="attendee@example.com", uid="user-1", organizer=False):
    app.dependency_overrides[current_identity] = lambda: Identity(
        uid=uid, email=email, organizer=organizer
    )


def submit(client, payload):
    login()
    response = client.post("/api/register", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def invite(client, ticket, request_id="request-invite-12345"):
    login(organizer=True)
    response = client.post(
        "/api/admin/curate",
        json={
            "ticketId": ticket["id"],
            "version": ticket["version"],
            "action": "invite",
            "requestId": request_id,
        },
    )
    assert response.status_code == 200, response.text
    login()
    return response.json()


def test_separate_profiles_tickets_and_retries(env, payload):
    client, store, _, _ = env
    ticket = submit(client, payload)
    assert ticket["status"] == "submitted"
    assert ticket["ticketType"] == "STANDARD"
    assert "firstName" not in ticket["answers"]
    assert ticket["email"] == payload["email"]
    user = store.records[f"users/{digest('user-1')}"]
    assert user["profile"]["status"] == "professional"
    assert "attendanceType" not in user["profile"]
    assert "secretCode" not in repr(store.records)
    assert client.post("/api/register", json=payload).json()["version"] == 1
    assert len([key for key in store.records if "/tickets/" in key]) == 1
    assert len([key for key in store.records if key.startswith("rsvpOutbox/")]) == 2


def test_edits_update_existing_profile_ticket_and_preserve_confirmation(env, payload):
    client, _, _, _ = env
    ticket = submit(client, payload)
    payload["form"]["firstName"] = "Updated"
    response = client.put("/api/registration", json={**payload, "version": ticket["version"]})
    assert response.status_code == 200
    assert response.json()["id"] == ticket["id"]
    assert response.json()["version"] == 2
    assert client.get("/api/me").json()["profile"]["firstName"] == "Updated"
    assert client.put("/api/registration", json={**payload, "version": 1}).status_code == 409
    ticket = invite(client, response.json())
    token = client.get("/api/invitations/current").json()["token"]
    confirmed = client.post("/api/invitations/confirm", json={"token": token}).json()
    response = client.put("/api/registration", json={**payload, "version": confirmed["version"]})
    assert response.json()["status"] == "confirmed"


def test_invitation_confirm_qr_and_cancellation(env, payload):
    client, _, service, _ = env
    ticket = submit(client, payload)
    assert client.get("/api/ticket/qr").status_code == 409
    assert client.get("/api/invitations/current").status_code == 404
    invited = invite(client, ticket)
    token = client.get("/api/invitations/current").json()["token"]
    assert client.post("/api/invitations/confirm", json={"token": "a" * 64}).status_code == 403
    confirmed = client.post("/api/invitations/confirm", json={"token": token}).json()
    assert confirmed["status"] == "confirmed"
    assert (
        client.post("/api/invitations/confirm", json={"token": token}).json()["version"]
        == confirmed["version"]
    )
    qr = client.get("/api/ticket/qr")
    assert qr.status_code == 200 and qr.content.startswith(b"\x89PNG")
    identity = Identity(uid="user-1", email=payload["email"])
    old_qr = service.qr_payload(service.ticket(identity))
    cancelled = client.post("/api/registration/cancel", json={"version": confirmed["version"]})
    assert cancelled.json()["status"] == "cancelled"
    assert client.get("/api/ticket/qr").status_code == 409
    assert client.post("/api/invitations/confirm", json={"token": token}).status_code == 403
    login(organizer=True)
    assert client.post("/api/admin/check-in", json={"qr": old_qr}).status_code == 403
    assert invited["invitationExpiresAt"]


def test_invitation_expires_at_exact_deadline_and_can_be_reissued(env, payload):
    client, _, _, now = env
    ticket = invite(client, submit(client, payload))
    token = client.get("/api/invitations/current").json()["token"]
    now[0] += timedelta(hours=48)
    assert client.get("/api/registration").json()["status"] == "expired"
    assert client.post("/api/invitations/confirm", json={"token": token}).status_code == 410
    assert client.get("/api/invitations/current").status_code == 410
    ticket = invite(client, ticket, request_id="request-invite-renewal")
    assert ticket["status"] == "invited"
    assert client.post("/api/invitations/confirm", json={"token": token}).status_code == 403


def test_vip_bypass_and_check_in(env, payload):
    client, store, service, _ = env
    service.settings.vip_code_hashes = [digest("vip-code")]
    payload["form"]["secretCode"] = "wrong-code"
    login()
    assert client.post("/api/register", json=payload).status_code == 403
    payload["form"]["secretCode"] = "vip-code"
    ticket = submit(client, payload)
    assert ticket["status"] == "confirmed" and ticket["ticketType"] == "VIP"
    assert "vip-code" not in repr(store.records)
    raw = service.ticket(Identity(uid="user-1", email=payload["email"]))
    login(organizer=True)
    qr = service.qr_payload(raw)
    first = client.post("/api/admin/check-in", json={"qr": qr})
    assert first.status_code == 200
    assert first.json()["alreadyCheckedIn"] is False
    assert client.post("/api/admin/check-in", json={"qr": qr}).json()["alreadyCheckedIn"] is True
    login()
    assert (
        client.put("/api/registration", json={**payload, "version": ticket["version"]}).status_code
        == 409
    )
    assert (
        client.post("/api/registration/cancel", json={"version": ticket["version"] + 1}).status_code
        == 409
    )


def test_identity_bound_privacy_and_unique_email(env, payload):
    client, _, _, _ = env
    submit(client, payload)
    login("someone-else@example.com", "someone-else")
    assert client.get("/api/registration").status_code == 404
    assert client.post("/api/register", json=payload).status_code == 403
    login(uid="different-uid")
    assert client.post("/api/register", json=payload).status_code == 409


def test_legacy_profile_access_only_after_verification(env):
    client, store, _, _ = env
    assert client.get("/api/me?email=attendee@example.com").status_code == 401
    assert store.legacy_calls == []
    login()
    response = client.get("/api/me?email=victim@example.com")
    assert response.json()["legacyProfileLoaded"] is True
    assert store.legacy_calls == ["attendee@example.com"]
    assert response.json()["ticket"] is None


def test_closed_registration_still_allows_existing_edits(env, payload):
    client, _, service, _ = env
    ticket = submit(client, payload)
    service.settings.force_close_registration = True
    assert (
        client.put("/api/registration", json={**payload, "version": ticket["version"]}).status_code
        == 200
    )
    login("new@example.com", "new-user")
    payload["email"] = payload["form"]["email"] = "new@example.com"
    assert client.post("/api/register", json=payload).status_code == 403


def test_rate_limiting(env, payload):
    client, _, service, _ = env
    service.settings.submissions_per_hour = 1
    ticket = submit(client, payload)
    assert (
        client.put("/api/registration", json={**payload, "version": ticket["version"]}).status_code
        == 429
    )


@pytest.mark.parametrize(
    "change",
    [
        {"linkedIn": "https://linkedin.com.evil.example/in/a"},
        {"firstName": " "},
        {"company": "", "university": ""},
        {"phone": "123"},
        {"expLevels": {"Professional": True}},
        {"expLevels": {"Professional": 8}},
        {"attendedBefore": 4},
        {"takeaways": []},
        {"activeExpCategories": ["Admin"]},
        {"activeExpCategories": ["Student"], "major": ""},
        {"attendanceType": "invalid"},
        {"email": "other@example.com"},
        {"comments": "x" * 5001},
    ],
)
def test_invalid_form_not_echoed(env, payload, change):
    client, _, _, _ = env
    login()
    payload["form"].update(change)
    payload["form"]["secretCode"] = "do-not-echo"
    response = client.post("/api/register", json=payload)
    assert response.status_code == 422
    assert "do-not-echo" not in response.text


def test_firebase_auth_verifies_revocation_and_email(env, monkeypatch, payload):
    from app import auth as module

    client, _, _, _ = env
    monkeypatch.setattr(module, "firebase_app", lambda: "firebase-app")
    claims = {"uid": "user-1", "email": payload["email"], "email_verified": False}

    def verify(token, **kwargs):
        assert token == "test-token"
        assert kwargs == {"app": "firebase-app", "check_revoked": True}
        return claims

    monkeypatch.setattr(module.auth, "verify_id_token", verify)
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/api/register", json=payload, headers=headers).status_code == 403
    claims["email_verified"] = True
    assert client.post("/api/register", json=payload, headers=headers).status_code == 200


def test_organizer_claim_required(env, payload):
    client, _, _, _ = env
    ticket = submit(client, payload)
    assert (
        client.post(
            "/api/admin/curate",
            json={
                "ticketId": ticket["id"],
                "action": "invite",
                "version": 1,
                "requestId": "test-organizer-123",
            },
        ).status_code
        == 403
    )


def test_signed_sheet_webhook_replay_and_tampering(env, payload):
    import hashlib
    import hmac
    import json

    client, _, service, now = env
    ticket = submit(client, payload)
    body = json.dumps(
        {
            "ticketId": ticket["id"],
            "action": "invite",
            "version": 1,
            "requestId": "webhook-request-123",
        }
    )
    stamp = str(int(now[0].timestamp()))
    sig = hmac.new(
        service.settings.sheets_webhook_secret.get_secret_value().encode(),
        f"{stamp}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-RSVP-Timestamp": stamp,
        "X-RSVP-Signature": sig,
    }
    first = client.post("/api/webhooks/sheets", content=body, headers=headers)
    assert first.status_code == 200
    assert client.post("/api/webhooks/sheets", content=body, headers=headers).json() == first.json()
    assert (
        client.post(
            "/api/webhooks/sheets", content=body.replace("invite", "reject"), headers=headers
        ).status_code
        == 401
    )
    now[0] += timedelta(minutes=6)
    assert client.post("/api/webhooks/sheets", content=body, headers=headers).status_code == 401


def test_outbox_retries_and_skips_stale_email(env, payload):
    client, store, service, now = env
    ticket = submit(client, payload)

    class Mail:
        def __init__(self):
            self.sent = []
            self.failing = True

        def send(self, *args):
            if self.failing:
                raise RuntimeError("provider failure")
            self.sent.append(args)

    class Sheets:
        def __init__(self):
            self.synced = []

        def upsert(self, user, ticket):
            self.synced.append((user, ticket))

    mail, sheets = Mail(), Sheets()
    worker = OutboxWorker(service, mail, sheets)
    assert worker.run_once()
    assert worker.run_once()
    jobs = [v for k, v in store.records.items() if k.startswith("rsvpOutbox/")]
    assert any(job.get("lastError") == "RuntimeError" for job in jobs)
    assert any(job["state"] == "done" for job in jobs)
    client.post("/api/registration/cancel", json={"version": ticket["version"]})
    now[0] += timedelta(hours=2)
    mail.failing = False
    while worker.run_once():
        pass
    assert len(mail.sent) == 1
    assert "cancelled" in mail.sent[0][2]
    assert sheets.synced[-1][1]["status"] == "cancelled"


def test_legacy_mapping_excludes_secrets_and_historical_status():
    from app.legacy import map_legacy_profile

    profile = map_legacy_profile(
        {
            "firstName": "Alex",
            "linkedin": "https://linkedin.com/in/a",
            "experience": "grad_student",
            "education": "Computer science",
            "region": "north",
            "gender": "male",
            "age": "18-23",
            "specialization": "frontend_developer",
            "OTP": "private",
            "selected": "TRUE",
            "status": "TRUE",
        }
    )
    assert profile["major"] == "Computer science"
    assert profile["activeExpCategories"] == ["Student"]
    assert profile["expLevels"]["Student"] == 2
    assert profile["region"] == "North"
    assert profile["status"] == "student"
    assert "OTP" not in profile and "selected" not in profile


def test_concurrent_creates_are_idempotent(env, payload):
    from concurrent.futures import ThreadPoolExecutor

    from app.models import RegistrationRequest

    _, store, service, _ = env
    body = RegistrationRequest.model_validate(payload)
    identity = Identity(uid="user-1", email=payload["email"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.submit(body, identity), range(10)))
    assert all(result["version"] == 1 for result in results)
    assert len([key for key in store.records if "/tickets/" in key]) == 1


def test_public_email_lookup_returns_presence_only_and_is_rate_limited(env):
    client, store, service, _ = env
    seen = []
    store.known_email = lambda email, event: (
        seen.append((email, event)) or email == "attendee@example.com"
    )
    response = client.post("/api/identity/lookup", json={"email": "Attendee@example.com"})
    assert response.json() == {"exists": True, "loginRequired": False}
    assert seen == [("attendee@example.com", service.settings.event_id)]
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/register", json={}).status_code == 401
    for _ in range(59):
        client.post("/api/identity/lookup", json={"email": "attendee@example.com"})
    assert (
        client.post("/api/identity/lookup", json={"email": "attendee@example.com"}).status_code
        == 429
    )


def test_otp_expiration_attempts_and_one_time_exchange(env):
    import re
    from types import SimpleNamespace

    from app.otp import OTPService

    _, store, service, now = env
    service.settings.mail_provider = "smtp"
    service.settings.mail_from = "event@example.com"

    class Mail:
        def __init__(self):
            self.body = ""

        def send(self, email, subject, body):
            self.body = body

    class Auth:
        UserNotFoundError = KeyError
        EmailAlreadyExistsError = ValueError

        def __init__(self):
            self.verified = False

        def get_user_by_email(self, email, app):
            return SimpleNamespace(uid="otp-user", disabled=False, email_verified=self.verified)

        def update_user(self, uid, email_verified, app):
            self.verified = email_verified

        def create_custom_token(self, uid, app):
            return b"custom-firebase-token"

    mail, auth = Mail(), Auth()
    otp = OTPService(service, mail, auth, lambda: None)
    challenge = otp.request("attendee@example.com", "127.0.0.1")["challengeId"]
    code = re.search(r"\b\d{6}\b", mail.body).group()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(HTTPException) as error:
            otp.verify(challenge, wrong)
        assert error.value.status_code == 401
    assert store.records[f"rsvpOtp/{challenge}"]["attempts"] == 5
    with pytest.raises(HTTPException):
        otp.verify(challenge, code)
    challenge = otp.request("attendee@example.com", "127.0.0.1")["challengeId"]
    code = re.search(r"\b\d{6}\b", mail.body).group()
    assert otp.verify(challenge, code) == {"customToken": "custom-firebase-token"}
    assert auth.verified
    with pytest.raises(HTTPException):
        otp.verify(challenge, code)
    challenge = otp.request("attendee@example.com", "127.0.0.1")["challengeId"]
    code = re.search(r"\b\d{6}\b", mail.body).group()
    now[0] += timedelta(minutes=10)
    with pytest.raises(HTTPException):
        otp.verify(challenge, code)


def test_otp_disabled_without_mail_provider(env):
    client, _, _, _ = env
    assert (
        client.post("/api/auth/otp/request", json={"email": "attendee@example.com"}).status_code
        == 503
    )
    assert client.get("/api/config").json()["otpAvailable"] is False


def test_unverified_submission_is_saved_with_server_controlled_flags(env, payload):
    client, store, _, _ = env
    body = {**payload, "submissionKey": "a" * 64, "emailVerified": True}
    response = client.post("/api/pending", json=body)
    assert response.status_code == 201
    receipt = response.json()
    assert receipt["emailVerified"] is False
    assert receipt["status"] == "pending_verification"
    assert "profile" not in receipt and "answers" not in receipt
    record = store.records[f"pendingRegistrations/{receipt['id']}"]
    assert record["emailVerified"] is False
    assert record["profile"]["firstName"] == "Alex"
    assert record["answers"]["attendanceType"] == "full_day"
    assert "secretCode" not in repr(store.records)
    assert not any(key.startswith("users/") or "/tickets/" in key for key in store.records)
    retry = client.post("/api/pending", json=body)
    assert retry.json()["id"] == receipt["id"]
    assert len([key for key in store.records if key.startswith("pendingRegistrations/")]) == 1


def test_saved_unverified_form_completes_after_matching_firebase_verification(env, payload):
    client, store, _, _ = env
    body = {**payload, "submissionKey": "b" * 64, "emailVerified": False}
    pending = client.post("/api/pending", json=body).json()
    assert client.post("/api/pending/complete", json={"id": pending["id"]}).status_code == 401
    login("someone-else@example.com", "other-user")
    assert client.post("/api/pending/complete", json={"id": pending["id"]}).status_code == 404
    assert store.records[f"pendingRegistrations/{pending['id']}"]["emailVerified"] is False
    login()
    response = client.post("/api/pending/complete", json={"id": pending["id"]})
    assert response.status_code == 200
    assert response.json()["emailVerified"] is True
    record = store.records[f"pendingRegistrations/{pending['id']}"]
    assert record["emailVerified"] is True and record["status"] == "completed"
    assert store.records[f"users/{digest('user-1')}"]["emailVerified"] is True
    assert client.get("/api/me").json()["user"]["emailVerified"] is True
    repeated = client.post("/api/pending/complete", json={"id": pending["id"]})
    assert repeated.json()["id"] == response.json()["id"]
    assert repeated.json()["version"] == 1
    assert client.post("/api/pending", json=body).status_code == 409


def test_anonymous_pending_form_cannot_overwrite_existing_registration(env, payload):
    client, _, _, _ = env
    untrusted = deepcopy(payload)
    untrusted["form"]["firstName"] = "Untrusted replacement"
    pending = client.post("/api/pending", json={**untrusted, "submissionKey": "c" * 64}).json()
    # A registration may be verified after an anonymous draft was saved.
    ticket = submit(client, payload)
    response = client.post("/api/pending/complete", json={"id": pending["id"]})
    assert response.json()["id"] == ticket["id"]
    assert client.get("/api/me").json()["profile"]["firstName"] == "Alex"


def test_unverified_vip_has_no_ticket_until_verified(env, payload):
    client, store, service, _ = env
    service.settings.vip_code_hashes = [digest("shared-vip")]
    payload["form"]["secretCode"] = "shared-vip"
    pending = client.post("/api/pending", json={**payload, "submissionKey": "d" * 64}).json()
    assert not any("/tickets/" in key for key in store.records)
    assert "shared-vip" not in repr(store.records)
    login()
    confirmed = client.post("/api/pending/complete", json={"id": pending["id"]})
    assert confirmed.status_code == 200
    assert confirmed.json()["ticketType"] == "VIP"
    assert confirmed.json()["status"] == "confirmed"


def test_unverified_submission_expiration_and_rate_limit(env, payload):
    client, _, _, now = env
    body = {**payload, "submissionKey": "e" * 64}
    pending = client.post("/api/pending", json=body).json()
    for _ in range(4):
        assert client.post("/api/pending", json=body).status_code == 201
    assert client.post("/api/pending", json=body).status_code == 429
    now[0] += timedelta(days=7)
    login()
    assert client.post("/api/pending/complete", json={"id": pending["id"]}).status_code == 410


def test_tracking_dates_preserve_creation_and_advance_on_edits_and_status_changes(env, payload):
    client, store, service, now = env
    ticket = submit(client, payload)
    ticket_path = service.ticket_path(ticket["id"])
    user_path = service.user_path("user-1")
    created = now[0]
    for record in store.records.values():
        assert record["createdAt"] == record["modifiedAt"] == record["updatedAt"] == created
    for path, record in store.records.items():
        if not path.startswith("rsvpRateLimits/"):
            assert record["email"] == payload["email"]

    now[0] += timedelta(minutes=1)
    assert client.post("/api/register", json=payload).status_code == 200
    assert store.records[ticket_path]["modifiedAt"] == created
    response = client.put("/api/registration", json={**payload, "version": 1})
    assert response.status_code == 200
    for path in (ticket_path, user_path):
        assert store.records[path]["createdAt"] == created
        assert store.records[path]["modifiedAt"] == store.records[path]["updatedAt"] == now[0]

    now[0] += timedelta(minutes=1)
    ticket = invite(client, response.json())
    assert store.records[ticket_path]["createdAt"] == created
    assert store.records[ticket_path]["modifiedAt"] == now[0]
    token = client.get("/api/invitations/current").json()["token"]
    now[0] += timedelta(minutes=1)
    assert client.post("/api/invitations/confirm", json={"token": token}).status_code == 200
    assert store.records[ticket_path]["modifiedAt"] == now[0]
    now[0] += timedelta(minutes=1)
    identity = Identity(uid="user-1", email=payload["email"])
    qr = service.qr_payload(service.ticket(identity))
    service.check_in(qr, "organizer")
    checked_in = deepcopy(store.records[ticket_path])
    assert checked_in["createdAt"] == created
    assert checked_in["modifiedAt"] == checked_in["updatedAt"] == now[0]
    now[0] += timedelta(minutes=1)
    service.check_in(qr, "organizer")
    assert store.records[ticket_path] == checked_in


def test_pending_verification_dates_and_completion_retry(env, payload):
    client, store, _, now = env
    created = now[0]
    pending = client.post("/api/pending", json={**payload, "submissionKey": "f" * 64}).json()
    path = f"pendingRegistrations/{pending['id']}"
    assert store.records[path]["createdAt"] == store.records[path]["modifiedAt"] == created
    now[0] += timedelta(minutes=3)
    login()
    assert client.post("/api/pending/complete", json={"id": pending["id"]}).status_code == 200
    completed = deepcopy(store.records[path])
    assert completed["createdAt"] == created
    assert completed["modifiedAt"] == completed["updatedAt"] == completed["completedAt"] == now[0]
    now[0] += timedelta(minutes=1)
    assert client.post("/api/pending/complete", json={"id": pending["id"]}).status_code == 200
    assert store.records[path] == completed


@pytest.mark.parametrize("record_type", ["profile", "unverified", "verified", "other_event"])
def test_registration_login_requires_verified_current_event_submission(env, payload, record_type):
    client, store, service, _ = env
    store.known_email = lambda email, event: True
    email = payload["email"]
    store.records[f"attendeeProfiles/{digest(email)}"] = {"email": email, "emailVerified": True}
    if record_type in ("verified", "other_event"):
        from app.models import RegistrationRequest
        service.submit(RegistrationRequest.model_validate(payload), Identity(uid="user-1", email=email))
        if record_type == "other_event":
            service.root = "events/another-event"
    body = {**payload, "submissionKey": "a" * 64}
    if record_type == "unverified":
        assert client.post("/api/pending", json=body).status_code == 201
    lookup = client.post("/api/identity/lookup", json={"email": email})
    assert lookup.json()["loginRequired"] is (record_type == "verified")
    response = client.post("/api/pending", json=body)
    if record_type == "verified":
        assert response.status_code == 409
        assert response.json()["detail"] == "Login is necessary to continue your registration."
        assert not any(key.startswith("pendingRegistrations/") for key in store.records)
    else:
        assert response.status_code == 201
