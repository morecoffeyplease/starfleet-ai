import json
import os
import plistlib
from pathlib import Path
from contextlib import closing
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hailing_frequencies as hf
import service


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        hf.atomic_json(self.root / 'config.json', {'interval': 30})

    def test_missing_stale_and_future_heartbeat_are_unhealthy(self):
        self.assertFalse(service.health(self.root, 200)['healthy'])
        for at in (1, 300):
            hf.atomic_json(self.root / 'last-scan.json', {'at': at, 'mode': 'send', 'targets': []})
            self.assertFalse(service.health(self.root, 200)['healthy'])

    def test_missing_or_malformed_config_is_an_unhealthy_report(self):
        config = self.root / 'config.json'
        config.write_text('{broken')
        malformed = service.health(self.root, 200)
        self.assertFalse(malformed['healthy'])
        self.assertEqual(malformed['stale_after'], 120)
        config.unlink()
        missing = service.health(self.root, 200)
        self.assertFalse(missing['healthy'])
        self.assertIn('No such file', missing['reason'])

    def test_success_after_error_restores_health(self):
        hf.atomic_json(self.root / 'last-scan.json', {'at': 150, 'mode': 'send', 'targets': []})
        self.assertTrue(service.health(self.root, 200)['healthy'])
        hf.atomic_json(self.root / 'last-error.json', {'at': 180})
        self.assertFalse(service.health(self.root, 200)['healthy'])
        hf.atomic_json(self.root / 'last-scan.json', {'at': 190, 'mode': 'send', 'targets': []})
        self.assertTrue(service.health(self.root, 200)['healthy'])
        hf.atomic_json(self.root / 'last-error.json', {'at': 190})
        self.assertFalse(service.health(self.root, 200)['healthy'])

    def test_health_uses_completed_scan_time_and_never_takes_monitor_lock(self):
        hf.atomic_json(self.root / 'last-scan.json', {
            'at': 1, 'completed_at': 190, 'mode': 'observe', 'targets': []
        })
        app = hf.Monitor(self.root)
        with app.locked():
            report = service.health(self.root, 200)
        self.assertTrue(report['healthy'])
        self.assertEqual(report['scan_age_seconds'], 10)

    def test_alerts_are_throttled_and_failed_delivery_is_retried(self):
        with patch('service.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(service.watchdog(self.root)['notification'], 'requested')
            self.assertNotIn('notification', service.watchdog(self.root))
            self.assertEqual(run.call_count, 1)
            hf.atomic_json(self.root / 'health.json', {'healthy': True})
            run.return_value.returncode = 1
            self.assertEqual(service.watchdog(self.root)['notification'], 'failed')
            service.watchdog(self.root)
            self.assertEqual(run.call_count, 3)

    def test_watchdog_can_be_read_only_without_notifications(self):
        with patch('service.subprocess.run') as run:
            self.assertFalse(service.watchdog(self.root, notify=False)['healthy'])
            run.assert_not_called()

    def test_corrupt_watchdog_history_does_not_suppress_alert(self):
        (self.root / 'health.json').write_text('[]')
        with patch('service.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(service.watchdog(self.root)['notification'], 'requested')

    def test_watcher_alerts_again_after_a_healthy_recovery(self):
        now = time.time()
        hf.atomic_json(self.root / 'last-scan.json', {
            'at': now - 500, 'mode': 'send', 'targets': []
        })
        with patch('service.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(service.watchdog(self.root)['notification'], 'requested')
            hf.atomic_json(self.root / 'last-scan.json', {
                'at': time.time(), 'mode': 'send', 'targets': []
            })
            self.assertTrue(service.watchdog(self.root)['healthy'])
            hf.atomic_json(self.root / 'last-scan.json', {
                'at': time.time() - 500, 'mode': 'send', 'targets': []
            })
            self.assertEqual(service.watchdog(self.root)['notification'], 'requested')
            self.assertEqual(run.call_count, 2)

    def test_health_service_installs_and_removes_only_its_owned_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launch_agent = root / 'Library' / 'LaunchAgents' / 'health.plist'
            fake_paths = (root, 'test.health', launch_agent)
            with patch('service.sys.platform', 'darwin'), patch('service.paths', return_value=fake_paths), \
                    patch('service.subprocess.run') as run:
                run.return_value.returncode = 0
                installed = service.health_service(root, 'install')
                self.assertFalse(installed['monitor_started'])
                payload = plistlib.loads(launch_agent.read_bytes())
                self.assertEqual(payload['StarfleetOwner'], service.OWNER)
                self.assertEqual(payload['ProgramArguments'][-1], 'health-watch')
                self.assertTrue(service.health_service(root, 'status')['loaded'])
                removed = service.health_service(root, 'uninstall')
                self.assertTrue(removed['monitor_unchanged'])
                self.assertFalse(launch_agent.exists())


class SupervisorTests(unittest.TestCase):
    def test_idle_capability_follows_actual_worker_replacement_and_stop(self):
        from temporal_beacon import TemporalBeacon
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, hf.__file__, '--state-dir', temporary]
            subprocess.run([*command, 'init', '--interval', '5'], check=True, capture_output=True)
            supervisor = subprocess.Popen([*command, 'supervise'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            beacon = TemporalBeacon(hf.Monitor(root))

            def wait_ready():
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    try:
                        beacon.require_idle_worker(time.time())
                        return json.loads((root / 'service.json').read_text())
                    except (OSError, ValueError):
                        time.sleep(0.05)
                self.fail('Supervised idle-capable worker did not become ready')

            try:
                first = wait_ready()  # Positive control: a real serving worker owns both locks.
                os.kill(first['worker_pid'], signal.SIGKILL)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = json.loads((root / 'service.json').read_text())
                    if state['state'] == 'restarting':
                        break
                    time.sleep(0.02)
                self.assertEqual(state['state'], 'restarting')
                self.assertLess(time.time() - json.loads((root / 'timer-last-scan.json').read_text())['at'], 120)
                with self.assertRaisesRegex(ValueError, 'Idle-aware worker'):
                    beacon.require_idle_worker(time.time())
                second = wait_ready()
                self.assertNotEqual(first['worker_token'], second['worker_token'])
                self.assertNotEqual(first['worker_pid'], second['worker_pid'])
                supervisor.terminate()
                _, stderr = supervisor.communicate(timeout=8)
                self.assertEqual(supervisor.returncode, 0, stderr)
                self.assertLess(time.time() - json.loads((root / 'timer-last-scan.json').read_text())['at'], 120)
                with self.assertRaisesRegex(ValueError, 'Idle-aware worker'):
                    beacon.require_idle_worker(time.time())
            finally:
                if supervisor.poll() is None:
                    supervisor.terminate()
                    supervisor.communicate(timeout=8)

    def test_worker_crash_restarts_and_keeps_receipts_and_singleton(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, hf.__file__, '--state-dir', temporary]
            subprocess.run([*command, 'init', '--interval', '5'], check=True, capture_output=True)
            monitor = hf.Monitor(root)
            with closing(monitor.database()) as db, db:
                db.execute("INSERT INTO batches VALUES ('preserved', 'session', 1, 'submitted', 'test')")
            supervisor = subprocess.Popen([*command, 'supervise'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            def wait_for(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        value = json.loads((root / 'service.json').read_text())
                        if predicate(value):
                            return value
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.05)
                self.fail('Supervisor did not reach expected state')
            try:
                first = wait_for(lambda v: v['state'] == 'running')
                os.kill(first['worker_pid'], signal.SIGKILL)
                second = wait_for(lambda v: v['state'] == 'running' and v['restarts'] == 1)
                self.assertNotEqual(first['worker_pid'], second['worker_pid'])
                with closing(monitor.database()) as db, db:
                    self.assertEqual(db.execute("SELECT token FROM batches").fetchall(), [('preserved',)])
                duplicate = subprocess.run([*command, 'supervise'], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn('already owns', duplicate.stderr)
                supervisor.terminate()
                _, stderr = supervisor.communicate(timeout=8)
                self.assertEqual(supervisor.returncode, 0, stderr)
                self.assertEqual(json.loads((root / 'service.json').read_text())['state'], 'stopped')
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.communicate()


class ServeIterationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        hf.atomic_json(self.root / 'config.json', {
            'version': 1, 'targets': [], 'coordinator': None, 'remind_after': 300,
            'escalate_after': 900, 'cooldown': 600, 'interval': 30,
        })
        self.now = 200
        self.app = hf.Monitor(self.root, clock=lambda: self.now)

    def write_scan(self, at):
        hf.atomic_json(self.root / 'last-scan.json', {
            'at': at, 'completed_at': at, 'mode': 'send', 'targets': [],
        })

    def call_while_locked(self):
        with self.app.locked():
            return hf.serve_iteration(self.app, send=True)

    def test_lock_busy_skip_preserves_fresh_healthy_heartbeat(self):
        self.write_scan(190)
        previous = (self.root / 'last-scan.json').read_bytes()
        result = self.call_while_locked()
        self.assertEqual(result['skipped'], 'lock_busy')
        self.assertEqual((self.root / 'last-scan.json').read_bytes(), previous)
        self.assertFalse((self.root / 'last-error.json').exists())
        self.assertTrue(service.health(self.root, self.now)['healthy'])

    def test_lock_busy_skip_does_not_mask_prior_real_error(self):
        self.write_scan(150)
        error = {'at': 180, 'error': 'previous CMUX failure', 'sending_paused': True}
        hf.atomic_json(self.root / 'last-error.json', error)
        result = self.call_while_locked()
        self.assertEqual(result['skipped'], 'lock_busy')
        self.assertEqual(json.loads((self.root / 'last-error.json').read_text()), error)
        report = service.health(self.root, self.now)
        self.assertFalse(report['healthy'])
        self.assertIn('Latest scan failed', report['reason'])

    def test_sustained_lock_busy_skip_leaves_success_deadline_unchanged(self):
        self.write_scan(1)
        result = self.call_while_locked()
        self.assertEqual(result['skipped'], 'lock_busy')
        report = service.health(self.root, self.now)
        self.assertFalse(report['healthy'])
        self.assertIn('No fresh successful scan', report['reason'])

    def test_successful_scan_recovers_health_after_prior_error(self):
        self.write_scan(190)
        hf.atomic_json(self.root / 'last-error.json', {
            'at': 195, 'error': 'previous CMUX failure', 'sending_paused': True,
        })
        self.assertFalse(service.health(self.root, self.now)['healthy'])
        self.now = 210
        report = hf.serve_iteration(self.app, send=True)
        self.assertEqual(report['completed_at'], 210)
        self.assertTrue(service.health(self.root, self.now)['healthy'])

    def test_real_scan_failure_is_still_recorded_and_unhealthy(self):
        self.write_scan(190)
        with patch.object(self.app, 'scan', side_effect=ValueError('bad source snapshot')):
            report = hf.serve_iteration(self.app, send=True)
        self.assertEqual(report['error'], 'bad source snapshot')
        self.assertTrue(report['sending_paused'])
        health = service.health(self.root, self.now)
        self.assertFalse(health['healthy'])
        self.assertIn('Latest scan failed', health['reason'])
