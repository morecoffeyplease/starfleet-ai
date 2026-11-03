#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    prefix: { type: "string" },
    bin: { type: "string" },
    uninstall: { type: "boolean" },
  },
});
const source = path.dirname(fileURLToPath(import.meta.url));
const prefix = path.resolve(
  values.prefix ||
    path.join(os.homedir(), ".local/share/starfleet-ai/markup-runtime"),
);
const bin = path.resolve(values.bin || path.join(os.homedir(), ".local/bin"));
const launcher = path.join(bin, "starfleet-markup");
const marker = "# starfleet-markup-owned-launcher:v1";
const quote = (value) => "'" + value.replaceAll("'", "'\\''") + "'";
try {
  if (values.uninstall) {
    if (fs.existsSync(launcher)) {
      if (!fs.readFileSync(launcher, "utf8").includes(marker))
        throw new Error("Refusing to remove an unrecognized launcher");
      fs.unlinkSync(launcher);
    }
    console.log(
      "Removed the owned launcher. Review data and installed runtimes are preserved.",
    );
  } else {
    if (
      fs.existsSync(launcher) &&
      !fs.readFileSync(launcher, "utf8").includes(marker)
    )
      throw new Error("Refusing to overwrite an existing unrecognized command");
    fs.mkdirSync(prefix, { recursive: true, mode: 0o700 });
    fs.mkdirSync(bin, { recursive: true });
    const release = path.join(
      prefix,
      "releases",
      Date.now() + "-" + crypto.randomBytes(4).toString("hex"),
    );
    fs.mkdirSync(release, { recursive: true, mode: 0o700 });
    for (const file of [
      "package.json",
      "package-lock.json",
      "README.md",
      "guide.md",
      "bin",
      "lib",
      "web",
    ]) {
      fs.cpSync(path.join(source, file), path.join(release, file), {
        recursive: true,
      });
    }
    const install = spawnSync(
      "npm",
      ["ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
      { cwd: release, stdio: "inherit" },
    );
    if (install.status !== 0)
      throw new Error(
        "Dependency installation failed; the previous runtime was left active",
      );
    const check = spawnSync(
      process.execPath,
      [path.join(release, "bin/starfleet-markup.mjs"), "--help"],
      { encoding: "utf8" },
    );
    if (check.status !== 0)
      throw new Error(
        "Installed runtime check failed; the previous runtime was left active",
      );
    const next = path.join(
      prefix,
      ".next-" + crypto.randomBytes(4).toString("hex"),
    );
    fs.symlinkSync(release, next);
    fs.renameSync(next, path.join(prefix, "current"));
    const script = `#!/bin/sh\n${marker}\nexec ${quote(process.execPath)} ${quote(path.join(prefix, "current/bin/starfleet-markup.mjs"))} "$@"\n`;
    const temp = launcher + "." + crypto.randomBytes(4).toString("hex");
    fs.writeFileSync(temp, script, { mode: 0o755 });
    fs.renameSync(temp, launcher);
    console.log(
      JSON.stringify(
        {
          command: launcher,
          runtime: path.join(prefix, "current"),
          note: "No service or harness hooks were installed. Start serve and opt into native delivery separately.",
        },
        null,
        2,
      ),
    );
  }
} catch (e) {
  console.error(e.message);
  process.exitCode = 1;
}
