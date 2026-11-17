"""Late chapters below the start chapter, same-group re-uploads and version checks."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.enums import LocalStatus
from app.services.catalog import WantContext, release_wanted, same_group_reuploads, version_changed

SET_AT = datetime(2026, 5, 1, tzinfo=UTC)
BEFORE = SET_AT - timedelta(days=30)
AFTER = SET_AT + timedelta(days=30)
G1, G2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def _rel(chapter, published, lang="en", groups=(G1,), status=LocalStatus.MISSING, rid=None, **kw):
    key = f"c:{chapter}" if chapter is not None else f"x:{rid or chapter}"
    sort = float(chapter) if chapter is not None else None
    base = dict(id=rid or f"r-{chapter}-{lang}-{published:%j}-{groups[0][:1] if groups else 'u'}",
                language_code=lang, is_unavailable=False, is_external=False, pages=20,
                group_ids=list(groups), volume=None, chapter=chapter, publish_at=published, readable_at=None,
                local_status=status, folder_rel_path=None, first_seen_at=published, downloaded_at=None,
                logical_chapter=SimpleNamespace(chapter_key=key, sort_number=sort))
    base.update(kw)
    return SimpleNamespace(**base)


def _ctx(*releases):
    first = {}
    for r in releases:
        k = (r.language_code, r.logical_chapter.chapter_key)
        first[k] = min(first.get(k, r.publish_at), r.publish_at)
    return WantContext(late_after=SET_AT, first_published=first)


def _wanted(r, ctx, start=40):
    return release_wanted(r, {"en", "it"}, "all", set(), True, start, None, None, None, ctx=ctx)


def test_late_extra_below_start_is_wanted():
    extra = _rel("12.5", AFTER)
    assert _wanted(extra, _ctx(extra))


def test_skipped_chapter_uploaded_later_is_wanted():
    old = _rel("22", BEFORE)
    late = _rel("23", AFTER)
    ctx = _ctx(old, late)
    assert _wanted(late, ctx)
    assert not _wanted(old, ctx)


def test_new_upload_of_an_existing_number_is_not_wanted():
    # Another group, or a re-upload of a chapter that was removed (still in the database).
    removed = _rel("15", BEFORE, is_unavailable=True)
    again = _rel("15", AFTER, groups=(G2,))
    assert not _wanted(again, _ctx(removed, again))


def test_number_existing_only_in_another_language_counts_as_new():
    en = _rel("12.5", BEFORE, lang="en")
    it = _rel("12.5", AFTER, lang="it")
    assert _wanted(it, _ctx(en, it))


def test_no_cutoff_keeps_the_old_behaviour():
    extra = _rel("12.5", AFTER)
    assert not _wanted(extra, WantContext())
    assert not _wanted(extra, None)
    unnumbered = _rel(None, BEFORE, rid="oneshot")
    assert _wanted(unnumbered, _ctx(unnumbered))  # unnumbered extras: always, as before


def test_at_or_above_start_unchanged():
    old = _rel("41", BEFORE)
    assert _wanted(old, _ctx(old))


def test_owned_elsewhere_context_still_wins():
    extra = _rel("12.5", AFTER)
    ctx = WantContext(owned_keys=frozenset({"c:12.5"}), extras_since=BEFORE)
    assert not _wanted(extra, ctx)


def _have(chapter, published, **kw):
    return _rel(chapter, published, status=LocalStatus.DOWNLOADED, folder_rel_path=f"M/Ch {chapter}",
                downloaded_at=published + timedelta(hours=1), **kw)


def test_same_group_reupload_replaces_old_copy():
    old = _have("45", BEFORE, rid="old")
    new = _rel("45", AFTER, rid="new")
    assert same_group_reuploads([old, new]) == [(new, old)]


def test_other_group_or_language_is_not_a_reupload():
    old = _have("45", BEFORE, rid="old")
    assert same_group_reuploads([old, _rel("45", AFTER, groups=(G2,), rid="g2")]) == []
    assert same_group_reuploads([old, _rel("45", AFTER, lang="it", rid="it")]) == []
    assert same_group_reuploads([old, _rel("45", AFTER, groups=(), rid="nogroup")]) == []


def test_long_standing_duplicate_is_not_a_reupload():
    # Both uploads were already on MangaDex when the chapter was downloaded.
    old = _have("45", BEFORE, rid="old")
    dup = _rel("45", BEFORE + timedelta(minutes=5), rid="dup", first_seen_at=BEFORE)
    assert same_group_reuploads([old, dup]) == []


def test_only_the_newest_reupload_is_taken():
    old = _have("45", BEFORE, rid="old")
    v2 = _rel("45", AFTER, rid="v2")
    v3 = _rel("45", AFTER + timedelta(days=1), rid="v3")
    assert same_group_reuploads([old, v2, v3]) == [(v3, old)]


def test_reupload_respects_filters_and_status():
    old = _have("45", BEFORE, rid="old")
    new = _rel("45", AFTER, rid="new")
    assert same_group_reuploads([old, new], lambda r: False) == []
    gone = _rel("45", BEFORE, rid="gone", status=LocalStatus.MISSING_ON_DISK, folder_rel_path=None)
    assert same_group_reuploads([gone, new]) == []


def test_version_change_detection():
    manifest = [{"index": 1, "file": "001.jpg", "source": "a.jpg"}]
    r = SimpleNamespace(local_status=LocalStatus.DOWNLOADED, is_unavailable=False, version=3,
                        downloaded_version=2, page_manifest=manifest)
    assert version_changed(r)
    r.downloaded_version = 3
    assert not version_changed(r)
    r.downloaded_version = None  # unknown: never re-checked
    assert not version_changed(r)
    r.downloaded_version, r.page_manifest = 2, [{"index": 1, "file": "001.jpg"}]  # imported, no sources
    assert not version_changed(r)
    r.page_manifest, r.local_status = manifest, LocalStatus.IMPORTED
    assert not version_changed(r)
