import asyncio
import csv
import json
import logging
import os
import platform

import aiofiles

from .. import db
from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import DEFAULT_FAILED_DOWNLOADS_LOG_PATH, Config
from ..console import console
from ..media import (
    Media,
    Pending,
    PendingAlbum,
    PendingArtist,
    PendingCsvPlaylist,
    PendingLabel,
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingSingle,
    remove_artwork_tempdirs,
)
from ..metadata import SearchResults
from ..progress import clear_progress
from .parse_url import parse_url
from .prompter import get_prompter

logger = logging.getLogger("streamrip")

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Main:
    """Provides all of the functionality called into by the CLI.

    * Logs in to Clients and prompts for credentials
    * Handles output logging
    * Handles downloading Media
    * Handles interactive search

    User input (urls) -> Main --> Download files & Output messages to terminal
    """

    def __init__(self, config: Config):
        # Data pipeline:
        # input URL -> (URL) -> (Pending) -> (Media) -> (Downloadable) -> audio file
        self.pending: list[Pending] = []
        self.media: list[Media] = []
        self.config = config
        self.clients: dict[str, Client] = {
            "qobuz": QobuzClient(config),
            "tidal": TidalClient(config),
            "deezer": DeezerClient(config),
            "soundcloud": SoundcloudClient(config),
        }

        self.database: db.Database

        c = self.config.session.database
        if c.downloads_enabled:
            downloads_db = db.Downloads(c.downloads_path)
        else:
            downloads_db = db.Dummy()

        if c.failed_downloads_enabled:
            failed_downloads_db = db.Failed(c.failed_downloads_path)
            log_path = c.failed_downloads_log_path or DEFAULT_FAILED_DOWNLOADS_LOG_PATH
            failed_log: db.FailedTrackLog | None = db.FailedTrackLog(log_path)
        else:
            failed_downloads_db = db.Dummy()
            failed_log = None

        self.database = db.Database(downloads_db, failed_downloads_db, failed_log)

    def _enable_unresolved_log(self, path: str) -> None:
        """Attach an :class:`~streamrip.db.UnresolvedQueryLog` to the database."""
        self.database.unresolved_log = db.UnresolvedQueryLog(path)

    async def add(self, url: str):
        """Add url as a pending item.

        Do not `asyncio.gather` calls to this! Use `add_all` for concurrency.
        """
        parsed = parse_url(url)
        if parsed is None:
            raise Exception(f"Unable to parse url {url}")

        client = await self.get_logged_in_client(parsed.source)
        self.pending.append(
            await parsed.into_pending(client, self.config, self.database),
        )
        logger.debug("Added url=%s", url)

    async def add_by_id(self, source: str, media_type: str, id: str):
        client = await self.get_logged_in_client(source)
        self._add_by_id_client(client, media_type, id)

    async def add_all_by_id(self, info: list[tuple[str, str, str]]):
        sources = set(s for s, _, _ in info)
        clients = {s: await self.get_logged_in_client(s) for s in sources}
        for source, media_type, id in info:
            self._add_by_id_client(clients[source], media_type, id)

    def _add_by_id_client(self, client: Client, media_type: str, id: str):
        if media_type == "track":
            item = PendingSingle(id, client, self.config, self.database)
        elif media_type == "album":
            item = PendingAlbum(id, client, self.config, self.database)
        elif media_type == "playlist":
            item = PendingPlaylist(id, client, self.config, self.database)
        elif media_type == "label":
            item = PendingLabel(id, client, self.config, self.database)
        elif media_type == "artist":
            item = PendingArtist(id, client, self.config, self.database)
        else:
            raise Exception(media_type)

        self.pending.append(item)

    async def add_all(self, urls: list[str]):
        """Add multiple urls concurrently as pending items."""
        parsed = [parse_url(url) for url in urls]
        url_client_pairs = []
        for i, p in enumerate(parsed):
            if p is None:
                console.print(
                    f"[red]Found invalid url [cyan]{urls[i]}[/cyan], skipping.",
                )
                continue
            url_client_pairs.append((p, await self.get_logged_in_client(p.source)))

        pendings = await asyncio.gather(
            *[
                url.into_pending(client, self.config, self.database)
                for url, client in url_client_pairs
            ],
        )
        self.pending.extend(pendings)

    async def get_logged_in_client(self, source: str):
        """Return a functioning client instance for `source`."""
        client = self.clients.get(source)
        if client is None:
            raise Exception(
                f"No client named {source} available. Only have {self.clients.keys()}",
            )
        if not client.logged_in:
            prompter = get_prompter(client, self.config)
            if not prompter.has_creds():
                # Get credentials from user and log into client
                await prompter.prompt_and_login()
                prompter.save()
            else:
                with console.status(f"[cyan]Logging into {source}", spinner="dots"):
                    # Log into client using credentials from config
                    await client.login()

        assert client.logged_in
        return client

    async def resolve(self):
        """Resolve all currently pending items."""

        async def _safe_resolve(p):
            try:
                return await p.resolve()
            except Exception as e:
                logger.error("Error resolving item: %s", e)
                return None

        with console.status("Resolving URLs...", spinner="dots"):
            coros = [_safe_resolve(p) for p in self.pending]
            new_media: list[Media] = [
                m for m in await asyncio.gather(*coros) if m is not None
            ]

        self.media.extend(new_media)
        self.pending.clear()

    async def rip(self) -> int:
        """Download all resolved items.

        Returns:
            Number of top-level failures (excludes track-level failures
            already recorded in ``self.database.stats``).
        """
        reliability = self.config.session.reliability
        top_level_failures = 0

        if reliability.fail_fast:
            for item in self.media:
                failures_before = self.database.stats.failed
                try:
                    await item.rip()
                except Exception as e:
                    logger.error("Fatal error processing media item: %s", e)
                    top_level_failures += 1
                    break

                if self.database.stats.failed > failures_before:
                    console.print("[red]Fail-fast: stopping after first failure.[/red]")
                    break
        else:
            results = await asyncio.gather(
                *[item.rip() for item in self.media], return_exceptions=True
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Error processing media item: %s", result)
                    top_level_failures += 1

        self._print_session_summary(top_level_failures)
        return top_level_failures + self.database.stats.failed

    def _print_session_summary(self, top_level_failures: int = 0):
        """Print a human-readable summary at the end of the session."""
        stats = self.database.stats
        total_failures = stats.failed + top_level_failures

        parts = [
            f"[green]{stats.succeeded} succeeded[/green]",
            f"[red]{total_failures} failed[/red]",
            f"[yellow]{stats.skipped} skipped[/yellow]",
        ]
        if stats.retried > 0:
            parts.append(f"[cyan]{stats.retried} retry attempt(s)[/cyan]")
        if stats.validation_failures > 0:
            parts.append(
                f"[magenta]{stats.validation_failures} FLAC validation failure(s)[/magenta]"
            )

        console.print("\n[bold]Session summary:[/bold] " + ", ".join(parts))

        failed_log = self.database.failed_log
        if failed_log is not None and failed_log.has_entries:
            console.print(
                f"[yellow]Failed download details logged to: [cyan]{failed_log.path}"
            )
            console.print(
                "[dim]Run [bold]rip repair[/bold] to retry failed downloads.[/dim]"
            )

    async def search_interactive(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)

        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=100)
            if len(pages) == 0:
                console.print(f"[red]No search results found for query {query}")
                return
            search_results = SearchResults.from_pages(source, media_type, pages)

        if platform.system() == "Windows":  # simple term menu not supported for windows
            from pick import pick

            choices = pick(
                search_results.results,
                title=(
                    f"{source.capitalize()} {media_type} search.\n"
                    "Press SPACE to select, RETURN to download, CTRL-C to exit."
                ),
                multiselect=True,
                min_selection_count=1,
            )
            assert isinstance(choices, list)

            await self.add_all_by_id(
                [(source, media_type, item.id) for item, _ in choices],
            )

        else:
            from simple_term_menu import TerminalMenu

            menu = TerminalMenu(
                search_results.summaries(),
                preview_command=search_results.preview,
                preview_size=0.5,
                title=(
                    f"Results for {media_type} '{query}' from {source.capitalize()}\n"
                    "SPACE - select, ENTER - download, ESC - exit"
                ),
                cycle_cursor=True,
                clear_screen=True,
                multi_select=True,
            )
            chosen_ind = menu.show()
            if chosen_ind is None:
                console.print("[yellow]No items chosen. Exiting.")
            else:
                choices = search_results.get_choices(chosen_ind)
                await self.add_all_by_id(
                    [(source, item.media_type(), item.id) for item in choices],
                )

    async def search_take_first(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=1)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        assert len(search_results.results) > 0
        first = search_results.results[0]
        await self.add_by_id(source, first.media_type(), first.id)

    async def search_output_file(
        self, source: str, media_type: str, query: str, filepath: str, limit: int
    ):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=limit)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        file_contents = json.dumps(search_results.as_list(source), indent=4)
        async with aiofiles.open(filepath, "w") as f:
            await f.write(file_contents)

        console.print(
            f"Wrote [purple]{len(search_results.results)}[/purple] results to [cyan]{filepath} as JSON!"
        )

    async def resolve_lastfm(self, playlist_url: str):
        """Resolve a last.fm playlist."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )
        playlist = await pending_playlist.resolve()

        if playlist is not None:
            self.media.append(playlist)

    async def resolve_csv(
        self,
        playlist_name: str,
        rows: list,
        source: str,
        fallback_source: str,
        unresolved_log_path: str | None = None,
        repair_mode: bool = False,
    ) -> None:
        """Resolve an Exportify CSV row list into a downloadable playlist.

        Args:
            playlist_name: Name derived from the CSV filename stem.
            rows: Parsed :class:`~streamrip.rip.file_lists.ExportifyCsvRow` list.
            source: Primary search source (e.g. ``"qobuz"``).
            fallback_source: Fallback search source; empty string disables fallback.
            unresolved_log_path: Optional path for the unresolved-query CSV log.
            repair_mode: When ``True``, uses expanded search window and fuzzy
                matching (:class:`~streamrip.media.csv_playlist.PendingCsvPlaylist`
                ``repair_mode=True``) to recover rows left unresolved on the main
                import path.
        """
        if unresolved_log_path:
            self._enable_unresolved_log(unresolved_log_path)

        primary_client = await self.get_logged_in_client(source)
        fallback_client: Client | None = None
        if fallback_source:
            fallback_client = await self.get_logged_in_client(fallback_source)

        pending_playlist = PendingCsvPlaylist(
            playlist_name=playlist_name,
            rows=rows,
            primary_client=primary_client,
            fallback_client=fallback_client,
            config=self.config,
            db=self.database,
            repair_mode=repair_mode,
        )

        mode_label = "[bold yellow](repair mode)[/bold yellow] " if repair_mode else ""
        console.print(
            f"{mode_label}Resolving [yellow]{len(rows)}[/yellow] tracks from "
            f"[cyan]{playlist_name}[/cyan] using [green]{source}[/green]"
            + (f" / [blue]{fallback_source}[/blue]" if fallback_source else "")
        )

        playlist = await pending_playlist.resolve()
        if playlist is not None:
            self.media.append(playlist)

        if self.database.unresolved_log and self.database.unresolved_log.has_entries:
            console.print(
                f"[yellow]Unresolved CSV tracks logged to: "
                f"[cyan]{self.database.unresolved_log.path}"
            )

    async def repair_csv(
        self,
        unresolved_csv_path: str,
        source: str,
        fallback_source: str,
    ) -> None:
        """Replay unresolved CSV rows from a previous import using repair-mode matching.

        Reads the ``*_unresolved.csv`` log written by a prior Exportify CSV
        import, re-runs the resolution with:
        - expanded search window (:data:`~streamrip.media.csv_playlist._REPAIR_SEARCH_LIMIT`)
        - fuzzy title scoring (:func:`~streamrip.file_lists.score_candidate_repair`)

        A new unresolved log is written to ``<stem>_repair_unresolved.csv``
        alongside the input file so multiple repair passes are idempotent and
        independently auditable.

        Args:
            unresolved_csv_path: Path to the ``*_unresolved.csv`` log file.
            source: Primary search source (e.g. ``"qobuz"``).
            fallback_source: Fallback search source; empty string disables fallback.
        """
        from ..file_lists import parse_unresolved_csv

        rows = parse_unresolved_csv(unresolved_csv_path)
        if not rows:
            console.print(
                "[yellow]No unresolved rows found in log — nothing to repair.[/yellow]"
            )
            return

        current_country = (os.getenv("STREAMRIP_COUNTRY_CODE") or "").strip().upper()
        row_context: dict[int, tuple[str, str]] = {}
        try:
            with open(unresolved_csv_path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for i, rec in enumerate(reader):
                    reason = (rec.get("reason") or "").strip().lower()
                    country = (rec.get("session_country") or "").strip().upper()
                    row_context[i] = (reason, country)
        except Exception:
            row_context = {}

        def _row_priority(item):
            idx, _row = item
            reason, row_country = row_context.get(idx, ("", ""))
            is_catalog_availability = (
                "unavailable on current service" in reason
                or "quality unavailable" in reason
            )
            if (
                is_catalog_availability
                and current_country
                and row_country
                and row_country != current_country
            ):
                # Country changed: prioritize catalog-availability retries.
                return 0
            normalized_reason = reason.replace("no search results", "no results")
            if (
                "no results" in normalized_reason
                or "low confidence" in normalized_reason
            ):
                return 1
            return 2

        rows = [row for _, row in sorted(enumerate(rows), key=_row_priority)]

        stem = os.path.splitext(unresolved_csv_path)[0]
        repair_unresolved_path = f"{stem}_repair_unresolved.csv"

        playlist_name = os.path.basename(stem)

        await self.resolve_csv(
            playlist_name=playlist_name,
            rows=rows,
            source=source,
            fallback_source=fallback_source,
            unresolved_log_path=repair_unresolved_path,
            repair_mode=True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        # Ensure all client sessions are closed
        for client in self.clients.values():
            if hasattr(client, "session"):
                await client.session.close()

        # close global progress bar manager
        clear_progress()
        # We remove artwork tempdirs here because multiple singles
        # may be able to share downloaded artwork in the same `rip` session
        # We don't know that a cover will not be used again until end of execution
        remove_artwork_tempdirs()
