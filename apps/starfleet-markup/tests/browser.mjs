import { chromium } from "playwright";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { serve } from "../lib/server.mjs";
import { outsideGit } from "../lib/store.mjs";

const run = promisify(execFile);
const dir = fs.realpathSync(
  fs.mkdtempSync(path.join(os.tmpdir(), "starfleet-markup-browser-")),
);
const evidence =
  process.env.STARFLEET_MARKUP_EVIDENCE || path.join(dir, "evidence");
fs.mkdirSync(evidence, { recursive: true });
outsideGit(fs.realpathSync(evidence));
const state = path.join(dir, "state"),
  worktree = dir,
  owner = "browser-qa-orchestrator";
const app = await serve(state);
const cli = new URL("../bin/starfleet-markup.mjs", import.meta.url).pathname;
const command = async (...args) =>
  JSON.parse(
    (await run(process.execPath, [cli, ...args, "--state", state])).stdout,
  );
const browser = await chromium.launch({
  headless: true,
  executablePath:
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ||
    (process.platform === "darwin"
      ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      : undefined),
});
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  deviceScaleFactor: 1,
});
await context.addInitScript(() => {
  window.__markupEvents = [];
  window.addEventListener("message", (e) => {
    if (e.data?.channel === "starfleet-markup")
      window.__markupEvents.push({
        type: e.data.type,
        key: e.data.anchor?.key,
      });
  });
  document.addEventListener(
    "click",
    (e) =>
      window.__markupEvents.push({
        click: e.target.tagName,
        text: e.target.textContent?.slice(0, 60),
      }),
    true,
  );
});
const page = await context.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
const checked = [];
const check = (message) => {
  checked.push(message);
  console.log("PASS " + message);
};
try {
  const assets = path.join(dir, "assets");
  fs.mkdirSync(assets);
  fs.writeFileSync(
    path.join(assets, "sample.svg"),
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="80"><rect width="300" height="80" fill="#254f48"/><text x="15" y="48" fill="white" font-size="22">Review illustration</text></svg>',
  );
  const source = path.join(dir, "review.md");
  fs.writeFileSync(
    source,
    "# A calmer way to review\n\nA paragraph for precise feedback.\n\n## Working together\n\nA second paragraph with a decision to discuss.\n\n![Sample illustration](sample.svg)\n",
  );
  const created = await command(
    "create",
    source,
    "--assets",
    assets,
    "--title",
    "Browser acceptance",
    "--owner",
    owner,
    "--worktree",
    worktree,
  );
  await page.goto(created.url);
  await page.locator('.review-surface[aria-busy="false"]').waitFor();
  const doc = page.frameLocator("#document");
  await doc.locator("p").first().click();
  await page.locator("#comment-text").fill("Discuss this opening.");
  await page.locator('input[name="intent"][value="discuss"]').check();
  await page.locator("#save-comment").click();
  await page.locator(".comment-card").waitFor();
  assert.equal(
    await page.locator(".intent-tag").textContent(),
    "Discuss in chat",
  );
  await page.locator(".comment-actions button").last().click();
  await page.locator("#comment-text").fill("Discuss a shorter opening.");
  await page.locator("#save-comment").click();
  await page
    .locator(".comment-body", { hasText: "Discuss a shorter opening." })
    .waitFor();
  check("single-click paragraph annotation, intent, and editing");

  // Reproduce an actual user drag across a substring in the opaque iframe.
  const paragraph = doc.locator("p").nth(1);
  await paragraph.scrollIntoViewIfNeeded();
  const frame = page.frames().find((f) => f.url().includes("/doc/"));
  const coordinates = await frame.evaluate(() => {
    const node = document.querySelectorAll("p")[1].firstChild;
    const range = document.createRange();
    range.setStart(node, 2);
    range.setEnd(node, 8);
    const r = range.getBoundingClientRect();
    return { x1: r.left + 1, x2: r.right - 1, y: r.top + r.height / 2 };
  });
  const box = await page.locator("#document").boundingBox();
  await page.mouse.move(box.x + coordinates.x1, box.y + coordinates.y);
  await page.mouse.down();
  await page.mouse.move(box.x + coordinates.x2, box.y + coordinates.y, {
    steps: 12,
  });
  await page.mouse.up();
  await page.locator("#editor[open]").waitFor({ timeout: 3000 });
  assert.equal(await page.locator("#target-quote").textContent(), "second");
  await page.locator("#comment-text").fill("Change this exact word.");
  await page.locator("#save-comment").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".comment-card").length === 2,
  );
  check("real mouse text selection preserves an exact quote");

  await page.locator(".mode label").filter({ hasText: "Click" }).click();
  await doc.locator("p").first().click();
  assert.equal(await page.locator("#editor").evaluate((el) => el.open), false);
  assert.equal(
    await doc.locator("[data-sfm-ui]").evaluate((el) => el.hidden),
    true,
  );
  await page.locator(".mode label").filter({ hasText: "Comment" }).click();
  await doc
    .getByRole("button", { name: "View comment 1", exact: true })
    .click();
  await page.locator("#editor[open]").waitFor();
  assert.equal(
    await page.locator("#comment-text").inputValue(),
    "Discuss a shorter opening.",
  );
  await page.locator("#close-editor").click();
  await paragraph.scrollIntoViewIfNeeded();
  const quoteBox = await paragraph.evaluate((el) => {
    const range = document.createRange();
    range.setStart(el.firstChild, 2);
    range.setEnd(el.firstChild, 8);
    const rect = range.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  });
  const iframeBox = await page.locator("#document").boundingBox();
  await page.mouse.click(iframeBox.x + quoteBox.x, iframeBox.y + quoteBox.y);
  await page.locator("#editor[open]").waitFor();
  assert.equal(
    await page.locator("#comment-text").inputValue(),
    "Change this exact word.",
  );
  await page.locator("#close-editor").click();
  await page.locator(".mode label").filter({ hasText: "Comment" }).click();
  check(
    "numbered markers and highlights open matching comments only in Comment mode",
  );

  await doc.locator("img").click();
  assert.equal(
    await page.locator("#target-quote").textContent(),
    "Sample illustration",
  );
  await page.locator("#comment-text").fill("Use a smaller illustration.");
  await page.locator('input[name="intent"][value="revise"]').check();
  await page.locator("#save-comment").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".comment-card").length === 3,
  );
  await page.locator(".quote-link").first().click();
  await page.waitForFunction(() => window.scrollY < 500);
  check("image annotation and one-click return to source");

  await doc.locator("h2").click();
  await page.locator("#comment-text").fill("Unfinished draft survives reload.");
  await page.reload();
  await page.locator("#editor[open]").waitFor();
  assert.equal(
    await page.locator("#comment-text").inputValue(),
    "Unfinished draft survives reload.",
  );
  await page.locator("#cancel-editor").click();
  check("unfinished editor draft recovery");

  // A second browser tab cannot silently overwrite the first tab's draft.
  const second = await page.context().newPage();
  await second.goto(created.url);
  await second.bringToFront();
  await second.locator('.review-surface[aria-busy="false"]').waitFor();
  await second.frameLocator("#document").locator("h2").click();
  await second.locator("#comment-text").fill("Stale tab comment");
  await page.bringToFront();
  await doc.locator("h2").click();
  await page.locator("#comment-text").fill("The current heading comment.");
  await page.locator("#save-comment").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".comment-card").length === 4,
  );
  await second.locator("#save-comment").click();
  await second
    .locator("#editor-error", { hasText: "Another tab changed" })
    .waitFor();
  await second.close();
  check("stale-tab conflicts are visible and do not overwrite comments");

  await page.waitForTimeout(300);
  const firstHeight = await page
    .locator("#document")
    .evaluate((el) => el.getBoundingClientRect().height);
  await page.waitForTimeout(500);
  assert.equal(
    await page
      .locator("#document")
      .evaluate((el) => el.getBoundingClientRect().height),
    firstHeight,
  );
  assert(firstHeight < 1500);
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(100);
  await page.screenshot({
    path: path.join(evidence, "desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(200);
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.screenshot({
    path: path.join(evidence, "narrow.png"),
    fullPage: true,
  });
  check(
    "stable iframe height and desktop/narrow layouts without horizontal overflow",
  );

  const listener = command("listen", "--owner", owner, "--worktree", worktree);
  await page.locator("#send").click();
  const feedback = await listener;
  assert.equal(feedback[0].round.comments.length, 4);
  assert.equal(feedback[0].round.comments[1].anchor.kind, "text");
  await page
    .locator("#status", { hasText: "awaiting agent receipt" })
    .waitFor();
  await command(
    "ack",
    created.id,
    "--version",
    "1",
    "--round",
    feedback[0].round.id,
    "--owner",
    owner,
    "--worktree",
    worktree,
  );
  await page
    .locator("#status", { hasText: "Agent received feedback" })
    .waitFor();
  await page.locator(".mode label").filter({ hasText: "Click" }).click();
  await doc.locator("p").first().click();
  assert.equal(await page.locator("#editor").evaluate((el) => el.open), false);
  await page.locator(".mode label").filter({ hasText: "Comment" }).click();
  await doc
    .getByRole("button", { name: "View comment 1", exact: true })
    .click();
  await page.locator("#editor[open]").waitFor();
  assert.equal(
    await page.locator("#comment-text").inputValue(),
    "Discuss a shorter opening.",
  );
  assert.equal(
    await page.locator("#comment-text").getAttribute("readonly"),
    "",
  );
  await page.locator("#close-editor").click();
  await page.locator(".mode label").filter({ hasText: "Comment" }).click();
  check(
    "Send to Agent releases CLI listener; explicit receipt and locked round are visible",
  );

  await page.locator("#new-round").click();
  await page.waitForFunction(
    () => document.querySelector("#count").textContent === "0",
  );
  await doc.locator("h1").click();
  await page.locator("#comment-text").fill("One more thought.");
  await page.locator("#save-comment").click();
  await page.locator("#send").click();
  await page
    .locator("#status", { hasText: "awaiting agent receipt" })
    .waitFor();
  fs.writeFileSync(source, "# Revised document\n\nA shorter opening.");
  await command("version", created.id, source);
  await page.waitForFunction(
    () => document.querySelector("#version").value === "2",
  );
  assert.equal(await page.locator("#count").textContent(), "0");
  await page.locator("#version").selectOption("1");
  await page.locator("#round").selectOption(feedback[0].round.id);
  await page.waitForFunction(
    () => document.querySelectorAll(".comment-card").length === 4,
  );
  assert.equal(await page.locator("#send").isDisabled(), true);
  check("additional rounds and immutable document/comment history");

  const html = path.join(dir, "interactive.html");
  fs.writeFileSync(
    html,
    "<!doctype html><html><head><meta charset=\"utf-8\"><style>body{font:18px sans-serif;padding:30px}button{padding:14px}body{min-height:100vh;margin:0;box-sizing:border-box}</style></head><body><h1>A mockup</h1><button onclick=\"document.querySelector('output').textContent='Clicked'\">Try it</button><output>Ready</output></body></html>",
  );
  const mock = await command(
    "create",
    html,
    "--owner",
    owner,
    "--worktree",
    worktree,
  );
  await page.goto(mock.url);
  await page.locator('.review-surface[aria-busy="false"]').waitFor();
  const mockDoc = page.frameLocator("#document");
  await mockDoc.locator("button").click();
  await page.locator("#editor[open]").waitFor();
  await page.locator("#cancel-editor").click();
  await page.locator(".mode label").filter({ hasText: "Click" }).click();
  await mockDoc.locator("button").click();
  assert.equal(await mockDoc.locator("output").textContent(), "Clicked");
  const h1 = await page
    .locator("#document")
    .evaluate((el) => el.getBoundingClientRect().height);
  await page.waitForTimeout(400);
  assert.equal(
    await page
      .locator("#document")
      .evaluate((el) => el.getBoundingClientRect().height),
    h1,
  );
  const isolated = await page
    .frames()
    .find((f) => f.url().includes("/doc/"))
    .evaluate(() => {
      try {
        return parent.document.title;
      } catch {
        return "isolated";
      }
    });
  assert.equal(isolated, "isolated");
  check(
    "Click/Comment interaction toggle, viewport-height mockup stability, and parent isolation",
  );
  const checklistSource = path.join(dir, "checklist.md");
  fs.writeFileSync(
    checklistSource,
    "# Checklist acceptance\n\n- [x] Completed item\n- [ ] Pending item\n",
  );
  const checklist = await command(
    "create",
    checklistSource,
    "--owner",
    owner,
    "--worktree",
    worktree,
  );
  await page.goto(checklist.url);
  await page.locator('.review-surface[aria-busy="false"]').waitFor();
  const checkboxes = page.frameLocator("#document").getByRole("checkbox");
  assert.equal(await checkboxes.nth(0).isChecked(), true);
  assert.equal(await checkboxes.nth(1).isChecked(), false);
  assert.equal(await checkboxes.nth(0).isDisabled(), true);
  assert.equal(await checkboxes.nth(1).isDisabled(), true);
  check(
    "Markdown checklists preserve completion state without becoming editable",
  );
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    path.join(evidence, "acceptance.json"),
    JSON.stringify(
      {
        date: new Date().toISOString(),
        checked,
        pageErrors: errors,
        state,
        evidence,
      },
      null,
      2,
    ),
  );
  console.log("Evidence: " + evidence);
} catch (error) {
  await page
    .screenshot({ path: path.join(evidence, "failure.png"), fullPage: true })
    .catch(() => {});
  const pages = [];
  for (const p of context.pages()) {
    const frames = [];
    for (const f of p.frames())
      frames.push(
        await f
          .evaluate(() => ({
            url: location.href,
            events: window.__markupEvents,
            ready: document.readyState,
            body: document.body?.textContent.slice(0, 400),
            busy: document
              .querySelector(".review-surface")
              ?.getAttribute("aria-busy"),
            modal: document.querySelector("#editor")?.open,
          }))
          .catch((e) => ({ error: e.message })),
      );
    pages.push(frames);
  }
  fs.writeFileSync(
    path.join(evidence, "failure.json"),
    JSON.stringify(
      { error: error.message, checked, pageErrors: errors, state, pages },
      null,
      2,
    ),
  );
  console.error("Failure evidence: " + evidence);
  throw error;
} finally {
  await browser.close();
  await app.close();
}
