# Dependencies

## Runtime

Flayr's core orchestration uses the standard library. Image-evidence helpers
also use optional Pillow for decoding, resizing and rendering. Media processing
uses these environment tools:

- `ffmpeg` and `ffprobe`: required for media probing, frame extraction and audio extraction.
- Online Fun-ASR access through the configured Beijing MaaS endpoint and the
  Qwen/DashScope API key. `curl` is used for the HTTPS request; no local ASR
  executable or model file is required.

The actual executable paths and reported versions are recorded in each run's
preprocessing fingerprint. A missing optional tool produces an explicit
`degraded` status; it must not create placeholder evidence.

## Development and CI

Development/CI tools and the optional image implementation are pinned in
[`requirements-dev.lock`](requirements-dev.lock):

- `Pillow==13.0.0`: contact sheets and visual evidence artifacts.
- `ruff==0.16.0`: focused CI lint gate for new errors and undefined names.
- `black==26.10.1`: formatting gate for maintained CI helpers.
- `mypy==2.3.0`: focused static typing gate for maintained contract modules.
- `bandit==1.9.4`: high-confidence security lint gate.
- `coverage==7.15.2`: test coverage measurement with a 55% minimum gate.
- `pip-audit==2.10.1`: dependency vulnerability audit.

The repository predates full-repository Black and strict typing. CI therefore
checks the maintained CI helpers and selected contract modules, while the
focused Ruff gate catches syntax errors, undefined names and new high-risk
lint failures across the source tree.

Pillow is optional for the core analysis path. The project does not carry the
removed voice-cloning or video-generation SDK dependencies.

Install these pins in the environment used to run image helpers. Updating the
lock file does not replace packages in a separate system Python environment.

## Upgrade policy

Dependency changes must update the lock file in the same change, run the full
test and contract checks, and record the reason in `CHANGELOG.md`. Open-ended
version ranges are not accepted in committed dependency files.
