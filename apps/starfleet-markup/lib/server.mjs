import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { fileURLToPath } from "node:url";
import { Store, atomic, id, demand, now, isSessionId } from "./store.mjs";
import { queueCodex } from "./codex.mjs";

const web = fileURLToPath(new URL("../web/", import.meta.url));
const mime = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".pdf": "application/pdf",
  ".mp4": "video/mp4",
};
const json = (res, value, status = 200) => {
  if (!res.headersSent)
    res.writeHead(status, {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    });
  res.end(JSON.stringify(value));
};
async function body(req) {
  let bytes = 0;
  const chunks = [];
  for await (const chunk of req) {
    bytes += chunk.length;
    demand(bytes <= 2 * 1024 * 1024, "Request too large", 413);
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString());
  } catch {
    throw Object.assign(new Error("Invalid JSON"), { status: 400 });
  }
}
export async function serve(root, port = 0, { notifyCodex = queueCodex } = {}) {
  const store = new Store(root),
    key = id() + id();
  const listeners = new Map(),
    clients = new Set();
  const deliveries = new Map();
  let deliveryChain = Promise.resolve();
  let closing = false;
  const emit = (reviewId) => {
    for (const client of clients) {
      if (client.review === reviewId)
        client.res.write(
          `event: change\ndata: ${JSON.stringify({ review: reviewId })}\n\n`,
        );
    }
    for (const [res, target] of listeners) {
      const pending = store
        .pending(target.owner, target.worktree)
        .filter(
          (p) =>
            !target.unnotified || !p.round.notifications?.[target.unnotified],
        );
      if (pending.length) {
        listeners.delete(res);
        json(res, pending);
      }
    }
  };
  const deliver = (review, version, round, retry = false) => {
    if (
      closing ||
      review.harness !== "codex" ||
      !round.submitted ||
      round.received ||
      deliveries.has(round.id) ||
      round.delivery?.state === "queued" ||
      (!retry && ["failed", "uncertain"].includes(round.delivery?.state))
    )
      return;
    round.delivery = {
      state: "pending",
      attempts: round.delivery?.attempts || 0,
    };
    store.save();
    const task = deliveryChain
      .then(async () => {
        if (closing) return;
        round.delivery = {
          state: "sending",
          attempted: now(),
          attempts: (round.delivery?.attempts || 0) + 1,
        };
        store.save();
        emit(review.id);
        try {
          const result = await notifyCodex({
            review,
            version,
            round,
            state: store.root,
          });
          round.delivery = {
            ...round.delivery,
            state: "queued",
            queued: now(),
            messageId: result.messageId,
          };
          round.notifications ||= {};
          round.notifications.codex = now();
        } catch (error) {
          round.delivery = {
            ...round.delivery,
            state:
              error.killed || error.code === "ETIMEDOUT"
                ? "uncertain"
                : "failed",
            error: String(error.message).slice(0, 1500),
          };
        }
        store.save();
        emit(review.id);
      })
      .catch((error) => {
        console.error(
          "Starfleet Markup could not save notification state: " +
            error.message,
        );
      })
      .finally(() => deliveries.delete(round.id));
    deliveryChain = task;
    deliveries.set(round.id, task);
  };
  const recover = (review, restarting = false) => {
    for (const version of review.versions)
      for (const round of version.rounds) {
        if (restarting && round.delivery?.state === "sending") {
          round.delivery.state = "uncertain";
          round.delivery.error =
            "The server stopped during notification. Retry explicitly to avoid an automatic duplicate.";
          store.save();
        }
        deliver(review, version, round);
      }
  };
  let origin;
  const server = http.createServer(async (req, res) => {
    try {
      demand(req.headers.host === new URL(origin).host, "Invalid host", 403);
      res.setHeader("X-Content-Type-Options", "nosniff");
      res.setHeader("Referrer-Policy", "no-referrer");
      const url = new URL(req.url, origin);
      const parts = url.pathname
        .split("/")
        .filter(Boolean)
        .map(decodeURIComponent);
      const bearer = req.headers.authorization === `Bearer ${key}`;
      const cookie = (req.headers.cookie || "")
        .split(";")
        .some((c) => c.trim() === `markup=${key}`);
      const publicStatic =
        req.method === "GET" &&
        parts.length === 1 &&
        ["bridge.js", "document-theme.css"].includes(parts[0]);
      let documentCapability = false;
      if (req.method === "GET" && parts[0] === "doc") {
        documentCapability =
          store.version(store.get(parts[1]), parts[2]).capability === parts[3];
      }
      if (
        req.method === "GET" &&
        parts[0] === "r" &&
        url.searchParams.get("key") === key
      ) {
        res.writeHead(303, {
          "Set-Cookie": `markup=${key}; HttpOnly; SameSite=Strict; Path=/`,
          Location: url.pathname,
        });
        return res.end();
      }
      demand(
        bearer || cookie || publicStatic || documentCapability,
        "Open the review URL printed by starfleet-markup",
        401,
      );
      if (req.method !== "GET") {
        demand(
          bearer ||
            (req.headers.origin === origin &&
              req.headers["x-markup-key"] === key),
          "Invalid request origin",
          403,
        );
      }
      if (req.method === "GET" && url.pathname === "/api/health")
        return json(res, { app: "starfleet-markup", pid: process.pid });
      if (req.method === "GET" && url.pathname === "/api/reviews")
        return json(res, Object.values(store.data.reviews));
      if (
        req.method === "GET" &&
        ["/api/wait", "/api/pending"].includes(url.pathname)
      ) {
        const owner = url.searchParams.get("owner"),
          worktree = url.searchParams.get("worktree");
        demand(owner && worktree, "Owner and worktree are required");
        const unnotified = url.searchParams.get("unnotified");
        demand(
          !unnotified || ["claude", "codex"].includes(unnotified),
          "Invalid notification channel",
        );
        const pending = store
          .pending(owner, worktree)
          .filter((p) => !unnotified || !p.round.notifications?.[unnotified]);
        if (pending.length || url.pathname.endsWith("pending"))
          return json(res, pending);
        // One open response, released on submission. There is no periodic model or HTTP polling.
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.flushHeaders();
        // JSON whitespace keeps standard HTTP client idle limits from ending a long review.
        const heartbeat = setInterval(() => res.write("\n"), 30000);
        heartbeat.unref();
        res.once("close", () => clearInterval(heartbeat));
        const channel = url.searchParams.get("channel");
        if (channel === "claude") {
          // Resume/Stop can overlap. Retire an older standby without generating a wake signal.
          for (const [old, target] of listeners)
            if (
              target.channel === channel &&
              target.owner === owner &&
              target.worktree === worktree
            ) {
              listeners.delete(old);
              json(old, {
                error: "Listener replaced by a newer hook for this session",
              });
            }
        }
        listeners.set(res, { owner, worktree, unnotified, channel });
        req.on("close", () => listeners.delete(res));
        return;
      }
      if (req.method === "POST" && url.pathname === "/api/create") {
        demand(bearer, "CLI authentication required", 403);
        return json(res, store.create(await body(req)), 201);
      }
      if (parts[0] === "api" && parts[1] === "review") {
        const review = store.get(parts[2]);
        if (req.method === "GET") return json(res, review);
        const input = await body(req);
        let result;
        if (parts[3] === "version") {
          demand(bearer, "CLI authentication required", 403);
          result = store.addVersion(review, input.source, input.assets);
          store.save();
        } else if (parts[3] === "bind") {
          demand(
            bearer &&
              input.owner === review.owner &&
              input.worktree === review.worktree,
            "Only the owning agent can bind delivery",
            403,
          );
          demand(
            !review.versions.some((v) =>
              v.rounds.some((r) => deliveries.has(r.id)),
            ),
            "Wait for the current notification before changing delivery binding",
            409,
          );
          demand(
            [null, "codex", "claude"].includes(input.harness),
            "Invalid originating harness",
          );
          if (input.harness === "codex")
            demand(
              isSessionId(review.owner),
              "Codex wake requires a session UUID",
            );
          review.harness = input.harness;
          store.save();
          result = review;
          recover(review);
        } else if (parts[3] === "round") {
          const version = store.version(review, input.version);
          demand(
            version.number === review.versions.at(-1).number,
            "Older versions are read-only",
            409,
          );
          demand(
            version.rounds.at(-1).submitted,
            "A draft round already exists",
            409,
          );
          result = store.newRound();
          version.rounds.push(result);
          store.save();
        } else if (parts[3] === "comments") {
          result = store.edit(review.id, input.version, input.round, input);
        } else if (parts[3] === "submit") {
          result = store.submit(
            review.id,
            input.version,
            input.round,
            input.revision,
          );
          deliver(review, store.version(review, input.version), result);
        } else if (parts[3] === "retry") {
          result = store.round(review, input.version, input.round);
          demand(
            result.submitted && !result.received && review.harness === "codex",
            "No pending Codex notification to retry",
            409,
          );
          deliver(review, store.version(review, input.version), result, true);
        } else if (parts[3] === "ack" || parts[3] === "notified") {
          demand(
            bearer &&
              input.owner === review.owner &&
              input.worktree === review.worktree,
            "Only the owning agent can acknowledge",
            403,
          );
          result = store.round(review, input.version, input.round);
          demand(result.submitted, "Round has not been submitted", 409);
          if (parts[3] === "ack") result.received ||= now();
          else {
            demand(
              ["claude", "codex"].includes(input.channel),
              "Invalid notification channel",
            );
            result.notifications ||= {};
            result.notifications[input.channel] ||= now();
          }
          store.save();
        } else
          throw Object.assign(new Error("Unknown operation"), { status: 404 });
        emit(review.id);
        return json(res, result);
      }
      if (req.method === "GET" && parts[0] === "events") {
        store.get(parts[1]);
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-store",
          Connection: "keep-alive",
        });
        res.write(": connected\n\n");
        const client = { review: parts[1], res };
        clients.add(client);
        req.on("close", () => clients.delete(client));
        return;
      }
      if (req.method === "GET" && parts[0] === "r") {
        const review = store.get(parts[1]);
        res.setHeader(
          "Content-Security-Policy",
          "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        );
        const shell = fs
          .readFileSync(path.join(web, "review.html"), "utf8")
          .replace("__KEY__", key)
          .replace("__REVIEW__", review.id);
        res.writeHead(200, {
          "Content-Type": mime[".html"],
          "Cache-Control": "no-store",
        });
        return res.end(shell);
      }
      if (req.method === "GET" && parts[0] === "doc") {
        const review = store.get(parts[1]),
          version = store.version(review, parts[2]);
        const folder = path.join(root, review.id, String(version.number));
        demand(documentCapability, "Invalid document capability", 403);
        if (parts.length === 4) {
          let html = fs.readFileSync(
            path.join(folder, "document.html"),
            "utf8",
          );
          // Opaque iframe origin keeps document scripts away from API credentials and the review UI.
          res.setHeader(
            "Content-Security-Policy",
            "sandbox allow-scripts; default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'none'; form-action 'none'; base-uri 'self'",
          );
          const bridge = `<base href="/doc/${review.id}/${version.number}/${version.capability}/assets/"><script src="/bridge.js"></script>`;
          html = /<head\b[^>]*>/i.test(html)
            ? html.replace(/<head\b[^>]*>/i, (m) => m + bridge)
            : bridge + html;
          res.writeHead(200, {
            "Content-Type": mime[".html"],
            "Cache-Control": "no-store",
          });
          return res.end(html);
        }
        demand(parts[4] === "assets", "Invalid asset path", 404);
        const assetRoot = path.join(folder, "assets");
        const file = path.resolve(assetRoot, ...parts.slice(5));
        demand(
          file.startsWith(assetRoot + path.sep) &&
            fs.existsSync(file) &&
            fs.statSync(file).isFile(),
          "Asset not found",
          404,
        );
        res.writeHead(200, {
          "Content-Type":
            mime[path.extname(file).toLowerCase()] ||
            "application/octet-stream",
          "Content-Security-Policy": "sandbox; default-src 'none'",
        });
        return fs.createReadStream(file).pipe(res);
      }
      if (
        req.method === "GET" &&
        parts.length === 1 &&
        ["review.js", "review.css", "bridge.js", "document-theme.css"].includes(
          parts[0],
        )
      ) {
        res.writeHead(200, { "Content-Type": mime[path.extname(parts[0])] });
        return fs.createReadStream(path.join(web, parts[0])).pipe(res);
      }
      json(res, { error: "Not found" }, 404);
    } catch (error) {
      if (!res.headersSent)
        json(res, { error: error.message }, error.status || 500);
      else res.end();
    }
  });
  // Keep listeners open indefinitely without imposing model turns.
  server.requestTimeout = 0;
  server.headersTimeout = 10000;
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  origin = `http://127.0.0.1:${server.address().port}`;
  const config = { origin, key, pid: process.pid };
  atomic(path.join(root, "server.json"), JSON.stringify(config));
  for (const review of Object.values(store.data.reviews)) recover(review, true);
  return {
    server,
    store,
    config,
    close: async () => {
      closing = true;
      await Promise.allSettled(deliveries.values());
      for (const res of listeners.keys())
        json(
          res,
          { error: "Server stopped. Reconnect to recover pending feedback." },
          503,
        );
      for (const c of clients) c.res.end();
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
      try {
        if (
          JSON.parse(fs.readFileSync(path.join(root, "server.json"))).key ===
          key
        )
          fs.unlinkSync(path.join(root, "server.json"));
      } catch {
        /* Another runtime or an already removed file must be left alone. */
      }
    },
  };
}
