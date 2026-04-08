![streamrip logo](https://github.com/cbkii/streamrippa/blob/dev/demo/logo.svg?raw=true)

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/python/black)

A scriptable stream downloader for Qobuz, Tidal, Deezer and SoundCloud.

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
- **Session summary** — every run ends with a concise table: succeeded / failed / skipped / retried / validation-failures
- **Non-zero exit codes** — the process exits with code 1 when any item fails, making scripted/CI use reliable
- **`--fail-fast`** — stop after the first failure for strict pipelines

## Installation

First, ensure [Python](https://www.python.org/downloads/) (version 3.10 or greater) and [pip](https://pip.pypa.io/en/stable/installing/) are installed. Then install `ffmpeg`. You may choose not to install this, but some functionality will be limited.

This fork is **not published to PyPI**. Install from the wheel (`.whl`) attached to the [latest GitHub Release](https://github.com/cbkii/streamrippa/releases/latest):

```bash
# 1. Download the .whl file from the Releases page, then install it:
pip3 install streamrip-<version>-py3-none-any.whl
```

Or install directly from the GitHub Release URL (replace `<version>` and the tag with the actual release values):

```bash
pip3 install https://github.com/cbkii/streamrippa/releases/download/v<version>/streamrip-<version>-py3-none-any.whl
```

### Updating

To update to a newer release, download the new wheel from the [Releases page](https://github.com/cbkii/streamrippa/releases) and reinstall:

```bash
pip3 install --upgrade streamrip-<new-version>-py3-none-any.whl
```

If you run into issues, or want the absolute latest development build, install directly from the `dev` branch:

```bash
pip3 install git+https://github.com/cbkii/streamrippa.git@dev
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

If you're confused about anything, see the help pages. The main help pages can be accessed by typing `rip` by itself in the command line. The help pages for each command can be accessed with the `--help` flag. For example, to see the help page for the `url` command, type

```
rip url --help
```

![example_help_page.png](https://github.com/cbkii/streamrippa/blob/dev/demo/example_help_page.png?raw=true)

## Other information

For more in-depth information about `streamrip`, see the help pages and the [wiki](https://github.com/nathom/streamrip/wiki/).

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
