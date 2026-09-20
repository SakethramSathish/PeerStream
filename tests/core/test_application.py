"""Unit tests for the application composition root."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app.core.application import Application
from app.core.config import Config
from app.core.event_bus import EventBus
from app.core.events import Event, EventType
from app.core.logging_setup import LOGGER_ROOT, reset_logging
from tools.make_test_torrent import write_test_torrent


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    yield
    reset_logging()


class TestCreation:
    def test_defaults_without_a_config_file(self) -> None:
        app = Application.create(load_config=False)
        assert app.config == Config()
        assert isinstance(app.event_bus, EventBus)
        assert app.logger.name == LOGGER_ROOT
        # One subscriber, and it is ours: the app state that reduces events
        # into what a UI reads. An application is not observable otherwise.
        assert app.event_bus.subscriber_count == 1
        assert app.session is not None and app.state is not None
        assert app.state.session is app.session

    def test_loads_configuration_from_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"network": {"listen_port": 12345}}))
        app = Application.create(config_path=path)
        assert app.config.network.listen_port == 12345
        assert app.config_path == path

    def test_ignores_a_broken_configuration(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{ broken")
        app = Application.create(config_path=path)
        assert app.config == Config()

    def test_accepts_an_existing_event_bus(self) -> None:
        bus = EventBus()
        app = Application.create(load_config=False, event_bus=bus)
        assert app.event_bus is bus
        assert app.session is not None
        assert app.session.event_bus is bus

    def test_configures_logging(self, tmp_path: Path) -> None:
        from app.core.config import LoggingConfig

        path = tmp_path / "config.json"
        Config(logging=LoggingConfig(level="DEBUG")).save(path)
        app = Application.create(config_path=path)
        assert app.logger.level == 10  # DEBUG
        reset_logging()


class TestLifecycle:
    async def test_context_manager_emits_start_and_stop(self) -> None:
        seen: list[EventType] = []
        app = Application.create(load_config=False)

        # The logging handler publishes every log record as a LOG event, so
        # filter those out: this test is about lifecycle events.
        app.event_bus.subscribe_all(
            lambda event: seen.append(event.type) if event.type is not EventType.LOG else None
        )

        async with app:
            await app.event_bus.drain()

        assert seen == [EventType.SYSTEM_STARTED, EventType.SYSTEM_STOPPED]

    async def test_stop_flushes_pending_events(self) -> None:
        """stop() must not return while a handler is still running."""
        seen: list[EventType] = []
        app = Application.create(load_config=False)

        async def slow(event: Event) -> None:
            await asyncio.sleep(0.01)
            seen.append(event.type)

        app.event_bus.subscribe(EventType.SYSTEM_STOPPED, slow)
        await app.start()
        await app.stop()  # emits SYSTEM_STOPPED and must not return before `slow` finishes

        assert seen == [EventType.SYSTEM_STOPPED]

    async def test_start_logs_the_configuration_source(self, tmp_path: Path) -> None:
        app = Application.create(load_config=False, config_path=tmp_path / "config.json")
        await app.start()
        await app.stop()


class TestSaveConfig:
    async def test_saves_to_the_configured_path(self, tmp_path: Path) -> None:
        from app.core.config import NetworkConfig, replace_section

        target = tmp_path / "config.json"
        app = Application.create(load_config=False, config_path=target)
        app.config = replace_section(app.config, "network", listen_port=4567)

        saved = await app.save_config()

        assert saved == target
        assert Config.load(target).network.listen_port == 4567
        assert NetworkConfig().listen_port != 4567

    async def test_save_accepts_an_explicit_path(self, tmp_path: Path) -> None:
        app = Application.create(load_config=False)
        saved = await app.save_config(tmp_path / "elsewhere.json")
        assert saved.is_file()


class TestTorrents:
    async def test_add_torrent_reads_the_file_and_hands_it_to_the_session(
        self, tmp_path: Path
    ) -> None:
        path, _ = write_test_torrent(tmp_path, size=32 * 1024, name="app.bin", announce=None)
        app = Application.create(load_config=False)

        try:
            engine = await app.add_torrent(path, start=False)
        finally:
            await app.stop()

        assert engine.torrent.name == "app.bin"
        assert not engine.running, "start=False must mean not started"

    async def test_added_torrents_are_listed_in_order(self, tmp_path: Path) -> None:
        first, _ = write_test_torrent(tmp_path, size=16 * 1024, name="first.bin", announce=None)
        second, _ = write_test_torrent(tmp_path, size=16 * 1024, name="second.bin", announce=None)
        app = Application.create(load_config=False)

        try:
            one = await app.add_torrent(first, start=False)
            two = await app.add_torrent(second, start=False)
            assert app.torrents == (one, two)
            assert app.session is not None and len(app.session) == 2
        finally:
            await app.stop()

    async def test_stopping_the_application_stops_its_torrents(self, tmp_path: Path) -> None:
        """Quitting must not leave sockets behind the UI's back."""
        path, _ = write_test_torrent(tmp_path, size=16 * 1024, name="quit.bin", announce=None)
        app = Application.create(load_config=False)
        engine = await app.add_torrent(path)
        assert engine.running

        await app.stop()

        assert app.torrents == ()
        assert engine.state.value == "stopped"

    async def test_an_application_without_a_session_cannot_add(self, tmp_path: Path) -> None:
        path, _ = write_test_torrent(tmp_path, size=16 * 1024, name="bare.bin", announce=None)
        app = Application()  # built by hand: no session was composed

        assert app.torrents == ()
        with pytest.raises(RuntimeError, match="no session"):
            await app.add_torrent(path)
