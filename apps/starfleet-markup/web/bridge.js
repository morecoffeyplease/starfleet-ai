/* Runs in the opaque document iframe. It never receives the review API credential. */
(() => {
  const origin = new URL(document.currentScript.src).origin;
  const post = (value) =>
    parent.postMessage({ channel: "starfleet-markup", ...value }, origin);
  let mode = "comment",
    comments = [],
    locked = false;
  let markerHost,
    markerRoot,
    markers = [];
  const byKey = new Map(),
    byElement = new WeakMap(),
    originalTabs = new WeakMap();
  const selector =
    "p,h1,h2,h3,h4,h5,h6,li,blockquote,pre,td,th,figcaption,img,svg,video,button,a,input,textarea,select,figure,section,article,div";
  const textNodes = (element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) =>
        n.parentElement.closest("script,style,noscript")
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_ACCEPT,
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  };
  const text = (element) =>
    textNodes(element)
      .map((n) => n.textContent)
      .join("");
  function register() {
    for (const el of [document.body, ...document.querySelectorAll(selector)]) {
      if (!el || el.closest("[data-sfm-ui]") || byElement.has(el)) continue;
      const key = "e" + byKey.size;
      byKey.set(key, el);
      byElement.set(el, key);
      originalTabs.set(el, el.getAttribute("tabindex"));
    }
  }
  function rangeAt(el, start, end) {
    const range = document.createRange();
    let offset = 0,
      began = false;
    for (const node of textNodes(el)) {
      const length = node.textContent.length;
      if (!began && start < offset + length) {
        range.setStart(node, start - offset);
        began = true;
      }
      if (began && end <= offset + length) {
        range.setEnd(node, end - offset);
        return range;
      }
      offset += length;
    }
    return null;
  }
  function highlight() {
    document
      .querySelectorAll("[data-sfm-comment]")
      .forEach((el) => el.removeAttribute("data-sfm-comment"));
    const ranges = [];
    for (const comment of comments) {
      const el = byKey.get(comment.anchor.key);
      if (!el) continue;
      if (comment.anchor.kind === "text") {
        const r = rangeAt(el, comment.anchor.start, comment.anchor.end);
        if (r && r.toString() === comment.anchor.quote) ranges.push(r);
        if (!CSS.highlights) el.setAttribute("data-sfm-comment", "");
      } else el.setAttribute("data-sfm-comment", "");
    }
    if (CSS.highlights && window.Highlight)
      CSS.highlights.set("sfm-feedback", new Highlight(...ranges));
    renderMarkers();
  }
  function positionMarkers() {
    const placed = [];
    for (const marker of markers) {
      const block = marker.el.getBoundingClientRect();
      const rect = marker.range
        ? [...marker.range.getClientRects()].find((r) => r.width && r.height)
        : block;
      const visible =
        rect &&
        rect.bottom >= 0 &&
        rect.top <= innerHeight &&
        block.width &&
        block.height;
      marker.button.hidden = !visible;
      if (!visible) continue;
      const outside = block.right + 50 <= innerWidth;
      const left = Math.max(
        4,
        Math.min(innerWidth - 46, outside ? block.right + 8 : block.right - 42),
      );
      let top = Math.max(4, outside ? rect.top : rect.top - 34);
      for (const previous of placed) {
        if (
          Math.abs(previous.left - left) < 42 &&
          Math.abs(previous.top - top) < 34
        )
          top = previous.top + 36;
      }
      top = Math.min(Math.max(4, innerHeight - 34), top);
      marker.button.style.left = left + "px";
      marker.button.style.top = top + "px";
      placed.push({ left, top });
    }
  }
  function renderMarkers() {
    if (!markerRoot) return;
    markerRoot.querySelectorAll("button").forEach((button) => button.remove());
    markers = [];
    comments.forEach((comment, index) => {
      const el = byKey.get(comment.anchor.key);
      if (!el) return;
      const range =
        comment.anchor.kind === "text"
          ? rangeAt(el, comment.anchor.start, comment.anchor.end)
          : null;
      if (
        comment.anchor.kind === "text" &&
        (!range || range.toString() !== comment.anchor.quote)
      )
        return;
      const button = document.createElement("button");
      button.type = "button";
      button.title = `View comment ${index + 1}`;
      button.setAttribute("aria-label", `View comment ${index + 1}`);
      button.setAttribute("aria-haspopup", "dialog");
      button.innerHTML =
        '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-9l-5 4v-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
      const number = document.createElement("span");
      number.textContent = index + 1;
      button.append(number);
      button.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (mode === "comment")
          post({
            type: "target",
            anchor: comment.anchor,
            comment: comment.id,
            locked,
          });
      };
      markerRoot.append(button);
      markers.push({ button, el, range });
    });
    positionMarkers();
  }
  function applyMode() {
    document.documentElement.dataset.sfmMode = mode;
    if (markerHost) {
      markerHost.hidden = mode !== "comment";
      markerHost.style.setProperty(
        "display",
        mode === "comment" ? "block" : "none",
        "important",
      );
      positionMarkers();
    }
    for (const [key, el] of byKey) {
      if (key === "e0") continue;
      const block = el.matches(
        "p,h1,h2,h3,h4,h5,h6,li,img,svg,button,pre,td,th,blockquote",
      );
      if (mode === "comment" && block) el.setAttribute("tabindex", "0");
      else {
        const tab = originalTabs.get(el);
        if (tab === null) el.removeAttribute("tabindex");
        else el.setAttribute("tabindex", tab);
      }
    }
  }
  function commentAt(event) {
    let elementComment;
    for (const comment of comments) {
      const el = byKey.get(comment.anchor.key);
      if (!el || !el.contains(event.target)) continue;
      if (comment.anchor.kind === "text") {
        const range = rangeAt(el, comment.anchor.start, comment.anchor.end);
        if (!range || range.toString() !== comment.anchor.quote) continue;
        if (
          event.type === "keydown" ||
          !CSS.highlights ||
          [...range.getClientRects()].some(
            (rect) =>
              event.clientX >= rect.left &&
              event.clientX <= rect.right &&
              event.clientY >= rect.top &&
              event.clientY <= rect.bottom,
          )
        )
          return comment;
      } else if (
        !elementComment ||
        byKey.get(elementComment.anchor.key).contains(el)
      )
        elementComment = comment;
    }
    return elementComment;
  }
  function choose(event) {
    if (mode !== "comment" || event.composedPath().includes(markerHost)) return;
    if (event.type === "keydown" && event.key !== "Enter") return;
    register();
    const selection = getSelection();
    const selectedText =
      selection && !selection.isCollapsed && selection.rangeCount;
    const saved = !selectedText && commentAt(event);
    if (saved) {
      event.preventDefault();
      event.stopImmediatePropagation();
      post({ type: "target", anchor: saved.anchor, comment: saved.id, locked });
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    let el = event.target.closest?.(selector) || document.body;
    let anchor;
    if (selection && !selection.isCollapsed && selection.rangeCount) {
      const r = selection.getRangeAt(0);
      const container = r.commonAncestorContainer;
      const parentEl =
        container.nodeType === Node.ELEMENT_NODE
          ? container
          : container.parentElement;
      el = parentEl.closest(selector) || document.body;
      if (!el.contains(r.startContainer) || !el.contains(r.endContainer))
        el = document.body;
      const nodes = textNodes(el);
      let offset = 0,
        start,
        end;
      for (const n of nodes) {
        if (n === r.startContainer) start = offset + r.startOffset;
        if (n === r.endContainer) end = offset + r.endOffset;
        offset += n.textContent.length;
      }
      if (start !== undefined && end !== undefined && end > start)
        anchor = {
          key: byElement.get(el),
          kind: "text",
          start,
          end,
          quote: text(el).slice(start, end),
          context: text(el).slice(0, 20000),
        };
    }
    register();
    anchor ||= {
      key: byElement.get(el),
      kind: "element",
      quote: (
        el.getAttribute("alt") ||
        el.getAttribute("aria-label") ||
        text(el).trim() ||
        el.tagName.toLowerCase()
      ).slice(0, 20000),
      context: text(el).slice(0, 20000),
    };
    if (!anchor.key) return;
    const existing = comments.find(
      (c) =>
        c.anchor.key === anchor.key &&
        ((anchor.kind === "element" && c.anchor.kind === "element") ||
          (c.anchor.kind === "text" &&
            anchor.start < c.anchor.end &&
            anchor.end > c.anchor.start)),
    );
    // Consume the selection after capturing it, so a later image click cannot reuse old text.
    selection?.removeAllRanges();
    post({ type: "target", anchor, comment: existing?.id, locked });
  }
  function start() {
    register();
    // A shadow overlay keeps annotation controls out of document text and anchor numbering.
    markerHost = document.createElement("div");
    markerHost.setAttribute("data-sfm-ui", "");
    markerHost.style.cssText =
      "position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important";
    markerRoot = markerHost.attachShadow({ mode: "open" });
    markerRoot.innerHTML =
      "<style>button{position:fixed;display:flex;align-items:center;justify-content:center;gap:4px;width:42px;height:30px;padding:0;border:1px solid #fff;border-radius:7px;background:#254f48;color:#fff;font:650 12px/1 system-ui,sans-serif;box-shadow:0 1px 5px #182c2833;pointer-events:auto;cursor:pointer}button:hover{background:#173b34}button:focus-visible{outline:3px solid #315bea;outline-offset:2px}[hidden]{display:none!important}</style>";
    document.documentElement.append(markerHost);
    applyMode();
    const style = document.createElement("style");
    style.textContent = `html[data-sfm-mode="comment"] :is(p,h1,h2,h3,h4,li,img,svg,button,pre,td,th,blockquote):hover{outline:2px solid #527d69;outline-offset:4px;cursor:pointer}html[data-sfm-mode="comment"] :focus-visible{outline:3px solid #315bea;outline-offset:4px}[data-sfm-comment]{box-shadow:inset 3px 0 #b58522;background-color:#fff5d980}::highlight(sfm-feedback){background:#f7d774;color:#26312b}`;
    document.head.append(style);
    document.addEventListener("click", choose, true);
    document.addEventListener("keydown", choose, true);
    const height = () => {
      positionMarkers();
      post({
        type: "height",
        height: Math.max(
          document.body.scrollHeight,
          document.documentElement.scrollHeight,
        ),
      });
    };
    new ResizeObserver(height).observe(document.body);
    window.addEventListener("scroll", positionMarkers, true);
    window.addEventListener("resize", positionMarkers);
    window.addEventListener("load", height);
    post({ type: "ready" });
    height();
  }
  window.addEventListener("message", (event) => {
    if (
      event.source !== parent ||
      event.origin !== origin ||
      event.data?.channel !== "starfleet-markup"
    )
      return;
    const m = event.data;
    if (m.type === "state") {
      mode = m.mode;
      comments = m.comments;
      locked = m.locked;
      register();
      applyMode();
      highlight();
    }
    if (m.type === "locate") {
      const a = m.anchor,
        el = byKey.get(a.key);
      if (!el) return post({ type: "missing" });
      let box = el.getBoundingClientRect();
      if (a.kind === "text") {
        const range = rangeAt(el, a.start, a.end);
        if (!range || range.toString() !== a.quote)
          return post({ type: "missing" });
        box = range.getBoundingClientRect();
        if (CSS.highlights && window.Highlight) {
          CSS.highlights.set("sfm-feedback", new Highlight(range));
          setTimeout(highlight, 1800);
        }
      }
      el.animate(
        [
          { outline: "3px solid #315bea" },
          { outline: "3px solid transparent" },
        ],
        { duration: 1800 },
      );
      post({ type: "located", top: box.top + scrollY });
    }
  });
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", start);
  else start();
})();
