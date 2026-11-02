"""Offline release behavior; publication payload tests mock all mutations."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
import zipfile
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = 'a' * 40
WHEEL = '0.1.0+g' + SHA[:12]
TAG = '0.1.0-' + SHA[:12]


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / '.agent-temp').mkdir(exist_ok=True)

    def test_versions(self):
        self.assertEqual(release.versions('0.1.0', SHA), (TAG, WHEEL))
        self.assertEqual(release.versions('0.1.0', '0' * 40)[1], '0.1.0+g000000000000')
        self.assertNotEqual(release.versions('0.1.0', SHA), release.versions('0.1.0', 'b' * 40))
        for base in ['01.1.0', '0.1', 'v0.1.0', '0.1.0+abc', '0.1.0\n', '١.0.0']:
            with self.subTest(base=base), self.assertRaises(ValueError):
                release.versions(base, SHA)
        for sha in ['', 'a' * 12, 'A' * 40, 'g' * 40, SHA + '\n']:
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                release.versions('0.1.0', sha)

    def test_stamp_prevalidates_and_is_deterministic(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.agent-temp') as tmp:
            root = Path(tmp)
            original = ('[project]\nversion = "0.1.0"\ndependencies = ["other==0.1.0"]\n'
                        '[tool.hermes]\nversion = "0.1.0"\n')
            project = root / 'pyproject.toml'
            manifest = root / 'plugin.yaml'
            for bad in ['version: 0.2.0\n', 'version: 0.1.0\nversion: 0.1.0\n']:
                project.write_text(original)
                manifest.write_text(bad)
                with self.assertRaises(ValueError):
                    release.stamp(root, SHA)
                self.assertEqual(project.read_text(), original)
                self.assertEqual(manifest.read_text(), bad)
            outputs = []
            for _ in range(2):
                project.write_text(original)
                manifest.write_text('version: 0.1.0\n')
                with self.assertRaises(ValueError):
                    release.stamp(root, 'bad')
                self.assertEqual(project.read_text(), original)
                self.assertEqual(release.stamp(root, SHA), (TAG, WHEEL))
                self.assertIn('other==0.1.0', project.read_text())
                self.assertEqual(project.read_text().count(WHEEL), 2)
                outputs.append((project.read_bytes(), manifest.read_bytes()))
            self.assertEqual(*outputs)
            project.write_text(original.replace('[tool.hermes]\nversion = "0.1.0"',
                                                '[tool.hermes]\nversion = "0.2.0"'))
            before = project.read_bytes()
            with self.assertRaises(ValueError):
                release.stamp(root, SHA)
            self.assertEqual(project.read_bytes(), before)

    def test_checksums_and_allowlist(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.agent-temp') as tmp:
            path = Path(tmp)
            names = release.asset_names(WHEEL)
            for name in names:
                (path / name).write_bytes(b'actual artifact')
            release.checksums(path, WHEEL, SHA, write=True)
            self.assertIn(hashlib.sha256(b'actual artifact').hexdigest(),
                          (path / 'SHA256SUMS').read_text())
            release.checksums(path, WHEEL, SHA)
            self.assertIn(SHA, (path / 'SHA256SUMS').read_text())
            with self.assertRaises(ValueError):
                release.checksums(path, WHEEL, SHA[:12] + 'b' * 28)
            (path / names[0]).write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                release.checksums(path, WHEEL, SHA)
            (path / names[0]).unlink()
            with self.assertRaises(ValueError):
                release.checksums(path, WHEEL, SHA)
            (path / names[0]).symlink_to(path / names[1])
            with self.assertRaises(ValueError):
                release.checksums(path, WHEEL, SHA)
            (path / 'extra').write_text('unexpected')
            with self.assertRaises(ValueError):
                release.checksums(path, WHEEL, SHA)

    def test_archive_metadata_fixtures(self):
        # Synthetic metadata fixtures test rejection; local verification also builds real archives.
        with tempfile.TemporaryDirectory(dir=ROOT / '.agent-temp') as tmp:
            path = Path(tmp)
            whl, sdist = [path / name for name in release.asset_names(WHEEL)]
            def wheel(version=WHEEL, entry='[hermes_agent.plugins]\nmodel-proxy = hermes_model_proxy.model_proxy:register'):
                with zipfile.ZipFile(whl, 'w') as archive:
                    prefix = f'hermes_model_proxy-{WHEEL}.dist-info/'
                    archive.writestr(prefix + 'METADATA', f'Version: {version}\n')
                    archive.writestr(prefix + 'entry_points.txt', entry)
            def source(version=WHEEL):
                contents = {'PKG-INFO': f'Version: {version}\n',
                            'pyproject.toml': f'[project]\nversion="{WHEEL}"\n',
                            'plugin.yaml': f'version: {version}\n'}
                with tarfile.open(sdist, 'w:gz') as archive:
                    for name, value in contents.items():
                        data = value.encode()
                        info = tarfile.TarInfo(f'hermes_model_proxy-{WHEEL}/{name}')
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
            wheel()
            source()
            release.metadata(path, WHEEL)
            for bad in ['0.1.0', '0.1.0+gbbbbbbbbbbbb']:
                wheel(bad)
                with self.assertRaises(ValueError):
                    release.metadata(path, WHEEL)
                wheel()
                source(bad)
                with self.assertRaises(ValueError):
                    release.metadata(path, WHEEL)
                source()
            wheel(entry='[hermes_agent.plugins]\nother = other:register')
            with self.assertRaises(ValueError):
                release.metadata(path, WHEEL)

    def test_mocked_publish_payload_and_gates(self):
        with patch.object(release, 'ensure_no_release'), \
                patch.object(release, 'checksums'), patch.object(release, 'metadata'), \
                patch.object(release, 'remote_commit', return_value=None) as remote, \
                patch.object(release.subprocess, 'run') as run:
            def publish(event='push', ref='refs/heads/main'):
                release.publish('owner/repo', '0.1.0', SHA, 'assets', event, ref)
            publish()
            args = run.call_args.args[0]
            self.assertNotIn('--draft', args)
            self.assertNotIn('--verify-tag', args)
            self.assertIn('--latest=false', args)
            self.assertNotIn('--clobber', args)
            self.assertEqual(args[3], TAG)
            self.assertEqual(args[args.index('--target') + 1], SHA)
            self.assertIn(WHEEL, args[args.index('--notes') + 1])
            self.assertIn(SHA, args[args.index('--notes') + 1])
            self.assertEqual(args[-3:], ['assets/' + n for n in
                                        release.asset_names(WHEEL) + ['SHA256SUMS']])
            remote.return_value = SHA
            publish()
            run.reset_mock()
            remote.return_value = 'b' * 40
            with self.assertRaises(ValueError):
                publish()
            for event, ref in [('pull_request', 'refs/heads/main'),
                               ('push', 'refs/heads/feature'), ('workflow_dispatch', 'refs/heads/main')]:
                with self.assertRaises(ValueError):
                    publish(event, ref)
            run.assert_not_called()
        for check in ['checksums', 'metadata', 'ensure_no_release']:
            with patch.object(release, 'checksums'), patch.object(release, 'metadata'), \
                    patch.object(release, check, side_effect=ValueError), \
                    patch.object(release.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    release.publish('owner/repo', '0.1.0', SHA, 'assets', 'push', 'refs/heads/main')
                run.assert_not_called()

    def test_remote_tags(self):
        tag = {'ref': f'refs/tags/{TAG}', 'object': {'type': 'tag', 'sha': 'b' * 40}}
        with patch.object(release, 'run', side_effect=[json.dumps([[tag]]),
                json.dumps({'object': {'type': 'commit', 'sha': SHA}})]):
            self.assertEqual(release.remote_commit('owner/repo', TAG), SHA)
        with patch.object(release, 'run', return_value='[[]]'):
            self.assertIsNone(release.remote_commit('owner/repo', TAG))
        for kind in ['tree', 'blob']:
            unrelated = {'ref': 'refs/tags/unrelated', 'object': {'type': kind, 'sha': 'b' * 40}}
            selected = {'ref': f'refs/tags/{TAG}', 'object': {'type': 'commit', 'sha': SHA}}
            with self.subTest(kind=kind), patch.object(
                    release, 'run', return_value=json.dumps([[unrelated, selected]])):
                self.assertEqual(release.remote_commit('owner/repo', TAG), SHA)
            unrelated['ref'] = f'refs/tags/{TAG}'
            with self.subTest(selected_kind=kind), patch.object(
                    release, 'run', return_value=json.dumps([[unrelated]])), self.assertRaises(ValueError):
                release.remote_commit('owner/repo', TAG)
        for value in ['invalid', '[]', '[{}]', '[[{}]]', '[[{"ref":"x","object":{}}]]']:
            with patch.object(release, 'run', return_value=value), self.assertRaises(ValueError):
                release.remote_commit('owner/repo', TAG)
        with patch.object(release, 'run', side_effect=subprocess.CalledProcessError(1, ['gh'])), \
                self.assertRaises(subprocess.CalledProcessError):
            release.remote_commit('owner/repo', TAG)

    def test_release_lookup_pages_and_failures(self):
        for draft in [True, False]:
            for page in [0, 1]:
                pages = [[], []]
                pages[page] = [{'tag_name': TAG, 'draft': draft}]
                with patch.object(release, 'run', return_value=json.dumps(pages)) as run, \
                        self.assertRaises(ValueError):
                    release.ensure_no_release('owner/repo', TAG)
                self.assertEqual(run.call_args.args[-2:], ('--paginate', '--slurp'))
        for response in ['invalid', '{}', '[]', '[{}]', '[[{}]]', '[[null]]',
                         '[[{"tag_name": null}]]']:
            with patch.object(release, 'run', return_value=response), self.assertRaises(ValueError):
                release.ensure_no_release('owner/repo', TAG)
        with patch.object(release, 'run', return_value='[[{"tag_name":"other"}],[]]'):
            release.ensure_no_release('owner/repo', TAG)
        with patch.object(release, 'run', side_effect=subprocess.CalledProcessError(1, ['gh'])), \
                self.assertRaises(subprocess.CalledProcessError):
            release.ensure_no_release('owner/repo', TAG)

    def test_workflow_contract(self):
        ci = yaml.load((ROOT / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)
        flow = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(flow['on'], {'push': {'branches': ['main']}})
        self.assertIn('workflow_call', ci['on'])
        self.assertIn('pull_request', ci['on'])
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency'], {'group': 'wheel-release-${{ github.sha }}',
                                              'cancel-in-progress': 'false'})
        jobs = flow['jobs']
        self.assertEqual(set(jobs), {'ci', 'publish'})
        self.assertEqual(jobs['ci']['uses'], './.github/workflows/ci.yml')
        self.assertEqual(jobs['ci']['with']['checkout_ref'], '${{ github.sha }}')
        self.assertIn("github.event_name == 'push'", jobs['ci']['if'])
        self.assertIn("github.ref == 'refs/heads/main'", jobs['ci']['if'])
        self.assertEqual(jobs['publish']['needs'], 'ci')
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
        steps = ci['jobs']['offline']['steps']
        upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@'))
        download = next(s for s in jobs['publish']['steps']
                        if s.get('uses', '').startswith('actions/download-artifact@'))
        self.assertEqual(upload['with']['name'], download['with']['name'])
        self.assertEqual(upload['with']['name'], 'release-dist-${{ github.run_id }}')
        self.assertEqual(upload['with']['overwrite'], 'true')
        self.assertNotIn('run-id', download['with'])
        self.assertEqual(upload['if'], "matrix.python == '3.11'")
        self.assertEqual(jobs['publish']['steps'][0]['with']['ref'], '${{ github.sha }}')
        self.assertFalse(any('hatchling' in s.get('run', '') for s in jobs['publish']['steps']))
        self.assertTrue(any('git rev-parse HEAD' in s.get('run', '') for s in steps))
        self.assertTrue(any('release.py stamp' in s.get('run', '') for s in steps))
