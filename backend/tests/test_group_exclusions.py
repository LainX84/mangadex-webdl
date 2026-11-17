"""Official-publisher placeholders and user-excluded groups are never downloaded."""

from __future__ import annotations

from sqlalchemy import delete, select

from app.db import session_scope
from app.enums import JobStatus, LocalStatus
from app.models import DownloadJob
from app.services.external_import import import_plan
from app.services.monitor import apply_group_exclusions
from app.settings_service import AppSettings, load_settings, save_settings
from tests.helpers import GROUP_A, GROUP_C, releases, scan
from tests.test_api_auth import admin, app_client, authed  # noqa: F401  (fixtures)

OFFICIAL = ("44444444-4444-4444-4444-444444444444", "Manga UP!")


def _ch(md, mid, chapter, day, group, pages=10, external_url=None):
    ch = md.add_chapter(mid, volume=None, chapter=chapter, title=None, lang="en", groups=[group], pages=pages,
                        external_url=external_url)
    ch["attributes"]["publishAt"] = ch["attributes"]["readableAt"] = f"2024-01-0{day}T00:00:00+00:00"
    return ch


def _follow(md, mid):
    # start_chapter 0: every numbered chapter is "new"; one release per chapter is on.
    import_plan([{"id": mid, "languages": ["en"], "start_chapter": 0, "external": []}], client=md.client)


def _exclude(names):
    with session_scope() as s:
        cur = load_settings(s, use_cache=False)
        save_settings(s, cur.model_copy(update={"excluded_group_names": names}))


def _jobs():
    with session_scope() as s:
        return sorted((j.release.chapter, j.release.group_names[0], j.status)
                      for j in s.scalars(select(DownloadJob)))


def test_settings_normalise_excluded_names():
    s = AppSettings(excluded_group_names=["  Manga  UP! ", "manga up!", "", "Omoi"])
    assert s.excluded_group_names == ["Manga UP!", "Omoi"]
    assert "MangaPlus" in AppSettings().excluded_group_names


def test_placeholders_and_excluded_groups_are_skipped(md):
    _exclude(["team z"])
    mid = md.add_manga(title="Paywalled")
    _ch(md, mid, "1", 1, OFFICIAL, pages=1, external_url="https://global.manga-up.com/manga/1/1")
    _ch(md, mid, "2", 2, GROUP_C)  # excluded by name (case-insensitive)
    _ch(md, mid, "3", 3, GROUP_A)
    _ch(md, mid, "1", 5, GROUP_A)
    _ch(md, mid, "2", 6, GROUP_A)
    _follow(md, mid)

    assert scan(md, mid)["queued"] == 3
    assert _jobs() == [("1", "Team X", JobStatus.PENDING), ("2", "Team X", JobStatus.PENDING),
                       ("3", "Team X", JobStatus.PENDING)]

    # A placeholder that was downloaded in the past does not "own" its chapter number.
    with session_scope() as s:
        s.execute(delete(DownloadJob))
    with session_scope() as s:
        for r in s.scalars(select(type(releases(mid)[0]))):
            if r.external_url:
                r.local_status = LocalStatus.DOWNLOADED
    assert scan(md, mid)["queued"] == 3


def test_apply_exclusions_cancels_jobs_and_queues_other_group(md):
    _exclude([])
    mid = md.add_manga(title="Late exclusion")
    _ch(md, mid, "1", 1, GROUP_C)
    _ch(md, mid, "1", 4, GROUP_A)
    _follow(md, mid)
    scan(md, mid)
    assert _jobs() == [("1", "Team Z", JobStatus.PENDING)]

    _exclude(["Team Z"])
    with session_scope() as s:
        report = apply_group_exclusions(s)
    assert report == {"cancelled": 1, "queued_instead": 1, "manga": 1}
    assert _jobs() == [("1", "Team X", JobStatus.PENDING), ("1", "Team Z", JobStatus.CANCELLED)]
    assert {r.group_names[0]: r.local_status for r in releases(mid)}["Team Z"] == LocalStatus.SKIPPED

    # Nothing else happens on the next scan.
    assert scan(md, mid)["queued"] == 0


def test_settings_api_applies_exclusions(md, authed):
    _exclude([])
    mid = md.add_manga(title="Via API")
    _ch(md, mid, "1", 1, GROUP_C)
    _ch(md, mid, "1", 4, GROUP_A)
    _follow(md, mid)
    scan(md, mid)
    r = authed.put("/api/settings", json={"excluded_group_names": ["Team Z", ""]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["settings"]["excluded_group_names"] == ["Team Z"]
    assert body["group_exclusions"]["cancelled"] == 1
    # Unchanged list: no clean-up pass.
    assert "group_exclusions" not in authed.put("/api/settings", json={"excluded_group_names": ["team z"]}).json()
