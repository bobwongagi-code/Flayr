#!/usr/bin/env python3
"""Exercise the local media-tool boundary used by the analysis pipeline."""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.video import probe_duration_seconds  # noqa: E402


def _run(
    command: list[str], *, timeout_seconds: float = 45.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    curl = shutil.which("curl")
    missing = [
        name
        for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe), ("curl", curl))
        if not path
    ]
    if missing:
        print(f"tool smoke failed: missing {', '.join(missing)}", file=sys.stderr)
        return 1

    assert ffmpeg is not None
    assert ffprobe is not None
    assert curl is not None
    with tempfile.TemporaryDirectory(prefix="flayr-ci-smoke-") as tmp:
        video_path = Path(tmp) / "smoke.mp4"
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=32x32:r=10:d=0.4",
                "-an",
                "-c:v",
                "mpeg4",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(video_path),
            ]
        )
        if video_path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("ffmpeg smoke output exceeded the expected size")
        duration = probe_duration_seconds(video_path)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise RuntimeError(
                f"project ffprobe integration returned invalid duration: {duration!r}"
            )
    _run([curl, "--version"], timeout_seconds=10.0)
    print("media-tool smoke passed: ffmpeg -> project ffprobe -> curl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
