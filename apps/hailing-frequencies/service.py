"""CMUX-descendant supervision and independent, read-only launchd health alerts."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time
import uuid

LABEL_PREFIX = 'ai.starfleet.hailing-health.'
OWNER = 'starfleet-hailing-health:v1'


def paths(directory):
    directory = Path(directory).expanduser().absolute()
    label = LABEL_PREFIX + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    return directory, label, Path.home() / 'Library/LaunchAgents' / (label + '.plist')


def health(directory, now=None):
    from hailing_frequencies import require
    directory = Path(directory)
    now = time.time() if now is None else now
    result = {'at': now, 'healthy': False, 'stale_after': 120}
    try:
        config = json.loads((directory / 'config.json').read_text())
        interval = config['interval']
        require(type(interval) in (int, float) and math.isfinite(interval) and interval > 0,
                'Invalid monitor interval')
        threshold = max(120, interval * 4)
        require(math.isfinite(threshold), 'Invalid health freshness threshold')
        result['stale_after'] = threshold
        scan = json.loads((directory / 'last-scan.json').read_text())
        heartbeat = scan['completed_at'] if 'completed_at' in scan else scan['at']
        require(type(heartbeat) in (int, float) and math.isfinite(heartbeat), 'Malformed scan heartbeat')
        age = now - heartbeat
        result.update(scan_age_seconds=round(age, 1), mode=scan['mode'], targets=len(scan['targets']))
        require(0 <= age <= threshold, 'No fresh successful scan')
        error_path = directory / 'last-error.json'
        if error_path.exists():
            error = json.loads(error_path.read_text())
            error_at = error['at']
            require(type(error_at) in (int, float) and math.isfinite(error_at), 'Malformed monitor error timestamp')
            require(error_at < heartbeat, 'Latest scan failed; sending is paused')
        timer_scan_path = directory / 'timer-last-scan.json'
        if timer_scan_path.exists():
            timer_scan = json.loads(timer_scan_path.read_text())
            timer_at = timer_scan['at']
            require(type(timer_at) in (int, float) and math.isfinite(timer_at), 'Malformed timer heartbeat')
            result['timer_scan_age_seconds'] = round(now - timer_at, 1)
            require(0 <= now - timer_at <= threshold, 'No fresh Temporal Beacon worker scan')
            timer_error_path = directory / 'timer-last-error.json'
            if timer_error_path.exists():
                timer_error = json.loads(timer_error_path.read_text())
                timer_error_at = timer_error['at']
                require(type(timer_error_at) in (int, float) and math.isfinite(timer_error_at),
                        'Malformed timer worker error timestamp')
                require(timer_error_at < timer_at, 'Latest Temporal Beacon worker scan failed')
        elif (directory / 'receipts.sqlite3').exists():
            # Existing Hailing state may predate timers; the first supervised iteration establishes this heartbeat.
            result['timer_scan_age_seconds'] = None
        result.update(healthy=True, reason='Recent successful scan; delivery holds remain visible in status')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['reason'] = str(exc)[:300]
    return result


def supervise(directory, cmux, send=False, only_sessions=None):
    from hailing_frequencies import atomic_json, require
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), 'Initialize a real state directory first')
    lock = directory / 'supervisor.lock'
    require(not lock.is_symlink(), 'Refusing symlink supervisor lock')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A supervisor already owns this state') from None
        stopping = False
        child = None

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True
            if child is not None and child.poll() is None:
                child.terminate()

        previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        restarts = 0
        try:
            while not stopping:
                # This supervisor stays in the CMUX ancestry chain. No detached authorization claim.
                command = [sys.executable, str(Path(__file__).with_name('hailing_frequencies.py')),
                           '--state-dir', str(directory), '--cmux', cmux, 'serve']
                if send:
                    command.append('--send')
                for session in only_sessions or []:
                    command.extend(['--only-session', session])
                worker_token = str(uuid.uuid4())
                child = subprocess.Popen(command, env={**os.environ, "STARFLEET_HF_WORKER_TOKEN": worker_token})
                atomic_json(directory / 'service.json', {'at': time.time(), 'supervisor_pid': os.getpid(),
                            'worker_pid': child.pid, 'worker_token': worker_token,
                            'restarts': restarts, 'state': 'running'})
                while child.poll() is None and not stopping:
                    time.sleep(0.2)
                if stopping:
                    try:
                        child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    break
                restarts += 1
                atomic_json(directory / 'service.json', {'at': time.time(), 'supervisor_pid': os.getpid(),
                            'restarts': restarts, 'state': 'restarting', 'last_exit': child.returncode})
                deadline = time.monotonic() + min(30, 2 ** min(restarts, 5))
                while not stopping and time.monotonic() < deadline:
                    time.sleep(0.2)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            atomic_json(directory / 'service.json', {'at': time.time(), 'supervisor_pid': os.getpid(),
                        'restarts': restarts, 'state': 'stopped'})
    return {'stopped': True, 'restarts': restarts}


def watchdog(directory, notify=True):
    from hailing_frequencies import atomic_json, require
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), 'Initialize a real state directory first')
    lock = directory / 'health.lock'
    require(not lock.is_symlink(), 'Refusing symlink health lock')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'skipped': 'Health check already running'}
        result = health(directory)
        old = directory / 'health.json'
        try:
            previous = json.loads(old.read_text()) if old.exists() else {}
        except (OSError, ValueError, TypeError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        last_alert = previous.get('last_alert_at', 0)
        if type(last_alert) not in (int, float) or not math.isfinite(last_alert) or last_alert < 0:
            last_alert = 0
        if not result['healthy'] and notify and (previous.get('healthy') is not False or time.time() - last_alert >= 3600):
            # Fixed text only. No inbox bodies, interpolated scripts, or socket credentials.
            script = ('display notification "Queue monitoring has stopped or its latest scan failed. '
                      'Run hailing-frequencies health and inspect the monitor terminal." '
                      'with title "Hailing Frequencies needs attention"')
            try:
                alert = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, timeout=10)
                result['notification'] = 'requested' if alert.returncode == 0 else 'failed'
            except (OSError, subprocess.SubprocessError):
                result['notification'] = 'failed'
            if result['notification'] == 'requested':
                last_alert = time.time()
        result['last_alert_at'] = last_alert
        atomic_json(old, result)
        return result


def health_service(directory, action):
    from hailing_frequencies import require
    require(sys.platform == 'darwin', 'The health watcher uses macOS launchd')
    directory, label, path = paths(directory)
    domain = f'gui/{os.getuid()}'
    require(not path.is_symlink(), 'Refusing symlink launch agent')
    if path.exists():
        require(plistlib.loads(path.read_bytes()).get('StarfleetOwner') == OWNER, 'Launch agent is not owned')
    if action == 'status':
        result = subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True, text=True)
        return {'label': label, 'loaded': result.returncode == 0, 'plist': str(path)}
    if action == 'uninstall':
        if path.exists():
            subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True, text=True)
            path.unlink()
        return {'removed': str(path), 'monitor_unchanged': True}
    require(directory.is_dir() and not directory.is_symlink(), 'Initialize a real state directory first')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only a health reader runs detached. It never contacts CMUX or sends agent prompts.
    payload = {'Label': label, 'StarfleetOwner': OWNER,
               'ProgramArguments': [sys.executable, str(Path(__file__).with_name('hailing_frequencies.py').resolve()),
                                    '--state-dir', str(directory), 'health-watch'],
               'RunAtLoad': True, 'StartInterval': 30,
               'StandardOutPath': str(directory / 'health-watch.log'),
               'StandardErrorPath': str(directory / 'health-watch-error.log')}
    if path.exists():
        subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True, text=True)
    temporary = path.with_suffix('.tmp')
    require(not temporary.exists() and not temporary.is_symlink(), 'Refusing existing launch-agent temporary file')
    try:
        with temporary.open('xb') as out:
            out.write(plistlib.dumps(payload))
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True, text=True)
    require(result.returncode == 0, result.stderr.strip() or 'Health watcher bootstrap failed')
    return {'installed': str(path), 'label': label, 'monitor_started': False, 'socket_access_changed': False}
