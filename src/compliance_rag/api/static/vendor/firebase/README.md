# Vendored: Firebase Auth web SDK

The sign-in page (`/signin`) loads these two files from this API's own origin,
never from a CDN, so the page has one origin and one CSP (ADR-0053 decision 19,
issue #280).

- Package: `firebase` on npm, **version 13.2.0** (Apache-2.0).
- Source: the package's own `firebase-app.js` and `firebase-auth.js`
  (`https://cdn.jsdelivr.net/npm/firebase@13.2.0/<file>`, byte-identical to the
  files in the npm tarball, whose sha256 is
  `3399f5767761a417f40854b927f5c1585c2ff5c71a3327b0fd1943164e866b29`).
  Downloaded once to vendor them; nothing loads from there at run time.
- Auth only: `firebase-app.js` is the app core `firebase-auth.js` needs; no other
  Firebase product is vendored.

## The one edit

`firebase-auth.js` imports the app core by an absolute gstatic URL. In the
vendored copy that specifier is rewritten to `./firebase-app.js`, so the browser
fetches it from here. Nothing else differs.

`tests/test_api_session.py` checks the hashes below against the files and that
no `gstatic.com` import remains.

| file | upstream sha256 | vendored sha256 |
| --- | --- | --- |
| `firebase-app.js` | `a67a49aeb0937561e6405f721cf486dc3085e73763716e8233082417be769d40` | `a67a49aeb0937561e6405f721cf486dc3085e73763716e8233082417be769d40` |
| `firebase-auth.js` | `f29ff093124d9173c0fce1882d595ebfd4edfb84615fd8d6a55f4579acf3756b` | `3a5f68e53e9187b80a4c205dc523bfe8507f2a94c4ef6304d9160336f516322c` |

## What the page uses of it

`signin.js` uses in-memory persistence and no popup/redirect resolver, so the SDK
never loads Google's `apis.google.com` script (that is what the popup and redirect
flows would need, and the CSP would refuse it). Email and password sign-in works;
Google popup sign-in does not, until a decision on a third-party script is made.

## Upgrading

Download the new version's two files, apply the same edit, update the version and
all four hashes here, and run `tests/test_api_session.py`.
