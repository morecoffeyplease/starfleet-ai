import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
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
        self.state = self.base / "hailing-state"
        self.state.mkdir(mode=0o700)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
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
                                       "startedAt": item["started_at"], "cwd": str(self.worktree)}
                    for item in (self.old, self.coordinator, self.successor)}
        hf.atomic_json(self.state / "claude-hook-sessions.json", {"sessions": sessions})
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
        script.write_text("#!/usr/bin/env python3\n" +
            "import json, os, sys\n" +
            f"sys.path.insert(0, {str(source_root)!r})\n" +
            "from pathlib import Path\n" +
            "import hailing_frequencies as hf\n" +
            "import temporal_beacon as tb\n" +
            "tb.TemporalBeacon.worker_leases_held=lambda self: True\n" +
            "class Native:\n" +
            f" def __init__(self, executable='cmux', hook_directory=None): self.hook_directory=Path(hook_directory or {str(self.state)!r})\n" +
            " def binding(self, target):\n" +
            "  store=json.loads((self.hook_directory/(target['harness']+'-hook-sessions.json')).read_text())\n" +
            "  record=next((value for key,value in store['sessions'].items() if key.upper()==target['session'].upper()),None)\n" +
            "  if record is None or record.get('surfaceId','').upper()!=target['surface'] or record.get('workspaceId','').upper()!=target['workspace'] or record.get('startedAt')!=target['started_at']: raise ValueError('Synthetic exact-session binding changed')\n" +
            "  return True\n" +
            " def activity(self, target):\n" +
            "  import datetime, time\n" +
            "  at=datetime.datetime.fromtimestamp(time.time()-60,datetime.timezone.utc).isoformat()\n" +
            "  return {'state':'idle','last_activity_at':at}\n" +
            f"result=hf.main(['--state-dir', {str(self.state)!r}, *sys.argv[1:]])\n" +
            "print(json.dumps(result))\n")
        script.chmod(0o700)

    def _set_owner_worktree(self, target, path):
        source = self.state / "claude-hook-sessions.json"
        store = json.loads(source.read_text())
        store["sessions"][target["session"]]["cwd"] = str(path)
        hf.atomic_json(source, store)

    def _set_timer(self, target=None, replacement_key=CHECKPOINT_IDLE_KEY, continuation="Checkpoint reminder"):
        target = target or self.old
        environment = self.environment(target)
        with patch.object(self.beacon, "worker_leases_held", return_value=True), patch.dict(
                os.environ, environment, clear=False):
            return self.beacon.set(after_idle="50m", continuation=continuation,
                                   request_id=str(uuid.uuid4()), replacement_key=replacement_key)["timer"]

    def _cli(self, args, environment=None):
        with patch.object(hf, "Cmux", return_value=self.native), patch.dict(
                os.environ, environment or self.coordinator_env, clear=False):
            return hf.main(["--state-dir", str(self.state), "temporal", *args])

    def _plan(self, timer_ids, authorization_ref=AUTHORIZATION_REF, environment=None):
        args = ["retirement", "plan"]
        for timer_id in timer_ids:
            args.extend(("--timer-id", timer_id))
        args.extend(("--authorization-ref", authorization_ref))
        return self._cli(args, environment)

    def _apply(self, plan, authorization_ref=AUTHORIZATION_REF, confirmation=None):
        return self._cli(["retirement", "apply", "--plan", plan["plan"], "--confirm",
                          confirmation or plan["sha256"], "--authorization-ref", authorization_ref])

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

    def test_plan_apply_cancels_only_unsent_occurrences_and_retry_is_idempotent(self):
        timer = self._set_timer()
        statuses = ["pending", "held", "submitted", "submitted", "uncertain", "received", "canceled", "expired"]
        for sequence, status in enumerate(statuses, 1):
            self._occurrence(timer["id"], sequence, status)
        before = self._statuses(timer["id"])[1]
        plan = self._plan([timer["id"]])
        result = self._apply(plan)
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
        repeated = self._apply(plan)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["timer_ids"], [timer["id"]])
        self.assertEqual(self._retirement_tables(), (1, 1))

    def test_plan_binds_nested_worktree_alias_and_blocks_source_pb_rearm(self):
        alias = self.worktree / ".context-injector" / "checkpoints" / "historical-owner"
        alias.mkdir(parents=True)
        self._set_owner_worktree(self.old, alias)
        timer = self._set_timer()
        plan = self._plan([timer["id"]])
        document = json.loads(Path(plan["plan"]).read_text())
        self.assertEqual(document["timers"][0]["owner"]["canonical_worktree"], str(alias.resolve()))
        self.assertEqual(document["timers"][0]["worktree_root"], str(self.worktree.resolve()))
        self.assertNotIn("private continuation", Path(plan["plan"]).read_text())
        self._apply(plan)
        self._set_owner_worktree(self.old, self.worktree)

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
        home_env = {**self.owner_env, "HOME": str(self.home)}
        with patch.dict(os.environ, home_env, clear=False):
            pb_checkpoint._arm_idle_timer(producer_state, "claude", self.old["session"], "prompt")
        self.assertEqual(producer_state["idle_timer"]["status"], "unavailable")
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(note_path.read_bytes(), note_before)
        self.assertEqual(self._statuses(timer["id"])[0], "canceled")

    def test_coordinator_identity_and_authorization_reference_are_required(self):
        timer = self._set_timer()
        with self.assertRaisesRegex(ValueError, "currently enrolled.*coordinator"):
            self._plan([timer["id"]], environment=self.owner_env)
        wrong_identity = {**self.coordinator_env, "CMUX_SURFACE_ID": str(uuid.uuid4()).upper()}
        with self.assertRaisesRegex(ValueError, "Caller harness or surface differs"):
            self._plan([timer["id"]], environment=wrong_identity)
        plan = self._plan([timer["id"]])
        with self.assertRaisesRegex(ValueError, "Authorization reference differs"):
            self._apply(plan, authorization_ref="different-reference")
        self.assertEqual(self._statuses(timer["id"])[0], "active")
        self.assertEqual(self._retirement_tables(), (0, 0))
        with self.assertRaisesRegex(ValueError, "exact retirement plan SHA-256"):
            self._apply(plan, confirmation="0" * 64)
        self.assertEqual(self._statuses(timer["id"])[0], "active")

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._cli(["retirement", "apply", "--plan", plan["plan"],
                       "--authorization-ref", AUTHORIZATION_REF])
        self.assertEqual(self._statuses(timer["id"])[0], "active")

    def test_coordinator_identity_is_rechecked_under_the_timer_lock(self):
        timer = self._set_timer()
        plan = self._plan([timer["id"]])
        with patch.dict(os.environ, self.coordinator_env, clear=False):
            coordinator = self.beacon._retirement_coordinator()
        changed = tb.OwnerSnapshot(coordinator.harness, coordinator.session, coordinator.surface,
                                   coordinator.workspace, coordinator.started_at + 1,
                                   coordinator.owner_key, coordinator.canonical_worktree)
        with patch.object(tb.TemporalBeacon, "_retirement_coordinator",
                          side_effect=[coordinator, changed]) as identity:
            with self.assertRaisesRegex(ValueError, "Coordinator identity changed"):
                self._apply(plan)
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(self._statuses(timer["id"])[0], "active")
        self.assertEqual(self._retirement_tables(), (0, 0))

    def test_changed_timer_record_refuses_all_mutation(self):
        first = self._set_timer()
        second_alias = self.worktree / ".context-injector" / "checkpoints" / "second-owner-path"
        second_alias.mkdir(parents=True)
        self._set_owner_worktree(self.old, second_alias)
        second = self._set_timer()
        plan = self._plan([first["id"], second["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timers SET due_at=due_at+1 WHERE id=?", (second["id"],))
        with self.assertRaisesRegex(ValueError, "state changed after plan review"):
            self._apply(plan)
        self.assertEqual(self._retirement_tables(), (0, 0))
        self.assertEqual(self._statuses(first["id"])[0], "active")

        owner_timer = self._set_timer(target=self.successor)
        owner_plan = self._plan([owner_timer["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timers SET surface=? WHERE id=?",
                       (str(uuid.uuid4()).upper(), owner_timer["id"]))
        with self.assertRaisesRegex(ValueError, "owner snapshot does not match"):
            self._apply(owner_plan)

        status_timer = self._set_timer(target=self.coordinator)
        status_plan = self._plan([status_timer["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timers SET status='canceled' WHERE id=?", (status_timer["id"],))
        with self.assertRaisesRegex(ValueError, "Timer or occurrence state changed"):
            self._apply(status_plan)
        self.assertEqual(self._retirement_tables(), (0, 0))
        self.assertEqual(self._statuses(owner_timer["id"])[0], "active")

    def test_changed_occurrence_state_refuses_all_mutation(self):
        first = self._set_timer()
        second = self._set_timer(target=self.successor)
        occurrence_id = self._occurrence(first["id"], 1, "held")
        plan = self._plan([first["id"], second["id"]])
        with contextlib.closing(self.beacon.database()) as db:
            db.execute("UPDATE timer_occurrences SET status='submitted' WHERE id=?", (occurrence_id,))
        with self.assertRaisesRegex(ValueError, "Timer or occurrence state changed"):
            self._apply(plan)
        self.assertEqual(self._retirement_tables(), (0, 0))
        self.assertEqual(self._statuses(first["id"])[0], "active")
        self.assertEqual(self._statuses(second["id"])[0], "active")
        self.assertEqual(self._statuses(first["id"])[1][0][1], "submitted")

    def test_changed_active_timer_set_refuses_all_mutation(self):
        timer = self._set_timer()
        plan = self._plan([timer["id"]])
        replacement = self._set_timer()
        with self.assertRaisesRegex(ValueError, "state changed after plan review"):
            self._apply(plan)
        self.assertEqual(self._statuses(timer["id"])[0], "canceled")
        self.assertEqual(self._statuses(replacement["id"])[0], "active")
        self.assertEqual(self._retirement_tables(), (0, 0))

    def test_changed_git_identity_and_plan_bytes_refuse_apply(self):
        timer = self._set_timer()
        plan = self._plan([timer["id"]])
        old_repo = self.base / "repo-before-replacement"
        self.worktree.rename(old_repo)
        self.worktree.mkdir()
        subprocess.run(["git", "-C", str(self.worktree), "init", "-q"], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "Timer or occurrence state changed"):
            self._apply(plan)
        self.assertEqual(self._retirement_tables(), (0, 0))

        fresh = self._set_timer()
        fresh_plan = self._plan([fresh["id"]])
        plan_path = Path(fresh_plan["plan"])
        plan_path.write_text(plan_path.read_text().replace(AUTHORIZATION_REF, "modified"))
        with self.assertRaisesRegex(ValueError, "digest is invalid"):
            self._apply(fresh_plan)
        self.assertEqual(self._statuses(fresh["id"])[0], "active")

    def test_retired_owner_cannot_rearm_pb_key_but_successor_and_other_keys_can(self):
        timer = self._set_timer()
        plan = self._plan([timer["id"]])
        self._apply(plan)
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


if __name__ == "__main__":
    unittest.main()
