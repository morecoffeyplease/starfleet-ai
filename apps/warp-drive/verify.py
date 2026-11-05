#!/usr/bin/env python3
"""Isolated CLI acceptance for warp-drive; never uses default state or native sends."""

import contextlib
import getpass
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
warp_drive = importlib.import_module("warp_drive")
policy = importlib.import_module("policy")
HAILING_SOURCE = ROOT.parent / "hailing-frequencies" / "hailing_frequencies.py"
spec = importlib.util.spec_from_file_location("warp_verify_hailing", HAILING_SOURCE)
hailing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hailing)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def fixture_hailing_path(state):
    return Path(str(state) + "-hailing")


def enroll_fixture_owners(state, owners):
    directory = fixture_hailing_path(state)
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except (OSError, json.JSONDecodeError):
            config = {}
    else:
        config = {}
    if not isinstance(config, dict) or not isinstance(config.get("targets"), list):
        config = {"version": 1, "targets": [], "remind_after": 60,
                  "escalate_after": 120, "cooldown": 30, "interval": 10}
    known = {(item.get("harness"), item.get("session"), item.get("surface"))
             for item in config["targets"] if isinstance(item, dict)}
    for owner in owners:
        if owner.get("harness") not in ("claude", "codex"):
            continue
        try:
            session = str(uuid.UUID(str(owner["session"]))).upper()
            surface = str(uuid.UUID(str(owner["surface"]))).upper()
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        key = (owner["harness"], session, surface)
        if key in known:
            continue
        config["targets"].append({"label": "verify-fixture", "harness": key[0],
                                  "session": session, "surface": surface,
                                  "workspace": uuid_text(), "started_at": time.time(),
                                  "cutoff": time.time() + 3600})
        known.add(key)
    if owners:
        config.setdefault("version", 1)
        config.setdefault("remind_after", 60)
        config.setdefault("escalate_after", 120)
        config.setdefault("cooldown", 30)
        config.setdefault("interval", 10)
        config_path.write_text(json.dumps(config))
    return directory


def owners_in_cli_args(args):
    words = list(map(str, args))
    if not words:
        return []
    if words[0] == "batch":
        try:
            file_index = words.index("--file") + 1
            items = json.loads(Path(words[file_index]).read_text())
            return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        except (ValueError, IndexError, OSError, json.JSONDecodeError):
            return []
    if words[0] not in ("submit", "run"):
        return []
    boundary = words.index("--") if "--" in words else len(words)
    values = words[:boundary]
    owner = {}
    for flag in ("--harness", "--session", "--surface"):
        if flag in values:
            index = values.index(flag)
            if index + 1 < len(values):
                owner[flag[2:]] = values[index + 1]
    return [owner] if len(owner) == 3 else []


def run_cli(state, *args, cwd=None, ok=True, timeout=30, env=None):
    command = [sys.executable, str(ROOT / "warp_drive.py"), "--state-dir", str(state), *map(str, args)]
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    if not env or "WARP_DRIVE_HAILING_STATE" not in env:
        enrollment_dir = enroll_fixture_owners(state, owners_in_cli_args(args))
        process_env["WARP_DRIVE_HAILING_STATE"] = str(enrollment_dir)
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=process_env)
    if ok and result.returncode != 0:
        raise AssertionError(f"CLI failed ({result.returncode}): {command}\n{result.stdout}\n{result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"CLI unexpectedly succeeded: {command}\n{result.stdout}")
    return result


def parse_json(result):
    return json.loads(result.stdout)


def uuid_text():
    return str(uuid.uuid4()).upper()


def owner_args(owner):
    return ["--harness", owner["harness"], "--session", owner["session"], "--surface", owner["surface"]]


def test_guide_and_install(temp):
    untouched = temp / "guide-state-must-not-exist"
    result = subprocess.run([sys.executable, str(ROOT / "warp_drive.py"), "--state-dir", str(untouched),
                             "guide", "--print"], text=True, capture_output=True, check=True)
    check(result.stdout == (ROOT / "guide.md").read_text(), "source guide output differs")
    check(not untouched.exists(), "guide --print created state")
    for command, expected in (("run", "status or inspect"),
                              ("status", "FIFO rank or ETA"),
                              ("inspect", "FIFO rank or ETA")):
        help_result = subprocess.run([sys.executable, str(ROOT / "warp_drive.py"), command,
                                      "--help"], text=True, capture_output=True, check=True)
        check(expected in " ".join(help_result.stdout.split()),
              f"{command} help omitted its passive diagnostic guidance")

    share, bin_dir, installed_state = temp / "share", temp / "bin", temp / "installed-state"
    install_command = [sys.executable, str(ROOT / "install.py"), "--share-dir", str(share),
                       "--bin-dir", str(bin_dir)]
    installed = subprocess.run(install_command, text=True, capture_output=True)
    check(installed.returncode == 0,
          f"installer CLI failed: {install_command}\n{installed.stdout}\n{installed.stderr}")
    runtimes = list((share / "runtimes").iterdir())
    check(len(runtimes) == 1, "installer did not publish one content-addressed runtime")
    runtime = runtimes[0]
    check(stat.S_IMODE(runtime.stat().st_mode) == 0o555,
          "installed runtime directory did not retain its read-only mode")
    for name in ("warp_drive.py", "host.py", "policy.py", "guide.md"):
        check(stat.S_IMODE((runtime / name).stat().st_mode) == 0o444,
              f"installed runtime file {name} did not retain its read-only mode")
    guide_command = [str(bin_dir / "warp-drive"), "--state-dir", str(installed_state),
                     "guide", "--print"]
    result = subprocess.run(guide_command, cwd=temp, text=True, capture_output=True)
    check(result.returncode == 0,
          f"installed guide CLI failed: {guide_command}\n{result.stdout}\n{result.stderr}")
    check(result.stdout == (ROOT / "guide.md").read_text(), "installed guide differs from source")
    check(not installed_state.exists(), "installed guide --print created state")


def test_install_removes_new_runtime_after_seal_failure(temp):
    spec = importlib.util.spec_from_file_location("warp_verify_install", ROOT / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    share, bin_dir = temp / "seal-failure-share", temp / "seal-failure-bin"
    runtime = share / "runtimes" / installer.digest_source(ROOT)
    chmod = os.chmod
    rmtree = shutil.rmtree

    def fail_runtime_seal(path, mode):
        if Path(path) == runtime and mode == 0o555:
            raise PermissionError("fixture runtime seal failure")
        chmod(path, mode)

    with patch.object(installer.os, "chmod", side_effect=fail_runtime_seal):
        try:
            installer.install(ROOT, share, bin_dir)
        except PermissionError as exc:
            check("fixture runtime seal failure" in str(exc), "unexpected runtime seal error")
        else:
            raise AssertionError("installer unexpectedly sealed the fixture runtime")
    check(not runtime.exists(), "installer left a partial runtime after sealing failed")
    check(not list(runtime.parent.glob(".warp-drive-*")), "installer left a staging runtime after sealing failed")

    def fail_runtime_cleanup(path):
        if Path(path) == runtime:
            raise PermissionError("fixture runtime cleanup failure")
        rmtree(path)

    with patch.object(installer.os, "chmod", side_effect=fail_runtime_seal), \
            patch.object(installer.shutil, "rmtree", side_effect=fail_runtime_cleanup):
        try:
            installer.install(ROOT, share, bin_dir)
        except RuntimeError as exc:
            check("cleanup also failed" in str(exc), "runtime cleanup failure was not reported")
        else:
            raise AssertionError("installer did not report cleanup failure")
    check(runtime.is_dir(), "fixture cleanup failure did not preserve its candidate for inspection")
    mode_before_retry = stat.S_IMODE(runtime.stat().st_mode)
    files_before_retry = {name: (runtime / name).read_bytes() for name in installer.FILES}
    try:
        installer.install(ROOT, share, bin_dir)
    except RuntimeError as exc:
        check("Existing runtime is not sealed" in str(exc), "unsealed runtime retry had an unexpected refusal")
    else:
        raise AssertionError("installer reused an unsealed runtime")
    check(stat.S_IMODE(runtime.stat().st_mode) == mode_before_retry,
          "refusing an unsealed runtime changed its mode")
    check({name: (runtime / name).read_bytes() for name in installer.FILES} == files_before_retry,
          "refusing an unsealed runtime changed its contents")


def test_product_command_inherits_caller_umask(temp):
    wrapper = temp / "caller-umask-exec.py"
    wrapper.write_text("import os,sys\nos.umask(int(sys.argv[1],8))\n"
                       "os.execv(sys.executable,[sys.executable,*sys.argv[2:]])\n")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}

    def cli_command(mask, state, *args):
        return [sys.executable, str(wrapper), f"{mask:o}", str(ROOT / "warp_drive.py"),
                "--state-dir", str(state), *map(str, args)]

    def cli_environment(state):
        result = os.environ.copy()
        result["WARP_DRIVE_HAILING_STATE"] = str(enroll_fixture_owners(state, [owner]))
        return result

    def cli(mask, state, *args):
        result = subprocess.run(cli_command(mask, state, *args), text=True,
                                capture_output=True, timeout=30, env=cli_environment(state))
        check(result.returncode == 0,
              f"umask CLI failed: {args}\n{result.stdout}\n{result.stderr}")
        return result

    def json_documents(output):
        decoder = json.JSONDecoder()
        remaining = output.lstrip()
        values = []
        while remaining:
            value, end = decoder.raw_decode(remaining)
            values.append(value)
            remaining = remaining[end:].lstrip()
        return values

    def wait_for(predicate, description):
        deadline = time.time() + 15
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {description}")

    for mask in (0o022, 0o027):
        for detached in (False, True):
            mode_name = "detached" if detached else "foreground"
            case = temp / f"product-umask-{mode_name}-{mask:03o}"
            state, worktree, target = case / "state", case / "repo", case / "product-directory"
            cli(mask, state, "init")
            worktree.mkdir(parents=True)
            code = ("import os,stat; from pathlib import Path; "
                    f"p=Path({str(target)!r}); os.mkdir(p,0o777); "
                    "Path(str(p/'mode.txt')).write_text(oct(stat.S_IMODE(p.stat().st_mode)))")
            key = f"umask-{mode_name}-{mask:03o}"
            request_args = ("--key", key, *owner_args(owner), "--size", "light",
                            "--family", "verify-umask", "--workers", "1",
                            "--expected-runtime", "60", "--repo", worktree,
                            "--worktree", worktree)
            log_mode = None
            if detached:
                run_result = cli(mask, state, "run", *request_args, "--detach", "--",
                                 sys.executable, "-c", code)
                documents = json_documents(run_result.stdout)
                request_id = documents[0]["request_id"]
                detached_result = documents[1]
                log_mode = stat.S_IMODE(Path(detached_result["log"]).stat().st_mode)
                helper = None
            else:
                helper = subprocess.Popen(cli_command(mask, state, "run", *request_args,
                    "--", sys.executable, "-c", code), text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=cli_environment(state))

                def submitted():
                    with contextlib.closing(warp_drive.connect(state)) as db:
                        return db.execute("SELECT id FROM reservations WHERE request_key=?",
                                          (key,)).fetchone()

                wait_for(submitted, f"{mode_name} request submission")
                with contextlib.closing(warp_drive.connect(state)) as db:
                    request_id = db.execute("SELECT id FROM reservations WHERE request_key=?",
                                             (key,)).fetchone()[0]

            def attached():
                with contextlib.closing(warp_drive.connect(state)) as db:
                    return bool(warp_drive.current_request(db, request_id)["helper_id"])

            wait_for(attached, f"{mode_name} helper attachment")
            result = run_fixture_pass(state, 0.05)
            check(result["granted"] == request_id,
                  f"{mode_name} helper was not granted under caller umask {mask:03o}")
            if helper is not None:
                stdout, stderr = helper.communicate(timeout=20)
                check(helper.returncode == 0, f"foreground child failed: {stdout}\n{stderr}")
            else:
                wait_for(lambda: (target / "mode.txt").is_file(), "detached product directory")
                wait_for(lambda: parse_json(cli(mask, state, "inspect", request_id))[
                    "reservation"]["state"] == "finished", "detached completion")

            actual_mode = int((target / "mode.txt").read_text(), 8)
            expected_mode = 0o777 & ~mask
            state_mode = stat.S_IMODE(state.stat().st_mode)
            db_mode = stat.S_IMODE((state / "warp-drive.sqlite3").stat().st_mode)
            check(actual_mode == expected_mode,
                  f"{mode_name} product directory mode was {actual_mode:03o}, expected {expected_mode:03o} from caller umask {mask:03o}")
            check(state_mode == 0o700 and db_mode == 0o600 and
                  (log_mode is None or log_mode == 0o600),
                  f"private Warp-drive modes changed under caller umask {mask:03o}: "
                  f"state={state_mode:03o}, db={db_mode:03o}, log={log_mode}")


def test_policy_fixtures():
    light = policy.admission_decision({"size": "light", "workers": 1},
        target_cores=8, projected_cores=1, light_ready=True, pressure="normal",
        sample_fresh=True, now=110, last_heavy_grant=107, heavy_wait_seconds=0,
        active_heavy_cores=4)
    check(light[0] and light[1] == "light allowance admitted", "light work was blocked by heavy pacing")
    heavy = policy.admission_decision({"size": "heavy", "workers": 1},
        target_cores=8, projected_cores=1, light_ready=True, pressure="normal",
        sample_fresh=True, now=110, last_heavy_grant=107, heavy_wait_seconds=0,
        active_heavy_cores=4)
    check(not heavy[0] and heavy[1] == "heavy startup observation interval",
          "heavy pacing was not applied")
    stale = policy.admission_decision({"size": "light", "workers": 1},
        target_cores=8, projected_cores=0, light_ready=True, pressure="normal",
        sample_fresh=False, now=110, last_heavy_grant=None, heavy_wait_seconds=0,
        active_heavy_cores=0)
    check(not stale[0], "stale CPU sample admitted work")
    pressure = policy.admission_decision({"size": "light", "workers": 1},
        target_cores=8, projected_cores=0, light_ready=True, pressure="pressure",
        sample_fresh=True, now=110, last_heavy_grant=None, heavy_wait_seconds=0,
        active_heavy_cores=0)
    check(not pressure[0], "memory pressure admitted work")
    drain = policy.admission_decision({"size": "heavy", "workers": 1},
        target_cores=8, projected_cores=6, light_ready=False, pressure="normal",
        sample_fresh=True, now=250, last_heavy_grant=None, heavy_wait_seconds=120,
        active_heavy_cores=4)
    check(not drain[0] and drain[3], "aged heavy request did not request a feasible managed-heavy drain")
    smaller_drain = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=8, projected_cores=6, light_ready=False, pressure="normal",
        sample_fresh=True, now=250, last_heavy_grant=None, heavy_wait_seconds=120,
        active_heavy_cores=4)
    check(not smaller_drain[0] and smaller_drain[3],
          "aged heavy drain stopped when managed heavy capacity fell below its estimate")
    light_only = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=8, projected_cores=5, light_ready=False, pressure="normal",
        sample_fresh=True, now=250, last_heavy_grant=None, heavy_wait_seconds=120,
        active_heavy_cores=0)
    check(not light_only[0] and not light_only[3],
          "light-only or unmanaged load requested a heavy drain")
    active_light_drain = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=8, projected_cores=4, light_ready=True, pressure="normal",
        sample_fresh=True, now=250, last_heavy_grant=None, heavy_wait_seconds=120,
        active_heavy_cores=0, active_managed_cores=1)
    check(not active_light_drain[0] and active_light_drain[3],
          "aged heavy work did not protect its turn while one managed light command could finish")
    unmanaged = policy.admission_decision({"size": "heavy", "workers": 1},
        target_cores=8, projected_cores=8, light_ready=False, pressure="normal",
        sample_fresh=True, now=250, last_heavy_grant=None, heavy_wait_seconds=120,
        active_heavy_cores=2)
    check(not unmanaged[0] and not unmanaged[3], "unmanaged load caused an indefinite managed drain")
    aged_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=0.6, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=0)
    check(aged_escape[0] and aged_escape[1] == "aged heavy escape with meaningful host headroom",
          "aged oversized heavy work remained permanently ineligible with no managed work to drain")
    reflected_active_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=4.0, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=4, active_managed_cores=4)
    check(reflected_active_escape[0] and
          reflected_active_escape[1] == "aged heavy escape with meaningful host headroom",
          "an active but reflected low-CPU heavy command vetoed the aged observed-headroom escape")
    active_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=0.6, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=12, active_managed_cores=12, active_aged_escape_count=1)
    check(active_escape[0] and
          active_escape[1] == "aged heavy escape with meaningful host headroom",
          "one active aged escape prevented a second escape despite all admission guards passing")
    second_escape_startup = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=0.6, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=698, heavy_wait_seconds=130,
        active_heavy_cores=12, active_managed_cores=12, active_aged_escape_count=1)
    check(not second_escape_startup[0] and
          second_escape_startup[1] == "heavy startup observation interval",
          "the second escape bypassed the existing heavy startup guard")
    cap_aged_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=0.6, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=20, active_managed_cores=20,
        active_aged_escape_count=policy.MAX_ACTIVE_AGED_ESCAPES)
    check(not cap_aged_escape[0] and cap_aged_escape[1] == "aged escape limit reached" and
          not cap_aged_escape[3],
          "the oversized aged admission passed the two-escape cap or requested a drain")
    cap_without_headroom = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=5.0, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=20, active_managed_cores=20, active_aged_escape_count=2)
    check(not cap_without_headroom[0] and cap_without_headroom[1] == "aged escape limit reached" and
          not cap_without_headroom[3],
          "an active escape count fell through to managed drain when observed headroom was low")
    fitting_at_cap = policy.admission_decision({"size": "heavy", "workers": 1},
        target_cores=9.5, projected_cores=1.0, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=0,
        active_heavy_cores=8, active_managed_cores=8, active_aged_escape_count=2)
    light_at_cap = policy.admission_decision({"size": "light", "workers": 1},
        target_cores=9.5, projected_cores=1.0, light_ready=True, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=699, heavy_wait_seconds=0,
        active_heavy_cores=8, active_managed_cores=8, active_aged_escape_count=2)
    check(fitting_at_cap[0] and fitting_at_cap[1] == "heavy work fits observed host headroom" and
          light_at_cap[0] and light_at_cap[1] == "light allowance admitted",
          "the escape cap blocked ordinary fitting heavy or light work")
    saturated_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=5.0, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=0)
    check(not saturated_escape[0], "aged heavy escape ignored unmanaged load without meaningful headroom")
    saturated_active_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=5.0, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=4, active_managed_cores=4)
    check(not saturated_active_escape[0] and saturated_active_escape[3],
          "high observed load bypassed the aged escape and protected drain guards")
    saturated_second_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=5.0, light_ready=False, pressure="normal",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=4, active_managed_cores=4, active_aged_escape_count=1)
    check(not saturated_second_escape[0] and not saturated_second_escape[3] and
          saturated_second_escape[1] == "host target leaves insufficient heavy capacity",
          "a blocked second escape started draining or assigned a causal job ID instead of host capacity")
    stale_second_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=0.6, light_ready=False, pressure="normal",
        sample_fresh=False, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=4, active_managed_cores=4, active_aged_escape_count=1)
    check(not stale_second_escape[0] and stale_second_escape[1] == "host sample stale or unavailable" and
          not stale_second_escape[3],
          "a second aged escape ignored the fresh-sample guard")
    pressured_second_escape = policy.admission_decision({"size": "heavy", "workers": 3},
        target_cores=9.5, projected_cores=0.6, light_ready=False, pressure="pressure",
        sample_fresh=True, now=700, last_heavy_grant=None, heavy_wait_seconds=130,
        active_heavy_cores=4, active_managed_cores=4, active_aged_escape_count=1)
    check(not pressured_second_escape[0] and pressured_second_escape[1] == "memory pressure observed" and
          not pressured_second_escape[3],
          "a second aged escape ignored the memory-pressure guard")


def test_scheduler_refreshes_policy_version(temp):
    state = temp / "policy-version-upgrade-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    worktree = temp / "policy-version-worktree"
    worktree.mkdir()
    request_id = submit_and_attach(state, owner, "policy-version-upgrade", worktree, "light")
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "policy_version", "warp-drive-admission-v1")
            warp_drive.event(db, "historical_grant", request_id,
                             policy_version="warp-drive-admission-v1")
        check(warp_drive.status_data(db)["policy_version"] == "warp-drive-admission-v1",
              "generic status read rewrote the persisted policy label before a scheduler pass")
    result = run_fixture_pass(state, 0.05)
    check(result["granted"] == request_id,
          "upgraded policy fixture did not admit its harmless light request")
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(warp_drive.status_data(db)["policy_version"] == policy.POLICY_VERSION,
              "scheduler pass did not refresh the current policy label")
        events = [(row["kind"], json.loads(row["data_json"])) for row in db.execute(
            "SELECT kind,data_json FROM events WHERE reservation_id=? ORDER BY id", (request_id,))]
    historical = next(data for kind, data in events if kind == "historical_grant")
    latest_grant = next(data for kind, data in reversed(events) if kind == "granted")
    check(historical["policy_version"] == "warp-drive-admission-v1" and
          latest_grant["policy_version"] == policy.POLICY_VERSION,
          "policy label refresh rewrote history or emitted the wrong current event version")


def test_explicit_cpu_estimates(temp):
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    owner_flags = owner_args(owner)
    migration_state = temp / "cpu-migration-state"
    run_cli(migration_state, "init")
    migration_path = temp / "cpu-migration-worktree"
    migration_path.mkdir()
    with contextlib.closing(warp_drive.connect(migration_state)) as db:
        with warp_drive.immediate_transaction(db):
            migration_worktree_id = warp_drive.worktree_identity(db, migration_path)
            warp_drive.set_setting(db, "policy_version", "warp-drive-admission-v6")
    migration_content = {"repo": str(migration_path.resolve()),
        "worktree_path": str(migration_path.resolve()), "size": "light",
        "family": "verify", "workers": 1, "expected_runtime": 30.0,
        "command": ["/bin/true"]}
    migration_digest = hashlib.sha256(warp_drive.json_text(migration_content).encode()).hexdigest()
    old_schema = sqlite3.connect(migration_state / "warp-drive.sqlite3")
    old_schema.executescript("""
        DROP INDEX reservations_waiting;
        DROP INDEX reservations_worktree;
        DROP TABLE reservations;
        CREATE TABLE reservations (
            id TEXT PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            content_sha256 TEXT NOT NULL,
            sequence INTEGER NOT NULL UNIQUE,
            worktree_id TEXT NOT NULL REFERENCES worktrees(id),
            repo TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            owner_harness TEXT NOT NULL,
            owner_session TEXT NOT NULL,
            owner_surface TEXT NOT NULL,
            size TEXT NOT NULL CHECK(size IN ('light','heavy')),
            family TEXT NOT NULL,
            workers INTEGER NOT NULL,
            expected_runtime REAL NOT NULL,
            command_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('waiting','reserved','finished','cancelled')),
            outcome TEXT,
            created_at REAL NOT NULL,
            eligible_at REAL,
            credit_seconds REAL NOT NULL DEFAULT 0,
            credit_started_at REAL,
            helper_id TEXT,
            helper_generation INTEGER NOT NULL DEFAULT 0,
            heartbeat_at REAL,
            reservation_claim TEXT,
            claim_helper_id TEXT,
            claim_generation INTEGER,
            granted_at REAL,
            started_at REAL,
            launch_intent_at REAL,
            ended_at REAL,
            exit_code INTEGER,
            lease_active INTEGER NOT NULL DEFAULT 0,
            wait_reason TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            priority_actor TEXT,
            priority_reason TEXT,
            wake_status TEXT,
            wake_detail TEXT
        );
        CREATE INDEX reservations_waiting ON reservations(state, eligible_at, sequence);
        CREATE INDEX reservations_worktree ON reservations(worktree_id, state, priority, sequence);
    """)
    old_schema.execute("""INSERT INTO reservations
        (id,request_key,content_sha256,sequence,worktree_id,repo,worktree_path,
         owner_harness,owner_session,owner_surface,size,family,workers,expected_runtime,
         command_json,state,created_at,eligible_at,credit_seconds,wait_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("legacy-migration-request", "legacy-migration-key", migration_digest, 1,
         migration_worktree_id, str(migration_path.resolve()), str(migration_path.resolve()),
         owner["harness"], owner["session"], owner["surface"], "light", "verify", 1,
         30.0, warp_drive.json_text(["/bin/true"]), "waiting", warp_drive.now(),
         warp_drive.now(), 12.5, "migration fixture"))
    old_schema.commit()
    old_schema.close()
    with contextlib.closing(warp_drive.connect(migration_state)) as db:
        migrated = db.execute("SELECT * FROM reservations WHERE id='legacy-migration-request'").fetchone()
        migrated_columns = {row["name"] for row in db.execute("PRAGMA table_info(reservations)")}
        check({"requested_cpu_cores", "cpu_basis"} <= migrated_columns and
              migrated["requested_cpu_cores"] is None and migrated["cpu_basis"] is None and
              migrated["credit_seconds"] == 12.5 and migrated["content_sha256"] == migration_digest and
              warp_drive.get_setting(db, "policy_version") == "warp-drive-admission-v6",
              "v7 schema migration changed a legacy row, hash, credit, or scheduler version")
    migrated_retry = parse_json(run_cli(migration_state, "submit", "--key", "legacy-migration-key",
        *owner_flags, "--size", "light", "--family", "verify", "--workers", "1",
        "--expected-runtime", "30", "--repo", str(migration_path), "--worktree",
        str(migration_path), "--", "/bin/true"))
    check(migrated_retry["request_id"] == "legacy-migration-request" and
          not migrated_retry["created"],
          "a pre-v7 row did not retain request-key identity after nullable-column migration")

    fallback_state = temp / "cpu-fallback-state"
    run_cli(fallback_state, "init")
    with contextlib.closing(warp_drive.connect(fallback_state)) as db:
        check(warp_drive.get_setting(db, "policy_version") is None,
              "client initialization marked an unobserved scheduler as policy v7")
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "policy_version", "warp-drive-admission-v6")
    path = temp / "cpu-fallback-worktree"
    path.mkdir()
    fallback_content = ["submit", "--key", "cpu-fallback", *owner_flags,
        "--size", "heavy", "--family", "serial-gpu-inference", "--workers", "4",
        "--expected-runtime", "30", "--repo", str(path), "--worktree", str(path),
        "--", "/bin/true"]
    rejected = run_cli(fallback_state, *fallback_content[:-2],
        "--cpu-cores", "0.5", "--cpu-basis", "synthetic fixture evidence",
        *fallback_content[-2:], ok=False)
    check("warp-drive-admission-v7" in rejected.stderr and "coordinator-authorized" in rejected.stderr,
          "explicit estimate was accepted while the shared state reported old policy v6")
    with contextlib.closing(warp_drive.connect(fallback_state)) as db:
        check(warp_drive.get_setting(db, "policy_version") == "warp-drive-admission-v6",
              "client submission rewrote the persisted scheduler policy version")

    run_fixture_pass(fallback_state, 0.10, cpus=18)
    fallback = parse_json(run_cli(fallback_state, *fallback_content))
    with contextlib.closing(warp_drive.connect(fallback_state)) as db:
        with db:
            warp_drive.attach_helper(db, fallback["request_id"], owner, str(uuid.uuid4()))
    fallback_pass = run_fixture_pass(fallback_state, 0.10, cpus=18)
    check(fallback_pass["granted"] is None,
          "the four-worker heavy fallback unexpectedly fit the synthetic 18-CPU host")
    with contextlib.closing(warp_drive.connect(fallback_state)) as db:
        fallback_candidate = warp_drive.admission_diagnostics(db)["candidates"][0]
        fallback_row = db.execute("SELECT content_sha256 FROM reservations WHERE id=?",
                                  (fallback["request_id"],)).fetchone()
    legacy_content = {"repo": str(path.resolve()), "worktree_path": str(path.resolve()),
        "size": "heavy", "family": "serial-gpu-inference", "workers": 4,
        "expected_runtime": 30.0, "command": ["/bin/true"]}
    legacy_digest = hashlib.sha256(warp_drive.json_text(legacy_content).encode()).hexdigest()
    check(fallback_candidate["estimated_cores"] == 16 and
          fallback_candidate["requested_cpu_cores"] is None and
          fallback_candidate["cpu_basis"] is None and
          fallback_row["content_sha256"] == legacy_digest,
          "omitted fields changed legacy fallback estimates or request-key hashing")
    retry = parse_json(run_cli(fallback_state, *fallback_content))
    check(retry["request_id"] == fallback["request_id"] and not retry["created"],
          "an identical legacy request-key retry did not reuse its row")
    with contextlib.closing(warp_drive.connect(fallback_state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "policy_version", "warp-drive-admission-v6")
    old_policy_retry = parse_json(run_cli(fallback_state, *fallback_content))
    check(old_policy_retry["request_id"] == fallback["request_id"] and
          not old_policy_retry["created"],
          "legacy idempotent reuse was blocked under an older scheduler policy")
    changed_key = run_cli(fallback_state, *fallback_content[:-2],
        "--cpu-cores", "0.5", "--cpu-basis", "synthetic fixture evidence",
        *fallback_content[-2:], ok=False)
    check("already names different work" in changed_key.stderr,
          "a changed CPU estimate reused a legacy request key as identical work")

    old_policy_batch_paths = [temp / "batch-old-legacy", temp / "batch-old-explicit"]
    for batch_path in old_policy_batch_paths:
        batch_path.mkdir()
    old_policy_items = [{"key": "batch-legacy-under-v6", "harness": owner["harness"],
        "session": owner["session"], "surface": owner["surface"], "size": "light",
        "family": "verify", "workers": 1, "expected_runtime": 30,
        "worktree": str(old_policy_batch_paths[0]), "command": ["/bin/true"]},
        {"key": "batch-explicit-under-v6", "harness": owner["harness"],
         "session": owner["session"], "surface": owner["surface"], "size": "heavy",
         "family": "verify", "workers": 1, "expected_runtime": 30,
         "worktree": str(old_policy_batch_paths[1]), "command": ["/bin/true"],
         "cpu_cores": 0.5, "cpu_basis": "synthetic fixture evidence"}]
    old_policy_batch_file = temp / "old-policy-batch.json"
    old_policy_batch_file.write_text(json.dumps(old_policy_items))
    batch_rejected = run_cli(fallback_state, "batch", "--file", str(old_policy_batch_file), ok=False)
    with contextlib.closing(warp_drive.connect(fallback_state)) as db:
        inserted_batch_rows = db.execute("SELECT count(*) FROM reservations WHERE request_key LIKE 'batch-%'").fetchone()[0]
    check("warp-drive-admission-v7" in batch_rejected.stderr and inserted_batch_rows == 0,
          "old-policy batch inserted a legacy member before rejecting its explicit estimate")

    explicit_state = temp / "cpu-explicit-state"
    run_cli(explicit_state, "init")
    explicit_path = temp / "cpu-explicit-worktree"
    explicit_path.mkdir()
    run_fixture_pass(explicit_state, 0.10, cpus=18)
    invalid_batch_path = temp / "cpu-invalid-batch-worktree"
    invalid_batch_path.mkdir()
    invalid_batch_item = {"key": "cpu-invalid-batch", "harness": owner["harness"],
        "session": owner["session"], "surface": owner["surface"], "size": "heavy",
        "family": "verify", "workers": 1, "expected_runtime": 30,
        "worktree": str(invalid_batch_path), "command": ["/bin/true"],
        "cpu_cores": True, "cpu_basis": "JSON booleans are not CPU estimates"}
    invalid_batch_file = temp / "cpu-invalid-batch.json"
    invalid_batch_file.write_text(json.dumps([invalid_batch_item]))
    invalid_batch_result = run_cli(explicit_state, "batch", "--file", str(invalid_batch_file), ok=False)
    check("finite positive" in invalid_batch_result.stderr,
          "batch accepted a JSON boolean as a numeric CPU estimate")
    explicit_content = ["submit", "--key", "cpu-explicit", *owner_flags,
        "--size", "heavy", "--family", "serial-gpu-inference", "--workers", "4",
        "--cpu-cores", "0.5", "--cpu-basis", "synthetic low-demand fixture",
        "--expected-runtime", "30", "--repo", str(explicit_path), "--worktree", str(explicit_path),
        "--", "/bin/true"]
    for invalid, reason in (("0", "finite positive"), ("-1", "finite positive"),
                            ("nan", "finite positive"), ("inf", "finite positive")):
        invalid_result = run_cli(explicit_state, "submit", "--key", f"invalid-{invalid}",
            *owner_flags, "--size", "heavy", "--family", "verify", "--workers", "1",
            "--cpu-cores", invalid, "--cpu-basis", "fixture", "--expected-runtime", "30",
            "--repo", str(explicit_path), "--worktree", str(explicit_path), "--", "/bin/true",
            ok=False)
        check(reason in invalid_result.stderr,
              f"invalid CLI estimate {invalid!r} was not rejected as nonpositive or nonfinite")
    missing_basis = run_cli(explicit_state, "submit", "--key", "missing-cpu-basis",
        *owner_flags, "--size", "heavy", "--family", "verify", "--workers", "1",
        "--cpu-cores", "0.5", "--expected-runtime", "30", "--repo", str(explicit_path),
        "--worktree", str(explicit_path), "--", "/bin/true", ok=False)
    missing_estimate = run_cli(explicit_state, "submit", "--key", "missing-cpu-estimate",
        *owner_flags, "--size", "heavy", "--family", "verify", "--workers", "1",
        "--cpu-basis", "fixture", "--expected-runtime", "30", "--repo", str(explicit_path),
        "--worktree", str(explicit_path), "--", "/bin/true", ok=False)
    check("provided together" in missing_basis.stderr and
          "provided together" in missing_estimate.stderr,
          "CLI did not require cpu estimate and basis together")
    explicit = parse_json(run_cli(explicit_state, *explicit_content))
    with contextlib.closing(warp_drive.connect(explicit_state)) as db:
        with db:
            warp_drive.attach_helper(db, explicit["request_id"], owner, str(uuid.uuid4()))
    explicit_pass = run_fixture_pass(explicit_state, 0.10, cpus=18)
    check(explicit_pass["granted"] == explicit["request_id"],
          "evidence-backed estimate did not fit the same synthetic host")
    batch_path = temp / "cpu-explicit-batch-worktree"
    legacy_batch_path = temp / "cpu-legacy-batch-worktree"
    batch_path.mkdir()
    legacy_batch_path.mkdir()
    batch_items = [{"key": "cpu-explicit-batch", "harness": owner["harness"],
        "session": owner["session"], "surface": owner["surface"], "size": "heavy",
        "family": "serial-gpu-inference", "workers": 1, "expected_runtime": 30,
        "worktree": str(batch_path), "command": ["/bin/true"],
        "cpu_cores": 0.75, "cpu_basis": "synthetic batch evidence"},
        {"key": "cpu-legacy-batch", "harness": owner["harness"],
         "session": owner["session"], "surface": owner["surface"], "size": "light",
         "family": "verify", "workers": 1, "expected_runtime": 30,
         "worktree": str(legacy_batch_path), "command": ["/bin/true"]}]
    batch_file = temp / "cpu-explicit-batch.json"
    batch_file.write_text(json.dumps(batch_items))
    batch_result = parse_json(run_cli(explicit_state, "batch", "--file", str(batch_file)))
    with contextlib.closing(warp_drive.connect(explicit_state)) as db:
        batch_rows = {row["request_key"]: row for row in db.execute(
            "SELECT request_key,requested_cpu_cores,cpu_basis FROM reservations WHERE request_key LIKE 'cpu-%-batch'")}
    check(len(batch_result["requests"]) == 2 and
          batch_rows["cpu-explicit-batch"]["requested_cpu_cores"] == 0.75 and
          batch_rows["cpu-explicit-batch"]["cpu_basis"] == "synthetic batch evidence" and
          batch_rows["cpu-legacy-batch"]["requested_cpu_cores"] is None,
          "batch did not persist optional per-item estimates alongside unchanged fallback rows")
    with contextlib.closing(warp_drive.connect(explicit_state)) as db:
        status = warp_drive.status_data(db)
        contributor = status["admission"]["host_constraints"]["active_managed_contributors"][0]
        event_row = db.execute("SELECT data_json FROM events WHERE reservation_id=? AND kind='granted'",
                               (explicit["request_id"],)).fetchone()
        grant = json.loads(event_row["data_json"])
        row = db.execute("SELECT helper_id,helper_generation FROM reservations WHERE id=?",
                         (explicit["request_id"],)).fetchone()
    check(contributor["estimated_cores"] == 0.5 and
          contributor["unreflected_cores"] == 0.5 and
          contributor["requested_cpu_cores"] == 0.5 and
          contributor["cpu_basis"] == "synthetic low-demand fixture" and
          grant["estimated_cores"] == 0.5 and grant["requested_cpu_cores"] == 0.5 and
          grant["cpu_basis"] == "synthetic low-demand fixture",
          "active, unreflected, or grant telemetry did not preserve the explicit estimate")
    waiting_path = temp / "cpu-explicit-diagnostic-worktree"
    waiting_path.mkdir()
    waiting = parse_json(run_cli(explicit_state, "submit", "--key", "cpu-explicit-waiting",
        *owner_flags, "--size", "heavy", "--family", "verify", "--workers", "1",
        "--cpu-cores", "0.25", "--cpu-basis", "synthetic candidate evidence",
        "--expected-runtime", "30", "--repo", str(waiting_path), "--worktree",
        str(waiting_path), "--", "/bin/true"))
    with contextlib.closing(warp_drive.connect(explicit_state)) as db:
        with db:
            warp_drive.attach_helper(db, waiting["request_id"], owner, str(uuid.uuid4()))
        candidate = warp_drive.admission_diagnostics(db)["candidates"][0]
    check(candidate["request_id"] == waiting["request_id"] and
          candidate["estimated_cores"] == 0.25 and
          candidate["requested_cpu_cores"] == 0.25 and
          candidate["cpu_basis"] == "synthetic candidate evidence",
          "candidate diagnostics did not use and expose the explicit CPU estimate")
    with contextlib.closing(warp_drive.connect(explicit_state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "policy_version", "warp-drive-admission-v6")
    resume = run_cli(explicit_state, "attach", explicit["request_id"], *owner_flags, ok=False)
    check("warp-drive-admission-v7" in resume.stderr,
          "an explicit request resumed under an incompatible old policy")
    with contextlib.closing(warp_drive.connect(explicit_state)) as db:
        after = db.execute("SELECT helper_id,helper_generation FROM reservations WHERE id=?",
                           (explicit["request_id"],)).fetchone()
    check(tuple(after) == tuple(row),
          "a rejected old-policy resume replaced or modified the attached helper")

    capped = policy.request_estimate({"size": "heavy", "workers": 1,
        "requested_cpu_cores": 100.0}, 8)
    light_reserve = policy.request_estimate({"size": "heavy", "workers": 1,
        "requested_cpu_cores": 100.0}, 8, light_ready=True)
    guarded = policy.admission_decision({"size": "heavy", "workers": 1,
        "requested_cpu_cores": 0.5}, target_cores=8, projected_cores=0,
        light_ready=False, pressure="pressure", sample_fresh=True, now=10,
        last_heavy_grant=None, heavy_wait_seconds=0, active_heavy_cores=0)
    reserved_admission = policy.admission_decision({"size": "heavy", "workers": 1,
        "requested_cpu_cores": 100.0}, target_cores=8, projected_cores=0,
        light_ready=True, pressure="normal", sample_fresh=True, now=10,
        last_heavy_grant=None, heavy_wait_seconds=0, active_heavy_cores=0)
    startup_guard = policy.admission_decision({"size": "heavy", "workers": 1,
        "requested_cpu_cores": 0.5}, target_cores=8, projected_cores=0,
        light_ready=False, pressure="normal", sample_fresh=True, now=10,
        last_heavy_grant=8, heavy_wait_seconds=0, active_heavy_cores=0)
    aged_cap_guard = policy.admission_decision({"size": "heavy", "workers": 1,
        "requested_cpu_cores": 100.0}, target_cores=8, projected_cores=0.5,
        light_ready=False, pressure="normal", sample_fresh=True, now=200,
        last_heavy_grant=None, heavy_wait_seconds=130, active_heavy_cores=20,
        active_managed_cores=20, active_aged_escape_count=policy.MAX_ACTIVE_AGED_ESCAPES)
    check(capped == 8 and light_reserve == 7 and not guarded[0] and
          guarded[1] == "memory pressure observed" and reserved_admission[0] and
          reserved_admission[2] == 7 and not startup_guard[0] and
          startup_guard[1] == "heavy startup observation interval" and
          startup_guard[2] == 0.5 and not aged_cap_guard[0] and
          aged_cap_guard[1] == "aged escape limit reached",
          "explicit estimate bypassed the target cap, light reserve, or pressure guard")


def test_missed_turn_ordering(temp):
    current_round = 8
    tie_rows = [
        {"id": "newer-earlier-position", "served_round": 6,
         "order_position": 1, "boosted": False, "credit": 7, "sequence": 1},
        {"id": "oldest-missed", "served_round": 3,
         "order_position": 4, "boosted": False, "credit": 7, "sequence": 2},
        {"id": "explicit-boost", "served_round": 7,
         "order_position": 99, "boosted": True, "credit": 0, "sequence": 3},
        {"id": "legacy-never-served", "served_round": 0,
         "order_position": 2, "boosted": False, "credit": 20, "sequence": 4},
        {"id": "new-join", "served_round": 7,
         "order_position": 3, "boosted": False, "credit": 20, "sequence": 5},
        {"id": "credit-breaks-tie", "served_round": 7,
         "order_position": 4, "boosted": False, "credit": 21, "sequence": 8},
        {"id": "sequence-breaks-tie", "served_round": 7,
         "order_position": 4, "boosted": False, "credit": 20, "sequence": 6},
        {"id": "id-breaks-tie-b", "served_round": 7,
         "order_position": 4, "boosted": False, "credit": 20, "sequence": 7},
        {"id": "id-breaks-tie-a", "served_round": 7,
         "order_position": 4, "boosted": False, "credit": 20, "sequence": 7},
    ]
    ordered_ids = [row["id"] for row in sorted(
        tie_rows, key=policy.request_order_key)]
    check(ordered_ids == ["explicit-boost", "legacy-never-served", "oldest-missed",
                          "newer-earlier-position", "new-join", "credit-breaks-tie",
                          "sequence-breaks-tie", "id-breaks-tie-a", "id-breaks-tie-b"],
          f"policy ordering lost boost, missed-round, manual-order or deterministic tie behavior: {ordered_ids}")
    check(policy.ordering_served_round(0) == 0,
          "a legacy never-served worktree lost its finite first-service debt")

    join_state = temp / "missed-turn-new-join-state"
    run_cli(join_state, "init")
    join_path = temp / "missed-turn-new-join-worktree"
    join_path.mkdir()
    with contextlib.closing(warp_drive.connect(join_state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "current_round", 4)
            joined_id = warp_drive.worktree_identity(db, join_path)
        joined = db.execute("SELECT served_round FROM worktrees WHERE id=?", (joined_id,)).fetchone()
        check(joined["served_round"] == 3,
              "a new worktree did not join immediately before the persisted current round")

    state = temp / "missed-turn-order-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    target_path = temp / "missed-turn-oldest-waiter"
    target_path.mkdir()
    peer_paths = [temp / f"missed-turn-newer-peer-{index}" for index in range(3)]
    for path in peer_paths:
        path.mkdir()
    target_id = submit_and_attach(state, owner, "missed-turn-oldest-waiter", target_path,
                                 "heavy", 4)
    peer_ids = [submit_and_attach(state, owner, f"missed-turn-peer-{index}", path,
                                  "heavy", 4)
                for index, path in enumerate(peer_paths)]
    peer_service_rounds = [4, 5, 7]
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "current_round", current_round)
            db.execute("UPDATE worktrees SET order_position=4,served_round=2 "
                       "WHERE id=(SELECT worktree_id FROM reservations WHERE id=?)", (target_id,))
            for index, (request_id, served_round) in enumerate(
                    zip(peer_ids, peer_service_rounds), start=1):
                db.execute("UPDATE worktrees SET order_position=?,served_round=? "
                           "WHERE id=(SELECT worktree_id FROM reservations WHERE id=?)",
                           (index, served_round, request_id))
            db.execute("UPDATE reservations SET credit_seconds=150,credit_started_at=NULL "
                       "WHERE id IN (" + ",".join("?" for _ in [target_id, *peer_ids]) + ")",
                       [target_id, *peer_ids])
        waiting = db.execute("""SELECT r.id,r.priority,r.credit_seconds,r.sequence,
            w.order_position,w.served_round FROM reservations r JOIN worktrees w ON w.id=r.worktree_id
            WHERE r.state='waiting' ORDER BY w.order_position,r.sequence""").fetchall()
        old_order = sorted(waiting, key=lambda row: (-int(bool(row["priority"])),
            row["order_position"], -row["credit_seconds"], row["sequence"], row["id"]))
        check([row["id"] for row in old_order[:3]] == peer_ids and
              old_order[3]["id"] == target_id and
              all(row["order_position"] < 4 and row["served_round"] > 2
                  for row in waiting if row["id"] in peer_ids),
              "fixture no longer shows earlier-position peers overtaking under the prior order")
        before = warp_drive.status_data(db)["admission"]["candidates"]
        check(before[0]["request_id"] == target_id and
              before[0]["ordering_served_round"] == 2,
              "diagnostics did not put the oldest missed service first")

    result = run_fixture_pass(state, 0.20, cpus=8)
    check(result["granted"] == target_id,
          "the oldest continuously eligible worktree did not receive equivalent available headroom")
    with contextlib.closing(warp_drive.connect(state)) as db:
        granted = db.execute("SELECT served_round FROM worktrees WHERE id=(SELECT worktree_id "
                             "FROM reservations WHERE id=?)", (target_id,)).fetchone()
        grant_event = db.execute("SELECT data_json FROM events WHERE reservation_id=? "
                                 "AND kind='granted' ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()
        check(granted["served_round"] == current_round and
              json.loads(grant_event["data_json"])["policy_version"] == policy.POLICY_VERSION,
              "grant did not preserve the current round or identify the revised measured policy")


def test_scheduler_startup_pacing_and_escape_limit(temp):
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}

    # A peer waiting only for the ordinary five-second heavy spacing still receives its round.
    state = temp / "startup-pacing-peer-state"
    run_cli(state, "init")
    target_path, first_path, second_path, active_one_path, active_two_path = [temp / name for name in
        ("pacing-target", "pacing-first", "pacing-second", "pacing-active-one", "pacing-active-two")]
    for path in (target_path, first_path, second_path, active_one_path, active_two_path):
        path.mkdir()
    target = submit_and_attach(state, owner, "pacing-large", target_path, "heavy", 10)
    first_peer = submit_and_attach(state, owner, "pacing-peer-first", first_path, "heavy", 1)
    second_peer = submit_and_attach(state, owner, "pacing-peer-second", second_path, "heavy", 1)
    active_one = submit_and_attach(state, owner, "pacing-active-one", active_one_path, "heavy", 1)
    active_two = submit_and_attach(state, owner, "pacing-active-two", active_two_path, "heavy", 1)
    clock = [time.time()]
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=300,credit_started_at=NULL,heartbeat_at=? WHERE id=?",
                       (clock[0], target))
            for identifier in (active_one, active_two):
                db.execute("""UPDATE reservations SET state='reserved',lease_active=1,
                    reservation_claim=?,granted_at=?,started_at=? WHERE id=?""",
                    (f"fixture-{identifier}", clock[0]-30, clock[0]-20, identifier))

    original_now = warp_drive.now
    warp_drive.now = lambda: clock[0]
    def pass_at(offset):
        clock[0] = clock[0] + offset
        fixture = {"at": clock[0], "logical_cpus": 18, "cpu_busy": 0.50,
                   "memory_pressure": "normal", "swap_used_mb": 0.0,
                   "thermal_state": None, "availability": {"cpu": True,
                   "memory_pressure": True, "swap": True, "thermal": False}}
        with contextlib.closing(warp_drive.connect(state)) as db:
            with warp_drive.immediate_transaction(db):
                db.execute("UPDATE reservations SET heartbeat_at=? WHERE state IN ('waiting','reserved')",
                           (clock[0],))
                warp_drive.host_sample(db, source="fixture", fixture=fixture)
                return warp_drive.scheduler_pass(db, sample=fixture)
    try:
        first = pass_at(0)
        second = pass_at(2)
        with contextlib.closing(warp_drive.connect(state)) as db:
            second_before_spacing = warp_drive.current_request(db, second_peer)
            stored_midturn = warp_drive.get_setting(db, "protected_turn_request_id")
        third = pass_at(4)
        with contextlib.closing(warp_drive.connect(state)) as db:
            second_after_spacing = warp_drive.current_request(db, second_peer)
            active_cores = sum(policy.estimated_cores(row["size"], row["workers"], 17.1)
                               for row in db.execute("SELECT * FROM reservations WHERE state='reserved' AND lease_active=1"))
        check(first["granted"] == first_peer and second["granted"] is None and
              second["protected_target_id"] is None and stored_midturn is None and
              second_before_spacing["wait_reason"] == "heavy startup observation interval",
              "an aged target protected its turn ahead of a peer blocked only by startup spacing")
        check(third["granted"] == second_peer and third["protected_target_id"] is None and
              second_after_spacing["state"] == "reserved" and active_cores >= 16,
              "a fitting unserved peer did not receive its normal turn after startup spacing")
    finally:
        warp_drive.now = original_now

    # Two aged escapes can overlap under the same observed-headroom guard, then the cap is isolated from fitting work.
    escape_state = temp / "aged-escape-limit-state"
    run_cli(escape_state, "init")
    paths = {name: temp / f"escape-cap-{name}" for name in
             ("first", "second", "third", "fit", "light")}
    for path in paths.values():
        path.mkdir()
    first_escape = submit_and_attach(escape_state, owner, "escape-first", paths["first"], "heavy", 10)
    second_escape = submit_and_attach(escape_state, owner, "escape-second", paths["second"], "heavy", 10)
    with contextlib.closing(warp_drive.connect(escape_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=180,credit_started_at=NULL")
    first_pass = run_fixture_pass(escape_state, 0.02, cpus=18)
    check(first_pass["granted"] == first_escape,
          "the first aged request did not use the existing observed-headroom escape")
    with contextlib.closing(warp_drive.connect(escape_state)) as db:
        with warp_drive.immediate_transaction(db):
            old = warp_drive.current_request(db, first_escape)
            old_claim = old["reservation_claim"]
            grant_at = time.time()-11
            db.execute("UPDATE reservations SET granted_at=?,started_at=? WHERE id=?",
                       (grant_at, grant_at, first_escape))
            warp_drive.set_setting(db, "last_heavy_grant", grant_at)
            warp_drive.attach_helper(db, first_escape, owner, str(uuid.uuid4()))
            reattached = warp_drive.current_request(db, first_escape)
            active = db.execute("SELECT * FROM reservations WHERE id=?", (first_escape,)).fetchone()
            check(reattached["reservation_claim"] != old_claim and
                  warp_drive.active_aged_escape_request_ids(db, [active]) == [first_escape],
                  "prelaunch reattachment erased the active escape classification")
    one_escape_status, _ = read_only_status_and_inspect(escape_state, second_escape)
    check(one_escape_status["admission"]["active_aged_escape_count"] == 1 and
          one_escape_status["admission"]["aged_escape_limit"] == 2 and
          one_escape_status["admission"]["active_aged_escape_request_ids"] == [first_escape],
          "a fresh CLI status read lost the active escape count after claim rotation")
    second_pass = run_fixture_pass(escape_state, 0.02, cpus=18)
    with contextlib.closing(warp_drive.connect(escape_state)) as db:
        second_row = warp_drive.current_request(db, second_escape)
        check(warp_drive.current_request(db, first_escape)["state"] == "reserved" and
              second_row["state"] == "reserved" and second_pass["granted"] == second_escape,
              f"the second aged escape did not pass the existing headroom guard: {second_row=}, {second_pass=}")

    third_escape = submit_and_attach(escape_state, owner, "escape-third", paths["third"], "heavy", 10)
    fitting_heavy = submit_and_attach(escape_state, owner, "escape-cap-fit", paths["fit"], "heavy", 1)
    fitting_light = submit_and_attach(escape_state, owner, "escape-cap-light", paths["light"], "light", 1)
    with contextlib.closing(warp_drive.connect(escape_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=180,credit_started_at=NULL WHERE id=?",
                       (third_escape,))
            grant_at = time.time()-11
            db.execute("UPDATE reservations SET granted_at=?,started_at=? WHERE id IN (?,?)",
                       (grant_at, grant_at, first_escape, second_escape))
            warp_drive.set_setting(db, "last_heavy_grant", grant_at)
    cap_heavy_pass = run_fixture_pass(escape_state, 0.02, cpus=18)
    cap_light_pass = run_fixture_pass(escape_state, 0.02, cpus=18)
    final_status, _ = read_only_status_and_inspect(escape_state, third_escape)
    third_candidate = next(item for item in final_status["admission"]["candidates"]
                           if item["request_id"] == third_escape)
    check(cap_heavy_pass["granted"] == fitting_heavy and
          cap_heavy_pass["protected_target_id"] is None and
          cap_light_pass["granted"] == fitting_light and
          cap_light_pass["protected_target_id"] is None,
          "the cap stopped fitting heavy or light work while two aged escapes remained active")
    check(final_status["admission"]["active_aged_escape_count"] == 2 and
          final_status["admission"]["aged_escape_limit"] == 2 and
          set(final_status["admission"]["active_aged_escape_request_ids"]) ==
              {first_escape, second_escape} and
          third_candidate["diagnostic_blocker"] == {
              "kind": "aged_escape_limit", "request_ids": sorted([first_escape, second_escape])} and
          third_candidate["policy_reason"] == "aged escape limit reached",
          "status omitted the cap, active IDs, or specific third-escape blocker")


def test_active_escape_preserves_fitting_progress(temp):
    state = temp / "active-escape-progress-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    paths = {name: temp / f"active-escape-{name}" for name in
             ("first", "second", "peer", "light-one", "light-two")}
    for path in paths.values():
        path.mkdir()
    first = submit_and_attach(state, owner, "active-escape-first", paths["first"], "heavy", 10)
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=300,credit_started_at=NULL WHERE id=?",
                       (first,))
    first_pass = run_fixture_pass(state, 0.02, cpus=18)
    check(first_pass["granted"] == first,
          "the first aged reservation did not receive its oversized escape")

    second = submit_and_attach(state, owner, "active-escape-second", paths["second"], "heavy", 10)
    peer = submit_and_attach(state, owner, "active-escape-peer", paths["peer"], "heavy", 1)
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            reflected_at = time.time()-11
            db.execute("UPDATE reservations SET granted_at=?,started_at=? WHERE id=?",
                       (reflected_at, reflected_at, first))
            warp_drive.set_setting(db, "last_heavy_grant", reflected_at)
            for offset in range(15):
                warp_drive.host_sample(db, source="fixture", fixture={
                    "at": time.time()-2*offset, "logical_cpus": 18, "cpu_busy": 0.20,
                    "memory_pressure": "normal"})
    peer_pass = run_fixture_pass(state, 0.20, cpus=18)
    check(peer_pass["granted"] == peer and peer_pass["protected_target_id"] is None,
          "a fitting ordinary heavy peer did not receive a turn during the active escape")
    status, inspect = read_only_status_and_inspect(state, second)
    admission = status["admission"]
    second_candidate = next(item for item in admission["candidates"]
                            if item["request_id"] == second)
    check(first in admission["active_aged_escape_request_ids"] and
          admission["active_aged_escape_count"] == 1 and
          admission["aged_escape_limit"] == 2 and
          second_candidate["diagnostic_blocker"] == {"kind": "host_capacity", "request_ids": []} and
          inspect["admission"]["request_context"]["eligible_now"],
          "read-only diagnostics omitted the active escape count or host-capacity blocker")

    light_one = submit_and_attach(state, owner, "active-escape-light-one", paths["light-one"],
                                  "light", 1)
    light_two = submit_and_attach(state, owner, "active-escape-light-two", paths["light-two"],
                                  "light", 1)
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            reflected_at = time.time()-11
            db.execute("UPDATE reservations SET granted_at=?,started_at=? WHERE id=?",
                       (reflected_at, reflected_at, peer))
            warp_drive.set_setting(db, "last_heavy_grant", reflected_at)
            for offset in range(15):
                warp_drive.host_sample(db, source="fixture", fixture={
                    "at": time.time()-2*offset, "logical_cpus": 18, "cpu_busy": 0.20,
                    "memory_pressure": "normal"})
    first_light_pass = run_fixture_pass(state, 0.20, cpus=18)
    check(first_light_pass["granted"] == light_one and
          first_light_pass["protected_target_id"] is None,
          "a second aged escape froze fitting light work while the first escape remained active")
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,"
                       "lease_active=0,ended_at=? WHERE id=?", (time.time(), light_one))
    second_light_pass = run_fixture_pass(state, 0.20, cpus=18)
    check(second_light_pass["granted"] == light_two and
          second_light_pass["protected_target_id"] is None,
          "light progress stopped after the first active-escape light phase finished")
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(warp_drive.current_request(db, second)["wait_reason"] ==
              "host target leaves insufficient heavy capacity",
              "an ordinary oversized request behind one active escape did not report host capacity")
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=300,credit_started_at=NULL WHERE id=?",
                       (second,))
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,"
                       "lease_active=0,ended_at=? WHERE id IN (?,?,?)",
                       (time.time(), first, peer, light_two))
    after_escape = run_fixture_pass(state, 0.20, cpus=18)
    check(after_escape["granted"] == second and after_escape["protected_target_id"] is None,
          "the second aged request did not receive its oversized turn after the first escape ended")


def submit_and_attach(state, owner, key, path, size, workers=1):
    result = parse_json(run_cli(state, "submit", "--key", key, *owner_args(owner),
        "--size", size, "--family", "verify", "--workers", workers,
        "--expected-runtime", "60", "--repo", path, "--worktree", path, "--", "/bin/true"))
    with contextlib.closing(warp_drive.connect(state)) as db:
        with db:
            warp_drive.attach_helper(db, result["request_id"], owner, str(uuid.uuid4()))
    return result["request_id"]


def prepare_reserved_command(state, owner, key, path, marker):
    code = "from pathlib import Path; Path(%r).open('a').write('ran\\n')" % str(marker)
    command = [sys.executable, "-c", code]
    result = parse_json(run_cli(state, "submit", "--key", key, *owner_args(owner),
        "--size", "light", "--family", "verify", "--workers", "1",
        "--expected-runtime", "60", "--repo", path, "--worktree", path,
        "--", *command))
    helper_id = str(uuid.uuid4())
    with contextlib.closing(warp_drive.connect(state)) as db:
        with db:
            _request_id, generation, _status = warp_drive.attach_helper(db, result["request_id"], owner, helper_id)
            db.execute("""UPDATE reservations SET state='reserved',reservation_claim=?,
                claim_helper_id=helper_id,claim_generation=helper_generation,
                granted_at=?,lease_active=1 WHERE id=?""",
                (str(uuid.uuid4()), time.time(), result["request_id"]))
    return result["request_id"], helper_id, generation


def test_claim_and_cancel_launch_interleavings(temp):
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    state = temp / "claim-race-state"
    run_cli(state, "init")
    worktree = temp / "claim-race-worktree"
    worktree.mkdir()
    marker = temp / "claim-race-executions"
    request_id, helper_a, generation_a = prepare_reserved_command(
        state, owner, "claim-race", worktree, marker)

    selected = threading.Event()
    release = threading.Event()
    claims = []
    errors = []
    original_current_request = warp_drive.current_request

    def pause_first_helper(db, request):
        row = original_current_request(db, request)
        if threading.current_thread().name == "claim-helper-a" and request == request_id:
            selected.set()
            if not release.wait(5):
                raise TimeoutError("controlled launch interleaving timed out")
        return row

    def execute_claim(name, helper_id, generation):
        try:
            with contextlib.closing(warp_drive.connect(state)) as db:
                claim, command, cwd = warp_drive.claim_launch(db, request_id, helper_id, generation)
            subprocess.run(command, cwd=cwd, check=True)
            claims.append((name, claim))
        except Exception as exc:
            errors.append((name, str(exc)))

    def attach_and_execute_b():
        helper_b = str(uuid.uuid4())
        try:
            with contextlib.closing(warp_drive.connect(state)) as db:
                with db:
                    _rid, generation, attach_state = warp_drive.attach_helper(db, request_id, owner, helper_b)
            if attach_state == "monitor-only":
                return
            execute_claim("b", helper_b, generation)
        except Exception as exc:
            errors.append(("b", str(exc)))

    warp_drive.current_request = pause_first_helper
    thread_a = threading.Thread(target=execute_claim, args=("a", helper_a, generation_a),
                                name="claim-helper-a")
    thread_b = threading.Thread(target=attach_and_execute_b, name="claim-helper-b")
    try:
        thread_a.start()
        check(selected.wait(5), "first helper did not reach the controlled claim read")
        thread_b.start()
        time.sleep(0.2)
        release.set()
        thread_a.join(10)
        thread_b.join(10)
    finally:
        release.set()
        warp_drive.current_request = original_current_request
    check(not thread_a.is_alive() and not thread_b.is_alive(), "claim race threads did not finish")
    check(not errors, f"claim interleaving raised unexpected errors: {errors}")
    lines = marker.read_text().splitlines() if marker.exists() else []
    check(len(lines) == 1, f"one reservation launched {len(lines)} times under helper replacement")

    # Race CLI cancellation against a helper that has read but not committed launch intent.
    cancel_state = temp / "cancel-race-state"
    run_cli(cancel_state, "init")
    cancel_path = temp / "cancel-race-worktree"
    cancel_path.mkdir()
    cancel_marker = temp / "cancel-race-executions"
    cancel_id, cancel_helper, cancel_generation = prepare_reserved_command(
        cancel_state, owner, "cancel-race", cancel_path, cancel_marker)
    selected.clear()
    release.clear()
    canceled_errors = []

    def launch_cancellable():
        try:
            with contextlib.closing(warp_drive.connect(cancel_state)) as db:
                claim, command, cwd = warp_drive.claim_launch(db, cancel_id, cancel_helper, cancel_generation)
            subprocess.run(command, cwd=cwd, check=True)
        except Exception as exc:
            canceled_errors.append(str(exc))

    def pause_cancelled_claim(db, request):
        row = original_current_request(db, request)
        if threading.current_thread().name == "cancel-launch-helper" and request == cancel_id:
            selected.set()
            if not release.wait(5):
                raise TimeoutError("controlled cancel interleaving timed out")
        return row

    warp_drive.current_request = pause_cancelled_claim
    launcher = threading.Thread(target=launch_cancellable, name="cancel-launch-helper")
    cancel_process = None
    try:
        launcher.start()
        check(selected.wait(5), "launch helper did not reach the controlled cancel read")
        cancel_command = [sys.executable, str(ROOT / "warp_drive.py"), "--state-dir", str(cancel_state),
                          "cancel", cancel_id]
        cancel_process = subprocess.Popen(cancel_command, text=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.2)
        release.set()
        launcher.join(10)
        cancel_out, cancel_err = cancel_process.communicate(timeout=10)
    finally:
        release.set()
        warp_drive.current_request = original_current_request
        if cancel_process is not None and cancel_process.poll() is None:
            cancel_process.kill()
            cancel_process.wait(timeout=5)
    check(not launcher.is_alive(), "cancel race helper did not finish")
    check(not canceled_errors, f"cancel race helper failed: {canceled_errors}")
    with contextlib.closing(warp_drive.connect(cancel_state)) as db:
        row = warp_drive.current_request(db, cancel_id)
    cancel_executions = cancel_marker.read_text().splitlines() if cancel_marker.exists() else []
    check(not (row["state"] == "cancelled" and cancel_executions),
          f"cancel committed before launch intent but child still ran: {cancel_out} {cancel_err}")
    check(len(cancel_executions) in (0, 1), "cancel/launch interleaving ran more than one child")

    # If cancellation wins before the launch claim starts, no child can execute.
    zero_state = temp / "cancel-wins-state"
    run_cli(zero_state, "init")
    zero_path = temp / "cancel-wins-worktree"
    zero_path.mkdir()
    zero_marker = temp / "cancel-wins-executions"
    zero_id, zero_helper, zero_generation = prepare_reserved_command(
        zero_state, owner, "cancel-wins", zero_path, zero_marker)
    run_cli(zero_state, "cancel", zero_id)
    with contextlib.closing(warp_drive.connect(zero_state)) as db:
        try:
            warp_drive.claim_launch(db, zero_id, zero_helper, zero_generation)
        except warp_drive.WarpError:
            pass
        else:
            raise AssertionError("launch claim succeeded after cancellation committed")
    check(not zero_marker.exists(), "cancellation that won before launch still ran a child")


def run_fixture_pass(state, busy, cpus=8, permit_grants=True):
    fixture = {"at": time.time(), "logical_cpus": cpus, "cpu_busy": busy,
               "memory_pressure": "normal", "swap_used_mb": 0.0,
               "thermal_state": None, "availability": {"cpu": True,
               "memory_pressure": True, "swap": True, "thermal": False}}
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.host_sample(db, source="fixture", fixture=fixture)
            result = warp_drive.scheduler_pass(db, sample=fixture,
                                               permit_grants=permit_grants)
        return result


def diagnostic_state_signature(state):
    with contextlib.closing(warp_drive.connect(state)) as db:
        return {
            "reservations": [tuple(row) for row in db.execute("""SELECT id,state,eligible_at,priority,
                granted_at,started_at,launch_intent_at,helper_generation,wait_reason
                FROM reservations ORDER BY id""")],
            "settings": [tuple(row) for row in db.execute("SELECT key,value FROM settings ORDER BY key")],
            "events": [tuple(row) for row in db.execute("SELECT id,at,reservation_id,kind,data_json FROM events ORDER BY id")],
            "worktrees": [tuple(row) for row in db.execute("SELECT id,order_position,served_round FROM worktrees ORDER BY id")],
            "host_samples": [tuple(row) for row in db.execute("SELECT at,logical_cpus,cpu_busy,memory_pressure,source FROM host_samples ORDER BY id")],
        }


def read_only_status_and_inspect(state, request_id):
    before = diagnostic_state_signature(state)
    status = parse_json(run_cli(state, "status"))
    inspect = parse_json(run_cli(state, "inspect", request_id))
    after = diagnostic_state_signature(state)
    check(before == after,
          "status or inspect changed a reservation grant, eligibility, priority, setting, sample or event")
    return status, inspect


def test_startup_observation_window(temp):
    state = temp / "startup-window-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    paths = [temp / f"startup-window-{index}" for index in range(3)]
    ids = []
    for index, path in enumerate(paths):
        path.mkdir()
        ids.append(submit_and_attach(state, owner, f"startup-window-{index}", path, "light"))

    def pass_at(at):
        sample = {"at": at, "logical_cpus": 8, "cpu_busy": 0.80,
                  "memory_pressure": "normal", "swap_used_mb": 0.0,
                  "thermal_state": None, "availability": {"cpu": True,
                  "memory_pressure": True, "swap": True, "thermal": False}}
        with contextlib.closing(warp_drive.connect(state)) as db:
            with warp_drive.immediate_transaction(db):
                warp_drive.host_sample(db, source="fixture", fixture=sample)
                return warp_drive.scheduler_pass(db, sample=sample)

    first_at = time.time()
    first = pass_at(first_at)
    check(first["granted"] == ids[0], "startup window fixture did not grant its first light request")
    second = pass_at(first_at + 2)
    check(second["granted"] is None,
          "a grant was treated as reflected by a sample taken before its startup observation window")
    third = pass_at(first_at + 11)
    check(third["granted"] == ids[1],
          "the startup observation window did not release the prior conservative grant estimate")


def test_incident_reopens_and_batch_owner_canonicalization(temp):
    state = temp / "incident-reopen-state"
    run_cli(state, "init")
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            incident_id = warp_drive.record_incident(db, "repeat-condition", {"pass": 1})
            warp_drive.resolve_incident(db, "repeat-condition")
            reopened_id = warp_drive.record_incident(db, "repeat-condition", {"pass": 2})
    check(reopened_id == incident_id, "resolved incident recurrence made a duplicate condition row")
    with contextlib.closing(warp_drive.connect(state)) as db:
        incident = db.execute("SELECT * FROM incidents WHERE condition_key='repeat-condition'").fetchone()
        events = [row["kind"] for row in db.execute("SELECT kind FROM events WHERE kind LIKE 'incident_%'")]
    check(incident["status"] == "open" and json.loads(incident["evidence_json"]) == {"pass": 2},
          "resolved incident did not reopen with the latest evidence")
    check("incident_reopened" in events, "incident recurrence was not recorded as an event")

    # A real CLI scheduler pass must survive resolve followed by the same overdue condition.
    overdue_state = temp / "overdue-recurrence-state"
    run_cli(overdue_state, "init")
    overdue_owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    overdue_path = temp / "overdue-recurrence-worktree"
    overdue_path.mkdir()
    overdue_id, overdue_helper, _generation = prepare_reserved_command(
        overdue_state, overdue_owner, "overdue-recurrence", overdue_path,
        temp / "overdue-unused-marker")
    with contextlib.closing(warp_drive.connect(overdue_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET expected_runtime=1,started_at=?,launch_intent_at=?,heartbeat_at=?,lease_active=1 WHERE id=?",
                       (time.time()-20, time.time()-21, time.time(), overdue_id))
    run_cli(overdue_state, "scheduler", "--once")
    condition = f"runtime-overdue:{overdue_id}"
    with contextlib.closing(warp_drive.connect(overdue_state)) as db:
        first_incident = db.execute("SELECT id FROM incidents WHERE condition_key=? AND status='open'",
                                    (condition,)).fetchone()[0]
    run_cli(overdue_state, "incident", condition, "--resolve")
    run_cli(overdue_state, "scheduler", "--once")
    with contextlib.closing(warp_drive.connect(overdue_state)) as db:
        reopened = db.execute("SELECT id,status FROM incidents WHERE condition_key=?",
                              (condition,)).fetchone()
    check(reopened["id"] == first_incident and reopened["status"] == "open",
          "scheduler failed to reopen a recurring overdue incident through the CLI")

    batch_state = temp / "batch-owner-state"
    run_cli(batch_state, "init")
    worktree = temp / "batch-owner-worktree"
    worktree.mkdir()
    compact_owner = {"harness": "codex", "session": uuid.uuid4().hex,
                     "surface": uuid.uuid4().hex}
    good = request_object("compact-owner", compact_owner, worktree, ["/bin/true"])
    batch_file = temp / "batch-owner.json"
    before = len(parse_json(run_cli(batch_state, "status"))["reservations"])
    bad = dict(good, key="invalid-owner", harness="not-a-harness")
    batch_file.write_text(json.dumps([good, bad]))
    run_cli(batch_state, "batch", "--file", batch_file, ok=False)
    after = len(parse_json(run_cli(batch_state, "status"))["reservations"])
    check(after == before, "invalid batch owner committed some reservations")
    batch_file.write_text(json.dumps([good]))
    created = parse_json(run_cli(batch_state, "batch", "--file", batch_file))["requests"][0]
    canonical_owner = {"harness": "codex", "session": str(uuid.UUID(compact_owner["session"])).upper(),
                       "surface": str(uuid.UUID(compact_owner["surface"])).upper()}
    attached = run_cli(batch_state, "attach", created["request_id"], *owner_args(canonical_owner),
                       "--timeout", "0.1", ok=False, timeout=5)
    check(attached.returncode == 4 and json.loads(attached.stdout)["state"] == "waiting",
          "compact UUID batch owner could not attach with its canonical CLI spelling")


def test_ready_demand_incident_tracks_meaningful_progress(temp):
    state = temp / "ready-demand-incident-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    shared_path, unrelated_path = temp / "ready-demand-shared", temp / "ready-demand-unrelated"
    shared_path.mkdir()
    unrelated_path.mkdir()
    first_id = submit_and_attach(state, owner, "ready-demand-first", shared_path, "light")
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "idle_unadmitted_since", time.time()-61)
    first_pass = run_fixture_pass(state, 0.05, permit_grants=False)
    check(first_pass["granted"] is None,
          "incident fixture unexpectedly granted its diagnostic-only held request")
    with contextlib.closing(warp_drive.connect(state)) as db:
        incident = db.execute("SELECT * FROM incidents WHERE condition_key='ready-demand-unadmitted'").fetchone()
        check(incident is not None and incident["status"] == "open" and
              first_id in json.loads(incident["evidence_json"])["ready_request_ids"],
              "a ready, policy-admissible request did not create an exception after the stall window")
        incident_id = incident["id"]
        first_opened_at = incident["opened_at"]

    added_id = submit_and_attach(state, owner, "ready-demand-added", shared_path, "light")
    run_fixture_pass(state, 0.05, permit_grants=False)
    with contextlib.closing(warp_drive.connect(state)) as db:
        incident = db.execute("SELECT * FROM incidents WHERE condition_key='ready-demand-unadmitted'").fetchone()
        events = [row["kind"] for row in db.execute(
            "SELECT kind FROM events WHERE kind LIKE 'incident_%' AND data_json LIKE ?",
            (f"%{incident_id}%",))]
    evidence = json.loads(incident["evidence_json"])
    check(incident["id"] == incident_id and incident["status"] == "open" and
          incident["opened_at"] == first_opened_at and
          set(evidence["ready_request_ids"]) == {first_id, added_id} and
          "incident_observation" in events and "incident_reopened" not in events,
          "changed blocked demand reopened instead of updating its continuing incident")

    unrelated_id = submit_and_attach(state, owner, "ready-demand-unrelated", unrelated_path,
                                     "light")
    actor = os.environ.get("USER") or getpass.getuser() or "operator"
    run_cli(state, "boost", unrelated_id, "--actor", actor,
            "--reason", "verify unrelated progress keeps the stalled requests grouped")
    with contextlib.closing(warp_drive.connect(state)) as db:
        before = warp_drive.get_setting(db, "idle_unadmitted_since")
    unrelated_grant = run_fixture_pass(state, 0.05)
    check(unrelated_grant["granted"] == unrelated_id,
          "boosted unrelated work did not provide the controlled unrelated-progress turn")
    with contextlib.closing(warp_drive.connect(state)) as db:
        incident = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        after = warp_drive.get_setting(db, "idle_unadmitted_since")
        check(incident["status"] == "open" and before == after,
              "unrelated grant resolved or reset the continuing ready-demand incident")

    first_progress = run_fixture_pass(state, 0.05)
    check(first_progress["granted"] == first_id,
          "first affected ready request did not make progress")
    with contextlib.closing(warp_drive.connect(state)) as db:
        incident = db.execute("SELECT status FROM incidents WHERE id=?", (incident_id,)).fetchone()
        check(incident["status"] == "open",
              "one affected request's progress erased a continuing incident for another blocked request")
    second_progress = run_fixture_pass(state, 0.05)
    check(second_progress["granted"] == added_id,
          "second affected ready request did not make progress")
    with contextlib.closing(warp_drive.connect(state)) as db:
        incident = db.execute("SELECT status FROM incidents WHERE id=?", (incident_id,)).fetchone()
        check(incident["status"] == "resolved",
              "ready-demand incident remained open after its affected requests made progress")


def test_ready_demand_incident_from_production_stall(temp):
    state = temp / "ready-demand-real-stall-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    worktree = temp / "ready-demand-real-stall-worktree"
    worktree.mkdir()
    request_id = submit_and_attach(state, owner, "ready-demand-real-stall", worktree, "heavy", 1)
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "last_heavy_grant", time.time()+120)
            warp_drive.set_setting(db, "idle_unadmitted_since", time.time()-61)
    stalled = run_fixture_pass(state, 0.05)
    check(stalled["granted"] is None,
          "future-dated heavy pacing did not produce the controlled scheduler stall")
    with contextlib.closing(warp_drive.connect(state)) as db:
        incident = db.execute("SELECT * FROM incidents WHERE condition_key='ready-demand-unadmitted'").fetchone()
        evidence = json.loads(incident["evidence_json"]) if incident else {}
        check(incident is not None and incident["status"] == "open" and
              request_id in evidence.get("ready_request_ids", []) and
              evidence.get("waiting_reasons", {}).get(request_id) ==
                  "heavy startup observation interval",
              "a real scheduler pass with idle headroom did not report its persistent startup stall")
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "last_heavy_grant", time.time()-10)
    recovered = run_fixture_pass(state, 0.05)
    check(recovered["granted"] == request_id,
          "the real-stall fixture did not admit its request when the pacing anomaly cleared")
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(db.execute("SELECT status FROM incidents WHERE condition_key='ready-demand-unadmitted'").fetchone()[0] ==
              "resolved", "real-stall incident did not resolve after its affected request progressed")


def test_transfer_credit_watcher_dedup_and_child_busy_retry(temp):
    state = temp / "transfer-credit-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    new_owner = {"harness": "claude", "session": uuid_text(), "surface": uuid_text()}
    transfer_path = temp / "transfer-credit-worktree"
    transfer_path.mkdir()
    transfer_id = submit_and_attach(state, owner, "transfer-credit", transfer_path, "light")
    run_fixture_pass(state, 0.05)
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            old_eligibility = time.time()-50
            db.execute("UPDATE reservations SET credit_seconds=7,eligible_at=?,credit_started_at=?,heartbeat_at=? WHERE id=?",
                       (old_eligibility, old_eligibility, time.time(), transfer_id))
    run_cli(state, "transfer", transfer_id, *owner_args(new_owner),
            "--from-session", owner["session"], "--reason", "verify retained credit")
    with contextlib.closing(warp_drive.connect(state)) as db:
        row = warp_drive.current_request(db, transfer_id)
        check(row["credit_seconds"] >= 55 and row["credit_started_at"] is None,
              "transfer requeue discarded earned pre-grant waiting credit")

    # Unattached batch rows and stale waiting owners are ordinary ineligibility, not duplicate incidents.
    watcher_state = temp / "watcher-state"
    run_cli(watcher_state, "init")
    watcher_path = temp / "watcher-worktree"
    watcher_path.mkdir()
    unattached_path = temp / "unattached-worktree"
    unattached_path.mkdir()
    watcher_id = submit_and_attach(watcher_state, owner, "stale-watcher", watcher_path, "light")
    unattached_id = parse_json(run_cli(watcher_state, "submit", "--key", "unattached-batch-member",
        *owner_args(owner), "--size", "light", "--family", "verify", "--workers", "1",
        "--expected-runtime", "30", "--repo", unattached_path, "--worktree", unattached_path,
        "--", sys.executable, "-c", "pass"))["request_id"]
    with contextlib.closing(warp_drive.connect(watcher_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET heartbeat_at=? WHERE id=?", (time.time()-60, watcher_id))
            db.execute("UPDATE reservations SET created_at=? WHERE id=?", (time.time()-60, unattached_id))
            warp_drive.monitor_reservations(db)
            warp_drive.monitor_reservations(db)
        incidents = db.execute("SELECT condition_key FROM incidents").fetchall()
    check(not incidents, "ordinary stale waiting helper created duplicate watcher incidents")

    # A known child continues through transient SQLite busy errors and records its exit exactly once.
    busy_state = temp / "busy-helper-state"
    run_cli(busy_state, "init")
    busy_path = temp / "busy-helper-worktree"
    busy_path.mkdir()
    started = temp / "busy-child-started"
    code = ("from pathlib import Path; import time; "
            f"Path({str(started)!r}).touch(); time.sleep(2.5)")
    busy_id = parse_json(run_cli(busy_state, "submit", "--key", "busy-child", *owner_args(owner),
        "--size", "light", "--family", "verify", "--workers", "1", "--expected-runtime", "30",
        "--repo", busy_path, "--worktree", busy_path, "--", sys.executable, "-c", code))["request_id"]
    with contextlib.closing(warp_drive.connect(busy_state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.attach_helper(db, busy_id, owner, str(uuid.uuid4()))
            sample = {"at": time.time(), "logical_cpus": 8, "cpu_busy": 0.05,
                      "memory_pressure": "normal", "swap_used_mb": 0.0,
                      "thermal_state": None, "availability": {"cpu": True,
                      "memory_pressure": True, "swap": True, "thermal": False}}
            warp_drive.host_sample(db, source="fixture", fixture=sample)
            check(warp_drive.scheduler_pass(db, sample=sample)["granted"] == busy_id,
                  "busy helper fixture was not granted")

    locker = warp_drive.connect(busy_state)
    original_sqlite_connect = warp_drive.sqlite3.connect
    def short_timeout_connect(path, timeout=10, *args, **kwargs):
        kwargs.pop("timeout", None)
        return original_sqlite_connect(path, timeout=0.05, *args, **kwargs)
    warp_drive.sqlite3.connect = short_timeout_connect
    results = []
    errors = []
    def run_known_child():
        try:
            results.append(warp_drive.run_helper(busy_state, busy_id, owner, timeout=10))
        except Exception as exc:
            errors.append(str(exc))
    helper_thread = threading.Thread(target=run_known_child, name="busy-helper-child")
    try:
        helper_thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not started.exists():
            if not helper_thread.is_alive():
                break
            time.sleep(0.02)
        check(started.exists(), "known child did not reach its running state")
        locker.execute("BEGIN IMMEDIATE")
        time.sleep(0.35)
        locker.commit()
        helper_thread.join(10)
    finally:
        if locker.in_transaction:
            locker.rollback()
        locker.close()
        warp_drive.sqlite3.connect = original_sqlite_connect
    check(not helper_thread.is_alive() and not errors,
          f"helper did not retry transient database busy while child ran: {errors}")
    check(results and results[0]["state"] == "finished" and results[0]["exit_code"] == 0,
          f"helper lost child completion after transient SQLite busy: {results}")

    # Foreground attach reports the product command's actual nonzero status.
    exit_state = temp / "exit-code-state"
    run_cli(exit_state, "init")
    exit_path = temp / "exit-code-worktree"
    exit_path.mkdir()
    failed_id = parse_json(run_cli(exit_state, "submit", "--key", "failed-child", *owner_args(owner),
        "--size", "light", "--family", "verify", "--workers", "1", "--expected-runtime", "30",
        "--repo", exit_path, "--worktree", exit_path, "--", sys.executable, "-c",
        "raise SystemExit(1)"))["request_id"]
    with contextlib.closing(warp_drive.connect(exit_state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.attach_helper(db, failed_id, owner, str(uuid.uuid4()))
    run_fixture_pass(exit_state, 0.05)
    failed = run_cli(exit_state, "attach", failed_id, *owner_args(owner),
                     ok=False, timeout=15)
    check(failed.returncode == 1 and json.loads(failed.stdout)["exit_code"] == 1,
          "foreground attach hid the product command's failing exit status")


def test_scheduler_fair_aged_worktree_and_idle_signal(temp):
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}

    # Fitting work gets its ordinary worktree turn before the old target is protected.
    state = temp / "aged-protection-state"
    run_cli(state, "init")
    aged_path, fitting_path, light_path, managed_path = [temp / name for name in
        ("aged-protection-target", "aged-protection-peer", "aged-protection-light",
         "aged-protection-managed")]
    for path in (aged_path, fitting_path, light_path, managed_path):
        path.mkdir()
    aged_id = submit_and_attach(state, owner, "aged-protection-large", aged_path, "heavy", 10)
    fitting_one = submit_and_attach(state, owner, "aged-protection-fitting-one",
                                    fitting_path, "heavy", 1)
    fitting_two = submit_and_attach(state, owner, "aged-protection-fitting-two",
                                    fitting_path, "heavy", 1)
    light_id = submit_and_attach(state, owner, "aged-protection-light", light_path, "light")
    managed_id = submit_and_attach(state, owner, "aged-protection-managed", managed_path,
                                   "heavy", 1)
    sample_at = time.time()
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=300,credit_started_at=NULL WHERE id=?",
                       (aged_id,))
            db.execute("UPDATE reservations SET state='reserved',lease_active=1,granted_at=?,started_at=?,reservation_claim='managed' WHERE id=?",
                       (sample_at-30, sample_at-20, managed_id))
    first = run_fixture_pass(state, 0.50, cpus=18)
    check(first["granted"] == fitting_one and first["protected_target_id"] is None,
          "aged work blocked a fitting worktree before its ordinary turn")
    second = run_fixture_pass(state, 0.50, cpus=18)
    check(second["granted"] == light_id,
          "light allowance stopped moving while another worktree received its turn")
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "last_heavy_grant", time.time()-10)
            db.execute("UPDATE reservations SET started_at=?,granted_at=? WHERE id IN (?,?)",
                       (time.time()-20, time.time()-30, fitting_one, light_id))
    third = run_fixture_pass(state, 0.50, cpus=18)
    check(third["granted"] is None and third["protected_target_id"] == aged_id,
          "aged worktree was not protected after fitting peers received their turns")
    late_fitting_path = temp / "aged-protection-late-fitting"
    late_fitting_path.mkdir()
    late_fitting = submit_and_attach(state, owner, "aged-protection-late-fitting",
                                     late_fitting_path, "heavy", 1)
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(warp_drive.get_setting(db, "current_round") == 1,
              "protected target lost its current turn to a capacity-skip round advance")
        check(warp_drive.current_request(db, fitting_two)["wait_reason"] ==
              f"heavy backfill held for aged request {aged_id}",
              "a fitting served heavy request did not identify the protected target")
        diagnostics = warp_drive.status_data(db)["admission"]
        target_candidate = next(item for item in diagnostics["candidates"]
                                if item["request_id"] == aged_id)
        fitting_candidate = next(item for item in diagnostics["candidates"]
                                 if item["request_id"] == fitting_two)
        late_candidate = next(item for item in diagnostics["candidates"]
                              if item["request_id"] == late_fitting)
        check(diagnostics["protected_target_id"] == aged_id and
              target_candidate["estimated_cores"] > fitting_candidate["estimated_cores"] and
              fitting_candidate["blocking_target_id"] == aged_id and
              "heavy backfill held" in fitting_candidate["blocking_reason"] and
              diagnostics["observed_cpu_cores"] >= 0 and
              diagnostics["projected_cores"] >= diagnostics["observed_cpu_cores"],
              "status omitted the measured demand, candidate size or protected blocking target")
        status, inspect = read_only_status_and_inspect(state, fitting_two)
        turns = status["admission"]["round_context"]
        fitting_turn = next(item for item in turns["worktrees"]
                            if item["worktree_id"] == fitting_candidate["worktree_id"])
        check(turns["current_round"] == 1 and
              turns["selection_round_if_scheduler_runs_now"] == 1 and
              fitting_turn["manual_order_position"] == fitting_candidate["manual_order_position"] and
              fitting_turn["served_round"] == fitting_candidate["served_round"] and
              fitting_candidate["diagnostic_blocker"] == {
                  "kind": "protected_backfill_target", "request_ids": [aged_id]} and
              late_candidate["diagnostic_blocker"] == {
                  "kind": "protected_backfill_target", "request_ids": [aged_id]} and
              not inspect["admission"]["request_context"]["eligible_this_selection_round"],
              "status used a stale wait reason instead of the live protected target or omitted turn context")
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,lease_active=0,ended_at=? WHERE id IN (?,?,?)",
                       (time.time(), managed_id, fitting_one, light_id))
            warp_drive.set_setting(db, "last_heavy_grant", time.time()-10)
    escape = run_fixture_pass(state, 0.20, cpus=18)
    check(escape["granted"] == aged_id,
          "protected aged heavy work did not receive its escape after active work finished")

    # An aged request that cannot use its escape must not idle the host while smaller work fits.
    unmanaged_state = temp / "aged-unmanaged-headroom-state"
    run_cli(unmanaged_state, "init")
    large_path, small_path = temp / "aged-unmanaged-large", temp / "aged-unmanaged-small"
    large_path.mkdir()
    small_path.mkdir()
    large_id = submit_and_attach(unmanaged_state, owner, "aged-unmanaged-large", large_path,
                                 "heavy", 10)
    small_id = submit_and_attach(unmanaged_state, owner, "aged-unmanaged-fitting", small_path,
                                 "heavy", 1)
    with contextlib.closing(warp_drive.connect(unmanaged_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=130,credit_started_at=NULL WHERE id=?",
                       (large_id,))
    busy_but_fitting = run_fixture_pass(unmanaged_state, 0.60, cpus=18)
    check(busy_but_fitting["granted"] == small_id and
          busy_but_fitting["protected_target_id"] is None,
          "aged work with insufficient observed escape headroom idled a fitting smaller request")
    status, inspect = read_only_status_and_inspect(unmanaged_state, large_id)
    large_candidate = next(item for item in status["admission"]["candidates"]
                           if item["request_id"] == large_id)
    check(large_candidate["diagnostic_blocker"] == {"kind": "host_capacity", "request_ids": []} and
          status["admission"]["active_aged_escape_request_ids"] == [] and
          status["admission"]["host_constraints"]["active_managed_contributors"][0]["request_id"] == small_id and
          status["admission"]["host_constraints"]["attribution_note"] and
          inspect["admission"]["request_context"]["eligible_now"],
          "capacity diagnostics invented a causal job or omitted unmanaged host load context")
    with contextlib.closing(warp_drive.connect(unmanaged_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE host_samples SET memory_pressure='warning'")
    pressured, pressured_inspect = read_only_status_and_inspect(unmanaged_state, large_id)
    pressured_candidate = next(item for item in pressured["admission"]["candidates"]
                               if item["request_id"] == large_id)
    check(pressured_candidate["diagnostic_blocker"] == {"kind": "memory_pressure", "request_ids": []} and
          pressured["admission"]["host_constraints"]["memory_pressure_restricts_admission"] and
          pressured_inspect["admission"]["request_context"]["eligible_now"],
          "memory-pressure diagnostics did not separate the host constraint from causal jobs")
    with contextlib.closing(warp_drive.connect(unmanaged_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE host_samples SET at=?", (time.time()-10,))
    stale_sample, stale_inspect = read_only_status_and_inspect(unmanaged_state, large_id)
    stale_candidate = next(item for item in stale_sample["admission"]["candidates"]
                           if item["request_id"] == large_id)
    check(stale_candidate["diagnostic_blocker"] == {"kind": "host_sample", "request_ids": []} and
          stale_sample["admission"]["host_constraints"]["sample_state"] == "stale" and
          stale_inspect["admission"]["request_context"]["eligible_now"],
          "stale-sample diagnostics did not identify the host constraint without assigning a job")

    # A reflected heavy command with low measured CPU does not veto the existing aged escape.
    reflected_state = temp / "aged-reflected-active-heavy-state"
    run_cli(reflected_state, "init")
    target_path, active_path = temp / "aged-reflected-target", temp / "aged-reflected-active"
    target_path.mkdir()
    active_path.mkdir()
    reflected_target = submit_and_attach(reflected_state, owner, "aged-reflected-target",
                                        target_path, "heavy", 10)
    reflected_active = submit_and_attach(reflected_state, owner, "aged-reflected-active",
                                         active_path, "heavy", 1)
    sample_at = time.time()
    with contextlib.closing(warp_drive.connect(reflected_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=130,credit_started_at=NULL WHERE id=?",
                       (reflected_target,))
            db.execute("UPDATE reservations SET state='reserved',lease_active=1,granted_at=?,started_at=?,reservation_claim='reflected-active' WHERE id=?",
                       (sample_at-30, sample_at-20, reflected_active))
    low_load_escape = run_fixture_pass(reflected_state, 0.26, cpus=18)
    check(low_load_escape["granted"] == reflected_target,
          "a reflected active heavy command vetoed the aged escape despite low observed CPU")
    with contextlib.closing(warp_drive.connect(reflected_state)) as db:
        check(warp_drive.current_request(db, reflected_active)["state"] == "reserved",
              "aged observed-headroom escape disturbed an already-running heavy command")

    # Active service in the target worktree is not a reason to protect its next request.
    same_tree_state = temp / "aged-same-worktree-active-state"
    run_cli(same_tree_state, "init")
    same_path, other_path = temp / "aged-same-worktree", temp / "aged-same-worktree-peer"
    same_path.mkdir()
    other_path.mkdir()
    same_active = submit_and_attach(same_tree_state, owner, "aged-same-active", same_path,
                                    "heavy", 1)
    same_target = submit_and_attach(same_tree_state, owner, "aged-same-target", same_path,
                                    "heavy", 10)
    same_peer = submit_and_attach(same_tree_state, owner, "aged-same-peer", other_path,
                                  "heavy", 1)
    sample_at = time.time()
    with contextlib.closing(warp_drive.connect(same_tree_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=130,credit_started_at=NULL WHERE id=?",
                       (same_target,))
            db.execute("UPDATE reservations SET state='reserved',lease_active=1,granted_at=?,started_at=?,reservation_claim='same-tree-active' WHERE id=?",
                       (sample_at-30, sample_at-20, same_active))
    same_tree_result = run_fixture_pass(same_tree_state, 0.60, cpus=18)
    check(same_tree_result["granted"] == same_peer and
          same_tree_result["protected_target_id"] is None,
          "queued work behind an active same-worktree request created an aged protection pause")

    # One active light command may finish before an aged oversized turn, without light backfill refilling its slot.
    light_drain_state = temp / "aged-light-backfill-state"
    run_cli(light_drain_state, "init")
    target_path, active_light_path, queued_light_path, late_light_path = [temp / name for name in
        ("aged-light-target", "aged-light-active", "aged-light-queued", "aged-light-late")]
    for path in (target_path, active_light_path, queued_light_path, late_light_path):
        path.mkdir()
    light_target = submit_and_attach(light_drain_state, owner, "aged-light-target", target_path,
                                     "heavy", 10)
    active_light = submit_and_attach(light_drain_state, owner, "aged-light-active",
                                     active_light_path, "light", 8)
    queued_light = submit_and_attach(light_drain_state, owner, "aged-light-queued",
                                     queued_light_path, "light")
    sample_at = time.time()
    with contextlib.closing(warp_drive.connect(light_drain_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=130,credit_started_at=NULL WHERE id=?",
                       (light_target,))
            db.execute("UPDATE worktrees SET served_round=1 WHERE id=(SELECT worktree_id FROM reservations WHERE id=?)",
                       (queued_light,))
            db.execute("UPDATE reservations SET state='reserved',lease_active=1,granted_at=?,started_at=?,reservation_claim='active-light' WHERE id=?",
                       (sample_at-30, sample_at-20, active_light))
    light_protected = run_fixture_pass(light_drain_state, 0.50, cpus=18)
    check(light_protected["granted"] is None and
          light_protected["protected_target_id"] == light_target,
          "aged heavy work did not protect its escape from repeated light backfill")
    with contextlib.closing(warp_drive.connect(light_drain_state)) as db:
        check(warp_drive.current_request(db, queued_light)["wait_reason"] ==
              f"light backfill held for aged request {light_target} while light work is active",
              "light backfill was not bounded while an active light job could finish")
    status, inspect = read_only_status_and_inspect(light_drain_state, queued_light)
    queued_light_candidate = next(item for item in status["admission"]["candidates"]
                                  if item["request_id"] == queued_light)
    check(queued_light_candidate["diagnostic_blocker"] == {
              "kind": "protected_backfill_target", "request_ids": [light_target]} and
          inspect["admission"]["request_context"]["eligible_now"] and
          status["admission"]["host_constraints"]["active_managed_contributors"][0]["request_id"] == active_light,
          "status did not report the live protected target and active-light backfill guard")
    late_light = submit_and_attach(light_drain_state, owner, "aged-light-late",
                                   late_light_path, "light")
    continued = run_fixture_pass(light_drain_state, 0.60, cpus=18)
    check(continued["granted"] is None and continued["protected_target_id"] == light_target,
          "a replenished light queue erased the protected aged turn while its light job remained active")
    with contextlib.closing(warp_drive.connect(light_drain_state)) as db:
        check(warp_drive.current_request(db, late_light)["wait_reason"] ==
              f"light backfill held for aged request {light_target} while light work is active",
              "new light backfill bypassed the protected turn during active light work")
    with contextlib.closing(warp_drive.connect(light_drain_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,lease_active=0,ended_at=? WHERE id=?",
                       (time.time(), active_light))
    light_release = run_fixture_pass(light_drain_state, 0.15, cpus=18)
    check(light_release["granted"] == light_target,
          "a finished light job did not yield its measured headroom to the aged oversized request")

    # Protected-target choice keeps its earned-credit ordering ahead of generic service-round order.
    multiple_state = temp / "multiple-aged-targets-state"
    run_cli(multiple_state, "init")
    old_path, next_path, active_path = [temp / name for name in
        ("multiple-aged-old", "multiple-aged-next", "multiple-aged-active")]
    for path in (old_path, next_path, active_path):
        path.mkdir()
    old_id = submit_and_attach(multiple_state, owner, "multiple-aged-old", old_path,
                               "heavy", 10)
    next_id = submit_and_attach(multiple_state, owner, "multiple-aged-next", next_path,
                                "heavy", 10)
    active_id = submit_and_attach(multiple_state, owner, "multiple-aged-active", active_path,
                                  "heavy", 1)
    sample_at = time.time()
    with contextlib.closing(warp_drive.connect(multiple_state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "current_round", 10)
            db.execute("UPDATE worktrees SET served_round=5 WHERE id=(SELECT worktree_id FROM reservations WHERE id=?)",
                       (old_id,))
            db.execute("UPDATE worktrees SET served_round=3 WHERE id=(SELECT worktree_id FROM reservations WHERE id=?)",
                       (next_id,))
            db.execute("UPDATE reservations SET credit_seconds=300,credit_started_at=NULL WHERE id=?",
                       (old_id,))
            db.execute("UPDATE reservations SET credit_seconds=180,credit_started_at=NULL WHERE id=?",
                       (next_id,))
            db.execute("UPDATE reservations SET state='reserved',lease_active=1,granted_at=?,started_at=?,reservation_claim='multiple-aged-active' WHERE id=?",
                       (sample_at-30, sample_at-20, active_id))
    blocked = run_fixture_pass(multiple_state, 0.50, cpus=18)
    check(blocked["granted"] is None and blocked["protected_target_id"] == old_id,
          "protected-target selection stopped honoring earned credit when service rounds differ")
    with contextlib.closing(warp_drive.connect(multiple_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,lease_active=0,ended_at=? WHERE id=?",
                       (time.time(), active_id))
            warp_drive.set_setting(db, "last_heavy_grant", time.time()-10)
    first_escape = run_fixture_pass(multiple_state, 0.05, cpus=18)
    check(first_escape["granted"] == old_id,
          "oldest aged target did not escape ahead of a second oversized waiter")
    with contextlib.closing(warp_drive.connect(multiple_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,lease_active=0,ended_at=? WHERE id=?",
                       (time.time(), old_id))
            warp_drive.set_setting(db, "last_heavy_grant", time.time()-10)
    second_escape = run_fixture_pass(multiple_state, 0.05, cpus=18)
    check(second_escape["granted"] == next_id,
          "second aged target remained blocked after the first oversized turn completed")

    # Held, cancelled and stale targets release only their computed protection.
    for mode in ("stale", "hold", "cancel"):
        release_state = temp / f"aged-target-release-{mode}"
        run_cli(release_state, "init")
        target_path, peer_path, active_path = [temp / f"aged-release-{mode}-{name}"
                                               for name in ("target", "peer", "active")]
        for path in (target_path, peer_path, active_path):
            path.mkdir()
        target_id = submit_and_attach(release_state, owner, f"aged-release-{mode}-target",
                                      target_path, "heavy", 10)
        peer_id = submit_and_attach(release_state, owner, f"aged-release-{mode}-peer",
                                    peer_path, "heavy", 1)
        active_id = submit_and_attach(release_state, owner, f"aged-release-{mode}-active",
                                      active_path, "heavy", 1)
        release_at = time.time()
        with contextlib.closing(warp_drive.connect(release_state)) as db:
            with warp_drive.immediate_transaction(db):
                db.execute("UPDATE reservations SET credit_seconds=130,credit_started_at=NULL WHERE id=?",
                           (target_id,))
                db.execute("UPDATE worktrees SET served_round=1 WHERE id=(SELECT worktree_id FROM reservations WHERE id=?)",
                           (peer_id,))
                db.execute("UPDATE reservations SET state='reserved',lease_active=1,granted_at=?,started_at=?,reservation_claim='release-active' WHERE id=?",
                           (release_at-30, release_at-20, active_id))
        protected = run_fixture_pass(release_state, 0.50, cpus=18)
        check(protected["protected_target_id"] == target_id,
              f"aged {mode} target did not establish protection before release")
        if mode == "cancel":
            run_cli(release_state, "cancel", target_id)
        elif mode == "hold":
            run_cli(release_state, "delay", target_id, "--hold")
        else:
            with contextlib.closing(warp_drive.connect(release_state)) as db:
                with warp_drive.immediate_transaction(db):
                    db.execute("UPDATE reservations SET heartbeat_at=? WHERE id=?",
                               (time.time()-60, target_id))
        resumed = run_fixture_pass(release_state, 0.05, cpus=18)
        check(resumed["granted"] == peer_id and resumed["protected_target_id"] is None,
              f"aged {mode} target kept protecting the queue after becoming ineligible")
        status, inspect = read_only_status_and_inspect(release_state, target_id)
        context = inspect["admission"]["request_context"]
        check(status["admission"]["protected_target_id"] is None and
              not context["eligible_now"] and context["not_eligible_reason"] and
              all(target_id not in (item["diagnostic_blocker"] or {}).get("request_ids", [])
                  for item in status["admission"]["candidates"]),
              f"{mode} inspect retained a stale blocker ID or claimed current eligibility")

    # Preserve the existing capacity-skip round advance for unrelated unfit work.
    round_state = temp / "round-skip-state"
    run_cli(round_state, "init")
    heavy_path, light_path = temp / "round-skip-heavy", temp / "round-skip-light"
    heavy_path.mkdir()
    light_path.mkdir()
    heavy_id = submit_and_attach(round_state, owner, "round-skip-heavy", heavy_path,
                                 "heavy", 1)
    light_one = submit_and_attach(round_state, owner, "round-skip-light-one", light_path,
                                  "light", 1)
    light_two = submit_and_attach(round_state, owner, "round-skip-light-two", light_path,
                                  "light", 1)
    first_round = run_fixture_pass(round_state, 0.80, cpus=8)
    check(first_round["granted"] == light_one,
          "first responsive light worktree turn was blocked by unfit heavy demand")
    with contextlib.closing(warp_drive.connect(round_state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,lease_active=0,ended_at=? WHERE id=?",
                       (time.time(), light_one))
            db.execute("UPDATE host_samples SET at=? WHERE source='fixture'", (time.time()-31,))
    next_round = run_fixture_pass(round_state, 0.50, cpus=8)
    check(next_round["granted"] == light_two,
          "capacity-blocked heavy work prevented the existing round skip from serving ready light work")
    with contextlib.closing(warp_drive.connect(round_state)) as db:
        check(warp_drive.get_setting(db, "current_round") == 2,
              "capacity skip stopped advancing the existing worktree round")
        check(db.execute("SELECT 1 FROM events WHERE kind='round_advanced_after_capacity_skip'").fetchone(),
              "capacity skip did not retain its existing decision event")
        check(warp_drive.current_request(db, heavy_id)["state"] == "waiting",
              "unfit heavy work changed state during the light round skip")
    status, inspect = read_only_status_and_inspect(round_state, heavy_id)
    round_context = status["admission"]["round_context"]
    heavy_candidate = next(item for item in status["admission"]["candidates"]
                           if item["request_id"] == heavy_id)
    check(round_context["current_round"] == 2 and
          heavy_candidate["served_round"] == 0 and
          heavy_candidate["manual_order_position"] == 0 and
          inspect["admission"]["request_context"]["eligible_this_selection_round"],
          "diagnostics omitted current-round eligibility or manual worktree order")
    served_peer = submit_and_attach(round_state, owner, "round-skip-light-three", light_path,
                                    "light", 1)
    status, inspect = read_only_status_and_inspect(round_state, served_peer)
    served_candidate = next(item for item in status["admission"]["candidates"]
                            if item["request_id"] == served_peer)
    check(served_candidate["blocking_reason"] == "worktree already received its turn this round" and
          served_candidate["diagnostic_blocker"] == {"kind": "worktree_round", "request_ids": []} and
          not inspect["admission"]["request_context"]["eligible_this_selection_round"],
          "diagnostic blocker reported an underlying CPU result instead of the current worktree-round gate")

    # Full CPU load remains ordinary saturation, not an idle-with-headroom incident.
    busy_state = temp / "busy-ready-state"
    run_cli(busy_state, "init")
    busy_path = temp / "busy-ready-worktree"
    busy_path.mkdir()
    busy_id = submit_and_attach(busy_state, owner, "busy-ready-light", busy_path, "light")
    with contextlib.closing(warp_drive.connect(busy_state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.set_setting(db, "idle_unadmitted_since", time.time()-61)
    busy_result = run_fixture_pass(busy_state, 1.0, cpus=8)
    check(busy_result["granted"] is None, "fully busy CPU unexpectedly admitted work")
    with contextlib.closing(warp_drive.connect(busy_state)) as db:
        check(db.execute("SELECT 1 FROM incidents WHERE condition_key='ready-demand-unadmitted' AND status='open'").fetchone() is None,
              f"fully busy host generated an idle incident for {busy_id}")


def test_protected_target_survives_mean_settling(temp):
    state = temp / "protected-mean-settling-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    target_path, active_path, peer_path, first_light_path, refill_light_path = [temp / name for name in
        ("settling-target", "settling-active-heavy", "settling-heavy-peer",
         "settling-first-light", "settling-refill-light")]
    for path in (target_path, active_path, peer_path, first_light_path, refill_light_path):
        path.mkdir()
    target = submit_and_attach(state, owner, "settling-large", target_path, "heavy", 10)
    active = submit_and_attach(state, owner, "settling-active", active_path, "heavy", 3)
    peer = submit_and_attach(state, owner, "settling-peer", peer_path, "heavy", 1)
    first_light = submit_and_attach(state, owner, "settling-light-first", first_light_path, "light", 1)
    refill_light = submit_and_attach(state, owner, "settling-light-refill", refill_light_path, "light", 1)
    sample_at = time.time()
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET credit_seconds=300,credit_started_at=NULL WHERE id=?",
                       (target,))
            for identifier in (peer, first_light, refill_light):
                db.execute("UPDATE worktrees SET served_round=1 WHERE id=(SELECT worktree_id FROM reservations WHERE id=?)",
                           (identifier,))
            db.execute("""UPDATE reservations SET state='reserved',lease_active=1,
                reservation_claim='settling-active',granted_at=?,started_at=? WHERE id=?""",
                (sample_at-30, sample_at-20, active))
            warp_drive.set_setting(db, "last_heavy_grant", sample_at-30)
            for offset in range(15):
                warp_drive.host_sample(db, source="fixture", fixture={
                    "at": sample_at - 2*offset, "logical_cpus": 18, "cpu_busy": 0.75,
                    "memory_pressure": "normal"})
    protected = run_fixture_pass(state, 0.75, cpus=18)
    with contextlib.closing(warp_drive.connect(state)) as db:
        protected_id = warp_drive.get_setting(db, "protected_turn_request_id")
    check(protected["protected_target_id"] == target and protected_id == target,
          "aged target did not retain one explicit protected-turn ID during managed drain")
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(warp_drive.current_request(db, peer)["wait_reason"] ==
              f"heavy backfill held for aged request {target}",
              "heavy backfill did not identify the selected live protected target")
        check(warp_drive.current_request(db, first_light)["state"] == "reserved",
              "bounded light work did not progress during the protected drain")
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE reservations SET state='finished',outcome='succeeded',exit_code=0,lease_active=0,ended_at=? WHERE id IN (?,?)",
                       (time.time(), active, first_light))

    # The latest sample is below half budget, but old high samples keep the 30-second mean elevated.
    settling = run_fixture_pass(state, 8/18, cpus=18)
    check(settling["granted"] is None and settling["protected_target_id"] == target and
          settling["cpu_30s_mean"] * 18 > 8.05,
          "the protected turn was lost during the finite post-completion CPU-mean window")
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(warp_drive.current_request(db, refill_light)["wait_reason"] ==
              f"light backfill held for aged request {target} while the observed CPU window settles",
              "a replacement light request refilled the slot while the existing CPU mean settled")
    still_settling = run_fixture_pass(state, 8/18, cpus=18)
    check(still_settling["granted"] is None and still_settling["protected_target_id"] == target,
          "light backfill repeatedly refilled the protected turn before the CPU window settled")
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("UPDATE host_samples SET at=at-31 WHERE source='fixture' AND cpu_busy>=0.75")
    after_window = run_fixture_pass(state, 8/18, cpus=18)
    check(after_window["granted"] == target,
          "aged target did not escape once the existing observation mean settled")
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(warp_drive.get_setting(db, "protected_turn_request_id") is None and
              warp_drive.current_request(db, refill_light)["state"] == "waiting",
              "completed target left protection stale or refilled light during the settle pass")


def request_object(key, owner, path, command):
    return {"key": key, **owner, "size": "light", "family": "verify",
            "workers": 1, "expected_runtime": 30, "worktree": str(path),
            "repo": str(path), "command": command}


def test_new_recipient_enrollment_guard(temp):
    state = temp / "recipient-guard-state"
    hailing_dir = temp / "recipient-guard-hailing"
    hailing_dir.mkdir()
    config_path = hailing_dir / "config.json"
    env = {"WARP_DRIVE_HAILING_STATE": str(hailing_dir)}
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    parent = {"harness": "claude", "session": uuid_text(), "surface": uuid_text()}
    worktree = temp / "recipient-guard-worktree"
    worktree.mkdir()
    marker = temp / "recipient-guard-command-ran"

    def config_for(owners):
        targets = [{"label": "verify-recipient", **item, "workspace": uuid_text(),
                    "started_at": 1.0, "cutoff": 0.0} for item in owners]
        config_path.write_text(json.dumps({"version": 1, "targets": targets,
            "remind_after": 60, "escalate_after": 120, "cooldown": 30, "interval": 10}))

    def args_for(recipient, key, command=None, command_name="submit"):
        return [command_name, "--key", key, "--harness", recipient["harness"],
                "--session", recipient["session"].lower(), "--surface", recipient["surface"].lower(),
                "--size", "light", "--family", "verify-recipient", "--workers", "1",
                "--expected-runtime", "30", "--repo", str(worktree), "--worktree", str(worktree),
                "--", *(command or ["/bin/true"])]

    run_cli(state, "init", env=env)
    missing = run_cli(state, *args_for(owner, "recipient-missing"), env=env, ok=False)
    check(missing.returncode == 2 and "config is missing" in missing.stderr and
          "hailing-frequencies guide --print" in missing.stderr,
          "missing Hailing enrollment did not fail with actionable owner guidance")
    config_path.write_text("{")
    malformed = run_cli(state, *args_for(owner, "recipient-malformed"), env=env, ok=False)
    check(malformed.returncode == 2 and "malformed JSON" in malformed.stderr,
          "malformed Hailing enrollment did not fail with a clear diagnostic")
    config_path.unlink()
    config_path.mkdir()
    unreadable = run_cli(state, *args_for(owner, "recipient-unreadable"), env=env, ok=False)
    check(unreadable.returncode == 2 and "could not be read" in unreadable.stderr,
          "unreadable Hailing enrollment did not fail with a clear diagnostic")
    config_path.rmdir()

    config_for([owner, parent])
    typo = dict(owner, session=uuid_text())
    typo_run_args = args_for(typo, "recipient-typo-run", [sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()"], command_name="run")
    delimiter = typo_run_args.index("--")
    typo_run_args[delimiter:delimiter] = ["--attach-timeout", "0.1"]
    typo_run = run_cli(state, *typo_run_args, env=env, ok=False)
    check(typo_run.returncode == 2 and "No exact Hailing enrollment" in typo_run.stderr and
          "Correct the tuple" in typo_run.stderr and
          "hailing-frequencies enroll" not in typo_run.stderr and not marker.exists(),
          "run accepted an unmatched recipient or launched its product command")
    with contextlib.closing(warp_drive.connect(state)) as db:
        check(db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0 and
              db.execute("SELECT count(*) FROM worktrees").fetchone()[0] == 0,
              "rejected recipient left a reservation or worktree event behind")

    submitted = parse_json(run_cli(state, *args_for(owner, "recipient-valid"), env=env))
    check(submitted["created"] is True, "an enrolled recipient with a stale cutoff was rejected")
    retry_config = {"version": 1, "targets": [], "remind_after": 60,
                    "escalate_after": 120, "cooldown": 30, "interval": 10}
    config_path.write_text(json.dumps(retry_config))
    retry = parse_json(run_cli(state, *args_for(owner, "recipient-valid"), env=env))
    check(retry["created"] is False and retry["request_id"] == submitted["request_id"],
          "identical stable-key retry failed after its Hailing enrollment was lost")
    changed = run_cli(state, *args_for(owner, "recipient-valid", ["/bin/echo", "changed"]),
                      env=env, ok=False)
    changed_owner = run_cli(state, *args_for(parent, "recipient-valid"), env=env, ok=False)
    check(changed.returncode == changed_owner.returncode == 2 and
          "already names different work" in changed.stderr and
          "another owner" in changed_owner.stderr,
          "lost enrollment weakened stable-key content or ownership checks")

    batch_worktree = temp / "recipient-guard-batch-worktree"
    batch_worktree.mkdir()
    batch_state = temp / "recipient-guard-batch-state"
    run_cli(batch_state, "init", env=env)
    batch_file = temp / "recipient-guard-batch.json"
    accepted = request_object("recipient-batch-good", owner, batch_worktree, ["/bin/true"])
    rejected = request_object("recipient-batch-typo", dict(parent, surface=uuid_text()),
                              batch_worktree, ["/bin/true"])
    batch_file.write_text(json.dumps([accepted, rejected]))
    config_for([owner])
    batch_result = run_cli(batch_state, "batch", "--file", batch_file, env=env, ok=False)
    check(batch_result.returncode == 2 and "No exact Hailing enrollment" in batch_result.stderr,
          "batch accepted a member whose completion owner was not enrolled")
    with contextlib.closing(warp_drive.connect(batch_state)) as db:
        check(db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0 and
              db.execute("SELECT count(*) FROM worktrees").fetchone()[0] == 0,
              "batch rejection committed a reservation or worktree event")

    config_for([owner, parent])
    accepted_parent = request_object("recipient-batch-parent", parent, batch_worktree, ["/bin/true"])
    batch_file.write_text(json.dumps([accepted, accepted_parent]))
    class BatchArgs:
        file = str(batch_file)
    original_read = warp_drive.read_hailing_enrollment_snapshot
    snapshot_reads = []
    def counted_read():
        snapshot_reads.append(1)
        return original_read()
    with patch.object(warp_drive, "read_hailing_enrollment_snapshot", side_effect=counted_read), \
            patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
        warp_drive.command_batch(BatchArgs(), batch_state)
    check(len(snapshot_reads) == 1,
          "one batch command read more than one local Hailing enrollment snapshot")


def test_cli_reservations(temp):
    state = temp / "state"
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    run_cli(state, "init")
    actor = os.environ.get("USER") or getpass.getuser() or "operator"
    run_cli(state, "config", "notifications", "off", "--actor", actor)

    wt_a, wt_b = temp / "worktree-a", temp / "worktree-b"
    wt_a.mkdir()
    wt_b.mkdir()
    marker_one = temp / "one-ran.txt"
    marker_two = temp / "two-ran.txt"
    code_one = ("sum(i*i for i in range(250000)); from pathlib import Path; "
                "p=Path(%r); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')") % str(marker_one)
    code_two = "from pathlib import Path; Path(%r).write_text('should-not-run')" % str(marker_two)
    command_one = [sys.executable, "-c", code_one]
    command_two = [sys.executable, "-c", code_two]
    items = [request_object("fair-a-heavy", owner, wt_a, command_one),
             request_object("fair-b-light", owner, wt_b, command_two),
             request_object("fair-a-second", owner, wt_a, command_two)]
    items[0]["size"] = "heavy"
    batch_file = temp / "batch.json"
    batch_file.write_text(json.dumps(items))
    batch_result = parse_json(run_cli(state, "batch", "--file", batch_file))
    ids = {item["request_key"]: item["request_id"] for item in batch_result["requests"]}
    check(len(ids) == 3, "atomic batch did not insert all requests")

    before_count = parse_json(run_cli(state, "status"))["reservations"]
    conflicting = [items[0], request_object("new-must-not-insert", owner, wt_b, command_one)]
    conflicting[0] = dict(conflicting[0], command=command_two)
    batch_file.write_text(json.dumps(conflicting))
    run_cli(state, "batch", "--file", batch_file, ok=False)
    after_count = parse_json(run_cli(state, "status"))["reservations"]
    check(len(after_count) == len(before_count), "conflicting batch inserted a partial row")

    for key, item in zip(("fair-a-heavy", "fair-b-light", "fair-a-second"), items):
        owner_fields = ["--harness", owner["harness"], "--session", owner["session"], "--surface", owner["surface"]]
        with contextlib.closing(warp_drive.connect(state)) as db:
            with db:
                helper_id = str(uuid.uuid4())
                warp_drive.attach_helper(db, ids[key], owner, helper_id)

    # Use a deterministic sample and a fixed policy pass to assert worktree turns.
    fixture = {"at": time.time(), "logical_cpus": 8, "cpu_busy": 0.10,
               "memory_pressure": "normal", "swap_used_mb": 0.0,
               "thermal_state": None, "availability": {"cpu": True, "memory_pressure": True,
               "swap": True, "thermal": False}}
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.host_sample(db, source="fixture", fixture=fixture)
            first = warp_drive.scheduler_pass(db, sample=fixture)
            check(first["granted"] == ids["fair-a-heavy"], "manual order did not grant the first worktree")
            # The following turn belongs to worktree B, even though worktree A has another queued item.
            fixture_second = dict(fixture, at=fixture["at"] + 11)
            warp_drive.host_sample(db, source="fixture", fixture=fixture_second)
            second = warp_drive.scheduler_pass(db, sample=fixture_second)
            check(second["granted"] == ids["fair-b-light"], "unequal batch skipped another worktree's turn")

    result = run_cli(state, "attach", ids["fair-a-heavy"], *owner_args(owner), timeout=20)
    check(marker_one.read_text() == "1", "helper did not execute the harmless command exactly once")
    inspected = parse_json(run_cli(state, "inspect", ids["fair-a-heavy"]))["reservation"]
    check(inspected["state"] == "finished" and inspected["exit_code"] == 0,
          "completion did not finish the same reservation")
    check(inspected["outcome"] == "succeeded" and inspected["wake_status"] == "pending",
          "completion outcome was not durably left pending for the scheduler sender")
    with contextlib.closing(warp_drive.connect(state)) as db:
        finished_event = db.execute("SELECT data_json FROM events WHERE reservation_id=? AND kind='finished'",
                                    (ids["fair-a-heavy"],)).fetchone()
    accounting = json.loads(finished_event["data_json"])
    if hasattr(os, "wait4") and hasattr(os, "waitstatus_to_exitcode"):
        check(accounting["child_cpu_seconds"] is not None and accounting["child_cpu_seconds"] >= 0,
              "supported POSIX child accounting omitted nonnegative CPU seconds")
        check(accounting["measurement"] == "wait4 child accounting" and
              accounting["cpu_unit"] == "seconds" and accounting["peak_memory_unit"] == "bytes" and
              "direct child plus descendants it waited for" in accounting["accounting_scope"] and
              "excludes Docker, daemons, and orphaned processes" in accounting["accounting_scope"] and
              "not an aggregate" in accounting["peak_memory_scope"],
              "child accounting omitted units or its direct-child measurement scope")
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            check(accounting["peak_memory_bytes"] is not None and accounting["peak_memory_bytes"] > 0,
                  "supported POSIX child accounting omitted peak RSS bytes")
        else:
            check(accounting["peak_memory_bytes"] is None and accounting.get("accounting_detail"),
                  "unknown ru_maxrss platform units were presented as normalized bytes")
    else:
        check(accounting["child_cpu_seconds"] is None and accounting["peak_memory_bytes"] is None and
              accounting["measurement"] == "unavailable" and accounting.get("accounting_detail"),
              "unsupported child accounting did not retain an explicit missing-data reason")

    # Reusing an identical finished key may inspect/attach, but never launches it again.
    run_cli(state, "run", "--key", "fair-a-heavy", *owner_args(owner), "--size", "heavy",
            "--family", "verify", "--workers", "1", "--expected-runtime", "30",
            "--repo", wt_a, "--worktree", wt_a, "--", *command_one, timeout=20)
    check(marker_one.read_text() == "1", "identical retry replayed a finished command")

    # A recorded launch intent is never replayed by a new helper.
    request_id = ids["fair-b-light"]
    with contextlib.closing(warp_drive.connect(state)) as db:
        row = warp_drive.current_request(db, request_id)
        claim, _command, _cwd = warp_drive.claim_launch(db, request_id,
            row["helper_id"], row["helper_generation"])
        with db:
            db.execute("UPDATE reservations SET lease_active=0,outcome='unknown' WHERE id=?", (request_id,))
    timeout_result = run_cli(state, "attach", request_id, *owner_args(owner), "--timeout", "0.1",
                             timeout=5, ok=False)
    check(timeout_result.returncode == 4 and json.loads(timeout_result.stdout)["state"] == "reserved",
          "uncertain launched work did not print its nonzero monitor-only status")
    check(not marker_two.exists(), "reattach replayed a command with launch intent")
    unknown = parse_json(run_cli(state, "inspect", request_id))["reservation"]
    check(unknown["state"] == "reserved" and unknown["outcome"] == "unknown",
          "uncertain launch was not retained as unknown")
    run_cli(state, "reconcile", request_id, *owner_args(owner), "--outcome", "unknown",
            "--evidence", "owner cannot determine child result")
    with contextlib.closing(warp_drive.connect(state)) as db:
        reconciled_event = db.execute("SELECT data_json FROM events WHERE reservation_id=? AND kind='owner_reconciled'",
                                      (request_id,)).fetchone()
    reconciled_accounting = json.loads(reconciled_event["data_json"])
    check(reconciled_accounting["measurement"] == "unavailable" and
          reconciled_accounting["child_cpu_seconds"] is None and
          reconciled_accounting["peak_memory_bytes"] is None and
          "no helper-observed wait4 sample" in reconciled_accounting["accounting_detail"],
          "unknown owner reconciliation omitted explicit missing child accounting provenance")

    # Delay and hold preserve earned credit, and priority/order reject unauthorized actors.
    waiting_id = ids["fair-a-second"]
    with contextlib.closing(warp_drive.connect(state)) as db:
        with db:
            db.execute("UPDATE reservations SET credit_seconds=12,credit_started_at=?,heartbeat_at=? WHERE id=?",
                       (time.time()-3, time.time(), waiting_id))
    run_cli(state, "delay", waiting_id, "--hold")
    held = parse_json(run_cli(state, "inspect", waiting_id))["reservation"]
    check(held["eligible_at"] is None and held["credit_seconds"] >= 12,
          f"hold did not preserve accrued waiting credit: eligible={held['eligible_at']} credit={held['credit_seconds']}")
    run_cli(state, "delay", waiting_id, "--resume")
    timeout_waiting = run_cli(state, "attach", waiting_id, *owner_args(owner), "--timeout", "0.1",
                              timeout=5, ok=False)
    check(timeout_waiting.returncode == 4 and json.loads(timeout_waiting.stdout)["state"] == "waiting",
          "attach timeout did not print JSON and return a nonzero status")
    status = parse_json(run_cli(state, "status"))
    revision = status["order_revision"]
    run_cli(state, "order", wt_b, "--before", wt_a, "--actor", actor, "--revision", revision)
    run_cli(state, "boost", waiting_id, "--actor", actor, "--reason", "verification fixture")
    run_cli(state, "boost", waiting_id, "--actor", "unlisted-agent", "--reason", "must refuse", ok=False)


def test_spawn_failure_accounting_provenance(temp):
    state = temp / "spawn-failure-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    worktree = temp / "spawn-failure-worktree"
    worktree.mkdir()
    request_id = parse_json(run_cli(state, "submit", "--key", "missing-child", *owner_args(owner),
        "--size", "light", "--family", "verify", "--workers", "1", "--expected-runtime", "30",
        "--repo", worktree, "--worktree", worktree, "--", str(temp / "not-installed-child")))["request_id"]
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.attach_helper(db, request_id, owner, str(uuid.uuid4()))
            sample = {"at": time.time(), "logical_cpus": 8, "cpu_busy": 0.05,
                      "memory_pressure": "normal", "swap_used_mb": 0.0,
                      "thermal_state": None, "availability": {"cpu": True,
                      "memory_pressure": True, "swap": True, "thermal": False}}
            warp_drive.host_sample(db, source="fixture", fixture=sample)
            check(warp_drive.scheduler_pass(db, sample=sample)["granted"] == request_id,
                  "spawn-failure fixture was not granted before helper launch")
    failed = run_cli(state, "attach", request_id, *owner_args(owner), ok=False)
    check(failed.returncode == 127 and json.loads(failed.stdout)["exit_code"] == 127,
          "missing executable did not produce a recorded spawn failure")
    with contextlib.closing(warp_drive.connect(state)) as db:
        finished = db.execute("SELECT data_json FROM events WHERE reservation_id=? AND kind='finished'",
                              (request_id,)).fetchone()
    accounting = json.loads(finished["data_json"])
    check(accounting["measurement"] == "unavailable" and
          accounting["child_cpu_seconds"] is None and accounting["peak_memory_bytes"] is None and
          "spawn failed" in accounting["accounting_detail"],
          "spawn failure did not retain explicit missing child accounting provenance")


def test_ignored_sigchld_preserves_child_exit(temp):
    state = temp / "ignored-sigchld-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    worktree = temp / "ignored-sigchld-worktree"
    worktree.mkdir()
    signal_marker = temp / "child-sigchld-disposition.txt"
    child_code = ("import signal; from pathlib import Path; "
                  f"Path({str(signal_marker)!r}).write_text(str(signal.getsignal(signal.SIGCHLD))); "
                  "raise SystemExit(23)")
    request_id = parse_json(run_cli(state, "submit", "--key", "ignored-sigchld-child", *owner_args(owner),
        "--size", "light", "--family", "verify", "--workers", "1", "--expected-runtime", "30",
        "--repo", worktree, "--worktree", worktree, "--", sys.executable, "-c", child_code))["request_id"]
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.attach_helper(db, request_id, owner, str(uuid.uuid4()))
            sample = {"at": time.time(), "logical_cpus": 8, "cpu_busy": 0.05,
                      "memory_pressure": "normal", "swap_used_mb": 0.0,
                      "thermal_state": None, "availability": {"cpu": True,
                      "memory_pressure": True, "swap": True, "thermal": False}}
            warp_drive.host_sample(db, source="fixture", fixture=sample)
            check(warp_drive.scheduler_pass(db, sample=sample)["granted"] == request_id,
                  "ignored-SIGCHLD fixture was not granted before helper launch")

    def ignore_sigchld():
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    command = [sys.executable, str(ROOT / "warp_drive.py"), "--state-dir", str(state),
               "attach", request_id, *owner_args(owner)]
    helper = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              preexec_fn=ignore_sigchld)
    stdout, stderr = helper.communicate(timeout=15)
    check(helper.returncode == 23 and json.loads(stdout)["exit_code"] == 23 and
          json.loads(stdout)["outcome"] == "failed",
          f"helper misreported a failing child with inherited SIGCHLD=SIG_IGN: {stdout}\n{stderr}")
    check(signal_marker.read_text() == str(signal.SIG_DFL),
          "helper did not reset inherited SIGCHLD=SIG_IGN before launching its child")


def test_transferred_completion_wake(temp):
    state = temp / "transfer-state"
    old_owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    new_owner = {"harness": "claude", "session": uuid_text(), "surface": uuid_text()}
    scheduler_surface = uuid_text()
    worktree = temp / "transfer-worktree"
    same_worktree = temp / "same-owner-worktree"
    worktree.mkdir()
    same_worktree.mkdir()
    started = temp / "child-started"
    release = temp / "child-release"
    log = temp / "fake-cmux.jsonl"
    hailing_dir = temp / "fake-hailing"
    hailing_dir.mkdir()
    targets = []
    for label, owner in (("same-owner", old_owner), ("transferred-owner", new_owner)):
        targets.append({"label": label, "harness": owner["harness"],
                        "session": owner["session"], "surface": owner["surface"],
                        "workspace": uuid_text(), "started_at": 1.0, "cutoff": 1.0})
    (hailing_dir / "config.json").write_text(json.dumps({"version": 1, "targets": targets,
        "remind_after": 60, "escalate_after": 120, "cooldown": 30, "interval": 10}))
    fake_cmux = temp / "fake-cmux"
    fake_cmux.write_text("""#!/usr/bin/env python3
import json, os, sys
lock_available = None
if os.environ.get('FAKE_DB_PATH'):
    import sqlite3
    db = sqlite3.connect(os.environ['FAKE_DB_PATH'], timeout=0.1)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.commit()
        lock_available = True
    except sqlite3.OperationalError:
        lock_available = False
    db.close()
with open(os.environ['FAKE_CMUX_LOG'], 'a') as stream:
    stream.write(json.dumps({'argv': sys.argv[1:], 'surface_env': os.environ.get('CMUX_SURFACE_ID'),
                             'write_lock_available': lock_available}) + '\\n')
sys.exit(int(os.environ.get('FAKE_CMUX_EXIT', '0')))
""")
    fake_cmux.chmod(0o755)
    helper_env = {"WARP_DRIVE_HAILING_STATE": str(hailing_dir),
                  "FAKE_CMUX_LOG": str(log), "CMUX_SURFACE_ID": old_owner["surface"],
                  "CODEX_THREAD_ID": old_owner["session"]}
    scheduler_env = {**helper_env, "WARP_DRIVE_CMUX": str(fake_cmux),
                     "FAKE_DB_PATH": str(state / "warp-drive.sqlite3"),
                     "CMUX_SURFACE_ID": scheduler_surface}
    run_cli(state, "init")
    command_code = ("from pathlib import Path; import time; "
                    f"Path({str(started)!r}).touch(); "
                    f"gate=Path({str(release)!r}); "
                    "deadline=time.time()+15; "
                    "exec('while not gate.exists() and time.time()<deadline: time.sleep(0.02)')")
    submitted = parse_json(run_cli(state, "submit", "--key", "transfer-active-command",
        *owner_args(old_owner), "--size", "light", "--family", "verify", "--workers", "1",
        "--expected-runtime", "30", "--repo", worktree, "--worktree", worktree,
        "--", sys.executable, "-c", command_code, env=helper_env))
    request_id = submitted["request_id"]
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.attach_helper(db, request_id, old_owner, str(uuid.uuid4()))
            fixture = {"at": time.time(), "logical_cpus": 8, "cpu_busy": 0.05,
                       "memory_pressure": "normal", "swap_used_mb": 0.0,
                       "thermal_state": None, "availability": {"cpu": True,
                       "memory_pressure": True, "swap": True, "thermal": False}}
            warp_drive.host_sample(db, source="fixture", fixture=fixture)
            grant = warp_drive.scheduler_pass(db, sample=fixture)
            check(grant["granted"] == request_id, "transfer fixture reservation was not granted")

    attach_command = [sys.executable, str(ROOT / "warp_drive.py"), "--state-dir", str(state),
                      "attach", request_id, *owner_args(old_owner)]
    helper = subprocess.Popen(attach_command, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env={**os.environ, **helper_env})
    deadline = time.time() + 10
    while time.time() < deadline and not started.exists():
        if helper.poll() is not None:
            break
        time.sleep(0.05)
    check(started.exists(), "real helper did not start the harmless child")
    run_cli(state, "transfer", request_id, *owner_args(new_owner),
            "--from-session", old_owner["session"], "--reason", "verify routed wake",
            env=helper_env)
    release.touch()
    stdout, stderr = helper.communicate(timeout=20)
    check(helper.returncode == 0, f"helper failed after transfer: {stdout}\n{stderr}")
    check(not log.exists(), "helper sent completion instead of leaving it pending for the scheduler")
    transferred = parse_json(run_cli(state, "inspect", request_id))["reservation"]
    check(transferred["wake_status"] == "pending", "helper did not durably record its pending completion")
    run_cli(state, "scheduler", "--once", "--send", env=scheduler_env)
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    check(len(messages) == 1, "completion did not queue exactly one native message")
    message = messages[0]
    check(message["argv"][0:2] == ["agent", "message"] and
          message["argv"][2] == new_owner["surface"], "completion was not routed to the transferred owner")
    check(message["surface_env"] == scheduler_surface,
          "scheduler sent from a surface other than its own distinct terminal")
    check(message["write_lock_available"] is True,
          "scheduler held a SQLite write transaction across the CMUX subprocess")
    check(request_id in message["argv"][3] and "warp-drive-complete-" in message["argv"][3],
          "completion message omitted the stable event identity")
    result = parse_json(run_cli(state, "inspect", request_id))["reservation"]
    check(result["wake_status"] == "queued", "queued completion was not distinguished from receipt")

    def assert_hailing_source(target, sender_surface, label):
        message_record = {"id": str(uuid.uuid4()), "recipient_surface_id": target["surface"],
                          "recipient_workspace_id": target["workspace"],
                          "sender_surface_id": sender_surface,
                          "sender_name": "warp-drive-scheduler", "created_at": time.time()}
        sources = hailing.Monitor.source_messages(object(), target, [message_record])
        check(len(sources) == 1, f"Hailing source filter rejected {label} from a distinct scheduler surface")
        self_message = dict(message_record, sender_surface_id=target["surface"])
        check(not hailing.Monitor.source_messages(object(), target, [self_message]),
              f"Hailing self-message guard did not reject {label}")

    assert_hailing_source(targets[1], scheduler_surface, "transferred-owner completion")

    same_result = parse_json(run_cli(state, "submit", "--key", "same-owner-completion",
        *owner_args(old_owner), "--size", "light", "--family", "verify", "--workers", "1",
        "--expected-runtime", "30", "--repo", same_worktree, "--worktree", same_worktree,
        "--", sys.executable, "-c", "pass", env=helper_env))
    same_id = same_result["request_id"]
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.attach_helper(db, same_id, old_owner, str(uuid.uuid4()))
            sample = {"at": time.time(), "logical_cpus": 8, "cpu_busy": 0.05,
                      "memory_pressure": "normal", "swap_used_mb": 0.0,
                      "thermal_state": None, "availability": {"cpu": True,
                      "memory_pressure": True, "swap": True, "thermal": False}}
            warp_drive.host_sample(db, source="fixture", fixture=sample)
            check(warp_drive.scheduler_pass(db, sample=sample)["granted"] == same_id,
                  "same-owner completion fixture was not granted")
    run_cli(state, "attach", same_id, *owner_args(old_owner), timeout=10, env=helper_env)
    check(parse_json(run_cli(state, "inspect", same_id))["reservation"]["wake_status"] == "pending",
          "same-owner helper did not leave completion pending")
    run_cli(state, "scheduler", "--once", "--send", env=scheduler_env)
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    check(len(messages) == 2 and messages[1]["argv"][2] == old_owner["surface"],
          "scheduler did not route same-owner completion through native inbox")
    assert_hailing_source(targets[0], scheduler_surface, "same-owner completion")

    def finished_pending(key, owner, path):
        request, helper_id, generation = prepare_reserved_command(state, owner, key, path,
                                                                  temp / (key + ".unused-marker"))
        with contextlib.closing(warp_drive.connect(state)) as db:
            claim, _command, _cwd = warp_drive.claim_launch(db, request, helper_id, generation)
            warp_drive.record_completion(db, request, claim, 0)
        return request

    recovery_path = temp / "recovery-worktree"
    recovery_path.mkdir()
    recovery_id = finished_pending("capability-recovery", new_owner, recovery_path)
    empty_path = temp / "empty-path"
    empty_path.mkdir()
    no_capability = {**scheduler_env, "WARP_DRIVE_CMUX": "", "PATH": str(empty_path)}
    run_cli(state, "scheduler", "--once", "--send", env=no_capability)
    check(parse_json(run_cli(state, "inspect", recovery_id))["reservation"]["wake_status"] in ("pending", "unavailable"),
          "missing CMUX capability was not retained for a later scheduler pass")
    run_cli(state, "scheduler", "--once", "--send", env=scheduler_env)
    check(parse_json(run_cli(state, "inspect", recovery_id))["reservation"]["wake_status"] == "queued",
          "scheduler restart did not deliver a previously unavailable completion")

    uncertain_path = temp / "uncertain-worktree"
    uncertain_path.mkdir()
    uncertain_id = finished_pending("uncertain-wake", new_owner, uncertain_path)
    run_cli(state, "scheduler", "--once", "--send", env={**scheduler_env, "FAKE_CMUX_EXIT": "23"})
    check(parse_json(run_cli(state, "inspect", uncertain_id))["reservation"]["wake_status"] == "uncertain",
          "failed external completion attempt was not marked uncertain")
    run_cli(state, "scheduler", "--once", "--send", env=scheduler_env)
    check(len([json.loads(line) for line in log.read_text().splitlines()]) == 4,
          "uncertain CMUX send was automatically repeated")


def test_missing_owner_completion_route_diagnostics_and_recovery(temp):
    state = temp / "missing-owner-route-state"
    run_cli(state, "init")
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    worktree = temp / "missing-owner-route-worktree"
    worktree.mkdir()
    request_id, helper_id, generation = prepare_reserved_command(
        state, owner, "missing-owner-route", worktree, temp / "unused-completion-marker")
    with contextlib.closing(warp_drive.connect(state)) as db:
        claim, _command, _cwd = warp_drive.claim_launch(db, request_id, helper_id, generation)
        warp_drive.record_completion(db, request_id, claim, 0)

    hailing_dir = temp / "missing-owner-route-hailing"
    hailing_dir.mkdir()
    config_path = hailing_dir / "config.json"
    config_path.write_text(json.dumps({"version": 1, "targets": [], "remind_after": 60,
                                      "escalate_after": 120, "cooldown": 30, "interval": 10}))
    send_log = temp / "missing-owner-route-cmux.jsonl"
    fake_cmux = temp / "missing-owner-route-cmux"
    fake_cmux.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['WARP_DRIVE_ROUTE_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
""")
    fake_cmux.chmod(0o755)
    scheduler_surface = uuid_text()
    send_env = {"WARP_DRIVE_HAILING_STATE": str(hailing_dir),
                "WARP_DRIVE_CMUX": str(fake_cmux),
                "WARP_DRIVE_ROUTE_LOG": str(send_log),
                "CMUX_SURFACE_ID": scheduler_surface}

    status = parse_json(run_cli(state, "status", env=send_env))
    doctor = parse_json(run_cli(state, "doctor", env=send_env))
    inspected = parse_json(run_cli(state, "inspect", request_id, env=send_env))["reservation"]
    gap = status["completion_route_gaps"][0]
    check(gap["status"] == "missing_exact_hailing_route" and
          gap["request_ids"] == [request_id] and
          gap["owner_setup_action"] ==
          f"hailing-frequencies enroll --harness codex --surface {owner['surface']} "
          f"--session {owner['session']} --label warp-drive-owner" and
          "warp-drive transfer REQUEST_ID" in gap["handoff_action"],
          "status omitted the missing owner route, affected ID or supported recovery actions")
    check(doctor["completion_route_gaps"][0]["request_ids"] == [request_id] and
          doctor["pending_completion_wakes"] == 1 and
          inspected["completion_route"]["status"] == "missing_exact_hailing_route",
          "doctor or inspect did not expose the missing owner route")

    run_cli(state, "scheduler", "--once", "--send", env=send_env)
    check(parse_json(run_cli(state, "inspect", request_id, env=send_env))["reservation"][
          "wake_status"] == "pending",
          "completion was not retained while the exact owner route was unavailable")
    target = {**owner, "workspace": uuid_text(), "started_at": time.time(),
              "cutoff": time.time()+3600, "label": "warp-drive-owner"}
    config_path.write_text(json.dumps({"version": 1, "targets": [target], "remind_after": 60,
                                      "escalate_after": 120, "cooldown": 30, "interval": 10}))
    enrolled_status = parse_json(run_cli(state, "status", env=send_env))
    enrolled_inspect = parse_json(run_cli(state, "inspect", request_id, env=send_env))["reservation"]
    check(not enrolled_status["completion_route_gaps"] and
          enrolled_inspect["completion_route"]["status"] == "enrolled",
          "status did not recognize recovery of the exact Hailing owner route")
    run_cli(state, "scheduler", "--once", "--send", env=send_env)
    messages = [json.loads(line) for line in send_log.read_text().splitlines()]
    final = parse_json(run_cli(state, "inspect", request_id, env=send_env))["reservation"]
    check(len(messages) == 1 and messages[0][0:3] == ["agent", "message", owner["surface"]] and
          request_id in messages[0][3] and final["wake_status"] == "queued" and
          not parse_json(run_cli(state, "doctor", env=send_env))["completion_route_gaps"],
          "existing sending scheduler did not retry the durable completion after owner enrollment")


def test_completion_backlog_connection_bound(temp):
    state = temp / "completion-backlog-state"
    run_cli(state, "init")
    worktree = temp.resolve()
    stat = worktree.stat()
    worktree_id = str(uuid.uuid4())
    owner_session, owner_surface = uuid_text(), uuid_text()
    inserted_at = time.time()
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("INSERT INTO worktrees(id,path,device,inode,order_position,created_at) VALUES (?,?,?,?,?,?)",
                       (worktree_id, str(worktree), stat.st_dev, stat.st_ino, 0, inserted_at))
            rows = []
            for sequence in range(1, 2001):
                rows.append((str(uuid.uuid4()), f"unavailable-{sequence}", f"sha-{sequence}",
                    sequence, worktree_id, str(worktree), str(worktree), "codex", owner_session,
                    owner_surface, "light", "verify", 1, 60, '["python3","-c","pass"]',
                    "finished", inserted_at, inserted_at, "succeeded", 0, inserted_at, 0,
                    "unavailable", "owner is not enrolled"))
            db.executemany("""INSERT INTO reservations
                (id,request_key,content_sha256,sequence,worktree_id,repo,worktree_path,
                 owner_harness,owner_session,owner_surface,size,family,workers,expected_runtime,
                 command_json,state,created_at,eligible_at,outcome,exit_code,ended_at,lease_active,
                 wake_status,wake_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)

    original_connect = warp_drive.connect
    original_targets = warp_drive.read_hailing_targets
    original_cmux = warp_drive.cmux_executable
    original_sender = warp_drive.observed_sender_surface
    original_hailing_state = warp_drive.hailing_state_dir
    connect_calls = []
    target_reads = []
    cmux_reads = []
    sender_reads = []
    no_hailing = temp / "no-hailing-for-backlog"

    def counted_connect(path):
        connect_calls.append(path)
        return original_connect(path)

    def counted_targets():
        target_reads.append(1)
        return original_targets()

    def no_cmux():
        cmux_reads.append(1)
        return None

    def no_sender():
        sender_reads.append(1)
        return uuid_text()

    warp_drive.connect = counted_connect
    warp_drive.read_hailing_targets = counted_targets
    warp_drive.cmux_executable = no_cmux
    warp_drive.observed_sender_surface = no_sender
    warp_drive.hailing_state_dir = lambda: no_hailing
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            warp_drive.scheduler(state, once=True, send=True)
    finally:
        warp_drive.connect = original_connect
        warp_drive.read_hailing_targets = original_targets
        warp_drive.cmux_executable = original_cmux
        warp_drive.observed_sender_surface = original_sender
        warp_drive.hailing_state_dir = original_hailing_state
    check(len(connect_calls) <= 2,
          f"2,000 undeliverable completions opened {len(connect_calls)} database connections")
    check(len(target_reads) == len(cmux_reads) == len(sender_reads) == 1,
          "scheduler did not cache enrollment and sender capability once per pass")
    with contextlib.closing(original_connect(state)) as db:
        count = db.execute("SELECT count(*) FROM reservations WHERE state='finished' AND wake_status='unavailable'").fetchone()[0]
    check(count == 2000, "undeliverable rows were mutated while the scheduler skipped them")


def test_notification_send_pacing(temp):
    state = temp / "notification-pacing-state"
    run_cli(state, "init")
    worktree = temp.resolve()
    stat = worktree.stat()
    worktree_id = str(uuid.uuid4())
    owner = {"harness": "codex", "session": uuid_text(), "surface": uuid_text()}
    engineer = {"harness": "claude", "session": uuid_text(), "surface": uuid_text()}
    scheduler_surface = uuid_text()
    inserted_at = time.time()
    completion_ids = [str(uuid.uuid4()) for _ in range(20)]
    incident_ids = []
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            db.execute("INSERT INTO worktrees(id,path,device,inode,order_position,created_at) VALUES (?,?,?,?,?,?)",
                       (worktree_id, str(worktree), stat.st_dev, stat.st_ino, 0, inserted_at))
            rows = []
            for sequence, request_id in enumerate(completion_ids, start=1):
                rows.append((request_id, f"paced-{sequence}", f"paced-sha-{sequence}",
                    sequence, worktree_id, str(worktree), str(worktree), owner["harness"],
                    owner["session"], owner["surface"], "light", "verify", 1, 60,
                    '["python3","-c","pass"]', "finished", inserted_at, inserted_at,
                    "succeeded", 0, inserted_at + sequence, 0, "pending", None))
            db.executemany("""INSERT INTO reservations
                (id,request_key,content_sha256,sequence,worktree_id,repo,worktree_path,
                 owner_harness,owner_session,owner_surface,size,family,workers,expected_runtime,
                 command_json,state,created_at,eligible_at,outcome,exit_code,ended_at,lease_active,
                 wake_status,wake_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            for index in range(5):
                incident_ids.append(warp_drive.record_incident(
                    db, f"paced-engineer-notice-{index}", {"notice": index}))

    hailing_dir = temp / "paced-hailing"
    hailing_dir.mkdir()
    targets = [
        {**owner, "workspace": uuid_text(), "started_at": inserted_at, "cutoff": inserted_at + 3600},
        {**engineer, "workspace": uuid_text(), "started_at": inserted_at, "cutoff": inserted_at + 3600,
         "label": "warp-drive-engineer"},
    ]
    (hailing_dir / "config.json").write_text(json.dumps({"version": 1, "targets": targets,
        "remind_after": 60, "escalate_after": 120, "cooldown": 30, "interval": 10}))
    log = temp / "paced-cmux.jsonl"
    fake_cmux = temp / "paced-cmux"
    fake_cmux.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['WARP_DRIVE_PACED_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
""")
    fake_cmux.chmod(0o755)

    variables = {"WARP_DRIVE_HAILING_STATE": str(hailing_dir), "WARP_DRIVE_CMUX": str(fake_cmux),
                 "WARP_DRIVE_PACED_LOG": str(log), "CMUX_SURFACE_ID": scheduler_surface}
    previous = {key: os.environ.get(key) for key in variables}
    try:
        os.environ.update(variables)
        for pass_number in range(20):
            before = len(log.read_text().splitlines()) if log.exists() else 0
            with contextlib.redirect_stdout(io.StringIO()):
                warp_drive.scheduler(state, once=True, send=True)
            messages = [json.loads(line) for line in log.read_text().splitlines()]
            added = messages[before:]
            check(len(added) <= 2,
                  f"scheduler pass {pass_number + 1} emitted {len(added)} notifications, above the two-category cap")
            check(sum("warp-drive-complete-" in message[-1] for message in added) <= 1,
                  "scheduler pass emitted multiple completion messages")
            check(sum("Warp-drive incident " in message[-1] for message in added) <= 1,
                  "scheduler pass emitted multiple engineer incident messages")
            if pass_number == 0:
                check(len(added) == 2, "first scheduler pass did not demonstrate both paced send categories")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    messages = [json.loads(line)[-1] for line in log.read_text().splitlines()]
    completion_messages = [message for message in messages if "warp-drive-complete-" in message]
    incident_messages = [message for message in messages if "Warp-drive incident " in message]
    check(len(completion_messages) == len(completion_ids) and
          all(sum(request_id in message for message in completion_messages) == 1
              for request_id in completion_ids),
          "paced completion backlog did not drain once per stable event identity")
    check(len(incident_messages) == len(incident_ids) and
          all(sum(incident_id in message for message in incident_messages) == 1
              for incident_id in incident_ids),
          "paced incident backlog did not drain without duplicate sends")
    with contextlib.closing(warp_drive.connect(state)) as db:
        pending = db.execute("SELECT count(*) FROM reservations WHERE state='finished' AND wake_status IN ('pending','unavailable')").fetchone()[0]
        queued = db.execute("SELECT count(*) FROM reservations WHERE state='finished' AND wake_status='queued'").fetchone()[0]
        incident_queued = db.execute("SELECT count(*) FROM incidents WHERE notification_status='queued'").fetchone()[0]
    check(pending == 0 and queued == 20 and incident_queued == 5,
          "repeated scheduler passes did not safely drain the eligible notification backlogs")


def test_engineer_unavailable_retry_and_send_error_isolation(temp):
    state = temp / "engineer-retry-state"
    run_cli(state, "init")
    targets = [{"label": "warp-drive-engineer", "harness": "claude",
                "session": uuid_text(), "surface": uuid_text(), "workspace": uuid_text(),
                "started_at": 1.0, "cutoff": 1.0}]
    log = temp / "fake-engineer-cmux.jsonl"
    fake_cmux = temp / "fake-engineer-cmux"
    fake_cmux.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_ENGINEER_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
sys.exit(int(os.environ.get('FAKE_ENGINEER_EXIT', '0')))
""")
    fake_cmux.chmod(0o755)
    first_id, second_id, uncertain_id = [], [], []
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            first_id.append(warp_drive.record_incident(db, "engineer-before-enrollment", {"case": 1}))
            second_id.append(warp_drive.record_incident(db, "engineer-cli-unavailable", {"case": 2}))
            db.execute("UPDATE incidents SET notification_status='unavailable',notification_detail='no target' WHERE id=?",
                       (second_id[0],))

    no_target = warp_drive.deliver_open_incidents(state, [], str(fake_cmux))
    check(no_target == [], "engineer send ran without an enrolled target")
    hailing_dir = temp / "engineer-hailing"
    hailing_dir.mkdir()
    malformed = {"label": "bad-target", "harness": "codex", "session": None,
                 "surface": None, "workspace": None, "started_at": "bad", "cutoff": None}
    config = {"version": 1, "targets": [malformed, *targets], "remind_after": 60,
              "escalate_after": 120, "cooldown": 30, "interval": 10}
    (hailing_dir / "config.json").write_text(json.dumps(config))
    original_hailing_state = warp_drive.hailing_state_dir
    original_log = os.environ.get("FAKE_ENGINEER_LOG")
    warp_drive.hailing_state_dir = lambda: hailing_dir
    os.environ["FAKE_ENGINEER_LOG"] = str(log)
    try:
        loaded_targets = warp_drive.read_hailing_targets()
        check(loaded_targets == targets, "malformed Hailing target fields were not skipped safely")
        first_delivered = warp_drive.deliver_open_incidents(state, loaded_targets, str(fake_cmux))
        second_delivered = warp_drive.deliver_open_incidents(state, loaded_targets, str(fake_cmux))
        delivered = first_delivered + second_delivered
        check(len(first_delivered) == len(second_delivered) == 1 and
              all(item["status"] == "queued" for item in delivered),
              "previously unavailable engineer notices did not retry after enrollment and CLI recovery")
        with contextlib.closing(warp_drive.connect(state)) as db:
            with warp_drive.immediate_transaction(db):
                uncertain_id.append(warp_drive.record_incident(db, "engineer-uncertain-send", {"case": 3}))
        os.environ["FAKE_ENGINEER_EXIT"] = "23"
        uncertain_result = warp_drive.deliver_open_incidents(state, loaded_targets, str(fake_cmux))
        os.environ.pop("FAKE_ENGINEER_EXIT", None)
    finally:
        warp_drive.hailing_state_dir = original_hailing_state
        os.environ.pop("FAKE_ENGINEER_EXIT", None)
        if original_log is None:
            os.environ.pop("FAKE_ENGINEER_LOG", None)
        else:
            os.environ["FAKE_ENGINEER_LOG"] = original_log
    check(len(uncertain_result) == 1 and uncertain_result[0]["status"] == "uncertain",
          "nonzero external CMUX result was treated as safely retryable")
    with contextlib.closing(warp_drive.connect(state)) as db:
        status = db.execute("SELECT notification_status FROM incidents WHERE id=?",
                            (uncertain_id[0],)).fetchone()[0]
    check(status == "uncertain", "uncertain engineer delivery was not retained as terminal")
    messages_before = len(log.read_text().splitlines())
    warp_drive.deliver_open_incidents(state, loaded_targets, str(fake_cmux))
    check(len(log.read_text().splitlines()) == messages_before,
          "uncertain engineer CMUX attempt was blindly retried")

    error_state = temp / "send-error-state"
    run_cli(error_state, "init")
    originals = {name: getattr(warp_drive, name) for name in
                 ("read_hailing_targets", "cmux_executable", "observed_sender_surface",
                  "dispatch_pending_completions", "deliver_open_incidents", "hailing_state_dir")}
    warp_drive.read_hailing_targets = lambda: []
    warp_drive.cmux_executable = lambda: None
    warp_drive.observed_sender_surface = lambda: None
    warp_drive.hailing_state_dir = lambda: temp / "no-hailing"
    warp_drive.dispatch_pending_completions = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("completion send fixture"))
    warp_drive.deliver_open_incidents = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("incident send fixture"))
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            result = warp_drive.scheduler(error_state, once=True, send=True)
    finally:
        for name, value in originals.items():
            setattr(warp_drive, name, value)
    check(result["ready_count"] == 0 and "completion send pass failed" in stderr.getvalue() and
          "incident send pass failed" in stderr.getvalue(),
          "send-path errors stopped the scheduler pass instead of being reported and isolated")


def test_opencode_native_owner_identity(temp):
    state = temp / "opencode-owner-state"
    worktree = temp / "opencode-owner-worktree"
    worktree.mkdir()
    run_cli(state, "init")
    first = {"harness": "opencode", "session": "ses_AaZ9", "surface": uuid_text()}
    second = {"harness": "opencode", "session": "ses_NeWCase7", "surface": uuid_text()}
    canonical = warp_drive.canonical_owner(first["harness"], first["session"], first["surface"].lower())
    check(canonical["session"] == "ses_AaZ9" and canonical["surface"] == first["surface"],
          "OpenCode native session case was changed or surface UUID stopped canonicalizing")
    for invalid in ("ses_", "SES_AaZ9", uuid_text(), "ses_bad-value"):
        try:
            warp_drive.canonical_owner("opencode", invalid, first["surface"])
        except warp_drive.WarpError:
            pass
        else:
            raise AssertionError(f"invalid OpenCode session namespace was accepted: {invalid}")

    submitted = parse_json(run_cli(state, "submit", "--key", "opencode-exact-owner", *owner_args(first),
        "--size", "light", "--family", "verify-opencode", "--workers", "1", "--expected-runtime", "30",
        "--repo", worktree, "--worktree", worktree, "--", sys.executable, "-c", "pass"))
    request_id = submitted["request_id"]
    duplicate = parse_json(run_cli(state, "submit", "--key", "opencode-exact-owner", *owner_args(first),
        "--size", "light", "--family", "verify-opencode", "--workers", "1", "--expected-runtime", "30",
        "--repo", worktree, "--worktree", worktree, "--", sys.executable, "-c", "pass"))
    check(duplicate["request_id"] == request_id,
          "identical OpenCode request key did not deduplicate to the original reservation")
    altered_case = dict(first, session="ses_aaz9")
    changed_owner = run_cli(state, "submit", "--key", "opencode-exact-owner", *owner_args(altered_case),
        "--size", "light", "--family", "verify-opencode", "--workers", "1", "--expected-runtime", "30",
        "--repo", worktree, "--worktree", worktree, "--", sys.executable, "-c", "pass", ok=False)
    check(changed_owner.returncode != 0,
          "case-changed OpenCode owner reused the original stable request key")
    with contextlib.closing(warp_drive.connect(state)) as db:
        with warp_drive.immediate_transaction(db):
            warp_drive.attach_helper(db, request_id, first, str(uuid.uuid4()))
    admitted = run_fixture_pass(state, 0.05)
    check(admitted["granted"] == request_id,
          "exact OpenCode owner reservation was not admitted by the existing fixture host policy")
    run_cli(state, "attach", request_id, *owner_args(first), timeout=20)
    check(parse_json(run_cli(state, "inspect", request_id))["reservation"]["state"] == "finished",
          "exact OpenCode owner could not attach and finish the harmless reserved command")
    hailing_dir = temp / "opencode-hailing"
    hailing_dir.mkdir()
    target = {"label": "native-opencode", "harness": "opencode", "session": first["session"],
              "surface": first["surface"], "workspace": uuid_text(), "started_at": 1.0, "cutoff": 1.0}
    (hailing_dir / "config.json").write_text(json.dumps({"version": 1, "targets": [target]}))
    route_log = temp / "opencode-route.jsonl"
    fake_cmux = temp / "opencode-cmux"
    fake_cmux.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['WARP_DRIVE_ROUTE_LOG'], 'a') as stream:
    stream.write(json.dumps({'argv': sys.argv[1:], 'surface': os.environ.get('CMUX_SURFACE_ID')}) + '\\n')
""")
    fake_cmux.chmod(0o755)
    scheduler_surface = uuid_text()
    scheduler_env = {"WARP_DRIVE_HAILING_STATE": str(hailing_dir), "WARP_DRIVE_CMUX": str(fake_cmux),
                     "WARP_DRIVE_ROUTE_LOG": str(route_log), "CMUX_SURFACE_ID": scheduler_surface}
    original_hailing_state = warp_drive.hailing_state_dir
    warp_drive.hailing_state_dir = lambda: hailing_dir
    try:
        targets = warp_drive.read_hailing_targets()
        check(len(targets) == 1 and warp_drive.hailing_target_for_owner(first, targets) == target,
              "native OpenCode Hailing target did not preserve exact owner correlation")
        check(warp_drive.hailing_target_for_owner(dict(first, session="ses_aaz9"), targets) is None,
              "Hailing owner correlation ignored case-sensitive OpenCode session identity")
        with contextlib.closing(warp_drive.connect(state)) as db:
            row = warp_drive.current_request(db, request_id)
            route = warp_drive.completion_route_info(row, targets)
            check(route["status"] == "enrolled" and route["owner"]["session"] == "ses_AaZ9",
                  "finished request lost exact OpenCode Hailing route")
        rejected = run_cli(state, "transfer", request_id, *owner_args(second), "--from-session", "ses_aaz9",
                           "--reason", "must preserve exact OpenCode owner", ok=False)
        check(rejected.returncode != 0, "transfer accepted a case-changed prior OpenCode owner")
        transferred = parse_json(run_cli(state, "transfer", request_id, *owner_args(second),
            "--from-session", first["session"], "--reason", "exact native OpenCode handoff"))
        check(transferred["to"]["session"] == "ses_NeWCase7",
              "transfer changed or rejected exact OpenCode native session case")
        with contextlib.closing(warp_drive.connect(state)) as db:
            row = warp_drive.current_request(db, request_id)
            route = warp_drive.completion_route_info(row, targets)
            check(route["status"] == "missing_exact_hailing_route" and
                  route["owner"]["session"] == "ses_NeWCase7",
                  "transferred owner borrowed the previous owner's exact Hailing route")
        run_cli(state, "scheduler", "--once", "--send", env=scheduler_env)
        check(not route_log.exists(), "completion was routed to the previous OpenCode owner after transfer")
        new_target = {**target, **second, "workspace": uuid_text()}
        (hailing_dir / "config.json").write_text(json.dumps({"version": 1, "targets": [new_target]}))
        run_cli(state, "scheduler", "--once", "--send", env=scheduler_env)
        run_cli(state, "scheduler", "--once", "--send", env=scheduler_env)
        messages = [json.loads(line) for line in route_log.read_text().splitlines()]
        check(len(messages) == 1 and messages[0]["argv"][0:3] == ["agent", "message", second["surface"]] and
              messages[0]["surface"] == scheduler_surface and request_id in messages[0]["argv"][3],
              "completion was not deduplicated and routed through fake native CMUX to the exact new owner")
    finally:
        warp_drive.hailing_state_dir = original_hailing_state

def main():
    with tempfile.TemporaryDirectory(prefix="warp-drive-verify-") as directory:
        temp = Path(directory)
        previous_hailing_state = os.environ.get("WARP_DRIVE_HAILING_STATE")
        os.environ["WARP_DRIVE_HAILING_STATE"] = str(temp / "unconfigured-hailing-state")
        try:
            test_guide_and_install(temp)
            test_install_removes_new_runtime_after_seal_failure(temp)
            test_product_command_inherits_caller_umask(temp)
            test_policy_fixtures()
            test_scheduler_refreshes_policy_version(temp)
            test_explicit_cpu_estimates(temp)
            test_new_recipient_enrollment_guard(temp)
            test_missed_turn_ordering(temp)
            test_scheduler_startup_pacing_and_escape_limit(temp)
            test_active_escape_preserves_fitting_progress(temp)
            test_claim_and_cancel_launch_interleavings(temp)
            test_startup_observation_window(temp)
            test_incident_reopens_and_batch_owner_canonicalization(temp)
            test_ready_demand_incident_tracks_meaningful_progress(temp)
            test_ready_demand_incident_from_production_stall(temp)
            test_transfer_credit_watcher_dedup_and_child_busy_retry(temp)
            test_scheduler_fair_aged_worktree_and_idle_signal(temp)
            test_protected_target_survives_mean_settling(temp)
            test_cli_reservations(temp)
            test_spawn_failure_accounting_provenance(temp)
            test_ignored_sigchld_preserves_child_exit(temp)
            test_transferred_completion_wake(temp)
            test_missing_owner_completion_route_diagnostics_and_recovery(temp)
            test_completion_backlog_connection_bound(temp)
            test_notification_send_pacing(temp)
            test_engineer_unavailable_retry_and_send_error_isolation(temp)
            test_opencode_native_owner_identity(temp)
        finally:
            if previous_hailing_state is None:
                os.environ.pop("WARP_DRIVE_HAILING_STATE", None)
            else:
                os.environ["WARP_DRIVE_HAILING_STATE"] = previous_hailing_state
    print("warp-drive verification passed (temporary state, harmless command, no native sends)")


if __name__ == "__main__":
    main()
