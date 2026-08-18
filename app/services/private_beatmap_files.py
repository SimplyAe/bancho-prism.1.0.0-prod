"""Serving beatmaps this server hosts to the osu! client.

The client fetches two things for a beatmap it does not have: the ``.osu`` for a
single difficulty (``/web/maps/{filename}``) and the ``.osz`` for a whole set
(``/d/{set_id}``). Stock bancho.py answers both with a redirect -- to osu! and to a
configured mirror respectively -- which is correct for mirrored maps and useless
for maps that exist only here. Neither destination has ever heard of them.

This service is the lookup behind those two routes: given a filename or a set id,
decide whether *we* host it, and if so where the file is. It returns ``None`` for
everything else, so the routes fall back to their existing redirect and mirrored
maps keep working exactly as before.

Two things it deliberately does:

- **checks the file is actually on disk.** A row whose file went missing must fall
  back to the redirect rather than produce a 500 -- the caller here is the game
  client, and an error there is a player who cannot load a map.
- **honours the submission's visibility.** A rejected or pending set is not served
  to strangers, because the download endpoint would otherwise be a way to read a
  submission that the API refuses to show.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.repositories.map_id_sequence import PRIVATE_BEATMAP_ID_FLOOR
from app.repositories.map_submissions import MapSubmissionReviewState
from app.repositories.map_submissions import MapSubmissionsRepository
from app.repositories.maps import Map
from app.repositories.maps import MapServer
from app.repositories.maps import MapsRepository


@dataclass(frozen=True, slots=True)
class PrivateBeatmapFile:
    """A hosted file that exists and may be served."""

    path: Path
    # what the client should call the download; only used for archives.
    download_name: str | None = None


@dataclass(frozen=True)
class PrivateBeatmapFilesService:
    maps: MapsRepository
    submissions: MapSubmissionsRepository
    beatmaps_path: Path
    archives_path: Path

    async def fetch_osu_file(self, filename: str) -> PrivateBeatmapFile | None:
        """The ``.osu`` we host under ``filename``, or None if we do not.

        No visibility check: an individual difficulty file is not secret (the
        client is asking because a player is trying to play it), and the filename
        is unguessable enough that gating it would only break legitimate play.
        """
        beatmap = await self.maps.fetch_one(filename=filename)
        if beatmap is None or beatmap.server != MapServer.PRIVATE:
            return None

        path = self.beatmaps_path / f"{beatmap.id}.osu"
        if not path.is_file():
            # the row outlived its file; let the caller redirect rather than 500.
            return None

        return PrivateBeatmapFile(path=path)

    async def fetch_osz_archive(self, set_id: int) -> PrivateBeatmapFile | None:
        """The ``.osz`` we host for ``set_id``, or None if we do not."""
        # a real mirror id can never be this high, so a lower id is definitely not
        # ours and needs no lookup at all.
        if set_id < PRIVATE_BEATMAP_ID_FLOOR:
            return None

        submission = await self.submissions.fetch_one(set_id)
        if submission is None:
            return None

        # a download must not disclose a set the API would not show. Pending and
        # rejected sets stay unavailable; only the owner's own client has any
        # business fetching those, and it already has the files.
        if submission.review_state != MapSubmissionReviewState.APPROVED.value:
            return None

        path = self.archives_path / f"{set_id}.osz"
        if not path.is_file():
            return None

        maps = await self.maps.fetch_many(
            set_id=set_id,
            server=MapServer.PRIVATE,
        )
        return PrivateBeatmapFile(
            path=path,
            download_name=_download_name(set_id, maps),
        )


def _download_name(set_id: int, maps: Sequence[Map]) -> str:
    """The filename osu! shows for a download: ``{id} {artist} - {title}.osz``."""
    if not maps:
        return f"{set_id}.osz"
    beatmap = maps[0]
    return f"{set_id} {beatmap.artist} - {beatmap.title}.osz"
