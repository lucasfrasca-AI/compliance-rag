// The Sources panel's one script (#354, ADR-0053 I1 and I2), shared by the chat page and
// every check page (`templates/_sources_panel.html`). Same origin, served beside the page,
// so the CSP needs no inline script. It does three things:
//
//  1. a click on a `[data-source-link]` (a citation in an answer, a card's "Open at the
//     clause", a claim, a finding's target) shows that source in the panel's own frame, at
//     the anchor the link names. Only this service's own source route is ever framed
//     (`source_href.js`); the source is served under its own sandboxing CSP, and the
//     frame is `sandbox=""` besides. The link itself is the fallback: without script it
//     opens in a new tab;
//  2. "Back to sources" (or Escape) returns from the source to the cards;
//  3. the panel collapses to a rail and back.
//
// What state follows what event is `panel_state.js`'s, pure and tested under Node; this file
// carries the state to the page and the events from it. Nothing from the server is ever
// written as HTML here: text goes in with textContent, and the frame's `src` is set from a
// link that has passed `sourceUrl`.

import { INITIAL, next } from "./panel_state.js";
import { sourceUrl } from "./source_href.js";

const panel = document.getElementById("sources-panel");

if (panel) {
  const part = (name) => panel.querySelector(`[${name}]`);
  const frame = part("data-sp-frame");
  const title = part("data-sp-title");
  const fallback = part("data-sp-fallback");
  const back = part("data-sp-close");
  const toggle = part("data-sp-toggle");
  const body = document.getElementById("sp-body");
  let state = INITIAL;
  let opener = null;

  /** Carries `state` to the page. */
  function show() {
    panel.dataset.view = state.view;
    if (state.view === "list") frame.removeAttribute("src");
    body.hidden = state.collapsed;
    document.body.classList.toggle("sources-collapsed", state.collapsed);
    toggle.setAttribute("aria-expanded", String(!state.collapsed));
    toggle.textContent = state.collapsed ? "Show sources" : "Collapse";
  }

  /** Moves to the state `event` leads to, and carries it to the page. */
  function go(event) {
    const before = state;
    state = next(state, event);
    show();
    return before;
  }

  /** The source's name: the link's own title (a citation, a card and a claim carry one),
   * else the link's text. */
  const titleOf = (link) => (link.dataset.title || link.textContent || "").trim();

  function leaveSource(event) {
    const before = go(event);
    if (before.view !== "source" || state.view !== "list") return false;
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    else toggle.focus({ preventScroll: true });
    opener = null;
    return true;
  }

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    // A citation in a finding jumps to its card (`#src-N`): make sure the card is on show.
    if (target?.closest("a[data-source-card]")) {
      go("card");
      return;
    }
    const link = target?.closest("a[data-source-link]");
    if (!link || event.defaultPrevented) return;
    // A modified click is the reader asking for a tab: leave it to the browser.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    const url = sourceUrl(link.dataset.sourceHref, link.getAttribute("href"));
    if (url === null) return;
    event.preventDefault();
    opener = link;
    title.textContent = titleOf(link);
    fallback.setAttribute("href", url);
    // The anchor in the url scrolls the source to the clause.
    frame.setAttribute("src", url);
    go("open");
    // Stacked under the page on a phone: bring it into view.
    panel.scrollIntoView({ block: "nearest" });
    back.focus({ preventScroll: true });
  });

  back.addEventListener("click", () => leaveSource("back"));
  toggle.addEventListener("click", () => go("toggle"));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") leaveSource("escape");
  });
}
