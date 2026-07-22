---
name: flayr
description: Analyze TikTok commerce videos, compare a benchmark video with a creator video, and produce a practical improvement package with extracted frames, transcript artifacts, structured recommendations, and an HTML report.
---

# Flayr

Use this skill when the user wants to analyze, compare, or improve TikTok commerce short videos for GMV-oriented creator coaching.

The product principle is simple: do not hand creators a long text report. Produce a visual, concrete improvement package that operations teams can use and creators can understand by watching.

## Modes

- `breakdown`: analyze one benchmark video and explain why it works.
- `compare`: compare one benchmark video with one creator video and identify concrete gaps.
- `improve`: compare videos, select the top 3 to 5 GMV-oriented improvements, and generate the improvement package.

## Default Command

Run the bundled script:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/Flayr/scripts/flayr.py" improve \
  --benchmark-video "/path/to/benchmark.mp4" \
  --creator-video "/path/to/creator.mp4" \
  --product-name "Product name" \
  --product-price "39" \
  --whisper-model "/path/to/ggml-large-v3-turbo-q5_0.bin"
```

When working from this repository directly, use:

```bash
python3 scripts/flayr.py improve \
  --benchmark-video "/path/to/benchmark.mp4" \
  --creator-video "/path/to/creator.mp4"
```

## Workflow

`scripts/flayr.py` is the skill harness: it owns CLI parsing, run-directory setup, orchestration, and output wiring. Core responsibilities live under `scripts/flayr_core/`: video evidence extraction, optional transcription/translation, LLM analysis, and report rendering.

1. Confirm the requested mode and local video paths.
2. Run dependency checks for `ffmpeg` and a Whisper command.
3. Extract one frame per second and a WAV audio file for each input video.
4. Extract denser 2 fps focus frames for the first 5 seconds and final 5 seconds, and write `focus_frames/manifest.json` with timestamps.
   Also write `frames/manifest.json` and `frames/stage_frames.json` so S1-S6 diagnosis has full-funnel visual evidence.
5. Transcribe speech in the detected local language when Whisper is available, and keep a Chinese translation beside it.
   Use `--translate-with-llm` when the Chinese translation should be generated automatically.
6. Classify `speech_mode`: `spoken`, `subtitle_driven`, `visual_driven`, or `music_driven`.
   Use `transcript_packed` / `transcript.srt` as the primary spine only for `spoken` videos. For no-speech videos, use OCR subtitles, visual changes, timeline views, shot tracks, and audio rhythm instead.
7. Generate secondary evidence artifacts from those assets: `frames/selection_report.*`, `contact_sheets/`, `timeline_views/`, `transcript_packed.*`, and `video_evidence_audit.json`.
   These artifacts are audit aids, not direct scoring inputs. Stage1 visual payloads should prefer Hook/CTA timeline views before raw frames when available.
8. Generate `analysis_input.md` for large-model diagnosis.
9. If `--llm-model` is provided, call the configured OpenAI-compatible chat endpoint to generate `analysis_result.json`.
   Use `--llm-include-images` when the model should inspect Hook/CTA focus frames directly; keep `--llm-image-limit` modest, such as 8 to 12 images.
   Validate the model JSON against the schema. Shared deterministic normalization resolves mechanical contradictions; the configured provider profile controls any supported repair path. Facts, evidence references, proposition references, and severity remain immutable during repair.
10. If a large-model result exists, pass it with `--analysis-result-json` and merge it back into the report.
11. Analyze videos through the 6-slot commerce structure: Hook, product intro, usage, result, trust, CTA.
12. For compare/improve mode, produce stage-level gaps and top 3 to 5 improvements.
13. Generate output under a timestamped run directory:
   - `analysis.json`
   - `analysis_input.md`
   - `report.html`
   - extracted frames and audio
   - contact sheets and timeline views
   - local-language `transcript.txt`
   - Chinese `transcript.zh.txt`
14. Do not render or synthesize a replacement video. Keep the report grounded in the original media, evidence frames, local audio checks, and executable improvement suggestions.

## Analysis Rules

- Keep advice GMV-oriented, not aesthetic for its own sake.
- Keep creator-facing wording plain and concrete.
- Limit final improvement points to 3 to 5.
- Every improvement must name the time range, current problem, benchmark reference, exact suggested change, and GMV reason.
- Prefer changes with high leverage and low execution difficulty.
- Do not recommend unverifiable claims, medical claims, or competitor attacks.
- Never use English-style space splitting or word count to decide whether a transcript has valid speech. Thai and other no-space languages can be valid even when token count is low.

## Structure Library

Use `structure_library_full.md` as the source of truth for short-video structure:

- S1 Hook
- S2 product intro
- S3 usage process
- S4 result presentation
- S5 trust amplification
- S6 CTA

When asking a large model to understand or analyze a video, explicitly tell it that the S1-S6 structure comes from `structure_library_full.md`. The model should apply that structure library, not invent a new funnel.

When choosing modules, apply the material tags and fallback rules from the library instead of inventing new structures.

## Dependency Handling

The script checks dependencies but does not install them automatically.

Expected tools:

- `ffmpeg`: frame/audio evidence extraction
- `whisper`, `whisper-cpp`, or `whisper-cli`: speech transcription

If optional dependencies are missing, record an explicit `degraded` status and continue only for outputs that do not require them. A requested LLM call, response parse, or schema failure is blocking and must return nonzero. Compare/improve without completed LLM analysis also fails by default; use `--allow-degraded` only when that state is intentional.

For `whisper-cli` or `whisper-cpp`, pass `--whisper-model` when the default `models/ggml-base.en.bin` is not available.

For Flayr model analysis, use a configured OpenAI-compatible vision-language model and endpoint supplied by the runtime environment. Do not put credentials, personal Keychain names, or private machine paths in a committed job manifest.

Subtitle OCR runs in `--ocr-mode auto` by default and reuses the configured visual model. Disable it with `--no-ocr` for fast local debugging. OCR improves on-screen subtitle grounding and is low cost, but it adds per-frame API latency.

Default to `--whisper-language auto`. Southeast Asia commerce videos often use Malay, Thai, Indonesian, or English local口播, so do not force Chinese unless the user explicitly says the video is Chinese.

When generating `transcript.zh.txt`, follow `references/commerce-translation-guidelines.md`. The Chinese translation must be commerce-aware: correct likely Whisper mistakes from product context, preserve product facts, and translate purchase calls such as `beg kuning` / `back kuning` as `黄色购物车`.
For model analysis, require evidence: every stage and improvement should cite time range, visual evidence, or spoken evidence. The HTML report should expose key frames, not just text.
Do not treat the S1-S6 reference times as fixed cuts. The model must first understand the full video, then write the actual `time_range` for each stage. The HTML report should match benchmark and creator frames to those actual ranges and show the frames inside the stage comparison.

## Output Contract

For normal work, summarize:

- where the run directory was created
- whether frame/audio extraction succeeded
- whether dense Hook/CTA focus frames were created
- whether transcription succeeded or was skipped
- whether the Chinese translation file exists
- whether LLM analysis was generated or merged
- the top improvements selected
- whether the report has complete evidence and grounded improvement suggestions

Do not over-explain the framework to creators. The HTML report is for operations and creator coaching.
