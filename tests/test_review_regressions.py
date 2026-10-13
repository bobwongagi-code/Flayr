"""Actual-module regressions for the six findings reviewed at 8e24255.

Provider and worker calls are mocked; all artifacts use temporary directories.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from email.message import Message
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import flayr as cli
from scripts import web_app as web, batch_analyze as batch
from scripts.flayr_core import run_state as state, run_manifest as manifest
from scripts.flayr_core.cli import build_parser, effective_run_identity


class ReviewRegressions(unittest.TestCase):
    def test_environment_model_change_invalidates_completed_batch_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / 'run'
            out.mkdir()
            benchmark = root / 'benchmark.mp4'
            creator = root / 'creator.mp4'
            benchmark.write_bytes(b'benchmark fixture')
            creator.write_bytes(b'creator fixture')
            job = {'name': 'fixture', 'benchmark': str(benchmark), 'creator': str(creator)}
            analysis = {
                'mode': 'improve', 'analysis_run_state': 'completed',
                'videos': {
                    'benchmark': {'transcription_status': 'completed'},
                    'creator': {'transcription_status': 'completed'},
                },
            }
            (out / 'analysis.json').write_text(json.dumps(analysis), encoding='utf-8')
            (out / 'report.html').write_text('<html></html>', encoding='utf-8')
            for name in ('raw_model_response.json', 'validated_normalized_result.json',
                         'postprocess_change_log.json'):
                (out / name).write_text('{}', encoding='utf-8')
            (out / 'final_derived_result.json').write_text(json.dumps({
                'postprocess_provenance': {'field_sources': {
                    'coverage': 'complete', 'unresolved_paths': [], 'truncated': False,
                }},
            }), encoding='utf-8')
            for name, version in (('bd_report.html', 'bd-internal-v2'),
                                  ('creator_report.html', 'creator-v2')):
                report = {'metadata': {
                    'report_schema_version': 2, 'template_version': version,
                    'generated_by': 'test-commit', 'generator': 'tests',
                }}
                html = '<html><script>var report = ' + json.dumps(report, separators=(',', ':'))
                (out / name).write_text(html + ';\n</script></html>', encoding='utf-8')
            state.initialize_run_state(out)
            for target in (state.PROCESSING, state.ANALYSIS_COMPLETED,
                           state.REPORT_GENERATING, state.COMPLETED):
                state.transition_run_state(out, target)
            old_environment = {'FLAYR_JUDGMENT_MODEL': 'qwen3.7-plus',
                               'FLAYR_VISION_MODEL': 'qwen3-vl-plus'}
            with mock.patch.dict(os.environ, old_environment, clear=True):
                identity = effective_run_identity(build_parser().parse_args(batch.build_command(job, out, [])[2:]))
                manifest.write_success_manifest(
                    out, {'benchmark_video': benchmark, 'creator_video': creator}, analysis,
                    {
                    'effective_run_identity': identity,
                    'argv_sha256': manifest.command_digest(batch.build_command(job, out, [])[2:]),
                    'judgment_model': 'qwen3.7-plus', 'vision_model': 'qwen3-vl-plus',
                    'code_commit': '8e242553544680cf55c919a670f88905e6182b1f',
                    },
                )
                self.assertTrue(batch._success_manifest_valid(job, out, []))
            self.assertTrue(manifest.validate_success_manifest(
                out, {'benchmark_video': benchmark, 'creator_video': creator}))
            with mock.patch.dict(os.environ, {
                'FLAYR_JUDGMENT_MODEL': 'gemini-3.7-flash',
                'FLAYR_VISION_MODEL': 'gemini-3.7-flash',
            }, clear=True):
                self.assertFalse(batch._success_manifest_valid(job, out, []),
                                 'A changed effective model route must not reuse the old result')
                self.assertTrue(batch._success_manifest_valid(job, out, [], reuse_historical_results=True))
                before = (out / manifest.SUCCESS_MANIFEST_NAME).read_bytes()
                with mock.patch.object(batch, 'subprocess') as subprocess_mock:
                    status_path = root / '_batch' / 'status.json'
                    status_path.parent.mkdir()
                    # Match the output identity used by this fixture without modifying old artifacts.
                    with mock.patch.object(batch, 'job_output_dir', return_value=out):
                        self.assertEqual(batch._run_jobs([job], [], root, status_path, 1), 1)
                    subprocess_mock.Popen.assert_not_called()
                self.assertEqual((out / manifest.SUCCESS_MANIFEST_NAME).read_bytes(), before)
                self.assertEqual(json.loads(status_path.read_text())['jobs']['fixture']['state'], 'failed')
                creator.write_bytes(b'changed source')
                self.assertFalse(batch._success_manifest_valid(job, out, [], reuse_historical_results=True))

    def test_effective_identity_binds_routes_defaults_and_attestation_not_code_commit(self):
        command = ['improve', '--benchmark-video', 'b.mp4', '--creator-video', 'c.mp4',
                   '--verification-stage', 'production']
        def identity(extra=()):
            return effective_run_identity(build_parser().parse_args([*command, *extra]))
        with mock.patch.dict(os.environ, {}, clear=True):
            baseline = identity()
            self.assertEqual(baseline, identity())
            self.assertEqual(baseline, identity(['--output-dir', 'elsewhere', '--reuse-preprocessing']))
            for env in (
                {'FLAYR_LLM_API_URL': 'https://other.example/v1'},
                {'FLAYR_VISION_API_URL': 'https://vision.example/v1'},
                {'FLAYR_ASR_MODEL': 'other-asr'},
                {'FLAYR_LLM_MODEL': 'other-model'},
            ):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    self.assertNotEqual(baseline, identity())
            self.assertNotEqual(baseline, identity(['--ocr-mode', 'off']))
            explicit = ['--judgment-model', 'fixed-text', '--vision-model', 'fixed-vision']
            fixed = identity(explicit)
            with mock.patch.dict(os.environ, {'FLAYR_LLM_MODEL': 'ignored'}):
                self.assertEqual(fixed, identity(explicit))
            with tempfile.TemporaryDirectory() as temp:
                attestation = Path(temp) / 'speech.json'
                attestation.write_text('{}', encoding='utf-8')
                extra = ['--verified-no-product-speech-attestation-file', str(attestation)]
                first = identity(extra)
                attestation.write_text('{"changed":true}', encoding='utf-8')
                self.assertNotEqual(first, identity(extra))

    def test_producer_identity_uses_the_attestation_bytes_actually_loaded(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            source = root / 'c.mp4'
            source.write_bytes(b'fixture')
            attestation = root / 'speech.json'
            attestation.write_text(json.dumps({'verified_no_product_speech_attestations': [{
                'status': 'verified_no_product_speech', 'scope': 'product_speech',
                'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'attestation_source': 'user_confirmation', 'attestation_statement': 'No product speech.',
            }]}), encoding='utf-8')
            command = ['improve', '--verification-stage', 'production',
                       '--verified-no-product-speech-attestation-file', str(attestation)]
            args = build_parser().parse_args(command)
            loaded = cli._load_verified_no_product_speech_attestations(attestation, {'creator': source})
            args._speech_attestation_sha256 = loaded['creator']['attestation_file']['sha256']
            first = effective_run_identity(args)
            attestation.write_text('{}', encoding='utf-8')
            self.assertEqual(first, effective_run_identity(args))
            self.assertNotEqual(first, effective_run_identity(build_parser().parse_args(command)))

    def test_replay_and_import_contents_are_bound_not_just_their_paths(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            for flag, filename in (
                ('--provider-replay-from', 'benchmark/provider_asr.json'),
                ('--stage1-replay-from', 'stage1_provider_creator_A.json'),
                ('--stage1-resume-from', 'stage1_provider_creator_D_S5.json'),
                ('--stage2-replay-from', 'stage2_provider_S1_S2.json'),
                ('--stage2-resume-from', 'stage2_provider_S6.json'),
                ('--analysis-result-json', 'analysis.json'),
            ):
                with self.subTest(flag=flag):
                    artifact = root / filename
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_text('{"fixture":1}', encoding='utf-8')
                    source = artifact if flag == '--analysis-result-json' else root
                    command = ['improve', '--verification-stage', 'production', flag, str(source)]
                    first = effective_run_identity(build_parser().parse_args(command))
                    artifact.write_text('{"fixture":2}', encoding='utf-8')
                    self.assertNotEqual(first, effective_run_identity(build_parser().parse_args(command)))

    def test_terminal_cached_jobs_fail_closed_when_lifecycle_is_unreadable(self):
        for status in ('completed', 'degraded'):
            for corrupted in (False, True):
                with self.subTest(status=status, corrupted=corrupted), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    run = root / 'jobs' / 'job-regression' / 'run'
                    run.mkdir(parents=True)
                    if corrupted:
                        (run / 'run_state.json').write_text('{invalid', encoding='utf-8')
                    job = {'id': 'job-regression', 'status': status, 'run_dir': str(run),
                           'owner_id': 'owner-a', 'workspace_id': 'local', 'created_at': web.utc_now()}
                    (root / 'jobs.json').write_text(json.dumps({'job-regression': job}), encoding='utf-8')
                    (run / 'degraded_manifest.json').write_text('{}', encoding='utf-8')
                    for name in ('report.html', 'bd_report.html', 'creator_report.html'):
                        (run / name).write_text('<html>stale</html>', encoding='utf-8')
                    store = web.JobStore(root)
                    try:
                        self.assertEqual(store.get('job-regression')['status'], 'failed')
                        self.assertEqual(store.public(job)['status'], 'failed')
                        for artifact in ('report', 'creator-report'):
                            handler = object.__new__(web.FlayrHandler)
                            handler._get_job = mock.Mock(return_value=job)
                            handler._json = mock.Mock()
                            handler._serve_file = mock.Mock()
                            handler._serve_job_artifact('job-regression', artifact)
                            handler._json.assert_called_once_with(409, {'error': '报告尚未生成'})
                            handler._serve_file.assert_not_called()
                        handler._serve_job_asset('job-regression', 'bd_report.html')
                        handler._serve_file.assert_not_called()
                    finally:
                        store.shutdown()

    def test_degraded_restart_requires_consistent_readable_publication_evidence(self):
        for analysis_state, marker in (
            ('completed', '{invalid'),
            ('degraded', '{invalid'),
            ('degraded', '{}'),
            ('degraded', '[]'),
            ('degraded', '{"analysis_run_state":"completed","degraded_flags":[],"reason":"fixture"}'),
            ('degraded', '{"analysis_run_state":"degraded","degraded_flags":["fixture"],"reason":"fixture"}'),
        ):
            with self.subTest(analysis_state=analysis_state, marker=marker), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run = root / 'jobs' / 'job-regression' / 'run'
                state.recover_run_state(run, state.DEGRADED, reason='fixture')
                (run / 'analysis.json').write_text(json.dumps({'analysis_run_state': analysis_state}), encoding='utf-8')
                (run / 'degraded_manifest.json').write_text(marker, encoding='utf-8')
                for name in ('bd_report.html', 'creator_report.html'):
                    (run / name).write_text('<html>fixture</html>', encoding='utf-8')
                job = {'id': 'job-regression', 'status': 'degraded', 'run_dir': str(run),
                       'owner_id': 'owner-a', 'workspace_id': 'local', 'created_at': web.utc_now()}
                (root / 'jobs.json').write_text(json.dumps({'job-regression': job}), encoding='utf-8')
                expected = 'degraded' if '"reason":"fixture"' in marker and '"analysis_run_state":"degraded"' in marker else 'failed'
                self.assertEqual(web._publication_status(run, {}, recovery=True), expected)
                self.assertEqual(web._publication_status(run, {}, returncode=0), expected)
                store = web.JobStore(root)
                try:
                    recovered = store.get('job-regression')
                    self.assertEqual(recovered['status'], expected)
                    self.assertEqual(bool(store.public(recovered)['report_url']), expected == 'degraded')
                finally:
                    store.shutdown()

    def test_corrupt_index_recovery_preserves_failed_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job_root = root / 'jobs' / 'job-regression'
            run = job_root / 'run'
            run.mkdir(parents=True)
            job = {
                'id': 'job-regression', 'owner_id': 'owner-a', 'workspace_id': 'local',
                'status': 'failed', 'created_at': web.utc_now(), 'run_dir': str(run),
            }
            (job_root / web.JOB_METADATA_FILE).write_text(json.dumps(job), encoding='utf-8')
            state.initialize_run_state(run, job_id='job-regression')
            state.transition_run_state(run, state.PROCESSING)
            state.transition_run_state(run, state.FAILED, reason='fixture failure')
            (run / 'degraded_manifest.json').write_text('{}', encoding='utf-8')
            for name in ('bd_report.html', 'creator_report.html'):
                (run / name).write_text('<html>leftover report</html>', encoding='utf-8')
            (root / 'jobs.json').write_text('{invalid-json', encoding='utf-8')
            store = web.JobStore(root)
            try:
                recovered = store.get('job-regression')
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered['status'], 'failed')
                self.assertEqual(store.public(recovered)['report_url'], '')
            finally:
                store.shutdown()

    def test_chunked_body_cannot_exceed_reserved_bytes_using_content_length(self):
        handler = object.__new__(web.FlayrHandler)
        handler.headers = {'Content-Length': '1', 'Transfer-Encoding': 'chunked'}
        handler.connection = mock.Mock()
        handler.connection.gettimeout.return_value = 15.0
        handler.rfile = io.BytesIO(b'1000\r\n' + b'x' * 4096 + b'\r\n0\r\n\r\n')
        destination = io.BytesIO()
        try:
            reserved = handler._upload_storage_reservation_bytes()
            handler._read_request_body(destination)
        except web.RequestError:
            return  # Rejecting an ambiguous framing request is also correct.
        self.assertGreaterEqual(reserved, len(destination.getvalue()),
                                'The actual body exceeded its admitted storage reservation')

    def test_upload_framing_rejects_duplicates_and_unsupported_encodings_before_reading(self):
        for fields in (
            [('Content-Length', '1'), ('Content-Length', '4096')],
            [('Content-Length', '1,4096')], [('Content-Length', '-1')],
            [('Transfer-Encoding', 'gzip, chunked')],
            [('Transfer-Encoding', 'chunked'), ('Transfer-Encoding', 'chunked')],
            [('Content-Length', str(web.MAX_REQUEST_BYTES + 1))], [],
            [('Content-Length', '9' * 5000)],
        ):
            with self.subTest(fields=fields):
                handler = object.__new__(web.FlayrHandler)
                handler.headers = Message()
                for key, value in fields:
                    handler.headers[key] = value
                handler.rfile = mock.Mock()
                with self.assertRaises(web.RequestError):
                    handler._upload_storage_reservation_bytes()
                with self.assertRaises(web.RequestError):
                    handler._read_request_body(io.BytesIO())
                handler.rfile.read.assert_not_called()

    def test_valid_chunked_upload_is_fully_reserved_and_truncation_is_rejected(self):
        for payload, valid in (
            (b'4\r\ndata\r\n0\r\n\r\n', True),
            (b'4\r\nda', False), (b'0\r\n', False),
            (b'+4\r\ndata\r\n0\r\n\r\n', False),
            (b'0\r\n' + (b'x: y\r\n' * 12000) + b'\r\n', False),
        ):
            handler = object.__new__(web.FlayrHandler)
            handler.headers = {'Transfer-Encoding': 'chunked'}
            handler.connection = mock.Mock()
            handler.connection.gettimeout.return_value = 15
            handler.rfile = io.BytesIO(payload)
            destination = io.BytesIO()
            if valid:
                reservation = handler._upload_storage_reservation_bytes()
                handler._read_request_body(destination)
                self.assertEqual(destination.getvalue(), b'data')
                self.assertGreaterEqual(reservation, len(destination.getvalue()) * 2)
            else:
                with self.assertRaises(web.RequestError):
                    handler._read_request_body(destination)
            handler.connection.settimeout.assert_called_with(15)

    def test_monitor_error_stops_child_before_forgetting_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = web.JobStore(root)
            job_id = 'job-regression'
            run = root / 'run'
            run.mkdir()
            store.jobs[job_id] = {
                'id': job_id, 'status': 'queued', 'run_dir': str(run),
                'log_path': str(root / 'worker.log'),
            }
            child = mock.Mock()
            child.poll.return_value = None
            def update(_job_id, **changes):
                if 'progress' in changes:
                    raise OSError('injected persistence error')
                store.jobs[_job_id].update(changes)
            try:
                with (
                    mock.patch.object(store, '_command', return_value=['fake-worker']),
                    mock.patch.object(store, '_update', side_effect=update),
                    mock.patch.object(web.subprocess, 'Popen', return_value=child),
                    mock.patch.object(web, 'stop_process_group') as stop,
                    mock.patch.object(web, 'recover_run_state'),
                ):
                    store._run_job(job_id)
                    self.assertTrue(stop.called,
                                    'A live child was removed from tracking without termination')
                    self.assertIs(stop.call_args.args[0], child)
            finally:
                # A failing test must not cause cleanup to touch a real process.
                with mock.patch.object(web, 'stop_process_group'):
                    store.shutdown()

    def test_monitor_and_failure_persistence_errors_still_stop_real_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = web.JobStore(root)
            job_id = 'job-regression'
            store.jobs[job_id] = {'id': job_id, 'status': 'queued', 'run_dir': str(root / 'run'),
                                  'log_path': str(root / 'worker.log')}
            real_popen = web.subprocess.Popen
            children = []
            def launch(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child
            def update(_job_id, **changes):
                if 'progress' in changes or changes.get('status') == 'failed':
                    raise OSError('injected index failure')
                store.jobs[_job_id].update(changes)
            try:
                with (
                    mock.patch.object(store, '_command', return_value=[sys.executable, '-c', 'import time; time.sleep(60)']),
                    mock.patch.object(store, '_update', side_effect=update),
                    mock.patch.object(web.subprocess, 'Popen', side_effect=launch),
                    mock.patch.object(web, 'recover_run_state', side_effect=OSError('state persistence failed')),
                ):
                    store._run_job(job_id)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertNotIn(job_id, store._running_processes)
            finally:
                for child in children:
                    web.stop_process_group(child)
                store.shutdown()

    def test_cleanup_failure_keeps_live_handle_for_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = web.JobStore(root)
            store.jobs['job-test'] = {'id': 'job-test', 'run_dir': str(root / 'run'),
                                      'log_path': str(root / 'worker.log')}
            child = mock.Mock()
            child.poll.return_value = None
            try:
                with (
                    mock.patch.object(store, '_command', return_value=['fake-worker']),
                    mock.patch.object(store, '_update', side_effect=OSError('cannot persist')),
                    mock.patch.object(web.subprocess, 'Popen', return_value=child),
                ):
                    # Fail only after Popen so the cleanup path has a live child.
                    store._update.side_effect = [None, OSError('monitor failed'), OSError('report failed')]
                    with mock.patch.object(web, 'stop_process_group', side_effect=OSError('kill failed')):
                        store._run_job('job-test')
                self.assertIs(store._running_processes['job-test'], child)
                with mock.patch.object(web, 'stop_process_group') as stop:
                    store.shutdown()
                    stop.assert_called_with(child, grace_seconds=5.0)
            finally:
                with mock.patch.object(web, 'stop_process_group'):
                    store.shutdown()

    def test_breakdown_report_generation_finishes_in_a_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            state.initialize_run_state(run)
            state.transition_run_state(run, state.PROCESSING)
            report = run / 'report.html'
            report.write_text('<html>fixture</html>', encoding='utf-8')
            budget = mock.Mock()
            budget.snapshot.return_value = {}
            with mock.patch.object(cli, 'write_report', return_value=report):
                result = cli._generate_reports_and_publish(
                    run, argparse.Namespace(mode='breakdown'), {},
                    {'analysis_run_state': 'completed'}, budget,
                )
            self.assertEqual(result, report)
            self.assertIn(state.read_run_state(run)['state'],
                          {'COMPLETED', 'FAILED', 'DEGRADED', 'UNPUBLISHED'})
            self.assertEqual(state.read_run_state(run)['state'], state.COMPLETED)
            self.assertFalse((run / manifest.SUCCESS_MANIFEST_NAME).exists())

    def test_scope_cli_closes_without_comparison_success_and_failure_is_visible(self):
        for fails, writes_eligibility in ((False, True), (False, False), (True, False)):
            with self.subTest(fails=fails, writes_eligibility=writes_eligibility), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                benchmark, creator = root / 'b.mp4', root / 'c.mp4'
                benchmark.write_bytes(b'b')
                creator.write_bytes(b'c')
                run = root / 'run'
                command = ['flayr', 'scope', '--benchmark-video', str(benchmark),
                           '--creator-video', str(creator), '--output-dir', str(run),
                           '--verification-stage', 'production', '--llm-dry-run']
                def preflight(*args):
                    if fails:
                        raise RuntimeError('injected scope error')
                    if writes_eligibility:
                        (run / 'comparison_eligibility.json').write_text('{}', encoding='utf-8')
                    return {}
                with (
                    mock.patch.object(sys, 'argv', command),
                    mock.patch.object(cli, 'probe_duration_seconds', return_value=10),
                    mock.patch.object(cli, 'process_video', return_value={}),
                    mock.patch.object(cli, 'run_comparison_scope_preflight', side_effect=preflight),
                    mock.patch.object(cli, 'print_scope_summary'),
                    mock.patch.object(cli, 'write_report') as report,
                    mock.patch.object(cli, 'write_success_manifest') as success,
                ):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, 'injected scope error'):
                            cli.main()
                    else:
                        self.assertEqual(cli.main(), 0)
                    report.assert_not_called()
                    success.assert_not_called()
                lifecycle = state.read_run_state(run)
                self.assertEqual(lifecycle['state'], state.FAILED if fails else state.UNPUBLISHED)
                self.assertIsNotNone(lifecycle['completed_at'])
                if not fails:
                    self.assertEqual('comparison_eligibility.json' in lifecycle['artifacts'], writes_eligibility)

    def test_report_endpoint_really_falls_back_to_legacy_html(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            legacy = run / 'report.html'
            legacy.write_text('<html>legacy report</html>', encoding='utf-8')
            state.recover_run_state(run, state.COMPLETED, reason='validated fixture')
            for invalid_new_report in (False, True):
                with self.subTest(invalid_new_report=invalid_new_report):
                    new = run / 'bd_report.html'
                    if invalid_new_report:
                        new.write_text('not HTML', encoding='utf-8')
                    else:
                        new.unlink(missing_ok=True)
                    handler = object.__new__(web.FlayrHandler)
                    handler._get_job = mock.Mock(return_value={
                        'status': 'completed', 'run_dir': str(run),
                    })
                    handler._json = mock.Mock()
                    handler._serve_file = mock.Mock()
                    handler._serve_job_artifact('job-regression', 'report')
                    handler._serve_file.assert_called_once()
                    self.assertEqual(handler._serve_file.call_args.args[0], legacy.resolve())

    def test_failed_and_unpublished_lifecycle_blocks_all_report_surfaces(self):
        for lifecycle in (state.FAILED, state.UNPUBLISHED):
            with self.subTest(lifecycle=lifecycle), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run = root / 'run'
                state.initialize_run_state(run)
                state.transition_run_state(run, state.PROCESSING)
                if lifecycle == state.UNPUBLISHED:
                    state.transition_run_state(run, state.ANALYSIS_COMPLETED)
                state.transition_run_state(run, lifecycle)
                for name in ('report.html', 'bd_report.html', 'creator_report.html'):
                    (run / name).write_text('<html>stale</html>', encoding='utf-8')
                (run / 'degraded_manifest.json').write_text('{}', encoding='utf-8')
                store = web.JobStore(root)
                try:
                    job = {'id': 'job-regression', 'status': 'completed', 'run_dir': str(run)}
                    public = store.public(job)
                    self.assertEqual(public['status'], 'failed')
                    for key in ('report_url', 'bd_report_url', 'creator_report_url'):
                        self.assertEqual(public[key], '')
                    self.assertEqual(web._publication_status(run, {}, recovery=True), 'failed')
                    self.assertEqual(web._publication_status(run, {}, returncode=0), 'failed')
                    for artifact in ('report', 'creator-report'):
                        handler = object.__new__(web.FlayrHandler)
                        handler._get_job = mock.Mock(return_value=job)
                        handler._json = mock.Mock()
                        handler._serve_file = mock.Mock()
                        handler._serve_job_artifact('job-regression', artifact)
                        handler._json.assert_called_once_with(409, {'error': '报告尚未生成'})
                        handler._serve_file.assert_not_called()
                    for name in ('report.html', 'bd_report.html', 'creator_report.html'):
                        handler = object.__new__(web.FlayrHandler)
                        handler._get_job = mock.Mock(return_value=job)
                        handler._json = mock.Mock()
                        handler._serve_file = mock.Mock()
                        handler._serve_job_asset('job-regression', name)
                        handler._json.assert_called_once_with(409, {'error': '报告尚未生成'})
                        handler._serve_file.assert_not_called()
                finally:
                    store.shutdown()

    def test_new_valid_report_wins_over_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            for name in ('bd_report.html', 'report.html'):
                (run / name).write_text('<html>valid</html>', encoding='utf-8')
            state.recover_run_state(run, state.COMPLETED, reason='validated fixture')
            handler = object.__new__(web.FlayrHandler)
            handler._get_job = mock.Mock(return_value={'status': 'completed', 'run_dir': str(run)})
            handler._serve_file = mock.Mock()
            handler._serve_job_artifact('job-test', 'report')
            self.assertEqual(handler._serve_file.call_args.args[0], (run / 'bd_report.html').resolve())
