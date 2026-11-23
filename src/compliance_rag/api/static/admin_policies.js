// The admin policies page's script (#303). Same origin, served beside the page; the CSP
// allows no inline script. It reads the chosen Markdown file as text and posts
// {filename, content, title, effective_from, backdate_reason} as JSON to the route the form
// names (data-action), with the session cookie, then reloads the list. The route is the
// policy's own, put in the page by the server: the script never works out which policy a
// document is for, and never from its title. Text goes in with textContent; nothing here
// writes HTML. The server decides everything that matters (size, format, date, reason,
// residency, who may): this carries the file and says what the server said.

/** What the server said, in its own words: `detail.message` for a refusal it named, the
 * first validation message for a 422. */
async function refusalOf(response) {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail?.message === "string") return detail.message;
    if (Array.isArray(detail) && typeof detail[0]?.msg === "string") {
      return `The policy was refused: ${detail[0].msg}.`;
    }
    if (typeof detail === "string") return `The policy was refused: ${detail}.`;
  } catch {
    // not JSON: fall through to the generic words
  }
  return `The policy was refused (${response.status}).`;
}

function say(form, text) {
  const notice = form.querySelector("[data-notice]");
  if (!notice) return;
  notice.textContent = text;
  notice.hidden = false;
}

async function upload(form) {
  const notice = form.querySelector("[data-notice]");
  if (notice) notice.hidden = true;
  const file = form.elements.file?.files?.[0];
  if (!file) {
    say(form, "Choose a file first.");
    return;
  }
  // The limit is the server's: the template hands it over in data-max-bytes.
  const maxBytes = Number(form.dataset.maxBytes);
  if (maxBytes > 0 && file.size > maxBytes) {
    say(form, `That file is over ${form.dataset.maxKib} KiB, so the server would refuse it.`);
    return;
  }
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    const body = {
      filename: file.name,
      content: await file.text(),
      title: form.elements.title.value.trim(),
    };
    const date = form.elements.effective_from.value;
    if (date) body.effective_from = date;
    const reason = form.elements.backdate_reason.value.trim();
    if (reason) body.backdate_reason = reason;
    const response = await fetch(form.dataset.action, {
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
      say(form, await refusalOf(response));
      return;
    }
    window.location.assign("/admin/policies?done=published");
  } catch {
    say(form, "The policy could not be sent. Check your connection and try again.");
  } finally {
    submit.disabled = false;
  }
}

for (const form of document.querySelectorAll("form.policy-upload")) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    upload(form);
  });
}
