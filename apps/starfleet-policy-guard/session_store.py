"""Durable local storage for one native orchestrator seat and its checkouts.

Product Git directories are references only. Policy Guard state and runtime files
live under the user's XDG state and data directories.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

SCHEMA = 1
RUNTIME_FILES = (
    'lane.py', 'lane_scope.py', 'delivery.py', 'delivery_runs.py',
    'github_records.py', 'session_store.py', 'cleanup.py', 'guard.py', 'hook.sh',
    'config/delivery-checklist.json', 'config/default-policy.json',
    'examples/delivery-evidence.json', 'examples/delivery-design-audit.json',
    'guide.md', 'README.md',
)


def _canonical(path):
    return str(Path(path).expanduser().resolve())


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _refuse_symlinked_path(path):
    target = Path(os.path.abspath(path))
    current = target
    while True:
        if current.is_symlink():
            raise ValueError('Refusing symlinked Policy Guard state path: ' + str(target))
        if current.parent == current:
            break
        current = current.parent


def _private_mkdir(path):
    path = Path(path)
    _refuse_symlinked_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def state_home():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')).expanduser()


def data_home():
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')).expanduser()


def state_root():
    return state_home() / 'starfleet-policy-guard'


def runtime_root():
    return data_home() / 'starfleet-policy-guard' / 'runtimes'


def seat_id(seat_root):
    return _digest(('starfleet-policy-guard-seat-v1\n' + _canonical(seat_root)).encode())[:32]


def checkout_id(checkout_root):
    return _digest(('starfleet-policy-guard-checkout-v1\n' + _canonical(checkout_root)).encode())[:32]


def seat_dir(identifier):
    if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('Invalid Policy Guard seat identifier')
    return state_root() / 'seats' / identifier


def _atomic_write(path, data, mode=0o600):
    path = Path(path)
    _private_mkdir(path.parent)
    _refuse_symlinked_path(path)
    descriptor, temp = tempfile.mkstemp(prefix='.policy-guard-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read_json(path):
    path = Path(path)
    _refuse_symlinked_path(path)
    return json.loads(path.read_text()) if path.exists() else None


def load_seat(identifier):
    path = seat_dir(identifier) / 'seat.json'
    value = _read_json(path)
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
            value.get('seat_id') != identifier or not isinstance(value.get('seat_root'), str) or
            not isinstance(value.get('owner'), dict) or not isinstance(value.get('checkouts'), dict)):
        raise ValueError('Policy Guard seat registry is malformed; preserve it and use recover --inspect')
    if seat_id(value['seat_root']) != identifier:
        raise ValueError('Policy Guard seat path identity changed; preserve its registry and coordinate recovery')
    return value


def save_seat(value):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ValueError('Refusing unsupported Policy Guard seat registry')
    path = seat_dir(value['seat_id']) / 'seat.json'
    _atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + '\n').encode())


@contextlib.contextmanager
def locked_seat(identifier):
    location = seat_dir(identifier)
    _private_mkdir(location)
    with (location / 'seat.lock').open('a') as stream:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('Policy Guard seat is busy; retry at a safe boundary')
                time.sleep(0.02)
        yield


@contextlib.contextmanager
def locked_runtime_catalog():
    location = _private_mkdir(runtime_root())
    with (location / '.catalog.lock').open('a') as stream:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('Policy Guard runtime catalog is busy; retry cleanup or installation')
                time.sleep(0.02)
        yield


def find_seat_for_root(checkout_root):
    wanted = _canonical(checkout_root)
    seats = state_root() / 'seats'
    if not seats.exists():
        return None
    for path in sorted(seats.glob('*/seat.json')):
        try:
            manifest = _read_json(path)
            if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA:
                continue
            if seat_id(manifest.get('seat_root', '')) != manifest.get('seat_id'):
                continue
            for key, record in manifest.get('checkouts', {}).items():
                if record.get('root') == wanted:
                    checkout = seat_dir(manifest['seat_id']) / 'checkouts' / key
                    return {'seat': manifest, 'seat_dir': seat_dir(manifest['seat_id']),
                            'checkout_id': key, 'checkout_dir': checkout,
                            'checkout': record}
        except (OSError, TypeError, ValueError):
            # A damaged unrelated seat is retained and cannot hide valid peers.
            continue
    return None


def lookup_checkout(checkout_root):
    return find_seat_for_root(checkout_root)


def all_checkouts(identifier, include_state=True):
    manifest = load_seat(identifier)
    if not manifest:
        return []
    rows = []
    for key, record in manifest['checkouts'].items():
        path = seat_dir(identifier) / 'checkouts' / key
        item = dict(record, checkout_id=key, checkout_dir=str(path))
        if include_state:
            state = _read_json(path / 'state.json')
            if state is not None:
                item['state'] = state
        rows.append(item)
    return rows


def state_for_root(checkout_root):
    found = lookup_checkout(checkout_root)
    if not found:
        return None
    return _read_json(found['checkout_dir'] / 'state.json')


def write_checkout(identifier, checkout_root, state, *, repository, host, gitdir, common_gitdir):
    checkout_root = _canonical(checkout_root)
    key = checkout_id(checkout_root)
    location = seat_dir(identifier) / 'checkouts' / key
    _private_mkdir(location)
    _atomic_write(location / 'state.json', (json.dumps(state, indent=2) + '\n').encode())
    manifest = load_seat(identifier)
    if not manifest:
        raise ValueError('Policy Guard seat registry disappeared during checkout attachment')
    manifest['checkouts'][key] = {
        'root': checkout_root, 'repository': repository.casefold(), 'host': host,
        'gitdir': str(Path(gitdir).resolve()), 'common_gitdir': str(Path(common_gitdir).resolve()),
        'attached_at': manifest.get('checkouts', {}).get(key, {}).get('attached_at', time.time()),
        'updated_at': time.time(), 'state_file': str(location / 'state.json'),
    }
    sync_pr_links(manifest, key, state, repository=repository, host=host)
    save_seat(manifest)
    return location


def sync_pr_links(manifest, checkout_key, state, *, repository=None, host=None):
    records=manifest.setdefault('prs',{})
    urls=set(state.get('pr_bindings',{}))
    if state.get('pr_url'): urls.add(state['pr_url'])
    lane_owner=state.get('lane_owner',{})
    repository=repository or lane_owner.get('repository')
    host=host or lane_owner.get('host')
    for url in urls:
        if not isinstance(url,str): continue
        record=records.setdefault(url,{'url':url,'host':host,'repository':repository,'checkout_ids':[]})
        links=record.setdefault('checkout_ids',[])
        if checkout_key not in links: links.append(checkout_key)
        if record.get('host') is None: record['host']=host
        if record.get('repository') is None: record['repository']=repository


def runtime_plan(source_root):
    source_root = Path(source_root)
    rows = {}
    digest = hashlib.sha256()
    for relative in RUNTIME_FILES:
        path = source_root / relative
        data = path.read_bytes()
        fingerprint = _digest(data)
        rows[relative] = fingerprint
        digest.update(relative.encode() + b'\0' + bytes.fromhex(fingerprint))
    version = digest.hexdigest()
    return {'version': version, 'path': str(runtime_root() / version), 'files': rows}


def _runtime_entries(path):
    path = Path(path)
    directories = []
    files = []
    for item in path.rglob('*'):
        mode = item.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError('Installed Policy Guard runtime contains a symlink; preserve it and inspect its fingerprint')
        if stat.S_ISDIR(mode):
            directories.append(item)
        else:
            files.append(item)
    return directories, files


def _seal_runtime(path):
    path = Path(path)
    directories, files = _runtime_entries(path)
    for item in files:
        os.chmod(item, 0o444)
    for item in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        os.chmod(item, 0o555)
    os.chmod(path, 0o555)


def _require_sealed_runtime(path):
    path = Path(path)
    root_mode = path.lstat().st_mode
    if not stat.S_ISDIR(root_mode) or root_mode & 0o777 != 0o555:
        raise ValueError('Installed Policy Guard runtime permissions changed; preserve it and inspect its fingerprint')
    directories, files = _runtime_entries(path)
    for item in directories:
        if item.lstat().st_mode & 0o777 != 0o555:
            raise ValueError('Installed Policy Guard runtime permissions changed; preserve it and inspect its fingerprint')
    for item in files:
        if item.lstat().st_mode & 0o777 != 0o444:
            raise ValueError('Installed Policy Guard runtime permissions changed; preserve it and inspect its fingerprint')


def _remove_new_runtime(path, identity):
    path = Path(path)
    try:
        root = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(root.st_mode) or (root.st_dev, root.st_ino) != identity:
        return False
    for current, directories, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            item = Path(current) / name
            try:
                if stat.S_ISREG(item.lstat().st_mode):
                    os.chmod(item, 0o600)
            except OSError:
                pass
        for name in directories:
            item = Path(current) / name
            try:
                if stat.S_ISDIR(item.lstat().st_mode):
                    os.chmod(item, 0o700)
            except OSError:
                pass
    try:
        root = path.lstat()
        if not stat.S_ISDIR(root.st_mode) or (root.st_dev, root.st_ino) != identity:
            return False
        os.chmod(path, 0o700)
        shutil.rmtree(path)
    except OSError:
        return False
    return not os.path.lexists(path)


def _install_runtime_locked(source_root, plan=None):
    plan = plan or runtime_plan(source_root)
    target = Path(plan['path'])
    _refuse_symlinked_path(target)
    if target.exists():
        missing = []
        for relative, expected in plan['files'].items():
            path = target / relative
            _refuse_symlinked_path(path)
            if not path.exists():
                missing.append(relative)
                continue
            if _digest(path.read_bytes()) != expected:
                raise ValueError('Installed Policy Guard runtime version was modified; preserve it and inspect its fingerprint')
        if not missing:
            _require_sealed_runtime(target)
            return target
        directories = [target, *(path for path in target.rglob('*') if path.is_dir())]
        for directory in directories:
            os.chmod(directory, 0o700)
        try:
            for relative in missing:
                source = Path(source_root) / relative
                data = source.read_bytes()
                if _digest(data) != plan['files'][relative]:
                    raise ValueError('Recovery source does not match the pinned immutable runtime')
                destination = target / relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix='.runtime-repair-', dir=destination.parent)
                try:
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, 0o444)
                    os.replace(temporary, destination)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        finally:
            _seal_runtime(target)
        return target
    _private_mkdir(runtime_root())
    temporary = Path(tempfile.mkdtemp(prefix='.runtime-', dir=runtime_root()))
    try:
        for relative, expected in plan['files'].items():
            source = Path(source_root) / relative
            data = source.read_bytes()
            if _digest(data) != expected:
                raise ValueError('Policy Guard source changed after planning; review a fresh plan')
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            os.chmod(destination, 0o444)
        temporary_stat = temporary.stat()
        identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        try:
            os.replace(temporary, target)
        except OSError:
            if not target.exists():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
            return _install_runtime_locked(source_root, plan)
        try:
            _seal_runtime(target)
        except BaseException as error:
            if not _remove_new_runtime(target, identity) and isinstance(error, Exception):
                raise RuntimeError('New Policy Guard runtime could not be sealed or safely removed; preserve it and inspect its permissions') from error
            raise
    finally:
        if temporary.exists():
            for path in temporary.rglob('*'):
                try:
                    os.chmod(path, 0o700 if path.is_dir() else 0o600)
                except OSError:
                    pass
            try:
                os.chmod(temporary, 0o700)
                shutil.rmtree(temporary)
            except OSError:
                pass
    return target


def install_runtime(source_root, plan=None):
    with locked_runtime_catalog():
        return _install_runtime_locked(source_root, plan)


def seat_manifest(identifier):
    return load_seat(identifier)


def make_manifest(seat_root, owner, runtime):
    canonical = _canonical(seat_root)
    return {'schema': SCHEMA, 'seat_id': seat_id(canonical), 'seat_root': canonical,
            'seat_gitdir': None, 'owner': dict(owner), 'owner_history': [],
            'runtime': dict(runtime), 'created_at': time.time(), 'updated_at': time.time(),
            'settings': None, 'hooks_installed': False, 'groups': {}, 'checkouts': {},
            'pinned': False}
