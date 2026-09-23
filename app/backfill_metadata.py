"""Preview: python -m app.backfill_metadata; apply with --apply.

Only fills missing tracking fields. No personal answers, auth flags, statuses,
document IDs or ticket versions are changed. Logs counts, never email addresses.
"""

import argparse
import json
from collections import Counter, defaultdict

from firebase_admin import auth
from google.api_core.exceptions import FailedPrecondition, NotFound

from app.config import get_settings
from app.firebase import firebase_app, get_store
from app.record_metadata import missing_metadata
from app.service import digest

PERSON_COLLECTIONS = (
    "attendeeProfiles",
    "rsvp_guests",
    "users",
    "pendingRegistrations",
    "rsvpOutbox",
    "rsvpOtp",
)
SYSTEM_COLLECTIONS = ("rsvpRateLimits", "rsvpLocks")
EVENT_COLLECTIONS = ("tickets", "emailOwners", "actions")


def normalized_email(value):
    return value.strip().lower() if isinstance(value, str) and "@" in value else None


def email_index(records, accounts):
    by_hash, by_uid, by_ticket = defaultdict(set), defaultdict(set), defaultdict(set)

    def remember(email, uid=None):
        email = normalized_email(email)
        if email:
            by_hash[digest(email)].add(email)
            if uid:
                by_uid[uid].add(email)
        return email

    for account in accounts:
        remember(account.email, account.uid)
    for snapshot in records:
        data = snapshot.to_dict()
        remember(data.get("email"), data.get("uid"))
    for snapshot in records:
        if snapshot.reference.parent.id == "tickets":
            data = snapshot.to_dict()
            email = normalized_email(data.get("email"))
            matches = {email} if email else by_uid.get(data.get("uid"), set())
            by_ticket[(snapshot.reference.parent.parent.id, snapshot.id)].update(matches)
    return by_hash, by_uid, by_ticket


def resolve_email(snapshot, indexes):
    by_hash, by_uid, by_ticket = indexes
    data = snapshot.to_dict()
    if email := normalized_email(data.get("email")):
        return email
    collection = snapshot.reference.parent.id
    matches = set()
    if collection in {"attendeeProfiles", "emailOwners"}:
        matches.update(by_hash.get(snapshot.id, set()))
    matches.update(by_uid.get(data.get("uid"), set()))
    if ticket_id := data.get("ticketId"):
        event_id = data.get("eventId")
        if collection == "actions":
            event_id = snapshot.reference.parent.parent.id
        matches.update(by_ticket.get((event_id, ticket_id), set()))
    # Ambiguous identities require an operator to resolve them, never a guess.
    return next(iter(matches)) if len(matches) == 1 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the additive metadata patches")
    args = parser.parse_args()
    settings = get_settings()
    db = get_store().client
    records = []
    for collection in (*PERSON_COLLECTIONS, *SYSTEM_COLLECTIONS):
        records.extend(db.collection(collection).stream())
    # list_documents includes parent placeholders whose subcollections exist.
    for event in db.collection("events").list_documents():
        for collection in EVENT_COLLECTIONS:
            records.extend(event.collection(collection).stream())
    indexes = email_index(records, auth.list_users(app=firebase_app()).iterate_all())
    reports = defaultdict(Counter)
    for snapshot in records:
        collection = snapshot.reference.parent.id
        data = snapshot.to_dict()
        personal = collection not in SYSTEM_COLLECTIONS
        email = resolve_email(snapshot, indexes) if personal else None
        patch = missing_metadata(
            data, snapshot.create_time, snapshot.update_time, email=email, include_email=personal
        )
        report = reports[collection]
        report["records"] += 1
        if personal:
            report["withEmail" if email else "emailUnknown"] += 1
        if not patch:
            continue
        report["patches"] += 1
        if args.apply:
            try:
                snapshot.reference.update(
                    patch, option=db.write_option(last_update_time=snapshot.update_time)
                )
                report["updated"] += 1
            except (FailedPrecondition, NotFound):
                # Leave concurrent writes/deletions intact; the next run can retry.
                report["concurrentlyChanged"] += 1
    print(
        json.dumps(
            {
                "project": settings.firebase_project_id,
                "database": settings.firebase_database_id,
                "mode": "apply" if args.apply else "preview",
                "collections": reports,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
