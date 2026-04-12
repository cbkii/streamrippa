import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from functools import wraps

import aiofiles
import aiohttp
import click
from click_help_colors import HelpColorsGroup  # type: ignore
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.prompt import Confirm
from rich.traceback import install

from .. import __version__, db
from ..config import DEFAULT_CONFIG_PATH, Config, OutdatedConfigError, set_user_defaults
from ..console import console
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .main import Main


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


@click.group(
    cls=HelpColorsGroup,
    help_headers_color="yellow",
    help_options_color="green",
)
@click.version_option(version=__version__)
@click.option(
    "--config-path",
    default=DEFAULT_CONFIG_PATH,
    help="Path to the configuration file",
    type=click.Path(readable=True, writable=True),
)
@click.option(
    "-f",
    "--folder",
    help="The folder to download items into.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option(
    "-ndb",
    "--no-db",
    help="Download items even if they have been logged in the database",
    default=False,
    is_flag=True,
)
@click.option(
    "-q",
    "--quality",
    help="The maximum quality allowed to download",
    type=click.IntRange(min=0, max=4),
)
@click.option(
    "-c",
    "--codec",
    help="Convert the downloaded files to an audio codec (ALAC, FLAC, MP3, AAC, or OGG)",
)
@click.option(
    "--no-progress",
    help="Do not show progress bars",
    is_flag=True,
    default=False,
)
@click.option(
    "--no-ssl-verify",
    help="Disable SSL certificate verification (use if you encounter SSL errors)",
    is_flag=True,
    default=False,
)
@click.option(
    "-v",
    "--verbose",
    help="Enable verbose output (debug mode)",
    is_flag=True,
)
@click.option(
    "--fail-fast",
    help="Stop immediately when any item fails instead of continuing",
    is_flag=True,
    default=False,
)
@click.option(
    "--retry",
    "retry_count",
    help="Number of times to retry a failed download (overrides config)",
    type=click.IntRange(min=0),
    default=None,
)
@click.option(
    "--retry-delay",
    help="Initial delay in seconds between retries (overrides config)",
    type=float,
    default=None,
)
@click.option(
    "--validate-flac/--no-validate-flac",
    help="Enable or disable FLAC integrity validation after download",
    default=None,
)
@click.pass_context
def rip(
    ctx,
    config_path,
    folder,
    no_db,
    quality,
    codec,
    no_progress,
    no_ssl_verify,
    verbose,
    fail_fast,
    retry_count,
    retry_delay,
    validate_flac,
):
    """Streamrip: the all in one music downloader."""
    global logger
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler()],
    )
    logger = logging.getLogger("streamrip")
    if verbose:
        install(
            console=console,
            suppress=[
                click,
            ],
            show_locals=True,
            locals_hide_sunder=False,
        )
        logger.setLevel(logging.DEBUG)
        logger.debug("Showing all debug logs")
    else:
        install(console=console, suppress=[click, asyncio], max_frames=1)
        logger.setLevel(logging.INFO)

    if not os.path.isfile(config_path):
        console.print(
            f"No file found at [bold cyan]{config_path}[/bold cyan], creating default config.",
        )
        set_user_defaults(config_path)

    # pass to subcommands
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    try:
        c = Config(config_path)
    except OutdatedConfigError as e:
        console.print(e)
        console.print("Auto-updating config file...")
        Config.update_file(config_path)
        c = Config(config_path)
    except Exception as e:
        console.print(
            f"Error loading config from [bold cyan]{config_path}[/bold cyan]: {e}\n"
            "Try running [bold]rip config reset[/bold]",
        )
        ctx.obj["config"] = None
        return

    # set session config values to command line args
    if no_db:
        c.session.database.downloads_enabled = False
    if folder is not None:
        c.session.downloads.folder = folder

    if quality is not None:
        c.session.qobuz.quality = quality
        c.session.tidal.quality = quality
        c.session.deezer.quality = quality
        c.session.soundcloud.quality = quality

    if codec is not None:
        c.session.conversion.enabled = True
        assert codec.upper() in ("ALAC", "FLAC", "OGG", "MP3", "AAC")
        c.session.conversion.codec = codec.upper()

    if no_progress:
        c.session.cli.progress_bars = False

    if no_ssl_verify:
        c.session.downloads.verify_ssl = False

    if fail_fast:
        c.session.reliability.fail_fast = True
    if retry_count is not None:
        c.session.reliability.retry_count = retry_count
    if retry_delay is not None:
        c.session.reliability.retry_delay = retry_delay
    if validate_flac is not None:
        c.session.reliability.validate_flac = validate_flac

    ctx.obj["config"] = c


@rip.command()
@click.argument("urls", nargs=-1, required=True)
@click.pass_context
@coro
async def url(ctx, urls):
    """Download content from URLs."""
    if ctx.obj["config"] is None:
        return

    failures = 0
    try:
        with ctx.obj["config"] as cfg:
            cfg: Config
            updates = cfg.session.misc.check_for_updates
            if updates:
                # Run in background
                version_coro = asyncio.create_task(
                    latest_streamrip_version(
                        verify_ssl=cfg.session.downloads.verify_ssl
                    )
                )
            else:
                version_coro = None

            async with Main(cfg) as main:
                await main.add_all(urls)
                await main.resolve()
                failures = await main.rip()

            if version_coro is not None:
                latest_version, notes = await version_coro
                if latest_version != __version__:
                    console.print(
                        f"\n[green]A new version of streamrip [cyan]v{latest_version}[/cyan]"
                        " is available! Run [white][bold]pip3 install streamrip --upgrade[/bold][/white]"
                        " to update.[/green]\n"
                    )

                    console.print(Markdown(notes))

    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()
        failures = 1

    if failures > 0:
        sys.exit(1)


@rip.command()
@click.argument(
    "path",
    required=True,
    type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False),
)
@click.option(
    "--list-mode",
    "list_mode",
    type=click.Choice(["auto", "json", "urls", "exportify-csv"], case_sensitive=False),
    default="auto",
    help=(
        "File list mode. 'auto' tries JSON, then Exportify CSV, then URL list. "
        "'exportify-csv' requires --source."
    ),
    show_default=True,
)
@click.option(
    "-s",
    "--source",
    "source",
    default=None,
    help=(
        "Primary search source for Exportify CSV mode "
        "(default: [lastfm].source from config)."
    ),
)
@click.option(
    "-fs",
    "--fallback-source",
    "fallback_source",
    default=None,
    help=(
        "Fallback search source for Exportify CSV mode "
        "(default: [lastfm].fallback_source from config)."
    ),
)
@click.pass_context
@coro
async def file(ctx, path, list_mode, source, fallback_source):
    """Download content from a file of URLs or an Exportify CSV.

    Example usage:

        rip file urls.txt

        rip file --list-mode exportify-csv Liked_Songs.csv

        rip file --list-mode exportify-csv --source qobuz --fallback-source deezer Liked_Songs.csv
    """
    import os as _os

    failures = 0
    try:
        with ctx.obj["config"] as cfg:
            async with Main(cfg) as main:
                # Resolve effective source / fallback for CSV mode
                _source = source or cfg.session.lastfm.source
                _fallback = (
                    fallback_source
                    if fallback_source is not None
                    else cfg.session.lastfm.fallback_source
                )

                async with aiofiles.open(path, "r", encoding="utf-8-sig") as f:
                    content = await f.read()

                # Determine mode
                if list_mode == "auto":
                    from ..file_lists import detect_file_mode

                    effective_mode = detect_file_mode(content)
                else:
                    effective_mode = list_mode.lower()

                if effective_mode == "json":
                    try:
                        items = json.loads(content)
                    except json.JSONDecodeError as e:
                        console.print(f"[red]Failed to parse JSON: {e}[/red]")
                        return
                    console.print(
                        f"Detected JSON file. Loading [yellow]{len(items)}[/yellow] items"
                    )
                    await main.add_all_by_id(
                        [(i["source"], i["media_type"], i["id"]) for i in items]
                    )
                    await main.resolve()

                elif effective_mode == "exportify-csv":
                    from ..file_lists import parse_exportify_csv

                    playlist_name, rows = parse_exportify_csv(path)
                    if not rows:
                        console.print("[red]No rows found in CSV file.[/red]")
                        return

                    # Unresolved log lives next to the CSV file
                    stem = _os.path.splitext(path)[0]
                    unresolved_log_path = f"{stem}_unresolved.csv"

                    await main.resolve_csv(
                        playlist_name=playlist_name,
                        rows=rows,
                        source=_source,
                        fallback_source=_fallback,
                        unresolved_log_path=unresolved_log_path,
                    )

                else:
                    # URL list mode
                    items_list = content.split()
                    s = set(items_list)
                    if len(s) < len(items_list):
                        console.print(
                            f"Found [orange]{len(items_list) - len(s)}[/orange] repeated URLs!"
                        )
                        items_list = list(s)
                    console.print(
                        f"Detected list of urls. Loading [yellow]{len(items_list)}[/yellow] items"
                    )
                    await main.add_all(items_list)
                    await main.resolve()

                failures = await main.rip()
    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()
        failures = 1

    if failures > 0:
        sys.exit(1)


@rip.group()
def config():
    """Manage configuration files."""


@config.command("open")
@click.option("-v", "--vim", help="Open in (Neo)Vim", is_flag=True)
@click.pass_context
def config_open(ctx, vim):
    """Open the config file in a text editor."""
    config_path = ctx.obj["config_path"]

    console.print(f"Opening file at [bold cyan]{config_path}")
    if vim:
        if shutil.which("nvim") is not None:
            subprocess.run(["nvim", config_path])
        elif shutil.which("vim") is not None:
            subprocess.run(["vim", config_path])
        else:
            logger.error("Could not find nvim or vim. Using default launcher.")
            click.launch(config_path)
    else:
        click.launch(config_path)


@config.command("reset")
@click.option("-y", "--yes", help="Don't ask for confirmation.", is_flag=True)
@click.pass_context
def config_reset(ctx, yes):
    """Reset the config file."""
    config_path = ctx.obj["config_path"]
    if not yes:
        if not Confirm.ask(
            f"Are you sure you want to reset the config file at {config_path}?",
        ):
            console.print("[green]Reset aborted")
            return

    set_user_defaults(config_path)
    console.print(f"Reset the config file at [bold cyan]{config_path}!")


@config.command("path")
@click.pass_context
def config_path(ctx):
    """Display the path of the config file."""
    config_path = ctx.obj["config_path"]
    console.print(f"Config path: [bold cyan]'{config_path}'")


@rip.group()
def database():
    """View and modify the downloads and failed downloads databases."""


@database.command("browse")
@click.argument("table")
@click.pass_context
def database_browse(ctx, table):
    """Browse the contents of a table.

    Available tables:

        * Downloads

        * Failed
    """
    from rich.table import Table

    cfg: Config = ctx.obj["config"]

    if table.lower() == "downloads":
        downloads = db.Downloads(cfg.session.database.downloads_path)
        t = Table(title="Downloads database")
        t.add_column("Row")
        t.add_column("ID")
        for i, row in enumerate(downloads.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    elif table.lower() == "failed":
        failed = db.Failed(cfg.session.database.failed_downloads_path)
        t = Table(title="Failed downloads database")
        t.add_column("Source")
        t.add_column("Media Type")
        t.add_column("ID")
        for i, row in enumerate(failed.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    else:
        console.print(
            f"[red]Invalid database[/red] [bold]{table}[/bold]. [red]Choose[/red] [bold]downloads "
            "[red]or[/red] failed[/bold].",
        )


@rip.command()
@click.option(
    "-f",
    "--first",
    help="Automatically download the first search result without showing the menu.",
    is_flag=True,
)
@click.option(
    "-o",
    "--output-file",
    help="Write search results to a file instead of showing interactive menu.",
    type=click.Path(writable=True),
)
@click.option(
    "-n",
    "--num-results",
    help="Maximum number of search results to show",
    default=100,
    type=click.IntRange(min=1),
)
@click.argument("source", required=True)
@click.argument("media-type", required=True)
@click.argument("query", required=True)
@click.pass_context
@coro
async def search(ctx, first, output_file, num_results, source, media_type, query):
    """Search for content using a specific source.

    Example:

        rip search qobuz album 'rumours'
    """
    if first and output_file:
        console.print("Cannot choose --first and --output-file!")
        return
    failures = 0
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            if first:
                await main.search_take_first(source, media_type, query)
            elif output_file:
                await main.search_output_file(
                    source, media_type, query, output_file, num_results
                )
            else:
                await main.search_interactive(source, media_type, query)
            await main.resolve()
            failures = await main.rip()

    if failures > 0:
        sys.exit(1)


@rip.command()
@click.option("-s", "--source", help="The source to search tracks on.")
@click.option(
    "-fs",
    "--fallback-source",
    help="The source to search tracks on if no results were found with the main source.",
)
@click.argument("url", required=True)
@click.pass_context
@coro
async def lastfm(ctx, source, fallback_source, url):
    """Download tracks from a last.fm playlist."""
    config = ctx.obj["config"]
    if source is not None:
        config.session.lastfm.source = source
    if fallback_source is not None:
        config.session.lastfm.fallback_source = fallback_source
    failures = 0
    with config as cfg:
        async with Main(cfg) as main:
            await main.resolve_lastfm(url)
            failures = await main.rip()

    if failures > 0:
        sys.exit(1)


@rip.command()
@click.argument("source")
@click.argument("media-type")
@click.argument("id")
@click.pass_context
@coro
async def id(ctx, source, media_type, id):
    """Download an item by ID."""
    failures = 0
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            await main.add_by_id(source, media_type, id)
            await main.resolve()
            failures = await main.rip()

    if failures > 0:
        sys.exit(1)


@rip.command()
@click.pass_context
@coro
async def repair(ctx):
    """Retry all previously failed downloads.

    Reads the failed-downloads database and attempts to download each item
    again. Items that were already successfully downloaded in a later run are
    skipped automatically (they are present in the downloads database).

    Example usage:

        rip repair
    """
    cfg: Config | None = ctx.obj.get("config")
    if cfg is None:
        console.print("[red]No config loaded. Cannot run repair.[/red]")
        return

    failures = 0
    with cfg as c:
        async with Main(c) as main:
            failed_items = main.database.get_failed_downloads()
            if not failed_items:
                console.print("[green]No failed downloads to retry.[/green]")
                return

            console.print(
                f"Retrying [yellow]{len(failed_items)}[/yellow] previously failed download(s)…"
            )
            await main.add_all_by_id(
                [
                    (source, media_type, item_id)
                    for source, media_type, item_id in failed_items
                ]
            )
            await main.resolve()
            failures = await main.rip()

    if failures > 0:
        sys.exit(1)


@rip.command("repair-csv")
@click.argument(
    "path",
    required=True,
    type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False),
)
@click.option(
    "-s",
    "--source",
    "source",
    default=None,
    help=("Primary search source for repair (default: [lastfm].source from config)."),
)
@click.option(
    "-fs",
    "--fallback-source",
    "fallback_source",
    default=None,
    help=(
        "Fallback search source for repair "
        "(default: [lastfm].fallback_source from config)."
    ),
)
@click.pass_context
@coro
async def repair_csv(ctx, path, source, fallback_source):
    """Retry unresolved rows from a previous Exportify CSV import.

    Reads the ``*_unresolved.csv`` log produced by a prior ``rip file``
    Exportify CSV run and attempts to resolve each row again using:

    \\b
    - an expanded search window (3x normal),
    - fuzzy title matching as a fallback when exact matching fails.

    Rows that still cannot be resolved are written to a new
    ``<stem>_repair_unresolved.csv`` file for further audit.

    Example usage:

        rip repair-csv Liked_Songs_unresolved.csv

        rip repair-csv --source qobuz --fallback-source deezer Liked_Songs_unresolved.csv
    """
    cfg: Config | None = ctx.obj.get("config")
    if cfg is None:
        console.print("[red]No config loaded. Cannot run repair-csv.[/red]")
        return

    failures = 0
    with cfg as c:
        _source = source or c.session.lastfm.source
        _fallback_source = fallback_source or c.session.lastfm.fallback_source or ""
        async with Main(c) as main:
            await main.repair_csv(
                unresolved_csv_path=path,
                source=_source,
                fallback_source=_fallback_source,
            )
            failures = await main.rip()

    if failures > 0:
        sys.exit(1)


async def latest_streamrip_version(verify_ssl: bool = True) -> tuple[str, str | None]:
    """Get the latest streamrip version from PyPI and release notes from GitHub.

    Args:
        verify_ssl: Whether to verify SSL certificates

    Returns:
        A tuple of (version, release_notes)
    """
    # Create connector with appropriate SSL settings
    connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
    connector = aiohttp.TCPConnector(**connector_kwargs)

    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.get("https://pypi.org/pypi/streamrip/json") as resp:
            data = await resp.json()
        version = data["info"]["version"]

        if version == __version__:
            return version, None

        async with s.get(
            "https://api.github.com/repos/cbkii/streamrippa/releases/latest"
        ) as resp:
            json = await resp.json()
        notes = json["body"]
    return version, notes


if __name__ == "__main__":
    rip()
