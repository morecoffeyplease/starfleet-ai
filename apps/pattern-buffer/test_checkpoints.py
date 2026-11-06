"""Installed CLI and hook paths for private orchestrator checkpoints."""
import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import uuid

import pattern_buffer_checkpoint as checkpoints

SCRIPT = Path(__file__).with_name('pattern_buffer.py')
NOTE = '# Working checkpoint\n\n' + '\n\n'.join(
    '## ' + title + '\n' + body for title, body in [
        ('Current state', 'Implementation is awaiting the authorized small check.'),
        ('Next action', 'Read result marker ORBIT-42; do not repeat the completed launch.'),
        ('Decisions and uncertainty', 'Rejected resubmission because ticket KEEP-17 still owns the job.'),
        ('Active work', 'CPU ticket KEEP-17; results in the private evidence directory.'),
        ('Evidence', 'No readiness claim. Historical proof belongs to its original candidate.'),
        ('Holds and guidance', 'The configured human authority must approve deployment; preserve that hold.')]) + '\n'


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('-c', 'user.name=Pilot', '-c', 'user.email=pilot@example.invalid',
                 'commit', '--allow-empty', '-qm', 'Fixture')
        self.cli('init', '--default-role', 'orchestrator')
        self.note = self.root.parent / 'draft.md'
        self.note.write_text(NOTE)
        self.trace = self.root.parent / 'session.jsonl'
        self.trace.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'pilot'}}) + '\n')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True, text=True)

    def cli(self, *args, data=None, ok=True, env=None, script=SCRIPT, cwd=None):
        r = subprocess.run([sys.executable, str(script), '--cwd', str(cwd or self.root), *args],
                           input=json.dumps(data) if data is not None else None, text=True,
                           capture_output=True, env={**os.environ, **(env or {})})
        self.assertEqual(r.returncode, 0 if ok else 1, r.stdout + r.stderr)
        return r

    def command(self, name, harness='codex', session='pilot', *extra, **kwargs):
        return self.cli(name, '--harness', harness, '--session', session, *extra, **kwargs)

    def enroll(self, harness='codex', session='pilot'):
        return json.loads(self.command('checkpoint-init', harness, session, '--compact-at', '95' if harness == 'codex' else '60').stdout)

    def save(self, harness='codex', session='pilot'):
        return json.loads(self.command('checkpoint-save', harness, session, '--file', str(self.note)).stdout)

    def event(self, name='SessionStart', harness='codex', session='pilot', env=None, **extra):
        payload = {'hook_event_name': name, 'session_id': session, 'cwd': str(self.root),
                   'source': 'compact', 'transcript_path': str(self.trace), **extra}
        return self.cli('hook', '--harness', harness, data=payload, env=env)

    def append(self, row):
        with self.trace.open('a') as f:
            f.write(json.dumps(row) + '\n')

    def usage(self, percent=92, timestamp=None):
        self.append({'type': 'event_msg', 'timestamp': timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     'payload': {'type': 'token_count', 'info': {'last_token_usage': {'total_tokens': percent * 100},
                                                              'model_context_window': 10000}}})

    def assert_pressure_warning(self, result):
        self.assertTrue(result.stdout,
                        f"Expected pressure warning but hook stdout was empty (exit {result.returncode}); "
                        f"stderr: {result.stderr}")
        output = json.loads(result.stdout)
        context = output['hookSpecificOutput']['additionalContext']
        self.assertTrue(context.startswith('Take action immediately, compaction imminent. '), context)
        self.assertEqual(context.count('Refresh this as a current snapshot.'), 1)
        self.assertIn('Keep the next action and active work, current decisions and relevant rejected approaches', context)
        self.assertIn('Link useful history instead of repeating it.', context)
        return context

    def fake_attention_launcher(self):
        home = self.root.parent / 'attention-home'
        launcher = home / '.local' / 'bin' / 'hailing-frequencies'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('''#!/usr/bin/env python3
import json
import os
import sys
import time

args = sys.argv[1:]
body = sys.stdin.buffer.read(4097)
capture = os.environ['HF_FAKE_CAPTURE']
with open(capture, 'a', encoding='utf-8') as handle:
    handle.write(json.dumps({'args': args, 'body': body.decode('utf-8')}) + '\\n')
event_key = args[args.index('--event-key') + 1]
outcome = os.environ.get('HF_FAKE_OUTCOME', 'submitted')
if outcome == 'timeout':
    time.sleep(10)
elif outcome == 'malformed':
    print('not-json')
elif outcome == 'oversized':
    sys.stdout.write('x' * 1000000)
    sys.stdout.flush()
    time.sleep(10)
else:
    attempted = outcome in ('submitted', 'uncertain')
    print(json.dumps({'event_key': event_key, 'outcome': outcome,
                      'attempted': attempted, 'detail': 'fake receipt'}))
''')
        launcher.chmod(0o755)
        return home, home / 'calls.jsonl'

    def attention_env(self, home, capture, session, surface, outcome='submitted'):
        return {'HOME': str(home), 'CMUX_AGENT_LAUNCH_KIND': 'codex',
                'CODEX_THREAD_ID': session, 'CMUX_SURFACE_ID': surface,
                'HF_FAKE_CAPTURE': str(capture), 'HF_FAKE_OUTCOME': outcome}

    def test_private_note_save_read_and_invalid_save_preserves_previous(self):
        registered = self.enroll()
        self.assertIsNone(registered['reviewed_at'])
        self.assertIn('context-injector/checkpoints', registered['checkpoint_path'])
        first = self.save()
        self.assertTrue(first['reviewed_at'])
        self.assertEqual(first['current_revision'], first['saved_revision'])
        path = Path(first['checkpoint_path'])
        before = path.read_bytes()
        self.note.write_text('x' * 8193)
        self.command('checkpoint-save', 'codex', 'pilot', '--file', str(self.note), ok=False)
        self.assertEqual(path.read_bytes(), before)
        self.note.write_text(NOTE.replace('ORBIT-42', 'ORBIT-43'))
        self.save()
        self.assertEqual((path.parent / 'previous.md').read_bytes(), before)
        read = json.loads(self.command('checkpoint-read').stdout)
        self.assertIn('ORBIT-43', read['note'])
        self.assertNotIn('checkpoint', self.git('status', '--porcelain').stdout)

    def test_note_external_edit_is_not_a_review(self):
        self.enroll()
        saved = self.save()
        Path(saved['checkpoint_path']).write_text(NOTE.replace('ORBIT-42', 'UNREVIEWED'))
        report = json.loads(self.command('checkpoint-read').stdout)
        self.assertIsNone(report['note'])
        self.assertIsNone(report['note_bytes'])
        self.assertIsNone(report['note_remaining_bytes'])
        self.assertEqual(report['note_limit_bytes'], 8192)
        self.assertEqual(report['reviewed_at'], saved['reviewed_at'])
        self.assertIn('checkpoint-save', ' '.join(report['warnings']))

    def test_role_and_session_isolation(self):
        self.cli('assign', '--harness', 'codex', '--session', 'worker', '--role', 'implementer')
        self.command('checkpoint-init', 'codex', 'worker', '--compact-at', '95', ok=False)
        self.enroll()
        self.save()
        self.assertNotIn('Checkpoint:', self.event(session='other').stdout)
        self.assertNotIn('Checkpoint:', self.event(agent_id='child').stdout)
        self.assertNotIn('Checkpoint:', self.event(harness='claude').stdout)

    def test_missing_note_recovery_preserves_standing_context_without_transcript(self):
        self.enroll()
        result = self.event(transcript_path='/does/not/exist')
        text = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('Track ownership', text)
        self.assertIn('checkpoint', text.lower())
        self.assertIn('missing', text.lower())
        self.assertNotIn('continue', json.loads(result.stdout))

    def test_counters_use_boundaries_not_hook_calls_and_capture_keeps_review_time(self):
        self.enroll()
        saved = self.save()
        self.event(source='startup')
        self.append({'type': 'compacted', 'message': 'native compacted checkpoint'})
        self.event()
        self.event()
        status = json.loads(self.command('checkpoint-status').stdout)
        self.assertEqual(status['observed_generation'], 1)
        self.assertEqual(status['generation_gap'], 1)
        self.assertTrue(status['pending_compact_boundary'])
        self.event(name='PreCompact')
        after = json.loads(self.command('checkpoint-status').stdout)
        self.assertEqual(after['reviewed_at'], saved['reviewed_at'])
        self.assertEqual(after['observed_generation'], 1)

    def test_codex_real_token_record_shape_warns_once_and_resets_on_compaction(self):
        self.enroll()
        self.save()
        self.event(source='startup')
        self.usage(89)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(91)
        self.assert_pressure_warning(self.event(name='PostToolUse'))
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.append({'type': 'compacted'})
        self.event()
        self.usage(92)
        self.assert_pressure_warning(self.event(name='PostToolUse'))

    def test_pressure_warning_prefix_dedup_and_normal_publication_receipt(self):
        registered = self.enroll()
        self.save()
        self.event(source='startup')
        self.usage(91)
        self.assert_pressure_warning(self.event(name='PostToolUse'))
        self.usage(92)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        Path(registered['draft_path']).write_text(NOTE.replace('ORBIT-42', 'ORBIT-43'))
        receipt = self.event(name='PostToolUse')
        context = json.loads(receipt.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('Checkpoint published', context)
        self.assertFalse(context.startswith('Take action immediately, compaction imminent. '))
        self.assertEqual(self.event(name='PostToolUse').stdout, '')

    def test_attention_episode_keys_change_after_low_usage_rearm_at_uncertain_generation_zero(self):
        session = str(uuid.uuid4()).upper()
        surface = str(uuid.uuid4()).upper()
        self.trace.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session}}) + '\n')
        registered = self.enroll(session=session)
        self.save(session=session)
        self.event(source='compact', session=session)
        home, capture = self.fake_attention_launcher()
        env = self.attention_env(home, capture, session, surface)
        self.usage(91)
        self.assert_pressure_warning(self.event(name='PostToolUse', session=session, env=env))
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        first_args = calls[0]['args']
        first_key = first_args[first_args.index('--event-key') + 1]
        self.assertEqual(calls[0]['body'].encode('utf-8')[:len('Take action immediately, compaction imminent. ')],
                         b'Take action immediately, compaction imminent. ')
        status = json.loads(self.command('checkpoint-status', session=session).stdout)
        self.assertEqual(status['observed_generation'], 0)
        self.assertTrue(status['generation_uncertain'])
        self.assertEqual(status['attention_delivery']['event_key'], first_key)
        self.assertEqual(status['attention_delivery']['outcome'], 'submitted')
        self.assertTrue(status['attention_delivery']['attempted'])
        self.assertEqual(status['attention_delivery']['detail'], 'fake receipt')
        self.usage(92)
        self.assertEqual(self.event(name='PostToolUse', session=session, env=env).stdout, '')
        Path(registered['draft_path']).write_text(NOTE.replace('ORBIT-42', 'ORBIT-43'))
        receipt = self.event(name='PostToolUse', session=session, env=env)
        self.assertIn('Checkpoint published', json.loads(receipt.stdout)['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(capture.read_text().splitlines()), 1)
        self.usage(84)
        self.assertEqual(self.event(name='PostToolUse', session=session, env=env).stdout, '')
        self.usage(91)
        self.assert_pressure_warning(self.event(name='PostToolUse', session=session, env=env))
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        second_key = calls[1]['args'][calls[1]['args'].index('--event-key') + 1]
        self.assertNotEqual(first_key, second_key)

    def test_attention_holds_and_invalid_receipts_keep_the_hook_warning(self):
        home, capture = self.fake_attention_launcher()
        for outcome, expected, attempted in [('held', 'held', False),
                                              ('timeout', 'uncertain', True),
                                              ('malformed', 'uncertain', True),
                                              ('oversized', 'uncertain', True)]:
            with self.subTest(outcome=outcome):
                session = str(uuid.uuid4()).upper()
                surface = str(uuid.uuid4()).upper()
                self.trace.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session}}) + '\n')
                self.enroll(session=session)
                self.save(session=session)
                self.event(source='startup', session=session)
                self.usage(91)
                env = self.attention_env(home, capture, session, surface, outcome)
                result = self.event(name='PostToolUse', session=session, env=env)
                self.assert_pressure_warning(result)
                status = json.loads(self.command('checkpoint-status', session=session).stdout)
                delivery = status['attention_delivery']
                self.assertEqual(delivery['outcome'], expected)
                self.assertEqual(delivery['attempted'], attempted)
                if outcome == 'held':
                    self.assertEqual(delivery['detail'], 'fake receipt')
                elif outcome == 'timeout':
                    self.assertIn('timed out', delivery['detail'])
                elif outcome == 'malformed':
                    self.assertIn('no valid receipt', delivery['detail'])
                else:
                    self.assertIn('oversized output', delivery['detail'])
        self.assertEqual(len(capture.read_text().splitlines()), 4)

    def test_attention_accepts_claudeteams_without_optional_session_environment(self):
        session = str(uuid.uuid4()).upper()
        surface = str(uuid.uuid4()).upper()
        self.enroll(harness='claude', session=session)
        self.save(harness='claude', session=session)
        self.event(source='startup', harness='claude', session=session)
        self.cli('statusline', data={'session_id': session, 'cwd': str(self.root),
                                     'context_window': {'used_percentage': 56}})
        home, capture = self.fake_attention_launcher()
        env = {'HOME': str(home), 'CMUX_AGENT_LAUNCH_KIND': 'claudeTeams', 'CLAUDE_CODE_SESSION_ID': '',
               'CMUX_SURFACE_ID': surface, 'HF_FAKE_CAPTURE': str(capture),
               'HF_FAKE_OUTCOME': 'submitted', 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': ''}
        warning = self.event(name='PostToolUse', harness='claude', session=session, env=env)
        self.assert_pressure_warning(warning)
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        args = calls[0]['args']
        self.assertEqual(args[args.index('--session') + 1], session)
        status = json.loads(self.command('checkpoint-status', 'claude', session).stdout)
        self.assertEqual(status['attention_delivery']['outcome'], 'submitted')
        self.assertTrue(status['attention_delivery']['attempted'])
        self.assertEqual(status['attention_delivery']['detail'], 'fake receipt')

    def test_attention_rejects_cross_harness_launch_kind(self):
        session = str(uuid.uuid4()).upper()
        surface = str(uuid.uuid4()).upper()
        self.enroll(harness='claude', session=session)
        self.save(harness='claude', session=session)
        self.event(source='startup', harness='claude', session=session)
        self.cli('statusline', data={'session_id': session, 'cwd': str(self.root),
                                     'context_window': {'used_percentage': 56}})
        home, capture = self.fake_attention_launcher()
        env = {'HOME': str(home), 'CMUX_AGENT_LAUNCH_KIND': 'codex',
               'CMUX_SURFACE_ID': surface, 'HF_FAKE_CAPTURE': str(capture),
               'HF_FAKE_OUTCOME': 'submitted', 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': ''}
        self.assert_pressure_warning(self.event(name='PostToolUse', harness='claude', session=session, env=env))
        self.assertFalse(capture.exists())
        status = json.loads(self.command('checkpoint-status', 'claude', session).stdout)
        self.assertEqual(status['attention_delivery']['outcome'], 'unavailable')
        self.assertFalse(status['attention_delivery']['attempted'])
        self.assertEqual(status['attention_delivery']['detail'],
                         'Caller launch identity does not match this hook session.')

    def test_attention_requires_matching_native_caller_identity(self):
        session = str(uuid.uuid4()).upper()
        surface = str(uuid.uuid4()).upper()
        self.trace.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session}}) + '\n')
        self.enroll(session=session)
        self.save(session=session)
        self.event(source='startup', session=session)
        home, capture = self.fake_attention_launcher()
        self.usage(91)
        env = self.attention_env(home, capture, str(uuid.uuid4()).upper(), surface)
        self.assert_pressure_warning(self.event(name='PostToolUse', session=session, env=env))
        self.assertFalse(capture.exists())
        status = json.loads(self.command('checkpoint-status', session=session).stdout)
        self.assertEqual(status['attention_delivery']['outcome'], 'unavailable')
        self.assertFalse(status['attention_delivery']['attempted'])
        self.assertEqual(status['attention_delivery']['detail'],
                         'Caller session identity does not match this hook session.')

    def test_reminder_rearms_after_verified_low_usage_with_hysteresis(self):
        self.enroll()
        self.save()
        self.event(source='startup')
        self.usage(91)
        self.assert_pressure_warning(self.event(name='PostToolUse'))
        self.usage(94)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(89)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(91)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(85)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(89)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(91)
        self.assert_pressure_warning(self.event(name='PostToolUse'))
        self.usage(85)
        self.event(name='PostToolUse')
        state = Path(json.loads(self.command('checkpoint-status').stdout)['checkpoint_path']).parent / 'state.json'
        self.assertIsNone(json.loads(state.read_text())['reminded_generation'])

    def test_large_compaction_record_does_not_disable_pressure_reminders(self):
        self.enroll()
        self.save()
        self.event(source='startup')
        self.usage(92)
        self.assert_pressure_warning(self.event(name='PostToolUse'))
        self.usage(92)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.append({'type': 'compacted', 'message': 'x' * (checkpoints.TRACE_LIMIT * 5)})
        self.event()
        report = json.loads(self.command('checkpoint-status').stdout)
        self.assertEqual(report['observed_generation'], 0)
        self.assertTrue(report['generation_uncertain'])
        self.usage(92)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(84)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(92)
        self.assert_pressure_warning(self.event(name='PostToolUse'))

    def test_unknown_or_foreign_usage_never_invents_a_warning(self):
        self.enroll()
        self.save()
        self.event(source='startup')
        self.usage(99)
        self.assert_pressure_warning(self.event(name='PostToolUse'))
        self.usage(10, timestamp='2000-01-01T00:00:00+00:00')
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(99)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.append({'type': 'event_msg', 'payload': 'malformed usage shape'})
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        with self.trace.open('a') as handle:
            handle.write('not-json\n')
        self.append({'type': 'event_msg', 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     'payload': {'type': 'token_count', 'info': {
                         'last_token_usage': {'total_tokens': '1000'}, 'model_context_window': 10000}}})
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.usage(99)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.trace.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'foreign'}}) + '\n')
        self.usage(10)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.trace.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'pilot'}}) + '\n')
        self.usage(99)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')

    def test_large_tool_response_still_allows_checkpoint_pressure_reminder(self):
        self.enroll()
        self.save()
        self.usage(99)
        result = self.event(name='PostToolUse', tool_response={'stdout': 'x' * 40000})
        self.assert_pressure_warning(result)
        result = self.event(name='PostToolUse', tool_response={'stdout': 'x' * (1024 * 1024)})
        self.assertEqual(result.stdout, '')
        self.assertIn('hook input too large', result.stderr)
        self.assertIn('Track ownership', self.event().stdout)

    def test_claude_statusline_preserves_display_and_follows_changed_limit(self):
        self.enroll('claude')
        self.save('claude')
        settings = self.root.parent / 'claude.json'
        prior = {'type': 'command', 'command': "printf 'EXISTING-DISPLAY'", 'padding': 3}
        settings.write_text(json.dumps({'statusLine': prior, 'env': {'KEPT': 'yes'}}))
        runtime = self.root.parent / 'runtime'
        self.cli('install', '--harness', 'claude', '--settings', str(settings), '--runtime-dir', str(runtime), '--checkpoints')
        installed = json.loads(settings.read_text())
        command = installed['statusLine']['command']
        data = {'session_id': 'pilot', 'cwd': str(self.root), 'model': {'id': 'fixture'},
                'context_window': {'used_percentage': 56, 'context_window_size': 200000}}
        env = {**os.environ, 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '60'}
        r = subprocess.run(command, shell=True, input=json.dumps(data), text=True, capture_output=True, env=env)
        self.assertEqual(r.stdout, 'EXISTING-DISPLAY')
        warning = self.event(name='PostToolUse', harness='claude')
        self.assert_pressure_warning(warning)
        self.assertIn('55.0%', warning.stdout)
        data['context_window']['used_percentage'] = 60
        subprocess.run(command, shell=True, input=json.dumps(data), text=True, capture_output=True, env=env, check=True)
        self.assertEqual(self.event(name='PostToolUse', harness='claude').stdout, '')
        data['context_window']['used_percentage'] = 50
        subprocess.run(command, shell=True, input=json.dumps(data), text=True, capture_output=True, env=env, check=True)
        self.assertEqual(self.event(name='PostToolUse', harness='claude').stdout, '')
        data['context_window']['used_percentage'] = 56
        subprocess.run(command, shell=True, input=json.dumps(data), text=True, capture_output=True, env=env, check=True)
        self.assert_pressure_warning(self.event(name='PostToolUse', harness='claude'))
        self.cli('compaction-settings', '--compact-at', '70', '--settings', str(settings))
        self.assertEqual(json.loads(settings.read_text())['env']['KEPT'], 'yes')
        self.assertEqual(json.loads(settings.read_text())['env']['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'], '70.0')
        self.cli('install', '--harness', 'claude', '--settings', str(settings), '--runtime-dir', str(runtime), '--checkpoints')
        self.assertEqual(json.loads(settings.read_text())['statusLine']['command'], command)
        self.cli('uninstall', '--harness', 'claude', '--settings', str(settings))
        self.assertEqual(json.loads(settings.read_text())['statusLine'], prior)

    def test_reminders_can_be_disabled_without_losing_recovery(self):
        self.enroll()
        self.save()
        self.command('checkpoint-config', 'codex', 'pilot', '--reminders', 'off')
        self.usage(99)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.assertIn('Checkpoint:', self.event().stdout)
        self.command('checkpoint-config', 'codex', 'pilot', '--enabled', 'off')
        self.assertNotIn('Checkpoint:', self.event().stdout)

    def test_standard_installer_preserves_other_hooks_and_explicit_opt_out_omits_pressure(self):
        settings = self.root.parent / 'hooks.json'
        other = {'type': 'command', 'command': 'echo unrelated'}
        settings.write_text(json.dumps({'hooks': {'PostToolUse': [{'hooks': [other]}]}}))
        self.cli('install', '--harness', 'codex', '--settings', str(settings), '--runtime-dir', str(self.root.parent / 'rt'))
        cfg = json.loads(settings.read_text())
        self.assertEqual(cfg['hooks']['PostToolUse'][0]['hooks'], [other])
        self.assertIn('PreCompact', cfg['hooks'])
        self.cli('install', '--harness', 'codex', '--settings', str(settings), '--runtime-dir', str(self.root.parent / 'rt'), '--no-checkpoints')
        cfg = json.loads(settings.read_text())
        self.assertNotIn('PreCompact', cfg['hooks'])
        self.assertEqual(cfg['hooks']['PostToolUse'], [{'hooks': [other]}])

    def test_truncated_trace_sets_uncertainty_instead_of_resetting_count(self):
        self.enroll()
        self.save()
        self.event(source='startup')
        self.append({'type': 'compacted'})
        self.event()
        self.trace.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'pilot'}}) + '\n')
        self.event()
        report = json.loads(self.command('checkpoint-status').stdout)
        self.assertEqual(report['observed_generation'], 1)
        self.assertTrue(report['generation_uncertain'])

    def test_external_existing_note_requires_private_path_and_exclusive_owner(self):
        # External notes must be durable, so this fixture deliberately lives outside OS temp.
        with tempfile.TemporaryDirectory(dir=Path.home(), prefix='.checkpoint-fixture-') as folder:
            note = Path(folder) / 'existing.md'
            note.write_text(NOTE)
            result = self.command('checkpoint-init', 'codex', 'pilot', '--compact-at', '95', '--path', str(note))
            self.assertIsNone(json.loads(result.stdout)['reviewed_at'])
            self.assertEqual(note.read_text(), NOTE)
            self.command('checkpoint-init', 'codex', 'other', '--compact-at', '95', '--path', str(note), ok=False)
            self.command('checkpoint-save', 'codex', 'pilot', '--file', str(note))
            self.assertEqual(checkpoints._note_body(json.loads(self.command('checkpoint-read').stdout)['note']), NOTE)
        self.command('checkpoint-init', 'codex', 'temporary', '--compact-at', '95', '--path', str(self.root.parent / 'unsafe.md'), ok=False)

    def test_corrupt_checkpoint_metadata_is_advisory_and_preserves_standing_context(self):
        registered = self.enroll()
        state = Path(registered['checkpoint_path']).parent / 'state.json'
        state.write_text('{}')
        text = self.event().stdout
        self.assertIn('Track ownership', text)
        self.assertIn('Checkpoint unavailable', text)
        self.assertNotIn('continue', json.loads(text))

    def test_claude_changed_runtime_limit_changes_reminder_without_rewriting_note(self):
        self.enroll('claude')
        saved = self.save('claude')
        # This is the same installed status-line entrypoint that receives real usage JSON.
        payload = {'session_id': 'pilot', 'cwd': str(self.root), 'model': {'id': 'fixture'},
                   'context_window': {'used_percentage': 56, 'context_window_size': 200000}}
        self.cli('statusline', data=payload, env={'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '70'})
        self.assertEqual(self.event(name='PostToolUse', harness='claude').stdout, '')
        payload['context_window']['used_percentage'] = 66
        self.cli('statusline', data=payload, env={'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '70'})
        self.assertIn('65.0%', self.event(name='PostToolUse', harness='claude').stdout)
        status = json.loads(self.command('checkpoint-status', 'claude').stdout)
        self.assertEqual(status['effective_compact_at'], 70)
        self.assertEqual(status['reviewed_at'], saved['reviewed_at'])

    def test_settings_refuse_invalid_percentage_without_writing(self):
        settings = self.root.parent / 'settings.json'
        settings.write_text('{"env":{"KEEP":"yes"}}')
        before = settings.read_bytes()
        self.cli('compaction-settings', '--settings', str(settings), '--compact-at', 'nan', ok=False)
        self.assertEqual(settings.read_bytes(), before)

    def test_checkpoint_read_uses_the_installed_package(self):
        self.enroll()
        self.save()
        settings = self.root.parent / 'hooks.json'
        runtime = self.root.parent / 'installed'
        self.cli('install', '--harness', 'codex', '--settings', str(settings), '--runtime-dir', str(runtime), '--checkpoints')
        read = self.command('checkpoint-read', script=runtime / 'context_injector_codex.py')
        self.assertIn('ORBIT-42', json.loads(read.stdout)['note'])

    def test_normal_draft_write_publishes_without_agent_writing_git_metadata(self):
        registered = self.enroll()
        self.save()
        draft = Path(registered['draft_path'])
        self.assertTrue(draft.is_relative_to(self.root.resolve() / '.context-injector/checkpoints'))
        self.assertEqual(self.git('check-ignore', str(draft)).returncode, 0)
        before = json.loads(self.command('checkpoint-status').stdout)['reviewed_at']
        draft.write_text(NOTE.replace('ORBIT-42', 'AGENT-WROTE-43'))
        result = self.event(name='PostToolUse')
        self.assertIn('Checkpoint published', result.stdout)
        read = json.loads(self.command('checkpoint-read').stdout)
        self.assertIn('AGENT-WROTE-43', read['note'])
        self.assertNotEqual(read['reviewed_at'], before)
        self.assertTrue(read['published_at'])
        self.assertEqual(self.event(name='PostToolUse').stdout, '')

    def test_utf8_headroom_exact_limit_and_oversized_draft_preserve_note(self):
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness):
                registered = self.enroll(harness)
                short = NOTE + 'Caf\u00e9 \U0001f52d\n'
                used = len(short.encode('utf-8'))
                self.assertGreater(used, len(short))
                header_bytes = 8192 - registered['draft_body_limit_bytes']
                used += header_bytes
                full = short + 'x' * (8192 - used)
                self.note.write_text(full)
                saved = self.save(harness)
                self.assertEqual(saved['note_bytes'], 8192)
                self.assertEqual(saved['note_remaining_bytes'], 0)
                self.assertEqual(saved['note_limit_bytes'], 8192)
                draft = Path(registered['draft_path'])
                draft.write_text(full + '\u00e9')
                self.assertIn('not published', self.event(name='PostToolUse', harness=harness).stdout)
                read = json.loads(self.command('checkpoint-read', harness).stdout)
                self.assertEqual(checkpoints._note_body(read['note']), full)
                self.assertEqual(read['note_bytes'], 8192)
                self.assertEqual(read['note_remaining_bytes'], 0)
                self.assertEqual(read['reviewed_at'], saved['reviewed_at'])
                draft.write_text(short)
                receipt = self.event(name='PostToolUse', harness=harness).stdout
                self.assertIn(f'{used}/8192 UTF-8 bytes', receipt)
                self.assertIn(f'{8192 - used} bytes remaining', receipt)
                status = json.loads(self.command('checkpoint-status', harness).stdout)
                self.assertEqual(status['note_bytes'], used)
                self.assertEqual(status['note_remaining_bytes'], 8192 - used)
                self.assertEqual(self.event(name='PostToolUse', harness=harness).stdout, '')

    def test_publication_header_is_owned_and_copying_back_does_not_duplicate_it(self):
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness):
                registered = self.enroll(harness)
                saved = self.save(harness)
                note = Path(saved['checkpoint_path']).read_text()
                self.assertTrue(note.startswith(checkpoints.HEADER))
                self.assertIn('harness: ' + json.dumps(harness), note)
                self.assertIn('session: "pilot"', note)
                self.assertIn('published_at: ' + json.dumps(saved['published_at']), note)
                self.assertEqual(checkpoints._note_body(note), NOTE)
                self.assertEqual(Path(registered['draft_path']).read_text(), NOTE)
                self.assertFalse(saved['draft_pending'])
                self.note.write_text(note)
                again = self.save(harness)
                published = Path(again['checkpoint_path']).read_text()
                self.assertEqual(published.count(checkpoints.HEADER), 1)
                self.assertEqual(checkpoints._note_body(published), NOTE)
                self.assertNotEqual(again['published_at'], saved['published_at'])
                self.assertFalse(again['draft_pending'])
                self.assertEqual(self.event(name='PostToolUse', harness=harness).stdout, '')

    def test_headerless_note_survives_upgrade_until_next_actual_publication(self):
        registered = self.enroll()
        self.save()
        path = Path(registered['checkpoint_path'])
        state_path = path.parent / 'state.json'
        state = json.loads(state_path.read_text())
        path.write_text(NOTE)
        state['note_sha256'] = checkpoints._digest(NOTE.encode())
        state.pop('published_draft_sha256')
        state_path.write_text(json.dumps(state))
        report = json.loads(self.command('checkpoint-read').stdout)
        self.assertEqual(report['note'], NOTE)
        self.assertFalse(report['draft_pending'])
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.assertEqual(path.read_text(), NOTE)
        draft = Path(registered['draft_path'])
        draft.write_text(NOTE.replace('ORBIT-42', 'MIGRATED-43'))
        self.assertIn('Checkpoint published', self.event(name='PostToolUse').stdout)
        self.assertTrue(path.read_text().startswith(checkpoints.HEADER))
        self.assertIn('MIGRATED-43', path.read_text())
        self.assertEqual((path.parent / 'previous.md').read_text(), NOTE)

    def test_header_budget_failure_does_not_replace_valid_note_or_explicit_draft(self):
        registered = self.enroll()
        saved = self.save()
        path, draft = Path(saved['checkpoint_path']), Path(registered['draft_path'])
        old_note, old_draft = path.read_bytes(), draft.read_bytes()
        self.note.write_text(NOTE + 'x' * (8192 - len(NOTE.encode())))
        self.command('checkpoint-save', 'codex', 'pilot', '--file', str(self.note), ok=False)
        self.assertEqual(path.read_bytes(), old_note)
        self.assertEqual(draft.read_bytes(), old_draft)

    def test_uncertain_recovery_does_not_present_zero_as_confirmed_count(self):
        self.enroll()
        self.save()
        output = self.event().stdout
        self.assertIn('Compaction count not yet confirmed', output)
        self.assertNotIn('Observed compaction generation since enrollment: 0', output)
        self.assertNotIn('Compaction boundaries since last saved checkpoint: 0', output)

    def test_invalid_draft_keeps_valid_note_and_warns_only_once_per_edit(self):
        registered = self.enroll()
        saved = self.save()
        draft = Path(registered['draft_path'])
        draft.write_text('Incomplete checkpoint')
        self.assertIn('not published', self.event(name='PostToolUse').stdout)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        read = json.loads(self.command('checkpoint-read').stdout)
        self.assertEqual(checkpoints._note_body(read['note']), NOTE)
        self.assertEqual(read['reviewed_at'], saved['reviewed_at'])
        draft.write_text(NOTE.replace('ORBIT-42', 'FIXED-44'))
        self.assertIn('Checkpoint published', self.event(name='PostToolUse').stdout)

    def test_explicit_save_syncs_draft_and_cannot_be_reverted_by_later_hook(self):
        registered = self.enroll()
        self.save()
        self.note.write_text(NOTE.replace('ORBIT-42', 'EXPLICIT-45'))
        self.save()
        self.assertIn('EXPLICIT-45', Path(registered['draft_path']).read_text())
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        self.assertIn('EXPLICIT-45', json.loads(self.command('checkpoint-read').stdout)['note'])

    def test_canonical_path_tamper_cannot_redirect_publication(self):
        registered = self.enroll()
        self.save()
        path = Path(registered['checkpoint_path']).parent / 'state.json'
        data = json.loads(path.read_text())
        foreign = self.root.parent / 'foreign.md'
        foreign.write_text('Must be preserved')
        data['checkpoint_path'] = str(foreign)
        data['previous_path'] = str(foreign.parent / 'previous.md')
        path.write_text(json.dumps(data))
        Path(registered['draft_path']).write_text(NOTE.replace('ORBIT-42', 'WRONG'))
        self.event(name='PostToolUse')
        self.assertEqual(foreign.read_text(), 'Must be preserved')

    def test_unpublished_or_missing_draft_is_visible_without_losing_reviewed_note(self):
        registered = self.enroll()
        saved = self.save()
        draft = Path(registered['draft_path'])
        draft.write_text(NOTE.replace('ORBIT-42', 'NOT-PUBLISHED-YET'))
        report = json.loads(self.command('checkpoint-read').stdout)
        self.assertTrue(report['draft_pending'])
        self.assertEqual(checkpoints._note_body(report['note']), NOTE)
        self.assertIn('unpublished', self.event().stdout)
        draft.unlink()
        report = json.loads(self.command('checkpoint-read').stdout)
        self.assertEqual(checkpoints._note_body(report['note']), NOTE)
        self.assertEqual(report['reviewed_at'], saved['reviewed_at'])
        self.assertIn('missing', self.event().stdout)

    def test_boundary_persisted_after_recovery_is_pending_then_reconciled(self):
        self.enroll()
        self.save()
        self.event(source='startup')
        self.assertIn('not yet observed', self.event().stdout)
        report = json.loads(self.command('checkpoint-status').stdout)
        self.assertEqual(report['observed_generation'], 0)
        self.assertTrue(report['pending_compact_boundary'])
        self.assertTrue(report['generation_uncertain'])
        self.append({'type': 'compacted'})
        result = self.event(name='PostToolUse')
        self.assertIn('generation is now 1', result.stdout)
        self.assertEqual(self.event(name='PostToolUse').stdout, '')
        report = json.loads(self.command('checkpoint-status').stdout)
        self.assertEqual(report['observed_generation'], 1)
        self.assertFalse(report['pending_compact_boundary'])


if __name__ == '__main__':
    unittest.main()
