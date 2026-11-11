"""Session-scoped lane ownership and per-PR delivery bindings.

These declarations coordinate cooperating agents, not hostile local processes.
No hooks, GitHub records, fix-round counts or readiness approvals are changed.
"""
import copy
import contextlib
import contextvars
import os
import re
import subprocess
import json
from pathlib import Path
from urllib.parse import urlencode

BINDING_FIELDS = ('branch', 'head_branch', 'pr_url', 'base_ref', 'receipt', 'history',
                  'guidance_attestation_ref')
OPENCODE_EVENT_SOURCE = 'opencode-server-plugin-v2'
_NATIVE_EVENT = contextvars.ContextVar('policy_guard_native_event', default=None)


def _opencode_identity(event):
    if not isinstance(event, dict) or event.get('native_source') != OPENCODE_EVENT_SOURCE:
        raise ValueError('An actual native OpenCode server tool context is required; environment session IDs do not enroll OpenCode')
    context = event.get('tool_context')
    session = event.get('session')
    tool = event.get('tool')
    if (not isinstance(context, dict) or not isinstance(session, dict) or
            tool not in ('prime_directive_manage', 'prime_directive_gh')):
        raise ValueError('OpenCode identity requires the supported Prime Directive tool context and session API result')
    session_id = context.get('sessionID')
    parent_id = session.get('parentID')
    valid_id = lambda value: isinstance(value, str) and re.fullmatch(r'ses_[A-Za-z0-9]+', value)
    if (not valid_id(session_id) or session.get('id') != session_id or
            (parent_id is not None and (not valid_id(parent_id) or parent_id == session_id))):
        raise ValueError('OpenCode session API identity does not match the actual tool session')
    for key in ('agent', 'messageID', 'id'):
        value = context.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value):
            raise ValueError('OpenCode tool identity is incomplete; use the native Prime Directive server tool')
    location = session.get('location')
    if (not isinstance(location, dict) or not isinstance(location.get('directory'), str) or
            not Path(location['directory']).is_absolute() or
            not isinstance(session.get('projectID'), str) or not session['projectID']):
        raise ValueError('OpenCode session API result has no project identity and absolute workspace directory')
    return {'harness': 'opencode', 'session_id': session_id}


def current_native_event():
    return _NATIVE_EVENT.get()


def native_session_context(event=None):
    event = current_native_event() if event is None else event
    if isinstance(event, dict) and event.get('native_source') == OPENCODE_EVENT_SOURCE:
        _opencode_identity(event)
        return {'session_parent_id': event['session'].get('parentID'),
                'session_project_id': event['session']['projectID'],
                'session_directory': event['session']['location']['directory']}
    return {}


@contextlib.contextmanager
def native_event_scope(event):
    identity = _opencode_identity(event)
    if identity['harness'] != 'opencode':
        raise ValueError('Only native OpenCode tool events may enter the OpenCode CLI bridge')
    token = _NATIVE_EVENT.set(event)
    try:
        yield identity
    finally:
        _NATIVE_EVENT.reset(token)


def native_identity(env=None, event=None):
    if event is None:
        event = current_native_event()
    if isinstance(event, dict) and event.get('native_source') == OPENCODE_EVENT_SOURCE:
        return _opencode_identity(event)
    env = os.environ if env is None else env
    keys = {'codex': 'CODEX_THREAD_ID', 'claude': 'CLAUDE_CODE_SESSION_ID'}
    selected = env.get('CMUX_AGENT_LAUNCH_KIND')
    candidates = [(kind, env[key]) for kind, key in keys.items()
                  if env.get(key) and (not selected or selected == kind)]
    if event and event.get('session_id'):
        harness = 'claude' if event.get('tool_name') == 'Bash' or event.get('hook_event_name') == 'SessionStart' else 'codex'
        # Native hook identity is authoritative over inherited shell variables.
        candidates = [(harness, event['session_id'])]
    if len(candidates) != 1 or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', candidates[0][1]):
        raise ValueError('One actual native Claude or Codex session identity is required; do not invent or override session IDs')
    return {'harness': candidates[0][0], 'session_id': candidates[0][1]}


def check_owner(state, *, env=None, event=None):
    owner = state.get('lane_owner')
    if owner:
        actual = native_identity(env, event)
        expected = {k: owner[k] for k in ('harness', 'session_id')}
        if actual != expected:
            raise ValueError('This lane belongs to another native session. Use an explicitly authorized rebind-owner handoff; implementers do not inherit lane ownership')
        if actual['harness'] == 'opencode' and native_session_context(event) != {
                key: owner.get(key) for key in ('session_parent_id', 'session_project_id', 'session_directory')}:
            raise ValueError('This lane belongs to a different OpenCode session API project or location; use an explicitly authorized handoff')


def expand(state):
    if not state.get('lane_owner'):
        return [state]
    bindings = copy.deepcopy(state.get('pr_bindings', {}))
    if state.get('pr_url'):
        bindings[state['pr_url']] = {k: copy.deepcopy(state[k]) for k in BINDING_FIELDS if k in state}
    shared={key:value for key,value in state.items() if key not in BINDING_FIELDS}
    return [dict(shared, **binding) for binding in bindings.values()]


def select_binding(state, binding):
    """Archive the old PR before selecting another; never copy proof across PRs."""
    bindings = state.setdefault('pr_bindings', {})
    if state.get('pr_url'):
        bindings[state['pr_url']] = {k: copy.deepcopy(state[k]) for k in BINDING_FIELDS if k in state}
    saved = bindings.get(binding['pr_url'], {})
    for key in BINDING_FIELDS:
        state.pop(key, None)
    state.update(copy.deepcopy(saved), **binding)
    state.setdefault('history', [])
    bindings[binding['pr_url']] = {k: copy.deepcopy(state[k]) for k in BINDING_FIELDS if k in state}


def published_branch(state, branch):
    """Reuse only this local branch's explicitly recorded published mapping."""
    if state.get('branch') == branch and state.get('pr_url'):
        return state.get('head_branch', branch)
    heads = {row.get('head_branch', branch) for row in state.get('pr_bindings', {}).values()
             if row.get('branch') == branch}
    if len(heads) > 1:
        raise ValueError('Conflicting published branch mappings; preserve history and inspect the explicit PR bindings')
    return next(iter(heads)) if heads else branch


def published_pr(state, branch):
    """Read exactly one open same-repository branch binding, without mutation."""
    repo, host = state['lane_owner']['repository'], state['lane_owner']['host']
    head = published_branch(state, branch)
    query = urlencode({'state': 'open', 'head': repo.split('/')[0] + ':' + head, 'per_page': 100})
    endpoint = 'repos/' + repo + '/pulls?' + query
    result = subprocess.run([state['real_gh'], 'api', '--hostname', host, '--method', 'GET', endpoint],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('PR discovery unavailable; retain existing bindings and retry discover at a safe checkpoint')
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError('Discovery requires exactly one open PR for the recorded published branch; '
                         'publish it or resolve ambiguous PRs, then run discover')
    row = rows[0]
    if (not isinstance(row, dict) or row.get('state') != 'open' or row.get('head', {}).get('ref') != head or
            any(row.get(side, {}).get('repo', {}).get('full_name', '').casefold() != repo.casefold() for side in ('head', 'base'))):
        raise ValueError('Discovered PR does not match the enrolled repository and actual product branch')
    return row
