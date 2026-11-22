// The [n] markers on a verified answer (#353). Pure, so it runs under Node in
// the tests: no DOM here, and nothing that writes HTML.
//
// `segments(text, cites)` cuts the verified text into pieces, in order: either
// `{ text }` (a stretch of the answer, exactly as it was) or `{ marker: n }`
// (where citation n, 1-based in `cites`' order, is named). Putting the `text`
// pieces back together gives `text` again, character for character: a marker is
// a place between two pieces, never a change to either.
//
// A cite is found where the answer names it by its own display form (`cite`).
// Where two cites could start at the same place the longer wins ("APP 11.10"
// over "APP 11.1"); a closing quote straight after the cite stays before its
// marker. A citation with no `cite` (an unreadable one) is numbered in the list
// but has nothing to find.

export function segments(text, cites) {
  const marks = [];
  cites.forEach((needle, index) => {
    if (typeof needle === "string" && needle) marks.push({ needle, n: index + 1 });
  });
  const parts = [];
  let rest = text;
  for (;;) {
    let best = null;
    for (const mark of marks) {
      const at = rest.indexOf(mark.needle);
      if (at < 0) continue;
      if (
        !best ||
        at < best.at ||
        (at === best.at && mark.needle.length > best.mark.needle.length)
      ) {
        best = { at, mark };
      }
    }
    if (!best) {
      if (rest) parts.push({ text: rest });
      return parts;
    }
    let end = best.at + best.mark.needle.length;
    if (rest.charAt(end) === '"' || rest.charAt(end) === "”") end += 1;
    parts.push({ text: rest.slice(0, end) }, { marker: best.mark.n });
    rest = rest.slice(end);
  }
}
