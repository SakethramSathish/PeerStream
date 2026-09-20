"""Application composition root.

This is the only place that knows how to assemble the client: configuration,
logging and the event bus are created here and handed to whoever needs them.
Keeping wiring in one module is what lets the CLI and the desktop UI drive the
same engine without either of them constructing the other's dependencies.

From M10 the application composes a :class:`~app.services.session.Session` and
an :class:`~app.services.app_state.AppState`: the session owns the torrents,
the app state is what a UI reads. Neither is optional — an application is a
client, and a client has somewhere to put its torrents.

    >>> async with Application.create(load_config=False) as app:   # doctest: +SKIP
    ...     engine = await app.add_torrent(Path("ubuntu.torrent")) # doctest: +SKIP
    ...     view = app.state.snapshot()                            # doctest: +SKIP
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from app.core.config import Config, default_config_path
from app.core.event_bus import EventBus
from app.core.events import EventType, make_event
from app.core.logging_setup import LOGGER_ROOT, configure_logging, reset_logging
from app.services import AppState, Engine, Session
from app.torrent import parse_magnet, parse_torrent_file


@dataclass(slots=True)
class Application:
    """A wired-up client instance.

    Attributes:
        config: Effective configuration.
        event_bus: The bus every subsystem publishes to.
        session: The torrent session; owns every running torrent.
        state: The observable view a UI renders.
        config_path: Where the configuration was read from (and can be saved).
    """

    config: Config = field(default_factory=Config)
    event_bus: EventBus = field(default_factory=EventBus)
    session: Session | None = None
    state: AppState | None = None
    config_path: Path | None = None
    logger: logging.Logger = field(default=logging.getLogger(LOGGER_ROOT))

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls,
        *,
        config_path: str | Path | None = None,
        load_config: bool = True,
        event_bus: EventBus | None = None,
    ) -> Application:
        """Build an application: load config, configure logging, create the bus.

        Args:
            config_path: Explicit config location; the default path is used when
                omitted.
            load_config: When false, start with built-in defaults and never
                touch the filesystem (used by tests and one-off CLI commands).
            event_bus: An existing bus to use, instead of creating one.

        Returns:
            A configured :class:`Application` that has not been started yet.
        """
        path = Path(config_path).expanduser() if config_path else default_config_path()
        config = Config.load_or_default(path) if load_config else Config()
        bus = event_bus or EventBus()
        package_logger = configure_logging(config.logging, event_bus=bus)
        session = Session(config, event_bus=bus)
        return cls(
            config=config,
            event_bus=bus,
            session=session,
            state=AppState(session),
            config_path=path,
            logger=package_logger,
        )

    # ------------------------------------------------------------- torrents

    @property
    def torrents(self) -> tuple[Engine, ...]:
        """Every torrent the session is running."""
        return self.session.engines if self.session is not None else ()

    async def add_torrent(
        self, path: str | Path, *, start: bool = True, **options: object
    ) -> Engine:
        """Parse a ``.torrent`` file and add it to the session.

        Args:
            path: The torrent file to read.
            start: Whether to start transferring immediately.
            **options: Passed to :meth:`Session.add_torrent` (``directory``,
                ``listen``, ``resume``, ``trackers``).

        Raises:
            RuntimeError: If the application was built without a session.
        """
        if self.session is None:
            raise RuntimeError("this application has no session")
        torrent = parse_torrent_file(path)
        self.logger.info("adding torrent %s (%s)", torrent.name, torrent.hex_info_hash)
        return await self.session.add_torrent(torrent, start=start, **options)  # type: ignore[arg-type]

    async def add_magnet(self, uri: str, *, start: bool = True, **options: object) -> Engine:
        """Resolve a magnet link and add the torrent it describes.

        Args:
            uri: The magnet URI.
            start: Whether to start transferring once the metadata arrives.
            **options: Passed to :meth:`Session.add_magnet` (``directory``,
                ``listen``, ``resume``, ``peers``).

        Raises:
            RuntimeError: If the application was built without a session.
            MetadataError: No peer supplied metadata for the info hash.
        """
        if self.session is None:
            raise RuntimeError("this application has no session")
        magnet = parse_magnet(uri)
        self.logger.info("resolving magnet %s (%s)", magnet.name, magnet.hex_info_hash)
        engine, resolution = await self.session.add_magnet(magnet, start=start, **options)  # type: ignore[arg-type]
        self.logger.info(
            "magnet resolved to %s from %s",
            resolution.torrent.name,
            resolution.metadata.address,
        )
        return engine

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the DHT (when enabled) and publish the start event.

        The DHT is started here rather than lazily because it needs a moment to
        fill its routing table before a magnet link arrives; a node that has
        never spoken to anyone cannot answer the first question asked of it.
        """
        self.logger.info("starting bittorrent client (config: %s)", self.config_path or "defaults")
        if self.session is not None:
            await self.session.start_dht()
        self.event_bus.emit(make_event(EventType.SYSTEM_STARTED, message="client started"))

    async def stop(self) -> None:
        """Stop every torrent, publish the stop event, and flush the bus.

        Closing the session first is what makes "quit" mean quit: a torrent
        left running would keep its sockets after the UI went away.
        """
        if self.session is not None:
            await self.session.aclose()
        self.event_bus.emit(make_event(EventType.SYSTEM_STOPPED, message="client stopped"))
        await self.event_bus.drain()
        reset_logging()

    async def save_config(self, path: str | Path | None = None) -> Path:
        """Persist the current configuration."""
        target = Path(path).expanduser() if path else (self.config_path or default_config_path())
        saved = self.config.save(target)
        self.logger.info("configuration saved to %s", saved)
        return saved

    async def __aenter__(self) -> Application:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()
