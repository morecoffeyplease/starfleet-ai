"""Bounded cleanup for terminal, unpinned local Policy Guard history."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import delivery_runs
import github_records
import lane_scope
import session_store

MAX_SEATS = 50
MAX_CHECKOUTS = 500
MAX_PR_RECORDS = 20
MAX_RUNTIMES = 5


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _refuse_symlink(path):
    target = Path(os.path.abspath(path))
    current = target
    while True:
        if current.is_symlink():
            raise ValueError('Refusing symlinked cleanup target: ' + str(target))
        if current.parent == current:
            break
        current = current.parent


def _within(path, parent):
    _refuse_symlink(path)
    resolved_parent = Path(parent).resolve()
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(resolved_parent):
        raise ValueError('Cleanup target escapes the owned Policy Guard directory')
    return resolved


def _state_path(seat_id, checkout_id):
    return session_store.seat_dir(seat_id) / 'checkouts' / checkout_id / 'state.json'


def _load_seats():
    root = session_store.state_root() / 'seats'
    if not root.exists():
        return [], False
    _refuse_symlink(root)
    rows = []
    uncertain = False
    paths=sorted(root.glob('*/seat.json'))
    if len(paths)>MAX_SEATS:
        uncertain=True
    for path in paths[:MAX_SEATS]:
        identifier = path.parent.name
        try:
            seat = session_store.load_seat(identifier)
            if seat:
                rows.append((identifier, seat))
        except (OSError, TypeError, ValueError):
            # A damaged seat may still refer to data. Preserve every runtime version.
            uncertain = True
    return rows, uncertain


def _state_record(seat_id, checkout_id, checkout):
    path = _state_path(seat_id, checkout_id)
    _within(path, session_store.seat_dir(seat_id))
    if checkout.get('state_file') != str(path) or not path.exists():
        return None
    value = json.loads(path.read_text())
    return {'path': str(path), 'sha256': delivery_runs.digest(path), 'value': value}


def _pr_refs(seat_id, seat):
    refs = {}
    scanned = 0
    for checkout_id, checkout in seat.get('checkouts', {}).items():
        scanned += 1
        if scanned > MAX_CHECKOUTS:
            raise ValueError('Seat checkout scan reached its safety bound; preserve records and retry later')
        record = _state_record(seat_id, checkout_id, checkout)
        if not record:
            continue
        state = record['value']
        for url in set(state.get('pr_bindings', {})) | ({state['pr_url']} if state.get('pr_url') else set()):
            refs.setdefault(url, []).append({'checkout_id': checkout_id, 'state': record})
    return refs


def _terminal_observation(pr, now):
    if not isinstance(pr, dict) or pr.get('state') != 'closed':
        return None
    return {'state': 'closed', 'closed_at': pr.get('closed_at'), 'merged_at': pr.get('merged_at'),
            'observed_at': now}


def _runtime_cleanup_suspended(rows,uncertain):
    return uncertain or any(seat.get('pinned') or seat.get('install_journal') or
        seat.get('migration_journal') or seat.get('handoff_journal') for _,seat in rows)


def observe_pr(seat_id, pr_url):
    """Refresh a terminal/open observation only on an explicit delivery API path."""
    seat = session_store.load_seat(seat_id)
    if not seat:
        raise ValueError('Permanent Policy Guard seat registry is missing')
    identity = lane_scope.native_identity()
    if identity != seat.get('owner'):
        raise ValueError('Only the exact seat owner may refresh terminal PR state')
    ref = github_records.reference(pr_url, 'pull')
    attached = any(item.get('host') == ref['host'] and
                   item.get('repository', '').casefold() == ref['repo'].casefold()
                   for item in seat.get('checkouts', {}).values())
    if not attached:
        raise ValueError('PR repository is not attached to this permanent seat')
    if ref['url'] not in seat.get('prs', {}):
        raise ValueError('PR is not in this seat\'s local registration history')
    remote = github_records.request(ref)
    now = time.time()
    terminal = _terminal_observation(remote, now)
    with session_store.locked_seat(seat_id):
        current = session_store.load_seat(seat_id)
        if not current or current.get('owner') != identity or current.get('handoff_journal'):
            raise ValueError('Seat owner or authority changed during terminal PR observation')
        record = current.setdefault('prs', {}).setdefault(ref['url'], {
            'url': ref['url'], 'host': ref['host'], 'repository': ref['repo'], 'checkout_ids': []})
        record['last_observation'] = {'state': remote.get('state'), 'observed_at': now,
                                      'closed_at': remote.get('closed_at'), 'merged_at': remote.get('merged_at')}
        if terminal:
            record['terminal_observation'] = terminal
        else:
            record.pop('terminal_observation', None)
        current['updated_at'] = now
        session_store.save_seat(current)
    return {'pr_url': ref['url'], 'state': remote.get('state'), 'terminal': bool(terminal),
            'observed_at': now, '_remote':remote,
            'notice': 'Read-only GitHub PR observation; no record body or fix count was changed.'}


def retire(seat_id, pr_url):
    result = observe_pr(seat_id, pr_url)
    if not result['terminal']:
        raise ValueError('PR is still open; no local terminal marker was added')
    result.pop('_remote',None)
    return {'retired': True, **result,
            'notice': 'Terminal state was observed from GitHub. Local receipts remain until retention cleanup; no GitHub record changed.'}


def set_pinned(seat_id, pr_url, pinned):
    seat = session_store.load_seat(seat_id)
    if not seat:
        raise ValueError('Permanent Policy Guard seat registry is missing')
    if lane_scope.native_identity() != seat.get('owner'):
        raise ValueError('Only the exact seat owner may change local retention pins')
    ref = github_records.reference(pr_url, 'pull')
    if ref['url'] not in seat.get('prs', {}):
        raise ValueError('PR has no local seat registration to pin')
    with session_store.locked_seat(seat_id):
        current = session_store.load_seat(seat_id)
        pins = current.setdefault('pinned_prs', {})
        if pinned:
            pins[ref['url']] = {'pinned_at': time.time(), 'owner': current['owner']}
        else:
            pins.pop(ref['url'], None)
        current['updated_at'] = time.time()
        session_store.save_seat(current)
    return {'pr_url': ref['url'], 'pinned': pinned, 'notice': 'Only local retention behavior changed.'}


def _runtime_candidates(rows, uncertain, cutoff):
    if _runtime_cleanup_suspended(rows,uncertain):
        return []
    referenced = set()
    for identifier, seat in rows:
        if seat.get('hooks_installed'):
            runtime = seat.get('runtime', {})
            if runtime.get('path'):
                referenced.add(str(Path(runtime['path']).resolve()))
        adapter_path=seat.get('adapter')
        if adapter_path and seat.get('adapter_sha256'):
            path=Path(adapter_path)
            try:
                _within(path,session_store.seat_dir(identifier))
                if path.exists() and delivery_runs.digest(path)==seat['adapter_sha256']:
                    runtime=seat.get('runtime',{})
                    if runtime.get('path'):
                        referenced.add(str(Path(runtime['path']).resolve()))
            except (OSError,TypeError,ValueError):
                return []
        for checkout_id, item in seat.get('checkouts', {}).items():
            state_path = _state_path(identifier, checkout_id)
            if item.get('state_file') != str(state_path):
                return []
            try:
                _within(state_path, session_store.seat_dir(identifier))
                state = json.loads(Path(state_path).read_text())
            except (OSError, ValueError, TypeError):
                return []
            if state.get('phase') not in ('active','installing','removed'):
                return []
            if state.get('phase') in ('active','installing'):
                for value in (state.get('source'),state.get('runtime',{}).get('path')):
                    if value and Path(value).resolve().is_relative_to(session_store.runtime_root().resolve()):
                        referenced.add(str(Path(value).resolve()))
    root = session_store.runtime_root()
    if not root.exists():
        return []
    _refuse_symlink(root)
    candidates = []
    for path in sorted(root.iterdir()):
        if len(candidates) >= MAX_RUNTIMES:
            break
        if str(path.resolve()) in referenced or not path.is_dir() or path.is_symlink():
            continue
        if len(path.name) != 64 or any(char not in '0123456789abcdef' for char in path.name):
            continue
        _within(path, root)
        if path.stat().st_mtime > cutoff:
            continue
        try:
            if session_store.runtime_plan(path)['version'] != path.name:
                continue
            if any(item.is_symlink() for item in path.rglob('*')):
                continue
        except (OSError, ValueError):
            continue
        candidates.append({'path': str(path), 'version': path.name, 'mtime': path.stat().st_mtime})
    return candidates


def preview(retention_days=None, *, limit=MAX_PR_RECORDS, seat_id=None):
    if retention_days is None:
        try:
            retention_days = int(os.environ.get('STARFLEET_POLICY_GUARD_RETENTION_DAYS', '30'))
        except ValueError:
            raise ValueError('STARFLEET_POLICY_GUARD_RETENTION_DAYS must be a positive whole number')
    if retention_days < 1 or retention_days > 3650:
        raise ValueError('Retention must be between 1 and 3650 days')
    now = time.time()
    cutoff = now - retention_days * 86400
    all_rows, uncertain = _load_seats()
    identity=lane_scope.native_identity()
    if seat_id:
        selected=next((row for row in all_rows if row[0]==seat_id),None)
        if not selected:
            raise ValueError('Permanent Policy Guard seat registry is missing')
        if selected[1].get('owner')!=identity:
            raise ValueError('Only the exact native seat owner may clean its local history')
        rows=[selected]
    else:
        rows=[row for row in all_rows if row[1].get('owner')==identity]
    pr_candidates = []
    checkout_candidates = []
    scanned_checkouts = 0
    for identifier, seat in rows:
        if seat.get('pinned') or seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal'):
            continue
        pins = seat.get('pinned_prs', {})
        refs = _pr_refs(identifier, seat)
        for url, record in sorted(seat.get('prs', {}).items()):
            if len(pr_candidates) >= limit:
                break
            terminal = record.get('terminal_observation')
            if (not terminal or terminal.get('state') != 'closed' or
                    not isinstance(terminal.get('observed_at'), (int, float)) or
                    terminal['observed_at'] > cutoff or url in pins or record.get('pinned')):
                continue
            links=set(record.get('checkout_ids', []))
            links.update(item['checkout_id'] for item in refs.get(url, []))
            changes = []
            blocked = False
            for link in sorted(links):
                checkout = seat.get('checkouts', {}).get(link)
                if not checkout:
                    blocked = True
                    break
                scanned_checkouts += 1
                if scanned_checkouts > MAX_CHECKOUTS:
                    blocked = True
                    break
                try:
                    state_record = _state_record(identifier, link, checkout)
                except (OSError, TypeError, ValueError):
                    blocked = True
                    break
                if not state_record:
                    continue
                state = state_record['value']
                if (state.get('pinned') or state.get('phase') == 'installing' or
                        state.get('recovery_journal') or state.get('upgrade_recovery') or
                        state.get('phase') not in ('active', 'removed')):
                    blocked = True
                    break
                if state.get('root') != checkout.get('root'):
                    blocked = True
                    break
                actual_refs=set(state.get('pr_bindings',{}))
                if state.get('pr_url'):
                    actual_refs.add(state['pr_url'])
                if url not in actual_refs:
                    blocked=True
                    break
                changes.append({'checkout_id': link, 'path': state_record['path'],
                                'sha256': state_record['sha256'], 'root': state.get('root')})
            if blocked:
                continue
            pr_candidates.append({'seat_id': identifier, 'pr_url': url,
                                  'terminal_observed_at': terminal['observed_at'],
                                  'checkout_changes': changes,
                                  'pr_record_sha256': _digest(json.dumps(record, sort_keys=True))})
        # Whole checkout deletion is restricted to removed, empty records with no evidence history.
        for checkout_id, checkout in sorted(seat.get('checkouts', {}).items()):
            if len(checkout_candidates) >= limit:
                break
            scanned_checkouts += 1
            if scanned_checkouts > MAX_CHECKOUTS:
                break
            try:
                state_record = _state_record(identifier, checkout_id, checkout)
            except (OSError, TypeError, ValueError):
                continue
            if not state_record:
                continue
            state = state_record['value']
            if (state.get('phase') != 'removed' or state.get('pinned') or state.get('pr_url') or
                    state.get('pr_bindings') or state.get('receipt') or state.get('history') or
                    state.get('installation_history') or state.get('recovery_journal')):
                continue
            removed_at = state.get('removed_at')
            if not isinstance(removed_at, (int, float)) or removed_at > cutoff:
                continue
            if any(item.name != 'state.json' for item in Path(state_record['path']).parent.iterdir()):
                continue
            checkout_candidates.append({'seat_id': identifier, 'checkout_id': checkout_id,
                'root': checkout['root'], 'path': state_record['path'],
                'sha256': state_record['sha256'], 'removed_at': removed_at})
    runtimes = _runtime_candidates(all_rows, uncertain, cutoff)
    plan = {'retention_days': retention_days, 'limit': limit, 'scanned_checkouts': scanned_checkouts,
            'terminal_prs': pr_candidates, 'empty_terminal_checkouts': checkout_candidates,
            'owner':identity,'seat_id':seat_id,
            'unreferenced_runtimes': runtimes,
            'runtime_cleanup_suspended_by_uncertain_seat': uncertain,
            'notice': 'Preview only. Cleanup removes owned local receipts and records only after an observed closed PR has aged past retention. External evidence and manual counts are never deleted.'}
    plan['plan_sha256'] = _digest(json.dumps(plan, sort_keys=True))
    return plan


def _history_path():
    return session_store.state_root() / 'cleanup-history.json'


def apply(plan):
    identity=lane_scope.native_identity()
    if plan.get('owner')!=identity:
        raise ValueError('Cleanup plan was reviewed by a different native owner; generate a fresh preview')
    history_path = _history_path()
    history = session_store._read_json(history_path) or {'applied_plans': []}
    if plan['plan_sha256'] in history.get('applied_plans', []):
        return {'removed': [], 'already_applied': True, 'plan_sha256': plan['plan_sha256']}
    removed_prs = []
    removed_checkouts = []
    removed_runtimes = []
    skipped = []
    for item in plan['terminal_prs']:
        identifier = item['seat_id']
        with session_store.locked_seat(identifier):
            seat = session_store.load_seat(identifier)
            if (not seat or seat.get('owner')!=identity or seat.get('pinned') or seat.get('install_journal') or
                    seat.get('migration_journal') or seat.get('handoff_journal') or
                    item['pr_url'] in seat.get('pinned_prs', {})):
                skipped.append(item['pr_url'])
                continue
            record = seat.get('prs', {}).get(item['pr_url'])
            if not record or _digest(json.dumps(record, sort_keys=True)) != item['pr_record_sha256']:
                skipped.append(item['pr_url'])
                continue
            current_changes = []
            changed = False
            for change in item['checkout_changes']:
                checkout = seat.get('checkouts', {}).get(change['checkout_id'])
                if not checkout:
                    continue
                path = Path(change['path'])
                _within(path, session_store.seat_dir(identifier))
                if not path.exists() or delivery_runs.digest(path) != change['sha256']:
                    changed = True
                    break
                state = json.loads(path.read_text())
                if state.get('phase') not in ('active', 'removed') or state.get('pinned') or state.get('root') != change['root']:
                    changed = True
                    break
                actual_refs=set(state.get('pr_bindings',{}))
                if state.get('pr_url'):
                    actual_refs.add(state['pr_url'])
                if item['pr_url'] not in actual_refs:
                    changed = True
                    break
                current_changes.append((path, state))
            if changed:
                skipped.append(item['pr_url'])
                continue
            for path, state in current_changes:
                _forget_pr_from_state(state,item['pr_url'])
                state['updated_at'] = time.time()
                session_store._atomic_write(path,(json.dumps(state,indent=2)+'\n').encode())
            seat['prs'].pop(item['pr_url'], None)
            # Rebuild exact checkout links for the records that remain.
            for url, remaining in list(seat.get('prs', {}).items()):
                links = []
                for checkout_id, checkout in seat.get('checkouts', {}).items():
                    try:
                        value = _state_record(identifier, checkout_id, checkout)
                    except (OSError, ValueError, TypeError):
                        continue
                    if value and (url in value['value'].get('pr_bindings', {}) or value['value'].get('pr_url') == url):
                        links.append(checkout_id)
                remaining['checkout_ids'] = links
            seat['updated_at'] = time.time()
            session_store.save_seat(seat)
            removed_prs.append(item['pr_url'])
    for item in plan['empty_terminal_checkouts']:
        identifier = item['seat_id']
        with session_store.locked_seat(identifier):
            seat = session_store.load_seat(identifier)
            if (not seat or seat.get('owner')!=identity or seat.get('pinned') or seat.get('install_journal') or
                    seat.get('migration_journal') or seat.get('handoff_journal')):
                skipped.append(item['root'])
                continue
            checkout = seat.get('checkouts', {}).get(item['checkout_id'])
            path = Path(item['path'])
            _within(path, session_store.seat_dir(identifier))
            if not checkout or not path.exists() or delivery_runs.digest(path) != item['sha256']:
                skipped.append(item['root'])
                continue
            state = json.loads(path.read_text())
            if (state.get('phase') != 'removed' or state.get('pr_url') or state.get('pr_bindings') or
                    state.get('receipt') or state.get('history') or state.get('installation_history') or
                    state.get('removed_at') != item['removed_at'] or any(
                        record.get('terminal_observation') is None and item['checkout_id'] in record.get('checkout_ids', [])
                        for record in seat.get('prs', {}).values())):
                skipped.append(item['root'])
                continue
            path.unlink()
            seat['checkouts'].pop(item['checkout_id'], None)
            for record in seat.get('prs', {}).values():
                record['checkout_ids'] = [value for value in record.get('checkout_ids', []) if value != item['checkout_id']]
            session_store.save_seat(seat)
            removed_checkouts.append(item['root'])
    for item in plan['unreferenced_runtimes']:
        path = Path(item['path'])
        root = session_store.runtime_root()
        try:
            with session_store.locked_runtime_catalog():
                _within(path, root)
                rows, uncertain = _load_seats()
                if _runtime_cleanup_suspended(rows,uncertain) or str(path.resolve()) in _runtime_references(rows):
                    skipped.append(str(path))
                    continue
                if not path.is_dir() or path.stat().st_mtime != item['mtime'] or any(p.is_symlink() for p in path.rglob('*')):
                    skipped.append(str(path))
                    continue
                for child in [path, *(value for value in path.rglob('*') if value.is_dir())]:
                    os.chmod(child, 0o700)
                shutil.rmtree(path)
                removed_runtimes.append(str(path))
        except (OSError, ValueError):
            skipped.append(str(path))
    history.setdefault('applied_plans', []).append(plan['plan_sha256'])
    history['applied_plans'] = history['applied_plans'][-100:]
    session_store._atomic_write(history_path, (json.dumps(history, indent=2) + '\n').encode())
    return {'removed_prs': removed_prs, 'removed_checkouts': removed_checkouts,
            'removed_runtimes': removed_runtimes, 'skipped': skipped,
            'plan_sha256': plan['plan_sha256'],
            'notice': 'Only user-owned XDG state/runtime data was removed. External packets, audits and manual counts remain untouched.'}


def apply_reviewed(retention_days, plan_sha256, seat_id=None):
    proposed = preview(retention_days,seat_id=seat_id)
    if proposed['plan_sha256'] != plan_sha256:
        history = session_store._read_json(_history_path()) or {'applied_plans': []}
        if plan_sha256 in history.get('applied_plans', []):
            return {'removed_prs': [], 'removed_checkouts': [], 'removed_runtimes': [],
                    'skipped': [], 'already_applied': True, 'plan_sha256': plan_sha256}
        raise ValueError('Cleanup candidates changed; review a fresh cleanup --preview before applying')
    return apply(proposed)


def _runtime_references(rows):
    refs = set()
    for identifier, seat in rows:
        runtime = seat.get('runtime', {})
        runtime_live=bool(seat.get('hooks_installed'))
        adapter=seat.get('adapter')
        if adapter and seat.get('adapter_sha256'):
            adapter_path=Path(adapter)
            _within(adapter_path,session_store.seat_dir(identifier))
            if adapter_path.exists() and delivery_runs.digest(adapter_path)==seat['adapter_sha256']:
                runtime_live=True
        if runtime_live and runtime.get('path'):
            refs.add(str(Path(runtime['path']).resolve()))
        for checkout_id, checkout in seat.get('checkouts', {}).items():
            path = _state_path(identifier, checkout_id)
            if checkout.get('state_file') != str(path):
                raise ValueError('Checkout state path differs from the owned seat registry')
            _within(path, session_store.seat_dir(identifier))
            try:
                state = json.loads(Path(path).read_text())
            except (OSError, ValueError, TypeError):
                raise ValueError('Checkout state is missing or malformed; retain all runtime versions')
            if state.get('phase') not in ('active','installing','removed'):
                raise ValueError('Checkout lifecycle is uncertain; retain all runtime versions')
            if state.get('phase') in ('active','installing'):
                for value in (state.get('source'), state.get('runtime', {}).get('path')):
                    if value and Path(value).resolve().is_relative_to(session_store.runtime_root().resolve()):
                        refs.add(str(Path(value).resolve()))
    return refs


def _forget_pr_from_state(state,pr_url):
    bindings=state.get('pr_bindings')
    if isinstance(bindings,dict):
        bindings.pop(pr_url,None)
        if not bindings:
            state.pop('pr_bindings',None)
    if state.get('pr_url')==pr_url:
        for key in lane_scope.BINDING_FIELDS:
            state.pop(key,None)
    history=state.get('installation_history')
    if isinstance(history,list):
        for previous in history:
            if not isinstance(previous,dict):
                continue
            old_bindings=previous.get('pr_bindings')
            if isinstance(old_bindings,dict):
                old_bindings.pop(pr_url,None)
                if not old_bindings:
                    previous.pop('pr_bindings',None)
            if previous.get('pr_url')==pr_url:
                for key in lane_scope.BINDING_FIELDS:
                    previous.pop(key,None)
