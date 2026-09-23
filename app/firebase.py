from datetime import UTC, datetime
from functools import lru_cache

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.config import get_settings
from app.record_metadata import missing_metadata


@lru_cache
def firebase_app():
    settings = get_settings()
    cred = (
        credentials.Certificate(settings.firebase_credentials_path)
        if settings.firebase_credentials_path
        else credentials.ApplicationDefault()
    )
    # Named app avoids interfering with other Admin SDK users in the process.
    return firebase_admin.initialize_app(
        cred, {"projectId": settings.firebase_project_id}, name="rsvp-backend"
    )


class FirestoreUnit:
    def __init__(self, client, transaction):
        self.client, self.transaction = client, transaction

    def get(self, path):
        return self.client.document(path).get(transaction=self.transaction).to_dict()

    def set(self, path, data):
        self.transaction.set(self.client.document(path), data)


class FirestoreStore:
    def __init__(self, client):
        self.client = client

    def atomic(self, operation):
        @firestore.transactional
        def execute(transaction):
            return operation(FirestoreUnit(self.client, transaction))

        return execute(self.client.transaction())

    def pending_jobs(self, now):
        # A single range/order key avoids a composite index; completed jobs are filtered in Python.
        query = (
            self.client.collection("rsvpOutbox")
            .where(filter=FieldFilter("nextAttemptAt", "<=", now))
            .order_by("nextAttemptAt")
            .limit(50)
        )
        return [doc.to_dict() for doc in query.stream() if doc.to_dict()["state"] == "pending"]

    def known_email(self, email, event_id):
        from app.service import digest

        for path in (
            f"events/{event_id}/emailOwners/{digest(email)}",
            f"attendeeProfiles/{digest(email)}",
        ):
            if self.client.document(path).get().exists:
                return True
        for collection in ("users", "rsvp_guests", "pendingRegistrations"):
            query = (
                self.client.collection(collection)
                .where(filter=FieldFilter("email", "==", email))
                .limit(1)
            )
            if next(query.stream(), None) is not None:
                return True
        return False

    def legacy_profile(self, email):
        from app.legacy import map_legacy_profile
        from app.service import digest

        profile = self._verified_legacy_record(
            self.client.document(f"attendeeProfiles/{digest(email)}"), email
        )
        if profile:
            return map_legacy_profile(profile)
        query = (
            self.client.collection("rsvp_guests")
            .where(filter=FieldFilter("email", "==", email))
            .limit(2)
        )
        matches = list(query.stream())
        if len(matches) == 1:
            data = self._verified_legacy_record(matches[0].reference, email)
            return map_legacy_profile(data) if data else None
        return None

    def _verified_legacy_record(self, reference, email):
        # Called only when /me has already verified ownership of this exact email.
        @firestore.transactional
        def read(transaction):
            snapshot = reference.get(transaction=transaction)
            data = snapshot.to_dict()
            if data is None:
                return None
            patch = missing_metadata(data, snapshot.create_time, snapshot.update_time, email=email)
            if patch.get("email"):
                now = datetime.now(UTC)
                patch.update(modifiedAt=now, updatedAt=now)
            if patch:
                transaction.update(reference, patch)
            return {**data, **patch}

        return read(self.client.transaction())


@lru_cache
def get_store():
    return FirestoreStore(
        firestore.client(app=firebase_app(), database_id=get_settings().firebase_database_id)
    )
