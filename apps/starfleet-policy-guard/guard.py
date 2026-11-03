#!/usr/bin/env python3
"""Local fix-round guard with fixture and explicit live GitHub record commands."""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import github_records as github
import delivery
import delivery_runs

APP = Path(__file__).resolve().parent
POLICY = APP / "config/default-policy.json"
SELF = Path(__file__).resolve()
KINDS = ("product-fix", "initial", "investigation", "rebase", "flake", "proof-harness")
LAST_EVENT = None


class Denied(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if result.returncode:
        raise Denied(result.stderr.strip() or "Not a Git worktree")
    return result.stdout.strip()


def identity(cwd):
    root = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    common = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    gitdir = git(cwd, "rev-parse", "--absolute-git-dir")
    branch = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    return root, common / "starfleet-policy-guard", gitdir + "::" + branch


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".guard-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def state(cwd, create=False):
    root, directory, binding = identity(cwd)
    if not directory.exists() and not create:
        yield root, directory, binding, None
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        # Bounded lock acquisition: a busy ledger must not hang a hook indefinitely.
        import time
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Denied("Guard record busy; retry the same action, not a new batch")
                time.sleep(0.02)
        path = directory / "state.json"
        data = json.loads(path.read_text()) if path.exists() else {"version": 1, "bindings": {}, "items": {}}
        try:
            yield root, directory, binding, data
        except BaseException:
            raise
        else:
            serialized = json.dumps(data, indent=2) + "\n"
            if not path.exists() or path.read_text() != serialized:
                atomic(path, serialized)


def policy():
    value = POLICY.read_text()
    conf = json.loads(value)
    authority = conf.get("approval_authority")
    exceptions = conf.get("exceptions")
    if (not isinstance(authority, dict) or not isinstance(authority.get("label"), str)
            or not authority["label"].strip()):
        raise Denied("Policy needs a nonempty human approval authority label")
    if (not isinstance(exceptions, dict)
            or exceptions.get("require_explicit_human_authorization") is not True
            or exceptions.get("peer_relay_alone_is_authorization") is not False):
        raise Denied("Policy cannot disable explicit human approval or accept peer relay alone")
    return conf, hashlib.sha256(value.encode()).hexdigest()


def item_for(data, binding):
    if not data or binding not in data["bindings"]:
        raise Denied("Worktree/branch is not enrolled. Enroll its draft PR before implementation.")
    return data["items"][data["bindings"][binding]]


def command(cwd, *args):
    return shlex.join([sys.executable, str(SELF), "--cwd", str(cwd), *map(str, args)])


def hook_command(required_root=None):
    argv = ["/bin/sh", str(APP / "hook.sh"), sys.executable, str(SELF)]
    if required_root is not None:
        argv += ["--require-state-root", str(required_root)]
    return shlex.join(argv)


def obligation(item, cwd):
    if item.get("pending_sync"):
        return "PR #{} round {} has an incomplete GitHub update. The stop remains in place. Resume with: {}".format(
            item["pr"], item["count"], command(cwd, "sync"))
    if item.get("current_review"):
        review = item["reviews"][item["current_review"]]
        return "PR #{} is in review {} ({}); the old implementation batch is closed. Record the review, then explicitly accept or dismiss its findings. See: {}".format(
            item["pr"], review["id"], review["status"], command(cwd, "review", "status"))
    active = item.get("active")
    if not active:
        return "PR #{}: no active batch. Register one with: {}".format(
            item["pr"], command(cwd, "begin", "--batch", "<stable-id>", "--kind", "product-fix", "--finding", "<accepted finding>"))
    conf, digest = policy()
    if active["kind"] != "product-fix" or active["round"] < conf["zoom_out"]["first_required_round"]:
        return None
    if active.get("assessment") and active["assessment"]["policy_hash"] == digest:
        return None
    instruction = conf["zoom_out"]["instruction_template"].replace("{N}", str(active["round"])).replace("{pr}", str(item["pr"]))
    return "{}\n\nBatch: {}. Evidence directory: {}\nRecord before acting: {}".format(
        instruction, active["id"], item["evidence_dir"], command(cwd, "record", "--report", "<assessment.json>"))


def ensure_edit(item, cwd):
    message = obligation(item, cwd)
    if message:
        raise Denied(message)


def text_required(value, name):
    if not isinstance(value, str) or not value.strip():
        raise Denied("Missing nonempty report field: " + name)


def status_line(item, decision=None):
    n = item["count"]
    if decision:
        return f"Fix rounds: {n} (zoom-out done {now()[:10]}: {decision})"
    active = item.get("active") or {}
    threshold = policy()[0]["zoom_out"]["first_required_round"]
    return f"Fix rounds: {n}" + (" (zoom-out due)" if active.get("round", 0) >= threshold else "")


def update_record(path, line):
    path = Path(path)
    body = path.read_text()
    if len(re.findall(r"(?m)^Fix rounds: .*?$", body)) != 1:
        raise Denied("Fixture must contain exactly one Fix rounds status line: " + str(path))
    atomic(path, re.sub(r"(?m)^Fix rounds: .*?$", lambda _: line, body))


def live_snapshots(item, root, expected_count):
    snapshots = github.snapshots(item, git(root, "branch", "--show-current"))
    pr_count = github.pr_status(snapshots[0]["body"])[0]
    tracker_count = github.tracker_status(snapshots[1]["body"], item["remote"]["marker"])[0]
    if pr_count != expected_count or tracker_count != expected_count:
        raise Denied("Live PR/tracker counts disagree with local history. Preserve evidence and reconcile ownership; do not reset the count.")
    return snapshots


def sync_pending(item, root):
    operation = item.get("pending_sync")
    if not operation:
        return
    if git(root, "rev-parse", "HEAD") != operation["local_head"]:
        raise Denied("Local candidate changed during the pending update; reassess before continuing")
    if policy()[1] != operation["policy_hash"]:
        raise Denied("Policy changed during the pending update; review the preserved operation before continuing")
    receipt = github.synchronize(item, operation, git(root, "branch", "--show-current"))
    if operation.get("assessment"):
        item["active"]["assessment"] = operation["assessment"]
    item["purpose"] = operation["purpose"]
    item.setdefault("sync_receipts", []).append({**receipt, "at": now(), "operation": operation["id"]})
    del item["pending_sync"]


def queue_sync(item, data, directory, root, snapshots, assessment=None):
    pr, tracker = snapshots
    operation = {
        "id": hashlib.sha256((str(item["count"]) + status_line(item) + json.dumps(assessment)).encode()).hexdigest()[:16],
        "line": status_line(item, assessment["report"]["decision"] if assessment else None),
        "purpose": github.purpose(pr["body"]), "remote_head": pr["head"]["sha"],
        "before_pr": github.pr_status(pr["body"])[1],
        "before_tracker": github.tracker_status(tracker["body"], item["remote"]["marker"])[1],
        "local_head": git(root, "rev-parse", "HEAD"), "policy_hash": policy()[1], "created": now(),
    }
    if assessment:
        operation["assessment"] = assessment
    item["pending_sync"] = operation
    # Commit the journal before external writes. An exception deliberately leaves
    # this state durable, with no approval attached, for an explicit sync retry.
    atomic(directory / "state.json", json.dumps(data, indent=2) + "\n")
    sync_pending(item, root)


def enroll(args):
    url = re.fullmatch(r"https://([^/]+)/([^/]+/[^/]+)/pull/([1-9][0-9]*)", args.pr_url)
    if not url:
        raise Denied("Expected a full https://host/owner/repo/pull/number identity")
    remote = None
    if args.fixture:
        if not args.body or not args.tracker:
            raise Denied("Fixture enrollment requires --body and --tracker files")
        body = args.body.resolve().read_text()
        count = args.count if args.count is not None else 0
    else:
        if args.body or args.tracker or not args.tracker_url or not args.tracker_marker or not args.single_writer:
            raise Denied("Live enrollment requires --tracker-url, --tracker-marker and --single-writer; fixture file options are not live inputs")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,100}", args.tracker_marker):
            raise Denied("Invalid tracker marker")
        pr_ref = github.reference(args.pr_url, "pull")
        args.pr_url = pr_ref["url"]
        tracker_ref = github.reference(args.tracker_url, "issues")
        if (pr_ref["host"], pr_ref["repo"].casefold()) != (tracker_ref["host"], tracker_ref["repo"].casefold()):
            raise Denied("The pilot requires the tracker and PR in the same repository")
        repo = github.origin_repo(git(args.cwd, "remote", "get-url", "origin"), pr_ref["host"])
        snapshot = github.request(pr_ref)
        github.validate_pr(snapshot, pr_ref, git(args.cwd, "branch", "--show-current"), repo, git(args.cwd, "rev-parse", "HEAD"))
        body = snapshot["body"]
        count = github.pr_status(body)[0]
        if args.count is not None and args.count != count:
            raise Denied("Explicit count disagrees with the live PR")
        tracker = github.request(tracker_ref)
        if tracker.get("state") != "open" or "pull_request" in tracker:
            raise Denied("Tracker must be an open issue")
        if github.tracker_status(tracker["body"], args.tracker_marker)[0] != count:
            raise Denied("Tracker count disagrees with the live PR")
        remote = {"pr": pr_ref, "tracker": tracker_ref, "marker": args.tracker_marker, "single_writer": True}
    purpose = body.split("\n\n", 1)[0].strip()
    if not purpose or purpose.startswith("Fix rounds:"):
        raise Denied("PR body must begin with the user problem or benefit")
    if not re.search(rf"(?m)^Fix rounds: {count}(?:\s|$)", body):
        raise Denied("Enrollment count must agree with the PR-body fixture")
    if count < 0:
        raise Denied("Negative count")
    with state(args.cwd, create=True) as (root, directory, binding, data):
        key = args.pr_url
        if binding in data["bindings"] or key in data["items"]:
            raise Denied("Already enrolled; use bind to attach another worktree without resetting history")
        evidence = directory / "evidence" / hashlib.sha256(key.encode()).hexdigest()[:16]
        evidence.mkdir(parents=True, exist_ok=True)
        data["items"][key] = {
            "pr": int(url[3]), "url": key, "purpose": purpose, "count": count,
            "fixture": args.fixture,
            "evidence_dir": str(evidence), "active": None, "history": [], "enrolled": now(),
        }
        if remote:
            data["items"][key]["remote"] = remote
        else:
            data["items"][key].update(body=str(args.body.resolve()), tracker=str(args.tracker.resolve()))
        if count >= policy()[0]["zoom_out"]["first_required_round"]:
            # Existing high counts must not acquire a silent push exemption.
            data["items"][key]["active"] = {
                "id": "enrollment-" + str(count), "kind": "product-fix", "round": count,
                "finding": "Assess the existing enrolled fix count before continuing", "enrollment": True,
                "start_head": git(root, "rev-parse", "HEAD"), "started": now(), "status": "started",
            }
        data["bindings"][binding] = key
        print(json.dumps(data["items"][key], indent=2))


def begin(args):
    text_required(args.finding, "finding / exclusion reason")
    with state(args.cwd) as (root, directory, binding, data):
        open_batch(args, root, directory, binding, data)


def open_batch(args, root, directory, binding, data):
    item = item_for(data, binding)
    review_id = getattr(args, "review_id", None)
    if item.get("current_review") and item["current_review"] != review_id:
        raise Denied("A review is outstanding. Use review decide; do not reopen an old batch or relabel its findings.")
    prior = next((b for b in item["history"] if b["id"] == args.batch), None)
    if prior:
        raise Denied("Batch already closed as " + prior["status"] + "; do not create a second count")
    if item["active"]:
        active = item["active"]
        if active["id"] == args.batch and active["kind"] == args.kind and active["finding"] == args.finding:
            if not item["fixture"]:
                sync_pending(item, root)
            print(json.dumps(active, indent=2))
            return
        raise Denied("Another batch is active: " + active["id"])
    if item.get("pending_sync"):
        raise Denied("Complete the pending GitHub update with sync before opening another batch")
    if args.kind == "initial" and (item["count"] or item["history"]):
        raise Denied("Initial implementation cannot reset an existing fix history")
    if args.kind != "product-fix" and item["history"] and item["history"][-1].get("status") == "abandoned":
        # Abandon cannot be used to relabel a paused fix as evidence work.
        authority = policy()[0]["approval_authority"]["label"]
        raise Denied("After abandonment, start a qualifying fix batch or obtain an exception from " + authority)
    n = item["count"] + (args.kind == "product-fix")
    active = {"id": args.batch, "kind": args.kind, "finding": args.finding, "round": n,
              "start_head": git(root, "rev-parse", "HEAD"), "started": now(), "status": "started"}
    if git(root, "status", "--porcelain"):
        raise Denied("Begin with a clean worktree so batch ownership and abandonment are unambiguous")
    snapshots = live_snapshots(item, root, item["count"]) if not item["fixture"] else None
    item["active"] = active
    item["count"] = n
    if review_id:
        active["review"] = review_id
        item["reviews"][review_id]["decision"] = args.review_decision
        item["reviews"][review_id]["status"] = "decided"
        item["current_review"] = None
    if snapshots:
        queue_sync(item, data, directory, root, snapshots)
    else:
        update_record(item["body"], status_line(item))
        update_record(item["tracker"], status_line(item))
    print(json.dumps(active, indent=2))
    message = obligation(item, root)
    if message:
        print(message)


def record(args):
    report_text = args.report.read_text()
    report = json.loads(report_text)
    with state(args.cwd) as (root, directory, binding, data):
        item = item_for(data, binding)
        active = item["active"]
        if not active or active["kind"] != "product-fix":
            raise Denied("No active fix batch")
        if item.get("pending_sync"):
            raise Denied("Complete the pending GitHub update with sync before recording another assessment")
        if report.get("batch") != active["id"] or report.get("round") != active["round"]:
            raise Denied("Report belongs to another batch or round")
        snapshots = live_snapshots(item, root, item["count"]) if not item["fixture"] else None
        body = snapshots[0]["body"] if snapshots else Path(item["body"]).read_text()
        purpose = body.split("\n\n", 1)[0].strip()
        if report.get("purpose_quote") != purpose:
            raise Denied("Quote the exact current purpose from the PR body")
        for field in ("observed", "concluded", "simplest_design", "design_reason", "decision"):
            text_required(report.get(field), field)
        inventory = report.get("inventory")
        if not isinstance(inventory, list):
            raise Denied("Inventory must be a list, including an explicit empty list if no mechanisms were added")
        for mechanism in inventory:
            for field in ("name", "needed_because", "deletion_effect"):
                text_required(mechanism.get(field), "inventory." + field)
            if type(mechanism.get("lines")) is not int or mechanism["lines"] < 0:
                raise Denied("Each inventory item needs a nonnegative line count")
            if mechanism.get("round") != "unknown" and (type(mechanism.get("round")) is not int or mechanism["round"] < 0):
                raise Denied("Each inventory item needs an originating round or historical unknown")
        if type(report.get("runtime_failure")) is not bool:
            raise Denied("Declare runtime_failure true or false")
        if report["runtime_failure"]:
            for field in ("solo_evidence", "main_evidence"):
                text_required(report.get(field), field)
        else:
            for field in ("finding_quote", "source_path", "unproven"):
                text_required(report.get(field), field)
        if report["decision"] not in ("keep", "delete", "revert", "split out", "simplify"):
            raise Denied("Unknown decision")
        for field in ("adds_scope", "adds_mechanism"):
            if type(report.get(field)) is not bool:
                raise Denied("Declare " + field + " true or false")
        if report["decision"] == "split out" or report["adds_scope"] or report["adds_mechanism"]:
            authority = policy()[0]["approval_authority"]["label"]
            raise Denied(authority + " approval required. Explain purpose, inventory, simpler alternative and tradeoff in plain language. Authorization import is not implemented.")
        conf, digest = policy()
        copy = Path(item["evidence_dir"]) / (hashlib.sha256(active["id"].encode()).hexdigest()[:16] + ".json")
        atomic(copy, report_text)
        assessment = {"report": report, "sha256": hashlib.sha256(report_text.encode()).hexdigest(),
                      "policy_hash": digest, "recorded": now(), "head": git(root, "rev-parse", "HEAD")}
        if snapshots:
            queue_sync(item, data, directory, root, snapshots, assessment)
        else:
            # Update both required records before clearing the stop.
            line = status_line(item, report["decision"])
            update_record(item["body"], line)
            update_record(item["tracker"], line)
            active["assessment"] = assessment
        print("Zoom-out recorded for PR #{} round {}".format(item["pr"], active["round"]))


def finish(args):
    with state(args.cwd) as (root, directory, binding, data):
        item = item_for(data, binding)
        active = item["active"]
        if item.get("pending_sync"):
            raise Denied("Complete the pending GitHub update with sync before closing the batch")
        if not active or active["id"] != args.batch:
            prior = next((b for b in item["history"] if b["id"] == args.batch), None)
            if prior and prior["status"] == args.action:
                print("Already " + args.action)
                return
            raise Denied("No matching active batch")
        if git(root, "status", "--porcelain"):
            raise Denied("Finish/abandon requires a clean worktree; preserve partial work instead of hiding it")
        head = git(root, "rev-parse", "HEAD")
        running_jobs(active)
        snapshots = live_snapshots(item, root, item["count"]) if not item["fixture"] and args.action == "abandoned" else None
        if args.action == "abandoned":
            if head != active["start_head"]:
                raise Denied("Cannot abandon a batch with commits; preserve the repair history")
            if active["kind"] == "product-fix":
                item["count"] -= 1
        else:
            ensure_edit(item, root)
            if active["kind"] == "product-fix":
                commits = git(root, "rev-list", active["start_head"] + "..HEAD").splitlines()
                if not commits and not active.get("enrollment"):
                    raise Denied("A fix round cannot complete without implementation commits")
                for commit in commits:
                    check_trailer(git(root, "show", "-s", "--format=%B", commit), active["round"])
        active["status"] = args.action
        active["finished"] = now()
        active["end_head"] = head
        item["history"].append(active)
        item["active"] = None
        if snapshots:
            queue_sync(item, data, directory, root, snapshots)
        print("Batch " + args.action)


def stable_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,100}", value):
        raise Denied("Use a stable ID of letters, numbers, dots, colons, underscores or hyphens")
    return value


def running_jobs(batch):
    jobs = [key for key, job in batch.get("jobs", {}).items() if job["status"] == "started"]
    if jobs:
        raise Denied("Dispatches still have no completion receipt: " + ", ".join(jobs) + ". Inspect status and the existing jobs; do not start a new round or silently abandon them.")


def current_review(item, review_id, root):
    if item.get("current_review") != review_id:
        raise Denied("Review is not the current registered review: " + review_id)
    review = item["reviews"][review_id]
    if git(root, "rev-parse", "HEAD") != review["head"]:
        raise Denied("The reviewed candidate changed; do not apply a stale review")
    if item.get("pending_sync"):
        raise Denied("Complete the pending record update with sync first")
    return review


def review_command(args):
    if args.review_action == "status":
        with state(args.cwd) as (root, _, binding, data):
            item = item_for(data, binding)
            print(json.dumps({"current_review": item.get("current_review"), "reviews": item.get("reviews", {})}, indent=2))
        return
    with state(args.cwd) as (root, directory, binding, data):
        item = item_for(data, binding)
        reviews = item.setdefault("reviews", {})
        if args.review_action == "start":
            stable_id(args.review)
            reviewers = [stable_id(x) for x in args.reviewer]
            if len(set(reviewers)) != len(reviewers):
                raise Denied("Reviewer IDs must be unique within one review")
            if args.review in reviews:
                review = current_review(item, args.review, root)
                if review["reviewers"] != reviewers:
                    raise Denied("Existing review has different reviewers; do not replace it")
                print(json.dumps(review, indent=2))
                return
            if item.get("current_review") or item.get("pending_sync"):
                raise Denied("Finish the outstanding review or pending sync first")
            if git(root, "status", "--porcelain"):
                raise Denied("Commit the candidate before starting review")
            active = item["active"]
            if active:
                ensure_edit(item, root)
                running_jobs(active)
                if active["kind"] == "product-fix":
                    commits = git(root, "rev-list", active["start_head"] + "..HEAD").splitlines()
                    if not commits and not active.get("enrollment"):
                        raise Denied("Finish implementation or explicitly abandon an unchanged reservation before review")
                    for commit in commits:
                        check_trailer(git(root, "show", "-s", "--format=%B", commit), active["round"])
                active.update(status="completed", finished=now(), end_head=git(root, "rev-parse", "HEAD"))
                item["history"].append(active)
                item["active"] = None
            review = {"id": args.review, "head": git(root, "rev-parse", "HEAD"), "reviewers": reviewers,
                      "jobs": {}, "status": "reviewing", "started": now()}
            reviews[args.review] = review
            item["current_review"] = args.review
            print(json.dumps(review, indent=2))
            return
        if args.review_action == "complete":
            report = json.loads(args.report.read_text())
            if args.review in reviews and reviews[args.review].get("report") == report:
                print("Already recorded; count unchanged")
                return
            review = current_review(item, args.review, root)
            if report.get("review") != args.review or report.get("head") != review["head"]:
                raise Denied("Synthesis must identify this review and its exact head")
            if review.get("report"):
                if review["report"] == report:
                    print("Already recorded")
                    return
                raise Denied("Review already has a different synthesis")
            if any(review["jobs"].get(worker, {}).get("status") != "completed" for worker in review["reviewers"]):
                raise Denied("Each registered reviewer needs a successful completion receipt; an absent report is not a clean review")
            findings = report.get("findings")
            if not isinstance(findings, list) or report.get("verdict") not in ("ready", "not-ready"):
                raise Denied("Synthesis needs ready/not-ready and a findings list")
            ids = []
            for finding in findings:
                ids.append(stable_id(finding.get("id")))
                for field in ("summary", "evidence"):
                    text_required(finding.get(field), "finding." + field)
            if len(ids) != len(set(ids)) or (report["verdict"] == "ready") != (len(ids) == 0):
                raise Denied("A ready review has no findings; a not-ready review needs unique findings")
            review.update(report=report, status="awaiting-decision", completed=now())
            if not findings:
                review["status"] = "decided"
                review["decision"] = {"accepted": [], "dismissed": {}}
                item["current_review"] = None
            print("Review recorded; fix count unchanged")
            return
        decision = json.loads(args.decision.read_text())
        review_id = stable_id(decision.get("review"))
        if review_id in reviews and reviews[review_id].get("decision") == decision:
            if item.get("active", {}) and item["active"].get("review") == review_id:
                sync_pending(item, root)
            print("Decision already applied; count unchanged")
            return
        review = current_review(item, review_id, root)
        if review["status"] != "awaiting-decision":
            raise Denied("Record all reviewer completions and synthesis before deciding")
        accepted, dismissed = decision.get("accepted"), decision.get("dismissed")
        if not isinstance(accepted, list) or not isinstance(dismissed, dict):
            raise Denied("Decision needs accepted IDs and a dismissed-ID-to-reason object")
        ids = {finding["id"] for finding in review["report"]["findings"]}
        if len(accepted) != len(set(accepted)) or set(accepted) & dismissed.keys() or set(accepted) | dismissed.keys() != ids:
            raise Denied("Accept or dismiss every finding exactly once")
        for reason in dismissed.values():
            text_required(reason, "dismissal reason")
        if not accepted:
            if decision.get("batch"):
                raise Denied("An all-dismissed review must not open a fix batch")
            review.update(decision=decision, status="decided")
            item["current_review"] = None
            print("Findings dismissed with reasons; count unchanged")
            return
        batch = stable_id(decision.get("batch"))
        finding_text = "; ".join(f["id"] + ": " + f["summary"] for f in review["report"]["findings"] if f["id"] in accepted)
        batch_args = argparse.Namespace(batch=batch, kind="product-fix", finding=finding_text,
                                        review_id=review_id, review_decision=decision)
        open_batch(batch_args, root, directory, binding, data)


def reserve_job(item, root, kind, identity_id, worker, token):
    if kind == "review":
        review = current_review(item, identity_id, root)
        if review["status"] != "reviewing" or worker not in review["reviewers"]:
            raise Denied("Register this reviewer with review start before dispatch")
        if worker in review["jobs"]:
            previous = review["jobs"][worker]
            if previous["status"] != "failed":
                raise Denied("Reviewer already has a pending or completed receipt; inspect review status rather than dispatch twice")
            review.setdefault("attempts", []).append(previous)
        jobs, key = review["jobs"], worker
    elif kind == "implement":
        ensure_edit(item, root)
        if identity_id != item["active"]["id"]:
            raise Denied("Implementation dispatch batch does not match the active batch")
        jobs, key = item["active"].setdefault("jobs", {}), token
    else:
        raise Denied("Dispatch kind must explicitly be review or implement")
    job = {"token": token, "kind": kind, "identity": identity_id, "worker": worker,
           "status": "started", "head": git(root, "rev-parse", "HEAD"), "started": now(), "registration_pid": os.getpid()}
    jobs[key] = job
    return job


def finish_job(item, kind, identity_id, worker, token, text, succeeded):
    if kind == "review":
        review = item.get("reviews", {}).get(identity_id, {})
        job = review.get("jobs", {}).get(worker)
    else:
        active = item.get("active") or {}
        job = active.get("jobs", {}).get(token) if active.get("id") == identity_id else None
    if not job or job["token"] != token:
        raise Denied("Stale dispatch completion does not match its registered attempt")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 1_000_000:
        succeeded = False
        text = "Missing, empty or oversized dispatch report; inspect the preserved runtime output"
    job.update(status="completed" if succeeded else "failed", finished=now(), report=text,
               sha256=hashlib.sha256(text.encode()).hexdigest())


def agent_dispatch(event, root):
    payload = event.get("tool_input", {})
    prompt = payload.get("prompt", payload.get("message", ""))
    header = prompt.splitlines()[0] if isinstance(prompt, str) and prompt.splitlines() else ""
    match = re.fullmatch(r"Starfleet-Dispatch: (review|implement) ([A-Za-z0-9_.:-]+)(?: ([A-Za-z0-9_.:-]+))?", header)
    with state(root) as (_, _, binding, data):
        item = item_for(data, binding)
        if not match:
            ensure_edit(item, root)
            raise Denied("Agent dispatch needs a first-line Starfleet-Dispatch: implement <batch>, or review <review-id> <reviewer-id>")
        kind, identity_id, worker = match.groups()
        token = event.get("tool_use_id")
        if not token:
            raise Denied("Agent dispatch has no native tool-use identity")
        if event["hook_event_name"] == "PreToolUse":
            if payload.get("run_in_background"):
                raise Denied("This pilot supports synchronous Agent receipts only; do not treat background acceptance as completion")
            job = reserve_job(item, root, kind, identity_id, worker, token)
            job.update(transport="native-agent", tool_use_id=token)
        else:
            response = event.get("tool_response")
            failed = event["hook_event_name"] == "PostToolUseFailure" or (isinstance(response, dict) and
                     (response.get("is_error") or response.get("isError") or response.get("error") or
                      response.get("status") not in (None, "completed", "success")))
            content = response.get("content", response.get("output", "")) if isinstance(response, dict) else response
            if isinstance(content, list):
                text = "\n".join(block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
            else:
                text = content if isinstance(content, str) else ""
            finish_job(item, kind, identity_id, worker, token, text, not failed)


def codex_target(argv, cwd):
    target = Path(cwd)
    for index, value in enumerate(argv):
        if value in ("-C", "--cd"):
            if index + 1 >= len(argv):
                raise Denied("Missing Codex working directory")
            path = Path(argv[index + 1])
            target = path if path.is_absolute() else target / path
        elif value.startswith("--cd="):
            path = Path(value.split("=", 1)[1])
            target = path if path.is_absolute() else target / path
        elif value.startswith("-C"):
            raise Denied("Use separate -C <worktree> arguments so dispatch identity is unambiguous")
    return target.resolve()


def codex_child(args):
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if "exec" not in argv:
        return subprocess.call([args.real_binary, *argv])
    root = codex_target(argv, args.cwd)
    kind = os.environ.get("STARFLEET_DISPATCH_KIND")
    identity_id = os.environ.get("STARFLEET_REVIEW_ID" if kind == "review" else "STARFLEET_BATCH_ID", "")
    worker = os.environ.get("STARFLEET_REVIEWER_ID")
    token = secrets.token_hex(12)
    report = None
    with state(root) as (root, _, binding, data):
        if data is None:
            if kind:
                raise Denied("A declared policy-guard dispatch must target an enrolled worktree")
            return subprocess.call([args.real_binary, *argv], cwd=root)
        item = item_for(data, binding)
        if kind == "review":
            sandbox = [argv[i + 1] for i, x in enumerate(argv[:-1]) if x in ("-s", "--sandbox")]
            if sandbox != ["read-only"] or any(x.startswith(("--dangerously-bypass-approvals", "--sandbox=")) for x in argv):
                raise Denied("Review dispatch requires exactly one explicit -s read-only sandbox")
            for index, arg in enumerate(argv):
                if (arg.startswith("-c") and arg != "-c") or arg.startswith("--config="):
                    raise Denied("Use explicit supported review config arguments")
                if arg in ("-c", "--config") and (index + 1 == len(argv) or not argv[index + 1].startswith(("model=", "model_reasoning_effort="))):
                    raise Denied("Unsupported review config override; do not weaken the read-only sandbox")
            outputs = [argv[i + 1] for i, x in enumerate(argv[:-1]) if x in ("-o", "--output-last-message")]
            if len(outputs) != 1:
                raise Denied("Review dispatch requires a unique -o report in the item's evidence directory")
            report = Path(outputs[0])
            report = report.resolve() if report.is_absolute() else (root / report).resolve()
            if not report.is_relative_to(Path(item["evidence_dir"])) or report.exists():
                raise Denied("Use a new report path inside the item's evidence directory; preserve old evidence")
        job = reserve_job(item, root, kind, identity_id, worker, token)
        job.update(transport="codex-cli", owner_pid=os.getpid())
    succeeded = False
    text = "Dispatch did not finish successfully"
    try:
        result = subprocess.run([args.real_binary, *argv], cwd=root)
        succeeded = result.returncode == 0
        text = report.read_text() if report and report.exists() else ("Implementation process exited " + str(result.returncode))
    finally:
        with state(root) as (_, _, binding, data):
            finish_job(item_for(data, binding), kind, identity_id, worker, token, text, succeeded)
    return result.returncode


def dispatch_command(args):
    with state(args.cwd) as (root, directory, binding, data):
        item = item_for(data, binding)
        if args.kind == "review":
            current_review(item, args.review or "", root)
            if not args.report or not args.reviewer:
                raise Denied("Review dispatch needs --review, --reviewer and --report")
        else:
            ensure_edit(item, root)
            if args.batch != item["active"]["id"]:
                raise Denied("Dispatch needs the active --batch ID")
        binary_record = directory / "real-codex"
        binary = binary_record.read_text().strip() if binary_record.exists() else shutil.which("codex")
    if not binary:
        raise Denied("Codex CLI is unavailable; no dispatch started")
    env = dict(os.environ, STARFLEET_DISPATCH_KIND=args.kind,
               STARFLEET_REVIEW_ID=args.review or "", STARFLEET_REVIEWER_ID=args.reviewer or "",
               STARFLEET_BATCH_ID=args.batch or "")
    argv = ["exec", "-C", str(root), "-s", "read-only" if args.kind == "review" else "workspace-write"]
    if args.report:
        argv += ["-o", str(args.report.resolve())]
    argv += ["-"]
    result = subprocess.run([sys.executable, str(SELF), "--cwd", str(root), "codex-child", "--real-binary", binary, "--", *argv],
                            input=args.brief.read_text(), text=True, env=env)
    return result.returncode


def check_trailer(message, n):
    trailers = git_trailers(message)
    if trailers != [str(n)]:
        raise Denied(f"Commit requires exactly one trailer: Starfleet-Fix-Round: {n}")


def git_trailers(message):
    result = subprocess.run(["git", "interpret-trailers", "--parse"], input=message, text=True, capture_output=True, check=True)
    return re.findall(r"(?mi)^Starfleet-Fix-Round:\s*(\S+)\s*$", result.stdout)


def check(args):
    with state(args.cwd) as (root, directory, binding, data):
        if data is None:
            return
        if args.check_action in ("ready", "merge") and binding not in data["bindings"]:
            return
        item = item_for(data, binding)
        if args.check_action in ("ready", "merge"):
            require_report(item, root)
            return
        if args.check_action == "push":
            if args.refs_stdin:
                for line in sys.stdin:
                    local_ref = line.split()[0]
                    if not local_ref.startswith("refs/heads/"):
                        raise Denied("Fixture push hook supports explicit local branches only")
                    branch = local_ref.removeprefix("refs/heads/")
                    keys = {key for bound, key in data["bindings"].items() if bound.endswith("::" + branch)}
                    if not keys:
                        raise Denied("Pushed branch is not enrolled: " + branch)
                    for key in keys:
                        pushed_item = data["items"][key]
                        if pushed_item["active"] or pushed_item.get("pending_sync") or pushed_item.get("current_review"):
                            ensure_edit(pushed_item, root)
                return
            # Push after a completed batch is allowed; its assessment remains attached.
            if item["active"] or item.get("pending_sync") or item.get("current_review"):
                ensure_edit(item, root)
            return
        ensure_edit(item, root)
        if args.check_action == "commit" and item["active"]["kind"] == "product-fix":
            check_trailer(args.message.read_text(), item["active"]["round"])


def require_report(item, root):
    try:
        report = delivery_runs.check(item.get("delivery_run"), root, item["url"])
    except Exception as error:
        raise Denied(str(error) + ". Run " + command(root, "delivery", "record", "--base-ref", "origin/main",
                     "--pr-url", item["url"], "--evidence", str(Path(item["evidence_dir"]) / "delivery-evidence.json")) +
                     " (use the actual PR target and packet path). No gap acknowledgement or green checklist is required.") from error
    if item["active"] or item.get("pending_sync") or item.get("current_review"):
        ensure_edit(item, root)
    return report


def delivery_record(args):
    with state(args.cwd) as (root, _, binding, data):
        item = item_for(data, binding)
        if args.pr_url.casefold() != item["url"].casefold():
            raise Denied("Report PR must match this worktree's enrolled PR")
        receipt = delivery_runs.run(root, args.base_ref, item["url"], args.evidence, args.policy)
        if item.get("delivery_run"):
            item.setdefault("delivery_history", []).append(item["delivery_run"])
        item["delivery_run"] = receipt
    # Print the entire report, including gaps, rather than just an opaque success token.
    return delivery.display(receipt["report"], args.format)


def gh_child(args):
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    # Ordinary GitHub reads/writes retain their existing behavior and permissions.
    if argv[:2] in (["pr", "ready"], ["pr", "merge"]):
        with state(args.cwd) as (root, _, binding, data):
            if data is not None and binding in data["bindings"]:
                action = delivery_runs.gh_action(argv)
                if not action["exempt"]:
                    item = item_for(data, binding)
                    delivery_runs.target_matches(action, item["url"], git(root, "branch", "--show-current"))
                    report = require_report(item, root)
                    if not item["fixture"]:
                        remote = github.request(item["remote"]["pr"])
                        target_ref = report["snapshot"]["base_ref"]
                        target_name = github.target_branch(target_ref)
                        if (remote.get("state") != "open" or
                                remote.get("base", {}).get("ref") != target_name or
                                remote.get("head", {}).get("ref") != git(root, "branch", "--show-current")):
                            raise Denied("Published PR candidate or target changed; refresh evidence and record the report again")
                        github.check_published_snapshot(item["remote"]["pr"], remote, report["snapshot"])
    return subprocess.call([args.real_binary, *argv], cwd=args.cwd)


def safe_shell(tokens):
    if not tokens:
        return True
    name = Path(tokens[0]).name
    if name in ("pwd", "true", "whoami"):
        return len(tokens) == 1
    if name == "git":
        return len(tokens) > 1 and tokens[1] in ("status", "diff", "log", "show", "rev-parse", "ls-files") and not any(
            t.startswith(("--output", "--ext-diff", "--textconv", "--exec", "--config")) for t in tokens[2:])
    if name in ("cat", "head", "tail", "wc", "ls"):
        return not any(t.startswith(("--files0-from", "--output")) for t in tokens[1:])
    if name == "rg":
        return not any(t.startswith(("--pre", "--hostname-bin")) for t in tokens[1:])
    return False


def shell_targets(payload, cwd):
    raw = payload.get("command", payload.get("cmd", ""))
    if not isinstance(raw, str):
        return [(cwd, False)]
    try:
        # Never interpret command substitutions, redirects, pipelines or compound programs.
        lexer = shlex.shlex(raw, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [(cwd, False)]
    if any(t in (";", "&&", "||", "|", "&", ">", ">>", "<", "(", ")") for t in tokens) or any(c in raw for c in ("`", "$", "\n")):
        return [(cwd, False)]
    actual = Path(payload.get("workdir", payload.get("cwd", cwd)))
    if not actual.is_absolute():
        actual = Path(cwd) / actual
    # Direct git -C is common, and must check the destination rather than the session.
    if tokens and Path(tokens[0]).name == "git":
        while len(tokens) > 2 and tokens[1] == "-C":
            target = Path(tokens[2])
            actual = target if target.is_absolute() else actual / target
            tokens = [tokens[0], *tokens[3:]]
        if len(tokens) > 1 and tokens[1] == "push":
            return [(actual, "push")]
    if tokens and Path(tokens[0]).name == "gh" and tokens[1:3] in (["pr", "ready"], ["pr", "merge"]):
        return [(actual, {"gh_argv": tokens[1:]})]
    if tokens and Path(tokens[0]).name in ("python3", "python", "python3.14") and len(tokens) > 2 and Path(tokens[1]).resolve() == SELF:
        # Narrow recovery CLI, not arbitrary Python. CLI itself validates the action.
        if not any(t in tokens for t in ("enroll", "bind", "begin", "finish", "abandon", "install", "hook", "check")):
            return [(actual, "recovery")]
    return [(actual, "read" if safe_shell(tokens) else False)]


def require_hook_state(args, root, mode=False):
    if (args.require_state_root is not None and root == args.require_state_root.resolve()
            and mode not in ("read", "recovery") and not (identity(root)[1] / "state.json").is_file()):
        raise Denied("Guard state is unavailable for " + str(root) +
                     "; restore its existing enrollment before continuing. Do not reset fix history.")


def hook(args):
    global LAST_EVENT
    event = json.load(sys.stdin)
    LAST_EVENT = {"tool": event.get("tool_name"), "event": event.get("hook_event_name"), "cwd": event.get("cwd")}
    cwd = event.get("cwd", str(args.cwd))
    tool = event.get("tool_name", "")
    payload = event.get("tool_input", {})
    if not isinstance(payload, dict):
        raise Denied("Unsupported tool arguments")
    raw_command = payload.get("command", payload.get("cmd"))
    if isinstance(raw_command, str):
        LAST_EVENT["command"] = raw_command[:250]
    if event.get("hook_event_name") == "SessionStart":
        with state(cwd) as (root, directory, binding, data):
            if data:
                item = item_for(data, binding)
                review = item.get("reviews", {}).get(item.get("current_review"), {})
                brief = {"pr": item["url"], "fix_rounds": item["count"], "active_batch": (item["active"] or {}).get("id"),
                         "current_review": item.get("current_review"), "review_status": review.get("status"),
                         "reviewer_status": {name: job["status"] for name, job in review.get("jobs", {}).items()},
                         "pending_sync": bool(item.get("pending_sync")), "status_command": command(root, "status"),
                         "delivery_report_head": item.get("delivery_run", {}).get("report", {}).get("snapshot", {}).get("head")}
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": json.dumps(brief)}}))
        return
    if tool in ("Read", "Glob", "Grep", "WebSearch", "WebFetch", "AskUserQuestion", "SendUserMessage"):
        return
    if tool in ("Agent", "Task", "spawn_agent"):
        prompt = payload.get("prompt", payload.get("message", ""))
        lines = prompt.splitlines() if isinstance(prompt, str) else []
        target = lines[1].removeprefix("Starfleet-Worktree: ") if len(lines) > 1 and lines[1].startswith("Starfleet-Worktree: ") else payload.get("cwd", cwd)
        require_hook_state(args, identity(target)[0])
        with state(target) as (root, _, _, data):
            enrolled = data is not None
        if not enrolled and lines and lines[0].startswith("Starfleet-Dispatch:"):
            raise Denied("A declared policy-guard Agent dispatch must target an enrolled worktree")
        if enrolled:
            agent_dispatch(event, root)
        return
    if event.get("hook_event_name") != "PreToolUse":
        return
    targets = []
    if tool in ("Bash", "exec_command"):
        targets = shell_targets(payload, cwd)
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = payload.get("file_path", payload.get("notebook_path"))
        targets = [(Path(path).parent if path and Path(path).is_absolute() else Path(cwd) / (Path(path).parent if path else ""), False)]
    elif tool in ("apply_patch", "functions.apply_patch"):
        patch = payload.get("command", payload.get("input", ""))
        paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M) if isinstance(patch, str) else []
        paths += re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M) if isinstance(patch, str) else []
        targets = [(Path(p).parent if Path(p).is_absolute() else Path(cwd) / Path(p).parent, False) for p in paths] or [(cwd, False)]
    else:
        # Dispatches and unknown local tools are conservative while a stop is due.
        targets = [(payload.get("cwd", payload.get("workdir", cwd)), False)]
    for target, mode in targets:
        target = Path(target).resolve()
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            root, directory, binding = identity(target)
        except Denied:
            root, directory, binding = identity(cwd)
        # Reads and narrow recovery calls do not need the ledger, and must not
        # recreate an empty ledger if an installed state file has disappeared.
        if mode in ("read", "recovery"):
            continue
        require_hook_state(args, root, mode)
        with state(root) as (_, directory, binding, data):
            if data is None:
                continue
            if isinstance(mode, dict) and binding not in data["bindings"]:
                continue
            item = item_for(data, binding)
            if isinstance(mode, dict) and "gh_argv" in mode:
                action = delivery_runs.gh_action(mode["gh_argv"])
                if not action["exempt"]:
                    delivery_runs.target_matches(action, item["url"], git(root, "branch", "--show-current"))
                    require_report(item, root)
                continue
            # Only dedicated evidence files may be edited while paused.
            file_path = payload.get("file_path")
            if file_path and Path(file_path).resolve().is_relative_to(Path(item["evidence_dir"])) and tool in ("Write", "Edit"):
                continue
            if mode in ("read", "recovery"):
                continue
            if mode == "push" and not item["active"] and not item.get("pending_sync") and not item.get("current_review"):
                continue
            ensure_edit(item, root)
    # Exit 0 without an allow decision: retain the runtime's ordinary permissions.


def install(args):
    if not args.fixture:
        raise Denied("Installer is disposable-fixture-only. No live installation support yet.")
    root, directory, binding = identity(args.cwd)
    if root == Path(git(APP, "rev-parse", "--show-toplevel")):
        raise Denied("Install only in a disposable test repository, not the source checkout")
    if subprocess.run(["git", "-C", str(root), "config", "--get", "core.hooksPath"], capture_output=True).returncode == 0:
        raise Denied("Existing core.hooksPath; will not replace it")
    paths = [root / ".claude/settings.local.json", root / ".codex/hooks.json"]
    hooks = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
    for path in [*paths, hooks / "commit-msg", hooks / "pre-push"]:
        if path.exists():
            raise Denied("Refusing to replace existing configuration: " + str(path))
    hook_cmd = hook_command(root)
    for path in paths:
        entry = {"type": "command", "command": hook_cmd, "timeout": 5}
        if path.parts[-2] == ".claude":
            entry["onFailure"] = "block"
        atomic(path, json.dumps({"hooks": {
            "PreToolUse": [{"matcher": ".*", "hooks": [entry]}],
            "PostToolUse": [{"matcher": "Agent|Task|spawn_agent", "hooks": [entry]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": hook_cmd, "timeout": 5}]}],
        }}, indent=2) + "\n")
    for name, action, extra in (("commit-msg", "commit", ' --message "$1"'), ("pre-push", "push", " --refs-stdin")):
        path = hooks / name
        atomic(path, "#!/bin/sh\n# Generated by starfleet-policy-guard install --fixture.\nexec " + shlex.join([sys.executable, str(SELF), "check", action]) + extra + "\n")
        path.chmod(0o755)
    binary = shutil.which("codex")
    if binary:
        atomic(directory / "real-codex", binary + "\n")
        wrapper = directory / "bin/codex"
        atomic(wrapper, "#!/bin/sh\n# Generated fixture-local adapter; shared Codex shims are untouched.\nexec " +
               shlex.join([sys.executable, str(SELF), "codex-child", "--real-binary", binary, "--"]) + ' "$@"\n')
        wrapper.chmod(0o755)
    github_binary = shutil.which("gh")
    if github_binary:
        wrapper = directory / "bin/gh"
        atomic(wrapper, "#!/bin/sh\n# Generated fixture-local adapter; shared GitHub CLI is untouched.\nexec " +
               shlex.join([sys.executable, str(SELF), "gh-child", "--real-binary", github_binary, "--"]) + ' "$@"\n')
        wrapper.chmod(0o755)
    print("Installed disposable project hooks only. Codex project trust and hook trust still need explicit activation in the test invocation.")


def evidence(args):
    """Run evidence against a detached copy, without opening the product-edit gate."""
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv:
        raise Denied("Evidence needs a command after --")
    with state(args.cwd) as (root, directory, binding, data):
        item = item_for(data, binding)
        if not item["active"]:
            raise Denied("Register the fix batch before collecting its evidence")
        batch = item["active"]["id"]
        sha = git(root, "rev-parse", "--verify", args.revision + "^{commit}")
        evidence_dir = Path(item["evidence_dir"])
    with tempfile.TemporaryDirectory(prefix="starfleet-proof-") as temp:
        tree = Path(temp) / "worktree"
        git(root, "worktree", "add", "--detach", str(tree), sha)
        try:
            result = subprocess.run(argv, cwd=tree, text=True, capture_output=True, timeout=args.timeout)
            changed = git(tree, "status", "--porcelain")
            receipt = {"batch": batch, "revision": sha, "requested_revision": args.revision, "command": argv,
                       "exit_code": result.returncode, "changed_copy": changed, "at": now(),
                       "stdout": result.stdout[-50000:], "stderr": result.stderr[-50000:]}
            token = hashlib.sha256(json.dumps(receipt).encode()).hexdigest()[:16]
            atomic(evidence_dir / ("run-" + token + ".json"), json.dumps(receipt, indent=2) + "\n")
        finally:
            # This is our disposable proof copy; never clean or reset the user's worktree.
            git(root, "worktree", "remove", "--force", str(tree))
    with state(args.cwd) as (_, _, binding, data):
        item = item_for(data, binding)
        if not item["active"] or item["active"]["id"] != batch:
            raise Denied("Batch changed while evidence ran; receipt preserved but not attached")
        item["active"].setdefault("evidence_runs", []).append(str(evidence_dir / ("run-" + token + ".json")))
    print(json.dumps({"receipt": str(evidence_dir / ("run-" + token + ".json")), "exit_code": result.returncode}))


def trace(outcome, reason=""):
    path = os.environ.get("STARFLEET_POLICY_GUARD_TRACE")
    if path and LAST_EVENT:
        try:
            with Path(path).open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                stream.write(json.dumps({**LAST_EVENT, "outcome": outcome, "reason": reason[:250], "at": now()}) + "\n")
        except OSError:
            # Optional test telemetry must never turn a deliberate deny into exit 1.
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--require-state-root", type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("delivery")
    delivery_sub = p.add_subparsers(dest="delivery_action", required=True)
    for delivery_action in ("snapshot", "report", "record"):
        d = delivery_sub.add_parser(delivery_action)
        d.add_argument("--base-ref", required=True)
        d.add_argument("--format", choices=("text", "json"), default="text")
        if delivery_action in ("report", "record"):
            d.add_argument("--pr-url", required=True)
            d.add_argument("--evidence", type=Path, required=True)
            d.add_argument("--policy", type=Path, default=delivery.POLICY)
    p = sub.add_parser("enroll")
    p.add_argument("--fixture", action="store_true")
    p.add_argument("--pr-url", required=True)
    p.add_argument("--body", type=Path)
    p.add_argument("--tracker", type=Path)
    p.add_argument("--count", type=int)
    p.add_argument("--tracker-url")
    p.add_argument("--tracker-marker")
    p.add_argument("--single-writer", action="store_true")
    p = sub.add_parser("bind")
    p.add_argument("--pr-url", required=True)
    p = sub.add_parser("begin")
    p.add_argument("--batch", required=True)
    p.add_argument("--kind", choices=KINDS, required=True)
    p.add_argument("--finding", required=True)
    p = sub.add_parser("record")
    p.add_argument("--report", type=Path, required=True)
    p = sub.add_parser("review")
    review_sub = p.add_subparsers(dest="review_action", required=True)
    r = review_sub.add_parser("start")
    r.add_argument("--review", required=True)
    r.add_argument("--reviewer", action="append", required=True)
    r = review_sub.add_parser("complete")
    r.add_argument("--review", required=True)
    r.add_argument("--report", type=Path, required=True)
    r = review_sub.add_parser("decide")
    r.add_argument("--decision", type=Path, required=True)
    review_sub.add_parser("status")
    p = sub.add_parser("codex-child")
    p.add_argument("--real-binary", required=True)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p = sub.add_parser("gh-child")
    p.add_argument("--real-binary", required=True)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p = sub.add_parser("dispatch")
    p.add_argument("--kind", choices=("review", "implement"), required=True)
    p.add_argument("--review")
    p.add_argument("--reviewer")
    p.add_argument("--batch")
    p.add_argument("--brief", type=Path, required=True)
    p.add_argument("--report", type=Path)
    p = sub.add_parser("evidence")
    p.add_argument("--revision", required=True)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("command", nargs=argparse.REMAINDER)
    for action in ("completed", "abandoned"):
        p = sub.add_parser(action)
        p.add_argument("--batch", required=True)
    for action in ("status", "hook", "sync"):
        sub.add_parser(action)
    p = sub.add_parser("check")
    p.add_argument("check_action", choices=("edit", "dispatch", "push", "commit", "ready", "merge"))
    p.add_argument("--message", type=Path)
    p.add_argument("--refs-stdin", action="store_true")
    p = sub.add_parser("install")
    p.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "delivery":
            if args.delivery_action == "record":
                return delivery_record(args)
            return delivery.command(args)
        elif args.action == "enroll":
            enroll(args)
        elif args.action == "begin":
            begin(args)
        elif args.action == "record":
            record(args)
        elif args.action == "review":
            review_command(args)
        elif args.action == "codex-child":
            return codex_child(args)
        elif args.action == "gh-child":
            return gh_child(args)
        elif args.action == "dispatch":
            return dispatch_command(args)
        elif args.action == "evidence":
            evidence(args)
        elif args.action in ("completed", "abandoned"):
            finish(args)
        elif args.action == "hook":
            hook(args)
        elif args.action == "check":
            check(args)
        elif args.action == "install":
            install(args)
        elif args.action == "sync":
            with state(args.cwd) as (root, directory, binding, data):
                item = item_for(data, binding)
                sync_pending(item, root)
                print("GitHub records synchronized" if not item["fixture"] else "Fixture has no pending GitHub operation")
        elif args.action == "bind":
            with state(args.cwd) as (root, directory, binding, data):
                if data and args.pr_url not in data["items"]:
                    args.pr_url = args.pr_url.casefold()
                if not data or args.pr_url not in data["items"]:
                    raise Denied("Unknown PR; enroll it once before binding another worktree")
                if binding in data["bindings"]:
                    raise Denied("Already bound")
                data["bindings"][binding] = args.pr_url
                print("Bound without resetting history")
        else:
            with state(args.cwd) as (root, directory, binding, data):
                print(json.dumps(item_for(data, binding), indent=2))
    except Exception as error:
        trace("deny", str(error))
        print("starfleet-policy-guard: " + str(error), file=sys.stderr)
        return 2
    trace("allow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
