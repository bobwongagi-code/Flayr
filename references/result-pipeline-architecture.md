# Result Pipeline Ownership

Status: frozen implementation contract. This document describes the active
ADR-007 segmented path. The legacy whole-object import remains compatibility
only and must not gain new business behavior.

## Four durable layers

```text
Provider response
  -> Canonical analysis result
  -> Finalized analysis result
  -> Semantic report view
```

### Provider response

- Owns only the structured response returned by one provider request.
- Stage2 group responses are stored as `stage2_provider_<GROUP>.json` before
  projection or normalization.
- A completed response may be replayed only when its complete request identity
  matches: group, model, endpoint and payload digest.
- Missing or mismatched data is a semantic rerun. It must never be silently
  combined with completed responses from a different request identity.

### Canonical analysis result

- `normalize_analysis_result` is the only provider-to-domain normalization.
- Provider aliases, textual time formats and provider-specific optional fields
  are absorbed here.
- Validation that requires external context may run before the snapshot is
  accepted, but it must not reinterpret an already canonical field later.
- `validated_normalized_result.json` is immutable. Finalization always mutates
  a detached copy represented by `CanonicalAnalysisResult`.

### Finalized analysis result

- `finalize_canonical_analysis_result` is the only Canonical-to-Finalized
  boundary and never calls a provider.
- Evidence gate, comparison scope, resolver and publishability remain separate
  testable functions. The finalizer alone writes their decisions into the
  final result.
- Code-only changes are verified with `scripts/replay_finalization.py`; video,
  ASR and provider calls are forbidden in this replay.

### Semantic report view

- `SemanticAnalysis` is the allowlisted read-only projection consumed by all
  reports.
- Report renderers may create presentation labels but may not change evidence,
  severity, comparison status or run completion.

## Critical field ownership

| Field family | Canonical source | Final writer | Consumers |
|---|---|---|---|
| Stage semantic response (`stage_state`, `relation`, `model_gap_magnitude`, `judgment_reason`) | Stage2 provider adapter | Canonical normalization | Finalizer, evaluation |
| Stage evidence references | Explicit Stage2 IDs filtered against frozen Stage1 ledger | Canonical normalization | Gate, resolver, reports, evaluation |
| `stage_handoff_status` | Stage2 evidence projection | Canonical normalization | Evidence gate only |
| `stage_evidence_gate`, final `analysis_status` | Frozen Stage1 ledger plus comparison scope | Evidence-gate submodule through finalizer | Resolver, reports, evaluation |
| `severity`, `severity_derivation` | Canonical model magnitude plus typed facts | Resolver through finalizer | Reports, evaluation |
| `stage_evidence_links` | Canonical stage references | Deterministic projector through finalizer | Validation, reports |
| `stage2_candidate_status` | Provider group records | Stage2 orchestrator | Finalizer only |
| `stage2_pipeline_status` | Group records plus finalized stage states | Finalizer | Run state, manifest, reports |
| Report payload | Finalized analysis | Semantic/view adapters | HTML renderers only |

## Repository-wide ownership audit

Run:

```bash
python3 scripts/audit_result_field_ownership.py
```

The command scans all tracked source and documentation paths except generated
run/output directories. It reports literal reads, writes and non-Python
references. A field or compatibility path may be deleted only after this audit
shows no unreviewed consumer.

The initial audit found multiple production write points for
`analysis_status`, `stage2_pipeline_status` and stage evidence references.
The segmented path now separates Canonical handoff/candidate states from final
publish states. The remaining top-level `analysis_status` assignment in the
comparison rejection path describes the whole analysis, not a stage; it must
not be interpreted as a second writer of `stage_analysis[*].analysis_status`.

## Retry semantics

- Technical replay: identical request identity; reuse the completed provider
  artifact and rerun only deterministic projection/finalization.
- Technical resume: identical completed groups are reused; only missing or
  failed groups may call the provider.
- Semantic rerun: any request-identity change. Results from the old identity
  cannot be merged into the new run.

## Verification order

1. Contract fixtures for each typed state.
2. Repeated Canonical-to-Finalized replay with byte-identical business output.
3. Frozen Stage1 plus saved Stage2 provider-response replay.
4. Fake-provider full lifecycle.
5. Ordinary samples.
6. Aavini boundary sample.

No live video or provider call is an acceptable substitute for steps 1-4.
