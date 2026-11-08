// The admin pages' script (#302). Same origin, served beside the page; the CSP
// allows no inline script. htmx does the work: it posts the form and swaps in
// the server's own fragment. This handles only what htmx leaves alone, a write
// the server refused (it does not swap an error status). Text goes in with
// textContent; nothing here writes HTML.

const REFUSALS = {
  403: "You are no longer an admin of this workspace. Nothing was changed.",
  404: "That page no longer exists. Reload to see the current list.",
};

function say(text) {
  const notice = document.getElementById("admin-notice");
  if (!notice) return;
  notice.textContent = text;
  notice.hidden = false;
}

document.body.addEventListener("htmx:responseError", (event) => {
  const status = event.detail?.xhr?.status;
  if (status === 401) {
    window.location.assign("/signin");
    return;
  }
  say(REFUSALS[status] ?? `The change was not made (${status}). Reload to see the current list.`);
});

document.body.addEventListener("htmx:sendError", () => {
  say("The change could not be sent. Check your connection and reload to see the current list.");
});

// A swap that worked clears an earlier complaint.
document.body.addEventListener("htmx:afterSwap", () => {
  const notice = document.getElementById("admin-notice");
  if (notice) notice.hidden = true;
});
