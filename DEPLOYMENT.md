# Deploy the RSVP application on Vercel

Production frontend: https://rsvp.gdglebanon.com/

Production backend: https://rsvp26-back-ktwg.vercel.app/

Use two Vercel projects, each connected to its matching GitHub repository and the `main` branch. Both repositories contain their application at the repository root.

## 1. Frontend project settings

Open the Vercel project connected to `gdglebanon/rsvp26-front`.

In Settings → Build and Deployment, use:

| Setting | Value |
| --- | --- |
| Framework Preset | Vite |
| Root Directory | Repository root (`./`) |
| Build Command | `npm run build` |
| Output Directory | `dist` |
| Install Command | Default (`npm install`) |

In Settings → Environment Variables, add/update these values for **Production**:

| Variable | Value |
| --- | --- |
| `VITE_BASE_PATH` | `/` |
| `VITE_API_URL` | `https://rsvp26-back-ktwg.vercel.app` |

Do not append `/api`: the frontend adds it. Do not set the base path to `/rsvp-gdg/` on this domain. These values are embedded at build time, so save them before redeploying. Preview deployments need their own allowed origin in backend CORS and Firebase if you want to test sign-in on them.

## 2. Frontend domain

In the frontend project's Settings → Domains, add or confirm `rsvp.gdglebanon.com`. It belongs to the frontend project.

If Vercel requests a DNS change, use the exact CNAME target it displays for the `rsvp` subdomain at your DNS provider. Keep existing email/MX records. If the domain already shows Valid Configuration, no DNS changes are required.

## 3. Backend project settings

Open the Vercel project connected to `gdglebanon/rsvp26-back`.

- Framework Preset: **FastAPI**.
- Root Directory: repository root (`./`), not `backend/` or `app/`.
- Leave build, install, and output overrides unset so Vercel uses its Python defaults.
- The repository declares `app.main:app` as the entry point. Do not use a static `dist` output or a continuously running Uvicorn build command.
- Confirm the production domain is `rsvp26-back-ktwg.vercel.app`. If Vercel displays a different domain, use that exact address in the frontend's `VITE_API_URL` and backend's `PUBLIC_BASE_URL`.

In Settings → Environment Variables, configure **Production**:

| Variable | Value |
| --- | --- |
| `FIREBASE_PROJECT_ID` | `rsvp-revamp` |
| `FIREBASE_DATABASE_ID` | `(default)` |
| `FIREBASE_CREDENTIALS_JSON` | Paste the complete service-account JSON file contents into this one backend-only variable |
| `FIREBASE_WEB_API_KEY` | The existing Firebase web app `apiKey` from your local backend `.env` or Firebase Project Settings → General → Your apps → SDK configuration |
| `FIREBASE_WEB_APP_ID` | The same web app's `appId` |
| `FIREBASE_AUTH_DOMAIN` | `rsvp-revamp.firebaseapp.com` |
| `CORS_ORIGINS` | `["https://rsvp.gdglebanon.com"]` |
| `FRONTEND_URL` | `https://rsvp.gdglebanon.com/` |
| `PUBLIC_BASE_URL` | `https://rsvp26-back-ktwg.vercel.app` |
| `TICKET_SIGNING_KEY` | Copy the existing value from your local backend `.env`; keep it stable |
| `SHEETS_WEBHOOK_SECRET` | Copy the existing value from your local backend `.env` |
| `MAIL_PROVIDER` | `disabled` |

Paste environment values without surrounding shell quotes. `CORS_ORIGINS` must be a JSON array as shown, with no trailing slash in the origin. Remove `FIREBASE_CREDENTIALS_PATH` from Vercel; a file path on your Mac cannot be read there. The code still supports that variable for local development, and JSON takes precedence when both are present.

The service-account JSON and signing secrets belong only in the backend project's environment, never in Git or any `VITE_` variable. Preserve the private key's escaped `\n` sequences in the JSON; paste the file as-is.

The event ID/deadline use the existing application defaults unless explicitly overridden. VIP remains disabled until `VIP_CODE_HASHES` is configured. Firebase sends authentication emails. Invitation/ticket mail and Sheet synchronization remain optional and disabled until configured. The separate `python -m app.worker` process does not run automatically in a Vercel Function; enabling these integrations also requires a worker host or a secured scheduled invocation.

## 4. Firebase authentication

Open Firebase project `rsvp-revamp` → Authentication:

1. In Settings → Authorized domains, add `rsvp.gdglebanon.com` (hostname only).
2. In Sign-in method, confirm Google is enabled.
3. Confirm Email/Password and its Email link (passwordless sign-in) option are enabled.
4. Keep `FIREBASE_AUTH_DOMAIN=rsvp-revamp.firebaseapp.com`; setting it to your custom frontend domain would require additional Firebase auth hosting/proxy configuration.

The app creates the email continuation URL from the domain where the user signs in. On production it returns to `https://rsvp.gdglebanon.com/?auth=callback` and includes the pending submission ID when needed.

## 5. Redeploy and verify

After saving environment variables, redeploy the latest `main` commit in **both projects**. In Vercel, open Deployments → latest deployment → Redeploy, and select Production when applicable. Confirm both deployments finish with Ready status.

Check:

1. `https://rsvp26-back-ktwg.vercel.app/health` → `{"status":"ok"}`.
2. `https://rsvp26-back-ktwg.vercel.app/ready` → `{"status":"ready"}`; this also checks Firebase connectivity.
3. `https://rsvp26-back-ktwg.vercel.app/api/config` → JSON with nonempty Firebase `apiKey` and `appId`.
4. Open `https://rsvp.gdglebanon.com/` and hard-refresh. JavaScript/CSS requests should be under `/assets/` and return 200.
5. Test Google sign-in or an email link with your own account; returning data must appear only after sign-in. Submit a test only when you intend to create a registration.

The backend's bare `/` may return FastAPI's JSON 404 because no root page is defined. Vercel's plain-text `NOT_FOUND` at `/health` indicates a deployment/framework/root-directory problem. A 500 or failed `/ready` indicates an application/environment problem: inspect the backend runtime logs. Browser CORS errors mean `CORS_ORIGINS` or the backend's public deployment access needs checking. An authentication screen or 401 from Vercel itself means deployment protection is blocking browser API access; allow public access to the intended production API.

## References

- [Vercel Vite deployment](https://vercel.com/docs/frameworks/frontend/vite)
- [Vercel FastAPI deployment](https://vercel.com/docs/frameworks/backend/fastapi)
- [Vercel environment variables](https://vercel.com/docs/environment-variables)
- [Vercel custom domains](https://vercel.com/docs/domains/working-with-domains/add-a-domain)
- [Firebase email-link authentication and authorized domains](https://firebase.google.com/docs/auth/web/email-link-auth)
