from datetime import UTC, datetime
from types import SimpleNamespace

from app.config import Settings
from app.integrations import SHEET_HEADERS, IntegrationNotConfigured, Mailer, SheetSync


def test_sheet_updates_same_ticket_row_using_raw_values(monkeypatch):
    from app import integrations

    calls = []

    class Session:
        def __init__(self, credentials):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"values": [["ticketId"], ["another"], ["target"]]},
            )

        def put(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(integrations, "AuthorizedSession", Session)
    monkeypatch.setattr(integrations.google.auth, "default", lambda **kwargs: ("cred", "project"))
    settings = Settings(
        _env_file=None, google_sheet_id="test-sheet", firebase_credentials_path=None
    )
    SheetSync(settings).upsert(
        {
            "email": "a@example.com",
            "profile": {
                "firstName": "=malicious formula",
                "lastName": "Name",
                "specialization": "Backend",
                "company": "Example",
                "university": "",
            },
        },
        {
            "id": "target",
            "answers": {"attendanceType": "full_day"},
            "ticketType": "STANDARD",
            "status": "submitted",
            "updatedAt": datetime(2026, 10, 1, tzinfo=UTC),
            "version": 2,
        },
    )
    assert len(calls) == 1
    assert calls[0][0].endswith("A3%3AL3")
    assert calls[0][1]["params"] == {"valueInputOption": "RAW"}
    assert calls[0][1]["json"]["values"][0][2] == "=malicious formula"
    assert SHEET_HEADERS[12] == "action"


def test_unconfigured_event_email_is_not_reported_sent():
    import pytest

    mailer = Mailer(Settings(_env_file=None, mail_provider="disabled"))
    with pytest.raises(IntegrationNotConfigured):
        mailer.send("a@example.com", "test", "test")
