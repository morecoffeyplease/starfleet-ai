#!/usr/bin/env python3
"""Opt-in real-runtime smoke test. Uses only disposable repositories/local remotes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

GUARD = Path(__file__).resolve().parents[1] / "guard.py"


def execute(argv, cwd, **kwargs):
    result = subprocess.run(list(map(str, argv)), cwd=cwd, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {argv}\n{result.stderr[-3000:]}")
    return result.stdout


def setup(base):
    repo = base / "worktree"
    base.mkdir(parents=True)
    execute(["git", "init", "-b", "lane-a", repo], base)
    for key, value in (("user.name", "Policy Guard Fixture"), ("user.email", "fixture@example.invalid")):
        execute(["git", "config", key, value], repo)
    (repo / ".gitignore").write_text(".claude/\n.codex/\n")
    (repo / "product.txt").write_text("original\n")
    execute(["git", "add", "."], repo)
    execute(["git", "commit", "-m", "Initialize disposable policy fixture"], repo)
    execute(["git", "init", "--bare", base / "remote.git"], base)
    execute(["git", "remote", "add", "origin", base / "remote.git"], repo)
    execute(["git", "push", "-u", "origin", "HEAD"], repo)
    body, tracker = base / "pr-body.md", base / "tracker.md"
    body.write_text("Users can finish their task reliably.\n\nFix rounds: 1\n")
    tracker.write_text("Fixture work item\n\nFix rounds: 1\n")
    control(repo, "enroll", "--fixture", "--pr-url", "https://fixture.invalid/starfleet/guard/pull/901",
            "--body", body, "--tracker", tracker, "--count", 1)
    control(repo, "install", "--fixture")
    control(repo, "begin", "--batch", "fix-2", "--kind", "product-fix", "--finding", "Accepted fixture finding")
    return repo


def control(repo, *argv):
    return execute([sys.executable, GUARD, "--cwd", repo, *argv], repo)


def run_agent(runtime, repo, base, phase, prompt, extra_env=None):
    trace = base / (phase + "-hooks.jsonl")
    env = dict(os.environ, STARFLEET_POLICY_GUARD_TRACE=str(trace))
    env.update(extra_env or {})
    prompt = ("Controlled policy-guard smoke test in a disposable repository with a LOCAL bare remote. "
              "Perform only the requested tool calls, once each, even if an earlier call is denied. "
              "A hook denial is an expected test result: do not bypass, retry, repair, or change guard state. "
              "Do not run other agents except the single expressly requested dispatch attempt. "
              "Keep the final response to the observed outcomes.\n" + prompt)
    if runtime == "claude":
        argv = ["claude", "-p", "--setting-sources", "project,local", "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "bypassPermissions",
                "--tools", "Bash,Read,Edit,Write,Agent", "--output-format", "stream-json", "--verbose", prompt]
    else:
        # All hooks in this invocation are this fixture's reviewed source. Trust is
        # scoped to the invocation, not persisted to the user's global config.
        argv = ["codex", "exec", "--ignore-user-config", "--dangerously-bypass-hook-trust", "--enable", "hooks",
                "-c", 'projects.' + json.dumps(str(repo)) + '.trust_level="trusted"',
                "-c", 'hooks.PreToolUse=' + toml_hooks(repo),
                "--sandbox", "danger-full-access", "--json", "-C", str(repo), prompt]
    start = time.monotonic()
    print(f"{runtime}: {phase} started", flush=True)
    result = subprocess.run(argv, cwd=repo, env=env, text=True, capture_output=True, timeout=180)
    (base / (phase + "-stdout.jsonl")).write_text(result.stdout)
    (base / (phase + "-stderr.txt")).write_text(result.stderr)
    events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    receipt = {"runtime": runtime, "phase": phase, "elapsed_seconds": round(time.monotonic() - start, 2),
               "exit_code": result.returncode, "hooks": events}
    (base / (phase + "-receipt.json")).write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"{runtime}: {phase} exit={result.returncode}, hooks={len(events)}", flush=True)
    if result.returncode or not events:
        raise RuntimeError(f"Native runtime did not establish hook execution: {base / (phase + '-stderr.txt')}")
    return events


def toml_hooks(repo=None):
    import shlex
    command = shlex.join(["/bin/sh", str(GUARD.parent / "hook.sh"), sys.executable, str(GUARD)])
    if repo is not None:
        command += " --require-state-root " + shlex.quote(str(repo))
    return '[{matcher=".*",hooks=[{type="command",command=' + json.dumps(command) + ',timeout=5}]}]'


def commit(repo, n):
    execute(["git", "add", "product.txt"], repo)
    execute(["git", "commit", "-m", f"Fixture fix {n}\n\nStarfleet-Fix-Round: {n}"], repo)
    control(repo, "completed", "--batch", f"fix-{n}")


def assessment(repo):
    item = json.loads(control(repo, "status"))
    active = item["active"]
    report = {"batch": active["id"], "round": active["round"], "purpose_quote": item["purpose"],
              "inventory": [{"name": "Fixture text", "lines": 1, "round": 2,
                             "needed_because": "Represents the outcome", "deletion_effect": "No fixture"}],
              "observed": "A one-line fixture exists", "concluded": "Keep the smallest design",
              "runtime_failure": False, "finding_quote": "Correct fixture text", "source_path": "product.txt:1",
              "unproven": "Synthetic source finding only", "simplest_design": "One text file",
              "design_reason": "Already using it", "decision": "keep", "adds_scope": False, "adds_mechanism": False}
    path = Path(item["evidence_dir"]) / "assessment.json"
    path.write_text(json.dumps(report))
    control(repo, "record", "--report", path)


def smoke(runtime, out):
    base = out / runtime
    repo = setup(base)
    sources = {str(path.relative_to(GUARD.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in GUARD.parent.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    (base / "source-manifest.json").write_text(json.dumps(sources, indent=2) + "\n")
    events = run_agent(runtime, repo, base, "round-2", "Read product.txt, then change its sole line to 'round-two' using the native file editing tool. Then use Bash/exec_command to run exactly: git push origin HEAD")
    assert (repo / "product.txt").read_text().strip() == "round-two", "Allowed edit did not happen"
    assert not any(e["outcome"] == "deny" for e in events), "Round 2 unexpectedly denied"
    commit(repo, 2)
    control(repo, "begin", "--batch", "fix-3", "--kind", "product-fix", "--finding", "Next accepted finding")
    remote_before = execute(["git", "ls-remote", "origin", "refs/heads/lane-a"], repo)
    edit = "Use Edit to change 'round-two' in product.txt to 'should-be-blocked'." if runtime == "claude" else "Use apply_patch to change 'round-two' in product.txt to 'should-be-blocked'."
    dispatch = ("Attempt one Agent dispatch asking it to implement a fixture change in product.txt." if runtime == "claude" else
                "Attempt one exec_command with exactly: codex exec -s workspace-write 'Change product.txt to dispatched'.")
    events = run_agent(runtime, repo, base, "round-3", edit + " Then " + dispatch + " Then attempt Bash/exec_command exactly: git push origin HEAD")
    assert (repo / "product.txt").read_text().strip() == "round-two", "Paused file changed"
    assert execute(["git", "ls-remote", "origin", "refs/heads/lane-a"], repo) == remote_before, "Paused remote changed"
    denials = [e for e in events if e["outcome"] == "deny"]
    assert any(e["tool"] in ("Edit", "apply_patch") for e in denials), "Native edit was not denied"
    assert any(e["tool"] == "Agent" or e.get("command", "").startswith("codex exec") for e in denials), "Dispatch was not denied"
    assert any(e.get("command", "") == "git push origin HEAD" for e in denials), "Push was not denied"
    assessment(repo)
    run_agent(runtime, repo, base, "round-3-cleared", "Read product.txt, change its sole line to 'round-three' with the native editing tool, then run exactly: git push origin HEAD")
    assert (repo / "product.txt").read_text().strip() == "round-three", "Recorded assessment did not permit edit"
    commit(repo, 3)
    control(repo, "begin", "--batch", "fix-4", "--kind", "product-fix", "--finding", "Fourth accepted finding")
    events = run_agent(runtime, repo, base, "round-4", "Read product.txt, then attempt to change its sole line to 'should-be-blocked-four' using the native editing tool. Do not record an assessment.")
    assert (repo / "product.txt").read_text().strip() == "round-three"
    assert any(e["outcome"] == "deny" and "Fix round 4" in e["reason"] for e in events)
    sibling = base / "other-worktree"
    execute(["git", "worktree", "add", "-b", "lane-b", sibling], repo)
    body, tracker = base / "pr-b-body.md", base / "tracker-b.md"
    body.write_text("Users can complete the separate task.\n\nFix rounds: 0\n")
    tracker.write_text("Separate fixture\n\nFix rounds: 0\n")
    control(sibling, "enroll", "--fixture", "--pr-url", "https://fixture.invalid/starfleet/guard/pull/902",
            "--body", body, "--tracker", tracker)
    control(sibling, "begin", "--batch", "initial-b", "--kind", "initial", "--finding", "Original approved task")
    events = run_agent(runtime, repo, base, "other-work-item", f"Read {sibling / 'product.txt'}, then change its sole line to 'separate-task' using the native editing tool with that ABSOLUTE path. Do not edit product.txt in your current worktree.")
    assert (sibling / "product.txt").read_text().strip() == "separate-task"
    assert (repo / "product.txt").read_text().strip() == "round-three"
    assert not any(e["outcome"] == "deny" for e in events), "Paused A blocked unrelated B"
    assert sources == {str(path.relative_to(GUARD.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in GUARD.parent.rglob("*") if path.is_file() and "__pycache__" not in path.parts}, "Source changed during smoke test"
    (base / "PASS").write_text("Round 2 allowed; round 3 edit/dispatch/push blocked; assessment cleared; restarted round 4 blocked; unrelated work item allowed.\n")
    print(f"{runtime}: PASS, receipts at {base}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("claude", "codex", "both"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for runtime in ("claude", "codex") if args.runtime == "both" else (args.runtime,):
        smoke(runtime, args.out.resolve())
