/* The shell owns all editing and persistence. Document content stays in an opaque iframe. */
(() => {
  const $ = (selector) => document.querySelector(selector);
  const key = $('meta[name="markup-key"]').content,
    reviewId = $('meta[name="review-id"]').content;
  const frame = $("#document"),
    editor = $("#editor");
  let review,
    version,
    round,
    mode = "comment",
    editing,
    selectedVersion,
    selectedRound,
    busy = false;
  let frameVersion,
    recovered = false;
  const locked = () =>
    Boolean(round?.submitted) ||
    version?.number !== review?.versions.at(-1).number;
  function error(message) {
    $("#error").textContent = message;
    $("#error").hidden = false;
  }
  async function api(operation = "", body) {
    const res = await fetch(
      `/api/review/${reviewId}${operation ? "/" + operation : ""}`,
      {
        method: body ? "POST" : "GET",
        headers: { "Content-Type": "application/json", "X-Markup-Key": key },
        body: body ? JSON.stringify(body) : undefined,
      },
    );
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    return data;
  }
  const packet = () => ({
    version: version.number,
    round: round.id,
    revision: round.revision,
  });
  const post = (value) =>
    frame.contentWindow?.postMessage(
      { channel: "starfleet-markup", ...value },
      "*",
    );
  function syncFrame() {
    post({ type: "state", comments: round.comments, mode, locked: locked() });
  }
  function option(value, text) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    return o;
  }
  function render() {
    $("#title").textContent = review.title;
    document.title = review.title + " | Starfleet Markup";
    $("#version").replaceChildren(
      ...review.versions.map((v) =>
        option(
          v.number,
          `${v.number}${v.number === review.versions.at(-1).number ? " · latest" : ""}`,
        ),
      ),
    );
    $("#version").value = version.number;
    $("#round").replaceChildren(
      ...version.rounds.map((r, i) =>
        option(r.id, `${i + 1} · ${r.submitted ? "sent" : "draft"}`),
      ),
    );
    $("#round").value = round.id;
    $("#count").textContent = round.comments.length;
    $("#send").disabled = busy || locked() || !round.comments.length;
    $("#send").textContent = round.submitted
      ? "Feedback sent"
      : "Send to Agent";
    const delivery = round.delivery?.state;
    $("#status").textContent = round.received
      ? "Agent received feedback"
      : delivery === "queued"
        ? "Wake message queued · awaiting receipt"
        : ["pending", "sending"].includes(delivery)
          ? "Feedback saved · notifying agent"
          : delivery === "failed"
            ? "Feedback saved · notification failed"
            : delivery === "uncertain"
              ? "Feedback saved · notification needs retry"
              : round.submitted
                ? "Submitted · awaiting agent receipt"
                : "Draft saved on this Mac";
    $("#retry-wake").hidden =
      round.received || !["failed", "uncertain"].includes(delivery);
    $("#retry-wake").disabled = busy;
    $("#new-round").hidden =
      !round.submitted ||
      version.number !== review.versions.at(-1).number ||
      round.id !== version.rounds.at(-1).id;
    $("#banner").hidden = !locked();
    $("#banner").textContent =
      version.number !== review.versions.at(-1).number
        ? "You are viewing an earlier document version. Its comments are preserved and read-only."
        : "This round has been sent and is locked. You can view your comments or start another round.";
    if (!$("#retry-wake").hidden)
      $("#banner").textContent =
        "Your feedback is saved. The agent notification did not complete. Use Retry notification to try again.";
    $("#hint").textContent =
      mode === "comment"
        ? "Click highlights or numbered comment markers to read feedback. Select a paragraph or text to add a comment."
        : "Click mode is on. Document links and controls work normally.";
    const list = $("#comment-list");
    list.replaceChildren();
    if (!round.comments.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent =
        "Your comments will appear here. Choose something in the document to get started.";
      list.append(p);
    }
    round.comments.forEach((comment, index) => {
      const card = document.createElement("article");
      card.className = "comment-card";
      card.id = `comment-${comment.id}`;
      const meta = document.createElement("div");
      meta.className = "comment-meta";
      const number = document.createElement("span");
      number.className = "comment-number";
      number.textContent = index + 1;
      meta.append(number);
      const label = document.createElement("span");
      label.textContent =
        comment.anchor.kind === "text" ? "Selected text" : "Document element";
      meta.append(label);
      if (comment.intent) {
        const intent = document.createElement("span");
        intent.className = "intent-tag";
        intent.textContent =
          comment.intent === "discuss"
            ? "Discuss in chat"
            : "New version of document";
        meta.append(intent);
      }
      const quote = document.createElement("button");
      quote.className = "quote-link";
      quote.textContent =
        comment.anchor.quote.slice(0, 350) +
        (comment.anchor.quote.length > 350 ? "…" : "");
      quote.setAttribute(
        "aria-label",
        `Go to source of comment ${index + 1}: ${quote.textContent}`,
      );
      quote.onclick = () => locate(comment);
      const text = document.createElement("p");
      text.className = "comment-body";
      text.textContent = comment.text;
      const actions = document.createElement("div");
      actions.className = "comment-actions";
      const go = document.createElement("button");
      go.textContent = "Back to source ↑";
      go.onclick = () => locate(comment);
      actions.append(go);
      const edit = document.createElement("button");
      edit.textContent = locked() ? "View comment" : "Edit comment";
      edit.onclick = () => openEditor(comment.anchor, comment.id);
      actions.append(edit);
      card.append(meta, quote, text, actions);
      list.append(card);
    });
    syncFrame();
  }
  async function load() {
    if (busy || editor.open) return;
    review = await api();
    version =
      review.versions.find((v) => v.number === selectedVersion) ||
      review.versions.at(-1);
    round =
      version.rounds.find((r) => r.id === selectedRound) ||
      version.rounds.at(-1);
    if (frameVersion !== version.number) {
      frameVersion = version.number;
      $(".review-surface").setAttribute("data-loading", "");
      $(".review-surface").setAttribute("aria-busy", "true");
      frame.src = `/doc/${reviewId}/${version.number}/${version.capability}`;
    }
    render();
    if (!recovered && !locked()) {
      recovered = true;
      try {
        const draft = JSON.parse(localStorage.getItem(draftKey()));
        if (draft?.anchor && typeof draft.text === "string") {
          openEditor(draft.anchor, draft.id);
          editing = draft;
          $("#comment-text").value = draft.text;
          $(`input[name="intent"][value="${draft.intent}"]`).checked = true;
        }
      } catch {
        /* Saved server comments remain available if browser drafts are unavailable. */
      }
    }
  }
  function locate(comment) {
    post({ type: "locate", anchor: comment.anchor });
  }
  function openEditor(anchor, existingId) {
    const existing = round.comments.find((c) => c.id === existingId);
    if (locked() && !existing) return;
    editing = existing || {
      id: crypto.randomUUID().replaceAll("-", ""),
      anchor,
      text: "",
      intent: "",
    };
    $("#target-quote").textContent = editing.anchor.quote;
    $("#comment-text").value = editing.text;
    $("#comment-text").readOnly = locked();
    $(`input[name="intent"][value="${editing.intent}"]`).checked = true;
    document
      .querySelectorAll('input[name="intent"]')
      .forEach((i) => (i.disabled = locked()));
    $("#editor-heading").textContent = locked()
      ? "Your comment"
      : existing
        ? "Edit your comment"
        : "Leave a comment";
    $("#delete-comment").hidden = !existing || locked();
    $("#save-comment").hidden = locked();
    $("#editor-error").textContent = "";
    editor.showModal();
    $("#comment-text").focus();
  }
  async function save(comments) {
    busy = true;
    render();
    try {
      round = await api("comments", { ...packet(), comments });
      clearTyping();
      editor.close();
      $("#error").hidden = true;
    } finally {
      busy = false;
      render();
    }
  }
  $("#comment-form").onsubmit = async (event) => {
    event.preventDefault();
    if (busy) return;
    const text = $("#comment-text").value.trim();
    if (!text) return;
    const updated = {
      ...editing,
      text,
      intent: $('input[name="intent"]:checked').value,
    };
    const comments = [...round.comments];
    const i = comments.findIndex((c) => c.id === updated.id);
    if (i < 0) comments.push(updated);
    else comments[i] = updated;
    try {
      await save(comments);
    } catch (e) {
      $("#editor-error").textContent = e.message;
    }
  };
  // Autosave text and intent as an editor draft. Saved comments remain transactional on the server.
  // A separate browser draft preserves unfinished typing without sending partial comments to the agent.
  const draftKey = () =>
    `starfleet-markup:${reviewId}:${version.number}:${round.id}`;
  function clearTyping() {
    try {
      localStorage.removeItem(draftKey());
    } catch {
      /* Server comments do not depend on browser storage. */
    }
  }
  function saveTyping() {
    if (!editing || locked()) return;
    try {
      localStorage.setItem(
        draftKey(),
        JSON.stringify({
          ...editing,
          text: $("#comment-text").value,
          intent: $('input[name="intent"]:checked').value,
        }),
      );
    } catch {
      $("#editor-error").textContent =
        "Browser draft storage is unavailable. Use Save comment before closing.";
    }
  }
  $("#comment-text").oninput = saveTyping;
  document
    .querySelectorAll('input[name="intent"]')
    .forEach((i) => (i.onchange = saveTyping));
  editor.addEventListener("close", () => {
    editing = undefined;
    load().catch((e) => error(e.message));
  });
  $("#close-editor").onclick = () => editor.close();
  $("#cancel-editor").onclick = () => {
    clearTyping();
    editor.close();
  };
  $("#delete-comment").onclick = async () => {
    if (busy) return;
    try {
      await save(round.comments.filter((c) => c.id !== editing.id));
      clearTyping();
    } catch (e) {
      $("#editor-error").textContent = e.message;
    }
  };
  $("#send").onclick = async () => {
    if (busy || locked()) return;
    busy = true;
    render();
    try {
      round = await api("submit", packet());
      $("#error").hidden = true;
    } catch (e) {
      error(e.message);
    } finally {
      busy = false;
      render();
    }
  };
  $("#new-round").onclick = async () => {
    try {
      const r = await api("round", { version: version.number });
      selectedRound = r.id;
      await load();
    } catch (e) {
      error(e.message);
    }
  };
  $("#retry-wake").onclick = async () => {
    $("#retry-wake").disabled = true;
    try {
      await api("retry", packet());
      await load();
    } catch (e) {
      error(e.message);
    } finally {
      render();
    }
  };
  $("#version").onchange = () => {
    selectedVersion = Number($("#version").value);
    selectedRound = undefined;
    load().catch((e) => error(e.message));
  };
  $("#round").onchange = () => {
    selectedRound = $("#round").value;
    load().catch((e) => error(e.message));
  };
  document.querySelectorAll('input[name="mode"]').forEach(
    (i) =>
      (i.onchange = () => {
        mode = $('input[name="mode"]:checked').value;
        render();
      }),
  );
  window.addEventListener("message", (event) => {
    if (
      event.source !== frame.contentWindow ||
      event.data?.channel !== "starfleet-markup" ||
      !round
    )
      return;
    const m = event.data;
    if (m.type === "ready") {
      syncFrame();
      $(".review-surface").removeAttribute("data-loading");
      $(".review-surface").setAttribute("aria-busy", "false");
    }
    if (m.type === "height" && Number.isFinite(m.height))
      frame.style.height =
        Math.min(200000, Math.max(250, Math.ceil(m.height))) + "px";
    if (m.type === "located" && Number.isFinite(m.top))
      window.scrollTo({
        top: frame.getBoundingClientRect().top + window.scrollY + m.top - 115,
        behavior: "smooth",
      });
    if (m.type === "missing")
      error(
        "This target is unavailable in the current interactive state. Its original selection is preserved in the comment.",
      );
    if (m.type === "target" && mode === "comment" && !busy && !editor.open)
      openEditor(m.anchor, m.comment);
  });
  const events = new EventSource(`/events/${reviewId}`);
  events.addEventListener("change", () =>
    load().catch((e) => error(e.message)),
  );
  events.onerror = () => {
    $("#status").textContent = "Connection interrupted · reconnecting";
  };
  window.addEventListener("online", () =>
    load().catch((e) => error(e.message)),
  );
  window.addEventListener("focus", () => load().catch((e) => error(e.message)));
  load().catch((e) => error(e.message));
})();
