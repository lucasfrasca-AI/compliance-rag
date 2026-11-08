// The Ask pages' one script (#281). Same origin, served beside the page, so the
// CSP needs no inline script. It does three things:
//
//  1. streams POST /v1/answers (fetch, not EventSource, which can't POST) with
//     the session cookie, and shows stage progress from the events;
//  2. when the stream ends, has htmx swap in the server-rendered turns
//     (GET /chats/{id}/turns), so every outcome is drawn by the server's
//     templates from what the ledger recorded, never built here;
//  3. opens a citation's source in the side panel.
//
// Nothing from the server is ever written as HTML here: text goes in with
// textContent. The only HTML that reaches the page is htmx's swap of the
// server's own autoescaped fragment.

const CHAT_ID = /^[0-9a-f]{32}$/;
const SOURCE_PREFIX = "/v1/sources/";

const $ = (id) => document.getElementById(id);

// ---- sign out (every page) ------------------------------------------------------------

document.addEventListener("click", async (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target?.closest("[data-sign-out]")) return;
  try {
    await fetch("/session/logout", { method: "POST", credentials: "same-origin" });
  } finally {
    window.location.assign("/signin");
  }
});

// ---- the citation panel ---------------------------------------------------------------

function closePanel() {
  const panel = $("panel");
  if (!panel) return;
  panel.hidden = true;
  $("panel-frame").removeAttribute("src");
  document.body.classList.remove("panel-open");
}

document.addEventListener("click", (event) => {
  const panel = $("panel");
  const target = event.target instanceof Element ? event.target : null;
  const link = target?.closest("a[data-source-link]");
  if (!panel || !link) return;
  const href = link.getAttribute("href") ?? "";
  // Only this service's own source route, whatever the link says.
  if (!href.startsWith(SOURCE_PREFIX)) return;
  event.preventDefault();
  $("panel-title").textContent = link.textContent;
  // The anchor in the href scrolls the source to the clause.
  $("panel-frame").setAttribute("src", href);
  panel.hidden = false;
  document.body.classList.add("panel-open");
  $("panel-close").focus();
});

$("panel-close")?.addEventListener("click", closePanel);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePanel();
});

// ---- asking ---------------------------------------------------------------------------

const form = $("ask-form");
let fragmentFailed = false;

function setNotice(text) {
  const notice = $("notice");
  notice.replaceChildren();
  notice.hidden = text === null;
  if (text === null) return;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  notice.append(paragraph);
}

/** A refusal the page can say in words (the request never became a stream). */
class Refused extends Error {}

const STAGES = ["coverage", "retrieval", "answer"];

function stageItem(stage) {
  return document.querySelector(`#progress [data-stage="${stage}"]`);
}

function setStage(stage, state, detail) {
  const item = stageItem(stage);
  item.dataset.state = state;
  if (detail !== undefined) item.querySelector("[data-detail]").textContent = detail;
}

function resetProgress() {
  for (const stage of STAGES) setStage(stage, "waiting", "");
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

function countsText(counts) {
  return `${counts.applicable} apply, ${counts.not_applicable} do not, ${counts.unassessable} can't be assessed`;
}

const REFUSALS = {
  403: "This account is not a member of this workspace.",
  404: "This chat no longer exists.",
  422: "That question wasn't accepted. It must be 1 to 2,000 characters.",
};

async function ask(question) {
  const chat = $("chat");
  const submit = $("ask-submit");
  let chatId = chat.dataset.chatId || null;
  let finished = null; // "answer" | "error"
  let refine = "";

  setNotice(null);
  submit.disabled = true;
  form.setAttribute("aria-busy", "true");
  $("pending-question").textContent = question;
  $("pending-question").hidden = false;
  const emptyNote = document.querySelector("#turns .empty");
  if (emptyNote) emptyNote.hidden = true;
  resetProgress();
  setStage("coverage", "running");
  $("progress").hidden = false;
  $("progress-note").hidden = false;

  const adopt = (data) => {
    if (chatId || !CHAT_ID.test(String(data.chat_id))) return;
    chatId = data.chat_id;
    chat.dataset.chatId = chatId;
    window.history.replaceState(null, "", `/chats/${chatId}`);
  };

  try {
    const body = { question };
    if (chatId) body.chat_id = chatId;
    const response = await fetch("/v1/answers", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      window.location.assign("/signin");
      return;
    }
    if (!response.ok || !response.body) {
      throw new Refused(REFUSALS[response.status] ?? `The request failed (${response.status}).`);
    }
    await readEvents(response.body, (name, data) => {
      adopt(data);
      if (name === "coverage") {
        setStage("coverage", "done", countsText(data.counts));
        setStage("retrieval", "running");
      } else if (name === "retrieval") {
        setStage("retrieval", "done", `${data.candidates} clauses`);
        setStage("answer", "running");
      } else if (name === "answer") {
        setStage("answer", "done");
        finished = "answer";
      } else if (name === "error") {
        const running = STAGES.find((s) => stageItem(s).dataset.state === "running");
        if (running) setStage(running, "failed");
        finished = "error";
        if (data.error === "generation_unavailable") refine = "?refine=generation_unavailable";
        else if (data.error === "policy_corpus_too_large") {
          refine =
            `?refine=policy_corpus_too_large&estimated_tokens=${Number(data.estimated_tokens)}` +
            `&cap_tokens=${Number(data.cap_tokens)}`;
        }
      }
    });
  } catch (failure) {
    const running = STAGES.find((s) => stageItem(s).dataset.state === "running");
    if (running) setStage(running, "failed");
    setNotice(
      failure instanceof Refused
        ? failure.message
        : "The connection ended before an answer arrived. The answer may still be recorded: reload this chat to check.",
    );
  }

  if (finished && chatId) {
    // Recorded before it was sent, so the server can draw it. Then the turns
    // are the server's, and the pending question is among them.
    fragmentFailed = false;
    try {
      await window.htmx.ajax("GET", `/chats/${chatId}/turns${refine}`, {
        target: "#turns", // htmx's default swap replaces its content
      });
    } catch {
      fragmentFailed = true;
    }
    const last = document.querySelector("#turns .turn:last-child");
    if (fragmentFailed) {
      setNotice("The answer could not be shown. Reload this chat to read it.");
    } else if (!last?.classList.contains("turn-answer")) {
      // The chat has no record of this answer (the ledger refused it), so
      // there is nothing the server could draw: say so rather than show nothing.
      setNotice("This question could not be answered, and the chat could not record it. Ask again.");
    } else {
      $("pending-question").hidden = true;
      $("progress").hidden = true;
      $("progress-note").hidden = true;
      form.reset();
      last.scrollIntoView({ block: "nearest" });
    }
  }
  if (!finished) $("progress-note").hidden = true;
  submit.disabled = false;
  form.removeAttribute("aria-busy");
}

if (form) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = form.elements.question.value.trim();
    if (question) ask(question);
  });
  // A fragment the server could not give (a lapsed session) is a sign-in.
  document.body.addEventListener("htmx:responseError", (event) => {
    fragmentFailed = true;
    if (event.detail?.xhr?.status === 401) window.location.assign("/signin");
  });
}
