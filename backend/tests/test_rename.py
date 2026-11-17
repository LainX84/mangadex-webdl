"""Changing a manga's title moves its library folder and every stored path."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select, text

from app.db import session_scope
from app.enums import JobStatus, LocalStatus
from app.models import DownloadJob, Manga
from app.services.rename import (
    RenameBusy,
    RenameError,
    folder_lock_key,
    rename_manga,
    title_candidates,
)
from tests.helpers import GROUP_A, add, releases, run_queue, scan
from tests.test_api_auth import admin, app_client, authed  # noqa: F401  (fixtures)


def _downloaded_manga(md, title="Ore no Imouto"):
    mid = md.add_manga(title=title)
    md.mangas[mid]["attributes"]["title"] = {"ja-ro": title}
    md.mangas[mid]["attributes"]["altTitles"] = [
        {"en": "My Little Sister"}, {"en": "My Little Sister Can't Be This Cute"},
        {"it": "La mia sorellina"}, {"ja": "俺の妹"}, {"ko": "내 여동생"},
    ]
    md.add_chapter(mid, volume="1", chapter="1", lang="en", groups=[GROUP_A], pages=2)
    md.add_chapter(mid, volume="1", chapter="2", lang="en", groups=[GROUP_A], pages=2)
    add(md, mid, languages=("en", "it"))
    scan(md, mid)
    run_queue(md)
    assert all(r.local_status == LocalStatus.DOWNLOADED for r in releases(mid))
    return mid


def _rename(mid, title, **kw):
    with session_scope() as s:
        return rename_manga(s, uuid.UUID(mid), title, **kw)


def test_title_candidates_order_and_dedupe():
    attrs = {"title": {"ja-ro": "Ore no Imouto"}}
    alts = [{"ja": "俺の妹"}, {"en": "My Little Sister"}, {"it": "La mia sorellina"},
            {"en": "My Little Sister"}, {"en": "My  little sister "}, {"en": "OreImo"}, {"ko": "내 여동생"}]
    out = title_candidates(attrs, alts, ["en", "it"])
    assert [(t["language"], t["title"]) for t in out] == [
        ("en", "My Little Sister"), ("en", "OreImo"), ("it", "La mia sorellina"),
        ("ja-ro", "Ore no Imouto"), ("ja", "俺の妹"), ("ko", "내 여동생"),
    ]
    assert [t["preferred"] for t in out] == [True, True, True, False, False, False]
    assert out[3]["source"] == "main"


def test_rename_moves_folder_and_paths(md, library):
    mid = _downloaded_manga(md)
    old_dir = library / "Ore no Imouto"
    assert old_dir.is_dir()

    preview = _rename(mid, "My Little Sister", dry_run=True)
    assert preview["folder_from"] == "Ore no Imouto" and preview["folder_to"] == "My Little Sister"
    assert preview["chapters_on_disk"] == 2 and preview["moved"] is False
    assert old_dir.is_dir()  # dry run touches nothing

    report = _rename(mid, "  My   Little Sister ")
    assert report["moved"] is True
    new_dir = library / "My Little Sister"
    assert new_dir.is_dir() and not old_dir.exists()
    for r in releases(mid):
        # The group is part of the chapter folder name, never a folder level (default naming).
        assert r.folder_rel_path.startswith("My Little Sister/ENG/Volume ")
        assert (library / r.folder_rel_path).is_dir()
    with session_scope() as s:
        m = s.get(Manga, uuid.UUID(mid))
        assert (m.title_override, m.folder_name, m.display_title) == ("My Little Sister", "My Little Sister",
                                                                       "My Little Sister")
        for j in s.scalars(select(DownloadJob)):
            assert j.target_rel_path.startswith("My Little Sister/")
    assert json.loads((new_dir / ".mdm-manga.json").read_text())["title"] == "My Little Sister"

    # New downloads land in the new folder.
    md.add_chapter(mid, volume="1", chapter="3", lang="en", groups=[GROUP_A], pages=2)
    scan(md, mid)
    run_queue(md)
    assert (new_dir / "ENG").exists()
    assert sorted(p.name for p in (new_dir / "ENG").iterdir() if not p.name.startswith(".")) == [
        "Volume 01 Chapter 01", "Volume 01 Chapter 02", "Volume 01 Chapter 03"]
    assert not old_dir.exists()

    # Back to the MangaDex title.
    back = _rename(mid, None)
    assert back["title_override"] is None and back["folder_to"] == "Ore no Imouto"
    assert old_dir.is_dir() and not new_dir.exists()


def test_rename_without_files_only_changes_names(md, library):
    mid = md.add_manga(title="Nothing Yet")
    add(md, mid)
    report = _rename(mid, "Still Nothing")
    assert report["moved"] is False and report["folder_to"] == "Still Nothing"
    assert not (library / "Still Nothing").exists()


def test_rename_refused_while_downloading(md):
    mid = _downloaded_manga(md)
    with session_scope() as s:
        j = s.scalars(select(DownloadJob)).first()
        j.status = JobStatus.DOWNLOADING
    with pytest.raises(RenameBusy):
        _rename(mid, "Other")
    with session_scope() as s:
        assert s.get(Manga, uuid.UUID(mid)).folder_name == "Ore no Imouto"


def test_rename_name_clash(md, library):
    mid = _downloaded_manga(md)
    (library / "Taken").mkdir()
    (library / "Taken" / "something.txt").write_text("x")
    with pytest.raises(RenameError, match="already exists"):
        _rename(mid, "Taken")
    assert (library / "Ore no Imouto").is_dir()
    (library / "Empty").mkdir()  # an empty leftover folder is replaced
    assert _rename(mid, "Empty")["moved"] is True


def test_dispatcher_skips_manga_being_renamed(md):
    from app.services.jobs import dispatch_jobs

    mid = md.add_manga(title="Locked")
    md.add_chapter(mid, volume="1", chapter="1", lang="en", groups=[GROUP_A], pages=2)
    add(md, mid)
    scan(md, mid)
    with session_scope() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(7341, :k)"), {"k": folder_lock_key(uuid.UUID(mid))})
        with session_scope() as s:
            assert dispatch_jobs(s, storage_ok=True, enqueue=lambda *a: None) == 0
    with session_scope() as s:
        assert dispatch_jobs(s, storage_ok=True, enqueue=lambda *a: None) == 1


def test_titles_and_rename_api(md, authed, library):
    mid = _downloaded_manga(md)
    r = authed.get(f"/api/manga/{mid}/titles")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live"] is True and body["folder_name"] == "Ore no Imouto"
    assert [t["title"] for t in body["titles"]][:4] == [
        "My Little Sister", "My Little Sister Can't Be This Cute", "La mia sorellina", "Ore no Imouto"]

    prev = authed.post(f"/api/manga/{mid}/rename", json={"title": "La mia sorellina", "dry_run": True}).json()
    assert prev["report"]["folder_to"] == "La mia sorellina" and "manga" not in prev
    done = authed.post(f"/api/manga/{mid}/rename", json={"title": "La mia sorellina"})
    assert done.status_code == 200, done.text
    assert done.json()["manga"]["title"] == "La mia sorellina"
    assert done.json()["manga"]["folder_name"] == "La mia sorellina"
    assert (library / "La mia sorellina").is_dir()

    (library / "Busy").mkdir()
    (library / "Busy" / "x").write_text("x")
    assert authed.post(f"/api/manga/{mid}/rename", json={"title": "Busy"}).status_code == 409


def test_search_lists_all_mangadex_titles(md, authed):
    mid = md.add_manga(title="Kara no Kyoukai")
    md.mangas[mid]["attributes"]["title"] = {"ja-ro": "Kara no Kyoukai"}
    md.mangas[mid]["attributes"]["altTitles"] = [{"en": "The Garden of Sinners"}, {"ja": "空の境界"}]
    r = authed.get(f"/api/mangadex/search?q={mid}")
    assert r.status_code == 200, r.text
    titles = r.json()["items"][0]["titles"]
    assert [(t["language"], t["title"], t["source"]) for t in titles] == [
        ("ja-ro", "Kara no Kyoukai", "main"), ("ja", "空の境界", "alt"), ("en", "The Garden of Sinners", "alt")]


def test_preview_lists_chapters_without_placeholders_or_excluded_groups(md, authed):
    from tests.helpers import GROUP_B, set_settings

    set_settings(excluded_group_names=["Team Y"])
    mid = md.add_manga(title="Preview")
    md.add_chapter(mid, volume="1", chapter="1", lang="en", groups=[GROUP_A], pages=10)
    md.add_chapter(mid, volume="1", chapter="2", lang="en", groups=[GROUP_A], pages=10)
    md.add_chapter(mid, volume="1", chapter="2", lang="it", groups=[GROUP_A], pages=10)
    md.add_chapter(mid, volume="1", chapter="2.50", lang="en", groups=[], pages=10)
    md.add_chapter(mid, volume="1", chapter="3", lang="en", groups=[GROUP_B], pages=10)  # excluded group
    md.add_chapter(mid, volume=None, chapter="4", lang="en", groups=[GROUP_A], pages=1,
                   external_url="https://mangaplus.example/4")  # official link
    md.add_chapter(mid, volume=None, chapter=None, title="Oneshot", lang="en", groups=[GROUP_A], pages=5)
    r = authed.get(f"/api/mangadex/manga/{mid}/groups?languages=en,it")
    assert r.status_code == 200, r.text
    got = [(c["chapter"], c["language"], c["groups"]) for c in r.json()["chapters"]]
    assert got == [("1", "en", [GROUP_A[0]]), ("2", "en", [GROUP_A[0]]), ("2", "it", [GROUP_A[0]]),
                   ("2.5", "en", [""]), (None, "en", [GROUP_A[0]])]
    assert r.json()["chapters"][-1]["title"] == "Oneshot"
    assert [g["name"] for g in r.json()["groups"]] == ["Team X"]


def test_same_manga_cannot_be_added_twice(md, authed, monkeypatch):
    import app.api.routes.manga as routes
    from sqlalchemy import func

    monkeypatch.setattr(routes, "enqueue_scan", lambda *a, **k: True)
    mid = md.add_manga(title="Only Once")
    body = {"languages": ["en"], "group_mode": "all"}
    assert authed.post("/api/manga", json={**body, "reference": f"https://mangadex.org/title/{mid}/only-once"}).status_code == 201
    for ref in (mid, mid.upper(), f"https://mangadex.org/title/{mid}", f"https://mangadex.org/title/{mid}/other-slug?tab=chapters"):
        r = authed.post("/api/manga", json={**body, "reference": ref})
        assert r.status_code == 409, (ref, r.text)
        assert "already in the library" in r.json()["detail"]
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Manga)) == 1
    item = authed.get(f"/api/mangadex/search?q={mid}").json()["items"][0]
    assert item["followed"] is True


def test_dashboard_lists_recent_downloads_newest_first(md, authed):
    mid = md.add_manga(title="Recent")
    for n in ("1", "2", "3"):
        md.add_chapter(mid, volume="1", chapter=n, lang="en", groups=[GROUP_A], pages=2)
    add(md, mid)
    scan(md, mid)
    run_queue(md)
    d = authed.get("/api/dashboard").json()
    got = [(j["release"]["chapter"], j["manga"]["title"], j["status"]) for j in d["recent_downloads"]]
    assert sorted(got) == [("1", "Recent", "completed"), ("2", "Recent", "completed"), ("3", "Recent", "completed")]
    finished = [j["finished_at"] for j in d["recent_downloads"]]
    assert finished == sorted(finished, reverse=True)
    q = authed.get("/api/queue?group=completed&limit=2").json()
    assert len(q["items"]) == 2 and q["counts"]["completed"] == 3
    assert [j["finished_at"] for j in q["items"]] == finished[:2]
