// The check page's run script (#300). Same origin, served beside the page; the CSP
// allows no inline script. It makes the two run requests, POST
// /v1/checks/{id}/claims and then POST /v1/checks/{id}/findings, with the session
// cookie, and shows their stages as the server-sent events arrive. When the findings
// are recorded it reloads the page: the report is drawn by the server from the ledger,
// never built here, so it is the same after a reload as on screen.
//
// Nothing from the server is ever written as HTML: text goes in with textContent. The
// server decides everything that matters (who may run, what is refused, what a finding
// is). This only carries the requests and says what the server said.

const CHECK_ID = /^[0-9a-f]{32}$/;

const $ = (id) => document.getElementById(id);
const run = $("run");
const button = $("run-check");

/** A refusal the page can say in words: the request never became a stream. */
class Refused extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;
  }
}

const STAGES = ["coverage", "claims", "findings"];

function stageItem(stage) {
  return document.querySelector(`#progress [data-stage="${stage}"]`);
}

function setStage(stage, state, detail) {
  const item = stageItem(stage);
  item.dataset.state = state;
  if (detail !== undefined) item.querySelector("[data-detail]").textContent = detail;
}

function failRunning() {
  const running = STAGES.find((s) => stageItem(s).dataset.state === "running");
  if (running) setStage(running, "failed");
}

function say(text) {
  const notice = $("run-notice");
  notice.replaceChildren();
  notice.hidden = text === null;
  if (text === null) return;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  notice.append(paragraph);
}

/** Server-sent events from a fetch body: calls `onEvent(name, data)` for each. */
async function readEvents(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = (block) => {
    let name = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length) onEvent(name, JSON.parse(data.join("\n")));
  };
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      flush(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
    }
    if (done) break;
  }
  if (buffer.trim()) flush(buffer);
}

/** What the server said of a refusal, in its own words: `detail.message`. */
async function refusalOf(response) {
  try {
    const detail = (await response.json())?.detail;
    if (typeof detail?.message === "string") {
      return new Refused(detail.message, typeof detail.error === "string" ? detail.error : "");
    }
  } catch {
    // not JSON: fall through to the generic words
  }
  return new Refused(`The request was refused (${response.status}).`, "");
}

/** POSTs a run endpoint and hands each event to `onEvent`; resolves with the stream's
 * last event name. A refusal before the stream is a `Refused`. */
async function post(path, onEvent) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "text/event-stream" },
  });
  if (response.status === 401) {
    window.location.assign("/signin");
    return "signin";
  }
  if (!response.ok || !response.body) throw await refusalOf(response);
  let last = null;
  await readEvents(response.body, (name, data) => {
    last = name;
    onEvent(name, data);
  });
  return last;
}

function countsText(counts) {
  return `${counts.applicable} apply, ${counts.not_applicable} do not, ${counts.unassessable} can't be assessed`;
}

/** The stream's own error, in the server's words (never the model's). */
let streamError = null;
function onError(data) {
  failRunning();
  streamError = typeof data.message === "string" ? data.message : "The check couldn't be completed.";
}

async function extractClaims(checkId) {
  setStage("coverage", "running");
  const last = await post(`/v1/checks/${checkId}/claims`, (name, data) => {
    if (name === "coverage") {
      setStage("coverage", "done", countsText(data.counts));
      setStage("claims", "running");
    } else if (name === "claims") {
      const n = Number(data.accepted);
      setStage("claims", "done", `${n} ${n === 1 ? "claim" : "claims"}`);
    } else if (name === "error") {
      onError(data);
    }
  });
  return last === "claims";
}

async function makeFindings(checkId) {
  setStage("findings", "running");
  const last = await post(`/v1/checks/${checkId}/findings`, (name, data) => {
    if (name === "findings") setStage("findings", "done");
    else if (name === "error") onError(data);
  });
  return last === "findings";
}

async function runCheck() {
  const checkId = run.dataset.checkId;
  if (!CHECK_ID.test(String(checkId))) {
    say("This page's check could not be read.");
    return;
  }
  button.disabled = true;
  run.setAttribute("aria-busy", "true");
  say(null);
  streamError = null;
  for (const stage of STAGES) setStage(stage, "waiting", "");
  $("progress").hidden = false;
  let finished = false;
  try {
    if (run.dataset.claims === "done") {
      setStage("coverage", "done", "already done");
      setStage("claims", "done", "already found");
    } else if (!(await extractClaims(checkId))) {
      if (streamError === null) failRunning();
      say(
        streamError ??
          "The connection ended before the claims were found. They may still be recorded: reload this page to check.",
      );
      return;
    }
    finished = await makeFindings(checkId);
    if (!finished) {
      if (streamError === null) failRunning();
      say(
        streamError ??
          "The connection ended before the findings were made. They may still be recorded: reload this page to check.",
      );
    }
  } catch (failure) {
    failRunning();
    if (failure instanceof Refused && failure.code === "already_found") {
      finished = true; // another run made them: the report is on the page after a reload
    } else if (failure instanceof Refused && failure.code === "already_extracted") {
      say(
        "This document's claims were already read, or another run is reading it now. Reload this page to see where the check stands.",
      );
    } else if (failure instanceof Refused && failure.code === "not_extracted") {
      say(
        "The document's claims are not recorded yet, so another run may still be reading it. Reload this page in a minute.",
      );
    } else {
      say(
        failure instanceof Refused
          ? failure.message
          : "The connection ended before the check finished. It may still be running: reload this page to check.",
      );
    }
  } finally {
    run.removeAttribute("aria-busy");
    button.disabled = false;
  }
  // Recorded before it was shown: the server draws the report from the ledger.
  if (finished) window.location.assign(window.location.pathname);
}

button?.addEventListener("click", () => {
  runCheck();
});
