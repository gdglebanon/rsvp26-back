import base64
import io
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

import google.auth
import httpx
import qrcode
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


class IntegrationNotConfigured(Exception):
    pass


def qr_png(payload):
    buffer = io.BytesIO()
    qrcode.make(payload).save(buffer, format="PNG")
    return buffer.getvalue()


class Mailer:
    def __init__(self, settings):
        self.settings = settings

    def send(self, recipient, subject, body, attachment=None):
        s = self.settings
        if s.mail_provider == "disabled" or not s.mail_from:
            raise IntegrationNotConfigured("Transactional email is not configured")
        if s.mail_provider == "sendgrid":
            if not s.sendgrid_api_key:
                raise IntegrationNotConfigured("SendGrid key is missing")
            payload = {
                "personalizations": [{"to": [{"email": recipient}]}],
                "from": {"email": s.mail_from},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            }
            if attachment:
                payload["attachments"] = [
                    {
                        "content": base64.b64encode(attachment).decode(),
                        "type": "image/png",
                        "filename": "devfest-ticket.png",
                    }
                ]
            result = httpx.post(
                "https://api.sendgrid.com/v3/mail/send",
                json=payload,
                headers={"Authorization": f"Bearer {s.sendgrid_api_key.get_secret_value()}"},
                timeout=15,
            )
            result.raise_for_status()
        else:
            if not s.smtp_host:
                raise IntegrationNotConfigured("SMTP host is missing")
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = s.mail_from, recipient, subject
            message.set_content(body)
            if attachment:
                message.add_attachment(
                    attachment, maintype="image", subtype="png", filename="devfest-ticket.png"
                )
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as server:
                if s.smtp_starttls:
                    import ssl

                    server.starttls(context=ssl.create_default_context())
                if s.smtp_username:
                    server.login(
                        s.smtp_username,
                        s.smtp_password.get_secret_value() if s.smtp_password else "",
                    )
                server.send_message(message)


SHEET_HEADERS = [
    "ticketId",
    "email",
    "firstName",
    "lastName",
    "specialization",
    "company",
    "university",
    "attendanceType",
    "ticketType",
    "status",
    "updatedAt",
    "version",
    "action",
    "actionResult",
]


class SheetSync:
    def __init__(self, settings):
        self.settings = settings

    def upsert(self, user, ticket):
        s = self.settings
        if not s.google_sheet_id:
            raise IntegrationNotConfigured("Google Sheet is not configured")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        cred = (
            service_account.Credentials.from_service_account_file(
                s.firebase_credentials_path, scopes=scopes
            )
            if s.firebase_credentials_path
            else google.auth.default(scopes=scopes)[0]
        )
        tab = "'" + s.google_sheet_tab.replace("'", "''") + "'"
        base = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(s.google_sheet_id, safe='')}/values/"
        with AuthorizedSession(cred) as session:

            def url(cell):
                return base + quote(f"{tab}!{cell}", safe="")

            response = session.get(url("A:A"), timeout=15)
            response.raise_for_status()
            rows = response.json().get("values", [])
            if rows and rows[0] != ["ticketId"]:
                raise ValueError("Sheet must use the documented ticketId header")
            if not rows:
                response = session.put(
                    url("A1:N1"),
                    params={"valueInputOption": "RAW"},
                    json={"values": [SHEET_HEADERS]},
                    timeout=15,
                )
                response.raise_for_status()
                rows = [["ticketId"]]
            # Find by immutable ticket ID, so sorting does not overwrite another attendee.
            matches = [i + 1 for i, row in enumerate(rows) if row and row[0] == ticket["id"]]
            if len(matches) > 1:
                raise ValueError("Duplicate ticket IDs in Sheet; resolve before syncing")
            row_number = matches[0] if matches else len(rows) + 1
            profile = user["profile"]
            values = [
                ticket["id"],
                user["email"],
                profile["firstName"],
                profile["lastName"],
                profile["specialization"],
                profile["company"],
                profile["university"],
                ticket["answers"]["attendanceType"],
                ticket["ticketType"],
                ticket["status"],
                ticket["updatedAt"].isoformat(),
                ticket["version"],
            ]
            # RAW blocks formula injection; M:N remain owned by the curation workflow.
            response = session.put(
                url(f"A{row_number}:L{row_number}"),
                params={"valueInputOption": "RAW"},
                json={"values": [values]},
                timeout=15,
            )
            response.raise_for_status()
