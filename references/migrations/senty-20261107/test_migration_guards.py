"""Regression tests use only isolated copies and synthetic credentials."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

TASK = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, TASK / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='senty-guard-test-')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.v = load('verify_content_transfer')
        self.a = load('audit_external_references')
        self.v.ROOT = self.workspace / 'synthetic-seed/Flayr'
        self.v.ARCHIVE = self.v.ROOT / 'references/archive/senty-20261107'
        self.v.SOURCE = self.workspace / 'absent-Senty'
        self.make_synthetic_archive()

    def make_synthetic_archive(self):
        archive = self.v.ARCHIVE
        archive.mkdir(parents=True)

        def write(relative, value):
            path = archive / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding='utf-8')
            return path

        files = []
        for i in range(4):
            relative = f'documents/method-{i}.md'
            path = write(relative, 'Synthetic method, no business data.')
            files.append({'sourceRelative': relative, 'archived': relative,
                          'sha256': self.v.digest(path)})
        jobs = []
        for i in range(15):
            relative = f'cases/example-{i}.json'
            value = {'state': 'needs_manual_review', 'input': {'name': f'Example {i}'}}
            write(relative, value)
            jobs.append({'file': relative, **value})
        media = []
        for i in range(10):
            relative = f'media/example-{i % 6}.txt'
            path = write(relative, f'Synthetic media {i % 6}')
            media.append({'archived': relative, 'sha256': self.v.digest(path)})
        write('content-index.json', {'jobs': jobs, 'media': media})
        write('content-fields-original.json', {'outputContract': {}, 'evidenceLevels': {}, 'decisionStates': {}})
        write('local-context-data.json', {'marketProfiles': {}, 'scenes': [], 'reviewTopics': []})
        write('local-context-confirmation.json', {'basis': 'synthetic', 'user_statement': 'fixture', 'current_status': 'fixture'})
        write('external-site-status.json', {'project_id': 'synthetic', 'conclusion': 'not checked', 'remote_checks': []})
        write('manifest.json', {'files': files, 'summary': {'documents': 4},
              'archive_files': {p.relative_to(archive).as_posix(): {
                  'sha256': self.v.digest(p), 'bytes': p.stat().st_size}
                  for p in archive.rglob('*') if p.is_file()}})
        for relative in ['references/content-planning.md', 'references/local-context-candidates.md',
                         'references/market-knowledge-my.md',
                         'references/migrations/senty-20261107/README.md',
                         'references/migrations/senty-20261107/content-framework-original.md']:
            path = self.v.ROOT / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Synthetic method fixture.\n', encoding='utf-8')

    def archive(self):
        out = self.workspace / 'Flayr/references/archive/senty-20261107'
        shutil.copytree(self.v.ARCHIVE, out)
        return out

    def test_intact_archive_is_accepted(self):
        self.v.check_archive(self.archive())

    def test_public_method_links_do_not_need_private_archive(self):
        root = TASK.parents[2]
        for relative in ['references/content-planning.md', 'references/local-context-candidates.md',
                         'references/migrations/senty-20261107/README.md']:
            self.v.check_links(root, relative)

    def test_archive_unlisted_addition_is_rejected(self):
        archive = self.archive()
        (archive / 'unexpected.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.v.check_archive(archive)

    def test_resealed_invalid_derived_schema_is_rejected(self):
        archive = self.archive()
        target = archive / 'local-context-data.json'
        target.write_text('{}')
        manifest = json.loads((archive / 'manifest.json').read_text(encoding='utf-8'))
        manifest['archive_files']['local-context-data.json'] = {
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'bytes': target.stat().st_size}
        (archive / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.v.check_archive(archive)

    def audit(self, files):
        if shutil.which('rg') is None:
            self.skipTest('Reference-scan integration requires ripgrep')
        for name, text in files.items():
            out = self.workspace / 'Flayr' / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding='utf-8')
        (self.workspace / 'task').mkdir(exist_ok=True)
        with mock.patch.multiple(self.a, WORKSPACE=self.workspace, KEYS=self.workspace / 'absent-keys',
                                 __file__=str(self.workspace / 'task/audit.py')):
            with mock.patch.dict(os.environ, {k: '' for k in (
                    'FLAYR_LLM_API_KEY', 'DASHSCOPE_API_KEY', 'GEMINI_API_KEY',
                    'OPENAI_API_KEY', 'CLOUDFLARE_API_TOKEN')}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.a.main()
        return json.loads((self.workspace / 'task/external-reference-audit.json').read_text(encoding='utf-8'))

    def test_required_derived_files_cannot_disappear(self):
        archive = self.archive()
        for name in ['local-context-data.json', 'content-fields-original.json',
                     'local-context-confirmation.json', 'external-site-status.json']:
            with self.subTest(name=name):
                target = archive / name
                original = target.read_bytes()
                target.unlink()
                try:
                    with self.assertRaises((ValueError, AssertionError, FileNotFoundError)):
                        self.v.check_archive(archive)
                finally:
                    target.write_bytes(original)

    def test_derived_corruption_is_rejected(self):
        archive = self.archive()
        (archive / 'local-context-data.json').write_text('{broken')
        with self.assertRaises((ValueError, AssertionError)):
            self.v.check_archive(archive)

    def test_new_source_content_is_rejected(self):
        source = self.workspace / 'Senty'
        source.mkdir()
        original = source / 'known.md'
        original.write_text('Known source content.', encoding='utf-8')
        manifest = {'files': [{'sourceRelative': 'known.md', 'sha256': self.v.digest(original)}]}
        self.v.check_source(source, manifest)
        (source / 'late-business-notes.md').write_text('Unique new content.', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.v.check_source(source, manifest)

    def test_common_credential_files_are_included(self):
        if shutil.which('rg') is None:
            self.skipTest('File-discovery integration requires ripgrep')
        names = ['.env', '.env.local', 'credentials', 'key.pem', 'control.txt']
        root = self.workspace / 'Flayr'
        root.mkdir()
        for name in names:
            (root / name).write_text('synthetic fixture')
        with mock.patch.object(self.a, 'WORKSPACE', self.workspace):
            found = {p.name for p in self.a.rg_files(root='Flayr')}
        self.assertEqual(found, set(names))

    def test_json_object_key_paths_are_reported(self):
        r = self.audit({'refs.json': json.dumps({'../Senty/assets/video.mp4': True})})
        self.assertEqual(r['references'][0]['match_count'], 1)
        self.assertTrue(r['references'][0]['repository_path_locations'])

    def test_media_cannot_hide_key_on_same_json_line(self):
        fake = 'AIza' + 'B' * 36
        payload = base64.b64encode(b'\0\0\0\x0cftypabc?' + base64.b64decode(fake)).decode()
        r = self.audit({'media.json': json.dumps({fake: True, 'video': 'data:video/mp4;base64,' + payload})})
        self.assertTrue(r['secret_scan']['unresolved_findings'])
        self.assertNotIn(fake, json.dumps(r))

    def test_media_pattern_is_still_distinguished(self):
        fake = 'AIza' + 'B' * 36
        payload = base64.b64encode(b'\0\0\0\x0cftypabc?' + base64.b64decode(fake)).decode()
        r = self.audit({'media.json': json.dumps({'video': 'data:video/mp4;base64,' + payload})})
        self.assertTrue(r['secret_scan']['findings'])
        self.assertFalse(r['secret_scan']['unresolved_findings'])

    def test_same_line_value_credential_is_not_hidden(self):
        fake = 'AIza' + 'B' * 36
        payload = base64.b64encode(b'\0\0\0\x0cftypabc?' + base64.b64decode(fake)).decode()
        r = self.audit({'media.json': json.dumps({'credential': fake, 'video': 'data:video/mp4;base64,' + payload})})
        self.assertEqual(len(r['secret_scan']['unresolved_findings']), 1)

    def test_escaped_json_credential_key_is_detected(self):
        fake = 'sk-' + 'A' * 32
        raw = json.dumps({fake: True}).replace('sk-', 'sk\\u002d')
        r = self.audit({'keys.json': raw})
        self.assertTrue(r['secret_scan']['unresolved_findings'])
        self.assertNotIn(fake, json.dumps(r))

    def test_invalid_base64_does_not_crash_or_dismiss(self):
        fake = 'AIza' + 'B' * 36
        r = self.audit({'bad.json': json.dumps({'video': 'data:video/mp4;base64,!!!' + fake})})
        self.assertTrue(r['secret_scan']['unresolved_findings'])

    def test_all_common_credential_fixtures_are_detected(self):
        names = ['.env', '.env.local', 'credentials', 'key.pem', 'control.txt']
        r = self.audit({n: 'API_KEY=sk-' + 'A' * 32 for n in names})
        self.assertEqual(r['secret_scan']['text_files_scanned'], 5)
        self.assertEqual(len(r['secret_scan']['unresolved_findings']), 5)

    def test_duplicate_json_value_does_not_hide_credential(self):
        fake = 'sk-' + 'A' * 32
        r = self.audit({'duplicate.json': '{"api_key":"' + fake + '","api_key":"redacted"}'})
        self.assertTrue(r['secret_scan']['unresolved_findings'])
        self.assertNotIn(fake, json.dumps(r))

    def test_duplicate_json_value_does_not_hide_repository_reference(self):
        raw = '{"path":"../Senty/assets/a.mp4","path":"./local/a.mp4"}'
        r = self.audit({'duplicate.json': raw})
        self.assertEqual(r['references'][0]['match_count'], 1)
        self.assertTrue(r['references'][0]['repository_path_locations'])

    def test_source_deleted_mode_does_not_claim_originals_checked(self):
        with mock.patch.object(self.v, 'SOURCE', self.workspace / 'absent-Senty'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.v.main()
        r = json.loads(out.getvalue())
        self.assertTrue(r['source_deleted'])
        self.assertFalse(r['originals_unchanged'])

    def test_binary_coverage_db_is_explicitly_classified(self):
        root = self.workspace / 'Flayr'
        root.mkdir()
        (root / '.coverage').write_bytes(b'SQLite format 3\x00\xff\xfe')
        r = self.audit({'control.txt': 'no credential'})
        self.assertFalse(r['secret_scan']['read_errors'])
        self.assertEqual(len(r['secret_scan']['non_text_exclusions']), 1)

    def test_known_value_inside_media_is_not_dismissed(self):
        import re
        fake = 'AIza' + 'B' * 36
        payload = base64.b64encode(b'\0\0\0\x0cftypabc?' + base64.b64decode(fake)).decode()
        text = json.dumps({'video': 'data:video/mp4;base64,' + payload})
        search = re.compile('|'.join(f'(?P<{name}>{pattern})' for name, pattern in self.a.TOKEN_PATTERNS.items()))
        found = self.a.scan_credentials(text, True, search, re.compile(re.escape(fake)))
        self.assertTrue(any('resolution' not in item for item in found))

    def test_escaped_repository_name_is_not_filtered_out(self):
        r = self.audit({'refs.json': '{"path":"../\\u0053enty/assets/video.mp4"}'})
        self.assertEqual(len(r['references']), 1)
        self.assertTrue(r['references'][0]['repository_path_locations'])


if __name__ == '__main__':
    unittest.main()
