from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from cliphunter.media import MediaSource, MediaSourceError, StaticMediaSourceResolver, YtDlpTwitchResolver
from cliphunter.media_worker import MediaWorker
from cliphunter.models import Candidate
from cliphunter.persistence import SQLiteCandidateStore
from cliphunter.renderer import RenderError, VerticalRenderer
from cliphunter.rolling_buffer import (
    BufferRangeUnavailable,
    RollingBuffer,
    RollingBufferError,
    Segment,
)


def generate_media(path: Path, duration: float = 12.0) -> None:
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(duration), "-c:v", "libx264", "-preset", "ultrafast",
        "-g", "50", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-y", str(path),
    ], check=True)


def test_static_resolver_and_twitch_login_validation(tmp_path):
    source = MediaSource("/tmp/generated.mp4", "fixture")
    assert asyncio.run(StaticMediaSourceResolver(source).resolve("ignored")) == source

    with pytest.raises(MediaSourceError, match="invalid Twitch channel"):
        asyncio.run(YtDlpTwitchResolver().resolve("https://twitch.tv/not-a-login"))

    fake_ytdlp = tmp_path / "yt-dlp"
    fake_ytdlp.write_text("#!/bin/sh\nprintf 'https://media.example/live.m3u8\\n'\n")
    fake_ytdlp.chmod(0o700)
    resolved = asyncio.run(YtDlpTwitchResolver(str(fake_ytdlp)).resolve("Test_Channel"))
    assert resolved == MediaSource("https://media.example/live.m3u8", "twitch:test_channel")


def test_playlist_timestamps_and_discontinuity_are_preserved(tmp_path):
    buffer = RollingBuffer(MediaSource("fixture", "fixture"), tmp_path)
    first = tmp_path / "first.ts"
    second = tmp_path / "second.ts"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    buffer.playlist.write_text(
        "#EXTM3U\n"
        "#EXT-X-DISCONTINUITY\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-11-16T12:00:00.250+00:00\n"
        "#EXTINF:2.08,\nfirst.ts\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-11-16T12:00:02.330+00:00\n"
        "#EXTINF:1.92,\nsecond.ts\n",
        encoding="utf-8",
    )

    buffer._ingest_playlist()

    assert len(buffer.segments) == 2
    assert buffer.segments[0].start == pytest.approx(1794830400.25)
    assert buffer.segments[0].duration == pytest.approx(2.08)
    assert buffer.segments[0].discontinuity_before
    assert not buffer.segments[1].discontinuity_before


def test_empty_and_temporary_segments_are_excluded(tmp_path):
    buffer = RollingBuffer(MediaSource("fixture", "fixture"), tmp_path)
    empty = tmp_path / "empty.ts"
    temporary = tmp_path / "partial.ts.tmp"
    empty.write_bytes(b"")
    temporary.write_bytes(b"partial")
    buffer.playlist.write_text(
        "#EXTM3U\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-11-16T12:00:00+00:00\n"
        "#EXTINF:2.0,\nempty.ts\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-11-16T12:00:02+00:00\n"
        "#EXTINF:2.0,\npartial.ts.tmp\n",
        encoding="utf-8",
    )

    buffer._ingest_playlist()

    assert buffer.segments == ()


def test_invalid_finalized_segment_is_removed_before_extraction(tmp_path):
    path = tmp_path / "invalid.ts"
    path.write_bytes(b"not-video")
    buffer = RollingBuffer(MediaSource("fixture", "fixture"), tmp_path)
    segment = Segment(path, time.time(), 2, "session")
    buffer._segments[path] = segment

    with pytest.raises(BufferRangeUnavailable, match="incomplete segment"):
        asyncio.run(buffer._validate_segments([segment]))

    assert not path.exists()
    assert buffer.segments == ()


def test_cleanup_preserves_protected_expired_segments(tmp_path):
    buffer = RollingBuffer(MediaSource("fixture", "fixture"), tmp_path, retention_seconds=10)
    now = time.time()
    expired = tmp_path / "expired.ts"
    protected = tmp_path / "protected.ts"
    current = tmp_path / "current.ts"
    for path in (expired, protected, current):
        path.write_bytes(b"media")
    buffer._segments = {
        expired: Segment(expired, now - 30, 2, "session"),
        protected: Segment(protected, now - 25, 2, "session"),
        current: Segment(current, now - 5, 2, "session"),
    }
    buffer.protect("candidate", now - 26, now - 22)

    buffer._cleanup(now)

    assert not expired.exists()
    assert protected.exists()
    assert current.exists()
    assert set(buffer._segments) == {protected, current}


def test_generated_stream_candidate_produces_valid_mp4(tmp_path):
    source_path = tmp_path / "source.mp4"
    generate_media(source_path)
    store = SQLiteCandidateStore(tmp_path / "candidates.db")
    buffer = RollingBuffer(
        MediaSource(str(source_path), "generated"),
        tmp_path / "buffer",
        retention_seconds=120,
        segment_seconds=2,
        scan_interval_seconds=0.05,
    )
    worker = MediaWorker(
        buffer,
        store,
        "streamer-1",
        tmp_path / "clips",
        renderer=VerticalRenderer(),
        rendered_directory=tmp_path / "rendered",
        poll_interval_seconds=0.05,
        clip_wait_timeout_seconds=5,
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        async with asyncio.timeout(10):
            while len(buffer.segments) < 4:
                await asyncio.sleep(0.05)
            segments = buffer.segments
            clip_start = segments[0].start + 0.25
            clip_end = min(segments[-1].end - 0.25, clip_start + 4)
            candidate = Candidate(
                id="generated-candidate",
                streamer_id="streamer-1",
                event_timestamp=(clip_start + clip_end) / 2,
                hype_score=8.0,
                signals={"source": "generated-test"},
                clip_start=clip_start,
                clip_end=clip_end,
                detector_version="test",
            )
            store.save(candidate)
            output = tmp_path / "clips" / "generated-candidate.mp4"
            rendered = tmp_path / "rendered" / "clean_gaming" / "generated-candidate.mp4"
            while not rendered.exists() \
                    and candidate.id not in worker.errors \
                    and candidate.id not in worker.render_errors:
                await asyncio.sleep(0.05)
            assert candidate.id not in worker.errors
            assert candidate.id not in worker.render_errors
            await buffer.validate_clip(output)
            await worker.renderer.validate(output, rendered, preset="clean_gaming")
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        return output, rendered

    output, rendered = asyncio.run(scenario())

    assert output.exists()
    assert rendered.exists()
    assert output.stat().st_size > 0
    assert rendered.stat().st_size > 0
    assert not buffer.running
    assert "#EXTINF:" in buffer.playlist.read_text(encoding="utf-8")
    assert not list((tmp_path / "buffer").glob("*.tmp"))


def test_render_failure_keeps_extracted_clip_and_is_isolated(tmp_path):
    class FakeBuffer:
        async def extract_clip(self, _id, _start, _end, output, **_kwargs):
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"original-clip")

        async def validate_clip(self, _path):
            return None

    class FailingRenderer:
        async def render(self, _input, _output, **_kwargs):
            raise RenderError("expected render failure")

        async def validate(self, _input, _output, **_kwargs):
            raise RenderError("expected render failure")

    worker = MediaWorker(
        FakeBuffer(),
        SQLiteCandidateStore(tmp_path / "failure.db"),
        "streamer-1",
        tmp_path / "clips",
        renderer=FailingRenderer(),
        rendered_directory=tmp_path / "rendered",
    )
    candidate = Candidate(
        id="render-failure", streamer_id="streamer-1", event_timestamp=10,
        hype_score=8, signals={}, clip_start=5, clip_end=15, detector_version="test",
    )

    asyncio.run(worker._extract(candidate))

    assert (tmp_path / "clips" / "render-failure.mp4").read_bytes() == b"original-clip"
    assert candidate.id not in worker.errors
    assert worker.render_errors[candidate.id] == "expected render failure"


def test_worker_drains_candidate_during_shutdown(tmp_path):
    buffer_started = asyncio.Event()
    extraction_started = asyncio.Event()
    allow_extraction = asyncio.Event()

    class SlowBuffer:
        last_error = None

        async def start(self):
            buffer_started.set()

        async def stop(self):
            return None

        async def extract_clip(self, _id, _start, _end, output, **_kwargs):
            extraction_started.set()
            await allow_extraction.wait()
            Path(output).write_bytes(b"completed")

    store = SQLiteCandidateStore(tmp_path / "shutdown.db")
    candidate = Candidate(
        id="shutdown-drain", streamer_id="streamer-1", event_timestamp=10,
        hype_score=8, signals={}, clip_start=5, clip_end=15,
        detector_version="test",
    )
    worker = MediaWorker(
        SlowBuffer(), store, "streamer-1", tmp_path / "clips",
        poll_interval_seconds=0.01, shutdown_drain_timeout_seconds=1,
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(buffer_started.wait(), timeout=1)
        # Simulate the chat monitor's final synchronous evaluation immediately
        # before all components observe the shared shutdown event.
        store.save(candidate)
        stop.set()
        await asyncio.wait_for(extraction_started.wait(), timeout=1)
        allow_extraction.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    assert (tmp_path / "clips" / "shutdown-drain.mp4").read_bytes() == b"completed"
    assert not worker.errors


def test_worker_replaces_interrupted_original_and_rendered_artifacts(tmp_path):
    class RecoveringBuffer:
        async def validate_clip(self, _path):
            raise RollingBufferError("corrupt original")

        async def extract_clip(self, _id, _start, _end, output, **_kwargs):
            Path(output).write_bytes(b"recovered-original")

    class RecoveringRenderer:
        async def validate(self, _input, _output, **_kwargs):
            raise RenderError("corrupt render")

        async def render(self, _input, output, **_kwargs):
            Path(output).write_bytes(b"recovered-render")

    clips = tmp_path / "clips"
    rendered = tmp_path / "rendered" / "clean_gaming"
    clips.mkdir()
    rendered.mkdir(parents=True)
    (clips / "recover.mp4").write_bytes(b"partial-original")
    (rendered / "recover.mp4").write_bytes(b"partial-render")
    worker = MediaWorker(
        RecoveringBuffer(), SQLiteCandidateStore(tmp_path / "recover.db"),
        "streamer-1", clips, renderer=RecoveringRenderer(),
        rendered_directory=tmp_path / "rendered",
    )
    item = Candidate(
        id="recover", streamer_id="streamer-1", event_timestamp=10,
        hype_score=8, signals={}, clip_start=5, clip_end=15, detector_version="test",
    )

    asyncio.run(worker._extract(item))

    assert (clips / "recover.mp4").read_bytes() == b"recovered-original"
    assert (rendered / "recover.mp4").read_bytes() == b"recovered-render"
    assert not worker.errors and not worker.render_errors
