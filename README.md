# DevFest 2026 RSVP backend

FastAPI + Firebase backend, connected to the [React/Vite frontend](https://github.com/gdglebanon/rsvp26-front), located at `../rsvp-gdg` in the combined local workspace. The registration form opens immediately. A debounced, rate-limited email-presence lookup prompts returning attendees to verify. Returning attendees use compact Google/email-password buttons below the email field. Unverified submissions are saved immediately in Firestore and then display the verification popup. **Only Firebase-verified identities can load private saved data, own a canonical user profile, or receive an event ticket.** AI screening is intentionally omitted.

For the production domains and exact Vercel environment settings, follow [DEPLOYMENT.md](DEPLOYMENT.md). Vercel credentials use the backend-only `FIREBASE_CREDENTIALS_JSON` variable; local development can continue using `FIREBASE_CREDENTIALS_PATH`.

## Start

Python 3.11+ is required. This workspace has dependencies installed and an ignored `.env` configured with the supplied service-account path, the existing Firebase web app's public config, and generated local signing secrets.

```bash
cd backend
uv sync --locked --extra dev
uv run uvicorn app.main:app --reload --port 8000 --no-proxy-headers
```

Or use `.venv/bin/python -m uvicorn app.main:app --reload --port 8000 --no-proxy-headers` directly. Without `uv`, create a venv and run `pip install -e '.[dev]'`.

In another terminal:

```bash
cd rsvp-gdg
npm install
npm run dev -- --host localhost
```

Open [the RSVP app](http://localhost:5173/rsvp-gdg/) and [API documentation](http://localhost:8000/docs). Use `localhost` for Firebase's authorized domain. The frontend reads public Firebase configuration from `/api/config`; it never receives the service-account key. Set `VITE_API_URL` in the frontend for other API hosts.

A fresh checkout must copy `.env.example` to `.env` and configure it. Keep `TICKET_SIGNING_KEY` stable across restarts and instances. Use `python -c 'import secrets; print(secrets.token_urlsafe(48))'` to generate secrets. In Google-hosted environments, omit `FIREBASE_CREDENTIALS_PATH` to use Application Default Credentials. Both `.gitignore` and `.dockerignore` exclude `.env` and the supplied credential filename.

## Identity and callback

- `AttendeeLogin.jsx` uses Google popup sign-in or Firebase email/password accounts. New accounts receive `sendEmailVerification` mail using the **Email address verification** template. No new passwordless sign-in emails are sent.
- Enable **Email/Password** in Firebase Authentication → Sign-in method. Keep the default Firebase-hosted email action handler in the verification and password-reset templates. Add the frontend domain to Authorized domains. Email link (passwordless sign-in) can be disabled after previously issued links expire.
- Verification mail returns to the React entry URL with `?auth=callback` and the pending registration ID. Attendees can also return to the original tab and select **I have verified my email**. The client reloads the Firebase user and force-refreshes the ID token before calling protected APIs. A different browser requires email/password sign-in after verification.
- Existing email-link users select **Forgot password / Set a password** to set a password on their existing Firebase account; their UID and registration remain intact. Already-issued sign-in links remain supported during migration.
- `/api/config` advertises `emailAuth: {method: "password", verificationRequired: true}`. Passwords are handled only by Firebase Auth, never by the RSVP backend or draft storage. The API continues to require a signed token with `email_verified: true`, including for password-authenticated accounts.
- `/auth/callback` on the backend can also forward Firebase callback parameters to the configured frontend. The redirect target is fixed in server configuration.
- **A URL user ID or email never authorizes a profile read.** The server derives the UID and verified email from a signed Firebase token, with revocation checks. Firebase's browser session persists in the current browser tab.
- Email OTP support is also implemented. It is enabled in the UI when transactional mail is configured. Codes have six cryptographically random digits, a ten-minute lifetime, five attempts per challenge, per-IP and per-email request limits, and one-time consumption. Only a keyed hash is stored. After a correct code, the server marks the Firebase user's email verified and returns a Firebase custom token; the browser exchanges it for an ordinary Firebase session. OTP delivery is disabled with the current Firebase-auth-email-only setup.
- The form checks `/api/identity/lookup` after a 600 ms pause in email typing. It returns only `{exists}` and is limited to 60 checks per IP/hour. It intentionally reveals account presence to support the requested returning-attendee prompt; it never returns profile or ticket details.
- Submitting an unverified form calls `/api/pending` before showing the popup. The backend stores separate profile/event-answer objects with `emailVerified: false`, `verificationStatus: unverified`, and `status: pending_verification`. A client-supplied verified flag never overrides Firebase authentication.
- The browser retains the opaque pending ID and retry key, and embeds the pending ID in the Firebase callback URL. The ID alone cannot retrieve personal information or authorize completion. After verification, `/api/pending/complete` verifies the same email, locks the saved snapshot, and creates/links the canonical user and event ticket with `emailVerified: true`. Completion is idempotent and will not overwrite an existing registration. VIP eligibility is validated before saving and applied only after email verification, without retaining the shared access code.
- Completion is resumable across the verification/registration steps: a failure can leave a verified pending record without a completed ticket; retrying finishes it safely. The account page offers a retry when this occurs. Unverified submissions expire after seven days; configure a TTL policy on `pendingRegistrations.expiresAt`. Registration creation still obeys the event deadline.
- A local form draft (excluding VIP codes) is retained for 30 minutes for form continuity. Server-side pending records survive beyond that local draft. Successful completion clears the local pending reference and draft.

Google and email sign-in and the `localhost` authorized domain were checked in the existing Firebase project. Configure production authorized domains, `CORS_ORIGINS`, and `FRONTEND_URL` before deployment. The end-to-end email/Google login flow still requires the attendee to sign in with their own account; automated tests do not send real emails or create Firebase users.

## Existing attendee data

Read-only samples confirmed legacy records in `rsvp_guests` and email-hash keyed `attendeeProfiles`. If the verified Firebase UID has no new `users` profile, `/api/me`:

1. Looks up `attendeeProfiles/{sha256(normalized_verified_email)}`.
2. Falls back to an exact normalized-email query in `rsvp_guests`. Ambiguous duplicate matches are not imported.
3. Returns only allowlisted profile fields, mapping `linkedin → linkedIn`, `education → major`, old experience choices, and region/gender/specialization values.

Legacy OTPs, `isVerified`, selection flags, prior ticket identifiers, and invitation status are never used as authority or copied into new tickets. Unknown old choices require attendee correction. After a verified owner loads a legacy profile, the application fills its missing email and tracking dates; other legacy fields are preserved. Old records without a reliable event identifier prefill the person; the attendee explicitly submits a new 2026 application. Saving creates/updates their new canonical user profile.

### Visible email and tracking dates

Person-related records have top-level `email`, `createdAt`, and `modifiedAt` fields, including users, pending registrations, tickets, email ownership claims, action receipts, OTP challenges, and delivery jobs. `createdAt` is preserved across edits; `modifiedAt` advances when the record changes. `updatedAt` remains as a compatibility alias for existing Sheets integration. System-only rate limits and worker locks have dates but no attendee email. All dates are Firestore timestamps in UTC. Neither emails nor these dates are accepted from the client as authentication proof.

For existing records, run from the backend directory:

```bash
python -m app.backfill_metadata          # Preview counts; no writes
python -m app.backfill_metadata --apply  # Fill missing tracking fields
```

The backfill uses original Firestore document creation/update times for missing dates. It only patches missing fields, uses write preconditions to protect concurrent changes, and prints counts without personal data. It matches legacy profile hashes to exact known emails from existing records and Firebase Auth. Unknown legacy emails are stored as `null`; hashes cannot be reversed, so filling those requires the original email list or the attendee signing in. `rsvp_guests` remains a legacy fallback; new registrations are saved to `pendingRegistrations`, `users`, and event `tickets`.

## Separate users and event tickets

| Collection | Contents |
| --- | --- |
| `pendingRegistrations/{opaque_id}` | Unverified profile/event-answer snapshot, verification flags, expiration, and verified ticket link after completion |
| `users/{sha256(uid)}` | UID, verified email, reusable personal/professional profile, timestamps |
| `events/{EVENT_ID}/tickets/{sha256(uid)}` | User reference, visible email, event answers, ticket type, application status, version, invitation/confirmation/check-in metadata |
| `events/{EVENT_ID}/emailOwners/{sha256(email)}` | Unique verified-email claim for the event |
| `events/{EVENT_ID}/actions/{sha256(requestId)}` | Idempotent organizer action receipts and actor audit |
| `rsvpOutbox/{event-ticket-version-kind}` | Durable integration jobs and retry state |
| `rsvpOtp/{random_id}` | One-time email-code challenge hashes, attempt counters, and expiry; configure TTL on `expiresAt` |
| `rsvpRateLimits/{hash}` | Per-user hourly mutation counters; configure TTL on `expiresAt` |
| `rsvpLocks/integrations` | Cross-worker delivery lease |

Personal fields are stored on the user, event-specific answers on the ticket. Atomic transactions prevent duplicate submissions and conflicting edits. A repeated create returns the existing ticket. Edits use `PUT` with the last seen `version`; stale updates return 409. New applications obey the deadline, while existing users may edit after the deadline. Editing preserves curation/confirmation status; it does not reinstate a cancelled or rejected ticket. Checked-in tickets cannot be edited or cancelled.

Public pending submissions are isolated from canonical users and tickets. Their profile/event answer data cannot be read anonymously, and they cannot claim an email or replace an existing verified registration. Requests are limited per IP and email. Organizer curation, notifications, and QR issuance apply only to verified tickets.

## Lifecycle

Standard applications start as `submitted`, awaiting human review. Organizers can `waitlist`, `reject`, or `invite`. Invitations expire after 48 hours by default. A verified owner must explicitly confirm before the server deadline; only then does a standard ticket become `confirmed` and expose a QR code. An expired invitation can be reissued by an organizer. Final confirmation is idempotent.

A correct shared VIP code creates a `VIP` ticket immediately in `confirmed` status, bypassing curation. Set its SHA-256 hash in `VIP_CODE_HASHES`, for example using:

```bash
python -c 'import getpass,hashlib; print(hashlib.sha256(getpass.getpass("VIP code: ").encode()).hexdigest())'
```

No VIP code is stored with the attendee. `?vip` only shows the input; the backend validates the code. Existing ticket types cannot be changed by editing.

Attendees can view status, update their details, cancel, confirm an invitation, or download a confirmed ticket from their account. While invitation delivery is disabled, the signed-in owner can retrieve their current invitation and confirm from the account page. Cancellation immediately invalidates prior QR codes and invitation tokens. QR payloads contain an opaque ticket identifier and signature, not contact details. Organizer check-in verifies the signature and current database status, and detects repeat scans.

## API

Pending creation, email-presence lookup, and OTP request/verification are public and rate limited. Private attendee routes require `Authorization: Bearer <Firebase ID token>`. All responses use `Cache-Control: no-store`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health`, `/ready` | Liveness / actual Firestore read readiness |
| GET | `/api/config` | Public event settings and public Firebase web config |
| POST | `/api/identity/lookup` | Public `{email}` → presence only, rate limited |
| POST | `/api/auth/otp/request` | Send a code with `{email}` when mail delivery is configured |
| POST | `/api/auth/otp/verify` | Exchange `{challengeId, code}` for a Firebase custom token |
| GET | `/api/me` | Verified identity, reusable profile, current event ticket |
| GET | `/api/profile` | Verified owner's reusable profile |
| POST | `/api/pending` | Save `{email, form, submissionKey, emailVerified: false}` before verification |
| POST | `/api/pending/complete` | Firebase-verified owner completes saved `{id}` and updates verification flags |
| POST | `/api/register` | Create with `{email, form}`; exact camel-case frontend fields |
| PUT | `/api/registration` | Update with `{email, form, version}` |
| GET | `/api/registration` | Own current event application |
| POST | `/api/registration/cancel` | Cancel with `{version}` |
| GET | `/api/invitations/current` | Own active invitation token and expiration |
| POST | `/api/invitations/confirm` | Confirm with `{token}` |
| GET | `/api/ticket/qr` | Confirmed owner's PNG ticket |
| POST | `/api/admin/curate` | Organizer `{ticketId, action, version, requestId}` |
| POST | `/api/admin/check-in` | Organizer `{qr}` verification and check-in |
| POST | `/api/webhooks/sheets` | Timestamped HMAC-signed curation action |

Organizer routes require the Firebase custom claim `organizer: true`, set only by a trusted project administrator. Never let a browser set claims. There is no public organizer-role enrollment endpoint.

## Email and Google Sheets integrations

**Currently active:** Firebase authentication emails. **Not configured:** OTP email delivery, transactional event email delivery, and Google Sheet destination. No AI model is called and no “high chance” claim is made.

For status, invitation, and ticket emails, set `MAIL_PROVIDER=sendgrid` with `SENDGRID_API_KEY` and `MAIL_FROM`, or `MAIL_PROVIDER=smtp` with the SMTP settings. Firebase's built-in authentication mail service does not send arbitrary event emails. Until a provider is configured, those jobs remain pending; they are never reported as sent. VIP ticket emails include the QR attachment, and standard ticket emails are queued after final confirmation.

To sync a Google Sheet, enable the Sheets API, create a dedicated `Attendees` tab, share the spreadsheet with the service account, and set `GOOGLE_SHEET_ID`. The worker finds rows by immutable ticket ID and updates the same row even if the sheet has been sorted. It uses `RAW` values to prevent formula injection. It creates these headers:

`ticketId, email, firstName, lastName, specialization, company, university, attendanceType, ticketType, status, updatedAt, version, action, actionResult`

The synchronized columns are A:L. Protect these from manual editing; the team uses M (`action`) with `invite`, `waitlist`, or `reject`. Install `examples/sheets-actions.gs` as a bound Apps Script and create an **installable on-edit trigger** for `curateOnEdit`. Set script properties `RSVP_API_URL`, `RSVP_WEBHOOK_SECRET`, and optional `RSVP_SHEET_TAB`. Only trusted curators should have edit access to this script/Sheet. The webhook verifies HMAC, rejects requests older than five minutes, checks ticket version, and deduplicates request IDs. Errors appear in column N; refresh the synced row and retry a stale action.

Run the durable worker as a separate supervised process:

```bash
cd backend
uv run python -m app.worker
```

Each API transaction writes its integration jobs atomically with the ticket. The worker retries failed/configuration-blocked jobs with backoff, ignores superseded email jobs, and serializes deliveries across processes. A job is done only after provider success. Delivery is **at least once**: a crash after provider acceptance and before receipt persistence can duplicate an email. Do not run the worker against production until delivery and Sheet settings are ready. The worker was not started against the live project during implementation.

## Tests and deployment

```bash
cd backend
uv run pytest
uv run ruff check app tests
cd ../rsvp-gdg
npm test
npm run build
```

Tests cover authentication, public lookup limits, OTP attempts/expiry/replay, legacy mapping, profile/ticket separation, stale edits, uniqueness, VIP, curation, invitation expiry, confirmation, QR/cancellation, replay protection, delivery retries, and metadata/backfill behavior. Tests use an in-memory transactional store with Firebase's read-before-write constraint; no test registers live attendees or sends mail.

Use the Dockerfile for the API and the same image with `python -m app.worker` for the worker. Supply credentials/signing secrets at runtime. Set HTTPS, exact allowed origins, production frontend URL, and ingress body-size/rate limits. Forward proxy headers only from explicitly trusted proxies. `firestore.rules` provides an Admin-SDK-only policy; review it before applying to a shared project. Rules and TTL policies have not been deployed automatically. No destructive migration of `rsvp_guests` is needed.

References: [Firebase Google sign-in](https://firebase.google.com/docs/auth/web/google-signin), [Firebase email/password authentication](https://firebase.google.com/docs/auth/web/password-auth), [ID token verification](https://firebase.google.com/docs/auth/admin/verify-id-tokens), [SendGrid Mail Send](https://www.twilio.com/docs/sendgrid/api-reference/mail-send/mail-send), [Sheets values update](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/update).
