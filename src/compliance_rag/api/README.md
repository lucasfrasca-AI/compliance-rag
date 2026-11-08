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
  403 page. There is no page at `/`.
- The fragment takes no parameter and the page shows exactly what the ledger recorded, so
  every outcome (`generation_unavailable` with its reason, `policy_corpus_too_large` with
  the estimate and the cap, #289) is the same card live and after a reload. If the ledger
  did not record the exchange, `ask.js` (which compares the chat's turn count before and
  after) says so and shows the stream's own outcome as text under "Not saved to this chat".
- Every response under `/chats`, errors included, carries the CSP (`pages.PageHeaders`).
- `ask.js` is not exercised by CI (no browser there). It was driven in headless Chrome
  against the app over fakes at 390px wide (ask, progress, panel, follow-up, withheld,
  failure, an unrecorded exchange, sign-out); the tests make the same requests in the same
  order.
