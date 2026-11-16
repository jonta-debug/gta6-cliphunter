"""Replaceable media-source resolution for capture workers."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Protocol


class MediaSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaSource:
    url: str
    label: str


class MediaSourceResolver(Protocol):
    async def resolve(self, channel: str) -> MediaSource: ...


class YtDlpTwitchResolver:
    """Resolve public, playable Twitch streams without cookies or bypass flags."""

    def __init__(self, executable: str = "yt-dlp", *, timeout_seconds: float = 30.0) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    async def resolve(self, channel: str) -> MediaSource:
        login = channel.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
            raise MediaSourceError("invalid Twitch channel login")
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "--no-config",
            "--no-playlist",
            "--format",
            "best",
            "--get-url",
            f"https://www.twitch.tv/{login}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise MediaSourceError("yt-dlp stream resolution timed out") from error
        if process.returncode != 0:
            raise MediaSourceError(
                f"yt-dlp could not resolve an accessible Twitch stream (exit {process.returncode})"
            )
        urls = [line.strip() for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
        if len(urls) != 1 or not urls[0].startswith(("https://", "http://")):
            raise MediaSourceError("yt-dlp did not return one playable HTTP media URL")
        return MediaSource(url=urls[0], label=f"twitch:{login}")


class StaticMediaSourceResolver:
    """Resolver for generated fixtures and explicitly supplied local media."""

    def __init__(self, source: MediaSource) -> None:
        self.source = source

    async def resolve(self, channel: str) -> MediaSource:
        del channel
        return self.source
