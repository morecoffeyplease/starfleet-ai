#!/usr/bin/env python3
"""Native cmux messaging adapter for Model Preferences and Away Mode."""
import datetime as dt
import json
import subprocess
import uuid


class PreferenceError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise PreferenceError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_command(argv, stdin=None):
    try:
        proc = subprocess.run(argv, input=stdin, text=True, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreferenceError(f"Command failed: {argv[0]}: {exc}") from exc
    require(proc.returncode == 0, f"{' '.join(argv[:3])}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def run_json(argv, stdin=None, runner=None):
    try:
        return json.loads((runner or run_command)(argv, stdin))
    except ValueError as exc:
        raise PreferenceError(f"Expected JSON from {argv[0]}.") from exc


class CmuxTransport:
    """Send native messages without maintaining another delivery or wake layer."""
    message_sender = "model-preferences"

    def run_command(self, argv, stdin=None):
        return run_command(argv, stdin)

    def run_json(self, argv, stdin=None):
        return run_json(argv, stdin)

    def current(self):
        return self.run_json(["cmux", "--json", "current", "--limit", "200"])

    def live(self, recipient, current):
        for item in current["items"]:
            if item.get("lifecycle") != "running":
                continue
            if not any(p.get("workspace_id", "").lower() == recipient["workspace"].lower() and
                       p.get("panel_id", "").lower() == recipient["surface"].lower()
                       for p in item.get("projections", [])):
                continue
            agents = item.get("agents", [])
            matched = [a for a in agents if a.get("session_id") == recipient["session"] and
                       a.get("kind") == recipient["harness"]]
            if any(a.get("state") in ("working", "idle", "needs_input") for a in matched):
                return "active"
            if matched and all(a.get("state") == "ended" for a in matched):
                return "inactive"
            return "unknown"
        return "unknown" if current.get("truncated") else "inactive"

    def native_delivery(self, recipient, receipt):
        """Keep stored caller receipts compatible while deferring delivery to cmux."""
        if receipt.get("state") in ("delivered", "read"):
            return {"status": "unnecessary", "reason": "cmux already reports delivery."}
        return {"status": "native", "reason": "cmux owns delivery and supported wake behavior; no terminal submit."}

    def send_action(self, recipient, text):
        receipt = self.run_json(["cmux", "agent", "message", recipient["surface"], "--from", self.message_sender, "--json", "--", "-"], text)
        return {"status": "queued", "receipt": receipt, "wake": self.native_delivery(recipient, receipt)}
