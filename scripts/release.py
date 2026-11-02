"""Deterministic checkout stamping and fail-closed main-push publication."""
import argparse
import configparser
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib
import zipfile

BASE = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'


def versions(base, commit):
    if not re.fullmatch(BASE, base, re.ASCII):
        raise ValueError('Expected MAJOR.MINOR.PATCH without leading zeros')
    if not re.fullmatch(r'[0-9a-f]{40}', commit, re.ASCII):
        raise ValueError('Expected full lowercase commit SHA')
    return f'{base}-{commit[:12]}', f'{base}+g{commit[:12]}'


def base_version(root):
    return tomllib.loads((Path(root) / 'pyproject.toml').read_text())['project']['version']


def stamp(root, commit):
    root = Path(root)
    project = root / 'pyproject.toml'
    text = project.read_text()
    data = tomllib.loads(text)
    base = data['project']['version']
    tag, wheel = versions(base, commit)
    manifest = root / 'plugin.yaml'
    manifest_text = manifest.read_text()
    if re.findall(r'^version: (.+)$', manifest_text, re.MULTILINE) != [base]:
        raise ValueError('Project and root plugin base versions must match')
    embedded = data.get('tool', {}).get('hermes', {})
    if embedded and embedded.get('version') != base:
        raise ValueError('Embedded plugin base version must match')
    # Only known section-local version fields; dependencies are never replaced.
    sections = ['project'] + (['tool.hermes'] if embedded else [])
    for section in sections:
        pattern = rf'(\[{re.escape(section)}\]\n(?:(?!\[)[^\n]*\n)*?^version = "){re.escape(base)}("$)'
        text, count = re.subn(pattern, rf'\g<1>{wheel}\2', text, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f'Expected one known-format version in {section}')
    if tomllib.loads(text)['project']['version'] != wheel:
        raise ValueError('Invalid stamped project')
    manifest_text = re.sub(r'^version: .+$', f'version: {wheel}', manifest_text, flags=re.MULTILINE)
    project.write_text(text)
    manifest.write_text(manifest_text)
    return tag, wheel


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def asset_names(wheel):
    if not re.fullmatch(BASE + r'\+g[0-9a-f]{12}', wheel, re.ASCII):
        raise ValueError('Expected stamped wheel version')
    return [f'hermes_model_proxy-{wheel}-py3-none-any.whl',
            f'hermes_model_proxy-{wheel}.tar.gz']


def checksums(directory, wheel, commit, write=False):
    base = wheel.split("+", 1)[0]
    tag, expected_wheel = versions(base, commit)
    if expected_wheel != wheel:
        raise ValueError("Artifact version does not match full commit")
    directory = Path(directory)
    names = asset_names(wheel)
    expected = set(names) | (set() if write else {'SHA256SUMS'})
    if {p.name for p in directory.iterdir()} != expected:
        raise ValueError('Unexpected or missing release assets')
    if any(p.is_symlink() or not p.is_file() for p in directory.iterdir()):
        raise ValueError('Assets must be regular files')
    header = f'# commit: {commit}\n# release: {tag}\n# package: {wheel}\n'
    content = header + ''.join(
        f'{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n'
        for name in names)
    checksum_file = directory / 'SHA256SUMS'
    if write:
        checksum_file.write_text(content, encoding='ascii')
    elif checksum_file.read_text(encoding='ascii') != content:
        raise ValueError('Checksum manifest does not match assets')


def metadata(directory, wheel):
    """Verify actual archive metadata without extracting untrusted paths."""
    whl, sdist = [Path(directory) / name for name in asset_names(wheel)]
    with zipfile.ZipFile(whl) as archive:
        prefix = f'hermes_model_proxy-{wheel}.dist-info/'
        message = Parser().parsestr(archive.read(prefix + 'METADATA').decode())
        entry = configparser.ConfigParser(interpolation=None)
        entry.read_string(archive.read(prefix + 'entry_points.txt').decode())
        expected = 'hermes_model_proxy.model_proxy:register'
        if (message.get_all('Version') != [wheel]
                or entry.get('hermes_agent.plugins', 'model-proxy', fallback=None) != expected):
            raise ValueError('Wheel metadata or plugin entry point mismatch')
    with tarfile.open(sdist) as archive:
        prefix = f'hermes_model_proxy-{wheel}/'
        def read(name):
            return archive.extractfile(prefix + name).read().decode()
        data = tomllib.loads(read('pyproject.toml'))
        if (Parser().parsestr(read('PKG-INFO'))['Version'] != wheel
                or data['project']['version'] != wheel
                or re.findall(r'^version: (.+)$', read('plugin.yaml'), re.MULTILINE) != [wheel]):
            raise ValueError('Source distribution version mismatch')
        embedded = data.get('tool', {}).get('hermes', {})
        if embedded and embedded.get('version') != wheel:
            raise ValueError('Embedded plugin version mismatch')


def pages(repo, endpoint):
    result = json.loads(run('gh', 'api', f'repos/{repo}/{endpoint}', '--paginate', '--slurp'))
    if not isinstance(result, list) or not result or any(not isinstance(p, list) for p in result):
        raise ValueError('Expected paginated lists')
    return [item for page in result for item in page]


def ensure_no_release(repo, tag):
    for item in pages(repo, 'releases'):
        if not isinstance(item, dict) or not isinstance(item.get('tag_name'), str):
            raise ValueError('Expected a release tag name')
        if item['tag_name'] == tag:
            raise ValueError('A release already exists; inspect before retrying')


def remote_commit(repo, tag):
    # Listing permits an absent tag without treating arbitrary lookup errors as absence.
    found = None
    for item in pages(repo, 'git/matching-refs/tags/'):
        if not isinstance(item, dict) or not isinstance(item.get('ref'), str):
            raise ValueError('Malformed tag lookup')
        obj = item.get('object')
        validate_object(obj)
        if item['ref'] == f'refs/tags/{tag}':
            if found is not None:
                raise ValueError('Duplicate tag lookup')
            found = obj
    if found is None:
        return None
    for _ in range(10):
        validate_object(found)
        if found['type'] == 'commit':
            return found['sha']
        if found['type'] != 'tag':
            break
        found = json.loads(run('gh', 'api', f'repos/{repo}/git/tags/{found["sha"]}'))['object']
    raise ValueError('Tag does not resolve to a commit')


def validate_object(obj):
    if (not isinstance(obj, dict) or obj.get('type') not in {'tag', 'commit'}
            or not isinstance(obj.get('sha'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', obj['sha'])):
        raise ValueError('Malformed tag object')


def publish(repo, base, commit, directory, event, ref):
    tag, wheel = versions(base, commit)
    if event != 'push' or ref != 'refs/heads/main':
        raise ValueError('Publication requires a main push')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Expected owner/repository')
    checksums(directory, wheel, commit)
    metadata(directory, wheel)
    ensure_no_release(repo, tag)
    existing = remote_commit(repo, tag)
    if existing is not None and existing != commit:
        raise ValueError('Existing tag points to another commit')
    subprocess.run(['gh', 'release', 'create', tag, '--repo', repo,
                    '--target', commit, '--latest=false', '--title', tag, '--notes',
                    f'CI-tested commit {commit}. Package version {wheel}. Includes SHA256SUMS.',
                    *[str(Path(directory) / name) for name in asset_names(wheel) + ['SHA256SUMS']]],
                   check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['stamp', 'checksums', 'validate', 'publish'])
    parser.add_argument('--root', default='.')
    parser.add_argument('--directory', default='dist')
    parser.add_argument('--repo')
    parser.add_argument('--commit', required=True)
    parser.add_argument('--event')
    parser.add_argument('--ref')
    args = parser.parse_args()
    if args.command == 'stamp':
        print('\n'.join(stamp(args.root, args.commit)))
    else:
        base = base_version(args.root)
        # CI's checkout is stamped; publish reads the tracked base checkout.
        if args.command == 'publish':
            publish(args.repo, base, args.commit, args.directory, args.event, args.ref)
        else:
            suffix = f'+g{args.commit[:12]}'
            raw = base.removesuffix(suffix)
            _, wheel = versions(raw, args.commit)
            if base != wheel:
                raise ValueError('Checkout must be stamped for this commit')
            metadata(args.directory, wheel)
            checksums(args.directory, wheel, args.commit, write=args.command == 'checksums')


if __name__ == '__main__':
    main()
