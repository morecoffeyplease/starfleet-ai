import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pattern-buffer"))
import hailing_frequencies as hf
import install as hf_install
import temporal_beacon as tb
import pattern_buffer_checkpoint as pb_checkpoint


CHECKPOINT_IDLE_KEY = "pattern-buffer-checkpoint-idle"
AUTHORIZATION_REF = "synthetic-approved-retirement"


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class Native:
    def __init__(self, hook_directory):
        self.hook_directory = Path(hook_directory)

    def binding(self, target):
        store = json.loads((self.hook_directory / (target["harness"] + "-hook-sessions.json")).read_text())
        record = next((value for key, value in store["sessions"].items()
                       if key.upper() == target["session"].upper()), None)
        if (record is None or record.get("surfaceId", "").upper() != target["surface"]
                or record.get("workspaceId", "").upper() != target["workspace"]
                or record.get("startedAt") != target["started_at"]):
            raise ValueError("Synthetic exact-session binding changed")
        return True

    def activity(self, _target):
        at = datetime.fromtimestamp(time.time() - 60, timezone.utc).isoformat()
        return {"state": "idle", "last_activity_at": at}


class TimerRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.home / ".local" / "state" / "starfleet-ai" / "hailing-frequencies"
        self.state.mkdir(parents=True, mode=0o700)
        self.cmux_home = self.home / ".cmuxterm"
        self.cmux_home.mkdir(mode=0o700)
        self.cmux_bin = self.base / "bin"
        self.cmux_bin.mkdir()
        self.worktree = self.base / "repo"
        self.worktree.mkdir()
        subprocess.run(["git", "-C", str(self.worktree), "init", "-q"], check=True, capture_output=True)
        self.clock = Clock(time.time())
        self.old = self.target("old-owner", self.worktree)
        self.coordinator = self.target("coordinator", self.worktree)
        self.successor = self.target("successor", self.worktree)
        config = {"version": 1, "targets": [self.old, self.coordinator, self.successor],
                  "coordinator": self.coordinator["session"], "remind_after": 300,
                  "escalate_after": 900, "cooldown": 600, "interval": 5}
        hf.atomic_json(self.state / "config.json", config)
        sessions = {item["session"]: {"surfaceId": item["surface"], "workspaceId": item["workspace"],
                                       "startedAt": item["started_at"], "cwd": str(self.worktree),
                                       "agentLifecycle": "idle"}
                    for item in (self.old, self.coordinator, self.successor)}
        hook_store = {"sessions": sessions,
                      "activeSessionsBySurface": {item["surface"]: {"sessionId": item["session"]}
                                                  for item in (self.old, self.coordinator, self.successor)}}
        hf.atomic_json(self.state / "claude-hook-sessions.json", hook_store)
        hf.atomic_json(self.cmux_home / "claude-hook-sessions.json", hook_store)
        for name in ("supervisor.lock", "serve.lock"):
            (self.state / name).touch(mode=0o600)
        now = self.clock.value
        hf.atomic_json(self.state / "service.json", {"at": now, "state": "running",
                      "worker_pid": os.getpid(), "worker_token": "synthetic-worker"})
        hf.atomic_json(self.state / "timer-last-scan.json", {"at": now, "supports_idle": True,
                      "worker_pid": os.getpid(), "worker_token": "synthetic-worker"})
        self.native = Native(self.state)
        self.app = hf.Monitor(self.state, self.native, clock=self.clock)
        self.beacon = tb.TemporalBeacon(self.app, clock=self.clock)
        self.owner_env = self.environment(self.old)
        self.coordinator_env = self.environment(self.coordinator)
        self.successor_env = self.environment(self.successor)
        self.launcher_dir = self.home / ".local" / "bin"
        self.launcher_dir.mkdir(parents=True)
        self._write_fake_cmux()
        self._write_source_launcher()

    def target(self, label, worktree):
        session = str(uuid.uuid4()).upper()
        return {"surface": str(uuid.uuid4()).upper(), "workspace": str(uuid.uuid4()).upper(),
                "session": session, "harness": "claude", "label": label,
                "started_at": self.clock.value - 600 if hasattr(self, "clock") else time.time() - 600,
                "cutoff": time.time()}

    @staticmethod
    def environment(target):
        return {"CMUX_AGENT_LAUNCH_KIND": "claude", "CLAUDE_CODE_SESSION_ID": target["session"],
                "CMUX_SURFACE_ID": target["surface"], "CMUX_WORKSPACE_ID": target["workspace"]}

    def _write_source_launcher(self):
        source_root = Path(__file__).resolve().parents[1]
        script = self.launcher_dir / "hailing-frequencies"
        source_cli = source_root / "hailing_frequencies.py"
        script.write_text("#!/usr/bin/env python3\n" +
            "import os, sys\n" +
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(source_cli)!r}, *sys.argv[1:]])\n")
        script.chmod(0o700)

    def _write_fake_cmux(self):
        targets = [self.old, self.coordinator, self.successor]
        script = self.cmux_bin / "cmux"
        script.write_text("#!/usr/bin/env python3\n" +
            "from datetime import datetime, timedelta, timezone\n" +
            "import json, sys\n" +
            f"targets={targets!r}\n" +
            "args=sys.argv[1:]\n" +
            "if len(args) >= 2 and args[:2] == ['--json', 'current']:\n" +
            " at=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()\n" +
            " items=[{'placement':{'kind':'local'},'projections':[{'panel_id':t['surface'],'workspace_id':t['workspace']}],\n" +
            "         'agents':[{'session_id':t['session'],'kind':t['harness'],'state':'idle','last_activity_at':at}]} for t in targets]\n" +
            " print(json.dumps({'truncated':False,'owner_availability':{'agent_sessions':'available'},'items':items}))\n" +
            "elif len(args) >= 4 and args[:2] == ['--json', 'rpc']:\n" +
            " method=args[2]\n" +
            " params=json.loads(args[3])\n" +
            " if method == 'surface.input_state':\n" +
            "  target=next(t for t in targets if t['surface']==params['surface_id'])\n" +
            "  result={'surface_id':target['surface'],'workspace_id':target['workspace'],'agent':True,'terminal':True,\n" +
            "          'state':'empty','blocks_typing':False,'waiting_on_human':False,'lifecycle':'idle'}\n" +
            " elif method == 'agent.message.settings':\n" +
            "  result={'messages_enabled':True,'receiving':True,'workspace_receiving':True}\n" +
            " else:\n" +
            "  print(json.dumps({'error':'Unsupported fake CMUX RPC'}),file=sys.stderr)\n" +
            "  sys.exit(2)\n" +
            " print(json.dumps(result))\n" +
            "else:\n" +
            " print(json.dumps({'error':'Unsupported fake CMUX command'}),file=sys.stderr)\n" +
            " sys.exit(2)\n")
        script.chmod(0o700)

    def _source_cli(self, arguments, target):
        identity = target if "CMUX_AGENT_LAUNCH_KIND" in target else self.environment(target)
        environment = {**os.environ, **identity, "HOME": str(self.home),
                       "PATH": str(self.cmux_bin) + os.pathsep + os.environ["PATH"]}
        with self._synthetic_worker():
            return subprocess.run([str(self.launcher_dir / "hailing-frequencies"), *arguments],
                                  capture_output=True, text=True, env=environment, timeout=15)

    @contextlib.contextmanager
    def _synthetic_worker(self):
        locks = []
        try:
            for name in ("supervisor.lock", "serve.lock"):
                stream = (self.state / name).open("a")
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locks.append(stream)
            yield
        finally:
            for stream in reversed(locks):
                fcntl.flock(stream, fcntl.LOCK_UN)
                stream.close()

    def _set_owner_worktree(self, target, path):
        source = self.state / "claude-hook-sessions.json"
        store = json.loads(source.read_text())
        store["sessions"][target["session"]]["cwd"] = str(path)
        hf.atomic_json(source, store)
        cmux_source = self.cmux_home / "claude-hook-sessions.json"
        cmux_store = json.loads(cmux_source.read_text())
        cmux_store["sessions"][target["session"]]["cwd"] = str(path)
        hf.atomic_json(cmux_source, cmux_store)

    def _set_timer(self, target=None, replacement_key=CHECKPOINT_IDLE_KEY, continuation="Checkpoint reminder"):
        target = target or self.old
        environment = self.environment(target)
        with patch.object(self.beacon, "worker_leases_held", return_value=True), patch.dict(
                os.environ, environment, clear=False):
            return self.beacon.set(after_idle="50m", continuation=continuation,
                                   request_id=str(uuid.uuid4()), replacement_key=replacement_key)["timer"]

    def _preview(self, timer_ids, authorization_ref=AUTHORIZATION_REF, environment=None):
        args = ["temporal", "retirement", "preview"]
        for timer_id in timer_ids:
            args.extend(("--timer-id", timer_id))
        args.extend(("--authorization-ref", authorization_ref))
        result = self._source_cli(args, environment or self.coordinator)
        if result.returncode:
            raise ValueError(json.loads(result.stderr)["error"])
        if result.stderr:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def _apply(self, preview, authorization_ref=AUTHORIZATION_REF, confirmation=None):
        args = ["temporal", "retirement", "apply"]
        for timer_id in preview["timer_ids"]:
            args.extend(("--timer-id", timer_id))
        args.extend(("--confirm", confirmation or preview["digest_sha256"],
                     "--authorization-ref", authorization_ref))
        result = self._source_cli(args, self.coordinator)
        if result.returncode:
            raise ValueError(json.loads(result.stderr)["error"])
        if result.stderr:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def _occurrence(self, timer_id, sequence, status):
        occurrence_id = str(uuid.uuid4())
        now = self.clock.value - sequence
        attempted = now if status in ("submitted", "uncertain") else None
        received = now if status == "received" else None
        with self.beacon.locked(), contextlib.closing(self.beacon.database()) as db:
            db.execute("INSERT INTO timer_occurrences "
                       "(id,timer_id,sequence,due_at,status,continuation,attempted_at,received_at,detail) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (occurrence_id, timer_id, sequence, now, status, "private continuation",
                        attempted, received, "synthetic detail"))
        return occurrence_id

    def _retirement_tables(self):
        with contextlib.closing(self.beacon.database()) as db:
            tombstones = db.execute("SELECT COUNT(*) FROM retired_checkpoint_timer_keys").fetchone()[0]
            receipts = db.execute("SELECT COUNT(*) FROM timer_retirement_receipts").fetchone()[0]
        return tombstones, receipts

    def _statuses(self, timer_id):
        with contextlib.closing(self.beacon.database()) as db:
            timer = db.execute("SELECT status FROM timers WHERE id=?", (timer_id,)).fetchone()[0]
            occurrences = db.execute("SELECT sequence,status,attempted_at,received_at,detail "
                                     "FROM timer_occurrences WHERE timer_id=? ORDER BY sequence",
                                     (timer_id,)).fetchall()
        return timer, occurrences

    def test_preview_apply_cancels_only_unsent_occurrences_and_retry_is_idempotent(self):
        timer = self._set_timer()
        statuses = ["pending", "held", "submitted", "submitted", "uncertain", "received", "canceled", "expired"]
        for sequence, status in enumerate(statuses, 1):
            self._occurrence(timer["id"], sequence, status)
        before = self._statuses(timer["id"])[1]
        preview = self._preview([timer["id"]])
        result = self._apply(preview)
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["canceled_occurrences"], {"pending": 1, "held": 1})
        self.assertEqual(result["preserved_occurrences"], {"submitted": 2, "uncertain": 1,
                                                             "received": 1, "canceled": 1, "expired": 1})
        self.assertFalse(result["submitted_input_recallable"])
        self.assertFalse(result["preserved_submitted_occurrences_retrievable"])
        after_timer, after = self._statuses(timer["id"])
        self.assertEqual(after_timer, "canceled")
        self.assertEqual([row[1] for row in after], ["canceled", "canceled", *statuses[2:]])
        for index in range(2, len(statuses)):
            self.assertEqual(after[index][2:], before[index][2:])
        repeated = self._apply(preview)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["timer_ids"], [timer["id"]])
        self.assertEqual(self._retirement_tables(), (1, 1))

    def test_preview_binds_nested_worktree_alias_and_blocks_installed_pb_rearm(self):
        source_app = Path(hf.__file__).resolve().parent
        old_hailing = self.launcher_dir / "hailing-frequencies"
        old_temporal = self.launcher_dir / "temporal-beacon"
        old_hailing.write_text("#!/bin/sh\n" + hf.INSTALL_MARKER + "\nexit 0\n")
        old_temporal.write_text("#!/bin/sh\n" + hf_install.BEACON_MARKER + "\nexit 0\n")
        old_hailing.chmod(0o755)
        old_temporal.chmod(0o755)
        standalone_target = self.home / "standalone-agent-notify"
        standalone_target.write_text("#!/bin/sh\nprintf 'standalone command remains active\\n'\n")
        standalone_target.chmod(0o755)
        standalone = self.launcher_dir / "agent-notify"
        standalone.symlink_to(standalone_target)

        def companion_snapshot():
            link = standalone.lstat()
            target_info = standalone_target.stat()
            link_metadata = (link.st_dev, link.st_ino, link.st_mode, link.st_uid, link.st_gid,
                             link.st_mtime_ns, link.st_ctime_ns)
            target_metadata = (target_info.st_dev, target_info.st_ino, target_info.st_mode,
                               target_info.st_uid, target_info.st_gid, target_info.st_mtime_ns,
                               target_info.st_ctime_ns)
            return standalone.readlink(), link_metadata, target_metadata, standalone.read_bytes()

        companion_before = companion_snapshot()
        runtime_root = self.home / ".local" / "share" / "starfleet-ai" / "hailing-frequencies"
        install_run = subprocess.run(
            [sys.executable, str(source_app / "install.py"), "--runtime-root", str(runtime_root),
             "--bin-dir", str(self.launcher_dir), "--skip-agent-notify"],
            capture_output=True, text=True, env={**os.environ, "HOME": str(self.home)}, timeout=15)
        self.assertEqual(install_run.returncode, 0, install_run.stderr)
        self.assertEqual(install_run.stderr, "")
        installation = json.loads(install_run.stdout)
        self.assertTrue(installation["agent_notify_skipped"])
        self.assertIsNone(installation["agent_notify_command"])
        runtime = Path(installation["runtime"])
        self.assertIn(str(runtime / "hailing_frequencies.py"), old_hailing.read_text())
        self.assertIn(str(runtime / "hailing_frequencies.py"), old_temporal.read_text())
        self.assertEqual(companion_snapshot(), companion_before)

        alias = self.worktree / ".context-injector" / "checkpoints" / "historical-owner"
        alias.mkdir(parents=True)
        self._set_owner_worktree(self.old, alias)
        gitdir = subprocess.check_output(["git", "-C", str(self.worktree), "rev-parse",
                                          "--absolute-git-dir"], text=True).strip()
        checkpoint = Path(gitdir) / "context-injector" / "checkpoints" / "historical-owner"
        checkpoint.mkdir(parents=True)
        state_path = checkpoint / "state.json"
        note_path = checkpoint / "checkpoint.md"
        state_path.write_text(json.dumps({"idle_warning_minutes": 50, "enabled": True,
                                          "reminders": True, "checkpoint_path": str(note_path)}))
        note_path.write_text("Reviewed checkpoint remains unchanged.\n")
        state_before, note_before = state_path.read_bytes(), note_path.read_bytes()
        producer_state = {"idle_warning_minutes": 50, "enabled": True, "reminders": True,
                          "checkpoint_path": str(note_path), "draft_path": str(checkpoint / "draft.md"),
                          "identity": {"root": str(self.worktree)}}
        home_env = {**self.owner_env, "HOME": str(self.home),
                    "PATH": str(self.cmux_bin) + os.pathsep + os.environ["PATH"]}
        with self._synthetic_worker(), patch.dict(os.environ, home_env, clear=False):
            pb_checkpoint._arm_idle_timer(producer_state, "claude", self.old["session"], "prompt")
        self.assertEqual(producer_state["idle_timer"]["status"], "armed",
                         producer_state["idle_timer"].get("detail"))
        timer_id = producer_state["idle_timer"]["timer_id"]
        self.assertTrue(timer_id)
        self.assertEqual(self._statuses(timer_id)[0], "active")

        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                    f"pattern-buffer-idle:{self.old['session']}:prompt:50"))
        message = (f"You have been idle for 50 minutes. Automatic compaction may occur in about five minutes; "
                   "this timing is an estimate, and waking can reset the idle clock. "
                   f"Review {producer_state['checkpoint_path']} and refresh {producer_state['draft_path']} now. "
                   f"{pb_checkpoint.SNAPSHOT_PRUNING} Do not start product work merely to service this reminder.")
        positive = self._source_cli(["temporal", "set", "--after-idle", "50m", "--message", message,
                                     "--request-id", request_id, "--replace-key", CHECKPOINT_IDLE_KEY], self.old)
        self.assertEqual(positive.returncode, 0, positive.stderr)
        self.assertEqual(positive.stderr, "")
        positive_result = json.loads(positive.stdout)
        self.assertEqual(positive_result["timer"]["id"], timer_id)
        self.assertTrue(positive_result["idempotent"])

        with contextlib.closing(self.beacon.database()) as db:
            active_rows = db.execute(
                "SELECT id,canonical_worktree FROM timers WHERE harness=? AND session=? AND surface=? AND workspace=? "
                "AND started_at=? AND replacement_key=? AND status='active'",
                (self.old["harness"], self.old["session"], self.old["surface"], self.old["workspace"],
                 self.old["started_at"], CHECKPOINT_IDLE_KEY)).fetchall()
            active_ids = sorted(timer_id for timer_id, worktree in active_rows
                                if self.beacon._git_identity(worktree)[0] == str(self.worktree.resolve()))
        self.assertEqual(active_ids, [timer_id])
        preview_args = ["temporal", "retirement", "preview"]
        for active_id in active_ids:
            preview_args.extend(("--timer-id", active_id))
        preview_args.extend(("--authorization-ref", AUTHORIZATION_REF))
        planned = self._source_cli(preview_args, self.coordinator)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(planned.stderr, "")
        preview = json.loads(planned.stdout)
        self.assertEqual(preview["timer_ids"], active_ids)
        self.assertEqual(preview["timers"][0]["stored_worktree_alias"], str(alias.resolve()))
        self.assertEqual(preview["timers"][0]["git"]["worktree_root"], str(self.worktree.resolve()))
        self.assertNotIn(message, planned.stdout)
        self.assertFalse((self.state / "retirement-plans").exists())

        apply_args = ["temporal", "retirement", "apply"]
        for active_id in active_ids:
            apply_args.extend(("--timer-id", active_id))
        apply_args.extend(("--confirm", preview["digest_sha256"],
                           "--authorization-ref", AUTHORIZATION_REF))
        applied = self._source_cli(apply_args, self.coordinator)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(applied.stderr, "")
        result = json.loads(applied.stdout)
        self.assertEqual(result["timer_ids"], active_ids)
        self.assertFalse(result["idempotent"])
        self.assertEqual(self._statuses(timer_id)[0], "canceled")
        retried = self._source_cli(apply_args, self.coordinator)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(retried.stderr, "")
        retry_result = json.loads(retried.stdout)
        self.assertTrue(retry_result["idempotent"])
        self.assertEqual(retry_result["digest_sha256"], preview["digest_sha256"])

        self._set_owner_worktree(self.old, self.worktree)
        count_before_refusal = self._timer_count()
        refused = self._source_cli(["temporal", "set", "--after-idle", "50m", "--message", message,
                                    "--request-id", request_id, "--replace-key", CHECKPOINT_IDLE_KEY],
                                   self.old)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, "")
        self.assertEqual(json.loads(refused.stderr)["error"],
                         "This exact checkpoint owner and Git worktree retired the idle timer key")
        with self._synthetic_worker(), patch.dict(os.environ, home_env, clear=False):
            pb_checkpoint._arm_idle_timer(producer_state, "claude", self.old["session"], "prompt")
        self.assertEqual(producer_state["idle_timer"]["status"], "unavailable")
        self.assertEqual(producer_state["idle_timer"]["pending_request_id"], request_id)
        self.assertEqual(self._timer_count(), count_before_refusal)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(note_path.read_bytes(), note_before)
        self.assertEqual(companion_snapshot(), companion_before)

        unrelated = self._source_cli(
            ["temporal", "set", "--after", "10m", "--message", "Unrelated timer",
             "--request-id", str(uuid.uuid4()), "--replace-key", "unrelated"], self.old)
        self.assertEqual(unrelated.returncode, 0, unrelated.stderr)
        self.assertEqual(json.loads(unrelated.stdout)["timer"]["replacement_key"], "unrelated")
        successor = self._source_cli(
            ["temporal", "set", "--after-idle", "50m", "--message", "Successor checkpoint reminder",
             "--request-id", str(uuid.uuid4()), "--replace-key", CHECKPOINT_IDLE_KEY], self.successor)
        self.assertEqual(successor.returncode, 0, successor.stderr)
        self.assertEqual(json.loads(successor.stdout)["timer"]["status"], "active")
        self.assertEqual(companion_snapshot(), companion_before)

    def _timer_count(self):
        with contextlib.closing(self.beacon.database()) as db:
            return db.execute("SELECT COUNT(*) FROM timers").fetchone()[0]

    def test_coordinator_identity_and_authorization_reference_are_required(self):
        timer = self._set_timer()
        owner_preview = self._source_cli(
            ["temporal", "retirement", "preview", "--timer-id", timer["id"],
             "--authorization-ref", AUTHORIZATION_REF], self.old)
        self.assertEqual(owner_preview.returncode, 1)
        self.assertEqual(owner_preview.stdout, "")
        self.assertIn("currently enrolled Hailing Frequencies coordinator", owner_preview.stderr)
        wrong_identity = {**self.coordinator_env, "CMUX_SURFACE_ID": str(uuid.uuid4()).upper()}
        identity_preview = self._source_cli(
            ["temporal", "retirement", "preview", "--timer-id", timer["id"],
             "--authorization-ref", AUTHORIZATION_REF], wrong_identity)
        self.assertEqual(identity_preview.returncode, 1)
        self.assertEqual(identity_preview.stdout, "")
        self.assertIn("Caller harness or surface differs", identity_preview.stderr)
        preview = self._preview([timer["id"]])
        wrong_reference = self._source_cli(
            ["temporal", "retirement", "apply", "--timer-id", timer["id"],
             "--confirm", preview["digest_sha256"], "--authorization-ref", "different-reference"],
            self.coordinator)
        self.assertEqual(wrong_reference.returncode, 1)
        self.assertEqual(wrong_reference.stdout, "")
        self.assertIn("changed after preview", wrong_reference.stderr)
        self.assertEqual(self._statuses(timer["id"])[0], "active")
        self.assertEqual(self._retirement_tables(), (0, 0))
        invalid_digest = self._source_cli(
            ["temporal", "retirement", "apply", "--timer-id", timer["id"],
             "--confirm", "not-a-digest", "--authorization-ref", AUTHORIZATION_REF],
            self.coordinator)
        self.assertEqual(invalid_digest.returncode, 1)
        self.assertEqual(invalid_digest.stdout, "")
        self.assertIn("Confirm the exact preview digest", invalid_digest.stderr)
        self.assertEqual(self._statuses(timer["id"])[0], "active")

        missing_authorization = self._source_cli(
            ["temporal", "retirement", "preview", "--timer-id", timer["id"]], self.coordinator)
        self.assertEqual(missing_authorization.returncode, 2)
        self.assertEqual(missing_authorization.stdout, "")
        self.assertIn("--authorization-ref", missing_authorization.stderr)
        self.assertEqual(self._statuses(timer["id"])[0], "active")

    def test_apply_reads_current_coordinator_identity_inside_locks(self):
        timer = self._set_timer()
        preview = self._preview([timer["id"]])
        with patch.dict(os.environ, self.coordinator_env, clear=False):
            coordinator = self.beacon._retirement_coordinator()
        changed = {**coordinator, "started_at": coordinator["started_at"] + 1}
        with patch.object(tb.TemporalBeacon, "_retirement_coordinator",
                          side_effect=[changed]) as identity:
            with self.assertRaisesRegex(ValueError, "changed after preview"):
                self.beacon.apply_retirement(preview["timer_ids"], preview["digest_sha256"],
                                             AUTHORIZATION_REF)
        self.assertEqual(identity.call_count, 1)
        self.assertEqual(self._statuses(timer["id"])[0], "active")
        self.assertEqual(self._retirement_tables(), (0, 0))

    def test_changed_timer_record_refuses_all_mutation(self):
        first = self._set_timer()
        second_alias = self.worktree / ".context-injector" / "checkpoints" / "second-owner-path"
        second_alias.mkdir(parents=True)
        self._set_owner_worktree(self.old, second_alias)
        second = self._set_timer()
        preview = self._preview([first["id"], second["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timers SET due_at=due_at+1 WHERE id=?", (second["id"],))
        with self.assertRaisesRegex(ValueError, "changed after preview"):
            self._apply(preview)
        self.assertEqual(self._retirement_tables(), (0, 0))
        self.assertEqual(self._statuses(first["id"])[0], "active")

        owner_timer = self._set_timer(target=self.successor)
        owner_preview = self._preview([owner_timer["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timers SET surface=? WHERE id=?",
                       (str(uuid.uuid4()).upper(), owner_timer["id"]))
        with self.assertRaisesRegex(ValueError, "owner snapshot does not match"):
            self._apply(owner_preview)

        status_timer = self._set_timer(target=self.coordinator)
        status_preview = self._preview([status_timer["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timers SET status='canceled' WHERE id=?", (status_timer["id"],))
        with self.assertRaisesRegex(ValueError, "Only active Pattern Buffer"):
            self._apply(status_preview)
        self.assertEqual(self._retirement_tables(), (0, 0))
        self.assertEqual(self._statuses(owner_timer["id"])[0], "active")

    def test_changed_occurrence_state_refuses_all_mutation(self):
        first = self._set_timer()
        second = self._set_timer(target=self.successor)
        occurrence_id = self._occurrence(first["id"], 1, "held")
        preview = self._preview([first["id"], second["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timer_occurrences SET status='submitted' WHERE id=?", (occurrence_id,))
        with self.assertRaisesRegex(ValueError, "changed after preview"):
            self._apply(preview)
        self.assertEqual(self._retirement_tables(), (0, 0))
        self.assertEqual(self._statuses(first["id"])[0], "active")
        self.assertEqual(self._statuses(second["id"])[0], "active")
        self.assertEqual(self._statuses(first["id"])[1][0][1], "submitted")

    def test_changed_active_timer_set_refuses_all_mutation(self):
        timer = self._set_timer()
        preview = self._preview([timer["id"]])
        replacement = self._set_timer()
        with self.assertRaisesRegex(ValueError, "Only active Pattern Buffer"):
            self._apply(preview)
        self.assertEqual(self._statuses(timer["id"])[0], "canceled")
        self.assertEqual(self._statuses(replacement["id"])[0], "active")
        self.assertEqual(self._retirement_tables(), (0, 0))

    def test_changed_git_identity_and_confirmation_refuse_apply(self):
        timer = self._set_timer()
        preview = self._preview([timer["id"]])
        old_repo = self.base / "repo-before-replacement"
        self.worktree.rename(old_repo)
        self.worktree.mkdir()
        subprocess.run(["git", "-C", str(self.worktree), "init", "-q"], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "changed after preview"):
            self._apply(preview)
        self.assertEqual(self._retirement_tables(), (0, 0))

        fresh = self._set_timer()
        fresh_preview = self._preview([fresh["id"]])
        with self.assertRaisesRegex(ValueError, "changed after preview"):
            self._apply(fresh_preview, authorization_ref="modified-reference")
        self.assertEqual(self._statuses(fresh["id"])[0], "active")

    def test_retired_owner_cannot_rearm_pb_key_but_successor_and_other_keys_can(self):
        timer = self._set_timer()
        preview = self._preview([timer["id"]])
        self._apply(preview)
        with patch.dict(os.environ, self.owner_env, clear=False), self.assertRaisesRegex(
                ValueError, "retired the idle timer key"):
            self.beacon.set(after="10m", continuation="Retry the checkpoint reminder",
                            request_id=str(uuid.uuid4()), replacement_key=CHECKPOINT_IDLE_KEY)
        with patch.object(self.beacon, "worker_leases_held", return_value=True), patch.dict(
                os.environ, self.owner_env, clear=False):
            unrelated = self.beacon.set(after="10m", continuation="Unrelated timer",
                                        request_id=str(uuid.uuid4()), replacement_key="unrelated")
        with patch.object(self.beacon, "worker_leases_held", return_value=True), patch.dict(
                os.environ, self.successor_env, clear=False):
            successor = self.beacon.set(after_idle="50m", continuation="Successor checkpoint reminder",
                                        request_id=str(uuid.uuid4()), replacement_key=CHECKPOINT_IDLE_KEY)
        self.assertEqual(unrelated["timer"]["status"], "active")
        self.assertEqual(successor["timer"]["status"], "active")
        self.assertEqual(self._retirement_tables(), (1, 1))

    def test_pb_timer_key_requires_git_identity_at_set_boundary(self):
        self._set_owner_worktree(self.old, self.base)
        other_repo = self.base / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "-C", str(other_repo), "init", "-q"], check=True, capture_output=True)
        git_dir = subprocess.check_output(["git", "-C", str(other_repo), "rev-parse",
                                           "--absolute-git-dir"], text=True).strip()
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text(f"#!{sys.executable}\nprint({str(other_repo)!r})\nprint({git_dir!r})\n")
        fake_git.chmod(0o700)
        with patch.dict(os.environ, {**self.owner_env,
                                     "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
                        clear=False), self.assertRaisesRegex(
                ValueError, "outside its Git worktree"):
            self.beacon.set(after="10m", continuation="Invalid worktree scope",
                            request_id=str(uuid.uuid4()), replacement_key=CHECKPOINT_IDLE_KEY)
        self.assertEqual(self._retirement_tables(), (0, 0))

    def test_preview_refuses_a_recorded_path_outside_its_git_root(self):
        timer = self._set_timer()
        other_repo = self.base / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "-C", str(other_repo), "init", "-q"], check=True, capture_output=True)
        git_dir = subprocess.check_output(["git", "-C", str(other_repo), "rev-parse",
                                           "--absolute-git-dir"], text=True).strip()
        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text(f"#!{sys.executable}\nprint({str(other_repo)!r})\nprint({git_dir!r})\n")
        fake_git.chmod(0o700)
        with patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}, clear=False):
            with self.assertRaisesRegex(ValueError, "outside its Git worktree"):
                self._preview([timer["id"]])
        self.assertEqual(self._statuses(timer["id"])[0], "active")
        self.assertEqual(self._retirement_tables(), (0, 0))


if __name__ == "__main__":
    unittest.main()
