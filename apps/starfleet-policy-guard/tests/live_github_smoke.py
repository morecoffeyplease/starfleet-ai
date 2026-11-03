#!/usr/bin/env python3
"""Opt-in live API test, restricted to explicitly marked synthetic draft PRs."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import github_records as github


def run(cwd, *argv, expected=0):
    result = subprocess.run([sys.executable, str(APP / "guard.py"), "--cwd", str(cwd), *map(str, argv)],
                            text=True, capture_output=True)
    if result.returncode != expected:
        raise RuntimeError("Unexpected CLI result: " + result.stdout + result.stderr)
    return result.stdout if expected == 0 else result.stderr


def smoke(args):
    pr_ref = github.reference(args.pr_url, "pull")
    tracker_ref = github.reference(args.tracker_url, "issues")
    before_pr, before_tracker = github.request(pr_ref), github.request(tracker_ref)
    sentinel = "POLICY_GUARD_SYNTHETIC_LIVE_TEST"
    if not before_pr.get("draft") or sentinel not in before_pr["body"] or sentinel not in before_tracker["body"]:
        raise RuntimeError("Only explicitly marked synthetic draft PRs and trackers may be mutated by this test")
    if github.pr_status(before_pr["body"])[0] != 2:
        raise RuntimeError("Synthetic fixture must start at simulated round 2")
    receipt = {"pr": args.pr_url, "tracker": args.tracker_url, "before_pr": before_pr, "before_tracker": before_tracker,
               "source_manifest": {str(p.relative_to(APP)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in APP.rglob("*") if p.is_file() and "__pycache__" not in p.parts}}
    run(args.cwd, "enroll", "--pr-url", args.pr_url, "--tracker-url", args.tracker_url,
        "--tracker-marker", args.marker, "--single-writer")
    item = json.loads(run(args.cwd, "status"))
    receipt["enrolled_count"] = item["count"]
    run(args.cwd, "begin", "--batch", "synthetic-live-round-3", "--kind", "product-fix",
        "--finding", "Synthetic accepted finding used only to exercise durable recording")
    receipt["denial_before_record"] = run(args.cwd, "check", "edit", expected=2)
    report = {
        "batch": "synthetic-live-round-3", "round": 3, "purpose_quote": github.purpose(before_pr["body"]),
        "inventory": [], "observed": "This synthetic fixture starts at simulated round 2; registration advanced its record to round 3.",
        "concluded": "Exercise the record workflow without changing product code or enrolling a real product lane.",
        "runtime_failure": False, "finding_quote": "Synthetic source-only finding for the integration fixture.",
        "source_path": "Synthetic PR body and dedicated tracker row", "unproven": "No product defect is claimed or repaired by this synthetic test.",
        "simplest_design": "Keep the synthetic PR and tracker isolated from real lane state.",
        "design_reason": "This is the current setup.", "decision": "keep", "adds_scope": False, "adds_mechanism": False,
    }
    report_path = Path(item["evidence_dir"]) / "live-assessment.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    run(args.cwd, "record", "--report", report_path)
    run(args.cwd, "check", "edit")
    after_pr, after_tracker = github.request(pr_ref), github.request(tracker_ref)
    assert github.pr_status(after_pr["body"])[0] == 3
    assert "zoom-out done" in github.pr_status(after_pr["body"])[1]
    assert github.tracker_status(after_tracker["body"], args.marker)[1] == github.pr_status(after_pr["body"])[1]
    assert after_pr["body"].startswith(github.purpose(before_pr["body"]))
    assert "UNRELATED_CONTENT_MUST_SURVIVE" in after_pr["body"] and "OTHER_ROW_MUST_SURVIVE" in after_tracker["body"]
    assert github.START in after_pr["body"]
    receipt.update(after_pr=after_pr, after_tracker=after_tracker, local_state=json.loads(run(args.cwd, "status")), result="PASS")
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print("PASS: live enrollment, round-3 stop, durable assessment, PR/tracker agreement, unrelated content retained")
    print(args.receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--tracker-url", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    smoke(parser.parse_args())
