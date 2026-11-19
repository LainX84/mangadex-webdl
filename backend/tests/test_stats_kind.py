"""Statistics: new releases vs backlog (a bulk import must not look like activity)."""

from datetime import UTC, datetime, timedelta

from app.api.routes.stats import NEW_RELEASE_WINDOW, _is_new_release

ADDED = datetime(2026, 10, 17, 12, tzinfo=UTC)          # title added to the service
FIN = datetime(2026, 11, 5, 10, tzinfo=UTC)             # download finished


def test_fresh_chapter_is_new():
    assert _is_new_release(FIN - timedelta(hours=3), FIN, ADDED)


def test_chapter_just_inside_the_window_is_new():
    assert _is_new_release(FIN - NEW_RELEASE_WINDOW, FIN, ADDED)


def test_old_chapter_of_a_title_added_the_same_day_is_backlog():
    added_today = FIN - timedelta(hours=1)
    assert not _is_new_release(datetime(2023, 4, 1, tzinfo=UTC), FIN, added_today)


def test_chapter_published_after_the_title_was_added_is_new_even_if_downloaded_late():
    # e.g. the service was down for two weeks: still a new release, not backlog
    published = ADDED + timedelta(days=2)
    assert published < FIN - NEW_RELEASE_WINDOW
    assert _is_new_release(published, FIN, ADDED)


def test_old_chapter_downloaded_later_is_backlog():
    assert not _is_new_release(ADDED - timedelta(days=30), FIN, ADDED)


def test_unknown_publish_date_is_backlog():
    assert not _is_new_release(None, FIN, ADDED)
