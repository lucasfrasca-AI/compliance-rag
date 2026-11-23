// The Sources panel's states and what moves between them (#354). Pure, so it runs under
// Node in the tests: no DOM here. `sources.js` is a thin layer that carries a state to the
// page and an event from it.
//
// The panel is in one of two views, the list of cards (`list`) or one source shown in its
// frame (`source`), and is open or `collapsed` to a rail. The events:
//
//   open    a source link was followed: show that source, and unfold the panel
//   back    "Back to sources": the list again (collapsing is not undone by it)
//   escape  Escape: back, when a source is showing; otherwise nothing
//   toggle  the collapse button: fold or unfold, whichever view is on
//   card    a link to a card (a finding's citation): the list, unfolded, so the card is there

export const INITIAL = Object.freeze({ view: "list", collapsed: false });

/** The state after `event`, given the state before. Never changes `state`. An event it does
 * not know leaves the state as it was. */
export function next(state, event) {
  switch (event) {
    case "open":
      return { view: "source", collapsed: false };
    case "back":
      return { view: "list", collapsed: state.collapsed };
    case "escape":
      return state.view === "source" ? { view: "list", collapsed: state.collapsed } : state;
    case "toggle":
      return { view: state.view, collapsed: !state.collapsed };
    case "card":
      return { view: "list", collapsed: false };
    default:
      return state;
  }
}
