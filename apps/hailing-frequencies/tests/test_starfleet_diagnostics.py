import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hailing_frequencies as hf


def snapshot(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_mode, info.st_mtime_ns,
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
    return result


class StarfleetDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".starfleet-hf-diagnostic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "hailing"
        self.state.mkdir()
        self.caller = {"harness": "codex", "session": "11111111-1111-4111-8111-111111111111",
                       "workspace": "22222222-2222-4222-8222-222222222222",
                       "surface": "33333333-3333-4333-8333-333333333333"}
        self.target = {"harness": self.caller["harness"], "session": self.caller["session"],
                       "workspace": self.caller["workspace"], "surface": self.caller["surface"],
                       "started_at": time.time() - 5, "cutoff": time.time() - 5, "label": "synthetic"}
        hf.atomic_json(self.state / "config.json", {
            "version": 1, "targets": [self.target], "coordinator": None,
            "remind_after": 300, "escalate_after": 900, "cooldown": 600, "interval": 5,
        })

    def test_hailing_and_temporal_use_fresh_read_only_heartbeat_evidence(self):
        now = time.time()
        hf.atomic_json(self.state / "last-scan.json", {
            "completed_at": now, "at": now, "mode": "send", "sending_sessions": None,
            "coverage_complete": True, "target_errors": [],
            "targets": [{"session": self.target["session"], "sending_enabled": True}],
        })
        hf.atomic_json(self.state / "timer-last-scan.json", {"at": now, "timers": []})
        before = snapshot(self.state)
        app = hf.Monitor(self.state)
        hailing = hf.hailing_diagnostic_report(app, self.caller)
        temporal = hf.temporal_diagnostic_report(app, self.caller)
        self.assertEqual(hailing["checks"]["registration"], "ready")
        self.assertEqual(hailing["checks"]["worker"], "ready")
        self.assertEqual(hailing["checks"]["intended_delivery"], "ready")
        self.assertEqual(temporal["checks"]["worker"], "ready")
        self.assertEqual(temporal["checks"]["intended_delivery"], "ready")
        self.assertEqual(snapshot(self.state), before)
        self.assertFalse((self.state / "monitor.lock").exists())
        self.assertFalse((self.state / "receipts.sqlite3").exists())

    def test_hailing_cli_missing_state_does_not_create_files_or_locks(self):
        state = self.root / "absent"
        before = snapshot(self.root)
        payload = json.dumps({"schema_version": 1, "caller": self.caller})
        result = subprocess.run([sys.executable, str(ROOT / "hailing_frequencies.py"),
                                 "--state-dir", str(state), "doctor", "--starfleet-json"],
                                input=payload, capture_output=True, text=True,
                                env={"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                                     "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["checks"]["configuration"], "missing")
        self.assertFalse(state.exists())
        self.assertEqual(snapshot(self.root), before)

    def test_agent_notify_adapter_does_not_source_credentials_or_contact_discord(self):
        config_home = self.root / "agent-notify"
        config_home.mkdir(mode=0o700)
        marker = self.root / "should-not-exist"
        env_file = config_home / "env"
        env_file.write_text(
            "DISCORD_WEBHOOK_URL=$(touch " + str(marker) + ")\n"
            "DISCORD_BOT_TOKEN=012345678901234567890123\n", encoding="utf-8")
        env_file.chmod(0o600)
        before = snapshot(self.root)
        result = subprocess.run(["bash", str(ROOT / "agent-notify"), "doctor", "--starfleet-json"],
                                cwd=self.root, capture_output=True, text=True,
                                env={"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                                     "HOME": str(self.root), "AGENT_NOTIFY_HOME": str(config_home),
                                     "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["checks"]["configuration"], "blocked")
        self.assertFalse(marker.exists())
        self.assertEqual(snapshot(self.root), before)

    def test_agent_notify_valid_local_credentials_are_not_printed_or_verified_over_network(self):
        config_home = self.root / "agent-notify"
        config_home.mkdir(mode=0o700)
        env_file = config_home / "env"
        webhook = "https://discord.com/api/webhooks/123456789012345678/secret-webhook-token"
        token = "012345678901234567890123"
        env_file.write_text(f"DISCORD_WEBHOOK_URL={webhook}\nDISCORD_BOT_TOKEN={token}\nDISCORD_USER_ID=''\n",
                            encoding="utf-8")
        env_file.chmod(0o600)
        before = snapshot(self.root)
        with patch.dict(os.environ, {"AGENT_NOTIFY_HOME": str(config_home)}):
            report = hf.agent_notify_diagnostic_report()
        rendered = json.dumps(report)
        self.assertEqual(report["checks"]["configuration"], "ready")
        self.assertEqual(report["checks"]["intended_delivery"], "unknown")
        self.assertNotIn(webhook, rendered)
        self.assertNotIn(token, rendered)
        self.assertEqual(snapshot(self.root), before)

    def test_agent_notify_public_cli_accepts_unquoted_and_single_quoted_literals(self):
        config_home = self.root / "agent-notify-quoted"
        config_home.mkdir(mode=0o700)
        env_file = config_home / "env"
        webhook = "https://discord.com/api/webhooks/123456789012345678/secret-webhook-token"
        token = "012345678901234567890123"
        command = ["bash", str(ROOT / "agent-notify"), "doctor", "--starfleet-json"]
        environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                       "HOME": str(self.root), "AGENT_NOTIFY_HOME": str(config_home),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        for webhook_value, token_value in ((webhook, token),
                                           (f"'{webhook}'", f"'{token}'")):
            user_id = "" if webhook_value == webhook else "''"
            env_file.write_text(
                f"DISCORD_WEBHOOK_URL={webhook_value}\n"
                f"DISCORD_BOT_TOKEN={token_value}\n"
                f"DISCORD_USER_ID={user_id}\n", encoding="utf-8")
            env_file.chmod(0o600)
            before = snapshot(self.root)
            result = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                    env=environment, check=False, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["checks"]["configuration"], "ready")
            self.assertEqual(report["checks"]["intended_delivery"], "unknown")
            self.assertNotIn(webhook, result.stdout + result.stderr)
            self.assertNotIn(token, result.stdout + result.stderr)
            self.assertEqual(snapshot(self.root), before)

    def test_agent_notify_discord_watch_config_check_accepts_bot_only_setup_and_cached_user(self):
        config_home = self.root / "agent-notify-bot-only"
        config_home.mkdir(mode=0o700)
        env_file = config_home / "env"
        token = "012345678901234567890123"
        env_file.write_text(f"DISCORD_BOT_TOKEN={token}\nDISCORD_USER_ID=''\n", encoding="utf-8")
        env_file.chmod(0o600)
        cache_file = config_home / "cache.json"
        cache_file.write_text(json.dumps({"user": "123456789012345678"}), encoding="utf-8")
        cache_file.chmod(0o600)
        before = snapshot(self.root)
        report = hf.agent_notify_diagnostic_report(
            config_home, require_webhook=False, require_cached_user=True)
        rendered = json.dumps(report)
        self.assertEqual(report["checks"]["configuration"], "ready")
        self.assertNotIn(token, rendered)
        self.assertNotIn("123456789012345678", rendered)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
