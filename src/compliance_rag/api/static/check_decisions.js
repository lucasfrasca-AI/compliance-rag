// The report's decision buttons (#301). Same origin, served beside the page; the CSP
// allows no inline script. A button names one decision: data-decide is its kind
// (claim_confirmed, claim_rejected, verdict_accepted, verdict_withdrawn) and data-claim or
// data-finding the claim or finding it is about. Clicking POSTs it to
// /v1/checks/{id}/decisions as JSON, with the session cookie, then reloads the page: the
// report is drawn by the server from the ledger, never built here, so what is on screen is
// what was recorded.
//
// Nothing from the server is ever written as HTML: the one message that is shown goes in
// with textContent. The server decides everything that matters (who may decide, what is
// refused, what a decision does). This only carries the request and says what the server
// said.

const CHECK_ID = /^[0-9a-f]{32}$/;
const KINDS = ["claim_confirmed", "claim_rejected", "verdict_accepted", "verdict_withdrawn"];

const root = document.getElementById("decisions");
const notice = document.getElementById("decision-notice");

function say(text) {
  notice.replaceChildren();
  notice.hidden = text === null;
  if (text === null) return;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  notice.append(paragraph);
}

/** The body for a button: its kind and the one thing it is about. */
function bodyOf(button) {
  const kind = button.dataset.decide;
  if (!KINDS.includes(kind)) return null;
  if (kind.startsWith("claim_")) {
    const index = Number(button.dataset.claim);
    return Number.isInteger(index) && index > 0 ? { kind, claim_index: index } : null;
  }
  const finding = button.dataset.finding;
  return typeof finding === "string" && finding !== "" ? { kind, finding_id: finding } : null;
}

/** What the server said of a refusal, in its own words: `detail.message`. */
async function refusalOf(response) {
  try {
    const detail = (await response.json())?.detail;
    if (typeof detail?.message === "string") return detail.message;
    if (typeof detail === "string") return detail;
  } catch {
    // not JSON: fall through to the generic words
  }
  return `The request was refused (${response.status}).`;
}

async function decide(button) {
  const checkId = root.dataset.checkId;
  const body = bodyOf(button);
  if (!CHECK_ID.test(String(checkId)) || body === null) {
    say("This button's decision could not be read.");
    return;
  }
  const buttons = root.querySelectorAll("[data-decide]");
  for (const each of buttons) each.disabled = true;
  say(null);
  let done = false;
  try {
    const response = await fetch(`/v1/checks/${checkId}/decisions`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      window.location.assign("/signin");
      return;
    }
    if (response.ok) done = true;
    else say(await refusalOf(response));
  } catch {
    say("The connection ended before the decision was recorded. Reload this page to see what was.");
  } finally {
    for (const each of buttons) each.disabled = false;
  }
  // Recorded before it was shown: the server draws the report from the ledger.
  if (done) window.location.reload();
}

root?.addEventListener("click", (event) => {
  const target = event.target;
  const button = target instanceof Element ? target.closest("[data-decide]") : null;
  if (button !== null && root.contains(button)) decide(button);
});
