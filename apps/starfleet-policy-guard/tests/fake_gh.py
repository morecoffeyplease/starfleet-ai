"""Fake gh api executable used only by canonical tests; no network calls."""

import json
import os
from pathlib import Path
import sys

path = Path(os.environ["FAKE_GITHUB_DB"])
db = json.loads(path.read_text())
argv = sys.argv[1:]
method = argv[argv.index("--method") + 1]
endpoint = argv[argv.index("--method") + 2]
db.setdefault("calls", []).append([method, endpoint])
if endpoint not in db["records"]:
    print("Not found", file=sys.stderr)
    raise SystemExit(1)
if method == "GET" and db.get("mutate_on_get", {}).get("endpoint") == endpoint:
    db["mutate_on_get"]["remaining"] -= 1
    if db["mutate_on_get"]["remaining"] == 0:
        db["records"][endpoint]["body"] += "\nConcurrent owner's note.\n"
        del db["mutate_on_get"]
if method == "PATCH":
    if db.get("fail_tracker_once") and "/issues/" in endpoint:
        db["fail_tracker_once"] = False
        path.write_text(json.dumps(db))
        print("Simulated tracker failure", file=sys.stderr)
        raise SystemExit(1)
    db["records"][endpoint].update(json.load(sys.stdin))
    if db.get("ambiguous_pr_once") and "/pulls/" in endpoint:
        db["ambiguous_pr_once"] = False
        path.write_text(json.dumps(db))
        print("Simulated lost acknowledgement after write", file=sys.stderr)
        raise SystemExit(1)
path.write_text(json.dumps(db))
print(json.dumps(db["records"][endpoint]))
