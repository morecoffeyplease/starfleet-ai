#!/usr/bin/env python3
"""Scoped delivery-only adapter. Probes never forward readiness or merge actions."""

import argparse
from collections import deque
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import delivery
import delivery_runs
import github_records as github
import lane_scope
import session_store
import cleanup as seat_cleanup

APP = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()
UPGRADE_JOURNAL = 'upgrade-journal.json'


def root(cwd):
    return Path(delivery.value(cwd, 'rev-parse', '--show-toplevel')).resolve()


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


def source_versions(harness=None):
    names = ['lane.py', 'lane_scope.py', 'delivery.py', 'delivery_runs.py',
             'github_records.py', 'session_store.py', 'cleanup.py']
    if harness == 'opencode':
        names.extend(('opencode-plugin/index.ts', 'opencode-plugin/policy.js'))
    return {name: delivery_runs.digest(APP / name) for name in names}


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


def upgrade_journal_path(cwd):
    found = session_store.lookup_checkout(root(cwd))
    if found:
        return found['seat_dir'] / UPGRADE_JOURNAL
    return directory(cwd) / UPGRADE_JOURNAL


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


def require(state, cwd, event=None):
    if state and state.get('lane_owner'):
        lane_scope.check_owner(state,event=event)
        check_lane_scope(state, cwd)
    if not active(state, cwd):
        raise ValueError(enrollment_help(cwd))
    if state['source_versions'] != source_versions(state.get('lane_owner', {}).get('harness')):
        raise ValueError('Pinned delivery adapter source changed; restore the reviewed source')
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


def remote_pr(cwd, pr_url, base_ref, head_branch=None, *, observed=None):
    ref = github.reference(pr_url, 'pull')
    repo = github.origin_repo(delivery.value(cwd, 'remote', 'get-url', 'origin'), ref['host'])
    if repo.casefold() != ref['repo'].casefold():
        raise ValueError('Delivery PR does not match worktree origin')
    pr = observed if observed is not None else github.request(ref)
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
    if (state['phase']!='active' or state['root']!=str(root(cwd)) or
            owner['product_gitdir']!=delivery.value(cwd,'rev-parse','--absolute-git-dir') or
            owner.get('seat_root', state['session_root']) != str(root(state['session_root'])) or
            owner['seat_gitdir']!=delivery.value(state['session_root'],'rev-parse','--absolute-git-dir') or
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
            target_groups=copy.deepcopy(seat.get('groups',{}))
            target_hooks_installed=bool(seat.get('hooks_installed'))
            target_settings=copy.deepcopy(seat.get('settings'))
            settings_change=None
            if previous['harness']!=identity['harness']:
                settings_path=None
                current_raw=''
                if previous['harness']=='claude' and seat.get('hooks_installed'):
                    settings_path,current_raw,current_settings=settings(seat['seat_root'])
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
                    settings_path,current_raw,current_settings=settings(seat['seat_root'])
                    runtime_cli=Path(seat['runtime']['path'])/'lane.py'
                    target_groups=hook_groups(seat['seat_root'],seat['seat_root'],
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
            for item in session_store.all_checkouts(identifier):
                state=item.get('state')
                if not state:
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
            changed=len(state_changes)
            seat['updated_at']=time.time()
            session_store.save_seat(seat)
        return {'rebound':True,'owner':identity,'history_preserved':True,'report_required':True,
                'checkouts_rebound':changed,
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
    ensure_no_upgrade_journal(cwd)
    if not args.lane and delivery.value(owner, 'rev-parse', '--path-format=absolute', '--git-common-dir') != delivery.value(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir'):
        raise ValueError('Session root and product worktree must belong to the same repository')
    previous = read(cwd)
    reattach_same_root=False
    if previous and previous.get('phase') != 'removed':
        if (args.lane and previous.get('phase')=='active' and previous.get('seat_id')==session_store.seat_id(owner)
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
        seat_identifier = session_store.seat_id(owner)
        seat = session_store.seat_manifest(seat_identifier)
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
        boundary = 'Claude lane-local delivery hooks and gh adapter only; no Git/Codex/CPU hooks or fix-round enrollment'
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
        seat_preimage = copy.deepcopy(seat)
        new_seat = seat is None
        if seat:
            if seat['owner'] != identity:
                raise ValueError('This permanent seat belongs to another native session; use an explicitly authorized handoff')
            if identity['harness'] == 'opencode' and seat.get('owner_context') != owner_context:
                raise ValueError('This permanent seat belongs to a different OpenCode session API project or location; use an explicitly authorized handoff')
            if seat.get('runtime', {}).get('version') != proposed['runtime']['version']:
                raise ValueError('The seat runtime changed after planning; review a fresh source upgrade plan')
        else:
            seat = session_store.make_manifest(proposed['session_root'], identity, proposed['runtime'])
            seat['seat_gitdir'] = delivery.value(proposed['session_root'], 'rev-parse', '--absolute-git-dir')
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


def validate_upgrade_scope(state, cwd):
    product = root(cwd)
    if state.get('phase') != 'active' or state.get('root') != str(product) or state.get('branch') != branch(product):
        raise ValueError('Upgrade requires the active installed product root and branch; inspect status before planning')
    if not state.get('lane_owner') and not state.get('pr_url'):
        raise ValueError('No active per-PR or native-owner installation to upgrade')
    seat_path = state.get('session_root', state['root'])
    seat = root(seat_path)
    if str(seat) != seat_path:
        raise ValueError('Permanent owner seat path changed; preserve history and coordinate an explicit path repair')
    if not state.get('seat_id') and delivery.value(seat, 'rev-parse', '--path-format=absolute', '--git-common-dir') != delivery.value(
            product, 'rev-parse', '--path-format=absolute', '--git-common-dir'):
        raise ValueError('Session root and product worktree must belong to the same repository')
    if state.get('lane_owner'):
        lane_scope.check_owner(state)
        owner = state['lane_owner']
        if (owner.get('product_gitdir') != delivery.value(product, 'rev-parse', '--absolute-git-dir') or
                owner.get('seat_gitdir') != delivery.value(seat, 'rev-parse', '--absolute-git-dir') or
                github.origin_repo(delivery.value(product, 'remote', 'get-url', 'origin'), owner['host']).casefold() !=
                owner.get('repository', '').casefold()):
            raise ValueError('Enrolled product/seat path or repository changed; preserve registration history and coordinate an explicit path repair')
    return product, seat


def build_upgrade_plan(cwd):
    product = root(cwd)
    ensure_no_upgrade_journal(product)
    state_path = directory(product) / 'state.json'
    state_file = capture_text(state_path)
    if not state_file['exists']:
        raise ValueError('No active installation state to upgrade')
    state = json.loads(state_file['text'])
    product, seat = validate_upgrade_scope(state, product)
    if delivery.value(APP, 'status', '--porcelain'):
        raise ValueError('Commit the new installer source, then review a fresh upgrade plan')
    new_revision = delivery.value(APP, 'rev-parse', 'HEAD')
    new_versions = source_versions(state.get('lane_owner', {}).get('harness'))
    old_pin = source_pin_status(state)
    if old_pin['integrity'] != 'intact':
        raise ValueError('Installed source pin is ' + old_pin['integrity'] +
                         '; restore its exact files before planning a recoverable upgrade')
    runtime_plan = session_store.runtime_plan(APP) if state.get('seat_id') else None
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
    if manages_hooks:
        settings_path, settings_text, settings_value = settings(seat)
        if str(settings_path) != state.get('settings'):
            raise ValueError('Managed settings path changed; inspect status before upgrade')
        settings_before = capture_text(settings_path, state.get('settings_mode', 0o600))
        if settings_before['text'] != settings_text:
            raise ValueError('Claude settings changed during planning; retry at the coordinated single-writer checkpoint')
        if settings_before['mode'] != state.get('settings_mode', 0o600):
            raise ValueError('Claude settings mode changed since installation; coordinate the settings writer before upgrade')
        if old_groups:
            available_groups = (hook_groups(product,seat,seat_identifier=state['seat_id'],
                                runtime_cli=Path(runtime_plan['path'])/'lane.py') if runtime_plan
                                else hook_groups(product, seat))
            if not set(old_groups).issubset(available_groups):
                raise ValueError('Recorded managed hook events are unsupported by this source version')
            new_groups = {event: available_groups[event] for event in old_groups}
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
    new_state['phase'] = 'active'
    seat_manifest_before=None
    seat_manifest_after=None
    if runtime_plan:
        seat_manifest_before=session_store.load_seat(state['seat_id'])
        expected_owner={key:state['lane_owner'].get(key) for key in ('harness','session_id')}
        if (not seat_manifest_before or seat_manifest_before.get('owner')!=expected_owner or
                seat_manifest_before.get('runtime',{}).get('version')!=state.get('runtime',{}).get('version')):
            raise ValueError('Permanent seat runtime or owner changed; inspect status and review a fresh upgrade plan')
        seat_manifest_after=copy.deepcopy(seat_manifest_before)
        seat_manifest_after['runtime']={'version':runtime_plan['version'],'path':runtime_plan['path'],
            'files':runtime_plan['files'],'source_revision':'runtime:'+runtime_plan['version']}
        seat_manifest_after['adapter_sha256']=new_adapter_hash
        new_state['runtime']={'version':runtime_plan['version'],'path':runtime_plan['path'],
                              'files':runtime_plan['files']}
    installing_state = copy.deepcopy(new_state)
    installing_state['phase'] = 'installing'
    active_state_text = json.dumps(new_state, indent=2) + '\n'
    installing_state_text = json.dumps(installing_state, indent=2) + '\n'
    checkout_states=[{'path':str(state_path),'before':state_file,
        'installing_text':installing_state_text,'active_text':active_state_text}]
    if runtime_plan:
        checkout_states=[]
        active_rows=[]
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
            if not product_path.is_dir() or root(product_path)!=product_path.resolve() or branch(product_path)!=prior.get('branch'):
                raise ValueError('An active seat checkout is unavailable or on another branch; preserve it and resolve that checkout before upgrading the seat')
            lane_scope.check_owner(prior)
            check_lane_identity(prior,product_path)
            if prior.get('runtime',{}).get('version')!=state.get('runtime',{}).get('version'):
                raise ValueError('Active seat checkouts do not share one runtime version; preserve history and inspect the seat')
            if source_pin_status(prior)['integrity']!='intact':
                raise ValueError('An active seat checkout source pin is unavailable; preserve it and recover before upgrade')
            next_state=upgraded_state(prior,new_source,new_groups,new_adapter_hash)
            next_state['phase']='active'
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
            active_rows.append(next_state)
        current_row=next((row for row in checkout_states if row['path']==str(state_path)),None)
        if current_row is None:
            raise ValueError('The selected checkout is not an active attachment in its permanent seat')
        new_state=json.loads(current_row['active_text'])
        installing_state_text=current_row['installing_text']
        active_state_text=current_row['active_text']
        bindings=[entry for candidate in active_rows for entry in upgrade_binding_summary(candidate)]
        if not bindings:
            bindings=[]
        seat_manifest_after['groups']=copy.deepcopy(new_groups)
    state_after_hash = text_digest(active_state_text)
    state_installing_hash = text_digest(installing_state_text)
    settings_after_hash = (text_digest(settings_after_text) if settings_after_text is not None else None)

    if not runtime_plan:
        bindings = upgrade_binding_summary(state)
    review = {
        'protocol': 'policy-guard-source-upgrade-v1',
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
                'sha256':text_digest(json.dumps(seat_manifest_before,indent=2,sort_keys=True)+'\n')
                    if seat_manifest_before else None}},
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
                    if seat_manifest_after else None}},
        'recovery': {'journal_path': str(upgrade_journal_path(product)),
                     'command': upgrade_recovery_command(product),
                     'strategy': 'Keep the journal guard until exact owned preimages are restored.'}}
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
               'settings_managed': bool(manages_hooks), 'old_groups': copy.deepcopy(old_groups),
               'new_groups': copy.deepcopy(new_groups), 'review': review}
    return review, payload


def upgrade_plan(args):
    review, _ = build_upgrade_plan(args.cwd)
    return review


def upgrade_journal(payload):
    settings_before = payload['settings_before']
    state_before = payload['state_before']
    adapter_before = payload['adapter_before']
    return {'version': 1, 'protocol': 'policy-guard-source-upgrade-v1',
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
                    if payload.get('seat_manifest_after') else None)}}


def upgrade(args):
    review, payload = build_upgrade_plan(args.cwd)
    if args.plan_sha256 != review['plan_sha256']:
        raise ValueError('Upgrade plan or a bound preimage changed; inspect a fresh upgrade-plan')
    product = Path(payload['root'])
    with file_locked(product):
        current_review, payload = build_upgrade_plan(product)
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
    with file_locked(product):
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
        if journal.get('version') != 1 or journal.get('protocol') != 'policy-guard-source-upgrade-v1':
            raise ValueError('Upgrade journal version is unsupported; leave the adapter guarded for owner review')
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
        if settings_info.get('managed'):
            validate_journal_preimage(settings_info.get('preimage'), original_state.get('settings'))
            if settings_info.get('replacement_mode') != settings_info['preimage'].get('mode'):
                raise ValueError('Upgrade journal settings modes are inconsistent; leave the adapter guarded for owner review')
            if settings_info['preimage']['path'] != original_state.get('settings'):
                raise ValueError('Upgrade journal settings path differs from the recorded owner settings')
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
            session_store.save_seat(seat_manifest_restore)
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
        receipt = delivery_runs.run(args.cwd, state['base_ref'], state['pr_url'], args.evidence, args.policy)
        if state.get('receipt'):
            state.setdefault('history', []).append(state['receipt'])
        state['receipt'] = receipt
    return delivery.display(receipt['report'], args.format)


def enrollment_help(cwd):
    if (read(cwd) or {}).get('lane_owner'):
        return 'Lane is enrolled; run lane.py --cwd '+str(root(cwd))+' discover from its owning native session. Preserve old PR bindings and reports; do not reinstall per PR.'
    return ('No active delivery-only installation for this worktree and branch. Coordinate with the lane owner, then run ' +
            shlex.join([sys.executable, str(SELF), '--cwd', str(root(cwd)), '--session-root', '<permanent-owner-seat>',
                        'plan', '--pr-url', '<actual-open-PR-URL>', '--base-ref', '<actual-local-target-ref>']) +
            '. Review that plan and use install with its --plan-sha256. Do not use guard.py enroll or install --fixture '
            'for delivery-only enrollment. Existing readiness holds still apply.')


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


def status(cwd, session_id=None, state=None):
    state = read(cwd) if state is None else state
    configured = active(state, cwd)
    lane_enrolled=bool(state and state.get('lane_owner') and state.get('phase')=='active')
    installed_source = source_pin_status(state or {})
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
                require(state, cwd)
                receipt_status, receipt_reason = 'valid-local-snapshot', 'Current local candidate, target and inputs match'
            except Exception as error:
                message = str(error)
                if 'belongs to another native session' in message or 'actual native' in message:
                    receipt_status = 'not-evaluated'
                else:
                    receipt_status = 'stale-or-unusable'
                receipt_reason = message
    owner = (state or {}).get('lane_owner')
    if not owner:
        current_owner = {'state': 'legacy-per-pr-no-native-owner', 'mismatch': None}
    else:
        try:
            actual_owner = lane_scope.native_identity()
            mismatch = actual_owner != {key: owner.get(key) for key in ('harness', 'session_id')}
            if actual_owner.get('harness')=='opencode':
                mismatch = mismatch or lane_scope.native_session_context() != {
                    key: owner.get(key) for key in ('session_parent_id','session_project_id','session_directory')}
            current_owner = {'state': 'mismatch' if mismatch else 'matches', 'mismatch': mismatch,
                             'expected': {key: owner.get(key) for key in ('harness', 'session_id')},
                             'expected_context': {key: owner.get(key) for key in
                                                  ('session_parent_id','session_project_id','session_directory')
                                                  if key in owner},
                             'actual': actual_owner,
                             'actual_context': lane_scope.native_session_context()}
        except ValueError as error:
            current_owner = {'state': 'unavailable', 'mismatch': None, 'reason': str(error),
                             'expected': {key: owner.get(key) for key in ('harness', 'session_id')}}
    binding_type = ('native-owner-lane' if owner else 'legacy-per-pr' if state else None)
    successor_guidance = None
    if state and owner:
        successor_guidance = ('Native-owner lane: preserve branch aliases. A new native owner needs an explicitly authorized '
                              'rebind-owner; bindings are not auto-discovered during source upgrade.')
    elif state:
        successor_guidance = ('Legacy per-PR binding: preserve its explicit local/published branch mapping. Do not use '
                              'rebind-owner; a separate PR or checkout requires its own authorized enrollment.')
    upgrade_state = {'state': 'in-progress' if journal_pending else 'none',
                     'journal_path': str(journal_path) if journal_pending else None,
                     'recovery_command': upgrade_recovery_command(cwd) if journal_pending else None}
    return {'lane_enrolled':lane_enrolled,'lane_owner':(state or {}).get('lane_owner'),
            'owner_history':(state or {}).get('owner_history',[]),'installed_here': configured, 'phase': (state or {}).get('phase'),
            'binding_type': binding_type, 'current_owner': current_owner,
            'executing_cli': executing_cli, 'installed_source': installed_source,
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
            'receipt': (state or {}).get('receipt'), 'history': (state or {}).get('history', []),
            'enrollment_help': None if configured else enrollment_help(cwd)}


def coverage(cwd, pr_url=None):
    """Describe local evidence without discovery, enrollment or synthetic probes."""
    state = read(cwd)
    try:
        identity = lane_scope.native_identity()
        identity_error = None
    except ValueError as error:
        identity, identity_error = None, str(error)
    detail = status(cwd, identity['session_id'] if identity else None, state=state)
    expected_pr = github.reference(pr_url, 'pull')['url'] if pr_url else None
    bound_pr = (state or {}).get('pr_url')
    if not state:
        binding = 'not-installed'
    elif not bound_pr:
        binding = 'lane-enrolled-no-pr-binding'
    elif expected_pr and expected_pr.casefold() != bound_pr.casefold():
        binding = 'different-pr-bound'
    elif not detail['installed_here']:
        binding = 'inactive-local-binding'
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
    owner_confirmed = bool(owner and identity and all(owner.get(k) == identity[k] for k in ('harness', 'session_id')))
    return {
        'product_root': str(root(cwd)), 'branch': branch(cwd),
        'session_root': (state or {}).get('session_root'),
        'native_session': {'actual': identity, 'unavailable_reason': identity_error,
                           'matches_installed_owner': owner_confirmed if owner else None},
        'binding': {'state': binding, 'declared_current_pr': expected_pr, 'installed_pr': bound_pr,
                    'type': detail['binding_type'], 'hosted_pr_checked': False},
        'installed_source': detail['installed_source'], 'executing_cli': detail['executing_cli'],
        'source_intact': (detail['activation']['source_intact'] if state else None),
        'entrypoints': {'private_gh_adapter': (state or {}).get('adapter'),
                        'settings': (state or {}).get('settings') if (state or {}).get('manages_claude_hooks', bool(state)) else None,
                        'configured_hook_groups': (state or {}).get('groups', {})},
        'delivery_report': {'receipt': detail['report_status'], 'observations': observations},
        'fix_rounds': {'state': 'not-installed-by-delivery-lane',
                       'paths': {name: 'unverified' for name in (
                           'direct_edit', 'shell_write', 'scripted_codex_dispatch', 'native_agent_dispatch',
                           'push', 'restart', 'compaction', 'unavailable_state')},
                       'notice': 'This command does not inspect the separate fixture-only fix-round installation.'},
        'upgrade': detail['upgrade'],
        'notice': 'Read-only local inspection. No hosted discovery, tests, probes, installation or history changes. '
                  'The declared PR is caller input. Preserve manual zoom-out reminders and repository delivery rules.'}


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
    adapter_state='missing'
    if seat.get('adapter'):
        adapter=Path(seat['adapter'])
        try:
            refuse_symlinked_path(adapter)
            if adapter.exists():
                adapter_state='intact' if delivery_runs.digest(adapter)==seat.get('adapter_sha256') else 'changed'
        except (OSError,ValueError):
            adapter_state='unavailable'
    checkouts=[]
    for item in session_store.all_checkouts(identifier):
        state=item.get('state') or {}
        checkouts.append({'checkout_id':item['checkout_id'],'root':item.get('root'),
            'repository':item.get('repository'),'host':item.get('host'),
            'phase':state.get('phase','missing-or-malformed-state'),'branch':state.get('branch'),
            'current_pr':state.get('pr_url'),'known_prs':sorted(set(state.get('pr_bindings',{})) |
                ({state['pr_url']} if state.get('pr_url') else set())),
            'last_updated_at':state.get('updated_at')})
    return {'seat_id':identifier,'seat_root':seat['seat_root'],'owner':seat['owner'],
        'owner_context':seat.get('owner_context',{}),'actual_native_identity':actual,
        'actual_session_context':actual_context,'owner_matches':owner_matches,'identity_error':identity_error,
        'hooks_installed':bool(seat.get('hooks_installed')),'adapter':seat.get('adapter'),
        'adapter_state':adapter_state,'runtime':{'version':runtime.get('version'),
            'path':str(runtime_path),'files':runtime_files},'checkouts':checkouts,
        'pr_records':len(seat.get('prs',{})),'pending_transition':bool(seat.get('install_journal') or
            seat.get('migration_journal') or seat.get('handoff_journal') or
            (session_store.seat_dir(identifier)/UPGRADE_JOURNAL).exists() or
            (session_store.seat_dir(identifier)/UPGRADE_JOURNAL).is_symlink()),
        'notice':'Read-only permanent-seat inspection. It remains available without product Git directories and does not prove native hook loading or delivery interception.'}


def coverage_summary(cwd, detail):
    """Render existing observations without adding checks or changing state."""
    binding = detail['binding']
    native = detail['native_session']
    source = detail['installed_source']
    report = detail['delivery_report']['receipt']
    installed = binding['state'] != 'not-installed'
    owner = ('unavailable' if native['matches_installed_owner'] is not None and native['actual'] is None else
             'matches' if native['matches_installed_owner'] is True else
             'mismatch' if native['matches_installed_owner'] is False else 'unconfirmed')
    installed_cli = Path(source['path']) / 'lane.py' if source.get('path') else SELF
    lane_command = [sys.executable, str(installed_cli), '--cwd', str(root(cwd))]
    standalone = [sys.executable, str(APP / 'guard.py'), '--cwd', str(root(cwd)),
                  'delivery', 'report', '--help']
    next_action = 'Continue under repository checks and existing human approvals.'
    if detail['upgrade']['state'] == 'in-progress':
        next_action = 'Recover the interrupted upgrade: ' + detail['upgrade']['recovery_command']
    elif installed and detail['source_intact'] is not True:
        next_action = 'Ask the installation owner to inspect source integrity; do not reinstall or reset history.'
    elif owner in ('mismatch', 'unavailable'):
        next_action = 'Confirm the owning native session before using this binding; no automatic handoff.'
    elif binding['state'] in ('different-pr-bound', 'inactive-local-binding'):
        next_action = 'Confirm the product branch and PR with the installation owner; preserve the existing binding.'
    elif not installed:
        next_action = 'Standalone reporting needs no enrollment. Start with: ' + shlex.join(standalone)
    elif binding['state'] == 'lane-enrolled-no-pr-binding':
        next_action = 'Once a real PR exists, discover it: ' + shlex.join(lane_command + ['discover'])
    elif report['state'] == 'not-evaluated':
        next_action = 'Inspect with the installed CLI from its owning session: ' + shlex.join(
            [sys.executable, str(installed_cli), '--cwd', str(root(cwd)), 'status'])
    elif report['state'] != 'valid-local-snapshot':
        next_action = 'Record a report from existing evidence before covered delivery; see: ' + shlex.join(
            lane_command + ['record', '--help'])
    observations = detail['delivery_report']['observations']
    return '\n'.join([
        'Policy Guard: ' + ('delivery-only installation' if installed else 'not installed here'),
        'Branch: ' + detail['branch'],
        'PR: declared=' + (binding['declared_current_pr'] or 'unknown') + '; bound=' +
        (binding['installed_pr'] or 'none') + ' (' + binding['state'] + '; hosted identity unchecked)',
        'Owner: ' + owner,
        'Installed source: ' + (str(source.get('revision') or 'unknown') + ' (' + source['integrity'] + ')'
                                if installed else 'none'),
        'Observed paths for current session: native hook=' + observations['native_hook']['state'] +
        '; adapter=' + observations['explicit_adapter']['state'],
        'Report: ' + report['state'] + ' - ' + report['reason'],
        'Fix-round enforcement: not installed by the delivery lane; other paths unverified.',
        'Next: ' + next_action,
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
            ref, remote = remote_pr(product, state['pr_url'], state['base_ref'], state.get('head_branch'),
                                    observed=observation.get('_remote'))
            github.check_published_snapshot(ref, remote, report['snapshot'])
    except Exception as error:
        error.delivery_state = state
        raise
    return state, argv, True, probe


def seat_gh(args):
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
    else:
        state, forwarded, checked = None, argv, False
    if checked:
        trace(state['root'], {'hook_event_name': 'Adapter', 'session_id':
                              lane_scope.native_identity()['session_id']}, action, probe, 'pass', '')
    if probe:
        if not checked:
            raise ValueError('Probe did not exercise an attached readiness/merge gate')
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
    if captured and captured_binary_identity(real_gh) != captured:
        raise ValueError('Captured real GitHub CLI changed since seat enrollment; inspect status and recover the runtime')
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
            ref, remote = remote_pr(product, state['pr_url'], state['base_ref'], state.get('head_branch'))
            github.check_published_snapshot(ref, remote, report['snapshot'])
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
        if seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal'):
            raise ValueError('This Policy Guard seat has an interrupted state transition; use recover --inspect before starting covered work')
        if (event.get('hook_event_name') != 'SessionStart' or
                root(event.get('cwd', args.cwd)) != root(seat['seat_root'])):
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
    for event in set(remove_groups) | set(add_groups):
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
    if not active(legacy, product):
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
                    adapter=str(session_store.seat_dir(identifier) / 'bin/gh'),
                    adapter_sha256=hashlib.sha256(adapter_script(product, seat_identifier=identifier,
                        runtime_cli=runtime_cli,real_gh=real_gh['path']).encode()).hexdigest(), real_gh=real_gh['path'],
                    real_gh_identity=real_gh, manages_claude_hooks=manages_hooks,
                    groups=new_groups, settings=target_settings if manages_hooks else None,
                    settings_sha256=hashlib.sha256((next((item['updated'] for item in changes
                        if item['path']==target_settings), '')).encode()).hexdigest(), phase='installing')
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
        journal={'kind':'migration','legacy_state_path':proposed['legacy_state_path'],
                 'legacy_state':proposed['legacy_state_text'],
                 'legacy_state_sha256':proposed['legacy_state_sha256'],
                 'legacy_adapter':proposed['legacy_adapter'],
                 'legacy_adapter_text':proposed['legacy_adapter_text'],
                 'legacy_adapter_sha256':proposed['legacy_adapter_sha256'],
                 'legacy_adapter_mode':Path(proposed['legacy_adapter']).stat().st_mode & 0o777
                    if proposed['legacy_adapter_text'] is not None else None,
                 'settings_changes':proposed['settings_changes'],
                 'seat_preimage':seat,'new_state_path':str(session_store.seat_dir(identifier)/'checkouts'/
                    session_store.checkout_id(proposed['migrated_state']['root'])/'state.json'),
                 'new_adapter_path':proposed['migrated_state']['adapter'],
                 'new_adapter_sha256':proposed['migrated_state']['adapter_sha256'],
                 'created_at':time.time()}
        if seat is None:
            seat=session_store.make_manifest(proposed['seat_root'],proposed['owner'],proposed['runtime'])
            seat['seat_gitdir']=delivery.value(proposed['seat_root'],'rev-parse','--absolute-git-dir')
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
        adapter = Path(seat.get('adapter', ''))
        adapter_state = ('missing' if not adapter.exists() else
                         'intact' if delivery_runs.digest(adapter) == seat.get('adapter_sha256') else 'changed')
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
        return {'seat_id': identifier, 'seat_root': seat['seat_root'], 'owner': seat['owner'],
                'actual_native_identity': actual, 'owner_matches': owner_matches,
                'identity_error': identity_error, 'runtime': {'path': str(runtime_path),
                'version': runtime.get('version'), 'files': files}, 'adapter': {'path': str(adapter), 'state': adapter_state},
                'settings': {'path': settings_path, 'state': settings_state},
                'install_journal': seat.get('install_journal'), 'migration_journal':seat.get('migration_journal'),
                'handoff_journal':seat.get('handoff_journal'),
                'checkouts': session_store.all_checkouts(identifier),
                'notice': 'Read-only local recovery inspection. It does not recreate receipts, checklists, manual counts or activation claims.'}
    state = read(args.cwd)
    if not state:
        raise ValueError('No local or seat registration exists for this checkout')
    return {'legacy_state': state, 'source_integrity': source_pin_status(state),
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
    if lane_scope.native_identity() != seat.get('owner'):
        raise ValueError('Only the exact current native seat owner may plan recovery')
    if seat.get('install_journal') or seat.get('migration_journal') or seat.get('handoff_journal'):
        raise ValueError('An interrupted seat state transition needs recover --rollback before repair planning')
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
    plan = {'seat_id': identifier, 'actions': actions, 'runtime_version': runtime.get('version')}
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
        if any(action['kind'] == 'restore-runtime' for action in proposed['actions']):
            session_store.install_runtime(APP, session_store.runtime_plan(APP))
        if any(action['kind'] == 'restore-adapter' for action in proposed['actions']):
            runtime_cli = Path(seat['runtime']['path']) / 'lane.py'
            write(seat['adapter'], adapter_script(seat['seat_root'], seat_identifier=identifier,
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


def recover_rollback(args):
    identifier=args.seat_id
    if not identifier:
        state=read(args.cwd)
        identifier=(state or {}).get('seat_id')
    if not identifier:
        raise ValueError('No durable seat journal is associated with this checkout')
    with session_store.locked_seat(identifier):
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
                expected_settings=Path(handoff['seat_preimage']['seat_root'])/'.claude/settings.local.json'
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
    parser.add_argument('--seat-id', help='Permanent orchestrator seat for operations that can run without a product checkout')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('plan', 'install'):
        help_text = ('Review a per-PR or native-owner delivery installation' if action == 'plan' else
                     'Apply a reviewed per-PR installation or native-owner checkout attachment')
        p = sub.add_parser(action, help=help_text)
        p.add_argument('--pr-url')
        p.add_argument('--base-ref')
        p.add_argument('--lane', action='store_true', help='Enroll this product and native owner, optionally binding an explicit PR')
        p.add_argument('--owner-harness',choices=('claude','codex','opencode'))
        p.add_argument('--owner-session')
        p.add_argument('--host',default='github.com')
        p.add_argument('--head-branch', help='Explicit published head-branch mapping when different from the local branch')
        if action == 'install':
            p.add_argument('--plan-sha256', required=True)
    sub.add_parser('upgrade-plan', help='Read-only plan to replace an active installation source pin')
    p = sub.add_parser('upgrade', help='Apply a reviewed active-installation source upgrade')
    p.add_argument('--plan-sha256', required=True)
    p = sub.add_parser('upgrade-recover', help='Restore exact preimages after an interrupted source upgrade')
    p.add_argument('--rollback', action='store_true', required=True,
                   help='Explicitly restore the previous source pin and owned files')
    sub.add_parser('discover',help='Read the published PR for this enrolled product branch and preserve per-PR report history')
    p = sub.add_parser('rebind-owner',help='Explicitly authorized native-session handoff; no implicit inheritance')
    p.add_argument('--from-harness',choices=('claude','codex','opencode'),required=True)
    p.add_argument('--from-session',required=True)
    p.add_argument('--confirm-handoff',action='store_true',required=True)
    p = sub.add_parser('status', help='Inspect local binding, owner, report freshness and activation observations')
    p.add_argument('--session-id', help='Actual orchestrator session ID; never inferred from config presence')
    p = sub.add_parser('coverage', help='Read-only binding, owner and observed-path coverage; no discovery or probes')
    p.add_argument('--summary', action='store_true', help='Concise text and next action; default remains detailed JSON')
    p.add_argument('--pr-url', help='Caller-declared current PR; hosted state is not checked')
    sub.add_parser('uninstall', help='Remove or deactivate an installation without deleting its delivery history')
    sub.add_parser('registrations', help='Read known local PR bindings from an attached checkout or permanent seat')
    sub.add_parser('hook')
    sub.add_parser('session')
    p = sub.add_parser('record')
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--policy', type=Path, default=delivery.POLICY)
    p.add_argument('--format', choices=('json', 'text'), default='text')
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
    p = sub.add_parser('migrate-plan', help='Review an existing Gitdir installation for durable seat migration')
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
    return parser


def dispatch(args):
    try:
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
        identity=lane_scope.native_identity(event=event)
        tool=event.get('tool')
        if tool not in ('prime_directive_manage','prime_directive_gh'):
            raise ValueError('OpenCode bridge is available only to the native Prime Directive tools')
        nested=build_parser().parse_args(request['argv'])
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
        with lane_scope.native_event_scope(event):
            if nested.action=='gh':
                nested.seat_id=selected_seat_id(nested)
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
