/* Billie chat UI.
 *
 * Talks to the local FastAPI backend over Server-Sent Events. Every answer is
 * labelled with the model that produced it (local or Claude) and with a
 * confidence flag showing whether it was grounded in a retrieved source.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $("messages"),
    empty: $("empty"),
    form: $("form"),
    input: $("input"),
    send: $("send"),
    hint: $("hint"),
    route: $("route"),
    private: $("private"),
    memory: $("memory"),
    chipLocal: $("chip-local"),
    chipCloud: $("chip-cloud"),
    refresh: $("btn-refresh"),
    reindex: $("btn-reindex"),
    newChat: $("btn-new"),
    panel: $("panel"),
    panelOpen: $("btn-panel"),
    panelClose: $("panel-close"),
    scrim: $("scrim"),
    proposals: $("proposals"),
    permissions: $("permissions"),
    roots: $("roots"),
    chipPending: $("chip-pending"),
  };

  // A token is only needed when reaching Billie from another device (phase 6).
  const token = localStorage.getItem("billie_token") || "";
  const headers = () => {
    const h = { "Content-Type": "application/json" };
    if (token) h["Authorization"] = `Bearer ${token}`;
    return h;
  };

  let conversationId = null;
  let busy = false;

  function setHint(text, isError = false) {
    els.hint.textContent = text || "";
    els.hint.classList.toggle("error", Boolean(isError));
  }

  function addMessage(kind) {
    els.empty?.remove();
    const wrap = document.createElement("div");
    wrap.className = `msg ${kind}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    wrap.appendChild(bubble);
    els.messages.appendChild(wrap);
    els.messages.scrollTop = els.messages.scrollHeight;
    return { wrap, bubble };
  }

  function badge(text, cls) {
    const span = document.createElement("span");
    span.className = `badge ${cls}`;
    span.textContent = text;
    return span;
  }

  const CONFIDENCE = {
    grounded: ["grounded", "grounded"],
    unverified: ["unverified — no source", "unverified"],
    "self-reported-uncertain": ["Billie flagged uncertainty", "uncertain"],
  };

  function renderMeta(wrap, { routing, grounding, sources, latency_ms }) {
    const meta = document.createElement("div");
    meta.className = "meta";

    if (routing) {
      const isLocal = routing.kind === "local";
      meta.appendChild(
        badge(`${isLocal ? "local" : "cloud"} · ${routing.model}`, isLocal ? "local" : "cloud")
      );
      if (routing.private) meta.appendChild(badge("private", "private"));
      if (routing.fell_back_from) meta.appendChild(badge(`fallback from ${routing.fell_back_from}`, "unverified"));
    }
    if (grounding) {
      const [label, cls] = CONFIDENCE[grounding.confidence] || [grounding.confidence, "plain"];
      meta.appendChild(badge(label, cls));
    }
    if (latency_ms) meta.appendChild(badge(`${(latency_ms / 1000).toFixed(1)}s`, "plain"));
    wrap.appendChild(meta);

    if (routing?.reason) {
      const why = document.createElement("div");
      why.className = "why";
      why.textContent = `Routed here because: ${routing.reason}`;
      wrap.appendChild(why);
    }

    const cited = grounding?.citations || [];
    const shown = cited.length ? cited : (sources || []).map((s) => ({ title: s.title, citation: s.citation, score: s.score }));
    if (shown.length) {
      const list = document.createElement("ul");
      list.className = "sources";
      const heading = document.createElement("li");
      heading.textContent = cited.length ? "Cited:" : "Retrieved but not cited:";
      list.appendChild(heading);
      for (const source of shown) {
        const item = document.createElement("li");
        const code = document.createElement("code");
        code.textContent = source.citation;
        item.append("· ", source.title, " — ", code);
        list.appendChild(item);
      }
      wrap.appendChild(list);
    }
    if (grounding && !grounding.grounded && grounding.note) {
      const note = document.createElement("div");
      note.className = "why";
      note.textContent = grounding.note;
      wrap.appendChild(note);
    }
  }

  /* Minimal SSE reader over fetch (EventSource cannot POST). */
  async function* readEvents(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        let event = "message";
        let data = "";
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (data) {
          try {
            yield { event, data: JSON.parse(data) };
          } catch {
            /* ignore malformed frame */
          }
        }
      }
    }
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true;
    els.send.disabled = true;
    setHint("Thinking…");

    addMessage("user").bubble.textContent = text;
    const answer = addMessage("billie");
    answer.bubble.classList.add("cursor");

    const payload = {
      message: text,
      conversation_id: conversationId,
      route: els.route.value,
      private: els.private.checked,
      use_memory: els.memory.checked,
    };

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${response.status}`);
      }

      let pending = null;
      for await (const { event, data } of readEvents(response)) {
        if (event === "start") {
          conversationId = data.conversation_id;
          pending = data;
          if (data.routing) {
            setHint(`${data.routing.kind === "local" ? "Local model" : "Claude"} · ${data.routing.reason}`);
          }
        } else if (event === "delta") {
          answer.bubble.textContent += data.text;
          els.messages.scrollTop = els.messages.scrollHeight;
        } else if (event === "final") {
          answer.bubble.classList.remove("cursor");
          answer.bubble.textContent = data.answer;
          renderMeta(answer.wrap, data);
          setHint("");
        } else if (event === "error") {
          answer.bubble.classList.remove("cursor");
          answer.wrap.className = "msg error";
          answer.bubble.textContent = data.error;
          setHint(data.error, true);
        }
      }
      if (pending && answer.bubble.classList.contains("cursor")) {
        answer.bubble.classList.remove("cursor");
      }
    } catch (error) {
      answer.bubble.classList.remove("cursor");
      answer.wrap.className = "msg error";
      answer.bubble.textContent = String(error.message || error);
      setHint(String(error.message || error), true);
    } finally {
      busy = false;
      els.send.disabled = false;
      els.input.focus();
    }
  }

  async function refreshStatus() {
    try {
      const response = await fetch("/api/status", { headers: headers() });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const status = await response.json();
      const paint = (chip, info, label) => {
        chip.textContent = `${label} ${info.available ? info.model : "offline"}`;
        chip.className = `chip ${info.available ? "ok" : "off"}`;
        chip.title = info.available ? `${info.provider}: ${info.model}` : info.reason || "unavailable";
      };
      paint(els.chipLocal, status.providers.local, "local");
      paint(els.chipCloud, status.providers.cloud, "cloud");

      const pending = status.files?.pending_proposals || 0;
      els.chipPending.hidden = pending === 0;
      els.chipPending.textContent = `${pending} to approve`;
      loadPermissions(status);
    } catch (error) {
      els.chipLocal.textContent = "backend unreachable";
      els.chipLocal.className = "chip off";
      els.chipCloud.textContent = "";
    }
  }

  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = els.input.value;
    els.input.value = "";
    els.input.style.height = "auto";
    send(text);
  });

  els.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      els.form.requestSubmit();
    }
  });
  els.input.addEventListener("input", () => {
    els.input.style.height = "auto";
    els.input.style.height = `${Math.min(els.input.scrollHeight, 200)}px`;
  });

  els.private.addEventListener("change", () => {
    if (els.private.checked) {
      els.route.value = "local";
      els.route.disabled = true;
      setHint("Private: this stays on your machine. Local model only.");
    } else {
      els.route.disabled = false;
      setHint("");
    }
  });

  /* --- Files, permissions, and pending diffs --------------------------- */

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: headers(), ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    return payload;
  }

  function renderDiff(text) {
    const pre = document.createElement("pre");
    pre.className = "diff";
    for (const line of text.split("\n")) {
      const span = document.createElement("span");
      if (line.startsWith("+++") || line.startsWith("---")) span.className = "hunk";
      else if (line.startsWith("@@")) span.className = "hunk";
      else if (line.startsWith("+")) span.className = "add";
      else if (line.startsWith("-")) span.className = "del";
      span.textContent = line + "\n";
      pre.appendChild(span);
    }
    return pre;
  }

  async function resolveProposal(id, action, buttons) {
    buttons.forEach((button) => (button.disabled = true));
    try {
      await api(`/api/workspace/proposals/${id}/${action}`, { method: "POST" });
      setHint(action === "approve" ? "Change applied." : "Change rejected.");
      await Promise.all([loadProposals(), refreshStatus()]);
    } catch (error) {
      setHint(error.message, true);
      buttons.forEach((button) => (button.disabled = false));
    }
  }

  async function loadProposals() {
    let proposals = [];
    try {
      proposals = (await api("/api/workspace/proposals")).proposals;
    } catch (error) {
      els.proposals.textContent = error.message;
      return;
    }

    els.chipPending.hidden = proposals.length === 0;
    els.chipPending.textContent = `${proposals.length} to approve`;

    els.proposals.innerHTML = "";
    if (!proposals.length) {
      const empty = document.createElement("p");
      empty.className = "panel-empty";
      empty.textContent = "Nothing waiting.";
      els.proposals.appendChild(empty);
      return;
    }

    for (const proposal of proposals) {
      const card = document.createElement("div");
      card.className = "proposal";

      const head = document.createElement("div");
      head.className = "proposal-head";
      const label = document.createElement("div");
      label.className = "proposal-path";
      label.textContent = `${proposal.action}: ${proposal.path}`;
      head.appendChild(label);

      const actions = document.createElement("div");
      actions.className = "proposal-actions";
      const approve = document.createElement("button");
      approve.className = "approve";
      approve.textContent = "Approve";
      const reject = document.createElement("button");
      reject.className = "reject";
      reject.textContent = "Reject";
      approve.addEventListener("click", () => resolveProposal(proposal.id, "approve", [approve, reject]));
      reject.addEventListener("click", () => resolveProposal(proposal.id, "reject", [approve, reject]));
      actions.append(approve, reject);
      head.appendChild(actions);

      card.appendChild(head);
      if (proposal.note) {
        const note = document.createElement("div");
        note.className = "panel-note";
        note.style.padding = "8px 12px 0";
        note.textContent = proposal.note;
        card.appendChild(note);
      }
      card.appendChild(renderDiff(proposal.diff));
      els.proposals.appendChild(card);
    }
  }

  async function loadPermissions(status) {
    els.permissions.innerHTML = "";
    for (const permission of status.permissions || []) {
      const row = document.createElement("div");
      row.className = "permission";

      const text = document.createElement("div");
      const title = document.createElement("h4");
      title.textContent = permission.title;
      const description = document.createElement("p");
      description.textContent = permission.description;
      text.append(title, description);

      const toggle = document.createElement("label");
      toggle.className = "switch";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = permission.granted;
      input.addEventListener("change", async () => {
        try {
          await api(`/api/permissions/${permission.tool}`, {
            method: "POST",
            body: JSON.stringify({ granted: input.checked }),
          });
          setHint(`${permission.title}: ${input.checked ? "granted" : "revoked"}.`);
          refreshStatus();
        } catch (error) {
          input.checked = !input.checked;
          setHint(error.message, true);
        }
      });
      toggle.append(input, document.createTextNode(permission.granted ? "on" : "off"));

      row.append(text, toggle);
      els.permissions.appendChild(row);
    }

    els.roots.innerHTML = "";
    for (const root of status.files?.roots || []) {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = root.name;
      const path = document.createElement("code");
      path.textContent = ` ${root.path}`;
      item.append(name, path);
      if (!root.exists) {
        const missing = document.createElement("span");
        missing.className = "missing";
        missing.textContent = " (not found)";
        item.appendChild(missing);
      }
      els.roots.appendChild(item);
    }
  }

  function togglePanel(open) {
    els.panel.hidden = !open;
    els.scrim.hidden = !open;
    if (open) loadProposals();
  }

  els.panelOpen.addEventListener("click", () => togglePanel(true));
  els.panelClose.addEventListener("click", () => togglePanel(false));
  els.scrim.addEventListener("click", () => togglePanel(false));
  els.chipPending.addEventListener("click", () => togglePanel(true));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.panel.hidden) togglePanel(false);
  });

  els.refresh.addEventListener("click", refreshStatus);

  els.reindex.addEventListener("click", async () => {
    setHint("Re-indexing notes…");
    try {
      const response = await fetch("/api/memory/reindex", { method: "POST", headers: headers() });
      const report = await response.json();
      if (!response.ok) throw new Error(report.detail || `HTTP ${response.status}`);
      setHint(
        `Indexed ${report.indexed} new/changed, ${report.unchanged} unchanged, ` +
          `${report.removed} removed, across ${report.roots.length} folder(s).`
      );
    } catch (error) {
      setHint(`Re-index failed: ${error.message}`, true);
    }
  });

  els.newChat.addEventListener("click", () => {
    conversationId = null;
    els.messages.innerHTML = "";
    setHint("New conversation started.");
  });

  refreshStatus();
  setInterval(refreshStatus, 30000);
  els.input.focus();
})();
