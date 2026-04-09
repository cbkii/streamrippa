import asyncio
import logging
import os
from dataclasses import dataclass

from .. import converter
from ..client import Client, Downloadable
from ..config import Config
from ..db import Database
from ..exceptions import DownloadError, NonStreamableError
from ..filepath_utils import clean_filename
from ..metadata import AlbumMetadata, Covers, TrackMetadata, tag_file
from ..progress import add_title, get_progress_callback, remove_title
from .artwork import download_artwork
from .media import Media, Pending
from .semaphore import global_download_semaphore

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Track(Media):
    meta: TrackMetadata
    downloadable: Downloadable
    config: Config
    folder: str
    # Is None if a cover doesn't exist for the track
    cover_path: str | None
    db: Database
    # change?
    download_path: str = ""
    is_single: bool = False

    async def rip(self):
        await self.preprocess()
        # File-based deduplication: skip if the output file already exists on disk.
        # This handles tracks downloaded externally or in prior sessions not captured
        # in the downloads database.
        if os.path.isfile(self.download_path):
            logger.info(
                "Track '%s' already exists at '%s', skipping.",
                self.meta.title,
                self.download_path,
            )
            if not self.db.downloaded(self.meta.info.id):
                self.db.set_downloaded(self.meta.info.id)
            else:
                self.db.set_skipped()
            if self.is_single:
                remove_title(self.meta.title)
            return
        try:
            await self.download()
        except DownloadError:
            # download() already called set_failed(); do not post-process.
            if self.is_single:
                remove_title(self.meta.title)
            return
        await self.postprocess()

    async def preprocess(self):
        self._set_download_path()
        os.makedirs(self.folder, exist_ok=True)
        if self.is_single:
            add_title(self.meta.title)

    async def download(self):
        reliability = self.config.session.reliability
        max_retries = reliability.retry_count
        delay = reliability.retry_delay
        backoff = reliability.retry_backoff_factor

        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                wait = delay * (backoff ** (attempt - 1))
                logger.info(
                    "Retry %d/%d for '%s' in %.1fs...",
                    attempt,
                    max_retries,
                    self.meta.title,
                    wait,
                )
                self.db.add_retry()
                await asyncio.sleep(wait)

            label = f"Track {self.meta.tracknumber}"
            if attempt > 0:
                label = f"{label} (retry {attempt}/{max_retries})"

            async with global_download_semaphore(self.config.session.downloads):
                try:
                    size = await self.downloadable.size()
                except Exception as e:
                    last_exc = e
                    if attempt < max_retries:
                        logger.warning(
                            "Error getting size for '%s' on attempt %d/%d: %s",
                            self.meta.title,
                            attempt + 1,
                            max_retries + 1,
                            e,
                        )
                    else:
                        logger.error(
                            "Persistent error getting size for '%s' after %d attempt(s): %s",
                            self.meta.title,
                            max_retries + 1,
                            e,
                        )
                    continue

                with get_progress_callback(
                    self.config.session.cli.progress_bars,
                    size,
                    label,
                ) as callback:
                    try:
                        await self.downloadable.download(self.download_path, callback)
                        return  # success
                    except Exception as e:
                        last_exc = e
                        if attempt < max_retries:
                            logger.warning(
                                "Download attempt %d/%d failed for '%s': %s",
                                attempt + 1,
                                max_retries + 1,
                                self.meta.title,
                                e,
                            )
                        else:
                            logger.error(
                                "Persistent error downloading '%s' after %d attempt(s): %s",
                                self.meta.title,
                                max_retries + 1,
                                e,
                            )

        # All retries exhausted — record failure and raise so rip() skips postprocess.
        self.db.set_failed(
            self.downloadable.source,
            "track",
            self.meta.info.id,
            title=self.meta.title,
            artist=self.meta.artist,
            error=str(last_exc),
        )
        raise DownloadError(
            f"Failed to download '{self.meta.title}' after {max_retries + 1} attempt(s): {last_exc}"
        )

    async def postprocess(self):
        if self.is_single:
            remove_title(self.meta.title)

        await tag_file(self.download_path, self.meta, self.cover_path)

        # Validate FLAC integrity if enabled and the output file is FLAC
        if (
            self.config.session.reliability.validate_flac
            and self.download_path.lower().endswith(".flac")
        ):
            await self._validate_flac()

        if self.config.session.conversion.enabled:
            await self._convert()

        # Clear from failed store if this item had previously failed (repair flow).
        self.db.clear_failed(self.downloadable.source, "track", self.meta.info.id)
        self.db.set_downloaded(self.meta.info.id)

    async def _validate_flac(self):
        """Verify FLAC file integrity using mutagen.

        Raises if the file is corrupt or invalid so the track is treated as
        failed rather than being silently stored as a bad download.
        """
        from mutagen.flac import FLAC as MutagenFLAC  # noqa: N811

        try:
            MutagenFLAC(self.download_path)
        except Exception as e:
            logger.error(
                "FLAC validation failed for '%s': %s — removing corrupt file.",
                self.meta.title,
                e,
            )
            try:
                os.remove(self.download_path)
            except OSError:
                pass
            self.db.set_failed(
                self.downloadable.source,
                "track",
                self.meta.info.id,
                title=self.meta.title,
                artist=self.meta.artist,
                error=f"FLAC validation failed: {e}",
                is_validation_failure=True,
            )
            raise ValueError(
                f"FLAC validation failed for '{self.meta.title}': {e}"
            ) from e

    async def _convert(self):
        c = self.config.session.conversion
        engine_class = converter.get(c.codec)
        engine = engine_class(
            filename=self.download_path,
            sampling_rate=c.sampling_rate,
            bit_depth=c.bit_depth,
            remove_source=True,  # always going to delete the old file
        )
        await engine.convert()
        self.download_path = engine.final_fn  # because the extension changed

    def _set_download_path(self):
        c = self.config.session.filepaths
        formatter = c.track_format
        track_path = clean_filename(
            self.meta.format_track_path(formatter),
            restrict=c.restrict_characters,
        )
        if c.truncate_to > 0 and len(track_path) > c.truncate_to:
            track_path = track_path[: c.truncate_to]

        self.download_path = os.path.join(
            self.folder,
            f"{track_path}.{self.downloadable.extension}",
        )


@dataclass(slots=True)
class PendingTrack(Pending):
    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: str
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: str | None

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            self.db.set_skipped()
            return None

        source = self.client.source
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Track {self.id} not available for stream on {source}: {e}")
            self.db.set_failed(source, "track", self.id, error=str(e))
            return None

        try:
            meta = TrackMetadata.from_resp(self.album, source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for {self.id}: {e}")
            self.db.set_failed(source, "track", self.id, error=str(e))
            return None

        if meta is None:
            logger.error(f"Track {self.id} not available for stream on {source}")
            self.db.set_failed(source, "track", self.id)
            return None

        quality = self.config.session.get_source(source).quality
        try:
            downloadable = await self.client.get_downloadable(self.id, quality)
        except NonStreamableError as e:
            logger.error(
                f"Error getting downloadable data for track {meta.tracknumber} [{self.id}]: {e}"
            )
            self.db.set_failed(source, "track", self.id, title=meta.title, error=str(e))
            return None

        downloads_config = self.config.session.downloads
        if downloads_config.disc_subdirectories and self.album.disctotal > 1:
            folder = os.path.join(self.folder, f"Disc {meta.discnumber}")
        else:
            folder = self.folder

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            self.cover_path,
            self.db,
        )


@dataclass(slots=True)
class PendingSingle(Pending):
    """Whereas PendingTrack is used in the context of an album, where the album metadata
    and cover have been resolved, PendingSingle is used when a single track is downloaded.

    This resolves the Album metadata and downloads the cover to pass to the Track class.
    """

    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            self.db.set_skipped()
            return None

        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Error fetching track {self.id}: {e}")
            self.db.set_failed(self.client.source, "track", self.id, error=str(e))
            return None
        # Patch for soundcloud
        try:
            album = AlbumMetadata.from_track_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for track {id=}: {e}")
            self.db.set_failed(self.client.source, "track", self.id, error=str(e))
            return None

        if album is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (am) ({self.id}) on {self.client.source}",
            )
            return None

        try:
            meta = TrackMetadata.from_resp(album, self.client.source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for track {id=}: {e}")
            self.db.set_failed(self.client.source, "track", self.id, error=str(e))
            return None

        if meta is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (tm) ({self.id}) on {self.client.source}",
            )
            return None

        config = self.config.session
        quality = getattr(config, self.client.source).quality
        assert isinstance(quality, int)
        parent = config.downloads.folder
        if config.filepaths.add_singles_to_folder:
            folder = self._format_folder(album)
        else:
            folder = parent

        os.makedirs(folder, exist_ok=True)

        embedded_cover_path, downloadable = await asyncio.gather(
            self._download_cover(album.covers, folder),
            self.client.get_downloadable(self.id, quality),
        )
        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            embedded_cover_path,
            self.db,
            is_single=True,
        )

    def _format_folder(self, meta: AlbumMetadata) -> str:
        c = self.config.session
        parent = c.downloads.folder
        formatter = c.filepaths.folder_format
        if c.downloads.source_subdirectories:
            parent = os.path.join(parent, self.client.source.capitalize())

        return os.path.join(parent, meta.format_folder_path(formatter))

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            folder,
            covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        return embed_path
