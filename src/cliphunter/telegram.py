"""Optional Telegram Bot API delivery and authorized review callbacks."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Candidate
from .reviews import Review, SQLiteReviewStore


class TelegramConfigurationError(ValueError):
    pass


class TelegramAPIError(RuntimeError):
    pass


class ReviewDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: str
    allowed_user_ids: frozenset[int]
    request_timeout_seconds: float = 45.0
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.bot_token or not self.chat_id or not self.allowed_user_ids:
            raise TelegramConfigurationError(
                "Telegram bot token, chat ID, and allowed user IDs are required"
            )
        if not self.chat_id.lstrip("-").isdigit():
            raise TelegramConfigurationError("TELEGRAM_CHAT_ID must be numeric")
        if self.request_timeout_seconds <= 0 or self.max_attempts < 1:
            raise TelegramConfigurationError("invalid Telegram retry configuration")

    @classmethod
    def from_env_optional(cls) -> "TelegramConfig | None":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        users_value = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        if not token and not chat_id and not users_value:
            return None
        if not token or not chat_id or not users_value:
            raise TelegramConfigurationError(
                "set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and TELEGRAM_ALLOWED_USER_IDS together"
            )
        try:
            users = frozenset(int(value.strip()) for value in users_value.split(",") if value.strip())
        except ValueError as error:
            raise TelegramConfigurationError(
                "TELEGRAM_ALLOWED_USER_IDS must contain numeric user IDs"
            ) from error
        return cls(bot_token=token, chat_id=chat_id, allowed_user_ids=users)


RequestCallable = Callable[
    [str, dict[str, str], Path | None], Awaitable[dict[str, Any]]
]


class TelegramBotAPI:
    def __init__(
        self,
        config: TelegramConfig,
        request: RequestCallable | None = None,
    ) -> None:
        self.config = config
        self._request = request or self._request_official

    async def _request_official(
        self, method: str, fields: dict[str, str], file_path: Path | None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_sync, method, fields, file_path)

    def _request_sync(
        self, method: str, fields: dict[str, str], file_path: Path | None
    ) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.config.bot_token}/{method}"
        headers: dict[str, str] = {}
        if file_path is None:
            body = urllib.parse.urlencode(fields).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            boundary = f"cliphunter-{uuid.uuid4().hex}"
            chunks: list[bytes] = []
            for name, value in fields.items():
                chunks.extend([
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ])
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="video"; filename="clip.mp4"\r\n',
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                file_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ])
            body = b"".join(chunks)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        timeout = self.config.request_timeout_seconds
        if method == "getUpdates":
            timeout = float(fields.get("timeout", "0")) + 10.0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise TelegramAPIError(f"Telegram {method} returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TelegramAPIError(f"Telegram {method} request failed") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TelegramAPIError(f"Telegram {method} returned invalid JSON") from error
        if not isinstance(data, dict):
            raise TelegramAPIError(f"Telegram {method} returned an invalid response")
        return data

    async def _call(
        self,
        method: str,
        fields: dict[str, str],
        file_path: Path | None = None,
    ) -> Any:
        delay = 0.25
        last_error: TelegramAPIError | None = None
        for attempt in range(self.config.max_attempts):
            try:
                response = await self._request(method, fields, file_path)
                if response.get("ok") is True:
                    return response.get("result")
                code = response.get("error_code")
                retryable = code == 429 or isinstance(code, int) and code >= 500
                error = TelegramAPIError(f"Telegram {method} returned API error {code}")
                if not retryable:
                    raise error
                last_error = error
                retry_after = response.get("parameters", {}).get("retry_after", delay)
                delay = min(float(retry_after), 5.0)
            except TelegramAPIError as error:
                last_error = error
            if attempt + 1 < self.config.max_attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        raise last_error or TelegramAPIError(f"Telegram {method} request failed")

    async def send_review(self, review: Review) -> tuple[str, int]:
        timestamp = datetime.fromtimestamp(
            review.event_timestamp, tz=timezone.utc
        ).isoformat(timespec="seconds")
        callback_prefix = f"review:{{action}}:{review.candidate_id}:{review.callback_token}"
        approve = callback_prefix.format(action="a")
        reject = callback_prefix.format(action="r")
        if max(len(approve.encode()), len(reject.encode())) > 64:
            raise TelegramAPIError("Telegram callback data exceeds 64 bytes")
        caption = (
            "GTA6 ClipHunter review\n"
            f"Channel: {review.streamer_label} ({review.streamer_id})\n"
            f"Hype score: {review.hype_score:.2f}/10\n"
            f"Highlight: {timestamp}\n"
            f"Duration: {review.clip_duration:.2f}s"
        )
        markup = {"inline_keyboard": [[
            {"text": "Approve", "callback_data": approve},
            {"text": "Reject", "callback_data": reject},
        ]]}
        result = await self._call("sendVideo", {
            "chat_id": self.config.chat_id,
            "caption": caption,
            "reply_markup": json.dumps(markup, separators=(",", ":")),
            "supports_streaming": "true",
        }, Path(review.rendered_path))
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramAPIError("Telegram sendVideo returned no message ID")
        chat = result.get("chat") if isinstance(result.get("chat"), dict) else {}
        return str(chat.get("id", self.config.chat_id)), result["message_id"]

    async def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = await self._call("getUpdates", {
            "offset": str(offset), "timeout": "5", "allowed_updates": '["callback_query"]'
        })
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self._call("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": text,
        })

    async def remove_buttons(self, chat_id: str, message_id: int) -> None:
        await self._call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": str(message_id),
            "reply_markup": '{"inline_keyboard":[]}',
        })


class ReviewCoordinator:
    def __init__(
        self,
        store: SQLiteReviewStore,
        telegram: TelegramBotAPI | None = None,
    ) -> None:
        self.store = store
        self.telegram = telegram

    async def complete(
        self,
        candidate: Candidate,
        *,
        streamer_label: str,
        original_path: str | Path,
        rendered_path: str | Path,
    ) -> Review:
        review = self.store.create_pending(
            candidate,
            streamer_label=streamer_label,
            clip_duration=candidate.clip_end - candidate.clip_start,
            original_path=original_path,
            rendered_path=rendered_path,
        )
        if self.telegram is None:
            return review
        claimed = self.store.claim_notification(candidate.id)
        if claimed is None:
            return self.store.get(candidate.id) or review
        try:
            chat_id, message_id = await self.telegram.send_review(claimed)
            self.store.mark_notified(candidate.id, chat_id, message_id)
        except TelegramAPIError as error:
            self.store.mark_notification_failed(candidate.id, str(error))
            raise ReviewDeliveryError(str(error)) from error
        return self.store.get(candidate.id) or review


class TelegramCallbackWorker:
    def __init__(
        self,
        config: TelegramConfig,
        api: TelegramBotAPI,
        store: SQLiteReviewStore,
    ) -> None:
        self.config = config
        self.api = api
        self.store = store

    async def handle_update(self, update: dict[str, Any]) -> str:
        query = update.get("callback_query")
        if not isinstance(query, dict):
            return "ignored"
        callback_id = str(query.get("id") or "")
        user = query.get("from") if isinstance(query.get("from"), dict) else {}
        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user_id = user.get("id")
        chat_id = str(chat.get("id") or "")
        if user_id not in self.config.allowed_user_ids or chat_id != self.config.chat_id:
            await self.api.answer_callback(callback_id, "Not authorized")
            return "unauthorized"
        parts = str(query.get("data") or "").split(":", 3)
        if len(parts) != 4 or parts[0] != "review" or parts[1] not in {"a", "r"}:
            await self.api.answer_callback(callback_id, "Invalid review action")
            return "invalid"
        decision = "approved" if parts[1] == "a" else "rejected"
        result = self.store.decide(parts[2], parts[3], decision, int(user_id))
        messages = {
            "updated": decision.capitalize(),
            "duplicate": f"Already {decision}",
            "conflict": "Review was already decided",
            "invalid": "Invalid review action",
        }
        await self.api.answer_callback(callback_id, messages[result])
        if result == "updated" and isinstance(message.get("message_id"), int):
            await self.api.remove_buttons(chat_id, message["message_id"])
        return result

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        owner = uuid.uuid4().hex
        if not self.store.acquire_callback_lease(owner):
            raise TelegramAPIError("another Telegram callback poller is already active")
        offset = self.store.update_offset()
        try:
            while not stop_event.is_set():
                if not self.store.refresh_callback_lease(owner):
                    raise TelegramAPIError("Telegram callback poller lost its lease")
                try:
                    updates = await self.api.get_updates(offset)
                    for update in updates:
                        update_id = update.get("update_id")
                        if not isinstance(update_id, int):
                            continue
                        await self.handle_update(update)
                        offset = max(offset, update_id + 1)
                        self.store.set_update_offset(offset)
                except TelegramAPIError:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=2)
                    except TimeoutError:
                        pass
        finally:
            self.store.release_callback_lease(owner)
