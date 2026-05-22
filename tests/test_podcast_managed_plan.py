"""Unit tests for ``PodcastManager.podcast_sync.build_podcast_managed_plan``.

Covers the ``"always_newest"`` fill mode (issue #86), the single-allocation
pipeline, and the ``episode_key()`` identity helper used to prevent
cross-phase duplication.

Tests use hand-crafted ``PodcastEpisode`` / ``PodcastFeed`` / iPod-track dicts.
No PyQt or filesystem dependencies.
"""

from __future__ import annotations

from PodcastManager import podcast_sync
from PodcastManager.models import (
    PodcastEpisode,
    PodcastFeed,
    STATUS_NOT_DOWNLOADED,
    STATUS_ON_IPOD,
)
from PodcastManager.podcast_sync import (
    build_podcast_managed_plan,
    episode_key,
)

# Podcast media_type bit (mhit constant 0x04 = podcast flag)
PODCAST_MEDIA_TYPE = 0x04


# ── Fixture helpers ──────────────────────────────────────────────────────────


def make_episode(
    *,
    guid: str = "",
    title: str = "Episode",
    audio_url: str = "",
    pub_date: float = 0.0,
    duration_seconds: int = 600,
    status: str = STATUS_NOT_DOWNLOADED,
    ipod_db_track_id: int = 0,
) -> PodcastEpisode:
    """Build a PodcastEpisode with sensible test defaults."""
    return PodcastEpisode(
        guid=guid,
        title=title,
        audio_url=audio_url,
        pub_date=pub_date,
        duration_seconds=duration_seconds,
        status=status,
        ipod_db_track_id=ipod_db_track_id,
    )


def make_feed(
    *,
    title: str = "Show",
    feed_url: str = "https://example.test/feed",
    episode_slots: int = 3,
    fill_mode: str = "newest",
    clear_when_listened: bool = False,
    clear_older_than: str = "never",
    clear_method: str = "remove",
    episodes: list[PodcastEpisode] | None = None,
) -> PodcastFeed:
    """Build a PodcastFeed with sensible test defaults (no clearing by default)."""
    return PodcastFeed(
        feed_url=feed_url,
        title=title,
        episode_slots=episode_slots,
        fill_mode=fill_mode,
        clear_when_listened=clear_when_listened,
        clear_older_than=clear_older_than,
        clear_method=clear_method,
        episodes=episodes or [],
    )


def make_ipod_track(
    *,
    title: str,
    album: str = "Show",
    audio_url: str = "",
    db_track_id: int = 1,
    date_added: int = 1_000_000,
    play_count: int = 0,
    size: int = 1_000_000,
    media_type: int = PODCAST_MEDIA_TYPE,
) -> dict:
    """Build an iPod-track dict matching the planner's expected shape."""
    return {
        "Title": title,
        "Album": album,
        "Podcast Enclosure URL": audio_url,
        "db_track_id": db_track_id,
        "date_added": date_added,
        "play_count_1": play_count,
        "size": size,
        "media_type": media_type,
    }


def link_episode_to_track(ep: PodcastEpisode, track: dict) -> None:
    """Mark an episode as on-iPod and link it to a track dict."""
    ep.status = STATUS_ON_IPOD
    ep.ipod_db_track_id = track["db_track_id"]


def setup_feed_with_on_ipod(
    on_ipod_specs: list[dict],
    rss_extra: list[PodcastEpisode] | None = None,
    **feed_kwargs,
) -> tuple[PodcastFeed, list[dict]]:
    """Build a feed where some episodes are already on-iPod, plus tracks for them.

    ``on_ipod_specs`` is a list of dicts each describing one on-iPod episode:
        guid, title, audio_url, pub_date, db_track_id, date_added, play_count

    ``rss_extra`` is a list of additional PodcastEpisode objects (NOT on iPod)
    that should appear in the feed catalog (RSS-only candidates).

    Returns: (feed, ipod_tracks).
    """
    episodes = []
    tracks = []
    for spec in on_ipod_specs:
        ep = make_episode(
            guid=spec.get("guid", ""),
            title=spec["title"],
            audio_url=spec.get("audio_url", ""),
            pub_date=spec.get("pub_date", 0.0),
            duration_seconds=spec.get("duration_seconds", 600),
        )
        track = make_ipod_track(
            title=spec["title"],
            album=spec.get("album", feed_kwargs.get("title", "Show")),
            audio_url=spec.get("audio_url", ""),
            db_track_id=spec["db_track_id"],
            date_added=spec["date_added"],
            play_count=spec.get("play_count", 0),
            size=spec.get("size", 1_000_000),
        )
        link_episode_to_track(ep, track)
        episodes.append(ep)
        tracks.append(track)

    if rss_extra:
        episodes.extend(rss_extra)

    feed = make_feed(episodes=episodes, **feed_kwargs)
    return feed, tracks


def keys_of(items, attr: str) -> set[str]:
    """Extract a set of identifiers from SyncItems by attribute name."""
    out: set[str] = set()
    for it in items:
        if attr == "pc_title":
            out.add(it.pc_track.title)
        elif attr == "remove_title":
            out.add(it.ipod_track.get("Title", ""))
        elif attr == "remove_db_id":
            out.add(it.db_track_id)
    return out


# ── 1. Existing-behavior regressions (must not change) ───────────────────────


def test_newest_no_clear_no_rotation():
    """fill_mode='newest' + no clear triggered + newer available → no rotation."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 3, "date_added": 3000},
        ],
        rss_extra=[
            make_episode(guid="d", title="D", audio_url="url-d", pub_date=400.0),
        ],
        fill_mode="newest",
        clear_when_listened=False,
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert plan.to_add == []
    assert plan.to_remove == []


def test_newest_clear_when_listened_refills():
    """'newest' + clear_when_listened + listened episode → cleared and refilled."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        rss_extra=[
            make_episode(guid="d", title="D", audio_url="url-d", pub_date=400.0),
        ],
        episode_slots=2,
        fill_mode="newest",
        clear_when_listened=True,
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert keys_of(plan.to_remove, "remove_title") == {"A"}
    assert keys_of(plan.to_add, "pc_title") == {"D"}


def test_newest_replace_fewer_candidates_unmatched_stay():
    """'newest' + replace + fewer candidates than to_clear → only paired removals."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000, "play_count": 1},
        ],
        rss_extra=[
            make_episode(guid="d", title="D", audio_url="url-d", pub_date=400.0),
        ],
        episode_slots=2,
        fill_mode="newest",
        clear_when_listened=True,
        clear_method="replace",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # 1 paired removal/add; 1 unmatched flagged stays.
    assert len(plan.to_remove) == 1
    assert len(plan.to_add) == 1


def test_newest_overslot_trims_oldest_by_date_added():
    """'newest' + over-slot (user reduced slot count) → trim oldest by date_added."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 500.0,
             "db_track_id": 1, "date_added": 100},  # oldest date_added
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 400.0,
             "db_track_id": 2, "date_added": 200},  # second oldest date_added
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 3, "date_added": 300},
            {"guid": "d", "title": "D", "audio_url": "url-d", "pub_date": 200.0,
             "db_track_id": 4, "date_added": 400},
            {"guid": "e", "title": "E", "audio_url": "url-e", "pub_date": 100.0,
             "db_track_id": 5, "date_added": 500},
        ],
        episode_slots=3,
        fill_mode="newest",
        clear_when_listened=False,
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # 5 on iPod, 3 slots → trim 2 oldest by date_added (titles A and B).
    assert keys_of(plan.to_remove, "remove_title") == {"A", "B"}
    assert plan.to_add == []


def test_next_overslot_same_outcome():
    """'next' + over-slot → same overflow trimming as 'newest'."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 500.0,
             "db_track_id": 1, "date_added": 100},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 400.0,
             "db_track_id": 2, "date_added": 200},
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 3, "date_added": 300},
            {"guid": "d", "title": "D", "audio_url": "url-d", "pub_date": 200.0,
             "db_track_id": 4, "date_added": 400},
        ],
        episode_slots=3,
        fill_mode="next",
        clear_when_listened=False,
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert keys_of(plan.to_remove, "remove_title") == {"A"}
    assert plan.to_add == []


def test_next_empty_device_picks_oldest():
    """'next' + nothing on iPod + multiple available → picks oldest first."""
    rss_eps = [
        make_episode(guid="a", title="A", audio_url="url-a", pub_date=100.0),
        make_episode(guid="b", title="B", audio_url="url-b", pub_date=200.0),
        make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
    ]
    feed = make_feed(episodes=rss_eps, episode_slots=2, fill_mode="next")
    plan = build_podcast_managed_plan([feed], [], store=None)
    # 'next' from empty picks oldest first (per existing _pick_candidates
    # behavior). Order matters here, so assert the list, not a set.
    assert [it.pc_track.title for it in plan.to_add] == ["A", "B"]
    assert plan.to_remove == []


# ── 2. New always_newest cases ───────────────────────────────────────────────


def test_always_newest_evicts_oldest_for_newer():
    """No listened/aged + newer available → oldest evicted, newer queued."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
        ],
        episode_slots=2,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # A has the oldest pub_date, so it gets evicted; C is added.
    assert keys_of(plan.to_remove, "remove_title") == {"A"}
    assert keys_of(plan.to_add, "pc_title") == {"C"}


def test_always_newest_no_newer_is_noop():
    """No newer episodes available → no churn."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 500.0,
             "db_track_id": 1, "date_added": 1000},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 400.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        rss_extra=[
            # Older than the on-iPod ones.
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=100.0),
        ],
        episode_slots=2,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert plan.to_add == []
    assert plan.to_remove == []


def test_always_newest_equal_pub_date_no_swap():
    """Equal pub_date between oldest staying and newest candidate → no swap."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 200.0,
             "db_track_id": 1, "date_added": 1000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=200.0),
        ],
        episode_slots=1,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert plan.to_add == []
    assert plan.to_remove == []


def test_always_newest_candidate_no_audio_url_skipped():
    """Candidate without audio_url → not used (filtered upstream)."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="", pub_date=999.0),
        ],
        episode_slots=1,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert plan.to_add == []
    assert plan.to_remove == []


def test_always_newest_multiple_newer_caps_at_slots():
    """Multiple newer candidates → all eligible slots rotate, capped at slot count."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
            make_episode(guid="d", title="D", audio_url="url-d", pub_date=400.0),
            make_episode(guid="e", title="E", audio_url="url-e", pub_date=500.0),
        ],
        episode_slots=2,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Both A and B evicted; replaced by D and E (the two newest).
    assert keys_of(plan.to_remove, "remove_title") == {"A", "B"}
    assert keys_of(plan.to_add, "pc_title") == {"D", "E"}


def test_always_newest_with_clear_no_double_queue():
    """clear_when_listened + 1 listened + 1 newer candidate → no double-queue."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},  # listened
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
        ],
        episode_slots=2,
        fill_mode="always_newest",
        clear_when_listened=True,
        clear_method="remove",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # A cleared and replaced by C. Rotation must not re-queue C.
    assert keys_of(plan.to_remove, "remove_title") == {"A"}
    assert keys_of(plan.to_add, "pc_title") == {"C"}
    # No duplicates in either list.
    assert len(plan.to_add) == 1
    assert len(plan.to_remove) == 1


def test_always_newest_replace_unmatched_stays_for_rotation():
    """replace + 2 to_clear + 1 candidate → 1 paired, 1 unmatched, no double-queue."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},  # listened
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000, "play_count": 1},  # listened
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
        ],
        episode_slots=2,
        fill_mode="always_newest",
        clear_when_listened=True,
        clear_method="replace",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # 1 paired removal/add (one of A/B and C); the other listened ep stays
    # in slot_holders for rotation. Rotation finds no remaining newer
    # candidates (C was consumed), so no more swaps.
    assert len(plan.to_remove) == 1
    assert len(plan.to_add) == 1
    assert keys_of(plan.to_add, "pc_title") == {"C"}


def test_always_newest_tie_break_by_date_added():
    """Two episodes with equal pub_date → older date_added evicted first."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},  # older date_added
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 100.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
        ],
        episode_slots=2,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # A and B have same pub_date; A is older by date_added → evicted.
    assert keys_of(plan.to_remove, "remove_title") == {"A"}
    assert keys_of(plan.to_add, "pc_title") == {"C"}


def test_always_newest_zero_pub_date_holder_not_evicted():
    """Staying with pub_date=0 → not evicted even when newer candidates exist."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 0.0,
             "db_track_id": 1, "date_added": 1000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
        ],
        episode_slots=1,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # A has pub_date=0 → not in evictable set; nothing changes.
    assert plan.to_add == []
    assert plan.to_remove == []


def test_always_newest_zero_pub_date_candidate_not_used():
    """Candidate with pub_date=0 → not used by rotation."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=0.0),
        ],
        episode_slots=1,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert plan.to_add == []
    assert plan.to_remove == []


# ── 3. Cross-phase accounting ────────────────────────────────────────────────


def test_clear_alone_resolves_overcapacity():
    """5 on iPod, 3 slots, 2 listened → only the listened ones are removed."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000, "play_count": 1},
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 3, "date_added": 3000},
            {"guid": "d", "title": "D", "audio_url": "url-d", "pub_date": 400.0,
             "db_track_id": 4, "date_added": 4000},
            {"guid": "e", "title": "E", "audio_url": "url-e", "pub_date": 500.0,
             "db_track_id": 5, "date_added": 5000},
        ],
        episode_slots=3,
        fill_mode="newest",
        clear_when_listened=True,
        clear_method="remove",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Only the 2 listened episodes should be removed. Overflow must NOT
    # additionally trim, because clear alone brings count down to 3.
    assert keys_of(plan.to_remove, "remove_title") == {"A", "B"}


def test_no_double_remove():
    """An episode picked by overflow must not also be queued by rotation."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            # 4 on iPod, 2 slots → overflow trims 2 oldest by date_added.
            # Then rotation runs on remaining 2 with no newer candidates.
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 3, "date_added": 3000},
            {"guid": "d", "title": "D", "audio_url": "url-d", "pub_date": 400.0,
             "db_track_id": 4, "date_added": 4000},
        ],
        episode_slots=2,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Each removed db_track_id appears at most once.
    removed_ids = [it.db_track_id for it in plan.to_remove]
    assert len(removed_ids) == len(set(removed_ids))


def test_always_newest_overslot_converges_on_newest():
    """5 on iPod, 3 slots, no clear, 2 newer candidates → final state = newest 3."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 100},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 200},
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 3, "date_added": 300},
            {"guid": "d", "title": "D", "audio_url": "url-d", "pub_date": 400.0,
             "db_track_id": 4, "date_added": 400},
            {"guid": "e", "title": "E", "audio_url": "url-e", "pub_date": 500.0,
             "db_track_id": 5, "date_added": 500},
        ],
        rss_extra=[
            make_episode(guid="f", title="F", audio_url="url-f", pub_date=600.0),
            make_episode(guid="g", title="G", audio_url="url-g", pub_date=700.0),
        ],
        episode_slots=3,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Overflow trims A, B (oldest by date_added).
    # Rotation: candidates G, F newer than oldest of {C, D, E} → swap C↔G, D↔F.
    # Final slot_holders content: {E, F, G}.
    removed = keys_of(plan.to_remove, "remove_title")
    added = keys_of(plan.to_add, "pc_title")
    assert removed == {"A", "B", "C", "D"}
    assert added == {"F", "G"}


# ── 4. Edge cases ────────────────────────────────────────────────────────────


def test_unknown_fill_mode_falls_back_to_newest():
    """Unknown fill_mode (e.g. 'foo' from corrupt JSON) → behaves as 'newest'."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=300.0),
        ],
        episode_slots=1,
        fill_mode="foo",  # corrupt/unknown
        clear_when_listened=False,
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # 'newest' behavior: no clear → no rotation → no churn (NOT always_newest).
    assert plan.to_add == []
    assert plan.to_remove == []


def test_empty_device_fresh_state():
    """Empty slot_holders (fresh device) → no clear/rotation; fill behaves normally."""
    rss_eps = [
        make_episode(guid="a", title="A", audio_url="url-a", pub_date=100.0),
        make_episode(guid="b", title="B", audio_url="url-b", pub_date=200.0),
    ]
    feed = make_feed(episodes=rss_eps, episode_slots=2, fill_mode="newest")
    plan = build_podcast_managed_plan([feed], [], store=None)
    # 'newest' picks most recently published first. Order matters; assert list.
    assert [it.pc_track.title for it in plan.to_add] == ["B", "A"]
    assert plan.to_remove == []


def test_empty_candidate_pool_remove_mode():
    """Empty candidate pool → clear-rule removes happen in 'remove' mode."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},
        ],
        # No rss_extra → no candidates available.
        episode_slots=1,
        fill_mode="newest",
        clear_when_listened=True,
        clear_method="remove",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert keys_of(plan.to_remove, "remove_title") == {"A"}
    assert plan.to_add == []


def test_empty_candidate_pool_replace_mode_keeps_flagged():
    """Empty candidate pool + replace mode → flagged episode stays on device."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},
        ],
        episode_slots=1,
        fill_mode="newest",
        clear_when_listened=True,
        clear_method="replace",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Replace mode + no candidate → flagged episode stays.
    assert plan.to_remove == []
    assert plan.to_add == []


# ── 5. Cross-phase identity ─────────────────────────────────────────────────


def test_blank_guid_distinct_db_ids_no_collapse():
    """Two on-device episodes with guid='' and distinct db_track_ids do not collapse."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
            {"guid": "", "title": "B", "audio_url": "url-b", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        episode_slots=5,  # large enough to not trigger overflow
        fill_mode="newest",
        clear_when_listened=False,
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Both episodes remain; no spurious add/remove from key collapse.
    assert plan.to_add == []
    assert plan.to_remove == []


def test_always_newest_replace_replacement_not_canceled():
    """Replace pairs newest candidate; rotation must not later evict that planned_add."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},  # listened
        ],
        rss_extra=[
            make_episode(guid="c", title="C", audio_url="url-c", pub_date=999.0),
        ],
        episode_slots=1,
        fill_mode="always_newest",
        clear_when_listened=True,
        clear_method="replace",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # C should be the planned_add and must NOT be queued for removal afterward.
    assert keys_of(plan.to_add, "pc_title") == {"C"}
    assert keys_of(plan.to_remove, "remove_title") == {"A"}
    # No remove for C should exist.
    for it in plan.to_remove:
        assert it.ipod_track.get("Title") != "C"


def test_overslot_with_zero_pubdate_survivors_rotation_skips_them():
    """Overflow trims by date_added; rotation must not evict zero-pub_date holders."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 100},  # oldest date_added → overflow trims
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 0.0,
             "db_track_id": 2, "date_added": 500},  # zero-pub_date survivor
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 3, "date_added": 600},
        ],
        rss_extra=[
            make_episode(guid="d", title="D", audio_url="url-d", pub_date=400.0),
        ],
        episode_slots=2,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Overflow: A trimmed (oldest date_added). Remaining: B (pub_date=0), C (pub_date=300).
    # Rotation: candidate D (pub_date=400) compared to evictable
    #   evictable (only pub_date > 0): just C.
    #   D > C → swap. Final: {B, D}.
    assert "A" in keys_of(plan.to_remove, "remove_title")
    assert "C" in keys_of(plan.to_remove, "remove_title")
    assert keys_of(plan.to_add, "pc_title") == {"D"}
    # B (pub_date=0) must NOT be removed.
    for it in plan.to_remove:
        assert it.ipod_track.get("Title") != "B"


def test_explicit_normalization_passes_fill_mode_to_pick_candidates(monkeypatch):
    """_pick_candidates must receive normalized fill_mode regardless of feed.fill_mode."""
    captured: list[str] = []

    real_pick = podcast_sync._pick_candidates

    def spy(feed, on_ipod_keys, count, fill_mode):
        captured.append(fill_mode)
        return real_pick(feed, on_ipod_keys, count, fill_mode)

    monkeypatch.setattr(podcast_sync, "_pick_candidates", spy)

    feed = make_feed(
        fill_mode="totally_bogus",
        episodes=[
            make_episode(guid="a", title="A", audio_url="url-a", pub_date=100.0),
        ],
        episode_slots=1,
    )
    build_podcast_managed_plan([feed], [], store=None)
    # Every call must have received a normalized value, never "totally_bogus".
    assert captured  # at least one call
    assert all(mode in {"newest", "next", "always_newest"} for mode in captured)
    assert "totally_bogus" not in captured


def test_candidate_pool_upper_bound_sufficient():
    """Many to_clear + rotation demand → pool size = len(to_clear) + slots is enough."""
    # 3 listened (all to_clear), 1 not-listened staying, slots=3, fill=always_newest
    # 5 newer candidates available.
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 1},
            {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 150.0,
             "db_track_id": 2, "date_added": 2000, "play_count": 1},
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 200.0,
             "db_track_id": 3, "date_added": 3000, "play_count": 1},
            {"guid": "d", "title": "D", "audio_url": "url-d", "pub_date": 250.0,
             "db_track_id": 4, "date_added": 4000},
        ],
        rss_extra=[
            make_episode(guid="e", title="E", audio_url="url-e", pub_date=300.0),
            make_episode(guid="f", title="F", audio_url="url-f", pub_date=400.0),
            make_episode(guid="g", title="G", audio_url="url-g", pub_date=500.0),
            make_episode(guid="h", title="H", audio_url="url-h", pub_date=600.0),
            make_episode(guid="i", title="I", audio_url="url-i", pub_date=700.0),
        ],
        episode_slots=3,
        fill_mode="always_newest",
        clear_when_listened=True,
        clear_method="remove",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Pool with `len(to_clear)=3 + slots=3 = 6` candidates must serve all phases.
    # 4 on iPod, 3 clear-removes (A, B, C). slot_holders after clear: {D}.
    # Fill 2 empty slots with 2 newest (H, I). slot_holders: {D, H, I}.
    # Rotation: candidates remaining {G, F, E}; sorted desc starts at G(500).
    #   Evictable sorted asc: D(250). Swap D↔G. slot_holders: {G, H, I}.
    # Final adds: H, I, G. Final removes: A, B, C, D.
    assert keys_of(plan.to_add, "pc_title") == {"G", "H", "I"}
    assert keys_of(plan.to_remove, "remove_title") == {"A", "B", "C", "D"}


def test_cross_phase_identity_blank_guid_same_audio_url():
    """On-device guid='', RSS-side guid==audio_url → both key as url:X, no duplicate.

    Mirrors the realistic case after feed_parser's `guid = id or audio_url`
    fallback fires: the RSS-refreshed copy of an episode that's already
    on-device (with blank guid) keys as `guid:X` because guid==audio_url=X.
    Without the short-circuit, slot_holders[url:X] and consumed_candidates
    would diverge, allowing duplicate adds.
    """
    on_ep = make_episode(
        guid="", title="A", audio_url="url-X", pub_date=100.0,
        status=STATUS_ON_IPOD, ipod_db_track_id=1,
    )
    rss_copy = make_episode(
        guid="url-X", title="A", audio_url="url-X", pub_date=100.0,
        # status defaults to STATUS_NOT_DOWNLOADED (RSS-side state).
    )
    # The feed catalog after refresh contains BOTH the existing on-device
    # episode AND the RSS-refreshed version (a real merge would dedup, but
    # this test simulates a worst-case where they slip through as two entries).
    feed = make_feed(
        episode_slots=2,
        fill_mode="newest",
        clear_when_listened=False,
        episodes=[on_ep, rss_copy],
    )
    track = make_ipod_track(
        title="A", audio_url="url-X", db_track_id=1, date_added=1000,
    )
    plan = build_podcast_managed_plan([feed], [track], store=None)
    # No add should be queued for rss_copy: it must be recognized as the
    # same episode as the on-device one via shared key 'url:url-X'.
    assert plan.to_add == []
    assert plan.to_remove == []


def test_blank_guid_distinct_urls_no_collapse():
    """Two episodes with guid='' but distinct audio URLs → two distinct slots."""
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            {"guid": "", "title": "A", "audio_url": "url-A", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000},
            {"guid": "", "title": "B", "audio_url": "url-B", "pub_date": 200.0,
             "db_track_id": 2, "date_added": 2000},
        ],
        episode_slots=5,
        fill_mode="newest",
        clear_when_listened=False,
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    assert plan.to_add == []
    assert plan.to_remove == []


def test_legacy_db_id_fallback():
    """Track with db_id but no db_track_id → episode_key uses dbid: fallback."""
    ep = make_episode(guid="", title="A", audio_url="", pub_date=100.0)
    track = {
        "Title": "A",
        "Album": "Show",
        "Podcast Enclosure URL": "",
        "db_id": 42,  # legacy field, no db_track_id
        "date_added": 1000,
        "play_count_1": 0,
        "size": 1_000_000,
        "media_type": PODCAST_MEDIA_TYPE,
    }
    key = episode_key(ep, track)
    assert key == "dbid:42"


def test_synthetic_fallback_collision_graceful():
    """Two distinct episodes with no guid/url/track but identical synth fields collide.

    Documents the known limitation: the synthetic last-resort key is not
    collision-proof. Two episodes with the same title, pub_date, and duration
    AND no guid/url/track will share the same key. Acceptable degradation:
    the planner should not crash; behavior is "treated as one episode."
    """
    ep1 = make_episode(guid="", title="Same", audio_url="", pub_date=100.0,
                       duration_seconds=600)
    ep2 = make_episode(guid="", title="Same", audio_url="", pub_date=100.0,
                       duration_seconds=600)
    assert episode_key(ep1) == episode_key(ep2)
    # Differing duration would distinguish them.
    ep3 = make_episode(guid="", title="Same", audio_url="", pub_date=100.0,
                       duration_seconds=601)
    assert episode_key(ep1) != episode_key(ep3)


def test_next_mode_url_fallback_identity():
    """'next' mode with blank-GUID on-device + url-keyed identity still picks next."""
    # On device: A (blank guid, url-A, pub_date 100) keys as url:url-A.
    # RSS catalog (after merge): A again (now with guid=url-A from parser
    # fallback) plus newer episodes B, C.
    on_ep = make_episode(
        guid="", title="A", audio_url="url-A", pub_date=100.0,
        status=STATUS_ON_IPOD, ipod_db_track_id=1,
    )
    # The RSS-refreshed catalog version of A has guid==audio_url (parser fallback).
    rss_a = make_episode(guid="url-A", title="A", audio_url="url-A", pub_date=100.0)
    rss_b = make_episode(guid="url-B", title="B", audio_url="url-B", pub_date=200.0)
    rss_c = make_episode(guid="url-C", title="C", audio_url="url-C", pub_date=300.0)

    feed = make_feed(
        episode_slots=2,
        fill_mode="next",
        clear_when_listened=False,
        episodes=[on_ep, rss_a, rss_b, rss_c],
    )
    track = make_ipod_track(
        title="A", audio_url="url-A", db_track_id=1, date_added=1000,
    )
    plan = build_podcast_managed_plan([feed], [track], store=None)
    # 'next' should pick B (the next episode after A).
    # A must NOT be re-queued (cross-phase identity).
    added_titles = keys_of(plan.to_add, "pc_title")
    assert "A" not in added_titles
    assert "B" in added_titles


# ── 6. Duplicate-candidate & anchor regression tests ───────────────────────


def test_always_newest_duplicate_candidate_keys_dedup_rotation():
    """RSS catalog with duplicate-key entries must not over-remove during rotation.

    Without de-duping rotation_candidates by episode_key, two RSS entries
    that share a key (e.g. ad-injection feeds emitting duplicate enclosures)
    would each evict a distinct existing holder while only one wins the
    planned_adds[key] slot, producing 2 removes + 1 add and an undersized
    final slot count. Slot capacity must stay full to actually exercise
    rotation: with one empty slot, fill would absorb the duplicate first.
    """
    # Two existing holders fill the slots; both have older pub_date than dups.
    on_specs = [
        {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
         "db_track_id": 1, "date_added": 1000},
        {"guid": "b", "title": "B", "audio_url": "url-b", "pub_date": 150.0,
         "db_track_id": 2, "date_added": 2000},
    ]
    # Two RSS entries that resolve to the SAME episode_key (shared guid),
    # both newer than A and B. With "newest"-first pool order, pub_date=300
    # is preferred over pub_date=200 when de-dup runs.
    dup1 = make_episode(
        guid="c-dup", title="C-dup-newer", audio_url="url-c-1", pub_date=300.0,
    )
    dup2 = make_episode(
        guid="c-dup", title="C-dup-older", audio_url="url-c-2", pub_date=200.0,
    )
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=on_specs,
        rss_extra=[dup1, dup2],
        episode_slots=2,
        fill_mode="always_newest",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # Exactly one swap should happen: 1 remove + 1 add. Without the
    # de-dup fix, this would be 2 removes + 1 add.
    assert len(plan.to_remove) == 1, (
        f"expected 1 remove, got {len(plan.to_remove)} "
        f"({[it.ipod_track.get('Title') for it in plan.to_remove]})"
    )
    assert len(plan.to_add) == 1
    # First-wins ordering: with rotation sorted by pub_date desc, the
    # higher-pub_date duplicate (C-dup-newer) is the one that survives.
    assert plan.to_add[0].pc_track.title == "C-dup-newer"


def test_next_mode_anchors_on_initial_slots_not_post_clear():
    """'next' mode anchors on initial slot keys, not the post-clear set.

    A listened episode flagged for clear still counts as "heard": the
    user wants the episode AFTER it, not the next-oldest unheard episode
    that pre-dates it. This pins the behavior so a future reviewer
    doesn't "fix" the anchor back to the post-clear set.
    """
    feed, tracks = setup_feed_with_on_ipod(
        on_ipod_specs=[
            # A: fresh, stays.
            {"guid": "a", "title": "A", "audio_url": "url-a", "pub_date": 100.0,
             "db_track_id": 1, "date_added": 1000, "play_count": 0},
            # C: listened, will be cleared.
            {"guid": "c", "title": "C", "audio_url": "url-c", "pub_date": 300.0,
             "db_track_id": 2, "date_added": 2000, "play_count": 1},
        ],
        rss_extra=[
            make_episode(guid="b", title="B", audio_url="url-b", pub_date=200.0),
            make_episode(guid="d", title="D", audio_url="url-d", pub_date=400.0),
        ],
        episode_slots=2,
        fill_mode="next",
        clear_when_listened=True,
        clear_method="remove",
    )
    plan = build_podcast_managed_plan([feed], tracks, store=None)
    # C removed (listened). Fill picks the episode after the newest on
    # initial slots (= C(300)), so D(400), NOT B(200).
    assert {it.ipod_track.get("Title") for it in plan.to_remove} == {"C"}
    assert [it.pc_track.title for it in plan.to_add] == ["D"]
