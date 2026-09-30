# Minimal Upstream Borrowing

## State and Scope

Implemented locally on the clean delivery checkout based on `de21928`. The
upcoming commit is engineering-only; mechanical refreeze and main integration
are pending, and no accuracy promotion is claimed. The mixed main working tree
and existing review artifacts remain unchanged.
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

- This implementation lives in the diagnostics clean checkout at baseline `de21928`; it is not deployed production code. Run the script from this checkout and provide each run directory explicitly.
- Every run must be named explicitly; the command does not recursively scan directories. Any stale, malformed, or otherwise invalid run fails the lookup with a path-specific error rather than being skipped.
- `--query` is literal substring filtering over displayed fields. It does not perform fuzzy search, relevance scoring, ranking, or automatic exemplar selection.
- The lookup does not display performance metrics, compare case performance, infer which case is best, promote a human-reviewed judgment to model-accuracy evidence, modify reviews, rerun analysis, or call a model.

## Verification

- 91 related tests passed across lookup, human review, video extraction, speech
  diagnostics, no-product-speech attestation, frame evidence, audio layers, and
  acquisition windows.
- Changed-scope Ruff checks using the existing CI rule selection and
  `git diff --check` passed. Full quality/dependency gates were not rerun.
- Real Yoodex S4 lookup returned model `benchmark_better / small` separately
  from human `not_applicable`, with the existing note and evidence IDs.
- The frozen-source verifier rejects `speech_mode.py` and `video.py` drift as
  expected. The old freeze record was not rewritten; these changes must not
  claim its source identity.
- No model-accuracy gain, no-speech detector accuracy, or production acceptance
  is claimed.
