"""Firebase VERIFY_EMAIL delivery and one-time proof exchange, without passwords."""
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException
from firebase_admin import auth

from app.firebase import firebase_app
from app.models import normalize_email
from app.record_metadata import timestamps
from app.service import digest


class EmailVerificationService:
    def __init__(self, service, auth_api=auth, app_factory=firebase_app, post=httpx.post):
        self.service, self.auth = service, auth_api
        self.app_factory, self.post = app_factory, post

    def firebase_request(self, action, body):
        key = self.service.settings.firebase_web_api_key
        if not key:
            raise HTTPException(503, "Email verification is not configured")
        try:
            response = self.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:{action}",
                params={"key": key}, json=body, timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise HTTPException(429, "Too many attempts. Please try again later.") from None
            if action in {"resetPassword", "update"} and exc.response.status_code == 400:
                raise HTTPException(400, "Invalid or expired verification link. Request a new email.") from None
            raise HTTPException(503, "Verification could not be completed. Please try again.") from None
        except (httpx.RequestError, ValueError):
            raise HTTPException(503, "Email verification is temporarily unavailable.") from None

    def request(self, email, pending_id, ip):
        self.service.rate_limit(f"verification-ip:{ip}", limit=10)
        self.service.rate_limit(f"verification-email:{email}", limit=5)
        app = self.app_factory()
        try:
            user = self.auth.get_user_by_email(email, app=app)
        except self.auth.UserNotFoundError:
            try:
                # No password is generated or stored, and no identity token is exposed here.
                user = self.auth.create_user(email=email, email_verified=False, app=app)
            except self.auth.EmailAlreadyExistsError:
                user = self.auth.get_user_by_email(email, app=app)
        if user.disabled:
            raise HTTPException(403, "Email verification is unavailable for this address")
        token = self.auth.create_custom_token(user.uid, app=app)
        private_session = self.firebase_request("signInWithCustomToken", {
            "token": token.decode() if isinstance(token, bytes) else token,
            "returnSecureToken": True,
        })
        page = urlsplit(self.service.settings.frontend_url)
        query = dict(parse_qsl(page.query))
        query["auth"] = "callback"
        if pending_id:
            query["pending"] = pending_id
        callback = urlunsplit((page.scheme, page.netloc, page.path, urlencode(query), ""))
        self.firebase_request("sendOobCode", {
            "requestType": "VERIFY_EMAIL", "idToken": private_session["idToken"],
            "continueUrl": callback, "canHandleCodeInApp": False,
        })
        # Never return the privileged server-side ID/refresh/custom tokens to the requester.
        return {"sent": True}

    def complete(self, code, ip):
        self.service.rate_limit(f"verification-complete-ip:{ip}", limit=30)
        proof = self.firebase_request("resetPassword", {"oobCode": code})
        if proof.get("requestType") != "VERIFY_EMAIL" or not proof.get("email"):
            raise HTTPException(400, "Use an email address verification link")
        verified = self.firebase_request("update", {"oobCode": code})
        app = self.app_factory()
        user = self.auth.get_user(verified["localId"], app=app)
        if (user.disabled or not user.email_verified
                or normalize_email(user.email or "") != normalize_email(proof["email"])):
            raise HTTPException(403, "Email verification could not be confirmed")
        now = self.service.clock()

        def consume(tx):
            path = f"rsvpEmailVerificationProofs/{digest(code)}"
            if tx.get(path):
                raise HTTPException(400, "This verification link has already been used")
            tx.set(path, {"expiresAt": now + timedelta(days=7), **timestamps(None, now)})

        self.service.store.atomic(consume)
        token = self.auth.create_custom_token(user.uid, app=app)
        return {"email": user.email, "customToken": token.decode() if isinstance(token, bytes) else token}
