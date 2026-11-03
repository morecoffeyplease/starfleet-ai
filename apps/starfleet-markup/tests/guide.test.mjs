import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("guide prints unchanged outside Git without creating state", (t) => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "markup-guide-"));
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
  const home = path.join(temporary, "nonexistent-home");
  const cli = fileURLToPath(new URL("../bin/starfleet-markup.mjs", import.meta.url));
  const expected = fs.readFileSync(new URL("../guide.md", import.meta.url), "utf8");
  const result = spawnSync(process.execPath, [cli, "guide", "--print"], {
    cwd: temporary,
    env: { ...process.env, HOME: home, STARFLEET_MARKUP_HOME: path.join(home, "state") },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, expected);
  assert.equal(fs.existsSync(home), false);
  assert.deepEqual(fs.readdirSync(temporary), []);
});
