"""Required report execution at the supported CLI and hook boundaries."""

import hashlib
import json
import os
from pathlib import Path
import sys

from test_guard import GuardFixture, run

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import delivery_runs


class DeliveryRunsTest(GuardFixture):
    def setUp(self):
        super().setUp()
        self.git('branch', 'main')
        self.packet = self.base / 'delivery.json'
        self.policy = self.base / 'policy.json'
        self.policy.write_bytes((APP / 'config/delivery-checklist.json').read_bytes())
        self.data = {'pr_url': self.url, 'base_ref': 'main',
                     'design-fidelity': {'applicable': False, 'reason': 'No design work in this fixture',
                                        'revision': self.git('rev-parse', 'HEAD').stdout.strip()}}
        self.packet.write_text(json.dumps(self.data))
        self.calls = self.base / 'gh-calls.jsonl'
        self.fake = self.base / 'gh'
        self.fake.write_text('#!' + sys.executable + '\nimport json,sys\n'
                             'with open(' + repr(str(self.calls)) + ', "a") as f:\n'
                             ' f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        self.fake.chmod(0o755)

    def record(self, ok=True):
        self.packet.write_text(json.dumps(self.data))
        return self.call('delivery', 'record', '--base-ref', 'main', '--pr-url', self.url,
                         '--evidence', self.packet, '--policy', self.policy, '--format', 'json', ok=ok)

    def child(self, *argv, ok=True):
        return self.call('gh-child', '--real-binary', self.fake, '--', *argv, ok=ok)

    def test_missing_report_stops_ready_merge_and_auto_merge_before_handoff(self):
        for argv in (('pr', 'ready'), ('pr', 'merge', '--auto')):
            result = self.child(*argv, ok=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn('delivery record', result.stderr)
        self.assertFalse(self.calls.exists())
        for action in ('ready', 'merge'):
            self.assertEqual(self.call('check', action, ok=False).returncode, 2)

    def test_record_displays_and_archives_gaps_without_requiring_green_or_ack(self):
        report = json.loads(self.record().stdout)
        self.assertEqual(report['missing'], 2)
        receipt = self.status()['delivery_run']
        self.assertEqual(receipt['report'], report)
        self.assertEqual(receipt['report']['snapshot']['head'], self.git('rev-parse', 'HEAD').stdout.strip())
        self.child('pr', 'ready', '901')
        self.child('pr', 'merge', self.url, '--auto')
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertNotIn('acknowledgement', receipt)

    def test_partial_audit_with_unapproved_deviation_still_counts_as_report_run(self):
        audit = self.base / 'audit.md'
        audit.write_text('Coverage: PARTIAL\nFidelity: DRIFT\n')
        self.data['design-fidelity'].update(applicable=True, skill='design-fidelity-audit',
                                           audit={'path': str(audit), 'sha256': hashlib.sha256(audit.read_bytes()).hexdigest()},
                                           coverage='PARTIAL', fidelity='DRIFT', unresolved=['Footer differs'],
                                           deviations=[{'id': 'D1', 'description': 'Footer differs'}])
        report = json.loads(self.record().stdout)
        design = next(item for item in report['items'] if item['id'] == 'design-fidelity')
        self.assertEqual(design['status'], 'missing')
        self.assertTrue(any(gap['field'] == 'deviations.0.approval' for gap in design['gaps']))
        self.child('pr', 'ready')

    def test_failed_report_and_readonly_report_do_not_create_receipt(self):
        self.call('delivery', 'report', '--base-ref', 'main', '--pr-url', self.url, '--evidence', self.packet)
        self.assertNotIn('delivery_run', self.status())
        self.data['design-fidelity'] = {'applicable': True}
        self.assertEqual(self.record(ok=False).returncode, 2)
        self.assertNotIn('delivery_run', self.status())
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)

    def test_new_commit_or_dirty_candidate_requires_another_report_but_not_tests(self):
        self.record()
        self.git('commit', '--allow-empty', '-m', 'metadata-only')
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)
        self.record()
        self.child('pr', 'ready')
        (self.repo / 'untracked.txt').write_text('new input')
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)
        self.assertEqual(self.record(ok=False).returncode, 2)

    def test_changed_target_policy_packet_and_artifact_require_new_run(self):
        asset = self.base / 'main-review.md'
        asset.write_text('Existing target assessment')
        self.data['main-awareness'] = {'revision': self.data['design-fidelity']['revision'],
                                       'evidence': {'path': str(asset), 'sha256': hashlib.sha256(asset.read_bytes()).hexdigest()}}
        self.record()
        asset.write_text('Changed assessment')
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)
        self.record()  # A mismatched declared artifact hash stays a visible missing item.
        self.child('pr', 'ready')
        self.policy.write_text(self.policy.read_text() + '\n')
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)
        self.record()
        self.packet.write_text(self.packet.read_text() + '\n')
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)
        self.record()
        self.git('commit', '--allow-empty', '-m', 'new target')
        self.git('branch', '-f', 'main', 'HEAD')
        self.git('reset', '--soft', 'HEAD~1')
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)

    def test_wrong_pr_or_repo_cannot_reuse_current_receipt(self):
        self.record()
        for argv in (('pr', 'ready', '902'), ('pr', 'merge', '--repo', 'other/repo'),
                     ('pr', 'ready', '--repo=other/repo')):
            self.assertEqual(self.child(*argv, ok=False).returncode, 2)
        self.child('pr', 'ready', '--repo', 'starfleet/guard', '901')
        self.data['pr_url'] = self.url.replace('/901', '/902')
        self.assertEqual(self.record(ok=False).returncode, 2)

    def test_report_does_not_clear_fix_round_stop(self):
        self.due_three()
        self.data['design-fidelity']['revision'] = self.git('rev-parse', 'HEAD').stdout.strip()
        self.record()
        self.assertEqual(self.child('pr', 'ready', ok=False).returncode, 2)
        self.assertEqual(self.call('check', 'edit', ok=False).returncode, 2)

    def test_read_and_deescalation_commands_are_unchanged(self):
        for argv in (('pr', 'view', '901'), ('api', 'user'), ('pr', 'ready', '--undo'),
                     ('pr', 'merge', '--disable-auto')):
            self.child(*argv)
        self.assertEqual(len(self.calls.read_text().splitlines()), 4)

    def test_hook_covers_direct_cli_and_fixture_shim_covers_opaque_script(self):
        event = {'command': 'gh pr ready 901'}
        self.assertEqual(self.event('Bash', event).returncode, 2)
        self.record()
        self.assertEqual(self.event('Bash', event).returncode, 0)
        env = dict(os.environ, PATH=str(self.base) + os.pathsep + os.environ['PATH'])
        self.call('install', '--fixture', env=env)
        adapter = self.repo / '.git/starfleet-policy-guard/bin'
        env['PATH'] = str(adapter) + os.pathsep + env['PATH']
        launcher = self.base / 'ready.sh'
        launcher.write_text('gh pr ready 901\n')
        self.call('begin', '--batch', 'metadata', '--kind', 'investigation', '--finding', 'Fixture metadata commit')
        self.git('commit', '--allow-empty', '-m', 'new candidate')
        result = run('bash', launcher, cwd=self.repo, env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.calls.exists())
        self.record()
        result = run('bash', launcher, cwd=self.repo, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_unenrolled_sibling_branch_is_not_subject_to_receipt_gate(self):
        self.git('switch', '-c', 'other-lane')
        self.child('pr', 'ready')
        self.assertEqual(self.event('Bash', {'command': 'gh pr ready'}).returncode, 0)

    def test_report_history_survives_retry(self):
        first = json.loads(self.record().stdout)
        self.record()
        self.assertEqual(self.status()['delivery_history'][0]['report'], first)
        self.assertEqual(self.status()['count'], 1)

    def test_changed_archived_report_is_not_a_valid_receipt(self):
        self.record()
        ledger = self.repo / '.git/starfleet-policy-guard/state.json'
        data = json.loads(ledger.read_text())
        data['items'][self.url]['delivery_run']['report']['missing'] = 0
        ledger.write_text(json.dumps(data))
        result = self.child('pr', 'ready', ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('report hash changed', result.stderr)

    def test_same_pr_candidate_can_use_receipt_after_worktree_handoff(self):
        self.record()
        sibling = self.base / 'sibling'
        self.git('worktree', 'add', '-b', 'handoff', sibling)
        self.call('bind', '--pr-url', self.url, cwd=sibling)
        self.call('check', 'ready', cwd=sibling)
        self.assertEqual(self.status(cwd=sibling)['delivery_run'], self.status()['delivery_run'])

    def test_live_adapter_checks_published_head_and_target_before_forwarding(self):
        self.record()
        ledger = self.repo / '.git/starfleet-policy-guard/state.json'
        data = json.loads(ledger.read_text())
        item = data['items'][self.url]
        item['fixture'] = False
        item['remote'] = {'pr': {'url': self.url, 'host': 'fixture.invalid',
                                  'endpoint': 'repos/starfleet/guard/pulls/901'}}
        ledger.write_text(json.dumps(data))
        remote_path = self.base / 'remote.json'
        remote = {'html_url': self.url, 'body': '', 'state': 'open',
                  'head': {'sha': self.git('rev-parse', 'HEAD').stdout.strip(), 'ref': 'lane-a'},
                  'base': {'sha': self.git('rev-parse', 'main').stdout.strip(), 'ref': 'main'}}
        api_dir = self.base / 'api-bin'
        api_dir.mkdir()
        api = api_dir / 'gh'
        api.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nimport json,sys\n'
                       'from urllib.parse import unquote\n'
                       'row=json.loads(Path(' + repr(str(remote_path)) + ').read_text())\n'
                       'if "/git/ref/heads/" in sys.argv[-1]:\n'
                       ' branch=unquote(sys.argv[-1].split("/git/ref/heads/")[1])\n'
                       ' print(json.dumps({"ref":"refs/heads/"+branch,"object":{"type":"commit","sha":row.get("target_head",row["base"]["sha"])}}))\n'
                       'else: print(json.dumps(row))\n')
        api.chmod(0o755)
        env = dict(os.environ, PATH=str(api_dir) + os.pathsep + os.environ['PATH'])
        for field in ('head', 'base'):
            original = remote[field]['sha']
            remote[field]['sha'] = '0' * 40
            remote_path.write_text(json.dumps(remote))
            result = self.call('gh-child', '--real-binary', self.fake, '--', 'pr', 'ready', env=env, ok=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Published PR candidate or target changed', result.stderr)
            self.assertFalse(self.calls.exists())
            remote[field]['sha'] = original
        remote['base']['ref'] = 'staging'
        remote_path.write_text(json.dumps(remote))
        self.assertEqual(self.call('gh-child', '--real-binary', self.fake, '--', 'pr', 'ready', env=env, ok=False).returncode, 2)
        remote['base']['ref'] = 'main'
        # A PR's cached base SHA can lag the real target without invalidating proof.
        remote['target_head'] = remote['base']['sha']
        remote['base']['sha'] = '1' * 40
        remote_path.write_text(json.dumps(remote))
        self.call('gh-child', '--real-binary', self.fake, '--', 'pr', 'ready', env=env)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_action_parser_does_not_mistake_body_text_for_target_or_exemption(self):
        action = delivery_runs.gh_action(['pr', 'merge', '901', '--body', '--disable-auto', '--auto'])
        self.assertFalse(action['exempt'])
        self.assertEqual(action['target'], '901')
        for argv in (['pr', 'ready', '--unexpected'], ['pr', 'merge', '901', '902'],
                     ['pr', 'ready', '-R', 'a/b', '--repo', 'a/b'],
                     ['pr', 'merge', '--disable-auto', '--auto']):
            with self.assertRaises(ValueError):
                delivery_runs.gh_action(argv)

    def test_github_repo_url_case_is_normalized_but_branch_names_are_not(self):
        action = delivery_runs.gh_action(['pr', 'ready', 'https://fixture.invalid/Starfleet/Guard/pull/901'])
        delivery_runs.target_matches(action, self.url, 'lane-a')
        for target in ('https://fixture.invalid/Starfleet/Guard/pull/902',
                       'https://fixture.invalid/Starfleet/Guard/pull/901?extra=1', 'LANE-A'):
            with self.assertRaises(ValueError):
                delivery_runs.target_matches(delivery_runs.gh_action(['pr', 'ready', target]), self.url, 'lane-a')
