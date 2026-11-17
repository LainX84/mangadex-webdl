# MangaDex WebDL

A self-hosted download manager for [MangaDex](https://mangadex.org). It follows manga, monitors them for new
chapters, and downloads each chapter as **individual page images** (no CBZ/ZIP/PDF) into a NAS-backed library:

```
{library}/{Manga Title}/[{Language}/]{Chapter Folder}/001.jpg
{library}/Some Manga/Volume 03 Chapter 12 - A New Challenge - Team X/001.jpg
```

The `{Language}` level only exists when it is needed, and the scanlation group is part of the chapter folder name
when the same chapter exists from more than one group (see [Folder layout](#folder-layout)). The library only ever
holds **complete chapters**: downloads in progress live in a separate staging folder and previous copies of
re-downloaded chapters in a separate replaced folder (see [Staging and replaced folders](#staging-and-replaced-folders)).

It runs entirely in Docker, keeps working when every browser is closed, resumes interrupted downloads, and
treats MangaDex's rate limits as a hard constraint.

<p align="center">
  <img src="https://raw.githubusercontent.com/lainx84/mangadex-webdl/main/docs/screenshots/home.png" alt="MangaDex WebDL — dashboard" width="900">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/lainx84/mangadex-webdl/main/docs/screenshots/library.png" alt="Library" width="440">
  &nbsp;
  <img src="https://raw.githubusercontent.com/lainx84/mangadex-webdl/main/docs/screenshots/statistics.png" alt="Statistics" width="440">
</p>

> **Not affiliated with MangaDex.** MangaDex WebDL is an independent, non-commercial project that uses only the
> official public MangaDex API. It contains and distributes no manga, scans or images: everything is downloaded by
> each user, for personal use, under their own responsibility. Please read [Acknowledgements](#acknowledgements) and
> [Disclaimer and legal notice](#disclaimer-and-legal-notice) before using it. Licensed under the
> [GNU AGPL-3.0](#license).

---

## Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Requirements](#requirements)
4. [Quick start](#quick-start)
5. [Host preparation: NFS mount](#host-preparation-nfs-mount)
6. [Configuration](#configuration)
7. [First run](#first-run)
8. [Reverse proxy (Nginx Proxy Manager)](#reverse-proxy-nginx-proxy-manager)
9. [Using the app](#using-the-app)
10. [Statistics](#statistics)
11. [MangaDex account: followed list & fast updates](#mangadex-account-followed-list--fast-updates)
12. [Comics archive: continue where your collection ends](#comics-archive-continue-where-your-collection-ends)
13. [Folder layout](#folder-layout)
14. [Staging and replaced folders](#staging-and-replaced-folders)
15. [Chapter folder names (ready for volumes)](#chapter-folder-names-ready-for-volumes)
16. [Per-volume chapter numbering (anthologies)](#per-volume-chapter-numbering-anthologies)
17. [Late chapters, re-uploads and new versions](#late-chapters-re-uploads-and-new-versions)
18. [Rate limiting: speed vs. politeness](#rate-limiting-speed-vs-politeness)
19. [How downloads, recovery and imports work](#how-downloads-recovery-and-imports-work)
20. [Security model](#security-model)
21. [Backup, restore, upgrade](#backup-restore-upgrade)
22. [Administration (CLI)](#administration-cli)
23. [Development and tests](#development-and-tests)
24. [Building and publishing the image](#building-and-publishing-the-image)
25. [Known limitations](#known-limitations)
26. [Acknowledgements](#acknowledgements)
27. [Disclaimer and legal notice](#disclaimer-and-legal-notice)
28. [License](#license)

---

## Features

- Add manga by **MangaDex URL, UUID or title search** (official API only, no HTML scraping).
- Per-manga **languages** (multi-select) and **scanlation groups** (all / selected / multiple, plus "Unknown Group").
- Releases are tracked by MangaDex **chapter UUID**; the same chapter number from different groups or
  languages is kept as separate releases and separate folders.
- **Chapter folders ready for volumes**: every name starts with the volume (`Volume TBD` until MangaDex assigns
  one, then renamed automatically) and ends with the scanlation group when needed, so chapters of several groups —
  and the same chapter from two groups — sit side by side in reading order.
  See [Chapter folder names](#chapter-folder-names-ready-for-volumes).
- **Only complete chapters in the library**: pages are downloaded into a separate staging folder and the chapter
  appears in the library in one atomic move when it is complete; replaced copies (redownloads, and revisions of
  chapters with manual edits) are moved out of the library too, while a group's revision of an untouched chapter
  simply replaces it. See [Staging and replaced folders](#staging-and-replaced-folders).
- **Adaptive folder layout**: no `{Language}` folder when only one language is selected. When a second language
  appears, the existing chapters are moved into the new structure automatically before the new chapter is downloaded.
- **Per-volume chapter numbering**: titles whose chapters restart at 1 in every volume (anthologies) are
  detected automatically and identified by volume + chapter.
  See [Per-volume chapter numbering](#per-volume-chapter-numbering-anthologies).
- **Late chapters and re-uploads**: chapter numbers that appear after you set the start chapter (a late `12.5`)
  are downloaded even below the start; a re-upload by the same group replaces the old copy while it is still in the
  library; a new MangaDex version of a chapter is checked and re-fetched only if its pages changed.
  See [Late chapters, re-uploads and new versions](#late-chapters-re-uploads-and-new-versions).
- **Monitoring** every 6 h by default (configurable globally and per manga), spread over time, incremental
  (`updatedAtSince`) with a daily full scan. Monitoring and automatic downloading are independent switches.
- **Optional MangaDex account** (personal API client): one request every 15 min checks the followed feed of
  *every* followed manga, so new chapters are queued minutes after release; titles followed on MangaDex but not
  in the app land in **Approvals** and are never scanned or downloaded until approved.
  See [MangaDex account](#mangadex-account-followed-list--fast-updates).
- **Comics archive (optional, read-only)**: when you add or approve a title, the app finds it in your existing CBZ
  collection, reads the newest chapter you own and offers *Continue from Ch. N*.
  See [Comics archive](#comics-archive-continue-where-your-collection-ends).
- **Statistics page**: live, interactive statistics of everything the service has done — chapters, pages, data,
  series, volumes, arrival time, activity over time, top series, genres, groups — with many filters.
  See [Statistics](#statistics).
- **Persistent download state machine** (pending → queued → downloading → completed, plus paused, retry
  scheduled, failed, needs attention, cancelled). Page-level progress is stored, so a restart only fetches
  missing pages.
- **Central rate limiter** shared by all containers (token buckets, minimum intervals, per-host and per-page
  concurrency, global cooldown on HTTP 429 honouring `Retry-After`, hard stop on HTTP 403).
- **NAS safety**: downloads only start when the library sentinel file is present, the filesystem type matches
  (`nfs`/`nfs4` by default) and a write probe succeeds. An unmounted NFS mountpoint never receives files.
- **Library import & reconciliation**: existing folders are recognised (by marker file or by
  language/group/chapter name, in any layout); anything ambiguous goes to a **Review** screen instead of being guessed.
- **Manual edits are respected**: removing ad pages or files from a chapter folder is detected as
  "Edited locally" and never undone; an edited copy is never deleted, not even when the group revises the chapter.
- **Mandatory authentication** (Argon2id, server-side sessions, HttpOnly cookies, CSRF tokens, login throttling).
- **Mobile-first UI** (bottom navigation on phones, sidebar on desktop, 44 px touch targets).

## Architecture

```
            ┌──────────── reverse proxy (HTTPS, optional) ────────┐
            │                                                      │
   phone / browser ──► app (FastAPI + React SPA) :8000 ──┐         │
                                                          │         │
   scheduler ── every 10 s: mount check, recover orphans, │         │
               dispatch jobs, start ≤1 due scan,          │         │
               every 30 s: queue due account syncs ───────┤         │
                                                          ▼         │
   worker (4 download + 1 scan + 1 follow proc, RQ) ◄── Redis (queue, rate-limit state, account token)
        │                                                 │
        ├──► MangaDex API / MD@Home nodes (through the shared rate limiter)
        └──► library + staging + replaced folders (one bind mount of the host's NFS share)
                                                          │
                     PostgreSQL (source of truth: manga, releases, jobs, pages, history, settings)
```

| Container | Purpose |
|---|---|
| `mangadex-webdl` | Web UI and REST API |
| `mangadex-webdl-worker` | Executes downloads, scans and account syncs (RQ `SimpleWorker` processes, auto-restarted) |
| `mangadex-webdl-scheduler` | Dispatching, retries, monitoring schedule, account sync schedule, crash recovery, mount monitoring |
| `mangadex-webdl-db` | PostgreSQL 17 (internal network only) |
| `mangadex-webdl-redis` | Redis (queue transport + shared rate-limit state; internal only, AOF persisted) |
| `migrate` | One-shot: runs Alembic migrations and seeds languages, then exits |

One image is used for `app`, `worker`, `scheduler` and `migrate` (different commands). PostgreSQL is the source of
truth for every job; Redis only wakes workers up, and if Redis loses its data the scheduler re-dispatches from the
database.

Stack: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, RQ, httpx, Pillow (validation only, never re-encoding),
React 19 + TypeScript + Vite, TanStack Query.

## Requirements

- Linux host with Docker Engine and Docker Compose v2 (`linux/amd64` or `linux/arm64`).
- The NAS share mounted on the **host** (NFS recommended). The containers never mount NFS themselves.
  A local disk works too (set `MDM_EXPECTED_FS_TYPES=` empty).
- Optionally an existing CBZ collection, mounted read-only (see [Comics archive](#comics-archive-continue-where-your-collection-ends)).
- Outbound HTTPS to `api.mangadex.org`, `uploads.mangadex.org`, MangaDex@Home nodes and `api.mangadex.network`
  (plus `auth.mangadex.org` when the MangaDex account is used).

## Quick start

The image is published on the GitHub Container Registry and Docker Hub:

```
ghcr.io/lainx84/mangadex-webdl:latest
docker.io/lainx84/mangadex-webdl:latest
```

```sh
git clone https://github.com/lainx84/mangadex-webdl.git && cd mangadex-webdl
cp .env.example .env                     # set MDM_LIBRARY_PATH, PUID/PGID, port, proxy settings…
sudo ./scripts/init-secrets.sh           # secrets/: random DB password + empty MangaDex account files
mkdir -p appdata/data appdata/postgres appdata/redis && sudo chown 1000:1000 appdata/data   # your PUID:PGID
mkdir -p /mnt/nas/manga/MangaDex-WebDL   # your MDM_LIBRARY_PATH (on the mounted share)
docker compose pull
docker compose up -d
docker compose ps
```

Then follow [First run](#first-run). Without cloning the repository, `compose.yml`, `.env.example` and
`scripts/init-secrets.sh` are all you need.

To build the image yourself instead of pulling it:

```sh
docker compose -f compose.yml -f compose.build.yml up -d --build
```

## Host preparation: NFS mount

Mount the share with systemd-aware options in `/etc/fstab`, e.g.:

```fstab
nas.local:/volume1/Download  /mnt/nas/manga  nfs  _netdev,noatime,nfsvers=3,tcp,hard,x-systemd.automount,x-systemd.mount-timeout=30  0 0
```

- `_netdev` / `x-systemd.automount` make the mount wait for the network and re-mount on access.
- **Do not** make `docker.service` hard-depend on the mount (`RequiresMountsFor=`): the database must start even
  when the NAS is down. If you want ordering without a hard dependency:

  ```ini
  # /etc/systemd/system/docker.service.d/nas.conf
  [Unit]
  Wants=remote-fs.target
  After=remote-fs.target
  ```
  then `systemctl daemon-reload`.

- Bind-mount **one** dedicated folder of the share that holds only the library and its two working folders (e.g.
  `MangaDex-WebDL/` with `MangaDex`, `Incoming` and `Replaced` inside, mounted at `/mangadex`). With a single bind
  mount a finished chapter moves into the library with an atomic rename, and the rest of the share stays invisible
  to the containers; two separate bind mounts would force a copy
  (see [Staging and replaced folders](#staging-and-replaced-folders)).
- The mounted directory must exist on the host. Compose uses `create_host_path: false`, so Docker refuses to start
  instead of silently creating a local dir.
- The bind mount uses `propagation: rslave`, so if the share is mounted *after* the containers started, they
  see it without a restart. If the library still shows "unavailable" after the NAS comes back,
  `docker compose restart worker scheduler app` re-binds it.

**Permissions.** The app runs as `PUID:PGID` (default `1000:1000`). That user must be able to create
directories and files in the library folder and next to it. With NFS, the NAS decides: check the export's squash
mapping (e.g. Synology "Map all users to admin" / `all_squash,anonuid=…`) and verify from the host:

```sh
sudo -u '#1000' touch /mnt/nas/manga/MangaDex-WebDL/.write-test && rm /mnt/nas/manga/MangaDex-WebDL/.write-test
```

## Configuration

Everything is in `.env` (see `.env.example` for all settings and comments):

| Variable | Meaning |
|---|---|
| `MDM_IMAGE` | Image to run (`ghcr.io/…` or Docker Hub; pin a version such as `:1.0.0` if you prefer) |
| `MDM_LIBRARY_PATH` | Host folder bind-mounted at `/mangadex` (a dedicated NAS folder holding only the library and its working folders) |
| `MDM_DOWNLOADS_ROOT` | Library path inside the container (default in the example: `/mangadex/MangaDex`) |
| `MDM_STAGING_ROOT` | Downloads in progress (`/mangadex/Incoming`); empty = `{library}/.mdm-staging` |
| `MDM_REPLACED_ROOT` | Previous copies of re-downloaded chapters (`/mangadex/Replaced`); empty = `{library}/.mdm-replaced` |
| `MDM_SENTINEL_DIR` | Folder holding the library sentinel (`/mangadex`, the folder above the library); empty = the library root |
| `MDM_ARCHIVE_PATH` | Optional host folder of an existing CBZ collection, mounted read-only at `/archive` |
| `MDM_APPDATA_PATH` | Host path for PostgreSQL, Redis and the cover cache — keep it **off** the NAS |
| `MDM_SECRETS_PATH` | Host folder with the Docker secrets (default `./secrets`) |
| `PUID` / `PGID` | User the app runs as; must be able to write to the share |
| `TZ` | Time zone (weekly library check, logs) |
| `MDM_BIND_ADDRESS` / `MDM_PORT` | Published web port (bind to a LAN IP if possible) |
| `MDM_PUBLIC_URL` | External HTTPS URL behind the proxy |
| `MDM_TRUSTED_PROXIES` | IPs/CIDRs whose `X-Forwarded-*` headers are trusted; nobody else's are |
| `MDM_EXPECTED_FS_TYPES` | Accepted filesystem types for the library and its working folders (`nfs,nfs4`; empty disables) |

**Secrets** (one value per file in `MDM_SECRETS_PATH`, readable by `PUID:PGID`): `db_password` (required) and
`md_client_id`, `md_client_secret`, `md_username`, `md_password` (optional, empty files = account features off).
`scripts/init-secrets.sh` creates all of them.

PostgreSQL and Redis publish no ports and sit on an `internal: true` network.

**Komodo / Portainer**: create a stack from this repository (or paste `compose.yml`), put the `.env` values into
the stack's environment, and keep the secrets on the host at `MDM_SECRETS_PATH`.

## First run

1. Initialise the library **while the NAS is mounted**. Either from the UI (*Settings → System →
   Initialise library*) or:
   ```sh
   docker exec -it mangadex-webdl mdm init-library
   ```
   This creates the sentinel `.mdm-library`: in `MDM_SENTINEL_DIR` when set (recommended: the folder above the
   library, as in `.env.example`), otherwise in the library root. Downloads never start without it; an unmounted
   NFS mountpoint is an empty local directory and therefore lacks the file. The library, staging and replaced
   folders are created automatically on first use (only after this check has passed).
   Keeping the sentinel out of the library folder means you can move the whole content of the library by hand
   (file managers that show hidden files would select it too) without pausing downloads. The sentinel folder must
   be on the same filesystem as the library (checked every time).
2. Create the administrator. Open the UI and enter the **one-time setup token** printed in the logs:
   ```sh
   docker logs mangadex-webdl 2>&1 | grep -A3 "FIRST-RUN SETUP"
   ```
   The token changes on every restart until setup is completed; there is no default password and no public
   registration. (Alternatively set `MDM_ADMIN_USERNAME` + `MDM_ADMIN_PASSWORD_FILE` pointing at a Docker
   secret, or run `docker exec -it mangadex-webdl mdm create-admin <name>`.)

## Reverse proxy (Nginx Proxy Manager)

Create a **Proxy Host**:

| Field | Value |
|---|---|
| Domain Names | `manga.example.com` |
| Scheme | `http` |
| Forward Hostname / IP | the Docker host IP — or `mangadex-webdl` if NPM shares a Docker network with the app (then use port `8000`) |
| Forward Port | `8098` (your `MDM_PORT`) |
| Block Common Exploits | on |
| Websockets Support | not needed |
| SSL | request a Let's Encrypt certificate, **Force SSL** on, **HTTP/2** on, **HSTS** on |

NPM sends `X-Forwarded-For` and `X-Forwarded-Proto` by default. Then in `.env`:

```env
MDM_PUBLIC_URL=https://manga.example.com
MDM_TRUSTED_PROXIES=172.16.0.0/12   # the Docker networks NPM connects from; or NPM's exact IP
MDM_COOKIE_SECURE=auto               # "true" if you never use plain-HTTP LAN access
```

With `auto`, session cookies get the `Secure` flag whenever the request arrived over HTTPS through a trusted
proxy. Direct LAN access over `http://host:8098` still works (and still requires login). Any other reverse proxy
works the same way.

Recommended *Advanced* snippet (optional, limits upload size and hides the app from crawlers):

```nginx
client_max_body_size 1m;
add_header X-Robots-Tag "noindex, nofollow" always;
```

## Using the app

- **Add**: search a title or paste a URL/UUID → choose languages → *All groups* or *Only selected groups*
  (groups are loaded from MangaDex for the chosen languages) → monitoring / auto-download → *Add*.
  The first scan runs immediately; with auto-download on, missing releases are queued.
- **Approvals** (with a MangaDex account): titles you follow on MangaDex that are not in the app yet.
  *Configure & approve* opens the add form pre-filled (languages, groups, chapter list with *download from
  chapter*, title/folder); *Dismiss* removes the title and never proposes it again (it can be restored from the
  *Dismissed* list). Filter by MangaDex reading status (Reading, Plan to read, …).
- **Import an existing folder**: *Settings → Review* lists unknown top-level folders after a library scan
  (*Library → Scan library*); *Find on MangaDex & import* adds the manga using that folder. Existing chapters
  are linked and only missing releases are downloaded.
- **Manga page**: scan now, download missing, check folder, toggle monitoring / auto-download, per-release
  download / skip / retry / redownload (redownload asks for confirmation and moves the old folder to the
  [replaced folder](#staging-and-replaced-folders)), language/group filters, interval, title override, history.
  For titles with [per-volume numbering](#per-volume-chapter-numbering-anthologies) the start point is a
  **volume + chapter**.
- **Review**: ambiguous library folders to link, and newer revisions of chapters you had already moved out (saved in
  the replaced folder, see [re-uploads](#late-chapters-re-uploads-and-new-versions)); *Seen* closes those.
- **Queue**: active / pending / paused / failed / completed tabs, retry schedule and errors, pause / resume /
  retry / cancel per job, pause or resume the whole queue.
- **Statistics** (sidebar on desktop, *Statistics* button on the dashboard on phones). See [Statistics](#statistics).
- **Settings**: rates, concurrency, retries, naming padding, chapter folders (group in the name, volume
  placeholder, rename on volume change), content ratings, language folder codes (`ENG`, `ITA`, `JPN`, `SPA`, … —
  editable), MangaDex account status and sync options, comics archive, system status (library, downloads in
  progress, replaced copies), password.

## Statistics

`/stats` shows what the service has done since it started (the first title added, job event or scan), computed
on the fly from PostgreSQL: no extra tables, no background job. The page refreshes itself every 30 s (*Live*, can
be paused) and makes no MangaDex requests.

**Period and grouping.** Presets *7D / 30D / 90D / 1Y / All* or any two dates; bars grouped by day, week or month
(*Auto*: day up to ~2 months, week up to a year, month beyond). Dates are local days in the browser's time zone.
With a preset other than *All*, the headline numbers are compared with the previous period of the same length.

**Filters** (combinable; every active filter is a removable chip; the whole state is in the URL, so a view can be
bookmarked): series, genres/themes/tags (*Any tag* / *All tags*), type (Manga / Manhwa / Manhua, from the original
language), demographic, content rating, publication status, scanlation group, chapter language. Almost everything on
the page is also a filter: clicking a genre bar, a group, a language, a donut slice or a series in the top list
toggles that filter; clicking a bar of the activity chart narrows the period to that day/week/month.

**What is shown**

| Panel | Content |
|---|---|
| Headline | Chapters, pages, data downloaded, series with downloads (+ series added), volumes touched, median **arrival time** |
| Activity | Chapters, pages, data, series added, per-title scans or API requests per bucket; running total of chapters |
| Right now | Series followed, chapters / pages / size / volumes in the library, chapters moved out, owned elsewhere, skipped, monitored / auto-download, waiting for approval, queue, monitoring levels |
| When chapters arrive | Weekday × hour heatmap, busiest day, active days, streaks, download speed, time spent downloading, biggest chapter |
| Top series | Top 25 by chapters, pages or data, with cover, level, last download and a link to the manga page |
| Genres & tags | Genres, themes, format and content tags by chapters downloaded or by number of series |
| Scanlation groups · Languages | Chapters and series per group (top 25), chapters and pages per language |
| Collection mix | Type, demographic, content rating, publication status — by series or by chapters |
| How chapters were found | Per-title scans, MangaDex followed feed, manual, manual redownload; redownloads, re-uploads, version refreshes |
| Monitoring & reliability | Scans, failed scans, API requests made by scans, releases discovered, retries, crash recoveries, cancellations |

**How things are counted**

- A *download* is a completed download job, so redownloads, same-group re-uploads and version refreshes count;
  version checks that found the same pages do not. Imported folders and chapters owned elsewhere are not downloads.
- *Volumes* are distinct volumes (per series and language) with at least one chapter downloaded in the period,
  not complete volumes. Chapters in `Volume TBD` are counted separately.
- *Arrival time*: median time between the chapter becoming readable on MangaDex and the end of its download, only
  for chapters found by scans or the followed feed and published after the title was added.
- *Moved out*: chapters whose folder is no longer in the library (*Folder deleted*), e.g. already packed into CBZ.

**API** (session required, read-only): `GET /api/stats/options` and
`GET /api/stats?from=YYYY-MM-DD&to=YYYY-MM-DD&tz=Europe/London&bucket=auto|day|week|month` plus the repeatable
filters `manga`, `tag` (+ `tag_match=any|all`), `type`, `rating`, `status`, `demographic`, `lang`, `group`.

## MangaDex account: followed list & fast updates

Optional. MangaDex *personal API clients* use the OAuth2 password grant: client id + secret plus the account's
username and password are exchanged at `auth.mangadex.org` for a ~15-minute access token (renewed with the
refresh token; one shared login for all containers, kept in Redis). Only read-only endpoints are used:
`/user/me`, `/user/follows/manga`, `/manga/status`, `/user/follows/manga/feed`. Nothing is written to the
account.

**Setup**: create the client at *mangadex.org → Settings → API Clients*, then put one value per file in the
secrets `md_client_id`, `md_client_secret`, `md_username`, `md_password` and recreate the containers
(`docker compose up -d --force-recreate app worker scheduler`). Empty or missing files simply leave the feature
off. *Settings → MangaDex account* shows which secrets are present (never their values) and has *Test login* /
*Sync now*.

| Job | Default | What it does |
|---|---|---|
| Followed feed | every 15 min | One request returns chapters updated since the last check (10 min overlap) for **all** followed manga. Releases of approved, monitored titles in their selected languages are stored and, with auto-download on, queued with the usual group / start-chapter / exclusion rules. First run looks back 2 days; older chapters are left to the regular scans. |
| Followed list | every 6 h | Mirrors the followed list and reading statuses. Followed titles not in the app are added **waiting for approval** (monitoring and auto-download off, no scans, no downloads) with the languages from *Languages preselected when approving*. |

- The per-manga scans are unchanged: they still cover titles not followed on MangaDex and act as a safety net.
- Removing a title from the library, or dismissing it in Approvals, records it so the followed list never adds
  it back.
- Account errors never stop anonymous traffic: an HTTP 401/403 from the token endpoint or `/user/*` marks the
  account *login failed* (retried at most hourly) instead of triggering the global MangaDex stop.
- Account syncs run on their own worker process (`follows` queue) and share the global rate limiter.

## Comics archive: continue where your collection ends

Optional. Mount an existing CBZ collection **read-only** at `/archive` (`MDM_ARCHIVE_PATH` in `.env`); the app
never writes there. It is only used to suggest the chapter to start from. An empty or missing folder turns the
feature off.

- **Which folders**: *Settings → Comics archive*, one folder per line relative to the archive root. The default
  `.` searches the whole archive (down to six levels). Narrow it to the folders that hold scanlations, e.g.
  `Manga/English/Scanlations`. In the *"- Custom" volumes only* list (empty by default — e.g. folders of official
  releases) only volumes named `… - Custom.cbz` (built by hand from MangaDex chapters) are looked at.
  The language comes from a folder name in the path (`English`/`Inglese` → EN, `Italian`/`Italiano` → IT, also
  Japanese, Spanish, French, German, Portuguese, Chinese, Korean).
- **Index** (daily, *Rescan archive now*, or `mdm archive index`): every folder that directly holds `.cbz`/`.zip`
  files is a title folder (status levels such as `In Progress` / `Completed` are walked through); sub-folders of a
  title (`Extra 1`, `Previous version`) are ignored. Only names are read.
- **Matching**: a folder matches when its name (without `[END]`) equals any MangaDex title of the manga — main,
  alternative, romaji, English, … — compared case-, width- and punctuation-insensitively. Folders in the
  selected languages come first; others are listed as alternatives.
- **Newest chapter**:
  - single chapters from the file name: `c046`, `Chapter 442 - …`, `Vol.08 Ch.110`, `Ch.143.5`;
  - volumes (`v07`, `Volume 6`, `… 23`, `… 34 - Custom`, `第03巻`): only the zip *central directory* of the newest
    volume is read (a few KB) — chapter folders (`Volume 1 Chapter 05/`) or page names (`… - c043 (v08) - p001 …`);
  - a volume whose pages are only numbered (`001.jpg`) is mapped through MangaDex: the last chapter MangaDex lists in
    that volume; without volume data on MangaDex it is shown as "up to volume N".
  The result is cached per folder until its file list changes.
- **In the add/approve form** (and in a title's settings): *Your archive* shows the folder, the newest chapter and the
  file it came from, and offers **Continue from Ch. N** or, when you already have everything, **New chapters only**.
  Nothing is applied automatically (unless *auto-approve titles found in the archive* is switched on in Settings).

## Folder layout

Each manga has its own layout, stored in the database (`manga.folder_layout`). With *Scanlation group in the
chapter folder name* on (the default, see [Chapter folder names](#chapter-folder-names-ready-for-volumes)) only the
language level is ever created:

| Situation | Path |
|---|---|
| one language | `{Manga}/{Chapter}` |
| several languages | `{Manga}/{Language}/{Chapter}` |

With the setting off, a `{Group}` level is added inside a language as soon as it has chapters from more than one
group (`{Manga}/[{Language}/]{Group}/{Chapter}`).

- **Languages** counted: the languages selected for the manga, plus any language already in the library or
  in the queue.
- **Growing the layout**: before a download starts, the worker checks whether the chapter needs a level that
  does not exist yet. If so, every chapter folder of the manga already in the library is moved into the new
  structure first, stored paths are updated, emptied folders are removed, and a *reorganised* event is written to
  the manga history. This runs under an exclusive per-manga folder lock and never while another chapter of the same
  manga is being written. If a destination is taken the moves are rolled back and the job needs attention.
- **Levels are never removed automatically** while chapters of the manga are in the library.
  `mdm folder-layout --apply` recomputes the minimal layout for existing titles; `mdm chapter-names --apply` does the
  same and also renames the chapter folders.
- Only folders linked to a release are moved; unknown folders are left where they are. Nothing is deleted
  except directories that became empty.

## Staging and replaced folders

The library holds complete chapters only, so it can be reorganised by hand at any time (chapters cut out to be
packed into volumes, ad pages removed) without leaving half a chapter or a stray page behind.

| Folder | Setting | Default | Content |
|---|---|---|---|
| Library | `MDM_DOWNLOADS_ROOT` | `/downloads` | Complete chapters |
| Downloads in progress | `MDM_STAGING_ROOT` | `{library}/.mdm-staging` | One flat folder per chapter being downloaded, named after the chapter UUID |
| Replaced copies | `MDM_REPLACED_ROOT` | `{library}/.mdm-replaced` | Previous copies from manual redownloads, and from re-uploads / changed versions **when the old copy had manual edits**; revisions of chapters you already moved out (within the *revision window*); all as `{Manga}/[{Language}/]{Chapter} {timestamp}` |

- **How a chapter arrives**: pages are written into `{staging}/{chapter uuid}/`; when every page is there and
  verified, the marker is written and the folder is moved to its final place in the library. With staging and
  library on the **same mount** this is a single atomic `rename`. On different filesystems the chapter is copied
  into a hidden `.mdm-incoming-*` folder next to its destination, synced, renamed into place and only then removed
  from staging.
- **Safety**: the staging and replaced folders are only created (never their parent folders) after the library check
  has passed; just before a chapter is moved in, the library is checked again.
- **A group's revision replaces an untouched copy**: the old copy still in the library is **deleted** if it is
  exactly as downloaded (every page in its `.mdm-release.json` present with the recorded size and SHA-256, nothing
  else except the marker and `Thumbs.db` / `desktop.ini` / `.DS_Store`). Any manual edit keeps the old copy in the
  replaced folder instead.
- **Manual redownloads** always keep the previous copy in the replaced folder. Nothing in the replaced folder is
  ever deleted automatically.
- **Leftovers**: a cancelled download keeps its staging folder (its pages are reused if it is queued again);
  `mdm staging cleanup --apply` removes the staging folders without an open job. `mdm staging` lists them.

## Chapter folder names (ready for volumes)

Chapter folders are named so that grouping them into volumes (and then into CBZ files, done by hand after removing
ad / recruitment pages) is a matter of selecting a prefix:

```
Volume 02 Chapter 10 - The Festival
Volume 02 Chapter 11 - Fireworks
Volume 03 Chapter 12 - A New Challenge - Team X           ← same chapter from two groups: the group is added
Volume 03 Chapter 12 - A New Challenge - Other Scans      ← to both, kept side by side
Volume 03 Chapter 13                                      ← gap filled by another group: no group needed
Volume TBD Chapter 14                                     ← no volume on MangaDex yet
```

- **Volume first, always**: chapters without a volume get the placeholder (*Settings → Chapter folders*, default
  `TBD`; empty gives `Chapter 014`). The placeholder sorts after every numbered volume. Oneshots without a number keep
  `Oneshot - …`.
- **Group last, only for duplicates**: ` - {Group}` (several groups joined with ` & `, `Unknown Group` when
  none) is added only when the same chapter is in the library or queued from more than one group. When the second
  copy is about to be downloaded, the folder already on disk is renamed first.
- **Rename when MangaDex assigns the volume** (on by default): after every scan and feed sync, chapter folders still
  in the library whose volume differs from MangaDex (`Volume TBD Chapter 14` → `Volume 04 Chapter 14`) are renamed,
  together with their marker. Folders with names the app cannot parse are never touched.
- **Metadata in the marker**: `.mdm-release.json` holds manga title, language, volume, chapter, title, scanlation
  group, chapter URL and publish date, for scripts that build volumes.
- **Existing folders**: `mdm chapter-names` (dry run) shows how existing folders would be renamed to the current
  rules; `--apply` does it, `--keep-groups` leaves `{Group}` levels in place.
- Chapter digits: with padding 2, a volume holding chapters above 99 sorts wrong alphabetically; set *Chapter
  digits* to 3 for such series.

## Per-volume chapter numbering (anthologies)

Most titles number chapters across the whole series, so every "chapter 2" — whatever the group or language — is
the same *logical chapter* (key `c:2`). Some titles (anthologies above all) restart at chapter 1 in every volume;
with series keys, owning any "chapter 2" would hide every other one.

- **Detection** (`manga.chapters_per_volume`): after every scan and feed sync, a title switches to per-volume
  numbering when **chapter 1 appears in at least two different volumes of the same language**. It never switches
  back automatically.
- **Keys and order**: chapters become `v15:c:2`; releases without a volume keep `c:2`. Chapters are ordered — and
  compared with the start chapter — as **volume × 1000 + chapter**.
- **When a title switches**: every logical chapter is re-keyed; the start chapter is converted to the new scale.
  Files and folders are never touched.
- **Manual control**: `mdm chapter-numbering` (dry run) lists the titles that would switch; `--apply` saves it;
  `--manga UUID --set volume|series --apply` forces one title either way.

## Late chapters, re-uploads and new versions

**Late chapters below the start chapter.** Each title remembers when its start chapter was set.

| Release numbered below the start | Downloaded? |
|---|---|
| Chapter number that first appears on MangaDex, in that language, after the start was set (a late `12.5` extra) | yes |
| New upload of a number that already existed (another group, a re-upload of a chapter MangaDex removed) | no |
| Released before the start was set | no |
| Without a number (one-shots, extras) | yes |

**Same-group re-upload (new chapter UUID).** When the group of a chapter you have uploads it again, the new release is
downloaded regardless of the start chapter and takes the old one's folder (old copy deleted when untouched, moved to
the replaced folder when edited). **Only a copy still in the library is replaced.** When you have already moved the
chapter out, a re-upload within the *revision window* (*Settings → Revisions of chapters you moved out*, default
**15 days** from the download, 0 = never) is downloaded into the replaced folder with a **Review item** and a
dashboard banner; later ones are skipped.

**Same chapter UUID, newer MangaDex version.** When MangaDex reports a higher `version`, a *version check* job compares
the page list with the downloaded pages: same pages → nothing downloaded; different pages → downloaded again with the
same rules as re-uploads.

## Rate limiting: speed vs. politeness

MangaDex documents a global limit of roughly 5 requests/second per IP and 40 `/at-home/server` lookups per
minute. The defaults stay well below that:

| Setting | Default |
|---|---|
| API requests/s · min interval | 1/s · 500 ms |
| Image requests/s · min interval | 2/s · 250 ms |
| Chapter server lookups | 20/min |
| Concurrent chapters · pages | 1 · 2 |
| Monitoring interval | 6 h (±10 % jitter), scans ≥ 60 s apart |
| Followed feed (account) | 15 min, usually 1 request |
| Retries | 8, 30 s → 6 h exponential backoff with jitter |
| 429 cooldown | 60 s, doubling per repeated 429 up to 1 h; `Retry-After` always honoured |

A 100-page chapter therefore takes roughly a minute. Raising the limits makes downloads faster but increases the
chance of rate limiting. **No configuration can guarantee that an IP will never be restricted.** If MangaDex answers
HTTP 401/403 to anonymous API requests, all MangaDex traffic stops, affected jobs are paused and the dashboard shows
the restriction; nothing is retried until you acknowledge it in *Settings → System* (or `mdm clear-access-block`).
The app never tries to circumvent a restriction.

## How downloads, recovery and imports work

1. A job claims the release, checks the library (sentinel, fs type, write probe) and the staging folder, grows the
   manga's [folder layout](#folder-layout) if needed and computes the final folder. If an unlinked folder is in the
   way, nothing is overwritten: the job needs attention and a Review item is created.
2. Pages are downloaded into `{staging}/{chapter-uuid}/`. Each page is checked (HTTP status, content type, length,
   image signature, end-of-image marker, Pillow verify), written atomically (temp + fsync + rename) and recorded with
   size and SHA-256.
3. When all pages are present and re-verified, a `.mdm-release.json` marker is written, the library is checked
   again, any previous copy is set aside and the staging folder is moved to the final chapter folder.

| Failure | Behaviour |
|---|---|
| Timeout, connection error, 5xx, invalid image | Retry the page (fresh MD@Home node); then job retry with backoff |
| HTTP 429 | Global cooldown, job rescheduled after it (attempt not wasted) |
| HTTP 401/403 from the API (anonymous requests) | Global stop, jobs paused until acknowledged |
| HTTP 400/401/403 from the account login or `/user/*` | Account marked *login failed*, retried hourly; nothing else affected |
| 404 (chapter removed) | Needs attention; release flagged unavailable |
| NAS missing / stale handle / I/O error | Job paused; resumes automatically when the library is back |
| Permission denied / disk full / read-only | Needs attention |
| Worker or host crash | Scheduler detects the stale heartbeat (3 min) and resumes; completed pages are kept |
| Redis flushed | Lost queue entries are re-dispatched from PostgreSQL |

**Weekly library check.** Chapters you move out of the library by hand are only noticed by a library scan. The
scheduler runs one every week — **Sunday at 05:00** local time (`TZ`) by default, configurable in *Settings*. It only
updates statuses (*Folder deleted*, *Edited locally*, back in place, unknown folders to Review) and never touches
files. `mdm library-check` shows the status, `mdm library-check --now` runs it.

**Reconciliation** (*Check folder* / *Scan library*): releases whose folder vanished become *Folder deleted*
(never re-downloaded automatically); folders whose files differ from the manifest become *Edited locally*
(left untouched); unlinked folders are matched by marker, or by language + group + chapter name when exactly one
release fits; otherwise they go to Review.

## Security model

- Every API route except `/api/health`, `/api/auth/status`, `/api/auth/login` and `/api/auth/setup` requires a
  valid session (enforced server-side; a test walks every route to verify this).
- Passwords: Argon2id (64 MiB, t=3, p=4), minimum 12 characters; login responses do not reveal whether a user
  exists; lockout after 5 failures per user and per IP within 15 min.
- Sessions: 256-bit random token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` over HTTPS); only its
  SHA-256 is stored; 7-day idle / 30-day absolute expiry; password change revokes all other sessions.
- CSRF: per-session token required in `X-CSRF-Token` for every state-changing request, plus Origin checks.
- Forwarded headers are honoured only from `MDM_TRUSTED_PROXIES`.
- Security headers: strict CSP, `X-Frame-Options: DENY`, `nosniff`, HSTS over HTTPS, `no-store` on API responses.
- Containers run as a non-root user with all capabilities dropped and `no-new-privileges`; DB and Redis are on
  an internal network without published ports; the DB password is a Docker secret; secrets are redacted from logs.
- Every path the app writes is joined component by component under its root and refused if it would escape it.
- MangaDex account credentials are Docker secrets only (never in the database, the API or the UI).

## Backup, restore, upgrade

**Backup** (the library itself lives on the NAS; back it up there):

```sh
docker exec mangadex-webdl-db pg_dump -U mangadex_webdl -Fc mangadex_webdl > mangadex-webdl-$(date +%F).dump
```

Also keep `.env` and `secrets/`. Redis needs no backup (state is rebuilt from PostgreSQL).

**Restore**:

```sh
docker compose stop app worker scheduler
docker exec -i mangadex-webdl-db pg_restore -U mangadex_webdl -d mangadex_webdl --clean --if-exists < backup.dump
docker compose up -d
```

Even without a database backup, *Scan library* re-links every chapter that has a `.mdm-release.json` marker
(the statistics history lives only in the database).

**Upgrade**:

```sh
docker compose pull
docker compose up -d     # the migrate service applies new Alembic migrations before the app starts
```

## Administration (CLI)

Run inside the app container: `docker exec -it mangadex-webdl mdm <command>`

| Command | Purpose |
|---|---|
| `create-admin <user>` | Create an administrator (prompts for the password) |
| `reset-password <user>` | **Password recovery**: set a new password, clear lockouts, revoke sessions |
| `list-users` | List accounts |
| `init-library [--yes]` | Create the library sentinel (only while the NAS is mounted) |
| `library-check [--now]` | Weekly library check: schedule and last result; `--now` queues it |
| `check-library` | Show mount/sentinel/fs-type/writability status |
| `staging [status\|tidy\|cleanup] [--apply]` | List downloads in progress; tidy or clean up staging folders |
| `clear-access-block` | Acknowledge a MangaDex 403 restriction and resume |
| `folder-layout [--manga UUID] [--apply]` | Recompute the minimal folder layout of existing titles (dry run without `--apply`) |
| `chapter-names [--manga UUID] [--keep-groups] [--apply]` | Rename existing chapter folders to the current naming rules (dry run without `--apply`) |
| `chapter-numbering [--manga UUID] [--set volume\|series] [--apply]` | Detect / force per-volume numbering (dry run without `--apply`) |
| `follow-sync [status\|list\|feed]` | MangaDex account: show status, or run the followed-list / followed-feed sync now |
| `archive [status\|index\|check --title T]` | Comics archive: index status, rebuild the index now, or show what a folder title resolves to |
| `init-db` | Run migrations + seed data (done by the `migrate` service) |

## Development and tests

```sh
# Backend: needs a PostgreSQL database and a Redis DB that may be wiped (MangaDex is an in-memory fake)
cd backend
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
export MDM_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/mdm_test
export MDM_TEST_REDIS_URL=redis://localhost:6379/15
pytest            # without the two variables only the pure unit tests run

# Frontend
cd frontend && npm ci && npm test && npm run build
```

`deploy/run-backend-tests.sh [pytest args]` runs the backend suite inside a built image (`MDM_TEST_IMAGE`, default
`mangadex-webdl:local`) against throwaway PostgreSQL 17 and Redis containers, removed afterwards. On GitHub, the
*Tests* workflow runs both suites on every push and pull request.

**Test infrastructure** (`backend/tests/conftest.py`, `tests/helpers.py`, `tests/test_api_auth.py`,
`frontend/src/test/setup.ts`):

- `conftest.py`: temporary library / staging / replaced folders reset before every test, Alembic migrations to head
  once per session (so every migration is exercised), all tables truncated and Redis flushed per test.
- `helpers.py`: `FakeMangaDex` answers the MangaDex API, the MD@Home image node (real JPEGs), covers and the health
  report through an `httpx.MockTransport`, so the real `MangaDexClient` is used end to end.
- `test_api_auth.py`: wrong passwords, **every `/api` route answering 401 without a session** (walked from the
  OpenAPI schema), CSRF and cross-origin rejection.

Suites: **backend 103 tests** (`test_api_auth`, `test_archive`, `test_group_exclusions`, `test_highlighted_tags`,
`test_late_releases`, `test_library_safety`, `test_numbering`, `test_rename`, `test_revisions`, `test_tiers`),
**frontend** Vitest suites in `frontend/src/test`.

## Building and publishing the image

`.github/workflows/docker-publish.yml` builds `linux/amd64` + `linux/arm64` images with Buildx and pushes them to:

- **GHCR**: `ghcr.io/<github owner>/mangadex-webdl` — works out of the box with the built-in `GITHUB_TOKEN`
  (after the first push, set the package visibility to *Public* in GitHub → Packages if you want anonymous pulls);
- **Docker Hub**: `docker.io/<DOCKERHUB_USERNAME>/mangadex-webdl` — when the repository has the variable
  `DOCKERHUB_USERNAME` and the secret `DOCKERHUB_TOKEN` (an access token with *Read & Write* scope) under
  *Settings → Secrets and variables → Actions*. Without them only GHCR is used.

| Event | Tags |
|---|---|
| push to `main` | `edge`, `sha-<short>` |
| tag `v1.2.3` | `1.2.3`, `1.2`, `1`, `latest`, `sha-<short>` (and the Docker Hub description is updated from `docs/DOCKERHUB.md`, since Docker Hub accepts at most 25,000 characters) |
| pull request | built, not pushed |

Release:

```sh
git tag v1.0.0 && git push origin v1.0.0
```

## Known limitations

- The MangaDex account is used read-only: following/unfollowing and read markers are not synced back to MangaDex.
- The followed feed only covers titles followed on MangaDex; other titles rely on the per-manga scans.
- The archive is matched by exact (normalised) title: a folder named differently from every MangaDex title is not
  found. Volumes with only numbered pages need volume data on MangaDex to be turned into a chapter number.
- Per-volume numbering: the add/approve form still takes a plain chapter number as the start; once the title is
  detected as per-volume it is converted. Volumes are capped at 99,999 and chapters within a volume should stay
  below 1000.
- Late chapters: a chapter that MangaDex removed *before* the app ever listed it is unknown to the database, so its
  re-upload looks like a new chapter number and is downloaded. Version checks compare page file names, so turning
  *data saver* on or off makes the next version check re-download the chapter.
- Revisions: an untouched copy is recognised by size and SHA-256 of its pages; renaming a page file without changing
  it also counts as an edit (the copy is kept).
- Chapters hosted externally (e.g. MangaPlus links) are listed as *External* and cannot be downloaded.
- MangaDex's feed endpoint is capped at 10 000 results per query; titles with more releases in the selected
  languages are only partially listed.
- Renaming a language folder code, a group or a chapter title later does not move existing folders
  (`mdm chapter-names --apply` does).
- Replaced copies are never cleaned up automatically. Staging folders of cancelled downloads stay until
  `mdm staging cleanup --apply`.
- Statistics are computed on every request (fine for years of history at this scale; there is no cache).

## Acknowledgements

- **[MangaDex](https://mangadex.org)** — this project exists thanks to MangaDex, its free public API and the
  volunteers who run the site and the MangaDex@Home network. If you use this tool, consider
  supporting MangaDex.
- **Scanlation groups** — every chapter downloaded by this app is the work of the group that translated, cleaned
  and typeset it. The app keeps the group name in the UI, in the chapter marker (`.mdm-release.json`) and, where
  needed, in the chapter folder name. Please support the groups you read.
- **Authors, artists and publishers** — the manga belong to them. Buy the official releases whenever they are
  available in your language; titles that are published officially (MangaPlus, K MANGA, Azuki, …) are listed on
  MangaDex only as external links and are never downloaded by this app.
- **Open-source software** — FastAPI, Uvicorn, SQLAlchemy, Alembic, PostgreSQL, Redis, RQ, httpx, Pillow, Argon2,
  React, React Router, TanStack Query, Vite and Vitest, among others.
- The application was designed and written together with Claude, Anthropic's AI assistant.

## Disclaimer and legal notice

**Not affiliated.** MangaDex WebDL is an independent project. It is not affiliated with, endorsed by or sponsored by
MangaDex, by any scanlation group or by any author or publisher. "MangaDex" and the MangaDex logo belong to their
respective owners and are used here only to describe what the software works with.

**No content is hosted or distributed.** This repository and the published Docker images contain source code only.
They contain no manga, chapters, scans, covers or other copyrighted material, and they link to none. The software
does not rehost, share, seed or publish anything: pages are fetched at runtime, by each user's own instance, from
the official MangaDex servers, and stored only on that user's own storage. (Cover art in the screenshots above is
pixelated on purpose; series titles are shown only to illustrate the interface.)

**Copyright.** Manga and their artwork are the property of their authors and publishers; scanlations are the work of
their groups. Whether downloading a given work is lawful depends on the work, on your country and on how you use it.
**You alone are responsible** for what you download with this software and for respecting copyright law and the
terms of the services you access. The software is intended for **personal, private, non-commercial use**: do not
redistribute, publicly host, sell or otherwise share downloaded content.

**How the MangaDex API is used.** The software follows the MangaDex API Acceptable Usage Policy:

- it uses only the official public API (`api.mangadex.org`) and the image servers MangaDex hands out — no HTML
  scraping, no unofficial endpoints, no circumvention of any limit or block;
- it credits MangaDex (this README, the in-app *About* page) and keeps the scanlation group of every chapter;
- it runs no ads and offers no paid features, and nothing obtained through the API is sold;
- it stays well below the documented rate limits by default ([details](#rate-limiting-speed-vs-politeness)),
  honours `Retry-After`, pauses itself on HTTP 429 and **stops completely** on HTTP 403 until the operator
  acknowledges it;
- it reports MangaDex@Home node health as requested, caches metadata and never re-downloads pages it already has;
- the optional MangaDex account is used **read-only** (followed list and feed); nothing is ever written to it;
- externally hosted chapters (official publishers) are never downloaded.

**Removal requests.** Scanlation groups, authors and rights holders who want their work excluded can open an issue
on this repository (or contact the maintainer privately through GitHub). Requests from scanlation groups are honoured
as MangaDex's policy requires; group exclusions can also be set per title by every user in the app.

**No warranty.** The software is provided "as is", without warranty of any kind, as stated in sections 15 and 16 of
the license. The authors are not liable for any use or misuse of it, nor for any consequence of that use (including
restrictions applied by MangaDex to an IP address or account).

## License

Copyright © 2026 LainX84.

MangaDex WebDL is free software: you can redistribute it and/or modify it under the terms of the
**GNU Affero General Public License v3.0 or later** — see [LICENSE](LICENSE). In short: you may use, study, modify
and share it, but any distributed or network-hosted modified version must keep this copyright notice, stay under
the same license and make its full source code available to its users.

The license covers the source code of this project only, not any content downloaded with it.
