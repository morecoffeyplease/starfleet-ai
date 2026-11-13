"""Delivery-only installation preserves other controls and probes never publish."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
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
        binary.write_text('#!' + sys.executable + '\nimport json,os,sys,time\nfrom pathlib import Path\nfrom urllib.parse import parse_qs,unquote,urlsplit\n'
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
                          ' elif "/issues/comments/" in sys.argv[-1]:\n'
                          '  if data.get("comment_error"): sys.exit(1)\n'
                          '  time.sleep(float(data.get("comment_delay", 0)))\n'
                          '  print(json.dumps(data["comments"][sys.argv[-1].split("/")[-1]]))\n'
                          ' else: print(json.dumps(data["records"][sys.argv[-1].split("/")[-1]] if "records" in data else data))\n'
                          'else:\n'
                          ' with open(os.environ["FAKE_LANE_FORWARDED"],"a") as f:\n'
                          '  f.write(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd()})+"\\n")\n')
        binary.chmod(0o755)
        pattern_buffer = binary.parent / 'pattern-buffer'
        pattern_buffer.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\n'
                                  'with open(os.environ["FAKE_PATTERN_BUFFER_CALLS"],"a") as stream:\n'
                                  ' stream.write(json.dumps(sys.argv[1:])+"\\n")\n'
                                  'sys.stdout.write(Path(os.environ["FAKE_PATTERN_BUFFER_REPORT"]).read_text())\n')
        pattern_buffer.chmod(0o755)
        self.pattern_buffer_report_path = self.base / 'pattern-buffer-report.json'
        self.pattern_buffer_report_path.write_text('{"registered":false}\n')
        self.pattern_buffer_calls = self.base / 'pattern-buffer-calls.jsonl'
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.base / 'xdg-state'),
                        XDG_DATA_HOME=str(self.base / 'xdg-data'),
                        PATH=str(binary.parent) + os.pathsep + os.environ['PATH'],
                        FAKE_LANE_REMOTE=str(self.remote_path), FAKE_LANE_FORWARDED=str(self.forwarded),
                        FAKE_LANE_API=str(self.api_calls),
                        FAKE_PATTERN_BUFFER_REPORT=str(self.pattern_buffer_report_path),
                        FAKE_PATTERN_BUFFER_CALLS=str(self.pattern_buffer_calls))
        self.packet = self.base / 'delivery.json'
        self.packet.write_text(json.dumps({'pr_url': self.url, 'base_ref': 'main', 'design-fidelity': {
            'applicable': False, 'revision': self.head, 'reason': 'No designs in this fixture'}}))

    def restore_xdg_environment(self):
        for key, value in (('XDG_STATE_HOME', self.previous_xdg_state), ('XDG_DATA_HOME', self.previous_xdg_data)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def set_pattern_buffer_report(self, report):
        self.pattern_buffer_report_path.write_text(json.dumps(report) + '\n')

    def required_reads_report(self, state='current', generation=3):
        entries = []
        required_dir = self.base / 'required-guidance'
        required_dir.mkdir(exist_ok=True)
        for index in range(1, 4):
            name = 'required-guide-' + str(index)
            path = required_dir / (name + '.md')
            path.write_text('Synthetic required guidance ' + str(index) + '\n')
            entries.append({'name': name, 'path': str(path.resolve()), 'roles': ['implementer'],
                            'state': state, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'byte_count': path.stat().st_size,
                            'parts': [{'part': 1, 'state': 'current' if state == 'current' else 'pending'}]})
        return {'registered': True, 'ok': True, 'integrity_errors': [],
                'list_path': str(self.base / 'required-reads.json'), 'list_sha256': 'c' * 64,
                'list_digest_matches': True, 'generation': generation, 'harness': 'codex',
                'session_id': 'codex-fixture-thread', 'role': 'implementer', 'entries': entries}

    def terminal_env(self):
        return {key: value for key, value in self.env.items() if key not in (
            'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID', 'CMUX_AGENT_LAUNCH_KIND')}

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

    def attach_native_checkout(self, source, owner, checkout, owner_env):
        options = ['--lane', '--host', 'fixture.invalid', '--owner-harness', 'claude',
                   '--owner-session', owner_env['CLAUDE_CODE_SESSION_ID']]
        plan_result = self.call_source(source, '--session-root', owner, 'plan', *options,
                                       env=owner_env, cwd=checkout)
        self.assertEqual(plan_result.returncode, 0, plan_result.stderr)
        plan = json.loads(plan_result.stdout)
        installed = self.call_source(source, '--session-root', owner, 'install', *options,
                                     '--plan-sha256', plan['plan_sha256'], env=owner_env, cwd=checkout)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        return plan

    def seed_attachment_pr_history(self, seat_id, checkout_id, state_path):
        state = json.loads(state_path.read_text())
        first_url = self.url
        second_url = self.url.replace('/901', '/902')
        bindings = {}
        for index, url in enumerate((first_url, second_url), start=1):
            rows = [{'count': index * 10 + offset, 'fixture': f'preserved-{index}-{offset}'}
                    for offset in range(index)]
            bindings[url] = {'branch': state['branch'], 'head_branch': state['branch'],
                             'pr_url': url, 'base_ref': 'main',
                             'receipt': {'fixture_receipt': f'pr-{index}'}, 'history': rows}
        state.update({'pr_url': first_url, 'head_branch': state['branch'], 'base_ref': 'main',
                      'receipt': copy.deepcopy(bindings[first_url]['receipt']),
                      'history': copy.deepcopy(bindings[first_url]['history']),
                      'pr_bindings': bindings})
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        seat = lane_runtime.session_store.load_seat(seat_id)
        lane_runtime.session_store.sync_pr_links(seat, checkout_id, state)
        lane_runtime.session_store.save_seat(seat)
        return state

    def call_seat_retirement(self, source, seat_id, checkout, *args, env=None, cwd=None, ok=True):
        result = subprocess.run([sys.executable, str(source / 'lane.py'), '--seat-id', seat_id,
                                 'retire-attachment', '--checkout', str(checkout), *map(str, args)],
                                cwd=cwd or self.base, env=env or self.env, capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def call_seat_rebind(self, source, seat_id, from_harness, from_session, env, ok=True):
        return self.call_source(source, '--seat-id', seat_id, 'rebind-owner',
                                '--from-harness', from_harness, '--from-session', from_session,
                                '--confirm-handoff', env=env, ok=ok)

    def prepare_seat_with_retired_attachment(self, label):
        owner, owner_env = self.native_owner_fixture()
        source = self.source_checkout(label + '-old-runtime', marker_file='lane.py')
        active = self.add_native_seat_attachment(source, owner, self.repo, owner_env)
        retired_path = self.base / (label + ' retired attachment')
        retired_branch = 'feature/' + label + '-retired'
        retired = self.add_native_seat_attachment(
            source, owner, retired_path, owner_env, retired_branch)
        seat_id = active['seat_id']
        runtime = Path(active['seat_runtime_path'])
        self.assertEqual(retired['seat_id'], seat_id)
        attachment = next(row for row in lane_runtime.session_store.all_checkouts(seat_id)
                          if row['root'] == str(retired_path.resolve()))
        state_path = Path(attachment['checkout_dir']) / 'state.json'
        self.seed_attachment_pr_history(seat_id, attachment['checkout_id'], state_path)

        removed_worktree = subprocess.run(
            ['git', '-C', str(self.repo), 'worktree', 'remove', '--force', str(retired_path)],
            capture_output=True, text=True, env=owner_env)
        self.assertEqual(removed_worktree.returncode, 0, removed_worktree.stderr)
        removed_branch = subprocess.run(
            ['git', '-C', str(self.repo), 'branch', '-D', retired_branch],
            capture_output=True, text=True, env=owner_env)
        self.assertEqual(removed_branch.returncode, 0, removed_branch.stderr)

        planned = self.call_seat_retirement(runtime, seat_id, retired_path,
                                            env=owner_env, cwd=self.repo)
        plan = json.loads(planned.stdout)
        applied = self.call_seat_retirement(runtime, seat_id, retired_path, '--apply',
                                            '--plan-sha256', plan['plan_sha256'],
                                            env=owner_env, cwd=self.repo)
        self.assertTrue(json.loads(applied.stdout)['retired'])
        return {'owner': owner, 'owner_env': owner_env, 'source': source, 'seat_id': seat_id,
                'runtime': runtime, 'active': active, 'retired_state_path': state_path}

    def native_owner_fixture(self):
        owner = self.owner_seat()
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='claude',
                         CLAUDE_CODE_SESSION_ID='claude-fixture-session')
        owner_env.pop('CODEX_THREAD_ID', None)
        return owner, owner_env

    def add_native_seat_attachment(self, source, owner, checkout, owner_env, branch_name=None):
        if branch_name:
            result = subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '-b', branch_name,
                                     str(checkout)], capture_output=True, text=True, env=owner_env)
            self.assertEqual(result.returncode, 0, result.stderr)
        return self.attach_native_checkout(source, owner, checkout, owner_env)

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

    def invoke_owner(self, env, *args):
        return subprocess.run([str(self.adapter), *args], cwd=self.repo, env=env, capture_output=True, text=True)

    def invoke(self, *args, cwd=None):
        return subprocess.run([str(self.adapter), *args], cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)

    def hook(self, command, cwd=None):
        return self.call('hook', ok=False, input=json.dumps({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
                         'cwd': str(cwd or self.repo), 'tool_input': {'command': command}}))

    def test_coverage_without_installation_is_readonly_and_unknown(self):
        before = self.git('status', '--porcelain').stdout
        terminal_env = self.terminal_env()
        value = json.loads(self.call('coverage', '--pr-url', self.url, env=terminal_env).stdout)
        self.assertEqual(value['binding']['state'], 'not-installed')
        self.assertIsNone(value['source_intact'])
        self.assertEqual(value['setup_guidance']['action'], 'attach')
        self.assertIn('proof-only work need no enrollment', value['setup_guidance']['reason'])
        self.assertIn('guide --print', value['setup_guidance']['command'])
        self.assertEqual(set(value['fix_rounds']['paths'].values()), {'unverified'})
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane').exists())
        self.assertEqual(self.git('status', '--porcelain').stdout, before)
        self.assertFalse(self.forwarded.exists())

    def test_status_recommends_optional_attach_plan_with_actual_identity(self):
        source = self.source_checkout('optional-attach-guidance')
        owner_session = 'codex-optional-attach-fixture'
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID=owner_session)
        result = self.call_source(source, 'status', env=owner_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        recommendation = json.loads(result.stdout)['setup_guidance']
        self.assertEqual(recommendation['action'], 'attach')
        self.assertIn('proof-only work need no enrollment', recommendation['reason'])
        self.assertIn('your chosen permanent owner seat', recommendation['reason'])
        self.assertIn('--host fixture.invalid', recommendation['command'])
        self.assertIn('--owner-session ' + owner_session, recommendation['command'])

        plan = subprocess.run(shlex.split(recommendation['command']), cwd=self.repo, env=owner_env,
                              capture_output=True, text=True)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(json.loads(plan.stdout)['lane_owner']['session_id'], owner_session)
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane').exists())

    def test_setup_guidance_help_and_printable_guide_explain_limits(self):
        status_help = self.call('status', '--help').stdout
        coverage_help = self.call('coverage', '--help').stdout
        guide = self.call('guide', '--print').stdout
        normalized_status_help = ' '.join(status_help.split())
        normalized_coverage_help = ' '.join(coverage_help.split())
        self.assertIn('one read-only setup recommendation', normalized_status_help)
        self.assertIn('one setup recommendation', normalized_coverage_help)
        self.assertIn('setup_guidance', guide)
        self.assertIn('proof-only work do not need enrollment', guide)
        self.assertIn('does not authorize transfer', guide)
        self.assertIn('separate from setup', guide)

    def test_coverage_separates_old_pr_and_current_binding_without_writes(self):
        source = self.source_checkout('coverage-guidance')
        self.install_from_source(source)
        state = self.repo / '.git/starfleet-delivery-lane/state.json'
        before = state.read_bytes()
        value = json.loads(self.call_source(source, 'coverage').stdout)
        self.assertEqual(value['binding']['state'], 'local-binding-current-pr-unconfirmed')
        self.assertEqual(value['setup_guidance']['action'], 'none')
        self.assertIn('separate from setup', value['setup_guidance']['reason'])
        self.assertIn('record --help', value['setup_guidance']['command'])
        other = self.url.replace('/901', '/902')
        value = json.loads(self.call_source(source, 'coverage', '--pr-url', other).stdout)
        self.assertEqual(value['binding']['state'], 'different-pr-bound')
        self.assertEqual(value['binding']['installed_pr'], self.url)
        value = json.loads(self.call_source(source, 'coverage', '--pr-url', self.url).stdout)
        self.assertEqual(value['binding']['state'], 'matches-declared-pr-and-local-branch')
        self.assertFalse(value['binding']['hosted_pr_checked'])
        wrong_root = json.loads(before)
        wrong_root['root'] = str(self.base / 'different-product-root')
        state.write_text(json.dumps(wrong_root, indent=2) + '\n')
        value = json.loads(self.call_source(source, 'coverage').stdout)
        self.assertEqual(value['binding']['state'], 'inactive-local-binding')
        self.assertEqual(value['setup_guidance']['action'], 'inspect')
        self.assertIsNone(value['setup_guidance']['command'])
        summary = self.call_source(source, 'coverage', '--summary').stdout
        self.assertIn('Policy Guard: inactive delivery installation', summary)
        state.write_bytes(before)
        self.git('switch', '-c', 'another-branch')
        value = json.loads(self.call_source(source, 'coverage', '--pr-url', self.url).stdout)
        self.assertEqual(value['binding']['state'], 'inactive-local-binding')
        self.assertEqual(value['setup_guidance']['action'], 'inspect')
        self.assertIsNone(value['setup_guidance']['command'])
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
            self.assertEqual(detail['setup_guidance']['action'], 'none')
            self.assertIn('No report or setup change is needed now', detail['setup_guidance']['reason'])
            self.assertIsNone(detail['pr_url'])
            self.assertEqual(detail['report_status']['state'], 'absent')
            self.assertFalse(lane_runtime.active(state, self.repo))
            with self.assertRaisesRegex(ValueError, 'Lane is enrolled; after its real PR is published'):
                lane_runtime.require(state, self.repo)

            coverage = lane_runtime.coverage(self.repo)
            self.assertTrue(coverage['installed_here'])
            self.assertEqual(coverage['attachment_state'], 'active-here')
            self.assertEqual(coverage['binding']['state'], 'lane-enrolled-no-pr-binding')
            self.assertEqual(coverage['setup_guidance'], detail['setup_guidance'])

        summary = self.call('coverage', '--summary', env=owner_env).stdout
        self.assertIn('Policy Guard: delivery-only installation', summary)
        self.assertIn('bound=none (lane-enrolled-no-pr-binding', summary)
        self.assertIn('Report: absent', summary)
        self.assertIn('Next setup: none - The owner lane is attached before a PR exists.', summary)

        wrong_owner = dict(owner_env, CODEX_THREAD_ID='another-codex-thread')
        wrong_owner_status = json.loads(self.call('status', env=wrong_owner).stdout)
        self.assertTrue(wrong_owner_status['installed_here'])
        self.assertEqual(wrong_owner_status['current_owner']['state'], 'mismatch')
        self.assertEqual(wrong_owner_status['setup_guidance']['action'], 'handoff')
        self.assertIn('does not authorize transfer', wrong_owner_status['setup_guidance']['reason'])
        self.assertIn('--confirm-handoff', wrong_owner_status['setup_guidance']['command'])
        self.assertIn('--from-session codex-fixture-thread', wrong_owner_status['setup_guidance']['command'])
        with mock.patch.dict(os.environ, wrong_owner):
            with self.assertRaisesRegex(ValueError, 'belongs to another native session'):
                lane_runtime.require(state, self.repo)

        with mock.patch.dict(os.environ, owner_env), mock.patch.object(
                lane_runtime, 'source_versions', return_value={'lane.py': 'changed-source'}):
            with self.assertRaisesRegex(ValueError, 'Pinned delivery adapter source changed'):
                lane_runtime.require(state, self.repo)

        authorized_handoff = subprocess.run(shlex.split(wrong_owner_status['setup_guidance']['command']),
                                            cwd=self.repo, env=wrong_owner, capture_output=True, text=True)
        self.assertEqual(authorized_handoff.returncode, 0, authorized_handoff.stderr)
        rebound = lane_runtime.read(self.repo)
        self.assertEqual(rebound['lane_owner']['session_id'], wrong_owner['CODEX_THREAD_ID'])
        self.assertEqual(len(rebound['owner_history']), 1)

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

        # Recreate the immutable 15-file runtime layout from before the OpenCode plugin files were added.
        legacy_runtime_files = tuple(
            relative for relative in lane_runtime.session_store.RUNTIME_FILES
            if relative not in ('opencode-plugin/index.ts', 'opencode-plugin/policy.js'))
        legacy_runtime_rows = {}
        legacy_runtime_digest = hashlib.sha256()
        legacy_runtime_source = Path(old_runtime['path'])
        for relative in legacy_runtime_files:
            content = (legacy_runtime_source / relative).read_bytes()
            fingerprint = hashlib.sha256(content).hexdigest()
            legacy_runtime_rows[relative] = fingerprint
            legacy_runtime_digest.update(relative.encode() + b'\0' + bytes.fromhex(fingerprint))
        legacy_runtime_version = legacy_runtime_digest.hexdigest()
        legacy_runtime_path = Path(old_runtime['path']).parent / legacy_runtime_version
        legacy_runtime_path.mkdir()
        for relative in legacy_runtime_files:
            destination = legacy_runtime_path / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes((legacy_runtime_source / relative).read_bytes())
            destination.chmod(0o444)
        for directory in sorted(
                (path for path in legacy_runtime_path.rglob('*') if path.is_dir()),
                key=lambda path: len(path.parts), reverse=True):
            directory.chmod(0o555)
        legacy_runtime_path.chmod(0o555)
        legacy_runtime = {
            'version': legacy_runtime_version,
            'path': str(legacy_runtime_path),
            'files': legacy_runtime_rows,
            'source_revision': 'runtime:' + legacy_runtime_version,
        }
        legacy_source_versions = {
            name: legacy_runtime_rows[name] for name in lane_runtime.source_versions('claude')}
        unsupported_runtime_rows = dict(legacy_runtime_rows)
        unsupported_runtime_rows.pop('README.md')
        with self.assertRaisesRegex(ValueError, 'runtime file layout is unsupported'):
            lane_runtime.session_store.recorded_runtime_plan(
                legacy_runtime_path, unsupported_runtime_rows)
        changed_runtime_rows = dict(legacy_runtime_rows)
        changed_runtime_rows['README.md'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'runtime files changed'):
            lane_runtime.session_store.recorded_runtime_plan(
                legacy_runtime_path, changed_runtime_rows)
        seat_manifest = json.loads(seat_manifest_path.read_text())
        seat_manifest['runtime'] = legacy_runtime
        seat_manifest_path.write_text(json.dumps(seat_manifest, indent=2) + '\n')
        for checkout_root in (self.repo, second_product, existing_migration_product):
            checkout_state_path = (seat_dir / 'checkouts' /
                lane_runtime.session_store.checkout_id(checkout_root) / 'state.json')
            checkout_state = json.loads(checkout_state_path.read_text())
            checkout_state.update(source=str(legacy_runtime_path),
                                  source_revision='runtime:' + legacy_runtime_version,
                                  source_versions=legacy_source_versions,
                                  runtime={'version': legacy_runtime_version,
                                           'path': str(legacy_runtime_path),
                                           'files': legacy_runtime_rows})
            checkout_state_path.write_text(json.dumps(checkout_state, indent=2) + '\n')
        first_state = json.loads(first_state_path.read_text())

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
        self.assertIn('Inspect attachment, owner, source and report state', parent_help)
        self.assertIn('Inspect attachment, owner, source and report state', status_help)
        self.assertIn('Read-only attachment and observed-path coverage', parent_help)
        self.assertIn('attachment and observed-path coverage', coverage_help)
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
        terminal_env = self.terminal_env()
        result = self.call('coverage', '--summary', '--pr-url', self.url, env=terminal_env).stdout
        self.assertIn('not installed here', result)
        self.assertIn('proof-only work need no enrollment', result)
        self.assertIn('guide --print', result)
        self.assertIn('Fix-round enforcement: not installed by the delivery lane', result)
        self.assertLessEqual(len(result.splitlines()), 11)
        self.assertEqual(self.git('status', '--porcelain').stdout, before)
        self.assertFalse((self.repo / '.git/starfleet-delivery-lane').exists())
        self.assertFalse(self.forwarded.exists())

    def test_coverage_summary_receipt_freshness_and_binding_are_readonly(self):
        self.install()
        self.record()
        # Legacy per-PR installations have no native owner, including in a plain terminal.
        terminal_env = self.terminal_env()
        state = self.repo / '.git/starfleet-delivery-lane/state.json'
        before = state.read_bytes()
        valid = self.call('coverage', '--summary', '--pr-url', self.url, env=terminal_env).stdout
        self.assertIn('Report: valid-local-snapshot', valid)
        self.assertIn('repository checks and human approval requirements before covered delivery', valid)
        other = self.url.replace('/901', '/902')
        mismatch = self.call('coverage', '--summary', '--pr-url', other, env=terminal_env).stdout
        self.assertIn('declared=' + other, mismatch)
        self.assertIn('bound=' + self.url, mismatch)
        self.assertIn('PR note: The declared PR differs from the local binding.', mismatch)
        self.assertIn('Next setup: none', mismatch)
        self.assertIn('Report: valid-local-snapshot', mismatch)
        (self.repo / 'product.txt').write_text('Unverified change\n')
        stale = self.call('coverage', '--summary', env=terminal_env).stdout
        self.assertIn('Report: stale-or-unusable', stale)
        self.assertIn('separate from setup', stale)
        self.assertIn('needed only before covered delivery', stale)
        self.assertIn('Confirm the product branch and PR', mismatch)
        self.assertEqual(state.read_bytes(), before)
        self.assertFalse(self.forwarded.exists())

    def test_coverage_summary_prioritizes_recovery_over_report_work(self):
        old_source = self.source_checkout('summary-recovery-old-pin')
        new_source = self.source_checkout('summary-recovery-new-pin')
        owner_session = 'codex-fixture-thread'
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID=owner_session)
        self.settings.unlink()
        self.settings.parent.rmdir()
        self.install_from_source(old_source, lane=True, env=owner_env)
        planned = self.call_source(new_source, 'upgrade-plan', env=owner_env)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        with mock.patch.object(lane_runtime, 'APP', new_source), mock.patch.object(
                lane_runtime, 'SELF', new_source / 'lane.py'):
            self.interrupt_upgrade_after_settings(plan['plan_sha256'], env=owner_env)
        journal = Path(lane_runtime.upgrade_journal_path(self.repo))
        self.assertTrue(journal.exists())

        summary = self.call_source(new_source, 'coverage', '--summary', env=owner_env)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertIn('Report: absent', summary.stdout)
        self.assertIn('Next setup: recover - An upgrade journal is pending', summary.stdout)
        self.assertNotIn('record --help', summary.stdout)
        self.assertIn('Run: ', summary.stdout)
        recover_command = summary.stdout.split('Run: ', 1)[1].splitlines()[0]
        self.assertIn('upgrade-recover --rollback', recover_command)

        wrong_owner = dict(owner_env, CODEX_THREAD_ID='codex-different-owner')
        wrong = self.call_source(new_source, 'status', env=wrong_owner)
        self.assertEqual(wrong.returncode, 0, wrong.stderr)
        recommendation = json.loads(wrong.stdout)['setup_guidance']
        self.assertEqual(recommendation['action'], 'inspect')
        self.assertIsNone(recommendation['command'])
        self.assertIn('native owner or checkout context is not confirmed', recommendation['reason'])
        wrong_summary = self.call_source(new_source, 'coverage', '--summary', env=wrong_owner)
        self.assertEqual(wrong_summary.returncode, 0, wrong_summary.stderr)
        self.assertIn('Next setup: inspect - An upgrade journal is pending', wrong_summary.stdout)
        self.assertNotIn('Run: ', wrong_summary.stdout)

        recovered = subprocess.run(shlex.split(recover_command), cwd=self.repo, env=owner_env,
                                   capture_output=True, text=True)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(journal.exists())

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
        self.assertIn('Standalone reporting and proof-only work need no enrollment', result.stderr)
        self.assertIn('actual native owner', result.stderr)
        self.assertIn('real PR and target inputs', result.stderr)
        self.assertIn('Do not use guard.py enroll', result.stderr)
        self.assertNotIn('<actual-', result.stderr)

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

    def test_record_requires_registered_receipts_even_without_guidance_attestation(self):
        source = self.source_checkout('record-required-reads-without-attestation')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(source, lane=True, lane_pr_url=self.url, env=owner_env)
        self.call_source(source, 'discover', env=owner_env)
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = lane_runtime.read(self.repo)['base_ref']
        self.packet.write_text(json.dumps(packet))

        explicit_no_list = self.call_source(source, 'record', '--evidence', self.packet,
                                            '--format', 'json', env=owner_env)
        self.assertEqual(explicit_no_list.returncode, 0, explicit_no_list.stderr)
        self.assertNotIn('required_reads', lane_runtime.read(self.repo)['receipt'])
        calls_after_no_list = self.pattern_buffer_calls.read_text().splitlines()
        self.assertEqual(len(calls_after_no_list), 1)

        no_pattern_buffer_bin = self.base / 'record-without-pattern-buffer-bin'
        no_pattern_buffer_bin.mkdir()
        (no_pattern_buffer_bin / 'git').symlink_to(shutil.which('git'))
        fake_gh = Path(self.env['PATH'].split(os.pathsep)[0]) / 'gh'
        (no_pattern_buffer_bin / 'gh').symlink_to(fake_gh)
        absent_pattern_buffer_env = dict(owner_env, PATH=str(no_pattern_buffer_bin))
        absent_no_list = self.call_source(source, 'record', '--evidence', self.packet,
                                          '--format', 'json', env=absent_pattern_buffer_env)
        self.assertEqual(absent_no_list.returncode, 0, absent_no_list.stderr)
        explicit_data = json.loads(explicit_no_list.stdout)
        absent_data = json.loads(absent_no_list.stdout)
        explicit_data['snapshot'].pop('observed_at', None)
        absent_data['snapshot'].pop('observed_at', None)
        self.assertEqual(absent_data, explicit_data)
        self.assertEqual(self.pattern_buffer_calls.read_text().splitlines(), calls_after_no_list)
        self.assertNotIn('required_reads', lane_runtime.read(self.repo)['receipt'])

        pending = self.required_reads_report('pending', generation=3)
        self.set_pattern_buffer_report(pending)
        refused_pending = self.call_source(source, 'record', '--evidence', self.packet,
                                           env=owner_env, ok=False)
        self.assertEqual(refused_pending.returncode, 2)
        for entry in pending['entries']:
            self.assertIn(entry['name'], refused_pending.stderr)
            self.assertIn('read-required', refused_pending.stderr)

        current = self.required_reads_report('current', generation=3)
        self.set_pattern_buffer_report(current)
        refused_omitted = self.call_source(source, 'record', '--evidence', self.packet,
                                          env=owner_env, ok=False)
        self.assertEqual(refused_omitted.returncode, 2)
        for entry in current['entries']:
            self.assertIn(entry['name'], refused_omitted.stderr)
            self.assertIn('missing from the packet reread list', refused_omitted.stderr)

        suggested = json.loads(self.call_source(source, 'attestation-rereads', env=owner_env).stdout)
        packet['guidance_attestation'] = {
            'stage': 'repair_batch',
            'reread': [{**entry, 'source_ref': 'https://fixture.invalid/guides/' + entry['name']}
                       for entry in suggested],
        }
        self.packet.write_text(json.dumps(packet))
        accepted = self.call_source(source, 'record', '--evidence', self.packet, env=owner_env)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        required_reads = lane_runtime.read(self.repo)['receipt']['required_reads']
        self.assertEqual(required_reads['list_sha256'], current['list_sha256'])
        self.assertEqual(required_reads['generation'], 3)
        self.assertEqual(required_reads['stage'], 'repair_batch')

    def test_successor_validation_reuses_exact_publications_across_two_handoffs(self):
        source = self.source_checkout_same_fingerprints('successor-validation')
        readme = self.repo / 'README.md'
        readme.write_text('Canonical verification: python3 verify.py\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'add canonical verification fixture')
        self.head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.remote['head']['sha'] = self.head
        self.remote_path.write_text(json.dumps(self.remote))

        owner_a = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex',
                       CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(source, lane=True, lane_pr_url=self.url, env=owner_a)
        self.call_source(source, 'discover', env=owner_a)
        state = lane_runtime.read(self.repo)
        packet = json.loads(self.packet.read_text())
        packet.update(pr_url=self.url, base_ref=state['base_ref'])
        packet['design-fidelity']['revision'] = self.head
        verify_log = self.base / 'verify-success.txt'
        verify_log.write_text('Synthetic successful canonical verification evidence.\n')
        packet['local-verification'] = {
            'revision': self.head,
            'command': ['python3', 'verify.py'],
            'command_source': {'path': str(readme), 'sha256': hashlib.sha256(readme.read_bytes()).hexdigest()},
            'exit_code': 0,
            'outcome': 'passed',
            'evidence': {'path': str(verify_log), 'sha256': hashlib.sha256(verify_log.read_bytes()).hexdigest()},
            'inputs': [],
            'input_scope': 'Synthetic fixture has no external verification inputs',
        }
        old_guidance = self.base / 'old-owner-guidance.md'
        old_guidance.write_text('Synthetic guidance read by the first owner.\n')
        packet['guidance_attestation'] = {
            'version': 1, 'stage': 'repair_batch',
            'issue_ref': 'https://fixture.invalid/starfleet/guard/issues/37',
            'accepted_batch_ref': 'https://fixture.invalid/starfleet/guard/issues/37#accepted-batch-1',
            'repair_history_ref': self.url + '#issuecomment-1',
            'prerequisites_complete': True, 'gaps': [],
            'reread': [{'kind': 'playbook', 'name': 'Existing repair playbook',
                        'source_ref': 'https://fixture.invalid/starfleet/guard/blob/abc/playbook.md',
                        'path': str(old_guidance),
                        'sha256': hashlib.sha256(old_guidance.read_bytes()).hexdigest()}],
            'guidance_application': 'The playbook bounds the existing batch to the approved target.',
            'approach': 'Keep the existing delivery path.',
            'approach_reason': 'The current covered path checks this publication.',
            'evidence_refs': ['The current synthetic PR packet.'],
            'assumptions': [], 'uncertainty': ['The fixture does not establish live GitHub delivery.'],
            'approved_scope_ref': 'https://fixture.invalid/starfleet/guard/issues/37#approved-scope',
            'human_holds_ref': 'No additional hold is represented in this fixture.',
        }
        self.packet.write_text(json.dumps(packet))
        comment_a = self.url + '#issuecomment-1001'
        rendered_a = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                      '--stage', 'repair-batch', env=owner_a).stdout
        self.remote['comments'] = {'1001': {
            'id': 1001, 'html_url': comment_a,
            'issue_url': 'https://api.fixture.invalid/repos/starfleet/guard/issues/901',
            'body': rendered_a,
        }}
        self.remote_path.write_text(json.dumps(self.remote))
        first_start = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'repair-batch', '--comment-url', comment_a, '--enable', env=owner_a)
        first_publication = json.loads(first_start.stdout)['publication']
        self.call_source(source, 'record', '--evidence', self.packet, env=owner_a)
        old_ready = self.invoke_owner(owner_a, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(old_ready.returncode, 0, old_ready.stderr)
        historical_body = self.remote['comments']['1001']['body']

        owner_b = dict(owner_a, CODEX_THREAD_ID='codex-successor-fixture')
        self.call_source(source, 'rebind-owner', '--from-harness', 'codex',
                         '--from-session', 'codex-fixture-thread', '--confirm-handoff', env=owner_b)
        after_first_handoff = lane_runtime.read(self.repo)
        self.assertIsNone(after_first_handoff.get('receipt'))
        self.assertEqual(after_first_handoff['guidance_attestation_ref'], first_publication)
        self.assertEqual(len(after_first_handoff['owner_history']), 1)
        status = json.loads(self.call_source(source, 'status', env=owner_b).stdout)
        self.assertEqual(status['guidance_attestation_ref'], first_publication)
        inherited_only = self.invoke_owner(owner_b, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(inherited_only.returncode, 2)

        def required_reads(session, state, generation):
            report = self.required_reads_report(state, generation=generation)
            report['session_id'] = session
            self.set_pattern_buffer_report(report)
            return report

        def successor_packet(predecessor, owner_work):
            packet_value = json.loads(self.packet.read_text())
            packet_value['guidance_attestation'] = {
                'version': 1, 'stage': 'successor_validation',
                'issue_ref': 'https://fixture.invalid/starfleet/guard/issues/37',
                'repair_history_ref': self.url + '#issuecomment-1001',
                'prior_publication_ref': copy.deepcopy(predecessor),
                'prerequisites_complete': True, 'gaps': [],
                'reread': [{'kind': 'playbook', 'name': 'Current playbook',
                            'source_ref': 'https://fixture.invalid/starfleet/guard/blob/def/playbook.md',
                            'path': str(current_guidance),
                            'sha256': hashlib.sha256(current_guidance.read_bytes()).hexdigest()}],
                'reconciliation': {
                    'inherited_work': 'Reviewed the completed work linked from the inherited publication.',
                    'repair_history': 'Reviewed existing repair history without accepting another batch.',
                    'scope_and_holds': 'Reconciled the existing approved scope and current human holds.',
                    'current_owner_work': owner_work,
                },
                'guidance_application': 'The current guidance supports this delivery review.',
                'approach': 'Continue the current candidate under the existing scope.',
                'approach_reason': 'The current evidence supports the approved outcome.',
                'evidence_refs': ['The current candidate report and PR files.'],
                'assumptions': [], 'uncertainty': ['The fixture does not establish live GitHub delivery.'],
                'approved_scope_ref': 'https://fixture.invalid/starfleet/guard/issues/37#approved-scope',
                'human_holds_ref': 'No additional hold is represented in this fixture.',
            }
            self.packet.write_text(json.dumps(packet_value))

        current_guidance = self.base / 'current-owner-guidance.md'
        current_guidance.write_text('Synthetic current guidance reread by each successor.\n')
        required_reads('codex-successor-fixture', 'pending', 3)
        rereads = json.loads(self.call_source(source, 'attestation-rereads', env=owner_b).stdout)
        successor_packet(first_publication, 'The incoming owner reviewed the current candidate and report.')
        for entry in rereads:
            packet_entry = {**entry, 'source_ref': 'https://fixture.invalid/guides/' + entry['name']}
            packet_value = json.loads(self.packet.read_text())
            packet_value['guidance_attestation']['reread'].append(packet_entry)
            self.packet.write_text(json.dumps(packet_value))
        pending_record = self.call_source(source, 'record', '--evidence', self.packet, env=owner_b, ok=False)
        self.assertEqual(pending_record.returncode, 2)
        pending_start = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', self.url + '#issuecomment-1002',
            env=owner_b, ok=False)
        self.assertEqual(pending_start.returncode, 2)
        enable_start = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', self.url + '#issuecomment-1002',
            '--enable', env=owner_b, ok=False)
        self.assertEqual(enable_start.returncode, 2)
        required_reads('codex-successor-fixture', 'current', 3)
        missing_report_start = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', self.url + '#issuecomment-1002',
            env=owner_b, ok=False)
        self.assertEqual(missing_report_start.returncode, 2)
        self.call_source(source, 'record', '--evidence', self.packet, env=owner_b)

        wrong_owner = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                       '--stage', 'successor-validation', env=owner_a, ok=False)
        self.assertEqual(wrong_owner.returncode, 2)
        wrong_predecessor = json.loads(self.packet.read_text())
        wrong_predecessor['guidance_attestation']['prior_publication_ref']['head_branch'] = 'wrong-published-branch'
        wrong_path = self.base / 'wrong-predecessor.json'
        wrong_path.write_text(json.dumps(wrong_predecessor))
        wrong_link = self.call_source(source, 'attestation-render', '--evidence', wrong_path,
                                      '--stage', 'successor-validation', env=owner_b, ok=False)
        self.assertEqual(wrong_link.returncode, 2)

        rendered_b = json.loads(self.call_source(
            source, 'attestation-render', '--evidence', self.packet,
            '--stage', 'successor-validation', '--format', 'json', env=owner_b).stdout)
        comment_b = self.url + '#issuecomment-1002'
        self.remote['comments']['1002'] = {
            'id': 1002, 'html_url': comment_b,
            'issue_url': 'https://api.fixture.invalid/repos/starfleet/guard/issues/901',
            'body': 'not the rendered current publication',
        }
        self.remote_path.write_text(json.dumps(self.remote))
        wrong_body = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', comment_b, env=owner_b, ok=False)
        self.assertEqual(wrong_body.returncode, 2)
        self.remote['comments']['1002']['body'] = rendered_b['body']
        self.remote_path.write_text(json.dumps(self.remote))
        published_b = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', comment_b, env=owner_b)
        publication_b = json.loads(published_b.stdout)['publication']
        self.assertEqual(publication_b['prior_publication_ref'], first_publication)
        self.assertEqual(lane_runtime.read(self.repo)['guidance_attestation_ref'], publication_b)
        self.assertEqual(self.remote['comments']['1001']['body'], historical_body)

        repeated = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', comment_b, env=owner_b)
        self.assertEqual(json.loads(repeated.stdout)['publication'], publication_b)
        current_ready = self.invoke_owner(owner_b, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(current_ready.returncode, 0, current_ready.stderr)
        self.remote['comment_delay'] = 3
        self.remote_path.write_text(json.dumps(self.remote))
        slow_comments = self.invoke_owner(owner_b, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(slow_comments.returncode, 2)
        self.assertIn('timed out', slow_comments.stderr)
        self.assertFalse(self.forwarded.exists())
        self.remote.pop('comment_delay')
        self.remote_path.write_text(json.dumps(self.remote))

        self.remote['comments']['1001']['body'] = 'changed inherited publication'
        self.remote_path.write_text(json.dumps(self.remote))
        changed_predecessor = self.invoke_owner(owner_b, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(changed_predecessor.returncode, 2)
        self.remote['comments']['1001']['body'] = historical_body
        self.remote_path.write_text(json.dumps(self.remote))
        inherited_comment = self.remote['comments'].pop('1001')
        self.remote_path.write_text(json.dumps(self.remote))
        missing_predecessor = self.invoke_owner(owner_b, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(missing_predecessor.returncode, 2)
        self.remote['comments']['1001'] = inherited_comment
        self.remote_path.write_text(json.dumps(self.remote))
        self.remote['comments']['1002']['body'] = 'changed current publication'
        self.remote_path.write_text(json.dumps(self.remote))
        changed_current = self.invoke_owner(owner_b, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(changed_current.returncode, 2)
        replacement_b_url = self.url + '#issuecomment-1004'
        self.remote['comments']['1004'] = {
            'id': 1004, 'html_url': replacement_b_url,
            'issue_url': 'https://api.fixture.invalid/repos/starfleet/guard/issues/901',
            'body': rendered_b['body'],
        }
        self.remote_path.write_text(json.dumps(self.remote))
        replacement_b = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', replacement_b_url, env=owner_b)
        publication_b = json.loads(replacement_b.stdout)['publication']
        self.assertEqual(publication_b['prior_publication_ref'], first_publication)
        self.assertEqual(self.remote['comments']['1002']['body'], 'changed current publication')
        self.assertEqual(self.invoke_owner(owner_b, 'pr', 'ready', self.url,
                                           '--starfleet-probe').returncode, 0)

        owner_c = dict(owner_a, CODEX_THREAD_ID='codex-third-fixture')
        self.call_source(source, 'rebind-owner', '--from-harness', 'codex',
                         '--from-session', 'codex-successor-fixture', '--confirm-handoff', env=owner_c)
        after_second_handoff = lane_runtime.read(self.repo)
        self.assertEqual(after_second_handoff['guidance_attestation_ref'], publication_b)
        self.assertEqual(len(after_second_handoff['owner_history']), 2)
        self.assertEqual(len(after_second_handoff['history']), 2)
        successor_packet(publication_b, 'The third owner reviewed the current candidate and report.')
        required_reads('codex-third-fixture', 'current', 4)
        self.call_source(source, 'record', '--evidence', self.packet, env=owner_c)
        rendered_c = json.loads(self.call_source(
            source, 'attestation-render', '--evidence', self.packet,
            '--stage', 'successor-validation', '--format', 'json', env=owner_c).stdout)
        comment_c = self.url + '#issuecomment-1003'
        self.remote['comments']['1003'] = {
            'id': 1003, 'html_url': comment_c,
            'issue_url': 'https://api.fixture.invalid/repos/starfleet/guard/issues/901',
            'body': rendered_c['body'],
        }
        self.remote_path.write_text(json.dumps(self.remote))
        published_c = self.call_source(
            source, 'attestation-start-check', '--evidence', self.packet,
            '--stage', 'successor-validation', '--comment-url', comment_c, env=owner_c)
        publication_c = json.loads(published_c.stdout)['publication']
        self.assertEqual(publication_c['prior_publication_ref'], publication_b)
        self.assertEqual(len(lane_runtime.read(self.repo)['owner_history']), 2)
        self.assertEqual(len(lane_runtime.read(self.repo)['history']), 2)
        self.assertEqual(self.remote['comments']['1001']['body'], historical_body)
        self.assertEqual(self.remote['comments']['1002']['body'], 'changed current publication')
        self.assertEqual(self.remote['comments']['1004']['body'], rendered_b['body'])
        self.assertFalse(self.forwarded.exists())
        self.assertEqual(self.invoke_owner(owner_c, 'pr', 'ready', self.url,
                                           '--starfleet-probe').returncode, 0)

    def test_published_guidance_attestation_requires_owner_current_packet_and_exact_remote_comment(self):
        source = self.source_checkout('published-guidance-attestation')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        plan = self.install_from_source(source, lane=True, lane_pr_url=self.url, env=owner_env)
        self.call_source(source, 'discover', env=owner_env)
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = lane_runtime.read(self.repo)['base_ref']
        self.packet.write_text(json.dumps(packet))
        legacy_report = json.loads(self.call_source(source, 'record', '--evidence', self.packet,
                                                    '--format', 'json', env=owner_env).stdout)
        self.assertGreater(legacy_report['missing'], 0)
        legacy_ready = self.invoke_owner(owner_env, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(legacy_ready.returncode, 0, legacy_ready.stderr)
        discovered_state = lane_runtime.read(self.repo)
        self.assertNotIn('attestation_required', discovered_state)
        evidence = self.base / 'owner-guidance.md'
        evidence.write_text('The selected current guidance.')
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = discovered_state['base_ref']
        packet['guidance_attestation'] = {
            'version': 1, 'stage': 'repair_batch',
            'issue_ref': 'https://fixture.invalid/starfleet/guard/issues/37',
            'accepted_batch_ref': 'https://fixture.invalid/starfleet/guard/issues/37#accepted-batch-1',
            'repair_history_ref': 'https://fixture.invalid/starfleet/guard/pull/901#issuecomment-1',
            'prerequisites_complete': True, 'gaps': [],
            'reread': [{'kind': 'playbook', 'name': 'Current repair playbook',
                        'source_ref': 'https://fixture.invalid/starfleet/guard/blob/abc/playbook.md',
                        'path': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}],
            'guidance_application': 'The playbook keeps this batch within the approved target.',
            'approach': 'Keep the existing PR packet and owner lane.',
            'approach_reason': 'A smaller note would not be checked on the covered ready path.',
            'evidence_refs': ['Existing issue evidence and the current PG packet.'],
            'assumptions': [], 'uncertainty': ['GitHub comment reads may be unavailable.'],
            'approved_scope_ref': 'https://fixture.invalid/starfleet/guard/issues/37#approved-scope',
            'human_holds_ref': 'No additional hold is recorded.',
        }
        self.packet.write_text(json.dumps(packet))
        comment_id = 1001
        comment_url = self.url + '#issuecomment-' + str(comment_id)
        pending_report = self.required_reads_report('pending', generation=3)
        self.set_pattern_buffer_report(pending_report)

        suggested = json.loads(self.call_source(source, 'attestation-rereads', env=owner_env).stdout)
        self.assertEqual([entry['name'] for entry in suggested],
                         [entry['name'] for entry in pending_report['entries']])
        self.assertTrue(all('source_ref' not in entry for entry in suggested))

        before_pending_record = lane_runtime.read(self.repo)['receipt']
        pending_record = self.call_source(source, 'record', '--evidence', self.packet,
                                          env=owner_env, ok=False)
        self.assertEqual(pending_record.returncode, 2)
        for entry in pending_report['entries']:
            self.assertIn(entry['name'], pending_record.stderr)
            self.assertIn('read-required', pending_record.stderr)
        self.assertEqual(lane_runtime.read(self.repo)['receipt'], before_pending_record)

        pending_render = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                          '--stage', 'repair-batch', env=owner_env, ok=False)
        self.assertEqual(pending_render.returncode, 2)
        for entry in pending_report['entries']:
            self.assertIn(entry['name'], pending_render.stderr)

        for stage, record_stage in (('repair-batch', 'repair_batch'), ('opening', 'opening')):
            packet['guidance_attestation']['stage'] = record_stage
            if record_stage == 'opening':
                packet['guidance_attestation'].pop('accepted_batch_ref', None)
                packet['guidance_attestation'].pop('repair_history_ref', None)
            else:
                packet['guidance_attestation']['accepted_batch_ref'] = (
                    'https://fixture.invalid/starfleet/guard/issues/37#accepted-batch-1')
                packet['guidance_attestation']['repair_history_ref'] = (
                    'https://fixture.invalid/starfleet/guard/pull/901#issuecomment-1')
            self.packet.write_text(json.dumps(packet))
            pending_start = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                             '--stage', stage, '--comment-url', comment_url, '--enable',
                                             env=owner_env, ok=False)
            self.assertEqual(pending_start.returncode, 2)
            for entry in pending_report['entries']:
                self.assertIn(entry['name'], pending_start.stderr)
                self.assertIn('read-required', pending_start.stderr)
            self.assertNotIn('attestation_required', lane_runtime.read(self.repo))

        packet['guidance_attestation']['stage'] = 'repair_batch'
        packet['guidance_attestation']['accepted_batch_ref'] = (
            'https://fixture.invalid/starfleet/guard/issues/37#accepted-batch-1')
        packet['guidance_attestation']['repair_history_ref'] = (
            'https://fixture.invalid/starfleet/guard/pull/901#issuecomment-1')
        current_report = self.required_reads_report('current', generation=3)
        self.set_pattern_buffer_report(current_report)
        suggestions = json.loads(self.call_source(source, 'attestation-rereads', env=owner_env).stdout)
        packet['guidance_attestation']['reread'].extend(
            {**entry, 'source_ref': 'https://fixture.invalid/starfleet/guard/blob/abc/' + entry['name'] + '.md'}
            for entry in suggestions)
        self.packet.write_text(json.dumps(packet))

        rendered = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                    '--stage', 'repair-batch', env=owner_env).stdout
        for entry in current_report['entries']:
            self.assertIn(entry['name'], rendered)
        self.assertEqual(rendered.count('[read in current context window, generation 3]'), 3)
        self.assertNotIn('[read in current context window',
                         next(line for line in rendered.splitlines() if 'Current repair playbook' in line))

        recorded = self.call_source(source, 'record', '--evidence', self.packet, env=owner_env)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        report_context = lane_runtime.read(self.repo)['receipt']['required_reads']
        self.assertEqual(report_context['list_sha256'], current_report['list_sha256'])
        self.assertEqual(report_context['generation'], 3)
        self.assertEqual(report_context['stage'], 'repair_batch')

        reset_report = self.required_reads_report('pending', generation=4)
        self.set_pattern_buffer_report(reset_report)
        reset_render = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                        '--stage', 'repair-batch', env=owner_env, ok=False)
        self.assertIn('generation 4', reset_render.stderr)
        reset_record = self.call_source(source, 'record', '--evidence', self.packet,
                                        env=owner_env, ok=False)
        self.assertIn('generation 4', reset_record.stderr)
        reset_start = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                       '--stage', 'repair-batch', '--comment-url', comment_url,
                                       '--enable', env=owner_env, ok=False)
        self.assertIn('generation 4', reset_start.stderr)
        self.set_pattern_buffer_report(current_report)

        self.remote['comments'] = {str(comment_id): {
            'id': comment_id, 'html_url': comment_url,
            'issue_url': 'https://api.fixture.invalid/repos/starfleet/guard/issues/901', 'body': 'old body'}}
        self.remote_path.write_text(json.dumps(self.remote))

        # A legacy attachment remains unmodified until its owner uses --enable.
        before = lane_runtime.read(self.repo)
        self.assertNotIn('attestation_required', before)
        skipped = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                   '--stage', 'repair-batch', '--comment-url', comment_url,
                                   env=owner_env, ok=False)
        self.assertEqual(skipped.returncode, 2)
        self.assertNotIn('attestation_required', lane_runtime.read(self.repo))

        wrong_actor = dict(owner_env, CODEX_THREAD_ID='codex-other-session')
        denied_actor = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                        '--stage', 'repair-batch', '--comment-url', comment_url, '--enable',
                                        env=wrong_actor, ok=False)
        self.assertIn('belongs to another native session', denied_actor.stderr)
        wrong_pr = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                    '--stage', 'repair-batch', '--comment-url',
                                    'https://fixture.invalid/starfleet/guard/pull/902#issuecomment-1001',
                                    '--enable', env=owner_env, ok=False)
        self.assertIn('currently bound pull request', wrong_pr.stderr)
        self.remote['base']['ref'] = 'other-target'
        self.remote_path.write_text(json.dumps(self.remote))
        wrong_target = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                        '--stage', 'repair-batch', '--comment-url', comment_url, '--enable',
                                        env=owner_env, ok=False)
        self.assertIn('branch and target must agree', wrong_target.stderr)
        self.remote['base']['ref'] = 'main'
        self.remote_path.write_text(json.dumps(self.remote))
        self.remote['comments'][str(comment_id)]['issue_url'] = 'https://api.fixture.invalid/repos/starfleet/guard/issues/902'
        self.remote_path.write_text(json.dumps(self.remote))
        wrong_membership = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                            '--stage', 'repair-batch', '--comment-url', comment_url, '--enable',
                                            env=owner_env, ok=False)
        self.assertIn('does not belong to the currently bound pull request', wrong_membership.stderr)
        self.remote['comments'][str(comment_id)]['issue_url'] = 'https://api.fixture.invalid/repos/starfleet/guard/issues/901'
        self.remote['comment_error'] = True
        self.remote_path.write_text(json.dumps(self.remote))

        unavailable = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                       '--stage', 'repair-batch', '--comment-url', comment_url, '--enable',
                                       env=owner_env, ok=False)
        self.assertIn('GitHub GET failed', unavailable.stderr)
        self.assertNotIn('attestation_required', lane_runtime.read(self.repo))

        self.remote.pop('comment_error')
        self.remote['comments'][str(comment_id)]['body'] = rendered
        self.remote_path.write_text(json.dumps(self.remote))
        started = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                   '--stage', 'repair-batch', '--comment-url', comment_url, '--enable', env=owner_env)
        self.assertEqual(json.loads(started.stdout)['status'], 'published_attestation_current')
        adopted = lane_runtime.read(self.repo)
        self.assertIs(adopted['attestation_required'], True)
        self.assertEqual(adopted['guidance_attestation_ref']['comment_url'], comment_url)
        self.assertEqual(adopted['guidance_attestation_ref']['required_reads']['list_sha256'],
                         current_report['list_sha256'])
        self.assertEqual(adopted['guidance_attestation_ref']['required_reads']['generation'], 3)

        def local_status_without_api_calls(env=owner_env, cli_source=source):
            before = self.api_calls.read_text().splitlines() if self.api_calls.exists() else []
            result = self.call_source(cli_source, 'status', env=env)
            after = self.api_calls.read_text().splitlines() if self.api_calls.exists() else []
            self.assertEqual(after, before)
            return json.loads(result.stdout)

        other_owner = copy.deepcopy(adopted)
        other_owner['lane_owner']['session_id'] = 'codex-successor-session'
        with self.assertRaisesRegex(ValueError, 'previous native owner'):
            lane_runtime.require_guidance_attestation(other_owner, self.repo, local_inspection=True)
        other_pr = copy.deepcopy(adopted)
        other_pr['pr_url'] = self.url.replace('901', '902')
        with self.assertRaisesRegex(ValueError, 'different PR binding'):
            lane_runtime.require_guidance_attestation(other_pr, self.repo, local_inspection=True)
        other_target = copy.deepcopy(adopted)
        other_target['base_ref'] = 'release'
        with self.assertRaisesRegex(ValueError, 'different branch or target binding'):
            lane_runtime.require_guidance_attestation(other_target, self.repo, local_inspection=True)

        self.call_source(source, 'record', '--evidence', self.packet, env=owner_env)
        local_status = local_status_without_api_calls()
        self.assertEqual(local_status['report_status']['state'], 'valid-local-snapshot')
        self.assertEqual(local_status['guidance_attestation_receipts']['list_sha256'],
                         current_report['list_sha256'])
        self.assertEqual(local_status['guidance_attestation_receipts']['generation'], 3)
        self.assertEqual(len(local_status['guidance_attestation_receipts']['entry_names']), 3)
        wrong_owner_status = local_status_without_api_calls(wrong_actor)
        self.assertEqual(wrong_owner_status['report_status']['state'], 'not-evaluated')
        self.assertIn('belongs to another native session', wrong_owner_status['report_status']['reason'])
        self.assertTrue(wrong_owner_status['current_owner']['mismatch'])

        pinned_source_file = Path(adopted['source']) / 'lane_scope.py'
        original_pinned_source = pinned_source_file.read_bytes()
        original_pinned_mode = pinned_source_file.stat().st_mode & 0o777
        try:
            pinned_source_file.chmod(original_pinned_mode | 0o200)
            pinned_source_file.write_bytes(original_pinned_source + b'\n# local pin control\n')
            changed_pin_status = local_status_without_api_calls(cli_source=Path(adopted['source']))
            self.assertEqual(changed_pin_status['installed_source']['integrity'], 'changed')
            self.assertEqual(changed_pin_status['report_status']['state'], 'not-evaluated')
        finally:
            pinned_source_file.write_bytes(original_pinned_source)
            pinned_source_file.chmod(original_pinned_mode)

        original_packet = self.packet.read_bytes()
        changed_packet = json.loads(original_packet)
        changed_packet['guidance_attestation']['approach'] += ' Changed after publication.'
        self.packet.write_text(json.dumps(changed_packet))
        changed_packet_status = local_status_without_api_calls()
        self.assertEqual(changed_packet_status['report_status']['state'], 'stale-or-unusable')
        self.assertIn('Local guidance attestation changed after publication',
                      changed_packet_status['report_status']['reason'])
        self.packet.write_bytes(original_packet)

        evidence.write_text('Changed source after publication.')
        changed_reread_status = local_status_without_api_calls()
        self.assertEqual(changed_reread_status['report_status']['state'], 'stale-or-unusable')
        self.assertIn('Evidence hash changed', changed_reread_status['report_status']['reason'])
        evidence.write_text('The selected current guidance.')

        pattern_calls_before_delivery = (self.pattern_buffer_calls.read_text().splitlines()
                                         if self.pattern_buffer_calls.exists() else [])
        current = self.invoke_owner(owner_env, 'pr', 'ready', self.url)
        self.assertEqual(current.returncode, 0, current.stderr)
        pattern_calls_after_delivery = (self.pattern_buffer_calls.read_text().splitlines()
                                        if self.pattern_buffer_calls.exists() else [])
        self.assertEqual(pattern_calls_after_delivery, pattern_calls_before_delivery)
        forwarded_before = self.forwarded.read_bytes()
        forwarded = list(map(json.loads, forwarded_before.splitlines()))
        self.assertEqual([item['argv'] for item in forwarded], [['pr', 'ready', self.url]])

        published_state = lane_runtime.read(self.repo)
        missing_reference = copy.deepcopy(published_state)
        missing_reference.pop('guidance_attestation_ref', None)
        missing_reference['pr_bindings'][self.url].pop('guidance_attestation_ref', None)

        def save_checkout_state(value):
            owner = value['lane_owner']
            gitdir = self.git('rev-parse', '--absolute-git-dir').stdout.strip()
            common_gitdir = self.git('rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            lane_runtime.session_store.write_checkout(
                value['seat_id'], self.repo, value,
                repository=owner['repository'], host=owner['host'],
                gitdir=gitdir, common_gitdir=common_gitdir)

        save_checkout_state(missing_reference)
        try:
            refused = {}
            for action in ('ready', 'merge'):
                result = self.invoke_owner(owner_env, 'pr', action, self.url)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.forwarded.read_bytes(), forwarded_before)
                refused[action] = result
        finally:
            save_checkout_state(published_state)

        guide_command = shlex.join([
            sys.executable,
            str(Path(published_state['runtime']['path']) / 'lane.py'),
            'guide', '--print'])
        self.assertIn('Published guidance attestations', refused['ready'].stderr)
        self.assertIn('Before the final start check', refused['ready'].stderr)
        self.assertLess(refused['ready'].stderr.index('Before the final start check'),
                        refused['ready'].stderr.index('attestation-start-check'))
        self.assertIn(guide_command, refused['ready'].stderr)
        printed_guide = subprocess.run(shlex.split(guide_command), cwd=self.repo, env=owner_env,
                                       capture_output=True, text=True)
        self.assertEqual(printed_guide.returncode, 0, printed_guide.stderr)
        self.assertIn('## Published guidance attestations', printed_guide.stdout)
        old_receipt = lane_runtime.read(self.repo)['receipt']

        evidence.write_text('Changed source after publication.')
        changed_reread_status = local_status_without_api_calls()
        self.assertEqual(changed_reread_status['report_status']['state'], 'stale-or-unusable')
        self.assertIn('Evidence hash changed', changed_reread_status['report_status']['reason'])
        stale_source = self.invoke_owner(owner_env, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(stale_source.returncode, 2)
        self.assertIn('Evidence hash changed', stale_source.stderr)
        self.assertEqual(lane_runtime.read(self.repo)['receipt'], old_receipt)
        evidence.write_text('The selected current guidance.')

        self.remote['comments'][str(comment_id)]['body'] = 'edited remotely'
        self.remote_path.write_text(json.dumps(self.remote))
        edited_status = local_status_without_api_calls()
        self.assertEqual(edited_status['report_status']['state'], 'valid-local-snapshot')
        edited = self.invoke_owner(owner_env, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(edited.returncode, 2)
        self.assertIn('edited or no longer matches', edited.stderr)

        edited_start = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                        '--stage', 'repair-batch', '--comment-url', comment_url,
                                        env=owner_env, ok=False)
        self.assertEqual(edited_start.returncode, 2)
        self.assertIn('Published comment body does not match', edited_start.stderr)

        del self.remote['comments'][str(comment_id)]
        self.remote_path.write_text(json.dumps(self.remote))
        missing_start = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                         '--stage', 'repair-batch', '--comment-url', comment_url,
                                         env=owner_env, ok=False)
        self.assertEqual(missing_start.returncode, 2)
        self.assertIn('GitHub GET failed', missing_start.stderr)
        missing_check = self.invoke_owner(owner_env, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(missing_check.returncode, 2)
        self.assertIn('GitHub GET failed', missing_check.stderr)

        self.remote['comment_error'] = True
        self.remote_path.write_text(json.dumps(self.remote))
        unavailable_start = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                             '--stage', 'repair-batch', '--comment-url', comment_url,
                                             env=owner_env, ok=False)
        self.assertEqual(unavailable_start.returncode, 2)
        self.assertIn('GitHub GET failed', unavailable_start.stderr)
        unavailable_check = self.invoke_owner(owner_env, 'pr', 'ready', self.url, '--starfleet-probe')
        self.assertEqual(unavailable_check.returncode, 2)
        self.assertIn('GitHub GET failed', unavailable_check.stderr)
        self.assertEqual(lane_runtime.read(self.repo)['receipt'], old_receipt)

        no_pattern_buffer_bin = self.base / 'without-pattern-buffer-bin'
        no_pattern_buffer_bin.mkdir()
        system_git = shutil.which('git')
        fake_gh = Path(self.env['PATH'].split(os.pathsep)[0]) / 'gh'
        (no_pattern_buffer_bin / 'git').symlink_to(system_git)
        (no_pattern_buffer_bin / 'gh').symlink_to(fake_gh)
        absent_pattern_buffer_env = dict(owner_env, PATH=str(no_pattern_buffer_bin))
        self.set_pattern_buffer_report({'registered': False})

        explicit_render = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                           '--stage', 'repair-batch', env=owner_env).stdout
        absent_render = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                         '--stage', 'repair-batch', env=absent_pattern_buffer_env).stdout
        self.assertEqual(absent_render, explicit_render)
        explicit_render_json = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                                '--stage', 'repair-batch', '--format', 'json',
                                                env=owner_env).stdout
        absent_render_json = self.call_source(source, 'attestation-render', '--evidence', self.packet,
                                              '--stage', 'repair-batch', '--format', 'json',
                                              env=absent_pattern_buffer_env).stdout
        self.assertEqual(absent_render_json, explicit_render_json)
        self.assertNotIn('required_reads', json.loads(absent_render_json))

        explicit_record = self.call_source(source, 'record', '--evidence', self.packet,
                                           '--format', 'json', env=owner_env).stdout
        absent_record = self.call_source(source, 'record', '--evidence', self.packet,
                                         '--format', 'json', env=absent_pattern_buffer_env).stdout
        explicit_record_data = json.loads(explicit_record)
        absent_record_data = json.loads(absent_record)
        explicit_record_data['snapshot'].pop('observed_at', None)
        absent_record_data['snapshot'].pop('observed_at', None)
        self.assertEqual(absent_record_data, explicit_record_data)
        self.assertNotIn('required_reads', lane_runtime.read(self.repo)['receipt'])

        no_list_render = explicit_render
        self.remote.pop('comment_error', None)
        self.remote['comments'] = {str(comment_id): {
            'id': comment_id, 'html_url': comment_url,
            'issue_url': 'https://api.fixture.invalid/repos/starfleet/guard/issues/901',
            'body': no_list_render}}
        self.remote_path.write_text(json.dumps(self.remote))
        explicit_start = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                          '--stage', 'repair-batch', '--comment-url', comment_url,
                                          env=owner_env)
        absent_start = self.call_source(source, 'attestation-start-check', '--evidence', self.packet,
                                        '--stage', 'repair-batch', '--comment-url', comment_url,
                                        env=absent_pattern_buffer_env)
        self.assertEqual(absent_start.stdout, explicit_start.stdout)
        publication = json.loads(absent_start.stdout)['publication']
        self.assertNotIn('required_reads', publication)
        status_after_no_list = json.loads(self.call_source(source, 'status', env=owner_env).stdout)
        self.assertNotIn('guidance_attestation_receipts', status_after_no_list)

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
        immutable_runtime = self.base / 'immutable-successor-runtime'
        shutil.copytree(APP, immutable_runtime, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        terminal_env = self.terminal_env()
        result = self.call_source(immutable_runtime, 'status', env=terminal_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['binding_type'], 'legacy-per-pr')
        self.assertEqual(value['installed_source']['integrity'], 'intact')
        self.assertFalse(value['executing_cli']['matches_installed_pin'])
        self.assertEqual(value['activation']['source_intact'], True)
        self.assertFalse(value['activation']['executing_cli_matches_installed_pin'])
        self.assertEqual(value['report_status']['state'], 'not-evaluated')
        self.assertIn('not evaluated', value['report_status']['reason'])
        self.assertIn('Do not use rebind-owner', value['successor_guidance'])
        self.assertEqual(value['setup_guidance']['action'], 'upgrade')
        self.assertIsNone(value['setup_guidance']['command'])
        self.assertIn('not a clean committed Git source', value['setup_guidance']['reason'])
        self.assertNotIn('stale', value['report_status']['reason'])

    def test_status_recommends_upgrade_plan_from_clean_distinct_source(self):
        old_source = self.source_checkout('setup-guidance-old-pin')
        new_source = self.source_checkout('setup-guidance-clean-source')
        self.install_from_source(old_source)

        status = self.call_source(new_source, 'status')
        self.assertEqual(status.returncode, 0, status.stderr)
        value = json.loads(status.stdout)
        recommendation = value['setup_guidance']
        self.assertEqual(recommendation['action'], 'upgrade')
        self.assertIn('clean committed source', recommendation['reason'])
        self.assertIn(str(new_source / 'lane.py'), recommendation['command'])
        self.assertNotIn('PLAN_SHA256', recommendation['command'])

        command = shlex.split(recommendation['command'])
        planned = subprocess.run(command, cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(json.loads(planned.stdout)['binding_type'], 'legacy-per-pr')

    def test_status_recommends_migrate_plan_for_missing_legacy_source(self):
        old_source = self.source_checkout('setup-guidance-missing-legacy-source')
        clean_source = self.source_checkout('setup-guidance-migration-source')
        self.install_from_source(old_source)
        shutil.rmtree(old_source)
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-migration-reviewer')

        status = self.call_source(clean_source, 'status', env=owner_env)
        self.assertEqual(status.returncode, 0, status.stderr)
        value = json.loads(status.stdout)
        recommendation = value['setup_guidance']
        self.assertEqual(value['installed_source']['integrity'], 'missing')
        self.assertEqual(recommendation['action'], 'migrate')
        self.assertIn('without recreating them', recommendation['reason'])
        self.assertIn('--confirm-owner', recommendation['command'])

        command = shlex.split(recommendation['command'])
        planned = subprocess.run(command, cwd=self.repo, env=owner_env, capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(json.loads(planned.stdout)['legacy_state']['phase'], 'active')

    def test_status_bounds_malformed_native_owner_metadata(self):
        source = self.source_checkout_same_fingerprints('malformed-owner-guidance')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(source, lane=True, env=owner_env)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state = json.loads(state_path.read_text())
        state['lane_owner'] = {'harness': 'codex'}
        state_path.write_text(json.dumps(state, indent=2) + '\n')

        result = self.call('coverage', '--summary', env=owner_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Next setup: inspect - Recorded native-owner metadata is unrecognized.', result.stdout)
        self.assertNotIn('Run:', result.stdout)
        value = json.loads(self.call('status', env=owner_env).stdout)
        self.assertEqual(value['current_owner']['state'], 'unrecognized')
        self.assertEqual(value['setup_guidance']['action'], 'inspect')
        self.assertIsNone(value['setup_guidance']['command'])

    def test_status_recommends_readonly_inspection_for_missing_adapter(self):
        source = self.source_checkout('missing-adapter-guidance')
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        installed = self.install_from_source(source, lane=True, env=owner_env)
        adapter = Path(installed['adapter'])
        adapter_contents = adapter.read_bytes()
        adapter.unlink()

        status = self.call_source(source, 'status', env=owner_env)
        self.assertEqual(status.returncode, 0, status.stderr)
        value = json.loads(status.stdout)
        self.assertEqual(value['managed_adapter']['state'], 'missing')
        self.assertEqual(value['setup_guidance']['action'], 'inspect')
        self.assertIn('recover --inspect', value['setup_guidance']['command'])

        inspected = subprocess.run(shlex.split(value['setup_guidance']['command']), cwd=self.repo,
                                   env=owner_env, capture_output=True, text=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)['adapter']['state'], 'missing')

        adapter.write_bytes(adapter_contents)
        adapter.chmod(0o600)
        status = self.call_source(source, 'status', env=owner_env)
        self.assertEqual(status.returncode, 0, status.stderr)
        value = json.loads(status.stdout)
        self.assertEqual(value['managed_adapter']['state'], 'changed')
        inspected = subprocess.run(shlex.split(value['setup_guidance']['command']), cwd=self.repo,
                                   env=owner_env, capture_output=True, text=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)['adapter']['state'], 'changed')
        seat_status = self.call_source(source, '--seat-id', installed['seat_id'], 'status', env=owner_env)
        self.assertEqual(seat_status.returncode, 0, seat_status.stderr)
        self.assertEqual(json.loads(seat_status.stdout)['adapter_state'], 'changed')

    def test_native_owner_upgrade_plan_summarizes_receipt_preimage(self):
        old_source = self.source_checkout('native-owner-receipt-old-pin')
        new_source = self.source_checkout('native-owner-receipt-new-pin')
        owner_session = 'codex-fixture-thread'
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID=owner_session)
        self.install_from_source(old_source, lane=True, env=owner_env, lane_pr_url=self.url)

        discovered = self.call_source(old_source, 'discover', env=owner_env)
        self.assertEqual(discovered.returncode, 0, discovered.stderr)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state = json.loads(state_path.read_text())
        packet = json.loads(self.packet.read_text())
        packet['base_ref'] = state['base_ref']
        self.packet.write_text(json.dumps(packet, indent=2) + '\n')
        recorded = self.call_source(old_source, 'record', '--evidence', self.packet, '--format', 'json', env=owner_env)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertGreater(json.loads(recorded.stdout)['missing'], 0)

        preimage = json.loads(state_path.read_text())
        receipt = preimage['receipt']
        self.assertEqual(len(preimage.get('history', [])), 0)
        status = json.loads(self.call_source(old_source, 'status', '--session-id', owner_session, env=owner_env).stdout)
        self.assertEqual(status['report_status']['state'], 'valid-local-snapshot')
        self.assertEqual(status['setup_guidance']['action'], 'none')
        self.assertIn('No setup change is recommended', status['setup_guidance']['reason'])

        planned = self.call_source(new_source, 'upgrade-plan', env=owner_env)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        binding = next(row for row in plan['bindings'] if row['pr_url'] == self.url)
        self.assertTrue(binding['receipt_will_be_archived'])
        self.assertEqual(binding['history_count_before'], 0)

        applied = self.call_source(new_source, 'upgrade', '--plan-sha256', plan['plan_sha256'], env=owner_env)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(json.loads(applied.stdout)['fresh_report_required'])
        postimage = json.loads(state_path.read_text())
        self.assertNotIn('receipt', postimage)
        self.assertEqual(postimage['history'], [receipt])

    def test_status_reports_changed_and_missing_installed_pin_files(self):
        old_source = self.source_checkout('integrity-old-pin')
        self.install_from_source(old_source)
        pinned_file = old_source / 'lane_scope.py'
        pinned_file.write_text(pinned_file.read_text() + '\n# local independent source change\n')
        changed = json.loads(self.call('status').stdout)
        self.assertEqual(changed['installed_source']['integrity'], 'changed')
        self.assertEqual(changed['installed_source']['changed_files'], ['lane_scope.py'])
        self.assertEqual(changed['setup_guidance']['action'], 'inspect')
        self.assertIsNone(changed['setup_guidance']['command'])
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
        new_source = self.source_checkout('codex-recovery-new-pin')
        self.settings.unlink()
        self.settings.parent.rmdir()
        owner_env = dict(self.env, CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='codex-fixture-thread')
        self.install_from_source(old_source, lane=True, env=owner_env)
        proposed = json.loads(self.call_source(new_source, 'upgrade-plan', env=owner_env).stdout)
        with mock.patch.object(lane_runtime, 'APP', new_source), mock.patch.object(
                lane_runtime, 'SELF', new_source / 'lane.py'):
            self.interrupt_upgrade_after_settings(proposed['plan_sha256'], env=owner_env)
        journal = Path(lane_runtime.upgrade_journal_path(self.repo))
        owner_status = json.loads(self.call('status', env=owner_env).stdout)
        self.assertEqual(owner_status['setup_guidance']['action'], 'recover')
        self.assertIn('upgrade-recover --rollback', owner_status['setup_guidance']['command'])
        selected_other = json.loads(self.call('status', '--session-id', 'observation-only-id', env=owner_env).stdout)
        self.assertEqual(selected_other['current_native_identity']['actual']['session_id'], owner_env['CODEX_THREAD_ID'])
        self.assertEqual(selected_other['setup_guidance']['action'], 'recover')
        wrong_owner = dict(owner_env, CODEX_THREAD_ID='different-codex-thread')
        wrong_owner_status = json.loads(self.call('status', env=wrong_owner).stdout)
        self.assertEqual(wrong_owner_status['setup_guidance']['action'], 'inspect')
        self.assertIsNone(wrong_owner_status['setup_guidance']['command'])
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

    def test_missing_attachment_retirement_preserves_pr_history_and_allows_upgrade(self):
        owner, owner_env = self.native_owner_fixture()
        old_source = self.source_checkout('issue49-multi-old-pin', marker_file='lane_scope.py')
        new_source = self.source_checkout('issue49-multi-new-pin', marker_file='lane_scope.py')
        first = self.add_native_seat_attachment(old_source, owner, self.repo, owner_env)
        second_path = self.base / 'temporary missing attachment'
        recorded_branch = 'feature/temporary-attachment'
        second = self.add_native_seat_attachment(old_source, owner, second_path, owner_env, recorded_branch)
        seat_id = first['seat_id']
        self.assertEqual(second['seat_id'], seat_id)
        attachment = next(row for row in lane_runtime.session_store.all_checkouts(seat_id)
                          if row['root'] == str(second_path.resolve()))
        state_path = Path(attachment['checkout_dir']) / 'state.json'
        state = self.seed_attachment_pr_history(seat_id, attachment['checkout_id'], state_path)
        original_history = copy.deepcopy(state['pr_bindings'])
        seat_preimage = lane_runtime.session_store.load_seat(seat_id)
        original_pr_records = copy.deepcopy(seat_preimage['prs'])

        matching = self.call_seat_retirement(new_source, seat_id, second_path,
                                             env=owner_env, cwd=self.repo, ok=False)
        self.assertEqual(matching.returncode, 2)
        self.assertIn('run uninstall from that checkout', matching.stderr)
        self.assertEqual(json.loads(state_path.read_text())['phase'], 'active')

        switched = subprocess.run(['git', '-C', str(second_path), 'switch', '-c', 'feature/wrong-attachment-branch'],
                                  capture_output=True, text=True, env=owner_env)
        self.assertEqual(switched.returncode, 0, switched.stderr)
        wrong_branch = self.call_seat_retirement(new_source, seat_id, second_path,
                                                env=owner_env, cwd=self.repo, ok=False)
        self.assertEqual(wrong_branch.returncode, 2)
        self.assertIn('restore its recorded branch', wrong_branch.stderr)
        restored = subprocess.run(['git', '-C', str(second_path), 'switch', recorded_branch],
                                  capture_output=True, text=True, env=owner_env)
        self.assertEqual(restored.returncode, 0, restored.stderr)

        removed_worktree = subprocess.run(['git', '-C', str(self.repo), 'worktree', 'remove', '--force',
                                           str(second_path)], capture_output=True, text=True, env=owner_env)
        self.assertEqual(removed_worktree.returncode, 0, removed_worktree.stderr)
        removed_branch = subprocess.run(['git', '-C', str(self.repo), 'branch', '-D', recorded_branch],
                                        capture_output=True, text=True, env=owner_env)
        self.assertEqual(removed_branch.returncode, 0, removed_branch.stderr)

        planned = self.call_seat_retirement(new_source, seat_id, second_path,
                                            env=owner_env, cwd=self.repo)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(plan['checkout_evidence']['state'], 'missing')
        self.assertEqual(plan['attachment']['branch'], recorded_branch)
        self.assertEqual(len(plan['pr_records']), 2)
        self.assertEqual([len(row['binding']['history']) for row in plan['pr_records']], [1, 2])
        applied = self.call_seat_retirement(new_source, seat_id, second_path, '--apply',
                                            '--plan-sha256', plan['plan_sha256'],
                                            env=owner_env, cwd=self.repo)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        retired = json.loads(state_path.read_text())
        self.assertEqual(retired['phase'], 'removed')
        self.assertEqual(retired['pr_bindings'], original_history)
        self.assertEqual(retired['history'], state['history'])
        self.assertEqual(retired['retirement_receipt']['owner'], seat_preimage['owner'])
        self.assertEqual(retired['retirement_receipt']['checkout_evidence']['state'], 'missing')
        self.assertEqual(lane_runtime.session_store.load_seat(seat_id)['prs'], original_pr_records)

        upgrade_plan = self.call_source(new_source, '--cwd', self.repo, 'upgrade-plan', env=owner_env)
        self.assertEqual(upgrade_plan.returncode, 0, upgrade_plan.stderr)
        proposed_upgrade = json.loads(upgrade_plan.stdout)
        upgraded = self.call_source(new_source, '--cwd', self.repo, 'upgrade', '--plan-sha256',
                                    proposed_upgrade['plan_sha256'], env=owner_env)
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        self.assertEqual(state_path.read_bytes(), json.dumps(retired, indent=2).encode() + b'\n')

        status = self.call_source(new_source, '--seat-id', seat_id, 'status', env=owner_env)
        self.assertEqual(status.returncode, 0, status.stderr)
        status_value = json.loads(status.stdout)
        retired_status = next(row for row in status_value['checkouts']
                              if row['checkout_id'] == attachment['checkout_id'])
        self.assertEqual(retired_status['attachment_state'], 'retired')
        self.assertEqual(len(retired_status['pr_records']), 2)
        coverage = self.call_source(new_source, '--cwd', self.repo, 'coverage', '--summary', env=owner_env)
        self.assertEqual(coverage.returncode, 0, coverage.stderr)
        self.assertIn('Retired attachments:', coverage.stdout)
        self.assertIn(str(second_path), coverage.stdout)
        self.assertIn('2 PR records preserved', coverage.stdout)

        before_repeat = state_path.read_bytes()
        repeat = self.call_seat_retirement(new_source, seat_id, second_path,
                                           env=owner_env, cwd=self.repo)
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertTrue(json.loads(repeat.stdout)['already_retired'])
        repeat_apply = self.call_seat_retirement(new_source, seat_id, second_path, '--apply',
                                                 '--plan-sha256', plan['plan_sha256'],
                                                 env=owner_env, cwd=self.repo)
        self.assertEqual(repeat_apply.returncode, 0, repeat_apply.stderr)
        self.assertTrue(json.loads(repeat_apply.stdout)['already_retired'])
        self.assertEqual(state_path.read_bytes(), before_repeat)

    def test_rebind_owner_accepts_matching_retired_source_pin_control(self):
        fixture = self.prepare_seat_with_retired_attachment('issue60-matching-retired')
        incoming_env = dict(fixture['owner_env'], CLAUDE_CODE_SESSION_ID='claude-incoming-matching')

        handoff = self.call_seat_rebind(
            fixture['runtime'], fixture['seat_id'], 'claude',
            fixture['owner_env']['CLAUDE_CODE_SESSION_ID'], incoming_env, ok=False)

        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue(json.loads(handoff.stdout)['rebound'])
        self.assertEqual(lane_runtime.session_store.load_seat(fixture['seat_id'])['owner']['session_id'],
                         incoming_env['CLAUDE_CODE_SESSION_ID'])

    def test_rebind_owner_preserves_retired_historical_source_pins_after_upgrade(self):
        fixture = self.prepare_seat_with_retired_attachment('issue60-stale-retired')
        new_source = self.source_checkout('issue60-retired-upgrade-source', marker_file='lane_scope.py')
        upgrade_plan = self.call_source(new_source, '--cwd', self.repo, 'upgrade-plan',
                                        env=fixture['owner_env'])
        self.assertEqual(upgrade_plan.returncode, 0, upgrade_plan.stderr)
        proposed = json.loads(upgrade_plan.stdout)
        upgraded = self.call_source(new_source, '--cwd', self.repo, 'upgrade', '--plan-sha256',
                                    proposed['plan_sha256'], env=fixture['owner_env'])
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

        seat_before = lane_runtime.session_store.load_seat(fixture['seat_id'])
        rows = lane_runtime.session_store.all_checkouts(fixture['seat_id'])
        active = next(row['state'] for row in rows if row['state']['phase'] == 'active')
        retired = next(row['state'] for row in rows if row['state']['phase'] == 'removed')
        retired_state_path = fixture['retired_state_path']
        retired_state_bytes = retired_state_path.read_bytes()
        self.assertNotEqual(active['source_versions'], retired['source_versions'])
        self.assertEqual(active['source_versions'], {
            name: seat_before['runtime']['files'][name] for name in active['source_versions']})

        incoming_env = dict(fixture['owner_env'], CLAUDE_CODE_SESSION_ID='claude-incoming-historical')
        handoff = self.call_seat_rebind(
            Path(seat_before['runtime']['path']), fixture['seat_id'], 'claude',
            fixture['owner_env']['CLAUDE_CODE_SESSION_ID'], incoming_env, ok=False)

        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue(json.loads(handoff.stdout)['history_preserved'])
        self.assertEqual(retired_state_path.read_bytes(), retired_state_bytes)
        retired_after = json.loads(retired_state_path.read_bytes())
        self.assertEqual(retired_after['phase'], 'removed')
        self.assertEqual(retired_after['pr_bindings'], retired['pr_bindings'])
        self.assertEqual(retired_after['history'], retired['history'])
        seat_after = lane_runtime.session_store.load_seat(fixture['seat_id'])
        self.assertEqual(seat_after['runtime'], seat_before['runtime'])
        self.assertEqual(seat_after['owner']['session_id'], incoming_env['CLAUDE_CODE_SESSION_ID'])

    def test_rebind_owner_refuses_stale_active_source_pin_without_changing_state(self):
        owner, owner_env = self.native_owner_fixture()
        source = self.source_checkout('issue60-stale-active-pin', marker_file='lane.py')
        attached = self.add_native_seat_attachment(source, owner, self.repo, owner_env)
        seat_id = attached['seat_id']
        runtime = Path(attached['seat_runtime_path'])
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state = json.loads(state_path.read_text())
        state['source_versions']['lane.py'] = '0' * 64
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        state_before = state_path.read_bytes()
        seat_path = lane_runtime.session_store.seat_dir(seat_id) / 'seat.json'
        seat_before = seat_path.read_bytes()
        incoming_env = dict(owner_env, CLAUDE_CODE_SESSION_ID='claude-incoming-active-pin')

        handoff = self.call_seat_rebind(runtime, seat_id, 'claude',
                                        owner_env['CLAUDE_CODE_SESSION_ID'], incoming_env, ok=False)

        self.assertEqual(handoff.returncode, 2)
        self.assertIn('Pinned delivery adapter source changed', handoff.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(seat_path.read_bytes(), seat_before)

    def test_new_source_rebinds_verified_older_seat_without_repinning_or_replacing_adapter(self):
        owner, owner_env = self.native_owner_fixture()
        old_source = self.source_checkout('issue60-older-seat-runtime', marker_file='lane.py')
        new_source = self.source_checkout('issue60-new-maintenance-cli', marker_file='lane_scope.py')
        attached = self.add_native_seat_attachment(old_source, owner, self.repo, owner_env)
        seat_id = attached['seat_id']
        seat_before = lane_runtime.session_store.load_seat(seat_id)
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state_before = json.loads(state_path.read_text())
        runtime_path = Path(seat_before['runtime']['path'])
        runtime_before = lane_runtime.session_store.recorded_runtime_plan(
            runtime_path, seat_before['runtime']['files'])
        self.assertEqual(runtime_before, {'version': seat_before['runtime']['version'],
                                          'path': seat_before['runtime']['path'],
                                          'files': seat_before['runtime']['files']})
        adapter_path = lane_runtime.session_store.seat_dir(seat_id) / 'bin' / 'gh'
        adapter_before = adapter_path.read_bytes()
        settings_path = owner / '.claude/settings.local.json'
        settings_before = settings_path.read_bytes()
        incoming_env = dict(owner_env, CLAUDE_CODE_SESSION_ID='claude-incoming-new-cli')

        handoff = self.call_seat_rebind(
            new_source, seat_id, 'claude', owner_env['CLAUDE_CODE_SESSION_ID'], incoming_env, ok=False)

        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue(json.loads(handoff.stdout)['rebound'])
        seat_after = lane_runtime.session_store.load_seat(seat_id)
        state_after = json.loads(state_path.read_text())
        self.assertEqual(seat_after['runtime'], seat_before['runtime'])
        runtime_after = lane_runtime.session_store.recorded_runtime_plan(
            runtime_path, seat_after['runtime']['files'])
        self.assertEqual(runtime_after, runtime_before)
        self.assertEqual(state_after['source_versions'], state_before['source_versions'])
        self.assertEqual(state_after['source'], state_before['source'])
        self.assertEqual(state_after['source_revision'], state_before['source_revision'])
        self.assertEqual(state_after['adapter'], state_before['adapter'])
        self.assertEqual(adapter_path.read_bytes(), adapter_before)
        self.assertEqual(settings_path.read_bytes(), settings_before)
        self.assertTrue(runtime_path.is_dir())

        state_after['source_versions']['lane.py'] = 'f' * 64
        state_path.write_text(json.dumps(state_after, indent=2) + '\n')
        corrupt_state = state_path.read_bytes()
        seat_after_bytes = (lane_runtime.session_store.seat_dir(seat_id) / 'seat.json').read_bytes()
        next_owner_env = dict(incoming_env, CLAUDE_CODE_SESSION_ID='claude-incoming-third')
        stale_active = self.call_seat_rebind(
            new_source, seat_id, 'claude', incoming_env['CLAUDE_CODE_SESSION_ID'], next_owner_env, ok=False)
        self.assertEqual(stale_active.returncode, 2)
        self.assertIn('Pinned delivery adapter source changed', stale_active.stderr)
        self.assertEqual(state_path.read_bytes(), corrupt_state)
        self.assertEqual((lane_runtime.session_store.seat_dir(seat_id) / 'seat.json').read_bytes(),
                         seat_after_bytes)
        self.assertEqual(adapter_path.read_bytes(), adapter_before)
        self.assertEqual(settings_path.read_bytes(), settings_before)

    def test_new_source_rebind_refuses_changed_recorded_runtime_without_writes(self):
        owner, owner_env = self.native_owner_fixture()
        old_source = self.source_checkout('issue60-changed-runtime-old', marker_file='lane.py')
        new_source = self.source_checkout('issue60-changed-runtime-new', marker_file='lane_scope.py')
        attached = self.add_native_seat_attachment(old_source, owner, self.repo, owner_env)
        seat_id = attached['seat_id']
        seat_path = lane_runtime.session_store.seat_dir(seat_id) / 'seat.json'
        seat_before = seat_path.read_bytes()
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state_before = state_path.read_bytes()
        runtime_path = Path(lane_runtime.session_store.load_seat(seat_id)['runtime']['path'])
        runtime_file = runtime_path / 'lane_scope.py'
        runtime_file.chmod(0o644)
        runtime_file.write_bytes(runtime_file.read_bytes() + b'\n# synthetic changed runtime\n')
        runtime_file.chmod(0o444)
        runtime_file_after = runtime_file.read_bytes()
        incoming_env = dict(owner_env, CLAUDE_CODE_SESSION_ID='claude-incoming-changed-runtime')

        handoff = self.call_seat_rebind(
            new_source, seat_id, 'claude', owner_env['CLAUDE_CODE_SESSION_ID'], incoming_env, ok=False)

        self.assertEqual(handoff.returncode, 2)
        self.assertIn('Permanent seat runtime is changed or unavailable', handoff.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(seat_path.read_bytes(), seat_before)
        self.assertEqual(runtime_file.read_bytes(), runtime_file_after)

    def test_new_source_rebind_refuses_mismatched_recorded_runtime_version_without_writes(self):
        owner, owner_env = self.native_owner_fixture()
        old_source = self.source_checkout('issue60-runtime-version-old', marker_file='lane.py')
        new_source = self.source_checkout('issue60-runtime-version-new', marker_file='lane_scope.py')
        attached = self.add_native_seat_attachment(old_source, owner, self.repo, owner_env)
        seat_id = attached['seat_id']
        seat_path = lane_runtime.session_store.seat_dir(seat_id) / 'seat.json'
        seat = json.loads(seat_path.read_text())
        seat['runtime']['version'] = 'f' * 64
        seat_path.write_text(json.dumps(seat, indent=2) + '\n')
        seat_before = seat_path.read_bytes()
        state_path = Path(lane_runtime.directory(self.repo)) / 'state.json'
        state_before = state_path.read_bytes()
        incoming_env = dict(owner_env, CLAUDE_CODE_SESSION_ID='claude-incoming-runtime-version')

        handoff = self.call_seat_rebind(
            new_source, seat_id, 'claude', owner_env['CLAUDE_CODE_SESSION_ID'], incoming_env, ok=False)

        self.assertEqual(handoff.returncode, 2)
        self.assertIn('Permanent seat runtime path is not its recorded immutable runtime', handoff.stderr)
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(seat_path.read_bytes(), seat_before)

    def test_missing_only_attachment_works_by_seat_id_and_refuses_wrong_owner_and_stale_plan(self):
        owner, owner_env = self.native_owner_fixture()
        source = self.source_checkout('issue49-only-attachment', marker_file='lane_scope.py')
        checkout = self.base / 'only disposable product worktree'
        attached = self.add_native_seat_attachment(source, owner, checkout, owner_env, 'feature/only-attachment')
        seat_id = attached['seat_id']
        attachment = lane_runtime.session_store.all_checkouts(seat_id)[0]
        state_path = Path(attachment['checkout_dir']) / 'state.json'
        original_state_bytes = state_path.read_bytes()
        removed = subprocess.run(['git', '-C', str(self.repo), 'worktree', 'remove', '--force', str(checkout)],
                                  capture_output=True, text=True, env=owner_env)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        branch_removed = subprocess.run(['git', '-C', str(self.repo), 'branch', '-D', 'feature/only-attachment'],
                                        capture_output=True, text=True, env=owner_env)
        self.assertEqual(branch_removed.returncode, 0, branch_removed.stderr)

        wrong_owner = dict(owner_env, CLAUDE_CODE_SESSION_ID='claude-other-session')
        denied = self.call_seat_retirement(source, seat_id, checkout, env=wrong_owner, cwd=self.base, ok=False)
        self.assertEqual(denied.returncode, 2)
        self.assertIn('belongs to another native session', denied.stderr)
        self.assertEqual(state_path.read_bytes(), original_state_bytes)

        seat_before = lane_runtime.session_store.load_seat(seat_id)
        seat_path = lane_runtime.session_store.seat_dir(seat_id) / 'seat.json'
        for field in ('install_journal', 'migration_journal'):
            pending = copy.deepcopy(seat_before)
            pending[field] = {'fixture': 'pending'}
            lane_runtime.session_store.save_seat(pending)
            refused = self.call_seat_retirement(source, seat_id, checkout, env=owner_env, cwd=self.base, ok=False)
            self.assertEqual(refused.returncode, 2)
            self.assertIn('recover --inspect', refused.stderr)
            self.assertEqual(state_path.read_bytes(), original_state_bytes)
            lane_runtime.session_store.save_seat(copy.deepcopy(seat_before))

        upgrade_journal = lane_runtime.session_store.seat_dir(seat_id) / lane_runtime.UPGRADE_JOURNAL
        upgrade_journal.write_text('{}\n')
        upgrade_refused = self.call_seat_retirement(source, seat_id, checkout, env=owner_env, cwd=self.base, ok=False)
        self.assertEqual(upgrade_refused.returncode, 2)
        self.assertIn('recover --inspect', upgrade_refused.stderr)
        self.assertIn('upgrade-recover --rollback', upgrade_refused.stderr)
        self.assertIn('from the checkout that started the upgrade', upgrade_refused.stderr)
        self.assertIn('preserve the journal and restore the original checkout', upgrade_refused.stderr)
        self.assertEqual(state_path.read_bytes(), original_state_bytes)
        upgrade_journal.unlink()

        planned = self.call_seat_retirement(source, seat_id, checkout, env=owner_env, cwd=self.base)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        stale_state = json.loads(state_path.read_text())
        stale_state['fixture_preimage_drift'] = True
        state_path.write_text(json.dumps(stale_state, indent=2) + '\n')
        drift_bytes = state_path.read_bytes()
        stale = self.call_seat_retirement(source, seat_id, checkout, '--apply', '--plan-sha256',
                                          plan['plan_sha256'], env=owner_env, cwd=self.base, ok=False)
        self.assertEqual(stale.returncode, 2)
        self.assertIn('preimage changed', stale.stderr)
        self.assertEqual(state_path.read_bytes(), drift_bytes)

        fresh = json.loads(self.call_seat_retirement(source, seat_id, checkout,
                                                      env=owner_env, cwd=self.base).stdout)
        applied = self.call_seat_retirement(source, seat_id, checkout, '--apply', '--plan-sha256',
                                            fresh['plan_sha256'], env=owner_env, cwd=self.base)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        retired = json.loads(state_path.read_text())
        self.assertEqual(retired['phase'], 'removed')
        self.assertTrue(retired['retirement_receipt']['checkout_evidence']['reason'])
        status = self.call_source(source, '--seat-id', seat_id, 'status', env=owner_env)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(len(json.loads(status.stdout)['retired_attachments']), 1)

    def test_missing_attachment_retirement_leaves_an_unrelated_replacement_untouched(self):
        owner, owner_env = self.native_owner_fixture()
        source = self.source_checkout('issue49-unrelated-replacement', marker_file='lane_scope.py')
        checkout = self.base / 'replaced recorded checkout'
        attached = self.add_native_seat_attachment(source, owner, checkout, owner_env, 'feature/replaced-attachment')
        seat_id = attached['seat_id']
        attachment = lane_runtime.session_store.all_checkouts(seat_id)[0]
        state_path = Path(attachment['checkout_dir']) / 'state.json'
        removed = subprocess.run(['git', '-C', str(self.repo), 'worktree', 'remove', '--force', str(checkout)],
                                  capture_output=True, text=True, env=owner_env)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        branch_removed = subprocess.run(['git', '-C', str(self.repo), 'branch', '-D', 'feature/replaced-attachment'],
                                        capture_output=True, text=True, env=owner_env)
        self.assertEqual(branch_removed.returncode, 0, branch_removed.stderr)
        replacement_gitdirs = self.init_fixture_repo(checkout, 'replacement/main',
                                                     'git@fixture.invalid:elsewhere/replacement.git')
        replacement_file = checkout / 'fixture.txt'
        replacement_bytes = replacement_file.read_bytes()
        replacement_status = subprocess.run(['git', '-C', str(checkout), 'status', '--porcelain'],
                                             capture_output=True, text=True, env=owner_env, check=True).stdout

        planned = self.call_seat_retirement(source, seat_id, checkout, env=owner_env, cwd=self.base)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(plan['checkout_evidence']['state'], 'not-recorded-worktree')
        applied = self.call_seat_retirement(source, seat_id, checkout, '--apply', '--plan-sha256',
                                            plan['plan_sha256'], env=owner_env, cwd=self.base)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(replacement_file.read_bytes(), replacement_bytes)
        self.assertEqual(subprocess.run(['git', '-C', str(checkout), 'status', '--porcelain'],
                                       capture_output=True, text=True, env=owner_env, check=True).stdout,
                         replacement_status)
        self.assertEqual(lane_runtime.delivery.value(checkout, 'rev-parse', '--absolute-git-dir'),
                         replacement_gitdirs[0])
        self.assertEqual(json.loads(state_path.read_text())['phase'], 'removed')

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
