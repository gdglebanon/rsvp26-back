import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app.models import RegistrationForm, RegistrationRequest
from app.record_metadata import timestamps

PROFILE_FIELDS = (
    "firstName",
    "lastName",
    "phone",
    "linkedIn",
    "major",
    "ageRange",
    "gender",
    "region",
    "specialization",
    "company",
    "university",
    "activeExpCategories",
    "expLevels",
    "status",
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class RegistrationService:
    def __init__(self, store, settings, clock=None):
        self.store, self.settings = store, settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.root = f"events/{settings.event_id}"

    def is_open(self):
        return (
            not self.settings.force_close_registration
            and self.clock() <= self.settings.registration_deadline
        )

    def require_open(self):
        if not self.is_open():
            raise HTTPException(403, "Registration is closed")

    def user_path(self, uid):
        return f"users/{digest(uid)}"

    def ticket_path(self, ticket_id):
        return f"{self.root}/tickets/{ticket_id}"

    def sign(self, purpose, ticket):
        key = self.settings.ticket_signing_key
        if not key:
            raise HTTPException(503, "Ticket signing is not configured")
        value = f"{purpose}:{self.settings.event_id}:{ticket['id']}:{ticket['nonce']}"
        return hmac.new(key.get_secret_value().encode(), value.encode(), hashlib.sha256).hexdigest()

    def qr_payload(self, ticket):
        return f"devfest:{self.settings.event_id}:{ticket['id']}:{ticket['nonce']}:{self.sign('qr', ticket)}"

    def rate_limit(self, subject, limit=None):
        now = self.clock()
        key = digest(f"{subject}:{int(now.timestamp()) // 3600}")
        path = f"rsvpRateLimits/{key}"

        def operation(tx):
            existing = tx.get(path)
            count = (existing or {}).get("count", 0)
            if count >= (limit or self.settings.submissions_per_hour):
                raise HTTPException(
                    429, "Too many requests; try again later", headers={"Retry-After": "3600"}
                )
            tx.set(
                path,
                {
                    "count": count + 1,
                    "expiresAt": now + timedelta(hours=2),
                    **timestamps(existing, now),
                },
            )

        self.store.atomic(operation)

    def queue(self, tx, ticket, *kinds):
        now = self.clock()
        for kind in kinds:
            key = f"{self.settings.event_id}-{ticket['id']}-{ticket['version']}-{kind}"
            tx.set(
                f"rsvpOutbox/{key}",
                {
                    "id": key,
                    "eventId": self.settings.event_id,
                    "ticketId": ticket["id"],
                    "email": ticket.get("email"),
                    "version": ticket["version"],
                    "kind": kind,
                    "state": "pending",
                    "attempts": 0,
                    "nextAttemptAt": now,
                    **timestamps(None, now),
                },
            )

    def owned(self, tx, identity):
        ticket = tx.get(self.ticket_path(digest(identity.uid)))
        if not ticket or ticket["uid"] != identity.uid:
            raise HTTPException(404, "No registration found")
        return ticket

    def public_ticket(self, ticket):
        data = {
            key: value
            for key, value in ticket.items()
            if key not in {"nonce", "uid", "checkedInBy"}
        }
        if ticket["status"] == "invited" and self.clock() >= ticket["invitationExpiresAt"]:
            data["status"] = "expired"
        return data

    def me(self, identity):
        def operation(tx):
            user = tx.get(self.user_path(identity.uid))
            ticket = tx.get(self.ticket_path(digest(identity.uid)))
            return {
                "user": {"uid": identity.uid, "email": identity.email, "emailVerified": True},
                "profile": user["profile"] if user else None,
                "ticket": self.public_ticket(ticket) if ticket else None,
            }

        result = self.store.atomic(operation)
        if result["profile"] is None and hasattr(self.store, "legacy_profile"):
            result["profile"] = self.store.legacy_profile(identity.email)
            result["legacyProfileLoaded"] = result["profile"] is not None
        return result

    def submit(self, request, identity, update=False, *, vip_authorized=False):
        if request.email != identity.email:
            raise HTTPException(403, "Sign in with the registration email")
        self.rate_limit(f"submit:{identity.uid}")
        initial = self.store.atomic(lambda tx: tx.get(self.ticket_path(digest(identity.uid))))
        if not update:
            if initial:
                return self.public_ticket(initial)
            self.require_open()
        elif not initial:
            raise HTTPException(404, "No registration found")
        elif initial["version"] != request.version:
            raise HTTPException(409, "Registration changed; reload before editing")
        if initial and initial.get("checkedInAt"):
            raise HTTPException(409, "Checked-in registrations cannot be edited")
        form = request.form.model_dump(exclude={"secretCode"})
        code = request.form.secretCode
        vip = bool(code) or vip_authorized
        if code and not any(
            secrets.compare_digest(digest(code), value) for value in self.settings.vip_code_hashes
        ):
            raise HTTPException(403, "Invalid VIP access code")
        if initial and initial["ticketType"] == "VIP":
            vip = True
        if initial and vip and initial["ticketType"] != "VIP":
            raise HTTPException(409, "Ticket type cannot be changed by editing")
        if vip and not self.settings.ticket_signing_key:
            raise HTTPException(503, "Ticket signing is not configured")
        now, nonce = self.clock(), secrets.token_hex(16)
        ticket_id = digest(identity.uid)
        email_path = f"{self.root}/emailOwners/{digest(identity.email)}"

        def operation(tx):
            existing = tx.get(self.ticket_path(ticket_id))
            email_owner = tx.get(email_path)
            user = tx.get(self.user_path(identity.uid))
            if email_owner and email_owner["uid"] != identity.uid:
                raise HTTPException(409, "This email is registered to another account")
            if existing and not update:
                return self.public_ticket(existing)
            if update and (not existing or existing["version"] != request.version):
                raise HTTPException(409, "Registration changed; reload before editing")
            if existing and existing.get("checkedInAt"):
                raise HTTPException(409, "Checked-in registrations cannot be edited")
            resubmitting = bool(existing and existing["status"] == "cancelled")
            if resubmitting:
                self.require_open()
            profile = {key: form[key] for key in PROFILE_FIELDS}
            answers = {
                key: value
                for key, value in form.items()
                if key not in PROFILE_FIELDS and key != "email"
            }
            ticket = {
                "id": ticket_id,
                "uid": identity.uid,
                "eventId": self.settings.event_id,
                "ticketType": "VIP" if vip else "STANDARD",
                "email": identity.email,
                "emailVerified": True,
                "answers": answers,
                "status": "confirmed" if vip else "submitted",
                **timestamps(existing, now),
                "version": 1,
                "nonce": nonce,
            }
            if resubmitting:
                ticket["status"] = "submitted"
                ticket["version"] = existing["version"] + 1
            elif existing:
                ticket.update(
                    {
                        key: existing[key]
                        for key in (
                            "createdAt",
                            "status",
                            "nonce",
                            "invitationExpiresAt",
                            "confirmedAt",
                            "checkedInAt",
                        )
                        if key in existing
                    }
                )
                ticket["version"] = existing["version"] + 1
            elif vip:
                ticket["confirmedAt"] = now
            tx.set(
                self.user_path(identity.uid),
                {
                    "uid": identity.uid,
                    "email": identity.email,
                    "emailVerified": True,
                    "verifiedAt": now,
                    "profile": profile,
                    **timestamps(user, now),
                },
            )
            tx.set(
                email_path,
                {
                    "uid": identity.uid,
                    "email": identity.email,
                    **timestamps(email_owner, now),
                },
            )
            tx.set(self.ticket_path(ticket_id), ticket)
            email_kind = {
                "submitted": "submission_email",
                "invited": "invitation_email",
                "confirmed": "ticket_email",
                "cancelled": "cancelled_email",
                "rejected": "rejected_email",
                "waitlisted": "waitlisted_email",
            }[ticket["status"]]
            self.queue(tx, ticket, "sheet", email_kind)
            return self.public_ticket(ticket)

        return self.store.atomic(operation)

    def requires_registration_login(self, email, tx=None):
        def check(unit):
            owner = unit.get(f"{self.root}/emailOwners/{digest(email)}")
            if not owner:
                return False
            ticket = unit.get(self.ticket_path(digest(owner["uid"])))
            return bool(ticket and ticket.get("email") == email
                        and ticket.get("emailVerified") is True)

        return check(tx) if tx is not None else self.store.atomic(check)

    def save_unverified(self, request, ip):
        self.require_open()
        self.rate_limit(f"unverified-ip:{ip}", limit=20)
        self.rate_limit(f"unverified-email:{request.email}", limit=5)
        code = request.form.secretCode
        if code and not any(
            secrets.compare_digest(digest(code), value) for value in self.settings.vip_code_hashes
        ):
            raise HTTPException(403, "Invalid VIP access code")
        form = request.form.model_dump(exclude={"secretCode"})
        pending_id = digest(f"{self.settings.event_id}:{request.submissionKey}")
        path = f"pendingRegistrations/{pending_id}"
        now = self.clock()

        def operation(tx):
            if self.requires_registration_login(request.email, tx):
                raise HTTPException(409, "Login is necessary to continue your registration.")
            existing = tx.get(path)
            if existing and (existing["email"] != request.email or existing["emailVerified"]):
                raise HTTPException(
                    409, "This submission can no longer be changed without signing in"
                )
            data = {
                "id": pending_id,
                "eventId": self.settings.event_id,
                "email": request.email,
                "emailVerified": False,
                "verificationStatus": "unverified",
                "status": "pending_verification",
                "ticketType": "VIP" if code else "STANDARD",
                "profile": {key: form[key] for key in PROFILE_FIELDS},
                "answers": {
                    key: value
                    for key, value in form.items()
                    if key not in PROFILE_FIELDS and key != "email"
                },
                **timestamps(existing, now),
                "expiresAt": now + timedelta(days=7),
            }
            tx.set(path, data)
            # Never return stored personal data from a public endpoint.
            return {
                "id": pending_id,
                "emailVerified": False,
                "status": "pending_verification",
                "expiresAt": data["expiresAt"],
            }

        return self.store.atomic(operation)

    def complete_pending(self, pending_id, identity):
        path = f"pendingRegistrations/{pending_id}"
        self.rate_limit(f"complete-pending:{identity.uid}")

        def verify(tx):
            data = tx.get(path)
            if (
                not data
                or data["eventId"] != self.settings.event_id
                or data["email"] != identity.email
            ):
                raise HTTPException(404, "No submission found for your verified email")
            if data.get("uid") and data["uid"] != identity.uid:
                raise HTTPException(403, "Submission belongs to another account")
            if self.clock() >= data["expiresAt"]:
                raise HTTPException(410, "This saved submission has expired; submit the form again")
            # Lock the untrusted snapshot once its owner proves email control. Only
            # the Firebase-authenticated endpoint can set these flags.
            if not data.get("emailVerified"):
                now = self.clock()
                data.update(
                    emailVerified=True,
                    verificationStatus="verified",
                    uid=identity.uid,
                    verifiedAt=now,
                    **timestamps(data, now),
                )
                tx.set(path, data)
            return data

        pending = self.store.atomic(verify)
        form = RegistrationForm.model_validate(
            {"email": identity.email, **pending["profile"], **pending["answers"]}
        )
        result = self.submit(
            RegistrationRequest(email=identity.email, form=form),
            identity,
            vip_authorized=pending["ticketType"] == "VIP",
        )

        def finish(tx):
            latest = tx.get(path)
            if latest and latest.get("uid") == identity.uid and latest["status"] != "completed":
                now = self.clock()
                latest.update(
                    status="completed",
                    ticketId=result["id"],
                    completedAt=now,
                    **timestamps(latest, now),
                )
                tx.set(path, latest)

        self.store.atomic(finish)
        return result

    def cancel(self, identity, version):
        def operation(tx):
            ticket = self.owned(tx, identity)
            if ticket["status"] == "cancelled":
                return self.public_ticket(ticket)
            if ticket["version"] != version:
                raise HTTPException(409, "Registration changed; reload before cancelling")
            if ticket.get("checkedInAt"):
                raise HTTPException(409, "Checked-in registrations cannot be cancelled")
            ticket.update(
                status="cancelled",
                nonce=secrets.token_hex(16),
                **timestamps(ticket, self.clock()),
                version=version + 1,
            )
            tx.set(self.ticket_path(ticket["id"]), ticket)
            self.queue(tx, ticket, "sheet", "cancelled_email")
            return self.public_ticket(ticket)

        return self.store.atomic(operation)

    def curate(self, ticket_id, action, version, actor, request_id):
        if action == "invite" and not self.settings.ticket_signing_key:
            raise HTTPException(503, "Ticket signing is not configured")
        receipt_path = f"{self.root}/actions/{digest(request_id)}"

        def operation(tx):
            receipt = tx.get(receipt_path)
            ticket = tx.get(self.ticket_path(ticket_id))
            if receipt:
                if receipt["ticketId"] != ticket_id or receipt["action"] != action:
                    raise HTTPException(409, "Action request ID was already used")
                return receipt["result"]
            if not ticket:
                raise HTTPException(404, "Ticket not found")
            if ticket["version"] != version:
                raise HTTPException(409, "Stale Sheet row; wait for synchronization then retry")
            if ticket["ticketType"] == "VIP" or ticket["status"] not in {
                "submitted",
                "waitlisted",
                "invited",
            }:
                raise HTTPException(409, "Ticket is not eligible for curation")
            if (
                action == "invite"
                and ticket["status"] == "invited"
                and self.clock() < ticket["invitationExpiresAt"]
            ):
                raise HTTPException(409, "An active invitation already exists")
            status = {"invite": "invited", "waitlist": "waitlisted", "reject": "rejected"}[action]
            ticket.update(
                status=status,
                version=version + 1,
                nonce=secrets.token_hex(16),
                **timestamps(ticket, self.clock()),
            )
            if status == "invited":
                ticket["invitationExpiresAt"] = self.clock() + timedelta(
                    hours=self.settings.invitation_ttl_hours
                )
            result = self.public_ticket(ticket)
            tx.set(self.ticket_path(ticket_id), ticket)
            tx.set(
                receipt_path,
                {
                    "ticketId": ticket_id,
                    "action": action,
                    "actor": actor,
                    "email": ticket.get("email"),
                    **timestamps(None, self.clock()),
                    "result": result,
                },
            )
            self.queue(
                tx,
                ticket,
                "sheet",
                {
                    "invited": "invitation_email",
                    "waitlisted": "waitlisted_email",
                    "rejected": "rejected_email",
                }[status],
            )
            return result

        return self.store.atomic(operation)

    def confirm(self, identity, token):
        def operation(tx):
            ticket = self.owned(tx, identity)
            if not secrets.compare_digest(token, self.sign("invite", ticket)):
                raise HTTPException(403, "Invalid invitation")
            if ticket["status"] == "confirmed":
                return self.public_ticket(ticket)
            if ticket["status"] != "invited":
                raise HTTPException(409, "No active invitation")
            if self.clock() >= ticket["invitationExpiresAt"]:
                raise HTTPException(410, "Invitation expired")
            now = self.clock()
            ticket.update(
                status="confirmed",
                confirmedAt=now,
                **timestamps(ticket, now),
                version=ticket["version"] + 1,
            )
            tx.set(self.ticket_path(ticket["id"]), ticket)
            self.queue(tx, ticket, "sheet", "ticket_email")
            return self.public_ticket(ticket)

        return self.store.atomic(operation)

    def ticket(self, identity):
        return self.store.atomic(lambda tx: self.owned(tx, identity))

    def check_in(self, payload, actor):
        parts = payload.split(":")
        if len(parts) != 5 or parts[0] != "devfest" or parts[1] != self.settings.event_id:
            raise HTTPException(400, "Invalid ticket QR")
        ticket_id = parts[2]
        if len(ticket_id) != 64 or any(c not in "0123456789abcdef" for c in ticket_id):
            raise HTTPException(400, "Invalid ticket QR")

        def operation(tx):
            ticket = tx.get(self.ticket_path(ticket_id))
            if (
                not ticket
                or ticket["status"] != "confirmed"
                or not secrets.compare_digest(payload, self.qr_payload(ticket))
            ):
                raise HTTPException(403, "Ticket is not valid for entry")
            already = bool(ticket.get("checkedInAt"))
            if not already:
                now = self.clock()
                ticket.update(
                    checkedInAt=now,
                    checkedInBy=actor,
                    version=ticket["version"] + 1,
                    **timestamps(ticket, now),
                )
                tx.set(self.ticket_path(ticket_id), ticket)
                self.queue(tx, ticket, "sheet")
            return {"valid": True, "alreadyCheckedIn": already, "ticketId": ticket_id}

        return self.store.atomic(operation)
