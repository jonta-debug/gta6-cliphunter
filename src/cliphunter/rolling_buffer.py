"""FFmpeg-backed rolling media buffer with protected clip ranges."""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .media import MediaSource


class RollingBufferError(RuntimeError):
    pass


class BufferRangeUnavailable(RollingBufferError):
    pass


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    duration: float
    session_id: str
    discontinuity_before: bool = False

    @property
    def end(self) -> float:
        return self.start + self.duration


class RollingBuffer:
    def __init__(
        self,
        source: MediaSource,
        directory: str | Path,
        *,
        retention_seconds: float = 120.0,
        segment_seconds: float = 2.0,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        scan_interval_seconds: float = 0.25,
    ) -> None:
        if retention_seconds <= 0 or segment_seconds <= 0 or scan_interval_seconds <= 0:
            raise ValueError("buffer durations must be positive")
        self.source = source
        self.directory = Path(directory)
        self.retention_seconds = retention_seconds
        self.segment_seconds = segment_seconds
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.scan_interval_seconds = scan_interval_seconds
        self.session_id = uuid.uuid4().hex[:12]
        self.playlist = self.directory / f"{self.session_id}.m3u8"
        self._segments: dict[Path, Segment] = {}
        self._protections: dict[str, tuple[float, float]] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stopping = False
        self.last_error = ""

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(sorted(self._segments.values(), key=lambda item: (item.start, str(item.path))))

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self._process is not None:
            raise RollingBufferError("rolling buffer already started")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._remove_temporary_segments()
        segment_pattern = self.directory / f"{self.session_id}-%Y%m%dT%H%M%S-%%05d.ts"
        playlist_segments = math.ceil(self.retention_seconds / self.segment_seconds) + 5
        environment = os.environ.copy()
        environment["TZ"] = "UTC"
        self._process = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            self.source.url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            str(self.segment_seconds),
            "-hls_list_size",
            str(playlist_segments),
            "-hls_segment_type",
            "mpegts",
            "-hls_flags",
            "program_date_time+temp_file+independent_segments+discont_start+second_level_segment_index",
            "-strftime",
            "1",
            "-hls_segment_filename",
            str(segment_pattern),
            str(self.playlist),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        self._watch_task = asyncio.create_task(self._watch())

    async def _discard_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while await self._process.stderr.readline():
            pass

    async def _watch(self) -> None:
        while not self._stopping:
            self._ingest_playlist()
            self._cleanup(time.time())
            if self._process is not None and self._process.returncode is not None:
                self._ingest_playlist()
                if self._process.returncode != 0:
                    self.last_error = f"FFmpeg capture exited with code {self._process.returncode}"
                return
            await asyncio.sleep(self.scan_interval_seconds)

    @staticmethod
    def _parse_program_time(value: str) -> float:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()

    def _ingest_playlist(self) -> None:
        try:
            lines = self.playlist.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return
        start: float | None = None
        duration: float | None = None
        discontinuity = False
        for line in lines:
            if line == "#EXT-X-DISCONTINUITY":
                discontinuity = True
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                try:
                    start = self._parse_program_time(line.split(":", 1)[1])
                except ValueError:
                    start = None
            elif line.startswith("#EXTINF:"):
                try:
                    duration = float(line.split(":", 1)[1].rstrip(","))
                except ValueError:
                    duration = None
            elif line and not line.startswith("#"):
                path = Path(line)
                if not path.is_absolute():
                    path = self.playlist.parent / path
                path = path.resolve()
                if path.name.endswith(".tmp"):
                    start = None
                    duration = None
                    discontinuity = False
                    continue
                try:
                    has_content = path.stat().st_size > 0
                except OSError:
                    has_content = False
                if start is not None and duration is not None and duration > 0 and has_content:
                    self._segments[path] = Segment(
                        path=path,
                        start=start,
                        duration=duration,
                        session_id=self.session_id,
                        discontinuity_before=discontinuity,
                    )
                start = None
                duration = None
                discontinuity = False

    def _remove_temporary_segments(self) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob("*.tmp"):
            try:
                path.unlink()
            except OSError:
                pass

    def protect(self, protection_id: str, start: float, end: float) -> None:
        if not protection_id or end <= start:
            raise ValueError("invalid protected range")
        self._protections[protection_id] = (start, end)

    def release(self, protection_id: str) -> None:
        self._protections.pop(protection_id, None)

    def _is_protected(self, segment: Segment) -> bool:
        return any(segment.end > start and segment.start < end for start, end in self._protections.values())

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.retention_seconds
        for path, segment in list(self._segments.items()):
            if segment.end >= cutoff or self._is_protected(segment):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self._segments.pop(path, None)

    def _select_range(self, start: float, end: float) -> list[Segment]:
        selected = [item for item in self.segments if item.end > start and item.start < end]
        if not selected or selected[0].start > start + 0.5 or selected[-1].end < end - 0.5:
            raise BufferRangeUnavailable("requested clip range is not fully present in the buffer")
        for previous, current in zip(selected, selected[1:]):
            gap = current.start - previous.end
            if gap > max(1.0, self.segment_seconds):
                raise BufferRangeUnavailable("requested clip range crosses missing media")
        return selected

    async def wait_for_range(self, start: float, end: float, *, timeout: float) -> list[Segment]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self._ingest_playlist()
            try:
                return self._select_range(start, end)
            except BufferRangeUnavailable:
                pass
            if self.last_error:
                raise RollingBufferError(self.last_error)
            if self._process is not None and self._process.returncode is not None:
                raise BufferRangeUnavailable("capture ended before the requested range was complete")
            if asyncio.get_running_loop().time() >= deadline:
                raise BufferRangeUnavailable("timed out waiting for post-roll media")
            await asyncio.sleep(self.scan_interval_seconds)

    @staticmethod
    def _concat_entry(path: Path) -> str:
        escaped = str(path).replace("'", "'\\''")
        return f"file '{escaped}'\n"

    async def extract_clip(
        self,
        protection_id: str,
        start: float,
        end: float,
        output: str | Path,
        *,
        wait_timeout: float = 90.0,
    ) -> Path:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.protect(protection_id, start, end)
        concat_path = self.directory / f"clip-{protection_id}.ffconcat"
        temporary = output_path.with_suffix(output_path.suffix + ".tmp.mp4")
        try:
            selected = await self.wait_for_range(start, end, timeout=wait_timeout)
            await self._validate_segments(selected)
            concat_path.write_text(
                "ffconcat version 1.0\n" + "".join(self._concat_entry(item.path) for item in selected),
                encoding="utf-8",
            )
            offset = max(0.0, start - selected[0].start)
            duration = end - start
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-ss",
                f"{offset:.6f}",
                "-i",
                str(concat_path),
                "-t",
                f"{duration:.6f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-avoid_negative_ts",
                "make_zero",
                "-y",
                str(temporary),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await process.wait() != 0:
                raise RollingBufferError("FFmpeg clip extraction failed")
            await self.validate_clip(temporary)
            temporary.replace(output_path)
            return output_path
        finally:
            self.release(protection_id)
            concat_path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

    async def _validate_segments(self, segments: list[Segment]) -> None:
        for segment in segments:
            process = await asyncio.create_subprocess_exec(
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(segment.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            valid = False
            if process.returncode == 0:
                try:
                    data = json.loads(stdout)
                    valid = any(
                        stream.get("codec_type") == "video"
                        for stream in data.get("streams", [])
                        if isinstance(stream, dict)
                    )
                except (json.JSONDecodeError, TypeError):
                    valid = False
            if valid:
                continue
            self._segments.pop(segment.path, None)
            segment.path.unlink(missing_ok=True)
            raise BufferRangeUnavailable("requested clip range contains an incomplete segment")

    async def validate_clip(self, path: str | Path) -> None:
        process = await asyncio.create_subprocess_exec(
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RollingBufferError("FFprobe rejected the extracted clip")
        try:
            data = json.loads(stdout)
            duration = float(data["format"]["duration"])
            stream_types = {stream.get("codec_type") for stream in data.get("streams", [])}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RollingBufferError("FFprobe returned incomplete clip metadata") from error
        if duration <= 0 or "video" not in stream_types:
            raise RollingBufferError("extracted clip has no playable video")

    async def stop(self) -> None:
        self._stopping = True
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except TimeoutError:
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except TimeoutError:
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass
                    await self._process.wait()
        self._ingest_playlist()
        self._remove_temporary_segments()
        for task in (self._watch_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._watch_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
