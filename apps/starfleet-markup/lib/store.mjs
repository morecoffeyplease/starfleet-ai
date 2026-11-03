import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { execFileSync } from "node:child_process";
import { marked } from "marked";
import sanitize from "sanitize-html";

export const id = () => crypto.randomBytes(16).toString("hex");
export const now = () => new Date().toISOString();
export const isSessionId = (value) =>
  typeof value === "string" &&
  /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
export function demand(condition, message, status = 400) {
  if (!condition) throw Object.assign(new Error(message), { status });
}
export function atomic(file, data) {
  const temp = `${file}.${id()}.tmp`;
  const fd = fs.openSync(temp, "wx", 0o600);
  try {
    fs.writeFileSync(fd, data);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temp, file);
}
export function outsideGit(directory) {
  // Reject repository markers even when Git cannot load configuration or metadata.
  for (let current = directory; ; current = path.dirname(current)) {
    demand(
      path.basename(current) !== ".git" &&
        !fs.lstatSync(path.join(current, ".git"), { throwIfNoEntry: false }),
      "Review storage must be outside a Git repository",
    );
    if (path.dirname(current) === current) break;
  }
  // A bare repository can have no .git marker. Treat ambiguous Git failures as errors.
  try {
    execFileSync("git", ["-C", directory, "rev-parse", "--git-dir"], {
      stdio: ["ignore", "ignore", "pipe"],
      env: { ...process.env, LC_ALL: "C" },
    });
  } catch (error) {
    demand(
      error.status === 128 &&
        /fatal: not a git repository\b/.test(String(error.stderr)),
      "Unable to check whether the state directory is in Git",
    );
    return;
  }
  throw new Error("Review storage must be outside a Git repository");
}
const allowedAsset =
  /\.(?:css|js|mjs|png|jpe?g|gif|webp|svg|ico|avif|woff2?|ttf|otf|mp4|webm|pdf)$/i;
export class Store {
  constructor(root) {
    fs.mkdirSync(root, { recursive: true, mode: 0o700 });
    this.root = fs.realpathSync(root);
    outsideGit(this.root);
    fs.chmodSync(this.root, 0o700);
    this.file = path.join(this.root, "reviews.json");
    this.data = fs.existsSync(this.file)
      ? JSON.parse(fs.readFileSync(this.file, "utf8"))
      : { reviews: {} };
  }
  save() {
    atomic(this.file, JSON.stringify(this.data, null, 2));
  }
  get(reviewId) {
    demand(/^[a-f0-9]{32}$/.test(reviewId), "Invalid review ID");
    const review = this.data.reviews[reviewId];
    demand(review, "Review not found", 404);
    return review;
  }
  version(review, number) {
    const v = review.versions.find((v) => v.number === Number(number));
    demand(v, "Version not found", 404);
    return v;
  }
  create({ title, owner, worktree, source, assets, harness = null }) {
    demand(
      typeof owner === "string" && owner.trim() && owner.length <= 250,
      "An explicit owner session is required",
    );
    demand(
      typeof worktree === "string" && path.isAbsolute(worktree),
      "An absolute owning worktree is required",
    );
    demand(
      [null, "codex", "claude"].includes(harness),
      "Invalid originating harness",
    );
    if (harness === "codex")
      demand(isSessionId(owner), "Codex wake requires a session UUID");
    const review = {
      id: id(),
      title: String(title || path.basename(source)).slice(0, 300),
      owner,
      worktree: fs.realpathSync(worktree),
      harness,
      created: now(),
      versions: [],
    };
    this.addVersion(review, source, assets);
    this.data.reviews[review.id] = review;
    this.save();
    return review;
  }
  addVersion(review, source, assets) {
    demand(
      !review.versions.length || review.versions.at(-1).rounds.at(-1).submitted,
      "Submit the current round before publishing another version",
      409,
    );
    const file = fs.realpathSync(source);
    const isMarkdown = /\.(md|markdown)$/i.test(file);
    demand(
      isMarkdown || /\.html?$/i.test(file),
      "Choose an HTML or Markdown file",
    );
    demand(fs.statSync(file).size <= 5 * 1024 * 1024, "Document exceeds 5 MiB");
    const raw = fs.readFileSync(file, "utf8");
    const number = review.versions.length + 1;
    const folder = path.join(this.root, review.id, String(number));
    demand(
      !fs.existsSync(folder),
      "Unpublished version storage already exists; recover it or create a separate review",
      409,
    );
    fs.mkdirSync(path.join(folder, "assets"), { recursive: true, mode: 0o700 });
    try {
      // Assets are copied into the immutable version, never served from the source tree.
      if (assets) {
        const base = fs.realpathSync(assets);
        demand(fs.statSync(base).isDirectory(), "Assets must be a directory");
        let bytes = 0;
        let files = 0;
        const copy = (from, to) => {
          for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
            if (
              entry.name.startsWith(".") ||
              ["node_modules", "vendor"].includes(entry.name)
            )
              continue;
            demand(!entry.isSymbolicLink(), "Asset symlinks are not supported");
            const src = path.join(from, entry.name),
              dst = path.join(to, entry.name);
            if (entry.isDirectory()) {
              fs.mkdirSync(dst, { recursive: true, mode: 0o700 });
              copy(src, dst);
            } else if (entry.isFile() && allowedAsset.test(entry.name)) {
              bytes += fs.statSync(src).size;
              files++;
              demand(
                bytes <= 50 * 1024 * 1024 && files <= 2000,
                "Assets exceed 50 MiB or 2,000 files",
              );
              fs.copyFileSync(src, dst);
              fs.chmodSync(dst, 0o600);
            }
          }
        };
        copy(base, path.join(folder, "assets"));
      }
      const content = isMarkdown
        ? sanitize(marked.parse(raw), {
            allowedTags: [
              ...sanitize.defaults.allowedTags,
              "img",
              "h1",
              "h2",
              "input",
            ],
            allowedAttributes: {
              ...sanitize.defaults.allowedAttributes,
              img: ["src", "alt", "width", "height"],
              input: ["type", "checked", "disabled"],
            },
            transformTags: {
              input: (_tag, attributes) => ({
                tagName: "input",
                attribs: {
                  type: "checkbox",
                  disabled: "",
                  ...(Object.hasOwn(attributes, "checked")
                    ? { checked: "" }
                    : {}),
                },
              }),
            },
          })
        : raw;
      atomic(path.join(folder, "source" + (isMarkdown ? ".md" : ".html")), raw);
      atomic(
        path.join(folder, "document.html"),
        isMarkdown
          ? `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/document-theme.css"></head><body><main>${content}</main></body></html>`
          : content,
      );
      const version = {
        number,
        capability: id() + id(),
        created: now(),
        format: isMarkdown ? "markdown" : "html",
        digest: crypto.createHash("sha256").update(raw).digest("hex"),
        rounds: [this.newRound()],
      };
      review.versions.push(version);
      return version;
    } catch (error) {
      // Roll back only the unpublished snapshot allocated by this attempt.
      fs.rmSync(folder, { recursive: true, force: true });
      throw error;
    }
  }
  newRound() {
    return {
      id: id(),
      comments: [],
      revision: 0,
      submitted: null,
      received: null,
    };
  }
  round(review, version, roundId) {
    const v = this.version(review, version);
    const r = v.rounds.find((r) => r.id === roundId);
    demand(r, "Round not found", 404);
    return r;
  }
  edit(reviewId, version, roundId, { revision, comments }) {
    const round = this.round(this.get(reviewId), version, roundId);
    demand(!round.submitted, "Submitted rounds are locked", 409);
    demand(
      round.revision === revision,
      "Another tab changed this review. Reload before editing.",
      409,
    );
    demand(
      Array.isArray(comments) && comments.length <= 500,
      "Invalid comments",
    );
    const ids = new Set();
    const clean = comments.map((c) => {
      demand(
        c &&
          typeof c.id === "string" &&
          /^[a-f0-9]{32}$/.test(c.id) &&
          !ids.has(c.id),
        "Invalid or duplicate comment ID",
      );
      ids.add(c.id);
      demand(
        typeof c.text === "string" && c.text.trim() && c.text.length <= 10000,
        "Comment must contain 1 to 10,000 characters",
      );
      demand(
        ["", "discuss", "revise"].includes(c.intent),
        "Invalid comment intent",
      );
      const a = c.anchor;
      demand(
        a &&
          typeof a.key === "string" &&
          a.key.length <= 100 &&
          typeof a.quote === "string" &&
          a.quote.length <= 20000 &&
          typeof a.context === "string" &&
          a.context.length <= 20000 &&
          ["element", "text"].includes(a.kind),
        "Invalid annotation target",
      );
      if (a.kind === "text")
        demand(
          Number.isInteger(a.start) &&
            Number.isInteger(a.end) &&
            a.start >= 0 &&
            a.end > a.start &&
            a.end <= 10000000,
          "Invalid text offsets",
        );
      return {
        id: c.id,
        text: c.text.trim(),
        intent: c.intent,
        anchor: {
          key: a.key,
          kind: a.kind,
          quote: a.quote,
          context: a.context,
          start: a.start,
          end: a.end,
        },
      };
    });
    round.comments = clean;
    round.revision++;
    this.save();
    return round;
  }
  submit(reviewId, version, roundId, revision) {
    const review = this.get(reviewId),
      round = this.round(review, version, roundId);
    if (round.submitted) return round; // Retry after a lost response is safe.
    demand(
      round.revision === revision,
      "The review changed. Reload before sending.",
      409,
    );
    demand(round.comments.length, "Add at least one comment before sending");
    round.submitted = now();
    this.save();
    return round;
  }
  pending(owner, worktree) {
    const result = [];
    for (const review of Object.values(this.data.reviews)) {
      if (review.owner !== owner || review.worktree !== worktree) continue;
      for (const version of review.versions)
        for (const round of version.rounds) {
          if (round.submitted && !round.received)
            result.push({
              review: review.id,
              title: review.title,
              owner: review.owner,
              worktree: review.worktree,
              version: version.number,
              digest: version.digest,
              round,
            });
        }
    }
    return result;
  }
}
