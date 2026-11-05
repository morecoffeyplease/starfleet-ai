"""OpenCode-native owner binding and delivery bridge fixtures."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_lane_install as fixtures

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import lane_scope


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.LaneInstallTest('test_plan_is_readonly_and_install_preserves_existing_settings')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.env.update(CMUX_AGENT_LAUNCH_KIND='codex', CODEX_THREAD_ID='inherited-codex-session',
                          OPENCODE_SESSION_ID='ses_environment_only')
        self.source = self.f.source_checkout_same_fingerprints('opencode-native-source')
        self.seat_root = self.f.base / 'opencode owner seat'
        self.f.git('worktree', 'add', '-b', 'opencode-owner-seat', self.seat_root)
        self.counter = 0

    def event(self, session_id, *, parent_id=None, directory=None, tool='prime_directive_manage'):
        self.counter += 1
        return {
            'native_source': lane_scope.OPENCODE_EVENT_SOURCE,
            'tool': tool,
            'tool_context': {
                'sessionID': session_id,
                'agent': 'build',
                'messageID': f'msg_native_{self.counter}',
                'id': f'call_native_{self.counter}',
            },
            'session': {
                'id': session_id,
                'parentID': parent_id,
                'projectID': 'project_fixture',
                'location': {'directory': str(directory or self.f.repo)},
            },
        }

    def bridge(self, event, argv, *, cwd=None, ok=True):
        payload = json.dumps({'native_event': event, 'argv': list(map(str, argv))})
        result = subprocess.run([sys.executable, str(self.source / 'lane.py'), '--cwd',
                                 str(cwd or self.f.repo), 'opencode'], capture_output=True,
                                text=True, input=payload, env=self.f.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def checkout_state_path(self, checkout):
        return Path(fixtures.lane_runtime.directory(checkout)) / 'state.json'

    def plan_and_install(self, event, checkout, seat_root):
        prefix = ['--cwd', checkout, '--session-root', seat_root]
        scope = ['--lane', '--host', 'fixture.invalid', '--owner-harness', 'opencode',
                 '--owner-session', event['tool_context']['sessionID']]
        proposed = json.loads(self.bridge(event, [*prefix, 'plan', *scope]).stdout)
        installed = json.loads(self.bridge(event, [*prefix, 'install', *scope,
            '--plan-sha256', proposed['plan_sha256']]).stdout)
        return proposed, installed

    def test_environment_only_identity_and_untrusted_context_shapes_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'actual native Claude or Codex session'):
            lane_scope.native_identity({'OPENCODE_SESSION_ID': 'ses_environment_only'})

        event = self.event('ses_NativeParent123')
        self.assertEqual(lane_scope.native_identity(self.f.env, event), {
            'harness': 'opencode', 'session_id': 'ses_NativeParent123'})
        self.assertEqual(lane_scope.native_session_context(event), {
            'session_parent_id': None,
            'session_project_id': 'project_fixture',
            'session_directory': str(self.f.repo),
        })
        owner_state = {'lane_owner': {
            'harness': 'opencode', 'session_id': event['tool_context']['sessionID'],
            'session_parent_id': None, 'session_project_id': 'project_fixture',
            'session_directory': str(self.f.repo),
        }}
        with lane_scope.native_event_scope(event):
            lane_scope.check_owner(owner_state)
        changed_context = json.loads(json.dumps(event))
        changed_context['session']['projectID'] = 'another_project'
        with lane_scope.native_event_scope(changed_context):
            with self.assertRaisesRegex(ValueError, 'different OpenCode session API'):
                lane_scope.check_owner(owner_state)

        invalid = json.loads(json.dumps(event))
        invalid['session']['id'] = 'ses_another_session'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            lane_scope.native_identity(event=invalid)

        forged_event = json.loads(json.dumps(event))
        forged_event.pop('native_source')
        forged = self.bridge(forged_event, ['--cwd', self.f.repo, 'plan'], ok=False)
        self.assertEqual(forged.returncode, 2)
        self.assertIn('actual native OpenCode server tool context', forged.stderr)

    def test_parent_child_seats_are_distinct_and_explicit_handoff_preserves_history(self):
        parent_id = 'ses_NativeParent123'
        parent_event = self.event(parent_id)
        before_settings = self.f.settings.read_bytes()
        parent_plan, parent_install = self.plan_and_install(parent_event, self.f.repo, self.seat_root)
        self.assertFalse(parent_plan['manages_claude_hooks'])
        self.assertEqual(parent_install['opencode_plugin_path'], parent_plan['opencode_plugin_path'])
        self.assertTrue(Path(parent_install['opencode_plugin_path']).is_dir())
        self.assertEqual(self.f.settings.read_bytes(), before_settings)

        parent_manifest = json.loads((Path(parent_plan['seat_dir']) / 'seat.json').read_text())
        self.assertEqual(parent_manifest['owner'], {'harness': 'opencode', 'session_id': parent_id})
        self.assertEqual(parent_manifest['owner_context'], {
            'session_parent_id': None,
            'session_project_id': 'project_fixture',
            'session_directory': str(self.f.repo),
        })
        runtime_files = parent_manifest['runtime']['files']
        for relative in ('opencode-plugin/index.ts', 'opencode-plugin/policy.js'):
            self.assertIn(relative, runtime_files)
            self.assertTrue((Path(parent_manifest['runtime']['path']) / relative).is_file())
        parent_state = json.loads(self.checkout_state_path(self.f.repo).read_text())
        self.assertEqual(parent_state['source_versions'], fixtures.lane_runtime.source_versions('opencode'))
        self.assertEqual(fixtures.lane_runtime.source_pin_status(parent_state)['integrity'], 'intact')

        missing_report = self.event(parent_id, tool='prime_directive_gh')
        denied = self.bridge(missing_report, ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready', self.f.url], ok=False)
        self.assertEqual(denied.returncode, 2)
        self.assertIn('No recorded delivery report run', denied.stderr)
        self.assertFalse(self.f.forwarded.exists())

        packet = json.loads(self.f.packet.read_text())
        packet['base_ref'] = 'refs/remotes/origin/main'
        self.f.packet.write_text(json.dumps(packet))
        record_event = self.event(parent_id)
        report = self.bridge(record_event, ['--cwd', self.f.repo, 'record', '--evidence', self.f.packet,
                                             '--format', 'json'])
        self.assertEqual(report.returncode, 0, report.stderr)
        prior_receipt = json.loads(self.checkout_state_path(self.f.repo).read_text())['receipt']

        unattached_target = self.event(parent_id, tool='prime_directive_gh')
        unattached = self.bridge(unattached_target, ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready',
            'https://fixture.invalid/other/repository/pull/902'], ok=False)
        self.assertEqual(unattached.returncode, 2)
        self.assertIn('requires an attached checkout', unattached.stderr)
        self.assertFalse(self.f.forwarded.exists())

        approved = self.event(parent_id, tool='prime_directive_gh')
        forwarded = self.bridge(approved, ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready', self.f.url])
        self.assertEqual(forwarded.returncode, 0, forwarded.stderr)
        self.assertEqual(len(self.f.forwarded.read_text().splitlines()), 1)

        child_checkout = self.f.base / 'child product'
        child_seat = self.f.base / 'child owner seat'
        self.f.git('worktree', 'add', '-b', 'opencode-child-product', child_checkout)
        self.f.git('worktree', 'add', '-b', 'opencode-child-seat', child_seat)
        child_id = 'ses_NativeChild456'
        child_event = self.event(child_id, parent_id=parent_id, directory=child_checkout)
        child_plan, _ = self.plan_and_install(child_event, child_checkout, child_seat)
        self.assertNotEqual(child_plan['seat_id'], parent_plan['seat_id'])
        child_manifest = json.loads((Path(child_plan['seat_dir']) / 'seat.json').read_text())
        self.assertEqual(child_manifest['owner_context']['session_parent_id'], parent_id)
        self.assertEqual(child_manifest['owner_context']['session_directory'], str(child_checkout))

        cross_seat = self.event(child_id, parent_id=parent_id, directory=child_checkout,
                                tool='prime_directive_gh')
        cross_denial = self.bridge(cross_seat, ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready', self.f.url], ok=False)
        self.assertEqual(cross_denial.returncode, 2)
        self.assertIn('belongs to another native session', cross_denial.stderr)
        self.assertEqual(len(self.f.forwarded.read_text().splitlines()), 1)

        handoff_id = 'ses_NativeHandoff789'
        handoff = self.event(handoff_id, parent_id=parent_id)
        transferred = self.bridge(handoff, ['--cwd', self.f.repo, 'rebind-owner',
            '--from-harness', 'opencode', '--from-session', parent_id, '--confirm-handoff'])
        self.assertEqual(transferred.returncode, 0, transferred.stderr)
        self.assertTrue(json.loads(transferred.stdout)['report_required'])
        state_path = self.checkout_state_path(self.f.repo)
        state = json.loads(state_path.read_text())
        self.assertEqual(state['lane_owner']['session_id'], handoff_id)
        self.assertEqual(state['lane_owner']['session_parent_id'], parent_id)
        self.assertNotIn('receipt', state)
        self.assertIn(prior_receipt, state['pr_bindings'][self.f.url]['history'])

        old_owner = self.event(parent_id, tool='prime_directive_gh')
        old_denial = self.bridge(old_owner, ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready', self.f.url], ok=False)
        self.assertEqual(old_denial.returncode, 2)
        self.assertIn('belongs to another native session', old_denial.stderr)
        new_owner = self.event(handoff_id, parent_id=parent_id, tool='prime_directive_gh')
        new_denial = self.bridge(new_owner, ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready', self.f.url], ok=False)
        self.assertEqual(new_denial.returncode, 2)
        self.assertIn('No recorded delivery report run', new_denial.stderr)
        self.assertEqual(len(self.f.forwarded.read_text().splitlines()), 1)

        child_report = self.bridge(self.event(handoff_id, parent_id=parent_id),
            ['--cwd', self.f.repo, 'record', '--evidence', self.f.packet, '--format', 'json'])
        self.assertEqual(child_report.returncode, 0, child_report.stderr)
        final_call = self.bridge(self.event(handoff_id, parent_id=parent_id, tool='prime_directive_gh'),
            ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready', self.f.url])
        self.assertEqual(final_call.returncode, 0, final_call.stderr)
        self.assertEqual(len(self.f.forwarded.read_text().splitlines()), 2)

        self.f.git('commit', '--allow-empty', '-m', 'make the child report stale')
        stale = self.bridge(self.event(handoff_id, parent_id=parent_id, tool='prime_directive_gh'),
            ['--cwd', self.f.repo, 'gh', '--', 'pr', 'ready', self.f.url], ok=False)
        self.assertEqual(stale.returncode, 2)
        self.assertIn('Candidate or local target changed', stale.stderr)
        self.assertEqual(len(self.f.forwarded.read_text().splitlines()), 2)

    def test_opencode_to_native_handoff_refreshes_pin_and_requires_fresh_report(self):
        session_id = 'ses_CrossHarnessOpenCode'
        event = self.event(session_id)
        self.plan_and_install(event, self.f.repo, self.seat_root)
        state_path = self.checkout_state_path(self.f.repo)
        packet = json.loads(self.f.packet.read_text())
        packet['base_ref'] = 'refs/remotes/origin/main'
        self.f.packet.write_text(json.dumps(packet))
        prior_report = self.bridge(event, ['--cwd', self.f.repo, 'record', '--evidence', self.f.packet,
                                            '--format', 'json'])
        self.assertEqual(prior_report.returncode, 0, prior_report.stderr)
        prior_state = json.loads(state_path.read_text())
        prior_receipt = prior_state['receipt']
        self.assertEqual(len(prior_state['source_versions']), 9)

        handoff = self.f.call_source(self.source, 'rebind-owner', '--from-harness', 'opencode',
            '--from-session', session_id, '--confirm-handoff')
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertTrue(json.loads(handoff.stdout)['report_required'])
        current = json.loads(state_path.read_text())
        self.assertEqual(current['lane_owner']['harness'], 'codex')
        self.assertEqual(current['lane_owner']['session_id'], 'inherited-codex-session')
        self.assertEqual(current['source_versions'], fixtures.lane_runtime.source_versions('codex'))
        self.assertNotIn('receipt', current)
        self.assertIn(prior_receipt, current['pr_bindings'][self.f.url]['history'])

        fresh_report = self.f.call_source(self.source, 'record', '--evidence', self.f.packet,
                                          '--format', 'json')
        self.assertEqual(fresh_report.returncode, 0, fresh_report.stderr)

    def test_native_to_opencode_handoff_pins_runtime_sources_and_records(self):
        owner_env = dict(self.f.env, CMUX_AGENT_LAUNCH_KIND='codex',
                         CODEX_THREAD_ID='codex-fixture-thread')
        self.f.install_from_source(self.source, owner=self.seat_root, lane=True, env=owner_env)
        state_path = self.checkout_state_path(self.f.repo)
        before = json.loads(state_path.read_text())
        self.assertEqual(len(before['source_versions']), 7)

        session_id = 'ses_CrossHarnessIncomingOpenCode'
        event = self.event(session_id)
        handoff = self.bridge(event, ['--cwd', self.f.repo, 'rebind-owner', '--from-harness', 'codex',
            '--from-session', 'codex-fixture-thread', '--confirm-handoff'])
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        current = json.loads(state_path.read_text())
        self.assertEqual(current['lane_owner']['harness'], 'opencode')
        self.assertEqual(current['lane_owner']['session_id'], session_id)
        self.assertEqual(current['source_versions'], fixtures.lane_runtime.source_versions('opencode'))

        packet = json.loads(self.f.packet.read_text())
        packet['base_ref'] = 'refs/remotes/origin/main'
        self.f.packet.write_text(json.dumps(packet))
        fresh_report = self.bridge(event, ['--cwd', self.f.repo, 'record', '--evidence', self.f.packet,
                                           '--format', 'json'])
        self.assertEqual(fresh_report.returncode, 0, fresh_report.stderr)

    def test_native_to_opencode_handoff_refuses_missing_or_changed_runtime_source(self):
        owner_env = dict(self.f.env, CMUX_AGENT_LAUNCH_KIND='codex',
                         CODEX_THREAD_ID='codex-fixture-thread')
        self.f.install_from_source(self.source, owner=self.seat_root, lane=True, env=owner_env)
        state_path = self.checkout_state_path(self.f.repo)
        before_state = state_path.read_bytes()
        state = json.loads(before_state)
        seat_path = Path(state['seat_dir']) / 'seat.json'
        before_seat = seat_path.read_bytes()
        plugin_file = self.source / 'opencode-plugin/policy.js'
        original = plugin_file.read_bytes()
        session_id = 'ses_CrossHarnessTamperOpenCode'

        try:
            for mode in ('missing', 'changed'):
                if mode == 'missing':
                    plugin_file.unlink()
                else:
                    plugin_file.write_bytes(original + b'\n// synthetic tamper\n')
                event = self.event(session_id)
                handoff = self.bridge(event, ['--cwd', self.f.repo, 'rebind-owner', '--from-harness', 'codex',
                    '--from-session', 'codex-fixture-thread', '--confirm-handoff'], ok=False)
                self.assertEqual(handoff.returncode, 2)
                expected_error = ('Incoming delivery adapter source is missing' if mode == 'missing' else
                                  'Permanent seat runtime is changed')
                self.assertIn(expected_error, handoff.stderr)
                self.assertEqual(state_path.read_bytes(), before_state)
                self.assertEqual(seat_path.read_bytes(), before_seat)
                self.assertNotIn('handoff_journal', json.loads(seat_path.read_text()))
                plugin_file.write_bytes(original)
        finally:
            plugin_file.write_bytes(original)

if __name__ == '__main__':
    unittest.main()
