// Which links the Sources panel may open in its frame (#354). Pure, so it runs under
// Node in the tests: no DOM here, and nothing that writes HTML.
//
// The panel frames only this service's own source route (`/v1/sources/...`, which serves
// every stored source under its own sandboxing CSP), whatever a link says. A link carries
// the route in `data-source-href` (a claim: the document at the quote's passage) or, when
// it has none, in its own `href` (a citation, a card). Anything else is `null`: the link
// is left to do what a plain link does.
//
// "Begins with the route" is not enough: the browser resolves dot segments before it asks,
// so `/v1/sources/../../logout` is `/logout`, and `%2e%2e` is the same. The path (what
// precedes any `?` or `#`) is therefore read segment by segment, and one that is empty
// (`//`, a trailing slash), a `.` or `..` in any spelling, or an encoded slash or backslash
// is refused. The fragment is the clause's anchor and takes no part in the route.

export const SOURCE_PREFIX = "/v1/sources/";

/** Whether `path` (no query, no fragment) is a path under the source route: every segment
 * after the prefix is non-empty and is neither `.` nor `..`, however it is spelled, and no
 * slash or backslash is hidden in a segment by percent-encoding. */
export function isSourcePath(path) {
  if (!path.startsWith(SOURCE_PREFIX)) return false;
  if (/%(2f|5c)/i.test(path)) return false;
  return path
    .slice(SOURCE_PREFIX.length)
    .split("/")
    .every((segment) => {
      if (segment === "") return false;
      const plain = segment.replace(/%2e/gi, ".");
      return plain !== "." && plain !== "..";
    });
}

/** The path to frame for a link, or `null` if it is not one the panel may open: a path
 * under the source route (`isSourcePath`), with no backslash or control character in it. */
export function sourceUrl(dataHref, href) {
  const candidate = typeof dataHref === "string" && dataHref !== "" ? dataHref : href;
  if (typeof candidate !== "string") return null;
  if (/[\\\u0000-\u001f\u007f]/.test(candidate)) return null;
  if (!isSourcePath(candidate.split(/[?#]/, 1)[0])) return null;
  return candidate;
}
