"""Small, fail-closed release checks; publication runs only from trusted main."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib


TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', re.ASCII)


def version(tag):
    if not TAG.fullmatch(tag):
        raise ValueError('Expected vMAJOR.MINOR.PATCH without leading zeros')
    return tag[1:]


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def preflight(tag):
    expected = version(tag)
    ref = f'refs/tags/{tag}'
    commit = run('git', 'rev-parse', '--verify', f'{ref}^{{commit}}')
    subprocess.run(['git', 'merge-base', '--is-ancestor', commit, 'origin/main'], check=True)
    project = tomllib.loads(run('git', 'show', f'{commit}:pyproject.toml'))
    manifest = run('git', 'show', f'{commit}:plugin.yaml')
    versions = re.findall(r'^version: ([^\n]+)$', manifest, re.MULTILINE)
    if project['project']['version'] != expected or versions != [expected]:
        raise ValueError('Tag, project and plugin versions must match exactly')
    return commit


def asset_names(tag):
    value = version(tag)
    return [f'hermes_model_proxy-{value}-py3-none-any.whl',
            f'hermes_model_proxy-{value}.tar.gz']


def checksums(directory, tag, write=False):
    directory = Path(directory)
    names = asset_names(tag)
    expected = set(names) | (set() if write else {'SHA256SUMS'})
    if {p.name for p in directory.iterdir()} != expected:
        raise ValueError('Unexpected or missing release assets')
    if any(p.is_symlink() or not p.is_file() for p in directory.iterdir()):
        raise ValueError('Assets must be regular files')
    content = ''.join(f'{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n'
                      for name in names)
    checksum_file = directory / 'SHA256SUMS'
    if write:
        checksum_file.write_text(content, encoding='ascii')
    elif checksum_file.read_text(encoding='ascii') != content:
        raise ValueError('Checksum manifest does not match assets')


def remote_commit(repo, tag):
    version(tag)
    obj = json.loads(run('gh', 'api', f'repos/{repo}/git/ref/tags/{tag}'))['object']
    for _ in range(10):
        if obj['type'] == 'commit':
            return obj['sha']
        if obj['type'] != 'tag':
            break
        obj = json.loads(run('gh', 'api', f'repos/{repo}/git/tags/{obj["sha"]}'))['object']
    raise ValueError('Tag does not resolve to a commit')


def publish(repo, tag, commit, directory):
    checksums(directory, tag)
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Expected full commit SHA')
    if remote_commit(repo, tag) != commit:
        raise ValueError('Tag changed since preflight')
    # gh refuses an existing release; no upload/clobber or tag creation path.
    subprocess.run(['gh', 'release', 'create', tag, '--repo', repo,
                    '--verify-tag', '--target', commit, '--draft', '--latest=false',
                    '--title', tag, '--notes',
                    'Reviewed wheel and source distribution with SHA256SUMS. '
                    'Owner publication is a separate manual action.',
                    *[str(Path(directory) / name) for name in asset_names(tag) + ['SHA256SUMS']]],
                   check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preflight', 'checksums', 'validate', 'publish'])
    parser.add_argument('--tag', required=True)
    parser.add_argument('--directory', default='dist')
    parser.add_argument('--repo')
    parser.add_argument('--commit')
    args = parser.parse_args()
    if args.command == 'preflight':
        print(preflight(args.tag))
    elif args.command == 'publish':
        publish(args.repo, args.tag, args.commit, args.directory)
    else:
        checksums(args.directory, args.tag, write=args.command == 'checksums')


if __name__ == '__main__':
    main()
