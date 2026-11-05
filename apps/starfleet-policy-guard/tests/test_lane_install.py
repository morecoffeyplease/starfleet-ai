"""Delivery-only installation preserves other controls and probes never publish."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1]
LANE = APP / 'lane.py'
sys.path.insert(0, str(APP))
import lane as lane_runtime


class LaneInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='starfleet-lane-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.previous_xdg_state = os.environ.get('XDG_STATE_HOME')
        self.previous_xdg_data = os.environ.get('XDG_DATA_HOME')
        os.environ['XDG_STATE_HOME'] = str(self.base / 'xdg-state')
        os.environ['XDG_DATA_HOME'] = str(self.base / 'xdg-data')
        self.addCleanup(self.restore_xdg_environment)
        self.repo = self.base / 'repo lane'
        self.repo.mkdir()
        self.git('init', '-b', 'feature/test')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / '.gitignore').write_text('.claude/settings.local.json\n')
        (self.repo / 'product.txt').write_text('Original\n')
        self.git('add', '.')
        self.git('commit', '-m', 'candidate')
        self.git('branch', 'main')
        self.git('remote', 'add', 'origin', 'git@fixture.invalid:starfleet/guard.git')
        self.head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('update-ref', 'refs/remotes/origin/main', self.head)
        self.url = 'https://fixture.invalid/starfleet/guard/pull/901'
        self.settings = self.repo / '.claude/settings.local.json'
        self.settings.parent.mkdir()
        self.original = {'permissions': {'allow': ['Bash(existing-command)']}, 'env': {'EXISTING_FLAG': 'keep'},
                         'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
                             {'type': 'command', 'command': 'existing-cpu-hook', 'onFailure': 'block'}]}],
                                   'SessionStart': [{'hooks': [{'type': 'command', 'command': 'existing-context-hook'}]}]}}
        self.original_raw = json.dumps(self.original, indent=4) + '\n'
        self.settings.write_text(self.original_raw)
        self.remote_path = self.base / 'remote.json'
        self.remote = {'html_url': self.url, 'body': 'Users can finish their tasks.', 'state': 'open', 'draft': False,
                       'head': {'sha': self.head, 'ref': 'feature/test', 'repo': {'full_name': 'starfleet/guard'}},
                       'base': {'sha': self.head, 'ref': 'main', 'repo': {'full_name': 'starfleet/guard'}}}
        self.remote_path.write_text(json.dumps(self.remote))
        self.forwarded = self.base / 'forwarded.jsonl'
        self.api_calls = self.base / 'api-calls.jsonl'
        binary = self.base / 'bin/gh'
        binary.parent.mkdir()
        binary.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\nfrom urllib.parse import parse_qs,unquote,urlsplit\n'
                          'if sys.argv[1:2] == ["api"]:\n'
                          ' with open(os.environ["FAKE_LANE_API"],"a") as f:\n'
                          '  f.write(json.dumps({"endpoint":sys.argv[-1]})+"\\n")\n'
                          ' data=json.loads(Path(os.environ["FAKE_LANE_REMOTE"]).read_text())\n'
                          ' if "/pulls?" in sys.argv[-1]:\n'
                          '  branch=parse_qs(urlsplit(sys.argv[-1]).query).get("head",[""])[0].split(":",1)[-1]\n'
                          '  rows=data["records"].values() if "records" in data else [data]\n'
                          '  print(json.dumps([row for row in rows if row.get("state")=="open" and row.get("head",{}).get("ref")==branch]))\n'
                          ' elif "/git/ref/heads/" in sys.argv[-1]:\n'
                          '  if data.get("target_error"): sys.exit(1)\n'
                          '  branch=unquote(sys.argv[-1].split("/git/ref/heads/")[1])\n'
                          '  primary=next(iter(data["records"].values())) if "records" in data else data\n'
                          '  print(json.dumps(data.get("target_response", {"ref":"refs/heads/"+branch,"object":{"type":"commit","sha":data.get("target_head",primary["base"]["sha"])}})))\n'
                          ' else: print(json.dumps(data["records"][sys.argv[-1].split("/")[-1]] if "records" in data else data))\n'
                          'else:\n'
                          ' with open(os.environ["FAKE_LANE_FORWARDED"],"a") as f:\n'
                          '  f.write(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd()})+"\\n")\n')
        binary.chmod(0o755)
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.base / 'xdg-state'),
                        XDG_DATA_HOME=str(self.base / 'xdg-data'),
                        PATH=str(binary.parent) + os.pathsep + os.environ['PATH'],
                        FAKE_LANE_REMOTE=str(self.remote_path), FAKE_LANE_FORWARDED=str(self.forwarded),
                        FAKE_LANE_API=str(self.api_calls))
        self.packet = self.base / 'delivery.json'
        self.packet.write_text(json.dumps({'pr_url': self.url, 'base_ref': 'main', 'design-fidelity': {
            'applicable': False, 'revision': self.head, 'reason': 'No designs in this fixture'}}))

    def restore_xdg_environment(self):
        for key, value in (('XDG_STATE_HOME', self.previous_xdg_state), ('XDG_DATA_HOME', self.previous_xdg_data)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def git(self, *args, ok=True):
        result = subprocess.run(['git', '-C', str(self.repo), *map(str, args)], capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def init_fixture_repo(self, path, branch, remote):
        path = Path(path)
        path.mkdir(parents=True)
        (path / '.gitignore').write_text('.claude/settings.local.json\n')
        (path / 'fixture.txt').write_text('disposable fixture\n')
        for args in (('init', '-b', branch), ('config', 'user.name', 'Fixture'),
                     ('config', 'user.email', 'fixture@example.invalid'),
                     ('add', '.gitignore', 'fixture.txt'), ('commit', '-m', 'disposable fixture'),
                     ('remote', 'add', 'origin', remote)):
            result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True,
                                    env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
        gitdir = subprocess.run(['git', '-C', str(path), 'rev-parse', '--absolute-git-dir'],
                                capture_output=True, text=True, env=self.env, check=True).stdout.strip()
        common_gitdir = subprocess.run(['git', '-C', str(path), 'rev-parse', '--path-format=absolute',
                                        '--git-common-dir'], capture_output=True, text=True,
                                       env=self.env, check=True).stdout.strip()
        return gitdir, common_gitdir

    def call(self, *args, ok=True, input=None, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(LANE), '--cwd', str(cwd or self.repo), *map(str, args)],
                                capture_output=True, text=True, input=input, env=env or self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def call_source(self, source, *args, ok=True, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(source / 'lane.py'), '--cwd', str(cwd or self.repo),
                                 *map(str, args)], capture_output=True, text=True, env=env or self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def source_checkout(self, label, marker_file='lane.py'):
        source = self.base / ('policy guard source ' + label)
        shutil.copytree(APP, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (source / '.gitignore').write_text('__pycache__/\n*.py[cod]\n')
        marker_path = source / marker_file
        marker_path.write_text(marker_path.read_text() + '\n# disposable pinned source fixture: ' + label + '\n')
        subprocess.run(['git', '-C', str(source), 'init', '-b', 'main'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(source), 'config', 'user.name', 'Fixture'], check=True)
        subprocess.run(['git', '-C', str(source), 'config', 'user.email', 'fixture@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '.'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(source), 'commit', '-m', 'pin ' + label],
                       capture_output=True, check=True)
        return source

    def source_checkout_same_fingerprints(self, label):
        source = self.base / ('policy guard source ' + label)
        shutil.copytree(APP, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        (source / '.gitignore').write_text('__pycache__/\n*.py[cod]\n')
        subprocess.run(['git', '-C', str(source), 'init', '-b', 'main'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(source), 'config', 'user.name', 'Fixture'], check=True)
        subprocess.run(['git', '-C', str(source), 'config', 'user.email', 'fixture@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '.'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(source), 'commit', '-m', 'same-content source pin'],
                       capture_output=True, check=True)
        return source

    def install_from_source(self, source, owner=None, head_branch=None, lane=False, env=None, lane_pr_url=None):
        prefix = ['--session-root', owner] if owner else []
        suffix = ['--head-branch', head_branch] if head_branch else []
        if lane:
            args = ['plan', '--lane', '--host', 'fixture.invalid', '--owner-harness', 'codex',
                    '--owner-session', 'codex-fixture-thread']
            if lane_pr_url:
                args += ['--pr-url', lane_pr_url, '--base-ref', 'main']
            if owner:
                prefix = ['--session-root', owner]
        else:
            args = ['plan', '--pr-url', self.url, '--base-ref', 'main', *suffix]
        plan = json.loads(self.call_source(source, *prefix, *args, env=env).stdout)
        if lane:
            install_args = ['install', '--lane', '--host', 'fixture.invalid', '--owner-harness', 'codex',
                            '--owner-session', 'codex-fixture-thread', '--plan-sha256', plan['plan_sha256']]
            if lane_pr_url:
                install_args += ['--pr-url', lane_pr_url, '--base-ref', 'main']
        else:
            install_args = ['install', '--pr-url', self.url, '--base-ref', 'main',
                            '--plan-sha256', plan['plan_sha256'], *suffix]
        self.call_source(source, *prefix, *install_args, env=env)
        self.adapter = Path(plan['adapter'])
        return plan

    def plan(self, owner=None, head_branch=None):
        prefix = ['--session-root', owner] if owner else []
        suffix = ['--head-branch', head_branch] if head_branch else []
        return json.loads(self.call(*prefix, 'plan', '--pr-url', self.url, '--base-ref', 'main', *suffix).stdout)

    def install(self, owner=None, head_branch=None):
        plan = self.plan(owner, head_branch)
        prefix = ['--session-root', owner] if owner else []
        suffix = ['--head-branch', head_branch] if head_branch else []
        self.call(*prefix, 'install', '--pr-url', self.url, '--base-ref', 'main', '--plan-sha256', plan['plan_sha256'], *suffix)
        self.adapter = Path(plan['adapter'])
        return plan

    def assert_runtime_is_sealed(self, runtime):
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o555)
        for path in runtime.rglob('*'):
            expected = 0o555 if path.is_dir() else 0o444
            self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))

    def test_runtime_publish_recovery_and_immutable_modes(self):
        store = lane_runtime.session_store
        plan = store.runtime_plan(APP)
        runtime = Path(store.install_runtime(APP, plan))
        self.assertEqual(runtime, Path(plan['path']))
        self.assert_runtime_is_sealed(runtime)

        directories = [runtime, *(path for path in runtime.rglob('*') if path.is_dir())]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            os.chmod(directory, 0o700)
        missing = runtime / 'README.md'
        missing.unlink()

        repaired = Path(store.install_runtime(APP, plan))
        self.assertEqual(repaired, runtime)
        self.assertTrue(missing.is_file())
        self.assertEqual(store._digest(missing.read_bytes()), plan['files']['README.md'])
        self.assert_runtime_is_sealed(runtime)

        os.chmod(runtime, 0o700)
        with self.assertRaisesRegex(ValueError, 'runtime permissions changed'):
            store.install_runtime(APP, plan)
        os.chmod(runtime, 0o555)

    def test_runtime_seal_failure_removes_only_new_runtime(self):
        store = lane_runtime.session_store
        existing_plan = store.runtime_plan(APP)
        existing = Path(store.install_runtime(APP, existing_plan))
        existing_stat = existing.stat()

        source = self.base / 'runtime variant'
        shutil.copytree(APP, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        with (source / 'guide.md').open('a') as stream:
            stream.write('\nSeal failure fixture.\n')
        plan = store.runtime_plan(source)
        target = Path(plan['path'])
        self.assertNotEqual(target, existing)
        self.assertFalse(target.exists())

        original_chmod = os.chmod

        def fail_final_seal(path, mode, *args, **kwargs):
            if Path(path) == target and mode == 0o555:
                raise OSError('fixture final seal failure')
            return original_chmod(path, mode, *args, **kwargs)

        with mock.patch.object(store.os, 'chmod', side_effect=fail_final_seal):
            with self.assertRaisesRegex(OSError, 'fixture final seal failure'):
                store.install_runtime(source, plan)

        self.assertFalse(os.path.lexists(target))
        after = existing.stat()
        self.assertEqual((after.st_dev, after.st_ino), (existing_stat.st_dev, existing_stat.st_ino))
        self.assert_runtime_is_sealed(existing)

    def record(self):
        return json.loads(self.call('record', '--evidence', self.packet, '--format', 'json').stdout)

    def invoke(self, *args, cwd=None):
        return subprocess.run([str(self.adapter), *args], cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)

    def hook(self, command, cwd=None):
        return self.call('hook', ok=False, input=json.dumps({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
                         'cwd': str(cwd or self.repo), 'tool_input': {'command': command}}))

    def test_coverage_without_installation_is_readonly_and_unknown(self):
        before = self.git('status', '--porcelain').stdout
        value = json.loads(self.call('coverage', '--pr-url', self.url).stdout)
        self.assertEqual(value['binding']['state'], 'not-installed')
        self.assertIsNone(value['source_intact'])
        self.assertEqual(set(value['fix_rounds']['paths'].values()), {'unverified'})
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane').exists())
        self.assertEqual(self.git('status', '--porcelain').stdout, before)
        self.assertFalse(self.forwarded.exists())

    def test_coverage_separates_old_pr_and_current_binding_without_writes(self):
        self.install()
        state = self.repo / '.git/starfleet-delivery-lane/state.json'
        before = state.read_bytes()
        value = json.loads(self.call('coverage').stdout)
        self.assertEqual(value['binding']['state'], 'local-binding-current-pr-unconfirmed')
        other = self.url.replace('/901', '/902')
        value = json.loads(self.call('coverage', '--pr-url', other).stdout)
        self.assertEqual(value['binding']['state'], 'different-pr-bound')
        self.assertEqual(value['binding']['installed_pr'], self.url)
        value = json.loads(self.call('coverage', '--pr-url', self.url).stdout)
        self.assertEqual(value['binding']['state'], 'matches-declared-pr-and-local-branch')
        self.assertFalse(value['binding']['hosted_pr_checked'])
        self.git('switch', '-c', 'another-branch')
        value = json.loads(self.call('coverage', '--pr-url', self.url).stdout)
        self.assertEqual(value['binding']['state'], 'inactive-local-binding')
        self.assertEqual(before, state.read_bytes())
        self.assertFalse(self.forwarded.exists())

    def test_no_pr_native_lane_is_attached_without_delivery_eligibility(self):
        source = self.source_checkout_same_fingerprints('no-pr-native-lane-status')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(source, lane=True, env=owner_env)
        state = lane_runtime.read(self.repo)

        with mock.patch.dict(os.environ, owner_env):
            detail = lane_runtime.status(self.repo, 'codex-fixture-thread', state=state)
            self.assertTrue(detail['lane_enrolled'])
            self.assertTrue(detail['installed_here'])
            self.assertEqual(detail['attachment_state'], 'active-here')
            self.assertEqual(detail['current_owner']['state'], 'matches')
            self.assertIsNone(detail['pr_url'])
            self.assertEqual(detail['report_status']['state'], 'absent')
            self.assertFalse(lane_runtime.active(state, self.repo))
            with self.assertRaisesRegex(ValueError, 'Lane is enrolled; after its real PR is published'):
                lane_runtime.require(state, self.repo)

            coverage = lane_runtime.coverage(self.repo)
            self.assertTrue(coverage['installed_here'])
            self.assertEqual(coverage['attachment_state'], 'active-here')
            self.assertEqual(coverage['binding']['state'], 'lane-enrolled-no-pr-binding')

        summary = self.call('coverage', '--summary', env=owner_env).stdout
        self.assertIn('Policy Guard: delivery-only installation', summary)
        self.assertIn('bound=none (lane-enrolled-no-pr-binding', summary)
        self.assertIn('Report: absent', summary)

        wrong_owner = dict(owner_env, CODEX_THREAD_ID='another-codex-thread')
        wrong_owner_status = json.loads(self.call('status', env=wrong_owner).stdout)
        self.assertTrue(wrong_owner_status['installed_here'])
        self.assertEqual(wrong_owner_status['current_owner']['state'], 'mismatch')
        with mock.patch.dict(os.environ, wrong_owner):
            with self.assertRaisesRegex(ValueError, 'belongs to another native session'):
                lane_runtime.require(state, self.repo)

        with mock.patch.dict(os.environ, owner_env), mock.patch.object(
                lane_runtime, 'source_versions', return_value={'lane.py': 'changed-source'}):
            with self.assertRaisesRegex(ValueError, 'Pinned delivery adapter source changed'):
                lane_runtime.require(state, self.repo)

    def test_no_pr_legacy_migration_cli_preserves_lane_and_owner_history(self):
        source = self.source_checkout_same_fingerprints('no-pr-migration-cli')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        branch = self.git('branch', '--show-current').stdout.strip()
        gitdir = self.git('rev-parse', '--absolute-git-dir').stdout.strip()
        fake_gh = Path(self.env['PATH'].split(os.pathsep)[0]) / 'gh'
        adapter_path = Path(gitdir) / 'starfleet-delivery-lane/bin/gh'
        adapter_path.parent.mkdir(parents=True)
        adapter_text = lane_runtime.adapter_script(self.repo)
        adapter_path.write_text(adapter_text)
        adapter_path.chmod(0o700)
        lane_owner = {
            'harness': 'codex',
            'session_id': 'legacy-fixture-owner',
            'host': 'fixture.invalid',
            'repository': 'starfleet/guard',
            'product_gitdir': gitdir,
            'seat_gitdir': gitdir,
        }
        # Model the active no-PR legacy schema observed from the historical CLI reproduction.
        # The owner history is added below through the CLI handoff, and no live state is read.
        legacy_state = {
            'root': str(self.repo),
            'session_root': str(self.repo),
            'branch': branch,
            'head_branch': branch,
            'pr_url': None,
            'base_ref': None,
            'settings': str(self.settings),
            'settings_sha256': hashlib.sha256(b'').hexdigest(),
            'settings_existed': False,
            'settings_mode': 0o600,
            'manages_claude_hooks': False,
            'groups': {},
            'real_gh': str(fake_gh),
            'source': str(source),
            'source_revision': subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                               capture_output=True, text=True, check=True).stdout.strip(),
            'source_versions': lane_runtime.source_versions(),
            'adapter': str(adapter_path),
            'adapter_sha256': hashlib.sha256(adapter_text.encode()).hexdigest(),
            'lane_owner': lane_owner,
            'pr_bindings': {},
            'owner_history': [],
            'history': [],
            'installation_history': [],
            'phase': 'active',
        }
        legacy_state_path = Path(gitdir) / 'starfleet-delivery-lane/state.json'
        legacy_state_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_state_path.write_text(json.dumps(legacy_state, indent=2) + '\n')

        handoff = self.call_source(source, 'rebind-owner', '--from-harness', 'codex',
                                   '--from-session', 'legacy-fixture-owner', '--confirm-handoff',
                                   env=owner_env)
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue(json.loads(handoff.stdout)['history_preserved'])
        before_plan = json.loads(legacy_state_path.read_text())
        self.assertIsNone(before_plan['pr_url'])
        self.assertEqual(len(before_plan['owner_history']), 1)
        before_hash = hashlib.sha256(legacy_state_path.read_bytes()).hexdigest()

        planned = self.call_source(source, 'migrate-plan', '--host', 'fixture.invalid', env=owner_env)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertIsNone(plan['migrated_state']['pr_url'])
        self.assertEqual(plan['migrated_state']['owner_history'], before_plan['owner_history'])
        self.assertEqual(hashlib.sha256(legacy_state_path.read_bytes()).hexdigest(), before_hash)

        self.git('switch', 'main')
        wrong_branch = self.call_source(source, 'migrate-plan', '--host', 'fixture.invalid',
                                       env=owner_env, ok=False)
        self.assertEqual(wrong_branch.returncode, 2)
        self.assertIn('Switch to the recorded legacy product branch', wrong_branch.stderr)
        self.assertEqual(hashlib.sha256(legacy_state_path.read_bytes()).hexdigest(), before_hash)
        self.git('switch', branch)

        migrated = self.call_source(source, 'migrate', '--host', 'fixture.invalid',
                                    '--plan-sha256', plan['plan_sha256'], env=owner_env)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        result = json.loads(migrated.stdout)
        self.assertTrue(result['migrated'])
        self.assertTrue(result['history_preserved'])
        state_info = plan['migrated_state']
        migrated_state_path = (Path(state_info['seat_dir']) / 'checkouts' /
                               state_info['checkout_id'] / 'state.json')
        migrated_state = json.loads(migrated_state_path.read_text())
        for key in ('root', 'branch', 'head_branch', 'pr_url', 'base_ref',
                    'owner_history', 'pr_bindings', 'history', 'installation_history'):
            self.assertEqual(migrated_state.get(key), before_plan.get(key), key)
        for key in ('harness', 'session_id', 'host', 'repository', 'product_gitdir', 'seat_gitdir'):
            self.assertEqual(migrated_state['lane_owner'].get(key), before_plan['lane_owner'].get(key), key)
        tombstone = json.loads(legacy_state_path.read_text())
        self.assertEqual(tombstone['phase'], 'migrated')
        self.assertEqual(tombstone['preserved_state_sha256'], before_hash)
        status = json.loads(self.call_source(source, 'status', '--session-id', 'codex-fixture-thread',
                                            env=owner_env).stdout)
        self.assertTrue(status['lane_enrolled'])
        self.assertTrue(status['installed_here'])
        self.assertEqual(status['attachment_state'], 'active-here')
        self.assertIsNone(status['pr_url'])

    def test_migration_preserves_target_settings_mode_and_runtime_for_followup_upgrade(self):
        source = self.source_checkout('migration-settings-mode')
        owner_session = 'claude-migration-mode-fixture'
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='claude',
                         CLAUDE_CODE_SESSION_ID=owner_session)
        owner_env.pop('CODEX_THREAD_ID', None)
        plan_env = dict(owner_env, PYTHONHASHSEED='1')
        apply_env = dict(owner_env, PYTHONHASHSEED='2')
        seat_root = self.base / 'permanent seat'
        seat_gitdir, _ = self.init_fixture_repo(
            seat_root, 'main', 'git@fixture.invalid:starfleet/seat.git')
        seat_settings = seat_root / '.claude/settings.local.json'
        seat_settings.parent.mkdir(parents=True)
        seat_original = {'permissions': {'allow': ['Bash(existing-command)']},
                         'env': {'EXISTING_FLAG': 'preserve'}}
        seat_settings.write_text(json.dumps(seat_original, indent=2) + '\n')
        seat_settings.chmod(0o644)

        self.settings.chmod(0o600)
        legacy_groups = lane_runtime.hook_groups(self.repo, seat_root)
        legacy_settings = json.loads(self.original_raw)
        for event, group in legacy_groups.items():
            legacy_settings.setdefault('hooks', {}).setdefault(event, []).append(group)
        legacy_settings_raw = json.dumps(legacy_settings, indent=2) + '\n'
        self.settings.write_text(legacy_settings_raw)
        self.settings.chmod(0o600)
        product_gitdir = self.git('rev-parse', '--absolute-git-dir').stdout.strip()
        branch = self.git('branch', '--show-current').stdout.strip()
        fake_gh = Path(self.env['PATH'].split(os.pathsep)[0]) / 'gh'
        adapter_path = Path(product_gitdir) / 'starfleet-delivery-lane/bin/gh'
        adapter_path.parent.mkdir(parents=True)
        adapter_text = lane_runtime.adapter_script(self.repo)
        adapter_path.write_text(adapter_text)
        adapter_path.chmod(0o700)
        legacy_state = {
            'root': str(self.repo), 'session_root': str(seat_root), 'branch': branch,
            'head_branch': branch, 'pr_url': None, 'base_ref': None,
            'settings': str(self.settings),
            'settings_sha256': hashlib.sha256(legacy_settings_raw.encode()).hexdigest(),
            'settings_existed': True, 'settings_mode': 0o600, 'manages_claude_hooks': True,
            'groups': legacy_groups, 'original_hook_events': list(legacy_settings.get('hooks', {})),
            'original_had_hooks': True, 'real_gh': str(fake_gh),
            'real_gh_identity': lane_runtime.captured_binary_identity(fake_gh),
            'source': str(APP), 'source_revision': lane_runtime.source_revision(),
            'source_versions': lane_runtime.source_versions(), 'adapter': str(adapter_path),
            'adapter_sha256': hashlib.sha256(adapter_text.encode()).hexdigest(),
            'lane_owner': {'harness': 'claude', 'session_id': owner_session,
                           'host': 'fixture.invalid', 'repository': 'starfleet/guard',
                           'seat_root': str(seat_root), 'product_gitdir': product_gitdir,
                           'seat_gitdir': seat_gitdir},
            'pr_bindings': {}, 'owner_history': [{'fixture': 'preserve owner history'}],
            'history': [{'fixture': 'preserve report history'}],
            'installation_history': [{'fixture': 'preserve installation history'}],
            'phase': 'active',
        }
        legacy_state_path = Path(product_gitdir) / 'starfleet-delivery-lane/state.json'
        legacy_state_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_state_path.write_text(json.dumps(legacy_state, indent=2) + '\n')

        planned = self.call_source(source, 'migrate-plan', '--host', 'fixture.invalid', env=plan_env)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        migration_plan = json.loads(planned.stdout)
        migrated_plan_state = migration_plan['migrated_state']
        self.assertEqual(migrated_plan_state['settings_mode'], 0o644)
        self.assertEqual(migrated_plan_state['settings'], str(seat_settings))
        self.assertEqual(migrated_plan_state['runtime']['version'], migration_plan['runtime']['version'])
        cross_seed_plan_result = self.call_source(
            source, 'migrate-plan', '--host', 'fixture.invalid', env=apply_env)
        self.assertEqual(cross_seed_plan_result.returncode, 0, cross_seed_plan_result.stderr)
        cross_seed_plan = json.loads(cross_seed_plan_result.stdout)
        self.assertEqual(cross_seed_plan['plan_sha256'], migration_plan['plan_sha256'])
        self.assertEqual(cross_seed_plan['settings_changes'], migration_plan['settings_changes'])
        target_change = next(row for row in migration_plan['settings_changes']
                             if row['path'] == str(seat_settings))
        self.assertEqual(target_change['mode'], 0o644)
        migrated = self.call_source(source, 'migrate', '--host', 'fixture.invalid',
                                    '--plan-sha256', migration_plan['plan_sha256'], env=apply_env)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)

        state_path = (Path(migrated_plan_state['seat_dir']) / 'checkouts' /
                      migrated_plan_state['checkout_id'] / 'state.json')
        migrated_state = json.loads(state_path.read_text())
        seat_manifest = json.loads((Path(migrated_plan_state['seat_dir']) / 'seat.json').read_text())
        target_text = seat_settings.read_text()
        self.assertEqual(migrated_state['settings_mode'], 0o644)
        self.assertEqual(migrated_state['settings_sha256'], hashlib.sha256(target_text.encode()).hexdigest())
        self.assertEqual(migrated_state['runtime']['version'], seat_manifest['runtime']['version'])
        self.assertEqual(migrated_state['runtime']['path'], seat_manifest['runtime']['path'])
        self.assertEqual(migrated_state['settings'], seat_manifest['settings']['path'])
        self.assertEqual(seat_manifest['settings']['mode'], 0o644)
        self.assertEqual(seat_settings.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(target_text)['permissions'], seat_original['permissions'])
        self.assertEqual(json.loads(target_text)['env'], seat_original['env'])
        self.assertEqual(migrated_state['owner_history'], legacy_state['owner_history'])
        self.assertEqual(migrated_state['history'], legacy_state['history'])
        self.assertEqual(migrated_state['installation_history'], legacy_state['installation_history'])
        self.assertEqual(json.loads(legacy_state_path.read_text())['phase'], 'migrated')

        successor = self.source_checkout('migration-settings-mode-followup')
        upgrade_plan_result = self.call_source(successor, 'upgrade-plan', env=owner_env)
        self.assertEqual(upgrade_plan_result.returncode, 0, upgrade_plan_result.stderr)
        upgrade_plan = json.loads(upgrade_plan_result.stdout)
        self.assertEqual(upgrade_plan['migration_metadata_recovery'], [])
        applied = self.call_source(successor, 'upgrade', '--plan-sha256',
                                   upgrade_plan['plan_sha256'], env=owner_env)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        upgraded_state = json.loads(state_path.read_text())
        updated_seat = json.loads((Path(migrated_plan_state['seat_dir']) / 'seat.json').read_text())
        self.assertEqual(upgraded_state['settings_mode'], 0o644)
        self.assertEqual(upgraded_state['runtime']['version'],
                         upgrade_plan['executing_source_after']['runtime_version'])
        self.assertEqual(updated_seat['runtime']['version'], upgraded_state['runtime']['version'])
        self.assertEqual(upgraded_state['owner_history'], legacy_state['owner_history'])
        self.assertEqual(upgraded_state['history'], legacy_state['history'])
        self.assertEqual(upgraded_state['installation_history'], legacy_state['installation_history'])
        self.assertEqual(seat_settings.stat().st_mode & 0o777, 0o644)

    def test_upgrade_recovers_exact_migrated_metadata_across_attached_checkouts(self):
        old_source = self.source_checkout('migration-metadata-old-runtime')
        owner_session = 'claude-migration-recovery-fixture'
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='claude',
                         CLAUDE_CODE_SESSION_ID=owner_session)
        owner_env.pop('CODEX_THREAD_ID', None)
        seat_root = self.base / 'recovery seat'
        seat_gitdir, _ = self.init_fixture_repo(
            seat_root, 'main', 'git@fixture.invalid:starfleet/seat-recovery.git')
        seat_settings = seat_root / '.claude/settings.local.json'
        seat_settings.parent.mkdir(parents=True)
        seat_original = {'permissions': {'allow': ['Bash(existing-command)']},
                         'env': {'EXISTING_FLAG': 'preserve'}}
        seat_settings.write_text(json.dumps(seat_original, indent=2) + '\n')
        seat_settings.chmod(0o644)

        owner_args = ['--lane', '--host', 'fixture.invalid', '--owner-harness', 'claude',
                      '--owner-session', owner_session]
        seat_plan_result = self.call_source(old_source, '--session-root', seat_root, 'plan',
                                            *owner_args, env=owner_env)
        self.assertEqual(seat_plan_result.returncode, 0, seat_plan_result.stderr)
        seat_plan = json.loads(seat_plan_result.stdout)
        attached = self.call_source(old_source, '--session-root', seat_root, 'install',
                                    *owner_args, '--plan-sha256', seat_plan['plan_sha256'], env=owner_env)
        self.assertEqual(attached.returncode, 0, attached.stderr)
        seat_id = seat_plan['seat_id']
        seat_dir = Path(seat_plan['seat_dir'])
        first_state_path = seat_dir / 'checkouts' / seat_plan['checkout_id'] / 'state.json'
        first_state = json.loads(first_state_path.read_text())
        seat_manifest_path = seat_dir / 'seat.json'
        seat_manifest = json.loads(seat_manifest_path.read_text())
        old_runtime = seat_manifest['runtime']
        old_runtime_cli = Path(old_runtime['path']) / 'lane.py'

        second_product = self.base / 'runtime-attached-product'
        self.init_fixture_repo(
            second_product, 'feature/runtime-attach', 'git@fixture.invalid:starfleet/guard.git')
        runtime_plan_result = subprocess.run(
            [sys.executable, str(old_runtime_cli), '--cwd', str(second_product),
             '--session-root', str(seat_root), 'plan', *owner_args],
            capture_output=True, text=True, env=owner_env)
        self.assertEqual(runtime_plan_result.returncode, 0, runtime_plan_result.stderr)
        runtime_plan = json.loads(runtime_plan_result.stdout)
        runtime_attached = subprocess.run(
            [sys.executable, str(old_runtime_cli), '--cwd', str(second_product),
             '--session-root', str(seat_root), 'install', *owner_args,
             '--plan-sha256', runtime_plan['plan_sha256']],
            capture_output=True, text=True, env=owner_env)
        self.assertEqual(runtime_attached.returncode, 0, runtime_attached.stderr)
        seat_manifest = json.loads(seat_manifest_path.read_text())
        self.assertEqual(seat_manifest['runtime']['source_revision'], 'runtime:' + old_runtime['version'])
        self.assertEqual(first_state['source_revision'], old_runtime['source_revision'])
        self.assertNotEqual(first_state['source_revision'], seat_manifest['runtime']['source_revision'])

        existing_migration_product = self.base / 'existing-seat-migration-product'
        existing_gitdir, _ = self.init_fixture_repo(
            existing_migration_product, 'feature/existing-seat-migration',
            'git@fixture.invalid:starfleet/guard.git')
        existing_settings = existing_migration_product / '.claude/settings.local.json'
        existing_settings.parent.mkdir(parents=True)
        existing_groups = lane_runtime.hook_groups(existing_migration_product, seat_root)
        existing_settings_value = {
            'permissions': {'allow': ['Bash(legacy-permission)']},
            'hooks': {event: [group] for event, group in existing_groups.items()}}
        existing_settings_raw = json.dumps(existing_settings_value, indent=2) + '\n'
        existing_settings.write_text(existing_settings_raw)
        existing_settings.chmod(0o600)
        fake_gh = Path(self.env['PATH'].split(os.pathsep)[0]) / 'gh'
        existing_adapter = Path(existing_gitdir) / 'starfleet-delivery-lane/bin/gh'
        existing_adapter.parent.mkdir(parents=True)
        existing_adapter_text = lane_runtime.adapter_script(existing_migration_product)
        existing_adapter.write_text(existing_adapter_text)
        existing_adapter.chmod(0o700)
        existing_state = {
            'root': str(existing_migration_product), 'session_root': str(seat_root),
            'branch': 'feature/existing-seat-migration', 'head_branch': 'feature/existing-seat-migration',
            'pr_url': None, 'base_ref': None, 'settings': str(existing_settings),
            'settings_sha256': hashlib.sha256(existing_settings_raw.encode()).hexdigest(),
            'settings_existed': True, 'settings_mode': 0o600, 'manages_claude_hooks': True,
            'groups': existing_groups, 'original_hook_events': [], 'original_had_hooks': False,
            'real_gh': str(fake_gh), 'real_gh_identity': lane_runtime.captured_binary_identity(fake_gh),
            'source': str(APP), 'source_revision': lane_runtime.source_revision(),
            'source_versions': lane_runtime.source_versions(), 'adapter': str(existing_adapter),
            'adapter_sha256': hashlib.sha256(existing_adapter_text.encode()).hexdigest(),
            'lane_owner': {'harness': 'claude', 'session_id': owner_session,
                           'host': 'fixture.invalid', 'repository': 'starfleet/guard',
                           'seat_root': str(seat_root), 'product_gitdir': existing_gitdir,
                           'seat_gitdir': seat_gitdir},
            'pr_bindings': {}, 'owner_history': [], 'history': [],
            'installation_history': [], 'phase': 'active',
        }
        existing_state_path = Path(existing_gitdir) / 'starfleet-delivery-lane/state.json'
        existing_state_path.parent.mkdir(parents=True, exist_ok=True)
        existing_state_path.write_text(json.dumps(existing_state, indent=2) + '\n')
        existing_migrate_plan_result = subprocess.run(
            [sys.executable, str(old_runtime_cli), '--cwd', str(existing_migration_product),
             '--session-root', str(seat_root), 'migrate-plan', '--host', 'fixture.invalid'],
            capture_output=True, text=True, env=owner_env)
        self.assertEqual(existing_migrate_plan_result.returncode, 0, existing_migrate_plan_result.stderr)
        existing_migrate_plan = json.loads(existing_migrate_plan_result.stdout)
        self.assertFalse(any(row['path'] == str(seat_settings)
                             for row in existing_migrate_plan['settings_changes']))
        self.assertEqual(existing_migrate_plan['migrated_state']['settings_mode'], 0o644)
        self.assertEqual(existing_migrate_plan['migrated_state']['settings_sha256'],
                         hashlib.sha256(seat_settings.read_bytes()).hexdigest())
        existing_migrated = subprocess.run(
            [sys.executable, str(old_runtime_cli), '--cwd', str(existing_migration_product),
             '--session-root', str(seat_root), 'migrate', '--host', 'fixture.invalid',
             '--plan-sha256', existing_migrate_plan['plan_sha256']],
            capture_output=True, text=True, env=owner_env)
        self.assertEqual(existing_migrated.returncode, 0, existing_migrated.stderr)
        existing_migrated_state_path = (seat_dir / 'checkouts' /
            existing_migrate_plan['migrated_state']['checkout_id'] / 'state.json')
        existing_migrated_state = json.loads(existing_migrated_state_path.read_text())
        self.assertEqual(existing_migrated_state['settings_mode'], 0o644)
        self.assertEqual(existing_migrated_state['settings_sha256'],
                         hashlib.sha256(seat_settings.read_bytes()).hexdigest())
        self.assertEqual(existing_migrated_state['runtime']['version'], old_runtime['version'])

        legacy_product = self.base / 'recovered-migrated-product'
        legacy_gitdir, legacy_common_gitdir = self.init_fixture_repo(
            legacy_product, 'feature/legacy', 'git@fixture.invalid:starfleet/guard.git')
        affected_state = json.loads(json.dumps(first_state))
        affected_state.update(root=str(legacy_product), branch='feature/legacy',
                              head_branch='feature/legacy', settings=str(seat_settings),
                              settings_mode=0o600,
                              settings_sha256=hashlib.sha256(seat_settings.read_bytes()).hexdigest(),
                              owner_history=[{'fixture': 'preserve owner history'}],
                              history=[{'fixture': 'preserve report history'}],
                              installation_history=[{'fixture': 'preserve installation history'}],
                              checkout_id=lane_runtime.session_store.checkout_id(legacy_product))
        affected_state['lane_owner'].update(product_gitdir=legacy_gitdir, seat_gitdir=seat_gitdir,
                                            repository='starfleet/guard')
        affected_state.pop('runtime')
        lane_runtime.session_store.write_checkout(
            seat_id, legacy_product, affected_state, repository='starfleet/guard',
            host='fixture.invalid', gitdir=legacy_gitdir, common_gitdir=legacy_common_gitdir)
        affected_state_path = (seat_dir / 'checkouts' /
                               lane_runtime.session_store.checkout_id(legacy_product) / 'state.json')

        successor = self.source_checkout('migration-metadata-successor')
        malformed_state = json.loads(affected_state_path.read_text())
        malformed_state['runtime'] = None
        lane_runtime.session_store.write_checkout(
            seat_id, legacy_product, malformed_state, repository='starfleet/guard',
            host='fixture.invalid', gitdir=legacy_gitdir, common_gitdir=legacy_common_gitdir)
        malformed_before = affected_state_path.read_bytes()
        manifest_before = seat_manifest_path.read_bytes()
        malformed_refusal = self.call_source(successor, 'upgrade-plan', ok=False, env=owner_env)
        self.assertEqual(malformed_refusal.returncode, 2)
        self.assertIn('Active seat checkouts do not share one runtime version', malformed_refusal.stderr)
        self.assertEqual(affected_state_path.read_bytes(), malformed_before)
        self.assertEqual(seat_manifest_path.read_bytes(), manifest_before)

        affected_state = json.loads(malformed_before)
        affected_state['runtime'] = {'version': 'unrelated-runtime-version'}
        lane_runtime.session_store.write_checkout(
            seat_id, legacy_product, affected_state, repository='starfleet/guard',
            host='fixture.invalid', gitdir=legacy_gitdir, common_gitdir=legacy_common_gitdir)
        incompatible_before = affected_state_path.read_bytes()
        manifest_before = seat_manifest_path.read_bytes()
        incompatible_refusal = self.call_source(successor, 'upgrade-plan', ok=False, env=owner_env)
        self.assertEqual(incompatible_refusal.returncode, 2)
        self.assertIn('Active seat checkouts do not share one runtime version', incompatible_refusal.stderr)
        self.assertEqual(affected_state_path.read_bytes(), incompatible_before)
        self.assertEqual(seat_manifest_path.read_bytes(), manifest_before)

        affected_state = json.loads(incompatible_before)
        affected_state.pop('runtime')
        legacy_source_revision = affected_state['source_revision']
        affected_state['source_revision'] = 'unvalidated-runtime-revision'
        lane_runtime.session_store.write_checkout(
            seat_id, legacy_product, affected_state, repository='starfleet/guard',
            host='fixture.invalid', gitdir=legacy_gitdir, common_gitdir=legacy_common_gitdir)
        malformed_revision_before = affected_state_path.read_bytes()
        manifest_before = seat_manifest_path.read_bytes()
        revision_refusal = self.call_source(successor, 'upgrade-plan', ok=False, env=owner_env)
        self.assertEqual(revision_refusal.returncode, 2)
        self.assertIn('Active seat checkouts do not share one runtime version', revision_refusal.stderr)
        self.assertEqual(affected_state_path.read_bytes(), malformed_revision_before)
        self.assertEqual(seat_manifest_path.read_bytes(), manifest_before)

        affected_state['source_revision'] = legacy_source_revision
        affected_state['source_versions'] = {
            'README.md': seat_manifest['runtime']['files']['README.md']}
        lane_runtime.session_store.write_checkout(
            seat_id, legacy_product, affected_state, repository='starfleet/guard',
            host='fixture.invalid', gitdir=legacy_gitdir, common_gitdir=legacy_common_gitdir)
        truncated_fingerprints_before = affected_state_path.read_bytes()
        manifest_before = seat_manifest_path.read_bytes()
        truncated_fingerprints_refusal = self.call_source(successor, 'upgrade-plan', ok=False, env=owner_env)
        self.assertEqual(truncated_fingerprints_refusal.returncode, 2)
        self.assertIn('Active seat checkouts do not share one runtime version', truncated_fingerprints_refusal.stderr)
        self.assertEqual(affected_state_path.read_bytes(), truncated_fingerprints_before)
        self.assertEqual(seat_manifest_path.read_bytes(), manifest_before)

        affected_state['source_versions'] = first_state['source_versions']
        lane_runtime.session_store.write_checkout(
            seat_id, legacy_product, affected_state, repository='starfleet/guard',
            host='fixture.invalid', gitdir=legacy_gitdir, common_gitdir=legacy_common_gitdir)
        stale_mode_state = affected_state_path.read_bytes()
        stale_mode_manifest = seat_manifest_path.read_bytes()
        seat_settings.chmod(0o600)
        stale_mode_refusal = self.call_source(successor, 'upgrade-plan', ok=False, env=owner_env)
        self.assertEqual(stale_mode_refusal.returncode, 2)
        self.assertIn('Claude settings mode changed since installation', stale_mode_refusal.stderr)
        self.assertEqual(affected_state_path.read_bytes(), stale_mode_state)
        self.assertEqual(seat_manifest_path.read_bytes(), stale_mode_manifest)
        seat_settings.chmod(0o644)

        planned = self.call_source(successor, 'upgrade-plan', env=owner_env)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        upgrade_plan = json.loads(planned.stdout)
        repairs = [row for row in upgrade_plan['migration_metadata_recovery']
                   if row['checkout'] == str(legacy_product)]
        self.assertEqual({row['field'] for row in repairs}, {'runtime', 'settings_mode'})
        self.assertFalse(any(row['checkout'] in (str(self.repo), str(second_product))
                             for row in upgrade_plan['migration_metadata_recovery']))
        applied = self.call_source(successor, 'upgrade', '--plan-sha256',
                                   upgrade_plan['plan_sha256'], env=owner_env)
        self.assertEqual(applied.returncode, 0, applied.stderr)

        updated_seat = json.loads(seat_manifest_path.read_text())
        self.assertEqual(seat_settings.stat().st_mode & 0o777, 0o644)
        self.assertEqual(updated_seat['settings']['mode'], 0o644)
        self.assertEqual(updated_seat['runtime']['version'],
                         upgrade_plan['executing_source_after']['runtime_version'])
        self.assertEqual(json.loads(seat_settings.read_text())['permissions'], seat_original['permissions'])
        self.assertEqual(json.loads(seat_settings.read_text())['env'], seat_original['env'])
        recovered_states = [item['state'] for item in lane_runtime.session_store.all_checkouts(seat_id)]
        by_root = {row['root']: row for row in recovered_states}
        self.assertEqual(set(by_root), {str(self.repo), str(second_product),
                                        str(existing_migration_product), str(legacy_product)})
        self.assertEqual(by_root[str(legacy_product)]['settings_mode'], 0o644)
        self.assertEqual(by_root[str(legacy_product)]['runtime']['version'], updated_seat['runtime']['version'])
        self.assertEqual(by_root[str(legacy_product)]['owner_history'], affected_state['owner_history'])
        self.assertEqual(by_root[str(legacy_product)]['history'], affected_state['history'])
        self.assertEqual(by_root[str(legacy_product)]['installation_history'], affected_state['installation_history'])
        self.assertEqual(by_root[str(self.repo)]['settings_mode'], 0o644)
        self.assertEqual(by_root[str(second_product)]['settings_mode'], 0o644)
        self.assertEqual(by_root[str(existing_migration_product)]['settings_mode'], 0o644)

    def test_upgrade_plan_still_refuses_a_real_settings_mode_change(self):
        self.settings.chmod(0o644)
        old_source = self.source_checkout('mode-change-old-pin')
        self.install_from_source(old_source)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        before_state = state_path.read_bytes()
        self.settings.chmod(0o600)
        successor = self.source_checkout('mode-change-successor')
        refused = self.call_source(successor, 'upgrade-plan', ok=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('Claude settings mode changed since installation', refused.stderr)
        self.assertEqual(state_path.read_bytes(), before_state)

    def test_lane_help_describes_attachment_and_no_pr_migration(self):
        parent_help = ' '.join(self.call('--help').stdout.split())
        plan_help = ' '.join(self.call('plan', '--help').stdout.split())
        status_help = ' '.join(self.call('status', '--help').stdout.split())
        coverage_help = ' '.join(self.call('coverage', '--help').stdout.split())
        migration_help = ' '.join(self.call('migrate-plan', '--help').stdout.split())
        self.assertIn('optionally binding a PR or discovering it later', plan_help)
        self.assertIn('Inspect checkout attachment, PR binding', parent_help)
        self.assertIn('Inspect checkout attachment, PR binding', status_help)
        self.assertIn('Read-only attachment, binding, owner', parent_help)
        self.assertIn('attachment, binding, owner', coverage_help)
        self.assertIn('including a no-PR lane', parent_help)
        self.assertIn('including a no-PR lane', migration_help)

    def test_coverage_does_not_credit_legacy_adapter_to_codex_owner(self):
        self.install()
        self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        env = dict(self.env, CODEX_THREAD_ID='codex-current', CMUX_AGENT_LAUNCH_KIND='codex')
        value = json.loads(self.call('coverage', env=env).stdout)
        self.assertEqual(value['native_session']['actual'], {'harness': 'codex', 'session_id': 'codex-current'})
        self.assertIsNone(value['native_session']['matches_installed_owner'])
        observation = value['delivery_report']['observations']['explicit_adapter']
        self.assertIsNotNone(observation['last_observation'])
        self.assertEqual(observation['state'], 'unverified-for-current-session')
        self.assertEqual(value['entrypoints']['private_gh_adapter'], str(self.adapter))
        self.assertEqual(value['fix_rounds']['state'], 'not-installed-by-delivery-lane')

    def test_coverage_summary_without_installation_points_to_standalone_reporting(self):
        before = self.git('status', '--porcelain').stdout
        result = self.call('coverage', '--summary', '--pr-url', self.url).stdout
        self.assertIn('not installed here', result)
        self.assertIn('Standalone reporting needs no enrollment', result)
        self.assertIn('delivery report --help', result)
        self.assertIn('Fix-round enforcement: not installed by the delivery lane', result)
        self.assertLessEqual(len(result.splitlines()), 10)
        self.assertEqual(self.git('status', '--porcelain').stdout, before)
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane').exists())
        self.assertFalse(self.forwarded.exists())

    def test_coverage_summary_receipt_freshness_and_binding_are_readonly(self):
        self.install()
        self.record()
        # Legacy per-PR installations have no native owner, including in a plain terminal.
        terminal_env = {key: value for key, value in self.env.items() if key not in (
            'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID', 'CMUX_AGENT_LAUNCH_KIND')}
        state = self.repo / '.git/starfleet-delivery-lane/state.json'
        before = state.read_bytes()
        valid = self.call('coverage', '--summary', '--pr-url', self.url, env=terminal_env).stdout
        self.assertIn('Report: valid-local-snapshot', valid)
        self.assertIn('Continue under repository checks', valid)
        (self.repo / 'product.txt').write_text('Unverified change\n')
        stale = self.call('coverage', '--summary', env=terminal_env).stdout
        self.assertIn('Report: stale-or-unusable', stale)
        self.assertIn('Record a report from existing evidence', stale)
        other = self.url.replace('/901', '/902')
        mismatch = self.call('coverage', '--summary', '--pr-url', other, env=terminal_env).stdout
        self.assertIn('declared=' + other, mismatch)
        self.assertIn('bound=' + self.url, mismatch)
        self.assertIn('Confirm the product branch and PR', mismatch)
        self.assertEqual(state.read_bytes(), before)
        self.assertFalse(self.forwarded.exists())

    def test_coverage_summary_prioritizes_recovery_over_report_work(self):
        self.install()
        detail = json.loads(self.call('coverage').stdout)
        detail['upgrade'] = {'state': 'in-progress', 'recovery_command': 'reviewed-recovery-command'}
        text = lane_runtime.coverage_summary(self.repo, detail)
        self.assertIn('Next: Recover the interrupted upgrade: reviewed-recovery-command', text)
        self.assertNotIn('Next: Record', text)
        detail['upgrade']['state'] = 'none'
        detail['source_intact'] = False
        self.assertIn('Next: Ask the installation owner', lane_runtime.coverage_summary(self.repo, detail))
        detail['source_intact'] = True
        detail['native_session']['matches_installed_owner'] = False
        self.assertIn('Next: Confirm the owning native session', lane_runtime.coverage_summary(self.repo, detail))

    def test_owner_flags_without_lane_refuse_before_plan_or_install(self):
        before = self.settings.read_bytes()
        self.remote_path.unlink()  # An unsupported invocation must not need a GitHub read.
        for action in ('plan', 'install'):
            for flags in (['--owner-harness', 'codex'], ['--owner-session', 'declared-session'],
                          ['--owner-harness', 'codex', '--owner-session', 'declared-session']):
                args = [action, '--pr-url', self.url, '--base-ref', 'main', *flags]
                if action == 'install':
                    args += ['--plan-sha256', '0' * 64]
                run = self.call(*args, ok=False)
                self.assertEqual(run.returncode, 2)
                self.assertIn('require --lane', run.stderr)
                self.assertEqual(run.stdout, '')
        self.assertEqual(before, self.settings.read_bytes())
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane/state.json').exists())

    def test_plan_is_readonly_and_install_preserves_existing_settings(self):
        before = self.settings.read_bytes()
        mode = self.settings.stat().st_mode & 0o777
        proposed = self.plan()
        self.assertEqual(before, self.settings.read_bytes())
        self.assertFalse(Path(proposed['adapter']).exists())
        self.install()
        current = json.loads(self.settings.read_text())
        self.assertEqual(current['permissions'], self.original['permissions'])
        self.assertEqual(current['env'], self.original['env'])
        self.assertEqual(self.settings.stat().st_mode & 0o777, mode)
        for event in self.original['hooks']:
            self.assertEqual(current['hooks'][event][0], self.original['hooks'][event][0])
        self.assertFalse((self.repo / '.git/starfleet-policy-guard').exists())
        self.assertFalse((self.repo / '.codex').exists())
        self.assertFalse((self.repo / '.git/hooks/pre-push').exists())

    def test_legacy_migration_works_without_old_source_and_preserves_local_history_and_hooks(self):
        old_source = self.source_checkout('legacy-migration-old-source')
        self.install_from_source(old_source)
        self.call_source(old_source, 'record', '--evidence', self.packet, '--format', 'json')
        self.call_source(old_source, 'record', '--evidence', self.packet, '--format', 'json')
        legacy_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        legacy = json.loads(legacy_path.read_text())
        self.assertTrue(legacy['history'])
        late_hook = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'owner-added-hook'}]}
        current_settings = json.loads(self.settings.read_text())
        current_settings['hooks']['PreToolUse'].append(late_hook)
        self.settings.write_text(json.dumps(current_settings, indent=2) + '\n')
        shutil.rmtree(old_source)
        self.assertFalse(old_source.exists())

        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='claude', CLAUDE_CODE_SESSION_ID='migration-owner')
        proposed = json.loads(self.call('migrate-plan', '--confirm-owner', '--host', 'fixture.invalid',
                                        env=owner_env).stdout)
        migrated = self.call('migrate', '--confirm-owner', '--host', 'fixture.invalid',
                             '--plan-sha256', proposed['plan_sha256'], env=owner_env)
        self.assertTrue(json.loads(migrated.stdout)['history_preserved'])

        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state = json.loads(state_path.read_text())
        self.assertEqual(state['receipt'], legacy['receipt'])
        self.assertEqual(state['history'], legacy['history'])
        self.assertEqual(state['pr_bindings'][self.url]['receipt'], legacy['receipt'])
        self.assertEqual(state['pr_bindings'][self.url]['history'], legacy['history'])
        self.assertEqual(state['lane_owner']['session_id'], 'migration-owner')
        self.assertEqual(json.loads(legacy_path.read_text())['phase'], 'migrated')

        migrated_settings = json.loads(self.settings.read_text())
        self.assertEqual(migrated_settings['permissions'], self.original['permissions'])
        self.assertEqual(migrated_settings['env'], self.original['env'])
        self.assertEqual(migrated_settings['hooks']['PreToolUse'][0], self.original['hooks']['PreToolUse'][0])
        self.assertIn(late_hook, migrated_settings['hooks']['PreToolUse'])
        self.assertEqual(migrated_settings['hooks']['SessionStart'][0], self.original['hooks']['SessionStart'][0])
        for event, group in legacy['groups'].items():
            self.assertNotIn(group, migrated_settings['hooks'][event])
        for event, group in proposed['groups'].items():
            self.assertEqual(migrated_settings['hooks'][event].count(group), 1)

    def test_stale_plan_does_not_overwrite_owner_changes(self):
        proposed = self.plan()
        self.settings.write_text(self.original_raw + '\n')
        result = self.call('install', '--pr-url', self.url, '--base-ref', 'main', '--plan-sha256', proposed['plan_sha256'], ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.settings.read_text(), self.original_raw + '\n')

    def test_probe_denies_without_run_then_allows_gaps_without_any_mutation(self):
        self.install()
        self.assertEqual(self.invoke('pr', 'ready', self.url, '--starfleet-probe').returncode, 2)
        self.assertEqual(self.hook('gh pr ready 901 --starfleet-probe').returncode, 2)
        report = self.record()
        self.assertEqual(report['missing'], 2)
        result = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output['executed'])
        self.assertTrue(output['published_pr_checked'])
        self.assertFalse(self.forwarded.exists())
        self.assertEqual(self.hook('gh pr ready 901 --starfleet-probe').returncode, 0)
        trace = self.repo / '.git/starfleet-delivery-lane/native-events.jsonl'
        self.assertEqual([row['outcome'] for row in map(json.loads, trace.read_text().splitlines())
                          if row['event'] == 'PreToolUse'], ['deny', 'pass'])

    def test_status_distinguishes_installation_adapter_and_current_session_hook(self):
        self.install()
        value = json.loads(self.call('status', '--session-id', 'current').stdout)
        self.assertTrue(value['installed_here'])
        self.assertEqual(value['activation']['state'], 'unobserved')
        self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        value = json.loads(self.call('status', '--session-id', 'current').stdout)
        self.assertEqual(value['activation']['state'], 'explicit-adapter-observed')
        event = {'hook_event_name': 'PreToolUse', 'session_id': 'current', 'tool_name': 'Bash',
                 'cwd': str(self.repo), 'tool_input': {'command': 'gh pr ready 901 --starfleet-probe'}}
        self.assertEqual(self.call('hook', input=json.dumps(event), ok=False).returncode, 2)
        value = json.loads(self.call('status', '--session-id', 'current').stdout)
        self.assertEqual(value['activation']['state'], 'native-hook-observed')
        self.assertEqual(value['activation']['last_native_hook']['outcome'], 'deny')
        other = json.loads(self.call('status', '--session-id', 'different').stdout)
        self.assertEqual(other['activation']['state'], 'explicit-adapter-observed')
        self.assertIsNone(other['activation']['last_native_hook'])
        # Reinstallation must not inherit observations from the previous install.
        self.call('uninstall')
        self.install()
        self.assertEqual(json.loads(self.call('status', '--session-id', 'current').stdout)['activation']['state'], 'unobserved')

    def test_status_exposes_stale_report_without_running_verification(self):
        self.install()
        self.record()
        self.assertEqual(json.loads(self.call('status').stdout)['report_status']['state'], 'valid-local-snapshot')
        self.git('commit', '--allow-empty', '-m', 'next candidate')
        value = json.loads(self.call('status').stdout)
        self.assertEqual(value['report_status']['state'], 'stale-or-unusable')
        self.assertIn('Candidate or local target changed', value['report_status']['reason'])
        self.assertFalse(self.forwarded.exists())

    def test_starter_names_tracked_and_untracked_blockers_without_modifying_them(self):
        self.install()
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        state = state_path.read_bytes()
        private = self.repo / 'private\nnotes.json'
        private.write_text('PRIVATE CONTENT MUST NOT BE PRINTED')
        output = self.base / 'starter.json'
        args = ['init-evidence', '--output', output, '--designs', 'no', '--no-design-reason', 'No designs']
        result = self.call(*args, ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Tracked changes (0): none', result.stderr)
        self.assertIn('Untracked paths (1)', result.stderr)
        self.assertIn('private\\nnotes.json', result.stderr)
        self.assertNotIn('PRIVATE CONTENT', result.stderr)
        self.assertNotIn('Commit the candidate', result.stderr)
        (self.repo / 'product.txt').write_text('Actual tracked change\n')
        result = self.call(*args, ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Tracked changes (1)', result.stderr)
        self.assertIn('product.txt', result.stderr)
        self.assertIn('Untracked paths (1)', result.stderr)
        self.assertTrue(private.exists())
        self.assertEqual(private.read_text(), 'PRIVATE CONTENT MUST NOT BE PRINTED')
        self.assertEqual(state, state_path.read_bytes())
        self.assertFalse(output.exists())

    def test_packet_starter_is_bound_but_does_not_manufacture_verification(self):
        self.install()
        output = self.base / 'evidence/starter.json'
        result = self.call('init-evidence', '--output', output, '--designs', 'no',
                           '--no-design-reason', 'No designs in this CLI change')
        self.assertIn('No audit, verification or report ran', result.stdout)
        packet = json.loads(output.read_text())
        self.assertEqual(packet['pr_url'], self.url)
        self.assertEqual(packet['main-awareness']['base_head'], self.head)
        self.assertIsNone(packet['local-verification']['exit_code'])
        self.assertNotEqual(packet['local-verification']['revision'], self.head)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(json.loads(self.call('status').stdout)['receipt'])
        before = output.read_bytes()
        self.assertEqual(self.call('init-evidence', '--output', output, '--designs', 'no',
                                  '--no-design-reason', 'no designs', ok=False).returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(self.call('init-evidence', '--output', self.repo / 'packet.json', '--designs', 'yes', ok=False).returncode, 2)
        self.assertFalse((self.repo / 'packet.json').exists())

    def test_design_starter_requires_actual_audit_and_no_design_requires_reason(self):
        self.install()
        output = self.base / 'design-starter.json'
        self.assertEqual(self.call('init-evidence', '--output', output, '--designs', 'no', ok=False).returncode, 2)
        self.call('init-evidence', '--output', output, '--designs', 'yes')
        packet = json.loads(output.read_text())
        self.assertEqual(packet['design-fidelity']['coverage'], 'NONE')
        self.assertEqual(packet['design-fidelity']['deviations'], [])
        self.assertEqual(self.call('record', '--evidence', output, ok=False).returncode, 2)
        self.assertIsNone(json.loads(self.call('status').stdout)['receipt'])

    def test_missing_enrollment_names_the_delivery_only_plan_not_full_guard(self):
        result = self.call('record', '--evidence', self.packet, ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--session-root', result.stderr)
        self.assertIn('plan --pr-url', result.stderr)
        self.assertIn('Do not use guard.py enroll', result.stderr)

    def test_packet_url_casing_is_normalized_but_wrong_pr_and_ref_are_rejected(self):
        self.install()
        packet = json.loads(self.packet.read_text())
        packet['pr_url'] = self.url.replace('/starfleet/guard/', '/Starfleet/Guard/')
        self.packet.write_text(json.dumps(packet))
        self.record()
        for key, bad in [('pr_url', self.url.replace('/901', '/902')), ('base_ref', 'MAIN')]:
            wrong = {**packet, key: bad}
            self.packet.write_text(json.dumps(wrong))
            result = self.call('record', '--evidence', self.packet, ok=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn(key, result.stderr)

    def test_registration_inventory_survives_missing_product_and_known_target_stops(self):
        first = self.install()
        product = self.base / 'temporary product'
        self.git('worktree', 'add', '-b', 'feature/second', product)
        second_url = self.url.replace('/901', '/902')
        remote = json.loads(json.dumps(self.remote))
        remote['html_url'] = second_url
        remote['head']['ref'] = 'feature/second'
        self.remote_path.write_text(json.dumps({'records': {'901': self.remote, '902': remote}}))
        plan = json.loads(self.call('--session-root', self.repo, 'plan', '--pr-url', second_url,
                                    '--base-ref', 'main', cwd=product).stdout)
        self.call('--session-root', self.repo, 'install', '--pr-url', second_url, '--base-ref', 'main',
                  '--plan-sha256', plan['plan_sha256'], cwd=product)
        product.rename(self.base / 'disappeared product')
        rows = json.loads(self.call('registrations').stdout)['registrations']
        missing = next(row for row in rows if row['pr_url'] == second_url)
        self.assertFalse(missing['binding_usable'])
        result = subprocess.run([first['adapter'], 'pr', 'ready', '902', '--starfleet-probe'],
                                cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('reviewed rebind', result.stderr)
        self.assertFalse(self.forwarded.exists())

    def test_normal_argv_and_repo_controls_are_forwarded_unchanged(self):
        self.install()
        self.record()
        argv = ['pr', 'merge', self.url, '--auto', '--match-head-commit', self.head]
        result = self.invoke(*argv)
        self.assertEqual(result.returncode, 0, result.stderr)
        forwarded = json.loads(self.forwarded.read_text())
        self.assertEqual(forwarded['argv'], argv)
        self.assertEqual(Path(forwarded['cwd']).resolve(), self.repo.resolve())

    def test_probe_marker_as_body_text_does_not_turn_a_command_into_probe(self):
        self.install()
        self.record()
        result = self.invoke('pr', 'merge', self.url, '--body', '--starfleet-probe', '--auto')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--starfleet-probe', json.loads(self.forwarded.read_text())['argv'])

    def test_other_repo_and_branch_keep_original_working_directory_and_behavior(self):
        self.install()
        other = self.base / 'other'
        other.mkdir()
        # Explicit target outside the installed worktree must not be redirected into it.
        result = self.invoke('pr', 'ready', 'https://fixture.invalid/other/repo/pull/1', cwd=other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(self.forwarded.read_text())['cwd']).resolve(), other.resolve())
        self.assertEqual(self.hook('gh pr ready', cwd=other).returncode, 0)
        self.git('switch', '-c', 'other-lane')
        self.assertEqual(self.invoke('pr', 'ready').returncode, 0)

    def test_no_edit_dispatch_push_or_read_stops_are_installed(self):
        self.install()
        for command in ('git push', 'python3 implement.py', 'bash existing-review.sh', 'gh pr view 901'):
            self.assertEqual(self.hook(command).returncode, 0)
        result = self.call('hook', ok=False, input=json.dumps({'hook_event_name': 'PreToolUse',
                           'tool_name': 'Edit', 'cwd': str(self.repo), 'tool_input': {'file_path': str(self.repo / 'product.txt')}}))
        self.assertEqual(result.returncode, 0)

    def test_published_mismatch_and_changed_local_candidate_cannot_probe_successfully(self):
        self.install()
        self.record()
        self.remote['head']['sha'] = '0' * 40
        self.remote_path.write_text(json.dumps(self.remote))
        self.assertEqual(self.invoke('pr', 'ready', '--starfleet-probe').returncode, 2)
        self.git('commit', '--allow-empty', '-m', 'new head')
        self.assertEqual(self.invoke('pr', 'ready', '--starfleet-probe').returncode, 2)
        self.assertFalse(self.forwarded.exists())

    def test_behind_main_uses_live_target_without_rebase_or_verification_rerun(self):
        self.install()
        self.record()
        self.git('switch', 'main')
        (self.repo / 'main-only.txt').write_text('Unrelated solution on main\n')
        self.git('add', '.')
        self.git('commit', '-m', 'advance main')
        target = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('switch', 'feature/test')
        self.remote['target_head'] = target
        # The live branch advanced while PR metadata retained its older base SHA.
        self.assertEqual(self.remote['base']['sha'], self.head)
        self.remote_path.write_text(json.dumps(self.remote))
        self.assertEqual(self.invoke('pr', 'ready', self.url, '--starfleet-probe').returncode, 2)
        self.record()
        probe = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertFalse(json.loads(probe.stdout)['executed'])
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), self.head)
        # An actually newer published target still requires renewed evidence.
        self.remote['target_head'] = '2' * 40
        self.remote_path.write_text(json.dumps(self.remote))
        denied = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(denied.returncode, 2)
        self.assertIn('current target branch is', denied.stderr)
        self.assertFalse(self.forwarded.exists())

    def test_target_read_failure_or_wrong_ref_cannot_clear_the_gate(self):
        self.install()
        self.record()
        for response in ([], {'ref': 'refs/heads/MAIN', 'object': {'type': 'commit', 'sha': self.head}},
                         {'ref': 'refs/heads/main', 'object': []},
                         {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'main'}},
                         {'ref': 'refs/heads/main', 'object': {'type': 'tree', 'sha': self.head}}):
            self.remote['target_response'] = response
            self.remote_path.write_text(json.dumps(self.remote))
            result = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
            self.assertEqual(result.returncode, 2)
            self.assertIn('invalid ref identity', result.stderr)
        self.remote.pop('target_response')
        self.remote['target_error'] = True
        self.remote_path.write_text(json.dumps(self.remote))
        self.assertEqual(self.invoke('pr', 'ready', self.url, '--starfleet-probe').returncode, 2)
        self.assertFalse(self.forwarded.exists())

    def test_live_target_branch_preserves_case_and_slashes(self):
        self.git('branch', 'release/Next')
        self.remote['base']['ref'] = 'release/Next'
        self.remote_path.write_text(json.dumps(self.remote))
        args = ['--pr-url', self.url, '--base-ref', 'refs/heads/release/Next']
        plan = json.loads(self.call('plan', *args).stdout)
        self.call('install', *args, '--plan-sha256', plan['plan_sha256'])
        self.adapter = Path(plan['adapter'])
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = 'refs/heads/release/Next'
        self.packet.write_text(json.dumps(packet))
        self.record()
        result = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_audit_cannot_record_a_successful_run(self):
        self.install()
        self.packet.write_text(json.dumps({'pr_url': self.url, 'base_ref': 'main', 'design-fidelity': {'applicable': True}}))
        self.assertEqual(self.call('record', '--evidence', self.packet, ok=False).returncode, 2)
        self.assertIsNone(json.loads(self.call('status').stdout)['receipt'])

    def test_rollback_restores_exact_original_and_retains_passthrough_adapter(self):
        self.install()
        self.record()
        self.call('uninstall')
        self.assertEqual(self.settings.read_text(), self.original_raw)
        self.assertEqual(self.invoke('pr', 'ready').returncode, 0)
        self.assertEqual(json.loads(self.call('status').stdout)['phase'], 'removed')
        self.assertIsNotNone(json.loads(self.call('status').stdout)['receipt'])
        self.assertNotIn(str(LANE), self.adapter.read_text())
        self.call('uninstall')

    def test_rollback_preserves_new_owner_settings_and_refuses_modified_managed_hook(self):
        plan = self.install()
        value = json.loads(self.settings.read_text())
        value['owner_added'] = 'preserve'
        self.settings.write_text(json.dumps(value))
        self.call('uninstall')
        self.assertEqual(json.loads(self.settings.read_text()), {**self.original, 'owner_added': 'preserve'})
        self.install()
        value = json.loads(self.settings.read_text())
        owned = value['hooks']['PreToolUse'][-1]
        owned['hooks'][0]['timeout'] = 99
        self.settings.write_text(json.dumps(value))
        self.assertEqual(self.call('uninstall', ok=False).returncode, 2)
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['PreToolUse'][-1], owned)

    def test_reinstall_after_rollback_keeps_previous_receipt_history(self):
        self.install()
        self.record()
        self.call('uninstall')
        self.env['PATH'] = str(self.adapter.parent) + os.pathsep + self.env['PATH']
        self.install()
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        state = json.loads(state_path.read_text())
        self.assertIsNotNone(state['installation_history'][0]['receipt'])
        self.assertNotIn('receipt', state)

    def test_modified_local_adapter_is_not_overwritten_during_rollback(self):
        self.install()
        original_settings = self.settings.read_text()
        self.adapter.write_text(self.adapter.read_text() + '# Another owner changed this adapter\n')
        result = self.call('uninstall', ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('independently modified', result.stderr)
        self.assertEqual(self.settings.read_text(), original_settings)

    def test_session_appends_local_gh_path_without_replacing_existing_environment(self):
        self.install()
        envfile = self.base / 'session-env'
        envfile.write_text('export EXISTING_FLAG=keep\n')
        self.call('session', env={**self.env, 'CLAUDE_ENV_FILE': str(envfile)}, input=json.dumps({
            'hook_event_name': 'SessionStart', 'cwd': str(self.repo)}))
        content = envfile.read_text()
        self.assertTrue(content.startswith('export EXISTING_FLAG=keep\n'))
        self.assertIn(str(self.adapter.parent), content)
        self.assertIn('"$PATH"', content)

    def test_tracked_or_shared_settings_are_refused(self):
        self.git('add', '-f', '.claude/settings.local.json')
        self.git('commit', '-m', 'tracked configuration')
        self.assertEqual(self.call('plan', '--pr-url', self.url, '--base-ref', 'main', ok=False).returncode, 2)
        self.git('rm', '--cached', '.claude/settings.local.json')
        self.git('commit', '-m', 'local config again')
        outside = self.base / 'shared-settings.json'
        outside.write_text(self.original_raw)
        self.settings.unlink()
        self.settings.symlink_to(outside)
        self.assertEqual(self.call('plan', '--pr-url', self.url, '--base-ref', 'main', ok=False).returncode, 2)

    def test_incomplete_installation_stops_only_covered_pr_actions(self):
        self.install()
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        state = json.loads(state_path.read_text())
        state['phase'] = 'installing'
        state_path.write_text(json.dumps(state))
        self.assertEqual(self.invoke('pr', 'ready', '--starfleet-probe').returncode, 2)
        self.assertEqual(self.hook('git push').returncode, 0)
        self.call('uninstall')

    def owner_seat(self):
        owner = self.base / 'orchestrator seat'
        self.git('worktree', 'add', '-b', 'owner/home', owner)
        (owner / '.claude').mkdir()
        (owner / '.claude/settings.local.json').write_text(self.original_raw)
        return owner

    def test_temporary_product_binding_loads_from_permanent_session_context(self):
        owner = self.owner_seat()
        product_settings = self.settings.read_text()
        planned = self.install(owner=owner)
        self.assertEqual(Path(planned['settings']).resolve(), (owner / '.claude/settings.local.json').resolve())
        self.assertEqual(self.settings.read_text(), product_settings)
        self.assertIn('--session-root', planned['groups']['PreToolUse']['hooks'][0]['command'])
        self.assertEqual(self.invoke('pr', 'ready', '901', '--starfleet-probe', cwd=owner).returncode, 2)
        event = json.dumps({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': str(owner),
                            'tool_input': {'command': 'gh pr ready 901 --starfleet-probe'}})
        self.assertEqual(self.call('--session-root', owner, 'hook', input=event, ok=False).returncode, 2)
        self.record()
        self.assertEqual(self.call('--session-root', owner, 'hook', input=event).returncode, 0)
        result = self.invoke('pr', 'ready', '901', '--starfleet-probe', cwd=owner)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['executed'])
        self.invoke('pr', 'ready', '901', cwd=owner)
        self.assertEqual(Path(json.loads(self.forwarded.read_text())['cwd']).resolve(), owner.resolve())
        self.call('uninstall')
        self.assertEqual((owner / '.claude/settings.local.json').read_text(), self.original_raw)
        self.assertEqual(self.settings.read_text(), product_settings)

    def test_session_start_activates_the_product_adapter_from_owner_seat(self):
        owner = self.owner_seat()
        self.install(owner=owner)
        envfile = self.base / 'owner-session-env'
        envfile.write_text('export KEEP=yes\n')
        self.call('--session-root', owner, 'session', env={**self.env, 'CLAUDE_ENV_FILE': str(envfile)},
                  input=json.dumps({'hook_event_name': 'SessionStart', 'cwd': str(owner)}))
        self.assertIn(str(self.adapter.parent), envfile.read_text())
        self.assertTrue(envfile.read_text().startswith('export KEEP=yes'))

    def test_explicit_published_branch_mapping_keeps_local_branch_unchanged(self):
        self.remote['head']['ref'] = 'published-head'
        self.remote_path.write_text(json.dumps(self.remote))
        self.assertEqual(self.call('plan', '--pr-url', self.url, '--base-ref', 'main', ok=False).returncode, 2)
        self.install(head_branch='published-head')
        self.record()
        result = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('branch', '--show-current').stdout.strip(), 'feature/test')

    def test_two_product_worktrees_share_one_owner_without_mixing_receipts(self):
        owner = self.owner_seat()
        first_plan = self.install(owner=owner)
        self.record()
        second = self.base / 'second product'
        self.git('worktree', 'add', '-b', 'feature/second', second)
        second_url = self.url.replace('/901', '/902')
        second_remote = json.loads(json.dumps(self.remote))
        second_remote['html_url'] = second_url
        second_remote['head']['ref'] = 'feature/second'
        self.remote_path.write_text(json.dumps({'records': {'901': self.remote, '902': second_remote}}))
        second_plan = json.loads(self.call('--session-root', owner, 'plan', '--pr-url', second_url,
                                          '--base-ref', 'main', cwd=second).stdout)
        self.call('--session-root', owner, 'install', '--pr-url', second_url, '--base-ref', 'main',
                  '--plan-sha256', second_plan['plan_sha256'], cwd=second)
        # Either SessionStart PATH order must route to the selected PR's actual candidate.
        for adapter in (first_plan['adapter'], second_plan['adapter']):
            result = subprocess.run([adapter, 'pr', 'ready', '902', '--starfleet-probe'], cwd=owner,
                                    env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('No recorded delivery report run', result.stderr)
        second_packet = self.base / 'second-packet.json'
        second_packet.write_text(json.dumps({'pr_url': second_url, 'base_ref': 'main', 'design-fidelity': {
            'applicable': False, 'reason': 'No design', 'revision': self.head}}))
        self.call('record', '--evidence', second_packet, cwd=second)
        for adapter in (first_plan['adapter'], second_plan['adapter']):
            for number in ('901', '902'):
                result = subprocess.run([adapter, 'pr', 'ready', number, '--starfleet-probe'], cwd=owner,
                                        env=self.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.call('uninstall')
        # The removed first adapter must not bypass the remaining product gate in an older PATH.
        result = subprocess.run([first_plan['adapter'], 'pr', 'ready', '902', '--starfleet-probe'], cwd=owner,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['probe'])
        self.call('uninstall', cwd=second)

    def test_session_root_from_another_repo_is_refused(self):
        unrelated = self.base / 'unrelated'
        unrelated.mkdir()
        subprocess.run(['git', '-C', str(unrelated), 'init'], capture_output=True, check=True)
        result = self.call('--session-root', unrelated, 'plan', '--pr-url', self.url, '--base-ref', 'main', ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('same repository', result.stderr)

    def test_repo_adapter_routes_another_registered_pr_to_its_own_receipt(self):
        self.install()
        self.record()
        second = self.base / 'another owner product'
        self.git('worktree', 'add', '-b', 'other-owner/other-owner', second)
        second_url = self.url.replace('/901', '/902')
        other = json.loads(json.dumps(self.remote))
        other['html_url'] = second_url
        other['head']['ref'] = 'other-owner/other-owner'
        self.remote_path.write_text(json.dumps({'records': {'901': self.remote, '902': other}}))
        plan = json.loads(self.call('plan', '--pr-url', second_url, '--base-ref', 'main', cwd=second).stdout)
        self.call('install', '--pr-url', second_url, '--base-ref', 'main', '--plan-sha256', plan['plan_sha256'], cwd=second)
        self.assertEqual(self.invoke('pr', 'ready', '902', '--starfleet-probe').returncode, 2)
        packet = self.base / 'another-owner-packet.json'
        packet.write_text(json.dumps({'pr_url': second_url, 'base_ref': 'main', 'design-fidelity': {
            'applicable': False, 'reason': 'No design', 'revision': self.head}}))
        self.call('record', '--evidence', packet, cwd=second)
        result = self.invoke('pr', 'ready', '902', '--starfleet-probe')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['pr_url'], second_url)

        second_state_path = Path(plan['adapter']).parent.parent / 'state.json'
        second_state_before = json.loads(second_state_path.read_text())
        second_state = json.loads(second_state_path.read_text())
        second_state.update(phase='removed', pr_url=self.url.replace('/901', '/999'))
        second_state_path.write_text(json.dumps(second_state, indent=2) + '\n')
        foreign_journal = second_state_path.with_name('upgrade-journal.json')
        foreign_journal.write_text(json.dumps({'root': second_state_before['root'], 'state': {
            'preimage': {'path': str(second_state_path), 'text': json.dumps(second_state_before)}}}, indent=2) + '\n')
        foreign_journal.chmod(0o600)
        blocked_original_scope = self.invoke('pr', 'ready', '902', '--starfleet-probe')
        self.assertEqual(blocked_original_scope.returncode, 2)
        self.assertIn('source upgrade is incomplete', blocked_original_scope.stderr)
        self.assertFalse(self.forwarded.exists())
        second_state_path.write_text('{ malformed current state')
        blocked_malformed_state = self.invoke('pr', 'ready', '902', '--starfleet-probe')
        self.assertEqual(blocked_malformed_state.returncode, 2)
        self.assertIn('source upgrade is incomplete', blocked_malformed_state.stderr)
        self.assertFalse(self.forwarded.exists())
        second_state_path.write_text(json.dumps(second_state, indent=2) + '\n')
        healthy_probe = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(healthy_probe.returncode, 0, healthy_probe.stderr)
        self.assertEqual(json.loads(healthy_probe.stdout)['pr_url'], self.url)

        # An unreadable foreign journal must not disable this healthy PR.
        read_result = self.invoke('pr', 'view', self.url)
        self.assertEqual(read_result.returncode, 0, read_result.stderr)
        forwarded_before = self.forwarded.read_bytes()
        foreign_journal.write_text('{ unreadable fixture journal')
        foreign_journal.chmod(0o600)
        healthy_probe = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(healthy_probe.returncode, 0, healthy_probe.stderr)
        self.assertEqual(json.loads(healthy_probe.stdout)['pr_url'], self.url)
        blocked_foreign = subprocess.run([plan['adapter'], 'pr', 'ready', '902'], cwd=second,
                                         env=self.env, capture_output=True, text=True)
        self.assertEqual(blocked_foreign.returncode, 2)
        self.assertIn('source upgrade is incomplete', blocked_foreign.stderr)
        self.assertEqual(self.forwarded.read_bytes(), forwarded_before)

    def test_installed_seat_runtime_attaches_another_checkout_and_forwards_unrelated_actions(self):
        owner = self.owner_seat()
        source = self.source_checkout('durable-seat-source')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        first = self.install_from_source(source, owner=owner, lane=True, env=owner_env)
        runtime = Path(first['seat_runtime_path'])
        seat_id = first['seat_id']
        adapter = Path(first['adapter'])
        original_settings = (owner / '.claude/settings.local.json').read_bytes()
        shutil.rmtree(source)

        second = self.base / 'second seat product'
        self.git('worktree', 'add', '-b', 'feature/second-seat', second)
        second_url = self.url.replace('/901', '/902')
        remote = json.loads(json.dumps(self.remote))
        remote.update(html_url=second_url)
        remote['head']['ref'] = 'feature/second-seat'
        self.remote_path.write_text(json.dumps({'records': {'901': self.remote, '902': remote}}))
        proposed = json.loads(self.call_source(runtime, '--session-root', owner, 'plan', '--lane',
            '--host', 'fixture.invalid', '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread',
            '--pr-url', second_url, '--base-ref', 'main', env=owner_env, cwd=second).stdout)
        self.call_source(runtime, '--session-root', owner, 'install', '--lane', '--host', 'fixture.invalid',
            '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread', '--pr-url', second_url,
            '--base-ref', 'main', '--plan-sha256', proposed['plan_sha256'], env=owner_env, cwd=second)

        seat = json.loads((Path(proposed['seat_dir']) / 'seat.json').read_text())
        self.assertEqual(len(seat['checkouts']), 2)
        self.assertEqual(seat['adapter'], str(adapter))
        self.assertEqual((owner / '.claude/settings.local.json').read_bytes(), original_settings)
        self.assertFalse((second / '.git/starfleet-delivery-lane/state.json').exists())
        self.assertTrue((runtime / 'guide.md').is_file())

        outside = self.base / 'outside git'
        outside.mkdir()
        passthrough = subprocess.run([str(adapter), 'pr', 'view', self.url], cwd=outside, env=owner_env,
                                     capture_output=True, text=True)
        self.assertEqual(passthrough.returncode, 0, passthrough.stderr)
        unrelated = subprocess.run([str(adapter), 'pr', 'ready',
            'https://fixture.invalid/unrelated/repo/pull/1'], cwd=outside, env=owner_env,
            capture_output=True, text=True)
        self.assertEqual(unrelated.returncode, 0, unrelated.stderr)
        forwarded = list(map(json.loads, self.forwarded.read_text().splitlines()))
        self.assertEqual([item['argv'] for item in forwarded], [
            ['pr', 'view', self.url], ['pr', 'ready', 'https://fixture.invalid/unrelated/repo/pull/1']])
        self.assertTrue(all(Path(item['cwd']).resolve() == outside.resolve() for item in forwarded))

        unknown = subprocess.run([str(adapter), 'pr', 'ready', '999', '--starfleet-probe'], cwd=second,
                                 env=owner_env, capture_output=True, text=True)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn('discover', unknown.stderr)
        self.assertEqual(len(list(map(json.loads, self.forwarded.read_text().splitlines()))), 2)

        unknown_with_repo = subprocess.run([str(adapter), 'pr', 'ready', '999', '--repo', 'starfleet/guard',
                                            '--starfleet-probe'], cwd=second, env=owner_env,
                                           capture_output=True, text=True)
        self.assertEqual(unknown_with_repo.returncode, 2)
        self.assertIn('discover', unknown_with_repo.stderr)
        self.assertEqual(len(list(map(json.loads, self.forwarded.read_text().splitlines()))), 2)

    def test_seat_gh_uses_full_pr_url_before_repo_selector_and_forwards_unattached_targets(self):
        source = self.source_checkout('seat-target-resolution')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        installed = self.install_from_source(source, lane=True, env=owner_env, lane_pr_url=self.url)
        adapter = Path(installed['adapter'])
        runtime = Path(installed['seat_runtime_path'])
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = 'refs/remotes/origin/main'
        self.packet.write_text(json.dumps(packet))
        self.call_source(runtime, 'record', '--evidence', self.packet, '--format', 'json', env=owner_env)

        exact_pr = subprocess.run([str(adapter), 'pr', 'ready', self.url, '--repo', 'foreign/repo',
                                   '--starfleet-probe'], cwd=self.repo, env=owner_env, capture_output=True, text=True)
        self.assertEqual(exact_pr.returncode, 0, exact_pr.stderr)
        self.assertTrue(json.loads(exact_pr.stdout)['probe'])

        foreign_url = 'https://fixture.invalid/url-owner/url-repo/pull/42'
        forwarded_url = ['pr', 'ready', foreign_url, '--repo', 'other/selector']
        forwarded_repo = ['pr', 'ready', '901', '--repo', 'foreign/repo']
        for argv in (forwarded_url, forwarded_repo):
            result = subprocess.run([str(adapter), *argv], cwd=self.repo, env=owner_env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        forwarded = list(map(json.loads, self.forwarded.read_text().splitlines()))
        self.assertEqual([item['argv'] for item in forwarded], [forwarded_url, forwarded_repo])
        self.assertTrue(all(Path(item['cwd']).resolve() == self.repo.resolve() for item in forwarded))

        unknown = subprocess.run([str(adapter), 'pr', 'ready', '999', '--starfleet-probe'], cwd=self.repo,
                                 env=owner_env, capture_output=True, text=True)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn('discover', unknown.stderr)
        self.assertEqual(len(list(map(json.loads, self.forwarded.read_text().splitlines()))), 2)

    def test_seat_repository_requires_host_when_selector_matches_multiple_attached_hosts(self):
        seat = {'checkouts': {
            'first': {'root': str(self.repo), 'host': 'fixture.invalid', 'repository': 'starfleet/guard'},
            'second': {'root': str(self.base / 'other checkout'), 'host': 'enterprise.invalid',
                       'repository': 'Starfleet/Guard'}}}
        action = {'target': '901', 'repo': 'starfleet/guard'}
        with self.assertRaisesRegex(ValueError, 'full PR URL or HOST/OWNER/REPO'):
            lane_runtime.seat_repository(seat, self.repo, action)

    def test_codex_seat_install_and_attachment_need_no_claude_settings_metadata(self):
        self.settings.unlink()
        self.settings.parent.rmdir()
        source = self.source_checkout('codex-seat-settings-free')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        first = self.install_from_source(source, lane=True, env=owner_env)
        self.assertFalse(self.settings.parent.exists())
        seat_id = first['seat_id']
        seat_path = Path(first['seat_dir']) / 'seat.json'
        seat = json.loads(seat_path.read_text())
        self.assertIsNone(seat['settings'])
        self.assertFalse(seat['hooks_installed'])
        self.assertEqual(seat['groups'], {})
        runtime = Path(first['seat_runtime_path'])
        shutil.rmtree(source)

        second = self.base / 'codex seat second checkout'
        self.git('worktree', 'add', '-b', 'feature/codex-second', second)
        proposed = json.loads(self.call_source(runtime, '--session-root', self.repo, 'plan', '--lane',
            '--host', 'fixture.invalid', '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread',
            env=owner_env, cwd=second).stdout)
        self.call_source(runtime, '--session-root', self.repo, 'install', '--lane', '--host', 'fixture.invalid',
            '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread',
            '--plan-sha256', proposed['plan_sha256'], env=owner_env, cwd=second)

        updated_seat = json.loads(seat_path.read_text())
        self.assertEqual(updated_seat['seat_id'], seat_id)
        self.assertEqual(len(updated_seat['checkouts']), 2)
        self.assertEqual(updated_seat['adapter'], first['adapter'])
        self.assertIsNone(updated_seat['settings'])
        self.assertFalse(updated_seat['hooks_installed'])
        self.assertFalse(self.settings.parent.exists())
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane/state.json').exists())
        self.assertFalse((second / '.git/starfleet-delivery-lane/state.json').exists())

    def test_same_pr_reattachment_preserves_history_but_requires_a_fresh_report(self):
        owner = self.owner_seat()
        source = self.source_checkout('reattach-seat-source')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        first = self.install_from_source(source, owner=owner, lane=True, env=owner_env, lane_pr_url=self.url)
        runtime = Path(first['seat_runtime_path'])
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = 'refs/remotes/origin/main'
        self.packet.write_text(json.dumps(packet))
        self.call_source(runtime, 'record', '--evidence', self.packet, '--format', 'json', env=owner_env)
        first_state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        first_state_before = json.loads(first_state_path.read_text())
        old_receipt = first_state_before['receipt']

        second = self.base / 'same-pr new product root'
        cloned = subprocess.run(['git', 'clone', '--no-hardlinks', str(self.repo), str(second)],
                                capture_output=True, text=True)
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        subprocess.run(['git', '-C', str(second), 'remote', 'set-url', 'origin',
                        'git@fixture.invalid:starfleet/guard.git'], capture_output=True, check=True)
        clone_base_ref = 'refs/remotes/origin/main'
        proposed = json.loads(self.call_source(runtime, '--session-root', owner, 'plan', '--lane',
            '--host', 'fixture.invalid', '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread',
            '--pr-url', self.url, '--base-ref', clone_base_ref, env=owner_env, cwd=second).stdout)
        self.call_source(runtime, '--session-root', owner, 'install', '--lane', '--host', 'fixture.invalid',
            '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread', '--pr-url', self.url,
            '--base-ref', clone_base_ref, '--plan-sha256', proposed['plan_sha256'], env=owner_env, cwd=second)

        attached_state_path = Path(lane_runtime.directory(second)) / 'state.json'
        attached_state = json.loads(attached_state_path.read_text())
        self.assertNotIn('receipt', attached_state)
        self.assertIn(old_receipt, attached_state['pr_bindings'][self.url]['history'])
        self.assertEqual(json.loads(first_state_path.read_text())['receipt'], old_receipt)
        stale = subprocess.run([first['adapter'], 'pr', 'ready', self.url, '--starfleet-probe'],
                               cwd=second, env=owner_env, capture_output=True, text=True)
        self.assertEqual(stale.returncode, 2)
        self.assertIn('No recorded delivery report run', stale.stderr)
        self.assertFalse(self.forwarded.exists())

        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = clone_base_ref
        self.packet.write_text(json.dumps(packet))
        self.call_source(runtime, 'record', '--evidence', self.packet, '--format', 'json',
                         env=owner_env, cwd=second)
        fresh = subprocess.run([first['adapter'], 'pr', 'ready', self.url, '--starfleet-probe'],
                               cwd=second, env=owner_env, capture_output=True, text=True)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(json.loads(fresh.stdout)['pr_url'], self.url)

    def test_terminal_pr_observation_uses_the_github_pulls_rest_collection(self):
        source = self.source_checkout('pr-observation-endpoint')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        installed = self.install_from_source(source, lane=True, env=owner_env, lane_pr_url=self.url)
        self.api_calls.unlink(missing_ok=True)

        observed_open = self.call_source(Path(installed['seat_runtime_path']), '--seat-id', installed['seat_id'],
            'retire', '--pr-url', self.url, ok=False, env=owner_env)
        self.assertEqual(observed_open.returncode, 2)
        self.assertIn('PR is still open', observed_open.stderr)
        calls = list(map(json.loads, self.api_calls.read_text().splitlines()))
        self.assertEqual(calls, [{'endpoint': 'repos/starfleet/guard/pulls/901'}])

    def test_mixed_case_full_url_reaches_report_gate_and_probe(self):
        self.install()
        mixed = self.url.replace('starfleet/guard', 'Starfleet/Guard')
        first = self.invoke('pr', 'ready', mixed, '--starfleet-probe')
        self.assertEqual(first.returncode, 2)
        self.assertIn('No recorded delivery report run', first.stderr)
        self.record()
        result = self.invoke('pr', 'ready', mixed, '--starfleet-probe')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['would_forward'][3], mixed)

    def test_upgrade_plan_is_readonly_while_installation_is_active(self):
        old_source = self.source_checkout('read-only-old-pin')
        self.install_from_source(old_source)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        journal = state_path.parent / 'upgrade-journal.json'
        before = {'state': state_path.read_bytes(), 'settings': self.settings.read_bytes(),
                  'adapter': self.adapter.read_bytes(), 'lock': (state_path.parent / 'lock').read_bytes(),
                  'lock_mtime': (state_path.parent / 'lock').stat().st_mtime_ns}
        result = json.loads(self.call('upgrade-plan').stdout)
        self.assertEqual(result['binding_type'], 'legacy-per-pr')
        self.assertEqual(result['installed_source_before']['integrity'], 'intact')
        self.assertEqual(result['preimages']['state']['path'], str(state_path))
        self.assertEqual(result['preimages']['settings']['path'], str(self.settings))
        self.assertEqual(result['replacements']['adapter']['path'], str(self.adapter))
        self.assertEqual(set(result['replacements']['settings']['owned_hooks']['remove']),
                         {'PreToolUse', 'SessionStart'})
        self.assertFalse(journal.exists())
        self.assertEqual(state_path.read_bytes(), before['state'])
        self.assertEqual(self.settings.read_bytes(), before['settings'])
        self.assertEqual(self.adapter.read_bytes(), before['adapter'])
        self.assertEqual((state_path.parent / 'lock').read_bytes(), before['lock'])
        self.assertEqual((state_path.parent / 'lock').stat().st_mtime_ns, before['lock_mtime'])
        self.assertNotIn('EXISTING_FLAG', json.dumps(result))

    def test_status_separates_successor_inspector_from_intact_installed_pin(self):
        old_source = self.source_checkout('status-old-pin')
        self.install_from_source(old_source)
        self.call_source(old_source, 'record', '--evidence', self.packet, '--format', 'json')
        value = json.loads(self.call('status').stdout)
        self.assertEqual(value['binding_type'], 'legacy-per-pr')
        self.assertEqual(value['installed_source']['integrity'], 'intact')
        self.assertFalse(value['executing_cli']['matches_installed_pin'])
        self.assertEqual(value['activation']['source_intact'], True)
        self.assertFalse(value['activation']['executing_cli_matches_installed_pin'])
        self.assertEqual(value['report_status']['state'], 'not-evaluated')
        self.assertIn('not evaluated', value['report_status']['reason'])
        self.assertIn('Do not use rebind-owner', value['successor_guidance'])
        self.assertNotIn('stale', value['report_status']['reason'])

    def test_status_reports_changed_and_missing_installed_pin_files(self):
        old_source = self.source_checkout('integrity-old-pin')
        self.install_from_source(old_source)
        pinned_file = old_source / 'lane_scope.py'
        pinned_file.write_text(pinned_file.read_text() + '\n# local independent source change\n')
        changed = json.loads(self.call('status').stdout)
        self.assertEqual(changed['installed_source']['integrity'], 'changed')
        self.assertEqual(changed['installed_source']['changed_files'], ['lane_scope.py'])
        self.assertFalse(changed['activation']['source_intact'])
        pinned_file.unlink()
        missing = json.loads(self.call('status').stdout)
        self.assertEqual(missing['installed_source']['integrity'], 'missing')
        self.assertEqual(missing['installed_source']['missing_files'], ['lane_scope.py'])

    def test_legacy_upgrade_preserves_aliases_and_archives_only_its_receipt(self):
        self.remote['head']['ref'] = 'published-head'
        self.remote_path.write_text(json.dumps(self.remote))
        old_source = self.source_checkout('legacy-old-pin')
        self.install_from_source(old_source, head_branch='published-head')
        self.call_source(old_source, 'record', '--evidence', self.packet, '--format', 'json')
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        old_state = json.loads(state_path.read_text())
        old_receipt = old_state['receipt']
        old_settings_mode = self.settings.stat().st_mode & 0o777
        old_settings = json.loads(self.settings.read_text())
        old_state.pop('manages_claude_hooks', None)
        old_state.pop('lane_owner', None)
        state_path.write_text(json.dumps(old_state, indent=2) + '\n')
        proposed = json.loads(self.call('upgrade-plan').stdout)
        self.assertEqual(proposed['binding_type'], 'legacy-per-pr')
        mapping = proposed['identity']['branch_mappings'][0]
        self.assertEqual(mapping, {'branch': 'feature/test', 'head_branch': 'published-head'})
        self.call('upgrade', '--plan-sha256', proposed['plan_sha256'])
        upgraded = json.loads(state_path.read_text())
        self.assertNotIn('lane_owner', upgraded)
        self.assertNotIn('manages_claude_hooks', upgraded)
        self.assertEqual(upgraded['root'], str(self.repo))
        self.assertEqual(upgraded['branch'], 'feature/test')
        self.assertEqual(upgraded['head_branch'], 'published-head')
        self.assertEqual(upgraded['pr_url'], self.url)
        self.assertNotIn('receipt', upgraded)
        self.assertEqual(upgraded['history'], [old_receipt])
        self.assertEqual(upgraded['source'], str(APP))
        self.assertEqual(upgraded['source_revision'], lane_runtime.delivery.value(APP, 'rev-parse', 'HEAD'))
        self.assertEqual(self.git('branch', '--show-current').stdout.strip(), 'feature/test')
        self.assertEqual(self.settings.stat().st_mode & 0o777, old_settings_mode)
        current_settings = json.loads(self.settings.read_text())
        self.assertEqual(current_settings['permissions'], old_settings['permissions'])
        self.assertEqual(current_settings['env'], old_settings['env'])
        self.assertNotEqual(current_settings['hooks']['PreToolUse'][-1],
                            old_settings['hooks']['PreToolUse'][-1])
        self.assertFalse(self.invoke('pr', 'ready', self.url).returncode == 0)
        self.assertEqual(json.loads(self.call('status').stdout)['report_status']['state'], 'absent')

    def test_same_hash_path_upgrade_rejects_old_adapter_as_current_activation(self):
        old_source = self.source_checkout_same_fingerprints('same-content-old-path')
        self.install_from_source(old_source)
        self.call_source(old_source, 'record', '--evidence', self.packet, '--format', 'json')
        old_adapter = self.base / 'saved-old-adapter'
        old_adapter.write_bytes(self.adapter.read_bytes())
        old_adapter.chmod(0o700)
        prior_probe = subprocess.run([old_adapter, 'pr', 'ready', self.url, '--starfleet-probe'],
                                     cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(prior_probe.returncode, 0, prior_probe.stderr)
        prior_status = json.loads(self.call('status').stdout)
        self.assertEqual(prior_status['activation']['state'], 'explicit-adapter-observed')

        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        previous_state = json.loads(state_path.read_text())
        proposed = json.loads(self.call('upgrade-plan').stdout)
        self.assertEqual(proposed['installed_source_before']['expected_fingerprints'],
                         proposed['executing_source_after']['fingerprints'])
        self.assertEqual(proposed['plan_sha256'], json.loads(self.call('upgrade-plan').stdout)['plan_sha256'])
        self.call('upgrade', '--plan-sha256', proposed['plan_sha256'])
        upgraded_state = json.loads(state_path.read_text())
        self.assertNotEqual(upgraded_state['activation_generation'], previous_state['activation_generation'])
        self.assertEqual(upgraded_state['source'], str(APP))
        self.assertEqual(upgraded_state['source_versions'], previous_state['source_versions'])
        self.assertNotIn('receipt', upgraded_state)

        old_path_event = subprocess.run([old_adapter, 'pr', 'ready', self.url, '--starfleet-probe'],
                                        cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(old_path_event.returncode, 2)
        self.assertIn('No recorded delivery report run', old_path_event.stderr)
        status = json.loads(self.call('status').stdout)
        self.assertEqual(status['installed_source']['integrity'], 'intact')
        self.assertEqual(status['activation']['state'], 'unobserved')
        self.assertIsNone(status['activation']['last_adapter'])

    def test_record_rechecks_journal_after_waiting_for_state_lock(self):
        old_source = self.source_checkout('locked-writer-old-pin')
        self.install_from_source(old_source)
        journal = Path(lane_runtime.upgrade_journal_path(self.repo))
        initial_guard_passed = threading.Event()
        lock_attempted = threading.Event()
        worker_completed = threading.Event()
        run_reached = threading.Event()
        errors = []
        original_guard = lane_runtime.ensure_no_upgrade_journal
        original_file_locked = lane_runtime.file_locked

        def observe_initial_guard(cwd):
            original_guard(cwd)
            if not initial_guard_passed.is_set():
                initial_guard_passed.set()

        def worker():
            try:
                lane_runtime.record(SimpleNamespace(cwd=self.repo, evidence=self.packet,
                                                    policy=lane_runtime.delivery.POLICY, format='json'))
            except Exception as error:
                errors.append(error)
            finally:
                worker_completed.set()

        worker_thread = None

        def observe_lock_attempt(cwd):
            if threading.current_thread() is worker_thread:
                lock_attempted.set()
            return original_file_locked(cwd)

        with original_file_locked(self.repo):
            with mock.patch.object(lane_runtime, 'file_locked', side_effect=observe_lock_attempt), \
                    mock.patch.object(lane_runtime, 'ensure_no_upgrade_journal', side_effect=observe_initial_guard), \
                    mock.patch.object(lane_runtime.delivery_runs, 'run', side_effect=lambda *args: run_reached.set()):
                worker_thread = threading.Thread(target=worker)
                worker_thread.start()
                self.assertTrue(initial_guard_passed.wait(5), 'writer did not pass its first journal check')
                self.assertTrue(lock_attempted.wait(5), 'writer did not queue for the state lock')
                self.assertFalse(worker_completed.wait(0.05), 'writer did not wait behind the held state lock')
                journal.write_text('{}\n')
                journal.chmod(0o600)
        worker_thread.join(5)
        self.assertFalse(worker_thread.is_alive(), 'writer did not leave its queued lock acquisition')
        self.assertFalse(run_reached.is_set(), 'writer ran report work after acquiring a pending journal')
        self.assertEqual(len(errors), 1)
        self.assertIn('source upgrade is incomplete', str(errors[0]))
        self.assertNotIn('receipt', json.loads((journal.parent / 'state.json').read_text()))

    def test_codex_owner_upgrade_never_reads_or_creates_claude_settings(self):
        old_source = self.source_checkout('codex-old-pin')
        self.settings.unlink()
        self.settings.parent.rmdir()
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(old_source, lane=True, env=owner_env)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state = json.loads(state_path.read_text())
        state['owner_history'] = [{'from': {'harness': 'codex', 'session_id': 'prior'},
                                   'to': {'harness': 'codex', 'session_id': 'codex-fixture-thread'}}]
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        with mock.patch.dict(os.environ, owner_env), mock.patch.object(
                lane_runtime, 'settings', side_effect=AssertionError('Codex upgrade must not read Claude settings')):
            proposed, _ = lane_runtime.build_upgrade_plan(self.repo)
        self.assertEqual(proposed['binding_type'], 'native-owner-lane')
        self.assertFalse(proposed['replacements']['settings']['managed'])
        self.assertIsNone(proposed['replacements']['settings']['path'])
        self.assertEqual(proposed['replacements']['settings']['owned_hooks'], {})
        wrong_owner = dict(owner_env, CODEX_THREAD_ID='different-codex-thread')
        denied = self.call('upgrade', '--plan-sha256', proposed['plan_sha256'], ok=False, env=wrong_owner)
        self.assertEqual(denied.returncode, 2)
        self.assertIn('belongs to another native session', denied.stderr)
        self.assertFalse((state_path.parent / 'upgrade-journal.json').exists())
        self.call('upgrade', '--plan-sha256', proposed['plan_sha256'], env=owner_env)
        upgraded = json.loads(state_path.read_text())
        self.assertEqual(upgraded['lane_owner'], state['lane_owner'])
        self.assertEqual(upgraded['owner_history'], state['owner_history'])
        self.assertEqual(upgraded['pr_bindings'], {})
        self.assertEqual(upgraded['groups'], {})
        self.assertFalse(self.settings.parent.exists())
        status = json.loads(self.call('status', env=wrong_owner).stdout)
        self.assertTrue(status['current_owner']['mismatch'])
        self.assertIn('explicitly authorized rebind-owner', status['successor_guidance'])

    def test_codex_upgrade_recovery_requires_actual_native_owner(self):
        old_source = self.source_checkout('codex-recovery-old-pin')
        self.settings.unlink()
        self.settings.parent.rmdir()
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(old_source, lane=True, env=owner_env)
        proposed = json.loads(self.call('upgrade-plan', env=owner_env).stdout)
        self.interrupt_upgrade_after_settings(proposed['plan_sha256'], env=owner_env)
        journal = Path(lane_runtime.upgrade_journal_path(self.repo))
        wrong_owner = dict(owner_env, CODEX_THREAD_ID='different-codex-thread')
        denied = self.call('upgrade-recover', '--rollback', ok=False, env=wrong_owner)
        self.assertIn('belongs to another native session', denied.stderr)
        self.assertTrue(journal.exists())
        self.call('upgrade-recover', '--rollback', env=owner_env)
        self.assertFalse(journal.exists())
        self.assertFalse(self.settings.parent.exists())

    def test_upgrade_archives_each_lane_receipt_without_mixing_history_or_aliases(self):
        old_source = self.source_checkout('multi-pr-old-pin')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(old_source, lane=True, env=owner_env)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state = json.loads(state_path.read_text())
        second_url = self.url.replace('/901', '/902')
        first_receipt = {'fixture_receipt': 'first-pr-only'}
        second_receipt = {'fixture_receipt': 'second-pr-only'}
        state.update({'pr_url': self.url, 'branch': 'feature/test', 'head_branch': 'published/first',
                      'base_ref': 'main', 'receipt': first_receipt, 'history': [{'prior': 'first-history'}],
                      'pr_bindings': {
                          self.url: {'branch': 'feature/test', 'head_branch': 'published/first',
                                     'pr_url': self.url, 'base_ref': 'main', 'receipt': first_receipt,
                                     'history': [{'prior': 'first-history'}]},
                          second_url: {'branch': 'feature/second', 'head_branch': 'published/second',
                                       'pr_url': second_url, 'base_ref': 'main', 'receipt': second_receipt,
                                       'history': [{'prior': 'second-history'}]}}})
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        proposed = json.loads(self.call('upgrade-plan', env=owner_env).stdout)
        self.assertEqual(len(proposed['bindings']), 2)
        self.call('upgrade', '--plan-sha256', proposed['plan_sha256'], env=owner_env)
        upgraded = json.loads(state_path.read_text())
        first = upgraded['pr_bindings'][self.url]
        second = upgraded['pr_bindings'][second_url]
        self.assertNotIn('receipt', first)
        self.assertNotIn('receipt', second)
        self.assertEqual(first['history'], [{'prior': 'first-history'}, first_receipt])
        self.assertEqual(second['history'], [{'prior': 'second-history'}, second_receipt])
        self.assertEqual(first['head_branch'], 'published/first')
        self.assertEqual(second['head_branch'], 'published/second')
        self.assertEqual(upgraded['owner_history'], state['owner_history'])

    def test_seat_upgrade_accepts_changed_old_pin_across_all_active_checkouts(self):
        owner = self.owner_seat()
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        old_source = self.source_checkout('seat-multi-checkout-old-pin', marker_file='lane_scope.py')
        first = self.install_from_source(old_source, owner=owner, lane=True, env=owner_env,
                                         lane_pr_url=self.url)
        runtime = Path(first['seat_runtime_path'])
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = 'refs/remotes/origin/main'
        self.packet.write_text(json.dumps(packet))
        self.call_source(runtime, '--session-root', owner, 'record', '--evidence', self.packet,
                         '--format', 'json', env=owner_env)
        first_state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        first_receipt = json.loads(first_state_path.read_text())['receipt']

        second = self.base / 'seat second product checkout'
        self.git('worktree', 'add', '-b', 'feature/second-seat-checkout', second)
        (second / 'product.txt').write_text('Second seat candidate\n')
        subprocess.run(['git', '-C', str(second), 'add', 'product.txt'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(second), 'commit', '-m', 'second seat candidate'],
                       capture_output=True, check=True)
        second_head = subprocess.run(['git', '-C', str(second), 'rev-parse', 'HEAD'],
                                     capture_output=True, check=True, text=True).stdout.strip()
        second_url = self.url.replace('/901', '/902')
        first_remote = json.loads(json.dumps(self.remote))
        second_remote = json.loads(json.dumps(first_remote))
        second_remote['html_url'] = second_url
        second_remote['head'].update(ref='feature/second-seat-checkout', sha=second_head)
        self.remote_path.write_text(json.dumps({'records': {'901': first_remote, '902': second_remote}}))
        second_plan = json.loads(self.call_source(runtime, '--session-root', owner, 'plan', '--lane',
            '--host', 'fixture.invalid', '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread',
            '--pr-url', second_url, '--base-ref', 'main', env=owner_env, cwd=second).stdout)
        self.call_source(runtime, '--session-root', owner, 'install', '--lane', '--host', 'fixture.invalid',
            '--owner-harness', 'codex', '--owner-session', 'codex-fixture-thread', '--pr-url', second_url,
            '--base-ref', 'main', '--plan-sha256', second_plan['plan_sha256'], env=owner_env, cwd=second)
        second_state_path = Path(lane_runtime.directory(second)) / 'state.json'
        second_packet = self.base / 'seat-second-delivery.json'
        second_packet.write_text(json.dumps({'pr_url': second_url, 'base_ref': 'refs/remotes/origin/main', 'design-fidelity': {
            'applicable': False, 'revision': second_head, 'reason': 'No designs in this fixture'}}))
        self.call_source(runtime, '--session-root', owner, 'record', '--evidence', second_packet,
                         '--format', 'json', env=owner_env, cwd=second)
        second_receipt = json.loads(second_state_path.read_text())['receipt']

        state_before = first_state_path.read_bytes()
        adapter_before = self.adapter.read_bytes()
        runtime_scope = runtime / 'lane_scope.py'
        pinned_scope = runtime_scope.read_bytes()
        pinned_mode = runtime_scope.stat().st_mode & 0o777
        runtime_scope.chmod(pinned_mode | 0o200)
        try:
            runtime_scope.write_bytes(pinned_scope + b'\n# independent fixture tampering\n')
            tampered_pin = self.call('upgrade-plan', ok=False, env=owner_env)
            self.assertEqual(tampered_pin.returncode, 2)
            self.assertIn('Installed source pin is changed', tampered_pin.stderr)
            self.assertEqual(first_state_path.read_bytes(), state_before)
            self.assertEqual(self.adapter.read_bytes(), adapter_before)
            self.assertFalse(Path(lane_runtime.upgrade_journal_path(self.repo)).exists())
        finally:
            runtime_scope.write_bytes(pinned_scope)
            runtime_scope.chmod(pinned_mode)

        proposed = json.loads(self.call('upgrade-plan', env=owner_env).stdout)
        self.assertEqual(proposed['installed_source_before']['integrity'], 'intact')
        self.assertEqual(len(proposed['preimages']['states']), 2)
        old_fingerprints = proposed['installed_source_before']['expected_fingerprints']
        new_fingerprints = proposed['executing_source_after']['fingerprints']
        self.assertEqual([name for name in old_fingerprints if old_fingerprints[name] != new_fingerprints[name]],
                         ['lane_scope.py'])
        self.call('upgrade', '--plan-sha256', proposed['plan_sha256'], env=owner_env)

        for path, pr_url, receipt in ((first_state_path, self.url, first_receipt),
                                      (second_state_path, second_url, second_receipt)):
            upgraded = json.loads(path.read_text())
            binding = upgraded['pr_bindings'][pr_url]
            self.assertEqual(upgraded['source_versions'], new_fingerprints)
            self.assertEqual(upgraded['runtime']['version'], proposed['executing_source_after']['runtime_version'])
            self.assertNotIn('receipt', binding)
            self.assertEqual(binding['history'][-1], receipt)

    def test_stale_upgrade_plans_reject_settings_branch_and_adapter_drift(self):
        old_source = self.source_checkout('stale-plan-old-pin')
        self.install_from_source(old_source)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        adapter_before = self.adapter.read_bytes()
        state_before = state_path.read_bytes()
        installed_settings_before = self.settings.read_bytes()
        settings_plan = json.loads(self.call('upgrade-plan').stdout)
        changed = json.loads(self.settings.read_text())
        changed['owner_added'] = 'preserve'
        self.settings.write_text(json.dumps(changed, indent=2) + '\n')
        rejected = self.call('upgrade', '--plan-sha256', settings_plan['plan_sha256'], ok=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(self.adapter.read_bytes(), adapter_before)
        self.assertFalse((state_path.parent / 'upgrade-journal.json').exists())

        self.settings.write_bytes(installed_settings_before)
        branch_plan = json.loads(self.call('upgrade-plan').stdout)
        self.git('switch', '-c', 'branch-drift')
        branch_rejected = self.call('upgrade', '--plan-sha256', branch_plan['plan_sha256'], ok=False)
        self.assertEqual(branch_rejected.returncode, 2)
        self.assertFalse((state_path.parent / 'upgrade-journal.json').exists())
        self.git('switch', 'feature/test')

        adapter_plan = json.loads(self.call('upgrade-plan').stdout)
        self.adapter.write_bytes(adapter_before + b'# independent adapter edit\n')
        adapter_rejected = self.call('upgrade', '--plan-sha256', adapter_plan['plan_sha256'], ok=False)
        self.assertEqual(adapter_rejected.returncode, 2)
        self.assertIn('adapter is missing or changed', adapter_rejected.stderr)
        self.assertFalse((state_path.parent / 'upgrade-journal.json').exists())

    def test_source_change_after_upgrade_plan_invalidates_hash(self):
        old_source = self.source_checkout('source-drift-old-pin')
        self.install_from_source(old_source)
        new_source = self.source_checkout('source-drift-proposed')
        proposed = json.loads(self.call_source(new_source, 'upgrade-plan').stdout)
        lane_copy = new_source / 'lane.py'
        lane_copy.write_text(lane_copy.read_text() + '# committed source drift\n')
        subprocess.run(['git', '-C', str(new_source), 'add', 'lane.py'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(new_source), 'commit', '-m', 'change proposed source'],
                       capture_output=True, check=True)
        rejected = self.call_source(new_source, 'upgrade', '--plan-sha256', proposed['plan_sha256'], ok=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('Upgrade plan or a bound preimage changed', rejected.stderr)
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        self.assertEqual(json.loads(state_path.read_text())['source'], str(old_source))
        self.assertFalse((state_path.parent / 'upgrade-journal.json').exists())

    def test_plan_binds_actual_gitdirs_and_captured_gh_binary(self):
        old_source = self.source_checkout('identity-old-pin')
        self.install_from_source(old_source)
        proposed = json.loads(self.call('upgrade-plan').stdout)
        self.assertEqual(proposed['identity']['product_git']['gitdir'],
                         self.git('rev-parse', '--absolute-git-dir').stdout.strip())
        self.assertEqual(proposed['identity']['seat_git']['gitdir'], proposed['identity']['product_git']['gitdir'])
        self.assertEqual(proposed['identity']['product_git']['common_gitdir'],
                         proposed['identity']['seat_git']['common_gitdir'])
        captured = proposed['captured_real_gh']
        self.assertEqual(captured['path'], json.loads((self.repo / '.git/starfleet-delivery-lane/state.json').read_text())['real_gh'])
        self.assertEqual(captured['resolved_path'], str(Path(captured['path']).resolve()))
        fake_gh = Path(captured['path'])
        original_binary = fake_gh.read_bytes()
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        state_before = state_path.read_bytes()
        adapter_before = self.adapter.read_bytes()
        fake_gh.write_bytes(original_binary + b'\n# fixture binary drift\n')
        rejected = self.call('upgrade', '--plan-sha256', proposed['plan_sha256'], ok=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('Upgrade plan or a bound preimage changed', rejected.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(self.adapter.read_bytes(), adapter_before)
        self.assertFalse((state_path.parent / 'upgrade-journal.json').exists())

    def test_duplicate_concurrent_upgrade_apply_accepts_only_one_plan(self):
        old_source = self.source_checkout('concurrent-old-pin')
        self.install_from_source(old_source)
        proposed = json.loads(self.call('upgrade-plan').stdout)
        barrier = threading.Barrier(3)
        results = []

        def apply_same_plan():
            barrier.wait()
            results.append(subprocess.run([sys.executable, str(LANE), '--cwd', str(self.repo), 'upgrade',
                                          '--plan-sha256', proposed['plan_sha256']], env=self.env,
                                         capture_output=True, text=True))

        workers = [threading.Thread(target=apply_same_plan) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()
        self.assertEqual(sum(result.returncode == 0 for result in results), 1)
        self.assertEqual(sum(result.returncode == 2 for result in results), 1)
        state = json.loads((self.repo / '.git/starfleet-delivery-lane/state.json').read_text())
        self.assertEqual(state['phase'], 'active')
        self.assertEqual(state['source'], str(APP))
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane/upgrade-journal.json').exists())

    def interrupt_upgrade_after_settings(self, plan_sha256, env=None, product=None):
        product = Path(product or self.repo).resolve()
        state_path = Path(lane_runtime.directory(product)) / 'state.json'
        original_write = lane_runtime.write

        def fail_final_state(path, content, mode=0o600):
            path = Path(path)
            if path.resolve() == state_path.resolve() and json.loads(content).get('phase') == 'active' and \
                    json.loads(state_path.read_text()).get('phase') == 'installing':
                raise OSError('fixture interruption after settings replacement')
            return original_write(path, content, mode)

        with mock.patch.dict(os.environ, env or {}), mock.patch.object(lane_runtime, 'write', side_effect=fail_final_state):
            with self.assertRaises(ValueError) as caught:
                lane_runtime.upgrade(SimpleNamespace(cwd=product, plan_sha256=plan_sha256))
        self.assertIn('upgrade-recover --rollback', str(caught.exception))

    def test_interrupted_upgrade_new_adapter_blocks_explicit_and_implicit_actions_and_recovers(self):
        old_source = self.source_checkout('recovery-old-pin')
        plan = self.install_from_source(old_source)
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        old_adapter = self.adapter.read_bytes()
        old_state = state_path.read_bytes()
        old_settings = self.settings.read_bytes()
        proposed = json.loads(self.call('upgrade-plan').stdout)
        self.interrupt_upgrade_after_settings(proposed['plan_sha256'])
        journal = state_path.parent / 'upgrade-journal.json'
        self.assertTrue(journal.exists())
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(state_path.read_text())['phase'], 'installing')
        self.assertNotEqual(self.adapter.read_bytes(), old_adapter)
        interrupted_state = state_path.read_bytes()
        drifted_state = json.loads(interrupted_state)
        drifted_state.update(phase='removed', pr_url=self.url.replace('/901', '/999'))
        state_path.write_text(json.dumps(drifted_state, indent=2) + '\n')
        drifted_explicit = self.invoke('pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(drifted_explicit.returncode, 2)
        self.assertIn('source upgrade is incomplete', drifted_explicit.stderr)
        drifted_implicit = self.invoke('pr', 'ready')
        self.assertEqual(drifted_implicit.returncode, 2)
        self.assertIn('source upgrade is incomplete', drifted_implicit.stderr)
        self.assertFalse(self.forwarded.exists())
        state_path.write_bytes(interrupted_state)
        self.assertFalse(self.invoke('pr', 'ready', self.url, '--starfleet-probe').returncode == 0)
        self.assertFalse(self.invoke('pr', 'ready').returncode == 0)
        self.assertFalse(self.forwarded.exists())

        settings = json.loads(self.settings.read_text())
        settings['owner_added_during_recovery'] = 'keep'
        self.settings.write_text(json.dumps(settings, indent=2) + '\n')
        recovered = json.loads(self.call('upgrade-recover', '--rollback').stdout)
        self.assertTrue(recovered['rolled_back'])
        restored = json.loads(state_path.read_text())
        self.assertEqual(restored, json.loads(old_state))
        self.assertEqual(self.adapter.read_bytes(), old_adapter)
        self.assertEqual(json.loads(self.settings.read_text())['owner_added_during_recovery'], 'keep')
        self.assertEqual(json.loads(self.settings.read_text())['hooks'], json.loads(old_settings)['hooks'])
        self.assertFalse(journal.exists())
        second_recovery = json.loads(self.call('upgrade-recover', '--rollback').stdout)
        self.assertTrue(second_recovery['already_clean'])

    def test_journal_before_adapter_swap_leaves_old_installation_gate_coherent(self):
        old_source = self.source_checkout('pre-adapter-crash-old-pin')
        self.install_from_source(old_source)
        proposed = json.loads(self.call('upgrade-plan').stdout)
        original_adapter = self.adapter.read_bytes()
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        original_write = lane_runtime.write

        def fail_adapter(path, content, mode=0o600):
            if Path(path) == self.adapter:
                raise OSError('fixture interruption before adapter replacement')
            return original_write(path, content, mode)

        with mock.patch.object(lane_runtime, 'write', side_effect=fail_adapter):
            with self.assertRaises(ValueError) as caught:
                lane_runtime.upgrade(SimpleNamespace(cwd=self.repo, plan_sha256=proposed['plan_sha256']))
        self.assertIn('upgrade-recover --rollback', str(caught.exception))
        self.assertTrue((state_path.parent / 'upgrade-journal.json').exists())
        self.assertEqual(self.adapter.read_bytes(), original_adapter)
        self.assertEqual(json.loads(state_path.read_text())['phase'], 'active')
        self.assertFalse(self.invoke('pr', 'ready').returncode == 0)
        self.assertFalse(self.forwarded.exists())
        self.call('upgrade-recover', '--rollback')
        self.assertFalse((state_path.parent / 'upgrade-journal.json').exists())
        self.assertEqual(self.adapter.read_bytes(), original_adapter)

    def test_recovery_refuses_independently_modified_owned_hook_and_keeps_gate(self):
        old_source = self.source_checkout('recovery-hook-conflict-old-pin')
        self.install_from_source(old_source)
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        proposed = json.loads(self.call('upgrade-plan').stdout)
        self.interrupt_upgrade_after_settings(proposed['plan_sha256'])
        settings = json.loads(self.settings.read_text())
        owned = settings['hooks']['PreToolUse'][-1]
        owned['hooks'][0]['timeout'] = 77
        changed_raw = json.dumps(settings, indent=2) + '\n'
        self.settings.write_text(changed_raw)
        rejected = self.call('upgrade-recover', '--rollback', ok=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('changed independently', rejected.stderr)
        self.assertEqual(self.settings.read_text(), changed_raw)
        self.assertTrue((state_path.parent / 'upgrade-journal.json').exists())
        self.assertFalse(self.invoke('pr', 'ready').returncode == 0)
        self.assertFalse(self.forwarded.exists())

    def test_recovery_refuses_symlinked_settings_parent(self):
        old_source = self.source_checkout('recovery-symlink-parent-old-pin')
        self.install_from_source(old_source)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        proposed = json.loads(self.call('upgrade-plan').stdout)
        self.interrupt_upgrade_after_settings(proposed['plan_sha256'])
        installed_settings = json.loads(self.settings.read_text())
        guarded_adapter = self.adapter.read_bytes()
        original_directory = self.settings.parent
        parked_directory = self.base / 'parked-claude-settings'
        original_directory.rename(parked_directory)
        redirected_directory = self.base / 'external-settings-target'
        redirected_directory.mkdir()
        redirected_settings = redirected_directory / self.settings.name
        redirected_raw = json.dumps({'external_owner_value': 'preserve'}, indent=2) + '\n'
        redirected_settings.write_text(redirected_raw)
        original_directory.symlink_to(redirected_directory, target_is_directory=True)

        rejected = self.call('upgrade-recover', '--rollback', ok=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('symlinked managed path', rejected.stderr)
        self.assertEqual(redirected_settings.read_text(), redirected_raw)
        self.assertEqual(json.loads((parked_directory / self.settings.name).read_text()), installed_settings)
        self.assertTrue((state_path.parent / 'upgrade-journal.json').exists())
        self.assertEqual(self.adapter.read_bytes(), guarded_adapter)

    def test_recovery_refuses_independently_changed_state_and_adapter(self):
        old_source = self.source_checkout('recovery-owned-conflict-old-pin')
        self.install_from_source(old_source)
        state_path = self.repo / '.git/starfleet-delivery-lane/state.json'
        proposed = json.loads(self.call('upgrade-plan').stdout)
        self.interrupt_upgrade_after_settings(proposed['plan_sha256'])
        state = json.loads(state_path.read_text())
        state['installation_history'].append({'newer_owner_history': True})
        changed_state = json.dumps(state, indent=2) + '\n'
        state_path.write_text(changed_state)
        rejected_state = self.call('upgrade-recover', '--rollback', ok=False)
        self.assertIn('state changed independently', rejected_state.stderr)
        self.assertEqual(state_path.read_text(), changed_state)
        self.assertTrue((state_path.parent / 'upgrade-journal.json').exists())

        state['installation_history'].pop()
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        adapter_changed = self.adapter.read_bytes() + b'# independent owner change\n'
        self.adapter.write_bytes(adapter_changed)
        rejected_adapter = self.call('upgrade-recover', '--rollback', ok=False)
        self.assertIn('adapter changed independently', rejected_adapter.stderr)
        self.assertEqual(self.adapter.read_bytes(), adapter_changed)
        self.assertTrue((state_path.parent / 'upgrade-journal.json').exists())
