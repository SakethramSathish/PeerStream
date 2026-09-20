"""Typed, file-backed application configuration (TRD §40).

Configuration is a plain dataclass tree rather than a bag of globals: every
subsystem receives the section it needs, which keeps modules testable and makes
the settings the UI exposes discoverable.

Design rules:

* **Unknown keys are rejected on load.** A typo like ``max_peerz`` silently
  doing nothing is worse than a startup error, especially once the UI has a
  settings screen.
* **Missing keys fall back to defaults**, so a partial config file is valid and
  new settings can be added without invalidating existing files.
* **A corrupt file never prevents launch**: :meth:`Config.load_or_default`
  logs a warning and starts with defaults. A download client that refuses to
  start because of a bad config is a download client the user cannot fix
  without editing files by hand.
* **JSON, not YAML**, to avoid a third runtime dependency (see A7 in the
  implementation plan).

The file lives at ``$XDG_CONFIG_HOME/bittorrent-client/config.json``
(``~/.config/bittorrent-client/config.json`` by default), overridable with the
``BITTORRENT_CLIENT_CONFIG`` environment variable.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Final

from app.core.constants import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_BLOCK_TIMEOUT,
    DEFAULT_CHOKE_INTERVAL,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_DHT_ANNOUNCE_INTERVAL,
    DEFAULT_DHT_BOOTSTRAP_NODES,
    DEFAULT_DOWNLOAD_DIRECTORY,
    DEFAULT_ENDGAME_DELAY,
    DEFAULT_ENDGAME_THRESHOLD,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_HISTORY_SAMPLES,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_KEEPALIVE_INTERVAL,
    DEFAULT_LISTEN_PORT,
    DEFAULT_MAX_PEER_FAILURES,
    DEFAULT_MAX_REQUESTS_PER_PEER,
    DEFAULT_OPTIMISTIC_UNCHOKE_INTERVAL,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SNUB_SECONDS,
    DEFAULT_STATS_WINDOW_SECONDS,
    DEFAULT_UPLOAD_QUEUE_TIMEOUT,
    DEFAULT_UPLOAD_SLOTS,
    MAX_BLOCK_SIZE,
    MAX_MESSAGE_LENGTH,
)

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR: Final[str] = "BITTORRENT_CLIENT_CONFIG"
CONFIG_DIRECTORY_NAME: Final[str] = "bittorrent-client"
CONFIG_FILENAME: Final[str] = "config.json"

CONFIG_VERSION: Final[int] = 1

_FLOAT_RANGES: Final[Mapping[tuple[str, str], tuple[float | None, float | None]]] = {
    ("network", "connection_timeout"): (0.1, 600.0),
    ("network", "handshake_timeout"): (0.1, 600.0),
    ("network", "request_timeout"): (0.1, 3600.0),
    ("network", "keepalive_interval"): (5.0, 600.0),
    ("network", "idle_timeout"): (10.0, 3600.0),
    ("network", "reconnect_delay"): (0.0, 3600.0),
    ("download", "block_timeout"): (1.0, 600.0),
    ("tracker", "announce_interval_multiplier"): (0.1, 10.0),
    ("tracker", "http_timeout"): (0.5, 300.0),
    ("tracker", "udp_timeout"): (0.5, 300.0),
    ("storage", "resume_autosave_seconds"): (1.0, 3600.0),
    ("stats", "window_seconds"): (0.5, 300.0),
    ("stats", "sample_interval"): (0.1, 60.0),
    ("download", "endgame_delay"): (0.0, 60.0),
    ("upload", "max_upload_speed"): (0, None),
    ("upload", "slots"): (0, 64),
    ("upload", "optimistic_interval"): (1.0, 3600.0),
    ("upload", "choke_interval"): (1.0, 3600.0),
    ("upload", "snub_seconds"): (0.0, 3600.0),
    ("upload", "max_requests_per_peer"): (1, 1024),
    ("upload", "queue_timeout"): (1.0, 3600.0),
    ("dht", "announce_interval"): (60.0, 3600.0),
}
"""Inclusive ``(minimum, maximum)`` bounds for float settings (``None`` = unbounded)."""

_INT_RANGES: Final[Mapping[tuple[str, str], tuple[int | None, int | None]]] = {
    ("config_version", "config_version"): (1, None),
    ("network", "listen_port"): (1, 65535),
    ("network", "max_peers_per_torrent"): (1, None),
    ("network", "max_peers_total"): (1, None),
    ("network", "max_message_length"): (1024, None),
    ("network", "max_peer_failures"): (1, 20),
    ("download", "block_size"): (1024, MAX_BLOCK_SIZE),
    ("download", "max_outstanding_requests"): (1, 1024),
    ("download", "endgame_threshold"): (0, 10_000),
    ("download", "hash_workers"): (1, 32),
    ("tracker", "min_announce_interval"): (1, None),
    ("tracker", "max_retries"): (0, 10),
    ("stats", "history_samples"): (10, 100_000),
    ("logging", "max_bytes"): (1024, None),
    ("logging", "backup_count"): (0, 100),
}
"""Inclusive ``(minimum, maximum)`` bounds for integer settings (``None`` = unbounded)."""


class ConfigError(ValueError):
    """Raised when a configuration value is invalid or unrecognised."""


class PieceStrategy(StrEnum):
    """Piece selection strategies (TRD §26)."""

    SEQUENTIAL = "sequential"
    RAREST_FIRST = "rarest_first"
    RANDOM = "random"


# ------------------------------------------------------------------- sections


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Peer connection settings."""

    listen_port: int = DEFAULT_LISTEN_PORT
    max_peers_per_torrent: int = 40
    max_peers_total: int = 200
    connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    max_peer_failures: int = DEFAULT_MAX_PEER_FAILURES
    reconnect_delay: float = DEFAULT_RECONNECT_DELAY
    max_message_length: int = MAX_MESSAGE_LENGTH
    accept_incoming_connections: bool = True


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    """Download and upload behaviour."""

    max_download_speed: int = 0  # bytes/second; 0 means unlimited
    piece_strategy: PieceStrategy = PieceStrategy.RAREST_FIRST
    block_size: int = DEFAULT_BLOCK_SIZE
    max_outstanding_requests: int = 16  # pipelined requests per peer
    block_timeout: float = DEFAULT_BLOCK_TIMEOUT
    endgame_enabled: bool = True
    endgame_threshold: int = DEFAULT_ENDGAME_THRESHOLD
    endgame_delay: float = DEFAULT_ENDGAME_DELAY
    hash_workers: int = 2  # threads for SHA-1 verification


@dataclass(frozen=True, slots=True)
class UploadConfig:
    """Upload and choking behaviour (PRD FR-13, FR-14).

    Uploading is reciprocal by design: the peers we serve are the peers that
    served us. ``slots`` of them are chosen by what they have sent us lately,
    and one slot is given away on a rotation so a new peer can earn its place
    instead of waiting for a vacancy.
    """

    max_upload_speed: int = 0  # bytes/second; 0 means unlimited
    slots: int = DEFAULT_UPLOAD_SLOTS
    optimistic_unchoke: bool = True
    optimistic_interval: float = DEFAULT_OPTIMISTIC_UNCHOKE_INTERVAL
    choke_interval: float = DEFAULT_CHOKE_INTERVAL
    snub_seconds: float = DEFAULT_SNUB_SECONDS
    max_requests_per_peer: int = DEFAULT_MAX_REQUESTS_PER_PEER
    queue_timeout: float = DEFAULT_UPLOAD_QUEUE_TIMEOUT


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Where and how data is written to disk."""

    download_directory: Path = Path(DEFAULT_DOWNLOAD_DIRECTORY)
    state_directory: Path = Path("data/state")
    preallocate_files: bool = True
    verify_before_write: bool = True
    resume_autosave_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Tracker announce behaviour."""

    announce_interval_multiplier: float = 1.0
    min_announce_interval: int = 60
    max_announce_interval: int = 1800
    http_timeout: float = 15.0
    udp_timeout: float = 8.0
    max_retries: int = 3


@dataclass(frozen=True, slots=True)
class DhtConfig:
    """Distributed hash table (BEP 5).

    Off by default, not because it is unfinished but because it needs outbound
    UDP: on a network where that is blocked, enabling it buys a socket that
    answers nothing. Magnet links still resolve through their trackers and
    ``x.pe`` peers without it.
    """

    enabled: bool = False
    port: int = DEFAULT_LISTEN_PORT
    bootstrap_nodes: tuple[tuple[str, int], ...] = DEFAULT_DHT_BOOTSTRAP_NODES
    announce_interval: float = DEFAULT_DHT_ANNOUNCE_INTERVAL


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Log formatting and destinations."""

    level: str = "INFO"
    directory: Path | None = None
    json_format: bool = False
    max_bytes: int = 5_000_000
    backup_count: int = 3


@dataclass(frozen=True, slots=True)
class StatsConfig:
    """Statistics sampling and history."""

    window_seconds: float = DEFAULT_STATS_WINDOW_SECONDS
    sample_interval: float = 1.0
    history_samples: int = DEFAULT_HISTORY_SAMPLES


@dataclass(frozen=True, slots=True)
class UiConfig:
    """Presentation preferences."""

    theme: str = "dark"
    reduced_motion: bool = False
    update_interval_ms: int = 500
    show_splash: bool = True


# --------------------------------------------------------------------- config


@dataclass(frozen=True, slots=True)
class Config:
    """The complete application configuration."""

    network: NetworkConfig = field(default_factory=NetworkConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    dht: DhtConfig = field(default_factory=DhtConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    config_version: int = CONFIG_VERSION

    # ------------------------------------------------------------ serialise

    def to_mapping(self) -> dict[str, Any]:
        """Render the configuration as a JSON-serialisable dictionary."""
        mapping: dict[str, Any] = {}
        for section in fields(self):
            if section.name == "config_version":
                continue
            mapping[section.name] = _section_to_mapping(getattr(self, section.name))
        mapping["config_version"] = self.config_version
        return mapping

    def save(self, path: str | Path) -> Path:
        """Write the configuration to ``path`` atomically.

        Returns:
            The path written.
        """
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_mapping(), indent=2, sort_keys=True) + "\n"
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
        return target

    # ---------------------------------------------------------- deserialise

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Config:
        """Build a :class:`Config` from a decoded JSON mapping.

        Raises:
            ConfigError: On unknown sections/keys, wrong types, or out-of-range
                values.
        """
        if not isinstance(data, Mapping):
            raise ConfigError(f"configuration must be an object, got {type(data).__name__}")

        unknown = set(data) - {section.name for section in fields(cls)}
        if unknown:
            raise ConfigError(f"unknown configuration section(s): {sorted(unknown)}")

        return cls(
            network=_build_section(NetworkConfig, data, "network"),
            download=_build_section(DownloadConfig, data, "download"),
            storage=_build_section(StorageConfig, data, "storage"),
            tracker=_build_section(TrackerConfig, data, "tracker"),
            dht=_build_section(DhtConfig, data, "dht"),
            logging=_build_section(LoggingConfig, data, "logging"),
            stats=_build_section(StatsConfig, data, "stats"),
            ui=_build_section(UiConfig, data, "ui"),
            config_version=_pop_int(dict(data), "config_version", CONFIG_VERSION, minimum=1),
        )

    @classmethod
    def load(cls, path: str | Path) -> Config:
        """Load configuration from ``path``.

        Raises:
            ConfigError: If the file is unreadable, not valid JSON, or invalid.
        """
        target = Path(path).expanduser()
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ConfigError(f"cannot read configuration {target}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"configuration {target} is not valid JSON: {exc}") from exc
        return cls.from_mapping(data)

    @classmethod
    def load_or_default(cls, path: str | Path | None = None) -> Config:
        """Load configuration, falling back to defaults on any problem.

        A broken config must never stop the client from starting: the user can
        then fix it (or regenerate it) from the settings screen.
        """
        target = Path(path) if path is not None else default_config_path()
        try:
            return cls.load(target)
        except FileNotFoundError:
            logger.info("no configuration at %s; using defaults", target)
        except ConfigError as exc:
            logger.warning("ignoring invalid configuration at %s: %s", target, exc)
        return cls()

    # ------------------------------------------------------------ overrides

    def with_overrides(self, **sections: Mapping[str, Any]) -> Config:
        """Return a copy with the given section values replaced.

        Used by the CLI (``--download-dir ...``) and by tests.

        Raises:
            ConfigError: On unknown sections or keys.
        """
        current = self.to_mapping()
        for name, overrides in sections.items():
            if not hasattr(self, name):
                raise ConfigError(f"unknown configuration section: {name!r}")
            if not isinstance(overrides, Mapping):
                raise ConfigError(f"overrides for {name!r} must be a mapping")
            merged = dict(current.get(name, {}))
            merged.update(overrides)
            current[name] = merged
        return Config.from_mapping(current)


def default_config_path() -> Path:
    """Resolve the default configuration file location."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / CONFIG_DIRECTORY_NAME / CONFIG_FILENAME


# ------------------------------------------------------------------ internals


def _section_to_mapping(section: Any) -> dict[str, Any]:
    """Convert one config section into JSON-safe primitives."""
    result: dict[str, Any] = {}
    for item in fields(section):
        value = getattr(section, item.name)
        if isinstance(value, Path):
            result[item.name] = str(value)
        elif isinstance(value, Enum):
            result[item.name] = value.value
        elif isinstance(value, tuple):
            result[item.name] = [list(entry) for entry in value]
        else:
            result[item.name] = value
    return result


def _build_section(section_type: type, data: Mapping[str, Any], name: str) -> Any:
    """Build one config section from its mapping, rejecting unknown keys."""
    source = data.get(name, {})
    if not isinstance(source, Mapping):
        raise ConfigError(f"'{name}' must be an object, got {type(source).__name__}")

    remaining = dict(source)
    known = {item.name for item in fields(section_type)}
    unknown = set(remaining) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in '{name}': {sorted(unknown)}")

    values: dict[str, Any] = {}
    for item in fields(section_type):
        key = item.name
        default = getattr(section_type(), key)
        if isinstance(default, bool):
            values[key] = _pop_bool(remaining, key, default, section=name)
        elif isinstance(default, int):
            minimum, maximum = _INT_RANGES.get((name, key), (None, None))
            values[key] = _pop_int(
                remaining, key, default, section=name, minimum=minimum, maximum=maximum
            )
        elif isinstance(default, float):
            low, high = _FLOAT_RANGES.get((name, key), (None, None))
            values[key] = _pop_float(
                remaining, key, default, section=name, minimum=low, maximum=high
            )
        elif isinstance(default, Path):
            values[key] = _pop_path(remaining, key, default, section=name)
        elif isinstance(default, Enum):
            values[key] = _pop_enum(remaining, key, type(default), default, section=name)
        elif isinstance(default, tuple):
            values[key] = _pop_host_port_list(remaining, key, default, section=name)
        elif default is None:
            values[key] = _pop_optional_path(remaining, key, section=name)
        else:
            values[key] = _pop_str(remaining, key, str(default), section=name)
    return section_type(**values)


def _label(section: str, key: str) -> str:
    return f"{section}.{key}"


def _pop_bool(source: dict[str, Any], key: str, default: bool, *, section: str) -> bool:
    if key not in source:
        return default
    value = source.pop(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{_label(section, key)} must be true or false, got {value!r}")
    return value


def _pop_int(
    source: dict[str, Any],
    key: str,
    default: int,
    *,
    section: str = "",
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if key not in source:
        return default
    value = source.pop(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{_label(section, key)} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{_label(section, key)} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{_label(section, key)} must be at most {maximum}, got {value}")
    return value


def _pop_float(
    source: dict[str, Any],
    key: str,
    default: float,
    *,
    section: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if key not in source:
        return default
    value = source.pop(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{_label(section, key)} must be a number, got {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"{_label(section, key)} must be at least {minimum}, got {number}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{_label(section, key)} must be at most {maximum}, got {number}")
    return number


def _pop_str(source: dict[str, Any], key: str, default: str, *, section: str) -> str:
    if key not in source:
        return default
    value = source.pop(key)
    if not isinstance(value, str):
        raise ConfigError(f"{_label(section, key)} must be a string, got {value!r}")
    return value


def _pop_enum(
    source: dict[str, Any],
    key: str,
    enum_type: type[Enum],
    default: Enum,
    *,
    section: str,
) -> Enum:
    if key not in source:
        return default
    value = source.pop(key)
    if not isinstance(value, str):
        raise ConfigError(f"{_label(section, key)} must be a string, got {value!r}")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ConfigError(
            f"{_label(section, key)} must be one of: {allowed}; got {value!r}"
        ) from exc


def _pop_path(source: dict[str, Any], key: str, default: Path, *, section: str) -> Path:
    if key not in source:
        return default
    value = source.pop(key)
    if not isinstance(value, str):
        raise ConfigError(f"{_label(section, key)} must be a string path, got {value!r}")
    if not value.strip():
        raise ConfigError(f"{_label(section, key)} must not be empty")
    return Path(value).expanduser()


def _pop_optional_path(source: dict[str, Any], key: str, *, section: str) -> Path | None:
    if key not in source:
        return None
    value = source.pop(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{_label(section, key)} must be a string path or null, got {value!r}")
    return Path(value).expanduser()


def _pop_host_port_list(
    source: dict[str, Any],
    key: str,
    default: tuple[tuple[str, int], ...],
    *,
    section: str,
) -> tuple[tuple[str, int], ...]:
    if key not in source:
        return default
    value = source.pop(key)
    if not isinstance(value, list):
        raise ConfigError(f"{_label(section, key)} must be a list of [host, port] pairs")

    entries: list[tuple[str, int]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ConfigError(
                f"{_label(section, key)}[{index}] must be a [host, port] pair, got {entry!r}"
            )
        host, port = entry
        if not isinstance(host, str) or not host:
            raise ConfigError(f"{_label(section, key)}[{index}] host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{_label(section, key)}[{index}] port must be 1-65535, got {port!r}")
        entries.append((host, port))
    return tuple(entries)


def replace_section(config: Config, name: str, **values: Any) -> Config:
    """Return a copy of ``config`` with one section's values replaced.

    Raises:
        ConfigError: On unknown sections or keys.
    """
    if not hasattr(config, name):
        raise ConfigError(f"unknown configuration section: {name!r}")
    section = getattr(config, name)
    unknown = set(values) - {item.name for item in fields(section)}
    if unknown:
        raise ConfigError(f"unknown key(s) in '{name}': {sorted(unknown)}")
    return replace(config, **{name: replace(section, **values)})
