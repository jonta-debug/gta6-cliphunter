"""Official Twitch EventSub WebSocket chat transport and normalization."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .models import ChatEmote, ChatMessage
from .replay import parse_timestamp

log = logging.getLogger(__name__)

EVENTSUB_WEBSOCKET_URL = "wss://eventsub.wss.twitch.tv/ws"
EVENTSUB_SUBSCRIPTIONS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
TWITCH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"


class TwitchConfigurationError(ValueError):
    pass


class TwitchEventSubError(RuntimeError):
    pass


class TwitchAuthenticationError(TwitchEventSubError):
    pass


@dataclass(frozen=True)
class TwitchConfig:
    client_id: str
    user_access_token: str = field(repr=False)
    user_id: str
    channel_ids: tuple[str, ...] = ()
    channel_logins: tuple[str, ...] = ()
    websocket_url: str = EVENTSUB_WEBSOCKET_URL
    reconnect_base_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    keepalive_grace_seconds: float = 5.0
    welcome_timeout_seconds: float = 10.0
    max_channels: int = 5
    minimum_token_lifetime_seconds: int = 300
    token_validation_interval_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if not self.client_id or not self.user_access_token or not self.user_id:
            raise TwitchConfigurationError("Twitch client ID, user access token, and user ID are required")
        if not self.user_id.isdigit():
            raise TwitchConfigurationError("TWITCH_USER_ID must be a numeric Twitch user ID")
        if not self.channel_ids and not self.channel_logins:
            raise TwitchConfigurationError("at least one Twitch channel ID or login is required")
        if len(self.channel_ids) + len(self.channel_logins) > self.max_channels:
            raise TwitchConfigurationError(f"at most {self.max_channels} Twitch channels are supported initially")
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise TwitchConfigurationError("Twitch channel IDs must be unique")
        if any(not channel_id.isdigit() for channel_id in self.channel_ids):
            raise TwitchConfigurationError("TWITCH_CHANNEL_IDS must contain numeric Twitch user IDs")
        if len({login.lower() for login in self.channel_logins}) != len(self.channel_logins):
            raise TwitchConfigurationError("Twitch channel logins must be unique")
        if any(not login or len(login) > 25 or not login.replace("_", "a").isalnum()
               for login in self.channel_logins):
            raise TwitchConfigurationError("TWITCH_CHANNELS contains an invalid Twitch login")
        if self.reconnect_base_seconds < 0 or self.reconnect_max_seconds < self.reconnect_base_seconds:
            raise TwitchConfigurationError("invalid Twitch reconnect delays")
        if self.keepalive_grace_seconds < 0 or self.welcome_timeout_seconds <= 0:
            raise TwitchConfigurationError("invalid Twitch health timeouts")
        if self.minimum_token_lifetime_seconds < 0 or self.token_validation_interval_seconds <= 0:
            raise TwitchConfigurationError("invalid Twitch token validation settings")

    @classmethod
    def from_env(cls) -> "TwitchConfig":
        channels = tuple(
            value.strip() for value in os.environ.get("TWITCH_CHANNEL_IDS", "").split(",") if value.strip()
        )
        channel_logins = tuple(
            value.strip().lower() for value in os.environ.get("TWITCH_CHANNELS", "").split(",") if value.strip()
        )
        return cls(
            client_id=os.environ.get("TWITCH_CLIENT_ID", "").strip(),
            user_access_token=os.environ.get("TWITCH_USER_ACCESS_TOKEN", "").strip(),
            user_id=os.environ.get("TWITCH_USER_ID", "").strip(),
            channel_ids=channels,
            channel_logins=channel_logins,
            websocket_url=os.environ.get("TWITCH_EVENTSUB_WEBSOCKET_URL", EVENTSUB_WEBSOCKET_URL).strip(),
            reconnect_base_seconds=float(os.environ.get("TWITCH_RECONNECT_BASE_SECONDS", "1")),
            reconnect_max_seconds=float(os.environ.get("TWITCH_RECONNECT_MAX_SECONDS", "30")),
            keepalive_grace_seconds=float(os.environ.get("TWITCH_KEEPALIVE_GRACE_SECONDS", "5")),
            minimum_token_lifetime_seconds=int(os.environ.get("TWITCH_MIN_TOKEN_LIFETIME_SECONDS", "300")),
        )


@dataclass
class ConnectionHealth:
    status: str = "stopped"
    connected: bool = False
    session_id: str = ""
    last_message_monotonic: float | None = None
    reconnects: int = 0
    failures: int = 0
    invalid_messages: int = 0
    subscriptions: int = 0
    received_chat_messages: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class TwitchTokenInfo:
    client_id: str
    user_id: str
    login: str
    scopes: tuple[str, ...]
    expires_in: int


@dataclass(frozen=True)
class TwitchChannel:
    user_id: str
    login: str


class TwitchIdentityClient:
    """Validate OAuth identity and resolve broadcaster logins with official APIs."""

    def __init__(
        self,
        config: TwitchConfig,
        request_json: Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.config = config
        self._request_json = request_json or self._request_official

    async def _request_official(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_sync, url, headers)

    @staticmethod
    def _request_sync(url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            # Twitch error bodies don't contain the submitted token; avoid echoing headers or URLs.
            raise TwitchAuthenticationError(f"Twitch authentication request returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise TwitchEventSubError(f"Twitch authentication request failed: {error.reason}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TwitchEventSubError("Twitch authentication response was not valid JSON") from error
        if not isinstance(data, dict):
            raise TwitchEventSubError("Twitch authentication response was not an object")
        return data

    async def validate(self) -> TwitchTokenInfo:
        data = await self._request_json(
            TWITCH_VALIDATE_URL,
            {"Authorization": f"OAuth {self.config.user_access_token}"},
        )
        client_id = data.get("client_id")
        user_id = data.get("user_id")
        login = data.get("login")
        scopes = data.get("scopes")
        expires_in = data.get("expires_in")
        if not isinstance(client_id, str) or not isinstance(user_id, str) or not user_id \
                or not isinstance(login, str) or not isinstance(scopes, list) \
                or isinstance(expires_in, bool) or not isinstance(expires_in, int):
            raise TwitchAuthenticationError("Twitch token is not a valid user access token")
        if client_id != self.config.client_id:
            raise TwitchAuthenticationError("Twitch token belongs to a different Client ID")
        if user_id != self.config.user_id:
            raise TwitchAuthenticationError("Twitch token belongs to a different user ID")
        normalized_scopes = tuple(str(scope) for scope in scopes)
        if "user:read:chat" not in normalized_scopes:
            raise TwitchAuthenticationError("Twitch token is missing required scope user:read:chat")
        if expires_in < self.config.minimum_token_lifetime_seconds:
            raise TwitchAuthenticationError("Twitch token expires too soon; obtain or refresh it before connecting")
        return TwitchTokenInfo(client_id, user_id, login, normalized_scopes, expires_in)

    async def resolve_channel_ids(self) -> tuple[str, ...]:
        if not self.config.channel_logins:
            return self.config.channel_ids
        logins = tuple(login.lower() for login in self.config.channel_logins)
        query = urllib.parse.urlencode([("login", login) for login in logins])
        data = await self._request_json(
            f"{TWITCH_USERS_URL}?{query}",
            {
                "Authorization": f"Bearer {self.config.user_access_token}",
                "Client-Id": self.config.client_id,
            },
        )
        users = data.get("data")
        if not isinstance(users, list):
            raise TwitchEventSubError("Twitch Get Users response did not contain a user list")
        resolved = {
            str(user.get("login") or "").lower(): str(user.get("id") or "")
            for user in users if isinstance(user, dict)
        }
        missing = [login for login in logins if not resolved.get(login)]
        if missing:
            raise TwitchConfigurationError(
                "Twitch channel login was not found: " + ", ".join(missing)
            )
        combined = list(self.config.channel_ids)
        for login in logins:
            channel_id = resolved[login]
            if channel_id not in combined:
                combined.append(channel_id)
        if len(combined) > self.config.max_channels:
            raise TwitchConfigurationError(
                f"resolved configuration exceeds the {self.config.max_channels}-channel initial limit"
            )
        return tuple(combined)

    async def resolve_media_channels(self) -> tuple[TwitchChannel, ...]:
        """Resolve every configured ID/login to the stable pair needed by media workers."""
        query = urllib.parse.urlencode(
            [("id", channel_id) for channel_id in self.config.channel_ids]
            + [("login", login.lower()) for login in self.config.channel_logins]
        )
        data = await self._request_json(
            f"{TWITCH_USERS_URL}?{query}",
            {
                "Authorization": f"Bearer {self.config.user_access_token}",
                "Client-Id": self.config.client_id,
            },
        )
        users = data.get("data")
        if not isinstance(users, list):
            raise TwitchEventSubError("Twitch Get Users response did not contain a user list")
        by_id: dict[str, TwitchChannel] = {}
        by_login: dict[str, TwitchChannel] = {}
        for user in users:
            if not isinstance(user, dict):
                continue
            user_id = str(user.get("id") or "")
            login = str(user.get("login") or "").lower()
            if not user_id.isdigit() or not login:
                continue
            channel = TwitchChannel(user_id=user_id, login=login)
            by_id[user_id] = channel
            by_login[login] = channel
        ordered: list[TwitchChannel] = []
        for channel_id in self.config.channel_ids:
            if channel_id not in by_id:
                raise TwitchConfigurationError(f"Twitch channel ID was not found: {channel_id}")
            ordered.append(by_id[channel_id])
        for login in self.config.channel_logins:
            normalized = login.lower()
            if normalized not in by_login:
                raise TwitchConfigurationError(f"Twitch channel login was not found: {normalized}")
            if by_login[normalized].user_id not in {item.user_id for item in ordered}:
                ordered.append(by_login[normalized])
        return tuple(ordered)

    async def prepare(self) -> TwitchConfig:
        await self.validate()
        channel_ids = await self.resolve_channel_ids()
        return replace(self.config, channel_ids=channel_ids, channel_logins=())


def normalize_chat_notification(payload: Any) -> ChatMessage | None:
    """Map a channel.chat.message EventSub notification to the shared chat model."""
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    body = payload.get("payload")
    if not isinstance(metadata, dict) or not isinstance(body, dict):
        return None
    event = body.get("event")
    if not isinstance(event, dict):
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None

    channel_id = event.get("broadcaster_user_id")
    user_id = event.get("chatter_user_id")
    text = message.get("text")
    timestamp_value = metadata.get("message_timestamp")
    if not all(isinstance(value, str) and value for value in (channel_id, user_id, text, timestamp_value)):
        return None
    try:
        timestamp = parse_timestamp(timestamp_value)
    except ValueError:
        return None

    emotes = []
    fragments = message.get("fragments")
    if isinstance(fragments, list):
        for fragment in fragments:
            if not isinstance(fragment, dict) or fragment.get("type") != "emote":
                continue
            emote = fragment.get("emote")
            if not isinstance(emote, dict) or not isinstance(emote.get("id"), str):
                continue
            formats = emote.get("format") if isinstance(emote.get("format"), list) else []
            emotes.append(ChatEmote(
                id=emote["id"],
                text=str(fragment.get("text") or ""),
                emote_set_id=str(emote.get("emote_set_id") or ""),
                owner_id=str(emote.get("owner_id") or ""),
                formats=tuple(value for value in formats if isinstance(value, str)),
            ))

    badges = event.get("badges") if isinstance(event.get("badges"), list) else []
    reply = event.get("reply") if isinstance(event.get("reply"), dict) else None
    cheer = event.get("cheer") if isinstance(event.get("cheer"), dict) else None
    cheer_bits = cheer.get("bits") if cheer else 0
    if isinstance(cheer_bits, bool) or not isinstance(cheer_bits, int):
        cheer_bits = 0
    attributes = {
        "eventsub_message_id": str(metadata.get("message_id") or ""),
        "broadcaster_login": str(event.get("broadcaster_user_login") or ""),
        "broadcaster_name": str(event.get("broadcaster_user_name") or ""),
        "chatter_login": str(event.get("chatter_user_login") or ""),
        "chatter_name": str(event.get("chatter_user_name") or ""),
        "message_type": str(event.get("message_type") or "text"),
        "badges": badges,
        "color": str(event.get("color") or ""),
        "reply": reply,
        "cheer_bits": cheer_bits,
        "channel_points_custom_reward_id": event.get("channel_points_custom_reward_id"),
        "source_broadcaster_user_id": event.get("source_broadcaster_user_id"),
        "source_message_id": event.get("source_message_id"),
        "is_source_only": event.get("is_source_only"),
    }
    return ChatMessage(
        streamer_id=channel_id,
        timestamp=timestamp,
        username=user_id,
        text=text,
        emote_count=len(emotes),
        user_id=user_id,
        event_id=str(event.get("message_id") or ""),
        emotes=tuple(emotes),
        attributes=attributes,
    )


class EventSubSubscriptionClient:
    """Create official channel.chat.message subscriptions for one WebSocket session."""

    def __init__(
        self,
        config: TwitchConfig,
        post: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        self.config = config
        self._post = post or self._post_official
        self.active_subscriptions: dict[str, str] = {}

    async def subscribe_all(self, session_id: str) -> None:
        bodies = [{
            "type": "channel.chat.message",
            "version": "1",
            "condition": {
                "broadcaster_user_id": channel_id,
                "user_id": self.config.user_id,
            },
            "transport": {"method": "websocket", "session_id": session_id},
        } for channel_id in self.config.channel_ids]
        results = await asyncio.gather(*(self._post(body) for body in bodies))
        active = {}
        for body, result in zip(bodies, results):
            subscription_id = ""
            if isinstance(result, dict) and isinstance(result.get("data"), list) and result["data"]:
                record = result["data"][0]
                if isinstance(record, dict):
                    subscription_id = str(record.get("id") or "")
            active[body["condition"]["broadcaster_user_id"]] = subscription_id
        self.active_subscriptions = active

    async def _post_official(self, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._post_sync, body)

    def _post_sync(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            EVENTSUB_SUBSCRIPTIONS_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.user_access_token}",
                "Client-Id": self.config.client_id,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 202:
                    raise TwitchEventSubError(f"Twitch subscription returned HTTP {response.status}")
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise TwitchEventSubError(f"Twitch subscription returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise TwitchEventSubError(f"Twitch subscription request failed: {error.reason}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TwitchEventSubError("Twitch subscription response was not valid JSON") from error
        if not isinstance(data, dict):
            raise TwitchEventSubError("Twitch subscription response was not an object")
        return data


class _SeenMessageIds:
    def __init__(self, limit: int = 10_000) -> None:
        self.limit = limit
        self.order: deque[str] = deque()
        self.values: set[str] = set()

    def add(self, message_id: str) -> bool:
        if not message_id or message_id in self.values:
            return False
        self.order.append(message_id)
        self.values.add(message_id)
        while len(self.order) > self.limit:
            self.values.discard(self.order.popleft())
        return True


class TwitchEventSubClient:
    """Maintain one EventSub WebSocket and normalize chat notifications."""

    def __init__(
        self,
        config: TwitchConfig,
        *,
        connect: Callable[[str], Awaitable[Any]] | None = None,
        subscriptions: EventSubSubscriptionClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        backoff_wait: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
        token_validator: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.config = config
        self._connect = connect or self._connect_official
        self.subscriptions = subscriptions or EventSubSubscriptionClient(config)
        self.monotonic = monotonic
        self._backoff_wait = backoff_wait or self._wait_backoff
        self._token_validator = token_validator
        self._last_token_validation = self.monotonic()
        self.health = ConnectionHealth()
        self._seen = _SeenMessageIds()

    async def _connect_official(self, url: str) -> Any:
        import websockets
        return await websockets.connect(url, open_timeout=self.config.welcome_timeout_seconds)

    async def _receive(self, connection: Any, stop_event: asyncio.Event, timeout: float) -> Any | None:
        receive_task = asyncio.create_task(connection.recv())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                (receive_task, stop_task), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise TimeoutError("Twitch EventSub connection exceeded its keepalive timeout")
            if stop_task in done and stop_task.result():
                return None
            return await receive_task
        finally:
            for task in (receive_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive_task, stop_task, return_exceptions=True)

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any]:
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
            raise TwitchEventSubError("Twitch EventSub sent invalid JSON") from error
        if not isinstance(decoded, dict):
            raise TwitchEventSubError("Twitch EventSub message was not an object")
        return decoded

    async def _open(
        self,
        url: str,
        stop_event: asyncio.Event,
        *,
        subscribe: bool,
    ) -> tuple[Any, float]:
        connection = await self._connect(url)
        try:
            raw = await self._receive(connection, stop_event, self.config.welcome_timeout_seconds)
            if raw is None:
                return connection, 0.0
            welcome = self._decode(raw)
            metadata = welcome.get("metadata")
            session = welcome.get("payload", {}).get("session") if isinstance(welcome.get("payload"), dict) else None
            if not isinstance(metadata, dict) or metadata.get("message_type") != "session_welcome" \
                    or not isinstance(session, dict) or not isinstance(session.get("id"), str):
                raise TwitchEventSubError("first Twitch EventSub message was not a valid welcome")
            keepalive = float(session.get("keepalive_timeout_seconds") or 10)
            if subscribe:
                await self.subscriptions.subscribe_all(session["id"])
            self.health.status = "connected"
            self.health.connected = True
            self.health.session_id = session["id"]
            self.health.last_message_monotonic = self.monotonic()
            active = getattr(self.subscriptions, "active_subscriptions", None)
            self.health.subscriptions = (
                len(active) if active is not None else len(self.config.channel_ids)
            )
            self.health.last_error = ""
            return connection, keepalive
        except BaseException:
            await connection.close()
            raise

    async def _wait_backoff(self, stop_event: asyncio.Event, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def run(
        self,
        on_message: Callable[[ChatMessage], Any],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        delay = self.config.reconnect_base_seconds
        self.health.status = "connecting"
        while not stop_event.is_set():
            connection = None
            try:
                connection, keepalive = await self._open(
                    self.config.websocket_url, stop_event, subscribe=True
                )
                if stop_event.is_set():
                    break
                while not stop_event.is_set():
                    raw = await self._receive(
                        connection, stop_event, keepalive + self.config.keepalive_grace_seconds
                    )
                    if raw is None:
                        break
                    envelope = self._decode(raw)
                    # A post-welcome message proves the connection stayed healthy.
                    # Do not reset on Welcome alone: repeated connect-then-drop
                    # failures must retain exponential backoff.
                    delay = self.config.reconnect_base_seconds
                    now = self.monotonic()
                    self.health.last_message_monotonic = now
                    if self._token_validator is not None \
                            and now - self._last_token_validation >= self.config.token_validation_interval_seconds:
                        await self._token_validator()
                        self._last_token_validation = now
                    metadata = envelope.get("metadata")
                    if not isinstance(metadata, dict):
                        self.health.invalid_messages += 1
                        continue
                    message_type = metadata.get("message_type")
                    if message_type == "session_keepalive":
                        continue
                    if message_type == "session_reconnect":
                        payload = envelope.get("payload")
                        session = payload.get("session") if isinstance(payload, dict) else None
                        reconnect_url = session.get("reconnect_url") if isinstance(session, dict) else None
                        if not isinstance(reconnect_url, str) or not reconnect_url:
                            raise TwitchEventSubError("Twitch reconnect message had no reconnect URL")
                        replacement, replacement_keepalive = await self._open(
                            reconnect_url, stop_event, subscribe=False
                        )
                        await connection.close()
                        connection, keepalive = replacement, replacement_keepalive
                        self.health.reconnects += 1
                        continue
                    if message_type == "revocation":
                        raise TwitchEventSubError("Twitch revoked a chat subscription")
                    if message_type != "notification" \
                            or metadata.get("subscription_type") != "channel.chat.message":
                        continue
                    message_id = str(metadata.get("message_id") or "")
                    if not self._seen.add(message_id):
                        continue
                    message = normalize_chat_notification(envelope)
                    if message is None:
                        self.health.invalid_messages += 1
                        continue
                    result = on_message(message)
                    self.health.received_chat_messages += 1
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                raise
            except TwitchAuthenticationError as error:
                self.health.failures += 1
                self.health.last_error = str(error)
                self.health.status = "authentication_failed"
                raise
            except Exception as error:  # reconnect after transport, health, or subscription failures
                self.health.failures += 1
                self.health.last_error = str(error)
                self.health.status = "backing_off"
                log.warning("Twitch EventSub connection failed; retrying in %.1fs: %s", delay, error)
                await self._backoff_wait(stop_event, delay)
                delay = min(self.config.reconnect_max_seconds, max(delay * 2, 0.001))
            finally:
                self.health.connected = False
                if connection is not None:
                    await connection.close()
        self.health.status = "stopped"
        self.health.connected = False
