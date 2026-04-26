import os
import shutil

import pytest
import tomlkit

from streamrip.config import (
    ArtworkConfig,
    CliConfig,
    Config,
    ConfigData,
    ConversionConfig,
    CsvResolverConfig,
    DatabaseConfig,
    DeezerConfig,
    DownloadsConfig,
    FilepathsConfig,
    LastFmConfig,
    MetadataConfig,
    MiscConfig,
    QobuzConfig,
    QobuzDiscographyFilterConfig,
    ReliabilityConfig,
    SoundcloudConfig,
    TidalConfig,
    YoutubeConfig,
    _get_dict_keys_r,
    _nested_set,
    update_config,
)

SAMPLE_CONFIG = "tests/test_config.toml"
OLD_CONFIG = "tests/test_config_old.toml"


# Define a fixture to create a sample ConfigData instance for testing
@pytest.fixture()
def sample_config_data() -> ConfigData:
    # Create a sample ConfigData instance here
    # You can customize this to your specific needs for testing
    with open(SAMPLE_CONFIG) as f:
        config_data = ConfigData.from_toml(f.read())
    return config_data


# Define a fixture to create a sample Config instance for testing
@pytest.fixture()
def sample_config() -> Config:
    # Create a sample Config instance here
    # You can customize this to your specific needs for testing
    config = Config(SAMPLE_CONFIG)
    return config


def test_get_keys_r():
    d = {
        "key1": {
            "key2": {
                "key3": 1,
                "key4": 1,
            },
            "key6": [1, 2],
            5: 1,
        }
    }
    res = _get_dict_keys_r(d)
    print(res)
    assert res == {
        ("key1", "key2", "key3"),
        ("key1", "key2", "key4"),
        ("key1", "key6"),
        ("key1", 5),
    }


def test_safe_set():
    d = {
        "key1": {
            "key2": {
                "key3": 1,
                "key4": 1,
            },
            "key6": [1, 2],
            5: 1,
        }
    }
    _nested_set(d, "key1", "key2", "key3", val=5)
    assert d == {
        "key1": {
            "key2": {
                "key3": 5,
                "key4": 1,
            },
            "key6": [1, 2],
            5: 1,
        }
    }


def test_config_update():
    old = {
        "downloads": {"folder": "some_path", "use_service": True},
        "qobuz": {"email": "asdf@gmail.com", "password": "test"},
        "legacy_conf": {"something": 1, "other": 2},
    }
    new = {
        "downloads": {"folder": "", "use_service": False, "keep_artwork": True},
        "qobuz": {"email": "", "password": ""},
        "tidal": {"email": "", "password": ""},
    }
    update_config(old, new)
    assert new == {
        "downloads": {"folder": "some_path", "use_service": True, "keep_artwork": True},
        "qobuz": {"email": "asdf@gmail.com", "password": "test"},
        "tidal": {"email": "", "password": ""},
    }


def test_config_throws_outdated():
    with pytest.raises(Exception, match="update"):
        _ = Config(OLD_CONFIG)


def test_config_file_update():
    tmp_conf = "tests/test_config_old2.toml"
    shutil.copy("tests/test_config_old.toml", tmp_conf)
    Config._update_file(tmp_conf, SAMPLE_CONFIG)

    with open(tmp_conf) as f:
        s = f.read()
        toml = tomlkit.parse(s)  # type: ignore

    assert toml["downloads"]["folder"] == "old_value"  # type: ignore
    assert toml["downloads"]["source_subdirectories"] is True  # type: ignore
    assert toml["downloads"]["concurrency"] is True  # type: ignore
    assert toml["downloads"]["max_connections"] == 6  # type: ignore
    assert toml["downloads"]["requests_per_minute"] == 60  # type: ignore
    assert toml["cli"]["text_output"] is True  # type: ignore
    assert toml["cli"]["progress_bars"] is True  # type: ignore
    assert toml["cli"]["max_search_results"] == 100  # type: ignore
    assert toml["misc"]["version"] == "2.3.0"  # type: ignore
    assert "YouTubeVideos" in str(toml["youtube"]["video_downloads_folder"])
    # type: ignore
    os.remove("tests/test_config_old2.toml")


def test_sample_config_data_properties(sample_config_data):
    # Test the properties of ConfigData
    assert sample_config_data.modified is False  # Ensure initial state is not modified


def test_sample_config_data_modification(sample_config_data):
    # Test modifying ConfigData and checking modified property
    sample_config_data.set_modified()
    assert sample_config_data._modified is True


def test_sample_config_data_fields(sample_config_data):
    test_config = ConfigData(
        toml=None,  # type: ignore
        downloads=DownloadsConfig(
            folder="test_folder",
            source_subdirectories=False,
            disc_subdirectories=True,
            concurrency=True,
            max_connections=6,
            requests_per_minute=60,
            verify_ssl=True,
            api_connect_timeout_seconds=15.0,
            api_read_timeout_seconds=120.0,
            api_request_retries=2,
            api_retry_delay_seconds=0.75,
        ),
        qobuz=QobuzConfig(
            use_auth_token=False,
            email_or_userid="test@gmail.com",
            password_or_token="test_pwd",
            app_id="12345",
            quality=3,
            download_booklets=False,
            secrets=["secret1", "secret2"],
        ),
        tidal=TidalConfig(
            user_id="userid",
            country_code="countrycode",
            access_token="accesstoken",
            refresh_token="refreshtoken",
            token_expiry="tokenexpiry",
            quality=3,
            download_videos=True,
        ),
        deezer=DeezerConfig(
            arl="testarl",
            quality=2,
            lower_quality_if_not_available=True,
            use_deezloader=False,
            deezloader_warnings=False,
        ),
        soundcloud=SoundcloudConfig(
            client_id="clientid",
            app_version="appversion",
            quality=0,
        ),
        youtube=YoutubeConfig(
            video_downloads_folder="videodownloadsfolder",
            quality=0,
            download_videos=False,
        ),
        lastfm=LastFmConfig(source="qobuz", fallback_source="deezer"),
        filepaths=FilepathsConfig(
            add_singles_to_folder=False,
            folder_format="{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]",
            track_format="{tracknumber}. {artist} - {title}{explicit}",
            restrict_characters=False,
            truncate_to=120,
        ),
        artwork=ArtworkConfig(
            embed=True,
            embed_size="large",
            embed_max_width=-1,
            save_artwork=True,
            saved_max_width=-1,
        ),
        metadata=MetadataConfig(
            set_playlist_to_album=True,
            renumber_playlist_tracks=True,
            exclude=[],
            exportify_tag_map={
                "Genres": "genre",
                "Loudness": "exportify_loudness",
                "Tempo": "tempo",
            },
        ),
        qobuz_filters=QobuzDiscographyFilterConfig(
            extras=False,
            repeats=False,
            non_albums=False,
            features=False,
            non_studio_albums=False,
            non_remaster=False,
        ),
        cli=CliConfig(
            text_output=False,
            progress_bars=False,
            max_search_results=100,
        ),
        database=DatabaseConfig(
            downloads_enabled=True,
            downloads_path="downloadspath",
            failed_downloads_enabled=True,
            failed_downloads_path="faileddownloadspath",
            failed_downloads_log_path="faileddownloadslogpath",
        ),
        reliability=ReliabilityConfig(
            retry_count=3,
            retry_delay=2.0,
            retry_backoff_factor=2.0,
            fail_fast=False,
            validate_flac=True,
        ),
        conversion=ConversionConfig(
            enabled=False,
            codec="ALAC",
            sampling_rate=48000,
            bit_depth=24,
            lossy_bitrate=320,
        ),
        csv_resolver=CsvResolverConfig(
            search_inflight_per_provider=3,
            metadata_inflight_per_provider=2,
            url_inflight_per_provider=2,
            provider_min_interval_seconds=0.2,
            cooldown_base_seconds=10.0,
            cooldown_max_seconds=120.0,
            failure_streak_for_cooldown=4,
            escalation_search_limit=15,
            default_source="qobuz",
            default_fallback_source="deezer",
            local_skip_enabled=False,
            local_skip_paths=[],
            local_skip_extensions=["flac", "mp3", "m4a", "ogg", "opus", "aac"],
            local_skip_require_duration_check=True,
            local_skip_duration_tolerance_ratio=0.20,
            local_skip_duration_tolerance_seconds=12,
            local_skip_max_file_scan=25000,
            variant_policy_enabled=True,
            live_mode="reject",
            acoustic_mode="reject",
            instrumental_mode="reject",
            radio_edit_mode="penalty",
            remaster_mode="equivalent",
            year_ignore_for_remaster=True,
            reject_bad_context_releases=True,
            bad_context_fields=["title", "album"],
            acceptance_threshold_by_source={},
            telemetry_jsonl_path="",
            enable_guarded_fuzzy_normal=False,
        ),
        misc=MiscConfig(version="2.0", check_for_updates=True),
        _modified=False,
    )
    assert sample_config_data.downloads == test_config.downloads
    assert sample_config_data.qobuz == test_config.qobuz
    assert sample_config_data.tidal == test_config.tidal
    assert sample_config_data.deezer == test_config.deezer
    assert sample_config_data.soundcloud == test_config.soundcloud
    assert sample_config_data.youtube == test_config.youtube
    assert sample_config_data.lastfm == test_config.lastfm
    assert sample_config_data.artwork == test_config.artwork
    assert sample_config_data.filepaths == test_config.filepaths
    assert sample_config_data.metadata == test_config.metadata
    assert sample_config_data.qobuz_filters == test_config.qobuz_filters
    assert sample_config_data.database == test_config.database
    assert sample_config_data.reliability == test_config.reliability
    assert sample_config_data.conversion == test_config.conversion
    assert sample_config_data.csv_resolver == test_config.csv_resolver


def test_config_update_on_save():
    tmp_config_path = "tests/config2.toml"
    shutil.copy(SAMPLE_CONFIG, tmp_config_path)
    conf = Config(tmp_config_path)
    conf.file.downloads.folder = "new_folder"
    conf.file.set_modified()
    conf.save_file()
    conf2 = Config(tmp_config_path)
    os.remove(tmp_config_path)

    assert conf2.session.downloads.folder == "new_folder"


def test_config_dont_update_without_set_modified():
    tmp_config_path = "tests/config2.toml"
    shutil.copy(SAMPLE_CONFIG, tmp_config_path)
    conf = Config(tmp_config_path)
    conf.file.downloads.folder = "new_folder"
    del conf
    conf2 = Config(tmp_config_path)
    os.remove(tmp_config_path)

    assert conf2.session.downloads.folder == "test_folder"


def test_config_backfills_missing_metadata_exportify_tag_map():
    with open(SAMPLE_CONFIG) as f:
        toml = tomlkit.parse(f.read())

    del toml["metadata"]["exportify_tag_map"]  # type: ignore
    cfg = ConfigData.from_toml(tomlkit.dumps(toml))

    assert cfg.metadata.exportify_tag_map == {
        "Genres": "genre",
        "Loudness": "exportify_loudness",
        "Tempo": "tempo",
    }


def test_config_backfills_missing_metadata_section():
    with open(SAMPLE_CONFIG) as f:
        toml = tomlkit.parse(f.read())

    del toml["metadata"]  # type: ignore
    cfg = ConfigData.from_toml(tomlkit.dumps(toml))

    assert cfg.metadata.exclude == []
    assert cfg.metadata.exportify_tag_map == {
        "Genres": "genre",
        "Loudness": "exportify_loudness",
        "Tempo": "tempo",
    }


# Other tests for the Config class can be added as needed

if __name__ == "__main__":
    pytest.main()
