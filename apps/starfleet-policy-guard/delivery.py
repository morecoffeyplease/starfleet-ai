"""Offline delivery evidence report. No verification execution or readiness authority."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

import github_records as github

POLICY = Path(__file__).resolve().parent / 'config/delivery-checklist.json'
CHECKS = ('design-fidelity', 'main-awareness', 'merge-conflicts', 'local-verification')
SKILLS = ('design-fidelity-audit', 'fidelity-audit')
ERRORS = (ValueError, OSError, TypeError, AttributeError, KeyError)
PATTERN_BUFFER_TIMEOUT_SECONDS = 5
PATTERN_BUFFER_REPORT_LIMIT_BYTES = 256 * 1024
GUIDANCE_ATTESTATION_STATEMENT = '\n'.join((
    'I have chosen the simplest viable approach that meets the approved outcome and constraints, and kept the solution proportionate to the problem it solves.',
    'I have made no claims based on assumptions, and I checked the required reads, investigation, evidence, verification, approved scope, and authority for this work.',
    'Any required fact that was initially unknown was investigated or raised to the configured human on Discord, then resolved before I attested.',
    'Disclosing uncertainty does not waive a missing prerequisite.',
))
SUCCESSOR_VALIDATION_STATEMENT = '\n'.join((
    'The current native owner has reread applicable guidance and reconciled inherited work, existing repair history, current approved scope and human holds, and current candidate evidence.',
    'For this current review, the current native owner confirms the inherited approach remains the simplest viable route for the approved outcome and constraints, and remains proportionate.',
    'No claim in this current review depends on an assumption; current required reads, investigation, evidence, verification, approved scope, and authority were checked.',
    'Any required fact that was initially unknown for this review was investigated or raised to the configured human on Discord, then resolved before this validation.',
    'This review records current responsibility for covered delivery. It does not claim authorship of prior work or accept a new repair batch.',
    'Disclosing uncertainty does not waive a missing prerequisite.',
))


class Gaps:
    """Collect independent observations without a first failure hiding the others."""

    def __init__(self):
        self.items = []

    def check(self, field, operation):
        try:
            return operation()
        except ERRORS as error:
            self.items.append({'field': field, 'message': str(error)})
            return None

    def require(self, field, condition, message):
        if not condition:
            self.items.append({'field': field, 'message': message})

    def result(self, success, **extra):
        return {'status': 'missing' if self.items else 'satisfied',
                'detail': '; '.join(x['message'] for x in self.items) if self.items else success,
                'gaps': self.items, **extra}


def commit(cwd, revision):
    if not isinstance(revision, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', revision):
        raise ValueError('Evidence revision must be an immutable full commit SHA, not a moving ref')
    return value(cwd, 'rev-parse', '--verify', revision + '^{commit}')


def git(cwd, *args, env=None):
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    clean_env.update({'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1'})
    clean_env.update(env or {})
    return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(cwd), *args], env=clean_env,
                          capture_output=True, text=True)


def value(cwd, *args):
    result = git(cwd, *args)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Git observation failed')
    return result.stdout.strip()


def snapshot(cwd, base_ref):
    # Refuse option-like ref names; Git resolves the ref once to immutable objects.
    if not base_ref or base_ref.startswith('-'):
        raise ValueError('Use a named local target ref, such as origin/main')
    root = value(cwd, 'rev-parse', '--show-toplevel')
    head = value(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    base = value(root, 'rev-parse', '--verify', base_ref + '^{commit}')
    return {'root': root, 'head': head, 'tree': value(root, 'rev-parse', head + '^{tree}'),
            'base_ref': base_ref, 'base_head': base,
            'clean': not bool(value(root, 'status', '--porcelain', '--untracked-files=all',
                                    '--ignore-submodules=none')),
            'observed_at': dt.datetime.now(dt.timezone.utc).isoformat()}


def text(record, key):
    if not isinstance(record.get(key), str) or not record[key].strip():
        raise ValueError('Missing nonempty ' + key)
    return record[key]


def artifact(record, directory):
    if not isinstance(record, dict):
        raise ValueError('Evidence reference must contain path and sha256')
    path = Path(text(record, 'path'))
    if not path.is_absolute():
        path = directory / path
    if not path.is_file():
        raise ValueError('Evidence file missing: ' + str(path))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != text(record, 'sha256'):
        raise ValueError('Evidence hash changed: ' + str(path))
    return path


def required_reads_status(worktree, harness, session):
    executable = shutil.which('pattern-buffer')
    if executable is None:
        return None
    root = Path(worktree).resolve()
    argv = [str(Path(executable).resolve()), '--cwd', str(root), 'required-reads', 'status',
            '--harness', harness, '--session', session, '--json']
    recovery = shlex.join(['pattern-buffer', '--cwd', str(root), 'required-reads', 'status',
                           '--harness', harness, '--session', session, '--json'])

    def refuse(reason):
        raise ValueError('Pattern Buffer required-read receipt check could not be completed: ' + reason +
                         '. Run ' + recovery + ' and inspect the Pattern Buffer guide.')

    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                                timeout=PATTERN_BUFFER_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        refuse(f'the status command timed out after {PATTERN_BUFFER_TIMEOUT_SECONDS} seconds')
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        refuse('the status command failed: ' + str(error))

    if len(result.stdout.encode('utf-8')) > PATTERN_BUFFER_REPORT_LIMIT_BYTES:
        refuse('the status report exceeded the size limit')
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip())[:512]
        refuse(f'the status command exited with code {result.returncode}' +
               (': ' + detail if detail else ''))
    try:
        report = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        refuse('the status command returned malformed JSON: ' + str(error))
    if not isinstance(report, dict):
        refuse('the status command returned an unsupported report shape')
    if report.get('registered') is False:
        if report != {'registered': False}:
            refuse('the no-list report contained unsupported fields')
        return None
    if report.get('registered') is not True:
        refuse('the status report did not identify whether a list is registered')

    integrity_errors = report.get('integrity_errors')
    if not isinstance(integrity_errors, list) or any(not isinstance(item, str) for item in integrity_errors):
        refuse('the status report contained malformed integrity details')
    entries = report.get('entries')
    if report.get('ok') is not True or report.get('list_digest_matches') is not True or integrity_errors:
        details = integrity_errors or ['Pattern Buffer reported that the registered list or a required file failed integrity checks']
        raise ValueError('Pattern Buffer integrity error: ' + '; '.join(details) + '. Run ' + recovery + '.')
    if report.get('harness') != harness or report.get('session_id') != session:
        refuse('the status report belongs to a different native session')
    role = report.get('role')
    if role not in ('orchestrator', 'implementer'):
        refuse('the status report did not identify a supported session role')
    list_sha256 = report.get('list_sha256')
    generation = report.get('generation')
    if (not isinstance(list_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', list_sha256) or
            type(generation) is not int or generation < 0 or not isinstance(entries, list)):
        refuse('the status report contained malformed list, generation, or entry data')

    normalized_entries = []
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            refuse('the status report contained a non-object entry')
        roles = entry.get('roles')
        if not isinstance(roles, list) or any(not isinstance(value, str) for value in roles):
            refuse('the status report contained malformed entry roles')
        if role not in roles:
            if entry.get('state') != 'not_applicable':
                refuse('the status report marked a non-applicable entry as applicable')
            continue
        name = entry.get('name')
        raw_path = entry.get('path')
        digest = entry.get('sha256')
        byte_count = entry.get('byte_count')
        state = entry.get('state')
        parts = entry.get('parts')
        if (not isinstance(name, str) or not name or name in names or
                not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or
                not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
                type(byte_count) is not int or byte_count < 0 or
                state not in ('current', 'pending', 'stale', 'integrity_error') or
                not isinstance(parts, list)):
            refuse('the status report contained malformed applicable entry data')
        names.add(name)
        if state == 'integrity_error':
            detail = entry.get('detail')
            raise ValueError('Pattern Buffer integrity error for ' + name + ': ' +
                             (detail if isinstance(detail, str) and detail else 'required file integrity failed') +
                             '. Run ' + recovery + '.')
        normalized_parts = []
        part_numbers = set()
        for part in parts:
            if (not isinstance(part, dict) or type(part.get('part')) is not int or part['part'] < 1 or
                    part.get('state') not in ('current', 'pending')):
                refuse('the status report contained malformed receipt parts for ' + name)
            if part['part'] in part_numbers:
                refuse('the status report repeated a receipt part for ' + name)
            part_numbers.add(part['part'])
            normalized_parts.append({'part': part['part'], 'state': part['state']})
        if not normalized_parts or part_numbers != set(range(1, len(part_numbers) + 1)):
            refuse('the status report contained incomplete receipt parts for ' + name)
        try:
            resolved_path = Path(raw_path).resolve(strict=True)
        except OSError as error:
            raise ValueError('Pattern Buffer reported a required file that is no longer available: ' +
                             name + ': ' + str(error) + '. Run ' + recovery + '.') from error
        normalized_entries.append({
            'name': name,
            'path': str(resolved_path),
            'roles': roles,
            'sha256': digest,
            'byte_count': byte_count,
            'state': state,
            'parts': normalized_parts,
        })
    return {'root': str(root), 'list_sha256': list_sha256, 'generation': generation, 'harness': harness,
            'session_id': session, 'role': role, 'entries': normalized_entries}


def required_read_packet_entries(report):
    if report is None:
        return []
    return [{'kind': 'required_read', 'name': entry['name'], 'path': entry['path'],
             'sha256': entry['sha256']} for entry in report['entries']]


def require_required_read_receipts(report, packet, directory):
    if report is None:
        return None
    attestation = packet.get('guidance_attestation') if isinstance(packet, dict) else None
    if (not isinstance(attestation, dict) or not isinstance(attestation.get('reread'), list)) and not report['entries']:
        raise ValueError('Pattern Buffer registered list requires packet.guidance_attestation.reread; '
                         'prepare the owner attestation and inspect lane.py attestation-rereads')
    sources = attestation.get('reread') if isinstance(attestation, dict) else None
    sources = sources if isinstance(sources, list) else []
    failures = []
    read_commands = []
    for entry in report['entries']:
        matching_names = [source for source in sources
                          if isinstance(source, dict) and source.get('name') == entry['name']]
        matching_entry = None
        for source in matching_names:
            try:
                source_path = Path(source.get('path', ''))
                if not source_path.is_absolute():
                    source_path = Path(directory) / source_path
                resolved = source_path.resolve(strict=True)
            except (OSError, TypeError, ValueError):
                continue
            if str(resolved) == entry['path'] and source.get('sha256') == entry['sha256']:
                matching_entry = source
                break
        current = (entry['state'] == 'current' and
                   all(part['state'] == 'current' for part in entry['parts']))
        reasons = []
        if not current:
            reasons.append('no current receipt for generation ' + str(report['generation']))
        if matching_entry is None:
            if not matching_names:
                reasons.append('missing from the packet reread list or its path and SHA-256 do not match')
            else:
                reasons.append('packet path and SHA-256 do not match the required file')
        if reasons:
            failures.append('- ' + entry['name'] + ': ' + '; '.join(reasons))
            pending_parts = [part['part'] for part in entry['parts'] if part['state'] != 'current']
            if not pending_parts:
                pending_parts = [None]
            for part_number in pending_parts:
                argv = ['pattern-buffer', '--cwd', report['root'], 'read-required',
                        '--harness', report['harness'], '--session', report['session_id'],
                        '--name', entry['name']]
                if part_number is not None and (len(entry['parts']) > 1 or part_number > 1):
                    argv.extend(('--part', str(part_number)))
                read_commands.append('  Read command: ' + shlex.join(argv))
    if failures:
        raise ValueError('Pattern Buffer required-read receipts are missing, stale, or do not match packet.reread:\n' +
                         '\n'.join(failures + read_commands) +
                         '\nRead each listed command in the owner session, update packet.reread with the matching '
                         'resolved paths and SHA-256 values, and rerun the attestation command.')
    context_entries = [{'name': entry['name'], 'path': entry['path'], 'sha256': entry['sha256']}
                       for entry in report['entries']]
    return {'list_sha256': report['list_sha256'], 'generation': report['generation'],
            'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'entries': context_entries}


def guidance_attestation(packet, directory, pr_url, stage=None):
    """Validate the one human-authored stage attestation in an evidence packet."""
    record = packet.get('guidance_attestation')
    if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1:
        raise ValueError('Evidence packet needs a version 1 guidance_attestation object')
    if record.get('stage') not in ('opening', 'repair_batch', 'successor_validation') or (stage and record['stage'] != stage):
        raise ValueError('Guidance attestation must identify the requested opening, repair_batch, or successor_validation stage')
    issue_ref = text(record, 'issue_ref')
    issue = github.reference(issue_ref, 'issues')
    pull = github.reference(pr_url, 'pull')
    if issue['repo'].casefold() != pull['repo'].casefold():
        raise ValueError('Guidance issue and bound pull request must identify the same repository')
    if record['stage'] == 'repair_batch':
        text(record, 'accepted_batch_ref')
        text(record, 'repair_history_ref')
    elif record['stage'] == 'successor_validation':
        if 'accepted_batch_ref' in record:
            raise ValueError('A successor validation cannot accept or create a repair batch')
        text(record, 'repair_history_ref')
        prior = record.get('prior_publication_ref')
        if not isinstance(prior, dict) or type(prior.get('version')) is not int or prior['version'] != 1:
            raise ValueError('Successor validation must link the exact inherited publication reference')
        for key in ('pr_url', 'branch', 'head_branch', 'base_ref', 'comment_url', 'stage'):
            text(prior, key)
        owner = prior.get('owner')
        if (not isinstance(owner, dict) or owner.get('harness') not in ('claude', 'codex') or
                not isinstance(owner.get('session_id'), str) or not owner['session_id'].strip()):
            raise ValueError('Inherited publication reference has no valid native owner')
        if type(prior.get('comment_id')) is not int or prior['comment_id'] < 1:
            raise ValueError('Inherited publication reference has no valid comment ID')
        for key in ('body_sha256', 'attestation_sha256'):
            if not isinstance(prior.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', prior[key]):
                raise ValueError('Inherited publication reference has an invalid ' + key)
        if prior['stage'] not in ('opening', 'repair_batch', 'successor_validation'):
            raise ValueError('Inherited publication reference has an unsupported stage')
        try:
            github.comment_reference(prior['comment_url'], pr_url)
        except github.RecordError as error:
            raise ValueError('Inherited publication comment must belong to this PR: ' + str(error)) from error
        reconciliation = record.get('reconciliation')
        if not isinstance(reconciliation, dict):
            raise ValueError('Successor validation requires a reconciliation object')
        for key in ('inherited_work', 'repair_history', 'scope_and_holds', 'current_owner_work'):
            text(reconciliation, key)
    elif record.get('accepted_batch_ref'):
        raise ValueError('An opening attestation cannot claim an accepted repair batch')
    if record.get('prerequisites_complete') is not True:
        raise ValueError('Guidance prerequisites are not explicitly complete')
    gaps = record.get('gaps')
    if not isinstance(gaps, list) or gaps:
        raise ValueError('Guidance attestation has unresolved prerequisite gaps')
    for key in ('guidance_application', 'approach', 'approach_reason', 'approved_scope_ref', 'human_holds_ref'):
        text(record, key)
    assumptions = record.get('assumptions')
    if not isinstance(assumptions, list) or assumptions:
        raise ValueError('Guidance attestation.assumptions must be an explicit empty list; assumed claims are not allowed')
    for key in ('evidence_refs', 'uncertainty'):
        values = record.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError('Guidance attestation.' + key + ' must be an explicit list of nonempty values')
    if not record['evidence_refs']:
        raise ValueError('Guidance attestation must link the deciding evidence')
    sources = record.get('reread')
    if not isinstance(sources, list) or not sources:
        raise ValueError('Guidance attestation must list the guidance files actually reread')
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError('Guidance reread entry ' + str(index) + ' must be an object')
        for key in ('kind', 'name', 'source_ref'):
            text(source, key)
        artifact(source, directory)
    return record


def render_guidance_attestation(packet, directory, pr_url, stage, report=None, receipt_context=None):
    """Render the approved stage ordering and return digests for the exact body."""
    record = guidance_attestation(packet, directory, pr_url, stage)
    if stage in ('opening', 'successor_validation') and not opening_report_ready(report):
        label = 'Successor validation' if stage == 'successor_validation' else 'Opening attestation'
        raise ValueError(label + ' requires a current clean report with successful canonical verification')
    if receipt_context is not None:
        if (not isinstance(receipt_context, dict) or
                not isinstance(receipt_context.get('list_sha256'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', receipt_context['list_sha256']) or
                type(receipt_context.get('generation')) is not int or receipt_context['generation'] < 0 or
                not isinstance(receipt_context.get('observed_at'), str) or
                not isinstance(receipt_context.get('entries'), list)):
            raise ValueError('Stored Pattern Buffer receipt context is malformed')
        for entry in receipt_context['entries']:
            if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or
                    not entry['name'] or not isinstance(entry.get('path'), str) or
                    not Path(entry['path']).is_absolute() or not isinstance(entry.get('sha256'), str) or
                    not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
                raise ValueError('Stored Pattern Buffer receipt context is malformed')

    def lines(values, empty='None recorded'):
        return '\n'.join('- ' + value for value in values) if values else '- ' + empty

    issue_line = 'Issue: ' + record['issue_ref']
    if stage == 'opening':
        sections = [
            '# Proposed PR opening',
            '## Approach',
            record['approach'] + '\n\nReason: ' + record['approach_reason'],
            '## Completed evidence',
            lines(record['evidence_refs']),
        ]
    elif stage == 'repair_batch':
        sections = [
            '# Accepted repair-batch attestation',
            GUIDANCE_ATTESTATION_STATEMENT,
            issue_line,
            'Accepted batch: ' + record['accepted_batch_ref'],
            '## Prerequisites completed before work',
            'Prerequisites are complete; no gaps are recorded.',
            '## Approach',
            record['approach'] + '\n\nReason: ' + record['approach_reason'],
            'Guidance application: ' + record['guidance_application'],
        ]
    else:
        prior = record['prior_publication_ref']
        reconciliation = record['reconciliation']
        sections = [
            '# Successor validation',
            SUCCESSOR_VALIDATION_STATEMENT,
            issue_line,
            'Inherited publication: ' + prior['comment_url'],
            'Inherited publication body SHA-256: ' + prior['body_sha256'],
            'Inherited publication attestation SHA-256: ' + prior['attestation_sha256'],
            'Current candidate: ' + report['snapshot']['head'],
            'Current report: clean candidate with successful canonical verification.',
            '## Inherited-work reconciliation',
            'Inherited work: ' + reconciliation['inherited_work'],
            'Existing repair history: ' + record['repair_history_ref'],
            'Repair history review: ' + reconciliation['repair_history'],
            'Scope and holds review: ' + reconciliation['scope_and_holds'],
            'Current owner work: ' + reconciliation['current_owner_work'],
            '## Current review',
            'Guidance application: ' + record['guidance_application'],
            record['approach'] + '\n\nReason: ' + record['approach_reason'],
            '## Current candidate evidence',
            lines(record['evidence_refs']),
        ]
    source_lines = []
    matched_receipts = set()
    for source in record['reread']:
        path = artifact(source, directory)
        line = (source['kind'] + ': ' + source['name'] + ' (' + source['source_ref'] + '; ' +
                str(path) + '@sha256:' + source['sha256'] + ')')
        receipt_key = (source['name'], str(path.resolve()), source['sha256'])
        if (receipt_context is not None and receipt_key not in matched_receipts and
                any((entry['name'], entry['path'], entry['sha256']) == receipt_key
                    for entry in receipt_context['entries'])):
            line += ' [read in current context window, generation ' + str(receipt_context['generation']) + ']'
            matched_receipts.add(receipt_key)
        source_lines.append(line)
    sections.extend([
        '## Guidance reread',
        lines(source_lines),
        '## Scope and holds',
        'Approved scope: ' + record['approved_scope_ref'] + '\n\nHuman holds: ' + record['human_holds_ref'],
        '## Material assumptions',
        lines(record['assumptions'], 'None'),
        '## Material uncertainty',
        lines(record['uncertainty']),
    ])
    if stage == 'repair_batch':
        sections.insert(sections.index('## Scope and holds') + 1,
                        'Existing repair history: ' + record['repair_history_ref'])
    elif stage == 'opening':
        sections.extend([
            '## Opening attestation',
            'Guidance application: ' + record['guidance_application'],
            issue_line,
            GUIDANCE_ATTESTATION_STATEMENT,
        ])
    body = '\n\n'.join(sections).rstrip()
    attestation_bytes = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    rendered = {'stage': stage, 'body': body,
                'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                'attestation_sha256': hashlib.sha256(attestation_bytes).hexdigest()}
    if receipt_context is not None:
        rendered['required_reads'] = receipt_context
    return rendered


def opening_report_ready(report):
    if (not isinstance(report, dict) or not isinstance(report.get('snapshot'), dict) or
            report['snapshot'].get('clean') is not True or not isinstance(report.get('items'), list)):
        return False
    return any(item.get('id') == 'local-verification' and item.get('status') == 'satisfied'
               for item in report.get('items', []) if isinstance(item, dict))


def candidate(record, current, cwd):
    recorded = commit(cwd, record.get('revision'))
    tree = value(cwd, 'rev-parse', recorded + '^{tree}')
    if not current['clean']:
        raise ValueError('Working tree has changes; committed evidence cannot cover them')
    if tree != current['tree']:
        raise ValueError('Candidate source or tracked verification inputs changed since evidence')
    return recorded


def audit_prerequisite(packet, cwd, directory):
    """An incomplete audit is valid input; an absent audit is not a report."""
    record = packet.get('design-fidelity')
    if not isinstance(record, dict) or type(record.get('applicable')) is not bool:
        raise ValueError('Before reporting, declare design applicability and conduct design-fidelity-audit '
                         'when designs were involved')
    if record['applicable'] is False:
        text(record, 'reason')
        return
    try:
        artifact(record.get('audit'), directory)
        commit(cwd, record.get('revision'))
        if record.get('skill') not in SKILLS:
            raise ValueError('Record the design-fidelity-audit skill used')
    except ERRORS as error:
        raise ValueError('Conduct and record design-fidelity-audit before making the report: ' + str(error)) from error


def audit_candidate(record, current, cwd, directory):
    audited = commit(cwd, record.get('revision'))
    if not current['clean']:
        raise ValueError('Working tree has changes; committed audit cannot cover them')
    if value(cwd, 'rev-parse', audited + '^{tree}') == current['tree']:
        return {'mode': 'identical-tree', 'audit_revision': audited}
    reuse = record.get('reuse')
    if not isinstance(reuse, dict):
        raise ValueError('Candidate changed since audit; record a scoped reuse comparison or audit affected scope again')
    if reuse.get('candidate_revision') != current['head']:
        raise ValueError('Audit reuse comparison must identify the exact current candidate SHA')
    artifact(reuse.get('comparison'), directory)
    text(reuse, 'dependency_scope')
    protected = []
    for field in ('audited_paths', 'dependency_paths'):
        paths = reuse.get(field)
        if not isinstance(paths, list) or (field == 'audited_paths' and not paths):
            raise ValueError('Reuse needs nonempty audited_paths and an explicit dependency_paths list')
        for path in paths:
            if (not isinstance(path, str) or not path or path.startswith('/') or
                    any(part in ('', '..') for part in path.split('/'))):
                raise ValueError('Audit reuse paths must be literal repository-relative paths')
            # Literal pathspecs prevent wildcards and Git pathspec magic from shrinking coverage.
            spec = ':(top,literal)' + path
            if not value(cwd, 'ls-tree', '-r', '--name-only', audited, '--', spec):
                raise ValueError('Protected audit path does not exist at audited revision: ' + path)
            protected.append(spec)
    changed = value(cwd, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only',
                    audited, current['head'], '--', *protected)
    if changed:
        raise ValueError('Audited UI or declared shared dependencies changed; audit affected scope again: ' + changed)
    return {'mode': 'reviewed-scope', 'audit_revision': audited, 'candidate_revision': current['head'],
            'audited_paths': reuse['audited_paths'], 'dependency_paths': reuse['dependency_paths'],
            'dependency_scope': reuse['dependency_scope'],
            'limitation': 'Git checks declared paths; completeness of dependency and behavior coverage is recorded judgment'}


def fidelity(record, current, cwd, directory, approval_authority):
    gaps = Gaps()
    if record.get('applicable') is False:
        gaps.check('revision', lambda: candidate(record, current, cwd))
        reason = gaps.check('reason', lambda: text(record, 'reason'))
        result = gaps.result(reason)
        if not gaps.items:
            result['status'] = 'explicitly-not-applicable'
        return result
    comparison = gaps.check('revision', lambda: audit_candidate(record, current, cwd, directory))
    gaps.check('design_reference', lambda: text(record, 'design_reference'))
    gaps.check('design', lambda: artifact(record.get('design'), directory))
    gaps.check('audit', lambda: artifact(record.get('audit'), directory))
    coverage = record.get('coverage')
    fidelity_value = record.get('fidelity')
    gaps.require('coverage', coverage == 'FULL', 'Audit coverage must be FULL; recorded: ' + str(coverage))
    gaps.require('fidelity', fidelity_value in ('FAITHFUL', 'DRIFT', 'MAJOR_DRIFT'),
                 'Record assessed fidelity separately: FAITHFUL, DRIFT or MAJOR_DRIFT; NOT_ASSESSABLE cannot satisfy fidelity')
    gaps.check('assessed_scope', lambda: text(record, 'assessed_scope'))
    gaps.require('not_assessable', record.get('not_assessable') == [],
                 'List not_assessable scope explicitly; uncovered properties remain gaps')
    gaps.require('unresolved', record.get('unresolved') == [], 'Audit has unresolved items or no explicit unresolved list')
    authority = record.get('authority_map')
    gaps.require('authority_map', isinstance(authority, list) and bool(authority), 'Record authority by region/state and specified/open properties')
    for index, row in enumerate(authority if isinstance(authority, list) else []):
        for key in ('region_state', 'reference', 'properties_specified', 'properties_open'):
            gaps.check('authority_map.' + str(index) + '.' + key, lambda row=row, key=key: text(row, key))
    deviations = record.get('deviations')
    gaps.require('deviations', isinstance(deviations, list), 'List every requested deviation, including an explicit empty list')
    seen = set()
    approved = set()
    for index, deviation in enumerate(deviations if isinstance(deviations, list) else []):
        field = 'deviations.' + str(index)
        before = len(gaps.items)
        identifier = gaps.check(field + '.id', lambda: text(deviation, 'id'))
        gaps.require(field + '.id', identifier not in seen, 'Duplicate deviation ID: ' + str(identifier))
        seen.add(identifier)
        gaps.check(field + '.description', lambda: text(deviation, 'description'))
        approval = deviation.get('approval', {}) if isinstance(deviation, dict) else {}
        approval = approval if isinstance(approval, dict) else {}
        gaps.require(field + '.approval',
                     approval.get('by') == approval_authority['label'] and approval.get('decision') == 'approved',
                     'Deviation ' + str(identifier) + ' needs approval from ' + approval_authority['label'])
        if approval:
            gaps.require(field + '.channel', approval.get('channel') in approval_authority['accepted_channels'],
                         'Deviation ' + str(identifier) + ': cite an accepted approval source; peer relays alone are insufficient')
            for key in ('source', 'quote'):
                gaps.check(field + '.' + key, lambda key=key: text(approval, key))
            gaps.check(field + '.evidence', lambda: artifact(approval.get('evidence'), directory))
        if len(gaps.items) == before:
            approved.add(identifier)
    findings = record.get('findings')
    gaps.require('findings', isinstance(findings, list) and bool(findings), 'Record property findings, including MATCH rows')
    finding_ids = set()
    verdicts = []
    for index, row in enumerate(findings if isinstance(findings, list) else []):
        field = 'findings.' + str(index)
        identifier = gaps.check(field + '.id', lambda: text(row, 'id'))
        gaps.require(field + '.id', identifier not in finding_ids, 'Duplicate finding ID: ' + str(identifier))
        finding_ids.add(identifier)
        for key in ('region_state', 'property', 'reference_says', 'implementation_shows'):
            gaps.check(field + '.' + key, lambda key=key: text(row, key))
        if not isinstance(row, dict):
            continue
        verdict = row.get('verdict')
        verdicts.append(verdict)
        gaps.require(field + '.verdict', verdict in ('MATCH', 'DRIFT', 'MISSING', 'INVENTED', 'NOT_ASSESSABLE'),
                     'Finding ' + str(identifier) + ' has an invalid property verdict')
        if verdict == 'NOT_ASSESSABLE':
            gaps.check(field + '.blocker', lambda: text(row, 'blocker'))
            gaps.require(field, False, 'Finding ' + str(identifier) + ' cannot be assessed; do not call it MISSING')
        else:
            for key in ('reference_evidence', 'implementation_evidence'):
                gaps.check(field + '.' + key, lambda key=key: artifact(row.get(key), directory))
        if verdict in ('DRIFT', 'MISSING', 'INVENTED'):
            gaps.require(field + '.deviation_id', isinstance(row.get('deviation_id'), str) and row['deviation_id'] in approved,
                         'Finding ' + str(identifier) + ' needs a linked, explicitly approved deviation or reconciliation')
    if fidelity_value == 'FAITHFUL':
        assessable = [verdict for verdict in verdicts if verdict != 'NOT_ASSESSABLE']
        gaps.require('fidelity', bool(assessable) and all(v == 'MATCH' for v in assessable),
                     'FAITHFUL requires at least one assessable property and every assessable property to MATCH')
    if fidelity_value in ('DRIFT', 'MAJOR_DRIFT'):
        gaps.require('fidelity', any(v in ('DRIFT', 'MISSING', 'INVENTED') for v in verdicts),
                     'A drift verdict needs a corresponding property finding')
    return gaps.result('Audit coverage and findings accounted for; approved deviations remain visible; authorship is not authenticated',
                       coverage=coverage, fidelity=fidelity_value, authority_map=authority,
                       assessed_scope=record.get('assessed_scope'), not_assessable=record.get('not_assessable'),
                       unresolved=record.get('unresolved'), findings=findings, deviations=deviations, audit_comparison=comparison)


def main_awareness(record, current, cwd, directory):
    gaps = Gaps()
    gaps.check('revision', lambda: candidate(record, current, cwd))
    gaps.require('base_head', record.get('base_head') == current['base_head'],
                 'main-awareness.base_head must equal the observed target snapshot ' + current['base_head'] +
                 '; renew the main assessment if the target changed, without rerunning unchanged verification')
    for key in ('relevant_changes', 'existing_solutions'):
        gaps.check(key, lambda key=key: text(record, key))
    gaps.check('evidence', lambda: artifact(record.get('evidence'), directory))
    return gaps.result('Recorded assessment covers this candidate and local target snapshot')


def verification(record, current, cwd, directory):
    gaps = Gaps()
    revision = gaps.check('revision', lambda: candidate(record, current, cwd))
    command = record.get('command')
    valid_command = isinstance(command, list) and bool(command) and all(isinstance(x, str) and x for x in command)
    gaps.require('command', valid_command, 'local-verification.command must be a nonempty argv array')
    source = gaps.check('command_source', lambda: artifact(record.get('command_source'), directory))
    if source and valid_command:
        gaps.check('command_source', lambda: documented_command(source, command))
    gaps.require('exit_code', type(record.get('exit_code')) is int and record['exit_code'] == 0,
                 'Canonical verification has no successful exit result')
    gaps.require('outcome', record.get('outcome') in ('passed', 'nothing-affected'),
                 'Outcome must be passed or explicit nothing-affected')
    gaps.check('evidence', lambda: artifact(record.get('evidence'), directory))
    if 'evidence' not in record:
        gaps.require('evidence', False, 'local-verification.evidence must contain path and sha256 for the actual result log; the field is not named result')
    inputs = record.get('inputs')
    gaps.require('inputs', isinstance(inputs, list), 'Declare external verification input files, including an explicit empty list')
    for index, item in enumerate(inputs if isinstance(inputs, list) else []):
        gaps.check('inputs.' + str(index), lambda item=item: artifact(item, directory))
    gaps.check('input_scope', lambda: text(record, 'input_scope'))
    detail = 'Successful canonical result recorded at ' + str(revision)
    if record.get('outcome') == 'nothing-affected':
        gaps.check('nothing_affected_reason', lambda: text(record, 'nothing_affected_reason'))
        detail = 'Explicit successful nothing-affected verdict recorded; no tests-passed claim'
    return gaps.result(detail + '; tracked tree and declared external inputs match')


def documented_command(source, command):
    content = source.read_text()
    if ' '.join(command) in content:
        return
    raise ValueError('local-verification.command_source must document the exact command ' + repr(' '.join(command)))


def conflicts(current, cwd):
    if not current['clean']:
        raise ValueError('Working tree has changes; conflict observation covers committed candidates only')
    # merge-tree writes synthetic objects. Keep them in a disposable object store,
    # with the real repository used only as a read-only alternate.
    drivers = git(cwd, 'config', '--get-regexp', r'^merge\..*\.driver$')
    if drivers.returncode == 0:
        raise ValueError('Custom merge drivers configured; offline report will not execute them')
    if drivers.returncode != 1:
        raise ValueError('Could not inspect merge-driver configuration')
    objects = value(cwd, 'rev-parse', '--path-format=absolute', '--git-path', 'objects')
    with tempfile.TemporaryDirectory(prefix='starfleet-conflict-') as directory:
        result = git(cwd, 'merge-tree', '--write-tree', '--messages', current['head'], current['base_head'],
                     env={'GIT_OBJECT_DIRECTORY': directory,
                          'GIT_ALTERNATE_OBJECT_DIRECTORIES': objects})
    if result.returncode == 0:
        return 'satisfied', 'No merge conflicts with the named local target snapshot; being behind is allowed'
    if result.returncode == 1:
        return 'missing', 'Merge conflicts found:\n' + result.stdout.strip()
    raise ValueError('Conflict check unavailable: ' + result.stderr.strip())


def report(cwd, base_ref, pr_url, packet_path, policy_path=POLICY):
    current = snapshot(cwd, base_ref)
    cwd = current['root']
    policy = json.loads(Path(policy_path).read_text())
    approval_authority = policy.get('approval_authority')
    if (not isinstance(approval_authority, dict) or
            not isinstance(approval_authority.get('label'), str) or not approval_authority['label'].strip() or
            not isinstance(approval_authority.get('accepted_channels'), list) or
            not approval_authority['accepted_channels'] or
            any(not isinstance(value, str) or not value.strip() for value in approval_authority['accepted_channels'])):
        raise ValueError('Checklist policy needs a configured human approval authority and accepted source channels')
    checks = policy.get('checks')
    if not isinstance(checks, list) or not checks:
        raise ValueError('Checklist policy needs a nonempty checks list')
    identifiers = [entry['id'] for entry in checks]
    if len(set(identifiers)) != len(identifiers) or any(x not in CHECKS for x in identifiers):
        raise ValueError('Checklist IDs must be unique supported checks')
    for entry in checks:
        text(entry, 'instruction')
        if type(entry.get('enabled')) is not bool:
            raise ValueError('Each checklist entry needs enabled: true or false')
    packet_path = Path(packet_path).resolve()
    try:
        packet = json.loads(packet_path.read_text())
        if github.reference(packet.get('pr_url', ''), 'pull')['url'] != github.reference(pr_url, 'pull')['url']:
            raise ValueError('pr_url must identify the requested repository and PR: ' + pr_url)
        if packet.get('base_ref') != base_ref:
            raise ValueError('base_ref must equal the requested target ref: ' + base_ref)
    except (OSError, ValueError, AttributeError, TypeError, github.RecordError) as error:
        raise ValueError('A bound evidence packet and conducted audit (or explicit no-design reason) '
                         'are required before reporting: ' + str(error)) from error
    audit_prerequisite(packet, cwd, packet_path.parent)
    items = []
    evaluators = {'design-fidelity': lambda record, snapshot, path, directory:
                  fidelity(record, snapshot, path, directory, approval_authority),
                  'main-awareness': main_awareness, 'local-verification': verification}
    for entry in checks:
        if not entry['enabled']:
            continue
        identifier = entry['id']
        try:
            if identifier == 'merge-conflicts':
                status, detail = conflicts(current, cwd)
                assessment = {'status': status, 'detail': detail,
                              'gaps': [] if status == 'satisfied' else [{'field': identifier, 'message': detail}]}
            else:
                record = packet.get(identifier)
                if not isinstance(record, dict):
                    raise ValueError('Missing evidence section: ' + identifier)
                assessment = evaluators[identifier](record, current, cwd, packet_path.parent)
        except ERRORS as error:
            assessment = {'status': 'missing', 'detail': str(error),
                          'gaps': [{'field': identifier, 'message': str(error)}]}
        items.append({'id': identifier, 'instruction': entry['instruction'], **assessment})
    # A concurrent writer must not yield an apparently coherent successful report.
    after = snapshot(cwd, base_ref)
    if any(current[key] != after[key] for key in ('head', 'base_head', 'tree', 'clean')):
        raise ValueError('Candidate or target changed during observation; retry the report')
    return {'pr_url': pr_url, 'snapshot': current, 'items': items,
            'disabled': [entry['id'] for entry in checks if not entry['enabled']],
            'omitted': [identifier for identifier in CHECKS if identifier not in identifiers],
            'policy_sha256': hashlib.sha256(Path(policy_path).read_bytes()).hexdigest(),
            'notice': 'Offline local snapshots only. PR identity/target, evidence completeness and approval authorship '
                      'are recorded assertions, not authenticated proof. No readiness or merge authorization.',
            'missing': sum(item['status'] == 'missing' for item in items)}


def command(args):
    if args.delivery_action == 'snapshot':
        output = snapshot(args.cwd, args.base_ref)
    else:
        output = report(args.cwd, args.base_ref, args.pr_url, args.evidence, args.policy)
    return display(output, args.format, args.delivery_action == 'snapshot')


def display(output, format_name, is_snapshot=False):
    if format_name == 'json' or is_snapshot:
        print(json.dumps(output, indent=2))
    else:
        print('Delivery evidence for ' + output['pr_url'])
        print('Candidate: ' + output['snapshot']['head'])
        print('Local target: ' + output['snapshot']['base_ref'] + ' @ ' + output['snapshot']['base_head'])
        for item in output['items']:
            print('\n' + item['id'] + ': ' + item['status'])
            print(item['instruction'])
            if item['gaps']:
                for gap in item['gaps']:
                    print('- ' + item['id'] + '.' + gap['field'] + ': ' + gap['message'])
            else:
                print(item['detail'])
            if item['id'] == 'design-fidelity' and 'coverage' in item:
                print('Coverage: ' + str(item['coverage']) + '; Fidelity: ' + str(item['fidelity']))
                for finding in item.get('findings') or []:
                    if isinstance(finding, dict):
                        print('  ' + str(finding.get('id')) + ' / ' + str(finding.get('region_state')) +
                              ' / ' + str(finding.get('property')) + ': ' + str(finding.get('verdict')))
                        print('    Reference: ' + str(finding.get('reference_says')))
                        print('    Implementation: ' + str(finding.get('implementation_shows')))
                for deviation in item.get('deviations') or []:
                    if isinstance(deviation, dict):
                        print('  Requested deviation ' + str(deviation.get('id')) + ': ' + str(deviation.get('description')))
        print('\nDisabled: ' + ', '.join(output['disabled']) + '; omitted: ' + ', '.join(output['omitted']))
        print(output['notice'])
    # Missing evidence is a report result, not a gate or permission change.
    return 0
