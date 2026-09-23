"""Durable integration worker. Run separately: python -m app.worker."""

import logging
import time
from datetime import timedelta
from uuid import uuid4

from app.config import get_settings
from app.firebase import get_store
from app.integrations import IntegrationNotConfigured, Mailer, SheetSync, qr_png
from app.record_metadata import timestamps
from app.service import RegistrationService


class OutboxWorker:
    def __init__(self, service, mailer=None, sheets=None):
        self.service = service
        self.store = service.store
        self.mailer = mailer or Mailer(service.settings)
        self.sheets = sheets or SheetSync(service.settings)

    def run_once(self):
        now, lease_id = self.service.clock(), uuid4().hex
        # Serialize deliveries across worker processes, including Sheet row allocation.
        lock_path = "rsvpLocks/integrations"

        def claim(tx):
            lock = tx.get(lock_path)
            if lock and lock.get("until", now) > now:
                return False
            tx.set(
                lock_path,
                {
                    "owner": lease_id,
                    "until": now + timedelta(minutes=3),
                    **timestamps(lock, now),
                },
            )
            return True

        if not self.store.atomic(claim):
            return False
        try:
            jobs = self.store.pending_jobs(now)
            job = next(
                (item for item in jobs if item["eventId"] == self.service.settings.event_id), None
            )
            if not job:
                return False
            state, error = "done", None
            try:
                self.deliver(job)
            except IntegrationNotConfigured:
                state, error = "pending", "integration_not_configured"
            except Exception as exc:  # noqa: BLE001 - persist delivery retries without logging PII
                state, error = "pending", type(exc).__name__
                logging.getLogger(__name__).warning("Delivery retry: %s", error)

            def finish(tx):
                lock = tx.get(lock_path)
                if not lock or lock["owner"] != lease_id:
                    return
                latest = tx.get(f"rsvpOutbox/{job['id']}")
                if not latest or latest["state"] != "pending":
                    return
                attempts = latest["attempts"] + 1
                latest.update(
                    state=state,
                    attempts=attempts,
                    lastError=error,
                    nextAttemptAt=self.service.clock()
                    + timedelta(seconds=min(3600, 30 * 2 ** min(attempts, 7))),
                    **timestamps(latest, self.service.clock()),
                )
                if state == "done":
                    latest["deliveredAt"] = self.service.clock()
                    latest.pop("nextAttemptAt", None)
                tx.set(f"rsvpOutbox/{job['id']}", latest)

            self.store.atomic(finish)
            return True
        finally:

            def release(tx):
                lock = tx.get(lock_path)
                if lock and lock["owner"] == lease_id:
                    now = self.service.clock()
                    tx.set(
                        lock_path,
                        {
                            "owner": lease_id,
                            "until": now,
                            **timestamps(lock, now),
                        },
                    )

            self.store.atomic(release)

    def deliver(self, job):
        def snapshot(tx):
            ticket = tx.get(self.service.ticket_path(job["ticketId"]))
            user = tx.get(self.service.user_path(ticket["uid"])) if ticket else None
            return ticket, user

        ticket, user = self.store.atomic(snapshot)
        if not ticket or not user:
            return
        if job["kind"] == "sheet":
            self.sheets.upsert(user, ticket)
            return
        # Never send a superseded invitation or confirmation after an edit/cancellation.
        if job["version"] != ticket["version"]:
            return
        link = self.service.settings.frontend_url
        subject = "DevFest 2026 registration"
        attachment = None
        kind = job["kind"]
        if kind == "submission_email":
            body = "Your application has been received and is awaiting team review. Check your account for updates."
        elif kind == "invitation_email":
            if self.service.clock() >= ticket["invitationExpiresAt"]:
                return
            link += f"#invite={self.service.sign('invite', ticket)}"
            body = f"You are invited to DevFest! Confirm your spot before {ticket['invitationExpiresAt'].isoformat()}."
        elif kind == "ticket_email":
            subject = "Your confirmed DevFest 2026 ticket"
            body = "Your spot is confirmed. Your entry QR ticket is attached and available in your account."
            attachment = qr_png(self.service.qr_payload(ticket))
        elif kind == "cancelled_email":
            body = "Your registration has been cancelled."
        elif kind == "waitlisted_email":
            body = "Your application is on the waiting list. Check your account for updates."
        elif kind == "rejected_email":
            body = "Thank you for your interest. We could not offer you a place at this event."
        else:
            raise ValueError("Unknown delivery kind")
        self.mailer.send(user["email"], subject, body + "\n\n" + link, attachment)


def main():
    service = RegistrationService(get_store(), get_settings())
    worker = OutboxWorker(service)
    while True:
        try:
            if not worker.run_once():
                time.sleep(5)
        except Exception as exc:  # noqa: BLE001 - persist delivery retries without logging PII
            logging.getLogger(__name__).error("Worker unavailable: %s", type(exc).__name__)
            time.sleep(10)


if __name__ == "__main__":
    main()
