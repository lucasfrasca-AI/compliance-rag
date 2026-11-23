// The report's "Check the document again" button (#306, ADR-0059 Q7). Same origin, served
// beside the page; the CSP allows no inline script. A button names the check it re-checks
// (data-recheck, a check id). Clicking POSTs /v1/checks/{id}/recheck with the session cookie:
// the server stores the same document as a new check, visible to the same people, and answers
// with its id. This then opens the new check at "#run-now", where its own page runs the claims
// and then the findings, as it does for any check. The report being read is never touched: an
// issued report does not change, a new check makes a new one.
//
// Nothing from the server is ever written as HTML: the one message that is shown goes in with
// textContent. The server decides everything that matters (who may re-check, what is refused);
// this only carries the request and says what the server said.

const CHECK_ID = /^[0-9a-f]{32}$/;

const notice = document.getElementById("recheck-notice");

function say(text) {
  if (!notice) return;
  notice.replaceChildren();
  notice.hidden = text === null;
  if (text === null) return;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  notice.append(paragraph);
}

/** What the server said of a refusal, in its own words: `detail.message`. */
async function refusalOf(response) {
  try {
    const detail = (await response.json())?.detail;
    if (typeof detail?.message === "string") return detail.message;
  } catch {
    // not JSON: fall through to the generic words
  }
  return `The request was refused (${response.status}).`;
}

async function recheck(button) {
  const checkId = button.dataset.recheck;
  if (!CHECK_ID.test(String(checkId))) {
    say("This button's check could not be read.");
    return;
  }
  const buttons = document.querySelectorAll("[data-recheck]");
  for (const each of buttons) each.disabled = true;
  say(null);
  let next = null;
  try {
    const response = await fetch(`/v1/checks/${checkId}/recheck`, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      window.location.assign("/signin");
      return;
    }
    if (!response.ok) {
      say(await refusalOf(response));
    } else {
      const made = await response.json();
      if (CHECK_ID.test(String(made.check_id))) next = made.check_id;
      else say("The server's answer was not understood.");
    }
  } catch {
    say("The document could not be checked again. Check your connection and try again.");
  } finally {
    for (const each of buttons) each.disabled = false;
  }
  if (next !== null) window.location.assign(`/checks/${next}#run-now`);
}

for (const button of document.querySelectorAll("[data-recheck]")) {
  button.addEventListener("click", () => {
    recheck(button);
  });
}
