import hashlib
import hmac
import json
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from firebase_admin.exceptions import FirebaseError
from google.api_core.exceptions import GoogleAPICallError

from app.auth import current_identity, organizer_identity
from app.config import Settings, get_settings
from app.email_verification import EmailVerificationService
from app.firebase import FirestoreStore, get_store
from app.integrations import qr_png
from app.models import (
    CheckInRequest,
    CompletePendingRequest,
    ConfirmRequest,
    CurationRequest,
    EmailLookupRequest,
    EmailVerificationCompleteRequest,
    EmailVerificationRequest,
    Identity,
    OTPVerifyRequest,
    RegistrationRequest,
    UnverifiedRegistrationRequest,
    UpdateRegistrationRequest,
    VersionRequest,
)
from app.otp import OTPService
from app.service import RegistrationService

SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[FirestoreStore, Depends(get_store)]
IdentityDep = Annotated[Identity, Depends(current_identity)]
OrganizerDep = Annotated[Identity, Depends(organizer_identity)]


def get_service(settings: SettingsDep, store: StoreDep):
    return RegistrationService(store, settings)


ServiceDep = Annotated[RegistrationService, Depends(get_service)]
app = FastAPI(title="DevFest RSVP API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]
        },
    )


async def database_error(request, exc):
    logging.getLogger(__name__).error("Firebase operation failed (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


app.add_exception_handler(GoogleAPICallError, database_error)
app.add_exception_handler(FirebaseError, database_error)


@app.get("/health", tags=["Operations"])
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["Operations"])
def ready(store: StoreDep, settings: SettingsDep):
    store.atomic(lambda tx: tx.get(f"events/{settings.event_id}"))
    return {"status": "ready"}


@app.get("/api/config", tags=["Event"])
def config(settings: SettingsDep):
    return {
        "eventId": settings.event_id,
        "eventName": settings.event_name,
        "registrationDeadline": settings.registration_deadline,
        "registrationOpen": RegistrationService(None, settings).is_open(),
        "emailAuth": {"method": "verificationLink", "verificationRequired": True},
        "otpAvailable": settings.mail_provider != "disabled" and bool(settings.mail_from),
        "firebase": {
            "apiKey": settings.firebase_web_api_key,
            "authDomain": settings.firebase_auth_domain,
            "projectId": settings.firebase_project_id,
            "appId": settings.firebase_web_app_id,
        },
    }


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(request: Request, settings: SettingsDep):
    # Firebase callback parameters are consumed by its JS SDK in the React callback page.
    # The redirect target comes only from configuration, never a user-supplied URL.
    query = request.url.query
    separator = "&" if "?" in settings.frontend_url else "?"
    return RedirectResponse(
        settings.frontend_url + separator + "auth=callback" + ("&" + query if query else "")
    )


@app.get("/api/me", tags=["Identity"])
def me(identity: IdentityDep, service: ServiceDep):
    return service.me(identity)


@app.get("/api/profile", tags=["Identity"])
def profile(identity: IdentityDep, service: ServiceDep):
    return {"profile": service.me(identity)["profile"]}


@app.post("/api/register", tags=["Registration"])
def register(body: RegistrationRequest, identity: IdentityDep, service: ServiceDep):
    return service.submit(body, identity)


@app.put("/api/registration", tags=["Registration"])
def update_registration(
    body: UpdateRegistrationRequest, identity: IdentityDep, service: ServiceDep
):
    return service.submit(body, identity, update=True)


@app.get("/api/registration", tags=["Registration"])
def registration(identity: IdentityDep, service: ServiceDep):
    return service.public_ticket(service.ticket(identity))


@app.post("/api/registration/cancel", tags=["Registration"])
def cancel(body: VersionRequest, identity: IdentityDep, service: ServiceDep):
    return service.cancel(identity, body.version)


@app.post("/api/invitations/confirm", tags=["Tickets"])
def confirm(body: ConfirmRequest, identity: IdentityDep, service: ServiceDep):
    return service.confirm(identity, body.token)


@app.get("/api/ticket/qr", tags=["Tickets"])
def ticket_qr(identity: IdentityDep, service: ServiceDep):
    ticket = service.ticket(identity)
    if ticket["status"] != "confirmed":
        raise HTTPException(409, "A confirmed ticket is required")
    return Response(
        qr_png(service.qr_payload(ticket)),
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="devfest-ticket.png"'},
    )


@app.get("/api/invitations/current", tags=["Tickets"])
def current_invitation(identity: IdentityDep, service: ServiceDep):
    ticket = service.ticket(identity)
    if ticket["status"] != "invited":
        raise HTTPException(404, "No active invitation")
    if service.clock() >= ticket["invitationExpiresAt"]:
        raise HTTPException(410, "Invitation expired")
    # Allows verified attendees to confirm from their account while email delivery is disabled.
    return {"token": service.sign("invite", ticket), "expiresAt": ticket["invitationExpiresAt"]}


@app.post("/api/admin/curate", tags=["Organizer"])
def curate(body: CurationRequest, identity: OrganizerDep, service: ServiceDep):
    return service.curate(body.ticketId, body.action, body.version, identity.uid, body.requestId)


@app.post("/api/admin/check-in", tags=["Organizer"])
def check_in(body: CheckInRequest, identity: OrganizerDep, service: ServiceDep):
    return service.check_in(body.qr, identity.uid)


@app.post("/api/webhooks/sheets", tags=["Organizer"])
async def sheet_action(
    request: Request,
    settings: SettingsDep,
    service: ServiceDep,
    x_rsvp_timestamp: Annotated[str, Header()] = "",
    x_rsvp_signature: Annotated[str, Header()] = "",
):
    if not settings.sheets_webhook_secret:
        raise HTTPException(503, "Sheet actions are not configured")
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(413, "Webhook body too large")
    try:
        if abs(service.clock().timestamp() - int(x_rsvp_timestamp)) > 300:
            raise ValueError()
    except ValueError:
        raise HTTPException(401, "Expired webhook") from None
    signature = hmac.new(
        settings.sheets_webhook_secret.get_secret_value().encode(),
        x_rsvp_timestamp.encode() + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, x_rsvp_signature):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        body = CurationRequest.model_validate(json.loads(raw))
    except ValueError:
        raise HTTPException(422, "Invalid curation request") from None
    return await run_in_threadpool(
        service.curate, body.ticketId, body.action, body.version, "google-sheets", body.requestId
    )


@app.post("/api/identity/lookup", tags=["Identity"])
def lookup_email(body: EmailLookupRequest, request: Request, service: ServiceDep):
    # Presence only. Profile and ticket details always require Firebase verification.
    ip = request.client.host if request.client else "unknown"
    service.rate_limit(f"lookup-ip:{ip}", limit=60)
    email = str(body.email).strip().lower()
    return {
        "exists": service.store.known_email(email, service.settings.event_id),
        "loginRequired": service.requires_registration_login(email),
    }


@app.post("/api/auth/otp/request", tags=["Identity"])
def request_otp(body: EmailLookupRequest, request: Request, service: ServiceDep):
    return OTPService(service).request(
        str(body.email).strip().lower(), request.client.host if request.client else "unknown"
    )


@app.post("/api/auth/otp/verify", tags=["Identity"])
def verify_otp(body: OTPVerifyRequest, request: Request, service: ServiceDep):
    service.rate_limit(
        "otp-verify-ip:" + (request.client.host if request.client else "unknown"), limit=30
    )
    return OTPService(service).verify(body.challengeId, body.code)


@app.post("/api/pending", status_code=201, tags=["Registration"])
def save_unverified(body: UnverifiedRegistrationRequest, request: Request, service: ServiceDep):
    return service.save_unverified(body, request.client.host if request.client else "unknown")


@app.post("/api/pending/complete", tags=["Registration"])
def complete_pending(body: CompletePendingRequest, identity: IdentityDep, service: ServiceDep):
    return service.complete_pending(body.id, identity)


@app.post("/api/auth/email/request", tags=["Identity"])
def request_email_verification(body: EmailVerificationRequest, request: Request, service: ServiceDep):
    return EmailVerificationService(service).request(
        str(body.email).strip().lower(), body.pendingId,
        request.client.host if request.client else "unknown",
    )


@app.post("/api/auth/email/complete", tags=["Identity"])
def complete_email_verification(
    body: EmailVerificationCompleteRequest, request: Request, service: ServiceDep,
):
    return EmailVerificationService(service).complete(
        body.code, request.client.host if request.client else "unknown",
    )
