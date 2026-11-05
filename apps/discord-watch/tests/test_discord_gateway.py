import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import discord_gateway as gateway


class FakeSocket:
    def __init__(self, packets):
        self.packets = list(packets)
        self.sent = []
        self.aborted = False

    def receive_packet(self, timeout):
        if not self.packets:
            return None
        packet = self.packets.pop(0)
        if isinstance(packet, BaseException):
            raise packet
        return packet

    def send_packet(self, packet):
        self.sent.append(packet)

    def abort(self):
        self.aborted = True


class FakeAPI:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return {"url": "wss://gateway.discord.gg", "session_start_limit": {
            "total": 1000, "remaining": 999, "reset_after": 3600000,
            "max_concurrency": 1}}


class FakeWatch:
    def __init__(self, session=None):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "state.sqlite3"
        with sqlite3.connect(self.database_path) as db:
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.api = FakeAPI()
        self.session = {"condition": "degraded", "session_id": None, "resume_url": None,
                        "sequence": None, "session_start_remaining": None,
                        "session_start_reset_at": None, "last_identify_at": None}
        if session:
            self.session.update(session)
        self.events = []
        self.conditions = []
        self.recovery_count = 0
        self.clears = []

    def read_credentials(self):
        return {"token": "synthetic-token", "user_id": "500"}, self.api

    def database(self, create=False):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _prepare_api_limits(db, api, global_not_before=0):
        return None

    @staticmethod
    def _api_deferred_until(db, api, path, now):
        return 0

    @staticmethod
    def _persist_api_limits(db, api, thread_id=None, now=None):
        return None

    def gateway_session(self):
        return dict(self.session)

    def save_gateway_start_limit(self, limit, now):
        self.session.update({"session_start_remaining": limit["remaining"],
                             "session_start_reset_at": now + limit["reset_after"] / 1000})

    def reserve_gateway_identify(self, now, limit):
        remaining = self.session.get("session_start_remaining")
        if remaining is None:
            remaining = limit["remaining"]
        if remaining <= 0:
            raise ValueError("session-start limit is exhausted")
        self.session["session_start_remaining"] = remaining - 1
        self.session["last_identify_at"] = now

    def clear_gateway_session(self, reset_sequence=True):
        self.clears.append(reset_sequence)
        self.session["session_id"] = None
        self.session["resume_url"] = None
        if reset_sequence:
            self.session["sequence"] = None

    def set_gateway_condition(self, condition, detail=None):
        self.conditions.append((condition, detail))

    def begin_gateway_recovery(self, reason):
        self.recovery_count += 1
        self.events.append(("recovery", reason))

    def commit_gateway_dispatch(self, sequence, event_name, data, configured_user_id,
                                validate_resume_url=None):
        self.events.append(("dispatch", sequence, event_name, data))
        self.session["sequence"] = str(sequence)
        if event_name == "READY":
            self.session["session_id"] = data["session_id"]
            self.session["resume_url"] = validate_resume_url(data["resume_gateway_url"])
            self.begin_gateway_recovery("fresh_identify_ready")
        elif event_name == "RESUMED":
            self.begin_gateway_recovery("resume_ready")
        return {"committed": True, "ingested": False}

    def close(self):
        self.temporary.cleanup()


class FakeMonotonic:
    def __init__(self, step=0):
        self.value = 0.0
        self.step = step

    def __call__(self):
        value = self.value
        self.value += self.step
        return value


class FakePayloadException(ValueError):
    pass


class FakeABNF:
    class ABNF:
        OPCODE_CONT = 0
        OPCODE_TEXT = 1
        OPCODE_BINARY = 2
        OPCODE_CLOSE = 8

    class frame_buffer:
        def __init__(self, recv_fn, skip_utf8_validation):
            self.recv_fn = recv_fn
            self.skip_utf8_validation = skip_utf8_validation
            self.requested = []
            self.clear()

        def clear(self):
            self.header = None
            self.length = None
            self.mask_value = None

        def recv_mask(self):
            self.mask_value = self.recv_strict(4) if self.header[5] else ""

        def recv_strict(self, size):
            self.requested.append(size)
            return b"x" * size


class GatewayProtocolTests(unittest.TestCase):
    def test_gateway_url_forces_v10_json_and_removes_compression(self):
        value = gateway.gateway_url("wss://gateway.discord.gg/?compress=zlib-stream&v=9&encoding=etf")
        self.assertEqual(value, "wss://gateway.discord.gg/?v=10&encoding=json")

    def test_gateway_url_rejects_untrusted_scheme_host_credentials_and_port(self):
        for value in ("ws://gateway.discord.gg", "wss://discord.com", "wss://user@gateway.discord.gg",
                      "wss://gateway.discord.gg:444"):
            with self.subTest(value=value), self.assertRaises(gateway.GatewayFatal):
                gateway.gateway_url(value)

    def test_session_start_limit_and_dispatch_sequence_are_bounded(self):
        valid = {"total": 10, "remaining": 9, "reset_after": 30000, "max_concurrency": 1}
        self.assertEqual(gateway.validate_session_start_limit(valid), valid)
        for invalid in ({**valid, "remaining": 11}, {**valid, "reset_after": float("inf")},
                        {**valid, "max_concurrency": 0}):
            with self.subTest(invalid=invalid), self.assertRaises(gateway.GatewayError):
                gateway.validate_session_start_limit(invalid)
        self.assertEqual(gateway.sequence_number(0), "0")
        with self.assertRaises(gateway.GatewayError):
            gateway.sequence_number(True)

    def test_hello_heartbeat_interval_has_finite_bounds(self):
        self.assertEqual(gateway.GatewayClient._heartbeat_interval(
            {"d": {"heartbeat_interval": 10000}}), 10)
        for interval in (0, -1, float("nan"), 301000):
            with self.subTest(interval=interval), self.assertRaises(gateway.GatewayError):
                gateway.GatewayClient._heartbeat_interval({"d": {"heartbeat_interval": interval}})

    def test_requested_heartbeat_is_sent_and_acknowledgement_is_accepted(self):
        watch = FakeWatch()
        self.addCleanup(watch.close)
        client = gateway.GatewayClient(watch, monotonic=FakeMonotonic())
        socket = FakeSocket([{"op": 1}, {"op": 11}, {"_close_code": 4000}])
        with self.assertRaises(gateway.GatewayReconnect):
            client._connection_loop(socket, threading.Event(), 60, 60, "500")
        self.assertEqual(socket.sent, [{"op": 1, "d": None}])

    def test_missing_heartbeat_acknowledgement_forces_reconnect(self):
        watch = FakeWatch()
        self.addCleanup(watch.close)
        client = gateway.GatewayClient(watch, monotonic=FakeMonotonic(step=0.6))
        socket = FakeSocket([None, None])
        with self.assertRaisesRegex(gateway.GatewayReconnect, "acknowledgement timed out"):
            client._connection_loop(socket, threading.Event(), 1, 0, "500")
        self.assertEqual(socket.sent[0]["op"], 1)

    def test_invalid_session_clears_only_a_nonresumable_session(self):
        for resumable, expected_clears in ((True, []), (False, [True])):
            with self.subTest(resumable=resumable):
                watch = FakeWatch()
                self.addCleanup(watch.close)
                client = gateway.GatewayClient(watch, monotonic=FakeMonotonic())
                socket = FakeSocket([{"op": 9, "d": resumable}])
                with self.assertRaises(gateway.GatewayReconnect) as caught:
                    client._connection_loop(socket, threading.Event(), 60, 60, "500")
                self.assertEqual(caught.exception.clear_session, not resumable)
                self.assertEqual(watch.clears, expected_clears)

    def test_fatal_and_resumable_close_codes_are_distinguished(self):
        watch = FakeWatch()
        self.addCleanup(watch.close)
        client = gateway.GatewayClient(watch)
        for code in gateway.FATAL_CLOSE_CODES:
            with self.subTest(code=code), self.assertRaises(gateway.GatewayFatal):
                client._raise_close(code)
        for code in (4000, 4008, 4015):
            with self.subTest(code=code), self.assertRaises(gateway.GatewayReconnect):
                client._raise_close(code)
        with self.assertRaises(gateway.GatewayReconnect) as caught:
            client._raise_close(4007)
        self.assertTrue(caught.exception.clear_session)

    def test_identify_commits_ready_state_before_reconnect(self):
        watch = FakeWatch()
        self.addCleanup(watch.close)
        socket = FakeSocket([
            {"op": 10, "d": {"heartbeat_interval": 30000}},
            {"op": 0, "s": 1, "t": "READY", "d": {
                "session_id": "synthetic-session", "resume_gateway_url": "wss://gateway.discord.gg"}},
            {"_close_code": 4000},
        ])
        connected_urls = []
        client = gateway.GatewayClient(watch, connector=lambda url: connected_urls.append(url) or socket,
                                       clock=lambda: 100, monotonic=FakeMonotonic(), random_value=lambda: 0.5)
        with self.assertRaises(gateway.GatewayReconnect):
            client._run_connection(threading.Event())
        self.assertEqual(socket.sent[0]["op"], 2)
        self.assertEqual(socket.sent[0]["d"]["intents"], gateway.GATEWAY_INTENTS)
        self.assertTrue(connected_urls[0].startswith("wss://gateway.discord.gg/"))
        self.assertEqual(watch.session["session_id"], "synthetic-session")
        self.assertEqual(watch.session["sequence"], "1")
        self.assertTrue(any(row[0] == "dispatch" and row[2] == "READY" for row in watch.events))
        self.assertGreaterEqual(watch.recovery_count, 1)

    def test_restart_uses_persisted_sequence_for_resume_packet(self):
        watch = FakeWatch({"session_id": "synthetic-session",
                           "resume_url": "wss://gateway.discord.gg/", "sequence": "42"})
        self.addCleanup(watch.close)
        socket = FakeSocket([{"op": 10, "d": {"heartbeat_interval": 30000}},
                             {"_close_code": 4000}])
        connected_urls = []
        client = gateway.GatewayClient(watch, connector=lambda url: connected_urls.append(url) or socket,
                                       clock=lambda: 100, monotonic=FakeMonotonic(), random_value=lambda: 0.5)
        with self.assertRaises(gateway.GatewayReconnect):
            client._run_connection(threading.Event())
        self.assertEqual(socket.sent[0], {"op": 6, "d": {"token": "synthetic-token",
                                                          "session_id": "synthetic-session", "seq": 42}})
        self.assertTrue(connected_urls[0].startswith("wss://gateway.discord.gg/"))
        self.assertEqual(watch.clears, [])

    def test_exhausted_session_start_limit_holds_identify(self):
        watch = FakeWatch({"session_start_remaining": 0, "session_start_reset_at": 200})
        self.addCleanup(watch.close)
        client = gateway.GatewayClient(watch, clock=lambda: 100)
        stop_event = threading.Event()
        with self.assertRaisesRegex(gateway.GatewayReconnect, "limit is exhausted"):
            client._await_identify_window({"remaining": 5}, stop_event)

    def test_json_socket_bounds_outbound_and_inbound_payloads(self):
        class RawSocket:
            def __init__(self, payload):
                self.payload = payload
                self.sent = []

            def settimeout(self, value):
                self.timeout = value

            def recv_data_frame(self, control_frame=True):
                return 1, types.SimpleNamespace(data=self.payload)

            def send(self, value):
                self.sent.append(value)

        raw = RawSocket(b'{"op":11,"d":null}')
        adapter = gateway._GatewaySocket(raw)
        self.assertEqual(adapter.receive_packet(1), {"op": 11, "d": None})
        adapter.send_packet({"op": 1, "d": None})
        self.assertEqual(json.loads(raw.sent[0]), {"op": 1, "d": None})
        oversized = gateway._GatewaySocket(RawSocket(b"x" * (gateway.MAX_GATEWAY_FRAME_BYTES + 1)))
        with self.assertRaisesRegex(gateway.GatewayError, "bounded payload size"):
            oversized.receive_packet(1)
        with self.assertRaisesRegex(gateway.GatewayError, "outbound payload"):
            adapter.send_packet({"d": "x" * gateway.MAX_GATEWAY_OUTBOUND_BYTES})

    def test_low_level_frame_buffer_rejects_declared_and_fragmented_oversize_before_read(self):
        bounded = gateway._bounded_frame_buffer_class(FakeABNF, FakePayloadException)
        frame = bounded(lambda size: b"x" * size, False)
        frame.header = (1, 0, 0, 0, FakeABNF.ABNF.OPCODE_TEXT, 0, 127)
        frame.length = gateway.MAX_GATEWAY_FRAME_BYTES + 1
        frame.recv_mask()
        with self.assertRaisesRegex(FakePayloadException, "bounded payload size"):
            frame.recv_strict(frame.length)
        self.assertNotIn(gateway.MAX_GATEWAY_FRAME_BYTES + 1, frame.requested)

        frame.clear()
        frame.header = (1, 0, 0, 0, 9, 0, 126)
        frame.length = 126
        frame.recv_mask()
        with self.assertRaisesRegex(FakePayloadException, "control frame"):
            frame.recv_strict(frame.length)
        self.assertNotIn(126, frame.requested)

        frame.clear()
        frame.fragment_bytes = gateway.MAX_GATEWAY_FRAME_BYTES - 2
        frame.header = (1, 0, 0, 0, FakeABNF.ABNF.OPCODE_CONT, 0, 3)
        frame.length = 3
        frame.recv_mask()
        with self.assertRaisesRegex(FakePayloadException, "bounded payload size"):
            frame.recv_strict(3)
        self.assertNotIn(3, frame.requested)


if __name__ == "__main__":
    unittest.main()
