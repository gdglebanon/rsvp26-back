"""Consistent, server-owned document tracking fields."""


def timestamps(existing, now):
    return {
        "createdAt": (existing or {}).get("createdAt") or now,
        "modifiedAt": now,
        # Retain the existing field used by Sheets and older clients.
        "updatedAt": now,
    }


def missing_metadata(data, created_time, updated_time, *, email=None, include_email=True):
    """Backfill only missing fields using the original Firestore document dates."""
    patch = {}
    if not data.get("createdAt"):
        patch["createdAt"] = created_time
    if not data.get("modifiedAt"):
        patch["modifiedAt"] = updated_time
    if not data.get("updatedAt"):
        patch["updatedAt"] = data.get("modifiedAt") or updated_time
    if include_email and not data.get("email") and (email or "email" not in data):
        # Null explicitly means unknown. Never infer an email from a person's name.
        patch["email"] = email
    return patch
