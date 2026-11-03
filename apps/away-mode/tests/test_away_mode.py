import concurrent.futures
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "away_mode.py"
spec = importlib.util.spec_from_file_location("away", SCRIPT)
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)


class AwayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="away-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = am.App(self.root / "state")
        self.call("init", "--mp-config-dir", str(self.root / "config"), "--mp-state-dir", str(self.root / "mp-state"))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            self.git(self.repo, *args)
        (self.repo / "roadmap.md").write_text("Fixture roadmap\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-qm", "fixture")
        self.lane = {"workspace": str(uuid.uuid4()), "surface": str(uuid.uuid4()), "session": "fixture-session", "harness": "codex"}
        self.inventory = {"truncated": False, "items": [{"lifecycle": "running", "freshness": {"state": "current"},
                          "projections": [{"workspace_id": self.lane["workspace"], "panel_id": self.lane["surface"]}],
                          "agents": [{"session_id": self.lane["session"], "kind": "codex", "state": "idle"}]}]}
        self.current = patch.object(am.App, "current", return_value=self.inventory)
        self.current.start()
        self.addCleanup(self.current.stop)
        self.call("enroll", "fixture", "--worktree", str(self.repo), *[x for k, v in self.lane.items() for x in ("--" + k, v)])
        self.update = {"attention": ["Approve the next trial."], "items": [{
            "roadmap_item": "Fixture recovery", "reference": "Issue #123", "progress": "Reproduction confirmed",
            "state": "Waiting", "next_step": "Run the approved trial", "blockers": [
                {"blocked": "Capacity trial", "unblocker": "user approval", "other_work": "Documentation can continue"}]}]}

    def git(self, root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT, text=True)

    def call(self, *args):
        return am.execute(am.parser().parse_args(["--state-dir", str(self.app.directory), *args]))

    def send(self, recipient, text):
        return {"status": "queued", "receipt": {"id": str(uuid.uuid4()), "recipient_surface_id": recipient["surface"],
                "recipient_workspace_id": recipient["workspace"], "body": text}, "wake": {"status": "submitted"}}

    def request(self, name="report-1"):
        return self.call("report", "--request-id", name, "--prepare-only")["report"]

    def submit(self, report):
        source = self.root / "update.json"
        source.write_text(json.dumps(self.update))
        caller = {"caller": {"workspace_id": self.lane["workspace"], "surface_id": self.lane["surface"]}}
        with patch.object(am.mp, "run_json", return_value=caller):
            return self.call("report-submit", report, "--lane", "fixture", "--file", str(source))

    def test_presence_window_survives_restart_and_duplicate_return(self):
        with patch.object(am.App, "send_action", side_effect=self.send) as sender:
            away = self.call("away", "--request-id", "away-1")
            start = self.app.read()["presence"]["started_at"]
            self.call("away", "--request-id", "away-2")
            self.assertEqual(self.app.read()["presence"]["started_at"], start)
            back = self.call("back", "--request-id", "back-1", "--prepare-only")
            again = self.call("back", "--request-id", "back-2", "--prepare-only")
            self.assertEqual(back["report"], again["report"])
            self.assertEqual(sender.call_count, 2)
            restarted = am.App(self.app.directory).read()
            self.assertEqual(restarted["reports"][back["report"]]["start"], start)
            self.assertEqual(restarted["reports"][back["report"]]["end"], restarted["presence"]["returned_at"])
            self.assertNotEqual(away["transition"], back["transition"])
            self.call("away", "--request-id", "away-3")
            self.call("back", "--request-id", "back-1", "--prepare-only")
            self.assertEqual(self.app.read()["presence"]["mode"], "away")
            self.assertEqual(sender.call_count, 3)

    def test_status_exact_two_hours_and_no_presence_change(self):
        before = self.app.read()["presence"]
        rid = self.request()
        report = self.call("report-show", rid)
        self.assertEqual(am.timestamp(report["end"]) - am.timestamp(report["start"]), dt.timedelta(hours=2))
        self.assertEqual(before, self.app.read()["presence"])
        self.assertEqual(rid, self.request())
        with self.assertRaises(am.Error):
            self.call("away", "--request-id", "report-1")

    def test_collect_retry_missing_stale_and_uncertain_delivery(self):
        rid = self.request()
        with patch.object(am.App, "send_action", side_effect=am.Error("transport response lost")) as sender:
            self.call("collect", rid)
            self.call("collect", rid)
            self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.app.read()["reports"][rid]["recipients"]["fixture"]["status"], "unknown")
        self.call("reconcile-send", "--report", rid, "--lane", "fixture", "--not-sent")
        with patch.object(am.App, "send_action", side_effect=self.send) as sender:
            self.call("collect", rid)
            self.call("collect", rid)
            self.assertEqual(sender.call_count, 1)
        other = self.request("report-2")
        stale = copy.deepcopy(self.inventory)
        stale["items"][0]["freshness"]["state"] = "stale"
        with patch.object(am.App, "current", return_value=stale), patch.object(am.App, "send_action") as sender:
            result = self.call("collect", other)
            sender.assert_not_called()
        self.assertEqual(result["recipients"]["fixture"]["status"], "unreachable")

    def test_owner_checks_schema_and_fixed_roster(self):
        rid = self.request()
        source = self.root / "bad.json"
        source.write_text('{"items": []}')
        with patch.object(am.mp, "run_json", return_value={"caller": {}}), self.assertRaises(am.Error):
            self.call("report-submit", rid, "--lane", "fixture", "--file", str(source))
        self.submit(rid)
        self.call("unenroll", "fixture")
        text = self.call("render", rid, "--details")
        self.assertIn("1 of 1 enrolled lanes responded", text)
        self.assertIn("user approval", text)
        self.assertIn("Documentation can continue", text)
        self.assertIn("Issue \\#123", text)
        self.assertLess(text.index("Needs your attention"), text.index("Roadmap progress"))
        self.assertIn("lane-reported", text)
        with self.assertRaises(am.Error):
            am.validate_update({"attention": [], "items": [{"roadmap_item": "missing fields"}]})

    def test_worktree_coverage_and_missing_response(self):
        child = self.root / "child"
        self.git(self.repo, "worktree", "add", "--detach", str(child), "HEAD")
        rid = self.request()
        report = self.call("report-show", rid)
        self.assertIn(str(child), report["coverage"]["without_enrolled_lane"])
        text = self.call("render", rid)
        self.assertIn("0 of 1", text)
        self.assertIn("No response yet", text)
        self.assertNotIn(am.cell(child), text)
        details = self.call("render", rid, "--details")
        self.assertIn(f"No enrolled agent for worktree: {am.cell(child)}", details)
        self.assertIn("blockers are unknown", text)

    def test_concurrent_returns_make_one_report(self):
        with patch.object(am.App, "send_action", side_effect=self.send):
            self.call("away", "--request-id", "away")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda n: self.call("back", "--request-id", f"back-{n}", "--prepare-only"), range(4)))
        self.assertEqual(len({r["report"] for r in results}), 1)

    def test_launch_uses_saved_choice_and_cannot_duplicate_uncertain_open(self):
        rid = self.request()
        choice = {"harness": "codex", "model": "gpt-6-luna", "effort": "high", "contributor_mode": False}
        with patch.object(am.mp, "run_json", side_effect=[choice, am.Error("lost response")]) as runner, \
                patch.object(am.mp.shutil, "which", return_value="codex"):
            launch = self.call("launch", rid)
            self.assertEqual(launch["status"], "opening_unconfirmed")
            self.assertEqual(self.call("launch", rid), launch)
            self.assertEqual(runner.call_count, 2)
            argv = runner.call_args_list[0].args[0]
            self.assertEqual(argv[-3:], ["resolve", "--role", "researcher"])
            self.assertIn("--focus", runner.call_args_list[1].args[0])
            self.assertEqual(Path(launch["cwd"]), self.app.directory)
            record = Path(launch["launch_file"])
            self.assertEqual(record.parent, self.app.directory / "briefings" / rid)
            native_argv = json.loads(record.read_text())["argv"]
            self.assertEqual(native_argv[native_argv.index("-C") + 1], str(self.app.directory))
        self.assertEqual(self.app.read()["reports"][rid]["launch"]["choice"], choice)

    def test_unavailable_model_leaves_launch_retryable_and_all_harness_arguments(self):
        rid = self.request()
        for harness, model in (("codex", "gpt-6-luna"), ("claude", "claude-haiku-5-5"), ("muse", "muse-spark-1.3-contributor")):
            choice = {"harness": harness, "model": model, "effort": "high", "contributor_mode": harness == "muse"}
            with patch.object(am.mp, "run_json", return_value=choice), patch.object(am.mp.shutil, "which", return_value=None), self.assertRaises(am.Error):
                self.call("launch", rid)
            self.assertEqual(self.app.read()["reports"][rid]["launch"]["status"], "not_started")
            with patch.object(am.mp.shutil, "which", return_value=harness):
                argv = am.mp.argv_for(choice, "brief", self.root, interactive=True)
            self.assertEqual(argv[0], harness)
            self.assertIn(model, argv)
            if harness == "muse":
                self.assertEqual(argv[argv.index("--contributor-mode") + 1], "on")

    def test_stale_ack_rejected_and_contact_route_remains_configurable(self):
        with patch.object(am.App, "send_action", side_effect=self.send):
            old = self.call("away", "--request-id", "away")
            text = self.call("context")["instructions"]
            self.assertIn("configured contact route", text)
            self.assertIn("No phrases are configured", text)
            self.call("back", "--request-id", "back", "--prepare-only")
        with self.assertRaises(am.Error):
            self.call("ack", old["transition"], "--lane", "fixture")

    def test_cli_real_subprocess_and_non_destructive_init(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(self.app.directory), "context"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["presence"]["mode"], "back")
        before = self.app.path.read_bytes()
        with self.assertRaises(am.Error):
            self.call("init")
        self.assertEqual(before, self.app.path.read_bytes())

    def test_muse_native_hook_binds_and_consumes_report_outbox_once(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["items"][0]["agents"] = []
        body = {"session_id": self.lane["session"], "hook_event_name": "SessionStart", "cwd": str(self.repo),
                "transcript_path": f"/fixture/sessions/{self.lane['session']}/session.jsonl"}
        original_run = am.mp.run_command

        def run(argv, stdin=None):
            if argv[0] == "/bin/ps":
                return "1 /fixture/muse-bin\n" if "ppid=,comm=" in argv else "Mon Oct 26 12:00:00 2026\n"
            return original_run(argv, stdin)

        def hook(event):
            body["hook_event_name"] = event
            with patch.object(sys, "stdin", io.StringIO(json.dumps(body))):
                return self.call("muse-hook", "--workspace", self.lane["workspace"], "--surface", self.lane["surface"], "--worktree", str(self.repo))

        with patch.dict(os.environ, {}, clear=False), patch.object(am.mp, "run_command", side_effect=run), \
                patch.object(am.App, "current", return_value=inventory):
            hook("SessionStart")
            child = dict(body, session_id="child", transcript_path=f"/fixture/sessions/{self.lane['session']}/subagent/child/session.jsonl")
            with patch.object(sys, "stdin", io.StringIO(json.dumps(child))):
                self.assertEqual(self.call("muse-hook", "--workspace", self.lane["workspace"], "--surface", self.lane["surface"], "--worktree", str(self.repo)), {})
            self.assertEqual(self.app.read()["muse_bindings"][self.lane["surface"].lower()]["session"], self.lane["session"])
            self.call("enroll", "fixture", "--worktree", str(self.repo), "--workspace", self.lane["workspace"],
                      "--surface", self.lane["surface"], "--session", self.lane["session"], "--harness", "muse")
            rid = self.request()
            context = hook("UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
            self.assertIn(rid, context)
            directory = self.repo / ".away-mode/outbox"
            directory.mkdir(parents=True)
            (directory / "00-malformed.json").write_text("not JSON")
            (directory / "report.json").write_text(json.dumps({"action": "report-submit", "report": rid, "update": self.update}))
            result = hook("PostToolUse")
            self.assertIn("Expected a JSON payload", result["hookSpecificOutput"]["additionalContext"])
            first = self.app.read()["reports"][rid]["responses"]["fixture"]
            hook("PostToolUse")
            self.assertEqual(self.app.read()["reports"][rid]["responses"]["fixture"], first)
            self.assertIn("native Muse hook", first["evidence"])

    def test_muse_hook_config_is_explicit_and_does_not_install(self):
        before = sorted(self.repo.rglob("*"))
        config = self.call("muse-hook-config", "--workspace", self.lane["workspace"], "--surface", self.lane["surface"], "--worktree", str(self.repo))
        self.assertIn("PostToolUse", config["hooks"])
        self.assertIn("muse-hook", config["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(sorted(self.repo.rglob("*")), before)

    def test_wait_retries_native_delivery_without_duplicate_requests(self):
        rid = self.request()
        receipt = self.send(self.lane, "fixture")
        receipt["wake"] = {"status": "deferred"}
        with patch.object(am.App, "send_action", return_value=receipt) as sender:
            self.call("collect", rid)
            with patch.object(am.App, "native_delivery", return_value={"status": "native"}) as wake:
                result = self.call("wait", rid, "--seconds", "0")
                self.assertEqual(result["pending"], ["fixture"])
                wake.assert_called_once()
            sender.assert_called_once()

    def test_muse_null_transcript_needs_primary_retained_identity(self):
        sid = str(uuid.uuid4())
        native = self.root / "native-data"
        transcript = native / "sessions/2026/10/26" / sid / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("")
        body = {"session_id": sid, "hook_event_name": "UserPromptSubmit", "cwd": str(self.repo), "transcript_path": None}
        original_run = am.mp.run_command

        def run(argv, stdin=None):
            if argv[0] == "/bin/ps":
                return "1 /fixture/muse-bin\n" if "ppid=,comm=" in argv else "Mon Oct 26 12:00:00 2026\n"
            return original_run(argv, stdin)

        def invoke():
            with patch.object(sys, "stdin", io.StringIO(json.dumps(body))):
                return self.call("muse-hook", "--workspace", self.lane["workspace"], "--surface", self.lane["surface"],
                                 "--worktree", str(self.repo), "--muse-data-root", str(native))

        with patch.dict(os.environ, {}, clear=False), patch.object(am.mp, "run_command", side_effect=run):
            result = invoke()
            self.assertIn(sid, result["hookSpecificOutput"]["additionalContext"])
            body["session_id"] = str(uuid.uuid4())
            self.assertEqual(invoke(), {})
            self.assertEqual(self.app.read()["muse_bindings"][self.lane["surface"].lower()]["session"], sid)

    def test_confirmed_claude_owner_can_share_surface_without_relaxing_default(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["items"][0]["agents"] = [
            {"session_id": "owner", "kind": "claude", "state": "needs_input"},
            {"session_id": "worker", "kind": "codex", "state": "working"}]
        recipient = dict(self.lane, session="owner", harness="claude")
        with self.assertRaises(am.Error):
            am.active_agent(recipient, inventory)
        recipient["shared_surface"] = True
        self.assertEqual(am.active_agent(recipient, inventory)["session_id"], "owner")
        with self.assertRaises(am.Error):
            am.active_agent(dict(recipient, session="wrong-owner"), inventory)
        with self.assertRaises(am.Error):
            am.active_agent(dict(recipient, session="worker", harness="codex"), inventory)
        stale = copy.deepcopy(inventory)
        stale["items"][0]["freshness"]["state"] = "stale"
        with self.assertRaises(am.Error):
            am.active_agent(recipient, stale)


if __name__ == "__main__":
    unittest.main()
