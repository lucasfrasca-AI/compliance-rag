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
  403 page. `GET /` redirects (303) to `/chats`.
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

While it is `demo`, **every** server-rendered page says so, by the server and not by
`ask.js`: a "FAKE GENERATOR — not a real answer" banner on the sign-in page, the chat list,
the new-chat and chat pages, the error page, and each answer turn of the turns fragment,
whatever its outcome (verified, withheld, or failed). Every `answer` event reports
`"generator": "demo"` (`"claude"` otherwise). The ledger does not record which generator wrote
a turn, so a chat read back later (`GET /v1/chats/{id}`) does not say; its pages carry the
banner for as long as the deployment is `demo`.

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
