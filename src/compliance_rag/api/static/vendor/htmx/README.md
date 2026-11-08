# Vendored: htmx

The Ask pages (`/chats`, issue #281) load htmx from this API's own origin, never
from a CDN, so every page has one origin and one CSP (ADR-0053 decision 19).

- Package: `htmx.org` on npm, **version 2.0.11** (0BSD, `LICENSE` beside it).
- Source: the package's own `dist/htmx.min.js`, byte-identical to the file in the
  npm tarball `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz`
  (sha1 `a85363c4c5af94cd634df04c4b83d06dd0e91aa7`, sha512 integrity
  `Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==`,
  both as the registry publishes them and checked against the download) and to the
  copy at `https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js`.
  Downloaded once to vendor it; nothing loads from there at run time.
- Not edited.

| file | sha256 |
| --- | --- |
| `htmx.min.js` | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

`tests/test_api_pages.py` checks the hash against the file.

## Configured to run under the CSP

htmx's defaults would break a strict CSP, so every page carries an `htmx-config`
meta tag (`templates/base.html`) that turns them off:

- `includeIndicatorStyles: false`: htmx otherwise inserts an inline `<style>`,
  which `style-src 'self'` refuses;
- `allowEval: false` and `allowScriptTags: false`: no `hx-on`, no `js:` values,
  no script in a swapped response;
- `selfRequestsOnly: true`: it requests this origin only;
- `historyEnabled: false`: no page snapshots in `localStorage`.

The pages use it for one thing: swapping a server-rendered, autoescaped fragment
(`GET /chats/{id}/turns`) into the chat after an answer.

## Upgrading

Download the new version's `dist/htmx.min.js` from the npm tarball, update the
version and hash here, and run `tests/test_api_pages.py`.
