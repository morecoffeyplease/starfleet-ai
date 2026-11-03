"""Report execution receipts and supported gh PR-action parsing, without authority grants."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import delivery
import github_records as github


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def artifact_versions(packet, directory):
    """Fingerprint supplied artifacts even when their recorded hash is already wrong."""
    found = {}

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get('path'), str) and 'sha256' in value:
                try:
                    path = Path(value['path'])
                    path = (directory / path).resolve() if not path.is_absolute() else path.resolve()
                    found[str(path)] = digest(path)
                except (OSError, ValueError) as error:
                    found['unavailable:' + value['path']] = type(error).__name__
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(packet)
    return found


def inputs(packet_path, policy_path):
    packet_path = Path(packet_path).resolve()
    raw = packet_path.read_bytes()
    packet = json.loads(raw)
    return {'packet_sha256': hashlib.sha256(raw).hexdigest(),
            'policy_sha256': digest(policy_path),
            'generator_sha256': digest(delivery.__file__),
            'artifacts': artifact_versions(packet, packet_path.parent)}


def run(cwd, base_ref, pr_url, packet_path, policy_path):
    packet_path, policy_path = Path(packet_path).resolve(), Path(policy_path).resolve()
    before = inputs(packet_path, policy_path)
    report = delivery.report(cwd, base_ref, pr_url, packet_path, policy_path)
    after = inputs(packet_path, policy_path)
    if before != after or report['policy_sha256'] != after['policy_sha256']:
        raise ValueError('Report inputs changed during execution; rerun before recording')
    if not report['snapshot']['clean']:
        raise ValueError('Commit the candidate before recording a report run')
    return {'version': 1, 'report': report, 'inputs': after, 'packet_path': str(packet_path),
            'policy_path': str(policy_path),
            'report_sha256': hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()}


def check(receipt, cwd, pr_url):
    if not isinstance(receipt, dict) or receipt.get('version') != 1:
        raise ValueError('No recorded delivery report run for this PR')
    report = receipt['report']
    if report['pr_url'].casefold() != pr_url.casefold():
        raise ValueError('Recorded report belongs to a different PR')
    if hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest() != receipt['report_sha256']:
        raise ValueError('Recorded report hash changed')
    observed = report['snapshot']
    current = delivery.snapshot(cwd, observed['base_ref'])
    if not current['clean'] or any(current[key] != observed[key] for key in ('head', 'tree', 'base_head')):
        raise ValueError('Candidate or local target changed since the report run')
    if inputs(receipt['packet_path'], receipt['policy_path']) != receipt['inputs']:
        raise ValueError('Evidence, checklist policy or report generator changed; run the report again')
    # Missing checklist items are deliberately not an enforcement condition.
    return report


def gh_action(argv):
    if argv[:2] not in (['pr', 'ready'], ['pr', 'merge']):
        return None
    action = argv[1]
    flags = {'ready': {'--undo', '--help'},
             'merge': {'--admin', '--auto', '--disable-auto', '--delete-branch', '-d',
                       '--merge', '-m', '--rebase', '-r', '--squash', '-s', '--help'}}[action]
    values = {'--repo', '-R'}
    if action == 'merge':
        values |= {'--author-email', '-A', '--body', '-b', '--body-file', '-F',
                   '--match-head-commit', '--subject', '-t'}
    target, repo, arguments = None, None, {}
    index = 2
    while index < len(argv):
        token = argv[index]
        flag, separator, inline = token.partition('=')
        if flag in values:
            if separator:
                value = inline
            else:
                index += 1
                if index == len(argv):
                    raise ValueError('Missing gh option value: ' + flag)
                value = argv[index]
            if flag in arguments:
                raise ValueError('Ambiguous repeated gh option: ' + flag)
            arguments[flag] = value
            if flag in ('-R', '--repo'):
                if repo is not None:
                    raise ValueError('Ambiguous repeated gh repository')
                repo = value
        elif token in flags:
            arguments[token] = True
        elif token.startswith('-'):
            raise ValueError('Unsupported PR action argument; use explicit long options: ' + token)
        elif target is None:
            target = token
        else:
            raise ValueError('Ambiguous gh PR action target')
        index += 1
    if '--disable-auto' in arguments and any(key not in ('--disable-auto', '--repo', '-R', '--help') for key in arguments):
        raise ValueError('Do not combine auto-merge cancellation with merge options')
    exempt = '--help' in arguments or (action == 'ready' and '--undo' in arguments) or (
        action == 'merge' and '--disable-auto' in arguments)
    return {'action': action, 'target': target, 'repo': repo, 'exempt': exempt, 'arguments': arguments}


def target_matches(action, pr_url, branch):
    parsed = urlsplit(pr_url)
    owner, repo, _, number = parsed.path.strip('/').split('/')
    expected_repo = owner + '/' + repo
    target = action['target']
    if isinstance(target, str) and target.startswith('https://'):
        try:
            url_matches = github.reference(target, 'pull')['url'].casefold() == pr_url.casefold()
        except github.RecordError:
            url_matches = False
        if not url_matches:
            raise ValueError('Run this PR action from its enrolled worktree; PR target does not match')
        return
    repo_arg = action['repo']
    if repo_arg and repo_arg.casefold() not in (expected_repo.casefold(), (parsed.hostname + '/' + expected_repo).casefold()):
        raise ValueError('Run this PR action from its enrolled worktree; repository does not match')
    if target not in (None, number, branch):
        raise ValueError('Run this PR action from its enrolled worktree; PR target does not match')
