import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { spawn, execFileSync } from "node:child_process";
import { once } from "node:events";
import { Store, id } from "../lib/store.mjs";
import { serve } from "../lib/server.mjs";

function fixture(t) {
  const dir = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "sfm-test-")),
  );
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, "source.md");
  fs.writeFileSync(
    file,
    "# A real review\n\nA paragraph with **emphasis**.\n\n<script>alert(1)</script>",
  );
  const comment = {
    id: id(),
    text: "Make this clearer.",
    intent: "discuss",
    anchor: {
      kind: "text",
      key: "e2",
      quote: "paragraph",
      context: "A paragraph with emphasis.",
      start: 2,
      end: 11,
    },
  };
  return {
    dir,
    file,
    comment,
    input: {
      owner: "orchestrator-one",
      worktree: dir,
      source: file,
      title: "Review fixture",
    },
  };
}
test("immutable snapshots, validated comments, optimistic concurrency, rounds, receipt, and version history", (t) => {
  const f = fixture(t),
    store = new Store(path.join(f.dir, "state")),
    review = store.create(f.input);
  const v = review.versions[0],
    r = v.rounds[0];
  assert.doesNotMatch(
    fs.readFileSync(
      path.join(store.root, review.id, "1", "document.html"),
      "utf8",
    ),
    /<script>alert/,
  );
  assert.throws(
    () =>
      store.edit(review.id, 1, r.id, {
        revision: 0,
        comments: [{ ...f.comment, intent: "invented" }],
      }),
    /intent/,
  );
  store.edit(review.id, 1, r.id, { revision: 0, comments: [f.comment] });
  assert.throws(
    () => store.edit(review.id, 1, r.id, { revision: 0, comments: [] }),
    /Another tab/,
  );
  store.submit(review.id, 1, r.id, 1);
  assert.equal(store.submit(review.id, 1, r.id, 1).id, r.id);
  assert.throws(
    () => store.edit(review.id, 1, r.id, { revision: 1, comments: [] }),
    /locked/,
  );
  assert.equal(store.pending("other-agent", f.dir).length, 0);
  assert.equal(store.pending(f.input.owner, "/other/worktree").length, 0);
  assert.equal(store.pending(f.input.owner, f.dir).length, 1);
  fs.writeFileSync(f.file, "# Changed source");
  store.addVersion(review, f.file);
  store.save();
  assert.equal(review.versions.length, 2);
  assert.equal(review.versions[0].rounds[0].comments[0].text, f.comment.text);
  assert.match(
    fs.readFileSync(path.join(store.root, review.id, "1", "source.md"), "utf8"),
    /A real review/,
  );
  assert.equal(new Store(store.root).get(review.id).versions.length, 2);
});
test("rejects Git storage, symlinks, malformed targets, and unsupported source files", (t) => {
  const f = fixture(t);
  const repo = path.join(f.dir, "repo");
  fs.mkdirSync(repo);
  execFileSync("git", ["init", "-q", repo]);
  assert.throws(() => new Store(path.join(repo, "state")), /outside a Git/);
  const assets = path.join(f.dir, "assets");
  fs.mkdirSync(assets);
  fs.symlinkSync(f.file, path.join(assets, "escape.css"));
  const store = new Store(path.join(f.dir, "state"));
  assert.throws(() => store.create({ ...f.input, assets }), /symlinks/);
  assert.throws(
    () => store.create({ ...f.input, source: path.join(f.dir, "missing") }),
    /ENOENT/,
  );
});
test("Git configuration failures cannot bypass outside-repository storage", (t) => {
  const f = fixture(t),
    repo = path.join(f.dir, "repo");
  fs.mkdirSync(repo);
  execFileSync("git", ["init", "-q", repo]);
  const config = path.join(f.dir, "bad.gitconfig");
  fs.writeFileSync(config, "[broken\n");
  const previous = process.env.GIT_CONFIG_GLOBAL;
  process.env.GIT_CONFIG_GLOBAL = config;
  try {
    assert.throws(
      () => new Store(path.join(repo, "reviews")),
      /outside a Git repository/,
    );
    assert.throws(
      () => new Store(path.join(f.dir, "nonrepo")),
      /Unable to check/,
    );
  } finally {
    if (previous === undefined) delete process.env.GIT_CONFIG_GLOBAL;
    else process.env.GIT_CONFIG_GLOBAL = previous;
  }
});
test("Markdown task items preserve checked state while remaining disabled", (t) => {
  const f = fixture(t),
    store = new Store(path.join(f.dir, "state"));
  fs.writeFileSync(f.file, "- [x] Completed item\n- [ ] Pending item\n");
  const review = store.create(f.input);
  const html = fs.readFileSync(
    path.join(store.root, review.id, "1", "document.html"),
    "utf8",
  );
  const inputs = html.match(/<input\b[^>]*>/g);
  assert.equal(inputs.length, 2);
  assert.match(inputs[0], /\bchecked\b/);
  assert.doesNotMatch(inputs[1], /\bchecked\b/);
  for (const input of inputs) {
    assert.match(input, /\bdisabled\b/);
    assert.match(input, /type="checkbox"/);
  }
});
test("failed version publication rolls back assets instead of leaking them into a retry", (t) => {
  const f = fixture(t),
    store = new Store(path.join(f.dir, "state")),
    review = store.create(f.input);
  const round = review.versions[0].rounds[0];
  store.edit(review.id, 1, round.id, { revision: 0, comments: [f.comment] });
  store.submit(review.id, 1, round.id, 1);
  const assets = path.join(f.dir, "assets");
  fs.mkdirSync(assets);
  fs.writeFileSync(
    path.join(assets, "aa.svg"),
    '<svg xmlns="http://www.w3.org/2000/svg"/>',
  );
  fs.symlinkSync(f.file, path.join(assets, "zz.css"));
  assert.throws(() => store.addVersion(review, f.file, assets), /symlinks/);
  assert.equal(fs.existsSync(path.join(store.root, review.id, "2")), false);
  assert.equal(review.versions.length, 1);
  store.addVersion(review, f.file);
  assert.equal(
    fs.existsSync(path.join(store.root, review.id, "2", "assets", "aa.svg")),
    false,
  );
  assert.equal(review.versions[0].rounds[0].comments[0].text, f.comment.text);
});
test("HTTP push, ownership, locked submissions, credential checks, and restart recovery", async (t) => {
  const f = fixture(t),
    state = path.join(f.dir, "state");
  let app = await serve(state);
  t.after(async () => {
    if (app) await app.close();
  });
  const request = async (route, data, extra = {}) => {
    const response = await fetch(app.config.origin + route, {
      method: data ? "POST" : "GET",
      headers: {
        Authorization: `Bearer ${app.config.key}`,
        "Content-Type": "application/json",
        ...extra,
      },
      body: data ? JSON.stringify(data) : undefined,
    });
    return { status: response.status, data: await response.json() };
  };
  const review = (await request("/api/create", f.input)).data,
    round = review.versions[0].rounds[0];
  const route = "/api/review/" + review.id;
  const unauth = await fetch(app.config.origin + route);
  assert.equal(unauth.status, 401);
  const badHost = await new Promise((resolve) => {
    http.get(
      app.config.origin + route,
      {
        headers: {
          Host: "evil.example",
          Authorization: `Bearer ${app.config.key}`,
        },
      },
      (res) => {
        res.resume();
        resolve(res.statusCode);
      },
    );
  });
  assert.equal(badHost, 403);
  const csrf = await fetch(app.config.origin + route + "/submit", {
    method: "POST",
    headers: {
      Cookie: `markup=${app.config.key}`,
      Origin: "https://evil.example",
    },
    body: "{}",
  });
  assert.equal(csrf.status, 403);
  const waiting = request(
    "/api/wait?" +
      new URLSearchParams({ owner: f.input.owner, worktree: f.dir }),
  );
  await request(route + "/comments", {
    version: 1,
    round: round.id,
    revision: 0,
    comments: [f.comment],
  });
  await request(route + "/submit", {
    version: 1,
    round: round.id,
    revision: 1,
  });
  const pushed = await waiting;
  assert.equal(pushed.data[0].round.comments[0].anchor.quote, "paragraph");
  assert.equal(
    (
      await request(route + "/ack", {
        version: 1,
        round: round.id,
        owner: "wrong",
        worktree: f.dir,
      })
    ).status,
    403,
  );
  await app.close();
  app = undefined;
  app = await serve(state);
  const recovered = await request(
    "/api/pending?" +
      new URLSearchParams({ owner: f.input.owner, worktree: f.dir }),
  );
  assert.equal(recovered.data.length, 1);
  await request(route + "/ack", { version: 1, round: round.id, ...f.input });
  assert.equal(
    (
      await request(
        "/api/pending?" +
          new URLSearchParams({ owner: f.input.owner, worktree: f.dir }),
      )
    ).data.length,
    0,
  );
  assert.equal(
    (
      await request(route + "/comments", {
        version: 1,
        round: round.id,
        revision: 1,
        comments: [],
      })
    ).status,
    409,
  );
  const next = await request(route + "/round", { version: 1 });
  assert.equal(next.status, 200);
  assert.equal((await request(route + "/round", { version: 1 })).status, 409);
});
test("opaque document frame uses version-scoped assets without API credentials", async (t) => {
  const f = fixture(t),
    app = await serve(path.join(f.dir, "state"));
  t.after(() => app.close());
  const assets = path.join(f.dir, "assets");
  fs.mkdirSync(assets);
  fs.writeFileSync(
    path.join(assets, "image.svg"),
    '<svg xmlns="http://www.w3.org/2000/svg"/>',
  );
  const review = app.store.create({ ...f.input, assets }),
    v = review.versions[0];
  const base = `${app.config.origin}/doc/${review.id}/1/${v.capability}`;
  const document = await fetch(base);
  assert.equal(document.status, 200);
  assert.match(
    document.headers.get("content-security-policy"),
    /sandbox allow-scripts;/,
  );
  assert.doesNotMatch(
    document.headers.get("content-security-policy"),
    /allow-same-origin/,
  );
  assert.match(await document.text(), /bridge.js/);
  assert.equal((await fetch(base + "/assets/image.svg")).status, 200);
  assert.equal((await fetch(base.replace(v.capability, "wrong"))).status, 401);
  assert.equal(
    (
      await fetch(app.config.origin + "/api/reviews", {
        headers: { Origin: "null" },
      })
    ).status,
    401,
  );
});
test("Claude listener wakes with exit 2 but never claims receipt", async (t) => {
  const f = fixture(t),
    state = path.join(f.dir, "state"),
    app = await serve(state);
  t.after(() => app.close());
  const cli = new URL("../bin/starfleet-markup.mjs", import.meta.url);
  const child = spawn(
    process.execPath,
    [cli.pathname, "claude-hook", "--state", state],
    { stdio: ["pipe", "pipe", "pipe"] },
  );
  t.after(() => child.kill());
  let message = "";
  child.stderr.on("data", (chunk) => {
    message += chunk;
  });
  child.stdin.end(JSON.stringify({ session_id: f.input.owner, cwd: f.dir }));
  const review = app.store.create(f.input),
    round = review.versions[0].rounds[0];
  app.store.edit(review.id, 1, round.id, {
    revision: 0,
    comments: [f.comment],
  });
  // Submit through HTTP so the actual event path releases the hook's response.
  const exited = once(child, "exit");
  await fetch(app.config.origin + "/api/review/" + review.id + "/submit", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${app.config.key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ version: 1, round: round.id, revision: 1 }),
  });
  const [code] = await exited;
  assert.equal(code, 2);
  assert.match(message, /feedback is ready/);
  assert.equal(app.store.pending(f.input.owner, f.dir).length, 1);
  assert(app.store.get(review.id).versions[0].rounds[0].notifications.claude);
});

test("Claude Stop hook rearms for the next round without repeating an unacknowledged notification", async (t) => {
  const f = fixture(t),
    state = path.join(f.dir, "state"),
    app = await serve(state);
  t.after(() => app.close());
  const cli = new URL("../bin/starfleet-markup.mjs", import.meta.url);
  const post = async (review, operation, data) => {
    const response = await fetch(
      `${app.config.origin}/api/review/${review.id}/${operation}`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${app.config.key}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(data),
      },
    );
    assert.equal(response.status, 200);
    return response.json();
  };
  const review = app.store.create(f.input),
    first = review.versions[0].rounds[0];
  await post(review, "comments", {
    version: 1,
    round: first.id,
    revision: 0,
    comments: [f.comment],
  });
  await post(review, "submit", { version: 1, round: first.id, revision: 1 });
  await post(review, "notified", {
    version: 1,
    round: first.id,
    owner: f.input.owner,
    worktree: f.dir,
    channel: "claude",
  });
  const waiting = new Promise((resolve) => {
    const onRequest = (req) => {
      if (req.url.startsWith("/api/wait")) {
        app.server.off("request", onRequest);
        resolve();
      }
    };
    app.server.on("request", onRequest);
  });
  const child = spawn(
    process.execPath,
    [cli.pathname, "claude-hook", "--state", state],
    { stdio: ["pipe", "pipe", "pipe"] },
  );
  t.after(() => child.kill());
  let output = "";
  child.stderr.on("data", (b) => {
    output += b;
  });
  const exited = once(child, "exit");
  child.stdin.end(
    JSON.stringify({
      session_id: f.input.owner,
      cwd: f.dir,
      hook_event_name: "Stop",
    }),
  );
  await waiting;
  assert.equal(child.exitCode, null);
  const second = await post(review, "round", { version: 1 });
  await post(review, "comments", {
    version: 1,
    round: second.id,
    revision: 0,
    comments: [{ ...f.comment, id: id(), text: "The next round." }],
  });
  await post(review, "submit", { version: 1, round: second.id, revision: 1 });
  assert.equal((await exited)[0], 2);
  assert.match(output, /The next round/);
  assert.doesNotMatch(output, /Make this clearer/);
  assert.equal(app.store.pending(f.input.owner, f.dir).length, 2);
});
