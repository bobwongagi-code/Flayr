"""Offline cross-layer retry, replay and fact-cache protocol checks."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.flayr_core.llm import api, cache, gemini_transport, pipeline
from scripts.flayr_core.llm import provider_artifacts as providers
from scripts.flayr_core.llm.json_codec import ResponseParseError, parse_json_text
from scripts.flayr_core.llm.stage_fact_artifacts import completed_stage_fact_artifact
from scripts.flayr_core.network import ValidatedOutboundURL
from scripts.flayr_core.resources import ResourceBudget, ResourceLimits
from scripts.flayr_core.stage_evidence_contracts import freeze_stage_evidence
from scripts.flayr_core.utils import write_json
from tests import test_stage_evidence_contracts as fact_fixtures

API_URL = "https://gateway.example.test/v1/chat/completions"
MODEL = "fixture-model"


def capture_request(command, wire):
    path = Path(command[command.index("--data-binary") + 1][1:])
    wire.append({"body": path.read_bytes(), "url": command[-1]})


def sse_sender(wire, outcomes):
    def send(command, **options):
        capture_request(command, wire)
        finish, content = outcomes[len(wire) - 1]
        if finish == "http":
            return subprocess.CompletedProcess(
                command, 22, "", f"__FLAYR_HTTP_STATUS__{content}"
            )
        event = {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}
        options["stdout_callback"](
            ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
        )
        return subprocess.CompletedProcess(command, 0, "", "__FLAYR_HTTP_STATUS__200")

    return send


@contextmanager
def offline_transport(send):
    def validated(url, **_options):
        return ValidatedOutboundURL(url, urlsplit(url).hostname, 443, ("1.1.1.1",))

    with (
        mock.patch.object(api.shutil, "which", return_value="fixture-curl"),
        mock.patch.object(api, "validate_outbound_url", side_effect=validated),
        mock.patch.object(
            gemini_transport, "validate_outbound_url", side_effect=validated
        ),
        mock.patch.object(api, "run_command", side_effect=send),
        mock.patch.object(gemini_transport, "run_command", side_effect=send),
        mock.patch.object(api.time, "sleep"),
    ):
        yield


def recorded_transport(
    root, payload, budget, meta, *, url=API_URL, retries=1, expansions=2
):
    request = root / "request.json"
    write_json(request, payload)

    def call():
        raw = api.call_llm_api(
            url,
            "fixture-key",
            request,
            root / "response.json",
            retries=retries,
            output_expansions=expansions,
            budget=budget,
            max_time_seconds=120,
            response_meta=meta,
            request_id=meta["logical_request_id"],
            cleanup_raw=False,
        )
        return json.loads(raw), meta

    return providers.provider_call_with_artifact(
        artifact_path=root / "provider.json",
        replay_root=None,
        call_kind="llm",
        payload=payload,
        model=payload["model"],
        api_url=url,
        call=call,
        response_meta=meta,
    )


class RetryAccountingTests(unittest.TestCase):
    def test_mixed_transport_and_expansion_attempts_share_one_budget(self):
        for case, cap, outcomes, expected_calls, expected_status in (
            (
                "mixed",
                4,
                [
                    ("length", "partial"),
                    ("http", 504),
                    ("length", "partial"),
                    ("stop", "{}"),
                ],
                4,
                "completed",
            ),
            (
                "budget",
                2,
                [("length", "partial"), ("http", 504), ("stop", "{}")],
                2,
                "failed",
            ),
            ("permanent", 4, [("http", 404), ("stop", "{}")], 1, "failed"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root, wire, meta = (
                    Path(temp),
                    [],
                    {"logical_request_id": "mixed-request"},
                )
                payload = {"model": MODEL, "messages": [], "max_tokens": 256}
                budget = ResourceBudget(
                    ResourceLimits(max_llm_calls=cap, max_total_wall_time=120)
                )
                with offline_transport(sse_sender(wire, outcomes)):
                    if expected_status == "completed":
                        response, _, source = recorded_transport(
                            root, payload, budget, meta
                        )
                        self.assertEqual(
                            response["choices"][0]["finish_reason"], "stop"
                        )
                        self.assertEqual(source, "live")
                    else:
                        with self.assertRaises(providers.ProviderCallError):
                            recorded_transport(root, payload, budget, meta)
                saved = providers.read_provider_artifact(root / "provider.json")
                self.assertEqual(saved["status"], expected_status)
                self.assertEqual(len(wire), expected_calls)
                self.assertEqual(budget.llm_calls, expected_calls)
                self.assertEqual(len(budget.api_events), expected_calls)
                self.assertEqual(
                    [item["attempt"] for item in budget.api_events],
                    list(range(1, expected_calls + 1)),
                )
                self.assertEqual(
                    {item["request_id"] for item in budget.api_events},
                    {"mixed-request"},
                )
                self.assertEqual(
                    budget.total_uploaded_bytes, sum(len(item["body"]) for item in wire)
                )
                self.assertEqual({item["url"] for item in wire}, {API_URL})
                if case == "mixed":
                    self.assertEqual(
                        meta["request_retry_kinds"],
                        ["output_expansion", "transport", "output_expansion"],
                    )
                    self.assertEqual(
                        [json.loads(item["body"])["max_tokens"] for item in wire],
                        [256, 16384, 16384, 32768],
                    )
                    self.assertEqual(wire[1]["body"], wire[2]["body"])
                    self.assertTrue(
                        all(json.loads(item["body"])["model"] == MODEL for item in wire)
                    )

    def test_exhausted_expansion_preserves_length_instead_of_claiming_semantic_success(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root, wire, meta = Path(temp), [], {"logical_request_id": "capped"}
            payload = {"model": MODEL, "messages": [], "max_tokens": 8192}
            budget = ResourceBudget(
                ResourceLimits(max_llm_calls=4, max_total_wall_time=120)
            )
            with offline_transport(sse_sender(wire, [("length", "{broken")] * 4)):
                response, _, _ = recorded_transport(
                    root, payload, budget, meta, retries=2
                )
            self.assertEqual(len(wire), 3)
            self.assertEqual(budget.llm_calls, 3)
            self.assertEqual(
                [json.loads(item["body"])["max_tokens"] for item in wire],
                [8192, 16384, 32768],
            )
            self.assertEqual(meta["request_retry_kinds"], ["output_expansion"] * 2)
            self.assertEqual(response["choices"][0]["finish_reason"], "length")
            with self.assertRaises(ResponseParseError):
                parse_json_text(response["choices"][0]["message"]["content"])
            saved = providers.read_provider_artifact(root / "provider.json")
            self.assertEqual(
                saved["provider_response"]["choices"][0]["finish_reason"], "length"
            )

    def test_outer_completion_is_explicit_and_permanent_errors_still_stop(self):
        for case, outer_attempts, outcomes, expected_calls, expected_status in (
            ("default", 1, [("stop", "not-json"), ("stop", "{}")], 1, "failed"),
            ("explicit", 2, [("stop", "not-json"), ("stop", "{}")], 2, "completed"),
            ("permanent", 3, [("http", 403), ("stop", "{}")], 1, "failed"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root, wire, meta = Path(temp), [], {"logical_request_id": "outer"}
                payload = {"model": MODEL, "messages": [], "max_tokens": 256}
                request = root / "request.json"
                write_json(request, payload)
                budget = ResourceBudget(
                    ResourceLimits(max_total_wall_time=120, max_llm_calls=3)
                )
                args = SimpleNamespace(llm_api_url=API_URL, _resource_budget=budget)

                def call(
                    args=args,
                    request=request,
                    root=root,
                    outer_attempts=outer_attempts,
                    meta=meta,
                ):
                    content = pipeline.fetch_json_completion(
                        args,
                        "fixture-key",
                        request,
                        root / "response.json",
                        max_attempts=outer_attempts,
                        request_retries=0,
                        response_meta=meta,
                    )
                    return parse_json_text(content), meta

                with offline_transport(sse_sender(wire, outcomes)):
                    options = {
                        "artifact_path": root / "provider.json",
                        "replay_root": None,
                        "call_kind": "qualification",
                        "payload": payload,
                        "model": MODEL,
                        "api_url": API_URL,
                        "call": call,
                        "response_meta": meta,
                    }
                    if expected_status == "completed":
                        providers.provider_call_with_artifact(**options)
                    else:
                        with self.assertRaises(providers.ProviderCallError):
                            providers.provider_call_with_artifact(**options)
                self.assertEqual(
                    providers.read_provider_artifact(root / "provider.json")["status"],
                    expected_status,
                )
                self.assertEqual(len(wire), expected_calls)
                self.assertEqual(budget.llm_calls, expected_calls)
                self.assertEqual(len(budget.api_events), expected_calls)
                self.assertEqual(
                    len({event["request_id"] for event in budget.api_events}), 1
                )
                self.assertEqual(
                    budget.total_uploaded_bytes, sum(len(item["body"]) for item in wire)
                )
                if case == "explicit":
                    self.assertEqual(wire[0], wire[1])
                    self.assertEqual(meta["completion_attempts"], 2)


class NativeStoppingTests(unittest.TestCase):
    @staticmethod
    def envelope():
        return {
            "modelVersion": "gemini-3.7-flash",
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"value":1}'}]},
                    "finishReason": "STOP",
                }
            ],
        }

    def test_native_transport_policy_and_budget_are_independent_limits(self):
        for statuses, cap, expected_calls, succeeds in (
            ([504, 504, 200], 3, 3, True),
            ([500, 500, 200], 3, 2, False),
            ([504, 500, 200], 3, 2, False),
            ([500, 504, 200], 3, 2, False),
            ([404, 200], 3, 1, False),
            ([429, 200], 3, 2, True),
            ([504, 504, 504, 200], 4, 3, False),
            ([504, 200], 1, 1, False),
        ):
            with self.subTest(
                statuses=statuses, cap=cap
            ), tempfile.TemporaryDirectory() as temp:
                root, wire, meta = Path(temp), [], {"logical_request_id": "native"}

                def send(command, wire=wire, statuses=statuses, **options):
                    capture_request(command, wire)
                    status = statuses[len(wire) - 1]
                    return subprocess.CompletedProcess(
                        command,
                        0 if status == 200 else 22,
                        json.dumps(self.envelope()) if status == 200 else "",
                        f"__FLAYR_HTTP_STATUS__{status}",
                    )

                payload = {
                    "model": "gemini-3.7-flash",
                    "messages": [{"role": "user", "content": "source"}],
                }
                budget = ResourceBudget(
                    ResourceLimits(max_total_wall_time=120, max_llm_calls=cap)
                )
                with offline_transport(send), mock.patch.object(
                    api, "_execute_streaming_request"
                ) as compatible:
                    if succeeds:
                        recorded_transport(
                            root,
                            payload,
                            budget,
                            meta,
                            url=api.DEFAULT_COMPANY_LLM_API_URL,
                            retries=2,
                        )
                    else:
                        with self.assertRaises(providers.ProviderCallError):
                            recorded_transport(
                                root,
                                payload,
                                budget,
                                meta,
                                url=api.DEFAULT_COMPANY_LLM_API_URL,
                                retries=2,
                            )
                    compatible.assert_not_called()
                self.assertEqual(len(wire), expected_calls)
                self.assertEqual(budget.llm_calls, expected_calls)
                self.assertEqual(len(budget.api_events), expected_calls)
                self.assertEqual(len({item["body"] for item in wire}), 1)
                self.assertEqual(len({item["url"] for item in wire}), 1)
                self.assertEqual(
                    providers.read_provider_artifact(root / "provider.json")["status"],
                    "completed" if succeeds else "failed",
                )

    def test_answer_rejection_is_not_output_expansion_or_route_fallback(self):
        for case in (
            "missing_model",
            "wrong_model",
            "truncated",
            "missing_finish",
            "empty",
            "invalid_json",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root, wire, meta = (
                    Path(temp),
                    [],
                    {"logical_request_id": "rejected-native"},
                )
                envelope = self.envelope()
                if case == "missing_model":
                    envelope.pop("modelVersion")
                elif case == "wrong_model":
                    envelope["modelVersion"] = "other-model"
                elif case == "truncated":
                    envelope["candidates"][0]["finishReason"] = "MAX_TOKENS"
                elif case == "missing_finish":
                    envelope["candidates"][0].pop("finishReason")
                elif case == "empty":
                    envelope["candidates"][0]["content"]["parts"] = []
                else:
                    envelope["candidates"][0]["content"]["parts"][0][
                        "text"
                    ] = "not-json"

                def send(command, wire=wire, envelope=envelope, **options):
                    capture_request(command, wire)
                    return subprocess.CompletedProcess(
                        command, 0, json.dumps(envelope), "__FLAYR_HTTP_STATUS__200"
                    )

                payload = {
                    "model": "gemini-3.7-flash",
                    "max_tokens": 8192,
                    "messages": [{"role": "user", "content": "source"}],
                }
                budget = ResourceBudget(ResourceLimits(max_total_wall_time=120))
                with offline_transport(send), mock.patch.object(
                    api, "_execute_streaming_request"
                ) as compatible:
                    with self.assertRaises(providers.ProviderCallError):
                        recorded_transport(
                            root,
                            payload,
                            budget,
                            meta,
                            url=api.DEFAULT_COMPANY_LLM_API_URL,
                            retries=2,
                            expansions=2,
                        )
                    compatible.assert_not_called()
                self.assertEqual(len(wire), 1)
                self.assertEqual(budget.llm_calls, 1)
                self.assertEqual(
                    providers.read_provider_artifact(root / "provider.json")["status"],
                    "failed",
                )
                self.assertTrue(
                    (root / "response.json.native-answer-failure.json").is_file()
                )


class ReplayResumeTests(unittest.TestCase):
    def test_strict_replay_and_resume_have_distinct_fallback_permissions(self):
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "source"}]}
        identity = {
            "call_kind": "qualification",
            "payload": payload,
            "model": MODEL,
            "api_url": API_URL,
        }
        metadata = {
            "logical_request_id": "original",
            "completion_attempts": 1,
            "retry_reasons": [],
            "usage": {},
        }
        for mode in ("replay", "resume"):
            for case in (
                "same",
                "missing",
                "failed",
                "response_changed",
                "payload",
                "model",
                "endpoint",
                "kind",
            ):
                with self.subTest(
                    mode=mode, case=case
                ), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    source, output = root / "source", root / "output" / "provider.json"
                    source.mkdir()
                    artifact = source / output.name
                    if case != "missing":
                        if case == "failed":
                            record = providers.failed_provider_artifact(
                                **identity,
                                error="prior failure",
                                response_meta=metadata,
                            )
                        else:
                            record = providers.completed_provider_artifact(
                                **identity,
                                response={"original": True},
                                response_meta=metadata,
                            )
                            if case == "response_changed":
                                record["provider_response"] = {"tampered": True}
                        providers.write_provider_artifact(artifact, record)
                    source_bytes = artifact.read_bytes() if artifact.exists() else None
                    requested = copy.deepcopy(identity)
                    if case == "payload":
                        requested["payload"]["messages"][0][
                            "content"
                        ] = "different source"
                    elif case == "model":
                        requested["model"] = "different-model"
                    elif case == "endpoint":
                        requested["api_url"] = (
                            "https://other.example.test/v1/chat/completions"
                        )
                    elif case == "kind":
                        requested["call_kind"] = "different-task"
                    live = mock.Mock(
                        return_value=(
                            {"new": True},
                            {**metadata, "logical_request_id": "new"},
                        )
                    )
                    options = dict(
                        artifact_path=output,
                        replay_root=source if mode == "replay" else None,
                        resume_root=source if mode == "resume" else None,
                        call=live,
                        response_meta={},
                        **requested,
                    )
                    if mode == "replay" and case != "same":
                        with self.assertRaises(providers.ProviderReplayError):
                            providers.provider_call_with_artifact(**options)
                        self.assertEqual(
                            providers.read_provider_artifact(output)["status"], "failed"
                        )
                    else:
                        response, _, execution_source = (
                            providers.provider_call_with_artifact(**options)
                        )
                        if case == "same":
                            self.assertEqual(response, {"original": True})
                            self.assertEqual(execution_source, "technical_" + mode)
                        else:
                            self.assertEqual(response, {"new": True})
                            self.assertEqual(execution_source, "live")
                        self.assertEqual(
                            providers.read_provider_artifact(output)["status"],
                            "completed",
                        )
                    if mode == "replay" or case == "same":
                        live.assert_not_called()
                    else:
                        live.assert_called_once_with()
                    self.assertEqual(
                        artifact.read_bytes() if artifact.exists() else None,
                        source_bytes,
                    )


class FactCacheProtocolTests(unittest.TestCase):
    def fixture(self, root):
        facts = fact_fixtures.StageEvidenceContractTests._active_side("C")
        audit = facts["stage1_coverage_audit"]["stages"]["S6"]
        audit.update(status="unknown", coverage="unknown", evidence_ids=[])
        facts["stage1_coverage_audit"].update(
            status="partial", target_stages=["S6"], stages={"S6": audit}
        )
        facts["stage1_recovery"] = {
            "source": "pipeline",
            "status": "focused_recovery_with_unresolved",
            "target_stages": ["S6"],
            "unresolved_stages": ["S6"],
        }
        facts["stage1_qualification"] = {
            "status": "completed",
            "failed_stage_codes": [],
        }
        name = "stage1_provider_creator_A.json"
        facts["stage1_acquisition"]["provider_artifacts"] = [
            {"phase": "A", "artifact": name}
        ]
        freeze_stage_evidence(facts)
        artifact = completed_stage_fact_artifact(
            role="creator",
            phase="A",
            payload={"model": MODEL},
            response={"observed": True},
            model=MODEL,
            api_url=API_URL,
            artifact_name=name,
            response_meta={
                "logical_request_id": "cache-source",
                "completion_attempts": 1,
                "retry_reasons": [],
                "usage": {},
            },
        )
        key = {
            "cache_schema_version": cache.VIDEO_FACT_CACHE_SCHEMA_VERSION,
            "source_video_sha256": "source",
            "role": "creator",
            "judgment_model": "judgment",
            "vision_model": "vision",
            "judgment_api_url": API_URL,
            "vision_api_url": API_URL,
        }
        path = root / "cache.json"
        cache._write_cache_result(
            path,
            {**key, "fact_result": facts, "stage_fact_artifacts": {name: artifact}},
        )
        return path, key, facts

    def test_real_unresolved_cache_reuse_does_not_requalify_recover_or_charge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, key, facts = self.fixture(root)
            original = path.read_bytes()
            analysis = {"videos": {"creator": {"duration_seconds": 6}}}
            self.assertTrue(
                cache._is_valid_video_fact_cache("creator", facts, analysis)
            )
            budget = ResourceBudget(ResourceLimits(max_llm_calls=0))
            args = SimpleNamespace(llm_dry_run=False, _resource_budget=budget)
            token = budget.activate()
            try:
                with mock.patch.object(
                    pipeline, "_run_stage1_qualification"
                ) as qualification, mock.patch.object(
                    pipeline, "provider_call_with_artifact"
                ) as provider:
                    for repeat in range(2):
                        output = root / f"restore-{repeat}"
                        output.mkdir()
                        reused = pipeline._load_cached_video_facts(
                            args=args,
                            analysis=analysis,
                            run_dir=output,
                            api_key="",
                            role="creator",
                            cache_path=path,
                            cache_key=key,
                            replay_source=None,
                        )
                        self.assertIsNotNone(reused)
                        self.assertEqual(
                            reused["stage1_recovery"]["unresolved_stages"], ["S6"]
                        )
                        self.assertEqual(
                            reused["stage_evidence_checks"][5]["status"], "unknown"
                        )
                        self.assertTrue(
                            cache._is_valid_video_fact_cache(
                                "creator", reused, analysis
                            )
                        )
                        self.assertTrue(
                            (output / "stage1_provider_creator_A.json").is_file()
                        )
                    qualification.assert_not_called()
                    provider.assert_not_called()
            finally:
                budget.deactivate(token)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(budget.llm_calls, 0)
            self.assertEqual(budget.api_events, [])

    def test_cache_identity_or_integrity_mismatch_is_a_miss_not_a_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, key, _ = self.fixture(root)
            original = path.read_bytes()

            def validator(value):
                return cache._is_valid_video_fact_cache(
                    "creator", value, {"videos": {"creator": {"duration_seconds": 6}}}
                )

            self.assertIsNotNone(
                cache._read_cache_record(path, "fact_result", key, validator)
            )
            for field in key:
                with self.subTest(field=field):
                    changed = {**key, field: "different"}
                    self.assertIsNone(
                        cache._read_cache_record(
                            path, "fact_result", changed, validator
                        )
                    )
                    self.assertEqual(path.read_bytes(), original)
            for case in (
                "status",
                "fact_bytes",
                "provider_bytes",
                "snapshot",
                "recovery_scope",
            ):
                with self.subTest(case=case):
                    record = json.loads(original)
                    if case == "status":
                        record["completion_status"] = "failed"
                    elif case == "fact_bytes":
                        record["fact_result"]["evidence_units"][0][
                            "visual_fact"
                        ] = "changed"
                    elif case == "provider_bytes":
                        record["stage_fact_artifacts"][
                            "stage1_provider_creator_A.json"
                        ]["provider_response"] = {}
                    else:
                        if case == "snapshot":
                            record["fact_result"].pop("evidence_set_sha256")
                        else:
                            record["fact_result"]["stage1_recovery"][
                                "target_stages"
                            ] = ["S5"]
                        cache._write_cache_result(path, record)
                        record = json.loads(path.read_text())
                    write_json(path, record)
                    before = path.read_bytes()
                    self.assertIsNone(
                        cache._read_cache_record(path, "fact_result", key, validator)
                    )
                    self.assertEqual(path.read_bytes(), before)
            path.write_bytes(original)
            self.assertIsNotNone(
                cache._read_cache_record(path, "fact_result", key, validator)
            )

    def test_strict_replay_and_dry_run_cannot_be_satisfied_by_fact_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, key, _ = self.fixture(root)
            for dry_run, replay_source in (
                (True, None),
                (False, root / "replay-source"),
            ):
                with self.subTest(dry_run=dry_run, replay_source=replay_source):
                    output = root / str(dry_run)
                    output.mkdir()
                    with mock.patch.object(pipeline, "_read_cache_record") as read:
                        result = pipeline._load_cached_video_facts(
                            args=SimpleNamespace(llm_dry_run=dry_run),
                            analysis={},
                            run_dir=output,
                            api_key="",
                            role="creator",
                            cache_path=path,
                            cache_key=key,
                            replay_source=replay_source,
                        )
                    self.assertIsNone(result)
                    read.assert_not_called()
                    self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
