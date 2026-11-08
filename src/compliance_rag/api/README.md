# The API's sessions (`session.py`, `sign_in.py`)

A page signs in with Firebase, hands the ID token to `POST /session`, and gets a
signed `__Host-session` cookie (HttpOnly, Secure, SameSite=Strict) holding only the
uid, the email and an expiry. `current_member` accepts it as well as the Bearer
header.

## The cookie is stateless

Nothing about a session is stored on the server. That has consequences:

- **Logout clears the browser's copy and nothing else.** `POST /session/logout`
  deletes the cookie in the browser that sends it. A copy of the cookie taken
  earlier (a stolen device, a leaked log) **stays valid until its expiry**: at most
  one hour, and never past the Firebase token it was made from.
- **Removing a member ends their sessions at once.** Membership (`API_MEMBERS`,
  the rule in `adapters.firebase_auth.is_member`, shared with the token verifier) is
  checked on every read, not only at sign-in.
- **Rotating `SESSION_SECRET` ends everyone's sessions.** There is no per-session
  revocation; if that is ever needed it needs a store, which is a decision.

## Roles

Two roles (ADR-0053 U6, #294): a member asks and reads; an admin also writes. Admins are
configuration, `API_ADMINS` beside `API_MEMBERS`, until a user table is worth managing.

- **An admin is a member first.** `deps.current_admin` (`CurrentAdmin`) requires the address to
  pass `is_member` against both `Settings.members` and `Settings.admins`: the members list's
  parsing, and the same case-insensitive comparison the token verifier and the cookie reader
  use. An address in `API_ADMINS` alone is refused with a 403.
- **The role is in neither credential.** The token and the cookie carry identity only, and
  `API_ADMINS` is read on every request, so removing an address takes effect on that user's next
  request, whichever credential they hold (the same property as removing a member, above).
- **Admin-only today:** `PUT` and `DELETE /v1/subscriptions/{instrument_id}` and
  `POST /v1/profile/evidence` (any member could call it before, #147). A signed-in member who is
  not an admin gets 403 `admin only`; no sign-in is still 401. Reads stay open to every member.
  ADR-0053 U6 plans the same role for policy versions, mapping approvals, withdraw and purge,
  and the questionnaire; a route that becomes admin-only takes `CurrentAdmin` in place of
  `CurrentCaller`.
- Empty `API_ADMINS` makes no one an admin: these writes are refused for everyone.

## The secret

`SESSION_SECRET` (Secret Manager in Cloud Run, `.env` locally) must be at least 32
characters; `Settings` and `SessionCodec` both refuse a shorter one. Unset, cookie
sessions are off and only the Bearer header signs anyone in. It is never logged, put
on a span or echoed in an error.

## Sign-in methods

The sign-in page offers email and password, and "Sign in with Google" (a Firebase
popup, #291). The human decided on 2026-11-08 to allow it: email and password alone is
friction for a demo. Both methods hand their Firebase ID token to the same `POST /session`,
so a Google sign-in sets the same `__Host-session` cookie and meets the same rule: the
account's email must be verified and listed in `API_MEMBERS`, and its `sign_in_provider`
must be in `API_SIGN_IN_PROVIDERS` (default `google.com,password`). A non-member Google
account gets the same 403 as a non-member password one and no cookie. Dropping `google.com`
from `API_SIGN_IN_PROVIDERS` refuses Google tokens whatever the page offers.

**What the sign-in page now loads, and only it.** The popup needs two things the shared
policy refuses, so `sign_in.sign_in_content_security_policy(auth_domain)` is the shared
policy plus exactly these, each named in a comment there:

- `script-src https://apis.google.com`: Firebase injects Google's `api.js` (gapi), and gapi
  loads its modules from the same host. It is Google's script and unversioned, so it cannot
  be pinned by hash. This is the one thing the page loads that is not served from here.
- `frame-src https://<auth domain>` (`FIREBASE_AUTH_DOMAIN`, else
  `<project>.firebaseapp.com`): Firebase frames `<auth domain>/__/auth/iframe` to receive the
  popup's result. A value that is not a hostname turns the page off (503) rather than reach a
  header.
- `connect-src` gains nothing: the SDK's calls go to `identitytoolkit.googleapis.com` and
  `securetoken.googleapis.com`, which the shared policy already names.

The Ask pages, the source view and every error response keep the shared policy byte for byte
(`tests/test_api_session.py` pins it as a literal). The script is loaded only when someone
clicks the Google button on desktop; on mobile and Safari the SDK loads it at page load to avoid
the browser blocking the popup. Console steps that are not code: enable the Google provider in
Firebase Authentication, and add the deployed host to *Authorized domains*.

## The Ask pages (`pages.py`, `templates/`, `static/ask.js`)

`/chats`, `/chats/new` and `/chats/{id}` are server-rendered (Jinja2, autoescaped, nothing
marked safe) and use htmx only to swap in the server's own fragment
(`/chats/{id}/turns`) after an answer. Every Ask page carries the one CSP
(`sign_in.CONTENT_SECURITY_POLICY`; the sign-in page alone is wider, see *Sign-in methods*): scripts, styles, images and frames from this origin
only, no inline script or style. The assets are `static/ask.js` (the stream, the progress,
the citation panel) and the vendored htmx (`static/vendor/htmx/README.md`: version, hash,
and the config that keeps it inside the CSP).

- A page request without a session redirects to `/signin`; a signed-in non-member gets a
  403 page. `GET /` redirects (303) to `/chats`.
- The fragment takes no parameter and the page shows exactly what the ledger recorded, so
  every outcome (`generation_unavailable` with its reason, `policy_corpus_too_large` with
  the estimate and the cap, #289) is the same card live and after a reload. If the ledger
  did not record the exchange, `ask.js` (which compares the chat's turn count before and
  after) says so and shows the stream's own outcome as text under "Not saved to this chat".
- Every response under `/chats` and `/admin`, errors included, carries the CSP (`pages.PageHeaders`).
- `ask.js` is not exercised by CI (no browser there). It was driven in headless Chrome
  against the app over fakes at 390px wide (ask, progress, panel, follow-up, withheld,
  failure, an unrecorded exchange, sign-out); the tests make the same requests in the same
  order.

## The generator, and the demo (`GENERATOR`, #292)

`GENERATOR` chooses what writes the answer, and nothing else about the pipeline changes (the
same retrieval, the same verifier, the same ledger):

- `claude` (the default): Claude on Vertex, built at startup through the residency gate
  (ADR-0023, ADR-0032). Refuses to start with no `VERTEX_CLAUDE_MODEL`.
- `demo`: `adapters/demo_generator.FakeDemoGenerator`, in process, no model called. It quotes
  the top retrieved clause and Acme's first policy clause verbatim, so the citations, the
  verifier and click-to-clause work; a question containing "withhold" makes it misquote, so
  the answer is withheld. `build_app` never imports `VertexClaudeGenerator` for it. It runs in
  Sydney, so `Settings` refuses `VERTEX_CLAUDE_REGION` anything but `australia-southeast1`
  with it.

**The banner** ("FAKE GENERATOR — not a real answer") is written by the server, never by
`ask.js`, from one partial (`templates/_banner.html`) and one stylesheet (`static/banner.css`);
the sign-in page, which is not a template, renders the same partial (`banner.render_banner`).
It is a `role="note"`, not an alert. Two rules decide where it shows:

- **A page** carries it while the deployment runs `demo`: the sign-in page, the chat list, the
  new-chat and chat pages, and the error page.
- **An answer turn** carries it when the ledger says the demo wrote that turn
  (`chat.generator`, ledger 0020), **whatever the deployment runs now**, whether the turn is
  verified, withheld or failed. So a chat read back after a switch to Claude still labels the
  stand-in's answers, and a Claude turn is never labelled, even on a page that is itself
  bannered because the deployment is back on `demo`. A turn written before the column holds
  NULL and shows none.

Every `answer` event reports `"generator"` (`"demo"` or `"claude"`), and each answer turn of
`GET /v1/chats/{id}` reports the one recorded with it (`null` for an old turn).

**Exempt: the source pages** (`/v1/sources/{instrument_id}/{version_id}`, the citation panel's
iframe). They show stored law and policy text, never generated text, so they carry no banner
even while the deployment is `demo`; `tests/test_demo_generator.py` pins that.

## The container (`docker/api.Dockerfile`)

`compliance-rag-api` on Cloud Run: `python:3.12-slim`, `uv sync --frozen --no-dev`, built for
`linux/amd64`, running as the non-root user `app` under
`uvicorn --factory compliance_rag.api.main:build_app --port ${PORT}`. CI does not build it
(Actions minutes); build and run it by hand:

    docker build --platform linux/amd64 -f docker/api.Dockerfile -t compliance-rag-api .

It holds the package; the files the API reads through repo-relative paths (`corpus/instruments`,
`corpus/register/instruments.yaml`, `corpus/synthetic/registry.json`,
`vendor/bge-reranker-v2-m3-tokenizer`); and the fastembed `Qdrant/bm25` model, baked in at
build time with `HF_HUB_OFFLINE=1`, so nothing is downloaded at request time. It does not hold
`.env`, `.git`, `models/`, the tests, the docs, `corpus/golden` or `corpus/mappings`
(`docker/api.Dockerfile.dockerignore`; the root `.dockerignore` is the same list without
`models/`, which the reranker image copies from). `tests/test_api_image.py` pins the COPY list
against the paths the code reads.

Configuration is environment only. Names: `ENV`, `PORT` (Cloud Run injects it), `TENANT_ID`,
`GENERATOR`, `GCP_PROJECT_ID`, `CORPUS_BUCKET`, `RERANKER_URL`, `QDRANT_URL`, `QDRANT_API_KEY`
(secret), `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` (secret), `NEO4J_DATABASE`,
`DATABASE_URL` (secret), `FIREBASE_PROJECT_ID`, `FIREBASE_WEB_API_KEY`, `API_MEMBERS`,
`SESSION_SECRET` (secret), and for `claude`, `VERTEX_CLAUDE_MODEL`, `VERTEX_CLAUDE_REGION` and
`OFFSHORE_GENERATION_TESTERS`.

## The admin panel (`admin.py`, `templates/subscriptions.html`, `static/admin.js`)

`GET /admin/subscriptions` (#302, ADR-0053 I5) is the first admin page: the library (the ledger's
`_public` environment on today's date, with the recorded titles) and this workspace's tick
against each instrument, with a Subscribe or Unsubscribe button per row. It is built like the
Ask pages: Jinja2 autoescaped, nothing marked safe, the one CSP, and every `/admin` response
carries it (`pages.PageHeaders`).

- **Admin only on every request.** Each route takes `admin.PageAdmin`: signed out redirects to
  `/signin`, a signed-in member who is not an admin (`deps.is_admin`, `API_ADMINS` read afresh)
  gets a 403 page, including on the writes. The nav shows the "Subscriptions" link only to an
  admin. The flag is set once per request, by `pages.page_caller`, and `render(request, ...)`
  reads it, so no page passes it and none can drop it; an error page shows it to an admin whose
  session resolved. Showing or hiding the link decides nothing: the routes do.
- **The panel has no rule of its own.** `POST /admin/subscriptions/{id}/subscribe|unsubscribe`
  call `tenant.subscribe_tenant` and `tenant.unsubscribe_tenant`, the operations `PUT` and
  `DELETE /v1/subscriptions/{id}` call. Unsubscribing deletes the subscription row and touches no
  clause or retrieval store (D-17); `tests/test_api_admin_subscriptions.py` snapshots both stores
  around it. Writes are unsafe methods, so the `OriginGuard` covers them.
- **htmx swaps the server's fragment.** A write carrying `HX-Request` is answered with the panel
  alone (`_subscriptions.html`), redrawn from the ledger after the write; the same form posted
  without script is answered 303 back to the page. An id the adapter refuses is said in words and
  records nothing (a 200 fragment, because htmx does not swap an error status; a 422 page without
  script). `admin.js` handles only what htmx leaves alone: a refused write (a 403 for an admin
  removed mid-session, a 401 to sign-in) and a send failure, in text via `textContent`.
- **Signed out or refused, an htmx request gets no page.** A 303 would be followed and the whole
  sign-in page swapped into the panel, so an htmx request (`HX-Request: true`) that needs sign-in
  gets a 401 with `HX-Redirect: /signin` and an empty body, and a refused one (the 403 page) gets
  only the refusal as a fragment (`_refusal.html`). Unsubscribing what was not subscribed is a
  no-op the panel says so in words (status 200); subscribing twice is silent.
- **A subscription the library no longer lists** is shown, marked, and can be removed.
- `admin.js` is not exercised by CI (no browser there), like `ask.js`; the tests make the
  requests it causes.
