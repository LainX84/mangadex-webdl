"""Per-volume chapter numbering (anthologies: chapter 1 again in every volume)."""

from types import SimpleNamespace

from app.library.naming import logical_chapter_key, logical_sort_number, split_sort_number
from app.services.catalog import release_number, release_wanted
from app.services.numbering import _convert_start, numbering_resets


def test_series_keys_unchanged():
    assert logical_chapter_key("2", "r1") == "c:2"
    assert logical_chapter_key("12.50", "r1", "3") == "c:12.5"
    assert logical_chapter_key(None, "r1", "3", True) == "x:r1"
    assert logical_sort_number("12.5", "3") == 12.5


def test_per_volume_keys_and_order():
    assert logical_chapter_key("2", "r1", "15", True) == "v15:c:2"
    assert logical_chapter_key("02", "r2", "015", True) == "v15:c:2"
    assert logical_chapter_key("2", "r1", None, True) == "c:2"  # no volume: plain key
    assert logical_sort_number("2", "15", True) == 15002
    assert logical_sort_number("5", "14", True) < logical_sort_number("1", "15", True)
    assert split_sort_number(15002.5) == (15.0, 2.5)


def test_detection_needs_chapter_one_in_two_volumes_of_a_language():
    anthology = [("en", "1", "1"), ("en", "1", "2"), ("en", "2", "1"), ("en", "2", "2")]
    assert numbering_resets(anthology)
    series = [("en", "1", "1"), ("en", "1", "2"), ("en", "2", "8"), ("en", "3", "20")]
    assert not numbering_resets(series)
    # Two groups disagreeing on the volume of a later chapter is not a reset.
    assert not numbering_resets(series + [("en", "4", "20")])
    # Chapter 1 in volume 1 for English and volume 2 for Italian: no reset either.
    assert not numbering_resets([("en", "1", "1"), ("it", "2", "1")])
    assert not numbering_resets([("en", "1", "1.5"), ("en", "2", "1.5")])


def _rel(volume, chapter, per_volume=True, **kw):
    sort = logical_sort_number(chapter, volume, per_volume)
    key = logical_chapter_key(chapter, f"r-{volume}-{chapter}", volume, per_volume)
    base = dict(language_code="en", is_unavailable=False, is_external=False, pages=30, group_ids=[],
                volume=volume, chapter=chapter, publish_at=None, readable_at=None,
                logical_chapter=SimpleNamespace(chapter_key=key, sort_number=sort))
    base.update(kw)
    return SimpleNamespace(**base)


def test_owning_vol3_ch2_no_longer_hides_vol15_ch2():
    owned = {logical_chapter_key("2", "x", str(v), True) for v in range(1, 15)} | {"v15:c:1"}
    new = _rel("15", "2")
    assert release_number(new) == 15002
    assert release_wanted(new, {"en"}, "all", set(), True, 15001, owned, None)
    old = _rel("14", "2")
    assert not release_wanted(old, {"en"}, "all", set(), True, 15001, owned, None)


def test_series_release_number_is_the_chapter():
    r = _rel("3", "25", per_volume=False)
    assert release_number(r) == 25
    assert release_wanted(r, {"en"}, "all", set(), True, 20, None, None)


def _manga(start):
    return SimpleNamespace(monitoring=SimpleNamespace(start_chapter=start))


def test_start_conversion():
    releases = [_rel(str(v), str(c)) for v in (1, 2, 3) for c in range(1, 6)]
    # Owned chapters decide: start right at the newest owned one.
    assert _convert_start(_manga(6), releases, True, [2005.0, 3001.0]) == 3001.0
    # No owned chapters: the newest volume that has that chapter number.
    assert _convert_start(_manga(4), releases, True, []) == 3004.0
    # A chapter number no volume has: after everything listed now.
    assert _convert_start(_manga(9), releases, True, []) == 3005.0001
    assert _convert_start(_manga(None), releases, True, []) is None
    assert _convert_start(_manga(15002), releases, True, []) == 15002
    # Back to series numbering.
    assert _convert_start(_manga(15002), releases, False, []) == 2


def test_owned_chapter_with_unknown_volume_still_covers_the_number():
    # JAM chapter whose MangaDex release is gone: key stays "c:30".
    assert not release_wanted(_rel("7", "30"), {"en"}, "all", set(), True, 6029.0001, {"c:30"}, None)
    assert release_wanted(_rel("7", "52"), {"en"}, "all", set(), True, 6029.0001, {"c:30"}, None)
