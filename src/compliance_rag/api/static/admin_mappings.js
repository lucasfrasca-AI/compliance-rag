// The mapping queue page's script (#305). Same origin, served beside the page; the CSP
// allows no inline script. A "Propose mappings" button asks the server for one policy
// version's proposals (POST /v1/policy-mappings/proposals, with the session cookie), waits
// for the server-sent stream to end, and reloads the page: the page is drawn by the server
// from the ledger, never built here, so it is the same after a reload as on screen.
//
// Nothing from the server is ever written as HTML: text goes in with textContent. The server
// decides everything that matters (who may ask, what is refused, what a proposal is). This
// only carries the request and says what the server said.

const notice = document.getElementById("admin-notice");

function say(text) {
  if (!notice) return;
  notice.textContent = text ?? "";
  notice.hidden = text === null;
}

/** The server's own words for a run that ended in an `error` event, or null if it did not. */
function errorIn(stream) {
  for (const block of stream.replace(/\r\n/g, "\n").split("\n\n")) {
    let name = "";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (name !== "error" || data.length === 0) continue;
    try {
      const message = JSON.parse(data.join("\n"))?.message;
      if (typeof message === "string") return message;
    } catch {
      // not JSON: the generic words below
    }
    return "The run could not be completed.";
  }
  return null;
}

/** What the server said of a refusal before any stream: `detail.message`. */
async function refusalIn(response) {
  try {
    const detail = (await response.json())?.detail;
    if (typeof detail?.message === "string") return detail.message;
  } catch {
    // not JSON: the generic words below
  }
  return `The request was refused (${response.status}).`;
}

async function propose(form) {
  const button = form.querySelector("button");
  button.disabled = true;
  form.setAttribute("aria-busy", "true");
  say(null);
  let reload = false;
  try {
    const response = await fetch("/v1/policy-mappings/proposals", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({
        instrument_id: form.dataset.instrument,
        version_id: form.dataset.version,
      }),
    });
    if (response.status === 401) {
      window.location.assign("/signin");
      return;
    }
    if (!response.ok) {
      say(await refusalIn(response));
    } else {
      const failure = errorIn(await response.text());
      if (failure === null) reload = true;
      else say(failure);
    }
  } catch {
    say("The connection ended before the run finished. It may still be running: reload this page to check.");
  } finally {
    form.removeAttribute("aria-busy");
    button.disabled = false;
  }
  // Recorded before it was shown: the server draws the list from the ledger.
  if (reload) window.location.assign(window.location.pathname);
}

for (const form of document.querySelectorAll("form.propose")) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    propose(form);
  });
}
