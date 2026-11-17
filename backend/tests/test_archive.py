"""Read-only comics archive: name parsing, CBZ inspection, index and suggestions.

File and folder names are modelled on a real collection.
"""

from __future__ import annotations

import os
import uuid
import zipfile
from pathlib import Path

import pytest

from app.services.archive import (
    FolderAnalysis,
    MangaDexChapters,
    analyze_folder,
    chapters_inside,
    folder_title,
    language_of,
    norm_title,
    parse_archive_name,
    resolve,
)


# The archive layout of the tests (an Italian collection): scanlations searched in full,
# official folders only for "… - Custom" volumes.
LAYOUT_DIRS = ["Manga/Inglese/Non ufficiale", "Manga/Italiano/Non ufficiale", "Manhua/Inglese/Non ufficiale",
               "Manhua/Italiano/Non ufficiale", "Manhwa/Inglese/Non ufficiale", "Manhwa/Italiano/Non ufficiale"]
LAYOUT_CUSTOM_DIRS = ["Manga/Inglese/Ufficiale", "Manga/Italiano/Ufficiale", "Manhua/Inglese/Ufficiale",
                      "Manhua/Italiano/Ufficiale", "Manhwa/Inglese/Ufficiale", "Manhwa/Italiano/Ufficiale"]


@pytest.mark.parametrize("name,title,expected", [
    ("Ichi the Witch c046.cbz", "Ichi the Witch", ("chapter", 46)),
    ("Blue Wings - Chapter 442 - The End.cbz", "Blue Wings", ("chapter", 442)),
    ("Please Bully Me, Miss Villainess! Vol.08 Ch.110 - Just Like a Kid.cbz", "Please Bully Me, Miss Villainess!", ("chapter", 110)),
    ("Please Bully Me, Miss Villainess! Vol.11 Ch.143.5 - Extra - Happy Chinese New Year!.cbz", "Please Bully Me, Miss Villainess!", ("chapter", 143.5)),
    ("Volume 4 Chapter 33 - Let's End the Friendship Game.cbz", "When the Villainess Meets the Yandere", ("chapter", 33)),
    ("Dragon Ball Z v09.cbz", "Dragon Ball Z", ("volume", 9)),
    ("Volume 6.cbz", "Super Cub", ("volume", 6)),
    ("Twin Star Exorcists 27.cbz", "Twin Star Exorcists", ("volume", 27)),
    ("RuriDragon 05 - Custom.cbz", "RuriDragon", ("volume", 5)),
    ("監禁王 - 第03巻.cbz", "監禁王", ("volume", 3)),
    ("Mob Psycho 100 c012.cbz", "Mob Psycho 100", ("chapter", 12)),       # number in the title
    ("Mob Psycho 100 v03.cbz", "Mob Psycho 100", ("volume", 3)),
    ("Shoulder-a-Coffin Kuro Side Story.cbz", "Shoulder-a-Coffin Kuro", ("other", None)),
    ("A Couple of Cuckoos 34 - Custom.cbz", "A Couple of Cuckoos", ("volume", 34)),
])


def test_archive_names(name, title, expected):
    kind, num = parse_archive_name(name, title)
    assert (kind, num) == (expected[0], None if expected[1] is None else float(expected[1]))


def test_chapters_inside_volumes():
    folders = ["Volume 1 Chapter 00/", "Volume 1 Chapter 00/001.jpg", "Volume 1 Chapter 05/020.jpg",
               "Volume 1 Chapter 03/001.jpg"]
    assert max(chapters_inside(folders)) == 5
    assert max(chapters_inside(["Volume 15 Chapter 78.1/01.jpg", "Volume 15 Chapter 80.2/01.jpg"])) == 80.2
    pages = ["Flying Witch - c043 (v08) - p000 [Kodansha Comics] [Digital] [1r0n] {HQ}.jpg",
             "Flying Witch - c048 (v08) - p174-p175 [Kodansha Comics] [Digital] [1r0n] {HQ}.jpg"]
    assert max(chapters_inside(pages)) == 48
    assert max(chapters_inside(["Nukozuke! - c024#1 (v01) - p168 [One Peace Books] [Digital] [1r0n].png"])) == 24
    assert max(chapters_inside(["A Certain Scientific Railgun - c017x2 (v03) - p213 [Digital-HD].jpg"])) == 17
    assert max(chapters_inside(["Volume 6 Chapter 31.5 - Part 2/003.jpg"])) == 31.5
    assert chapters_inside(["001.jpg", "002.png", "ComicInfo.xml"]) == []
    assert chapters_inside(["p000.png", "p199.jpg"]) == []


def test_paths_and_titles():
    assert language_of("Manga/Inglese/Non ufficiale/In Progress/X") == "en"
    assert language_of("Manhwa/Italiano/R-18/Completed/X") == "it"
    assert folder_title("Tomgirl [END]") == "Tomgirl"
    assert norm_title("Handsome Girl and Sheltered Girl [END]") == norm_title("handsome girl & sheltered girl")
    assert norm_title("Let's Take a Walk") == norm_title("Lets Take a Walk")


def _cbz(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            if n.endswith("/"):
                z.writestr(n, "")
            else:
                z.writestr(n, b"x")


def test_analyze_folder_mixed_volumes_and_chapters(tmp_path):
    t = "A Harem in the Fantasy World Dungeon"
    _cbz(tmp_path / f"{t} v01.cbz", [f"Volume 1 Chapter 0{i}/001.jpg" for i in range(6)])
    _cbz(tmp_path / f"{t} v02.cbz", [f"Volume 2 Chapter 0{i}/001.jpg" for i in range(6, 10)])
    res = analyze_folder(tmp_path, sorted(os.listdir(tmp_path)), t)
    assert res.chapter == 9 and res.chapter_from == "inside" and res.chapter_file.endswith("v02.cbz")
    # Later single chapters win over the volumes.
    _cbz(tmp_path / f"{t} c012.cbz", ["001.jpg"])
    res = analyze_folder(tmp_path, sorted(os.listdir(tmp_path)), t)
    assert res.chapter == 12 and res.chapter_from == "file"


def test_volume_without_chapter_numbers_uses_mangadex_volumes(tmp_path):
    _cbz(tmp_path / "Magikano Volume 04.cbz", ["001.jpg", "202.jpg"])
    res = analyze_folder(tmp_path, ["Magikano Volume 04.cbz"], "Magikano")
    assert res.chapter is None and res.volume == 4
    md = MangaDexChapters.build([(n, "3") for n in range(10, 20)] + [(n, "4") for n in range(20, 27)]
                                + [(n, None) for n in range(27, 31)])
    r = resolve(res, md)
    assert r["last_chapter"] == 26 and r["source"] == "volume" and r["next_chapter"] == 27 and r["after_count"] == 4
    # MangaDex without volume data: we only know the volume.
    r = resolve(res, MangaDexChapters.build([(n, None) for n in range(1, 30)]))
    assert r["last_chapter"] is None and r["last_volume"] == 4 and r["source"] == "volume_unmapped"


def test_resolve_up_to_date_and_decimal_next():
    res = FolderAnalysis(chapter=452, chapter_file="Blue Wings - Chapter 452.cbz", chapter_from="file")
    assert resolve(res, MangaDexChapters.build([(n, None) for n in range(1, 453)]))["up_to_date"] is True
    r = resolve(FolderAnalysis(chapter=442, chapter_from="file"),
                MangaDexChapters.build([(n, None) for n in range(1, 453)] + [(442.5, None)]))
    assert r["next_chapter"] == 442.5 and r["after_count"] == 11 and r["after_last"] == 452


def test_broken_archive_is_reported_not_fatal(tmp_path):
    (tmp_path / "X v02.cbz").write_bytes(b"not a zip")
    _cbz(tmp_path / "X v01.cbz", ["X - c007 (v01) - p001.jpg"])
    res = analyze_folder(tmp_path, ["X v01.cbz", "X v02.cbz"], "X")
    assert res.errors and res.chapter == 7


# --------------------------------------------------------------------------- with the database

DB_URL = os.environ.get("MDM_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("MDM_TEST_REDIS_URL")
needs_db = pytest.mark.skipif(not (DB_URL and REDIS_URL), reason="needs MDM_TEST_DATABASE_URL and MDM_TEST_REDIS_URL")


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MDM_DATABASE_URL", DB_URL)
    monkeypatch.setenv("MDM_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("MDM_LOG_JSON", "false")
    root = tmp_path / "archive"
    monkeypatch.setenv("MDM_ARCHIVE_ROOT", str(root))
    from app.config import reset_config_cache
    from app.db import Base, get_engine, reset_engine
    from app.redis_client import get_redis, reset_redis
    from app.settings_service import invalidate_cache

    reset_config_cache()
    reset_engine()
    reset_redis()
    invalidate_cache()
    get_redis().flushdb()
    import app.models  # noqa: F401

    with get_engine().begin() as c:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables if t.name not in {"languages", "alembic_version"})
        c.exec_driver_sql(f"TRUNCATE {tables} CASCADE")
    return root


@needs_db
def test_index_and_suggest(db_env):
    from app.db import session_scope
    from app.services import archive as arc

    root = db_env
    blue = root / "Manhua/Inglese/Non ufficiale/In Progress/Blue Wings"
    blue.mkdir(parents=True)
    for n in (1, 2, 442):
        (blue / f"Blue Wings - Chapter {n} - Title.cbz").write_bytes(b"")
    (blue / "Extra 1").mkdir()
    (blue / "Extra 1" / "Blue Wings - Chapter 999.cbz").write_bytes(b"")  # sub-folders are ignored
    official = root / "Manga/Inglese/Ufficiale/Blue Wings"
    official.mkdir(parents=True)
    (official / "Blue Wings v99.cbz").write_bytes(b"")                     # official volume: ignored
    ita = root / "Manhua/Italiano/Non ufficiale/Completed/Ali Blu [END]"
    ita.mkdir(parents=True)
    (ita / "Ali Blu c010.cbz").write_bytes(b"")

    with session_scope() as s:
        res = arc.rebuild_index(s, dirs=LAYOUT_DIRS, custom_dirs=LAYOUT_CUSTOM_DIRS)
    assert res["folders"] == 2
    md = arc.MangaDexChapters.build([(float(n), None) for n in range(1, 453)])
    with session_scope() as s:
        out = arc.suggest(s, ["Lan Se Zhi Yi"], [{"en": "Blue Wings"}, {"it": "Ali Blu"}], ["en"], md)
    assert out["best"]["folder"].endswith("In Progress/Blue Wings")
    assert out["best"]["last_chapter"] == 442 and out["best"]["next_chapter"] == 443 and out["best"]["after_count"] == 10
    assert [c["language_match"] for c in out["candidates"]] == [True, False]  # Italian folder listed as alternative

    # The analysis is cached until the folder changes.
    (blue / "Blue Wings - Chapter 452 - Title.cbz").write_bytes(b"")
    with session_scope() as s:
        arc.rebuild_index(s, dirs=LAYOUT_DIRS, custom_dirs=LAYOUT_CUSTOM_DIRS)
        out = arc.suggest(s, ["Blue Wings"], [], ["en"], md)
    assert out["best"]["last_chapter"] == 452 and out["best"]["up_to_date"] is True
    # Removed folders leave the index.
    for f in ita.iterdir():
        f.unlink()
    with session_scope() as s:
        assert arc.rebuild_index(s, dirs=LAYOUT_DIRS, custom_dirs=LAYOUT_CUSTOM_DIRS)["removed"] == 1


@needs_db
def test_suggest_endpoint_for_title_not_in_library(db_env, monkeypatch):
    import httpx
    import respx
    from fastapi.testclient import TestClient

    from app.api.deps import require_auth
    from app.api.main import create_app
    from app.db import session_scope
    from app.services import archive as arc

    folder = db_env / "Manga/Inglese/Non ufficiale/In Progress/Super Cub"
    folder.mkdir(parents=True)
    _cbz(folder / "Volume 6.cbz", ["Volume 6 Chapter 27/001.jpg", "Volume 6 Chapter 31.5 - Part 2/003.jpg"])
    with session_scope() as s:
        arc.rebuild_index(s, dirs=LAYOUT_DIRS, custom_dirs=LAYOUT_CUSTOM_DIRS)
    mid = str(uuid.uuid4())
    manga = {"id": mid, "type": "manga", "attributes": {"title": {"ja-ro": "Super Cub"}, "altTitles": []},
             "relationships": []}
    chapters = [{"id": str(uuid.uuid4()), "type": "chapter",
                 "attributes": {"translatedLanguage": "en", "chapter": str(n), "volume": None, "pages": 10},
                 "relationships": []} for n in (30, 31, 31.5, 32, 33)]
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: None
    with respx.mock(assert_all_called=False) as router:
        router.get(f"https://api.mangadex.org/manga/{mid}").mock(
            return_value=httpx.Response(200, json={"result": "ok", "data": manga}))
        router.get(f"https://api.mangadex.org/manga/{mid}/feed").mock(
            return_value=httpx.Response(200, json={"result": "ok", "data": chapters, "total": len(chapters)}))
        r = TestClient(app).get(f"/api/archive/suggest/{mid}?languages=en")
    assert r.status_code == 200, r.text
    best = r.json()["best"]
    assert best["last_chapter"] == 31.5 and best["source"] == "inside" and best["next_chapter"] == 32


@needs_db
def test_custom_volumes_in_official_folders(db_env):
    from app.db import session_scope
    from app.services import archive as arc

    folder = db_env / "Manga/Inglese/Ufficiale/A Couple of Cuckoos"
    folder.mkdir(parents=True)
    _cbz(folder / "A Couple of Cuckoos v31.cbz", ["A Couple of Cuckoos - c400 (v31) - p001.jpg"])  # official: ignored
    for vol, chapters in ((33, range(286, 295)), (34, range(295, 304))):
        _cbz(folder / f"A Couple of Cuckoos {vol} - Custom.cbz",
             [f"A Couple of Cuckoos c{c}/001.jpg" for c in chapters] + [f"A Couple of Cuckoos c{chapters[-1]}/Thumbs.db"])
    only_official = db_env / "Manga/Inglese/Ufficiale/Frieren"
    only_official.mkdir(parents=True)
    (only_official / "Frieren 07.cbz").write_bytes(b"")
    with session_scope() as s:
        assert arc.rebuild_index(s, dirs=LAYOUT_DIRS, custom_dirs=LAYOUT_CUSTOM_DIRS)["folders"] == 1                          # Frieren has no Custom volume
        md = arc.MangaDexChapters.build([(float(n), None) for n in range(1, 310)])
        out = arc.suggest(s, ["Kakkou no Iinazuke"], [{"en": "A Couple of Cuckoos"}], ["en"], md)
    best = out["best"]
    assert best is not None, out
    assert best["custom_only"] is True and best["last_chapter"] == 303 and best["next_chapter"] == 304
    assert best["evidence"] == "A Couple of Cuckoos 34 - Custom.cbz"


@needs_db
def test_refresh_single_folder(db_env):
    from fastapi.testclient import TestClient

    from app.api.deps import require_auth
    from app.api.main import create_app
    from app.db import session_scope
    from app.services import archive as arc

    folder = db_env / "Manga/Inglese/Ufficiale/The Moon on a Rainy Night"
    folder.mkdir(parents=True)
    _cbz(folder / "The Moon on a Rainy Night v11 - Custom.cbz", ["Volume 11 Chapter 41 - Rain/001.jpg"])
    with session_scope() as s:
        arc.rebuild_index(s, dirs=LAYOUT_DIRS, custom_dirs=LAYOUT_CUSTOM_DIRS)
        md = arc.MangaDexChapters.build([(float(n), None) for n in range(1, 53)])
        assert arc.suggest(s, ["The Moon on a Rainy Night"], [], ["en"], md)["best"]["last_chapter"] == 41

    # New volume added: only this folder is re-read, without a full index run.
    _cbz(folder / "The Moon on a Rainy Night v13 - Custom.cbz", ["Volume 13 Chapter 52.1 - Mutual Love/001.jpg"])
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: None
    client = TestClient(app)
    rel = "Manga/Inglese/Ufficiale/The Moon on a Rainy Night"
    r = client.post("/api/archive/folder/rescan", json={"folder": rel})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True and r.json()["chapter"] == 52.1
    with session_scope() as s:
        best = arc.suggest(s, ["The Moon on a Rainy Night"], [], ["en"], md)["best"]
    assert best["last_chapter"] == 52.1 and best["evidence"] == "The Moon on a Rainy Night v13 - Custom.cbz"

    # Same file name, new content: still re-read.
    _cbz(folder / "The Moon on a Rainy Night v13 - Custom.cbz", ["Volume 13 Chapter 52.2/001.jpg"])
    r = client.post("/api/archive/folder/rescan", json={"folder": rel})
    assert r.json()["changed"] is False and r.json()["chapter"] == 52.2

    # Only indexed folders can be refreshed.
    assert client.post("/api/archive/folder/rescan", json={"folder": "../etc"}).status_code == 404
    # A folder that lost its archives leaves the index.
    for f in folder.iterdir():
        f.unlink()
    assert client.post("/api/archive/folder/rescan", json={"folder": rel}).json()["status"] == "removed"


@needs_db
def test_default_searches_whole_archive(db_env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ArchiveFolder
    from app.services import archive as arc

    deep = db_env / "Manga/English/Scanlations/In Progress/Blue Wings"
    deep.mkdir(parents=True)
    (deep / "Blue Wings - Chapter 3.cbz").write_bytes(b"")
    (db_env / "loose file.cbz").write_bytes(b"")                       # archives directly in the root: no title
    flat = db_env / "Ali Blu"
    flat.mkdir()
    (flat / "Ali Blu c010.cbz").write_bytes(b"")
    with session_scope() as s:
        res = arc.rebuild_index(s)
    assert res["dirs"] == ["."] and res["folders"] == 2
    with session_scope() as s:
        paths = sorted(f.rel_path for f in s.scalars(select(ArchiveFolder)))
        langs = {f.rel_path: f.language for f in s.scalars(select(ArchiveFolder))}
    assert paths == ["Ali Blu", "Manga/English/Scanlations/In Progress/Blue Wings"]
    assert langs["Manga/English/Scanlations/In Progress/Blue Wings"] == "en" and langs["Ali Blu"] is None
