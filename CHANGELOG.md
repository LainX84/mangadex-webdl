# Changelog

## 1.1.2 — 2026-11-19

### Changed

- **Statistics: new releases vs backlog.** Downloads are split into *new releases* (the chapter came out on MangaDex
  at most 7 days before it was downloaded, or after the title was added) and *backlog* (older chapters: a newly added
  title's back catalogue, a bulk import). The page shows new releases by default, so one big import no longer dwarfs
  every other day on the chart or skews averages and records; a *New releases / Backlog / All* switch changes the
  view, and *All* draws each bar in two colours.
- API: `GET /api/stats` accepts `kind=all|new|backlog` (default `all`); the summary and every timeline bucket carry
  `new_chapters` and `backlog_chapters`.

## 1.1.0 — 2026-11-17

### Added

- **Page format** (*Settings → Output format → Page images*): keep pages as uploaded (default) or convert every page
  to **PNG**, **JPEG** (`jpeg_quality`, default 92), **WebP lossy** (`webp_quality`, default 90) or **WebP
  lossless**, or use **MIXED**, which saves space without losing a pixel (JPEG/WebP kept, PNG/GIF/BMP pages stored as
  the smallest of original, optimised PNG and lossless WebP). Lossless conversions are verified pixel for pixel;
  pages a format cannot hold fall back to the closest format; any conversion problem keeps the original page.
- **One CBZ per chapter** (`chapter_cbz`): `Volume 03 Chapter 12 - Title.cbz` with the pages, `ComicInfo.xml` and the
  app's marker; library scans, revisions, layout moves and volume renames work with CBZ files as with folders.
- **One CBZ per complete volume**: `Volume 03.cbz` in the manga folder, from a button under each volume on the manga
  page or automatically (`volume_cbz_auto`) for volumes that are closed, complete and without numbering gaps. The
  archive is checked twice (in staging and at its final place: CRCs, member list, SHA-256 and full decoding of every
  page) before the packed chapter folders are deleted, and a chapter that changed or holds other files is kept.
- API: `GET /api/manga/{id}/volumes`, `POST /api/manga/{id}/volumes/build`; chapter list items carry `in_volume_cbz`.
- Database migration `0011` (`volume_archives`).

### Changed

- Docker images are pushed only for version tags (`:1.1.0` and `:latest`); pushes to `main` build without publishing.

All new options are off by default: upgrading changes nothing until you enable them.

## 1.0.0 — 2026-11-17

First public release.
