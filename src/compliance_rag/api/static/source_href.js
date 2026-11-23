// Which links the Sources panel may open in its frame (#354). Pure, so it runs under
// Node in the tests: no DOM here, and nothing that writes HTML.
//
// The panel frames only this service's own source route (`/v1/sources/...`, which serves
// every stored source under its own sandboxing CSP), whatever a link says. A link carries
// the route in `data-source-href` (a claim: the document at the quote's passage) or, when
// it has none, in its own `href` (a citation, a card). Anything else is `null`: the link
// is left to do what a plain link does.

const SOURCE_PREFIX = "/v1/sources/";

/** The path to frame for a link, or `null` if it is not one the panel may open: a path that
 * begins with the source route, with no backslash or control character in it. */
export function sourceUrl(dataHref, href) {
  const candidate = typeof dataHref === "string" && dataHref !== "" ? dataHref : href;
  if (typeof candidate !== "string" || !candidate.startsWith(SOURCE_PREFIX)) return null;
  if (candidate.length === SOURCE_PREFIX.length) return null;
  if (/[\\\u0000-\u001f\u007f]/.test(candidate)) return null;
  return candidate;
}
