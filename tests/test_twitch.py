from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from cliphunter.twitch import (
    EventSubSubscriptionClient,
    TWITCH_USERS_URL,
    TWITCH_VALIDATE_URL,
    TwitchAuthenticationError,
    TwitchConfig,
    TwitchConfigurationError,
    TwitchEventSubClient,
    TwitchIdentityClient,
    normalize_chat_notification,
)


def twitch_config(**changes):
    values = {
        "client_id": "client-id",
        "user_access_token": "secret-user-token",
        "user_id": "9001",
        "channel_ids": ("1001", "1002"),
        "reconnect_base_seconds": 0,
        "reconnect_max_seconds": 0,
        "keepalive_grace_seconds": 0.1,
        "welcome_timeout_seconds": 0.1,
    }
    values.update(changes)
    return TwitchConfig(**values)


def welcome(session_id="session-1", keepalive=10):
    return {
        "metadata": {
            "message_id": f"welcome-{session_id}",
            "message_type": "session_welcome",
            "message_timestamp": "2026-11-16T10:00:00.123456789Z",
        },
        "payload": {
            "session": {
                "id": session_id,
                "status": "connected",
                "keepalive_timeout_seconds": keepalive,
                "reconnect_url": None,
            }
        },
    }


def notification(
    *,
    transport_id="transport-1",
    channel_id="1001",
    user_id="viewer-1",
    text="normal message",
    emote=False,
):
    fragments = [{"type": "text", "text": text}]
    if emote:
        fragments.append({
            "type": "emote",
            "text": "Pog",
            "emote": {
                "id": "emote-1",
                "emote_set_id": "set-1",
                "owner_id": channel_id,
                "format": ["static", "animated"],
            },
        })
    return {
        "metadata": {
            "message_id": transport_id,
            "message_type": "notification",
            "message_timestamp": "2026-11-16T10:00:05.123456789Z",
            "subscription_type": "channel.chat.message",
            "subscription_version": "1",
        },
        "payload": {
            "subscription": {"type": "channel.chat.message", "version": "1"},
            "event": {
                "broadcaster_user_id": channel_id,
                "broadcaster_user_login": f"channel_{channel_id}",
                "broadcaster_user_name": f"Channel {channel_id}",
                "chatter_user_id": user_id,
                "chatter_user_login": f"login_{user_id}",
                "chatter_user_name": f"Viewer {user_id}",
                "message_id": f"chat-{transport_id}",
                "message": {"text": text, "fragments": fragments},
                "message_type": "text",
                "badges": [{"set_id": "subscriber", "id": "3", "info": "3"}],
                "cheer": {"bits": 100},
                "color": "#00FF00",
                "reply": {"parent_message_id": "parent-1"},
                "channel_points_custom_reward_id": None,
            },
        },
    }


class FakeConnection:
    def __init__(self, *items):
        self.items = list(items)
        self.closed = False
        self.waiting = asyncio.Event()

    async def recv(self):
        if not self.items:
            await self.waiting.wait()
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return __import__("json").dumps(item) if not isinstance(item, str) else item

    async def close(self):
        self.closed = True
        self.waiting.set()


class FakeSubscriptions:
    def __init__(self):
        self.sessions = []
        self.called = asyncio.Event()

    async def subscribe_all(self, session_id):
        self.sessions.append(session_id)
        self.called.set()


class SequentialConnector:
    def __init__(self, connections):
        self.connections = list(connections)
        self.urls = []

    async def __call__(self, url):
        self.urls.append(url)
        if not self.connections:
            raise RuntimeError("no fake connection remaining")
        return self.connections.pop(0)


def test_normalizes_chat_identity_and_attributes():
    message = normalize_chat_notification(notification())

    assert message.streamer_id == "1001"
    assert message.username == "viewer-1"
    assert message.user_id == "viewer-1"
    assert message.event_id == "chat-transport-1"
    assert message.text == "normal message"
    assert message.emote_count == 0
    assert message.attributes["eventsub_message_id"] == "transport-1"
    assert message.attributes["badges"][0]["set_id"] == "subscriber"
    assert message.attributes["cheer_bits"] == 100
    assert message.attributes["reply"]["parent_message_id"] == "parent-1"


def test_normalizes_emote_metadata():
    message = normalize_chat_notification(notification(text="Pog", emote=True))

    assert message.emote_count == 1
    assert message.emotes[0].id == "emote-1"
    assert message.emotes[0].emote_set_id == "set-1"
    assert message.emotes[0].formats == ("static", "animated")


@pytest.mark.parametrize("payload", [None, {}, {"metadata": {}}, notification() | {"payload": {}}])
def test_invalid_payloads_are_ignored(payload):
    assert normalize_chat_notification(payload) is None


def test_configuration_accepts_five_unique_channels_and_rejects_more():
    config = twitch_config(channel_ids=("1", "2", "3", "4", "5"))
    assert len(config.channel_ids) == 5
    assert "secret-user-token" not in repr(config)

    with pytest.raises(TwitchConfigurationError):
        twitch_config(channel_ids=("1", "2", "3", "4", "5", "6"))


def test_configuration_accepts_channel_logins_without_ids():
    config = twitch_config(channel_ids=(), channel_logins=("RockstarGames", "gta_streamer"))

    assert config.channel_logins == ("RockstarGames", "gta_streamer")


def test_identity_validates_user_token_and_resolves_channel_logins():
    requests = []

    async def request_json(url, headers):
        requests.append((url, headers))
        if url == TWITCH_VALIDATE_URL:
            return {
                "client_id": "client-id",
                "user_id": "9001",
                "login": "operator",
                "scopes": ["user:read:chat"],
                "expires_in": 7200,
            }
        assert url == f"{TWITCH_USERS_URL}?login=rockstargames&login=gta_streamer"
        return {"data": [
            {"id": "101", "login": "rockstargames"},
            {"id": "102", "login": "gta_streamer"},
        ]}

    config = twitch_config(
        channel_ids=(), channel_logins=("RockstarGames", "gta_streamer")
    )
    prepared = asyncio.run(TwitchIdentityClient(config, request_json).prepare())

    assert prepared.channel_ids == ("101", "102")
    assert prepared.channel_logins == ()
    assert requests[0][1] == {"Authorization": "OAuth secret-user-token"}
    assert requests[1][1]["Authorization"] == "Bearer secret-user-token"
    assert requests[1][1]["Client-Id"] == "client-id"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"client_id": "other"}, "different Client ID"),
        ({"user_id": "42"}, "different user ID"),
        ({"scopes": []}, "missing required scope"),
        ({"expires_in": 10}, "expires too soon"),
    ],
)
def test_identity_rejects_invalid_or_unsafe_token(change, message):
    response = {
        "client_id": "client-id",
        "user_id": "9001",
        "login": "operator",
        "scopes": ["user:read:chat"],
        "expires_in": 7200,
    }
    response.update(change)

    async def request_json(_url, _headers):
        return response

    with pytest.raises(TwitchAuthenticationError, match=message):
        asyncio.run(TwitchIdentityClient(twitch_config(), request_json).validate())


def test_identity_rejects_unknown_channel_login():
    async def request_json(url, _headers):
        if url == TWITCH_VALIDATE_URL:
            return {
                "client_id": "client-id", "user_id": "9001", "login": "operator",
                "scopes": ["user:read:chat"], "expires_in": 7200,
            }
        return {"data": []}

    config = twitch_config(channel_ids=(), channel_logins=("missing_channel",))
    with pytest.raises(TwitchConfigurationError, match="missing_channel"):
        asyncio.run(TwitchIdentityClient(config, request_json).prepare())


def test_identity_resolves_ordered_media_channel_id_and_login_pairs():
    async def request_json(url, _headers):
        assert url == f"{TWITCH_USERS_URL}?id=1001&login=second_channel"
        return {"data": [
            {"id": "1001", "login": "first_channel"},
            {"id": "1002", "login": "second_channel"},
        ]}

    identity = TwitchIdentityClient(
        twitch_config(channel_ids=("1001",), channel_logins=("second_channel",)),
        request_json,
    )
    channels = asyncio.run(identity.resolve_media_channels())

    assert [(channel.user_id, channel.login) for channel in channels] == [
        ("1001", "first_channel"), ("1002", "second_channel")
    ]


def test_subscription_requests_all_channels():
    requests = []

    async def post(body):
        requests.append(body)

    client = EventSubSubscriptionClient(twitch_config(), post=post)
    asyncio.run(client.subscribe_all("session-1"))

    assert {body["condition"]["broadcaster_user_id"] for body in requests} == {"1001", "1002"}
    assert all(body["condition"]["user_id"] == "9001" for body in requests)
    assert all(body["transport"] == {"method": "websocket", "session_id": "session-1"} for body in requests)
    assert client.active_subscriptions == {"1001": "", "1002": ""}


def test_subscription_tracks_returned_subscription_ids():
    async def post(body):
        channel_id = body["condition"]["broadcaster_user_id"]
        return {"data": [{"id": f"subscription-{channel_id}"}]}

    client = EventSubSubscriptionClient(twitch_config(), post=post)
    asyncio.run(client.subscribe_all("session-1"))

    assert client.active_subscriptions == {
        "1001": "subscription-1001", "1002": "subscription-1002"
    }


def test_multiple_channels_and_duplicate_event_are_handled_once():
    first = notification(transport_id="same-id", channel_id="1001")
    duplicate = deepcopy(first)
    second = notification(transport_id="second-id", channel_id="1002", emote=True)
    connection = FakeConnection(welcome(), first, duplicate, second)
    connector = SequentialConnector([connection])
    subscriptions = FakeSubscriptions()
    received = []
    stop = asyncio.Event()

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(), connect=connector, subscriptions=subscriptions
        )

        def on_message(message):
            received.append(message)
            if len(received) == 2:
                stop.set()

        await client.run(on_message, stop)
        return client

    client = asyncio.run(scenario())

    assert [message.streamer_id for message in received] == ["1001", "1002"]
    assert subscriptions.sessions == ["session-1"]
    assert connection.closed
    assert client.health.status == "stopped"
    assert client.health.received_chat_messages == 2


def test_connected_client_revalidates_token_periodically():
    connection = FakeConnection(welcome(), notification(transport_id="validated"))
    stop = asyncio.Event()
    validations = []
    ticks = iter((0.0, 1.0, 2.0, 3.0))

    async def validate():
        validations.append(True)

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(token_validation_interval_seconds=1.5),
            connect=SequentialConnector([connection]),
            subscriptions=FakeSubscriptions(),
            monotonic=lambda: next(ticks),
            token_validator=validate,
        )

        def on_message(_message):
            stop.set()

        await client.run(on_message, stop)

    asyncio.run(scenario())

    assert validations == [True]


def test_unexpected_disconnect_reconnects_and_resubscribes():
    broken = FakeConnection(welcome("first"), RuntimeError("connection lost"))
    recovered = FakeConnection(welcome("second"), notification(transport_id="after-reconnect"))
    connector = SequentialConnector([broken, recovered])
    subscriptions = FakeSubscriptions()
    stop = asyncio.Event()
    received = []

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(), connect=connector, subscriptions=subscriptions
        )

        def on_message(message):
            received.append(message)
            stop.set()

        await client.run(on_message, stop)
        return client

    client = asyncio.run(scenario())

    assert len(received) == 1
    assert subscriptions.sessions == ["first", "second"]
    assert client.health.failures == 1
    assert broken.closed and recovered.closed


def test_reconnect_uses_bounded_exponential_backoff():
    first = FakeConnection(welcome("first"), RuntimeError("first failure"))
    second = FakeConnection(welcome("second"), RuntimeError("second failure"))
    recovered = FakeConnection(welcome("third"), notification(transport_id="recovered"))
    connector = SequentialConnector([first, second, recovered])
    subscriptions = FakeSubscriptions()
    waits = []
    stop = asyncio.Event()

    async def backoff_wait(_stop, delay):
        waits.append(delay)

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(reconnect_base_seconds=1, reconnect_max_seconds=2),
            connect=connector,
            subscriptions=subscriptions,
            backoff_wait=backoff_wait,
        )

        def on_message(_message):
            stop.set()

        await client.run(on_message, stop)
        return client

    client = asyncio.run(scenario())

    assert waits == [1, 2]
    assert client.health.failures == 2
    assert subscriptions.sessions == ["first", "second", "third"]


def test_keepalive_timeout_is_treated_as_connection_failure():
    timed_out = FakeConnection(welcome("timed-out", keepalive=0.01))
    recovered = FakeConnection(welcome("recovered"), notification(transport_id="after-timeout"))
    connector = SequentialConnector([timed_out, recovered])
    subscriptions = FakeSubscriptions()
    stop = asyncio.Event()

    async def no_wait(_stop, _delay):
        return None

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(keepalive_grace_seconds=0),
            connect=connector,
            subscriptions=subscriptions,
            backoff_wait=no_wait,
        )

        def on_message(_message):
            stop.set()

        await client.run(on_message, stop)
        return client

    client = asyncio.run(scenario())

    assert client.health.failures == 1
    assert subscriptions.sessions == ["timed-out", "recovered"]
    assert timed_out.closed and recovered.closed


def test_invalid_notification_is_counted_and_skipped():
    invalid = notification(transport_id="invalid")
    invalid["payload"]["event"].pop("message")
    valid = notification(transport_id="valid")
    connection = FakeConnection(welcome(), invalid, valid)
    stop = asyncio.Event()
    received = []

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(),
            connect=SequentialConnector([connection]),
            subscriptions=FakeSubscriptions(),
        )

        def on_message(message):
            received.append(message)
            stop.set()

        await client.run(on_message, stop)
        return client

    client = asyncio.run(scenario())

    assert len(received) == 1
    assert client.health.invalid_messages == 1


def test_server_directed_reconnect_keeps_subscriptions():
    reconnect = {
        "metadata": {
            "message_id": "reconnect-1",
            "message_type": "session_reconnect",
            "message_timestamp": "2026-11-16T10:00:10Z",
        },
        "payload": {"session": {"reconnect_url": "wss://reconnect.example/ws"}},
    }
    old = FakeConnection(welcome("old"), reconnect)
    replacement = FakeConnection(welcome("new"), notification(transport_id="on-new"))
    connector = SequentialConnector([old, replacement])
    subscriptions = FakeSubscriptions()
    stop = asyncio.Event()

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(), connect=connector, subscriptions=subscriptions
        )

        def on_message(_message):
            stop.set()

        await client.run(on_message, stop)
        return client

    client = asyncio.run(scenario())

    assert connector.urls == [twitch_config().websocket_url, "wss://reconnect.example/ws"]
    assert subscriptions.sessions == ["old"]
    assert client.health.reconnects == 1
    assert old.closed and replacement.closed


def test_graceful_shutdown_closes_waiting_connection():
    connection = FakeConnection(welcome())
    connector = SequentialConnector([connection])
    subscriptions = FakeSubscriptions()
    stop = asyncio.Event()

    async def scenario():
        client = TwitchEventSubClient(
            twitch_config(), connect=connector, subscriptions=subscriptions
        )
        task = asyncio.create_task(client.run(lambda _message: None, stop))
        await subscriptions.called.wait()
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        return client

    client = asyncio.run(scenario())

    assert connection.closed
    assert client.health.status == "stopped"
    assert not client.health.connected
