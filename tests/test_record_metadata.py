from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.backfill_metadata import email_index, resolve_email
from app.record_metadata import missing_metadata
from app.service import digest


def test_backfill_preserves_data_and_uses_original_document_dates():
    created = datetime(2020, 1, 1, tzinfo=UTC)
    modified = created + timedelta(days=5)
    data = {"firstName": "Alex", "updatedAt": created, "emailVerified": False}
    patch = missing_metadata(data, created, modified, email="alex@example.com")
    assert patch == {"createdAt": created, "modifiedAt": modified, "email": "alex@example.com"}
    assert data == {"firstName": "Alex", "updatedAt": created, "emailVerified": False}
    assert missing_metadata({**data, **patch}, created, modified + timedelta(days=1)) == {}


def test_unknown_email_is_visible_as_null_and_can_be_filled_later():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    data = missing_metadata({}, now, now)
    assert "email" in data and data["email"] is None
    assert missing_metadata(data, now, now) == {}
    assert missing_metadata(data, now, now, email="alex@example.com") == {
        "email": "alex@example.com"
    }
    data["email"] = "existing@example.com"
    assert missing_metadata(data, now, now, email="different@example.com") == {}


def snapshot(collection, doc_id, data, event="devfest-test"):
    return SimpleNamespace(
        id=doc_id,
        reference=SimpleNamespace(
            parent=SimpleNamespace(id=collection, parent=SimpleNamespace(id=event))
        ),
        to_dict=lambda: data,
    )


def test_email_backfill_matches_exact_hashes_and_ticket_owners_only():
    email = "alex@example.com"
    profile = snapshot("attendeeProfiles", digest(email), {"firstName": "Alex"})
    unrelated = snapshot("attendeeProfiles", "unknown-hash", {"firstName": "Alex"})
    ticket = snapshot("tickets", "ticket-id", {"uid": "user-id"})
    job = snapshot("rsvpOutbox", "job-id", {"ticketId": "ticket-id", "eventId": "devfest-test"})
    accounts = [SimpleNamespace(uid="user-id", email=email)]
    indexes = email_index([profile, unrelated, ticket, job], accounts)
    assert resolve_email(profile, indexes) == email
    assert resolve_email(ticket, indexes) == email
    assert resolve_email(job, indexes) == email
    assert resolve_email(unrelated, indexes) is None
    # A changed account email is ambiguous for a historical ticket: don't guess.
    user = snapshot("users", "user-hash", {"uid": "user-id", "email": "other@example.com"})
    indexes = email_index([user, ticket, job], accounts)
    assert resolve_email(ticket, indexes) is None
    assert resolve_email(job, indexes) is None
