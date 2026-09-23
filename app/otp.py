import hashlib
import hmac
import secrets
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from firebase_admin import auth

from app.firebase import firebase_app
from app.integrations import Mailer
from app.record_metadata import timestamps


class OTPService:
    def __init__(self, service, mailer=None, auth_api=auth, app_factory=firebase_app):
        self.service, self.store = service, service.store
        self.mailer = mailer or Mailer(service.settings)
        self.auth, self.app_factory = auth_api, app_factory

    def code_hash(self, challenge_id, code):
        key = self.service.settings.ticket_signing_key
        if not key:
            raise HTTPException(503, "OTP signing is not configured")
        return hmac.new(
            key.get_secret_value().encode(), f"otp:{challenge_id}:{code}".encode(), hashlib.sha256
        ).hexdigest()

    def request(self, email, ip):
        settings = self.service.settings
        if settings.mail_provider == "disabled" or not settings.mail_from:
            raise HTTPException(
                503, "Use Google or an email sign-in link; OTP email delivery is not configured"
            )
        self.service.rate_limit(f"otp-ip:{ip}", limit=10)
        self.service.rate_limit(f"otp-email:{email}", limit=5)
        challenge_id, code, now = (
            uuid4().hex,
            f"{secrets.randbelow(1000000):06d}",
            self.service.clock(),
        )
        data = {
            "email": email,
            "codeHash": self.code_hash(challenge_id, code),
            "attempts": 0,
            "consumed": False,
            "expiresAt": now + timedelta(minutes=10),
            **timestamps(None, now),
        }
        self.store.atomic(lambda tx: tx.set(f"rsvpOtp/{challenge_id}", data))
        try:
            self.mailer.send(
                email,
                "Your DevFest sign-in code",
                f"Your code is {code}. It expires in 10 minutes. Do not share this code.",
            )
        except Exception:  # noqa: BLE001 - provider failures must not expose credentials or OTPs
            raise HTTPException(
                503,
                "The verification email could not be sent. Use an email sign-in link or try again.",
            ) from None
        return {"challengeId": challenge_id, "expiresIn": 600}

    def verify(self, challenge_id, code):
        def operation(tx):
            path = f"rsvpOtp/{challenge_id}"
            data = tx.get(path)
            if (
                not data
                or data["consumed"]
                or data["attempts"] >= 5
                or self.service.clock() >= data["expiresAt"]
            ):
                return None
            data["attempts"] += 1
            matches = secrets.compare_digest(data["codeHash"], self.code_hash(challenge_id, code))
            if matches:
                data["consumed"] = True
            data.update(timestamps(data, self.service.clock()))
            tx.set(path, data)
            return data["email"] if matches else None

        email = self.store.atomic(operation)
        if not email:
            raise HTTPException(
                401, "Invalid or expired code. Request a new code after five unsuccessful attempts."
            )
        app = self.app_factory()
        try:
            user = self.auth.get_user_by_email(email, app=app)
        except self.auth.UserNotFoundError:
            try:
                user = self.auth.create_user(email=email, email_verified=True, app=app)
            except self.auth.EmailAlreadyExistsError:
                user = self.auth.get_user_by_email(email, app=app)
        if user.disabled:
            raise HTTPException(403, "This account is disabled")
        if not user.email_verified:
            self.auth.update_user(user.uid, email_verified=True, app=app)
        token = self.auth.create_custom_token(user.uid, app=app)
        return {"customToken": token.decode() if isinstance(token, bytes) else token}
