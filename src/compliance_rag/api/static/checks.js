// The Check page's script (#297). Same origin, served beside the page; the CSP
// allows no inline script. It reads the chosen file as text and posts
// {filename, content, title} to POST /v1/checks with the session cookie, then
// opens the check the server made. Text goes in with textContent; nothing here
// writes HTML. The server decides everything that matters (size, format,
// residency, who may see it): this only carries the file and says what the
// server said.

// Mirrors the server's check id (`deps.UUID4_HEX`): only to refuse an answer that
// is not one before navigating on it. The server decides what an id is.
const CHECK_ID = /^[0-9a-f]{32}$/;

function say(text) {
  const notice = document.getElementById("check-notice");
  if (!notice) return;
  notice.textContent = text;
  notice.hidden = false;
}

/** What the server said, in its own words: `detail.message` for a refusal it
 * named, the first validation message for a 422. */
async function refusalOf(response) {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail?.message === "string") return detail.message;
    if (Array.isArray(detail) && typeof detail[0]?.msg === "string") {
      return `The document was refused: ${detail[0].msg}.`;
    }
  } catch {
    // not JSON: fall through to the generic words
  }
  return `The document was refused (${response.status}).`;
}

const form = document.getElementById("check-form");
form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.getElementById("check-file")?.files?.[0];
  const title = document.getElementById("check-title")?.value.trim();
  const submit = document.getElementById("check-submit");
  const notice = document.getElementById("check-notice");
  if (notice) notice.hidden = true;
  if (!file) {
    say("Choose a file first.");
    return;
  }
  // The limit is the server's: the template hands it over in data-max-bytes.
  const maxBytes = Number(form.dataset.maxBytes);
  if (maxBytes > 0 && file.size > maxBytes) {
    say(`That file is over ${form.dataset.maxKib} KiB, so the server would refuse it.`);
    return;
  }
  submit.disabled = true;
  try {
    const body = { filename: file.name, content: await file.text() };
    if (title) body.title = title;
    const response = await fetch("/v1/checks", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      window.location.assign("/signin");
      return;
    }
    if (!response.ok) {
      say(await refusalOf(response));
      return;
    }
    const made = await response.json();
    if (!CHECK_ID.test(String(made.check_id))) {
      say("The server's answer was not understood.");
      return;
    }
    window.location.assign(`/checks/${made.check_id}`);
  } catch {
    say("The document could not be sent. Check your connection and try again.");
  } finally {
    submit.disabled = false;
  }
});
