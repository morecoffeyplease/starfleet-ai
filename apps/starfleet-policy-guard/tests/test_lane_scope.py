"""CLI capability trials on real Git branches, with a private GitHub adapter."""
import copy
import json
import subprocess
import time
from pathlib import Path
import sys
import unittest

import test_lane_install as fixtures


class LaneScopeTest(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LaneInstallTest('test_plan_is_readonly_and_install_preserves_existing_settings')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f
        f.env.update(CMUX_AGENT_LAUNCH_KIND='codex',CODEX_THREAD_ID='native-owner-1')
        f.git('update-ref','refs/remotes/origin/main',f.head)
        self.seat=f.base/'owner seat'
        f.git('worktree','add','-b','owner-seat',self.seat)
        binary=f.base/'bin/gh'
        binary.write_text('#!'+sys.executable+'\n'+'''import json,os,sys
from pathlib import Path
from urllib.parse import parse_qs,urlsplit,unquote
if sys.argv[1:2]==['api']:
 records=json.loads(Path(os.environ['FAKE_LANE_REMOTE']).read_text())['records']
 endpoint=sys.argv[-1]
 with open(os.environ['FAKE_LANE_API'],'a') as stream:stream.write(json.dumps({'endpoint':endpoint})+'\\n')
 if '/git/ref/heads/' in endpoint:
  branch=unquote(endpoint.split('/git/ref/heads/')[1])
  primary=next(iter(records.values()))
  print(json.dumps({'ref':'refs/heads/'+branch,'object':{'type':'commit','sha':primary['base']['sha']}}))
 elif '/pulls?' in endpoint:
  branch=parse_qs(urlsplit(endpoint).query)['head'][0].split(':',1)[1]
  print(json.dumps([r for r in records.values() if r['state']=='open' and r['head']['ref']==branch]))
 else:print(json.dumps(records[endpoint.split('/')[-1]]))
else:
 with open(os.environ['FAKE_LANE_FORWARDED'],'a') as stream:stream.write(json.dumps(sys.argv[1:])+'\\n')
''')
        binary.chmod(0o700)
        self.records={'901':f.remote}
        self.remote()
        packet=json.loads(f.packet.read_text());packet['base_ref']='refs/remotes/origin/main';f.packet.write_text(json.dumps(packet))

    def checkout_state_path(self, checkout):
        record = fixtures.lane_runtime.session_store.lookup_checkout(checkout)
        self.assertIsNotNone(record)
        return Path(record['checkout_dir']) / 'state.json'

    def remote(self):
        self.f.remote_path.write_text(json.dumps({'records':self.records}))

    def enroll(self):
        f=self.f
        args=['--session-root',str(self.seat)]
        scope=['--lane','--owner-harness','codex','--owner-session','native-owner-1','--host','fixture.invalid']
        proposed=json.loads(f.call(*args,'plan',*scope).stdout)
        f.call(*args,'install',*scope,'--plan-sha256',proposed['plan_sha256'])
        f.adapter=Path(proposed['adapter'])
        return proposed

    def test_one_enrollment_discovers_next_pr_and_restores_only_its_own_report(self):
        f=self.f;before=f.settings.read_bytes();plan=self.enroll()
        self.assertEqual(f.settings.read_bytes(),before)
        self.assertFalse((self.seat/'.claude/settings.local.json').exists())
        self.assertTrue(json.loads(f.call('status').stdout)['lane_enrolled'])
        self.assertEqual(json.loads(f.call('discover').stdout)['pr_url'],f.url)
        first=f.record()
        self.assertEqual(f.invoke('pr','ready',f.url,'--starfleet-probe').returncode,0)
        f.git('checkout','-b','feature/next')
        (f.repo/'product.txt').write_text('Next feature\n')
        f.git('add','.');f.git('commit','-m','next candidate')
        next_head=f.git('rev-parse','HEAD').stdout.strip()
        second=copy.deepcopy(f.remote)
        second.update(html_url=f.url.replace('901','902'))
        second['head'].update(ref='feature/next',sha=next_head)
        self.records['902']=second;self.remote()
        old_target=f.invoke('pr','ready',f.url,'--starfleet-probe')
        self.assertEqual(old_target.returncode,2)
        self.assertIn('Switch the product checkout back',old_target.stderr)
        self.assertIn('discover',old_target.stderr)
        self.assertNotIn('reviewed rebind',old_target.stderr)
        denied=f.invoke('pr','ready',second['html_url'],'--starfleet-probe')
        self.assertEqual(denied.returncode,2)
        self.assertIn('No recorded delivery report run for this PR',denied.stderr)
        state=json.loads(self.checkout_state_path(f.repo).read_text())
        self.assertEqual(state['pr_url'],second['html_url'])
        self.assertNotIn('receipt',state)
        self.assertEqual(state['pr_bindings'][f.url]['receipt']['report'],first)
        packet=f.base/'second-packet.json'
        packet.write_text(json.dumps({'pr_url':second['html_url'],'base_ref':'refs/remotes/origin/main',
            'design-fidelity':{'applicable':False,'revision':next_head,'reason':'No designs in this fixture'}}))
        f.call('record','--evidence',packet,'--format','json')
        self.assertEqual(f.invoke('pr','ready',second['html_url'],'--starfleet-probe').returncode,0)
        f.git('checkout','feature/test')
        f.call('discover')
        self.assertEqual(f.invoke('pr','ready',f.url,'--starfleet-probe').returncode,0)
        self.assertFalse(f.forwarded.exists(),'non-executing probes must never publish readiness')
        bindings=json.loads(f.call('registrations').stdout)['registrations']
        self.assertEqual({b['pr_url'] for b in bindings},{f.url,second['html_url']})
        self.assertFalse((f.repo/'.git/starfleet-policy-guard').exists(),'delivery enrollment must not create/reset fix-round state')

    def test_published_attestation_reference_stays_with_its_exact_pr_binding(self):
        lane_scope=fixtures.lane_runtime.lane_scope
        first_url='https://fixture.invalid/starfleet/guard/pull/901'
        second_url='https://fixture.invalid/starfleet/guard/pull/902'
        reference={'version':1,'pr_url':first_url,'comment_url':first_url+'#issuecomment-10',
                   'body_sha256':'a'*64,'attestation_sha256':'b'*64}
        state={'lane_owner':{'harness':'codex','session_id':'native-owner-1'},
               'branch':'feature/test','head_branch':'feature/test','pr_url':first_url,'base_ref':'main',
               'guidance_attestation_ref':reference,'pr_bindings':{}}
        lane_scope.select_binding(state, {'branch':'feature/next','head_branch':'feature/next',
                                          'pr_url':second_url,'base_ref':'main'})
        self.assertNotIn('guidance_attestation_ref',state)
        self.assertEqual(state['pr_bindings'][first_url]['guidance_attestation_ref'],reference)
        self.assertNotIn('guidance_attestation_ref',state['pr_bindings'][second_url])

    def test_native_hook_stays_local_while_explicit_seat_entrypoint_discovers(self):
        f=self.f
        plan=self.enroll()
        f.call('discover')
        f.record()
        f.api_calls.unlink(missing_ok=True)
        event={'hook_event_name':'PreToolUse','tool_name':'exec_command','session_id':'native-owner-1',
               'cwd':str(f.repo),'tool_input':{'cmd':'gh pr ready '+f.url+' --starfleet-probe'}}
        hooked=f.call('--seat-id',plan['seat_id'],'hook',input=json.dumps(event))
        self.assertEqual(hooked.returncode,0,hooked.stderr)
        self.assertFalse(f.api_calls.exists(),'native hook evaluation must not make GitHub API calls')
        explicit=f.invoke('pr','ready',f.url,'--starfleet-probe')
        self.assertEqual(explicit.returncode,0,explicit.stderr)
        calls=list(map(json.loads,f.api_calls.read_text().splitlines()))
        self.assertTrue(any('/pulls?' in row['endpoint'] for row in calls),
                        'explicit seat gh path must retain published-branch discovery')

    def test_recovered_native_lane_preserves_explicit_pr_alias_across_discovery(self):
        f = self.f
        published = f.remote['head']['ref']
        f.git('branch', '-m', 'recovered/local')
        original_remote = f.remote_path.read_bytes()
        historical = f.base / 'historical-checkpoint.json'
        historical.write_text('{"fix_rounds":17,"prior_installer_state":"unavailable"}')
        before = historical.read_bytes()
        scope = ['--lane', '--owner-harness', 'codex', '--owner-session', 'native-owner-1',
                 '--host', 'fixture.invalid', '--pr-url', f.url, '--base-ref', 'main',
                 '--head-branch', published]
        prefix = ['--session-root', self.seat]
        proposed = json.loads(f.call(*prefix, 'plan', *scope).stdout)
        self.assertFalse(proposed['manages_claude_hooks'])
        self.assertEqual(proposed['head_branch'], published)
        result = json.loads(f.call(*prefix, 'install', *scope,
                                  '--plan-sha256', proposed['plan_sha256']).stdout)
        self.assertIn('existing per-PR history is retained', result['history_notice'])
        self.assertIn('No manual count or receipt is inferred', result['history_notice'])
        f.adapter = Path(proposed['adapter'])
        self.assertEqual(json.loads(f.call('discover').stdout)['pr_url'], f.url)
        state_path = self.checkout_state_path(f.repo)
        state = json.loads(state_path.read_text())
        self.assertEqual(state['pr_bindings'][f.url]['history'], [])
        self.assertNotIn('receipt', state)
        self.assertEqual(state['installation_history'], [])
        self.assertEqual(original_remote, f.remote_path.read_bytes())
        first = f.record()
        self.assertEqual(f.invoke('pr', 'ready', f.url, '--starfleet-probe').returncode, 0)

        # A mapping belongs to its local branch, not every branch in the lane.
        f.git('switch', '-c', 'next/local')
        saved = state_path.read_bytes()
        self.assertEqual(f.call('discover', ok=False).returncode, 2)
        self.assertEqual(saved, state_path.read_bytes())
        other = copy.deepcopy(f.remote)
        other.update(html_url=f.url.replace('901', '902'))
        other['head']['ref'] = 'next/local'
        self.records['902'] = other
        self.remote()
        f.call('discover')
        self.assertNotIn('receipt', json.loads(state_path.read_text()))
        f.git('switch', 'recovered/local')
        f.call('discover')
        state = json.loads(state_path.read_text())
        self.assertEqual(state['head_branch'], published)
        self.assertEqual(state['receipt']['report'], first)
        self.assertEqual(f.invoke('pr', 'ready', f.url, '--starfleet-probe').returncode, 0)
        self.assertEqual(before, historical.read_bytes())
        self.assertFalse(f.forwarded.exists())
        self.assertFalse((f.repo / '.git/starfleet-policy-guard').exists())
        self.assertFalse((self.seat / '.claude/settings.local.json').exists())

    def test_explicit_native_pr_binding_refuses_incomplete_or_wrong_mapping(self):
        f = self.f
        common = ['plan', '--lane', '--host', 'fixture.invalid',
                  '--owner-harness', 'codex', '--owner-session', 'native-owner-1']
        for flags in (['--pr-url', f.url], ['--base-ref', 'main'], ['--head-branch', 'original']):
            self.assertEqual(f.call(*common, *flags, ok=False).returncode, 2)
        result = f.call(*common, '--pr-url', f.url, '--base-ref', 'main',
                        '--head-branch', 'wrong-published-branch', ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('must agree', result.stderr)
        self.assertFalse((f.repo / '.git/starfleet-delivery-lane/state.json').exists())

    def test_foreign_native_session_requires_explicit_handoff_and_new_report(self):
        f=self.f;self.enroll();f.call('discover');f.record()
        successor=dict(f.env,CMUX_AGENT_LAUNCH_KIND='claude',CLAUDE_CODE_SESSION_ID='native-successor')
        denied=f.call('discover',ok=False,env=successor)
        self.assertEqual(denied.returncode,2)
        self.assertIn('another native session',denied.stderr)
        wrong=f.call('rebind-owner','--from-harness','codex','--from-session','wrong','--confirm-handoff',ok=False,env=successor)
        self.assertEqual(wrong.returncode,2)
        f.call('rebind-owner','--from-harness','codex','--from-session','native-owner-1','--confirm-handoff',env=successor)
        result=json.loads(f.call('status',env=successor).stdout)
        self.assertEqual(result['lane_owner']['session_id'],'native-successor')
        self.assertIsNone(result['receipt'])
        self.assertEqual(len(result['history']),1)
        self.assertEqual(len(result['owner_history']),1)
        f.call('discover',env=successor)
        self.assertIsNone(json.loads(f.call('status',env=successor).stdout)['receipt'])
        self.assertEqual(f.call('discover',ok=False).returncode,2,'outgoing session loses enrollment ownership')

    def test_same_sha_retarget_requires_report_for_new_target(self):
        f=self.f;self.enroll();f.call('discover');f.record()
        f.git('update-ref','refs/remotes/origin/alternate',f.head)
        self.records['901']['base']['ref']='alternate';self.remote()
        f.call('discover')
        denied=f.invoke('pr','ready',f.url,'--starfleet-probe')
        self.assertEqual(denied.returncode,2)
        self.assertIn('target ref changed',denied.stderr)
        packet=json.loads(f.packet.read_text());packet['base_ref']='refs/remotes/origin/alternate'
        replacement=f.base/'retargeted-packet.json';replacement.write_text(json.dumps(packet))
        f.call('record','--evidence',replacement,'--format','json')
        self.assertEqual(f.invoke('pr','ready',f.url,'--starfleet-probe').returncode,0)
        self.assertFalse(f.forwarded.exists())

    def test_outgoing_record_cannot_race_owner_handoff(self):
        f=self.f;plan=self.enroll();f.call('discover')
        marker=f.base/'paused';release=f.base/'release'
        bootstrap='''import sys,pathlib,time,argparse
sys.path.insert(0,sys.argv[1]);import lane
original=lane.discover
def paused(cwd):
 result=original(cwd);pathlib.Path(sys.argv[4]).touch()
 deadline=time.monotonic()+10
 while not pathlib.Path(sys.argv[5]).exists():
  if time.monotonic()>deadline:raise RuntimeError('private fixture deadline')
  time.sleep(.01)
 return result
lane.discover=paused
try:lane.record(argparse.Namespace(cwd=pathlib.Path(sys.argv[2]),evidence=pathlib.Path(sys.argv[3]),policy=lane.delivery.POLICY,format='json'))
except Exception as error:print(str(error),file=sys.stderr);raise SystemExit(2)
'''
        process=subprocess.Popen([sys.executable,'-c',bootstrap,str(fixtures.APP),str(f.repo),str(f.packet),str(marker),str(release)],
                                 env=f.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            deadline=time.monotonic()+10
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(marker.exists())
            incoming=dict(f.env,CMUX_AGENT_LAUNCH_KIND='claude',CLAUDE_CODE_SESSION_ID='native-successor')
            f.call('rebind-owner','--from-harness','codex','--from-session','native-owner-1','--confirm-handoff',env=incoming)
            release.touch();_,error=process.communicate(timeout=10)
            self.assertEqual(process.returncode,2)
            self.assertIn('another native session',error)
            state=json.loads(self.checkout_state_path(f.repo).read_text())
            self.assertNotIn('receipt',state)
        finally:
            release.touch()
            if process.poll() is None:process.communicate(timeout=12)

    def test_ambiguous_or_absent_pr_preserves_registration_and_repo_changes_refuse(self):
        f=self.f;plan=self.enroll();f.call('discover')
        state=self.checkout_state_path(f.repo)
        before=state.read_bytes()
        self.records['902']=copy.deepcopy(f.remote);self.records['902']['html_url']=f.url.replace('901','902');self.remote()
        self.assertEqual(f.call('discover',ok=False).returncode,2)
        self.assertEqual(state.read_bytes(),before)
        self.records.clear();self.remote()
        self.assertEqual(f.call('discover',ok=False).returncode,2)
        self.assertEqual(state.read_bytes(),before)
        f.git('remote','set-url','origin','git@fixture.invalid:someone/else.git')
        self.assertEqual(f.call('discover',ok=False).returncode,2)
        self.assertEqual(state.read_bytes(),before)


if __name__=='__main__':unittest.main()
