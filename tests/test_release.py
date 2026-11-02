"""Offline release validation and trusted workflow contract."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / '.agent-temp').mkdir(exist_ok=True)

    def test_clean_temp_root(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.agent-temp') as tmp:
            root = Path(tmp)
            with patch.dict(globals(), ROOT=root):
                ReleaseTests.setUpClass()
                with tempfile.TemporaryDirectory(dir=root / '.agent-temp') as fixture:
                    self.assertTrue(Path(fixture).is_dir())

    def test_strict_tags(self):
        for tag in ['v0.1.0', 'v12.30.4']:
            self.assertEqual(release.version(tag), tag[1:])
        for tag in ['0.1.0', 'v01.1.0', 'v1.0.0-rc1', 'v1.0.0\n',
                    '--help', 'v1.0.0;echo bad', 'v١.0.0', '../v1.0.0']:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.version(tag)

    def test_checksums_and_allowlist(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.agent-temp') as tmp:
            path = Path(tmp)
            names = release.asset_names('v0.1.0')
            for name in names:
                (path / name).write_bytes(b'actual artifact')
            release.checksums(path, 'v0.1.0', write=True)
            self.assertIn(hashlib.sha256(b'actual artifact').hexdigest(),
                          (path / 'SHA256SUMS').read_text())
            release.checksums(path, 'v0.1.0')
            (path / names[0]).write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                release.checksums(path, 'v0.1.0')
            (path / names[0]).unlink()
            with self.assertRaises(ValueError):
                release.checksums(path, 'v0.1.0')
            (path / names[0]).symlink_to(path / names[1])
            with self.assertRaises(ValueError):
                release.checksums(path, 'v0.1.0')
            (path / 'extra').write_text('unexpected')
            with self.assertRaises(ValueError):
                release.checksums(path, 'v0.1.0')

    def test_local_tag_preflight(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.agent-temp') as tmp:
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], text=True,
                                               stderr=subprocess.PIPE).strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            path = Path(tmp)
            (path / 'pyproject.toml').write_text('[project]\nversion="0.1.0"\n')
            (path / 'plugin.yaml').write_text('version: 0.1.0\n')
            git('add', '.')
            git('commit', '-m', 'fixture')
            sha = git('rev-parse', 'HEAD')
            git('update-ref', 'refs/remotes/origin/main', sha)
            git('tag', '-a', 'v0.1.0', '-m', 'annotated fixture')
            git('tag', 'v0.2.0')
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                self.assertEqual(release.preflight('v0.1.0'), sha)
                with self.assertRaises(ValueError):
                    release.preflight('v0.2.0')
                with self.assertRaises(subprocess.CalledProcessError):
                    release.preflight('v9.0.0')
                (path / 'plugin.yaml').write_text('version: 0.2.0\n')
                git('add', '.')
                git('commit', '-m', 'unmerged')
                git('tag', 'v0.3.0')
                with self.assertRaises(subprocess.CalledProcessError):
                    release.preflight('v0.3.0')
                git('update-ref', 'refs/remotes/origin/main', git('rev-parse', 'HEAD'))
                git('tag', '-f', 'v0.1.0')
                with self.assertRaises(ValueError):
                    release.preflight('v0.1.0')
            finally:
                os.chdir(previous)

    def test_publish_payload_and_race(self):
        with patch.object(release, 'ensure_no_release'), \
                patch.object(release, 'checksums'), patch.object(release, 'remote_commit',
                return_value='a' * 40), patch.object(release.subprocess, 'run') as run:
            release.publish('owner/repo', 'v0.1.0', 'a' * 40, 'assets')
            args = run.call_args.args[0]
            self.assertIn('--draft', args)
            self.assertIn('--verify-tag', args)
            self.assertIn('--latest=false', args)
            self.assertNotIn('--clobber', args)
            self.assertEqual(args[-3:], ['assets/' + n for n in
                                        release.asset_names('v0.1.0') + ['SHA256SUMS']])
            run.reset_mock()
            with self.assertRaises(ValueError):
                release.publish('owner/repo', 'v0.1.0', 'b' * 40, 'assets')
            run.assert_not_called()
        with patch.object(release, 'checksums', side_effect=ValueError), \
                patch.object(release.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                release.publish('owner/repo', 'v0.1.0', 'a' * 40, 'assets')
            run.assert_not_called()

    def test_remote_annotated_tag(self):
        with patch.object(release, 'run', side_effect=[
                '{"object":{"type":"tag","sha":"abc"}}',
                '{"object":{"type":"commit","sha":"def"}}']) as run:
            self.assertEqual(release.remote_commit('owner/repo', 'v0.1.0'), 'def')
            self.assertEqual(run.call_args.args,
                             ('gh', 'api', 'repos/owner/repo/git/tags/abc'))

    def test_release_lookup_pages_and_failures(self):
        for draft in [True, False]:
            for page in [0, 1]:
                pages = [[], []]
                pages[page] = [{'tag_name': 'v0.1.0', 'draft': draft}]
                with self.subTest(draft=draft, page=page), \
                        patch.object(release, 'run', return_value=json.dumps(pages)), \
                        patch.object(release, 'checksums'), \
                        patch.object(release, 'remote_commit') as remote, \
                        patch.object(release.subprocess, 'run') as create:
                    with self.assertRaises(ValueError):
                        release.publish('owner/repo', 'v0.1.0', 'a' * 40, 'assets')
                    remote.assert_not_called()
                    create.assert_not_called()
        for response in ['invalid', '{}', '[]', '[{}]', '[[{}]]', '[[null]]',
                         '[[{"tag_name": null}]]']:
            with self.subTest(response=response), \
                    patch.object(release, 'run', return_value=response), \
                    patch.object(release, 'checksums'), \
                    patch.object(release, 'remote_commit') as remote, \
                    patch.object(release.subprocess, 'run') as create:
                with self.assertRaises(ValueError):
                    release.publish('owner/repo', 'v0.1.0', 'a' * 40, 'assets')
                remote.assert_not_called()
                create.assert_not_called()
        with patch.object(release, 'run', side_effect=subprocess.CalledProcessError(1, ['gh'])), \
                patch.object(release, 'checksums'), \
                patch.object(release, 'remote_commit') as remote, \
                patch.object(release.subprocess, 'run') as create:
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish('owner/repo', 'v0.1.0', 'a' * 40, 'assets')
            remote.assert_not_called()
            create.assert_not_called()

    def test_absent_release_checked_before_tag_and_create(self):
        events = []
        with patch.object(release, 'checksums'), \
                patch.object(release, 'run', side_effect=lambda *args:
                             events.append(args) or '[[{"tag_name":"v0.0.9"}],[]]'), \
                patch.object(release, 'remote_commit', side_effect=lambda *args:
                             events.append('tag') or 'a' * 40), \
                patch.object(release.subprocess, 'run', side_effect=lambda *args, **kwargs:
                             events.append('create')):
            release.publish('owner/repo', 'v0.1.0', 'a' * 40, 'assets')
        self.assertEqual(events, [('gh', 'api', 'repos/owner/repo/releases',
                                   '--paginate', '--slurp'), 'tag', 'create'])

    def test_workflow_contract(self):
        # BaseLoader preserves YAML's `on` key (rather than YAML 1.1 boolean coercion).
        ci = yaml.load((ROOT / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)
        flow = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertIn('workflow_call', ci['on'])
        self.assertIn('pull_request', ci['on'])
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        jobs = flow['jobs']
        self.assertEqual(jobs['preflight']['if'], "github.ref == 'refs/heads/main'")
        self.assertEqual(jobs['ci']['uses'], './.github/workflows/ci.yml')
        self.assertEqual(jobs['ci']['needs'], 'preflight')
        self.assertEqual(jobs['publish']['needs'], ['preflight', 'ci'])
        self.assertEqual(jobs['publish']['permissions'], {'contents': 'write'})
        self.assertEqual(ci['jobs']['offline']['strategy']['matrix']['python'],
                         ['3.11', '3.12', '3.13'])
        for workflow in [ci, flow]:
            for job in workflow['jobs'].values():
                for step in job.get('steps', []):
                    if 'uses' in step:
                        self.assertRegex(step['uses'], r'@[0-9a-f]{40}$')
                    if step.get('uses', '').startswith('actions/checkout@'):
                        self.assertEqual(step['with']['persist-credentials'], 'false')
        upload = next(s for s in ci['jobs']['offline']['steps']
                      if s.get('uses', '').startswith('actions/upload-artifact@'))
        download = next(s for s in jobs['publish']['steps']
                        if s.get('uses', '').startswith('actions/download-artifact@'))
        self.assertEqual(upload['with']['name'], download['with']['name'])
        self.assertEqual(upload['with']['name'], 'release-dist-${{ github.run_id }}')
        self.assertEqual(upload['with']['overwrite'], 'true')
        self.assertNotIn('run-id', download['with'])  # Download defaults to this run.
        self.assertEqual(upload['if'], "matrix.python == '3.11'")
        self.assertNotIn('if', jobs['publish'])
        checkout = jobs['publish']['steps'][0]
        self.assertEqual(checkout['with']['ref'], '${{ github.sha }}')
