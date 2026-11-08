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
