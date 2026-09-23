from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth
from firebase_admin.exceptions import FirebaseError

from app.firebase import firebase_app
from app.models import Identity, normalize_email

bearer = HTTPBearer(auto_error=False)


def current_identity(token: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not token:
        raise HTTPException(
            401, "Firebase ID token required", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        claims = auth.verify_id_token(token.credentials, app=firebase_app(), check_revoked=True)
    except (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError, ValueError):
        raise HTTPException(401, "Invalid or expired Firebase ID token") from None
    except FirebaseError:
        raise HTTPException(503, "Authentication temporarily unavailable") from None
    if claims.get("email_verified") is not True or not claims.get("email"):
        raise HTTPException(403, "Verify your email before registering")
    return Identity(
        uid=claims["uid"],
        email=normalize_email(claims["email"]),
        organizer=claims.get("organizer") is True,
    )


def organizer_identity(identity: Annotated[Identity, Depends(current_identity)]):
    if not identity.organizer:
        raise HTTPException(403, "Organizer access required")
    return identity
