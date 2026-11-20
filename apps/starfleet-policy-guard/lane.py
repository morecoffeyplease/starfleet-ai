#!/usr/bin/env python3
"""Scoped delivery-only adapter. Probes never forward readiness or merge actions."""

import argparse
from collections import deque
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import delivery
import delivery_runs
import github_records as github
import lane_scope
import session_store
import cleanup as seat_cleanup

APP = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()
UPGRADE_JOURNAL = 'upgrade-journal.json'
RETIREMENT_PROTOCOL = 'policy-guard-missing-checkout-retirement-v1'
SEAT_RETIREMENT_PROTOCOL = 'policy-guard-drained-seat-retirement-v1'
SEAT_RETIREMENT_JOURNAL = 'seat-retirement-journal.json'


def root(cwd):
    return Path(delivery.value(cwd, 'rev-parse', '--show-toplevel')).resolve()


def active_operational_anchor(seat):
    anchor = session_store.operational_anchor(seat)
    if anchor['state'] != 'active':
        raise ValueError('This permanent seat has released its operational anchor and is history-only')
    try:
        observed_root = str(root(anchor['root']))
        observed_gitdir = delivery.value(anchor['root'], 'rev-parse', '--absolute-git-dir')
        observed_common = delivery.value(anchor['root'], 'rev-parse', '--path-format=absolute', '--git-common-dir')
    except (OSError, TypeError, ValueError):
        raise ValueError('The permanent seat operational anchor no longer resolves as its recorded Git worktree') from None
    if (observed_root != anchor['root'] or observed_gitdir != anchor['gitdir'] or
            (anchor.get('common_gitdir') is not None and observed_common != anchor['common_gitdir'])):
        raise ValueError('The permanent seat operational anchor Git identity changed; preserve history and inspect recovery')
    return anchor


def directory(cwd):
    product_root = root(cwd)
    found = session_store.lookup_checkout(product_root)
    if found:
        return found['checkout_dir']
    return Path(delivery.value(cwd, 'rev-parse', '--absolute-git-dir')) / 'starfleet-delivery-lane'


def managed_adapter_path(state, cwd):
    if state.get('seat_id'):
        return session_store.seat_dir(state['seat_id']) / 'bin/gh'
    return directory(cwd) / 'bin/gh'


def branch(cwd):
    return delivery.value(cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD')


def sync_directory(path):
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def refuse_symlinked_path(path):
    path = Path(os.path.abspath(path))
    current = path
    while True:
        if current.is_symlink():
            raise ValueError('Refusing symlinked managed path: ' + str(path))
        if current.parent == current:
            break
        current = current.parent


def write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.delivery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read(cwd):
    product_root = root(cwd)
    central = session_store.state_for_root(product_root)
    if central is not None:
        return central
    path = Path(delivery.value(cwd, 'rev-parse', '--absolute-git-dir')) / 'starfleet-delivery-lane/state.json'
    return json.loads(path.read_text()) if path.exists() else None


@contextlib.contextmanager
def file_locked(cwd):
    location = directory(cwd)
    location.mkdir(parents=True, exist_ok=True)
    found = session_store.lookup_checkout(root(cwd))
    lock_path = found['seat_dir'] / 'seat.lock' if found else location / 'lock'
    if found:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as stream:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('Delivery lane state busy; retry at a safe boundary')
                time.sleep(0.02)
        yield


@contextlib.contextmanager
def source_upgrade_locked(cwd, seat_ids):
    if seat_ids:
        with session_store.locked_seats(seat_ids):
            yield
    else:
        with file_locked(cwd):
            yield


@contextlib.contextmanager
def locked(cwd):
    with file_locked(cwd):
        ensure_no_upgrade_journal(cwd)
        location = directory(cwd)
        value = read(cwd) or {}
        yield value
        if value:
            value['updated_at'] = time.time()
        write(location / 'state.json', json.dumps(value, indent=2) + '\n')
        found=session_store.lookup_checkout(root(cwd))
        if found and value:
            manifest=session_store.load_seat(found['seat']['seat_id'])
            session_store.sync_pr_links(manifest,found['checkout_id'],value)
            session_store.save_seat(manifest)


def source_version_names(harness=None):
    names = ['lane.py', 'lane_scope.py', 'delivery.py', 'delivery_runs.py',
             'github_records.py', 'session_store.py', 'cleanup.py']
    if harness == 'opencode':
        names.extend(('opencode-plugin/index.ts', 'opencode-plugin/policy.js'))
    return names


def source_versions(harness=None):
    return {name: delivery_runs.digest(APP / name) for name in source_version_names(harness)}


def permanent_handoff_runtime(seat):
    runtime = seat.get('runtime') if isinstance(seat, dict) else None
    if (not isinstance(runtime, dict) or not isinstance(runtime.get('version'), str) or
            not isinstance(runtime.get('path'), str) or not Path(runtime['path']).is_absolute() or
            not isinstance(runtime.get('files'), dict)):
        raise ValueError('Permanent seat runtime metadata is malformed; preserve it and inspect status')
    runtime_path = Path(runtime['path'])
    expected_path = session_store.runtime_root() / runtime['version']
    if runtime_path != expected_path:
        raise ValueError('Permanent seat runtime path is not its recorded immutable runtime; inspect status')
    try:
        session_store._refuse_symlinked_path(runtime_path)
        session_store._require_sealed_runtime(runtime_path)
        verified = session_store.recorded_runtime_plan(runtime_path, runtime['files'])
    except (OSError, TypeError, ValueError):
        raise ValueError('Permanent seat runtime is changed or unavailable; preserve it and inspect status') from None
    if (verified['version'] != runtime['version'] or verified['path'] != runtime['path'] or
            verified['files'] != runtime['files']):
        raise ValueError('Permanent seat runtime metadata differs from its immutable runtime; inspect status')
    return runtime


def runtime_source_versions(runtime, harness):
    names = source_version_names(harness)
    if any(name not in runtime['files'] for name in names):
        raise ValueError('Permanent seat runtime does not contain the incoming adapter source; no owner handoff was recorded')
    return {name: runtime['files'][name] for name in names}


def permanent_handoff_source_versions(seat, harness):
    try:
        incoming = source_versions(harness)
    except OSError:
        raise ValueError('Incoming delivery adapter source is missing or unavailable; no owner handoff was recorded') from None
    runtime = seat.get('runtime') if isinstance(seat, dict) else None
    if (not isinstance(runtime, dict) or not isinstance(runtime.get('version'), str) or
            not isinstance(runtime.get('path'), str) or not Path(runtime['path']).is_absolute() or
            not isinstance(runtime.get('files'), dict)):
        raise ValueError('Permanent seat runtime metadata is malformed; preserve it and inspect status')
    try:
        verified = session_store.recorded_runtime_plan(APP, runtime['files'])
    except (OSError, TypeError, ValueError):
        raise ValueError('Permanent seat runtime is changed or unavailable; preserve it and inspect status') from None
    if (verified['version'] != runtime['version'] or verified['path'] != runtime['path'] or
            any(runtime['files'].get(name) != digest for name, digest in incoming.items())):
        raise ValueError('Incoming delivery adapter source differs from the permanent seat runtime; no owner handoff was recorded')
    return incoming


def source_revision(runtime=None):
    try:
        return delivery.value(APP, 'rev-parse', 'HEAD')
    except ValueError:
        return 'runtime:' + (runtime or session_store.runtime_plan(APP)['version'])


def source_is_immutable_runtime(runtime=None):
    try:
        planned = runtime or session_store.runtime_plan(APP)
        return Path(planned['path']).resolve() == APP.resolve()
    except (OSError, ValueError, KeyError):
        return False


def upgrade_source_guidance_reason(immutable_runtime=False):
    if immutable_runtime:
        return ('This CLI is an immutable runtime, and upgrade-plan reads the executing installer source Git revision. '
                'The product checkout has a separate Git revision that cannot supply it. '
                'No exact compatible source entrypoint is recorded. Choose a verified matching clean committed newer '
                'Starfleet source checkout before planning. Status and coverage show the existing owner and source diagnostics.')
    return ('upgrade-plan reads the executing installer source Git revision, and the product checkout has a separate '
            'Git revision that cannot supply it. No exact compatible source entrypoint is recorded. Choose a verified '
            'matching clean committed newer Starfleet source checkout before planning. Status and coverage show the '
            'existing owner and source diagnostics.')


def require_intact_upgrade_source_pin(state):
    old_pin = source_pin_status(state)
    if old_pin['integrity'] != 'intact':
        raise ValueError('Installed source pin is ' + old_pin['integrity'] +
                         '; restore its exact files before planning a recoverable upgrade')
    return old_pin


def upgrade_journal_path(cwd):
    found = session_store.lookup_checkout(root(cwd))
    if found:
        return found['seat_dir'] / UPGRADE_JOURNAL
    return directory(cwd) / UPGRADE_JOURNAL


def seat_retirement_journal_path(identifier):
    return session_store.seat_dir(identifier) / SEAT_RETIREMENT_JOURNAL


def ensure_no_seat_retirement_journal(identifier):
    journal = seat_retirement_journal_path(identifier)
    if journal.exists() or journal.is_symlink():
        raise ValueError('A drained-seat retirement is incomplete; inspect it with --seat-id ' + identifier +
                         ' recover --inspect, then use recover --rollback from the exact native operator')


def upgrade_recovery_command(cwd):
    return shlex.join([sys.executable, str(SELF), '--cwd', str(root(cwd)),
                       'upgrade-recover', '--rollback'])


def ensure_no_upgrade_journal(cwd):
    journal = upgrade_journal_path(cwd)
    if journal.exists() or journal.is_symlink():
        raise ValueError('A source upgrade is incomplete. Run ' + upgrade_recovery_command(cwd) +
                         ' to restore the recorded preimages before continuing')
    found = session_store.lookup_checkout(root(cwd))
    if found and (found['seat'].get('install_journal') or found['seat'].get('migration_journal') or
                  found['seat'].get('handoff_journal')):
        raise ValueError('A seat installation or migration is incomplete. Preserve its journal and run the installed runtime with --seat-id ' +
                         found['seat']['seat_id'] + ' recover --inspect')
    if found:
        ensure_no_seat_retirement_journal(found['seat']['seat_id'])


def state_for_upgrade_guard(cwd):
    journal_path = upgrade_journal_path(cwd)
    state_error = None
    try:
        state = read(cwd)
    except (OSError, TypeError, ValueError) as error:
        state, state_error = None, error
    if state is not None and not isinstance(state, dict):
        state, state_error = None, ValueError('Managed delivery state is malformed')
    if not journal_path.exists() and not journal_path.is_symlink():
        if state_error:
            raise state_error
        return state
    if journal_path.is_symlink():
        ensure_no_upgrade_journal(cwd)
    try:
        journal = json.loads(journal_path.read_text())
        journal_state = json.loads(journal['state']['preimage']['text'])
        if not isinstance(journal_state, dict):
            raise ValueError('Upgrade journal state preimage is invalid')
        expected_root = str(root(cwd))
        if journal.get('root') != expected_root or journal_state.get('root') != expected_root:
            ensure_no_upgrade_journal(cwd)
        if state is not None and state.get('root') != expected_root:
            ensure_no_upgrade_journal(cwd)
        # The journal preimage is the immutable action scope while recovery is pending.
        # A changed phase or binding in the current state must not hide the covered PR.
        return journal_state
    except (OSError, KeyError, TypeError, ValueError):
        ensure_no_upgrade_journal(cwd)


def source_pin_status(state):
    expected = state.get('source_versions') if isinstance(state, dict) else None
    source = state.get('source') if isinstance(state, dict) else None
    if not isinstance(expected, dict) or not expected or not isinstance(source, str):
        return {'integrity': 'unknown', 'path': source, 'files': {}}
    files = {}
    missing = []
    changed = []
    source_root = Path(source)
    for name, expected_hash in sorted(expected.items()):
        relative = Path(name) if isinstance(name, str) else None
        if (relative is None or not name or not relative.parts or relative.is_absolute() or '\\' in name or
                any(part in ('.', '..') for part in relative.parts)):
            files[str(name)] = {'expected_sha256': expected_hash, 'actual_sha256': None, 'state': 'invalid-name'}
            changed.append(str(name))
            continue
        path = source_root
        symlinked = path.is_symlink()
        for part in relative.parts:
            path = path / part
            symlinked = symlinked or path.is_symlink()
        if symlinked:
            files[name] = {'expected_sha256': expected_hash, 'actual_sha256': None, 'state': 'symlink'}
            changed.append(name)
            continue
        try:
            actual_hash = delivery_runs.digest(path)
        except FileNotFoundError:
            files[name] = {'expected_sha256': expected_hash, 'actual_sha256': None, 'state': 'missing'}
            missing.append(name)
            continue
        except OSError:
            files[name] = {'expected_sha256': expected_hash, 'actual_sha256': None, 'state': 'unavailable'}
            missing.append(name)
            continue
        matches = isinstance(expected_hash, str) and actual_hash == expected_hash
        files[name] = {'expected_sha256': expected_hash, 'actual_sha256': actual_hash,
                       'state': 'intact' if matches else 'changed'}
        if not matches:
            changed.append(name)
    integrity = 'missing' if missing else 'changed' if changed else 'intact'
    return {'integrity': integrity, 'path': source, 'revision': state.get('source_revision'),
            'expected_fingerprints': expected, 'files': files,
            'missing_files': missing, 'changed_files': changed}


def current_cli_status(state):
    installed = state.get('source_versions') if isinstance(state, dict) else None
    current = source_versions((state or {}).get('lane_owner', {}).get('harness'))
    match = bool(isinstance(installed, dict) and installed == current)
    try:
        revision = delivery.value(APP, 'rev-parse', 'HEAD')
        dirty = bool(delivery.value(APP, 'status', '--porcelain'))
    except Exception:
        revision, dirty = None, None
    return {'source_path': str(APP), 'source_revision': revision, 'source_fingerprints': current,
            'working_tree_dirty': dirty, 'matches_installed_pin': match,
            'compatibility': ('no-installed-pin' if not installed else
                              'matches-installed-pin' if match else 'inspector-differs-from-installed-pin'),
            'pin_relation': 'same-source-pin' if match else 'different-source-pin' if installed else 'unknown',
            'state_compatibility': 'recognized' if isinstance(state, dict) and
            isinstance(state.get('root'), str) and isinstance(state.get('phase'), str) else 'unknown'}


def upgrade_action_states(initial, cwd, action, search):
    if not initial or not action or action.get('exempt'):
        return []
    states = [initial]
    if initial.get('lane_owner'):
        states = lane_scope.expand(initial)
    elif search:
        states = registered_states(initial, include_upgrade_preimages=True)
        if not any(row.get('root') == initial.get('root') and row.get('pr_url') == initial.get('pr_url')
                   for row in states):
            states.append(initial)
    try:
        actual = str(root(cwd))
    except ValueError:
        return []
    if search:
        try:
            if delivery.value(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir') != delivery.value(
                    initial['root'], 'rev-parse', '--path-format=absolute', '--git-common-dir'):
                return []
        except ValueError:
            return []
    elif actual not in (initial.get('root'), initial.get('session_root', initial.get('root'))):
        return []
    matches = []
    for state in states:
        if state.get('phase') not in ('active', 'installing'):
            continue
        if action.get('target') is None and actual != state.get('root'):
            continue
        if not state.get('pr_url'):
            continue
        try:
            delivery_runs.target_matches(action, state['pr_url'], state.get('head_branch', state.get('branch')))
        except ValueError:
            continue
        matches.append(state)
    return matches


def guard_pending_upgrade(initial, cwd, action, search=True):
    for state in upgrade_action_states(initial, cwd, action, search):
        try:
            upgrade_journal_path(state['root'])
        except ValueError:
            # A missing registered checkout cannot perform this action. Let normal
            # routing report its actionable unavailable-checkout guidance.
            continue
        ensure_no_upgrade_journal(state['root'])


def active(state, cwd):
    try:
        return bool(state and state.get('phase') == 'active' and state.get('pr_url') and
                    state['root'] == str(root(cwd)) and state['branch'] == branch(cwd))
    except ValueError:
        return False


def attached_here(state, cwd):
    """Check active checkout configuration without requiring a PR binding."""
    try:
        return bool(state and state.get('phase') == 'active' and
                    state.get('root') == str(root(cwd)) and state.get('branch') == branch(cwd))
    except ValueError:
        return False


def attestation_owner_state(state, cwd):
    if not state or not state.get('lane_owner'):
        raise ValueError('Guidance attestation requires an enrolled native-owner lane')
    lane_scope.check_owner(state)
    if state.get('lane_owner', {}).get('harness') not in ('claude', 'codex'):
        raise ValueError('Guidance attestation adoption is qualified only for Claude and Codex owners')
    check_lane_scope(state, cwd)
    if not active(state, cwd):
        raise ValueError('Guidance attestation requires the owner checkout to have its current published PR binding')
    return state


def attestation_packet(path, state):
    path = Path(path).resolve()
    packet = json.loads(path.read_text())
    try:
        packet_pr = github.reference(packet.get('pr_url', ''), 'pull')['url']
        current_pr = github.reference(state['pr_url'], 'pull')['url']
    except (AttributeError, TypeError, github.RecordError) as error:
        raise ValueError('Guidance evidence packet must bind the current PR: ' + str(error)) from error
    if (packet_pr.casefold() != current_pr.casefold() or
            packet.get('base_ref') != state.get('base_ref')):
        raise ValueError('Guidance evidence packet PR and target must match the attached PG binding')
    return packet, path


def captured_gh(state):
    path = state.get('real_gh')
    if not isinstance(path, str) or not path:
        raise ValueError('Attestation publication checks require the GitHub CLI captured by the owner lane')
    captured = state.get('real_gh_identity')
    if captured and captured_binary_identity(path) != captured:
        raise ValueError('Captured GitHub CLI changed since lane enrollment; inspect the owner lane before continuing')
    if not captured:
        captured_binary_identity(path)
    return path


def guidance_owner_key(owner, label):
    if (not isinstance(owner, dict) or owner.get('harness') not in ('claude', 'codex') or
            not isinstance(owner.get('session_id'), str) or not owner['session_id'].strip()):
        raise ValueError(label + ' has no valid Claude or Codex native owner')
    return {'harness': owner['harness'], 'session_id': owner['session_id']}


def guidance_publication_owner(publication, state, label):
    if (not isinstance(publication, dict) or type(publication.get('version')) is not int or
            publication['version'] != 1):
        raise ValueError(label + ' is missing or malformed')
    try:
        publication_pr = github.reference(publication.get('pr_url', ''), 'pull')['url']
        current_pr = github.reference(state.get('pr_url', ''), 'pull')['url']
    except (KeyError, TypeError, github.RecordError) as error:
        raise ValueError(label + ' has an invalid PR reference: ' + str(error)) from error
    if publication_pr.casefold() != current_pr.casefold():
        raise ValueError(label + ' belongs to a different PR binding')
    binding = {'branch': state.get('branch'),
               'head_branch': state.get('head_branch', state.get('branch')),
               'base_ref': state.get('base_ref')}
    if any(publication.get(key) != value for key, value in binding.items()):
        raise ValueError(label + ' belongs to a different branch or target binding')
    try:
        comment_ref = github.comment_reference(publication.get('comment_url', ''), state['pr_url'])
    except (KeyError, TypeError, github.RecordError) as error:
        raise ValueError(label + ' has an invalid comment reference: ' + str(error)) from error
    if publication.get('stage') not in ('opening', 'repair_batch', 'successor_validation'):
        raise ValueError(label + ' has an unsupported publication stage')
    if type(publication.get('comment_id')) is not int or publication['comment_id'] != comment_ref['comment_id']:
        raise ValueError(label + ' comment ID does not match its exact comment URL')
    for key in ('body_sha256', 'attestation_sha256'):
        if (not isinstance(publication.get(key), str) or
                not re.fullmatch(r'[0-9a-f]{64}', publication[key])):
            raise ValueError(label + ' has an invalid ' + key)
    return guidance_owner_key(publication.get('owner'), label)


def guidance_handoff_path(state, previous_owner, current_owner):
    if previous_owner == current_owner:
        raise ValueError('Successor validation requires a handoff from a different native owner')
    history = state.get('owner_history')
    if not isinstance(history, list) or not history:
        raise ValueError('Successor validation requires the supported owner handoff history')
    expected = current_owner
    path = []
    for transition in reversed(history):
        if not isinstance(transition, dict):
            raise ValueError('Owner handoff history is malformed; preserve it and inspect the lane')
        source = guidance_owner_key(transition.get('from'), 'Owner handoff history')
        target = guidance_owner_key(transition.get('to'), 'Owner handoff history')
        at = transition.get('at')
        valid_time = type(at) is int or (type(at) is float and math.isfinite(at))
        if (source == target or not valid_time or
                transition.get('authority') !=
                'caller explicitly confirms an authorized handoff; not independently authenticated'):
            raise ValueError('Owner handoff history is malformed; preserve it and inspect the lane')
        if target != expected:
            raise ValueError('Owner handoff history does not lead to the current native owner')
        path.append(transition)
        expected = source
        if expected == previous_owner:
            return list(reversed(path))
    raise ValueError('No supported owner handoff path connects the inherited publication to the current owner')


def successor_predecessor(state, packet):
    if state.get('attestation_required') is not True:
        raise ValueError('Successor validation is available only for an already adopted target; it cannot enable adoption')
    current_owner = guidance_owner_key(state.get('lane_owner'), 'Current lane owner')
    current_publication = state.get('guidance_attestation_ref')
    current_publication_owner = guidance_publication_owner(
        current_publication, state, 'Current or inherited guidance publication')
    record = packet.get('guidance_attestation') if isinstance(packet, dict) else None
    prior = record.get('prior_publication_ref') if isinstance(record, dict) else None
    same_owner_publication = None
    if current_publication_owner == current_owner:
        if current_publication.get('stage') != 'successor_validation':
            raise ValueError('Successor validation must link an inherited publication from a different native owner')
        expected_prior = current_publication.get('prior_publication_ref')
        if not isinstance(expected_prior, dict) or prior != expected_prior:
            raise ValueError('Repeated successor validation must retain the exact inherited publication and cannot reference itself')
        same_owner_publication = current_publication
        prior = expected_prior
    elif prior != current_publication:
        raise ValueError('Successor packet must link the exact currently inherited publication')
    prior_owner = guidance_publication_owner(prior, state, 'Inherited guidance publication')
    guidance_handoff_path(state, prior_owner, current_owner)
    return prior, same_owner_publication


def check_guidance_publication_comment(publication, state, executable, label, *, timeout=5):
    remote = github.read_comment(publication['comment_url'], state['pr_url'], executable, timeout=timeout)
    if remote['reference']['comment_id'] != publication.get('comment_id'):
        raise ValueError(label + ' comment ID does not match its exact publication reference')
    digest = hashlib.sha256(remote['body'].encode()).hexdigest()
    if digest != publication.get('body_sha256'):
        raise ValueError(label + ' comment body changed or no longer matches its exact publication reference')
    return remote


def packet_successor_acceptance(state, receipt, packet):
    policy = json.loads(Path(receipt['policy_path']).read_text())
    ref = github.reference(state['pr_url'], 'pull')
    repositories = policy.get('packet_successor_acceptance_repositories', [])
    if not isinstance(repositories, list) or any(not isinstance(value, str) for value in repositories):
        raise ValueError('Malformed packet successor acceptance repository policy')
    if ref['host'] + '/' + ref['repo'] not in repositories:
        raise ValueError('This repository retains its published successor validation policy')
    acceptance = packet.get('handoff_acceptance')
    if not isinstance(acceptance, dict) or acceptance.get('version') != 1:
        raise ValueError('The current handoff packet needs a version 1 handoff_acceptance')
    owner = guidance_owner_key(state.get('lane_owner'), 'Current lane owner')
    if guidance_owner_key(acceptance.get('owner'), 'Handoff acceptance') != owner:
        raise ValueError('Handoff acceptance belongs to a different native owner')
    previous = guidance_owner_key(acceptance.get('from_owner'), 'Handoff predecessor')
    guidance_handoff_path(state, previous, owner)
    for key in ('summary', 'repair_history_ref', 'scope_and_holds_ref'):
        delivery.text(acceptance, key)
    report = receipt['report']
    if acceptance.get('candidate') != report['snapshot']['head'] or not delivery.opening_report_ready(report):
        raise ValueError('Handoff acceptance requires the current clean candidate and successful canonical verification')
    return acceptance


def require_packet_successor_acceptance(state, cwd):
    receipt = state.get('receipt')
    delivery_runs.check(receipt, cwd, state['pr_url'])
    packet = json.loads(Path(receipt['packet_path']).read_text())
    acceptance = packet_successor_acceptance(state, receipt, packet)
    for entry in (receipt.get('required_reads') or {}).get('entries', []):
        if hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Required read changed after handoff acceptance; reread and record the current packet')
    return acceptance


def require_guidance_attestation(state, cwd, *, local_inspection=False):
    receipt = state.get('receipt')
    if isinstance(receipt, dict) and receipt.get('handoff_acceptance') is True:
        require_packet_successor_acceptance(state, cwd)
        return
    published = state.get('guidance_attestation_ref')
    if (not isinstance(published, dict) or type(published.get('version')) is not int or
            published['version'] != 1):
        guide_command = shlex.join([sys.executable, str(SELF), 'guide', '--print'])
        raise ValueError(
            'This target requires a published guidance attestation. Before the final start check, print the local '
            'guide with ' + guide_command + ' and reread its "Published guidance attestations" section. Then run '
            'attestation-start-check from its owner session.')
    published_owner = guidance_publication_owner(published, state, 'Published guidance attestation')
    owner = guidance_owner_key(state.get('lane_owner'), 'Current lane owner')
    if published_owner != owner:
        raise ValueError('Published guidance attestation belongs to a previous native owner; the current owner must validate its own publication')
    receipt = state.get('receipt')
    packet_path = receipt.get('packet_path') if isinstance(receipt, dict) else None
    if not isinstance(packet_path, str):
        raise ValueError('Guidance attestation requires the current report packet path; record the report before covered delivery')
    packet, path = attestation_packet(packet_path, state)
    report = receipt.get('report') if isinstance(receipt.get('report'), dict) else None
    prior = None
    if published.get('stage') == 'successor_validation':
        prior, _same_owner_publication = successor_predecessor(state, packet)
        if published.get('prior_publication_ref') != prior:
            raise ValueError('Successor publication no longer links the exact inherited publication in its current packet')
    record = packet.get('guidance_attestation') if isinstance(packet, dict) else None
    legacy_repair_publication = (
        published.get('stage') == 'repair_batch' and isinstance(record, dict) and
        record.get('stage') == 'repair_batch' and 'repair_assessment' not in record and
        delivery.guidance_attestation_sha256(record) == published.get('attestation_sha256'))
    rendered = delivery.render_guidance_attestation(packet, path.parent, state['pr_url'],
                                                    published.get('stage'), report=report,
                                                    receipt_context=published.get('required_reads'),
                                                    _allow_legacy_repair=legacy_repair_publication)
    if (published.get('body_sha256') != rendered['body_sha256'] or
            published.get('attestation_sha256') != rendered['attestation_sha256']):
        raise ValueError('Local guidance attestation changed after publication; render and publish the current stage again')
    if local_inspection:
        return
    executable = captured_gh(state)
    comment_deadline = time.monotonic() + 5
    if prior is not None:
        predecessor_timeout = comment_deadline - time.monotonic()
        if predecessor_timeout <= 0:
            raise ValueError('Combined inherited and current guidance comment read budget expired; current publication is unverified')
        check_guidance_publication_comment(prior, state, executable, 'Inherited guidance',
                                           timeout=predecessor_timeout)
    current_timeout = comment_deadline - time.monotonic()
    if current_timeout <= 0:
        raise ValueError('Combined inherited and current guidance comment read budget expired; current publication is unverified')
    remote = github.read_comment(published.get('comment_url', ''), state['pr_url'], executable,
                                timeout=current_timeout)
    if remote['reference']['comment_id'] != published.get('comment_id'):
        raise ValueError('Published guidance comment ID does not match its per-PR reference')
    if remote['body'] != rendered['body']:
        raise ValueError('Published guidance comment was edited or no longer matches the current attestation; republish a reviewed exact body')


def attestation_render(args):
    state = attestation_owner_state(read(args.cwd), args.cwd)
    packet, path = attestation_packet(args.evidence, state)
    stage = args.stage.replace('-', '_')
    report = None
    if stage in ('opening', 'successor_validation'):
        report = delivery.report(args.cwd, state['base_ref'], state['pr_url'], path)
        if report['snapshot']['base_ref'] != state['base_ref']:
            label = 'Successor validation' if stage == 'successor_validation' else 'Opening attestation'
            raise ValueError(label + ' report must match the current PG target binding')
    if stage == 'successor_validation':
        successor_predecessor(state, packet)
    owner = state['lane_owner']
    receipt_report = delivery.required_reads_status(state['root'], owner['harness'], owner['session_id'])
    receipt_context = delivery.require_required_read_receipts(receipt_report, packet, path.parent)
    rendered = delivery.render_guidance_attestation(packet, path.parent, state['pr_url'],
                                                    stage, report=report, receipt_context=receipt_context)
    if args.format == 'json':
        print(json.dumps(rendered, indent=2))
    else:
        print(rendered['body'], end='')
    return 0


def attestation_rereads(args):
    state = attestation_owner_state(read(args.cwd), args.cwd)
    owner = state['lane_owner']
    report = delivery.required_reads_status(state['root'], owner['harness'], owner['session_id'])
    print(json.dumps(delivery.required_read_packet_entries(report), indent=2, ensure_ascii=False))
    return 0


def attestation_start_check(args):
    ensure_no_upgrade_journal(args.cwd)
    adopted_now = False
    with locked(args.cwd) as state:
        attestation_owner_state(state, args.cwd)
        stage = args.stage.replace('-', '_')
        if 'attestation_required' in state and state.get('attestation_required') is not True:
            raise ValueError('Malformed attestation adoption state; there is no disable or fallback mode')
        if stage == 'successor_validation':
            if args.enable:
                raise ValueError('Successor validation cannot adopt or enable an attestation-required target')
            if state.get('attestation_required') is not True:
                raise ValueError('Successor validation is available only after the owner has adopted attestation requirements')
        elif state.get('attestation_required') is True and args.enable:
            raise ValueError('This owner lane is already adopted; omit --enable when binding another PR')
        if stage != 'successor_validation' and 'attestation_required' not in state and not args.enable:
            raise ValueError('This legacy target is unchanged; first owner-coordinated adoption requires --enable')
        packet, path = attestation_packet(args.evidence, state)
        delivery.guidance_attestation(packet, path.parent, state['pr_url'], stage)
        prior_publication = None
        same_owner_publication = None
        if stage == 'successor_validation':
            prior_publication, same_owner_publication = successor_predecessor(state, packet)
        owner = state['lane_owner']
        receipt_report = delivery.required_reads_status(state['root'], owner['harness'], owner['session_id'])
        receipt_context = delivery.require_required_read_receipts(receipt_report, packet, path.parent)
        report = None
        if stage in ('opening', 'successor_validation'):
            report = delivery_runs.check(state.get('receipt'), args.cwd, state['pr_url'])
            if report['snapshot']['base_ref'] != state['base_ref']:
                label = 'Successor validation' if stage == 'successor_validation' else 'Opening attestation'
                raise ValueError(label + ' report must match the current PG target binding')
            if not delivery.opening_report_ready(report):
                label = 'Successor validation' if stage == 'successor_validation' else 'Opening attestation'
                raise ValueError(label + ' requires a current clean PG report with successful canonical verification')
        rendered = delivery.render_guidance_attestation(packet, path.parent, state['pr_url'],
                                                        stage, report=report, receipt_context=receipt_context)
        executable = captured_gh(state)
        pr_ref, remote_pr_record = remote_pr(args.cwd, state['pr_url'], state['base_ref'],
                                             state.get('head_branch'), executable=executable)
        if report is not None:
            github.check_published_snapshot(pr_ref, remote_pr_record, report['snapshot'], executable=executable)
        if prior_publication is not None:
            check_guidance_publication_comment(prior_publication, state, executable, 'Inherited guidance')
        remote_comment = github.read_comment(args.comment_url, state['pr_url'], executable)
        if remote_comment['body'] != rendered['body']:
            raise ValueError('Published comment body does not match the exact current rendered attestation')
        comment_ref = remote_comment['reference']
        if prior_publication is not None and comment_ref['url'] == prior_publication['comment_url']:
            raise ValueError('Successor validation must use a new comment and preserve the inherited publication')
        if (same_owner_publication and comment_ref['url'] == same_owner_publication.get('comment_url') and
                (rendered['body_sha256'] != same_owner_publication.get('body_sha256') or
                 rendered['attestation_sha256'] != same_owner_publication.get('attestation_sha256'))):
            raise ValueError('A changed successor validation must use a new comment and preserve the existing publication')
        if same_owner_publication and comment_ref['url'] == same_owner_publication.get('comment_url'):
            previous_reads = same_owner_publication.get('required_reads')
            current_reads = rendered.get('required_reads')
            if previous_reads is None and current_reads is None:
                pass
            elif (isinstance(previous_reads, dict) and isinstance(current_reads, dict) and
                  all(previous_reads.get(key) == current_reads.get(key)
                      for key in ('list_sha256', 'generation', 'entries'))):
                rendered['required_reads'] = copy.deepcopy(previous_reads)
            else:
                raise ValueError('Successor required-read context changed; publish a new comment and preserve the existing publication')
        publication = {
            'version': 1,
            'pr_url': pr_ref['url'],
            'branch': state['branch'],
            'head_branch': state.get('head_branch', state['branch']),
            'base_ref': state['base_ref'],
            'owner': {key: state['lane_owner'][key] for key in ('harness', 'session_id')},
            'comment_url': comment_ref['url'],
            'comment_id': comment_ref['comment_id'],
            'stage': rendered['stage'],
            'body_sha256': rendered['body_sha256'],
            'attestation_sha256': rendered['attestation_sha256'],
        }
        if prior_publication is not None:
            publication['prior_publication_ref'] = copy.deepcopy(prior_publication)
        if 'required_reads' in rendered:
            publication['required_reads'] = rendered['required_reads']
        if 'attestation_required' not in state:
            state['attestation_required'] = True
            adopted_now = True
        state['guidance_attestation_ref'] = publication
    print(json.dumps({'status': 'published_attestation_current', 'adopted': adopted_now,
                      'publication': publication}, indent=2))
    return 0


def require(state, cwd, event=None, *, local_inspection=False):
    if state and state.get('lane_owner'):
        lane_scope.check_owner(state,event=event)
        check_lane_scope(state, cwd)
    if not active(state, cwd):
        raise ValueError(enrollment_help(cwd))
    if state['source_versions'] != source_versions(state.get('lane_owner', {}).get('harness')):
        raise ValueError('Pinned delivery adapter source changed; restore the reviewed source')
    if 'attestation_required' in state:
        if state.get('attestation_required') is not True:
            raise ValueError('Malformed attestation adoption state; there is no disable or fallback mode')
        if state.get('lane_owner', {}).get('harness') not in ('claude', 'codex'):
            raise ValueError('Published guidance attestation checks are qualified only for the supported Claude and Codex delivery paths')
        require_guidance_attestation(state, cwd, local_inspection=local_inspection)
    try:
        report=delivery_runs.check(state.get('receipt'),cwd,state['pr_url'])
        if report['snapshot']['base_ref']!=state['base_ref']:
            raise ValueError('Published PR target ref changed; record a report for the selected target')
        return report
    except Exception as error:
        command = shlex.join([sys.executable, str(SELF), '--cwd', str(root(cwd)), 'record',
                              '--evidence', str(directory(cwd) / 'delivery-evidence.json')])
        raise ValueError(str(error) + '. Run ' + command + ' using the actual packet path. '
                         'Missing checklist items are allowed; report execution is required.') from error


def remote_pr(cwd, pr_url, base_ref, head_branch=None, *, observed=None, executable=None):
    ref = github.reference(pr_url, 'pull')
    repo = github.origin_repo(delivery.value(cwd, 'remote', 'get-url', 'origin'), ref['host'])
    if repo.casefold() != ref['repo'].casefold():
        raise ValueError('Delivery PR does not match worktree origin')
    pr = observed if observed is not None else github.request(ref, executable=executable)
    target = github.target_branch(base_ref)
    if (pr.get('state') != 'open' or pr.get('head', {}).get('ref') != (head_branch or branch(cwd)) or
            pr.get('base', {}).get('ref') != target or
            any(pr.get(side, {}).get('repo', {}).get('full_name', '').casefold() != repo.casefold()
                for side in ('head', 'base'))):
        raise ValueError('Open same-repository PR, worktree branch and target must agree')
    return ref, pr


def check_lane_identity(state, cwd):
    owner=state['lane_owner']
    found = session_store.lookup_checkout(root(cwd))
    anchor = None
    if state.get('seat_id'):
        seat = session_store.load_seat(state['seat_id'])
        if not seat:
            raise ValueError('The permanent seat registry is missing; preserve its history and inspect recovery')
        anchor = active_operational_anchor(seat)
        anchor_root = anchor['root']
        anchor_gitdir = anchor['gitdir']
    else:
        anchor_root = state.get('session_root', state['root'])
        anchor_gitdir = owner.get('seat_gitdir')
    if (state['phase']!='active' or state['root']!=str(root(cwd)) or
            owner['product_gitdir']!=delivery.value(cwd,'rev-parse','--absolute-git-dir') or
            state.get('session_root') != anchor_root or
            owner.get('seat_root', state.get('session_root')) != anchor_root or
            owner.get('seat_gitdir') != anchor_gitdir or
            anchor_gitdir != delivery.value(anchor_root,'rev-parse','--absolute-git-dir') or
            (state.get('seat_id') and (not found or found['seat']['seat_id']!=state['seat_id'])) or
            github.origin_repo(delivery.value(cwd,'remote','get-url','origin'),owner['host']).casefold()!=owner['repository']):
        raise ValueError('Enrolled product/seat path or repository changed; preserve registration history and coordinate an explicit path repair')


def check_lane_scope(state, cwd):
    check_lane_identity(state, cwd)
    if state['source_versions']!=source_versions(state.get('lane_owner', {}).get('harness')): raise ValueError('Pinned delivery adapter source changed')


def discover(cwd):
    ensure_no_upgrade_journal(cwd)
    initial=read(cwd)
    if not initial or not initial.get('lane_owner'): raise ValueError('No lane enrollment; review plan --lane and install once first')
    lane_scope.check_owner(initial);check_lane_scope(initial,cwd)
    current=branch(cwd)
    remote=lane_scope.published_pr(initial,current)
    ref=github.reference(remote['html_url'],'pull')
    if ref['host']!=initial['lane_owner']['host'] or ref['repo']!=initial['lane_owner']['repository']:
        raise ValueError('Discovered PR URL differs from enrolled repository')
    target=remote.get('base',{}).get('ref')
    if not isinstance(target,str) or not target: raise ValueError('Published PR target unavailable')
    base_ref='refs/remotes/origin/'+target
    delivery.value(cwd,'rev-parse','--verify',base_ref+'^{commit}')
    binding={'branch':current,'head_branch':remote['head']['ref'],'pr_url':ref['url'],'base_ref':base_ref}
    with locked(cwd) as state:
        lane_scope.check_owner(state);check_lane_scope(state,cwd)
        if state['lane_owner']!=initial['lane_owner'] or branch(cwd)!=current:
            raise ValueError('Lane owner or product branch changed during discovery; retry')
        if state.get('seat_id') and ref['url'] not in state.get('pr_bindings', {}):
            historical=[]
            for item in session_store.all_checkouts(state['seat_id']):
                prior=item.get('state')
                if not prior or prior.get('lane_owner',{}).get('repository','').casefold()!=state['lane_owner']['repository'].casefold():
                    continue
                historical.extend((prior.get('root'),row) for row in lane_scope.expand(prior)
                                  if row.get('pr_url','').casefold()==ref['url'].casefold())
            distinct={json.dumps(row,sort_keys=True) for _,row in historical}
            if len(distinct)>1:
                raise ValueError('Existing PR history is linked to multiple checkouts with different records; preserve it and use recover --inspect')
            if historical:
                prior_root,prior_binding=historical[0]
                restored=copy.deepcopy(prior_binding)
                if prior_root!=state['root'] and restored.get('receipt'):
                    restored.setdefault('history',[]).append(restored.pop('receipt'))
                binding={**restored,**binding}
        lane_scope.select_binding(state,binding)
        result=copy.deepcopy(state)
    return {'discovered':True,'pr_url':ref['url'],'branch':current,'base_ref':base_ref,'state':result,
            'notice':'Binding only; no verification, report, fix-round reset, readiness or merge approval.'}


def rebind_owner(args):
    if args.seat_id:
        identifier=args.seat_id
        identity=lane_scope.native_identity()
        with session_store.locked_seat(identifier):
            seat=session_store.load_seat(identifier)
            if not seat:
                raise ValueError('Permanent seat registry is missing; preserve its history and inspect recovery')
            anchor = active_operational_anchor(seat)
            if (session_store.seat_dir(identifier)/UPGRADE_JOURNAL).exists() or \
                    (session_store.seat_dir(identifier)/UPGRADE_JOURNAL).is_symlink():
                raise ValueError('A seat source upgrade is incomplete; recover it before transferring ownership')
            if seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal'):
                raise ValueError('Seat transfer is unavailable during an interrupted install or migration; inspect and recover first')
            previous={'harness':seat['owner'].get('harness'),'session_id':seat['owner'].get('session_id')}
            if previous!={'harness':args.from_harness,'session_id':args.from_session}:
                raise ValueError('Expected outgoing owner does not match the permanent seat; inspect status before handoff')
            previous_context=copy.deepcopy(seat.get('owner_context',{}))
            new_context=lane_scope.native_session_context()
            if identity==previous and previous_context==new_context:
                return {'rebound':False,'reason':'same native owner','history_preserved':True}
            transition={'from':previous,'to':identity,'from_context':previous_context,
                        'to_context':new_context,'at':time.time(),
                        'authority':'caller explicitly confirms an authorized handoff; not independently authenticated'}
            if seat.get('handoff_journal'):
                raise ValueError('A previous owner handoff is incomplete; use recover --rollback before another transfer')
            checkout_rows = session_store.all_checkouts(identifier)
            runtime = permanent_handoff_runtime(seat)
            same_harness = previous['harness'] == identity['harness']
            outgoing_versions = (runtime_source_versions(runtime, previous['harness']) if same_harness
                                 else source_versions(previous['harness']))
            for item in checkout_rows:
                state = item.get('state')
                if state is not None and not isinstance(state,dict):
                    raise ValueError('Checkout state is malformed; preserve its history and inspect status before transfer')
                if not state:
                    continue
                if state.get('phase') == 'removed':
                    continue
                pr_bindings=state.get('pr_bindings',{})
                if (not isinstance(pr_bindings,dict) or
                        any(not isinstance(url,str) or not isinstance(binding,dict)
                            for url,binding in pr_bindings.items()) or
                        (state.get('pr_url') is not None and not isinstance(state.get('pr_url'),str))):
                    raise ValueError('Checkout PR history map is malformed; preserve it and inspect status before transfer')
                state_owner = state.get('lane_owner')
                if (not isinstance(state_owner, dict) or
                        {key: state_owner.get(key) for key in ('harness', 'session_id')} != previous):
                    raise ValueError('Permanent seat and checkout owners differ; preserve history and inspect status')
                if state.get('source_versions') != outgoing_versions:
                    raise ValueError('Pinned delivery adapter source changed; restore the reviewed source')
            incoming_versions = (runtime_source_versions(runtime, identity['harness']) if same_harness
                                 else permanent_handoff_source_versions(seat, identity['harness']))
            changed_attachments=[]
            retained_attachments=[]
            path_state_by_checkout={}
            for item in checkout_rows:
                state=item.get('state') or {}
                path_state=checkout_path_availability(item)
                path_state_by_checkout[item.get('checkout_id')]=path_state
                bindings=state.get('pr_bindings')
                pr_refs=sorted((set(bindings) if isinstance(bindings,dict) else set()) |
                               ({state['pr_url']} if isinstance(state.get('pr_url'),str) else set()))
                pr_history_intact=('pr_bindings' not in state or (isinstance(bindings,dict) and
                    all(isinstance(url,str) and isinstance(binding,dict)
                        for url,binding in bindings.items())))
                disposition={'checkout_id':item.get('checkout_id'),'root':item.get('root'),
                             'phase':state.get('phase','missing-or-malformed-state'),
                             'branch':state.get('branch'),'pr_refs':pr_refs,
                             'pr_history_state':'intact' if pr_history_intact else 'malformed-preserved',
                             'worktree_path_state':path_state}
                if not item.get('state'):
                    retained_attachments.append({**disposition,'disposition':'retained',
                                                 'reason':'state-unavailable'})
                elif state.get('phase') == 'removed':
                    retained_attachments.append({**disposition,'disposition':'retained',
                                                 'reason':'retired-history-preserved'})
                else:
                    changed_attachments.append({**disposition,'disposition':'rebound',
                                                'reason':'owner-and-source-pin-updated'})
            target_groups=copy.deepcopy(seat.get('groups',{}))
            target_hooks_installed=bool(seat.get('hooks_installed'))
            target_settings=copy.deepcopy(seat.get('settings'))
            settings_change=None
            if previous['harness']!=identity['harness']:
                settings_path=None
                current_raw=''
                if previous['harness']=='claude' and seat.get('hooks_installed'):
                    settings_path,current_raw,current_settings=settings(anchor['root'])
                    if str(settings_path)!=(seat.get('settings') or {}).get('path'):
                        raise ValueError('Recorded Claude seat settings path changed; inspect recovery before transfer')
                    for event,group in seat.get('groups',{}).items():
                        groups=current_settings.get('hooks',{}).get(event,[])
                        if not isinstance(groups,list) or groups.count(group)!=1:
                            raise ValueError('A managed Claude seat hook is missing or duplicated; inspect settings before transfer')
                    remove={event:[group] for event,group in seat.get('groups',{}).items()}
                    change=migration_settings_change(settings_path,remove,{})
                    settings_change={key:change[key] for key in ('path','preimage','preimage_sha256',
                        'preimage_exists','mode','updated','updated_sha256')}
                    updated_value=json.loads(settings_change['updated'])
                    hooks=updated_value.get('hooks',{})
                    original_events=(seat.get('settings') or {}).get('original_hook_events',[])
                    for event in remove:
                        if not hooks.get(event) and event not in original_events:
                            hooks.pop(event,None)
                    if not hooks and not (seat.get('settings') or {}).get('original_had_hooks'):
                        updated_value.pop('hooks',None)
                    settings_change['updated']=json.dumps(updated_value,indent=2)+'\n'
                    settings_change['updated_sha256']=hashlib.sha256(
                        settings_change['updated'].encode()).hexdigest()
                    target_groups={}
                    target_hooks_installed=False
                elif identity['harness']=='claude':
                    settings_path,current_raw,current_settings=settings(anchor['root'])
                    runtime_cli=Path(seat['runtime']['path'])/'lane.py'
                    target_groups=hook_groups(anchor['root'],anchor['root'],
                        seat_identifier=identifier,runtime_cli=runtime_cli)
                    add={event:[group] for event,group in target_groups.items()}
                    change=migration_settings_change(settings_path,{},add)
                    settings_change={key:change[key] for key in ('path','preimage','preimage_sha256',
                        'preimage_exists','mode','updated','updated_sha256')}
                    raw_value=json.loads(current_raw) if current_raw else {}
                    backup=session_store.seat_dir(identifier)/'settings-backups'/(
                        hashlib.sha256(current_raw.encode()).hexdigest()+'.json')
                    target_settings={'path':str(settings_path),'backup':str(backup),
                        'sha256':hashlib.sha256(current_raw.encode()).hexdigest(),
                        'existed':settings_path.exists(),'mode':change['mode'],
                        'original_hook_events':list(raw_value.get('hooks',{})),
                        'original_had_hooks':'hooks' in raw_value}
                    settings_change['backup_path']=str(backup)
                    target_hooks_installed=True
            state_changes=[]
            for item in checkout_rows:
                state=item.get('state')
                if not state:
                    continue
                if state.get('phase') == 'removed':
                    continue
                path=Path(item['checkout_dir'])/'state.json'
                if not path.resolve().is_relative_to(session_store.seat_dir(identifier).resolve()):
                    raise ValueError('Seat checkout registry points outside its owned state directory')
                refuse_symlinked_path(path)
                preimage=path.read_text()
                pre_state=copy.deepcopy(state)
                current_pr=pre_state.get('pr_url')
                pre_state.setdefault('owner_history',[]).append(transition)
                owner=pre_state.setdefault('lane_owner',{})
                for key in ('session_parent_id','session_project_id','session_directory'):
                    owner.pop(key,None)
                owner.update(identity)
                owner.update(new_context)
                pre_state['source_versions'] = copy.deepcopy(incoming_versions)
                bindings=lane_scope.expand(pre_state)
                records={}
                for binding in bindings:
                    if binding.get('receipt'):
                        binding.setdefault('history',[]).append(binding.pop('receipt'))
                    if binding.get('pr_url'):
                        records[binding['pr_url']]={key:copy.deepcopy(binding[key])
                            for key in lane_scope.BINDING_FIELDS if key in binding}
                pre_state['pr_bindings']=records
                if current_pr in records:
                    for key in lane_scope.BINDING_FIELDS: pre_state.pop(key,None)
                    pre_state.update(copy.deepcopy(records[current_pr]))
                pre_state.pop('receipt',None)
                pre_state['updated_at']=time.time()
                postimage=json.dumps(pre_state,indent=2)+'\n'
                state_changes.append({'path':str(path),'preimage':preimage,
                    'preimage_sha256':hashlib.sha256(preimage.encode()).hexdigest(),
                    'postimage':postimage,'postimage_sha256':hashlib.sha256(postimage.encode()).hexdigest()})
            seat_preimage=copy.deepcopy(seat)
            changed=len(state_changes)
            settings_receipt={'path':(settings_change['path'] if settings_change else
                                      (target_settings or {}).get('path')),
                'changed':bool(settings_change),
                'before_sha256':settings_change.get('preimage_sha256') if settings_change else None,
                'after_sha256':settings_change.get('updated_sha256') if settings_change else None,
                'before_exists':settings_change.get('preimage_exists') if settings_change else None,
                'after_exists':True if settings_change else None,
                'mode':settings_change.get('mode') if settings_change else (target_settings or {}).get('mode'),
                'hook_events_before':sorted((seat_preimage.get('groups') or {}).keys()),
                'hook_events_after':sorted(target_groups.keys()),
                'hook_groups_before':copy.deepcopy(seat_preimage.get('groups') or {}),
                'hook_groups_after':copy.deepcopy(target_groups),
                'notice':('Hashes cover the exact settings bytes before and after this handoff.'
                          if settings_change else
                          'No settings write occurred; the recorded baseline hash is not a current-content observation.')}
            coverage_commands=[]
            for item in checkout_rows:
                state=item.get('state') or {}
                if (state.get('phase') != 'active' or
                        path_state_by_checkout.get(item.get('checkout_id')) != 'present'):
                    continue
                coverage_commands.append({'checkout_id':item.get('checkout_id'),'root':item.get('root'),
                    'command':shlex.join([sys.executable,str(Path(runtime['path'])/'lane.py'),
                                          '--cwd',item['root'],'coverage','--summary'])})
            fresh_report={'required':True,'checkouts':[row['checkout_id'] for row in changed_attachments
                    if row['phase'] == 'active' and row['pr_refs']],
                'reason':'Prior receipts were archived where present; each active rebound checkout with a covered PR needs a fresh report before covered actions can pass.'}
            verification_receipt={'configured':{'hooks_installed':target_hooks_installed,
                    'settings_path':settings_receipt['path'],'hook_events':sorted(target_groups.keys()),
                    'adapter_path':seat_preimage.get('adapter')},
                'observed':{'state':'not-observed-by-rebind',
                    'notice':'Rebind records local configuration only; it does not show that the incoming native runtime loaded or intercepted a hook.'},
                'coverage_commands':coverage_commands}
            seat['handoff_journal']={'kind':'owner-handoff','from':previous,'to':identity,
                'seat_preimage':seat_preimage,'state_changes':state_changes,
                'settings_change':settings_change,'target_groups':target_groups,
                'target_hooks_installed':target_hooks_installed,'target_settings':target_settings,
                'created_at':time.time()}
            session_store.save_seat(seat)
            for change in state_changes:
                write(change['path'],change['postimage'])
            if settings_change:
                path=Path(settings_change['path'])
                refuse_symlinked_path(path)
                current=path.read_text() if path.exists() else ''
                if (hashlib.sha256(current.encode()).hexdigest()!=settings_change['preimage_sha256'] or
                        path.exists()!=settings_change['preimage_exists']):
                    raise ValueError('Claude settings changed during native-session transfer; preserve the journal and inspect recovery')
                if settings_change.get('backup_path'):
                    write(settings_change['backup_path'],settings_change['preimage'])
                write(path,settings_change['updated'],settings_change['mode'])
            seat=session_store.load_seat(identifier)
            seat.setdefault('owner_history',[]).append(transition)
            seat['owner']=identity
            seat['owner_context']=new_context
            seat['groups']=target_groups
            seat['hooks_installed']=target_hooks_installed
            seat['settings']=target_settings
            seat.pop('handoff_journal',None)
            seat['updated_at']=time.time()
            session_store.save_seat(seat)
        return {'rebound':True,'owner':identity,'history_preserved':True,'report_required':True,
                'checkouts_rebound':changed,'changed_attachments':changed_attachments,
                'retained_attachments':retained_attachments,'settings_receipt':settings_receipt,
                'fresh_report':fresh_report,'verification':verification_receipt,
                'notice':'Explicit seat-wide handoff from the permanent seat registry. Deleted product Git directories were not read.'}
    current=read(args.cwd) or {}
    if current.get('seat_id'):
        args.seat_id=current['seat_id']
        return rebind_owner(args)
    ensure_no_upgrade_journal(args.cwd)
    identity=lane_scope.native_identity()
    current=read(args.cwd) or {}
    with locked(args.cwd) as state:
        if not state.get('lane_owner'): raise ValueError('No lane enrollment to hand off')
        check_lane_scope(state,args.cwd)
        previous={k:state['lane_owner'][k] for k in ('harness','session_id')}
        if previous!={'harness':args.from_harness,'session_id':args.from_session}:
            raise ValueError('Expected outgoing owner does not match; inspect status before handoff')
        incoming_versions=source_versions(identity['harness'])
        if any(state['source_versions'].get(name)!=digest for name,digest in incoming_versions.items()):
            raise ValueError('Incoming delivery adapter source is not covered by the outgoing pin; review plan --lane and install a permanent seat, then hand off using its installed runtime')
        previous_context={key:state['lane_owner'].get(key) for key in
                          ('session_parent_id','session_project_id','session_directory') if key in state['lane_owner']}
        new_context=lane_scope.native_session_context()
        if identity==previous and previous_context==new_context:
            return {'rebound':False,'reason':'same native owner','history_preserved':True}
        state.setdefault('owner_history',[]).append({'from':previous,'to':identity,
                'from_context':previous_context,'to_context':new_context,'at':time.time(),
                'authority':'caller explicitly confirms an authorized handoff; not independently authenticated'})
        for key in ('session_parent_id','session_project_id','session_directory'):
            state['lane_owner'].pop(key,None)
        state['lane_owner'].update(identity)
        state['lane_owner'].update(new_context)
        state['source_versions']=incoming_versions
        # Keep reusable source verification artifacts, but require this owner to
        # record its own report rather than inheriting an execution receipt.
        bindings=lane_scope.expand(state)
        for binding in bindings:
            if binding.get('receipt'): binding.setdefault('history',[]).append(binding.pop('receipt'))
        state['pr_bindings']={binding['pr_url']:{k:binding[k] for k in lane_scope.BINDING_FIELDS if k in binding} for binding in bindings}
        if state.get('receipt'): state.setdefault('history',[]).append(state.pop('receipt'))
    return {'rebound':True,'owner':identity,'history_preserved':True,'report_required':True,
            'notice':'Explicit delivery-only ownership rebind; no hook activation or expanded enforcement.'}


def hook_groups(cwd, session_root=None, *, seat_identifier=None, runtime_cli=None):
    if seat_identifier and runtime_cli:
        command = shlex.join([sys.executable, str(runtime_cli), '--seat-id', seat_identifier])
    else:
        command = shlex.join([sys.executable, str(SELF), '--cwd', str(root(cwd))])
        if session_root and root(session_root) != root(cwd):
            command += ' --session-root ' + shlex.quote(str(root(session_root)))
    return {
        'PreToolUse': {'matcher': '^Bash$', 'hooks': [
            {'type': 'command', 'command': command + ' hook', 'timeout': 10, 'onFailure': 'block'}]},
        'SessionStart': {'hooks': [
            {'type': 'command', 'command': command + ' session', 'timeout': 5}]}
    }


def adapter_script(cwd, *, seat_identifier=None, runtime_cli=None, real_gh=None):
    if seat_identifier and runtime_cli:
        command = [sys.executable, str(runtime_cli), '--seat-id', seat_identifier, 'gh', '--']
        prefix = ('POLICY_GUARD_REAL_GH=' + shlex.quote(str(real_gh)) + ' ' if real_gh else '')
    else:
        command = [sys.executable, str(SELF), '--installation', str(root(cwd)), 'gh', '--']
        prefix = ''
    return '#!/bin/sh\n# Generated by the delivery-only lane installer.\n' + prefix + 'exec ' + shlex.join(command) + ' "$@"\n'


def passthrough(binary):
    return '#!/bin/sh\n# Generated delivery-lane rollback adapter.\nexec ' + shlex.quote(binary) + ' "$@"\n'


def registered_states(initial, include_upgrade_preimages=False):
    if initial and initial.get('seat_id'):
        rows = [item['state'] for item in session_store.all_checkouts(initial['seat_id']) if item.get('state')]
        return [binding for state in rows for binding in
                (lane_scope.expand(state) if state.get('lane_owner') else [state])
                if state.get('phase') in ('active', 'installing')]
    common = Path(delivery.value(initial['root'], 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    paths = [common / 'starfleet-delivery-lane/state.json', *common.glob('worktrees/*/starfleet-delivery-lane/state.json')]
    states = []
    for path in paths:
        try:
            value = json.loads(path.read_text()) if path.exists() else None
        except (ValueError, OSError):
            value = None  # An unrelated damaged registration must not block every lane.
        if value and value.get('phase') in ('active', 'installing'):
            states.extend(lane_scope.expand(value))
        journal_path = path.with_name(UPGRADE_JOURNAL)
        if include_upgrade_preimages and (journal_path.exists() or journal_path.is_symlink()):
            try:
                journal = json.loads(journal_path.read_text())
                image = journal['state']['preimage']
                journal_state = json.loads(image['text'])
                expected_state_path = upgrade_journal_path(journal_state['root']).with_name('state.json')
                if (image.get('path') != str(path) or journal.get('root') != journal_state.get('root') or
                        expected_state_path != path):
                    raise ValueError('Registered upgrade journal identity differs from its state file')
            except (OSError, KeyError, TypeError, ValueError):
                # A foreign damaged registration must not stop unrelated PRs.
                # Its own adapter still fails closed in state_for_upgrade_guard.
                continue
            states.extend(lane_scope.expand(journal_state))
    return states


def settings(cwd):
    path = root(cwd) / '.claude/settings.local.json'
    refuse_symlinked_path(path)
    if delivery.git(cwd, 'ls-files', '--error-unmatch', '.claude/settings.local.json').returncode == 0:
        raise ValueError('Refusing tracked settings; this installer only changes lane-local configuration')
    raw = path.read_text() if path.exists() else ''
    if 'do not edit' in raw.lower() or 'auto-generated' in raw.lower():
        raise ValueError('Generated settings must be changed through their owning source/generator')
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict) or not isinstance(value.get('hooks', {}), dict):
        raise ValueError('Unsupported existing Claude settings shape')
    for event in hook_groups(cwd):
        if not isinstance(value.get('hooks', {}).get(event, []), list):
            raise ValueError('Existing hook groups must be lists')
    return path, raw, value


def plan(args):
    if not args.lane and (args.owner_harness is not None or args.owner_session is not None):
        raise ValueError('--owner-harness and --owner-session require --lane; use plan/install --lane for native-owner '
                         'enrollment, or omit owner flags for a legacy per-PR installation')
    cwd = root(args.cwd)
    owner = root(args.session_root or cwd)
    selected_manifest = None
    if args.lane and args.seat_id:
        selected_manifest = session_store.load_seat(args.seat_id)
        if not selected_manifest:
            raise ValueError('The exact --seat-id does not identify a permanent seat')
        ensure_no_seat_retirement_journal(args.seat_id)
        selected_anchor = active_operational_anchor(selected_manifest)
        if args.session_root:
            if str(owner) != selected_anchor['root']:
                raise ValueError('The supplied session root differs from the exact seat operational anchor')
        else:
            owner = root(selected_anchor['root'])
    ensure_no_upgrade_journal(cwd)
    if not args.lane and delivery.value(owner, 'rev-parse', '--path-format=absolute', '--git-common-dir') != delivery.value(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir'):
        raise ValueError('Session root and product worktree must belong to the same repository')
    previous = read(cwd)
    reattach_same_root=False
    if previous and previous.get('phase') != 'removed':
        if (args.lane and previous.get('phase')=='active' and
                previous.get('seat_id')==(args.seat_id or session_store.seat_id(owner))
                and previous.get('root')==str(cwd) and previous.get('lane_owner') and
                previous['lane_owner'].get('product_gitdir')!=delivery.value(cwd,'rev-parse','--absolute-git-dir')):
            seat=session_store.load_seat(previous['seat_id'])
            actual=lane_scope.native_identity()
            if not seat or seat.get('owner')!=actual or previous.get('lane_owner',{}).get('session_id')!=actual['session_id'] or \
                    previous.get('lane_owner',{}).get('harness')!=actual['harness'] or \
                    (actual['harness']=='opencode' and lane_scope.native_session_context()!=seat.get('owner_context')):
                raise ValueError('Reattaching a recreated checkout requires the exact current seat owner')
            reattach_same_root=True
        else:
            raise ValueError('Installation already exists; inspect status or uninstall before changing it')
    if not source_is_immutable_runtime() and delivery.value(APP, 'status', '--porcelain'):
        raise ValueError('Commit and verify the installer source, then use a clean pinned checkout')
    lane_owner = None
    seat = None
    runtime = None
    runtime_cli = None
    seat_identifier = None
    if args.lane:
        if bool(args.pr_url) != bool(args.base_ref):
            raise ValueError('An explicit lane PR binding requires both --pr-url and --base-ref')
        if args.head_branch and not args.pr_url:
            raise ValueError('--head-branch requires an explicit --pr-url and --base-ref')
        if not args.owner_harness or not args.owner_session:
            raise ValueError('--lane requires --owner-harness and --owner-session from the actual native owner')
        identity = lane_scope.native_identity()
        if identity != {'harness':args.owner_harness,'session_id':args.owner_session}:
            raise ValueError('Declared lane owner must match the actual native session')
        seat_identifier = args.seat_id or session_store.seat_id(owner)
        seat = selected_manifest or session_store.seat_manifest(seat_identifier)
        if seat:
            selected_anchor = active_operational_anchor(seat)
            if selected_anchor['root'] != str(owner):
                raise ValueError('This seat has no matching active operational anchor; select its exact --seat-id and anchor')
        if seat and seat.get('owner') != identity:
            raise ValueError('This permanent seat belongs to another native session. Use an explicitly authorized rebind-owner handoff.')
        owner_context=lane_scope.native_session_context()
        if identity['harness']=='opencode' and seat and seat.get('owner_context')!=owner_context:
            raise ValueError('This permanent seat belongs to a different OpenCode session API project or location. Use an explicitly authorized rebind-owner handoff.')
        runtime = session_store.runtime_plan(APP)
        if seat and seat.get('runtime', {}).get('version') != runtime['version']:
            raise ValueError('The permanent seat has a different immutable runtime version. Use its supported source-upgrade or recovery path before attaching another checkout.')
        runtime_cli = Path(runtime['path']) / 'lane.py'
        repository = github.origin_repo(delivery.value(cwd,'remote','get-url','origin'),args.host).casefold()
        lane_owner = dict(identity, host=args.host, repository=repository,
                          seat_root=str(owner),
                          product_gitdir=delivery.value(cwd,'rev-parse','--absolute-git-dir'),
                          seat_gitdir=delivery.value(owner,'rev-parse','--absolute-git-dir'))
        lane_owner.update(lane_scope.native_session_context())
        ref={'url':None}
        if args.pr_url:
            ref, _ = remote_pr(cwd, args.pr_url, args.base_ref, args.head_branch)
            delivery.snapshot(cwd, args.base_ref)
    else:
        if not args.pr_url or not args.base_ref: raise ValueError('Per-PR installation requires --pr-url and --base-ref')
        ref, _ = remote_pr(cwd, args.pr_url, args.base_ref, args.head_branch)
        delivery.snapshot(cwd, args.base_ref)
    manages_claude_hooks=not lane_owner or lane_owner['harness']=='claude'
    if manages_claude_hooks: path,raw,current_settings=settings(owner)
    else: path,raw=root(owner)/'.claude/settings.local.json',''
    binary = shutil.which('gh')
    if previous and binary and Path(binary).absolute() == Path(previous['adapter']).absolute():
        binary = previous['real_gh']
    if args.lane and seat and binary and Path(binary).resolve().is_relative_to(session_store.seat_dir(seat_identifier).resolve()):
        binary = seat.get('real_gh', {}).get('path') or seat.get('real_gh')
    if not binary:
        raise ValueError('Resolve the existing GitHub CLI before adding the delivery adapter')
    captured = captured_binary_identity(binary)
    managed_roots = [session_store.runtime_root().resolve(), session_store.state_root().resolve()]
    if any(Path(captured['resolved_path']).is_relative_to(path) for path in managed_roots):
        raise ValueError('Refusing to capture a Policy Guard runtime or nested/stale adapter as the real GitHub CLI')
    if not args.lane and Path(captured['resolved_path']).is_relative_to(directory(cwd).resolve()):
        raise ValueError('Resolve the existing GitHub CLI before adding the delivery adapter')
    groups = {}
    hook_install_needed = False
    if manages_claude_hooks:
        if args.lane and seat and seat.get('hooks_installed'):
            groups = copy.deepcopy(seat.get('groups', {}))
            stored_settings = seat.get('settings')
            if not isinstance(stored_settings, dict):
                raise ValueError('The permanent seat settings metadata is malformed; use recover --inspect before attaching another checkout')
            stored_path = stored_settings.get('path')
            if stored_path != str(path):
                raise ValueError('The permanent seat settings path changed; use the supported seat recovery path')
            for event, group in groups.items():
                existing = current_settings.get('hooks', {}).get(event, [])
                if existing.count(group) != 1:
                    raise ValueError('The installed seat hook is missing or changed; use recover --inspect before attaching another checkout')
        else:
            groups = hook_groups(cwd, owner, seat_identifier=seat_identifier,
                                 runtime_cli=runtime_cli) if args.lane else hook_groups(cwd, owner)
            hook_install_needed = bool(args.lane)
    adapter = str(session_store.seat_dir(seat_identifier) / 'bin/gh') if args.lane else str(directory(cwd) / 'bin/gh')
    adapter_body = adapter_script(cwd, seat_identifier=seat_identifier,
                                   runtime_cli=runtime_cli, real_gh=captured['path']) if args.lane else adapter_script(cwd)
    if lane_owner and lane_owner['harness'] == 'opencode':
        boundary = ('OpenCode 2.2.5 server plugin prototype, native permission assertion, and covered readiness/merge tool only; '
                    'plain direct shell command segments are refused, while alternate APIs and opaque shell routes remain outside coverage')
    elif manages_claude_hooks:
        boundary = ('Claude lane-local delivery hooks and gh adapter only; adopted attestation targets perform bounded GitHub comment reads; '
                    'no Git/Codex/CPU hooks or fix-round enrollment')
    else:
        boundary = 'Private seat delivery gh adapter only; no native hooks, resource hooks or fix-round enrollment'
    result = {'root': str(cwd), 'session_root': str(owner), 'branch': branch(cwd),
              'head_branch': args.head_branch or branch(cwd), 'pr_url': ref['url'], 'base_ref': args.base_ref,
              'settings': str(path), 'settings_sha256': hashlib.sha256(raw.encode()).hexdigest(),
              'settings_existed': path.exists() if manages_claude_hooks else False,
              'manages_claude_hooks':manages_claude_hooks,'groups':groups,
              'hook_install_needed':hook_install_needed,
              'settings_mode':(path.stat().st_mode & 0o777) if manages_claude_hooks and path.exists() else 0o600,
              'previous_state_sha256': delivery_runs.digest(directory(cwd) / 'state.json') if previous else None,
              'reattach_same_root':reattach_same_root,
              'real_gh': captured['path'], 'real_gh_identity':captured,
              'source': str(Path(runtime['path']) if args.lane else APP),
              'source_revision': source_revision(runtime['version'] if runtime else None),
              'source_versions': source_versions(lane_owner.get('harness') if lane_owner else None),
              'adapter': adapter,
              'adapter_sha256': hashlib.sha256(adapter_body.encode()).hexdigest(),
              'boundary': boundary}
    if lane_owner:
        result.update(lane_owner=lane_owner,pr_bindings={},owner_history=[],seat_id=seat_identifier,
                      seat_dir=str(session_store.seat_dir(seat_identifier)),
                      checkout_id=session_store.checkout_id(cwd),runtime=runtime,
                      seat_runtime_path=runtime['path'])
        if lane_owner['harness'] == 'opencode':
            result['opencode_plugin_path'] = str(Path(runtime['path']) / 'opencode-plugin')
        if ref['url']:
            lane_scope.select_binding(result, {key: result[key] for key in (
                'branch', 'head_branch', 'pr_url', 'base_ref')})
    result['plan_sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def install_seat(proposed):
    seat_identifier = proposed['seat_id']
    seat_location = session_store.seat_dir(seat_identifier)
    runtime = session_store.install_runtime(APP, proposed['runtime'])
    identity = {key: proposed['lane_owner'][key] for key in ('harness', 'session_id')}
    owner_context = {key: proposed['lane_owner'][key] for key in
                    ('session_parent_id', 'session_project_id', 'session_directory') if key in proposed['lane_owner']}
    with session_store.locked_seat(seat_identifier):
        seat = session_store.load_seat(seat_identifier)
        ensure_no_seat_retirement_journal(seat_identifier)
        seat_preimage = copy.deepcopy(seat)
        new_seat = seat is None
        if seat:
            if seat['owner'] != identity:
                raise ValueError('This permanent seat belongs to another native session; use an explicitly authorized handoff')
            anchor = active_operational_anchor(seat)
            if (anchor['root'] != proposed['session_root'] or
                    anchor['gitdir'] != proposed['lane_owner'].get('seat_gitdir')):
                raise ValueError('The exact permanent seat no longer has the planned active operational anchor')
            if identity['harness'] == 'opencode' and seat.get('owner_context') != owner_context:
                raise ValueError('This permanent seat belongs to a different OpenCode session API project or location; use an explicitly authorized handoff')
            if seat.get('runtime', {}).get('version') != proposed['runtime']['version']:
                raise ValueError('The seat runtime changed after planning; review a fresh source upgrade plan')
        else:
            seat = session_store.make_manifest(proposed['session_root'], identity, proposed['runtime'])
            seat['seat_gitdir'] = delivery.value(proposed['session_root'], 'rev-parse', '--absolute-git-dir')
            seat['seat_common_gitdir'] = delivery.value(
                proposed['session_root'], 'rev-parse', '--path-format=absolute', '--git-common-dir')
            seat['real_gh'] = proposed['real_gh_identity']
        checkout_state_path = seat_location / 'checkouts' / proposed['checkout_id'] / 'state.json'
        refuse_symlinked_path(checkout_state_path)
        current_hash = delivery_runs.digest(checkout_state_path) if checkout_state_path.exists() else None
        if current_hash != proposed['previous_state_sha256']:
            raise ValueError('Installation history changed since planning')
        checkout_preimage = checkout_state_path.read_text() if checkout_state_path.exists() else None
        checkout_preimage_mode = checkout_state_path.stat().st_mode & 0o777 if checkout_state_path.exists() else None
        if seat.get('real_gh') and seat['real_gh'].get('resolved_path') != proposed['real_gh_identity']['resolved_path']:
            if proposed['real_gh_identity']['path'] != str(seat_location / 'bin/gh'):
                raise ValueError('The captured real GitHub CLI changed for this seat; coordinate a reviewed runtime recovery')
        adapter_path = Path(proposed['adapter'])
        refuse_symlinked_path(adapter_path)
        adapter_preimage = adapter_path.read_text() if adapter_path.exists() else None
        adapter_preimage_mode = adapter_path.stat().st_mode & 0o777 if adapter_path.exists() else None
        adapter_preimage_sha256 = delivery_runs.digest(adapter_path) if adapter_path.exists() else None
        adapter_body = adapter_script(proposed['root'], seat_identifier=seat_identifier,
                                      runtime_cli=runtime / 'lane.py', real_gh=proposed['real_gh'])
        adapter_expected = proposed['adapter_sha256']
        adapter_passthrough = hashlib.sha256(passthrough(proposed['real_gh']).encode()).hexdigest()
        if adapter_preimage_sha256 not in (None, adapter_expected, adapter_passthrough):
            raise ValueError('Seat GitHub entrypoint was independently changed; preserve it and use recover --inspect')
        groups = copy.deepcopy(seat.get('groups', {}))
        settings_journal = None
        updated_settings = None
        settings_path = None
        settings_raw = None
        if proposed['hook_install_needed']:
            settings_path, settings_raw, original = settings(proposed['session_root'])
            if hashlib.sha256(settings_raw.encode()).hexdigest() != proposed['settings_sha256']:
                raise ValueError('Settings changed during installation planning; review a fresh plan')
            updated = copy.deepcopy(original)
            hooks = updated.setdefault('hooks', {})
            for event, group in proposed['groups'].items():
                hooks.setdefault(event, []).append(group)
            groups = copy.deepcopy(proposed['groups'])
            backup = seat_location / 'settings-backups' / (proposed['settings_sha256'] + '.json')
            seat.update(settings={'path': str(settings_path), 'backup': str(backup), 'sha256': proposed['settings_sha256'],
                                  'existed': settings_path.exists(), 'mode': proposed['settings_mode'],
                                  'original_hook_events': list(original.get('hooks', {})),
                                  'original_had_hooks': 'hooks' in original},
                        groups=groups, hooks_installed=False)
            updated_settings = json.dumps(updated, indent=2) + '\n'
            settings_journal = {'path':str(settings_path), 'preimage':settings_raw,
                                'preimage_sha256':hashlib.sha256(settings_raw.encode()).hexdigest(),
                                'preimage_exists':settings_path.exists(),
                                'mode':proposed['settings_mode'], 'postimage':updated_settings,
                                'postimage_sha256':hashlib.sha256(updated_settings.encode()).hexdigest()}
        state = copy.deepcopy(proposed)
        state.pop('runtime', None)
        seat_settings = seat.get('settings')
        if seat_settings is None:
            if seat.get('hooks_installed') or seat.get('groups'):
                raise ValueError('Permanent seat hook state is present without settings metadata; preserve it and use recover --inspect')
            seat_settings = {}
        elif not isinstance(seat_settings, dict):
            raise ValueError('Permanent seat settings metadata is malformed; preserve it and use recover --inspect')
        installed_at = delivery.dt.datetime.now(delivery.dt.timezone.utc).isoformat()
        state.update(phase='installing', installed_at=installed_at,
                     activation_generation=next_activation_generation(installed_at, proposed,
                                                                     proposed['adapter_sha256']),
                     original_hook_events=seat_settings.get('original_hook_events', []),
                     original_had_hooks=seat_settings.get('original_had_hooks', False),
                     settings_backup=seat_settings.get('backup'),
                     installation_history=[])
        if checkout_state_path.exists():
            prior = json.loads(checkout_state_path.read_text())
            history = prior.get('installation_history', [])
            prior.pop('installation_history', None)
            history = [*history, prior]
            state['installation_history'] = history
            if proposed.get('reattach_same_root') and prior.get('lane_owner'):
                records={}
                for binding in lane_scope.expand(prior):
                    if not binding.get('pr_url'):
                        continue
                    archived=copy.deepcopy(binding)
                    if archived.get('receipt'):
                        archived.setdefault('history',[]).append(archived.pop('receipt'))
                    records[archived['pr_url']]={key:copy.deepcopy(archived[key])
                        for key in lane_scope.BINDING_FIELDS if key in archived}
                selected={key:copy.deepcopy(state[key]) for key in
                          ('branch','head_branch','pr_url','base_ref') if key in state}
                for key in lane_scope.BINDING_FIELDS:
                    state.pop(key,None)
                state['pr_bindings']=records
                state['branch']=proposed['branch']
                if selected.get('pr_url'):
                    lane_scope.select_binding(state,selected)
        elif proposed.get('pr_url'):
            historical = []
            for item in session_store.all_checkouts(seat_identifier):
                prior_state = item.get('state')
                if not prior_state:
                    continue
                for binding in lane_scope.expand(prior_state):
                    if binding.get('pr_url', '').casefold() == proposed['pr_url'].casefold():
                        historical.append(binding)
            if historical:
                distinct = {json.dumps(item, sort_keys=True) for item in historical}
                if len(distinct) > 1:
                    raise ValueError('Existing PR history is linked to multiple checkouts with different records; preserve it and use recover --inspect')
                binding = copy.deepcopy(historical[0])
                if binding.get('receipt'):
                    binding.setdefault('history', []).append(binding.pop('receipt'))
                binding.update(branch=proposed['branch'], head_branch=proposed['head_branch'],
                               pr_url=proposed['pr_url'], base_ref=proposed['base_ref'])
                state['pr_bindings'] = {proposed['pr_url']:{key:copy.deepcopy(binding[key])
                                                           for key in lane_scope.BINDING_FIELDS if key in binding}}
                for key in lane_scope.BINDING_FIELDS:
                    state.pop(key, None)
                state.update({key:copy.deepcopy(binding[key]) for key in lane_scope.BINDING_FIELDS if key in binding})
        state['runtime'] = {'version': proposed['runtime']['version'], 'path': str(runtime),
                            'files': proposed['runtime']['files']}
        seat['groups'] = groups
        seat['owner_context'] = owner_context
        seat['adapter'] = proposed['adapter']
        seat['adapter_sha256'] = proposed['adapter_sha256']
        seat['real_gh'] = proposed['real_gh_identity']
        seat['runtime'] = {'version': proposed['runtime']['version'], 'path': str(runtime),
                           'files': proposed['runtime']['files'], 'source_revision': proposed['source_revision']}
        seat['updated_at'] = time.time()
        seat.setdefault('checkouts', {})
        state_installing = json.dumps(state, indent=2) + '\n'
        state_active = copy.deepcopy(state)
        state_active['phase'] = 'active'
        active_text = json.dumps(state_active, indent=2) + '\n'
        journal = {'kind':'seat-install','seat_id':seat_identifier,'checkout_id':proposed['checkout_id'],
                   'seat_preimage':seat_preimage,'state_path':str(checkout_state_path),
                   'state_preimage':checkout_preimage,
                   'state_preimage_sha256':current_hash,
                   'state_preimage_mode':checkout_preimage_mode,
                   'state_installing_sha256':hashlib.sha256(state_installing.encode()).hexdigest(),
                   'state_active_sha256':hashlib.sha256(active_text.encode()).hexdigest(),
                   'adapter_path':str(adapter_path),
                   'adapter_preimage':adapter_preimage,'adapter_preimage_sha256':adapter_preimage_sha256,
                   'adapter_preimage_mode':adapter_preimage_mode,
                   'adapter_replacement_sha256':adapter_expected,'settings':settings_journal,
                   'created_at':time.time()}
        seat['install_journal'] = journal
        session_store.save_seat(seat)
        if proposed['hook_install_needed']:
            write(backup, settings_raw)
        location = session_store.write_checkout(seat_identifier, proposed['root'], state,
                                                repository=proposed['lane_owner']['repository'],
                                                host=proposed['lane_owner']['host'],
                                                gitdir=proposed['lane_owner']['product_gitdir'],
                                                common_gitdir=delivery.value(proposed['root'], 'rev-parse',
                                                                             '--path-format=absolute', '--git-common-dir'))
        adapter_path.parent.mkdir(parents=True, exist_ok=True)
        if not adapter_path.exists() or adapter_preimage_sha256 != adapter_expected:
            write(adapter_path, adapter_body, 0o700)
        if proposed['hook_install_needed']:
            path = Path(seat['settings']['path'])
            current_raw = path.read_text() if path.exists() else ''
            if hashlib.sha256(current_raw.encode()).hexdigest() != proposed['settings_sha256']:
                raise ValueError('Settings changed during installation; preserve the journal and use recover --inspect')
            write(path, updated_settings, proposed['settings_mode'])
        seat = session_store.load_seat(seat_identifier)
        seat['hooks_installed'] = True if proposed['hook_install_needed'] else bool(seat.get('hooks_installed'))
        seat['install_journal'] = journal
        seat['updated_at'] = time.time()
        session_store.save_seat(seat)
        write(location / 'state.json', active_text)
        seat = session_store.load_seat(seat_identifier)
        seat.pop('install_journal', None)
        seat['updated_at'] = time.time()
        session_store.save_seat(seat)
    try:
        cleanup = cleanup_apply_plan(cleanup_plan(seat_id=seat_identifier))
        cleaned = sum(len(cleanup.get(key, [])) for key in
                      ('removed_prs', 'removed_checkouts', 'removed_runtimes'))
        cleanup_notice = cleanup.get('notice')
    except (OSError, TypeError, ValueError) as error:
        cleaned = 0
        cleanup_notice = 'Opportunistic cleanup was skipped: ' + str(error)
    return {'installed': True, 'adapter': proposed['adapter'], 'settings': proposed['settings'],
            'seat_id': seat_identifier, 'seat_runtime': str(runtime),
            'opencode_plugin_path': (str(runtime / 'opencode-plugin')
                                     if identity['harness'] == 'opencode' else None),
            'attached_checkouts': len(session_store.all_checkouts(seat_identifier)),
            'terminal_records_cleaned':cleaned, 'cleanup_notice':cleanup_notice,
            'history_notice': 'This checkout is attached to the durable seat ledger; existing per-PR history is retained. No manual count or receipt is inferred.',
            'activation': ('Verify the one installed hook and seat gh entrypoint in the actual Claude session; a file write is not proof of activation' if proposed['manages_claude_hooks'] else
                           'Configure the returned immutable OpenCode plugin path, then verify its actual session and permission events' if identity['harness'] == 'opencode' else
                           'Use and verify the seat gh entrypoint explicitly; no native Codex hook was installed')}


def install(args):
    proposed = plan(args)
    if args.plan_sha256 != proposed['plan_sha256']:
        raise ValueError('Plan or settings changed; inspect a fresh plan before installing')
    if args.lane:
        return install_seat(proposed)
    cwd = proposed['root']
    if proposed['manages_claude_hooks']: path,raw,original=settings(proposed['session_root'])
    else: path,raw,original=Path(proposed['settings']),'',{}
    updated = copy.deepcopy(original)
    hooks = updated.setdefault('hooks', {})
    for event, group in proposed['groups'].items():
        hooks.setdefault(event, []).append(group)
    with locked(cwd) as state:
        if state and state.get('phase') != 'removed':
            raise ValueError('Another installation already owns this lane')
        state_hash = delivery_runs.digest(directory(cwd) / 'state.json') if state else None
        if state_hash != proposed['previous_state_sha256']:
            raise ValueError('Installation history changed since planning')
        history = state.get('installation_history', [])
        if state:
            previous = copy.deepcopy(state)
            previous.pop('installation_history', None)
            history = [*history, previous]
        state.clear()
        backup = directory(cwd) / 'settings-backups' / (proposed['settings_sha256'] + '.json')
        # Journal first so an interrupted config write has a supported rollback path.
        installed_at = delivery.dt.datetime.now(delivery.dt.timezone.utc).isoformat()
        state.update(proposed, phase='installing', installed_at=installed_at,
                     activation_generation=next_activation_generation(
                         installed_at, proposed, proposed['adapter_sha256']),
                     original_hook_events=list(original.get('hooks', {})), original_had_hooks='hooks' in original,
                     settings_backup=str(backup), installation_history=history)
        write(backup, raw)
        write(directory(cwd) / 'state.json', json.dumps(state, indent=2) + '\n')
        write(proposed['adapter'], adapter_script(cwd), 0o700)
        if proposed['manages_claude_hooks']:
            if (path.read_text() if path.exists() else '') != raw:
                raise ValueError('Settings changed during installation; stop and inspect before rollback')
            refuse_symlinked_path(path)
            write(path,json.dumps(updated,indent=2)+'\n',proposed['settings_mode'])
        state['phase'] = 'active'
    return {'installed': True, 'adapter': proposed['adapter'], 'settings': str(path),
            'source_revision': proposed['source_revision'],
            'history_notice': 'New registration for this checkout; missing prior installer state is not restored. '
                              'Existing PR/tracker assessments and manual fix counts remain authoritative.',
            'activation': ('Owner must verify hook loading and gh resolution in the actual session; a file write is not proof of activation' if proposed['manages_claude_hooks'] else
                           'Use and verify the private adapter explicitly; no native Codex or Claude hooks were installed')}


def text_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def capture_text(path, default_mode=0o600):
    path = Path(path)
    refuse_symlinked_path(path)
    if not path.exists():
        return {'path': str(path), 'exists': False, 'text': '', 'mode': default_mode,
                'sha256': text_digest('')}
    raw = path.read_bytes().decode('utf-8')
    return {'path': str(path), 'exists': True, 'text': raw,
            'mode': path.stat().st_mode & 0o777, 'sha256': text_digest(raw)}


def git_path_identity(cwd):
    return {'gitdir': delivery.value(cwd, 'rev-parse', '--absolute-git-dir'),
            'common_gitdir': delivery.value(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')}


def captured_binary_identity(path):
    captured = Path(path)
    if not captured.is_absolute():
        raise ValueError('Captured GitHub executable path must be absolute')
    try:
        resolved = captured.resolve(strict=True)
        metadata = resolved.stat()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError('Captured GitHub executable is not a runnable regular file')
        if is_policy_guard_adapter(resolved):
            raise ValueError('Refusing to capture a nested or stale Policy Guard GitHub adapter')
        symlink_target = os.readlink(captured) if captured.is_symlink() else None
        return {'path': str(captured), 'resolved_path': str(resolved), 'symlink_target': symlink_target,
                'sha256': delivery_runs.digest(resolved), 'mode': metadata.st_mode & 0o777,
                'size_bytes': metadata.st_size}
    except (OSError, RuntimeError) as error:
        raise ValueError('Captured GitHub executable is missing or unreadable; inspect the active installation') from error


def is_policy_guard_adapter(path):
    path = Path(path)
    if path.name == 'gh' and ('starfleet-delivery-lane' in path.parts or
                              'starfleet-policy-guard' in path.parts):
        try:
            sample = path.read_bytes()[:8192].decode('utf-8', errors='ignore')
            if 'Generated by the delivery-only lane installer' in sample or 'delivery-lane rollback adapter' in sample:
                return True
        except OSError:
            return True
    return False


def hook_commands(group):
    if not isinstance(group, dict):
        return set()
    return {hook.get('command') for hook in group.get('hooks', [])
            if isinstance(hook, dict) and isinstance(hook.get('command'), str)}


def replace_exact_hook_groups(value, source_groups, target_groups):
    updated = copy.deepcopy(value)
    hooks = updated.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Unsupported existing Claude settings shape')
    for event, source in source_groups.items():
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError('Existing hook groups must be lists')
        positions = [index for index, group in enumerate(groups) if group == source]
        if len(positions) != 1:
            raise ValueError('Managed ' + event + ' hook is missing or duplicated; inspect settings before upgrade')
        position = positions[0]
        commands = hook_commands(source)
        for index, group in enumerate(groups):
            if index != position and commands.intersection(hook_commands(group)):
                raise ValueError('Managed ' + event + ' hook was independently changed; inspect settings before upgrade')
        target = target_groups.get(event)
        if target is None:
            raise ValueError('No successor replacement exists for managed ' + event + ' hook')
        if any(index != position and group == target for index, group in enumerate(groups)):
            raise ValueError('Successor ' + event + ' hook already exists; inspect settings before upgrade')
        groups[position] = copy.deepcopy(target)
        hooks[event] = groups
    return updated


def upgraded_state(state, source, new_groups, new_adapter_hash):
    result = copy.deepcopy(state)
    result.update(source)
    result['adapter_sha256'] = new_adapter_hash
    result['activation_generation'] = next_activation_generation(
        state.get('activation_generation') or state.get('installed_at') or '', source, new_adapter_hash)
    if state.get('manages_claude_hooks', True):
        result['groups'] = copy.deepcopy(new_groups)
    if state.get('lane_owner'):
        bindings = lane_scope.expand(state)
        for binding in bindings:
            if binding.get('receipt'):
                binding.setdefault('history', []).append(binding.pop('receipt'))
        result['pr_bindings'] = {
            binding['pr_url']: {key: copy.deepcopy(binding[key]) for key in lane_scope.BINDING_FIELDS
                                if key in binding}
            for binding in bindings if binding.get('pr_url')
        }
        current = next((binding for binding in bindings if binding.get('pr_url') == state.get('pr_url')), None)
        if current:
            for key in lane_scope.BINDING_FIELDS:
                result.pop(key, None)
            result.update({key: copy.deepcopy(current[key]) for key in lane_scope.BINDING_FIELDS
                           if key in current})
    elif result.get('receipt'):
        result.setdefault('history', []).append(result.pop('receipt'))
    return result


def apply_anchor_transition_state(state, transition, settings_after_text, settings_after_mode,
                                 settings_baseline_text, settings_baseline_value):
    result = copy.deepcopy(state)
    result['session_root'] = transition['replacement_root']
    owner = result.get('lane_owner')
    if not isinstance(owner, dict):
        raise ValueError('Replacement-anchor recovery requires a native-owner checkout state')
    owner['seat_root'] = transition['replacement_root']
    owner['seat_gitdir'] = transition['replacement_gitdir']
    result['settings'] = str(Path(transition['replacement_root']) / '.claude/settings.local.json')
    if settings_baseline_text is not None:
        settings_path = result['settings']
        result.update(settings=settings_path, settings_sha256=text_digest(settings_after_text),
                      settings_mode=settings_after_mode,
                      settings_backup=transition['settings_backup_path'],
                      original_hook_events=list(settings_baseline_value.get('hooks', {})),
                      original_had_hooks='hooks' in settings_baseline_value)
    return result


def next_activation_generation(previous, source, adapter_sha256):
    identity = {'previous': previous, 'source': source.get('source'),
                'source_revision': source.get('source_revision'),
                'source_versions': source.get('source_versions'),
                'adapter_sha256': adapter_sha256}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def upgrade_binding_summary(state):
    bindings = lane_scope.expand(state) if state.get('lane_owner') else [state]
    return [{'pr_url': binding.get('pr_url'), 'branch': binding.get('branch'),
             'head_branch': binding.get('head_branch', binding.get('branch')),
             'base_ref': binding.get('base_ref'), 'receipt_will_be_archived': bool(binding.get('receipt')),
             'history_count_before': len(binding.get('history', []))}
            for binding in bindings if binding.get('pr_url')]


def validate_upgrade_scope(state, cwd, replacement_seat_root=None, exact_seat_id=None):
    product = root(cwd)
    if state.get('phase') != 'active' or state.get('root') != str(product) or state.get('branch') != branch(product):
        raise ValueError('Upgrade requires the active installed product root and branch; inspect status before planning')
    if not state.get('lane_owner') and not state.get('pr_url'):
        raise ValueError('No active per-PR or native-owner installation to upgrade')
    seat_path = state.get('session_root', state['root'])
    if replacement_seat_root is not None:
        if not state.get('seat_id') or exact_seat_id != state.get('seat_id'):
            raise ValueError('Missing-root recovery requires the exact --seat-id for the recorded seat')
        seat = root(replacement_seat_root)
    elif state.get('seat_id'):
        manifest = session_store.load_seat(state['seat_id'])
        if not manifest:
            raise ValueError('Permanent seat registry is missing; preserve checkout history and inspect recovery')
        anchor = active_operational_anchor(manifest)
        if seat_path != anchor['root']:
            raise ValueError('Permanent owner seat path differs from its active operational anchor; inspect before upgrading')
        seat = root(anchor['root'])
    else:
        seat = root(seat_path)
    if replacement_seat_root is None and str(seat) != seat_path:
        raise ValueError('Permanent owner seat path changed; preserve history and coordinate an explicit path repair')
    if not state.get('seat_id') and delivery.value(seat, 'rev-parse', '--path-format=absolute', '--git-common-dir') != delivery.value(
            product, 'rev-parse', '--path-format=absolute', '--git-common-dir'):
        raise ValueError('Session root and product worktree must belong to the same repository')
    if state.get('lane_owner'):
        lane_scope.check_owner(state)
        owner = state['lane_owner']
        if (owner.get('product_gitdir') != delivery.value(product, 'rev-parse', '--absolute-git-dir') or
                (replacement_seat_root is None and
                 owner.get('seat_gitdir') != delivery.value(seat, 'rev-parse', '--absolute-git-dir')) or
                github.origin_repo(delivery.value(product, 'remote', 'get-url', 'origin'), owner['host']).casefold() !=
                owner.get('repository', '').casefold()):
            raise ValueError('Enrolled product/seat path or repository changed; preserve registration history and coordinate an explicit path repair')
    return product, seat


def migration_runtime_metadata_matches(state, seat_manifest):
    """Return whether the durable seat can restore runtime metadata omitted by legacy migration."""
    runtime = seat_manifest.get('runtime') if isinstance(seat_manifest, dict) else None
    if not isinstance(runtime, dict):
        return False
    version = runtime.get('version')
    path = runtime.get('path')
    files = runtime.get('files')
    source_revision = state.get('source_revision') if isinstance(state, dict) else None
    manifest_revision = runtime.get('source_revision')
    pinned = state.get('source_versions') if isinstance(state, dict) else None
    owner = state.get('lane_owner') if isinstance(state, dict) else None
    harness = owner.get('harness') if isinstance(owner, dict) else None
    if (not isinstance(version, str) or not version or not isinstance(path, str) or not Path(path).is_absolute() or
            not isinstance(files, dict) or not isinstance(manifest_revision, str) or not manifest_revision or
            not isinstance(pinned, dict) or not pinned or state.get('source') != path):
        return False
    canonical_runtime_revision = 'runtime:' + version
    valid_git_revision = (isinstance(source_revision, str) and len(source_revision) in (40, 64) and
                          all(character in '0123456789abcdef' for character in source_revision))
    valid_manifest_git_revision = (len(manifest_revision) in (40, 64) and
                                   all(character in '0123456789abcdef' for character in manifest_revision))
    if manifest_revision != canonical_runtime_revision and not valid_manifest_git_revision:
        return False
    if source_revision not in (manifest_revision, canonical_runtime_revision) and not valid_git_revision:
        return False
    expected_source_files = set(source_versions(harness)) & set(files)
    if set(pinned) != expected_source_files:
        return False
    if not all(isinstance(name, str) and isinstance(digest, str) and files.get(name) == digest
               for name, digest in pinned.items()):
        return False
    try:
        recorded_runtime = session_store.recorded_runtime_plan(Path(path), files)
    except (OSError, ValueError):
        return False
    return recorded_runtime['version'] == version and recorded_runtime['files'] == files


def validate_upgrade_seat_runtime(state, seat_manifest, seat, *, replacing_anchor=False):
    runtime = seat_manifest.get('runtime') if isinstance(seat_manifest, dict) else None
    if not isinstance(runtime, dict) or not isinstance(runtime.get('version'), str) or \
            not isinstance(runtime.get('path'), str) or not Path(runtime['path']).is_absolute() or \
            not isinstance(runtime.get('files'), dict) or not isinstance(runtime.get('source_revision'), str) or \
            not runtime['source_revision']:
        raise ValueError('Permanent seat runtime metadata is malformed; preserve it and inspect status')
    expected_owner = {key: state.get('lane_owner', {}).get(key) for key in ('harness', 'session_id')}
    anchor = (session_store.operational_anchor(seat_manifest) if replacing_anchor else
              active_operational_anchor(seat_manifest))
    if (anchor['state'] != 'active' or seat_manifest.get('owner') != expected_owner or
            (not replacing_anchor and (anchor.get('root') != str(seat) or
             anchor.get('gitdir') != delivery.value(seat, 'rev-parse', '--absolute-git-dir')))):
        raise ValueError('Permanent seat runtime or owner changed; inspect status and review a fresh upgrade plan')
    current = state.get('runtime')
    if isinstance(current, dict) and current.get('version') == runtime['version']:
        return False
    if 'runtime' not in state and migration_runtime_metadata_matches(state, seat_manifest):
        return True
    raise ValueError('Permanent seat runtime or owner changed; inspect status and review a fresh upgrade plan')


def replacement_anchor_plan(state, target_seat, replacement_root, release_identifier):
    """Bind one missing birth anchor to an exact same-repository operational anchor."""
    identifier = state.get('seat_id')
    if not identifier or not isinstance(target_seat, dict):
        raise ValueError('Missing-root recovery requires an intact durable seat registry')
    birth_root = target_seat.get('seat_root')
    birth_gitdir = target_seat.get('seat_gitdir')
    current_anchor = session_store.operational_anchor(target_seat)
    if (current_anchor['state'] != 'active' or current_anchor['root'] != birth_root or
            current_anchor['gitdir'] != birth_gitdir):
        raise ValueError('Only an unreleased immutable birth anchor can use this missing-root recovery path')
    if not isinstance(birth_root, str) or not Path(birth_root).is_absolute() or \
            not isinstance(birth_gitdir, str) or not Path(birth_gitdir).is_absolute():
        raise ValueError('The immutable birth root or Gitdir is not recorded completely; preserve the seat and inspect recovery')
    if os.path.lexists(birth_root) or os.path.lexists(birth_gitdir):
        raise ValueError('Replacement-anchor recovery requires the recorded birth root and Gitdir to both be absent')
    destination = root(replacement_root)
    if str(destination) != str(replacement_root):
        raise ValueError('Replacement seat root must be an exact canonical Git worktree path')
    destination_identity = git_path_identity(destination)
    birth_rows = [(checkout_id, row) for checkout_id, row in target_seat.get('checkouts', {}).items()
                  if row.get('root') == birth_root and row.get('gitdir') == birth_gitdir]
    if len(birth_rows) != 1 or not isinstance(birth_rows[0][1].get('common_gitdir'), str):
        raise ValueError('Same-repository proof is unavailable: no unique retained birth-root attachment records its Git common directory')
    birth_checkout_id, birth_row = birth_rows[0]
    expected_state_path = session_store.seat_dir(identifier) / 'checkouts' / birth_checkout_id / 'state.json'
    if (birth_checkout_id != session_store.checkout_id(birth_root) or
            birth_row.get('state_file') != str(expected_state_path)):
        raise ValueError('Same-repository proof does not come from the exact retained birth-root attachment')
    recorded_common = birth_row['common_gitdir']
    if (current_anchor.get('common_gitdir') is not None and
            current_anchor['common_gitdir'] != recorded_common) or \
            (target_seat.get('seat_common_gitdir') is not None and
             target_seat['seat_common_gitdir'] != recorded_common):
        raise ValueError('Recorded birth-root Git common-directory identities disagree; preserve the seat and inspect recovery')
    if destination_identity['common_gitdir'] != recorded_common:
        raise ValueError('Replacement root belongs to a different Git common directory than the retained birth-root attachment')
    destination_identifier = session_store.seat_id(destination)
    seats = session_store.all_seats()
    destination_claimants = []
    for candidate in seats:
        anchor = session_store.operational_anchor(candidate)
        if anchor['state'] == 'active' and (anchor['root'] == str(destination) or
                                             anchor['gitdir'] == destination_identity['gitdir']):
            destination_claimants.append(candidate)
    if len(destination_claimants) > 1:
        raise ValueError('More than one active seat claims the replacement Git anchor; preserve both histories and inspect status')
    if destination_claimants:
        claimant = destination_claimants[0]
        if (not release_identifier or release_identifier != claimant['seat_id'] or
                claimant['seat_id'] != destination_identifier):
            raise ValueError('Replacement root is already owned by a distinct seat; name that exact seat with --release-anchor-seat after its attachments are retired')
    elif release_identifier:
        raise ValueError('--release-anchor-seat does not identify the active seat that owns the replacement root')
    if release_identifier:
        if release_identifier == identifier:
            raise ValueError('A seat cannot release itself as a replacement anchor')
        release = session_store.load_seat(release_identifier)
        if not release or release['seat_id'] != destination_identifier or release.get('seat_root') != str(destination):
            raise ValueError('--release-anchor-seat must name the exact durable seat born at the replacement root')
        release_anchor = active_operational_anchor(release)
        if (release_anchor['root'] != str(destination) or
                release_anchor['gitdir'] != destination_identity['gitdir']):
            raise ValueError('The named destination seat no longer owns the exact replacement Git anchor')
        current_owner = lane_scope.native_identity()
        if current_owner != target_seat.get('owner') or current_owner != release.get('owner'):
            raise ValueError('Both seats must have the exact same current native owner')
        if current_owner.get('harness') == 'opencode':
            context = lane_scope.native_session_context()
            if context != target_seat.get('owner_context') or context != release.get('owner_context'):
                raise ValueError('Both OpenCode seats must match the exact current native session context')
        if any(release.get(key) for key in ('install_journal', 'migration_journal', 'handoff_journal')) or \
                (session_store.seat_dir(release_identifier) / UPGRADE_JOURNAL).exists() or \
                (session_store.seat_dir(release_identifier) / UPGRADE_JOURNAL).is_symlink():
            raise ValueError('The destination seat has a pending transition; recover it before releasing its anchor')
        release_rows = session_store.all_checkouts(release_identifier)
        for row in release_rows:
            prior = row.get('state')
            expected_state_path = session_store.seat_dir(release_identifier) / 'checkouts' / row['checkout_id'] / 'state.json'
            if (not isinstance(prior, dict) or prior.get('phase') != 'removed' or
                    prior.get('seat_id') != release_identifier or prior.get('root') != row.get('root') or
                    row.get('state_file') != str(expected_state_path)):
                raise ValueError('Every destination attachment must already be retired before its anchor can be released')
        permanent_handoff_runtime(release)
        expected_release_adapter = session_store.seat_dir(release_identifier) / 'bin' / 'gh'
        if Path(release.get('adapter', '')) != expected_release_adapter:
            raise ValueError('Destination seat adapter path differs from its durable seat identity')
        recorded_real_gh = release.get('real_gh')
        if not isinstance(recorded_real_gh, dict) or not isinstance(recorded_real_gh.get('path'), str) or \
                captured_binary_identity(recorded_real_gh['path']) != recorded_real_gh:
            raise ValueError('Destination seat captured GitHub executable identity changed; preserve its pin')
        adapter_integrity = managed_adapter_status(release)
        if adapter_integrity['state'] != 'intact':
            raise ValueError('The destination seat adapter pin is not intact; inspect it before releasing the anchor')
        release_groups = release.get('groups', {})
        if not isinstance(release_groups, dict):
            raise ValueError('The destination seat hook pin is malformed; preserve it and inspect recovery')
        if release.get('hooks_installed'):
            settings_info = release.get('settings')
            if not isinstance(settings_info, dict) or settings_info.get('path') != str(destination / '.claude/settings.local.json'):
                raise ValueError('Destination hook settings do not identify the replacement root')
            settings_path, settings_raw, settings_value = settings(destination)
            if str(settings_path) != settings_info['path']:
                raise ValueError('Destination hook settings path changed; preserve it and inspect recovery')
            release_settings_before = capture_text(settings_path, settings_info.get('mode', 0o600))
            if (release_settings_before['mode'] != settings_info.get('mode') or
                    release_settings_before['exists'] != settings_info.get('existed')):
                raise ValueError('Destination hook settings mode or existence changed; preserve it and inspect recovery')
            for event, group in release_groups.items():
                groups = settings_value.get('hooks', {}).get(event, [])
                if not isinstance(groups, list) or groups.count(group) != 1:
                    raise ValueError('Each owned destination hook must be present exactly once before anchor release')
        else:
            if release_groups:
                raise ValueError('Destination seat records hook groups without owning installed hooks; inspect before release')
            release_settings_before = None
        release_after = copy.deepcopy(release)
        release_after['operational_anchor'] = {'state': 'released', 'root': None,
                                               'gitdir': None, 'common_gitdir': None}
        release_after['hooks_installed'] = False
        release_manifest_file_before = capture_text(
            session_store.seat_dir(release_identifier) / 'seat.json')
    else:
        release = None
        release_after = None
        release_settings_before = None
        release_groups = {}
        release_manifest_file_before = None
    for key in ('install_journal', 'migration_journal', 'handoff_journal'):
        if target_seat.get(key):
            raise ValueError('The source seat has a pending transition; recover it before replacing its anchor')
    if (session_store.seat_dir(identifier) / UPGRADE_JOURNAL).exists() or \
            (session_store.seat_dir(identifier) / UPGRADE_JOURNAL).is_symlink():
        raise ValueError('The source seat has a pending source upgrade; recover it before replacing its anchor')
    return {'target_seat_id': identifier, 'birth_root': birth_root, 'birth_gitdir': birth_gitdir,
            'birth_common_gitdir': recorded_common, 'replacement_root': str(destination),
            'replacement_seat_id': destination_identifier, 'replacement_gitdir': destination_identity['gitdir'],
            'replacement_common_gitdir': destination_identity['common_gitdir'],
            'release_seat_id': release_identifier, 'release_seat_before': release,
            'release_seat_after': release_after, 'release_settings_before': release_settings_before,
            'release_manifest_file_before': release_manifest_file_before,
            'release_groups': copy.deepcopy(release_groups)}


def migrated_settings_mode_repair(state, seat_manifest, runtime_missing, settings_before,
                                  relocated_settings_path=None):
    if not runtime_missing or not state.get('manages_claude_hooks', True):
        return None
    settings_info = seat_manifest.get('settings') if isinstance(seat_manifest, dict) else None
    if not isinstance(seat_manifest, dict) or not seat_manifest.get('hooks_installed') or \
            not isinstance(settings_info, dict):
        raise ValueError('Permanent seat settings metadata is malformed; preserve it and inspect status')
    path = settings_info.get('path')
    mode = settings_info.get('mode')
    expected_current_path = relocated_settings_path or path
    if (not isinstance(path, str) or not Path(path).is_absolute() or path != state.get('settings') or
            expected_current_path != settings_before.get('path') or isinstance(mode, bool) or not isinstance(mode, int) or
            mode < 0 or mode > 0o777):
        raise ValueError('Permanent seat settings path or mode is malformed; preserve it and inspect status')
    if 'settings_mode' not in state:
        raise ValueError('Migrated checkout settings mode is missing; preserve it and inspect status')
    recorded_mode = state['settings_mode']
    if isinstance(recorded_mode, bool) or not isinstance(recorded_mode, int) or recorded_mode < 0 or recorded_mode > 0o777:
        raise ValueError('Migrated checkout settings mode is malformed; preserve it and inspect status')
    if not settings_before.get('exists') or settings_before['mode'] != mode:
        raise ValueError('Claude settings mode changed since installation; coordinate the settings writer before upgrade')
    if recorded_mode != mode:
        recorded_hash = state.get('settings_sha256')
        if not isinstance(recorded_hash, str) or settings_before.get('sha256') != recorded_hash:
            raise ValueError('Claude settings changed since migration; preserve them and inspect status before upgrade')
        return mode
    return None


def validate_checkout_runtime(state, seat_manifest):
    runtime = seat_manifest.get('runtime', {})
    current = state.get('runtime')
    if isinstance(current, dict) and current.get('version') == runtime.get('version'):
        return False
    if 'runtime' not in state and migration_runtime_metadata_matches(state, seat_manifest):
        return True
    raise ValueError('Active seat checkouts do not share one runtime version; preserve history and inspect the seat')


def retirement_recovery_command(seat_id, action='recover'):
    return shlex.join([sys.executable, str(SELF), '--seat-id', seat_id, action, '--inspect'])


def ensure_retirement_lifecycle_clear(seat):
    identifier = seat['seat_id']
    seat_location = session_store.seat_dir(identifier)
    seat_retirement_journal = seat_retirement_journal_path(identifier)
    if seat_retirement_journal.exists() or seat_retirement_journal.is_symlink():
        raise ValueError('A drained-seat retirement is incomplete; inspect it with ' +
                         retirement_recovery_command(identifier) + ' and use recover --rollback before retiring an attachment')
    journal = seat_location / UPGRADE_JOURNAL
    if journal.exists() or journal.is_symlink():
        inspect_command = retirement_recovery_command(identifier)
        rollback_command = shlex.join([sys.executable, str(SELF), 'upgrade-recover', '--rollback'])
        raise ValueError('A seat source upgrade is incomplete; inspect the seat with ' + inspect_command +
                         ', then run ' + rollback_command + ' from the checkout that started the upgrade before ' +
                         'retiring an attachment. If that checkout is unavailable, preserve the journal and restore ' +
                         'the original checkout for recovery')
    if any(seat.get(key) for key in ('install_journal', 'migration_journal', 'handoff_journal')):
        raise ValueError('A seat installation, migration, or handoff is incomplete; run ' +
                         retirement_recovery_command(identifier) + ' before retiring an attachment')
    for item in session_store.all_checkouts(identifier):
        state = item.get('state')
        if not isinstance(state, dict):
            raise ValueError('A registered checkout state is missing or malformed; run ' +
                             retirement_recovery_command(identifier) + ' before retiring an attachment')
        phase = state.get('phase')
        if phase not in ('active', 'removed'):
            recovery = ('upgrade-recover --rollback from a recorded checkout'
                        if phase == 'upgrading' else retirement_recovery_command(identifier))
            raise ValueError('A registered checkout has an interrupted or unknown phase; run ' + recovery +
                             ' before retiring an attachment')


def checkout_pr_records(seat, checkout_id, state):
    urls = set(state.get('pr_bindings', {}))
    if state.get('pr_url'):
        urls.add(state['pr_url'])
    for url, record in seat.get('prs', {}).items():
        if checkout_id in record.get('checkout_ids', []):
            urls.add(url)
    rows = []
    for url in sorted(urls):
        binding = copy.deepcopy(state.get('pr_bindings', {}).get(url))
        if binding is None and state.get('pr_url') == url:
            binding = {key: copy.deepcopy(state[key]) for key in lane_scope.BINDING_FIELDS if key in state}
        rows.append({'url': url, 'seat_record': copy.deepcopy(seat.get('prs', {}).get(url)),
                     'binding': binding, 'checkout_id': checkout_id})
    return rows


def retirement_checkout_evidence(checkout, state):
    checkout_path = Path(checkout['root'])
    recorded_branch = state.get('branch')
    if not checkout_path.exists() and not checkout_path.is_symlink():
        return {'state': 'missing', 'reason': 'recorded checkout path does not exist',
                'path_exists': False, 'recorded_branch': recorded_branch}
    if checkout_path.is_symlink():
        return {'state': 'not-recorded-worktree', 'reason': 'recorded checkout path is now a symlink',
                'path_exists': True, 'path_is_symlink': True,
                'symlink_target': os.readlink(checkout_path), 'recorded_branch': recorded_branch}
    if not checkout_path.is_dir():
        return {'state': 'not-recorded-worktree', 'reason': 'recorded checkout path is not a directory',
                'path_exists': True, 'path_is_symlink': False, 'recorded_branch': recorded_branch}
    if not (checkout_path / '.git').exists():
        return {'state': 'not-recorded-worktree', 'reason': 'recorded checkout path has no Git worktree metadata',
                'path_exists': True, 'path_is_symlink': False, 'recorded_branch': recorded_branch}
    try:
        observed_root = str(root(checkout_path))
        observed_gitdir = delivery.value(checkout_path, 'rev-parse', '--absolute-git-dir')
        observed_common_gitdir = delivery.value(checkout_path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    except (OSError, ValueError) as error:
        raise ValueError('The existing checkout Git metadata cannot be verified; preserve it and inspect it before retirement') from error
    try:
        observed_branch = branch(checkout_path)
    except ValueError:
        observed_branch = None
    try:
        observed_repository = github.origin_repo(
            delivery.value(checkout_path, 'remote', 'get-url', 'origin'), checkout.get('host', 'github.com')).casefold()
    except (OSError, ValueError):
        observed_repository = None
    observed = {'root': observed_root, 'repository': observed_repository,
                'gitdir': observed_gitdir, 'common_gitdir': observed_common_gitdir,
                'branch': observed_branch}
    same_worktree = (observed_root == checkout.get('root') and
                     observed_gitdir == checkout.get('gitdir') and
                     observed_common_gitdir == checkout.get('common_gitdir'))
    if same_worktree and observed_branch != recorded_branch:
        raise ValueError('The recorded checkout exists on another branch; restore its recorded branch and run uninstall from that checkout. Missing-checkout retirement does not apply to a live worktree on another branch')
    if same_worktree and observed_repository == checkout.get('repository'):
        raise ValueError('The recorded checkout is live and matches its recorded repository and branch; run uninstall from that checkout')
    return {'state': 'not-recorded-worktree', 'reason': 'recorded path no longer identifies its recorded repository worktree',
            'path_exists': True, 'path_is_symlink': False, 'recorded_branch': recorded_branch,
            'observed': observed}


def build_retire_attachment_plan(identifier, checkout_value):
    seat = session_store.load_seat(identifier)
    if not seat:
        raise ValueError('Permanent Policy Guard seat registry is missing; preserve history and use recover --inspect')
    lane_scope.check_owner({'lane_owner': seat['owner']})
    ensure_retirement_lifecycle_clear(seat)
    checkout_value = Path(checkout_value)
    if not checkout_value.is_absolute():
        raise ValueError('--checkout must name the absolute recorded checkout path')
    requested_path = Path(os.path.abspath(checkout_value))
    rows = session_store.all_checkouts(identifier)
    item = next((row for row in rows if row.get('root') == str(requested_path)), None)
    if not item:
        raise ValueError('The requested path is not a recorded attachment in this seat')
    state = item.get('state')
    if not isinstance(state, dict):
        raise ValueError('The recorded attachment state is missing or malformed; preserve it and use recover --inspect')
    if (state.get('root') != item.get('root') or state.get('seat_id') != identifier or
            state.get('branch') is None or not isinstance(state.get('lane_owner'), dict) or
            {key: state['lane_owner'].get(key) for key in ('harness', 'session_id')} != seat.get('owner')):
        raise ValueError('The recorded attachment identity is inconsistent with its permanent seat; preserve it and inspect recovery')
    state_path = Path(item['checkout_dir']) / 'state.json'
    if state.get('phase') == 'removed':
        return {'protocol': RETIREMENT_PROTOCOL, 'seat_id': identifier, 'already_retired': True,
                'attachment': {'checkout_id': item['checkout_id'], 'path': item['root'],
                               'branch': state.get('branch')},
                'retirement_receipt': copy.deepcopy(state.get('retirement_receipt')),
                'history_preserved': True}
    if state.get('phase') != 'active':
        raise ValueError('The recorded attachment is not active; preserve its state and use recover --inspect')
    evidence = retirement_checkout_evidence(item, state)
    pr_records = checkout_pr_records(seat, item['checkout_id'], state)
    seat_path = session_store.seat_dir(identifier) / 'seat.json'
    review = {'protocol': RETIREMENT_PROTOCOL, 'seat_id': identifier,
              'seat': {'seat_root': seat['seat_root'], 'owner': copy.deepcopy(seat['owner'])},
              'attachment': {'checkout_id': item['checkout_id'], 'path': item['root'],
                             'repository': item.get('repository'), 'host': item.get('host'),
                             'gitdir': item.get('gitdir'), 'common_gitdir': item.get('common_gitdir'),
                             'branch': state.get('branch')},
              'checkout_evidence': evidence,
              'pr_records': pr_records,
              'preimages': {'seat_manifest': {'path': str(seat_path),
                                               'sha256': delivery_runs.digest(seat_path)},
                            'attachment_state': {'path': str(state_path),
                                                 'sha256': delivery_runs.digest(state_path)},
                            'pr_records_sha256': text_digest(json.dumps(
                                pr_records, sort_keys=True, separators=(',', ':')))},
              'lifecycle_preconditions': {'upgrade_journal': False, 'install_journal': False,
                                           'migration_journal': False, 'handoff_journal': False}}
    review['plan_sha256'] = text_digest(json.dumps(review, sort_keys=True, separators=(',', ':')))
    return review


def retire_attachment(args):
    identifier = selected_seat_id(args)
    if args.apply:
        if not args.plan_sha256:
            raise ValueError('retire-attachment --apply requires --plan-sha256 from a reviewed retire-attachment plan')
        review = build_retire_attachment_plan(identifier, args.checkout)
        if review.get('already_retired'):
            return {'retired': True, 'already_retired': True, 'history_preserved': True,
                    'seat_id': identifier, 'attachment': review['attachment'],
                    'retirement_receipt': review.get('retirement_receipt')}
        if args.plan_sha256 != review['plan_sha256']:
            raise ValueError('Retirement plan or a bound preimage changed; inspect a fresh retire-attachment plan')
        with session_store.locked_seat(identifier):
            current = build_retire_attachment_plan(identifier, args.checkout)
            if current.get('already_retired'):
                return {'retired': True, 'already_retired': True, 'history_preserved': True,
                        'seat_id': identifier, 'attachment': current['attachment'],
                        'retirement_receipt': current.get('retirement_receipt')}
            if args.plan_sha256 != current['plan_sha256']:
                raise ValueError('Retirement plan or a bound preimage changed; inspect a fresh retire-attachment plan')
            checkout_id = current['attachment']['checkout_id']
            item = next(row for row in session_store.all_checkouts(identifier)
                        if row['checkout_id'] == checkout_id)
            state = copy.deepcopy(item['state'])
            retired_at = time.time()
            receipt = {'protocol': RETIREMENT_PROTOCOL, 'seat_id': identifier,
                       'checkout_id': checkout_id, 'checkout_path': current['attachment']['path'],
                       'repository': current['attachment']['repository'],
                       'branch': current['attachment']['branch'],
                       'owner': copy.deepcopy(current['seat']['owner']), 'retired_at': retired_at,
                       'checkout_evidence': copy.deepcopy(current['checkout_evidence']),
                       'plan_sha256': current['plan_sha256'],
                       'pr_records_sha256': current['preimages']['pr_records_sha256'],
                       'pr_records_preserved': len(current['pr_records'])}
            state['phase'] = 'removed'
            state['removed_at'] = retired_at
            state['updated_at'] = retired_at
            state['retirement_receipt'] = receipt
            state_path = Path(item['checkout_dir']) / 'state.json'
            refuse_symlinked_path(state_path)
            write(state_path, json.dumps(state, indent=2) + '\n')
        return {'retired': True, 'already_retired': False, 'history_preserved': True,
                'seat_id': identifier, 'attachment': current['attachment'],
                'retirement_receipt': receipt, 'plan_sha256': current['plan_sha256']}
    if args.plan_sha256:
        raise ValueError('Use --apply with --plan-sha256 to apply a reviewed retirement plan')
    return build_retire_attachment_plan(identifier, args.checkout)


def seat_retirement_attachment_summary(identifier, seat):
    rows = session_store.all_checkouts(identifier)
    if not rows:
        raise ValueError('No recorded retired attachments prove this seat is drained; preserve its history and inspect status')
    summaries = []
    for item in rows:
        checkout_id = item.get('checkout_id')
        checkout_root = item.get('root')
        if (not isinstance(checkout_id, str) or not isinstance(checkout_root, str) or
                not Path(checkout_root).is_absolute() or
                str(Path(os.path.abspath(checkout_root))) != checkout_root or
                session_store.checkout_id(checkout_root) != checkout_id):
            raise ValueError('A registered attachment identity is malformed; preserve history and inspect status')
        expected_dir = session_store.seat_dir(identifier) / 'checkouts' / checkout_id
        state_path = expected_dir / 'state.json'
        if (Path(item.get('checkout_dir', '')) != expected_dir or
                item.get('state_file') != str(state_path) or not state_path.is_file()):
            raise ValueError('A registered attachment state path is missing or inconsistent; preserve history and inspect status')
        refuse_symlinked_path(state_path)
        state = item.get('state')
        if not isinstance(state, dict):
            raise ValueError('A registered attachment state is malformed; preserve history and inspect status')
        if (state.get('phase') != 'removed' or state.get('seat_id') != identifier or
                state.get('root') != checkout_root or not isinstance(state.get('branch'), str) or
                not isinstance(state.get('lane_owner'), dict)):
            raise ValueError('Every registered attachment must have an exact terminal removed identity before seat retirement')
        receipt = state.get('retirement_receipt')
        if (not isinstance(receipt, dict) or receipt.get('protocol') != RETIREMENT_PROTOCOL or
                receipt.get('seat_id') != identifier or receipt.get('checkout_id') != checkout_id or
                receipt.get('checkout_path') != checkout_root):
            raise ValueError('A removed attachment lacks its exact retirement receipt; preserve history and inspect status')
        row_owner = state['lane_owner']
        owner_pair = {key: row_owner.get(key) for key in ('harness', 'session_id')}
        valid_pair = (owner_pair['harness'] in ('claude', 'codex', 'opencode') and
                      isinstance(owner_pair['session_id'], str) and
                      bool(re.fullmatch(r'ses_[A-Za-z0-9]+' if owner_pair['harness'] == 'opencode'
                                        else r'[A-Za-z0-9_.:-]{1,256}', owner_pair['session_id'])))
        if not valid_pair or receipt.get('owner') != owner_pair:
            raise ValueError('A retired attachment owner receipt is malformed; preserve its recorded ownership and inspect status')
        bindings = state.get('pr_bindings', {})
        if (not isinstance(bindings, dict) or
                any(not isinstance(url, str) or not isinstance(binding, dict)
                    for url, binding in bindings.items()) or
                (state.get('pr_url') is not None and not isinstance(state.get('pr_url'), str))):
            raise ValueError('A retired attachment PR binding is malformed; preserve history and inspect status')
        pr_records = checkout_pr_records(seat, checkout_id, state)
        expected_pr_digest = text_digest(json.dumps(pr_records, sort_keys=True, separators=(',', ':')))
        if (receipt.get('pr_records_sha256') != expected_pr_digest or
                receipt.get('pr_records_preserved') != len(pr_records)):
            raise ValueError('A retired attachment PR history no longer matches its receipt; preserve history and inspect status')
        summaries.append({'checkout_id': checkout_id, 'path': checkout_root,
                          'repository': item.get('repository'), 'branch': state['branch'],
                          'owner': {key: row_owner.get(key) for key in ('harness', 'session_id')},
                          'state_sha256': delivery_runs.digest(state_path),
                          'retirement_receipt_sha256': text_digest(json.dumps(
                              receipt, sort_keys=True, separators=(',', ':'))),
                          'pr_records_sha256': expected_pr_digest,
                          'pr_records_preserved': len(pr_records), 'phase': 'removed'})
    return summaries


def seat_retirement_lock_ids(identifier, anchor_root):
    ids = {identifier, session_store.seat_id(anchor_root)}
    found = session_store.lookup_checkout(anchor_root)
    if found:
        ids.add(found['seat']['seat_id'])
    root_seat_id = session_store.seat_id(anchor_root)
    root_seat = session_store.load_seat(root_seat_id) if root_seat_id != identifier else None
    if root_seat:
        root_anchor = session_store.operational_anchor(root_seat)
        if root_anchor['state'] == 'active' and root_anchor['root'] == anchor_root:
            raise ValueError('A different active seat owns the settings anchor; preserve both seats and inspect status')
    return sorted(ids)


def valid_historical_seat_owner(seat):
    owner = seat.get('owner') if isinstance(seat, dict) else None
    context = seat.get('owner_context', {}) if isinstance(seat, dict) else None
    if not isinstance(context, dict):
        return False
    if valid_recorded_owner(owner):
        return context == {}
    if not isinstance(owner, dict) or owner.get('harness') != 'opencode':
        return False
    return (isinstance(owner.get('session_id'), str) and
            bool(re.fullmatch(r'ses_[A-Za-z0-9]+', owner['session_id'])) and
            isinstance(context, dict) and
            (context.get('session_parent_id') is None or
             (isinstance(context.get('session_parent_id'), str) and
              bool(re.fullmatch(r'ses_[A-Za-z0-9]+', context['session_parent_id'])))) and
            isinstance(context.get('session_project_id'), str) and bool(context['session_project_id']) and
            isinstance(context.get('session_directory'), str) and
            Path(context['session_directory']).is_absolute())


@contextlib.contextmanager
def seat_retirement_locks(identifier, anchor_root):
    lock_ids = seat_retirement_lock_ids(identifier, anchor_root)
    with session_store.locked_seats(lock_ids):
        found = session_store.lookup_checkout(anchor_root)
        if found and found['seat']['seat_id'] not in lock_ids:
            raise ValueError('The settings-writer seat changed while acquiring locks; review a fresh retirement plan')
        if found:
            yield
        else:
            # The per-worktree lock serializes an existing legacy settings writer at this exact anchor.
            with file_locked(anchor_root):
                yield


def build_retire_seat_plan(identifier, *, expected_owner_harness, expected_owner_session,
                           expected_operator_harness, expected_operator_session, authorization_ref,
                           confirmed):
    seat = session_store.load_seat(identifier)
    if not seat:
        raise ValueError('Permanent Policy Guard seat registry is missing; preserve history and inspect status')
    owner = seat.get('owner')
    owner_context = seat.get('owner_context', {})
    if not valid_historical_seat_owner(seat):
        raise ValueError('The recorded historical seat owner is malformed; preserve it and inspect status')
    if (owner.get('harness') != expected_owner_harness or
            owner.get('session_id') != expected_owner_session):
        raise ValueError('Expected historical owner flags do not match the exact recorded seat owner')
    if not confirmed:
        raise ValueError('retire-seat requires explicit --confirm-retirement after existing authorization')
    if (not isinstance(authorization_ref, str) or not authorization_ref.strip() or
            len(authorization_ref) > 512 or any(char in authorization_ref for char in '\r\n\0')):
        raise ValueError('retire-seat requires a short existing authorization reference')
    operator = lane_scope.native_identity()
    expected_operator = {'harness': expected_operator_harness,
                         'session_id': expected_operator_session}
    if operator != expected_operator:
        raise ValueError('Expected operator flags do not match the actual native operator')
    operator_context = (lane_scope.native_session_context()
                        if operator['harness'] == 'opencode' else {})
    if operator['harness'] == 'opencode' and not isinstance(operator_context, dict):
        raise ValueError('The actual OpenCode operator context is unavailable; retire-seat cannot bind this operator')
    if not isinstance(seat.get('checkouts'), dict):
        raise ValueError('The permanent seat attachment registry is malformed; preserve history and inspect status')
    anchor = active_operational_anchor(seat)
    anchor_root = anchor['root']
    if (session_store.operational_anchor(seat)['state'] != 'active' or
            not Path(anchor_root).is_absolute() or not Path(anchor['gitdir']).is_absolute()):
        raise ValueError('Only a seat with its exact active operational anchor can be retired')
    ensure_retirement_lifecycle_clear(seat)
    retirement_journal = seat_retirement_journal_path(identifier)
    if retirement_journal.exists() or retirement_journal.is_symlink():
        raise ValueError('A drained-seat retirement is already pending; inspect and roll it back before planning again')
    runtime = permanent_handoff_runtime(seat)
    runtime_source_versions(runtime, owner['harness'])
    attachment_summaries = seat_retirement_attachment_summary(identifier, seat)
    if (not isinstance(seat.get('hooks_installed'), bool) or
            not isinstance(seat.get('groups', {}), dict)):
        raise ValueError('The recorded seat hook ownership is malformed; preserve it and inspect status')
    adapter_path = session_store.seat_dir(identifier) / 'bin' / 'gh'
    if Path(seat.get('adapter', '')) != adapter_path:
        raise ValueError('The recorded seat adapter path differs from its durable identity; preserve it and inspect status')
    adapter_before = capture_text(adapter_path)
    if (not adapter_before['exists'] or adapter_before['mode'] != 0o700 or
            adapter_before['sha256'] != seat.get('adapter_sha256')):
        raise ValueError('The recorded seat adapter is missing or changed; preserve it and inspect status')
    real_gh = seat.get('real_gh')
    if (not isinstance(real_gh, dict) or not isinstance(real_gh.get('path'), str) or
            captured_binary_identity(real_gh['path']) != real_gh):
        raise ValueError('The captured GitHub executable identity changed; preserve it and inspect status')

    groups = seat.get('groups', {})
    settings_before = None
    settings_after = None
    group_summaries = []
    if seat['hooks_installed']:
        settings_info = seat.get('settings')
        expected_settings_path = str(Path(anchor_root) / '.claude/settings.local.json')
        if (not isinstance(settings_info, dict) or settings_info.get('path') != expected_settings_path or
                not groups):
            raise ValueError('The installed seat hooks do not identify one exact owned settings path and group set')
        settings_path, settings_text, settings_value = settings(anchor_root)
        if str(settings_path) != expected_settings_path:
            raise ValueError('The Claude settings path differs from the active operational anchor')
        settings_before = capture_text(settings_path, settings_info.get('mode', 0o600))
        if (not settings_before['exists'] or settings_before['text'] != settings_text or
                settings_before['mode'] != settings_info.get('mode')):
            raise ValueError('Claude settings existence, contents or mode changed; coordinate the settings writer and inspect again')
        hooks = settings_value.get('hooks', {})
        if not isinstance(hooks, dict):
            raise ValueError('Claude settings hook map is malformed; preserve settings and inspect status')
        for event, group in sorted(groups.items()):
            if not isinstance(event, str) or not isinstance(group, dict):
                raise ValueError('The recorded seat hook group is malformed; preserve it and inspect status')
            existing = hooks.get(event, [])
            if not isinstance(existing, list) or existing.count(group) != 1:
                raise ValueError('Each owned Claude hook group must be present exactly once before seat retirement')
            commands = hook_commands(group)
            if any(commands.intersection(hook_commands(other)) and other != group for other in existing):
                raise ValueError('A Claude hook command was changed independently; preserve settings and inspect status')
            group_summaries.append({'event': event,
                                    'group_sha256': text_digest(json.dumps(
                                        group, sort_keys=True, separators=(',', ':'))),
                                    'occurrences': 1})
        settings_change = migration_settings_change(
            settings_path, {event: [group] for event, group in groups.items()}, {})
        settings_after = {'path': settings_change['path'], 'exists': True,
                          'sha256': settings_change['updated_sha256'],
                          'mode': settings_change['mode']}
    elif groups:
        raise ValueError('The seat records hook groups without installed hooks; preserve settings and inspect status')

    manifest_path = session_store.seat_dir(identifier) / 'seat.json'
    manifest_before = capture_text(manifest_path)
    if not manifest_before['exists']:
        raise ValueError('Permanent seat manifest disappeared during retirement planning')
    lock_ids = seat_retirement_lock_ids(identifier, anchor_root)
    plan = {'protocol': SEAT_RETIREMENT_PROTOCOL, 'seat_id': identifier,
            'birth_identity': {'seat_root': seat['seat_root'], 'seat_gitdir': seat.get('seat_gitdir'),
                               'seat_common_gitdir': seat.get('seat_common_gitdir')},
            'operational_anchor': anchor,
            'historical_owner': copy.deepcopy(owner), 'historical_owner_context': copy.deepcopy(owner_context),
            'expected_historical_owner': {'harness': expected_owner_harness,
                                          'session_id': expected_owner_session},
            'actual_operator': copy.deepcopy(operator), 'operator_context': copy.deepcopy(operator_context),
            'expected_operator': expected_operator,
            'authorization': {'reference': authorization_ref.strip(),
                              'confirmation_required': True,
                              'notice': 'The reference records existing authorization; Policy Guard does not authenticate it.'},
            'attachments': attachment_summaries,
            'effects': {'settings': ({'path': settings_before['path'],
                                     'preimage_sha256': settings_before['sha256'],
                                     'postimage_sha256': settings_after['sha256'],
                                     'mode': settings_before['mode'],
                                     'removed_groups': group_summaries}
                                    if settings_before else None),
                        'adapter': {'path': adapter_before['path'],
                                    'preimage_sha256': adapter_before['sha256'],
                                    'mode': adapter_before['mode'], 'remove': True},
                        'operational_anchor': {'before': 'active', 'after': 'released'},
                        'native_refresh_observed': False},
            'preimages': {'seat_manifest': {'path': str(manifest_path),
                                            'sha256': manifest_before['sha256'],
                                            'mode': manifest_before['mode']},
                          'settings': ({'path': settings_before['path'],
                                        'exists': settings_before['exists'],
                                        'sha256': settings_before['sha256'],
                                        'mode': settings_before['mode']}
                                       if settings_before else None),
                          'adapter': {'path': adapter_before['path'],
                                      'exists': adapter_before['exists'],
                                      'sha256': adapter_before['sha256'],
                                      'mode': adapter_before['mode']}},
            'lock_seat_ids': lock_ids,
            'history': {'attachments_preserved': True, 'owner_preserved': True,
                        'pull_request_records_preserved': True},
            'lifecycle_preconditions': {'install_journal': False, 'migration_journal': False,
                                        'handoff_journal': False, 'upgrade_journal': False,
                                        'seat_retirement_journal': False,
                                        'all_attachments_removed': True}}
    plan['plan_sha256'] = text_digest(json.dumps(plan, sort_keys=True, separators=(',', ':')))
    return plan


def retire_seat(args):
    identifier = selected_seat_id(args)
    parameters = {'expected_owner_harness': args.expected_owner_harness,
                  'expected_owner_session': args.expected_owner_session,
                  'expected_operator_harness': args.expected_operator_harness,
                  'expected_operator_session': args.expected_operator_session,
                  'authorization_ref': args.authorization_ref,
                  'confirmed': args.confirm_retirement}
    if args.apply and not args.plan_sha256:
        raise ValueError('retire-seat --apply requires --plan-sha256 from a reviewed retire-seat plan')
    if not args.apply and args.plan_sha256:
        raise ValueError('Use --apply with --plan-sha256 to apply a reviewed retire-seat plan')
    plan = build_retire_seat_plan(identifier, **parameters)
    if not args.apply:
        return plan
    if args.plan_sha256 != plan['plan_sha256']:
        raise ValueError('Seat retirement plan or a bound preimage changed; inspect a fresh retire-seat plan')
    anchor_root = plan['operational_anchor']['root']
    with seat_retirement_locks(identifier, anchor_root):
        current = build_retire_seat_plan(identifier, **parameters)
        if current['plan_sha256'] != args.plan_sha256:
            raise ValueError('Seat retirement plan or a bound preimage changed after locking; inspect a fresh plan')
        return apply_retire_seat_plan(current)


def apply_retire_seat_plan(plan):
    identifier = plan['seat_id']
    seat_path = session_store.seat_dir(identifier) / 'seat.json'
    adapter_path = Path(plan['effects']['adapter']['path'])
    settings_effect = plan['effects']['settings']
    settings_path = Path(settings_effect['path']) if settings_effect else None
    seat_before = capture_text(seat_path)
    adapter_before = capture_text(adapter_path)
    settings_before = capture_text(settings_path) if settings_path else None
    if (seat_before['exists'] != True or
            seat_before['sha256'] != plan['preimages']['seat_manifest']['sha256'] or
            seat_before['mode'] != plan['preimages']['seat_manifest']['mode'] or
            not adapter_before['exists'] or
            adapter_before['sha256'] != plan['preimages']['adapter']['sha256'] or
            adapter_before['mode'] != plan['preimages']['adapter']['mode'] or
            (settings_before and (settings_before['exists'] != plan['preimages']['settings']['exists'] or
                                  settings_before['sha256'] != plan['preimages']['settings']['sha256'] or
                                  settings_before['mode'] != plan['preimages']['settings']['mode']))):
        raise ValueError('A seat retirement preimage changed while the reviewed plan was being applied')
    seat = session_store.load_seat(identifier)
    retired_at = time.time()
    receipt = {'protocol': SEAT_RETIREMENT_PROTOCOL, 'seat_id': identifier,
               'retired_at': retired_at, 'plan_sha256': plan['plan_sha256'],
               'historical_owner': copy.deepcopy(seat['owner']),
               'operator': copy.deepcopy(plan['actual_operator']),
               'operator_context': copy.deepcopy(plan['operator_context']),
               'authorization_reference': plan['authorization']['reference'],
               'birth_identity': copy.deepcopy(plan['birth_identity']),
               'operational_anchor': copy.deepcopy(plan['operational_anchor']),
               'attachments_sha256': text_digest(json.dumps(plan['attachments'], sort_keys=True,
                                                             separators=(',', ':'))),
               'settings': copy.deepcopy(plan['effects']['settings']),
               'adapter': copy.deepcopy(plan['effects']['adapter']),
               'history_preserved': True,
               'native_refresh_observed': False,
               'notice': 'Managed settings and adapter removal are on disk; native hook refresh and detachment remain unobserved.'}
    seat_after = copy.deepcopy(seat)
    seat_after['operational_anchor'] = {'state': 'released', 'root': None,
                                        'gitdir': None, 'common_gitdir': None}
    seat_after['hooks_installed'] = False
    seat_after['groups'] = {}
    seat_after['retirement_receipt'] = receipt
    seat_after['updated_at'] = retired_at
    seat_after_text = json.dumps(seat_after, indent=2, sort_keys=True) + '\n'
    settings_after_text = None
    if settings_path:
        current_settings_path, current_settings_text, current_settings_value = settings(plan['operational_anchor']['root'])
        if str(current_settings_path) != str(settings_path):
            raise ValueError('The seat settings path changed after planning; inspect a fresh plan')
        settings_change = migration_settings_change(
            settings_path, {event: [group] for event, group in seat.get('groups', {}).items()}, {})
        if (settings_change['preimage_sha256'] != settings_before['sha256'] or
                settings_change['preimage_exists'] != settings_before['exists'] or
                settings_change['mode'] != settings_before['mode']):
            raise ValueError('Claude settings changed after planning; coordinate the settings writer and inspect again')
        settings_after_text = settings_change['updated']
        if settings_change['updated_sha256'] != plan['effects']['settings']['postimage_sha256']:
            raise ValueError('Claude settings removal differs from the reviewed retirement plan')
    journal = {'version': 1, 'protocol': SEAT_RETIREMENT_PROTOCOL, 'seat_id': identifier,
               'operator': copy.deepcopy(plan['actual_operator']),
               'operator_context': copy.deepcopy(plan['operator_context']),
               'plan_sha256': plan['plan_sha256'],
               'seat_manifest': {'path': str(seat_path), 'preimage': seat_before,
                                 'postimage': {'path': str(seat_path), 'exists': True, 'text': seat_after_text,
                                               'sha256': text_digest(seat_after_text),
                                               'mode': seat_before['mode']}},
               'adapter': {'path': str(adapter_path), 'preimage': adapter_before,
                           'postimage': {'path': str(adapter_path), 'exists': False, 'text': '', 'sha256': text_digest(''),
                                         'mode': adapter_before['mode']}},
               'settings': ({'path': str(settings_path), 'preimage': settings_before,
                             'postimage': {'path': str(settings_path), 'exists': True, 'text': settings_after_text,
                                           'sha256': text_digest(settings_after_text),
                                           'mode': settings_before['mode']}}
                            if settings_path else None)}
    journal_path = seat_retirement_journal_path(identifier)
    if journal_path.exists() or journal_path.is_symlink():
        raise ValueError('A seat retirement journal already exists; inspect and roll it back before applying')
    write(journal_path, json.dumps(journal, indent=2, sort_keys=True) + '\n', 0o600)
    if settings_path:
        current_settings = capture_text(settings_path, settings_before['mode'])
        if (current_settings['exists'] != settings_before['exists'] or
                current_settings['sha256'] != settings_before['sha256'] or
                current_settings['mode'] != settings_before['mode']):
            raise ValueError('Claude settings changed after the retirement journal was prepared; preserve the journal and inspect again')
        write(settings_path, settings_after_text, settings_before['mode'])
    current_adapter = capture_text(adapter_path, adapter_before['mode'])
    if (current_adapter['exists'] != adapter_before['exists'] or
            current_adapter['sha256'] != adapter_before['sha256'] or
            current_adapter['mode'] != adapter_before['mode']):
        raise ValueError('The seat adapter changed after the retirement journal was prepared; preserve it and inspect manually')
    adapter_path.unlink()
    sync_directory(adapter_path.parent)
    current_manifest = capture_text(seat_path, seat_before['mode'])
    if (current_manifest['exists'] != seat_before['exists'] or
            current_manifest['sha256'] != seat_before['sha256'] or
            current_manifest['mode'] != seat_before['mode']):
        raise ValueError('The seat manifest changed during retirement; preserve the journal and inspect manually')
    write(seat_path, seat_after_text, seat_before['mode'])
    journal_path.unlink()
    sync_directory(journal_path.parent)
    return {'retired': True, 'already_retired': False, 'seat_id': identifier,
            'history_preserved': True, 'plan_sha256': plan['plan_sha256'],
            'retirement_receipt': receipt,
            'notice': 'The exact seat adapter and owned hook groups were removed on disk. Native hook refresh and detachment remain unobserved.'}


def build_upgrade_plan(cwd, *, replacement_seat_root=None, release_anchor_seat=None, exact_seat_id=None):
    product = root(cwd)
    ensure_no_upgrade_journal(product)
    state_path = directory(product) / 'state.json'
    state_file = capture_text(state_path)
    if not state_file['exists']:
        raise ValueError('No active installation state to upgrade')
    state = json.loads(state_file['text'])
    if release_anchor_seat and replacement_seat_root is None:
        raise ValueError('--release-anchor-seat requires --replacement-seat-root')
    if exact_seat_id and exact_seat_id != state.get('seat_id'):
        raise ValueError('The selected --seat-id differs from this active checkout state')
    product, seat = validate_upgrade_scope(state, product, replacement_seat_root, exact_seat_id)
    if source_is_immutable_runtime():
        require_intact_upgrade_source_pin(state)
        raise ValueError(upgrade_source_guidance_reason(immutable_runtime=True))
    try:
        source_dirty = delivery.value(APP, 'status', '--porcelain')
    except ValueError:
        require_intact_upgrade_source_pin(state)
        raise ValueError(upgrade_source_guidance_reason()) from None
    if source_dirty:
        raise ValueError('Commit the new installer source, then review a fresh upgrade plan')
    try:
        new_revision = delivery.value(APP, 'rev-parse', 'HEAD')
    except ValueError:
        require_intact_upgrade_source_pin(state)
        raise ValueError(upgrade_source_guidance_reason()) from None
    new_versions = source_versions(state.get('lane_owner', {}).get('harness'))
    old_pin = require_intact_upgrade_source_pin(state)
    runtime_plan = session_store.runtime_plan(APP) if state.get('seat_id') else None
    seat_manifest_before = None
    selected_runtime_missing = False
    if runtime_plan:
        seat_manifest_before = session_store.load_seat(state['seat_id'])
        if not seat_manifest_before:
            raise ValueError('Permanent seat registry is missing; preserve checkout history and inspect recovery')
        selected_runtime_missing = validate_upgrade_seat_runtime(
            state, seat_manifest_before, seat, replacing_anchor=replacement_seat_root is not None)
    anchor_transition = None
    if replacement_seat_root is not None:
        if not runtime_plan or not seat_manifest_before:
            raise ValueError('Replacement-anchor recovery requires a durable seat with an intact pinned runtime')
        permanent_handoff_runtime(seat_manifest_before)
        if (seat_manifest_before.get('adapter_sha256') != state.get('adapter_sha256') or
                seat_manifest_before.get('adapter') != state.get('adapter')):
            raise ValueError('Source seat adapter pin differs between its manifest and active checkout')
        anchor_transition = replacement_anchor_plan(
            state, seat_manifest_before, replacement_seat_root, release_anchor_seat)
        anchor_transition['target_manifest_file_before'] = capture_text(
            session_store.seat_dir(state['seat_id']) / 'seat.json')
    new_source = {'source': runtime_plan['path'] if runtime_plan else str(APP),
                  'source_revision': ('runtime:' + runtime_plan['version']) if runtime_plan else new_revision,
                  'source_versions': new_versions}
    if all(state.get(key) == new_source[key] for key in new_source):
        raise ValueError('This installation already uses the executing source pin')
    if not isinstance(state.get('real_gh'), str) or not state['real_gh']:
        raise ValueError('No captured GitHub executable; inspect the active installation before upgrading')
    captured_gh = captured_binary_identity(state['real_gh'])

    adapter_path = Path(state.get('adapter', ''))
    expected_adapter_path = managed_adapter_path(state, product)
    if adapter_path != expected_adapter_path:
        raise ValueError('Managed adapter path changed; inspect the active installation before upgrading')
    adapter_before = capture_text(adapter_path)
    if not adapter_before['exists'] or adapter_before['mode'] != 0o700 or \
            adapter_before['sha256'] != state.get('adapter_sha256'):
        raise ValueError('Managed GitHub adapter is missing or changed; refusing to replace it')
    new_adapter_text = (adapter_script(product,seat_identifier=state['seat_id'],
                        runtime_cli=Path(runtime_plan['path'])/'lane.py',real_gh=state['real_gh'])
                        if runtime_plan else adapter_script(product))
    new_adapter_hash = text_digest(new_adapter_text)

    manages_hooks = state.get('manages_claude_hooks', True)
    old_groups = state.get('groups', {}) if manages_hooks else {}
    if not isinstance(old_groups, dict):
        raise ValueError('Recorded managed hook groups are malformed; inspect state before upgrade')
    new_groups = {}
    settings_before = None
    settings_after_text = None
    settings_after_exists = None
    settings_after_mode = None
    selected_settings_mode_repair = None
    if manages_hooks:
        settings_path, settings_text, settings_value = settings(seat)
        if anchor_transition:
            if anchor_transition.get('release_settings_before') and \
                    str(settings_path) != anchor_transition['release_settings_before']['path']:
                raise ValueError('Replacement root settings differ from the released seat settings path')
        elif str(settings_path) != state.get('settings'):
            raise ValueError('Managed settings path changed; inspect status before upgrade')
        settings_before = capture_text(settings_path, state.get('settings_mode', 0o600))
        if settings_before['text'] != settings_text:
            raise ValueError('Claude settings changed during planning; retry at the coordinated single-writer checkpoint')
        if selected_runtime_missing:
            selected_settings_mode_repair = migrated_settings_mode_repair(
                state, seat_manifest_before, selected_runtime_missing, settings_before,
                relocated_settings_path=(settings_before['path'] if anchor_transition else None))
        expected_settings_mode = (selected_settings_mode_repair if selected_settings_mode_repair is not None
                                  else state.get('settings_mode', 0o600))
        if anchor_transition and anchor_transition.get('release_settings_before'):
            expected_settings_mode = anchor_transition['release_settings_before']['mode']
        if settings_before['mode'] != expected_settings_mode:
            raise ValueError('Claude settings mode changed since installation; coordinate the settings writer before upgrade')
        if old_groups:
            available_groups = (hook_groups(product,seat,seat_identifier=state['seat_id'],
                                runtime_cli=Path(runtime_plan['path'])/'lane.py') if runtime_plan
                                else hook_groups(product, seat))
            if not set(old_groups).issubset(available_groups):
                raise ValueError('Recorded managed hook events are unsupported by this source version')
            new_groups = {event: available_groups[event] for event in old_groups}
        if anchor_transition:
            release_groups = anchor_transition['release_groups'] if \
                anchor_transition['release_seat_before'].get('hooks_installed') else {}
            for event, group in release_groups.items():
                groups = settings_value.get('hooks', {}).get(event, [])
                if not isinstance(groups, list) or groups.count(group) != 1:
                    raise ValueError('Released seat hook preimage changed after planning; inspect its settings')
            additions = {event: [group] for event, group in new_groups.items()}
            cleanup = migration_settings_change(
                settings_path, {event: [group] for event, group in release_groups.items()}, {})
            settings_baseline_text = cleanup['updated']
            settings_baseline_value = json.loads(settings_baseline_text)
            for event, group in new_groups.items():
                if group in settings_baseline_value.get('hooks', {}).get(event, []):
                    raise ValueError('Target seat hook already exists at the replacement root')
            settings_change = migration_settings_change(
                settings_path, {event: [group] for event, group in release_groups.items()}, additions)
            replacement = json.loads(settings_change['updated'])
            anchor_transition['settings_baseline_text'] = settings_baseline_text
            anchor_transition['settings_baseline_sha256'] = text_digest(settings_baseline_text)
            anchor_transition['settings_baseline_value'] = settings_baseline_value
            anchor_transition['settings_backup_path'] = str(
                session_store.seat_dir(state['seat_id']) / 'settings-backups' /
                (anchor_transition['settings_baseline_sha256'] + '.json'))
            anchor_transition['settings_backup_before'] = capture_text(
                anchor_transition['settings_backup_path'])
            if (anchor_transition['settings_backup_before']['exists'] and
                    (anchor_transition['settings_backup_before']['text'] != settings_baseline_text or
                     anchor_transition['settings_backup_before']['mode'] != 0o600)):
                raise ValueError('Target settings backup path already contains different history; preserve it and inspect')
            settings_after_text = json.dumps(replacement, indent=2) + '\n'
            settings_after_exists = True
        elif old_groups:
            replacement = replace_exact_hook_groups(settings_value, old_groups, new_groups)
            settings_after_text = json.dumps(replacement, indent=2) + '\n'
            settings_after_exists = True
        else:
            # Never expand an installation whose recorded hook ownership is empty.
            new_groups = {}
            settings_after_text = settings_text
            settings_after_exists = settings_before['exists']
        settings_after_mode = settings_before['mode']

    new_state = upgraded_state(state, new_source, new_groups, new_adapter_hash)
    if anchor_transition:
        new_state = apply_anchor_transition_state(
            new_state, anchor_transition, settings_after_text, settings_after_mode,
            anchor_transition.get('settings_baseline_text'),
            anchor_transition.get('settings_baseline_value', {}))
    new_state['phase'] = 'active'
    metadata_repairs = []
    seat_manifest_after=None
    if runtime_plan:
        seat_manifest_after=copy.deepcopy(seat_manifest_before)
        seat_manifest_after['runtime']={'version':runtime_plan['version'],'path':runtime_plan['path'],
            'files':runtime_plan['files'],'source_revision':'runtime:'+runtime_plan['version']}
        seat_manifest_after['adapter_sha256']=new_adapter_hash
        new_state['runtime']={'version':runtime_plan['version'],'path':runtime_plan['path'],
                              'files':runtime_plan['files']}
    if anchor_transition:
        seat_manifest_after['operational_anchor'] = {
            'state': 'active', 'root': anchor_transition['replacement_root'],
            'gitdir': anchor_transition['replacement_gitdir'],
            'common_gitdir': anchor_transition['replacement_common_gitdir']}
        if manages_hooks and anchor_transition.get('settings_baseline_text') is not None:
            baseline_value = anchor_transition['settings_baseline_value']
            seat_manifest_after['settings'] = {
                'path': str(Path(anchor_transition['replacement_root']) / '.claude/settings.local.json'),
                'backup': anchor_transition['settings_backup_path'],
                'sha256': anchor_transition['settings_baseline_sha256'],
                'existed': settings_before['exists'], 'mode': settings_before['mode'],
                'original_hook_events': list(baseline_value.get('hooks', {})),
                'original_had_hooks': 'hooks' in baseline_value}
            seat_manifest_after['groups'] = copy.deepcopy(new_groups)
            seat_manifest_after['hooks_installed'] = bool(new_groups)
    installing_state = copy.deepcopy(new_state)
    installing_state['phase'] = 'installing'
    active_state_text = json.dumps(new_state, indent=2) + '\n'
    installing_state_text = json.dumps(installing_state, indent=2) + '\n'
    checkout_states=[{'path':str(state_path),'before':state_file,
        'installing_text':installing_state_text,'active_text':active_state_text}]
    if runtime_plan:
        checkout_states=[]
        bindings=[]
        for item in session_store.all_checkouts(state['seat_id']):
            prior=item.get('state')
            if prior is None:
                raise ValueError('A registered checkout state is missing; preserve the seat and recover before upgrading')
            phase=prior.get('phase')
            if phase=='removed':
                continue
            if phase!='active':
                raise ValueError('A registered checkout has an interrupted or unknown phase; recover before upgrading the seat')
            product_path=Path(prior.get('root',''))
            try:
                checkout_available = (product_path.is_dir() and root(product_path) == product_path.resolve() and
                                      branch(product_path) == prior.get('branch'))
                if checkout_available:
                    observed_gitdir = delivery.value(product_path, 'rev-parse', '--absolute-git-dir')
                    observed_common_gitdir = delivery.value(
                        product_path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
                    observed_repository = github.origin_repo(
                        delivery.value(product_path, 'remote', 'get-url', 'origin'),
                        prior.get('lane_owner', {}).get('host', 'github.com')).casefold()
                    checkout_available = (observed_gitdir == item.get('gitdir') and
                                          observed_common_gitdir == item.get('common_gitdir') and
                                          observed_repository == item.get('repository'))
            except (OSError, ValueError):
                checkout_available = False
            if not checkout_available:
                retire_command = shlex.join([sys.executable, str(SELF), '--seat-id', state['seat_id'],
                                             'retire-attachment', '--checkout', str(product_path)])
                raise ValueError('An active seat checkout is unavailable or on another branch; when its recorded path is missing or no longer identifies its recorded worktree, review ' +
                                 retire_command + '. If the recorded worktree exists on another branch, restore its recorded branch and run uninstall from that checkout')
            lane_scope.check_owner(prior)
            if anchor_transition:
                prior_owner = prior.get('lane_owner', {})
                if (prior.get('session_root') != anchor_transition['birth_root'] or
                        prior_owner.get('seat_root') != anchor_transition['birth_root'] or
                        prior_owner.get('seat_gitdir') != anchor_transition['birth_gitdir'] or
                        prior.get('seat_id') != state['seat_id']):
                    raise ValueError('An active target checkout does not retain the exact missing birth-anchor identity')
                if (state.get('lane_owner', {}).get('harness') != prior_owner.get('harness') or
                        state.get('lane_owner', {}).get('session_id') != prior_owner.get('session_id')):
                    raise ValueError('Active target checkout owners differ; preserve the seat and inspect status')
                if (prior.get('root') != str(root(product_path)) or
                        prior_owner.get('product_gitdir') != item.get('gitdir') or
                        prior_owner.get('repository', '').casefold() != item.get('repository', '').casefold() or
                        prior_owner.get('host') != item.get('host')):
                    raise ValueError('An active target checkout does not match its recorded owner or destination settings metadata')
            else:
                check_lane_identity(prior,product_path)
            prior_runtime_missing = validate_checkout_runtime(prior, seat_manifest_before)
            if source_pin_status(prior)['integrity']!='intact':
                raise ValueError('An active seat checkout source pin is unavailable; preserve it and recover before upgrade')
            bindings.extend(upgrade_binding_summary(prior))
            next_state=upgraded_state(prior,new_source,new_groups,new_adapter_hash)
            if anchor_transition:
                next_state = apply_anchor_transition_state(
                    next_state, anchor_transition, settings_after_text, settings_after_mode,
                    anchor_transition.get('settings_baseline_text'),
                    anchor_transition.get('settings_baseline_value', {}))
            next_state['phase']='active'
            if prior_runtime_missing:
                metadata_repairs.append({'checkout':str(product_path),'field':'runtime',
                    'before':None,'after':{'version':runtime_plan['version'],'path':runtime_plan['path']}})
                prior_mode_repair = None
                if prior.get('manages_claude_hooks', True):
                    prior_settings_path = prior.get('settings')
                    if not settings_before or (not anchor_transition and prior_settings_path != settings_before['path']):
                        raise ValueError('Migrated checkout settings path differs from the permanent seat; preserve it and inspect status')
                    prior_mode_repair = migrated_settings_mode_repair(
                        prior, seat_manifest_before, prior_runtime_missing, settings_before,
                        relocated_settings_path=(settings_before['path'] if anchor_transition else None))
                    if prior_mode_repair is not None:
                        next_state['settings_mode'] = prior_mode_repair
                        metadata_repairs.append({'checkout':str(product_path),'field':'settings_mode',
                            'before':prior.get('settings_mode',0o600),'after':prior_mode_repair})
            next_state['runtime']={'version':runtime_plan['version'],'path':runtime_plan['path'],
                                   'files':runtime_plan['files']}
            installing=copy.deepcopy(next_state)
            installing['phase']='installing'
            before=capture_text(Path(item['checkout_dir'])/'state.json')
            if (not before['exists'] or before['sha256']!=delivery_runs.digest(Path(item['checkout_dir'])/'state.json') or
                    prior.get('root')!=item.get('root')):
                raise ValueError('A seat checkout state changed during upgrade planning; review a fresh plan')
            checkout_states.append({'path':before['path'],'before':before,
                'installing_text':json.dumps(installing,indent=2)+'\n',
                'active_text':json.dumps(next_state,indent=2)+'\n'})
        current_row=next((row for row in checkout_states if row['path']==str(state_path)),None)
        if current_row is None:
            raise ValueError('The selected checkout is not an active attachment in its permanent seat')
        new_state=json.loads(current_row['active_text'])
        installing_state_text=current_row['installing_text']
        active_state_text=current_row['active_text']
        seat_manifest_after['groups']=copy.deepcopy(new_groups)
    state_after_hash = text_digest(active_state_text)
    state_installing_hash = text_digest(installing_state_text)
    settings_after_hash = (text_digest(settings_after_text) if settings_after_text is not None else None)

    if not runtime_plan:
        bindings = upgrade_binding_summary(state)
    anchor_transition_review = None
    if anchor_transition:
        release_before = anchor_transition.get('release_seat_before')
        release_after = anchor_transition.get('release_seat_after')
        anchor_transition_review = {
            'target_seat_id': anchor_transition['target_seat_id'],
            'immutable_birth_root': anchor_transition['birth_root'],
            'immutable_birth_gitdir': anchor_transition['birth_gitdir'],
            'birth_common_gitdir_proof': anchor_transition['birth_common_gitdir'],
            'replacement_root': anchor_transition['replacement_root'],
            'replacement_seat_id': anchor_transition['replacement_seat_id'],
            'replacement_git_identity': {'gitdir': anchor_transition['replacement_gitdir'],
                                         'common_gitdir': anchor_transition['replacement_common_gitdir']},
            'released_seat_id': anchor_transition['release_seat_id'],
            'released_seat_manifest_before_sha256': (text_digest(json.dumps(
                release_before, indent=2, sort_keys=True) + '\n') if release_before else None),
            'target_manifest_file_before_sha256': anchor_transition['target_manifest_file_before']['sha256'],
            'target_manifest_file_before_mode': anchor_transition['target_manifest_file_before']['mode'],
            'released_manifest_file_before_sha256': (anchor_transition['release_manifest_file_before']['sha256']
                if anchor_transition.get('release_manifest_file_before') else None),
            'released_manifest_file_before_mode': (anchor_transition['release_manifest_file_before']['mode']
                if anchor_transition.get('release_manifest_file_before') else None),
            'released_seat_manifest_after_sha256': (text_digest(json.dumps(
                release_after, indent=2, sort_keys=True) + '\n') if release_after else None),
            'settings_backup_path': anchor_transition.get('settings_backup_path'),
            'settings_backup_sha256': anchor_transition.get('settings_baseline_sha256')}
    review = {
        'protocol': ('policy-guard-source-upgrade-v2' if anchor_transition else
                     'policy-guard-source-upgrade-v1'),
        'binding_type': 'native-owner-lane' if state.get('lane_owner') else 'legacy-per-pr',
        'identity': {'root': str(product), 'branch': branch(product), 'session_root': str(seat),
                     'product_git': git_path_identity(product), 'seat_git': git_path_identity(seat),
                     'lane_owner': copy.deepcopy(state.get('lane_owner')),
                     'owner_history': copy.deepcopy(state.get('owner_history', [])),
                     'pr_url': state.get('pr_url'), 'base_ref': state.get('base_ref'),
                     'head_branch': state.get('head_branch', state.get('branch')),
                     'branch_mappings': [{'branch': row['branch'], 'head_branch': row['head_branch']}
                                         for row in bindings]},
        'captured_real_gh': captured_gh,
        'installed_source_before': old_pin,
        'executing_source_after': {'path': new_source['source'], 'revision': new_source['source_revision'],
                                   'fingerprints': new_versions,
                                   'runtime_version':runtime_plan['version'] if runtime_plan else None},
        'bindings': bindings,
        'receipt_policy': 'Archive each binding receipt in that binding history and require a fresh report; preserve verification artifacts and fix counts.',
        'preimages': {
            'state': {'path': str(state_path), 'sha256': state_file['sha256'], 'mode': state_file['mode']},
            'states': [{'path':row['path'],'sha256':row['before']['sha256'],
                        'mode':row['before']['mode']} for row in checkout_states],
            'adapter': {'path': str(adapter_path), 'sha256': adapter_before['sha256'],
                        'mode': adapter_before['mode']},
            'settings': {'managed': bool(manages_hooks),
                         'path': settings_before['path'] if settings_before else None,
                         'exists': settings_before['exists'] if settings_before else False,
                         'sha256': settings_before['sha256'] if settings_before else None,
                         'mode': settings_before['mode'] if settings_before else None},
            'seat_manifest': {'managed':bool(runtime_plan),
                'path':str(session_store.seat_dir(state['seat_id'])/'seat.json') if runtime_plan else None,
                'sha256':(anchor_transition['target_manifest_file_before']['sha256'] if anchor_transition else
                    text_digest(json.dumps(seat_manifest_before,indent=2,sort_keys=True)+'\n'))
                    if seat_manifest_before else None,
                'mode':anchor_transition['target_manifest_file_before']['mode'] if anchor_transition else None},
            'released_seat_manifest': ({'path':str(session_store.seat_dir(anchor_transition['release_seat_id'])/'seat.json'),
                'sha256':anchor_transition['release_manifest_file_before']['sha256'],
                'mode':anchor_transition['release_manifest_file_before']['mode']}
                if anchor_transition and anchor_transition['release_seat_id'] else None),
            'settings_backup': ({'path':anchor_transition['settings_backup_path'],
                'exists':anchor_transition['settings_backup_before']['exists'],
                'sha256':anchor_transition['settings_backup_before']['sha256'],
                'mode':anchor_transition['settings_backup_before']['mode']}
                if anchor_transition and anchor_transition.get('settings_backup_before') else None)},
        'replacements': {
            'adapter': {'path': str(adapter_path), 'mode': 0o700, 'sha256': new_adapter_hash,
                        'content': new_adapter_text},
            'settings': {'managed': bool(manages_hooks),
                         'path': settings_before['path'] if settings_before else None,
                         'exists': settings_after_exists,
                         'sha256': settings_after_hash,
                         'mode': settings_after_mode,
                         'owned_hooks': {'remove': copy.deepcopy(old_groups),
                                         'add': copy.deepcopy(new_groups)} if manages_hooks else {}},
            'state': {'path': str(state_path), 'mode': state_file['mode'],
                      'installing_sha256': state_installing_hash, 'active_sha256': state_after_hash,
                      'phase_sequence': ['installing', 'active']},
            'states': [{'path':row['path'],'installing_sha256':text_digest(row['installing_text']),
                        'active_sha256':text_digest(row['active_text'])} for row in checkout_states],
            'seat_manifest': {'managed':bool(runtime_plan),
                'path':str(session_store.seat_dir(state['seat_id'])/'seat.json') if runtime_plan else None,
                'sha256':text_digest(json.dumps(seat_manifest_after,indent=2,sort_keys=True)+'\n')
                    if seat_manifest_after else None},
            'released_seat_manifest': ({'path':str(session_store.seat_dir(anchor_transition['release_seat_id'])/'seat.json'),
                'sha256':anchor_transition_review['released_seat_manifest_after_sha256']}
                if anchor_transition and anchor_transition['release_seat_id'] else None),
            'settings_backup': ({'path':anchor_transition['settings_backup_path'],
                'sha256':anchor_transition['settings_baseline_sha256']}
                if anchor_transition and anchor_transition.get('settings_backup_path') else None)},
        'anchor_transition': anchor_transition_review,
        'recovery': {'journal_path': str(upgrade_journal_path(product)),
                     'command': upgrade_recovery_command(product),
                     'strategy': 'Keep the journal guard until exact owned preimages are restored.'},
        'migration_metadata_recovery': metadata_repairs}
    review['plan_sha256'] = text_digest(json.dumps(review, sort_keys=True, separators=(',', ':')))
    payload = {'root': str(product), 'state': state, 'state_path': str(state_path),
               'state_before': state_file, 'state_installing_text': installing_state_text,
               'state_after_text': active_state_text, 'adapter_before': adapter_before,
               'adapter_after_text': new_adapter_text, 'adapter_after_mode': 0o700,
               'settings_before': settings_before, 'settings_after_text': settings_after_text,
               'settings_after_exists': settings_after_exists, 'settings_after_mode': settings_after_mode,
               'checkout_states':checkout_states,
               'runtime_plan':runtime_plan,'seat_manifest_before':seat_manifest_before,
               'seat_manifest_after':seat_manifest_after,
               'anchor_transition':anchor_transition,
               'settings_managed': bool(manages_hooks), 'old_groups': copy.deepcopy(old_groups),
               'new_groups': copy.deepcopy(new_groups),
               'lock_seat_ids': sorted(set(([state['seat_id']] if state.get('seat_id') else []) +
                   ([anchor_transition['replacement_seat_id']] if anchor_transition else []))),
               'review': review}
    return review, payload


def upgrade_plan(args):
    review, _ = build_upgrade_plan(args.cwd,
        replacement_seat_root=getattr(args, 'replacement_seat_root', None),
        release_anchor_seat=getattr(args, 'release_anchor_seat', None),
        exact_seat_id=getattr(args, 'seat_id', None))
    return review


def upgrade_journal(payload):
    settings_before = payload['settings_before']
    state_before = payload['state_before']
    adapter_before = payload['adapter_before']
    protocol = ('policy-guard-source-upgrade-v2' if payload.get('anchor_transition') else
                'policy-guard-source-upgrade-v1')
    return {'version': 2 if payload.get('anchor_transition') else 1, 'protocol': protocol,
            'root': payload['root'], 'binding_type': payload['review']['binding_type'],
            'git_identity': {'product': payload['review']['identity']['product_git'],
                             'seat': payload['review']['identity']['seat_git'],
                             'branch': payload['review']['identity']['branch']},
            'captured_real_gh': copy.deepcopy(payload['review']['captured_real_gh']),
            'owner': copy.deepcopy(payload['state'].get('lane_owner')),
            'state': {'path': payload['state_path'], 'preimage': state_before,
                      'installing_sha256': text_digest(payload['state_installing_text']),
                      'active_sha256': text_digest(payload['state_after_text']),
                      'mode': state_before['mode']},
            'checkout_states': [{'path':row['path'],'preimage':row['before'],
                'installing_sha256':text_digest(row['installing_text']),
                'active_sha256':text_digest(row['active_text'])} for row in payload['checkout_states']],
            'adapter': {'path': adapter_before['path'], 'preimage': adapter_before,
                        'replacement_sha256': text_digest(payload['adapter_after_text']),
                        'replacement_mode': payload['adapter_after_mode']},
            'settings': {'managed': payload['settings_managed'],
                         'preimage': settings_before,
                         'replacement_sha256': (text_digest(payload['settings_after_text'])
                                                if payload['settings_after_text'] is not None else None),
                         'replacement_exists': payload['settings_after_exists'],
                         'replacement_mode': payload['settings_after_mode'],
                         'old_groups': payload['old_groups'], 'new_groups': payload['new_groups']},
            'seat_manifest': {'managed':bool(payload.get('runtime_plan')),
                'path':str(session_store.seat_dir(payload['state']['seat_id'])/'seat.json')
                    if payload.get('runtime_plan') else None,
                'preimage':payload.get('seat_manifest_before'),
                'preimage_sha256':(hashlib.sha256((json.dumps(payload['seat_manifest_before'],
                    indent=2,sort_keys=True)+'\n').encode()).hexdigest()
                    if payload.get('seat_manifest_before') else None),
                'postimage':payload.get('seat_manifest_after'),
                'postimage_sha256':(hashlib.sha256((json.dumps(payload['seat_manifest_after'],
                    indent=2,sort_keys=True)+'\n').encode()).hexdigest()
                    if payload.get('seat_manifest_after') else None)},
            'anchor_transition': (None if not payload.get('anchor_transition') else {
                'target_seat_id':payload['anchor_transition']['target_seat_id'],
                'replacement_seat_id':payload['anchor_transition']['replacement_seat_id'],
                'replacement_root':payload['anchor_transition']['replacement_root'],
                'replacement_gitdir':payload['anchor_transition']['replacement_gitdir'],
                'replacement_common_gitdir':payload['anchor_transition']['replacement_common_gitdir'],
                'birth_root':payload['anchor_transition']['birth_root'],
                'birth_gitdir':payload['anchor_transition']['birth_gitdir'],
                'birth_common_gitdir':payload['anchor_transition']['birth_common_gitdir'],
                'release_seat_id':payload['anchor_transition']['release_seat_id'],
                'target_manifest_preimage':payload.get('seat_manifest_before'),
                'target_manifest_preimage_sha256':(text_digest(json.dumps(
                    payload.get('seat_manifest_before'),indent=2,sort_keys=True)+'\n')
                    if payload.get('seat_manifest_before') else None),
                'target_manifest_postimage':payload.get('seat_manifest_after'),
                'target_manifest_postimage_sha256':(text_digest(json.dumps(
                    payload.get('seat_manifest_after'),indent=2,sort_keys=True)+'\n')
                    if payload.get('seat_manifest_after') else None),
                'target_manifest_file_preimage':payload['anchor_transition'].get('target_manifest_file_before'),
                'target_manifest_file_postimage':(json.dumps(
                    payload.get('seat_manifest_after'),indent=2,sort_keys=True)+'\n'
                    if payload.get('seat_manifest_after') else None),
                'release_manifest_preimage':payload['anchor_transition'].get('release_seat_before'),
                'release_manifest_preimage_sha256':(text_digest(json.dumps(
                    payload['anchor_transition'].get('release_seat_before'),indent=2,sort_keys=True)+'\n')
                    if payload['anchor_transition'].get('release_seat_before') else None),
                'release_manifest_postimage':payload['anchor_transition'].get('release_seat_after'),
                'release_manifest_postimage_sha256':(text_digest(json.dumps(
                    payload['anchor_transition'].get('release_seat_after'),indent=2,sort_keys=True)+'\n')
                    if payload['anchor_transition'].get('release_seat_after') else None),
                'release_manifest_file_preimage':payload['anchor_transition'].get('release_manifest_file_before'),
                'release_manifest_file_postimage':(json.dumps(
                    payload['anchor_transition'].get('release_seat_after'),indent=2,sort_keys=True)+'\n'
                    if payload['anchor_transition'].get('release_seat_after') else None),
                'settings_backup': ({'preimage':payload['anchor_transition']['settings_backup_before'],
                    'postimage':payload['anchor_transition']['settings_baseline_text'],
                    'postimage_sha256':payload['anchor_transition']['settings_baseline_sha256']}
                    if payload['anchor_transition'].get('settings_backup_path') else None)})}


def upgrade(args):
    review, payload = build_upgrade_plan(args.cwd,
        replacement_seat_root=getattr(args, 'replacement_seat_root', None),
        release_anchor_seat=getattr(args, 'release_anchor_seat', None),
        exact_seat_id=getattr(args, 'seat_id', None))
    if args.plan_sha256 != review['plan_sha256']:
        raise ValueError('Upgrade plan or a bound preimage changed; inspect a fresh upgrade-plan')
    product = Path(payload['root'])
    with source_upgrade_locked(product, payload.get('lock_seat_ids', [])):
        current_review, payload = build_upgrade_plan(product,
            replacement_seat_root=getattr(args, 'replacement_seat_root', None),
            release_anchor_seat=getattr(args, 'release_anchor_seat', None),
            exact_seat_id=getattr(args, 'seat_id', None))
        if args.plan_sha256 != current_review['plan_sha256']:
            raise ValueError('Upgrade plan or a bound preimage changed; inspect a fresh upgrade-plan')
        journal_path = upgrade_journal_path(product)
        if journal_path.exists() or journal_path.is_symlink():
            raise ValueError('An upgrade journal already exists; recover it before starting another upgrade')
        if payload.get('runtime_plan'):
            session_store.install_runtime(APP,payload['runtime_plan'])
        journal_text = json.dumps(upgrade_journal(payload), indent=2) + '\n'
        try:
            write(journal_path, journal_text, 0o600)
        except Exception as error:
            if journal_path.exists():
                raise ValueError('Upgrade journal exists but its durability could not be confirmed. Run ' +
                                 upgrade_recovery_command(product) + ' to restore the preimages. Cause: ' + str(error)) from error
            raise
        try:
            # This is the first managed-file replacement. The new adapter blocks covered
            # actions whenever the journal exists, including selector-less actions.
            write(payload['adapter_before']['path'], payload['adapter_after_text'],
                  payload['adapter_after_mode'])
            for row in payload['checkout_states']:
                write(row['path'],row['installing_text'],row['before']['mode'])
            transition = payload.get('anchor_transition')
            if transition and transition.get('settings_backup_path'):
                backup = transition['settings_backup_before']
                current_backup = capture_text(backup['path'], backup['mode'])
                if (current_backup['exists'] != backup['exists'] or
                        current_backup['sha256'] != backup['sha256'] or
                        current_backup['mode'] != backup['mode']):
                    raise ValueError('Target seat settings backup changed after planning; stop and recover')
                if not backup['exists']:
                    write(backup['path'], transition['settings_baseline_text'], 0o600)
            if payload['settings_managed'] and payload['settings_after_text'] is not None:
                settings_before = payload['settings_before']
                current = capture_text(settings_before['path'], settings_before['mode'])
                if (current['exists'] != settings_before['exists'] or
                        current['sha256'] != settings_before['sha256'] or
                        current['mode'] != settings_before['mode']):
                    raise ValueError('Claude settings changed after planning; stop and recover before retrying')
                if (payload['settings_after_exists'] != settings_before['exists'] or
                        payload['settings_after_text'] != settings_before['text']):
                    refuse_symlinked_path(settings_before['path'])
                    write(settings_before['path'], payload['settings_after_text'],
                          payload['settings_after_mode'])
            for row in payload['checkout_states']:
                write(row['path'],row['active_text'],row['before']['mode'])
            if payload.get('seat_manifest_after'):
                seat_info=json.loads(journal_text)['seat_manifest']
                manifest_path=Path(seat_info['path'])
                refuse_symlinked_path(manifest_path)
                current_manifest=session_store.load_seat(payload['state']['seat_id'])
                current_hash=hashlib.sha256((json.dumps(current_manifest,indent=2,sort_keys=True)+'\n').encode()).hexdigest()
                if current_hash!=seat_info['preimage_sha256']:
                    raise ValueError('Permanent seat changed during source upgrade; retain the journal and recover')
                session_store.save_seat(payload['seat_manifest_after'])
            if transition and transition.get('release_seat_after'):
                release_id = transition['release_seat_id']
                release_path = session_store.seat_dir(release_id) / 'seat.json'
                current_release = session_store.load_seat(release_id)
                current_hash = text_digest(json.dumps(current_release, indent=2, sort_keys=True) + '\n')
                expected_hash = text_digest(json.dumps(transition['release_seat_before'],
                                                      indent=2, sort_keys=True) + '\n')
                if current_hash != expected_hash:
                    raise ValueError('Destination seat changed during source upgrade; retain the journal and recover')
                session_store.save_seat(transition['release_seat_after'])
            journal_path.unlink()
            sync_directory(journal_path.parent)
        except Exception as error:
            if not journal_path.exists():
                try:
                    write(journal_path, journal_text, 0o600)
                except Exception:
                    pass
            raise ValueError('Source upgrade is incomplete; retain the single-writer hold and run ' +
                             upgrade_recovery_command(product) + ' to restore exact preimages. Cause: ' + str(error)) from error
    return {'upgraded': True, 'plan_sha256': review['plan_sha256'],
            'source_revision': review['executing_source_after']['revision'],
            'history_preserved': True, 'fresh_report_required': any(
                row['receipt_will_be_archived'] for row in review['bindings']),
            'operational_anchor_replaced': bool(payload.get('anchor_transition')),
            'adapter': review['replacements']['adapter']['path'],
            'notice': 'Source upgrade only; no verification or audit was run. Native activation still requires observation in the actual owner session.'}


def recover_settings(journal_settings):
    before = journal_settings['preimage']
    path = before['path']
    current = capture_text(path, before['mode'])
    if current['exists'] == before['exists'] and current['sha256'] == before['sha256'] and current['mode'] == before['mode']:
        return None
    if current['sha256'] == journal_settings['replacement_sha256'] and \
            current['exists'] == journal_settings['replacement_exists'] and \
            current['mode'] == journal_settings['replacement_mode']:
        return {'exists': before['exists'], 'text': before['text'], 'mode': before['mode']}
    if not current['exists'] or current['mode'] != journal_settings['replacement_mode']:
        raise ValueError('Claude settings changed independently; journal retained and adapter remains guarded')
    try:
        value = json.loads(current['text'])
    except ValueError as error:
        raise ValueError('Claude settings are no longer valid JSON; journal retained and adapter remains guarded') from error
    if not isinstance(value, dict) or not isinstance(value.get('hooks', {}), dict):
        raise ValueError('Claude settings shape changed independently; journal retained and adapter remains guarded')
    hooks = value.get('hooks', {})
    for event, new_group in journal_settings['new_groups'].items():
        old_group = journal_settings['old_groups'].get(event)
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or old_group is None:
            raise ValueError('Managed ' + event + ' hook ownership changed; journal retained and adapter remains guarded')
        new_positions = [index for index, group in enumerate(groups) if group == new_group]
        old_positions = [index for index, group in enumerate(groups) if group == old_group]
        commands = hook_commands(new_group) | hook_commands(old_group)
        if any(index not in new_positions and index not in old_positions and commands.intersection(hook_commands(group))
               for index, group in enumerate(groups)):
            raise ValueError('Managed ' + event + ' hook changed independently; journal retained and adapter remains guarded')
        if len(new_positions) == 1 and not old_positions:
            groups[new_positions[0]] = copy.deepcopy(old_group)
        elif not new_positions and len(old_positions) == 1:
            continue
        else:
            raise ValueError('Managed ' + event + ' hook is missing, duplicated, or changed; journal retained and adapter remains guarded')
    if not hooks and not before.get('exists'):
        value.pop('hooks', None)
    return {'exists': True, 'text': json.dumps(value, indent=2) + '\n', 'mode': current['mode']}


def validate_journal_preimage(image, expected_path):
    if not isinstance(image, dict) or image.get('path') != str(expected_path) or not isinstance(image.get('text'), str):
        raise ValueError('Upgrade journal preimage identity is invalid; leave the adapter guarded for owner review')
    if image.get('sha256') != text_digest(image['text']) or not isinstance(image.get('mode'), int):
        raise ValueError('Upgrade journal preimage digest or mode is invalid; leave the adapter guarded for owner review')
    if not isinstance(image.get('exists'), bool):
        raise ValueError('Upgrade journal preimage existence is invalid; leave the adapter guarded for owner review')


def upgrade_recover(args):
    product = root(args.cwd)
    journal_path = upgrade_journal_path(product)
    lock_ids = []
    if journal_path.exists() and not journal_path.is_symlink():
        try:
            journal_hint = json.loads(journal_path.read_text())
            transition_hint = journal_hint.get('anchor_transition') or {}
            lock_ids = [value for value in (transition_hint.get('target_seat_id'),
                transition_hint.get('replacement_seat_id'), transition_hint.get('release_seat_id')) if value]
            if not lock_ids and journal_hint.get('seat_manifest', {}).get('managed'):
                state_hint = json.loads(journal_hint['state']['preimage']['text'])
                if state_hint.get('seat_id'):
                    lock_ids = [state_hint['seat_id']]
        except (OSError, TypeError, ValueError, KeyError):
            lock_ids = []
    with source_upgrade_locked(product, lock_ids):
        if not journal_path.exists() and not journal_path.is_symlink():
            return {'recovered': False, 'already_clean': True,
                    'notice': 'No pending source-upgrade journal exists.'}
        if journal_path.is_symlink():
            raise ValueError('Upgrade journal is symlinked; leave it and the adapter untouched for owner review')
        if journal_path.stat().st_mode & 0o777 != 0o600:
            raise ValueError('Upgrade journal mode changed; leave it and the adapter untouched for owner review')
        try:
            journal = json.loads(journal_path.read_text())
        except (OSError, ValueError) as error:
            raise ValueError('Upgrade journal is unreadable; leave it and the adapter untouched for owner review') from error
        if (journal.get('version'), journal.get('protocol')) not in (
                (1, 'policy-guard-source-upgrade-v1'), (2, 'policy-guard-source-upgrade-v2')):
            raise ValueError('Upgrade journal version is unsupported; leave the adapter guarded for owner review')
        if ((journal.get('version') == 2) != bool(journal.get('anchor_transition'))):
            raise ValueError('Upgrade journal anchor-transition protocol is malformed; leave the adapter guarded for owner review')
        if journal.get('root') != str(product):
            raise ValueError('Upgrade journal root differs from this product worktree; leave it and the adapter untouched')
        state_path = directory(product) / 'state.json'
        state_info = journal['state']
        adapter_info = journal['adapter']
        settings_info = journal['settings']
        validate_journal_preimage(state_info.get('preimage'), state_path)
        try:
            original_state = json.loads(state_info['preimage']['text'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Upgrade journal state preimage is invalid; leave the adapter guarded') from error
        if not isinstance(original_state, dict):
            raise ValueError('Upgrade journal state preimage is invalid; leave the adapter guarded')
        adapter_path = managed_adapter_path(original_state, product)
        if state_info.get('path') != str(state_path) or adapter_info.get('path') != str(adapter_path):
            raise ValueError('Upgrade journal paths do not match the installed lane; leave the adapter guarded')
        validate_journal_preimage(adapter_info.get('preimage'), adapter_path)
        if state_info.get('mode') != state_info['preimage'].get('mode') or \
                adapter_info.get('replacement_mode') != adapter_info['preimage'].get('mode'):
            raise ValueError('Upgrade journal file modes are inconsistent; leave the adapter guarded for owner review')
        if original_state.get('lane_owner'):
            lane_scope.check_owner(original_state)
        if original_state.get('root') != str(product) or original_state.get('phase') != 'active':
            raise ValueError('Recorded preimage is not the original active installation; journal retained')
        transition = journal.get('anchor_transition')
        if transition:
            birth_root = transition.get('birth_root')
            birth_gitdir = transition.get('birth_gitdir')
            replacement_root = transition.get('replacement_root')
            if (original_state.get('session_root') != birth_root or
                    original_state.get('lane_owner', {}).get('seat_root') != birth_root or
                    original_state.get('lane_owner', {}).get('seat_gitdir') != birth_gitdir or
                    os.path.lexists(birth_root) or os.path.lexists(birth_gitdir)):
                raise ValueError('The immutable birth root or Gitdir changed during anchor rollback; preserve the journal')
            replacement = root(replacement_root)
            replacement_identity = git_path_identity(replacement)
            if (str(replacement) != replacement_root or
                    replacement_identity.get('gitdir') != transition.get('replacement_gitdir') or
                    replacement_identity.get('common_gitdir') != transition.get('replacement_common_gitdir') or
                    transition.get('birth_common_gitdir') != transition.get('replacement_common_gitdir')):
                raise ValueError('Replacement Git identity changed during anchor rollback; keep the journal gate for review')
            seat = replacement
        else:
            seat = root(original_state.get('session_root', original_state['root']))
        current_git_identity = {'product': git_path_identity(product), 'seat': git_path_identity(seat),
                                'branch': branch(product)}
        if journal.get('git_identity') != current_git_identity:
            raise ValueError('Product or owner seat Git identity changed; keep the journal gate and recover from the recorded worktree')
        if source_pin_status(original_state)['integrity'] != 'intact':
            raise ValueError('Previous source pin changed during upgrade; keep the journal gate for owner review')
        if captured_binary_identity(original_state['real_gh']) != journal.get('captured_real_gh'):
            raise ValueError('Captured GitHub executable changed during upgrade; keep the journal gate for owner review')

        state_now = capture_text(state_path)
        allowed_state_hashes = {state_info['preimage']['sha256'], state_info['installing_sha256'],
                                state_info['active_sha256']}
        if state_now['sha256'] not in allowed_state_hashes or state_now['mode'] != state_info['mode']:
            raise ValueError('Managed state changed independently; journal retained and adapter remains guarded')
        checkout_rows=journal.get('checkout_states') or [{'path':state_info['path'],
            'preimage':state_info['preimage'],'installing_sha256':state_info['installing_sha256'],
            'active_sha256':state_info['active_sha256']}]
        seat_manifest_now=None
        if original_state.get('seat_id'):
            seat_manifest_now=session_store.load_seat(original_state['seat_id'])
            if not seat_manifest_now:
                raise ValueError('Permanent seat registry disappeared during source upgrade; preserve the journal')
        secondary_state_paths=[]
        for row in checkout_rows:
            row_path=Path(row['path'])
            refuse_symlinked_path(row_path)
            validate_journal_preimage(row.get('preimage'),row_path)
            if original_state.get('seat_id'):
                seat_dir=session_store.seat_dir(original_state['seat_id']).resolve()
                if not row_path.resolve().is_relative_to(seat_dir):
                    raise ValueError('Source upgrade journal points outside the permanent seat; preserve it for review')
                checkout_id=row_path.parent.name
                attachment=seat_manifest_now.get('checkouts',{}).get(checkout_id)
                if not attachment or attachment.get('state_file')!=str(row_path):
                    raise ValueError('Source upgrade journal checkout identity differs from the seat registry')
                prior_state=json.loads(row['preimage']['text'])
                if prior_state.get('seat_id')!=original_state['seat_id'] or prior_state.get('root')!=attachment.get('root'):
                    raise ValueError('Source upgrade journal preimage belongs to another checkout')
                if prior_state.get('lane_owner'):
                    lane_scope.check_owner(prior_state)
            elif row_path!=state_path:
                raise ValueError('Legacy source upgrade journal contains an unexpected checkout state')
            current=capture_text(row_path,row['preimage']['mode'])
            allowed={row['preimage']['sha256'],row['installing_sha256'],row['active_sha256']}
            if not current['exists'] or current['sha256'] not in allowed or current['mode']!=row['preimage']['mode']:
                raise ValueError('A registered checkout state changed independently; journal retained and adapter remains guarded')
            if row_path!=state_path:
                secondary_state_paths.append((row_path,row['preimage']))
        adapter_now = capture_text(adapter_path)
        allowed_adapter = {adapter_info['preimage']['sha256'], adapter_info['replacement_sha256']}
        if adapter_now['sha256'] not in allowed_adapter or adapter_now['mode'] not in (
                adapter_info['preimage']['mode'], adapter_info['replacement_mode']):
            raise ValueError('Managed adapter changed independently; journal retained and recovery refused')
        settings_restore = None
        settings_backup_restore = None
        if settings_info.get('managed'):
            settings_expected_path = (str(Path(transition['replacement_root']) / '.claude/settings.local.json')
                                      if transition else original_state.get('settings'))
            validate_journal_preimage(settings_info.get('preimage'), settings_expected_path)
            if settings_info.get('replacement_mode') != settings_info['preimage'].get('mode'):
                raise ValueError('Upgrade journal settings modes are inconsistent; leave the adapter guarded for owner review')
            if not transition and settings_info['preimage']['path'] != original_state.get('settings'):
                raise ValueError('Upgrade journal settings path differs from the recorded owner settings')
            if transition:
                preimage = settings_info['preimage']
                settings_path = Path(preimage['path'])
                current_settings = capture_text(settings_path, preimage['mode'])
                is_preimage = (current_settings['exists'] == preimage['exists'] and
                               current_settings['sha256'] == preimage['sha256'] and
                               current_settings['mode'] == preimage['mode'])
                is_postimage = (current_settings['exists'] == settings_info['replacement_exists'] and
                                current_settings['sha256'] == settings_info['replacement_sha256'] and
                                current_settings['mode'] == settings_info['replacement_mode'])
                if not is_preimage and not is_postimage:
                    raise ValueError('Replacement settings changed independently; journal retained and adapter remains guarded')
                try:
                    previous_settings = json.loads(preimage['text']) if preimage['exists'] else {}
                except ValueError as error:
                    raise ValueError('Upgrade journal settings preimage is invalid JSON; leave the adapter guarded') from error
                release_manifest_preimage = transition.get('release_manifest_preimage') or {}
                release_groups = release_manifest_preimage.get('groups', {})
                release_settings = release_manifest_preimage.get('settings') or {}
                if release_manifest_preimage.get('hooks_installed'):
                    previous_hooks = previous_settings.get('hooks', {}) if isinstance(previous_settings, dict) else {}
                    for event, group in release_groups.items():
                        groups = previous_hooks.get(event, []) if isinstance(previous_hooks, dict) else []
                        if not isinstance(groups, list) or groups.count(group) != 1:
                            raise ValueError('Upgrade journal does not bind the exact released-seat hook preimage')
                backup_info = transition.get('settings_backup')
                if backup_info:
                    backup_before = backup_info.get('preimage')
                    backup_postimage = backup_info.get('postimage')
                    backup_path = Path(backup_before.get('path', '')) if isinstance(backup_before, dict) else None
                    validate_journal_preimage(backup_before, backup_path)
                    current_backup = capture_text(backup_path, backup_before['mode'])
                    backup_is_preimage = (current_backup['exists'] == backup_before['exists'] and
                        current_backup['sha256'] == backup_before['sha256'] and
                        current_backup['mode'] == backup_before['mode'])
                    backup_is_postimage = (current_backup['exists'] and
                        current_backup['sha256'] == backup_info.get('postimage_sha256') and
                        current_backup['text'] == backup_postimage and current_backup['mode'] == 0o600)
                    if not backup_is_preimage and not backup_is_postimage:
                        raise ValueError('Target settings backup changed independently; journal retained')
                    if backup_is_postimage:
                        settings_backup_restore = backup_before
                if is_postimage:
                    settings_restore = {'exists': preimage['exists'], 'text': preimage['text'],
                                        'mode': preimage['mode']}
            else:
                try:
                    previous_settings = json.loads(settings_info['preimage']['text']) if settings_info['preimage']['exists'] else {}
                except ValueError as error:
                    raise ValueError('Upgrade journal settings preimage is invalid JSON; leave the adapter guarded') from error
                previous_hooks = previous_settings.get('hooks', {}) if isinstance(previous_settings, dict) else {}
                for event, group in settings_info.get('old_groups', {}).items():
                    groups = previous_hooks.get(event, []) if isinstance(previous_hooks, dict) else []
                    if not isinstance(groups, list) or groups.count(group) != 1:
                        raise ValueError('Upgrade journal does not bind the exact old ' + event + ' hook preimage')
                if set(settings_info.get('old_groups', {})) != set(settings_info.get('new_groups', {})):
                    raise ValueError('Upgrade journal hook replacement scope changed; leave the adapter guarded')
                settings_restore = recover_settings(settings_info)

        seat_manifest_info=journal.get('seat_manifest',{})
        seat_manifest_restore=None
        seat_manifest_file_restore=None
        if seat_manifest_info.get('managed'):
            expected_path=session_store.seat_dir(original_state['seat_id'])/'seat.json'
            manifest_path=Path(seat_manifest_info.get('path',''))
            refuse_symlinked_path(manifest_path)
            if manifest_path!=expected_path:
                raise ValueError('Seat manifest journal path differs from the owned registry')
            current_manifest_hash=delivery_runs.digest(manifest_path)
            if current_manifest_hash not in (seat_manifest_info.get('preimage_sha256'),
                                              seat_manifest_info.get('postimage_sha256')):
                raise ValueError('Permanent seat registry changed independently; preserve it and the upgrade journal')
            seat_manifest_restore=seat_manifest_info.get('preimage')
            if not isinstance(seat_manifest_restore,dict):
                raise ValueError('Seat manifest preimage is malformed; preserve it and the upgrade journal')
            if transition:
                transition_manifest = transition.get('target_manifest_file_preimage')
                validate_journal_preimage(transition_manifest, expected_path)
                expected_post = transition.get('target_manifest_file_postimage')
                if not isinstance(expected_post, str):
                    raise ValueError('Anchor rollback target manifest postimage is malformed')
                current_image = capture_text(expected_path, transition_manifest['mode'])
                if (current_image['sha256'] != transition_manifest['sha256'] and
                        current_image['sha256'] != text_digest(expected_post)):
                    raise ValueError('Target seat manifest changed independently; preserve it and the upgrade journal')
                seat_manifest_file_restore = transition_manifest

        release_manifest_file_restore = None
        if transition and transition.get('release_seat_id'):
            release_id = transition['release_seat_id']
            release_manifest_path = session_store.seat_dir(release_id) / 'seat.json'
            release_image = transition.get('release_manifest_file_preimage')
            validate_journal_preimage(release_image, release_manifest_path)
            release_postimage = transition.get('release_manifest_file_postimage')
            if not isinstance(release_postimage, str):
                raise ValueError('Anchor rollback released-seat manifest postimage is malformed')
            current_release_image = capture_text(release_manifest_path, release_image['mode'])
            if (current_release_image['sha256'] != release_image['sha256'] and
                    current_release_image['sha256'] != text_digest(release_postimage)):
                raise ValueError('Released seat manifest changed independently; preserve it and the upgrade journal')
            if transition.get('release_manifest_preimage_sha256') != text_digest(
                    json.dumps(transition.get('release_manifest_preimage'), indent=2, sort_keys=True) + '\n'):
                raise ValueError('Released seat manifest journal hash is inconsistent')
            release_manifest_file_restore = release_image

        # The journal and new adapter continue guarding work throughout rollback.
        if state_now['sha256'] != state_info['preimage']['sha256']:
            write(state_path, state_info['preimage']['text'], state_info['mode'])
        if settings_restore is not None:
            refuse_symlinked_path(settings_info['preimage']['path'])
            if settings_restore['exists']:
                write(settings_info['preimage']['path'], settings_restore['text'], settings_restore['mode'])
            else:
                path = Path(settings_info['preimage']['path'])
                if path.exists():
                    path.unlink()
                    sync_directory(path.parent)
        if adapter_now['sha256'] != adapter_info['preimage']['sha256']:
            write(adapter_path, adapter_info['preimage']['text'], adapter_info['preimage']['mode'])
        for path,image in secondary_state_paths:
            current=capture_text(path,image['mode'])
            if current['sha256']!=image['sha256']:
                write(path,image['text'],image['mode'])
        if seat_manifest_restore is not None and current_manifest_hash!=seat_manifest_info['preimage_sha256']:
            if seat_manifest_file_restore:
                write(seat_manifest_file_restore['path'], seat_manifest_file_restore['text'],
                      seat_manifest_file_restore['mode'])
            else:
                session_store.save_seat(seat_manifest_restore)
        if release_manifest_file_restore:
            release_current = capture_text(release_manifest_file_restore['path'],
                                           release_manifest_file_restore['mode'])
            post = transition['release_manifest_file_postimage']
            if release_current['sha256'] == text_digest(post):
                write(release_manifest_file_restore['path'], release_manifest_file_restore['text'],
                      release_manifest_file_restore['mode'])
        if settings_backup_restore:
            if settings_backup_restore['exists']:
                write(settings_backup_restore['path'], settings_backup_restore['text'],
                      settings_backup_restore['mode'])
            else:
                backup_path = Path(settings_backup_restore['path'])
                if backup_path.exists():
                    backup_path.unlink()
                    sync_directory(backup_path.parent)
        journal_path.unlink()
        sync_directory(journal_path.parent)
    return {'recovered': True, 'rolled_back': True, 'history_preserved': True,
            'adapter': str(adapter_path),
            'notice': 'The exact previous active source pin and installation state were restored; no report or verification ran.'}


def uninstall_seat(cwd, state):
    identifier = state['seat_id']
    with session_store.locked_seat(identifier):
        seat = session_store.load_seat(identifier)
        if not seat:
            raise ValueError('Permanent seat registry is missing; preserve its checkout history and use recover --inspect')
        lane_scope.check_owner(state)
        check_lane_scope(state, cwd)
        if state.get('phase') == 'removed':
            return {'removed': True, 'already_removed': True, 'history_preserved': True}
        adapter = Path(state['adapter'])
        expected_adapter = state['adapter_sha256']
        passthrough_sha = hashlib.sha256(passthrough(state['real_gh']).encode()).hexdigest()
        if adapter.exists() and delivery_runs.digest(adapter) not in (expected_adapter, passthrough_sha):
            raise ValueError('Seat GitHub entrypoint was independently modified; refusing destructive rollback')
        state['phase'] = 'removed'
        state['removed_at'] = time.time()
        state['updated_at'] = time.time()
        write(directory(cwd) / 'state.json', json.dumps(state, indent=2) + '\n')
        others = [item['state'] for item in session_store.all_checkouts(identifier)
                  if item.get('state') and item['state'].get('root') != state['root'] and
                  item['state'].get('phase') in ('active', 'installing')]
        if others:
            return {'removed': True, 'history_preserved': True, 'seat_still_active': True,
                    'remaining_checkouts': len(others), 'adapter': str(adapter)}
        if seat.get('hooks_installed'):
            settings_info = seat.get('settings') or {}
            path = Path(settings_info.get('path', ''))
            if not path.is_absolute():
                raise ValueError('Recorded seat settings path is missing; preserve the registry and use recover --inspect')
            current_raw = path.read_text() if path.exists() else ''
            current = json.loads(current_raw) if current_raw else {}
            updated = copy.deepcopy(current)
            hooks = updated.get('hooks', {})
            for event, owned in seat.get('groups', {}).items():
                groups = hooks.get(event, [])
                commands = hook_commands(owned)
                if any(commands.intersection(hook_commands(group)) and group != owned for group in groups):
                    raise ValueError('Managed seat hook was independently modified; preserve it and inspect recovery')
                if groups.count(owned) > 1:
                    raise ValueError('Managed seat hook was duplicated; inspect settings before rollback')
                if owned in groups:
                    groups.remove(owned)
                if not groups and event not in settings_info.get('original_hook_events', []):
                    hooks.pop(event, None)
            if not hooks and not settings_info.get('original_had_hooks'):
                updated.pop('hooks', None)
            backup = Path(settings_info['backup'])
            previous = backup.read_text() if backup.exists() else ''
            if (path.read_text() if path.exists() else '') != current_raw:
                raise ValueError('Seat settings changed during uninstall; coordinate the settings writer and retry')
            if updated == (json.loads(previous) if previous else {}):
                if settings_info.get('existed'):
                    write(path, previous, settings_info.get('mode', 0o600))
                elif path.exists():
                    path.unlink()
            else:
                write(path, json.dumps(updated, indent=2) + '\n', settings_info.get('mode', 0o600))
        if adapter.exists():
            write(adapter, passthrough(state['real_gh']), 0o700)
        seat['hooks_installed'] = False
        seat['updated_at'] = time.time()
        session_store.save_seat(seat)
    return {'removed': True, 'history_preserved': True, 'seat_still_active': False, 'adapter': state['adapter']}


def uninstall(cwd):
    ensure_no_upgrade_journal(cwd)
    current = read(cwd)
    if current and current.get('seat_id'):
        return uninstall_seat(cwd, current)
    with locked(cwd) as state:
        if state.get('lane_owner'): lane_scope.check_owner(state)
        if not state:
            raise ValueError('No delivery installation history')
        if state['root'] != str(root(cwd)):
            raise ValueError('Installation root changed; inspect instead of editing another worktree')
        adapter = Path(state['adapter'])
        if adapter.exists() and delivery_runs.digest(adapter) not in (
                state['adapter_sha256'], hashlib.sha256(passthrough(state.get('rollback_binary', state['real_gh'])).encode()).hexdigest()):
            raise ValueError('Managed GitHub adapter was independently modified; refusing destructive rollback')
        if state.get('phase') == 'removed':
            write(state['adapter'], passthrough(state.get('rollback_binary', state['real_gh'])), 0o700)
            return {'removed': True, 'already_removed': True}
        if state.get('manages_claude_hooks',True):
            path, current_raw, current = settings(state.get('session_root', cwd))
            mode = (path.stat().st_mode & 0o777) if path.exists() else state['settings_mode']
            updated = copy.deepcopy(current)
            hooks = updated.get('hooks', {})
            for event, owned in state['groups'].items():
                groups = hooks.get(event, [])
                if any(owned_hook['command'] == hook.get('command') and group != owned
                       for group in groups for hook in group.get('hooks', []) for owned_hook in owned['hooks']):
                    raise ValueError('Managed hook was independently modified; refusing destructive rollback')
                if groups.count(owned) > 1:
                    raise ValueError('Managed hook duplicated; inspect before rollback')
                if owned in groups:
                    groups.remove(owned)
                if not groups and event not in state['original_hook_events']:
                    hooks.pop(event, None)
            if not hooks and not state['original_had_hooks']:
                updated.pop('hooks', None)
            previous = Path(state['settings_backup']).read_text()
            if (path.read_text() if path.exists() else '') != current_raw:
                raise ValueError('Settings changed during rollback; retry after coordinating the writer')
            if updated == (json.loads(previous) if previous else {}):
                if state['settings_existed']:
                    write(path, previous, mode)
                elif path.exists():
                    path.unlink()
            else:
                write(path, json.dumps(updated, indent=2) + '\n', mode)
        state['phase'] = 'removed'
        owner = state.get('session_root', state['root'])
        alternatives = [other for other in registered_states(state) if other['root'] != state['root'] and
                        other.get('session_root', other['root']) == owner and active(other, other['root'])]
        state['rollback_binary'] = alternatives[0]['adapter'] if alternatives else state['real_gh']
        write(state['adapter'], passthrough(state['rollback_binary']), 0o700)
    # Keep the adapter as pass-through for older shells whose PATH still includes it.
    return {'removed': True, 'history_preserved': True, 'adapter': state['adapter']}


def record(args):
    ensure_no_upgrade_journal(args.cwd)
    discovered=None
    if (read(args.cwd) or {}).get('lane_owner'): discovered=discover(args.cwd)['state']
    with locked(args.cwd) as state:
        if state.get('lane_owner'):
            lane_scope.check_owner(state);check_lane_scope(state,args.cwd)
            if discovered is None or any(state.get(key)!=discovered.get(key) for key in ('lane_owner','branch','pr_url','base_ref')):
                raise ValueError('Lane owner or PR binding changed during report preparation; retry as the current owner')
        if not active(state, args.cwd):
            raise ValueError(enrollment_help(args.cwd))
        if state['source_versions'] != source_versions(state.get('lane_owner', {}).get('harness')):
            raise ValueError('Pinned source changed')
        receipt_context = None
        packet = None
        if state.get('lane_owner'):
            evidence_path = Path(args.evidence).expanduser().resolve()
            packet = json.loads(evidence_path.read_text())
            owner = state['lane_owner']
            receipt_report = delivery.required_reads_status(state['root'], owner['harness'],
                                                            owner['session_id'])
            if 'handoff_acceptance' in packet:
                packet = {**packet, 'guidance_attestation': {
                    'reread': delivery.required_read_packet_entries(receipt_report)}}
            receipt_context = delivery.require_required_read_receipts(
                receipt_report, packet, evidence_path.parent)
        receipt = delivery_runs.run(args.cwd, state['base_ref'], state['pr_url'], args.evidence, args.policy)
        if packet is not None and 'handoff_acceptance' in packet:
            packet_successor_acceptance(state, receipt, packet)
            receipt['handoff_acceptance'] = True
        if receipt_context is not None:
            attestation = packet['guidance_attestation']
            receipt['required_reads'] = {**receipt_context, 'stage': attestation.get('stage')}
        if state.get('receipt'):
            state.setdefault('history', []).append(state['receipt'])
        state['receipt'] = receipt
    return delivery.display(receipt['report'], args.format)


def enrollment_help(cwd):
    if (read(cwd) or {}).get('lane_owner'):
        return 'Lane is enrolled; after its real PR is published, run lane.py --cwd '+str(root(cwd))+' discover from its owning native session. Preserve old PR bindings and reports; do not reinstall per PR.'
    return ('No active delivery-only installation for this worktree and branch. Standalone reporting and proof-only work need no enrollment. '
            'For covered delivery, choose the permanent owner seat and actual native owner, then review the supported plan with the real PR and target inputs. '
            'Do not use guard.py enroll or install --fixture for delivery-only enrollment. Existing readiness holds still apply.')


def dirty_candidate_message(cwd):
    """Explain a refused starter without changing files or relaxing cleanliness."""
    commands = (
        ('Tracked changes', ('diff', '--name-only', '--no-ext-diff', '--no-textconv',
                             '--ignore-submodules=none', '-z', 'HEAD', '--')),
        ('Untracked paths', ('ls-files', '--others', '--exclude-standard', '-z')))
    details = []
    for label, command in commands:
        result = delivery.git(cwd, *command)
        if result.returncode:
            details.append(label + ': unavailable; inspect git status')
            continue
        paths = [path for path in result.stdout.split('\0') if path]
        names = [json.dumps(path) for path in paths[:5]]
        names = [name if len(name) <= 120 else name[:117] + '...' for name in names]
        suffix = ('; ' + str(len(paths) - 5) + ' more') if len(paths) > 5 else ''
        details.append(label + ' (' + str(len(paths)) + '): ' + (', '.join(names) or 'none') + suffix)
    return ('Evidence starter requires a clean committed candidate. ' + '. '.join(details) +
            '. Review these paths; keep private evidence outside the product worktree and commit intended source changes.')


def init_evidence(args):
    """Create a bound starter, never a verification result or an audit assertion."""
    state = read(args.cwd)
    if state and state.get('lane_owner'): state=discover(args.cwd)['state']
    if not active(state, args.cwd):
        raise ValueError(enrollment_help(args.cwd))
    current = delivery.snapshot(args.cwd, state['base_ref'])
    if not current['clean']:
        raise ValueError(dirty_candidate_message(args.cwd))
    if args.designs == 'no' and not (args.no_design_reason or '').strip():
        raise ValueError('--designs no requires --no-design-reason describing why designs were not involved')
    if args.designs == 'yes' and args.no_design_reason:
        raise ValueError('--no-design-reason is only valid with --designs no')
    packet = json.loads((APP / 'examples/delivery-evidence.json').read_text())
    packet.update(pr_url=state['pr_url'], base_ref=state['base_ref'])
    packet['main-awareness'].update(revision=current['head'], base_head=current['base_head'])
    verify = packet['local-verification']
    verify.update(command=['<program>', '<argument>'],
                  exit_code=None, outcome='<passed or nothing-affected after actual successful verification>')
    if args.designs == 'no':
        packet['design-fidelity'] = {'applicable': False, 'revision': current['head'], 'reason': args.no_design_reason}
    else:
        packet['design-fidelity'] = {
            'applicable': True, 'revision': '<actual audited full commit SHA>', 'skill': 'design-fidelity-audit',
            'design_reference': '<authoritative approved design version and location>',
            'design': {'path': '<actual design artifact>', 'sha256': '<actual hash>'},
            'audit': {'path': '<actual conducted audit report>', 'sha256': '<actual hash>'},
            'coverage': 'NONE', 'fidelity': 'NOT_ASSESSABLE',
            'assessed_scope': '<copy from the conducted audit>',
            'not_assessable': ['<replace with the actual audited scope gaps>'],
            'unresolved': ['<replace with the actual unresolved audit findings>'],
            'authority_map': [], 'findings': [], 'deviations': []}
    output = args.output.expanduser().resolve()
    ancestor = output.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if delivery.git(ancestor, 'rev-parse', '--show-toplevel').returncode == 0:
        raise ValueError('--output must be outside Git worktrees; evidence is private local working material')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves earlier packets and their receipts.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(packet, indent=2) + '\n')
    return {'created': str(output), 'candidate': current['head'], 'base_head': current['base_head'],
            'notice': 'Starter only. Replace placeholders with actual evidence. No audit, verification or report ran.',
            'next': shlex.join([sys.executable, str(SELF), '--cwd', str(root(args.cwd)), 'record',
                                '--evidence', str(output), '--format', 'json'])}


def native_identity_context():
    try:
        actual = lane_scope.native_identity()
        session_context = (lane_scope.native_session_context()
                           if actual['harness'] == 'opencode' else {})
        return {'actual': actual, 'unavailable_reason': None, 'session_context': session_context}
    except ValueError as error:
        return {'actual': None, 'unavailable_reason': str(error), 'session_context': {}}


def valid_recorded_owner(owner):
    if not isinstance(owner, dict) or not isinstance(owner.get('session_id'), str):
        return False
    harness = owner.get('harness')
    if harness in ('claude', 'codex'):
        return bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', owner['session_id']))
    if harness != 'opencode' or not re.fullmatch(r'ses_[A-Za-z0-9]+', owner['session_id']):
        return False
    parent_id = owner.get('session_parent_id')
    project_id = owner.get('session_project_id')
    directory = owner.get('session_directory')
    return bool((parent_id is None or
                 (isinstance(parent_id, str) and re.fullmatch(r'ses_[A-Za-z0-9]+', parent_id) and
                  parent_id != owner['session_id'])) and
                isinstance(project_id, str) and project_id and
                isinstance(directory, str) and Path(directory).is_absolute())


def rebind_route_ready(owner, actual, runtime, checkout_rows):
    try:
        outgoing_versions=runtime_source_versions(runtime, owner['harness'])
        runtime_source_versions(runtime, actual['harness'])
    except (KeyError, TypeError, ValueError):
        return False
    for item in checkout_rows:
        state=item.get('state')
        if state is None:
            continue
        if not isinstance(state,dict):
            return False
        if not state or state.get('phase') == 'removed':
            continue
        bindings=state.get('pr_bindings',{})
        if (not isinstance(bindings,dict) or
                any(not isinstance(url,str) or not isinstance(binding,dict)
                    for url,binding in bindings.items()) or
                (state.get('pr_url') is not None and not isinstance(state.get('pr_url'),str))):
            return False
        state_owner=state.get('lane_owner')
        if (not isinstance(state_owner, dict) or
                {key:state_owner.get(key) for key in ('harness','session_id')} !=
                {key:owner.get(key) for key in ('harness','session_id')} or
                state.get('source_versions') != outgoing_versions):
            return False
    return True


def managed_adapter_status(state):
    path_value = state.get('adapter') if isinstance(state, dict) else None
    expected = state.get('adapter_sha256') if isinstance(state, dict) else None
    if not isinstance(path_value, str) or not path_value or not isinstance(expected, str):
        return {'state': 'unknown', 'path': path_value}
    path = Path(path_value)
    try:
        refuse_symlinked_path(path)
        if not path.exists():
            return {'state': 'missing', 'path': str(path)}
        actual = delivery_runs.digest(path)
        mode = path.stat().st_mode & 0o777
    except (OSError, ValueError):
        return {'state': 'unavailable', 'path': str(path)}
    intact = actual == expected and bool(mode & 0o100)
    return {'state': 'intact' if intact else 'changed',
            'path': str(path), 'expected_sha256': expected, 'actual_sha256': actual, 'mode': mode}


def origin_host(cwd):
    try:
        remote = delivery.value(cwd, 'remote', 'get-url', 'origin')
        if remote.startswith('git@') and ':' in remote:
            host = remote.split('@', 1)[1].split(':', 1)[0]
        elif remote.startswith('https://'):
            parsed = urlsplit(remote)
            if parsed.username or parsed.password:
                return None
            host = parsed.hostname
        else:
            return None
        if not host:
            return None
        github.origin_repo(remote, host)
        return host
    except (OSError, ValueError, github.RecordError):
        return None


def recorded_seat_root_problem(state):
    """Read-only check that a durable lane still points at its recorded Git worktree."""
    if not state or not state.get('seat_id') or not isinstance(state.get('lane_owner'), dict):
        return None
    owner = state['lane_owner']
    try:
        seat = session_store.load_seat(state['seat_id'])
        anchor = session_store.operational_anchor(seat)
    except (OSError, TypeError, ValueError):
        return 'The durable seat registry or its operational Git anchor cannot be verified.'
    if not seat:
        return 'The durable seat registry is unavailable; preserve its history and inspect it.'
    if anchor['state'] != 'active':
        return 'This durable seat has released its operational anchor and is history-only.'
    expected_root = state.get('session_root')
    expected_owner_root = owner.get('seat_root', expected_root)
    expected_gitdir = owner.get('seat_gitdir')
    if not isinstance(expected_root, str) or not Path(expected_root).is_absolute():
        return 'The durable lane has no valid recorded permanent seat Git root.'
    if expected_root != anchor['root'] or expected_owner_root != anchor['root']:
        return ('The recorded permanent seat root differs from the durable seat: ' +
                f"checkout {expected_root}, owner binding {expected_owner_root}, seat {anchor['root']}.")
    if expected_gitdir != anchor['gitdir']:
        return ('The recorded permanent seat Gitdir changed: ' +
                f"owner binding {expected_gitdir}, durable seat {anchor['gitdir']}.")
    if not os.path.lexists(expected_root):
        return 'The recorded permanent seat Git root is missing at ' + expected_root + '.'
    root_path = Path(expected_root)
    if root_path.is_symlink() or not root_path.is_dir():
        return 'The recorded permanent seat Git root no longer identifies its original worktree path.'
    if not isinstance(expected_gitdir, str) or not Path(expected_gitdir).is_dir():
        return 'The recorded permanent seat Gitdir is unavailable at ' + str(expected_gitdir) + '.'
    try:
        observed_root = str(root(root_path))
        observed_gitdir = delivery.value(root_path, 'rev-parse', '--absolute-git-dir')
        observed_common = delivery.value(root_path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    except (OSError, ValueError):
        return 'The recorded permanent seat path no longer resolves as a Git worktree.'
    if observed_root != expected_root:
        return ('The recorded permanent seat root now resolves to a different worktree path: ' +
                observed_root + '.')
    if observed_gitdir != expected_gitdir:
        return ('The recorded permanent seat Gitdir changed: expected ' + expected_gitdir +
                ', observed ' + observed_gitdir + '.')
    if anchor.get('common_gitdir') is not None and observed_common != anchor['common_gitdir']:
        return ('The recorded permanent seat Git common directory changed: expected ' +
                anchor['common_gitdir'] + ', observed ' + observed_common + '.')
    return None


def setup_guidance(cwd, state, detail):
    """Choose one read-only setup recommendation from status observations."""
    identity = detail['current_native_identity']['actual']

    def guidance(action, reason, command=None):
        if command and identity and identity.get('harness') == 'opencode' and action != 'attach':
            reason += ' The actual OpenCode identity is available only through the native plugin context; status does not recommend a plain-shell command for this owner action.'
            command = None
        return {'action': action, 'reason': reason, 'command': command}

    current_cli = detail['executing_cli']
    source = detail['installed_source']
    if state is None:
        if (identity and identity['harness'] != 'opencode' and current_cli['source_revision'] and
                current_cli['working_tree_dirty'] is False):
            host = origin_host(cwd)
            if host:
                command = shlex.join([sys.executable, str(SELF), '--cwd', detail['product_root'],
                                      '--session-root', detail['product_root'], 'plan', '--lane', '--host', host,
                                      '--owner-harness', identity['harness'], '--owner-session', identity['session_id']])
                reason = ('No Policy Guard lane is attached here. Standalone reporting and proof-only work need no enrollment; '
                          'if covered delivery is intended and this checkout is your chosen permanent owner seat, review this optional plan.')
                return guidance('attach', reason, command)
        guide = shlex.join([sys.executable, str(SELF), 'guide', '--print'])
        return guidance('attach', 'No Policy Guard lane is attached here. Standalone reporting and proof-only work need no enrollment; use the guide only if covered delivery is intended.', guide)

    owner = state.get('lane_owner')
    if owner is not None and not valid_recorded_owner(owner):
        return guidance('inspect', 'Recorded native-owner metadata is unrecognized. Preserve the binding and history while the installation owner inspects it.')

    product_root = detail['product_root']
    branch_matches = state.get('root') == product_root and state.get('branch') == detail['branch']
    if detail['upgrade']['state'] == 'in-progress':
        owner_matches = (identity is not None and
                         (owner is None or detail['current_owner']['state'] == 'matches'))
        if branch_matches and owner_matches and source['integrity'] == 'intact':
            root_problem = recorded_seat_root_problem(state)
            if root_problem:
                command = shlex.join([sys.executable, str(SELF), '--seat-id', state['seat_id'],
                                      'recover', '--inspect'])
                return guidance('inspect', root_problem + ' Recover --inspect shows the preserved seat state. A separate reviewed upgrade can replace an absent birth root only when both its root and Gitdir are absent and a retained attachment proves the same Git common directory. Missing active attachments must be retired first, and a conflicting anchor seat requires its exact ID after every attachment is retired. The released seat remains history-only; source upgrade archives receipts and requires fresh reports.', command)
            return guidance('recover', 'An upgrade journal is pending for this checkout. The recorded owner can review the exact rollback command before continuing.',
                            detail['upgrade']['recovery_command'])
        return guidance('inspect', 'An upgrade journal is pending, but this native owner or checkout context is not confirmed. Keep the journal and adapter guard in place for the recorded owner to inspect.')

    if state.get('phase') != 'active' or not branch_matches:
        return guidance('inspect', 'The recorded installation is inactive or belongs to another product root or branch. Preserve its binding and ask its owner to inspect the intended checkout.')

    if owner:
        owner_state = detail['current_owner']['state']
        if owner_state == 'unavailable':
            return guidance('inspect', 'The actual native session is unavailable, so this owner binding cannot be confirmed. Inspect it from the owning native session.')
        if owner_state != 'matches':
            if source['integrity'] != 'intact':
                return guidance('inspect', 'The actual native session differs from the recorded owner and the installed source pin is ' + source['integrity'] + '. Status does not authorize a transfer; preserve the binding and ask the recorded owner to inspect it.')
            installed_cli = Path(source['path']) / 'lane.py' if source.get('path') else None
            command = None
            if installed_cli and installed_cli.is_file() and source['integrity'] == 'intact' and identity:
                command = shlex.join([sys.executable, str(installed_cli), '--cwd', product_root,
                                      'rebind-owner', '--from-harness', owner['harness'],
                                      '--from-session', owner['session_id'], '--confirm-handoff'])
            reason = ('This lane belongs to another native owner. A status mismatch does not authorize transfer; '
                      'only after the recorded owner explicitly authorizes a handoff may the actual incoming session run the supported command.')
            return guidance('handoff', reason, command)

    root_problem = recorded_seat_root_problem(state)
    if root_problem:
        command = shlex.join([sys.executable, str(SELF), '--seat-id', state['seat_id'],
                              'recover', '--inspect'])
        return guidance('inspect', root_problem + ' Recover --inspect shows the preserved seat state. A separate reviewed upgrade can replace an absent birth root only when both its root and Gitdir are absent and a retained attachment proves the same Git common directory. Missing active attachments must be retired first, and a conflicting anchor seat requires its exact ID after every attachment is retired. The released seat remains history-only; source upgrade archives receipts and requires fresh reports.', command)

    if source['integrity'] == 'missing' and not state.get('seat_id'):
        current_source_clean = current_cli['source_revision'] and current_cli['working_tree_dirty'] is False
        if not identity:
            return guidance('inspect', 'The legacy source files are missing, but no actual native identity is available to review a migration plan. Preserve the state and continue from its native owner.')
        if identity['harness'] == 'opencode':
            return guidance('inspect', 'The legacy source files are missing. OpenCode identity is scoped to its native plugin context, so status does not recommend a plain-shell migration command.')
        if not current_source_clean:
            return guidance('migrate', 'The legacy source files are missing. The supported migrate-plan can assess recovery without recreating them, but it needs a clean committed Starfleet source checkout.')
        host = None
        if owner and isinstance(owner.get('host'), str) and owner['host']:
            host = owner['host']
        elif state.get('pr_url'):
            try:
                host = github.reference(state['pr_url'], 'pull')['host']
            except (TypeError, ValueError, github.RecordError):
                host = None
        if not host:
            return guidance('inspect', 'The legacy source files are missing, but the recorded GitHub host is unknown. Preserve the state and inspect the original setup inputs before migration.')
        args = ['migrate-plan', '--host', host]
        if not owner:
            args.append('--confirm-owner')
        command = shlex.join([sys.executable, str(SELF), '--cwd', product_root, *args])
        return guidance('migrate', 'The active legacy installation has missing source files. Review the supported migration plan from this clean source; it preserves the existing state and can assess recovery without recreating them.', command)

    if source['integrity'] != 'intact':
        return guidance('inspect', 'The installed source pin is ' + source['integrity'] + '. Preserve settings and delivery history while the installation owner inspects the exact source files.')

    adapter = detail['managed_adapter']
    if adapter['state'] in ('missing', 'changed'):
        installed_cli = Path(source['path']) / 'lane.py' if source.get('path') else None
        command = None
        if installed_cli and installed_cli.is_file():
            if state.get('seat_id'):
                command = shlex.join([sys.executable, str(installed_cli), '--seat-id', state['seat_id'],
                                      'recover', '--inspect'])
            else:
                command = shlex.join([sys.executable, str(installed_cli), '--cwd', product_root,
                                      'recover', '--inspect'])
        return guidance('inspect', 'The recorded managed adapter is ' + adapter['state'] + '. Inspect its recovery state before planning a source upgrade.', command)
    if adapter['state'] != 'intact':
        return guidance('inspect', 'Managed adapter integrity is ' + adapter['state'] + '. Preserve the installation and inspect its recorded path before planning changes.')

    if not detail['installed_here']:
        return guidance('inspect', 'This root or branch is not the active attachment. Preserve existing bindings and ask the seat owner to review any new attachment.')

    if not current_cli['matches_installed_pin']:
        if current_cli['working_tree_dirty'] is False and current_cli['source_revision']:
            if identity and identity['harness'] == 'opencode':
                return guidance('upgrade', 'The installed source pin is intact and this CLI is a different clean committed source. OpenCode identity is scoped to its native plugin context, so status does not recommend a plain-shell upgrade command.')
            command = shlex.join([sys.executable, str(SELF), '--cwd', product_root, 'upgrade-plan'])
            return guidance('upgrade', 'The installed source pin is intact and this CLI is a different clean committed source. Review an upgrade plan; upgrade remains optional and report freshness is a separate delivery concern.', command)
        if current_cli['source_revision'] is None:
            return guidance('upgrade', upgrade_source_guidance_reason(source_is_immutable_runtime()))
        return guidance('upgrade', 'The installed source pin is intact, but this CLI is not a clean committed Git source that can create an upgrade plan. Use a clean Starfleet source checkout before planning.')

    if owner and not state.get('pr_url') and not state.get('pr_bindings'):
        return guidance('none', 'The owner lane is attached before a PR exists. No report or setup change is needed now; after a real PR is published, discover that binding from this owner session.')
    if state.get('pr_url') and detail['report_status']['state'] != 'valid-local-snapshot':
        record_help = shlex.join([sys.executable, str(Path(source['path']) / 'lane.py'), '--cwd',
                                  product_root, 'record', '--help'])
        return guidance('none', 'Setup is intact. A recorded report is separate from setup and is needed only before covered delivery; use existing evidence when that delivery is intended.', record_help)
    if state.get('seat_id') is None and not owner:
        return guidance('none', 'This active legacy per-PR binding remains usable. Migration is not required just because the installation is legacy; continue existing repository checks and human approval requirements before covered delivery.')
    return guidance('none', 'The active attachment, owner and installed source are confirmed. No setup change is recommended; existing report, repository checks and human approval requirements remain separate.')


def retired_attachments_for_seat(identifier, seat=None, checkout_rows=None):
    seat = seat or session_store.load_seat(identifier)
    checkout_rows = checkout_rows if checkout_rows is not None else session_store.all_checkouts(identifier)
    retired = []
    for item in checkout_rows:
        state = item.get('state') or {}
        receipt = state.get('retirement_receipt')
        if state.get('phase') != 'removed' or not isinstance(receipt, dict):
            continue
        pr_records = checkout_pr_records(seat, item['checkout_id'], state)
        retired.append({'checkout_id': item['checkout_id'], 'path': item.get('root'),
                        'repository': item.get('repository'), 'branch': state.get('branch'),
                        'attachment_state': 'retired', 'retired_at': receipt.get('retired_at'),
                        'pr_records': [dict(row, attachment_state='retired') for row in pr_records],
                        'retirement_receipt': copy.deepcopy(receipt)})
    return retired


def seat_retirement_status(seat, checkout_rows, anchor, actual, adapter_status,
                           pending_transition):
    identifier = seat['seat_id']
    journal_path = seat_retirement_journal_path(identifier)
    pending = pending_transition or journal_path.exists() or journal_path.is_symlink()
    receipt = seat.get('retirement_receipt')
    if isinstance(receipt, dict) and receipt.get('protocol') == SEAT_RETIREMENT_PROTOCOL:
        adapter_record = receipt.get('adapter')
        adapter_path_value = adapter_record.get('path') if isinstance(adapter_record, dict) else None
        try:
            if not isinstance(adapter_path_value, str) or not adapter_path_value:
                raise ValueError('retirement adapter path is malformed')
            adapter_path = Path(adapter_path_value)
            refuse_symlinked_path(adapter_path)
            adapter_removed = not adapter_path.exists() and not adapter_path.is_symlink()
        except (OSError, ValueError):
            adapter_removed = False
        settings_receipt = receipt.get('settings')
        settings_state = 'not-managed'
        if isinstance(settings_receipt, dict):
            settings_path = Path(settings_receipt.get('path', ''))
            try:
                current_settings = capture_text(settings_path, settings_receipt.get('mode', 0o600))
                settings_state = ('removed-on-disk' if current_settings['exists'] and
                                  current_settings['sha256'] == settings_receipt.get('postimage_sha256') and
                                  current_settings['mode'] == settings_receipt.get('mode') else
                                  'changed-after-retirement')
            except (OSError, TypeError, ValueError):
                settings_state = 'unavailable'
        state = ('pending-recovery' if pending else
                 'retired-on-disk' if anchor['state'] == 'released' and adapter_removed and
                 settings_state in ('not-managed', 'removed-on-disk') else
                 'retirement-effect-changed')
        next_action = ('Inspect the exact operator-owned retirement journal, then use recover --rollback if the operation was interrupted.'
                       if pending else
                       'Verify native hook refresh through real receiving evidence or a supported owner reload at a safe boundary. File removal does not prove native detachment; do not force a restart.'
                       if state == 'retired-on-disk' else
                       'Preserve the receipt and inspect the changed adapter or settings before any new operation.')
        return {'state': state, 'attachments': 'preserved-and-retired',
                'retained_hook_condition': 'removed-on-disk' if settings_state == 'removed-on-disk' else settings_state,
                'adapter_state': 'removed-on-disk' if adapter_removed else 'unexpected-or-unavailable',
                'settings_path': settings_receipt.get('path') if isinstance(settings_receipt, dict) else None,
                'settings_state': settings_state, 'receipt': copy.deepcopy(receipt),
                'native_refresh_observed': False, 'next_action': next_action,
                'command': None,
                'notice': 'The receipt records local file changes only; it does not establish native hook refresh or detachment.'}

    phases = [((item.get('state') or {}).get('phase')) for item in checkout_rows]
    recorded_all_removed = bool(phases) and all(phase == 'removed' for phase in phases)
    retirement_plan = None
    readiness_error = None
    owner = seat.get('owner')
    if (not pending and not pending_transition and anchor['state'] == 'active' and
            actual is not None and isinstance(owner, dict)):
        try:
            retirement_plan = build_retire_seat_plan(
                identifier, expected_owner_harness=owner.get('harness'),
                expected_owner_session=owner.get('session_id'),
                expected_operator_harness=actual.get('harness'),
                expected_operator_session=actual.get('session_id'),
                authorization_ref='EXISTING_AUTHORIZATION_REFERENCE', confirmed=True)
        except (OSError, TypeError, ValueError) as error:
            readiness_error = str(error)
    all_removed = retirement_plan is not None
    command = None
    if all_removed:
        command = shlex.join([sys.executable, str(SELF), '--seat-id', identifier, 'retire-seat',
                              '--expected-owner-harness', owner['harness'],
                              '--expected-owner-session', owner['session_id'],
                              '--expected-operator-harness', actual['harness'],
                              '--expected-operator-session', actual['session_id'],
                              '--authorization-ref', 'EXISTING_AUTHORIZATION_REFERENCE',
                              '--confirm-retirement'])
    if pending:
        state = 'pending-recovery'
        next_action = 'Inspect the exact operator-owned retirement journal, then use recover --rollback before another lifecycle action.'
    elif all_removed:
        state = 'drained-hook-retained' if seat.get('hooks_installed') else 'drained-seat-ready'
        next_action = ('After the existing authorization is explicit, review the exact retire-seat plan and apply only its digest.'
                       if command else
                       'Run status in the actual native operator session, resolve any pending transition or pin drift, and then review retire-seat after explicit authorization.')
    elif recorded_all_removed and actual is None:
        state = 'drained-operator-unavailable'
        next_action = 'Run status from the actual native operator session, then review retire-seat only after existing authorization.'
    elif recorded_all_removed:
        state = 'drained-retirement-blocked'
        next_action = ('Resolve this plan refusal before retrying: ' + readiness_error
                       if readiness_error else
                       'Resolve the anchor, runtime, adapter, settings, or lifecycle state before reviewing retire-seat.')
    else:
        state = 'attachments-active-or-uncertain'
        next_action = 'Remove live attachments through uninstall, and retire only missing recorded attachments through retire-attachment before reviewing seat retirement.'
    return {'state': state, 'active_or_uncertain_attachments': sum(phase != 'removed' for phase in phases),
            'retired_attachments': sum(phase == 'removed' for phase in phases),
            'retained_hook_condition': ('retained-active' if seat.get('hooks_installed') and
                                        anchor['state'] == 'active' and all_removed else
                                        'recorded-but-unverified' if seat.get('hooks_installed') and
                                        anchor['state'] == 'active' and recorded_all_removed else
                                        'configured' if seat.get('hooks_installed') else 'not-installed'),
            'adapter_state': adapter_status.get('state'), 'next_action': next_action,
            'command': command, 'authorization_required': True,
            'readiness_reason': readiness_error,
            'native_refresh_observed': False,
            'notice': 'A retirement plan records an existing authorization reference but does not authenticate it. Status does not prove native hook loading or delivery interception.'}


def status(cwd, session_id=None, state=None, native_context=None):
    state = read(cwd) if state is None else state
    native_context = native_identity_context() if native_context is None else native_context
    actual_identity = native_context['actual']
    configured = active(state, cwd)
    installed_here = attached_here(state, cwd)
    product_root = str(root(cwd))
    current_branch = branch(cwd)
    if installed_here:
        attachment_state = 'active-here'
    elif not state:
        attachment_state = 'not-installed'
    elif state.get('phase') != 'active':
        attachment_state = 'inactive'
    else:
        attachment_state = 'different-root-or-branch'
    owner = (state or {}).get('lane_owner')
    owner_valid = owner is None or valid_recorded_owner(owner)
    lane_enrolled=bool(state and owner and owner_valid and state.get('phase')=='active')
    installed_source = source_pin_status(state or {})
    adapter_integrity = managed_adapter_status(state)
    executing_cli = current_cli_status(state)
    versions_match = bool(executing_cli['matches_installed_pin'])
    journal_path = upgrade_journal_path(cwd)
    journal_pending = journal_path.exists() or journal_path.is_symlink()
    observations = []
    if state:
        try:
            with (directory(cwd) / 'native-events.jsonl').open() as stream:
                for line in deque(stream, maxlen=1000):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    generation = state.get('activation_generation')
                    if generation:
                        belongs_to_current_installation = row.get('activation_generation') == generation
                        belongs_to_current_source = row.get('source_path') == state.get('source')
                        belongs_to_current_pin = row.get('source_versions') == state.get('source_versions')
                        belongs_to_current_time = row.get('installation_at') == state.get('installed_at')
                        belongs_to_current_installation = (belongs_to_current_installation and
                                                           belongs_to_current_source and
                                                           belongs_to_current_pin and
                                                           belongs_to_current_time)
                    else:
                        belongs_to_current_installation = (
                            row.get('installation_at') == state.get('installed_at') and
                            row.get('source_versions') == state.get('source_versions'))
                    if belongs_to_current_installation:
                        observations.append(row)
        except OSError:
            pass
    matching = [row for row in observations if session_id and row.get('session_id') == session_id]
    native = [row for row in matching if row.get('event') == 'PreToolUse']
    adapter = [row for row in observations if row.get('event') == 'Adapter' and
               (not lane_enrolled or row.get('session_id')==state['lane_owner']['session_id'])]
    activation = ('native-hook-observed' if native else 'explicit-adapter-observed' if adapter else 'unobserved')
    receipt_status = 'absent'
    receipt_reason = 'No recorded delivery report run for this PR'
    if state and state.get('receipt'):
        if not versions_match:
            receipt_status = 'not-evaluated'
            receipt_reason = 'Executing CLI differs from the installed pin; report freshness was not evaluated.'
        elif journal_pending or state.get('phase') != 'active':
            receipt_status = 'not-evaluated'
            receipt_reason = 'An installation upgrade is incomplete; recover it before evaluating this report.'
        else:
            try:
                require(state, cwd, local_inspection=True)
                receipt_status, receipt_reason = 'valid-local-snapshot', 'Current local candidate, target and inputs match'
            except Exception as error:
                message = str(error)
                if 'belongs to another native session' in message or 'actual native' in message:
                    receipt_status = 'not-evaluated'
                else:
                    receipt_status = 'stale-or-unusable'
                receipt_reason = message
    if owner is None:
        current_owner = {'state': 'legacy-per-pr-no-native-owner', 'mismatch': None}
    elif not owner_valid:
        current_owner = {'state': 'unrecognized', 'mismatch': None,
                         'reason': 'Recorded native-owner metadata is unrecognized; preserve the binding and inspect it.'}
    else:
        if actual_identity is None:
            current_owner = {'state': 'unavailable', 'mismatch': None,
                             'reason': native_context['unavailable_reason'],
                             'expected': {key: owner.get(key) for key in ('harness', 'session_id')}}
        else:
            mismatch = actual_identity != {key: owner.get(key) for key in ('harness', 'session_id')}
            expected_context = {key: owner.get(key) for key in
                                ('session_parent_id', 'session_project_id', 'session_directory')}
            actual_context = native_context.get('session_context', {})
            if owner.get('harness') == 'opencode':
                mismatch = mismatch or actual_context != expected_context
            current_owner = {'state': 'mismatch' if mismatch else 'matches', 'mismatch': mismatch,
                             'expected': {key: owner.get(key) for key in ('harness', 'session_id')},
                             'actual': actual_identity,
                             'expected_context': expected_context if owner.get('harness') == 'opencode' else None,
                             'actual_context': actual_context if owner.get('harness') == 'opencode' else None}
    binding_type = ('native-owner-lane' if owner is not None else 'legacy-per-pr' if state else None)
    successor_guidance = None
    if state and owner and owner_valid:
        successor_guidance = ('Native-owner lane: preserve branch aliases. A new native owner needs an explicitly authorized '
                              'rebind-owner; bindings are not auto-discovered during source upgrade.')
    elif state and owner is not None:
        successor_guidance = 'Recorded native-owner metadata is unrecognized; preserve the binding and ask the installation owner to inspect it.'
    elif state:
        successor_guidance = ('Legacy per-PR binding: preserve its explicit local/published branch mapping. Do not use '
                              'rebind-owner; a separate PR or checkout requires its own authorized enrollment.')
    upgrade_state = {'state': 'in-progress' if journal_pending else 'none',
                     'journal_path': str(journal_path) if journal_pending else None,
                     'recovery_command': upgrade_recovery_command(cwd) if journal_pending else None}
    retired_attachments = (retired_attachments_for_seat(state['seat_id'])
                          if state and state.get('seat_id') else [])
    result = {'lane_enrolled':lane_enrolled,'lane_owner':(state or {}).get('lane_owner'),
            'owner_history':(state or {}).get('owner_history',[]),'installed_here': installed_here,
            'product_root': product_root, 'branch': current_branch,
            'attachment_state': attachment_state, 'phase': (state or {}).get('phase'),
            'binding_type': binding_type, 'current_owner': current_owner,
            'current_native_identity': native_context,
            'executing_cli': executing_cli, 'installed_source': installed_source,
            'managed_adapter': adapter_integrity,
            'upgrade': upgrade_state, 'successor_guidance': successor_guidance,
            'activation': {'state': activation, 'session_id': session_id,
                           'last_native_hook': native[-1] if native else None,
                           'last_adapter': adapter[-1] if adapter else None,
                           'last_observation': observations[-1] if observations else None,
                           'source_intact': installed_source['integrity'] == 'intact',
                           'executing_cli_matches_installed_pin': versions_match,
                           'observed_paths': {'native_hook': bool(native), 'explicit_adapter': bool(adapter)},
                           'notice': 'Local observations only; pair with actual owner tool output. '
                                     'Configuration presence does not prove native interception or hook loading. '
                                     'Pass --session-id to inspect the current session; no session is inferred.'},
            'pr_url': (state or {}).get('pr_url'), 'adapter': (state or {}).get('adapter'),
            'source_revision': (state or {}).get('source_revision'),
            'report_status': {'state': receipt_status, 'reason': receipt_reason,
                              'notice': 'No network freshness check, readiness approval or merge authority.'},
            'guidance_attestation_ref': (state or {}).get('guidance_attestation_ref'),
            'receipt': (state or {}).get('receipt'), 'history': (state or {}).get('history', []),
            'retired_attachments': retired_attachments,
            'enrollment_help': None if configured else enrollment_help(cwd)}
    publication = (state or {}).get('guidance_attestation_ref')
    published_context = publication.get('required_reads') if isinstance(publication, dict) else None
    report_receipt = (state or {}).get('receipt')
    recorded_context = report_receipt.get('required_reads') if isinstance(report_receipt, dict) else None
    receipt_context = (recorded_context if isinstance(report_receipt, dict) and report_receipt.get('handoff_acceptance')
                       else published_context if isinstance(published_context, dict) else recorded_context)
    if isinstance(receipt_context, dict):
        entries = receipt_context.get('entries')
        result['guidance_attestation_receipts'] = {
            'stage': ('handoff_acceptance' if isinstance(report_receipt, dict) and report_receipt.get('handoff_acceptance')
                      else publication.get('stage') if isinstance(publication, dict) else receipt_context.get('stage')),
            'list_sha256': receipt_context.get('list_sha256'),
            'generation': receipt_context.get('generation'),
            'observed_at': receipt_context.get('observed_at'),
            'entry_names': ([entry.get('name') for entry in entries if isinstance(entry, dict)]
                            if isinstance(entries, list) else []),
        }
    result['setup_guidance'] = setup_guidance(cwd, state, result)
    if state is None:
        permanent_seat = permanent_seat_status_for_anchor(cwd)
        if permanent_seat is not None:
            result['permanent_seat_hint'] = permanent_seat
            result['permanent_seat_hint_notice'] = (
                'This exact permanent seat is already registered. setup_guidance above describes only the current product checkout attachment.' )
    return result


def coverage(cwd, pr_url=None):
    """Describe local evidence without discovery, enrollment or synthetic probes."""
    state = read(cwd)
    identity_context = native_identity_context()
    identity = identity_context['actual']
    identity_error = identity_context['unavailable_reason']
    detail = status(cwd, identity['session_id'] if identity else None,
                    state=state, native_context=identity_context)
    expected_pr = github.reference(pr_url, 'pull')['url'] if pr_url else None
    bound_pr = (state or {}).get('pr_url')
    if not state:
        binding = 'not-installed'
    elif not detail['installed_here']:
        binding = 'inactive-local-binding'
    elif not bound_pr:
        binding = 'lane-enrolled-no-pr-binding'
    elif expected_pr and expected_pr.casefold() != bound_pr.casefold():
        binding = 'different-pr-bound'
    elif not expected_pr:
        binding = 'local-binding-current-pr-unconfirmed'
    else:
        binding = 'matches-declared-pr-and-local-branch'
    observations = {}
    for key, field in (('native_hook', 'last_native_hook'), ('explicit_adapter', 'last_adapter')):
        row = detail['activation'][field]
        # Legacy adapters carry no Codex identity. Do not credit them to this owner.
        owner_match = bool(identity and row and row.get('session_id') == identity['session_id'])
        observations[key] = {
            'state': 'observed-for-current-session' if owner_match else 'unverified-for-current-session',
            'last_observation': row,
            'notice': 'A recorded local event is not independent proof of interception or every action path.'}
    owner = (state or {}).get('lane_owner')
    owner_confirmed = (None if owner is None or not valid_recorded_owner(owner) else
                       detail['current_owner']['state'] == 'matches')
    return {
        'product_root': str(root(cwd)), 'branch': branch(cwd),
        'installed_here': detail['installed_here'],
        'attachment_state': detail['attachment_state'],
        'session_root': (state or {}).get('session_root'),
        'native_session': {'actual': identity, 'actual_context': identity_context['session_context'],
                           'unavailable_reason': identity_error,
                           'matches_installed_owner': owner_confirmed if owner else None},
        'binding': {'state': binding, 'declared_current_pr': expected_pr, 'installed_pr': bound_pr,
                    'type': detail['binding_type'], 'hosted_pr_checked': False},
        'installed_source': detail['installed_source'], 'executing_cli': detail['executing_cli'],
        'source_intact': (detail['activation']['source_intact'] if state else None),
        'entrypoints': {'private_gh_adapter': (state or {}).get('adapter'),
                        'adapter_integrity': detail['managed_adapter'],
                        'settings': (state or {}).get('settings') if (state or {}).get('manages_claude_hooks', bool(state)) else None,
                        'configured_hook_groups': (state or {}).get('groups', {})},
        'delivery_report': {'receipt': detail['report_status'], 'observations': observations},
        'retired_attachments': detail['retired_attachments'],
        'setup_guidance': detail['setup_guidance'],
        'fix_rounds': {'state': 'not-installed-by-delivery-lane',
                       'paths': {name: 'unverified' for name in (
                           'direct_edit', 'shell_write', 'scripted_codex_dispatch', 'native_agent_dispatch',
                           'push', 'restart', 'compaction', 'unavailable_state')},
                       'notice': 'This command does not inspect the separate fixture-only fix-round installation.'},
        'upgrade': detail['upgrade'],
        'notice': 'Read-only local inspection. No hosted discovery, tests, probes, installation or history changes. '
                  'The declared PR is caller input. Preserve manual zoom-out reminders and repository delivery rules.'}


def checkout_path_availability(item):
    state = item.get('state') or {}
    if state.get('phase') == 'removed':
        return 'retired' if isinstance(state.get('retirement_receipt'), dict) else 'inactive'
    if state.get('phase') != 'active':
        return 'unavailable' if not state else 'inactive'
    path = item.get('root')
    if not isinstance(path, str) or not Path(path).is_absolute():
        return 'unavailable'
    if not os.path.lexists(path):
        return 'missing'
    if Path(path).is_symlink() or not Path(path).is_dir():
        return 'changed'
    return 'present'


def checkout_worktree_availability(item):
    availability = checkout_path_availability(item)
    if availability != 'present':
        return availability
    state = item.get('state') or {}
    path = item.get('root')
    try:
        observed_root = str(root(path))
        observed_identity = git_path_identity(path)
        observed_branch = branch(path)
    except (OSError, ValueError):
        return 'changed'
    expected_common = item.get('common_gitdir')
    if (observed_root != path or observed_identity['gitdir'] != item.get('gitdir') or
            (expected_common is not None and observed_identity['common_gitdir'] != expected_common) or
            observed_branch != state.get('branch')):
        return 'changed'
    return 'present'


def permanent_seat_status_for_anchor(cwd):
    """Return one seat only when its active Git anchor exactly matches this unbound root."""
    try:
        checkout_root = str(root(cwd))
        checkout_identity = git_path_identity(cwd)
        seats = session_store.all_seats()
    except (OSError, TypeError, ValueError):
        return None
    matches = []
    for seat in seats:
        try:
            anchor = session_store.operational_anchor(seat)
        except (TypeError, ValueError):
            return None
        if (anchor['state'] == 'active' and anchor['root'] == checkout_root and
                anchor['gitdir'] == checkout_identity['gitdir'] and
                (anchor.get('common_gitdir') is None or
                 anchor['common_gitdir'] == checkout_identity['common_gitdir'])):
            matches.append(seat['seat_id'])
    if len(matches) != 1:
        return None
    return seat_status(matches[0])


def seat_status(identifier):
    seat=session_store.load_seat(identifier)
    if not seat:
        raise ValueError('Permanent Policy Guard seat registry is missing; preserve history and use recover --inspect')
    try:
        actual=lane_scope.native_identity()
        actual_context=lane_scope.native_session_context()
        owner_matches=(actual==seat.get('owner') and
                       (actual.get('harness')!='opencode' or actual_context==seat.get('owner_context',{})))
        identity_error=None
    except ValueError as error:
        actual=None
        actual_context={}
        owner_matches=False
        identity_error=str(error)
    runtime=seat.get('runtime',{})
    runtime_files={}
    runtime_path=Path(runtime.get('path',''))
    for relative,expected in runtime.get('files',{}).items():
        path=runtime_path/relative
        try:
            refuse_symlinked_path(path)
            digest=delivery_runs.digest(path)
        except (OSError,ValueError):
            digest=None
        runtime_files[relative]={'expected_sha256':expected,'actual_sha256':digest,
            'state':'intact' if digest==expected else 'missing-or-changed'}
    adapter_status = managed_adapter_status(seat)
    checkout_rows = session_store.all_checkouts(identifier)
    retired = retired_attachments_for_seat(identifier, seat, checkout_rows)
    retired_by_id = {row['checkout_id']: row for row in retired}
    checkouts=[]
    for item in checkout_rows:
        state=item.get('state') or {}
        retirement = retired_by_id.get(item['checkout_id'])
        bindings=state.get('pr_bindings')
        bindings_valid=(('pr_bindings' not in state or (isinstance(bindings,dict) and
            all(isinstance(url,str) and isinstance(binding,dict) for url,binding in bindings.items()))) and
            (state.get('pr_url') is None or isinstance(state.get('pr_url'),str)))
        known_prs=set(bindings) if isinstance(bindings,dict) else set()
        if isinstance(state.get('pr_url'),str):
            known_prs.add(state['pr_url'])
        checkouts.append({'checkout_id':item['checkout_id'],'root':item.get('root'),
            'repository':item.get('repository'),'host':item.get('host'),
            'phase':state.get('phase','missing-or-malformed-state'),'branch':state.get('branch'),
            'attachment_state': 'retired' if retirement else state.get('phase','missing-or-malformed-state'),
            'worktree_availability': checkout_worktree_availability(item),
            'current_pr':state.get('pr_url'),'known_prs':sorted(known_prs),
            'pr_history_state':'intact' if bindings_valid else 'malformed-preserve-and-inspect',
            'pr_records': retirement['pr_records'] if retirement else [],
            'retirement_receipt': copy.deepcopy(state.get('retirement_receipt')),
            'last_updated_at':state.get('updated_at')})
    runtime_cli = runtime_path / 'lane.py'
    recorded_owner = seat.get('owner')
    anchor = session_store.operational_anchor(seat)
    upgrade_journal = session_store.seat_dir(identifier)/UPGRADE_JOURNAL
    retirement_journal = seat_retirement_journal_path(identifier)
    pending_transition = bool(seat.get('install_journal') or seat.get('migration_journal') or
        seat.get('handoff_journal') or upgrade_journal.exists() or upgrade_journal.is_symlink() or
        retirement_journal.exists() or retirement_journal.is_symlink())
    complete_runtime_pin = bool(isinstance(runtime.get('version'), str) and
                                isinstance(runtime.get('path'), str) and Path(runtime['path']).is_absolute() and
                                isinstance(runtime.get('files'), dict) and runtime.get('files') and
                                'lane.py' in runtime.get('files', {}) and runtime_files and
                                all(row['state'] == 'intact' for row in runtime_files.values()) and
                                runtime_files.get('lane.py', {}).get('state') == 'intact')
    all_checkouts_removed = bool(checkout_rows) and all(
        isinstance(item.get('state'), dict) and item['state'].get('phase') == 'removed'
        for item in checkout_rows)
    can_offer_rebind = (actual is not None and not owner_matches and anchor['state'] == 'active' and
                        not pending_transition and valid_recorded_owner(recorded_owner) and
                        not all_checkouts_removed and
                        complete_runtime_pin and adapter_status.get('state') == 'intact' and
                        rebind_route_ready(recorded_owner, actual, runtime, checkout_rows))
    rebind_command = (shlex.join([sys.executable, str(runtime_cli), '--seat-id', identifier,
                                  'rebind-owner', '--from-harness', recorded_owner['harness'],
                                  '--from-session', recorded_owner['session_id'], '--confirm-handoff'])
                      if can_offer_rebind else None)
    settings_info = seat.get('settings') if isinstance(seat.get('settings'), dict) else {}
    prospective_settings_path = (str(Path(anchor['root'])/'.claude/settings.local.json')
                                 if anchor['state'] == 'active' else None)
    recorded_settings_path = settings_info.get('path')
    settings_path = recorded_settings_path or prospective_settings_path
    settings_path_source = 'recorded' if recorded_settings_path else (
        'prospective' if prospective_settings_path else 'unavailable')
    settings_state = ('installed' if recorded_settings_path and seat.get('hooks_installed') else
                      'recorded-inactive' if recorded_settings_path else
                      'prospective' if prospective_settings_path else 'unavailable')
    retirement_status = seat_retirement_status(seat, checkout_rows, anchor, actual,
                                                adapter_status,
                                                pending_transition)
    if retirement_status['state'] == 'retired-on-disk':
        adapter_status = dict(adapter_status, state='retired')
        settings_state = ('retired-on-disk' if retirement_status['settings_state'] == 'removed-on-disk'
                          else 'not-managed')
    elif retirement_status['state'] == 'pending-recovery':
        settings_state = 'retirement-pending-recovery'
    return {'seat_id':identifier,'seat_root':seat['seat_root'],'birth_root':seat['seat_root'],
        'operational_anchor':anchor,'owner':seat['owner'],
        'owner_context':seat.get('owner_context',{}),'actual_native_identity':actual,
        'actual_session_context':actual_context,'owner_matches':owner_matches,'identity_error':identity_error,
        'hooks_installed':bool(seat.get('hooks_installed')),'adapter':seat.get('adapter'),
        'adapter_state':adapter_status['state'],'adapter_integrity':adapter_status,
            'runtime_cli':str(runtime_cli),
            'settings':{'path':settings_path,'path_source':settings_path_source,'state':settings_state,
                'recorded_path':recorded_settings_path,'prospective_path':prospective_settings_path,
                'hooks_installed':bool(seat.get('hooks_installed')),
                'recorded_baseline_sha256':settings_info.get('sha256'),
                'baseline_hash_notice':'This is the install-time baseline, not a current settings-content observation.',
                'mode':settings_info.get('mode')},
            'handoff':{'rebind_owner_command':rebind_command,
                'command_condition':'Use only after the authorized scope transfer is recorded and the incoming owner has accepted it. Status does not establish transfer authority.',
                'retirement_notice':'A drained-seat retirement preserves recorded owner and checkout history. On-disk hook removal does not prove a running native host refreshed its configuration.'},
            'verification':{'configured':{'hooks_installed':bool(seat.get('hooks_installed')),
                    'settings_path':settings_path,'settings_path_state':settings_state,
                    'adapter_path':seat.get('adapter')},
                'observed':{'state':'not-observed-by-seat-status',
                    'notice':'Use coverage from each active checkout to inspect local events; configured hooks and adapter probes do not prove native interception.'}},
            'runtime':{'version':runtime.get('version'),
            'path':str(runtime_path),'files':runtime_files},'checkouts':checkouts,
        'retired_attachments': retired,
        'seat_retirement': retirement_status,
        'pr_records':len(seat.get('prs',{})),'pending_transition':pending_transition,
        'notice':'Read-only permanent-seat inspection. It remains available without product Git directories and does not prove native hook loading or delivery interception.'}


def coverage_summary(cwd, detail):
    """Render existing observations without adding checks or changing state."""
    binding = detail['binding']
    native = detail['native_session']
    source = detail['installed_source']
    report = detail['delivery_report']['receipt']
    installed_here = detail['installed_here']
    owner = ('unavailable' if native['matches_installed_owner'] is not None and native['actual'] is None else
             'matches' if native['matches_installed_owner'] is True else
             'mismatch' if native['matches_installed_owner'] is False else 'unconfirmed')
    setup = detail['setup_guidance']
    observations = detail['delivery_report']['observations']
    has_state = binding['state'] != 'not-installed'
    installation = ('delivery-only installation' if installed_here else
                    'inactive delivery installation' if has_state else 'not installed here')
    return '\n'.join([
        'Policy Guard: ' + installation,
        'Branch: ' + detail['branch'],
        'PR: declared=' + (binding['declared_current_pr'] or 'unknown') + '; bound=' +
        (binding['installed_pr'] or 'none') + ' (' + binding['state'] + '; hosted identity unchecked)',
        *(['PR note: The declared PR differs from the local binding. Confirm the product branch and PR before covered delivery.']
          if binding['state'] == 'different-pr-bound' else []),
        'Owner: ' + owner,
        'Installed source: ' + (str(source.get('revision') or 'unknown') + ' (' + source['integrity'] + ')'
                                if has_state else 'none'),
        'Observed paths for current session: native hook=' + observations['native_hook']['state'] +
        '; adapter=' + observations['explicit_adapter']['state'],
        'Report: ' + report['state'] + ' - ' + report['reason'],
        *(['Retired attachments: ' + '; '.join(
            row['path'] + ' (branch ' + str(row['branch']) + ', ' + str(len(row['pr_records'])) +
            ' PR records preserved)' for row in detail.get('retired_attachments', []))]
          if detail.get('retired_attachments') else []),
        'Fix-round enforcement: not installed by the delivery lane; other paths unverified.',
        'Next setup: ' + setup['action'] + ' - ' + setup['reason'],
        *(['Run: ' + setup['command']] if setup.get('command') else []),
        'Local observations only. No tests, audit, hosted check, installation or readiness approval performed.'])


def registrations(cwd, seat_identifier=None):
    """Inventory known PRs from a surviving seat, even if a product checkout is gone."""
    rows = []
    local = read(cwd) if not seat_identifier else None
    identifier = seat_identifier or (local or {}).get('seat_id')
    if identifier:
        states = [item['state'] for item in session_store.all_checkouts(identifier)
                  if item.get('state') and item['state'].get('phase') in ('active', 'installing')]
        states = [binding for state in states for binding in
                  (lane_scope.expand(state) if state.get('lane_owner') else [state])]
    else:
        states = registered_states({'root': str(root(cwd))})
    for state in states:
        available = active(state, state['root'])
        rows.append({'pr_url': state['pr_url'], 'root': state['root'], 'branch': state['branch'],
                     'head_branch': state.get('head_branch', state['branch']),
                     'session_root': state.get('session_root', state['root']),
                     'phase': state['phase'], 'binding_usable': available,
                     'source_revision': state.get('source_revision'),
                     'notice': None if available else 'Checkout unavailable or branch changed; coordinate a rebind and preserve history. Do not reset the PR count.'})
    return {'registrations': rows, 'notice': 'Known local bindings only, not discovery of all GitHub PRs or proof of session activation. '
            'Legacy PR installations remain explicit; lane enrollments discover bindings without reinstalling. Activation and readiness remain separate.'}


def parse_action(argv):
    if argv[:2] not in (['pr', 'ready'], ['pr', 'merge']):
        return argv, None, False
    cleaned, probe, index = argv[:2], False, 2
    values = {'--repo', '-R', '--author-email', '-A', '--body', '-b', '--body-file', '-F', '--match-head-commit', '--subject', '-t'}
    while index < len(argv):
        token = argv[index]
        if token == '--starfleet-probe':
            probe = True
        else:
            cleaned.append(token)
            if token in values and index + 1 < len(argv):
                index += 1
                cleaned.append(argv[index])
        index += 1
    return cleaned, delivery_runs.gh_action(cleaned), probe


def routed_state(initial, cwd, action, search):
    if not initial:
        return None
    owner = initial.get('session_root', initial['root'])
    states = [initial]
    if initial.get('lane_owner'):
        states=lane_scope.expand(initial)
    elif search:
        states = registered_states(initial, include_upgrade_preimages=True)
    try:
        actual = str(root(cwd))
    except ValueError:
        return None
    if search:
        if delivery.value(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir') != delivery.value(initial['root'], 'rev-parse', '--path-format=absolute', '--git-common-dir'):
            return None
    elif actual not in (owner, initial['root']):
        return None
    candidates = []
    for state in states:
        if state.get('phase') not in ('active', 'installing'):
            continue
        # An explicit PR may be managed from the permanent orchestrator seat.
        # With no explicit selector, preserve gh's actual current-branch behavior.
        if action['target'] is None and actual != state['root']:
            continue
        if action['target'] is None and not active(state, state['root']):
            continue
        try:
            delivery_runs.target_matches(action, state['pr_url'], state.get('head_branch', state['branch']))
        except ValueError:
            continue
        candidates.append(state)
    if len(candidates) > 1:
        raise ValueError('Ambiguous registered PR target; use the full PR URL from its product worktree')
    return candidates[0] if candidates else None


def same_repository_action(state, action):
    owner=state['lane_owner']
    repo=action.get('repo')
    if repo and repo.casefold() not in (owner['repository'],owner['host']+'/'+owner['repository']): return False
    target=action.get('target')
    if isinstance(target,str) and target.startswith('https://'):
        try:
            ref=github.reference(target,'pull')
            return ref['host']==owner['host'] and ref['repo']==owner['repository']
        except github.RecordError: return False
    return True


def seat_repository(seat, cwd, action):
    """Resolve an attached repository for a covered command without discovery or mutation."""
    attached = list(seat.get('checkouts', {}).values())
    current_root = None
    current_repo = None
    try:
        current_root = str(root(cwd))
        for record in attached:
            if record.get('root') == current_root:
                current_repo = (record.get('host'), record.get('repository', '').casefold())
                break
        if current_repo is None:
            for host in {item.get('host') for item in attached if item.get('host')}:
                try:
                    repo = github.origin_repo(delivery.value(cwd, 'remote', 'get-url', 'origin'), host).casefold()
                except Exception:
                    continue
                if any(row.get('host') == host and row.get('repository', '').casefold() == repo for row in attached):
                    current_repo = (host, repo)
                    break
    except Exception:
        pass
    target_repo = None
    target = action.get('target')
    if isinstance(target, str) and target.startswith('https://'):
        try:
            ref = github.reference(target, 'pull')
            target_repo = (ref['host'], ref['repo'].casefold())
        except github.RecordError:
            raise ValueError('Covered delivery target must be a canonical GitHub pull request URL or number')
    elif action.get('repo'):
        selected = action['repo'].removeprefix('https://').rstrip('/').removesuffix('.git')
        parts = selected.split('/')
        if len(parts) == 3:
            target_repo = (parts[0].casefold(), '/'.join(parts[1:]).casefold())
        elif len(parts) == 2:
            matches = [row for row in attached if row.get('repository', '').casefold() == selected.casefold()]
            identities = {(row['host'], row['repository'].casefold()) for row in matches}
            if len(identities) > 1:
                raise ValueError('Repository selector is attached on multiple hosts; use a full PR URL or HOST/OWNER/REPO')
            target_repo = next(iter(identities)) if identities else (None, selected.casefold())
        else:
            target_repo = (None, selected.casefold())
    repo = current_repo if target_repo is None else target_repo
    if not repo:
        return None, current_root, None, None
    matching = [row for row in attached if row.get('host') == repo[0] and
                row.get('repository', '').casefold() == repo[1]]
    if not matching:
        return None, current_root, None, None
    checkout = next((row for row in matching if row.get('root') == current_root), None)
    return matching, current_root, current_repo, checkout


def seat_action(seat_identifier, cwd, argv, *, event=None, published=False):
    """Evaluate one documented readiness/merge path for a durable native seat."""
    argv, action, probe = parse_action(argv)
    if not action or action['exempt']:
        return None, argv, False, probe
    seat = session_store.load_seat(seat_identifier)
    if not seat:
        raise ValueError('Permanent Policy Guard seat registry is missing; preserve history and use recover --inspect')
    actual = lane_scope.native_identity(event=event)
    if actual != seat.get('owner'):
        raise ValueError('This Policy Guard seat belongs to another native session. Use an explicitly authorized rebind-owner handoff.')
    if actual['harness']=='opencode' and lane_scope.native_session_context(event)!=seat.get('owner_context',{}):
        raise ValueError('This Policy Guard seat belongs to a different OpenCode session API project or location. Use an explicitly authorized rebind-owner handoff.')
    anchor = active_operational_anchor(seat)
    if seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal'):
        raise ValueError('Policy Guard seat installation or migration is incomplete; preserve the journal and use recover --inspect')
    attached, current_root, current_repo, checkout = seat_repository(seat, cwd, action)
    if attached is None:
        return None, argv, False, probe
    seat_journal = session_store.seat_dir(seat_identifier) / UPGRADE_JOURNAL
    if seat_journal.exists() or seat_journal.is_symlink():
        runtime_cli = Path(seat.get('runtime', {}).get('path', '')) / 'lane.py'
        command = shlex.join([sys.executable, str(runtime_cli), '--seat-id', seat_identifier,
                              'upgrade-recover', '--rollback'])
        raise ValueError('A seat source upgrade is incomplete. Run ' + command +
                         ' to restore its recorded preimages before continuing')
    if current_root:
        ensure_no_upgrade_journal(current_root)
    checkout_state = session_store.state_for_root(current_root) if checkout and current_root else None
    if published and checkout_state and checkout_state.get('lane_owner') and checkout_state.get('phase') == 'active':
        # The explicit seat gh entrypoint keeps published-branch discovery available.
        # Native hook evaluation remains local in check_action.
        discover(current_root)
    if current_repo and current_repo == (attached[0].get('host'), attached[0].get('repository', '').casefold()) and checkout is None:
        raise ValueError('This repository is attached to the seat, but this checkout is not. Attach it with plan/install --lane before covered delivery.')
    states = []
    for item in session_store.all_checkouts(seat_identifier):
        state = item.get('state')
        if not state or state.get('phase') not in ('active', 'installing'):
            continue
        if state.get('lane_owner', {}).get('host') != attached[0]['host'] or \
                state.get('lane_owner', {}).get('repository', '').casefold() != attached[0]['repository'].casefold():
            continue
        states.extend(lane_scope.expand(state))
    candidates = []
    inactive_branch_targets = []
    for state in states:
        if not state.get('pr_url'):
            continue
        if action.get('target') is None:
            if (checkout and state.get('root') == current_root and state.get('branch') == branch(cwd) and
                    active(state,state['root'])):
                candidates.append(state)
            continue
        try:
            delivery_runs.target_matches(action, state['pr_url'], state.get('head_branch', state.get('branch')))
        except ValueError:
            continue
        if not active(state,state['root']):
            if checkout and state.get('root') == checkout.get('root'):
                inactive_branch_targets.append(state)
            continue
        candidates.append(state)
    if checkout:
        local_candidates=[state for state in candidates if state.get('root')==checkout['root']]
        if local_candidates:
            candidates=local_candidates
    if len(candidates) > 1:
        raise ValueError('Ambiguous attached PR target; use the full PR URL from its product worktree')
    if not candidates:
        if checkout and inactive_branch_targets:
            if len(inactive_branch_targets) > 1:
                raise ValueError('Ambiguous registered branch for this PR; use the product worktree attached to its current PR branch')
            state = inactive_branch_targets[0]
            command = shlex.join([sys.executable, str(SELF), '--cwd', current_root, 'discover'])
            raise ValueError('Registered product branch is unavailable for ' + state['pr_url'] +
                             '. Switch the product checkout back to ' + state['branch'] + ' for this PR, or run ' +
                             command + ' after publishing the intended new branch. Keep the existing lane enrollment and report history.')
        if checkout:
            command = shlex.join([sys.executable, str(SELF), '--cwd', current_root, 'discover'])
            raise ValueError('This covered PR is not registered for the attached checkout. Run ' + command +
                             ' for its published branch, or attach it with plan/install --lane and preserve existing history.')
        raise ValueError('The repository is attached to this seat, but the current checkout and PR are unknown. '
                         'Use plan/install --lane from the actual owner session to attach the checkout, then discover its PR.')
    state = candidates[0]
    product = state['root']
    if state['phase'] == 'installing':
        raise ValueError('Delivery installation is incomplete; inspect status and finish recovery before PR actions')
    if not active(state, product):
        raise ValueError('Registered checkout or branch is unavailable for ' + state['pr_url'] +
                         '; preserve its history and attach the intended checkout explicitly')
    try:
        check_lane_scope(state, product)
        observation=None
        if published:
            observation=seat_cleanup.observe_pr(state['seat_id'],state['pr_url'])
            if observation['terminal']:
                raise ValueError('Published PR is closed or merged. Its terminal state was recorded locally; no readiness or merge action was forwarded.')
        report = require(state, product, event=event)
        if published:
            executable = captured_gh(state) if state.get('attestation_required') is True else None
            ref, remote = remote_pr(product, state['pr_url'], state['base_ref'], state.get('head_branch'),
                                    observed=observation.get('_remote'), executable=executable)
            github.check_published_snapshot(ref, remote, report['snapshot'], executable=executable)
    except Exception as error:
        error.delivery_state = state
        raise
    return state, argv, True, probe


def recheck_opencode_delivery(seat_identifier, selected_state, cwd, action):
    seat = session_store.load_seat(seat_identifier)
    if not seat:
        raise ValueError('Permanent Policy Guard seat registry is missing; preserve history and use recover --inspect')
    actual = lane_scope.native_identity()
    if actual != seat.get('owner'):
        raise ValueError('This Policy Guard seat belongs to another native session; no GitHub action was forwarded')
    if actual.get('harness') != 'opencode' or lane_scope.native_session_context() != seat.get('owner_context', {}):
        raise ValueError('This Policy Guard seat belongs to a different OpenCode session API project or location; no GitHub action was forwarded')
    if seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal'):
        raise ValueError('Policy Guard seat installation or migration is incomplete; preserve the journal and use recover --inspect')
    ensure_no_upgrade_journal(cwd)
    current = session_store.state_for_root(selected_state['root'])
    if (not current or current.get('seat_id') != seat_identifier or
            current.get('root') != selected_state.get('root') or
            current.get('pr_url') != selected_state.get('pr_url') or
            current.get('branch') != selected_state.get('branch')):
        raise ValueError('The attached checkout or PR binding changed after OpenCode permission approval; no GitHub action was forwarded')
    check_lane_scope(current, current['root'])
    if not active(current, current['root']):
        raise ValueError('The attached branch changed after OpenCode permission approval; no GitHub action was forwarded')
    delivery_runs.target_matches(action, current['pr_url'], current.get('head_branch', current['branch']))
    require(current, current['root'])
    return seat, current


def seat_gh(args, *, require_coverage=False):
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    _, action, probe = parse_action(argv)
    covered=bool(action and not action.get('exempt'))
    if covered:
        try:
            state, forwarded, checked, probe = seat_action(args.seat_id, args.cwd, argv, published=True)
        except Exception as error:
            failed = getattr(error, 'delivery_state', None)
            if failed and active(failed, failed['root']):
                trace(failed['root'], {'hook_event_name':'Adapter',
                      'session_id':lane_scope.native_identity()['session_id']}, action, probe, 'deny', str(error))
            raise
        if require_coverage and (not checked or not state):
            raise ValueError('The OpenCode GitHub tool requires an attached checkout and a fresh Policy Guard report before forwarding')
    else:
        state, forwarded, checked = None, argv, False
    if probe:
        if not checked:
            raise ValueError('Probe did not exercise an attached readiness/merge gate')
        trace(state['root'], {'hook_event_name': 'Adapter', 'session_id':
                              lane_scope.native_identity()['session_id']}, action, probe, 'pass', '')
        print(json.dumps({'probe': True, 'executed': False, 'published_pr_checked': True,
                          'would_forward': [state['real_gh'], *forwarded], 'pr_url': state['pr_url']}))
        return 0
    try:
        seat = session_store.load_seat(args.seat_id)
    except (OSError, TypeError, ValueError):
        if covered:
            raise
        seat = None
    if not state:
        captured = seat.get('real_gh') if seat else None
        fallback = os.environ.get('POLICY_GUARD_REAL_GH')
        real_gh = captured.get('path') if isinstance(captured, dict) else fallback
        if not real_gh:
            raise ValueError('No captured real GitHub CLI; use the normal GitHub CLI outside this seat entrypoint')
    else:
        captured = state.get('real_gh_identity')
        real_gh = state.get('real_gh')
    if require_coverage:
        try:
            seat, state = recheck_opencode_delivery(args.seat_id, state, args.cwd, action)
        except Exception as error:
            trace(args.cwd, {'hook_event_name': 'Adapter', 'session_id':
                             lane_scope.native_identity()['session_id']}, action, probe, 'deny', str(error))
            raise
        captured = state.get('real_gh_identity')
        real_gh = state.get('real_gh')
        if seat.get('real_gh') != captured:
            raise ValueError('Captured GitHub CLI identity changed after OpenCode permission approval; no GitHub action was forwarded')
    if captured and captured_binary_identity(real_gh) != captured:
        raise ValueError('Captured real GitHub CLI changed since seat enrollment; inspect status and recover the runtime')
    if checked:
        trace(state['root'], {'hook_event_name': 'Adapter', 'session_id':
                              lane_scope.native_identity()['session_id']}, action, probe, 'pass', '')
    result = subprocess.call([real_gh, *forwarded], cwd=args.cwd)
    if result == 0 and state and action and action.get('action') == 'merge':
        try:
            # A successful covered merge is a normal lifecycle boundary where
            # the host can confirm terminal status for local retention.
            seat_cleanup.observe_pr(state['seat_id'], state['pr_url'])
        except (OSError, TypeError, ValueError, subprocess.SubprocessError, github.RecordError):
            # The forwarded GitHub command already succeeded. A failed follow-up
            # observation must not change its result or imply that it was undone.
            pass
    return result


def check_action(cwd, argv, published=False, installation=None, search=True, event=None):
    initial = state_for_upgrade_guard(installation or cwd)
    argv, action, probe = parse_action(argv)
    if not action or action['exempt']:
        return initial, argv, False, probe
    guard_pending_upgrade(initial, cwd, action, search)
    if initial and initial.get('lane_owner') and not same_repository_action(initial,action):
        return initial,argv,False,probe
    state = routed_state(initial, cwd, action, search)
    if initial and initial.get('lane_owner'):
        if state is None and str(root(cwd))!=initial['root']:
            return initial,argv,False,probe
        lane_scope.check_owner(initial,event=event)
        check_lane_scope(initial,initial['root'])
        if state is None and not active(initial,initial['root']):
            raise ValueError(enrollment_help(initial['root']))
    if not state:
        return initial, argv, False, probe
    if state['phase'] == 'installing':
        raise ValueError('Delivery installation is incomplete; inspect status and finish rollback before PR actions')
    product = state['root']
    try:
        if not active(state, product):
            if state.get('lane_owner'):
                command=shlex.join([sys.executable,str(SELF),'--cwd',product,'discover'])
                raise ValueError('Registered product branch is unavailable for '+state['pr_url']+
                                 '. Switch the product checkout back to '+state['branch']+' for this PR, or run '+command+
                                 ' after publishing the intended new branch. Keep the existing lane enrollment and report history.')
            raise ValueError('Registered checkout or branch unavailable for ' + state['pr_url'] +
                             '; coordinate a reviewed rebind with the lane owner, retaining registration history')
        report = require(state, product,event=event)
        if published:
            executable = captured_gh(state) if state.get('attestation_required') is True else None
            ref, remote = remote_pr(product, state['pr_url'], state['base_ref'], state.get('head_branch'),
                                    executable=executable)
            github.check_published_snapshot(ref, remote, report['snapshot'], executable=executable)
            require(read(product), product)
    except Exception as error:
        error.delivery_state = state
        raise
    return state, argv, True, probe


def gh(args):
    if args.seat_id:
        return seat_gh(args)
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    initial = state_for_upgrade_guard(args.installation or args.cwd)
    _, action, probe = parse_action(argv)
    if action and not action['exempt']:
        guard_pending_upgrade(initial, args.cwd, action, search=True)
    scoped=bool(initial and initial.get('lane_owner') and action and not action['exempt'] and same_repository_action(initial,action))
    if scoped: discover(args.installation or args.cwd)
    identity=lane_scope.native_identity() if scoped else None
    event = {'hook_event_name': 'Adapter', 'session_id': identity['session_id'] if identity else os.environ.get('CLAUDE_SESSION_ID')}
    try:
        state, argv, checked, probe = check_action(args.cwd, argv, published=True, installation=args.installation)
    except Exception as error:
        target = getattr(error, 'delivery_state', initial)
        if target and action and active(target, target['root']):
            trace(target['root'], event, action, probe, 'deny', str(error))
        raise
    if checked:
        trace(state['root'], event, action, probe, 'pass', '')
    if probe:
        if not checked:
            raise ValueError('Probe did not exercise an active readiness/merge gate')
        print(json.dumps({'probe': True, 'executed': False, 'published_pr_checked': True,
                          'would_forward': [state['real_gh'], *argv], 'pr_url': state['pr_url']}))
        return 0
    if not state or not state.get('real_gh'):
        raise ValueError('No captured GitHub executable; use the normal GitHub CLI outside this adapter')
    return subprocess.call([state['real_gh'], *argv], cwd=args.cwd)


def hook(args):
    event = json.load(sys.stdin)
    if event.get('hook_event_name') != 'PreToolUse' or event.get('tool_name') not in ('Bash', 'exec_command'):
        return 0
    payload = event.get('tool_input', {})
    command = payload.get('command', payload.get('cmd', ''))
    if not isinstance(command, str):
        return 0
    # Unknown shell scripts are left to the local gh adapter; no edit/dispatch gating.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        argv = list(lexer)
    except ValueError:
        return 0
    if any(c in command for c in ('$', '`', '\n')) or any(token in (';', '&&', '||', '|', '&', '>', '>>', '<', '(', ')') for token in argv):
        return 0
    target = Path(payload.get('workdir', payload.get('cwd', event.get('cwd', str(args.cwd)))))
    if not target.is_absolute():
        target = Path(event.get('cwd', args.cwd)) / target
    if args.seat_id:
        if not argv or Path(argv[0]).name != 'gh':
            return 0
        try:
            state, _, checked, probe = seat_action(args.seat_id, target, argv[1:], event=event)
        except Exception as error:
            failed = getattr(error, 'delivery_state', None)
            if failed and active(failed, failed['root']):
                _, action, probe = parse_action(argv[1:])
                trace(failed['root'], event, action, probe, 'deny', str(error))
            raise
        if checked:
            _, action, _ = parse_action(argv[1:])
            trace(state['root'], event, action, probe, 'pass', '')
        return 0
    try:
        same_root = root(target) in (root(args.cwd), root(args.session_root or args.cwd))
    except ValueError:
        same_root = False
    if not same_root:
        return 0
    if argv and Path(argv[0]).name == 'gh':
        _, action, probe = parse_action(argv[1:])
        if not action:
            return 0
        try:
            _, _, checked, _ = check_action(target, argv[1:], installation=args.cwd, search=False,event=event)
        except Exception as error:
            trace(args.cwd, event, action, probe, 'deny', str(error))
            raise
        if checked:
            trace(args.cwd, event, action, probe, 'pass', '')
    return 0


def trace(cwd, event, action, probe, outcome, reason):
    path = directory(cwd) / 'native-events.jsonl'
    state = read(cwd) or {}
    row = {'at': delivery.dt.datetime.now(delivery.dt.timezone.utc).isoformat(),
           'event': event.get('hook_event_name'), 'session_id': event.get('session_id'),
           'tool_use_id': event.get('tool_use_id'), 'action': action['action'], 'probe': probe,
           'outcome': outcome, 'reason': reason[:1200],
           'installation_at': state.get('installed_at'),
           'activation_generation': state.get('activation_generation'),
           'source_path': str(APP),
           'source_versions': source_versions(state.get('lane_owner', {}).get('harness'))}
    # One append preserves concurrent event boundaries; never log full commands or approval content.
    fd = os.open(path, os.O_APPEND | os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(row) + '\n').encode())
    finally:
        os.close(fd)


def session(args):
    if args.seat_id:
        event = json.load(sys.stdin)
        seat = session_store.load_seat(args.seat_id)
        if not seat:
            raise ValueError('Permanent Policy Guard seat registry is missing; preserve history and use recover --inspect')
        if lane_scope.native_identity(event=event) != seat.get('owner'):
            raise ValueError('This Policy Guard seat belongs to another native session; use the explicit handoff command')
        if seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal') or \
                (session_store.seat_dir(args.seat_id) / UPGRADE_JOURNAL).exists():
            raise ValueError('This Policy Guard seat has an interrupted state transition; use recover --inspect before starting covered work')
        anchor = active_operational_anchor(seat)
        if (event.get('hook_event_name') != 'SessionStart' or
                root(event.get('cwd', args.cwd)) != root(anchor['root'])):
            return 0
        envfile = os.environ.get('CLAUDE_ENV_FILE')
        if envfile:
            with Path(envfile).open('a') as stream:
                stream.write('export PATH=' + shlex.quote(str(session_store.seat_dir(args.seat_id) / 'bin'))
                             + ':"$PATH"\n')
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext':
            'Delivery-only Policy Guard seat. Its stable gh entrypoint is ' +
            str(session_store.seat_dir(args.seat_id) / 'bin/gh') +
            '. Attach each product checkout once and discover its published PR. Report execution is required; checklist gaps may pass. '
            'Existing repository checks, human approvals and native trust remain required. No fix-round, edit or dispatch enforcement is installed.'}}))
        return 0
    state = read(args.cwd)
    if not state or (not state.get('lane_owner') and not active(state, args.cwd)):
        return 0
    event = json.load(sys.stdin)
    if state.get('lane_owner'):
        lane_scope.check_owner(state,event=event); check_lane_scope(state,args.cwd)
    if event.get('hook_event_name') != 'SessionStart' or root(event.get('cwd', args.cwd)) != root(state.get('session_root', args.cwd)):
        return 0
    envfile = os.environ.get('CLAUDE_ENV_FILE')
    if envfile:
        with Path(envfile).open('a') as stream:
            stream.write('export PATH=' + shlex.quote(str(Path(state['adapter']).parent)) + ':"$PATH"\n')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext':
        'Delivery-only Policy Guard for ' + (state.get('pr_url') or 'this enrolled lane; run discover after publishing a PR') + '. Run and record the report before PR readiness/merge; '
        'gaps need no separate acknowledgement. Existing repository checks and authority remain required. '
        'Confirm gh resolves to ' + state['adapter'] + '. No fix-round, edit or dispatch enforcement is installed.'}}))
    return 0


def migration_settings_change(path, remove_groups, add_groups):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Managed settings path must be absolute')
    refuse_symlinked_path(path)
    raw = path.read_text() if path.exists() else ''
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict) or not isinstance(value.get('hooks', {}), dict):
        raise ValueError('Unsupported existing Claude settings shape during migration')
    updated = copy.deepcopy(value)
    hooks = updated.setdefault('hooks', {})
    for event in sorted(set(remove_groups) | set(add_groups)):
        if not isinstance(hooks.get(event, []), list):
            raise ValueError('Existing hook groups must be lists')
        groups = hooks.setdefault(event, [])
        for owned in remove_groups.get(event, []):
            commands = hook_commands(owned)
            if groups.count(owned) > 1:
                raise ValueError('Old managed hook is duplicated; inspect settings before migration')
            if any(commands.intersection(hook_commands(group)) and group != owned for group in groups):
                raise ValueError('Old managed hook was changed independently; inspect settings before migration')
            if owned in groups:
                groups.remove(owned)
        for group in add_groups.get(event, []):
            commands = hook_commands(group)
            if groups.count(group) > 1:
                raise ValueError('New managed hook is duplicated; inspect settings before migration')
            if group in groups:
                continue
            if any(commands.intersection(hook_commands(existing)) for existing in groups):
                raise ValueError('New managed hook command conflicts with an existing hook')
            groups.append(group)
    if not any(hooks.values()) and not value.get('hooks'):
        updated.pop('hooks', None)
    updated_raw = json.dumps(updated, indent=2) + '\n'
    return {'path': str(path), 'preimage': raw, 'preimage_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'preimage_exists': path.exists(), 'mode': path.stat().st_mode & 0o777 if path.exists() else 0o600,
            'updated': updated_raw, 'updated_sha256': hashlib.sha256(updated_raw.encode()).hexdigest()}


def migration_plan(args):
    product = root(args.cwd)
    legacy_path = Path(delivery.value(product, 'rev-parse', '--absolute-git-dir')) / 'starfleet-delivery-lane/state.json'
    refuse_symlinked_path(legacy_path)
    if not legacy_path.exists():
        raise ValueError('No legacy per-worktree state exists to migrate; inspect the central seat with recover --inspect')
    legacy_raw = legacy_path.read_text()
    legacy = json.loads(legacy_raw)
    if legacy.get('seat_id'):
        raise ValueError('This checkout already uses the durable seat registry')
    if legacy.get('phase') != 'active' or legacy.get('root') != str(product):
        raise ValueError('Legacy state is not a recognized installation for the current checkout; preserve it and inspect manually')
    if not attached_here(legacy, product):
        raise ValueError('Switch to the recorded legacy product branch before migration; preserve its history')
    if not source_is_immutable_runtime() and delivery.value(APP, 'status', '--porcelain'):
        raise ValueError('Commit and verify the migration source, then use a clean pinned checkout')
    identity = lane_scope.native_identity()
    old_owner = legacy.get('lane_owner')
    handoff = False
    if old_owner:
        previous = {key: old_owner.get(key) for key in ('harness', 'session_id')}
        if previous != identity:
            if (not args.confirm_handoff or args.from_harness != previous['harness'] or
                    args.from_session != previous['session_id']):
                raise ValueError('Legacy native owner differs; state the exact outgoing owner and confirm the authorized handoff')
            handoff = True
    elif not args.confirm_owner:
        raise ValueError('Legacy per-PR state has no native owner; pass --confirm-owner to declare the current actual session as its owner')
    seat_root = root(args.session_root or legacy.get('session_root') or product)
    identifier = session_store.seat_id(seat_root)
    seat = session_store.seat_manifest(identifier)
    runtime = session_store.runtime_plan(APP)
    if seat:
        anchor = active_operational_anchor(seat)
        if anchor['root'] != str(seat_root) or anchor['gitdir'] != delivery.value(
                seat_root, 'rev-parse', '--absolute-git-dir'):
            raise ValueError('Target permanent seat no longer owns this exact active operational root; released seats are history-only')
        if seat.get('owner') != identity:
            raise ValueError('Target permanent seat belongs to another native session; use an authorized handoff first')
        if seat.get('runtime', {}).get('version') != runtime['version']:
            raise ValueError('Target seat uses another immutable runtime; use its reviewed source upgrade before migration')
    host = (old_owner or {}).get('host') or args.host
    repository = github.origin_repo(delivery.value(product, 'remote', 'get-url', 'origin'), host).casefold()
    real_gh_path = legacy.get('real_gh')
    if not real_gh_path:
        raise ValueError('Legacy state has no captured GitHub CLI path; preserve it and inspect the original setup')
    real_gh = captured_binary_identity(real_gh_path)
    if legacy.get('real_gh_identity') and legacy['real_gh_identity'] != real_gh:
        raise ValueError('Captured real GitHub CLI changed; preserve state and resolve the actual executable before migration')
    if seat and seat.get('real_gh', {}).get('resolved_path') != real_gh['resolved_path']:
        raise ValueError('Target seat uses a different captured GitHub CLI; preserve both registrations and inspect recovery')
    old_groups = legacy.get('groups', {}) if legacy.get('manages_claude_hooks', True) else {}
    old_adapter = Path(legacy.get('adapter', ''))
    if old_groups or old_adapter.exists():
        refuse_symlinked_path(old_adapter)
    old_adapter_text = old_adapter.read_text() if old_adapter.exists() else None
    if old_adapter_text is not None:
        digest = hashlib.sha256(old_adapter_text.encode()).hexdigest()
        passthrough_digest = hashlib.sha256(passthrough(real_gh_path).encode()).hexdigest()
        if digest not in (legacy.get('adapter_sha256'), passthrough_digest):
            raise ValueError('Legacy adapter was independently changed; preserve it and inspect manually')
    manages_hooks = identity['harness'] == 'claude'
    runtime_cli = Path(runtime['path']) / 'lane.py'
    new_groups = hook_groups(product, seat_root, seat_identifier=identifier,
                             runtime_cli=runtime_cli) if manages_hooks else {}
    target_settings_mode = None
    target_settings_raw = None
    if seat and seat.get('hooks_installed'):
        new_groups = copy.deepcopy(seat.get('groups', {}))
    old_settings = legacy.get('settings') if old_groups else None
    if old_settings and not Path(old_settings).is_absolute():
        raise ValueError('Legacy managed settings path is not absolute; preserve the installation and inspect manually')
    if seat and seat.get('hooks_installed'):
        stored_settings = seat.get('settings')
        if not isinstance(stored_settings, dict) or not stored_settings.get('path'):
            raise ValueError('Permanent seat settings metadata is malformed; preserve it and inspect recovery')
        target_settings = str(stored_settings['path'])
        if manages_hooks:
            settings_path, target_settings_raw, current_settings = settings(seat_root)
            stored_mode = stored_settings.get('mode')
            if (str(settings_path) != target_settings or isinstance(stored_mode, bool) or
                    not isinstance(stored_mode, int) or stored_mode < 0 or stored_mode > 0o777 or
                    not settings_path.exists() or (settings_path.stat().st_mode & 0o777) != stored_mode):
                raise ValueError('Permanent seat settings path or mode changed; preserve it and inspect recovery')
            for event, group in new_groups.items():
                if current_settings.get('hooks', {}).get(event, []).count(group) != 1:
                    raise ValueError('Installed permanent seat hook is missing or changed; use recover --inspect')
            target_settings_mode = stored_mode
    else:
        target_settings = str(seat_root / '.claude/settings.local.json')
    changes_by_path = {}
    if old_settings and Path(old_settings).is_absolute():
        changes_by_path[str(Path(old_settings))] = {
            'remove': {event: [group] for event, group in old_groups.items()}, 'add': {}}
    if manages_hooks and not (seat and seat.get('hooks_installed')):
        item = changes_by_path.setdefault(target_settings, {'remove': {}, 'add': {}})
        item['add'] = {event: [group] for event, group in new_groups.items()}
    changes = [migration_settings_change(path, action['remove'], action['add'])
               for path, action in sorted(changes_by_path.items())]
    target_settings_change = next((change for change in changes if change['path'] == target_settings), None)
    target_settings_postimage = (target_settings_change['updated'] if target_settings_change else
                                 target_settings_raw)
    binding_rows = lane_scope.expand(legacy) if old_owner else [copy.deepcopy(legacy)]
    migrated = copy.deepcopy(legacy)
    pr_bindings = {}
    for binding in binding_rows:
        if handoff and binding.get('receipt'):
            binding.setdefault('history', []).append(binding.pop('receipt'))
        if binding.get('pr_url'):
            pr_bindings[binding['pr_url']] = {key:copy.deepcopy(binding[key])
                                               for key in lane_scope.BINDING_FIELDS if key in binding}
    migrated['pr_bindings'] = pr_bindings
    if migrated.get('pr_url') and migrated['pr_url'] in pr_bindings:
        active_binding = pr_bindings[migrated['pr_url']]
        for key in lane_scope.BINDING_FIELDS:
            migrated.pop(key, None)
        migrated.update(copy.deepcopy(active_binding))
    lane_owner = dict(identity, host=host, repository=repository, seat_root=str(seat_root),
                      product_gitdir=delivery.value(product, 'rev-parse', '--absolute-git-dir'),
                      seat_gitdir=delivery.value(seat_root, 'rev-parse', '--absolute-git-dir'))
    if handoff:
        migrated.setdefault('owner_history', []).append({'from':previous,'to':identity,'at':'authorized migration',
            'authority':'caller explicitly confirms an authorized handoff; not independently authenticated'})
    migrated.update(seat_id=identifier, checkout_id=session_store.checkout_id(product),
                    seat_dir=str(session_store.seat_dir(identifier)), session_root=str(seat_root),
                    lane_owner=lane_owner, source=str(Path(runtime['path'])),
                    source_revision=source_revision(runtime['version']),
                    source_versions=source_versions(identity['harness']),
                    runtime={'version':runtime['version'],'path':str(runtime['path']),'files':runtime['files']},
                    adapter=str(session_store.seat_dir(identifier) / 'bin/gh'),
                    adapter_sha256=hashlib.sha256(adapter_script(product, seat_identifier=identifier,
                        runtime_cli=runtime_cli,real_gh=real_gh['path']).encode()).hexdigest(), real_gh=real_gh['path'],
                    real_gh_identity=real_gh, manages_claude_hooks=manages_hooks,
                    groups=new_groups, settings=target_settings if manages_hooks else None,
                    settings_sha256=hashlib.sha256((target_settings_postimage or '').encode()).hexdigest(),
                    phase='installing')
    if manages_hooks:
        if target_settings_change:
            migrated['settings_mode'] = target_settings_change['mode']
        elif target_settings_mode is not None:
            migrated['settings_mode'] = target_settings_mode
    plan = {'legacy_state_path':str(legacy_path), 'legacy_state_sha256':hashlib.sha256(legacy_raw.encode()).hexdigest(),
            'legacy_state':legacy, 'legacy_state_text':legacy_raw, 'legacy_adapter':str(old_adapter),
            'legacy_adapter_sha256':hashlib.sha256(old_adapter_text.encode()).hexdigest() if old_adapter_text is not None else None,
            'legacy_adapter_text':old_adapter_text, 'seat_id':identifier, 'seat_root':str(seat_root),
            'owner':identity, 'handoff':handoff, 'seat_preimage':seat, 'runtime':runtime,
            'real_gh':real_gh, 'repository':repository, 'host':host, 'groups':new_groups,
            'settings_changes':changes, 'migrated_state':migrated,
            'product_gitdir':delivery.value(product, 'rev-parse', '--absolute-git-dir'),
            'common_gitdir':delivery.value(product, 'rev-parse', '--path-format=absolute', '--git-common-dir')}
    plan['plan_sha256']=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    return plan


def migrate_legacy(args):
    proposed=migration_plan(args)
    if args.plan_sha256!=proposed['plan_sha256']:
        raise ValueError('Legacy migration inputs changed; review a fresh migrate-plan before applying')
    if hashlib.sha256(Path(proposed['legacy_state_path']).read_bytes()).hexdigest()!=proposed['legacy_state_sha256']:
        raise ValueError('Legacy state changed since planning; inspect it and review a fresh plan')
    identifier=proposed['seat_id']
    runtime=session_store.install_runtime(APP,proposed['runtime'])
    with session_store.locked_seat(identifier):
        seat=session_store.load_seat(identifier)
        if seat and (seat.get('owner')!=proposed['owner'] or seat.get('runtime',{}).get('version')!=proposed['runtime']['version']):
            raise ValueError('Target seat changed since migration planning')
        if seat:
            anchor = active_operational_anchor(seat)
            expected_root = proposed['seat_root']
            expected_gitdir = proposed['migrated_state']['lane_owner']['seat_gitdir']
            if anchor['root'] != expected_root or anchor['gitdir'] != expected_gitdir:
                raise ValueError('Target seat anchor changed since migration planning; released seats remain history-only')
        journal={'kind':'migration','legacy_state_path':proposed['legacy_state_path'],
                 'legacy_state':proposed['legacy_state_text'],
                 'legacy_state_sha256':proposed['legacy_state_sha256'],
                 'legacy_adapter':proposed['legacy_adapter'],
                 'legacy_adapter_text':proposed['legacy_adapter_text'],
                 'legacy_adapter_sha256':proposed['legacy_adapter_sha256'],
                 'legacy_adapter_mode':Path(proposed['legacy_adapter']).stat().st_mode & 0o777
                    if proposed['legacy_adapter_text'] is not None else None,
                 'settings_changes':proposed['settings_changes'],
                 'seat_preimage':copy.deepcopy(seat),'new_state_path':str(session_store.seat_dir(identifier)/'checkouts'/
                    session_store.checkout_id(proposed['migrated_state']['root'])/'state.json'),
                 'new_adapter_path':proposed['migrated_state']['adapter'],
                 'new_adapter_sha256':proposed['migrated_state']['adapter_sha256'],
                 'created_at':time.time()}
        if seat is None:
            seat=session_store.make_manifest(proposed['seat_root'],proposed['owner'],proposed['runtime'])
            seat['seat_gitdir']=delivery.value(proposed['seat_root'],'rev-parse','--absolute-git-dir')
            seat['seat_common_gitdir']=delivery.value(
                proposed['seat_root'],'rev-parse','--path-format=absolute','--git-common-dir')
        seat['migration_journal']=journal
        seat['real_gh']=proposed['real_gh']
        seat['adapter']=proposed['migrated_state']['adapter']
        seat['adapter_sha256']=proposed['migrated_state']['adapter_sha256']
        seat['runtime']={'version':proposed['runtime']['version'],'path':str(runtime),
                         'files':proposed['runtime']['files'],
                         'source_revision':proposed['migrated_state']['source_revision']}
        seat['groups']=proposed['groups']
        seat['settings']=seat.get('settings') or None
        session_store.save_seat(seat)
        state=copy.deepcopy(proposed['migrated_state'])
        installed_at=delivery.dt.datetime.now(delivery.dt.timezone.utc).isoformat()
        state.update(installed_at=installed_at,
                     activation_generation=next_activation_generation(installed_at,state,state['adapter_sha256']))
        location=session_store.write_checkout(identifier,state['root'],state,
            repository=proposed['repository'],host=proposed['host'],gitdir=proposed['product_gitdir'],
            common_gitdir=proposed['common_gitdir'])
        adapter=Path(state['adapter'])
        body=adapter_script(state['root'],seat_identifier=identifier,runtime_cli=runtime/'lane.py',
                            real_gh=state['real_gh'])
        if adapter.exists() and delivery_runs.digest(adapter)!=state['adapter_sha256']:
            raise ValueError('Target seat entrypoint is independently modified; preserve it and inspect recovery')
        if not adapter.exists(): write(adapter,body,0o700)
        for change in proposed['settings_changes']:
            path=Path(change['path'])
            refuse_symlinked_path(path)
            raw=path.read_text() if path.exists() else ''
            if hashlib.sha256(raw.encode()).hexdigest()!=change['preimage_sha256']:
                raise ValueError('Managed settings changed since migration planning; preserve the journal and inspect recovery')
            if raw!=change['updated']:
                write(path,change['updated'],change['mode'])
        old_adapter=Path(proposed['legacy_adapter'])
        if proposed['legacy_adapter_text'] is not None:
            if delivery_runs.digest(old_adapter)!=proposed['legacy_adapter_sha256']:
                raise ValueError('Legacy adapter changed during migration; preserve the journal and inspect recovery')
            write(old_adapter,passthrough(state['real_gh']),0o700)
        legacy_path=Path(proposed['legacy_state_path'])
        tombstone={'phase':'migrated','seat_id':identifier,'migrated_to':str(location/'state.json'),
                   'preserved_state_sha256':proposed['legacy_state_sha256']}
        write(legacy_path,json.dumps(tombstone,indent=2)+'\n')
        state['phase']='active'
        write(location/'state.json',json.dumps(state,indent=2)+'\n')
        seat=session_store.load_seat(identifier)
        seat['hooks_installed']=bool(proposed['groups'])
        seat['groups']=proposed['groups']
        if proposed['groups']:
            target=Path(proposed['migrated_state']['settings'])
            change=next((row for row in proposed['settings_changes'] if row['path']==str(target)),None)
            prior=(proposed['seat_preimage'] or {}).get('settings',{})
            seat['settings']=prior or {'path':str(target),'backup':None,'sha256':None,
                'existed':change['preimage_exists'] if change else target.exists(),
                'mode':change['mode'] if change else 0o600,
                'original_hook_events':list((json.loads(change['preimage']) if change and change['preimage'] else {}).get('hooks',{})),
                'original_had_hooks':'hooks' in (json.loads(change['preimage']) if change and change['preimage'] else {})}
        seat.pop('migration_journal',None)
        seat['updated_at']=time.time()
        session_store.save_seat(seat)
    return {'migrated':True,'seat_id':identifier,'checkout':state['root'],'adapter':state['adapter'],
            'history_preserved':True,'handoff':proposed['handoff'],
            'notice':'Legacy state was copied to the user state directory and the old adapter was made pass-through. Roll back with recover --rollback while the migration journal remains.'}


def recover_inspect(args):
    identifier = args.seat_id
    if identifier:
        seat = session_store.load_seat(identifier)
        if not seat:
            raise ValueError('Permanent seat registry is missing; no state was changed')
        try:
            actual = lane_scope.native_identity()
            owner_matches = actual == seat.get('owner')
        except ValueError as error:
            actual, owner_matches = None, False
            identity_error = str(error)
        else:
            identity_error = None
        runtime = seat.get('runtime', {})
        runtime_path = Path(runtime.get('path', ''))
        files = {}
        for relative, expected in runtime.get('files', {}).items():
            path = runtime_path / relative
            try:
                actual_hash = delivery_runs.digest(path)
            except OSError:
                actual_hash = None
            files[relative] = {'expected_sha256': expected, 'actual_sha256': actual_hash,
                               'state': 'intact' if actual_hash == expected else 'missing-or-changed'}
        adapter_status = managed_adapter_status(seat)
        retirement_journal = seat_retirement_journal_path(identifier)
        retirement_pending = retirement_journal.exists() or retirement_journal.is_symlink()
        settings_state = 'not-managed'
        settings_path = (seat.get('settings') or {}).get('path')
        if seat.get('hooks_installed') and settings_path:
            try:
                raw = Path(settings_path).read_text()
                current = json.loads(raw) if raw else {}
                settings_state = 'intact' if all(
                    current.get('hooks', {}).get(event, []).count(group) == 1
                    for event, group in seat.get('groups', {}).items()) else 'missing-or-changed'
            except (OSError, ValueError):
                settings_state = 'missing-or-changed'
        return {'seat_id': identifier, 'seat_root': seat['seat_root'],
                'operational_anchor': session_store.operational_anchor(seat), 'owner': seat['owner'],
                'actual_native_identity': actual, 'owner_matches': owner_matches,
                'identity_error': identity_error, 'runtime': {'path': str(runtime_path),
                'version': runtime.get('version'), 'files': files}, 'adapter': adapter_status,
                'settings': {'path': settings_path, 'state': settings_state},
                'install_journal': seat.get('install_journal'), 'migration_journal':seat.get('migration_journal'),
                'handoff_journal':seat.get('handoff_journal'),
                'seat_retirement_journal': {'pending': retirement_pending,
                    'path': str(retirement_journal) if retirement_pending else None,
                    'recovery': 'Use recover --rollback from the exact native operator recorded by the journal.'
                    if retirement_pending else None},
                'checkouts': session_store.all_checkouts(identifier),
                'notice': 'Read-only local recovery inspection. It does not recreate receipts, checklists, manual counts or activation claims.'}
    state = read(args.cwd)
    if not state:
        raise ValueError('No local or seat registration exists for this checkout')
    return {'legacy_state': state, 'source_integrity': source_pin_status(state),
            'adapter': managed_adapter_status(state),
            'notice': 'Read-only legacy inspection. Migration is a separate reviewed operation; no files or history changed.'}


def recover_plan(args):
    identifier = args.seat_id
    if not identifier:
        state = read(args.cwd)
        identifier = (state or {}).get('seat_id')
    if not identifier:
        raise ValueError('Use recover --migrate-plan for a legacy per-worktree installation')
    seat = session_store.load_seat(identifier)
    if not seat:
        raise ValueError('Permanent seat registry is missing; preserve its checkout history and inspect the private state directory')
    ensure_no_seat_retirement_journal(identifier)
    if lane_scope.native_identity() != seat.get('owner'):
        raise ValueError('Only the exact current native seat owner may plan recovery')
    anchor = active_operational_anchor(seat)
    if seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal'):
        raise ValueError('An interrupted seat state transition needs recover --rollback before repair planning')
    if (session_store.seat_dir(identifier) / UPGRADE_JOURNAL).exists() or \
            (session_store.seat_dir(identifier) / UPGRADE_JOURNAL).is_symlink():
        raise ValueError('A source upgrade is incomplete; use upgrade-recover --rollback from its recorded checkout')
    actions = []
    runtime = seat.get('runtime', {})
    runtime_path = Path(runtime.get('path', ''))
    missing=[];changed=[]
    for relative,expected in runtime.get('files',{}).items():
        path=runtime_path/relative
        refuse_symlinked_path(path)
        if not path.exists():
            missing.append(relative)
        elif not path.is_file() or delivery_runs.digest(path)!=expected:
            changed.append(relative)
    if changed:
        raise ValueError('Installed immutable runtime contains changed files; preserve them and inspect the recorded fingerprints')
    if missing:
        candidate = session_store.runtime_plan(APP)
        if candidate['version'] != runtime.get('version'):
            raise ValueError('The immutable installed runtime is missing or changed, and this CLI source is not the same pinned version. Use the matching pinned source to recover it.')
        actions.append({'kind': 'restore-runtime', 'path': str(runtime_path), 'version': runtime['version'],
                        'missing_files':missing})
    adapter = Path(seat.get('adapter', ''))
    if not adapter.exists():
        actions.append({'kind': 'restore-adapter', 'path': str(adapter)})
    elif delivery_runs.digest(adapter) != seat.get('adapter_sha256'):
        raise ValueError('Seat GitHub entrypoint was independently changed; preserve it and inspect manually')
    settings_action = None
    if seat.get('hooks_installed'):
        info = seat.get('settings') or {}
        path = Path(info.get('path', ''))
        refuse_symlinked_path(path)
        raw = path.read_text() if path.exists() else ''
        current = json.loads(raw) if raw else {}
        updated = copy.deepcopy(current)
        hooks = updated.setdefault('hooks', {})
        missing = False
        for event, group in seat.get('groups', {}).items():
            groups = hooks.setdefault(event, [])
            if group in groups:
                if groups.count(group) != 1:
                    raise ValueError('Seat hook is duplicated; preserve settings and inspect manually')
                continue
            commands = hook_commands(group)
            if any(commands.intersection(hook_commands(existing)) for existing in groups):
                raise ValueError('Seat hook was changed independently; preserve settings and inspect manually')
            groups.append(group)
            missing = True
        if missing:
            settings_action = {'kind': 'restore-hooks', 'path': str(path),
                               'preimage_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                               'preimage_exists': path.exists(), 'preimage_mode': path.stat().st_mode & 0o777 if path.exists() else 0o600,
                               'updated': json.dumps(updated, indent=2) + '\n'}
            actions.append(settings_action)
    plan = {'seat_id': identifier, 'operational_anchor': anchor,
            'seat_manifest_sha256': delivery_runs.digest(session_store.seat_dir(identifier) / 'seat.json'),
            'actions': actions, 'runtime_version': runtime.get('version')}
    plan['plan_sha256'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def recover_apply(args):
    proposed = recover_plan(args)
    if args.plan_sha256 != proposed['plan_sha256']:
        raise ValueError('Recovery plan changed; inspect a fresh recover --plan before applying')
    if not proposed['actions']:
        return {'recovered': False, 'reason': 'No repairable drift observed', 'plan_sha256': proposed['plan_sha256']}
    identifier = proposed['seat_id']
    seat = session_store.load_seat(identifier)
    with session_store.locked_seat(identifier):
        seat = session_store.load_seat(identifier)
        if not seat or lane_scope.native_identity() != seat.get('owner'):
            raise ValueError('Permanent seat owner changed after recovery planning; inspect a fresh plan')
        current_proposed = recover_plan(args)
        if current_proposed['plan_sha256'] != args.plan_sha256:
            raise ValueError('Recovery plan or seat anchor changed after planning; inspect a fresh plan')
        proposed = current_proposed
        if any(action['kind'] == 'restore-runtime' for action in proposed['actions']):
            session_store.install_runtime(APP, session_store.runtime_plan(APP))
        if any(action['kind'] == 'restore-adapter' for action in proposed['actions']):
            runtime_cli = Path(seat['runtime']['path']) / 'lane.py'
            anchor = active_operational_anchor(seat)
            write(seat['adapter'], adapter_script(anchor['root'], seat_identifier=identifier,
                                                   runtime_cli=runtime_cli,
                                                   real_gh=seat['real_gh']['path']), 0o700)
        for action in proposed['actions']:
            if action['kind'] == 'restore-hooks':
                path = Path(action['path'])
                raw = path.read_text() if path.exists() else ''
                if hashlib.sha256(raw.encode()).hexdigest() != action['preimage_sha256']:
                    raise ValueError('Seat settings changed after recovery planning; review a new plan')
                write(path, action['updated'], action['preimage_mode'])
        seat = session_store.load_seat(identifier)
        seat['updated_at'] = time.time()
        session_store.save_seat(seat)
    return {'recovered': True, 'plan_sha256': proposed['plan_sha256'],
            'actions': [action['kind'] for action in proposed['actions']],
            'notice': 'Only missing managed runtime, entrypoint or exact hook content was restored; no report or verification ran.'}


def validate_seat_retirement_image(image, expected_path, label):
    if (not isinstance(image, dict) or image.get('path') != str(expected_path) or
            not isinstance(image.get('exists'), bool) or not isinstance(image.get('text'), str) or
            not isinstance(image.get('mode'), int) or
            image.get('sha256') != text_digest(image['text'])):
        raise ValueError('The seat retirement ' + label + ' image is malformed; preserve the journal and inspect manually')


def recover_seat_retirement(identifier):
    journal_path = seat_retirement_journal_path(identifier)
    if journal_path.is_symlink():
        raise ValueError('Seat retirement journal is symlinked; leave it and all managed files untouched for owner review')
    if not journal_path.exists():
        return None
    refuse_symlinked_path(journal_path)
    if journal_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError('Seat retirement journal mode changed; preserve it and inspect manually')
    try:
        journal = json.loads(journal_path.read_text())
    except (OSError, TypeError, ValueError) as error:
        raise ValueError('Seat retirement journal is unreadable; preserve it and inspect manually') from error
    if (not isinstance(journal, dict) or journal.get('version') != 1 or
            journal.get('protocol') != SEAT_RETIREMENT_PROTOCOL or journal.get('seat_id') != identifier):
        raise ValueError('Seat retirement journal version or identity is unsupported; preserve it and inspect manually')
    actual = lane_scope.native_identity()
    operator = journal.get('operator')
    if (not isinstance(operator, dict) or
            {key: actual.get(key) for key in ('harness', 'session_id')} !=
            {key: operator.get(key) for key in ('harness', 'session_id')}):
        raise ValueError('Only the exact native operator that applied this seat retirement may roll it back')
    expected_context = journal.get('operator_context', {})
    actual_context = lane_scope.native_session_context() if actual['harness'] == 'opencode' else {}
    if actual_context != expected_context:
        raise ValueError('The native operator session context changed since this retirement began')
    seat_dir = session_store.seat_dir(identifier)
    seat_path = seat_dir / 'seat.json'
    adapter_path = seat_dir / 'bin' / 'gh'
    manifest_info = journal.get('seat_manifest')
    adapter_info = journal.get('adapter')
    settings_info = journal.get('settings')
    if (not isinstance(manifest_info, dict) or not isinstance(adapter_info, dict) or
            manifest_info.get('path') != str(seat_path) or adapter_info.get('path') != str(adapter_path)):
        raise ValueError('Seat retirement journal paths do not match the exact durable seat')
    manifest_pre = manifest_info.get('preimage')
    manifest_post = manifest_info.get('postimage')
    adapter_pre = adapter_info.get('preimage')
    adapter_post = adapter_info.get('postimage')
    validate_seat_retirement_image(manifest_pre, seat_path, 'manifest preimage')
    validate_seat_retirement_image(adapter_pre, adapter_path, 'adapter preimage')
    if (not manifest_pre['exists'] or not adapter_pre['exists'] or
            not isinstance(manifest_post, dict) or not isinstance(adapter_post, dict)):
        raise ValueError('Seat retirement journal preimages or postimages are incomplete')
    if (manifest_post.get('exists') is not True or not isinstance(manifest_post.get('text'), str) or
            manifest_post.get('sha256') != text_digest(manifest_post['text']) or
            manifest_post.get('mode') != manifest_pre['mode'] or
            adapter_post.get('exists') is not False or adapter_post.get('text') != '' or
            adapter_post.get('sha256') != text_digest('')):
        raise ValueError('Seat retirement journal postimages are malformed; preserve it and inspect manually')
    validate_seat_retirement_image(manifest_post, seat_path, 'manifest postimage')
    validate_seat_retirement_image(adapter_post, adapter_path, 'adapter postimage')
    try:
        original_seat = json.loads(manifest_pre['text'])
        retired_seat = json.loads(manifest_post['text'])
    except (TypeError, ValueError) as error:
        raise ValueError('Seat retirement manifest journal images are malformed; preserve them and inspect manually') from error
    if (not isinstance(original_seat, dict) or not isinstance(retired_seat, dict) or
            original_seat.get('seat_id') != identifier or retired_seat.get('seat_id') != identifier or
            session_store.seat_id(original_seat.get('seat_root', '')) != identifier or
            original_seat.get('adapter') != str(adapter_path) or
            adapter_pre.get('mode') != 0o700 or
            adapter_pre.get('sha256') != original_seat.get('adapter_sha256') or
            retired_seat.get('owner') != original_seat.get('owner') or
            retired_seat.get('checkouts') != original_seat.get('checkouts') or
            retired_seat.get('prs') != original_seat.get('prs') or
            session_store.operational_anchor(retired_seat)['state'] != 'released' or
            retired_seat.get('hooks_installed') is not False or retired_seat.get('groups') != {} or
            (retired_seat.get('retirement_receipt') or {}).get('protocol') != SEAT_RETIREMENT_PROTOCOL or
            (retired_seat.get('retirement_receipt') or {}).get('plan_sha256') != journal.get('plan_sha256') or
            (retired_seat.get('retirement_receipt') or {}).get('operator') != journal.get('operator') or
            (retired_seat.get('retirement_receipt') or {}).get('operator_context') != journal.get('operator_context') or
            (retired_seat.get('retirement_receipt') or {}).get('history_preserved') is not True):
        raise ValueError('Seat retirement journal does not preserve the exact seat history; preserve it and inspect manually')
    retirement_receipt = retired_seat.get('retirement_receipt') or {}
    retired_adapter = retirement_receipt.get('adapter')
    if (not isinstance(retired_adapter, dict) or retired_adapter.get('path') != str(adapter_path) or
            retired_adapter.get('preimage_sha256') != adapter_pre['sha256'] or
            retired_adapter.get('mode') != adapter_pre['mode'] or retired_adapter.get('remove') is not True or
            adapter_post.get('mode') != adapter_pre['mode']):
        raise ValueError('Seat retirement adapter receipt does not match its journal preimage')
    settings_pre = settings_post = None
    if bool(original_seat.get('hooks_installed')) != (settings_info is not None):
        raise ValueError('Seat retirement journal settings do not match the recorded hook ownership')
    if settings_info is not None:
        if not isinstance(settings_info, dict) or not isinstance(settings_info.get('path'), str):
            raise ValueError('Seat retirement settings journal is malformed; preserve it and inspect manually')
        settings_path = Path(settings_info['path'])
        old_anchor = session_store.operational_anchor(original_seat)
        if (old_anchor['state'] != 'active' or
                settings_path != Path(old_anchor['root']) / '.claude/settings.local.json' or
                (original_seat.get('settings') or {}).get('path') != str(settings_path)):
            raise ValueError('Seat retirement settings path differs from the recorded active anchor')
        settings_pre = settings_info.get('preimage')
        settings_post = settings_info.get('postimage')
        validate_seat_retirement_image(settings_pre, settings_path, 'settings preimage')
        validate_seat_retirement_image(settings_post, settings_path, 'settings postimage')
        if (not settings_pre['exists'] or not isinstance(settings_post, dict) or
                settings_post.get('exists') is not True or not isinstance(settings_post.get('text'), str) or
                settings_post.get('sha256') != text_digest(settings_post['text']) or
                settings_post.get('mode') != settings_pre['mode']):
            raise ValueError('Seat retirement settings postimage is malformed; preserve it and inspect manually')
        owned_groups = original_seat.get('groups')
        if not isinstance(owned_groups, dict) or not owned_groups:
            raise ValueError('The preimage seat has no valid hook groups for its recorded settings removal')
        try:
            settings_value = json.loads(settings_pre['text'])
        except (TypeError, ValueError) as error:
            raise ValueError('Seat retirement settings preimage is malformed; preserve it and inspect manually') from error
        if not isinstance(settings_value, dict) or not isinstance(settings_value.get('hooks', {}), dict):
            raise ValueError('Seat retirement settings preimage has no valid hook map')
        updated_settings = copy.deepcopy(settings_value)
        updated_hooks = updated_settings['hooks']
        for event, group in sorted(owned_groups.items()):
            existing = updated_hooks.get(event, [])
            if not isinstance(existing, list) or existing.count(group) != 1:
                raise ValueError('Seat retirement journal does not remove one exact recorded hook group')
            commands = hook_commands(group)
            if any(commands.intersection(hook_commands(other)) and other != group for other in existing):
                raise ValueError('Seat retirement journal hook group changed independently')
            existing.remove(group)
        if not any(updated_hooks.values()) and not settings_value.get('hooks'):
            updated_settings.pop('hooks', None)
        expected_settings_post = json.dumps(updated_settings, indent=2) + '\n'
        if settings_post['text'] != expected_settings_post:
            raise ValueError('Seat retirement settings postimage changes data beyond the exact owned hook groups')
        retired_settings = retirement_receipt.get('settings')
        if (not isinstance(retired_settings, dict) or
                retired_settings.get('path') != str(settings_path) or
                retired_settings.get('preimage_sha256') != settings_pre['sha256'] or
                retired_settings.get('postimage_sha256') != settings_post['sha256'] or
                retired_settings.get('mode') != settings_pre['mode']):
            raise ValueError('Seat retirement receipt settings do not match their journal preimages')
    elif retirement_receipt.get('settings') is not None:
        raise ValueError('Seat retirement receipt names settings without a journaled hook removal')

    def image_state(path, before, after, label):
        refuse_symlinked_path(path)
        current = capture_text(path, before['mode'])
        for expected in (before, after):
            if (current['exists'] == expected['exists'] and current['sha256'] == expected['sha256'] and
                    (not current['exists'] or current['mode'] == expected['mode'])):
                return current, expected
        raise ValueError('The ' + label + ' changed during interrupted seat retirement; preserve the journal and inspect manually')

    manifest_now, manifest_expected = image_state(seat_path, manifest_pre, manifest_post, 'seat manifest')
    adapter_now, adapter_expected = image_state(adapter_path, adapter_pre, adapter_post, 'seat adapter')
    settings_now = None
    if settings_info is not None:
        settings_now, settings_expected = image_state(Path(settings_info['path']), settings_pre,
                                                      settings_post, 'Claude settings')

    def restore(path, before, after, label):
        refuse_symlinked_path(path)
        current = capture_text(path, before['mode'])
        if (current['exists'] == before['exists'] and current['sha256'] == before['sha256'] and
                (not current['exists'] or current['mode'] == before['mode'])):
            return
        if (current['exists'] != after['exists'] or current['sha256'] != after['sha256'] or
                (current['exists'] and current['mode'] != after['mode'])):
            raise ValueError('The ' + label + ' changed immediately before rollback; preserve the journal and inspect manually')
        if before['exists']:
            write(path, before['text'], before['mode'])
        else:
            path.unlink()
            sync_directory(path.parent)

    if settings_info is not None:
        restore(Path(settings_info['path']), settings_pre, settings_post, 'Claude settings')
    restore(adapter_path, adapter_pre, adapter_post, 'seat adapter')
    restore(seat_path, manifest_pre, manifest_post, 'seat manifest')
    journal_path.unlink()
    sync_directory(journal_path.parent)
    return {'rolled_back': True, 'kind': 'seat-retirement', 'history_preserved': True,
            'seat_id': identifier,
            'notice': 'The exact seat manifest, adapter and settings preimages were restored. The native host refresh state remains unobserved.'}


def recover_rollback(args):
    identifier=args.seat_id
    if not identifier:
        state=read(args.cwd)
        identifier=(state or {}).get('seat_id')
    if not identifier:
        raise ValueError('No durable seat journal is associated with this checkout')
    with session_store.locked_seat(identifier):
        retirement_rollback = recover_seat_retirement(identifier)
        if retirement_rollback is not None:
            return retirement_rollback
        seat=session_store.load_seat(identifier)
        if not seat:
            raise ValueError('Permanent seat registry is missing; no rollback was attempted')
        handoff=seat.get('handoff_journal')
        if handoff:
            actual=lane_scope.native_identity()
            if actual not in (handoff.get('from'),handoff.get('to')):
                raise ValueError('Only the exact outgoing or incoming owner may recover this interrupted handoff')
            seat_path=session_store.seat_dir(identifier).resolve()
            for change in handoff.get('state_changes',[]):
                path=Path(change['path']);refuse_symlinked_path(path)
                if not path.resolve().is_relative_to(seat_path):
                    raise ValueError('Handoff journal points outside its owned state directory; refusing rollback')
                if not path.exists():
                    raise ValueError('A handoff checkout state disappeared; preserve the journal and inspect manually')
                digest=delivery_runs.digest(path)
                if digest not in (change['preimage_sha256'],change['postimage_sha256']):
                    raise ValueError('Checkout state changed during handoff recovery; preserve it and inspect manually')
            settings_change=handoff.get('settings_change')
            if settings_change:
                settings_path=Path(settings_change['path']);refuse_symlinked_path(settings_path)
                preimage_anchor=active_operational_anchor(handoff['seat_preimage'])
                expected_settings=Path(preimage_anchor['root'])/'.claude/settings.local.json'
                if settings_path!=expected_settings:
                    raise ValueError('Handoff settings path differs from the permanent seat; preserve the journal')
                settings_exists=settings_path.exists()
                settings_raw=settings_path.read_text() if settings_exists else ''
                current_hash=hashlib.sha256(settings_raw.encode()).hexdigest()
                is_preimage=(settings_exists==settings_change['preimage_exists'] and
                             current_hash==settings_change['preimage_sha256'])
                is_postimage=(settings_exists and current_hash==settings_change['updated_sha256'])
                if not (is_preimage or is_postimage):
                    raise ValueError('Claude settings changed during handoff recovery; preserve them and inspect manually')
                if settings_exists and settings_path.stat().st_mode & 0o777 != settings_change['mode']:
                    raise ValueError('Claude settings mode changed during handoff recovery; preserve them and inspect manually')
            for change in handoff.get('state_changes',[]):
                if delivery_runs.digest(change['path'])!=change['preimage_sha256']:
                    write(change['path'],change['preimage'])
            if settings_change and is_postimage:
                if settings_change['preimage_exists']:
                    write(settings_path,settings_change['preimage'],settings_change['mode'])
                else:
                    settings_path.unlink(missing_ok=True)
                    sync_directory(settings_path.parent)
            session_store.save_seat(handoff['seat_preimage'])
            return {'rolled_back':True,'kind':'owner-handoff','history_preserved':True,
                    'notice':'Every recorded checkout preimage and the outgoing seat owner were restored; no report was recreated.'}
        if lane_scope.native_identity()!=seat.get('owner'):
            raise ValueError('Only the exact current seat owner may roll back its interrupted operation')
        journal=seat.get('migration_journal')
        if journal:
            legacy_path=Path(journal['legacy_state_path'])
            refuse_symlinked_path(legacy_path)
            expected_legacy=json.loads(journal['legacy_state'])
            if legacy_path.exists():
                current=json.loads(legacy_path.read_text())
                is_original=hashlib.sha256(legacy_path.read_bytes()).hexdigest()==journal['legacy_state_sha256']
                is_tombstone=(current.get('phase')=='migrated' and current.get('seat_id')==identifier and
                              current.get('preserved_state_sha256')==journal['legacy_state_sha256'])
                if not (is_original or is_tombstone):
                    raise ValueError('Legacy state changed during interrupted migration; preserve the journal and inspect manually')
            for change in journal.get('settings_changes',[]):
                path=Path(change['path']);refuse_symlinked_path(path)
                raw=path.read_text() if path.exists() else ''
                digest=hashlib.sha256(raw.encode()).hexdigest()
                if digest not in (change['preimage_sha256'],change['updated_sha256']):
                    raise ValueError('Settings changed during interrupted migration; preserve the journal and inspect manually')
            old_adapter=Path(journal['legacy_adapter'])
            old_text=journal.get('legacy_adapter_text')
            if old_text is not None and old_adapter.exists():
                current_hash=delivery_runs.digest(old_adapter)
                passthrough_hash=hashlib.sha256(passthrough(seat['real_gh']['path']).encode()).hexdigest()
                if current_hash not in (journal['legacy_adapter_sha256'],passthrough_hash):
                    raise ValueError('Legacy adapter changed during interrupted migration; preserve the journal and inspect manually')
            new_adapter=Path(journal['new_adapter_path'])
            preimage=journal.get('seat_preimage') or {}
            old_adapter_path=preimage.get('adapter')
            old_adapter_hash=preimage.get('adapter_sha256')
            if new_adapter.exists() and (str(new_adapter)!=old_adapter_path or
                                         delivery_runs.digest(new_adapter)!=old_adapter_hash):
                if delivery_runs.digest(new_adapter)!=journal['new_adapter_sha256']:
                    raise ValueError('New seat entrypoint changed during interrupted migration; preserve it and inspect manually')
            state_path=Path(journal['new_state_path'])
            seat_path=session_store.seat_dir(identifier).resolve()
            if not state_path.resolve().is_relative_to(seat_path):
                raise ValueError('Migration journal points outside the owned seat directory; refusing rollback')
            if state_path.exists():
                try:
                    migrated=json.loads(state_path.read_text())
                except (OSError,ValueError):
                    raise ValueError('Migrated checkout state is malformed; preserve it and inspect manually')
                if migrated.get('seat_id')!=identifier or migrated.get('root')!=expected_legacy.get('root'):
                    raise ValueError('Migrated checkout state identity changed; preserve it and inspect manually')
            for change in journal.get('settings_changes',[]):
                path=Path(change['path'])
                if hashlib.sha256((path.read_text() if path.exists() else '').encode()).hexdigest()!=change['preimage_sha256']:
                    write(path,change['preimage'],change['mode'])
            if old_text is not None and (not old_adapter.exists() or
                                         delivery_runs.digest(old_adapter)!=journal['legacy_adapter_sha256']):
                write(old_adapter,old_text,journal.get('legacy_adapter_mode') or 0o700)
            if new_adapter.exists() and (str(new_adapter)!=old_adapter_path or
                                         delivery_runs.digest(new_adapter)!=old_adapter_hash):
                new_adapter.unlink()
            if state_path.exists():
                state_path.unlink()
            if not legacy_path.exists() or hashlib.sha256(legacy_path.read_bytes()).hexdigest()!=journal['legacy_state_sha256']:
                write(legacy_path,journal['legacy_state'])
            if journal.get('seat_preimage') is None:
                seat_file=session_store.seat_dir(identifier)/'seat.json'
                if seat_file.exists(): seat_file.unlink()
            else:
                session_store.save_seat(journal['seat_preimage'])
            return {'rolled_back':True,'kind':'migration','history_preserved':True,
                    'notice':'The legacy state, settings and adapter preimages were restored. The immutable runtime was retained for safe future use.'}
        journal=seat.get('install_journal')
        if not journal:
            raise ValueError('No interrupted install or migration journal exists')
        if journal.get('kind') == 'seat-install':
            seat_path=session_store.seat_dir(identifier).resolve()
            state_path=Path(journal['state_path'])
            adapter=Path(journal['adapter_path'])
            for path in (state_path,adapter):
                refuse_symlinked_path(path)
            if (journal.get('seat_id') != identifier or
                    state_path.resolve().parent.name != journal.get('checkout_id') or
                    not state_path.resolve().is_relative_to(seat_path) or
                    adapter.resolve().parent != (seat_path / 'bin').resolve()):
                raise ValueError('Install journal paths do not match the owned seat directory; preserve it for review')
            state_preimage=journal.get('state_preimage')
            if state_path.exists():
                current_state_hash=delivery_runs.digest(state_path)
                allowed={journal.get('state_installing_sha256'),journal.get('state_active_sha256')}
                if journal.get('state_preimage_sha256'):
                    allowed.add(journal['state_preimage_sha256'])
                if current_state_hash not in allowed:
                    raise ValueError('Checkout state changed during interrupted install; preserve it and inspect manually')
            elif state_preimage is not None:
                raise ValueError('Checkout state disappeared during interrupted install; preserve the journal and inspect manually')
            adapter_preimage=journal.get('adapter_preimage')
            if adapter.exists():
                current_adapter_hash=delivery_runs.digest(adapter)
                allowed={journal.get('adapter_replacement_sha256')}
                if journal.get('adapter_preimage_sha256'):
                    allowed.add(journal['adapter_preimage_sha256'])
                if current_adapter_hash not in allowed:
                    raise ValueError('Seat entrypoint changed during interrupted install; preserve it and inspect manually')
            elif adapter_preimage is not None:
                raise ValueError('Seat entrypoint disappeared during interrupted install; preserve the journal and inspect manually')
            settings_info=journal.get('settings')
            if settings_info:
                settings_path=Path(settings_info['path'])
                refuse_symlinked_path(settings_path)
                settings_exists=settings_path.exists()
                settings_raw=settings_path.read_text() if settings_exists else ''
                settings_hash=hashlib.sha256(settings_raw.encode()).hexdigest()
                is_preimage=(settings_exists == settings_info['preimage_exists'] and
                             settings_hash == settings_info['preimage_sha256'])
                is_postimage=(settings_exists and settings_hash == settings_info['postimage_sha256'])
                if not (is_preimage or is_postimage):
                    raise ValueError('Seat settings changed during interrupted install; preserve them and inspect manually')
            else:
                settings_path=None
            if state_preimage is None:
                state_path.unlink(missing_ok=True)
            elif state_path.read_text()!=state_preimage:
                write(state_path,state_preimage,journal.get('state_preimage_mode') or 0o600)
            if adapter_preimage is None:
                adapter.unlink(missing_ok=True)
            elif adapter.read_text()!=adapter_preimage:
                write(adapter,adapter_preimage,journal.get('adapter_preimage_mode') or 0o700)
            if settings_info:
                if settings_info['preimage_exists']:
                    if not settings_path.exists() or settings_path.read_text()!=settings_info['preimage']:
                        write(settings_path,settings_info['preimage'],settings_info['mode'])
                elif settings_path.exists():
                    settings_path.unlink()
                    sync_directory(settings_path.parent)
            preimage=journal.get('seat_preimage')
            if preimage is None:
                (session_store.seat_dir(identifier)/'seat.json').unlink(missing_ok=True)
            else:
                session_store.save_seat(preimage)
            return {'rolled_back':True,'kind':'seat-install','history_preserved':True,
                    'notice':'The incomplete seat checkout, entrypoint and owned hook changes were restored to their exact preimages. The immutable runtime was retained.'}
        settings_path=Path(journal['settings_path']);refuse_symlinked_path(settings_path)
        raw=settings_path.read_text() if settings_path.exists() else ''
        preimage=journal['settings_preimage']
        postimage=journal['settings_postimage']
        if raw not in (preimage,postimage):
            raise ValueError('Seat settings changed during interrupted install; preserve the journal and inspect manually')
        adapter=Path(journal['adapter_path'])
        if adapter.exists() and delivery_runs.digest(adapter)!=journal['adapter_sha256']:
            adapter_preimage=journal.get('adapter_preimage')
            if adapter_preimage is None or delivery_runs.digest(adapter)!=hashlib.sha256(adapter_preimage.encode()).hexdigest():
                raise ValueError('Seat entrypoint changed during interrupted install; preserve it and inspect manually')
        if raw!=preimage:
            write(settings_path,preimage,journal['settings_mode'])
        if adapter.exists() and journal.get('adapter_preimage') is None:
            adapter.unlink()
        elif adapter.exists() and journal.get('adapter_preimage') is not None:
            write(adapter,journal['adapter_preimage'],journal.get('adapter_preimage_mode') or 0o700)
        for item in session_store.all_checkouts(identifier):
            state=item.get('state')
            if state and state.get('phase')=='installing':
                path=Path(item['checkout_dir'])/'state.json'
                if state.get('seat_id')!=identifier:
                    raise ValueError('Interrupted checkout state identity changed; preserve it and inspect manually')
                path.unlink(missing_ok=True)
        if journal.get('seat_preimage') is None:
            (session_store.seat_dir(identifier)/'seat.json').unlink(missing_ok=True)
        else:
            session_store.save_seat(journal['seat_preimage'])
        return {'rolled_back':True,'kind':'install','history_preserved':True,
                'notice':'The incomplete hook install, adapter and state were rolled back. The immutable runtime was retained.'}


def cleanup_plan(retention_days=None, *, limit=20, seat_id=None):
    return seat_cleanup.preview(retention_days,limit=limit,seat_id=seat_id)


def cleanup_apply_plan(proposed):
    return seat_cleanup.apply(proposed)


def cleanup_apply(args):
    return seat_cleanup.apply_reviewed(args.retention_days,args.plan_sha256,args.seat_id)


def selected_seat_id(args):
    if args.seat_id:
        return args.seat_id
    state=read(args.cwd)
    if state and state.get('seat_id'):
        return state['seat_id']
    raise ValueError('Use an attached product checkout or pass --seat-id for the permanent orchestrator seat')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd', type=Path, default=Path.cwd())
    parser.add_argument('--installation', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--session-root', type=Path, help='Permanent orchestrator context when the product worktree is separate')
    parser.add_argument('--seat-id', help='Permanent seat for standalone operations or to select the exact identity of a moved seat')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('plan', 'install'):
        help_text = ('Review a per-PR or native-owner delivery installation' if action == 'plan' else
                     'Apply a reviewed per-PR installation or native-owner checkout attachment')
        p = sub.add_parser(action, help=help_text, description=help_text)
        p.add_argument('--pr-url')
        p.add_argument('--base-ref')
        p.add_argument('--lane', action='store_true', help='Enroll this product and native owner, optionally binding a PR or discovering it later')
        p.add_argument('--owner-harness',choices=('claude','codex','opencode'))
        p.add_argument('--owner-session')
        p.add_argument('--host',default='github.com')
        p.add_argument('--head-branch', help='Explicit published head-branch mapping when different from the local branch')
        if action == 'install':
            p.add_argument('--plan-sha256', required=True)
    p = sub.add_parser('upgrade-plan', help='Read-only plan to replace an active installation source pin or recover a missing permanent seat root')
    p.add_argument('--replacement-seat-root', type=Path,
                   help='Exact same-repository Git worktree to become the active operational anchor when the birth root and Gitdir are absent')
    p.add_argument('--release-anchor-seat',
                   help='Exact same-owner seat at the replacement root; all of its attachments must already be retired')
    p = sub.add_parser('upgrade', help='Apply a reviewed active-installation source upgrade')
    p.add_argument('--plan-sha256', required=True)
    p.add_argument('--replacement-seat-root', type=Path,
                   help='Exact replacement root from the reviewed upgrade plan')
    p.add_argument('--release-anchor-seat',
                   help='Exact seat whose empty operational anchor is released in this transaction')
    p = sub.add_parser('upgrade-recover', help='Restore exact preimages after an interrupted source upgrade')
    p.add_argument('--rollback', action='store_true', required=True,
                   help='Explicitly restore the previous source pin and owned files')
    sub.add_parser('discover',help='Read the published PR for this enrolled product branch and preserve per-PR report history')
    p = sub.add_parser('rebind-owner',help='Explicitly authorized native-session handoff after validating the seat runtime; no implicit inheritance')
    p.add_argument('--from-harness',choices=('claude','codex','opencode'),required=True)
    p.add_argument('--from-session',required=True)
    p.add_argument('--confirm-handoff',action='store_true',required=True)
    status_help = 'Inspect attachment, owner, source and report state, with one read-only setup recommendation'
    p = sub.add_parser('status', help=status_help, description=status_help)
    p.add_argument('--session-id', help='Select activation observations for this actual session; does not identify the acting owner')
    coverage_help = 'Read-only attachment and observed-path coverage, with one setup recommendation; no discovery or probes'
    p = sub.add_parser('coverage', help=coverage_help, description=coverage_help)
    p.add_argument('--summary', action='store_true', help='Concise text and next action; default remains detailed JSON')
    p.add_argument('--pr-url', help='Caller-declared current PR; hosted state is not checked')
    sub.add_parser('uninstall', help='Remove or deactivate an installation without deleting its delivery history')
    sub.add_parser('registrations', help='Read known local PR bindings from an attached checkout or permanent seat')
    sub.add_parser('hook')
    sub.add_parser('session')
    p = sub.add_parser(
        'record', help='record a read-only delivery report and validate an included guidance attestation',
        epilog='Example: lane.py --cwd /worktree record --evidence /private/delivery-evidence.json')
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--policy', type=Path, default=delivery.POLICY)
    p.add_argument('--format', choices=('json', 'text'), default='text')
    p = sub.add_parser(
        'attestation-render', help='render a current opening, repair-batch, or successor-validation attestation without writes',
        epilog='Example: lane.py --cwd /worktree attestation-render --evidence /private/delivery-evidence.json --stage repair-batch')
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--stage', choices=('opening', 'repair-batch', 'successor-validation'), required=True)
    p.add_argument('--format', choices=('markdown', 'json'), default='markdown')
    sub.add_parser(
        'attestation-rereads',
        help='print required-read packet entries; add source_ref values before use',
        description='Print ready-to-paste reread entries for applicable Pattern Buffer files. Add a source_ref to each entry.',
        epilog='Example: lane.py --cwd /worktree attestation-rereads')
    p = sub.add_parser(
        'attestation-start-check', help='validate one exact published attestation comment and bind it to this PR',
        epilog='Example: lane.py --cwd /worktree attestation-start-check --evidence /private/delivery-evidence.json --stage repair-batch --comment-url https://github.com/OWNER/REPO/pull/123#issuecomment-ID')
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--stage', choices=('opening', 'repair-batch', 'successor-validation'), required=True)
    p.add_argument('--comment-url', required=True)
    p.add_argument('--enable', action='store_true', help='Explicitly adopt attestation-required behavior for this owner lane')
    p = sub.add_parser('init-evidence', help='Create a PR-bound evidence starter outside Git worktrees; never run checks')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--designs', choices=('yes', 'no'), required=True)
    p.add_argument('--no-design-reason')
    p = sub.add_parser('gh', help='Gate covered PR actions for attached repositories and pass unrelated commands through')
    p.add_argument('argv', nargs=argparse.REMAINDER)
    p = sub.add_parser('guide', help='Print the setup and authority guide without creating state')
    p.add_argument('--print', action='store_true', dest='print_guide', required=True)
    p = sub.add_parser('recover', help='Inspect or repair missing seat runtime, adapter or owned hook files, or roll back an interrupted transaction')
    recovery_modes = p.add_mutually_exclusive_group(required=True)
    recovery_modes.add_argument('--inspect', action='store_true')
    recovery_modes.add_argument('--plan', action='store_true')
    recovery_modes.add_argument('--apply', action='store_true')
    recovery_modes.add_argument('--rollback', action='store_true')
    p.add_argument('--plan-sha256')
    sub.add_parser('opencode', help=argparse.SUPPRESS)
    migration_help = 'Review a legacy installation, including a no-PR lane, for durable seat migration'
    p = sub.add_parser('migrate-plan', help=migration_help, description=migration_help)
    p.add_argument('--session-root', type=Path)
    p.add_argument('--host', default='github.com')
    p.add_argument('--confirm-owner', action='store_true')
    p.add_argument('--confirm-handoff', action='store_true')
    p.add_argument('--from-harness', choices=('claude','codex'))
    p.add_argument('--from-session')
    p = sub.add_parser('migrate', help='Move reviewed legacy state into a durable seat without fabricating evidence')
    p.add_argument('--session-root', type=Path)
    p.add_argument('--host', default='github.com')
    p.add_argument('--confirm-owner', action='store_true')
    p.add_argument('--confirm-handoff', action='store_true')
    p.add_argument('--from-harness', choices=('claude','codex'))
    p.add_argument('--from-session')
    p.add_argument('--plan-sha256', required=True)
    p = sub.add_parser('cleanup', help='Preview or apply bounded removal of aged terminal PR history and unreferenced runtimes')
    cleanup_modes=p.add_mutually_exclusive_group(required=True)
    cleanup_modes.add_argument('--preview',action='store_true',help='Show exact eligible local records and plan hash without removing them')
    cleanup_modes.add_argument('--apply',action='store_true',help='Remove only candidates from the exact reviewed plan hash')
    p.add_argument('--retention-days',type=int)
    p.add_argument('--plan-sha256')
    p = sub.add_parser('retire', help='Record local terminal status from a read-only GitHub PR observation')
    p.add_argument('--pr-url',required=True)
    p = sub.add_parser('pin', help='Protect one local PR record from retention cleanup')
    p.add_argument('--pr-url',required=True)
    p = sub.add_parser('unpin', help='Remove a local PR retention pin')
    p.add_argument('--pr-url',required=True)
    p = sub.add_parser('retire-attachment', help='Retire a missing Policy Guard checkout with a reviewed plan hash',
                       description='Plan or apply history-preserving retirement of one missing Policy Guard attachment.',
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog='Example:\n  lane.py --seat-id SEAT_ID retire-attachment --checkout /worktrees/temporary-feature\n  lane.py --seat-id SEAT_ID retire-attachment --checkout /worktrees/temporary-feature --apply --plan-sha256 REVIEWED_PLAN_SHA256')
    p.add_argument('--checkout', type=Path, required=True,
                   help='Absolute recorded checkout path to inspect; the command never writes to this path')
    p.add_argument('--apply', action='store_true', help='Apply only the exact plan named by --plan-sha256')
    p.add_argument('--plan-sha256', help='Exact hash from a reviewed retire-attachment plan')
    p = sub.add_parser('retire-seat', help='Retire one exact drained durable seat after existing authorization',
                       description='Plan or apply removal of one drained seat adapter and its exact owned hook groups.')
    p.add_argument('--expected-owner-harness', choices=('claude', 'codex', 'opencode'), required=True,
                   help='Exact historical harness recorded on the durable seat')
    p.add_argument('--expected-owner-session', required=True,
                   help='Exact historical session recorded on the durable seat')
    p.add_argument('--expected-operator-harness', choices=('claude', 'codex', 'opencode'), required=True,
                   help='Harness of the actual operator running this command')
    p.add_argument('--expected-operator-session', required=True,
                   help='Session of the actual operator running this command')
    p.add_argument('--authorization-ref', required=True,
                   help='Short reference to the existing explicit authorization; this command does not authenticate it')
    p.add_argument('--confirm-retirement', action='store_true', required=True,
                   help='Confirm the reviewed seat-local retirement scope')
    p.add_argument('--apply', action='store_true', help='Apply only the exact plan named by --plan-sha256')
    p.add_argument('--plan-sha256', help='Exact hash from a reviewed retire-seat plan')
    return parser


def dispatch(args):
    try:
        if args.action not in ('status', 'coverage', 'registrations', 'guide', 'recover',
                               'retire-seat', 'opencode'):
            guarded_seat_id = args.seat_id
            if not guarded_seat_id:
                try:
                    found = session_store.lookup_checkout(root(args.cwd))
                    guarded_seat_id = found['seat']['seat_id'] if found else None
                except (OSError, TypeError, ValueError):
                    guarded_seat_id = None
            if guarded_seat_id:
                ensure_no_seat_retirement_journal(guarded_seat_id)
        if args.action == 'plan':
            print(json.dumps(plan(args), indent=2))
        elif args.action == 'install':
            print(json.dumps(install(args), indent=2))
        elif args.action == 'upgrade-plan':
            print(json.dumps(upgrade_plan(args), indent=2))
        elif args.action == 'upgrade':
            print(json.dumps(upgrade(args), indent=2))
        elif args.action == 'upgrade-recover':
            print(json.dumps(upgrade_recover(args), indent=2))
        elif args.action == 'discover':
            result=discover(args.cwd);result.pop('state');print(json.dumps(result,indent=2))
        elif args.action == 'rebind-owner':
            print(json.dumps(rebind_owner(args),indent=2))
        elif args.action == 'uninstall':
            print(json.dumps(uninstall(args.cwd), indent=2))
        elif args.action == 'record':
            return record(args)
        elif args.action == 'attestation-render':
            return attestation_render(args)
        elif args.action == 'attestation-rereads':
            return attestation_rereads(args)
        elif args.action == 'attestation-start-check':
            return attestation_start_check(args)
        elif args.action == 'gh':
            return gh(args)
        elif args.action == 'guide':
            print((APP / 'guide.md').read_text(), end='')
            return 0
        elif args.action == 'recover':
            if args.inspect:
                print(json.dumps(recover_inspect(args), indent=2))
            elif args.plan:
                print(json.dumps(recover_plan(args), indent=2))
            elif args.rollback:
                print(json.dumps(recover_rollback(args), indent=2))
            else:
                if not args.plan_sha256:
                    raise ValueError('recover --apply requires --plan-sha256 from a reviewed recover --plan')
                print(json.dumps(recover_apply(args), indent=2))
            return 0
        elif args.action == 'migrate-plan':
            print(json.dumps(migration_plan(args), indent=2))
            return 0
        elif args.action == 'migrate':
            print(json.dumps(migrate_legacy(args), indent=2))
            return 0
        elif args.action == 'cleanup':
            if args.preview:
                print(json.dumps(cleanup_plan(args.retention_days,seat_id=args.seat_id), indent=2))
            else:
                if not args.plan_sha256:
                    raise ValueError('cleanup --apply requires --plan-sha256 from a reviewed cleanup --preview')
                print(json.dumps(cleanup_apply(args), indent=2))
            return 0
        elif args.action == 'retire':
            print(json.dumps(seat_cleanup.retire(selected_seat_id(args),args.pr_url),indent=2))
            return 0
        elif args.action == 'pin':
            print(json.dumps(seat_cleanup.set_pinned(selected_seat_id(args),args.pr_url,True),indent=2))
            return 0
        elif args.action == 'unpin':
            print(json.dumps(seat_cleanup.set_pinned(selected_seat_id(args),args.pr_url,False),indent=2))
            return 0
        elif args.action == 'retire-attachment':
            print(json.dumps(retire_attachment(args), indent=2))
            return 0
        elif args.action == 'retire-seat':
            print(json.dumps(retire_seat(args), indent=2))
            return 0
        elif args.action == 'hook':
            return hook(args)
        elif args.action == 'session':
            return session(args)
        elif args.action == 'init-evidence':
            print(json.dumps(init_evidence(args), indent=2))
        elif args.action == 'coverage':
            detail = coverage(args.cwd, args.pr_url)
            print(coverage_summary(args.cwd, detail) if args.summary else json.dumps(detail, indent=2))
        elif args.action == 'registrations':
            print(json.dumps(registrations(args.cwd, args.seat_id), indent=2))
        elif args.action == 'status' and args.seat_id:
            print(json.dumps(seat_status(args.seat_id),indent=2))
        else:
            print(json.dumps(status(args.cwd, args.session_id), indent=2))
        return 0
    except Exception as error:
        print('starfleet-policy-guard delivery lane: ' + str(error), file=sys.stderr)
        return 2


def opencode_bridge(args):
    try:
        request=json.load(sys.stdin)
        if not isinstance(request,dict) or not isinstance(request.get('argv'),list) or \
                any(not isinstance(value,str) or '\0' in value for value in request['argv']):
            raise ValueError('OpenCode bridge request must contain a native tool event and string argv')
        event=request.get('native_event')
        nested=build_parser().parse_args(request['argv'])
        with lane_scope.native_event_scope(event) as identity:
            tool=event['tool']
            if nested.action not in ('plan','install','discover','rebind-owner','status','coverage','registrations','record','gh'):
                raise ValueError('OpenCode bridge request is outside the supported native Policy Guard operations')
            if nested.action=='opencode' or nested.installation or nested.seat_id:
                raise ValueError('OpenCode bridge does not accept nested bridges, legacy installations, or caller-selected seats')
            if tool=='prime_directive_gh':
                _,action,probe=parse_action(nested.argv[1:] if nested.argv[:1]==['--'] else nested.argv) \
                    if nested.action=='gh' else (None,None,False)
                if (nested.action!='gh' or not action or action.get('exempt') or probe or
                        action.get('action') not in ('ready','merge')):
                    raise ValueError('The OpenCode GitHub tool accepts only covered readiness or merge actions')
            elif nested.action=='gh':
                raise ValueError('Use the dedicated OpenCode GitHub tool for covered readiness and merge actions')
            if nested.action in ('plan','install'):
                if (not nested.lane or nested.owner_harness!='opencode' or
                        nested.owner_session!=identity['session_id'] or not nested.session_root):
                    raise ValueError('OpenCode enrollment must use the actual session API identity and an explicit permanent seat')
            if nested.action=='gh':
                nested.seat_id=selected_seat_id(nested)
                return seat_gh(nested, require_coverage=True)
            return dispatch(nested)
    except Exception as error:
        print('starfleet-policy-guard delivery lane: ' + str(error), file=sys.stderr)
        return 2


def main(argv=None):
    args=build_parser().parse_args(argv)
    if args.action=='opencode':
        return opencode_bridge(args)
    return dispatch(args)


if __name__ == '__main__':
    raise SystemExit(main())
