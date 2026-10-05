"use strict";

// HR Assistant front end. No frameworks; every piece of server text is escaped before rendering.

const CSRF = { "X-Requested-With": "hr-chat" };
const $ = (id) => document.getElementById(id);
const state = { threadId: null, busy: false, turn: null };

const LOGO = '<img class="logo" src="/static/logo.png" alt="">';

const ICONS = {
  chevron: '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
  copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>',
  speaker: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
  stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  shield: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/><path d="M12 8v5M12 16h.01"/></svg>',
};

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// ---------------------------------------------------------------- markdown (safe subset)
// Text is escaped before any markup is added, so model output can never inject HTML.

function inline(raw) {
  // Split out `code` spans first so their content gets no further formatting.
  return String(raw).split(/(`[^`\n]+`)/).map((part) => {
    if (/^`[^`]+`$/.test(part)) return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
    return escapeHtml(part)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*(?!\s)([^*\n]+?)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>")
      .replace(/\[([^\]]{3,200})\]/g, '<span class="cite">$1</span>');
  }).join("");
}

const RE = {
  fence: /^\s*```\s*([\w+-]*)\s*$/,
  heading: /^\s*#{1,6}\s+(.+?)\s*#*\s*$/,
  rule: /^\s*([-*_])(\s*\1){2,}\s*$/,
  quote: /^\s*>\s?(.*)$/,
  bullet: /^(\s*)([-*•])\s+(.*)$/,
  ordered: /^(\s*)(\d+)[.)]\s+(.*)$/,
  tableRow: /^\s*\|.*\|\s*$/,
  tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
};

function cells(row) {
  return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function renderMarkdown(text) {
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let para = [];
  let list = null; // { tag, items: [{ text, nested }] }
  const flushPara = () => { if (para.length) html += `<p>${para.map(inline).join("<br>")}</p>`; para = []; };
  const flushList = () => {
    if (list) html += `<${list.tag}>${list.items.map((i) => `<li${i.nested ? ' class="nested"' : ""}>${inline(i.text)}</li>`).join("")}</${list.tag}>`;
    list = null;
  };
  const flush = () => { flushPara(); flushList(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if ((m = line.match(RE.fence))) {
      flush();
      const body = [];
      for (i++; i < lines.length && !RE.fence.test(lines[i]); i++) body.push(lines[i]);
      html += `<div class="codebox"><div class="codebox-head"><span>${escapeHtml(m[1] || "text")}</span>` +
        `<button type="button" class="icon-btn" data-copy-code title="Copy">${ICONS.copy}</button></div>` +
        `<pre><code>${escapeHtml(body.join("\n"))}</code></pre></div>`;
    } else if (RE.tableRow.test(line) && RE.tableSep.test(lines[i + 1] || "")) {
      flush();
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && RE.tableRow.test(lines[i]); i++) rows.push(cells(lines[i]));
      i--;
      html += '<div class="table-wrap"><table><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join("") +
        "</tr></thead><tbody>" + rows.map((r) => `<tr>${head.map((_, k) => `<td>${inline(r[k] ?? "")}</td>`).join("")}</tr>`).join("") +
        "</tbody></table></div>";
    } else if (RE.quote.test(line)) {
      flush();
      const body = [];
      for (; i < lines.length && RE.quote.test(lines[i]); i++) body.push(lines[i].match(RE.quote)[1]);
      i--;
      html += `<div class="callout">${renderMarkdown(body.join("\n"))}</div>`;
    } else if (RE.rule.test(line)) {
      flush();
      html += "<hr>";
    } else if ((m = line.match(RE.heading))) {
      flush();
      html += `<h3>${inline(m[1])}</h3>`;
    } else if ((m = line.match(RE.bullet) || line.match(RE.ordered))) {
      flushPara();
      const tag = RE.bullet.test(line) ? "ul" : "ol";
      const nested = m[1].length >= 2;
      if (!list || (list.tag !== tag && !nested)) { flushList(); list = { tag, items: [] }; }
      list.items.push({ text: m[3], nested });
    } else if (!line.trim()) {
      flush();
    } else if (list && /^\s{2,}\S/.test(line)) {
      list.items[list.items.length - 1].text += " " + line.trim(); // continuation of a list item
    } else {
      flushList();
      para.push(line);
    }
  }
  flush();
  return html;
}

// Plain text for copying and reading aloud.
function plainText(markdown, { speech = false } = {}) {
  let text = String(markdown ?? "")
    .replace(/```[\w+-]*\n?/g, "")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^\s*#{1,6}\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*\|?\s*:?-{2,}.*$/gm, "");
  if (speech) {
    text = text
      .replace(/\s*\[[^\]]{3,200}\]/g, "") // citations are for reading, not listening
      .replace(/\|/g, ", ")
      .replace(/^\s*([-*•]|\d+[.)])\s+/gm, "");
  }
  return text.replace(/\n{3,}/g, "\n\n").trim();
}

// ---------------------------------------------------------------- api

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).error?.message || message; } catch { /* keep default */ }
    const err = new Error(message);
    err.status = response.status;
    throw err;
  }
  return response;
}

// ---------------------------------------------------------------- sign-in

async function showLogin() {
  $("chat-view").hidden = true;
  $("user-area").hidden = true;
  $("login-view").hidden = false;
  const params = new URLSearchParams(location.search);
  if (params.get("login_error")) {
    $("login-error").textContent = params.get("login_error") === "no_record"
      ? "Your account has no HR record. Contact HR."
      : "Sign-in failed. Use your company Google account.";
    $("login-error").hidden = false;
    history.replaceState(null, "", "/");
  }
  try {
    const config = await (await api("/auth/config")).json();
    $("google-login").hidden = !config.google_enabled;
    $("google-domain").textContent = config.google_enabled ? `(@${config.allowed_domain})` : "";
    $("dev-login").hidden = !config.dev_login_enabled;
    $("no-login").hidden = config.google_enabled || config.dev_login_enabled;
    const list = $("demo-users");
    list.replaceChildren();
    for (const user of config.demo_users) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = `<span>${escapeHtml(user.name)}</span><span class="meta">${escapeHtml(user.designation)} · ${escapeHtml(user.location)}</span>`;
      button.addEventListener("click", () => devLogin(user.email));
      li.append(button);
      list.append(li);
    }
  } catch (err) {
    $("login-error").textContent = err.message;
    $("login-error").hidden = false;
  }
}

async function devLogin(email) {
  try {
    await api("/auth/dev-login", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify({ email }),
    });
    await boot();
  } catch (err) {
    $("login-error").textContent = err.message;
    $("login-error").hidden = false;
  }
}

async function logout() {
  await api("/auth/logout", { method: "POST", headers: CSRF }).catch(() => {});
  sessionStorage.removeItem("threadId");
  location.reload();
}

// ---------------------------------------------------------------- chat rendering

function el(tag, className, html) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function scrollToEnd() {
  const scroller = $("scroller");
  scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
}

function addUser(text) {
  $("welcome")?.remove();
  $("messages").append(el("div", "msg-user", escapeHtml(text)));
  scrollToEnd();
}

// One assistant turn: a collapsible activity block ("thinking"), then the answer.
function newTurn() {
  $("welcome")?.remove();
  const turn = el("div", "turn");
  turn.append(el("div", "avatar", LOGO));
  const activity = el("details", "activity working");
  activity.innerHTML =
    `<summary>${ICONS.chevron}<span class="label">Thinking…</span></summary>` +
    '<div class="activity-body"><ol class="steps"></ol></div>';
  activity.querySelector("summary").addEventListener("click", (e) => {
    if (activity.classList.contains("working")) e.preventDefault();
  });
  turn.append(activity);
  $("messages").append(turn);
  scrollToEnd();
  return turn;
}

function setLabel(turn, text) {
  turn.querySelector(".activity .label").textContent = text;
}

function describeArgs(args) {
  const parts = Object.entries(args || {})
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`);
  return parts.join(", ");
}

function addStep(turn, text, cls, tool) {
  const li = el("li", cls);
  li.append(el("span", "", escapeHtml(text)));
  if (tool) li.dataset.tool = tool;
  turn.querySelector(".steps").append(li);
  return li;
}

function finishActivity(turn, result) {
  const activity = turn.querySelector(".activity");
  activity.classList.remove("working");
  const tools = result.tools_used || [];
  let label = tools.length ? `Used ${tools.length} tool${tools.length > 1 ? "s" : ""}` : "Answered directly";
  if (result.guard && result.guard !== "allow") label = `Handled by a guardrail (${result.guard})`;
  if (result.fallback) label = "The AI service had a problem";
  setLabel(turn, label);

  // Show the arguments the model chose for each tool (explainability).
  const steps = [...activity.querySelectorAll(".steps li[data-tool]")];
  tools.forEach((t, k) => {
    const li = steps[k];
    const args = describeArgs(t.args);
    if (li && args && !li.querySelector(".args")) li.append(el("span", "args", escapeHtml(args)));
  });

  const body = activity.querySelector(".activity-body");
  body.querySelector(".extra")?.remove();
  const extra = el("div", "extra");
  if (result.citations?.length) {
    extra.innerHTML += `<h4>Sources</h4><ul class="sources">${result.citations.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>`;
  }
  extra.innerHTML += `<div class="meta">${result.input_tokens.toLocaleString()} in · ${result.output_tokens.toLocaleString()} out tokens · prompt ${escapeHtml(result.prompt_version)}</div>`;
  body.append(extra);
}

function addAnswer(container, markdown) {
  const answer = el("div", "answer", renderMarkdown(markdown));
  const actions = el("div", "actions");
  const copy = el("button", "icon-btn", ICONS.copy);
  copy.type = "button";
  copy.title = "Copy";
  copy.addEventListener("click", () => copyText(plainText(markdown), copy));
  actions.append(copy);
  if ("speechSynthesis" in window) {
    const speak = el("button", "icon-btn", ICONS.speaker);
    speak.type = "button";
    speak.title = "Read aloud";
    speak.addEventListener("click", () => readAloud(plainText(markdown, { speech: true }), speak));
    actions.append(speak);
  }
  container.append(answer, actions);
  scrollToEnd();
}

function addNotice(container, text) {
  container.append(el("div", "notice", escapeHtml(text)));
  scrollToEnd();
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    button.innerHTML = ICONS.check;
    setTimeout(() => { button.innerHTML = ICONS.copy; }, 1500);
  } catch {
    button.title = "Copy failed";
  }
}

let speaking = null;
function readAloud(text, button) {
  const synth = window.speechSynthesis;
  const wasThis = speaking === button;
  synth.cancel();
  if (speaking) { speaking.innerHTML = ICONS.speaker; speaking.classList.remove("active"); speaking = null; }
  if (wasThis) return; // second click stops
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.02;
  const done = () => { if (speaking === button) { button.innerHTML = ICONS.speaker; button.classList.remove("active"); speaking = null; } };
  utterance.onend = done;
  utterance.onerror = done;
  speaking = button;
  button.innerHTML = ICONS.stop;
  button.classList.add("active");
  synth.speak(utterance);
}

function showConfirmation(turn, action) {
  const card = el("div", "confirm");
  card.innerHTML =
    `<div class="title">${ICONS.shield}Please confirm</div><div>${escapeHtml(action.summary)}</div>` +
    '<div class="buttons"><button class="primary" type="button">Approve</button><button type="button">Reject</button></div>';
  const [approve, reject] = card.querySelectorAll("button");
  const decide = (approved) => {
    approve.disabled = reject.disabled = true;
    card.classList.add("done");
    card.querySelector(".title").innerHTML = `${ICONS.shield}${approved ? "Approved" : "Rejected"}`;
    const activity = turn.querySelector(".activity");
    activity.classList.add("working");
    activity.open = false;
    setLabel(turn, approved ? "Carrying it out…" : "Cancelling…");
    stream("/api/chat/confirm", { thread_id: state.threadId, approved }, turn);
  };
  approve.addEventListener("click", () => decide(true));
  reject.addEventListener("click", () => decide(false));
  turn.append(card);
  scrollToEnd();
}

// ---------------------------------------------------------------- streaming

async function stream(path, body, turn) {
  setBusy(true);
  try {
    const response = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify(body),
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        if (frame.startsWith("data: ")) handleEvent(JSON.parse(frame.slice(6)), turn);
      }
    }
  } catch (err) {
    if (err.status === 401) return showLogin();
    endWithError(turn, err.message);
  } finally {
    if (turn.querySelector(".activity.working") && !turn.querySelector(".confirm:not(.done)")) {
      endWithError(turn, "The connection ended unexpectedly. Please try again.");
    }
    setBusy(false);
  }
}

function endWithError(turn, message) {
  const activity = turn.querySelector(".activity");
  activity.classList.remove("working");
  setLabel(turn, "Stopped");
  addNotice(turn, message);
}

function handleEvent(event, turn) {
  if (event.type === "thread") {
    state.threadId = event.thread_id;
    sessionStorage.setItem("threadId", event.thread_id);
  } else if (event.type === "tool_call") {
    addStep(turn, event.label, "running", event.tool);
    setLabel(turn, `${event.label}…`);
  } else if (event.type === "tool_result") {
    const li = [...turn.querySelectorAll(".steps li[data-tool]")]
      .find((node) => node.dataset.tool === event.tool && /running|waiting/.test(node.className));
    if (li) {
      li.className = event.ok ? "ok" : "fail";
      if (!event.ok) {
        const why = event.error_code === "declined_by_user" ? "declined" : event.error_code;
        li.firstChild.textContent += ` (${why})`;
      }
    }
    setLabel(turn, "Thinking…");
  } else if (event.type === "result") {
    if (event.status === "awaiting_confirmation" && event.pending_action) {
      const pending = [...turn.querySelectorAll(".steps li.running")].pop();
      if (pending) pending.className = "waiting";
      const activity = turn.querySelector(".activity");
      activity.classList.remove("working");
      setLabel(turn, "Waiting for your approval");
      showConfirmation(turn, event.pending_action);
    } else {
      finishActivity(turn, event);
      addAnswer(turn, event.answer || "(no answer)");
    }
  } else if (event.type === "error") {
    endWithError(turn, event.message);
  }
}

function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy;
  $("input").disabled = busy;
  if (!busy) $("input").focus();
}

function send(text) {
  const message = text.trim();
  if (!message || state.busy) return;
  addUser(message);
  $("input").value = "";
  autoGrow();
  stream("/api/chat", { message, thread_id: state.threadId || undefined }, newTurn());
}

function autoGrow() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

async function restoreThread() {
  const saved = sessionStorage.getItem("threadId");
  if (!saved) return;
  try {
    const data = await (await api(`/api/chat/${encodeURIComponent(saved)}/messages`)).json();
    state.threadId = saved;
    for (const m of data.messages) {
      if (m.role === "user") addUser(m.content);
      else {
        $("welcome")?.remove();
        const turn = el("div", "turn");
        turn.append(el("div", "avatar", LOGO));
        $("messages").append(turn);
        addAnswer(turn, m.content);
      }
    }
  } catch {
    sessionStorage.removeItem("threadId");
  }
}

// ---------------------------------------------------------------- boot

function greet(name) {
  const hour = new Date().getHours();
  const part = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const first = String(name || "").split(" ")[0];
  $("greeting").textContent = first ? `${part}, ${first}. How can I help?` : "How can I help?";
}

async function boot() {
  try {
    const me = await (await api("/auth/me")).json();
    $("login-view").hidden = true;
    $("chat-view").hidden = false;
    $("user-area").hidden = false;
    $("user-name").textContent = me.name;
    $("user-location").textContent = me.location;
    greet(me.name);
    await restoreThread();
    $("input").focus();
  } catch {
    showLogin();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); send($("input").value); });
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("input").value); }
  });
  $("input").addEventListener("input", autoGrow);
  $("suggestions").addEventListener("click", (e) => {
    if (e.target.classList.contains("chip")) send(e.target.textContent);
  });
  $("messages").addEventListener("click", (e) => {
    const button = e.target.closest("[data-copy-code]");
    if (button) copyText(button.closest(".codebox").querySelector("code").textContent, button);
  });
  $("logout").addEventListener("click", logout);
  $("new-chat").addEventListener("click", () => {
    window.speechSynthesis?.cancel();
    sessionStorage.removeItem("threadId");
    location.reload();
  });
  boot();
});
