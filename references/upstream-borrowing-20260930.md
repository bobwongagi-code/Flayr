# Minimal Upstream Borrowing

## State and Scope

Implemented as `5748467`, mechanically refrozen as `a01feb2`, and integrated
into local main as `498a4b6` on 2026-10-01. No accuracy promotion is claimed.
The 24 pre-existing working files (semantic candidates and historical notes)
are preserved in local branch `archive/main-wip-20261001` at `fc086e99`;
content and file modes matched 24/24 before removal from the active worktree.
They are not included in the integrated runtime. Existing review artifacts
and ignored diagnostic directories remain unchanged. No push was performed.
No new dependencies, services, provider requests, video-analysis runs, model
routes, qualification thresholds, or gap definitions were introduced.

| Reference | Applied here | Deliberately not copied |
| --- | --- | --- |
| [creator-lab](https://github.com/artemnovitckii/creator-lab) | Read-only lookup of reviewed stages, separating model and human provenance | Collection, classification, performance ranking, databases, automatic prompt examples |
| [claude-real-video](https://github.com/HUANGCHIHHUNGLeo/claude-real-video) | Tests for window seeking, original timestamps, and unknown timestamps on incomplete logs | Replacement extraction, local ASR, new frame-selection thresholds |
| [video-use](https://github.com/browser-use/video-use) | Record the actual selected audio stream and separate speech diagnostics from routing | Default track changes, silence thresholds, audio-first analysis, extra critic calls |

## Media Diagnostics

`extract_audio` records `audio_stream_index` only after successful extraction
and an unambiguous FFmpeg mapping log. It is the container input stream index,
not the ordinal for `-map 0:a:N`. No `-map` is added; missing, ambiguous, or
truncated mapping information leaves the index unknown.

Speech-mode results retain their existing mode, evidence priorities, reasons,
and provider-facing prompt text. Two diagnostic fields are added:

- `transcription_status`: the original status, including failure or missing status.
- `speech_evidence_status`: `present`, `verified_absent`, `non_speech_label`, or
  `unknown`. `present` follows the existing transcript check, not a new factual
  guarantee. `verified_absent` requires the existing source-hash-bound human
  attestation. A music label is not verified absence; empty/failed transcription
  is not evidence of silence or music.

Window extraction was already correct in the audited path; it was not rewritten.
Tests check input-side seek/duration and source-time offsets. Missing timestamps
stay unknown. The separate native-video helper still expands positive sub-100ms
durations to 100ms; audited ordinary Phase C windows are at least 500ms, so that
helper was left unchanged. These checks do not prove semantic recall.

## Usage

Run `scripts/lookup_reviewed_cases.py` with one or more explicit completed run directories. Repeat `--run-dir` for each directory:

```sh
python3 scripts/lookup_reviewed_cases.py \
  --run-dir runs/example-a \
  --run-dir runs/example-b
```

Use `--stage S1` through `--stage S6` to limit the displayed stage. Use `--query TEXT` to retain cases whose displayed text contains the exact, case-sensitive substring `TEXT`:

```sh
python3 scripts/lookup_reviewed_cases.py \
  --run-dir runs/example-a \
  --stage S4 \
  --query "效果证据不足"
```

The command validates each completion manifest and hash-bound human-review sidecar using the existing review APIs. It prints the source run path, `draft_visible` review mode, stage, model judgment and reason, human decision and note, and evidence IDs. Only final human decisions are shown; pending stages are omitted. Results are written to stdout, with no files created or changed. The output explicitly marks the review as nonblind and not evidence of model accuracy.

## Limitations

- This implementation is available in local main; integration is not production deployment or accuracy acceptance. Provide each run directory explicitly.
- Every run must be named explicitly; the command does not recursively scan directories. Any stale, malformed, or otherwise invalid run fails the lookup with a path-specific error rather than being skipped.
- `--query` is literal substring filtering over displayed fields. It does not perform fuzzy search, relevance scoring, ranking, or automatic exemplar selection.
- The lookup does not display performance metrics, compare case performance, infer which case is best, promote a human-reviewed judgment to model-accuracy evidence, modify reviews, rerun analysis, or call a model.

## Verification

- At engineering commit `5748467`, the full quality gate passed, including
  dependency audit, secret scan and media-tool smoke; coverage was 79%.
- After mechanical refreeze, all 1,046 unit tests and the frozen-source
  verifier passed. The earlier pre-refreeze identity mismatch remains in
  history; it was not reclassified as a passing check.
- On integrated main `498a4b6`, the existing `scripts/run-tests.sh` project
  checks and the frozen-source verifier passed. No dependency audit was repeated.
- Real read-only Yoodex S4 lookup on main returned model
  `benchmark_better / small` separately from human `not_applicable`.
  SHA256 values of analysis, completion manifest and review sidecar were
  identical before and after lookup.
- No model-accuracy gain, no-speech detector accuracy, or production acceptance
  is claimed.
