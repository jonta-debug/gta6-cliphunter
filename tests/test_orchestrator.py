from __future__ import annotations

import asyncio

from cliphunter.media import MediaSource, MediaSourceError
from cliphunter.orchestrator import ChannelMediaSupervisor, PipelineComponent, PipelineRunner


def test_pipeline_runner_isolates_noncritical_failure_and_shuts_down_cleanly():
    started = {"chat": asyncio.Event(), "media": asyncio.Event()}
    stopped = {"chat": False, "media": False}

    async def component(name, stop_event):
        started[name].set()
        try:
            await stop_event.wait()
        finally:
            stopped[name] = True

    async def fail(_stop_event):
        raise RuntimeError("expected component failure")

    async def scenario():
        stop = asyncio.Event()
        runner = PipelineRunner([
            PipelineComponent("chat", lambda event: component("chat", event), critical=True),
            PipelineComponent("media", lambda event: component("media", event)),
            PipelineComponent("failure", fail),
        ])
        task = asyncio.create_task(runner.run(stop))
        await asyncio.wait_for(
            asyncio.gather(started["chat"].wait(), started["media"].wait()), timeout=1
        )
        await asyncio.sleep(0)
        assert not stop.is_set()
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    assert stopped == {"chat": True, "media": True}


def test_media_supervisor_retries_resolution_and_stops_worker():
    class Resolver:
        attempts = 0

        async def resolve(self, _channel):
            self.attempts += 1
            if self.attempts == 1:
                raise MediaSourceError("offline")
            return MediaSource("fixture", "fixture")

    class Worker:
        started = asyncio.Event()
        stopped = False

        async def run(self, stop_event):
            self.started.set()
            await stop_event.wait()
            self.stopped = True

    resolver = Resolver()
    worker = Worker()
    supervisor = ChannelMediaSupervisor(
        "channel", resolver, lambda _source: worker,
        backoff_base_seconds=0.001, backoff_max_seconds=0.002,
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))
        await asyncio.wait_for(worker.started.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    assert resolver.attempts == 2
    assert worker.stopped
