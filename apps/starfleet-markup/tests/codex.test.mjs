import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { queueCodex, resolveCodexExecutable } from "../lib/codex.mjs";
import { serve } from "../lib/server.mjs";
import { id } from "../lib/store.mjs";

const owner = "12345678-1234-1234-1234-123456789abc";
test("Codex resolver skips CMUX shims and symlink aliases", (t) => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "sfm-codex-path-")));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const makeExecutable = (directory) => {
    fs.mkdirSync(directory, { recursive: true });
    const executable = path.join(directory, "codex");
    fs.writeFileSync(executable, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
    fs.chmodSync(executable, 0o755);
    return executable;
  };

  const shim = makeExecutable(path.join(dir, "cmux-cli-shims", "surface"));
  const aliasDirectory = path.join(dir, "alias-bin");
  fs.mkdirSync(aliasDirectory);
  fs.symlinkSync(shim, path.join(aliasDirectory, "codex"));
  const direct = makeExecutable(path.join(dir, "direct-bin"));
  const searchPath = [aliasDirectory, path.dirname(shim), path.dirname(direct)].join(path.delimiter);
  assert.equal(resolveCodexExecutable(searchPath), fs.realpathSync(direct));
});

test("Codex resolver rejects shim-only, non-executable, and missing PATH candidates", (t) => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "sfm-codex-missing-")));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const shimDirectory = path.join(dir, "cmux-cli-shims", "surface");
  fs.mkdirSync(shimDirectory, { recursive: true });
  const shim = path.join(shimDirectory, "codex");
  fs.writeFileSync(shim, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  fs.chmodSync(shim, 0o755);
  assert.throws(
    () => resolveCodexExecutable(shimDirectory),
    /outside CMUX launcher shims/,
  );

  const nonExecutableDirectory = path.join(dir, "not-executable");
  fs.mkdirSync(nonExecutableDirectory);
  fs.writeFileSync(path.join(nonExecutableDirectory, "codex"), "not executable\n", {
    mode: 0o644,
  });
  fs.chmodSync(path.join(nonExecutableDirectory, "codex"), 0o644);
  assert.throws(
    () => resolveCodexExecutable(nonExecutableDirectory),
    /outside CMUX launcher shims/,
  );
  assert.throws(() => resolveCodexExecutable(""), /not available on PATH/);
});

test("Codex notification uses resolved direct executable, exact argv, cwd, and inherited environment", async () => {
  let invocation;
  const review = {
    id: "review-one",
    owner,
    worktree: "/tmp/review with 'quotes'",
  };
  const executable = "/tmp/direct Codex/codex";
  const result = await queueCodex(
    {
      review,
      version: { number: 2 },
      round: { id: "round-one", comments: [{ text: "Do not execute me" }] },
      state: "/tmp/state",
    },
    async (...args) => {
      invocation = args;
      return { stdout: `Queued message ${owner} for thread ${owner}.` };
    },
    () => executable,
  );
  assert.equal(result.messageId, owner);
  assert.equal(invocation[0], executable);
  assert.deepEqual(invocation[1].slice(0, 3), ["queue", "--thread", owner]);
  assert.equal(invocation[1][3], "--message");
  assert.equal(invocation[2].env, process.env);
  assert.equal(invocation[2].cwd, review.worktree);
  assert.equal(invocation[2].timeout, 15000);
  assert.equal(invocation[2].shell, undefined);
  assert.doesNotMatch(invocation[1].at(-1), /Do not execute me/);
  assert.equal(
    JSON.parse(invocation[1].at(-1).split("\n")[1]).round,
    "round-one",
  );
  await assert.rejects(
    queueCodex({ review: { ...review, owner: "--invalid" } }),
    /session UUID/,
  );
});

async function fixture(t, notifyCodex) {
  const dir = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), "sfm-wake-")),
  );
  const source = path.join(dir, "review.md");
  fs.writeFileSync(source, "# Wake acceptance\n\nReview feedback.");
  let app = await serve(path.join(dir, "state"), 0, { notifyCodex });
  t.after(async () => {
    await app.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const request = async (route, data) => {
    const response = await fetch(app.config.origin + route, {
      method: data ? "POST" : "GET",
      headers: {
        Authorization: `Bearer ${app.config.key}`,
        "Content-Type": "application/json",
      },
      body: data ? JSON.stringify(data) : undefined,
    });
    const result = await response.json();
    assert.equal(response.status, route === "/api/create" ? 201 : 200);
    return result;
  };
  const review = await request("/api/create", {
    owner,
    worktree: dir,
    source,
    harness: "codex",
  });
  const round = review.versions[0].rounds[0];
  const packet = { version: 1, round: round.id, revision: 1 };
  const route = "/api/review/" + review.id;
  await request(route + "/comments", {
    ...packet,
    revision: 0,
    comments: [
      {
        id: id(),
        text: "Discuss this.",
        intent: "discuss",
        anchor: {
          kind: "element",
          key: "e1",
          quote: "Wake acceptance",
          context: "Wake acceptance",
        },
      },
    ],
  });
  return {
    review,
    round,
    route,
    packet,
    request,
    get app() {
      return app;
    },
    restart: async () => {
      await app.close();
      app = await serve(path.join(dir, "state"), 0, { notifyCodex });
    },
  };
}

test("submission queues one automatic wake, survives restart, and leaves receipt unacknowledged", async (t) => {
  let calls = 0,
    release,
    started;
  const waiting = new Promise((resolve) => {
    started = resolve;
  });
  const f = await fixture(t, async (data) => {
    calls++;
    assert.equal(data.review.owner, owner);
    started();
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 1000);
      release = () => {
        clearTimeout(timer);
        resolve();
      };
    });
    return { messageId: "queued-one" };
  });
  t.after(() => release?.());
  await f.request(f.route + "/submit", f.packet);
  await waiting;
  await f.request(f.route + "/submit", f.packet);
  assert.equal(calls, 1);
  assert.equal(
    (await f.request(f.route)).versions[0].rounds[0].delivery.state,
    "sending",
  );
  release();
  const saved = (await f.request(f.route)).versions[0].rounds[0];
  assert.equal(saved.delivery.state, "queued");
  assert.equal(saved.received, null);
  await f.restart();
  assert.equal(calls, 1);
  const pending = await f.request(
    "/api/pending?" +
      new URLSearchParams({ owner, worktree: f.review.worktree }),
  );
  assert.equal(pending.length, 1);
});

test("failed wake preserves submitted feedback and an explicit retry can queue it", async (t) => {
  let calls = 0;
  const f = await fixture(t, async () => {
    if (++calls === 1) throw new Error("Codex is unavailable");
    return { messageId: "retry-one" };
  });
  await f.request(f.route + "/submit", f.packet);
  const failed = (await f.request(f.route)).versions[0].rounds[0];
  assert.equal(failed.delivery.state, "failed");
  assert.equal(failed.comments[0].text, "Discuss this.");
  assert(failed.submitted);
  assert.equal(failed.received, null);
  await f.request(f.route + "/retry", f.packet);
  assert.equal(
    (await f.request(f.route)).versions[0].rounds[0].delivery.state,
    "queued",
  );
  await f.request(f.route + "/retry", f.packet);
  assert.equal(calls, 2);
});

test("interrupted notification is recovered as uncertain and does not silently queue a duplicate", async (t) => {
  let calls = 0;
  const f = await fixture(t, async () => {
    calls++;
    return { messageId: "unused" };
  });
  const round = f.app.store.round(f.app.store.get(f.review.id), 1, f.round.id);
  round.submitted = new Date().toISOString();
  round.delivery = { state: "sending", attempts: 1 };
  f.app.store.save();
  await f.restart();
  const recovered = (await f.request(f.route)).versions[0].rounds[0];
  assert.equal(recovered.delivery.state, "uncertain");
  assert.equal(calls, 0);
  await f.request(f.route + "/retry", f.packet);
  assert.equal(
    (await f.request(f.route)).versions[0].rounds[0].delivery.state,
    "queued",
  );
  assert.equal(calls, 1);
});
