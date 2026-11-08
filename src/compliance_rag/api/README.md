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

The sign-in page offers email and password only. Firebase's Google popup loads
`apis.google.com` and is refused by the page's CSP (see
`static/vendor/firebase/README.md`); allowing it is the human's decision.

## The Ask pages (`pages.py`, `templates/`, `static/ask.js`)

`/chats`, `/chats/new` and `/chats/{id}` are server-rendered (Jinja2, autoescaped, nothing
marked safe) and use htmx only to swap in the server's own fragment
(`/chats/{id}/turns`) after an answer. Every page carries the one CSP
(`sign_in.CONTENT_SECURITY_POLICY`): scripts, styles, images and frames from this origin
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
