"""Bounded low-level Discord Gateway v10 JSON transport and session loop."""

import contextlib
import json
import math
from pathlib import Path
import random
import struct
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


GATEWAY_VERSION = 10
GATEWAY_INTENTS = (1 << 9) | (1 << 15)
MAX_GATEWAY_FRAME_BYTES = 1024 * 1024
MAX_GATEWAY_OUTBOUND_BYTES = 4096
MAX_HELLO_WAIT_SECONDS = 10
MAX_CONNECT_SECONDS = 8
MAX_READ_SECONDS = 1
MAX_HEARTBEAT_INTERVAL_SECONDS = 300
MIN_HEARTBEAT_INTERVAL_SECONDS = 1
MAX_SESSION_START_RESET_MS = 7 * 24 * 60 * 60 * 1000
MAX_RECONNECT_SECONDS = 60
IDENTIFY_CONCURRENCY_WINDOW_SECONDS = 5
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}
REIDENTIFY_CLOSE_CODES = {1000, 1001, 4007, 4009}


class GatewayError(ValueError):
    pass


class GatewayFatal(GatewayError):
    pass


class GatewayReconnect(GatewayError):
    def __init__(self, message="gateway requested reconnect", clear_session=False, delay=0):
        super().__init__(message)
        self.clear_session = clear_session
        self.delay = delay


def gateway_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise GatewayFatal("Discord Gateway URL is invalid")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise GatewayFatal("Discord Gateway URL is invalid") from None
    host = (parts.hostname or "").lower()
    host_ok = host == "discord.gg" or host.endswith(".discord.gg")
    if (parts.scheme != "wss" or not host_ok or parts.username is not None or
            parts.password is not None or port not in (None, 443) or parts.fragment or
            len(parts.path) > 256):
        raise GatewayFatal("Discord Gateway URL is outside the allowed secure endpoint format")
    query = dict(parse_qsl(parts.query, keep_blank_values=True, max_num_fields=16))
    if "compress" in query:
        del query["compress"]
    query.update({"v": str(GATEWAY_VERSION), "encoding": "json"})
    return urlunsplit(("wss", parts.netloc, parts.path or "/", urlencode(query), ""))


def validate_session_start_limit(value):
    if not isinstance(value, dict):
        raise GatewayError("Discord Gateway session-start limit is missing")
    parsed = {}
    for key in ("total", "remaining", "reset_after", "max_concurrency"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise GatewayError("Discord Gateway session-start limit is invalid")
        if key != "reset_after" and not float(item).is_integer():
            raise GatewayError("Discord Gateway session-start count is invalid")
        parsed[key] = item
    if (parsed["total"] < 1 or parsed["remaining"] < 0 or
            parsed["remaining"] > parsed["total"] or parsed["reset_after"] < 0 or
            parsed["reset_after"] > MAX_SESSION_START_RESET_MS or
            parsed["max_concurrency"] < 1 or parsed["max_concurrency"] > 1000):
        raise GatewayError("Discord Gateway session-start limit is outside bounded values")
    return {key: int(value) for key, value in parsed.items()}


def sequence_number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10 ** 18:
        raise GatewayError("Discord Gateway dispatch sequence is invalid")
    return str(value)


class _GatewaySocket:
    """Adapter over websocket-client's low-level API with bounded frame buffers."""

    def __init__(self, websocket):
        self.websocket = websocket
        self.close_code = None

    def send_packet(self, packet):
        encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_GATEWAY_OUTBOUND_BYTES:
            raise GatewayError("Discord Gateway outbound payload exceeded its protocol limit")
        self.websocket.send(encoded)

    def receive_packet(self, timeout):
        self.websocket.settimeout(max(0.05, min(float(timeout), MAX_READ_SECONDS)))
        try:
            opcode, frame = self.websocket.recv_data_frame(control_frame=True)
        except Exception as error:
            if type(error).__name__ == "WebSocketTimeoutException":
                return None
            raise
        if opcode in (9, 10):
            return None
        if opcode == 8:
            data = frame.data if isinstance(frame.data, bytes) else b""
            self.close_code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1006
            return {"_close_code": self.close_code}
        if opcode != 1:
            raise GatewayError("Discord Gateway sent a non-JSON WebSocket frame")
        payload = frame.data
        if isinstance(payload, str):
            encoded = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            encoded = payload
        else:
            raise GatewayError("Discord Gateway JSON frame is invalid")
        if len(encoded) > MAX_GATEWAY_FRAME_BYTES:
            raise GatewayError("Discord Gateway frame exceeded the bounded payload size")
        try:
            value = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise GatewayError("Discord Gateway sent invalid JSON") from None
        if not isinstance(value, dict):
            raise GatewayError("Discord Gateway event payload is not an object")
        return value

    def abort(self):
        try:
            self.websocket.abort()
        except (AttributeError, OSError):
            try:
                self.websocket.shutdown()
            except (AttributeError, OSError):
                pass


def _bounded_frame_buffer_class(abnf, payload_exception):
    class BoundedFrameBuffer(abnf.frame_buffer):
        def __init__(self, recv_fn, skip_utf8_validation):
            super().__init__(recv_fn, skip_utf8_validation)
            self.fragment_bytes = 0
            self.payload_pending = False

        def recv_mask(self):
            super().recv_mask()
            self.payload_pending = True

        def recv_strict(self, bufsize):
            if self.payload_pending and self.length is not None and bufsize == self.length:
                header = self.header or (1, 0, 0, 0, 1, 0, 0)
                opcode = header[4]
                fin = header[0]
                if self.length > MAX_GATEWAY_FRAME_BYTES:
                    raise payload_exception("Discord Gateway frame exceeded the bounded payload size")
                if opcode >= abnf.ABNF.OPCODE_CLOSE:
                    if self.length > 125:
                        raise payload_exception("Discord Gateway control frame exceeded its protocol limit")
                    payload = super().recv_strict(bufsize)
                    self.payload_pending = False
                    return payload
                if opcode not in (abnf.ABNF.OPCODE_TEXT, abnf.ABNF.OPCODE_BINARY,
                                  abnf.ABNF.OPCODE_CONT):
                    payload = super().recv_strict(bufsize)
                    self.payload_pending = False
                    return payload
                if opcode == abnf.ABNF.OPCODE_CONT:
                    total = self.fragment_bytes + self.length
                else:
                    total = self.length
                if total > MAX_GATEWAY_FRAME_BYTES:
                    raise payload_exception("Discord Gateway frame exceeded the bounded payload size")
                payload = super().recv_strict(bufsize)
                self.fragment_bytes = 0 if fin else total
                self.payload_pending = False
                return payload
            return super().recv_strict(bufsize)

    return BoundedFrameBuffer


def connect_gateway(value):
    runtime = Path(__file__).resolve().parent / "vendor"
    if not runtime.is_dir():
        raise GatewayFatal("Pinned websocket-client runtime dependency is unavailable")
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    try:
        import websocket
        from websocket import _abnf
        from websocket._exceptions import WebSocketPayloadException
    except ImportError:
        raise GatewayFatal("Pinned websocket-client runtime dependency is unavailable") from None
    if getattr(websocket, "__version__", None) != "1.9.2":
        raise GatewayFatal("Pinned websocket-client runtime version does not match 1.9.2")
    if not Path(websocket.__file__).resolve().is_relative_to(runtime.resolve()):
        raise GatewayFatal("Pinned websocket-client did not load from the private runtime vendor")

    BoundedFrameBuffer = _bounded_frame_buffer_class(_abnf, WebSocketPayloadException)

    class BoundedWebSocket(websocket.WebSocket):
        def __init__(self):
            super().__init__(enable_multithread=True, skip_utf8_validation=False)
            self.frame_buffer = BoundedFrameBuffer(self._recv, False)

    socket = BoundedWebSocket()
    socket.connect(gateway_url(value), timeout=MAX_CONNECT_SECONDS, suppress_origin=True,
                   redirect_limit=0)
    if socket.getstatus() != 101:
        socket.abort()
        raise GatewayError("Discord Gateway WebSocket handshake did not upgrade")
    return _GatewaySocket(socket)


class GatewayClient:
    """Owns one reconnecting Gateway lane and delegates durable events to Watch."""

    def __init__(self, watch, connector=None, clock=time.time, monotonic=time.monotonic,
                 random_value=random.random):
        self.watch = watch
        self.connector = connector or connect_gateway
        self.clock = clock
        self.monotonic = monotonic
        self.random_value = random_value
        self.active = None
        self.active_lock = threading.Lock()
        self.last_cycle_connected = False

    def stop(self):
        with self.active_lock:
            active = self.active
        if active is not None:
            active.abort()

    def run(self, stop_event):
        self.watch.begin_gateway_recovery("gateway_process_start")
        delay = 1.0
        while not stop_event.is_set():
            self.last_cycle_connected = False
            try:
                self._run_connection(stop_event)
                delay = 1.0
            except GatewayFatal as error:
                if self.last_cycle_connected:
                    self.watch.begin_gateway_recovery("gateway_fatal_disconnect")
                self.watch.set_gateway_condition("fatal_configuration", self._safe_detail(error))
                return
            except Exception as error:
                if stop_event.is_set():
                    break
                if isinstance(error, GatewayReconnect) and error.clear_session:
                    self.watch.clear_gateway_session(reset_sequence=True)
                if self.last_cycle_connected:
                    self.watch.begin_gateway_recovery("gateway_disconnect")
                condition = "degraded" if ("session-start limit" in str(error) or
                                           "rate limited" in str(error)) else "reconnecting"
                self.watch.set_gateway_condition(condition, self._safe_detail(error))
                wait_for = error.delay if isinstance(error, GatewayReconnect) and error.delay else delay
                stop_event.wait(min(MAX_RECONNECT_SECONDS, max(0.25, wait_for)))
                delay = min(MAX_RECONNECT_SECONDS, delay * 2)

    @staticmethod
    def _safe_detail(error):
        if isinstance(error, (GatewayFatal, GatewayReconnect)):
            return str(error)[:300]
        return f"Gateway connection interrupted ({type(error).__name__})"

    def _run_connection(self, stop_event):
        try:
            credentials, api = self.watch.read_credentials()
        except (ValueError, OSError) as error:
            raise GatewayFatal(f"Discord Gateway configuration is unavailable ({type(error).__name__})") from None
        session = self.watch.gateway_session()
        resume = bool(session.get("session_id") and session.get("resume_url"))
        now = self.clock()
        with contextlib.closing(self.watch.database(create=False)) as db:
            global_row = db.execute("SELECT value FROM meta WHERE key='global_not_before'").fetchone()
            self.watch._prepare_api_limits(db, api, float(global_row[0]) if global_row else 0.0)
            deferred_until = self.watch._api_deferred_until(db, api, "/gateway/bot", now)
        if deferred_until > now:
            raise GatewayReconnect("Discord Gateway endpoint is rate limited",
                                   delay=min(MAX_RECONNECT_SECONDS, deferred_until - now))
        try:
            gateway_info = api.get("/gateway/bot")
        except Exception as error:
            with contextlib.closing(self.watch.database(create=False)) as db:
                self.watch._persist_api_limits(db, api, now=now)
            status = getattr(error, "status", None)
            if status in (401, 403):
                raise GatewayFatal(f"Discord Gateway endpoint rejected credentials or access (HTTP {status})") from None
            if status == 429:
                self.watch.record_api_retry(api, "/gateway/bot",
                                            getattr(error, "retry_after", None),
                                            bool(getattr(error, "global_rate_limit", False)), now)
                raise GatewayReconnect("Discord Gateway endpoint is rate limited",
                                       delay=min(MAX_RECONNECT_SECONDS,
                                                 max(1.0, getattr(error, "retry_after", 1.0) or 1.0))) from None
            raise GatewayError(f"Discord Gateway endpoint request failed ({type(error).__name__})") from None
        with contextlib.closing(self.watch.database(create=False)) as db:
            self.watch._persist_api_limits(db, api, now=now)
        if not isinstance(gateway_info, dict):
            raise GatewayError("Discord Gateway endpoint response is invalid")
        initial_url = gateway_url(gateway_info.get("url"))
        limit = validate_session_start_limit(gateway_info.get("session_start_limit"))
        self.watch.save_gateway_start_limit(limit, self.clock())
        if resume:
            connect_url = gateway_url(session["resume_url"])
        else:
            self.watch.clear_gateway_session(reset_sequence=True)
            self._await_identify_window(limit, stop_event)
            connect_url = initial_url
        self.watch.set_gateway_condition("connecting")
        socket = self.connector(connect_url)
        with self.active_lock:
            self.active = socket
        try:
            hello = self._wait_hello(socket, stop_event)
            interval = self._heartbeat_interval(hello)
            next_heartbeat = self.monotonic() + interval * self._jitter()
            if resume:
                seq = session.get("sequence")
                packet = {"op": 6, "d": {"token": credentials["token"],
                                         "session_id": session["session_id"],
                                         "seq": int(seq) if seq is not None else None}}
            else:
                self.watch.reserve_gateway_identify(self.clock(), limit)
                packet = {"op": 2, "d": {
                    "token": credentials["token"], "intents": GATEWAY_INTENTS,
                    "properties": {"os": sys.platform, "browser": "discord-watch", "device": "discord-watch"}
                }}
            socket.send_packet(packet)
            self._connection_loop(socket, stop_event, interval, next_heartbeat,
                                  credentials["user_id"])
        finally:
            with self.active_lock:
                if self.active is socket:
                    self.active = None
            socket.abort()

    def _await_identify_window(self, limit, stop_event):
        state = self.watch.gateway_session()
        now = self.clock()
        remaining = state.get("session_start_remaining")
        reset_at = state.get("session_start_reset_at")
        if remaining is not None and reset_at is not None and now >= reset_at:
            remaining = limit["remaining"]
        if remaining is not None and remaining <= 0 and reset_at and now < reset_at:
            self.watch.set_gateway_condition("degraded", "Discord Gateway session-start limit is exhausted")
            raise GatewayReconnect("Gateway session-start limit is exhausted", delay=min(60, reset_at - now))
        last_identify = state.get("last_identify_at")
        if last_identify is not None:
            remaining_wait = IDENTIFY_CONCURRENCY_WINDOW_SECONDS - (now - last_identify)
            if remaining_wait > 0:
                stop_event.wait(min(remaining_wait, IDENTIFY_CONCURRENCY_WINDOW_SECONDS))
                if stop_event.is_set():
                    raise GatewayReconnect("Gateway stopped before Identify")

    def _wait_hello(self, socket, stop_event):
        deadline = self.monotonic() + MAX_HELLO_WAIT_SECONDS
        while not stop_event.is_set() and self.monotonic() < deadline:
            packet = socket.receive_packet(min(MAX_READ_SECONDS, max(0.05, deadline - self.monotonic())))
            if packet is None:
                continue
            if "_close_code" in packet:
                self._raise_close(packet["_close_code"])
            if packet.get("op") == 10:
                return packet
            raise GatewayError("Discord Gateway sent data before Hello")
        if stop_event.is_set():
            raise GatewayReconnect("Gateway stopped while waiting for Hello")
        raise GatewayError("Discord Gateway Hello timed out")

    @staticmethod
    def _heartbeat_interval(hello):
        data = hello.get("d")
        interval_ms = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, (int, float)) or not math.isfinite(interval_ms):
            raise GatewayError("Discord Gateway heartbeat interval is invalid")
        interval = interval_ms / 1000
        if not MIN_HEARTBEAT_INTERVAL_SECONDS <= interval <= MAX_HEARTBEAT_INTERVAL_SECONDS:
            raise GatewayError("Discord Gateway heartbeat interval is outside bounded values")
        return interval

    def _connection_loop(self, socket, stop_event, interval, next_heartbeat, configured_user_id):
        awaiting_ack = False
        last_heartbeat = None
        while not stop_event.is_set():
            now = self.monotonic()
            if awaiting_ack and last_heartbeat is not None and now >= last_heartbeat + interval:
                raise GatewayReconnect("Discord Gateway heartbeat acknowledgement timed out")
            if now >= next_heartbeat:
                if awaiting_ack:
                    raise GatewayReconnect("Discord Gateway heartbeat acknowledgement timed out")
                self._send_heartbeat(socket)
                awaiting_ack = True
                last_heartbeat = now
                next_heartbeat = now + interval
            timeout = min(MAX_READ_SECONDS, max(0.05, next_heartbeat - self.monotonic()))
            if awaiting_ack and last_heartbeat is not None:
                timeout = min(timeout, max(0.05, last_heartbeat + interval - self.monotonic()))
            packet = socket.receive_packet(timeout)
            if packet is None:
                continue
            if "_close_code" in packet:
                self._raise_close(packet["_close_code"])
            opcode = packet.get("op")
            if opcode == 11:
                awaiting_ack = False
                continue
            if opcode == 1:
                self._send_heartbeat(socket)
                awaiting_ack = True
                last_heartbeat = self.monotonic()
                next_heartbeat = last_heartbeat + interval
                continue
            if opcode == 7:
                raise GatewayReconnect("Discord Gateway requested a reconnect")
            if opcode == 9:
                resumable = packet.get("d") is True
                if not resumable:
                    self.watch.clear_gateway_session(reset_sequence=True)
                raise GatewayReconnect("Discord Gateway invalidated the current session",
                                       clear_session=not resumable,
                                       delay=1 + 4 * self._jitter())
            if opcode == 0:
                sequence = packet.get("s")
                event_name = packet.get("t")
                self.watch.commit_gateway_dispatch(sequence, event_name, packet.get("d"),
                                                   configured_user_id,
                                                   validate_resume_url=gateway_url)
                if event_name in ("READY", "RESUMED"):
                    self.last_cycle_connected = True
                    self.watch.set_gateway_condition("connected")

    def _send_heartbeat(self, socket):
        sequence = self.watch.gateway_session().get("sequence")
        socket.send_packet({"op": 1, "d": int(sequence) if sequence is not None else None})

    def _raise_close(self, code):
        if code in FATAL_CLOSE_CODES:
            raise GatewayFatal(f"Discord Gateway closed with fatal configuration code {code}")
        if code in REIDENTIFY_CLOSE_CODES:
            raise GatewayReconnect(f"Discord Gateway closed with code {code}", clear_session=True)
        raise GatewayReconnect(f"Discord Gateway closed with code {code}")

    def _jitter(self):
        value = self.random_value()
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0.5
        return min(1.0, max(0.0, float(value)))
