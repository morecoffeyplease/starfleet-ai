"""Explicit live GitHub record operations; never called by pre-tool hooks.

GitHub body PATCH has no compare-and-swap contract. These operations require
single-writer ownership, retain unrelated text, and detect observed interference.
"""

import json
import re
import shutil
import subprocess
from urllib.parse import quote, urlsplit

START = "<!-- starfleet-policy-guard:assessment:start -->"
END = "<!-- starfleet-policy-guard:assessment:end -->"
STATUS = re.compile(r"Fix rounds: ([0-9]+)(?: \([^\n|]*\))?")


class RecordError(Exception):
    pass


def reference(url, kind):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/" + kind + r"/([1-9][0-9]*)", parsed.path)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment or not match:
        raise RecordError("Expected an https GitHub " + kind + " URL without query or fragment")
    if not parsed.hostname or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname):
        raise RecordError("Invalid GitHub host")
    owner, repo, number = match.groups()
    owner, repo = owner.casefold(), repo.casefold()
    # GitHub browser identities use /pull/, while the REST collection is /pulls/.
    collection = "pulls" if kind == "pull" else kind
    canonical = "https://" + parsed.hostname + "/" + owner + "/" + repo + "/" + kind + "/" + number
    return {"url": canonical, "host": parsed.hostname, "repo": owner + "/" + repo, "number": int(number),
            "endpoint": "repos/" + owner + "/" + repo + "/" + collection + "/" + number}


def api(ref, method="GET", payload=None):
    """Authenticated JSON transport; record and ref identities are checked by callers."""
    # gh-axi's published command surface has no generic REST API command.
    # Delegate authentication to gh rather than reading or handling credentials.
    executable = shutil.which("gh")
    if not executable:
        raise RecordError("GitHub CLI gh is required for live records")
    argv = [executable, "api", "--hostname", ref["host"], "--method", method, ref["endpoint"]]
    if payload is not None:
        argv += ["--input", "-"]
    result = subprocess.run(argv, input=json.dumps(payload) if payload is not None else "",
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        # A failed write might have reached GitHub. Preserve the pending operation
        # and re-read on explicit sync; never blindly replay a mutation here.
        raise RecordError("GitHub " + method + " failed for " + ref["url"] + ": " + result.stderr.strip()[:600])
    return json.loads(result.stdout)


def request(ref, method="GET", payload=None):
    value = api(ref, method, payload)
    if not isinstance(value, dict) or not isinstance(value.get("body"), (str, type(None))):
        raise RecordError("Unexpected GitHub response for " + ref["url"])
    if value.get("html_url", "").casefold() != ref["url"].casefold():
        raise RecordError("GitHub returned a different record identity")
    value["body"] = value.get("body") or ""
    return value


def target_branch(base_ref):
    return base_ref.removeprefix('refs/remotes/origin/').removeprefix('origin/').removeprefix('refs/heads/')


def check_published_snapshot(pr_ref, pr, snapshot):
    """PR base.sha may lag the live branch. Read its exact ref without fetching/rebasing."""
    target = target_branch(snapshot['base_ref'])
    if pr.get('head', {}).get('sha') != snapshot['head'] or pr.get('base', {}).get('ref') != target:
        raise RecordError('Published PR candidate or target changed; refresh evidence and record the report again')
    ref = reference(pr_ref['url'], 'pull')
    ref['endpoint'] = 'repos/' + ref['repo'] + '/git/ref/heads/' + quote(target, safe='')
    observed = api(ref)
    obj = observed.get('object', {}) if isinstance(observed, dict) else {}
    sha = obj.get('sha') if isinstance(obj, dict) else None
    if (not isinstance(observed, dict) or not isinstance(obj, dict) or observed.get('ref') != 'refs/heads/' + target or
            obj.get('type') != 'commit' or not isinstance(sha, str) or
            not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha)):
        raise RecordError('Current target branch response has an invalid ref identity or commit SHA')
    if sha != snapshot['base_head']:
        raise RecordError('Published PR candidate or target changed; current target branch is ' + sha +
                          ', report observed ' + snapshot['base_head'] +
                          '. Refresh the target assessment and report; unchanged verification may be reused')


def purpose(body):
    first = body.strip().split("\n\n", 1)[0].strip()
    if not first or first.startswith(("#", "<!--", "Fix rounds:")):
        raise RecordError("PR body must start with a user problem or benefit paragraph")
    return first


def pr_status(body):
    # Assessment JSON is deliberately fenced, so its text is not another status line.
    matches = list(re.finditer(r"(?m)^Fix rounds: [^\n]*$", body))
    if len(matches) != 1 or not STATUS.fullmatch(matches[0].group()):
        raise RecordError("PR body needs exactly one valid Fix rounds: N status line")
    status = matches[0].group()
    return int(STATUS.fullmatch(status)[1]), status


def tracker_status(body, marker):
    token = "<!-- starfleet-policy-guard:" + marker + " -->"
    rows = [line for line in body.splitlines() if token in line]
    if body.count(token) != 1 or len(rows) != 1 or not rows[0].lstrip().startswith("|"):
        raise RecordError("Tracker needs exactly one markdown table row containing " + token)
    cells = rows[0].split("|")
    found = [STATUS.fullmatch(cell.strip()) for cell in cells if STATUS.fullmatch(cell.strip())]
    if len(found) != 1:
        raise RecordError("Selected tracker row needs exactly one Fix rounds: N cell")
    return int(found[0][1]), found[0].group()


def validate_pr(snapshot, ref, branch, repo, expected_head=None):
    if snapshot.get("state") != "open" or snapshot.get("draft") is not True:
        raise RecordError("Live enrollment/recording requires an open draft PR")
    head = snapshot.get("head", {})
    if head.get("ref") != branch or head.get("repo", {}).get("full_name", "").casefold() != repo.casefold():
        raise RecordError("PR head repository/branch does not match this worktree")
    if snapshot.get("base", {}).get("repo", {}).get("full_name", "").casefold() != repo.casefold():
        raise RecordError("This pilot supports same-repository PRs only")
    if expected_head and head.get("sha") != expected_head:
        raise RecordError("Push the current candidate before enrolling its live PR")
    if ref["repo"].casefold() != repo.casefold():
        raise RecordError("PR URL repository does not match the worktree origin")
    purpose(snapshot["body"])
    pr_status(snapshot["body"])


def origin_repo(remote, host):
    # Only normalize user-provided Git remote identities, never API response URLs.
    if remote.startswith("git@" + host + ":"):
        repo = remote.split(":", 1)[1]
    elif remote.startswith("https://" + host + "/"):
        repo = remote.split("/", 3)[3]
    else:
        raise RecordError("Origin must identify the PR's GitHub host using SSH or HTTPS")
    repo = repo.removesuffix("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise RecordError("Unsupported origin repository identity")
    return repo


def snapshots(item, branch, expected_head=None):
    remote = item["remote"]
    pr = request(remote["pr"])
    validate_pr(pr, remote["pr"], branch, remote["pr"]["repo"], expected_head)
    tracker = request(remote["tracker"])
    if tracker.get("state") != "open" or "pull_request" in tracker:
        raise RecordError("Tracker must be an open issue, not another pull request")
    tracker_status(tracker["body"], remote["marker"])
    return pr, tracker


def replace_status(body, line):
    pr_status(body)
    return re.sub(r"(?m)^Fix rounds: [^\n]*$", lambda _: line, body)


def assessment_block(assessment):
    encoded = json.dumps(assessment, indent=2)
    if "<!-- starfleet-policy-guard:" in encoded:
        raise RecordError("Report contains a reserved record marker")
    fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", encoded)), default=0))
    report = assessment["report"]
    return (START + "\n<details>\n<summary>Recorded zoom-out: " + report["decision"] + "</summary>\n\n"
            + "Purpose: " + report["purpose_quote"] + "\n\nSimplest design: " + report["simplest_design"]
            + "\n\n" + fence + "json\n" + encoded + "\n" + fence + "\n</details>\n" + END)


def render_pr(body, line, assessment):
    body = replace_status(body, line)
    if assessment is None:
        return body
    if body.count(START) != body.count(END) or body.count(START) > 1:
        raise RecordError("Ambiguous existing assessment markers; do not replace them")
    block = assessment_block(assessment)
    if START not in body:
        return body.rstrip() + "\n\n" + block + "\n"
    begin, end = body.index(START), body.index(END) + len(END)
    if end < begin:
        raise RecordError("Assessment markers are out of order")
    return body[:begin] + block + body[end:]


def render_tracker(body, marker, line):
    tracker_status(body, marker)
    token = "<!-- starfleet-policy-guard:" + marker + " -->"
    result = []
    for row in body.splitlines(keepends=True):
        if token in row:
            cells = row.split("|")
            cells = [cell.replace(cell.strip(), line) if STATUS.fullmatch(cell.strip()) else cell for cell in cells]
            row = "|".join(cells)
        result.append(row)
    return "".join(result)


def update(ref, transform):
    first = request(ref)
    target = transform(first)
    if target == first["body"]:
        return first  # Retry after an acknowledged or ambiguous successful PATCH.
    second = request(ref)
    if second["body"] != first["body"] or second.get("updated_at") != first.get("updated_at"):
        raise RecordError("Remote record changed before writing; pending operation preserved for explicit sync")
    # No unsafe-method conditional requests are promised by GitHub here.
    request(ref, "PATCH", {"body": target})
    verified = request(ref)
    if verified["body"] != target:
        raise RecordError("Remote record changed during write; pending operation preserved")
    return verified


def synchronize(item, operation, branch):
    remote = item["remote"]

    def pr_transform(snapshot):
        validate_pr(snapshot, remote["pr"], branch, remote["pr"]["repo"])
        if snapshot["head"]["sha"] != operation["remote_head"] or purpose(snapshot["body"]) != operation["purpose"]:
            raise RecordError("PR head or purpose changed; reassess before completing the pending operation")
        if pr_status(snapshot["body"])[1] not in (operation["before_pr"], operation["line"]):
            raise RecordError("PR status changed independently; refusing to overwrite it")
        return render_pr(snapshot["body"], operation["line"], operation.get("assessment"))

    def tracker_transform(snapshot):
        if snapshot.get("state") != "open" or "pull_request" in snapshot:
            raise RecordError("Tracker is no longer an open issue")
        if tracker_status(snapshot["body"], remote["marker"])[1] not in (operation["before_tracker"], operation["line"]):
            raise RecordError("Tracker status changed independently; refusing to overwrite it")
        return render_tracker(snapshot["body"], remote["marker"], operation["line"])

    # Preserve the user-requested order. Local pending state survives either failure.
    update(remote["pr"], pr_transform)
    update(remote["tracker"], tracker_transform)
    return {"pr": remote["pr"]["url"], "tracker": remote["tracker"]["url"], "status": operation["line"]}
