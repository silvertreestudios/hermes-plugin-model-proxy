"""Check distribution inclusion using an allowlist, without extracting files."""
from pathlib import Path
import tarfile
import zipfile


def check():
    modules = {p.name for p in Path('src/hermes_proxy_codex').glob('*.py')}
    wheel, = Path('dist').glob('*.whl')
    source, = Path('dist').glob('*.tar.gz')
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        expected = {'hermes_proxy_codex/'+name for name in modules}
        assert expected <= set(names), 'Wheel missing package modules'
        for name in names:
            if name in expected:
                continue
            parts = name.split('/')
            assert len(parts) >= 2 and parts[0].endswith('.dist-info'), name
            assert '/'.join(parts[1:]) in {
                'METADATA', 'WHEEL', 'entry_points.txt', 'RECORD', 'licenses/LICENSE'}, name
    with tarfile.open(source) as archive:
        files = {m.name.split('/', 1)[1] for m in archive.getmembers() if m.isfile()}
        expected = {'src/hermes_proxy_codex/'+name for name in modules}
        expected |= {'README.md', 'SECURITY.md', 'LICENSE', 'pyproject.toml', 'PKG-INFO', '.gitignore'}
        assert files == expected, (files - expected, expected - files)
    print('Wheel and source distribution contain only allowed project files')


if __name__ == '__main__':
    check()
