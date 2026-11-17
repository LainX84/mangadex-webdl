"""Output formats: WebP pages, one CBZ per chapter, volume CBZ files (app.library.entries, app.services.volumes)."""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from PIL import Image
from sqlalchemy import select

from app.db import session_scope
from app.enums import JobStatus, LocalStatus
from app.library.images import convert_page
from app.library.marker import list_page_files, read_release_marker
from app.models import ChapterRelease, DownloadJob, Manga, ReviewItem, VolumeArchive
from app.services.library_scan import run_library_scan
from app.services.volumes import VolumeError, build_volume, plan_volumes, run_auto_volumes
from tests.helpers import GROUP_A, GROUP_B, _jpeg, add, releases, run_queue, scan, set_settings
from tests.test_api_auth import admin, app_client, authed  # noqa: F401  (fixtures)


def _pixels(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB").tobytes()


def _one_chapter(md, **chapter):
    mid = md.add_manga(title="Formats")
    ch = md.add_chapter(mid, **{"volume": "1", "chapter": "1", "groups": [GROUP_A], "pages": 3, **chapter})
    add(md, mid)
    scan(md, mid)
    run_queue(md)
    return mid, ch


# ------------------------------------------------------------------ WebP


def test_convert_page_original_is_untouched():
    data = _jpeg(3)
    assert convert_page(data, "jpg", "original") == (data, "jpg")


def test_convert_page_lossless_keeps_every_pixel():
    data = _jpeg(5)
    out, ext = convert_page(data, "jpg", "webp_lossless")
    assert ext == "webp" and out[:4] == b"RIFF" and _pixels(out) == _pixels(data)


def _png(mode="RGB", size=(40, 30), color=(10, 200, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def _webp(lossless: bool) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (200, 10, 10)).save(buf, "WEBP", lossless=lossless, quality=80)
    return buf.getvalue()


@pytest.mark.parametrize("mode,target", [("png", "png"), ("jpg", "jpg"), ("webp_lossy", "webp"),
                                         ("webp_lossless", "webp")])
def test_every_source_ends_in_the_chosen_format(mode, target):
    for data, ext in ((_jpeg(4), "jpg"), (_png(), "png"), (_webp(True), "webp"), (_png("RGBA", color=(1, 2, 3, 128)), "png")):
        out, out_ext = convert_page(data, ext, mode, 90, 92)
        assert out_ext == target, (mode, ext)
        with Image.open(io.BytesIO(out)) as im:
            im.load()
        if mode in ("png", "webp_lossless") and ext != target:
            assert _pixels(out) == _pixels(data)  # lossless: every pixel kept


def test_same_format_is_never_compressed_again():
    jpg, webp = _jpeg(6), _webp(False)
    assert convert_page(jpg, "jpg", "jpg", 90, 80) == (jpg, "jpg")
    assert convert_page(webp, "webp", "webp_lossy") == (webp, "webp")
    assert convert_page(webp, "webp", "webp_lossless") == (webp, "webp")  # may be lossy inside


def test_jpg_flattens_transparency_on_white():
    out, ext = convert_page(_png("RGBA", (4, 4), (0, 0, 0, 0)), "png", "jpg", 90, 95)
    with Image.open(io.BytesIO(out)) as im:
        assert ext == "jpg" and im.convert("RGB").getpixel((1, 1)) == (255, 255, 255)


def test_mixed_keeps_jpeg_and_shrinks_png_losslessly():
    jpg = _jpeg(7)
    assert convert_page(jpg, "jpg", "mixed") == (jpg, "jpg")
    gray_in_rgb = _png("RGB", (300, 300), (90, 90, 90))  # grayscale saved as colour
    out, ext = convert_page(gray_in_rgb, "png", "mixed")
    assert len(out) < len(gray_in_rgb) and ext in ("png", "webp") and _pixels(out) == _pixels(gray_in_rgb)


def test_legacy_page_format_value_is_mixed():
    from app.settings_service import AppSettings

    assert AppSettings.model_validate({"page_format": "webp_lossless_png"}).page_format == "mixed"


def test_too_big_for_webp_uses_the_closest_format():
    tall = _png("L", (10, 17000), 255)
    assert convert_page(tall, "png", "webp_lossless")[1] == "png"
    assert convert_page(tall, "png", "webp_lossy")[1] == "jpg"


def test_convert_page_keeps_animated_gif():
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    data = buf.getvalue()
    assert convert_page(data, "gif", "png") == (data, "gif")


def test_webp_lossless_download_and_version_check(md, library):
    set_settings(page_format="webp_lossless")
    mid, ch = _one_chapter(md)
    (rel,) = releases(mid)
    folder = library / rel.folder_rel_path
    pages = list_page_files(folder)
    assert len(pages) == 3 and all(p.endswith(".webp") for p in pages)
    marker = read_release_marker(folder)
    # The MangaDex page names are kept: they are what version checks compare.
    assert [m["source"] for m in marker["pages"]] == ch["_files"]
    # A metadata-only MangaDex version: same page names -> nothing downloaded, no image compared.
    ch["attributes"]["version"] = 2
    scan(md, mid)
    run_queue(md)
    assert list_page_files(folder) == pages
    with session_scope() as s:
        job = s.scalars(select(DownloadJob).order_by(DownloadJob.id.desc())).first()
        assert job.is_refresh and job.status == JobStatus.COMPLETED
        assert s.get(ChapterRelease, rel.id).downloaded_version == 2


def test_png_and_jpg_downloads(md, library):
    set_settings(page_format="png")
    mid, _ = _one_chapter(md)
    (rel,) = releases(mid)
    assert all(p.endswith(".png") for p in list_page_files(library / rel.folder_rel_path))
    set_settings(page_format="jpg")
    mid2 = md.add_manga(title="Formats J")
    md.add_chapter(mid2, volume="1", chapter="1", groups=[GROUP_A], pages=2)
    add(md, mid2)
    scan(md, mid2)
    run_queue(md)
    (rel2,) = releases(mid2)
    assert all(p.endswith(".jpg") for p in list_page_files(library / rel2.folder_rel_path))


def test_webp_untouched_copy_replaced_by_revision(md, library):
    set_settings(page_format="webp_lossy", webp_quality=85)
    mid, ch = _one_chapter(md)
    (rel,) = releases(mid)
    folder = library / rel.folder_rel_path
    md.revise(ch["id"], pages=4)
    scan(md, mid)
    run_queue(md)
    assert len(list_page_files(folder)) == 4  # the untouched converted copy was recognised and replaced


# ------------------------------------------------------------------ chapter CBZ


def test_chapter_cbz_download(md, library):
    set_settings(chapter_cbz=True)
    mid, ch = _one_chapter(md, title="Start")
    (rel,) = releases(mid)
    assert rel.folder_rel_path.endswith("Volume 01 Chapter 01 - Start.cbz")
    cbz = library / rel.folder_rel_path
    assert cbz.is_file() and rel.local_status == LocalStatus.DOWNLOADED
    with zipfile.ZipFile(cbz) as zf:
        names = sorted(zf.namelist())
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
        info = zf.read("ComicInfo.xml").decode()
    pages = list_page_files(cbz)
    assert len(pages) == 3 and names == sorted([".mdm-release.json", "ComicInfo.xml", *pages])
    assert "<Number>1</Number>" in info and "<Series>Formats</Series>" in info
    assert read_release_marker(cbz)["chapter_id"] == ch["id"]
    staging = [p for p in library.parent.rglob("*") if ch["id"] in p.name]
    assert staging == []  # the staging folder and the packed CBZ are gone


def test_chapter_cbz_library_scan_and_relink(md, library):
    set_settings(chapter_cbz=True)
    mid, ch = _one_chapter(md)
    (rel,) = releases(mid)
    run_library_scan(uuid.UUID(mid), client=md.client, root=library)
    assert releases(mid)[0].local_status == LocalStatus.DOWNLOADED
    with session_scope() as s:  # database lost the link: the marker inside the CBZ finds it again
        r = s.get(ChapterRelease, rel.id)
        r.folder_rel_path, r.local_status = None, LocalStatus.MISSING
    run_library_scan(uuid.UUID(mid), client=md.client, root=library)
    (again,) = releases(mid)
    assert again.folder_rel_path == rel.folder_rel_path and again.local_status == LocalStatus.DOWNLOADED
    (library / rel.folder_rel_path).unlink()
    run_library_scan(uuid.UUID(mid), client=md.client, root=library)
    assert releases(mid)[0].local_status == LocalStatus.MISSING_ON_DISK


def test_chapter_cbz_revision_and_volume_rename(md, library):
    set_settings(chapter_cbz=True)
    mid, ch = _one_chapter(md, volume=None)
    (rel,) = releases(mid)
    assert rel.folder_rel_path.endswith("Volume TBD Chapter 001.cbz")
    md.revise(ch["id"], pages=5)
    scan(md, mid)
    run_queue(md)
    (rel,) = releases(mid)
    cbz = library / rel.folder_rel_path
    assert len(list_page_files(cbz)) == 5 and rel.local_status == LocalStatus.DOWNLOADED
    ch["attributes"]["volume"] = "2"
    ch["attributes"]["updatedAt"] = datetime.now(UTC).isoformat()
    scan(md, mid)
    (rel,) = releases(mid)
    assert rel.folder_rel_path.endswith("Volume 02 Chapter 01.cbz")
    assert not cbz.exists() and read_release_marker(library / rel.folder_rel_path)["volume"] == "2"


def test_markerless_cbz_is_ignored_by_library_scan(md, library):
    mid, _ = _one_chapter(md)
    (rel,) = releases(mid)
    other = (library / rel.folder_rel_path).parent / "Volume 09.cbz"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("Volume 09 Chapter 90/001.jpg", _jpeg())
    run_library_scan(uuid.UUID(mid), client=md.client, root=library)
    with session_scope() as s:
        assert s.scalars(select(ReviewItem)).all() == []


# ------------------------------------------------------------------ volume CBZ


def _series(md, chapters, **manga):
    mid = md.add_manga(title="Vols", **manga)
    out = [md.add_chapter(mid, volume=v, chapter=c, groups=g, pages=2) for v, c, g in chapters]
    add(md, mid)
    scan(md, mid)
    run_queue(md)
    return mid, out


def _plans(mid, library):
    with session_scope() as s:
        m = s.get(Manga, uuid.UUID(mid))
        return {p.volume: p for p in plan_volumes(s, library, m)}


def test_volume_states(md, library):
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("2", "3", [GROUP_A])])
    p = _plans(mid, library)
    assert p["1"].status == "ready" and [r.chapter for r in p["1"].chapters] == ["1", "2"]
    assert p["2"].status == "open"  # no later volume on MangaDex yet


def test_build_volume_packs_and_deletes_chapters(md, library):
    mid, chs = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("2", "3", [GROUP_A])])
    folders = {r.chapter: library / r.folder_rel_path for r in releases(mid)}
    with session_scope() as s:
        res = build_volume(s, uuid.UUID(mid), "en", "1", root=library)
    cbz = library / res["path"]
    assert res["path"] == "Vols/Volume 01.cbz" and cbz.is_file() and res["chapters"] == 2 and res["pages"] == 4
    with zipfile.ZipFile(cbz) as zf:
        names = sorted(zf.namelist())
    assert "ComicInfo.xml" in names
    assert sorted({n.split("/")[0] for n in names if "/" in n}) == ["Volume 01 Chapter 01", "Volume 01 Chapter 02"]
    assert not folders["1"].exists() and not folders["2"].exists() and folders["3"].exists()
    st = {r.chapter: r.local_status for r in releases(mid)}
    assert st == {"1": LocalStatus.MISSING_ON_DISK, "2": LocalStatus.MISSING_ON_DISK, "3": LocalStatus.DOWNLOADED}
    assert _plans(mid, library)["1"].status == "built"
    # Never downloaded again, the volume file is not a chapter for the library scan.
    run_library_scan(uuid.UUID(mid), client=md.client, root=library)
    assert scan(md, mid)["queued"] == 0
    with session_scope() as s:
        assert s.scalars(select(ReviewItem)).all() == []
        assert s.scalar(select(VolumeArchive)).release_ids == [chs[0]["id"], chs[1]["id"]]
    with session_scope() as s, pytest.raises(VolumeError):
        build_volume(s, uuid.UUID(mid), "en", "1", root=library)  # already built


def test_volume_from_chapter_cbz_and_edited_chapters(md, library):
    set_settings(chapter_cbz=True)
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("2", "3", [GROUP_A])])
    with session_scope() as s:
        res = build_volume(s, uuid.UUID(mid), "en", "1", root=library)
    with zipfile.ZipFile(library / res["path"]) as zf:
        names = zf.namelist()
    assert any(n.startswith("Volume 01 Chapter 01/") for n in names)
    assert not any(n.endswith(".cbz") or n.endswith(".mdm-release.json") for n in names)


def test_volume_gap_and_missing_chapter(md, library):
    mid, chs = _series(md, [("1", "1", [GROUP_A]), ("1", "3", [GROUP_A]), ("2", "4", [GROUP_A])])
    p = _plans(mid, library)["1"]
    assert p.status == "ready_with_warnings" and "no chapter 2" in p.warnings[0]
    with session_scope() as s, pytest.raises(VolumeError):
        build_volume(s, uuid.UUID(mid), "en", "1", root=library)
    with session_scope() as s:
        assert build_volume(s, uuid.UUID(mid), "en", "1", allow_warnings=True, root=library)["chapters"] == 2
    # A chapter moved out by hand makes the volume incomplete.
    (r4,) = [r for r in releases(mid) if r.chapter == "4"]
    md.add_chapter(mid, volume="3", chapter="5", groups=[GROUP_A], pages=2)
    import shutil
    shutil.rmtree(library / r4.folder_rel_path)
    run_library_scan(uuid.UUID(mid), client=md.client, root=library)
    p2 = _plans(mid, library)["2"]
    assert p2.status == "incomplete" and "moved out" in p2.problems[0]


def test_volume_duplicate_from_two_groups(md, library):
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("1", "2", [GROUP_B]),
                          ("2", "3", [GROUP_A])])
    p = _plans(mid, library)["1"]
    assert p.status == "ready_with_warnings"
    assert [r.group_names for r in p.chapters] == [["Team X"], ["Team X"]] and len(p.leftovers) == 1
    with session_scope() as s:
        res = build_volume(s, uuid.UUID(mid), "en", "1", allow_warnings=True, root=library)
    assert (library / res["kept_duplicates"][0]).exists()


def test_completed_title_last_volume_is_closed(md, library):
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A])], status="completed", lastVolume="1")
    assert _plans(mid, library)["1"].status == "ready"


def test_auto_volumes_only_builds_clean_ready_volumes(md, library):
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("2", "4", [GROUP_A]),
                          ("3", "5", [GROUP_A])])
    assert run_auto_volumes(uuid.UUID(mid))["skipped"] == "setting off"
    set_settings(volume_cbz_auto=True)
    res = run_auto_volumes(uuid.UUID(mid))
    assert [b["volume"] for b in res["built"]] == ["1"]  # 2 has a gap (chapter 3), 3 is the latest
    assert {p.volume: p.status for p in _plans(mid, library).values()} == \
        {"1": "built", "2": "ready_with_warnings", "3": "open"}


def test_volume_api(md, library, authed):  # noqa: F811
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("2", "2", [GROUP_A])])
    r = authed.get(f"/api/manga/{mid}/volumes")
    assert r.status_code == 200
    assert [(i["volume"], i["status"]) for i in r.json()["items"]] == [("1", "ready"), ("2", "open")]
    assert authed.post(f"/api/manga/{mid}/volumes/build", json={"language": "en", "volume": "1"}).status_code == 400
    ok = authed.post(f"/api/manga/{mid}/volumes/build", json={"language": "en", "volume": "1", "confirm": True})
    assert ok.status_code == 200 and ok.json()["queued"] is True
    chapters = authed.get(f"/api/manga/{mid}/chapters").json()["items"]
    assert all("in_volume_cbz" in c for c in chapters)


def test_volume_check_refuses_a_damaged_archive(md, library, monkeypatch):
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("2", "3", [GROUP_A])])
    folders = [library / r.folder_rel_path for r in releases(mid) if r.chapter in ("1", "2")]
    import app.services.volumes as vol

    real = vol.write_cbz

    def damaged(dest, members):  # one page altered on its way into the archive
        out = list(members)
        out[0] = (out[0][0], out[0][1][:-1] + b"x")
        return real(dest, out)

    monkeypatch.setattr(vol, "write_cbz", damaged)
    with session_scope() as s, pytest.raises(VolumeError, match="check failed"):
        build_volume(s, uuid.UUID(mid), "en", "1", root=library)
    assert all(f.exists() for f in folders)  # nothing deleted
    assert not (library / "Vols" / "Volume 01.cbz").exists()
    with session_scope() as s:
        assert s.scalars(select(VolumeArchive)).all() == []


def test_volume_keeps_chapter_folder_with_other_files(md, library):
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("2", "3", [GROUP_A])])
    (r1,) = [r for r in releases(mid) if r.chapter == "1"]
    (library / r1.folder_rel_path / "notes.txt").write_text("mine")
    with session_scope() as s:
        res = build_volume(s, uuid.UUID(mid), "en", "1", root=library)
    assert res["deleted"] == 1 and res["not_deleted"][0]["folder"] == r1.folder_rel_path
    assert (library / r1.folder_rel_path / "notes.txt").exists()
    st = {r.chapter: r.local_status for r in releases(mid)}
    assert st["1"] == LocalStatus.DOWNLOADED and st["2"] == LocalStatus.MISSING_ON_DISK


def test_volume_second_check_at_destination(md, library, monkeypatch):
    mid, _ = _series(md, [("1", "1", [GROUP_A]), ("1", "2", [GROUP_A]), ("2", "3", [GROUP_A])])
    folders = [library / r.folder_rel_path for r in releases(mid) if r.chapter in ("1", "2")]
    import app.services.volumes as vol

    real = vol.move_entry

    def move_and_damage(src, dst):  # the copy on the NAS goes wrong after a good staging copy
        real(src, dst)
        with zipfile.ZipFile(dst, "a") as zf:
            zf.writestr("Volume 01 Chapter 01/extra.jpg", _jpeg(9))

    monkeypatch.setattr(vol, "move_entry", move_and_damage)
    with session_scope() as s, pytest.raises(VolumeError, match="check failed"):
        build_volume(s, uuid.UUID(mid), "en", "1", root=library)
    assert all(f.exists() for f in folders) and not (library / "Vols" / "Volume 01.cbz").exists()
