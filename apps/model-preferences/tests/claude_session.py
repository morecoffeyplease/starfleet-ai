"""Tool-free stream client shared by opt-in live acceptance checks."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


class ClaudeSession:
    def __init__(self, *, cwd, settings, model, effort, session, log, events, resume=False, env=None):
        environment = dict(os.environ if env is None else env)
        environment.pop("CLAUDECODE", None)
        self.session, self.events = session, events
        self.errors = Path(log).open("a")
        self.proc = subprocess.Popen(["claude", "-p", "--model", model, "--effort", effort,
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--include-hook-events",
            "--setting-sources", "", "--settings", str(settings), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", "", "--system-prompt", "You are a tool-free context-delivery acceptance subject. Answer briefly.",
            "--resume" if resume else "--session-id", session], cwd=cwd, env=environment, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, bufsize=1)
        self.messages = queue.Queue()
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                pass
        self.messages.put({"exit": True})

    def turn(self, text):
        self.proc.stdin.write(json.dumps({"type": "user", "session_id": self.session, "parent_tool_use_id": None,
                                        "message": {"role": "user", "content": text}}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + 120
        seen = []
        while True:
            message = self.messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if message.get("exit"):
                raise RuntimeError("Claude exited; inspect the captured stderr log")
            self.events.append(message)
            seen.append(message)
            if message.get("type") == "assistant" and any(
                    b.get("type") == "tool_use" for b in message.get("message", {}).get("content", [])):
                raise RuntimeError("Unexpected tool use")
            if message.get("type") == "result":
                if message.get("is_error"):
                    raise RuntimeError(message.get("result"))
                return message.get("result", ""), seen

    def close(self):
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        self.errors.close()
