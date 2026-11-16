"""Modular FFmpeg rendering for vertical short-form video."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaProbe:
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None


class RenderPreset(Protocol):
    name: str
    width: int
    height: int

    def video_filter(self) -> str: ...


@dataclass(frozen=True)
class CleanGamingPreset:
    name: str = "clean_gaming"
    width: int = 1080
    height: int = 1920

    def video_filter(self) -> str:
        return (
            "[0:v:0]split=2[background][foreground];"
            "[background]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1,boxblur=20:1[blurred];"
            "[foreground]scale=1080:1920:force_original_aspect_ratio=decrease,"
            "setsar=1[gameplay];"
            "[blurred][gameplay]overlay=(W-w)/2:(H-h)/2:shortest=1,"
            "format=yuv420p[video]"
        )


DEFAULT_PRESETS: Mapping[str, RenderPreset] = {
    "clean_gaming": CleanGamingPreset(),
}


class VerticalRenderer:
    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        presets: Mapping[str, RenderPreset] | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.presets = dict(presets or DEFAULT_PRESETS)

    async def probe(self, path: str | Path) -> MediaProbe:
        process = await asyncio.create_subprocess_exec(
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RenderError("FFprobe rejected the media file")
        try:
            data = json.loads(stdout)
            video = next(
                stream for stream in data["streams"] if stream.get("codec_type") == "video"
            )
            audio = next(
                (stream for stream in data["streams"] if stream.get("codec_type") == "audio"),
                None,
            )
            probe = MediaProbe(
                duration=float(data["format"]["duration"]),
                width=int(video["width"]),
                height=int(video["height"]),
                video_codec=str(video["codec_name"]),
                audio_codec=str(audio["codec_name"]) if audio else None,
            )
        except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RenderError("FFprobe returned incomplete media metadata") from error
        if probe.duration <= 0 or probe.width <= 0 or probe.height <= 0:
            raise RenderError("media has invalid duration or dimensions")
        return probe

    async def render(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        preset: str = "clean_gaming",
    ) -> Path:
        source = Path(input_path)
        output = Path(output_path)
        if source.resolve() == output.resolve():
            raise RenderError("rendered output must not overwrite the source clip")
        selected = self.presets.get(preset)
        if selected is None:
            raise RenderError(f"unknown rendering preset: {preset}")
        source_probe = await self.probe(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp.mp4")
        try:
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-filter_complex",
                selected.video_filter(),
                "-map",
                "[video]",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "faster",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-fps_mode",
                "vfr",
                "-movflags",
                "+faststart",
                "-shortest",
                "-y",
                str(temporary),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                return_code = await process.wait()
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            if return_code != 0:
                raise RenderError("FFmpeg vertical rendering failed")
            rendered_probe = await self.probe(temporary)
            self._validate_render(source_probe, rendered_probe, selected)
            temporary.replace(output)
            return output
        finally:
            temporary.unlink(missing_ok=True)

    async def validate(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        preset: str = "clean_gaming",
    ) -> None:
        selected = self.presets.get(preset)
        if selected is None:
            raise RenderError(f"unknown rendering preset: {preset}")
        self._validate_render(
            await self.probe(input_path), await self.probe(output_path), selected
        )

    @staticmethod
    def _validate_render(
        source: MediaProbe,
        rendered: MediaProbe,
        preset: RenderPreset,
    ) -> None:
        if (rendered.width, rendered.height) != (preset.width, preset.height):
            raise RenderError("rendered video has incorrect dimensions")
        if rendered.video_codec != "h264":
            raise RenderError("rendered video is not H.264")
        if source.audio_codec is not None and rendered.audio_codec != "aac":
            raise RenderError("rendered video did not preserve an AAC audio stream")
        tolerance = max(0.5, source.duration * 0.03)
        if abs(rendered.duration - source.duration) > tolerance:
            raise RenderError("rendered duration differs from the source clip")
