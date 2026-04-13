![streamrip logo](https://github.com/cbkii/streamrippa/blob/dev/demo/logo.svg?raw=true)

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/python/black)

A scriptable stream downloader for Qobuz, Tidal, Deezer and SoundCloud.

> Naming note: this repository is **streamrippa** (`cbkii/streamrippa`), while the installable Python package and CLI name remain **streamrip** / `rip` for compatibility.

![downloading an album](https://github.com/cbkii/streamrippa/blob/dev/demo/download_album.png?raw=true)

## Features

- Fast, concurrent downloads powered by `aiohttp`
- Downloads tracks, albums, playlists, discographies, and labels from Qobuz, Tidal, Deezer, and SoundCloud
- Supports downloads of Spotify and Apple Music playlists through [last.fm](https://www.last.fm)
- Automatically converts files to a preferred format
- Has a database that stores the downloaded tracks' IDs so that repeats are avoided
- Concurrency and rate limiting
- Interactive search for all sources
- Highly customizable through the config file
- Integration with `youtube-dl`

### Reliability features (batch / long-run workflows)

- **Continue-on-error** — one failed track or album never aborts the whole run; failures are logged and the run continues
- **Bounded retry with exponential backoff** — configurable retry count and delay per track download
- **FLAC integrity validation** — corrupt FLAC files are detected with `mutagen`, removed, and recorded for replay
- **Persistent failed-item tracking** — every failure is written to the SQLite failed-downloads database and an auditable CSV log
- **`rip repair`** — replay all previously failed items without redoing successful ones
- **`rip repair-csv`** — retry unresolved Exportify CSV rows with expanded search + fuzzy matching
- **Session summary** — every run ends with a concise table: succeeded / failed / skipped / retried / validation-failures
- **Non-zero exit codes** — the process exits with code 1 when any item fails, making scripted/CI use reliable
- **`--fail-fast`** — stop after the first failure for strict pipelines

## Installation

Ensure **Python 3.10+** is installed. `ffmpeg` is recommended; some features are limited without it.

This fork is **not published to PyPI**. Install it from release assets attached to the [latest GitHub Release](https://github.com/cbkii/streamrippa/releases/latest):

- wheel (`.whl`) — easiest for direct install/upgrade
- source distribution (`.tar.gz`) — useful for source-based packaging workflows

### Debian / Raspberry Pi OS packages

```bash
sudo apt update
sudo apt install -y python3-full python3-venv ffmpeg
```

If `pip` needs to build native dependencies from source on older Debian / Raspberry Pi OS images, install build tooling once:

```bash
sudo apt install -y build-essential python3-dev libffi-dev
```

> On Debian, Ubuntu, and Raspberry Pi OS, installing with `pip3` into the system Python may fail with an `externally-managed-environment` error. A virtual environment avoids this.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install /path/to/streamrip-<version>-py3-none-any.whl
```

Or install directly from a release asset URL:

```bash
python -m pip install "https://github.com/cbkii/streamrippa/releases/download/v<version>/streamrip-<version>-py3-none-any.whl"
```

### Verify

```bash
python -m pip show streamrip
rip --help
```

### Updating

To update to a newer release, download the new wheel from the [Releases page](https://github.com/cbkii/streamrippa/releases) and reinstall:

```bash
python -m pip install --upgrade ./streamrip-<new-version>-py3-none-any.whl
```

If you run into issues, or want the absolute latest development build, install directly from the `dev` branch:

```bash
python -m pip install git+https://github.com/cbkii/streamrippa.git@dev
```

When you type

```bash
rip
```

it should show the main help page. If you have no idea what these mean, or are having other issues installing, check out the [detailed installation instructions](https://github.com/nathom/streamrip/wiki#detailed-installation-instructions).

## Example Usage

**For Tidal and Qobuz, you NEED a premium subscription.**

Download an album from Qobuz

```bash
rip url https://www.qobuz.com/us-en/album/rumours-fleetwood-mac/0603497941032
```

Download multiple albums from Qobuz

```bash
rip url https://www.qobuz.com/us-en/album/back-in-black-ac-dc/0886444889841 https://www.qobuz.com/us-en/album/blue-train-john-coltrane/0060253764852
```

Download the album and convert it to `mp3`

```bash
rip --codec mp3 url https://open.qobuz.com/album/0060253780968
```

To set the maximum quality, use the `--quality` option to `0, 1, 2, 3, 4`:

| Quality ID | Audio Quality         | Available Sources                            |
| ---------- | --------------------- | -------------------------------------------- |
| 0          | 128 kbps MP3 or AAC   | Deezer, Tidal, SoundCloud (most of the time) |
| 1          | 320 kbps MP3 or AAC   | Deezer, Tidal, Qobuz, SoundCloud (rarely)    |
| 2          | 16 bit, 44.1 kHz (CD) | Deezer, Tidal, Qobuz, SoundCloud (rarely)    |
| 3          | 24 bit, ≤ 96 kHz      | Tidal (MQA), Qobuz, SoundCloud (rarely)      |
| 4          | 24 bit, ≤ 192 kHz     | Qobuz                                        |

```bash
rip --quality 3 url https://tidal.com/browse/album/147569387
```

> Using `4` is generally a waste of space. It is impossible for humans to perceive the difference between sampling rates higher than 44.1 kHz. It may be useful if you're processing/slowing down the audio.

Search for playlists matching `rap` on Tidal

```bash
rip search tidal playlist 'rap'
```

![streamrip interactive search](https://github.com/cbkii/streamrippa/blob/dev/demo/playlist_search.png?raw=true)

Search for *Rumours* on Tidal, and download it

```bash
rip search tidal album 'fleetwood mac rumours'
```

Download a last.fm playlist using the lastfm command

```
rip lastfm https://www.last.fm/user/nathan3895/playlists/12126195
```

For more customization, see the config file

```
rip config open
```

### Reliability and batch usage

By default streamrip will continue past any single failure in a batch run and print a summary at the end:

```
Session summary: 14 succeeded, 1 failed, 2 skipped
Failed download details logged to: ~/.config/streamrip/failed_downloads.csv
Run rip repair to retry failed downloads.
```

#### Retry failed downloads

```bash
rip repair
```

Reads the failed-downloads database and re-attempts each item.  When an item is successfully downloaded during repair it is **automatically removed from the failed store**, so subsequent `rip repair` runs do not re-queue it.  Items that were already successfully downloaded (present in the downloads DB) are skipped.  Running `rip repair` multiple times is safe.

#### Retry count and backoff (command line)

```bash
rip --retry 5 --retry-delay 3.0 url https://www.deezer.com/album/12345
```

The delay doubles between each attempt (`retry_backoff_factor`).  Both settings can also be set permanently in the `[reliability]` section of the config file.

#### Fail-fast mode

```bash
rip --fail-fast url https://www.deezer.com/album/12345
```

The run stops after the first **media item** (track, album, playlist) that records a failure.  In practice this means: if the failing item is an album or playlist, all tracks within that item are still attempted before the run halts — fail-fast acts at media-item granularity, not at individual track granularity inside a collection.

#### Disable FLAC validation

```bash
rip --no-validate-flac url https://www.deezer.com/album/12345
```

FLAC validation is **enabled by default**.  After each FLAC download, streamrip uses `mutagen` (already a dependency) to parse the file header and stream-info block.  **This catches obviously corrupt or truncated files where the header is unreadable, but it does not perform a full audio-frame decode.**  A file with valid headers but corrupted audio frames will pass this check.  If a file fails validation it is removed before being stored, and the failure is recorded for replay.

For a stronger guarantee you can run `flac --test` on completed downloads manually, or after a `rip repair` session.  Pass `--no-validate-flac` to skip the check entirely.

#### Reliability config reference

All settings live in the `[reliability]` section of the config file (`rip config open`):

| Key | Default | Description |
|-----|---------|-------------|
| `retry_count` | `3` | Number of retries per download (0 = no retry) |
| `retry_delay` | `2.0` | Initial delay in seconds before the first retry |
| `retry_backoff_factor` | `2.0` | Multiplier applied to delay after each retry |
| `fail_fast` | `false` | Stop immediately on first failure |
| `validate_flac` | `true` | Verify FLAC integrity; remove and record corrupt files |

#### Exit codes

| Exit code | Meaning |
|-----------|---------|
| `0` | All items succeeded (or were skipped as already downloaded) |
| `1` | At least one item failed |

This makes it straightforward to use streamrip in shell scripts or CI:

```bash
rip file urls.txt || notify_failure
```

### Exportify CSV mode

Download tracks from a Spotify playlist exported with [Exportify](https://exportify.net/).

**Export your Spotify playlist** from Exportify (downloads a `.csv` file), then:

```bash
# Auto-detect mode (tries JSON, then Exportify CSV, then URL list)
rip file Liked_Songs.csv

# Explicit CSV mode
rip file --list-mode exportify-csv Liked_Songs.csv

# Override search sources (default: uses [lastfm].source / [lastfm].fallback_source from config)
rip file --list-mode exportify-csv --source qobuz --fallback-source deezer Liked_Songs.csv
rip file --list-mode exportify-csv --source deezer --fallback-source qobuz Liked_Songs.csv
```

**Supported `--list-mode` values:**

| Value | Behaviour |
|-------|-----------|
| `auto` (default) | Tries JSON, then Exportify CSV header detection, then URL list |
| `json` | Force JSON mode (list of `{"source", "media_type", "id"}`) |
| `urls` | Force URL list mode (whitespace-separated service URLs) |
| `exportify-csv` | Force Exportify CSV mode |

**Service-first / quality-second fallback:**

For each CSV row, streamrip:

1. Searches both the primary and fallback services.
2. Scores results deterministically (exact ISRC > title+artist > title+album > year > first).
3. Tries the primary service first at its configured quality.
4. Falls back to the fallback service at the same pass quality before stepping down.
5. Only steps to a lower quality after **both** services have been tried at that pass.

**Rerunning is safe** — tracks already in the download database or on disk are skipped automatically.

**Unresolved tracks** (no match found on any service) are logged to a CSV file next to
the input file (e.g. `Liked_Songs_unresolved.csv`) for audit. These are separate from
provider-backed download failures tracked by `rip repair`.

#### Repair unresolved Exportify rows

```bash
rip repair-csv Liked_Songs_unresolved.csv
rip repair-csv --source qobuz --fallback-source deezer Liked_Songs_unresolved.csv
```

`rip repair-csv` re-runs unresolved rows using a larger search window and fuzzy title
matching, then writes a new audit file (`*_repair_unresolved.csv`) for anything still
unresolved. When unresolved logs include provider candidate IDs, repair mode attempts
those IDs first, then falls back to normal search/matching if they no longer resolve.
Re-running repair passes is safe and keeps logs separate per pass.

**Exportify column metadata mapping:**

By default, Exportify CSV columns are mapped into the downloaded file's tags:

| CSV Column | Tag |
|-----------|-----|
| `Genres` | `genre` (merged with provider genre, no duplicates) |
| `Loudness` | `exportify_loudness` (custom tag) |
| `Tempo` | `tempo` (custom tag) |

Metadata mapping is **best-effort**: failures are logged but never block a download.

Customise or disable the mapping in the config file (`rip config open`):

```toml
[metadata]
# Disable all extra metadata mapping:
exportify_tag_map = {}

# Custom mapping:
exportify_tag_map = { "Genres" = "genre", "Tempo" = "bpm", "Added At" = "spotify_added_at" }
```

If you're confused about anything, see the help pages. The main help pages can be accessed by typing `rip` by itself in the command line. The help pages for each command can be accessed with the `--help` flag. For example, to see the help page for the `url` command, type

```
rip url --help
```

![example_help_page.png](https://github.com/cbkii/streamrippa/blob/dev/demo/example_help_page.png?raw=true)

## Other information

For more in-depth information about this fork (`streamrippa`) and the `streamrip` CLI/package, see the help pages and the [upstream wiki](https://github.com/nathom/streamrip/wiki/).

## Contributions

All contributions are appreciated! You can help out the project by opening an issue
or by submitting code.

### Issues

If you're opening an issue **use the Feature Request or Bug Report templates properly**. This ensures
that I have all of the information necessary to debug the issue. If you do not follow the templates,
**I will silently close the issue** and you'll have to deal with it yourself.

### Code

If you're new to Git, follow these steps to open your first Pull Request (PR):

- Fork this repository
- Clone the new repository
- Commit your changes
- Open a pull request to the `dev` branch

Please document any functions or obscure lines of code.

### The Wiki

To help out `streamrip` users that may be having trouble, consider contributing some information to the [upstream wiki](https://github.com/nathom/streamrip/wiki).
Nothing is too obvious and everything is appreciated.

## Acknowledgements

Thanks to Vitiko98, Sorrow446, and DashLt for their contributions to this project, and the previous projects that made this one possible.

`streamrip` was inspired by:

- [qobuz-dl](https://github.com/vitiko98/qobuz-dl)
- [Qo-DL Reborn](https://github.com/badumbass/Qo-DL-Reborn)
- [Tidal-Media-Downloader](https://github.com/yaronzz/Tidal-Media-Downloader)
- [scdl](https://github.com/flyingrub/scdl)

## Disclaimer

I will not be responsible for how **you** use `streamrip`. By using `streamrip`, you agree to the terms and conditions of the Qobuz, Tidal, and Deezer APIs.

## Sponsorship

Consider becoming a Github sponsor for [nathom](https://github.com/sponsors/nathom), the original author of `streamrip`, if you enjoy this open source software.
