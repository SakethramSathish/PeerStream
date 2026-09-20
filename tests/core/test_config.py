"""Unit tests for application configuration.

The contract being tested: a config file is either understood completely or
rejected loudly, a partial file is completed with defaults, and a broken file
never prevents the client from starting.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from app.core.config import (
    Config,
    ConfigError,
    DhtConfig,
    DownloadConfig,
    LoggingConfig,
    NetworkConfig,
    PieceStrategy,
    StatsConfig,
    StorageConfig,
    TrackerConfig,
    UiConfig,
    default_config_path,
    replace_section,
)
from app.core.constants import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_DOWNLOAD_DIRECTORY,
    DEFAULT_LISTEN_PORT,
    MAX_BLOCK_SIZE,
)


class TestDefaults:
    def test_defaults_are_sane(self) -> None:
        config = Config()
        assert config.network.listen_port == DEFAULT_LISTEN_PORT
        assert config.download.piece_strategy is PieceStrategy.RAREST_FIRST
        assert config.download.block_size == DEFAULT_BLOCK_SIZE
        assert config.storage.download_directory == Path(DEFAULT_DOWNLOAD_DIRECTORY)
        assert config.dht.enabled is False  # off by default: it needs outbound UDP
        assert config.logging.level == "INFO"

    def test_default_path_uses_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITTORRENT_CLIENT_CONFIG", str(tmp_path / "custom.json"))
        assert default_config_path() == tmp_path / "custom.json"

    def test_default_path_uses_xdg_config_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BITTORRENT_CLIENT_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert default_config_path() == tmp_path / "bittorrent-client" / "config.json"

    def test_default_path_falls_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BITTORRENT_CLIENT_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert default_config_path().name == "config.json"
        assert ".config" in default_config_path().parts


class TestRoundTrip:
    def test_save_then_load_preserves_every_value(self, tmp_path: Path) -> None:
        config = Config(
            network=NetworkConfig(listen_port=51413, max_peers_per_torrent=25),
            download=DownloadConfig(piece_strategy=PieceStrategy.SEQUENTIAL, block_size=32768),
            storage=StorageConfig(download_directory=Path("/tmp/downloads")),
            tracker=TrackerConfig(http_timeout=7.5),
            dht=DhtConfig(enabled=True, bootstrap_nodes=(("router.example", 6881),)),
            logging=LoggingConfig(level="DEBUG", directory=Path("/tmp/logs")),
            stats=StatsConfig(history_samples=120),
            ui=UiConfig(theme="light", reduced_motion=True),
        )
        path = config.save(tmp_path / "nested" / "config.json")
        assert path.exists()

        restored = Config.load(path)
        assert restored == config
        assert restored.network.listen_port == 51413
        assert restored.download.piece_strategy is PieceStrategy.SEQUENTIAL
        assert restored.dht.bootstrap_nodes == (("router.example", 6881),)

    def test_saved_json_contains_every_section(self, tmp_path: Path) -> None:
        path = Config().save(tmp_path / "config.json")
        data = json.loads(path.read_text())
        assert set(data) == {
            "network",
            "download",
            "upload",
            "storage",
            "tracker",
            "dht",
            "logging",
            "stats",
            "ui",
            "config_version",
        }

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        path = Config().save(tmp_path / "a" / "b" / "config.json")
        assert path.is_file()

    def test_save_leaves_no_temporary_files(self, tmp_path: Path) -> None:
        path = Config().save(tmp_path / "config.json")
        assert sorted(item.name for item in tmp_path.iterdir()) == ["config.json"]
        assert path.stat().st_size > 0

    def test_to_mapping_round_trips_through_json(self) -> None:
        payload = json.dumps(Config().to_mapping())
        restored = Config.from_mapping(json.loads(payload))

        # "~" is expanded when a config is loaded, so the first round trip
        # differs from the raw defaults by exactly that expansion.
        expected = replace_section(
            Config(),
            "storage",
            download_directory=Path(DEFAULT_DOWNLOAD_DIRECTORY).expanduser(),
        )
        assert restored == expected

    def test_round_trip_is_stable_after_one_pass(self) -> None:
        once = Config.from_mapping(json.loads(json.dumps(Config().to_mapping())))
        twice = Config.from_mapping(json.loads(json.dumps(once.to_mapping())))
        assert once == twice


class TestPartialFiles:
    def test_missing_keys_use_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"network": {"listen_port": 9000}}))
        config = Config.load(path)

        assert config.network.listen_port == 9000
        assert config.network.max_peers_per_torrent == NetworkConfig().max_peers_per_torrent
        assert config.download == DownloadConfig()

    def test_empty_object_is_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{}")
        assert Config.load(path) == Config()


class TestValidation:
    def test_rejects_unknown_section(self) -> None:
        with pytest.raises(ConfigError, match="unknown configuration section"):
            Config.from_mapping({"networkz": {}})

    def test_rejects_unknown_key(self) -> None:
        with pytest.raises(ConfigError, match=r"unknown key\(s\) in 'network'"):
            Config.from_mapping({"network": {"listen_pot": 6881}})

    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            ("network", "listen_port", "6881"),
            ("network", "accept_incoming_connections", 1),
            ("download", "block_size", True),
            ("download", "endgame_enabled", "yes"),
            ("logging", "directory", 42),
            ("dht", "bootstrap_nodes", "router.example:6881"),
        ],
    )
    def test_rejects_wrong_types(self, section: str, key: str, value: object) -> None:
        with pytest.raises(ConfigError):
            Config.from_mapping({section: {key: value}})

    def test_rejects_out_of_range_port(self) -> None:
        with pytest.raises(ConfigError, match="at most 65535"):
            Config.from_mapping({"network": {"listen_port": 70000}})

    def test_rejects_block_size_above_protocol_maximum(self) -> None:
        with pytest.raises(ConfigError, match=f"at most {MAX_BLOCK_SIZE}"):
            Config.from_mapping({"download": {"block_size": MAX_BLOCK_SIZE + 1}})

    def test_rejects_non_integer_config_version(self) -> None:
        with pytest.raises(ConfigError, match="at least 1"):
            Config.from_mapping({"config_version": 0})

    def test_rejects_invalid_piece_strategy(self) -> None:
        with pytest.raises(ConfigError, match="must be one of"):
            Config.from_mapping({"download": {"piece_strategy": "greediest_first"}})

    def test_rejects_non_mapping_section(self) -> None:
        with pytest.raises(ConfigError, match="must be an object"):
            Config.from_mapping({"network": [1, 2, 3]})

    def test_rejects_non_mapping_document(self) -> None:
        with pytest.raises(ConfigError, match="must be an object"):
            Config.from_mapping([1, 2, 3])  # type: ignore[arg-type]

    def test_rejects_malformed_bootstrap_node(self) -> None:
        with pytest.raises(ConfigError, match=r"\[0\] must be a \[host, port\] pair"):
            Config.from_mapping({"dht": {"bootstrap_nodes": ["router.example"]}})

    def test_rejects_bootstrap_port_out_of_range(self) -> None:
        with pytest.raises(ConfigError, match="port must be 1-65535"):
            Config.from_mapping({"dht": {"bootstrap_nodes": [["router.example", 99999]]}})

    def test_rejects_empty_download_directory(self) -> None:
        with pytest.raises(ConfigError, match="must not be empty"):
            Config.from_mapping({"storage": {"download_directory": "   "}})

    @pytest.mark.parametrize(
        ("section", "key", "value", "expected"),
        [
            ("tracker", "http_timeout", "fast", "must be a number"),
            ("tracker", "http_timeout", 0.01, "at least 0.5"),
            ("tracker", "http_timeout", 10_000, "at most 300.0"),
            ("logging", "level", 10, "must be a string"),
            ("download", "piece_strategy", 1, "must be a string"),
            ("storage", "download_directory", 42, "must be a string path"),
        ],
    )
    def test_rejects_invalid_scalar_values(
        self, section: str, key: str, value: object, expected: str
    ) -> None:
        with pytest.raises(ConfigError, match=expected):
            Config.from_mapping({section: {key: value}})

    def test_rejects_non_string_bootstrap_host(self) -> None:
        with pytest.raises(ConfigError, match="host must be a non-empty string"):
            Config.from_mapping({"dht": {"bootstrap_nodes": [[123, 6881]]}})


class TestLoadOrDefault:
    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        assert Config.load_or_default(tmp_path / "absent.json") == Config()

    def test_invalid_json_uses_defaults(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("{not json")
        assert Config.load_or_default(path) == Config()
        assert "not valid JSON" in caplog.text

    def test_invalid_config_uses_defaults(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"network": {"listen_port": "nope"}}))
        assert Config.load_or_default(path) == Config()
        assert "ignoring invalid configuration" in caplog.text

    def test_valid_file_is_used(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"network": {"listen_port": 12345}}))
        assert Config.load_or_default(path).network.listen_port == 12345

    def test_load_raises_filenotfound_for_a_missing_file(self, tmp_path: Path) -> None:
        """A missing file is distinguishable from a broken one."""
        with pytest.raises(FileNotFoundError):
            Config.load(tmp_path / "absent.json")

    def test_load_reports_unreadable_files(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{}")
        path.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="cannot read configuration"):
                Config.load(path)
        finally:
            path.chmod(0o644)

    def test_missing_file_logs_at_info_level(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            Config.load_or_default(tmp_path / "absent.json")
        assert "no configuration at" in caplog.text


class TestOverrides:
    def test_with_overrides_changes_only_given_values(self) -> None:
        config = Config().with_overrides(network={"listen_port": 7000})
        assert config.network.listen_port == 7000
        assert config.network.max_peers_per_torrent == NetworkConfig().max_peers_per_torrent

    def test_with_overrides_rejects_unknown_section(self) -> None:
        with pytest.raises(ConfigError, match="unknown configuration section"):
            Config().with_overrides(networking={})

    def test_with_overrides_rejects_non_mapping(self) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            Config().with_overrides(network=6881)  # type: ignore[arg-type]

    def test_replace_section_updates_one_section(self) -> None:
        config = replace_section(Config(), "download", max_download_speed=1024)
        assert config.download.max_download_speed == 1024
        assert config.network == NetworkConfig()

    def test_replace_section_rejects_unknown_key(self) -> None:
        with pytest.raises(ConfigError, match=r"unknown key\(s\) in 'download'"):
            replace_section(Config(), "download", max_speed=1024)

    def test_replace_section_rejects_unknown_section(self) -> None:
        with pytest.raises(ConfigError, match="unknown configuration section"):
            replace_section(Config(), "networking", listen_port=1)


class TestPaths:
    def test_download_directory_expands_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        config = Config.from_mapping({"storage": {"download_directory": "~/Downloads"}})
        assert config.storage.download_directory == tmp_path / "Downloads"
        assert config.storage.download_directory.is_absolute()

    def test_logging_directory_may_be_null(self) -> None:
        config = Config.from_mapping({"logging": {"directory": None}})
        assert config.logging.directory is None
