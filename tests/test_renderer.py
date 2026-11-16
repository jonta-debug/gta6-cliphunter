from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

from cliphunter.renderer import VerticalRenderer


def generate_clip(
    path: Path, *, duration: float = 2.0, size: str = "640x360", rate: int = 24
) -> None:
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
        "-f", "lavfi", "-i", "sine=frequency=523:sample_rate=48000",
        "-t", str(duration), "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(path),
    ], check=True)


@pytest.mark.parametrize(("size", "rate"), [("640x360", 24), ("360x640", 30)])
def test_clean_gaming_render_is_vertical_playable_and_preserves_source(tmp_path, size, rate):
    source = tmp_path / "source.mp4"
    output = tmp_path / "rendered" / "vertical.mp4"
    generate_clip(source, size=size, rate=rate)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    renderer = VerticalRenderer()

    asyncio.run(renderer.render(source, output, preset="clean_gaming"))
    source_probe = asyncio.run(renderer.probe(source))
    output_probe = asyncio.run(renderer.probe(output))

    assert output.exists()
    assert (output_probe.width, output_probe.height) == (1080, 1920)
    assert output_probe.video_codec == "h264"
    assert output_probe.audio_codec == "aac"
    assert output_probe.duration == pytest.approx(source_probe.duration, abs=0.5)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert source != output
