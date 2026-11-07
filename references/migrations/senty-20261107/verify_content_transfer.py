"""Verify retained content without importing or starting the Senty application."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = ROOT / 'references/archive/senty-20261107'
SOURCE = ROOT.parent / 'Senty'
REQUIRED_DERIVED = {
    'content-fields-original.json': {'outputContract': dict, 'evidenceLevels': dict, 'decisionStates': dict},
    'local-context-data.json': {'marketProfiles': dict, 'scenes': list, 'reviewTopics': list},
    'local-context-confirmation.json': {'basis': str, 'user_statement': str, 'current_status': str},
    'external-site-status.json': {'project_id': str, 'conclusion': str, 'remote_checks': list},
    'content-index.json': {'media': list, 'jobs': list},
}
SOURCE_SKIP_DIRS = {'.git', 'node_modules', 'dist', '.next', '.wrangler', 'build'}
SOURCE_SKIP_FILES = {'.DS_Store', '.gitkeep'}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def local_path(root, relative):
    relative = Path(relative)
    require(not relative.is_absolute() and '..' not in relative.parts, 'Unsafe inventory path')
    target = root / relative
    require(not target.is_symlink() and target.resolve().is_relative_to(root.resolve()), 'Inventory escapes root')
    return target


def file_set(root, skip_dirs=(), skip_files=()):
    result = set()
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in dirs + files:
            require(not (Path(directory) / name).is_symlink(), 'Symlink needs explicit review')
        for name in files:
            if name not in skip_files:
                result.add((Path(directory) / name).relative_to(root).as_posix())
    return result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_archive(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    inventory = manifest.get('archive_files')
    require(isinstance(inventory, dict) and bool(inventory), 'Missing archive-wide integrity inventory')
    actual = file_set(root) - {'manifest.json'}
    require(actual == set(inventory), 'Archive file set differs from sealed inventory')
    require(set(REQUIRED_DERIVED) <= set(inventory), 'Required derived files absent from inventory')
    for relative, expected in inventory.items():
        target = local_path(root, relative)
        require(target.is_file() and digest(target) == expected['sha256'], f'Archive hash mismatch: {relative}')
        require(target.stat().st_size == expected['bytes'], f'Archive size mismatch: {relative}')
    for relative, schema in REQUIRED_DERIVED.items():
        value = json.loads(local_path(root, relative).read_text(encoding='utf-8'))
        require(isinstance(value, dict), f'Invalid object: {relative}')
        require(all(isinstance(value.get(k), t) for k, t in schema.items()), f'Invalid structure: {relative}')
    for entry in manifest['files']:
        if entry['archived']:
            target = local_path(root, entry['archived'])
            require(target.is_file(), entry['archived'])
            require(digest(target) == entry['sha256'], entry['archived'])
    index = json.loads((root / 'content-index.json').read_text(encoding='utf-8'))
    for media in index['media']:
        require(digest(local_path(root, media['archived'])) == media['sha256'], 'Media hash mismatch')
    for case in index['jobs']:
        original = json.loads(local_path(root, case['file']).read_text(encoding='utf-8'))
        require(original['state'] == case['state'], 'Case state changed')
        require(original.get('input') == case['input'], 'Case input changed')
    require(manifest['summary']['documents'] == 4, 'Document count changed')
    require(len(index['jobs']) == 15, 'Job count changed')
    require(len(index['media']) == 10, 'Media-reference count changed')
    require(len({a['archived'] for a in index['media']}) == 6, 'Unique-media count changed')
    require(not any(Path(p).suffix in {'.js', '.mjs', '.py', '.tsx'} for p in actual), 'Application code in content archive')
    return manifest


def check_source(source, manifest):
    expected = {entry['sourceRelative'] for entry in manifest['files']}
    require(len(expected) == len(manifest['files']), 'Duplicate source inventory entries')
    actual = file_set(source, SOURCE_SKIP_DIRS, SOURCE_SKIP_FILES)
    require(actual == expected,
            f'Source inventory drift: added={len(actual - expected)}, missing={len(expected - actual)}')
    for entry in manifest['files']:
        require(digest(local_path(source, entry['sourceRelative'])) == entry['sha256'],
                f'Source changed: {entry["sourceRelative"]}')


def check_links(root, relative):
    file = root / relative
    for target in re.findall(r'\]\(([^)]+)\)', file.read_text(encoding='utf-8')):
        if '://' in target or target.startswith('#'):
            continue
        target = target.split('#')[0]
        resolved = (file.parent / target).resolve()
        require(resolved.is_relative_to(root.resolve()), f'Link escapes archive: {relative}')
        require(resolved.exists(), f'Broken link: {relative} -> {target}')
        require('Senty' not in resolved.relative_to(root.resolve()).parts, 'Link depends on retired repository')


def main():
    manifest = check_archive(ARCHIVE)
    preserved = [e for e in manifest['files'] if e['archived']]
    # Source comparison is an extra check, not required to use the migrated files.
    if SOURCE.exists():
        check_source(SOURCE, manifest)
    current = ['references/content-planning.md', 'references/local-context-candidates.md',
               'references/migrations/senty-20261107/README.md']
    for relative in current:
        check_links(ROOT, relative)
    with tempfile.TemporaryDirectory(prefix='flayr-content-without-senty-') as tmp:
        isolated = Path(tmp) / 'Flayr'
        copied_archive = isolated / ARCHIVE.relative_to(ROOT)
        shutil.copytree(ARCHIVE, copied_archive)
        for relative in ['references/content-planning.md', 'references/local-context-candidates.md',
                         'references/market-knowledge-my.md',
                         'references/migrations/senty-20261107/README.md',
                         'references/migrations/senty-20261107/content-framework-original.md']:
            dest = isolated / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, dest)
        assert not (isolated.parent / 'Senty').exists()
        check_archive(copied_archive)
        for relative in current:
            check_links(isolated, relative)
        sample = next(e for e in preserved if e['archived'].startswith('documents/'))
        target = copied_archive / sample['archived']
        original_bytes = target.read_bytes()
        target.write_bytes(original_bytes + b'corruption')
        try:
            check_archive(copied_archive)
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError('Corrupted content was not rejected')
        target.write_bytes(original_bytes)
        missing = target.with_suffix('.held-for-test')
        target.rename(missing)
        try:
            check_archive(copied_archive)
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError('Missing content was not rejected')
        missing.rename(target)
        method = isolated / 'references/content-planning.md'
        method.write_text(method.read_text(encoding='utf-8') + '\n[broken](missing-file.md)\n', encoding='utf-8')
        try:
            check_links(isolated, 'references/content-planning.md')
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError('Broken local link was not rejected')
    print(json.dumps({'status': 'PASS', 'preserved_source_paths': len(preserved),
                      'original_source_inventory': len(manifest['files']),
                      'originals_unchanged': SOURCE.exists(), 'isolated_without_senty': True,
                      'negative_checks': ['corrupt_bytes', 'missing_document', 'broken_local_link'],
                      'archive_payloads_checked': len(manifest['archive_files']),
                      'application_code_migrated': False, 'source_deleted': not SOURCE.exists()}))


if __name__ == '__main__':
    main()
