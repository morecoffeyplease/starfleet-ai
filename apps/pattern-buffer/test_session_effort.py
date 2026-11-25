"""Behavior checks for exact-session effort observations."""

import datetime
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

import pattern_buffer_session_effort as effort
import pattern_buffer_sessions as sessions


SCRIPT = Path(__file__).with_name("pattern_buffer.py")
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"


class SessionEffortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def cli(self, *args, input_bytes=None, env=None, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root,
                                input=input_bytes, capture_output=True,
                                env={**os.environ, **(env or {})})
        self.assertEqual(result.returncode, expected, result.stderr.decode("utf-8", "replace"))
        return result

    def report(self, *args, env=None):
        result = self.cli("session-effort", *args, env=env)
        return json.loads(result.stdout)

    @staticmethod
    def timestamp(value):
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def write_transcript(self, path, session=SESSION, turns=(), extra=()):
        rows = [{"type": "session_meta", "payload": {"id": session}}]
        rows.extend({"type": "turn_context", "timestamp": stamp, "payload": payload}
                    for stamp, payload in turns)
        rows.extend(extra)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def test_claude_statusline_captures_effort_and_keeps_sessions_separate(self):
        state_home = self.root / "state-home"
        env = {"XDG_STATE_HOME": str(state_home)}
        payload = {
            "session_id": SESSION,
            "cwd": "/private/worktree/path",
            "model": {"id": "claude-sonnet-4-5"},
            "effort": {"level": "medium"},
            "prompt": "TOP-SECRET-PROMPT",
            "context_window": {"used_percentage": 42},
        }
        statusline = self.cli("statusline", input_bytes=json.dumps(payload).encode(), env=env)
        self.assertEqual(statusline.stdout, b"Context: 42%\n")

        state_dir = state_home / "starfleet-ai/pattern-buffer/session-effort"
        report = self.report("--harness", "claude", "--session", SESSION, env=env)
        self.assertEqual(report["harness"], "claude")
        self.assertEqual(report["session"], SESSION)
        self.assertEqual(report["model"], "claude-sonnet-4-5")
        self.assertEqual(report["effort"], "medium")
        self.assertEqual(report["source"], "claude_statusline")
        self.assertEqual(report["state"], "last_observed")
        self.assertIsInstance(report["observed_at"], str)
        self.assertGreaterEqual(report["age_seconds"], 0)
        record = (state_dir / f"claude-{SESSION}.json").read_text()
        self.assertEqual(stat.S_IMODE((state_dir / f"claude-{SESSION}.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
        self.assertNotIn("TOP-SECRET-PROMPT", record)
        self.assertNotIn("/private/worktree/path", record)

        isolated = self.report("--harness", "claude", "--session", OTHER_SESSION, env=env)
        self.assertEqual(isolated["state"], "unknown")
        self.assertIsNone(isolated["effort"])
        self.assertFalse((state_dir / f"claude-{OTHER_SESSION}.json").exists())

    def test_missing_effort_is_unknown_and_a_read_creates_no_state(self):
        state_dir = self.root / "not-created"
        report = self.report("--harness", "claude", "--session", SESSION,
                             "--state-dir", str(state_dir))
        self.assertEqual(report["state"], "unknown")
        self.assertIsNone(report["model"])
        self.assertIsNone(report["effort"])
        self.assertIsNone(report["observed_at"])
        self.assertFalse(state_dir.exists())

    def test_unsupported_claude_effort_is_unknown(self):
        state_home = self.root / "state-home"
        payload = {"session_id": SESSION, "model": {"id": "claude-sonnet-4-5"},
                   "effort": {"level": "unsupported-level"}}
        self.cli("statusline", input_bytes=json.dumps(payload).encode(),
                 env={"XDG_STATE_HOME": str(state_home)})
        report = self.report("--harness", "claude", "--session", SESSION,
                             env={"XDG_STATE_HOME": str(state_home)})
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["model"], "claude-sonnet-4-5")
        self.assertIsNone(report["effort"])

    def test_statusline_observer_failure_preserves_delegate_bytes_output_and_exit(self):
        delegate = self.root / "delegate.py"
        delegate.write_text("import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.exit(23)\n")
        command = shlex.join([sys.executable, str(delegate)])
        raw = json.dumps({"session_id": SESSION, "model": {"id": "claude-opus-4-5"},
                          "effort": {"level": "high"}}).encode() + b"  \n"
        result = self.cli("statusline", "--delegate-json", json.dumps({"command": command}),
                          input_bytes=raw, env={"XDG_STATE_HOME": "relative-state"}, expected=23)
        self.assertEqual(result.stdout, raw)
        self.assertEqual(result.stderr, b"")
        self.assertFalse((self.root / "relative-state").exists())

    def test_codex_uses_latest_timestamp_after_exact_session_validation(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        latest = now - datetime.timedelta(seconds=10)
        older = now - datetime.timedelta(seconds=90)
        transcript = self.root / "native.jsonl"
        self.write_transcript(transcript, turns=(
            (self.timestamp(latest), {"model": "gpt-6-codex", "effort": "xhigh"}),
            (self.timestamp(older), {"model": "gpt-5-codex", "effort": "low"}),
        ), extra=({"type": "event_msg", "payload": {"text": "HIDDEN-TRANSCRIPT-CONTENT"}},))
        report = self.report("--harness", "codex", "--session", SESSION,
                             "--transcript", str(transcript))
        self.assertEqual(report["model"], "gpt-6-codex")
        self.assertEqual(report["effort"], "xhigh")
        self.assertEqual(report["source"], "codex_turn_context")
        self.assertEqual(report["observed_at"], self.timestamp(latest))
        self.assertGreaterEqual(report["age_seconds"], 9)
        self.assertLessEqual(report["age_seconds"], 12)
        self.assertEqual(report["state"], "last_observed")
        self.assertNotIn("HIDDEN-TRANSCRIPT-CONTENT", json.dumps(report))

    def test_long_codex_transcript_uses_header_and_recent_turn_only(self):
        transcript = self.root / "long.jsonl"
        stamp = self.timestamp(datetime.datetime.now(datetime.timezone.utc))
        with transcript.open("wb") as handle:
            handle.write((json.dumps({"type": "session_meta", "payload": {"id": SESSION}}) + "\n").encode())
            handle.write(b'{"type":"event_msg","payload":{"text":"')
            for _ in range(18):
                handle.write(b"x" * 1024 * 1024)
            handle.write(b'"}}\n')
            handle.write((json.dumps({"type": "turn_context", "timestamp": stamp,
                                     "payload": {"model": "codex-fixture", "effort": "max"}}) + "\n").encode())
        self.assertGreater(transcript.stat().st_size, effort.MAX_TRANSCRIPT_BYTES)
        report = self.report("--harness", "codex", "--session", SESSION, "--transcript", str(transcript))
        self.assertEqual((report["state"], report["model"], report["effort"]),
                         ("last_observed", "codex-fixture", "max"))
        mismatch = self.report("--harness", "codex", "--session", OTHER_SESSION,
                               "--transcript", str(transcript))
        self.assertEqual(mismatch["state"], "unknown")
        with transcript.open("ab") as handle:
            handle.write(b'{"type":"turn_context"')
        self.assertEqual(self.report("--harness", "codex", "--session", SESSION,
                                     "--transcript", str(transcript))["state"], "unknown")

    def test_bounded_forked_rollout_does_not_report_a_copied_parent_turn(self):
        transcript = self.root / "fork.jsonl"
        with transcript.open("w") as handle:
            print(json.dumps({"type": "session_meta", "payload": {
                "id": SESSION, "forked_from_id": OTHER_SESSION}}), file=handle)
            print(json.dumps({"type": "session_meta", "payload": {
                "id": OTHER_SESSION}}), file=handle)
            padding = json.dumps({"type": "event_msg", "payload": "x" * (512 * 1024)})
            for _ in range(17):
                print(padding, file=handle)
            print(json.dumps({"type": "turn_context", "timestamp": "2026-11-15T00:00:00Z",
                              "payload": {"model": "copied-parent", "effort": "high"}}), file=handle)
        self.assertGreater(transcript.stat().st_size, effort.MAX_TRANSCRIPT_BYTES)
        report = self.report("--harness", "codex", "--session", SESSION, "--transcript", str(transcript))
        self.assertEqual(report["state"], "unknown")
        self.assertIsNone(report["model"])
        self.assertIsNone(report["effort"])

    def test_supported_observed_effort_levels_are_preserved(self):
        stamp = self.timestamp(datetime.datetime.now(datetime.timezone.utc))
        for level in ("none", "minimal", "xhigh", "max", "ultra"):
            with self.subTest(level=level):
                transcript = self.root / (level + ".jsonl")
                self.write_transcript(transcript, turns=((stamp, {"model": "codex-fixture", "effort": level}),))
                report = self.report("--harness", "codex", "--session", SESSION,
                                     "--transcript", str(transcript))
                self.assertEqual(report["effort"], level)
        state = self.root / "claude-state"
        effort.observe_claude_statusline(sessions, {"session_id": SESSION, "model": {"id": "claude-fixture"},
                                         "effort": {"level": "xhigh"}}, state_dir=state)
        self.assertEqual(self.report("--harness", "claude", "--session", SESSION,
                                     "--state-dir", str(state))["effort"], "xhigh")

    def test_installed_claude_statusline_and_effort_launcher(self):
        runtime = self.root / "runtime"
        settings = self.root / "claude.json"
        state_home = self.root / "state-home"
        env = {**os.environ, "XDG_STATE_HOME": str(state_home), "PYTHONPATH": ""}
        bin_dir = self.root / "bin"
        self.cli("install", "--harness", "claude", "--settings", str(settings),
                 "--runtime-dir", str(runtime), "--bin-dir", str(bin_dir), env=env)
        cmd = [str(bin_dir / "pattern-buffer"), "session-effort", "--harness", "claude",
               "--session", SESSION]

        def installed_report():
            result = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual(installed_report()["state"], "unknown")
        self.assertFalse(state_home.exists())
        statusline = json.loads(settings.read_text())["statusLine"]["command"]
        payload = {"session_id": SESSION, "model": {"id": "claude-pilot-fixture"},
                   "effort": {"level": "max"}, "context_window": {"used_percentage": 11}}
        result = subprocess.run(statusline, shell=True, cwd=self.root, env=env,
                                input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Context: 11%\n")
        report = installed_report()
        self.assertEqual((report["model"], report["effort"], report["source"]),
                         ("claude-pilot-fixture", "max", "claude_statusline"))

    def test_opencode_installer_packages_the_common_effort_command(self):
        runtime = self.root / "installed"
        self.cli("install", "--harness", "opencode", "--settings", str(self.root / "opencode.json"),
                 "--runtime-dir", str(runtime))
        transcript = self.root / "native.jsonl"
        self.write_transcript(transcript, turns=((self.timestamp(datetime.datetime.now(datetime.timezone.utc)),
                                                  {"model": "codex-fixture", "effort": "medium"}),))
        result = subprocess.run([sys.executable, str(runtime / "pattern_buffer.py"), "session-effort",
                                 "--harness", "codex", "--session", SESSION,
                                 "--transcript", str(transcript)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["effort"], "medium")

    def test_codex_mismatch_and_partial_latest_turn_remain_unknown(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        mismatch = self.root / "mismatch.jsonl"
        self.write_transcript(mismatch, session=OTHER_SESSION,
                              turns=((self.timestamp(now), {"model": "gpt-6-codex", "effort": "high"}),),
                              extra=({"type": "event_msg", "payload": {"text": "NO-LEAK"}},))
        rejected = self.report("--harness", "codex", "--session", SESSION,
                               "--transcript", str(mismatch))
        self.assertEqual(rejected["state"], "unknown")
        self.assertIsNone(rejected["model"])
        self.assertNotIn("NO-LEAK", json.dumps(rejected))

        transcript = self.root / "partial.jsonl"
        earlier = now - datetime.timedelta(seconds=30)
        latest = now - datetime.timedelta(seconds=2)
        self.write_transcript(transcript, turns=(
            (self.timestamp(earlier), {"model": "gpt-5-codex", "effort": "high"}),
            (self.timestamp(latest), {"model": "gpt-6-codex"}),
        ))
        partial = self.report("--harness", "codex", "--session", SESSION,
                              "--transcript", str(transcript))
        self.assertEqual(partial["model"], "gpt-6-codex")
        self.assertIsNone(partial["effort"])
        self.assertEqual(partial["state"], "unknown")
        self.assertEqual(partial["observed_at"], self.timestamp(latest))

    def test_codex_malformed_oversized_missing_and_discovered_inputs_are_safe(self):
        malformed = self.root / "malformed.jsonl"
        malformed.write_text("not-json\n")
        malformed_report = self.report("--harness", "codex", "--session", SESSION,
                                        "--transcript", str(malformed))
        self.assertEqual(malformed_report["state"], "unknown")
        self.assertNotIn("not-json", json.dumps(malformed_report))

        oversized = self.root / "oversized.jsonl"
        with oversized.open("wb") as handle:
            handle.truncate(effort.MAX_TRANSCRIPT_BYTES + 1)
        oversized_report = self.report("--harness", "codex", "--session", SESSION,
                                       "--transcript", str(oversized))
        self.assertEqual(oversized_report["state"], "unknown")

        codex_home = self.root / "codex-home"
        missing = self.report("--harness", "codex", "--session", SESSION,
                              "--codex-home", str(codex_home))
        self.assertEqual(missing["state"], "unknown")
        self.assertFalse(codex_home.exists())

        date_path = datetime.datetime.now(datetime.timezone.utc).strftime("%Y/%m/%d")
        transcript = codex_home / "sessions" / date_path / f"rollout-example-{SESSION}.jsonl"
        transcript.parent.mkdir(parents=True)
        self.write_transcript(transcript, turns=((self.timestamp(
            datetime.datetime.now(datetime.timezone.utc)),
            {"model": "gpt-6-codex", "effort": "medium"}),))
        discovered = self.report("--harness", "codex", "--session", SESSION,
                                 "--codex-home", str(codex_home))
        self.assertEqual(discovered["model"], "gpt-6-codex")
        self.assertEqual(discovered["effort"], "medium")
        self.assertEqual(discovered["state"], "last_observed")


if __name__ == "__main__":
    unittest.main()
