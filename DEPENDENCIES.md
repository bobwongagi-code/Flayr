# Dependencies

## Runtime

Flayr's Python runtime uses only the standard library. Media processing uses
these environment tools:

- `ffmpeg` and `ffprobe`: required for media probing, frame extraction and audio extraction.
- `whisper`, `whisper-cpp` or `whisper-cli`: optional local transcription.

The actual executable paths and reported versions are recorded in each run's
preprocessing fingerprint. A missing optional tool produces an explicit
`degraded` status; it must not create placeholder evidence.

## Development and CI

The development-only dependency is pinned in
[`requirements-dev.lock`](requirements-dev.lock):

- `Pillow==11.3.0`: contact sheets and visual evidence artifacts.

Pillow is optional for the core analysis path. The project does not carry the
removed voice-cloning or video-generation SDK dependencies.

## Upgrade policy

Dependency changes must update the lock file in the same change, run the full
test and contract checks, and record the reason in `CHANGELOG.md`. Open-ended
version ranges are not accepted in committed dependency files.
