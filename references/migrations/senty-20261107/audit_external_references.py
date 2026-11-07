"""Report references and secret indicators without emitting credential values."""
import collections
import base64
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[3]
WORKSPACE = ROOT.parent
KEYS = WORKSPACE.parent / '有价值的md/apikey.md'
SUFFIXES = {'md', 'json', 'jsonc', 'py', 'js', 'mjs', 'ts', 'tsx', 'html', 'toml',
            'yaml', 'yml', 'sh', 'txt', 'pem', 'key', 'env', 'ini', 'cfg', 'conf', 'config', 'properties'}
EXCLUDE = ['Senty/**', '**/node_modules/**', '**/.git/**', '**/.next/**', '**/dist/**',
           '**/build/**', '**/.wrangler/**', '**/.venv/**', '**/__pycache__/**']
TOKEN_PATTERNS = {
    'prefixed_api_token': r'\bsk-[A-Za-z0-9_-]{20,}',
    'cloudflare_user_token': r'\bcfut_[A-Za-z0-9_-]{20,}',
    'google_api_key': r'\bAIza[A-Za-z0-9_-]{30,}',
    'github_token': r'\b(?:ghp_|github_pat_)[A-Za-z0-9_]{25,}',
    'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
}


def rg_files(pattern=None, root='.'):
    args = ['rg', '--files' if pattern is None else '-l', '--hidden', '--no-ignore']
    if pattern is not None:
        args += ['-i', pattern]
    for item in EXCLUDE:
        args += ['-g', '!' + item]
    args += [root]
    r = subprocess.run(args, cwd=WORKSPACE, capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise RuntimeError('File inventory incomplete; inspect local rg failure without printing file contents.')
    paths = [WORKSPACE / line.removeprefix('./') for line in r.stdout.splitlines()]
    return [p for p in paths if p.name != '.DS_Store' and (
        not p.suffix or p.suffix.lower().lstrip('.') in SUFFIXES or p.name.startswith('.env.'))]


class ObjectPairs(list):
    """Preserve duplicate JSON members instead of silently dropping earlier values."""


def leaves(value, at='$'):
    if isinstance(value, (dict, ObjectPairs)):
        # Ordinal locations never copy a credential used as an object key into logs.
        pairs = value.items() if isinstance(value, dict) else value
        for i, (key, item) in enumerate(pairs):
            yield f'{at}.key[{i}]', str(key), True
            yield from leaves(item, f'{at}.value[{i}]')
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from leaves(item, f'{at}[{i}]')
    elif isinstance(value, str):
        yield at, value, False


def json_strings(text):
    try:
        return leaves(json.loads(text, object_pairs_hook=ObjectPairs))
    except (ValueError, RecursionError):
        return None


def scan_credentials(text, is_json, token_search, known_search):
    entries = json_strings(text) if is_json else None
    findings = []
    if entries is not None:
        for location, value, is_key in entries:
            matches = [(m, m.lastgroup) for m in token_search.finditer(value)]
            if known_search:
                matches += [(m, 'matches_local_reference_credential_candidate') for m in known_search.finditer(value)]
            if not matches:
                continue
            prefix = 'data:video/mp4;base64,'
            media_body = False
            if not is_key and value.startswith(prefix):
                try:
                    raw = base64.b64decode(value[len(prefix):], validate=True)
                    media_body = raw[4:8] == b'ftyp'
                except ValueError:
                    pass
            for match, category in matches:
                item = {'json_location': location, 'string_offset': match.start(), 'categories': [category]}
                if media_body and category == 'google_api_key' and match.start() >= len(prefix):
                    item['resolution'] = 'pattern_inside_base64_media_body_with_mp4_signature'
                findings.append(item)
        return findings
    # Malformed JSON is still scanned as raw text; never dismiss its media-looking lines.
    positions = collections.defaultdict(set)
    for match in token_search.finditer(text):
        positions[text.count('\n', 0, match.start()) + 1].add(match.lastgroup)
    if known_search:
        for match in known_search.finditer(text):
            positions[text.count('\n', 0, match.start()) + 1].add('matches_local_reference_credential_candidate')
    return [{'line': n, 'categories': sorted(kinds)} for n, kinds in positions.items()]


def main():
    keytext = KEYS.read_text(encoding='utf-8') if KEYS.exists() else ''
    # Keep comparisons in memory; report only locations and counts.
    known = set()
    for pattern in TOKEN_PATTERNS.values():
        known.update(re.findall(pattern, keytext))
    for line in keytext.splitlines():
        if re.search(r'key|token|secret|密钥', line, re.I):
            for value in re.findall(r'(?:=|:|：)\s*["\x27`]?([A-Za-z0-9_./+=-]{20,})', line):
                if not re.search(r'example|placeholder|your[_-]|https?://', value, re.I):
                    known.add(value)
    for name in ['FLAYR_LLM_API_KEY', 'DASHSCOPE_API_KEY', 'GEMINI_API_KEY', 'OPENAI_API_KEY', 'CLOUDFLARE_API_TOKEN']:
        if len(os.environ.get(name, '')) >= 20:
            known.add(os.environ[name])
    references = []
    # Include escaped JSON strings before decoding, even with no raw "Senty" text.
    for file in rg_files(r'senty|\\u[0-9a-f]{4}') + ([KEYS] if KEYS.exists() else []):
        text = file.read_text(encoding='utf-8')
        structured = json_strings(text) if file.suffix.lower() == '.json' else None
        entries = [(loc, val) for loc, val, _ in structured] if structured is not None else [(f'line:{i}', s) for i, s in enumerate(text.splitlines(), 1)]
        hits = [(location, value) for location, value in entries if re.search('senty', value, re.I)]
        if not hits:
            continue
        path_hits = []
        for location, value in hits:
            if re.search(r'(?:/|\\|\.\./)Senty(?:/|\\|\b)', value, re.I):
                path_hits.append(location)
        relative = str(file.relative_to(WORKSPACE)) if file.is_relative_to(WORKSPACE) else 'external-key-notes/apikey.md'
        if file == KEYS:
            kind = 'service_note_no_repository_path' if not path_hits else 'key_note_repository_reference'
        elif 'references/archive/senty-' in relative:
            kind = 'preserved_historical_provenance'
        elif relative.startswith('ClipMentor/'):
            kind = 'historical_text_or_logo' if not path_hits else 'review_repository_path'
        elif relative.startswith('Flayr/runs/diagnostics/senty-content-integration-'):
            kind = 'migration_only_record_or_tool'
        elif relative.startswith('Flayr/runs/'):
            kind = 'historical_evidence_text' if not path_hits else 'review_repository_path'
        else:
            kind = 'instruction_or_handoff_reference'
        references.append({'file': relative, 'classification': kind,
                           'match_count': len(hits), 'repository_path_locations': path_hits,
                           'locations': [loc for loc, _ in hits]})
    secrets, errors, non_text = [], [], []
    token_search = re.compile('|'.join(f'(?P<{name}>{pattern})' for name, pattern in TOKEN_PATTERNS.items()))
    known_search = re.compile('|'.join(re.escape(v) for v in sorted(known, key=len, reverse=True))) if known else None
    scanned = 0
    for file in rg_files(root='Flayr'):
        try:
            text = file.read_text(encoding='utf-8')
        except UnicodeError:
            with file.open('rb') as stream:
                signature = stream.read(16)
            if file.name == '.coverage' and signature == b'SQLite format 3\x00':
                non_text.append({'file': str(file.relative_to(WORKSPACE)), 'reason': 'SQLite coverage database, not a text credential/config file'})
                continue
            errors.append(str(file.relative_to(WORKSPACE)))
            continue
        except OSError:
            errors.append(str(file.relative_to(WORKSPACE)))
            continue
        scanned += 1
        for finding in scan_credentials(text, file.suffix.lower() == '.json', token_search, known_search):
            secrets.append({'file': str(file.relative_to(WORKSPACE)), **finding})
    report = {
        'date': '2026-11-07', 'source_repository_exists': (WORKSPACE / 'Senty').is_dir(),
        'reference_scope': 'workspace text/config files including ignored history, .env variants, PEM/key and extensionless files; excludes dependencies, builds, binary media and source Senty',
        'references': references, 'secret_scan': {
            'text_files_scanned': scanned, 'read_errors': errors, 'findings': secrets,
            'non_text_exclusions': non_text,
            'unresolved_findings': [f for f in secrets if 'resolution' not in f],
            'method': 'Known token-prefix/private-key patterns and in-memory equality against credential candidates from the external key notes and selected environment variables; not a proof against unknown secret formats.',
            'credential_values_logged_or_copied': False,
        },
        'live_site': {'status': 'not_checked_by_this_scan',
                      'scope': 'Local file scanning does not inspect or change hosted projects.'},
    }
    output = Path(__file__).with_name('external-reference-audit.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'reference_files': len(references), 'classifications': dict(collections.Counter(r['classification'] for r in references)),
                      'review_path_files': [r['file'] for r in references if r['classification']=='review_repository_path'],
                      'secret_scan': report['secret_scan'], 'source_exists': report['source_repository_exists']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
