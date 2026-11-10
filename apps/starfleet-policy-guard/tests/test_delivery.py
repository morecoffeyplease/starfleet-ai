"""Read-only delivery evidence against real disposable Git histories."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import delivery


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'product.txt').write_text('original\n')
        (self.repo / 'README.md').write_text('Canonical verification: python3 verify.py\n')
        self.git('add', '.')
        self.git('commit', '-m', 'base')
        self.git('switch', '-c', 'feature')
        (self.repo / 'feature.txt').write_text('user benefit\n')
        self.git('add', '.')
        self.git('commit', '-m', 'feature')
        self.current = delivery.snapshot(self.repo, 'main')
        self.url = 'https://github.com/example/repo/pull/1'
        self.packet = self.root / 'packet.json'
        self.data = {
            'pr_url': self.url, 'base_ref': 'main',
            'design-fidelity': {'applicable': False, 'reason': 'No designed UI in this change',
                                'revision': self.current['head']},
            'main-awareness': {'revision': self.current['head'], 'base_head': self.current['base_head'],
                               'relevant_changes': 'No overlapping changes', 'existing_solutions': 'None found',
                               'evidence': self.artifact('main-review.txt', 'Compared target changes')},
            'local-verification': {'revision': self.current['head'], 'command': ['python3', 'verify.py'],
                                   'command_source': self.ref(self.repo / 'README.md'),
                                   'exit_code': 0, 'outcome': 'passed',
                                   'evidence': self.artifact('verify.txt', 'Successful canonical verification'),
                                   'inputs': [], 'input_scope': 'Tracked tree; no external inputs required'}
        }

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True,
                                env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def ref(self, path):
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def artifact(self, name, contents):
        path = self.root / name
        path.write_text(contents)
        return self.ref(path)

    def report(self, data=None, policy=None):
        self.packet.write_text(json.dumps(self.data if data is None else data))
        return delivery.report(self.repo, 'main', self.url, self.packet, policy or delivery.POLICY)

    def guidance_attestation(self, stage='opening'):
        accepted = ({'accepted_batch_ref': 'https://github.com/example/repo/issues/37#accepted-batch-2',
                     'repair_history_ref': 'https://github.com/example/repo/pull/1#issuecomment-9'}
                    if stage == 'repair_batch' else {})
        return {
            'version': 1, 'stage': stage, 'issue_ref': 'https://github.com/example/repo/issues/37',
            **accepted, 'prerequisites_complete': True, 'gaps': [],
            'reread': [{'kind': 'playbook', 'name': 'Feature playbook',
                        'source_ref': 'https://github.com/example/repo/blob/abc/playbook.md',
                        **self.artifact('guidance.md', 'Current reviewed guidance')}],
            'guidance_application': 'The playbook limits the change to the validated delivery path.',
            'approach': 'Reuse the existing evidence packet and owner-bound delivery lane.',
            'approach_reason': 'A narrower change would leave the approved readiness path without the current check.',
            'evidence_refs': ['main-awareness.evidence at main-review.txt'],
            'assumptions': [], 'uncertainty': ['A later authenticated GitHub comment read may be unavailable.'],
            'approved_scope_ref': 'https://github.com/example/repo/issues/37#issuecomment-1',
            'human_holds_ref': 'No additional hold; issue 37 remains authoritative',
        }

    def item(self, identifier, report=None):
        return next(x for x in (report or self.report())['items'] if x['id'] == identifier)

    def test_satisfied_and_explicit_not_applicable_without_writes(self):
        before = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        result = self.report()
        self.assertEqual(result['missing'], 0)
        self.assertEqual(self.item('design-fidelity', result)['status'], 'explicitly-not-applicable')
        after = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_opening_attestation_allows_unrelated_gaps_but_requires_clean_canonical_verification(self):
        self.data['guidance_attestation'] = self.guidance_attestation()
        report = self.report()
        packet = json.loads(self.packet.read_text())
        rendered = delivery.render_guidance_attestation(packet, self.packet.parent, self.url, 'opening', report)
        self.assertIn(delivery.GUIDANCE_ATTESTATION_STATEMENT, rendered['body'])
        self.assertLess(rendered['body'].index('## Approach'), rendered['body'].index('## Opening attestation'))
        self.assertLess(rendered['body'].index('## Material uncertainty'), rendered['body'].index('## Opening attestation'))
        self.assertTrue(rendered['body'].rstrip().endswith(delivery.GUIDANCE_ATTESTATION_STATEMENT))

        self.data['main-awareness']['evidence']['sha256'] = '0' * 64
        unrelated_gap = self.report()
        self.assertEqual(unrelated_gap['missing'], 1)
        self.assertEqual(self.item('local-verification', unrelated_gap)['status'], 'satisfied')
        packet = json.loads(self.packet.read_text())
        rendered = delivery.render_guidance_attestation(packet, self.packet.parent, self.url, 'opening', unrelated_gap)
        self.assertIn(delivery.GUIDANCE_ATTESTATION_STATEMENT, rendered['body'])

        self.data['main-awareness']['evidence']['sha256'] = self.artifact('main-review.txt', 'Compared target changes')['sha256']
        self.data['local-verification']['outcome'] = 'nothing-affected'
        self.data['local-verification']['nothing_affected_reason'] = 'The canonical runner reported no affected targets'
        nothing_affected = self.report()
        self.assertEqual(self.item('local-verification', nothing_affected)['status'], 'satisfied')
        packet = json.loads(self.packet.read_text())
        delivery.render_guidance_attestation(packet, self.packet.parent, self.url, 'opening', nothing_affected)

        self.data['local-verification']['exit_code'] = 1
        incomplete = self.report()
        packet = json.loads(self.packet.read_text())
        self.assertEqual(self.item('local-verification', incomplete)['status'], 'missing')
        with self.assertRaisesRegex(ValueError, 'successful canonical verification'):
            delivery.render_guidance_attestation(packet, self.packet.parent, self.url, 'opening', incomplete)

    def test_repair_attestation_precedes_work_without_requiring_final_report(self):
        self.data['guidance_attestation'] = self.guidance_attestation('repair_batch')
        self.report()
        packet = json.loads(self.packet.read_text())
        rendered = delivery.render_guidance_attestation(packet, self.packet.parent, self.url, 'repair_batch')
        self.assertTrue(rendered['body'].startswith('# Accepted repair-batch attestation\n\n' +
                                                     delivery.GUIDANCE_ATTESTATION_STATEMENT))
        self.assertIn('Accepted batch: https://github.com/example/repo/issues/37#accepted-batch-2', rendered['body'])
        self.assertNotIn('repair verification passed', rendered['body'].casefold())

    def test_assumptions_must_be_explicitly_empty_and_render_as_none(self):
        self.data['guidance_attestation'] = self.guidance_attestation()
        report = self.report()
        packet = json.loads(self.packet.read_text())
        rendered = delivery.render_guidance_attestation(packet, self.packet.parent, self.url, 'opening', report)
        self.assertIn('## Material assumptions\n\n- None', rendered['body'])

        invalid_cases = (
            ('missing', None, False),
            ('null', None, True),
            ('object', {}, True),
            ('string', 'not-a-list', True),
            ('nonempty', ['The remote comment will remain readable.'], True),
        )
        for name, value, present in invalid_cases:
            attestation = self.guidance_attestation()
            if present:
                attestation['assumptions'] = value
            else:
                del attestation['assumptions']
            with self.subTest(assumptions=name):
                with self.assertRaisesRegex(ValueError, 'assumptions must be an explicit empty list'):
                    delivery.guidance_attestation({'guidance_attestation': attestation},
                                                  self.packet.parent, self.url)

    def test_attestation_gaps_and_changed_reread_bytes_refuse(self):
        attestation = self.guidance_attestation()
        attestation['prerequisites_complete'] = False
        self.data['guidance_attestation'] = attestation
        self.report()
        with self.assertRaisesRegex(ValueError, 'not explicitly complete'):
            delivery.guidance_attestation(json.loads(self.packet.read_text()), self.packet.parent, self.url)
        attestation['prerequisites_complete'] = True
        attestation['gaps'] = ['Need a decision']
        self.report()
        with self.assertRaisesRegex(ValueError, 'unresolved prerequisite gaps'):
            delivery.guidance_attestation(json.loads(self.packet.read_text()), self.packet.parent, self.url)
        attestation['gaps'] = []
        self.report()
        (self.root / 'guidance.md').write_text('Changed reviewed guidance')
        with self.assertRaisesRegex(ValueError, 'Evidence hash changed'):
            delivery.guidance_attestation(json.loads(self.packet.read_text()), self.packet.parent, self.url)

    def test_missing_packet_cannot_produce_baseline_report(self):
        with self.assertRaisesRegex(ValueError, 'required before reporting'):
            delivery.report(self.repo, 'main', self.url, self.root / 'absent.json')

    def test_source_change_invalidates_evidence(self):
        (self.repo / 'feature.txt').write_text('new mechanism\n')
        self.git('commit', '-am', 'change')
        result = self.report()
        self.assertEqual(result['missing'], 3)
        self.assertIn('changed', self.item('local-verification', result)['detail'])

    def test_empty_commit_reuses_verification(self):
        self.git('commit', '--allow-empty', '-m', 'metadata only')
        self.assertEqual(self.report()['missing'], 0)

    def test_dirty_untracked_candidate_is_unproven(self):
        (self.repo / 'untracked.py').write_text('new input')
        self.assertEqual(self.report()['missing'], 4)

    def test_behind_target_is_allowed_and_main_assessment_needs_new_snapshot(self):
        self.git('switch', 'main')
        (self.repo / 'other.txt').write_text('fix on main\n')
        self.git('add', '.')
        self.git('commit', '-m', 'main fix')
        self.git('switch', 'feature')
        result = self.report()
        self.assertEqual(result['missing'], 1)
        self.assertEqual(self.item('merge-conflicts', result)['status'], 'satisfied')
        self.assertEqual(self.item('local-verification', result)['status'], 'satisfied')
        self.assertIn('main-awareness.base_head', self.item('main-awareness', result)['detail'])

    def test_real_conflict_without_repository_mutation(self):
        (self.repo / 'product.txt').write_text('feature change\n')
        self.git('commit', '-am', 'feature conflict')
        self.git('switch', 'main')
        (self.repo / 'product.txt').write_text('main change\n')
        self.git('commit', '-am', 'main conflict')
        self.git('switch', 'feature')
        before = self.git('status', '--porcelain')
        result = self.report()
        self.assertEqual(self.item('merge-conflicts', result)['status'], 'missing')
        self.assertIn('CONFLICT', self.item('merge-conflicts', result)['detail'])
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertFalse((self.repo / '.git/MERGE_HEAD').exists())

    def test_external_verification_input_changed(self):
        self.data['local-verification']['inputs'] = [self.artifact('environment.txt', 'version 1')]
        self.assertEqual(self.report()['missing'], 0)
        (self.root / 'environment.txt').write_text('version 2')
        self.assertEqual(self.item('local-verification')['status'], 'missing')

    def test_failed_missing_and_nothing_affected_verification(self):
        self.data['local-verification']['exit_code'] = 1
        self.assertEqual(self.item('local-verification')['status'], 'missing')
        self.data['local-verification']['exit_code'] = 0
        self.data['local-verification']['outcome'] = 'nothing-affected'
        self.assertEqual(self.item('local-verification')['status'], 'missing')
        self.data['local-verification']['nothing_affected_reason'] = 'Canonical runner explicitly found no affected targets'
        item = self.item('local-verification')
        self.assertEqual(item['status'], 'satisfied')
        self.assertIn('no tests-passed claim', item['detail'])

    def test_package_verify_script_does_not_override_documented_command(self):
        (self.repo / 'package.json').write_text('{"scripts":{"verify":"some other command"}}')
        self.git('add', '.')
        self.git('commit', '-m', 'canonical script')
        self.data['local-verification']['revision'] = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.item('local-verification')['status'], 'satisfied')
        self.data['local-verification']['command'] = ['make', 'verify']
        item = self.item('local-verification')
        self.assertEqual(item['status'], 'missing')
        self.assertIn('local-verification.command_source', item['detail'])

    def designed(self):
        self.data['design-fidelity'] = {
            'applicable': True, 'revision': self.current['head'], 'skill': 'design-fidelity-audit',
            'design_reference': 'Approved design version 1', 'design': self.artifact('design.html', 'Design v1'),
            'audit': self.artifact('audit.txt', 'Completed audit'), 'coverage': 'FULL', 'fidelity': 'DRIFT',
            'assessed_scope': 'Feature page in its saved state at phone viewport', 'not_assessable': [], 'unresolved': [],
            'authority_map': [{'region_state': 'Saved feature page', 'reference': 'Design v1 spacing annotation',
                               'properties_specified': 'Spacing', 'properties_open': 'Placeholder names'}],
            'findings': [{'id': 'F1', 'region_state': 'Saved feature page', 'property': 'Spacing',
                          'reference_says': '8px', 'implementation_shows': '12px', 'verdict': 'DRIFT',
                          'reference_evidence': self.artifact('reference.png', 'reference image fixture'),
                          'implementation_evidence': self.artifact('implementation.png', 'implementation image fixture'),
                          'deviation_id': 'D1'}],
            'deviations': [{'id': 'D1', 'description': 'Use alternative spacing', 'approval': {
                'by': 'user', 'decision': 'approved', 'channel': 'direct-user', 'source': 'conversation ID / message ID',
                'quote': 'I approve D1 spacing', 'evidence': self.artifact('approval.txt', 'User: I approve D1 spacing')}}]}

    def test_every_design_deviation_requires_configured_authority_and_direct_source(self):
        self.designed()
        self.assertEqual(self.item('design-fidelity')['status'], 'satisfied')
        self.data['design-fidelity']['deviations'].append({'id': 'D2', 'description': 'Other deviation'})
        self.assertEqual(self.item('design-fidelity')['status'], 'missing')
        self.data['design-fidelity']['deviations'].pop()
        self.data['design-fidelity']['deviations'][0]['approval']['channel'] = 'peer-relay'
        self.assertEqual(self.item('design-fidelity')['status'], 'missing')

    def test_custom_authority_and_accepted_channel_are_configurable(self):
        self.designed()
        policy = json.loads(delivery.POLICY.read_text())
        policy['approval_authority'] = {'label': 'release owner', 'accepted_channels': ['change-control']}
        path = self.root / 'custom-policy.json'
        path.write_text(json.dumps(policy))
        approval = self.data['design-fidelity']['deviations'][0]['approval']
        approval.update(by='release owner', channel='change-control')
        self.assertEqual(self.item('design-fidelity', self.report(policy=path))['status'], 'satisfied')
        approval['by'] = 'user'
        self.assertEqual(self.item('design-fidelity', self.report(policy=path))['status'], 'missing')
        approval.update(by='release owner', channel='direct-user')
        self.assertEqual(self.item('design-fidelity', self.report(policy=path))['status'], 'missing')

    def test_design_change_and_incomplete_audit_are_missing(self):
        self.designed()
        self.data['design-fidelity']['coverage'] = 'partial'
        self.assertEqual(self.item('design-fidelity')['status'], 'missing')
        self.data['design-fidelity']['coverage'] = 'FULL'
        (self.root / 'design.html').write_text('Changed design')
        self.assertEqual(self.item('design-fidelity')['status'], 'missing')

    def test_unknown_applicability_and_malformed_evidence_do_not_pass(self):
        self.data['design-fidelity'] = {}
        with self.assertRaisesRegex(ValueError, 'declare design applicability'):
            self.report()
        self.data['design-fidelity'] = {'applicable': False, 'reason': 'No design', 'revision': self.current['head']}
        self.data['main-awareness'] = []
        self.data['local-verification']['evidence'] = {'path': 'missing', 'sha256': 'bad'}
        self.assertEqual(self.report()['missing'], 2)
        self.packet.write_text('not json')
        with self.assertRaises(ValueError):
            delivery.report(self.repo, 'main', self.url, self.packet)

    def test_wrong_pr_binding(self):
        self.data['pr_url'] = 'https://github.com/other/repo/pull/1'
        with self.assertRaisesRegex(ValueError, 'pr_url must identify the requested repository and PR'):
            self.report()

    def test_configurable_checklist_and_invalid_check_ids(self):
        policy = json.loads(delivery.POLICY.read_text())
        policy['checks'][0]['enabled'] = False
        policy['checks'][1]['instruction'] = 'Custom PM-readable instruction'
        path = self.root / 'policy.json'
        path.write_text(json.dumps(policy))
        result = self.report(policy=path)
        self.assertEqual(result['disabled'], ['design-fidelity'])
        self.assertEqual(self.item('main-awareness', result)['instruction'], 'Custom PM-readable instruction')
        policy['checks'][0]['id'] = 'arbitrary-shell-command'
        path.write_text(json.dumps(policy))
        with self.assertRaises(ValueError):
            self.report(policy=path)

    def test_moving_evidence_revision_is_rejected(self):
        self.data['local-verification']['revision'] = 'HEAD'
        self.assertIn('immutable full commit SHA', self.item('local-verification')['detail'])

    def test_custom_merge_driver_is_not_executed(self):
        sentinel = self.root / 'driver-executed'
        self.git('config', 'merge.custom.driver', 'touch ' + str(sentinel))
        item = self.item('merge-conflicts')
        self.assertEqual(item['status'], 'missing')
        self.assertIn('Custom merge drivers', item['detail'])
        self.assertFalse(sentinel.exists())

    def test_cli_report_has_no_enrollment_dependency_or_gate(self):
        del self.data['main-awareness']
        del self.data['local-verification']
        self.packet.write_text(json.dumps(self.data))
        result = subprocess.run([sys.executable, str(APP / 'guard.py'), '--cwd', str(self.repo),
                                 'delivery', 'report', '--base-ref', 'main', '--pr-url', self.url,
                                 '--evidence', str(self.packet), '--format', 'json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['missing'], 2)
        self.assertFalse((self.repo / '.git/starfleet-policy-guard').exists())

    def test_no_audit_no_report_but_partial_audit_is_reported(self):
        self.designed()
        record = self.data['design-fidelity']
        audit = record.pop('audit')
        with self.assertRaisesRegex(ValueError, 'Conduct and record'):
            self.report()
        record['audit'] = audit
        record['coverage'] = 'PARTIAL'
        record['not_assessable'] = ['Error state blocked by unavailable fixture']
        self.assertEqual(self.item('design-fidelity')['status'], 'missing')

    def test_all_independent_fidelity_gaps_survive_stale_revision(self):
        self.designed()
        record = self.data['design-fidelity']
        record['coverage'] = 'PARTIAL'
        record['unresolved'] = ['Missing error state']
        record['not_assessable'] = ['Error state']
        del record['deviations'][0]['approval']
        (self.repo / 'feature.txt').write_text('later UI change')
        self.git('commit', '-am', 'later change')
        item = self.item('design-fidelity')
        fields = {gap['field'] for gap in item['gaps']}
        self.assertTrue({'revision', 'coverage', 'unresolved', 'not_assessable',
                         'deviations.0.approval', 'findings.0.deviation_id'} <= fields)
        self.assertEqual(item['coverage'], 'PARTIAL')
        self.assertEqual(item['fidelity'], 'DRIFT')
        self.assertEqual(item['findings'], record['findings'])

    def test_all_verification_and_main_awareness_gaps_are_listed(self):
        verify = self.data['local-verification']
        verify.update(exit_code=1, outcome='unknown', inputs=[{'path': 'missing', 'sha256': 'bad'}], input_scope='')
        verify.pop('outcome')
        verify.pop('evidence')
        main = self.data['main-awareness']
        main.update(base_head='old', relevant_changes='', existing_solutions='')
        result = self.report()
        self.assertTrue({'exit_code', 'outcome', 'evidence', 'inputs.0', 'input_scope'} <=
                        {gap['field'] for gap in self.item('local-verification', result)['gaps']})
        self.assertTrue({'base_head', 'relevant_changes', 'existing_solutions'} <=
                        {gap['field'] for gap in self.item('main-awareness', result)['gaps']})

    def prepare_reuse(self):
        self.designed()
        (self.repo / 'README.md').write_text('Unrelated documentation change\n')
        self.git('commit', '-am', 'unrelated docs')
        self.data['design-fidelity']['reuse'] = {
            'candidate_revision': self.git('rev-parse', 'HEAD'),
            'comparison': self.artifact('reuse.md', 'Compared later changes; only unrelated documentation changed'),
            'audited_paths': ['feature.txt'], 'dependency_paths': ['product.txt'],
            'dependency_scope': 'product.txt represents the full declared shared dependency for this fixture'}

    def test_reviewed_unchanged_scope_reuses_audit_but_not_verification(self):
        self.prepare_reuse()
        result = self.report()
        item = self.item('design-fidelity', result)
        self.assertEqual(item['status'], 'satisfied', item['detail'])
        self.assertEqual(item['audit_comparison']['mode'], 'reviewed-scope')
        self.assertEqual(item['findings'][0]['verdict'], 'DRIFT')
        self.assertEqual(self.item('local-verification', result)['status'], 'missing')

    def test_reuse_requires_exact_candidate_and_existing_literal_paths(self):
        self.prepare_reuse()
        reuse = self.data['design-fidelity']['reuse']
        candidate = reuse['candidate_revision']
        reuse['candidate_revision'] = self.current['head']
        self.assertIn('exact current candidate', self.item('design-fidelity')['detail'])
        reuse['candidate_revision'] = candidate
        for path in ('does-not-exist', '../feature.txt', ':(exclude)feature.txt', '*.txt'):
            reuse['audited_paths'] = [path]
            self.assertEqual(self.item('design-fidelity')['status'], 'missing', path)

    def test_changed_shared_dependency_requires_new_audit(self):
        self.prepare_reuse()
        (self.repo / 'product.txt').write_text('Changed shared behavior\n')
        self.git('commit', '-am', 'shared change')
        self.data['design-fidelity']['reuse']['candidate_revision'] = self.git('rev-parse', 'HEAD')
        self.assertIn('shared dependencies changed', self.item('design-fidelity')['detail'])

    def test_reuse_requires_preserved_comparison_artifact(self):
        self.prepare_reuse()
        (self.root / 'reuse.md').write_text('Changed comparison')
        self.assertIn('Evidence hash changed', self.item('design-fidelity')['detail'])

    def test_new_file_in_audited_directory_prevents_reuse(self):
        (self.repo / 'ui').mkdir()
        (self.repo / 'ui/view.txt').write_text('Audited view')
        self.git('add', '.')
        self.git('commit', '-m', 'view')
        self.current = delivery.snapshot(self.repo, 'main')
        self.prepare_reuse()
        self.data['design-fidelity']['reuse']['audited_paths'] = ['ui']
        self.assertEqual(self.item('design-fidelity')['status'], 'satisfied')
        (self.repo / 'ui/control.txt').write_text('New control')
        self.git('add', '.')
        self.git('commit', '-m', 'new UI file')
        self.data['design-fidelity']['reuse']['candidate_revision'] = self.git('rev-parse', 'HEAD')
        self.assertIn('ui/control.txt', self.item('design-fidelity')['detail'])

    def test_renamed_or_deleted_audited_surface_cannot_reuse(self):
        self.prepare_reuse()
        self.git('mv', 'feature.txt', 'renamed.txt')
        self.git('commit', '-m', 'rename audited surface')
        self.data['design-fidelity']['reuse']['candidate_revision'] = self.git('rev-parse', 'HEAD')
        self.assertIn('feature.txt', self.item('design-fidelity')['detail'])

    def test_reuse_does_not_clear_other_findings_or_unapproved_deviations(self):
        self.prepare_reuse()
        self.data['design-fidelity']['coverage'] = 'PARTIAL'
        del self.data['design-fidelity']['deviations'][0]['approval']
        item = self.item('design-fidelity')
        self.assertEqual(item['audit_comparison']['mode'], 'reviewed-scope')
        self.assertEqual(item['status'], 'missing')
        self.assertIn('approval from user', item['detail'])

    def test_faithful_cannot_hide_drift_and_unreachable_is_not_missing(self):
        self.designed()
        self.data['design-fidelity']['fidelity'] = 'FAITHFUL'
        self.assertIn('every assessable property', self.item('design-fidelity')['detail'])
        finding = self.data['design-fidelity']['findings'][0]
        finding.update(verdict='NOT_ASSESSABLE', blocker='No seeded error state')
        finding.pop('implementation_evidence')
        item = self.item('design-fidelity')
        self.assertEqual(item['status'], 'missing')
        self.assertIn('cannot be assessed', item['detail'])
        self.assertFalse(any(gap['field'].endswith('implementation_evidence') for gap in item['gaps']))

    def test_partial_faithful_scope_keeps_coverage_gaps_without_false_conflict(self):
        self.designed()
        record = self.data['design-fidelity']
        record.update(coverage='PARTIAL', fidelity='FAITHFUL', not_assessable=['Spinner not captured'], deviations=[])
        record['findings'][0].update(verdict='MATCH', implementation_shows='8px')
        record['findings'][0].pop('deviation_id', None)
        record['findings'].append({'id': 'F2', 'region_state': 'Spinner', 'property': 'Position',
                                   'reference_says': 'Centered', 'implementation_shows': 'Not captured',
                                   'verdict': 'NOT_ASSESSABLE', 'blocker': 'No comparable rendered frame'})
        self.packet.write_text(json.dumps(self.data))
        run = subprocess.run([sys.executable, str(APP / 'guard.py'), '--cwd', str(self.repo),
                              'delivery', 'report', '--base-ref', 'main', '--pr-url', self.url,
                              '--evidence', str(self.packet), '--format', 'json'], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        item = self.item('design-fidelity', json.loads(run.stdout))
        fields = {gap['field'] for gap in item['gaps']}
        self.assertEqual(item['status'], 'missing')
        self.assertTrue({'coverage', 'not_assessable', 'findings.1'}.issubset(fields))
        self.assertNotIn('fidelity', fields)
        record['findings'].pop(0)
        self.assertIn('fidelity', {gap['field'] for gap in self.item('design-fidelity')['gaps']})

    def test_text_report_keeps_gaps_and_approved_deviations_visible(self):
        self.designed()
        self.data['design-fidelity']['coverage'] = 'PARTIAL'
        self.packet.write_text(json.dumps(self.data))
        result = subprocess.run([sys.executable, str(APP / 'guard.py'), '--cwd', str(self.repo),
                                 'delivery', 'report', '--base-ref', 'main', '--pr-url', self.url,
                                 '--evidence', str(self.packet)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Coverage: PARTIAL; Fidelity: DRIFT', result.stdout)
        self.assertIn('F1 / Saved feature page / Spacing: DRIFT', result.stdout)
        self.assertIn('Requested deviation D1', result.stdout)

    def test_cli_stale_audit_does_not_hide_coverage_or_approval_gaps(self):
        self.designed()
        self.data['design-fidelity']['coverage'] = 'PARTIAL'
        del self.data['design-fidelity']['deviations'][0]['approval']
        (self.repo / 'feature.txt').write_text('Changed audited UI\n')
        self.git('add', '.')
        self.git('commit', '-m', 'changed UI')
        self.packet.write_text(json.dumps(self.data))
        result = subprocess.run([sys.executable, str(APP / 'guard.py'), '--cwd', str(self.repo),
                                 'delivery', 'report', '--base-ref', 'main', '--pr-url', self.url,
                                 '--evidence', str(self.packet), '--format', 'json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        item = self.item('design-fidelity', json.loads(result.stdout))
        fields = [gap['field'] for gap in item['gaps']]
        self.assertIn('revision', fields)
        self.assertIn('coverage', fields)
        self.assertIn('deviations.0.approval', fields)
        self.assertEqual(item['findings'][0]['id'], 'F1')

    def test_package_manifest_does_not_stand_in_for_documented_command(self):
        package = self.repo / 'package.json'
        package.write_text('{"scripts":{"verify":"python3 verify.py"}}')
        self.git('add', '.')
        self.git('commit', '-m', 'canonical script')
        record = self.data['local-verification']
        record.update(revision=self.git('rev-parse', 'HEAD'), command=['make', 'verify'],
                      command_source=self.ref(package))
        item = self.item('local-verification')
        self.assertEqual(item['status'], 'missing')
        self.assertIn('command_source', item['detail'])
        record.update(command=['python3', 'verify.py'], command_source=self.ref(self.repo / 'README.md'))
        self.assertEqual(self.item('local-verification')['status'], 'satisfied')

    def test_cli_requires_exact_documentation_for_canonical_command(self):
        package = self.repo / 'package.json'
        contents = '{"scripts":{"verify":"python3 verify.py"}}'
        package.write_text(contents)
        self.git('add', '.')
        self.git('commit', '-m', 'canonical script')
        record = self.data['local-verification']
        record.update(revision=self.git('rev-parse', 'HEAD'), command=['make', 'verify'],
                      command_source=self.ref(package))

        def inspect():
            self.packet.write_text(json.dumps(self.data))
            run = subprocess.run([sys.executable, str(APP / 'guard.py'), '--cwd', str(self.repo),
                                  'delivery', 'report', '--base-ref', 'main', '--pr-url', self.url,
                                  '--evidence', str(self.packet), '--format', 'json'], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            return self.item('local-verification', json.loads(run.stdout))

        self.assertEqual(inspect()['status'], 'missing')
        record.update(command=['python3', 'verify.py'], command_source=self.ref(self.repo / 'README.md'))
        self.assertEqual(inspect()['status'], 'satisfied')

    def test_verification_errors_name_the_exact_packet_fields(self):
        record = self.data['local-verification']
        record['command'] = 'python3 verify.py'
        record['result'] = record.pop('evidence')
        detail = self.item('local-verification')['detail']
        self.assertIn('local-verification.command', detail)
        self.assertIn('local-verification.evidence', detail)
        self.assertIn('not named result', detail)

    def test_cli_refuses_an_empty_packet_before_reporting(self):
        self.packet.write_text('{}')
        result = subprocess.run([sys.executable, str(APP / 'guard.py'), '--cwd', str(self.repo),
                                 'delivery', 'report', '--base-ref', 'main', '--pr-url', self.url,
                                 '--evidence', str(self.packet)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
