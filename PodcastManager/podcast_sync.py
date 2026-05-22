"""Bridge between downloaded podcast episodes and the iPod sync pipeline.

Converts PodcastEpisode + PodcastFeed models into PCTrack objects that
flow through the standard sync pipeline (SyncPlan → SyncReview →
SyncExecutor → write_itunesdb).  The SyncExecutor's _pc_track_to_info()
detects podcasts via ``is_podcast=True`` and sets the correct media_type,
podcast_flag, etc.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (
    STATUS_DOWNLOADED,
    STATUS_DOWNLOADING,
    STATUS_NOT_DOWNLOADED,
    STATUS_ON_IPOD,
    PodcastEpisode,
    PodcastFeed,
)

if TYPE_CHECKING:
    from SyncEngine.fingerprint_diff_engine import SyncPlan
    from SyncEngine.pc_library import PCTrack

log = logging.getLogger(__name__)


class PodcastTrackMatcher:
    """Fast matcher for resolving podcast episodes against iPod tracks.

    The matcher pre-indexes iPod podcast tracks once, then can reconcile
    many feeds without rebuilding lookup maps for each feed.
    """

    def __init__(self, ipod_tracks: list[dict]):
        self._by_enclosure: dict[str, dict] = {}
        self._by_title_album: dict[tuple[str, str], dict] = {}

        for track in ipod_tracks:
            media_type = track.get("media_type", 0)
            if not (media_type & 0x04):
                continue

            enc_url = track.get("Podcast Enclosure URL", "")
            if enc_url:
                self._by_enclosure[enc_url] = track

            title = track.get("Title", "")
            album = track.get("Album", "")
            if title and album:
                self._by_title_album[(title.lower(), album.lower())] = track

    def match_feed(self, feed: PodcastFeed) -> bool:
        """Reconcile one feed against indexed iPod tracks.

        Returns:
            True if any episode state changed, else False.
        """
        changed = False

        for ep in feed.episodes:
            matched_track = None
            if ep.audio_url:
                matched_track = self._by_enclosure.get(ep.audio_url)
            if not matched_track and ep.title and feed.title:
                matched_track = self._by_title_album.get(
                    (ep.title.lower(), feed.title.lower())
                )

            if matched_track:
                new_db_track_id = matched_track.get("db_track_id", matched_track.get("db_id", 0))
                if ep.ipod_db_track_id != new_db_track_id or ep.status != STATUS_ON_IPOD:
                    ep.ipod_db_track_id = new_db_track_id
                    ep.status = STATUS_ON_IPOD
                    changed = True
                continue

            # No longer present on iPod: clear stale db link and derive local status.
            if ep.ipod_db_track_id != 0:
                ep.ipod_db_track_id = 0
                changed = True

            # Keep transient download state if a transfer is currently running.
            if ep.status == STATUS_DOWNLOADING:
                continue

            has_local_file = bool(ep.downloaded_path and os.path.exists(ep.downloaded_path))
            if not has_local_file and ep.downloaded_path:
                ep.downloaded_path = ""
                changed = True

            next_status = STATUS_DOWNLOADED if has_local_file else STATUS_NOT_DOWNLOADED
            if ep.status != next_status:
                ep.status = next_status
                changed = True

        return changed


def episode_to_pc_track(
    episode: PodcastEpisode,
    feed: PodcastFeed,
    store: object | None = None,
) -> PCTrack:
    """Convert a podcast episode into a PCTrack for the sync pipeline.

    Works for both downloaded and not-yet-downloaded episodes.  For
    episodes without a local file, RSS metadata is used and the file
    will be downloaded during sync execution.

    The returned PCTrack is fully compatible with SyncExecutor's
    ``_pc_track_to_info()`` — which detects ``is_podcast=True`` and sets
    media_type=PODCAST, podcast_flag, skip_when_shuffling, etc.

    Args:
        episode: Episode (may or may not have a downloaded_path).
        feed: Parent feed (for show-level metadata).
        store: Optional SubscriptionStore (for predicting download path).

    Returns:
        A PCTrack ready for use in a SyncItem.
    """
    from SyncEngine.pc_library import PCTrack

    path = episode.downloaded_path or ""
    has_file = bool(path and os.path.exists(path))

    # If not downloaded, predict the download path from the audio URL
    if not has_file and episode.audio_url:
        if store is not None:
            from PodcastManager.subscription_store import SubscriptionStore
            if isinstance(store, SubscriptionStore):
                dest_dir = store.feed_dir(feed)
                from .downloader import _safe_filename
                path = os.path.join(dest_dir, _safe_filename(episode))
                if os.path.exists(path):
                    has_file = True
                    episode.downloaded_path = path
                    if episode.status != STATUS_ON_IPOD:
                        episode.status = STATUS_DOWNLOADED

    # Derive extension from path or audio URL
    if path:
        ext = Path(path).suffix.lower()
    elif episode.audio_url:
        url_path = episode.audio_url.split("?")[0]
        ext = Path(url_path).suffix.lower()
        if ext not in (".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".opus",
                       ".flac", ".wav", ".wma"):
            ext = ".mp3"  # safe default
    else:
        ext = ".mp3"

    # Read real audio metadata from the downloaded file
    bitrate: int | None = None
    sample_rate: int | None = 44100
    duration_ms = episode.duration_seconds * 1000
    vbr = False

    if has_file:
        try:
            from mutagen import File as MutagenFile  # type: ignore[import-untyped]
            audio = MutagenFile(path)
            if audio and audio.info:
                if hasattr(audio.info, 'bitrate') and audio.info.bitrate:
                    bitrate = int(audio.info.bitrate / 1000)
                if hasattr(audio.info, 'sample_rate') and audio.info.sample_rate:
                    sample_rate = audio.info.sample_rate
                if hasattr(audio.info, 'length') and audio.info.length:
                    duration_ms = int(audio.info.length * 1000)
                if hasattr(audio.info, 'bitrate_mode'):
                    from mutagen.mp3 import BitrateMode  # type: ignore[import-untyped]
                    vbr = audio.info.bitrate_mode == BitrateMode.VBR
        except Exception as exc:
            log.debug("Could not read audio metadata for %s: %s", path, exc)

    if has_file:
        file_size = Path(path).stat().st_size
    else:
        file_size = episode.size_bytes

    art_hash: str | None = None
    if has_file:
        try:
            from ArtworkDB_Writer import art_extractor

            art_bytes = art_extractor.extract_art_with_folder(path)
            if art_bytes:
                art_hash = art_extractor.art_hash(art_bytes)
        except Exception as exc:
            log.debug("Could not read artwork hash for %s: %s", path, exc)

    # iPod-native formats
    native = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff"}

    # Extract chapter markers from the downloaded file
    chapters = None
    if has_file:
        try:
            from .downloader import extract_chapters
            chapters = extract_chapters(path)
        except Exception as exc:
            log.debug("Could not extract chapters from %s: %s", path, exc)

    source = Path(path) if path else Path("pending_download" + ext)

    return PCTrack(
        path=path,
        relative_path=source.name,
        filename=source.name,
        extension=ext,
        mtime=source.stat().st_mtime if has_file else 0.0,
        size=file_size,
        title=episode.title or "Untitled Episode",
        artist=feed.author or feed.title,
        album=feed.title,
        album_artist=feed.author or None,
        genre=feed.category or "Podcast",
        year=(int(time.strftime("%Y", time.localtime(episode.pub_date)))
              if episode.pub_date else None),
        track_number=episode.episode_number,
        track_total=None,
        disc_number=episode.season_number,
        disc_total=None,
        duration_ms=duration_ms,
        bitrate=bitrate,
        sample_rate=sample_rate,
        rating=None,
        vbr=vbr,
        date_released=int(episode.pub_date) if episode.pub_date else 0,
        description=episode.description[:255] if episode.description else None,
        episode_number=episode.episode_number,
        season_number=episode.season_number,
        is_podcast=True,
        art_hash=art_hash,
        show_name=feed.title or None,
        category=feed.category or None,
        podcast_url=feed.feed_url or None,
        podcast_enclosure_url=episode.audio_url or None,
        needs_transcoding=ext not in native,
        chapters=chapters,
    )


def build_podcast_sync_plan(
    episodes: list[tuple[PodcastEpisode, PodcastFeed]],
    ipod_tracks: list[dict],
    store: object | None = None,
) -> SyncPlan:
    """Build a SyncPlan for podcast episodes to add to iPod.

    Filters out episodes already on iPod (matched by enclosure URL or
    title+album), and creates ADD_TO_IPOD SyncItems for the rest.

    Works for both downloaded and not-yet-downloaded episodes.  For
    pending episodes, the actual download happens during sync execution
    (see ``SyncExecutor._download_podcast_episodes``).

    Args:
        episodes: List of (episode, feed) tuples.
        ipod_tracks: Parsed track dicts from iTunesDBCache.get_tracks().
        store: Optional SubscriptionStore (for predicting download paths).

    Returns:
        A SyncPlan ready for the SyncReview widget.
    """
    from SyncEngine.fingerprint_diff_engine import StorageSummary, SyncAction, SyncItem, SyncPlan

    # Build lookup of existing podcast tracks on iPod
    by_enclosure: dict[str, dict] = {}
    by_title_album: dict[tuple[str, str], dict] = {}
    for t in ipod_tracks:
        media_type = t.get("media_type", 0)
        if not (media_type & 0x04):
            continue
        enc_url = t.get("Podcast Enclosure URL", "")
        if enc_url:
            by_enclosure[enc_url] = t
        title = t.get("Title", "")
        album = t.get("Album", "")
        if title and album:
            by_title_album[(title.lower(), album.lower())] = t

    to_add: list[SyncItem] = []
    bytes_to_add = 0

    for episode, feed in episodes:
        # Skip if already on iPod
        already_on_ipod = False
        if episode.audio_url and episode.audio_url in by_enclosure:
            already_on_ipod = True
        elif episode.title and feed.title:
            key = (episode.title.lower(), feed.title.lower())
            if key in by_title_album:
                already_on_ipod = True

        if already_on_ipod:
            continue

        pc_track = episode_to_pc_track(episode, feed, store)
        to_add.append(SyncItem(
            action=SyncAction.ADD_TO_IPOD,
            pc_track=pc_track,
            description=f"🎙 {feed.title} — {episode.title}",
        ))
        bytes_to_add += pc_track.size

    return SyncPlan(
        to_add=to_add,
        storage=StorageSummary(bytes_to_add=bytes_to_add),
    )


def needs_transcode(episode: PodcastEpisode) -> bool:
    """Check if an episode's audio format needs transcoding for iPod."""
    if not episode.downloaded_path:
        return False
    ext = Path(episode.downloaded_path).suffix.lower()
    native = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff"}
    return ext not in native


# ── Identity helper ──────────────────────────────────────────────────────────


def episode_key(episode: PodcastEpisode, track: dict | None = None) -> str:
    """Stable identity key for an episode across planner phases.

    Priority: ``guid`` → ``audio_url`` → ``track.db_track_id`` → synthetic.
    Namespace prefixes (``guid:``, ``url:``, ``dbid:``, ``syn:``) prevent
    cross-bucket collisions.

    The ``guid == audio_url`` short-circuit downgrades the parser's
    ``guid = entry.get("id") or audio_url`` fallback (see
    ``feed_parser.py``) to a ``url:`` key, so a feed that omits ``<guid>``
    doesn't produce divergent keys between the on-device-stored episode
    (``guid=""``) and the RSS-refreshed copy (``guid=audio_url``).

    The synthetic fallback is not collision-proof; it's a last resort that
    only fires when every prior source is missing.
    """
    if episode.audio_url and episode.guid == episode.audio_url:
        return f"url:{episode.audio_url}"
    if episode.guid:
        return f"guid:{episode.guid}"
    if episode.audio_url:
        return f"url:{episode.audio_url}"
    if track is not None:
        dbid = track.get("db_track_id") or track.get("db_id")
        if dbid:
            return f"dbid:{dbid}"
    return f"syn:{episode.title}|{episode.pub_date}|{episode.duration_seconds}"


# ── Age threshold helpers ─────────────────────────────────────────────────────

_AGE_THRESHOLDS: dict[str, int] = {
    "1_day": 86400,
    "3_days": 86400 * 3,
    "1_week": 86400 * 7,
    "2_weeks": 86400 * 14,
    "1_month": 86400 * 30,
    "2_months": 86400 * 60,
    "3_months": 86400 * 90,
}


def _should_clear_episode(
    ipod_track: dict,
    feed: PodcastFeed,
    now: float,
) -> bool:
    """Decide whether an on-iPod episode should be cleared from its slot.

    Returns True if the episode matches any of the feed's clear criteria.
    """
    # Clear when listened: play_count > 0
    if feed.clear_when_listened:
        play_count = ipod_track.get("play_count_1", 0)
        if play_count and play_count > 0:
            return True

    # Clear when older than threshold (by date added to iPod)
    max_age = _AGE_THRESHOLDS.get(feed.clear_older_than)
    if max_age is not None:
        date_added = ipod_track.get("date_added", 0)
        if date_added and (now - date_added) > max_age:
            return True

    return False


def _pick_candidates(
    feed: PodcastFeed,
    on_ipod_keys: set[str],
    count: int,
    fill_mode: str,
) -> list[PodcastEpisode]:
    """Pick episodes to fill empty slots according to ``fill_mode``.

    Args:
        feed: The feed with a full episode catalog (after RSS refresh).
        on_ipod_keys: ``episode_key()`` values of episodes currently
            occupying a slot (passed by the caller — typically
            ``slot_holders.keys()``). Episodes whose key is in this set
            are excluded from the candidate pool.
        count: Number of candidates to return at most.
        fill_mode: One of ``"newest"``, ``"next"``, ``"always_newest"``.
            The caller is responsible for normalizing unknown values
            (typically to ``"newest"``) before invoking this function.
            Both ``"newest"`` and ``"always_newest"`` use newest-first
            ordering here — rotation semantics live in the planner.

    Returns:
        List of episodes to add, up to *count*.
    """
    if count <= 0:
        return []

    # Consider any episode not already accounted for in a slot.
    available = [
        ep for ep in feed.episodes
        if ep.status != STATUS_ON_IPOD
        and episode_key(ep) not in on_ipod_keys
        and ep.audio_url  # must have a download URL
    ]

    if not available:
        return []

    if fill_mode == "next":
        # "next" mode: pick the next unheard episodes after the latest
        # one currently in a slot. Sort by pub_date ascending, then take
        # from the episode after the newest slot-holder.
        available.sort(key=lambda e: e.pub_date)

        # Find the pub_date of the newest episode currently in a slot.
        on_ipod_eps = [
            ep for ep in feed.episodes
            if episode_key(ep) in on_ipod_keys
        ]
        if on_ipod_eps:
            latest_on_ipod = max(ep.pub_date for ep in on_ipod_eps)
            after = [ep for ep in available if ep.pub_date > latest_on_ipod]
            if after:
                return after[:count]

        # No on-iPod episodes or none newer: start from the oldest available.
        return available[:count]

    # "newest" and "always_newest" — most recently published first.
    available.sort(key=lambda e: e.pub_date, reverse=True)
    return available[:count]


_VALID_FILL_MODES: frozenset[str] = frozenset({"newest", "next", "always_newest"})


@dataclass
class _SlotHolder:
    """An episode projected to occupy a slot after the plan executes."""

    episode: PodcastEpisode
    track: dict | None  # None when source == "planned_add"
    source: str         # "existing" or "planned_add"
    key: str            # cached episode_key(episode, track)


def build_podcast_managed_plan(
    feeds: list[PodcastFeed],
    ipod_tracks: list[dict],
    store: object | None = None,
) -> SyncPlan:
    """Build a SyncPlan that applies per-feed podcast settings.

    Per-feed pipeline: identify clear-rule flagged episodes, build the
    candidate pool once, apply ``clear_method`` (with ``"replace"`` pairing),
    trim any over-capacity holders, fill empty slots, then for
    ``always_newest`` rotate oldest-pub_date holders out for newer
    candidates. All accounting is deduplicated by ``episode_key()`` so an
    episode is never queued twice across phases.
    """
    from SyncEngine.fingerprint_diff_engine import (
        StorageSummary,
        SyncAction,
        SyncItem,
        SyncPlan,
    )

    now = time.time()
    to_add: list[SyncItem] = []
    to_remove: list[SyncItem] = []
    bytes_to_add = 0
    bytes_to_remove = 0

    # Index all podcast tracks on iPod by enclosure URL and title+album.
    by_enclosure: dict[str, dict] = {}
    by_title_album: dict[tuple[str, str], dict] = {}
    for t in ipod_tracks:
        if not (t.get("media_type", 0) & 0x04):
            continue
        enc = t.get("Podcast Enclosure URL", "")
        if enc:
            by_enclosure[enc] = t
        title = t.get("Title", "")
        album = t.get("Album", "")
        if title and album:
            by_title_album[(title.lower(), album.lower())] = t

    for feed in feeds:
        # ── Step 0: normalize fill_mode ────────────────────────────────────
        effective_fill_mode = (
            feed.fill_mode if feed.fill_mode in _VALID_FILL_MODES else "newest"
        )

        # Build slot_holders from on-iPod episodes for this feed.
        slot_holders: dict[str, _SlotHolder] = {}
        for ep in feed.episodes:
            if ep.status != STATUS_ON_IPOD or not ep.ipod_db_track_id:
                continue
            track = None
            if ep.audio_url:
                track = by_enclosure.get(ep.audio_url)
            if track is None and ep.title and feed.title:
                track = by_title_album.get(
                    (ep.title.lower(), feed.title.lower())
                )
            if track is None:
                continue
            key = episode_key(ep, track)
            slot_holders[key] = _SlotHolder(
                episode=ep, track=track, source="existing", key=key,
            )

        on_ipod_initial_count = len(slot_holders)

        planned_removes: dict[str, SyncItem] = {}
        planned_adds: dict[str, SyncItem] = {}
        consumed_candidates: set[str] = set()

        def _queue_remove(holder: _SlotHolder, suffix: str) -> None:
            item = SyncItem(
                action=SyncAction.REMOVE_FROM_IPOD,
                db_track_id=holder.episode.ipod_db_track_id,
                ipod_track=holder.track,
                description=(
                    f"\U0001f399 {feed.title} \u2014 "
                    f"{holder.episode.title} ({suffix})"
                ),
            )
            planned_removes[holder.key] = item
            slot_holders.pop(holder.key, None)

        def _queue_add(candidate: PodcastEpisode) -> None:
            key = episode_key(candidate)
            pc_track = episode_to_pc_track(candidate, feed, store)
            item = SyncItem(
                action=SyncAction.ADD_TO_IPOD,
                pc_track=pc_track,
                description=(
                    f"\U0001f399 {feed.title} \u2014 {candidate.title}"
                ),
            )
            planned_adds[key] = item
            consumed_candidates.add(key)
            slot_holders[key] = _SlotHolder(
                episode=candidate, track=None, source="planned_add", key=key,
            )

        # ── Step 1: identify clear-rule flagged episodes ──────────────────
        to_clear: list[_SlotHolder] = [
            holder
            for holder in slot_holders.values()
            if _should_clear_episode(holder.track, feed, now)
        ]

        # ── Step 2: build candidate pool once ─────────────────────────────
        pool_size = len(to_clear) + feed.episode_slots
        pool = _pick_candidates(
            feed,
            set(slot_holders.keys()),
            pool_size,
            effective_fill_mode,
        )

        # ── Step 3: apply clear_method ────────────────────────────────────
        if feed.clear_method == "replace":
            # Pair each flagged episode with one unconsumed candidate.
            # Unpaired flagged episodes stay on device; they remain in
            # slot_holders and become eligible for rotation/overflow.
            pool_iter = iter(pool)
            for holder in to_clear:
                replacement: PodcastEpisode | None = None
                for candidate in pool_iter:
                    if episode_key(candidate) in consumed_candidates:
                        continue
                    replacement = candidate
                    break
                if replacement is None:
                    continue  # no candidate; flagged episode stays
                _queue_remove(holder, "replaced")
                _queue_add(replacement)
        else:
            # "remove" — clear unconditionally.
            for holder in to_clear:
                _queue_remove(holder, "cleared")

        # ── Step 4: overflow trim (existing holders only) ─────────────────
        if len(slot_holders) > feed.episode_slots:
            excess = len(slot_holders) - feed.episode_slots
            trim_candidates = [
                h for h in slot_holders.values() if h.source == "existing"
            ]
            trim_candidates.sort(
                key=lambda h: h.track.get("date_added", 0) if h.track else 0
            )
            for holder in trim_candidates[:excess]:
                _queue_remove(holder, "over slot limit")

        # ── Step 5: slot fill for empty slots ─────────────────────────────
        # Fill before rotate: a free slot absorbs a candidate for 1 add;
        # rotation costs 1 add + 1 remove. Fill-first keeps rotation
        # restricted to candidates that actually beat an existing holder.
        remaining_capacity = max(0, feed.episode_slots - len(slot_holders))
        if remaining_capacity > 0:
            for candidate in pool:
                if remaining_capacity == 0:
                    break
                if episode_key(candidate) in consumed_candidates:
                    continue
                _queue_add(candidate)
                remaining_capacity -= 1

        # ── Step 6: rotation (only always_newest) ─────────────────────────
        if effective_fill_mode == "always_newest":
            evictable = [
                h for h in slot_holders.values()
                if h.source == "existing" and h.episode.pub_date > 0
            ]
            evictable.sort(
                key=lambda h: (
                    h.episode.pub_date,
                    h.track.get("date_added", 0) if h.track else 0,
                )
            )
            # De-dupe by key before sorting: pool entries that share an
            # episode_key would each evict a distinct holder while only one
            # survives in planned_adds, producing 2 removes for 1 add.
            # Pool order is newest-first, so first-wins keeps the newer dup.
            seen_candidate_keys: set[str] = set()
            deduped_candidates: list[PodcastEpisode] = []
            for c in pool:
                ck = episode_key(c)
                if ck in consumed_candidates or ck in seen_candidate_keys:
                    continue
                if c.pub_date <= 0:
                    continue
                seen_candidate_keys.add(ck)
                deduped_candidates.append(c)
            rotation_candidates = sorted(
                deduped_candidates,
                key=lambda c: c.pub_date,
                reverse=True,
            )
            i = j = 0
            while i < len(evictable) and j < len(rotation_candidates):
                holder = evictable[i]
                cand = rotation_candidates[j]
                if cand.pub_date <= holder.episode.pub_date:
                    break
                _queue_remove(holder, "rotated")
                _queue_add(cand)
                i += 1
                j += 1

        # ── Step 7: validate invariants and assemble outputs ──────────────
        # Explicit raises (not assert) so these still fire under python -O.
        if len(slot_holders) > feed.episode_slots:
            raise RuntimeError(
                f"podcast planner over-capacity for feed {feed.title!r}: "
                f"projected {len(slot_holders)} occupants > "
                f"episode_slots={feed.episode_slots}"
            )
        overlap = planned_removes.keys() & planned_adds.keys()
        if overlap:
            sample = sorted(overlap)[:3]
            raise RuntimeError(
                f"podcast planner queued same key for both remove and add "
                f"in feed {feed.title!r}: {len(overlap)} overlapping key(s), "
                f"sample={sample}"
            )

        feed_removes = list(planned_removes.values())
        feed_adds = list(planned_adds.values())
        to_remove.extend(feed_removes)
        to_add.extend(feed_adds)
        bytes_to_remove += sum(
            item.ipod_track.get("size", 0)
            for item in feed_removes if item.ipod_track
        )
        bytes_to_add += sum(
            item.pc_track.size for item in feed_adds if item.pc_track
        )

        if feed_removes or feed_adds:
            log.info(
                "Podcast %s: %d to remove, %d to add (slots=%d, on_ipod=%d)",
                feed.title, len(feed_removes), len(feed_adds),
                feed.episode_slots, on_ipod_initial_count,
            )

    return SyncPlan(
        to_add=to_add,
        to_remove=to_remove,
        storage=StorageSummary(
            bytes_to_add=bytes_to_add,
            bytes_to_remove=bytes_to_remove,
        ),
    )


def match_ipod_tracks(
    feed: PodcastFeed,
    ipod_tracks: list[dict],
) -> bool:
    """Match existing iPod tracks to feed episodes.

    Scans the iPod's parsed track list for podcast tracks matching this
    feed (by enclosure URL or title+album).  Updates episode.ipod_db_track_id
    and episode.status for matched episodes.

    Args:
        feed: A PodcastFeed with episodes.
        ipod_tracks: Parsed track dicts from iTunesDBCache.get_tracks().

    Returns:
        True if any episode state changed, otherwise False.
    """
    matcher = PodcastTrackMatcher(ipod_tracks)
    return matcher.match_feed(feed)
