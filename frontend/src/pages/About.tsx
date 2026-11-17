import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { Dashboard } from "../types";

const FLOW = [
  { name: "MangaDex", text: "Official API only: catalogue, chapters and the image servers (MangaDex@Home)." },
  { name: "Scheduler", text: "With a MangaDex account it reads the feed of every followed manga every 15 minutes (one request for all of them); every few hours it also checks each title on its own. New chapters go to the queue." },
  { name: "Archive", text: "When you add a title it looks in your CBZ collection (read-only) for the newest chapter you already own and suggests where to continue." },
  { name: "Queue", text: "Redis + RQ carry the job list; the real state lives in PostgreSQL." },
  { name: "Worker", text: "Downloads page by page, verifies every image and writes atomically." },
  { name: "Library", text: "One folder per manga → (language) → chapter, with numbered pages." },
];

const STACK: { area: string; items: [string, string][] }[] = [
  {
    area: "Backend",
    items: [
      ["Python 3.12", "the whole server side"],
      ["FastAPI + Uvicorn", "the API used by the interface"],
      ["SQLAlchemy 2 + Alembic", "database access and migrations"],
      ["httpx", "MangaDex requests through a shared rate limiter"],
      ["OAuth2 (personal client)", "read-only access to your MangaDex account"],
      ["Pillow", "checks that every page is an intact image"],
      ["zipfile", "reads only the index of the archive's CBZ files, never the images"],
      ["Argon2", "password hashing"],
    ],
  },
  {
    area: "Frontend",
    items: [
      ["React 19 + TypeScript", "the interface you are using"],
      ["Vite", "builds the interface"],
      ["TanStack Query", "data kept up to date without reloading the page"],
      ["React Router", "navigation between screens"],
    ],
  },
  {
    area: "Data and infrastructure",
    items: [
      ["PostgreSQL 17", "manga, chapters, jobs and settings"],
      ["Redis 7.4 + RQ", "download queue and rate limiter"],
      ["Docker Compose", "one image, six services"],
      ["NAS (NFS)", "the library itself, plus the optional read-only CBZ archive"],
    ],
  },
];

const SERVICES: [string, string][] = [
  ["app", "web interface and API (port 8000 in the container)"],
  ["worker", "runs downloads, scans and the MangaDex account sync"],
  ["scheduler", "periodic checks, account feed every 15 minutes, resuming interrupted jobs"],
  ["db", "PostgreSQL"],
  ["redis", "queue and rate limiter"],
  ["migrate", "updates the database at startup, then exits"],
];

const RULES = [
  "It never overwrites a folder it did not create: when in doubt it goes to Review.",
  "It downloads nothing while the NAS is not mounted: without the .mdm-library file it stops.",
  "It respects MangaDex's limits: on a 429 it pauses by itself, on a 403 it stops completely.",
  "Manual edits stay: removed pages or moved folders are never restored on their own.",
  "A chapter already downloaded (or already owned elsewhere) is not downloaded again, not even from another group.",
  "Paid / external groups (MangaPlus, K MANGA, Azuki…) are excluded: their chapters are only links.",
  "Titles followed on MangaDex never come in by themselves: they wait in Approvals, with no scans or downloads, until you approve them.",
  "The MangaDex account is used read-only, and a login error never stops the rest of the app.",
  "The comics archive is mounted read-only: it is only read, never changed, and what it finds is a suggestion, not a choice made for you.",
];

export default function AboutPage() {
  const { data } = useQuery({ queryKey: ["dashboard"], queryFn: () => api<Dashboard>("/api/dashboard") });

  return (
    <div className="about">
      <div className="topbar">
        <h1 className="page-title">
          <small>About</small>MangaDex WebDL
        </h1>
      </div>

      <p className="about-lead">
        A self-hosted, always-on download manager for MangaDex: it follows the manga you choose, notices new chapters
        and saves them to your NAS as plain images, ready to be tidied up or packed into CBZ files.
      </p>

      {data && (
        <div className="about-stats">
          <div><b>{data.followed_manga.toLocaleString()}</b><span>manga followed</span></div>
          <div><b>{data.completed_releases.toLocaleString()}</b><span>chapters downloaded</span></div>
          <div><b>{(data.jobs.pending + data.jobs.active).toLocaleString()}</b><span>in the queue now</span></div>
          {(data.pending_approvals ?? 0) > 0 && (
            <div><b>{(data.pending_approvals ?? 0).toLocaleString()}</b><span>waiting for approval</span></div>
          )}
        </div>
      )}

      <h2 className="section-title">What it is for</h2>
      <div className="card about-prose">
        <p>
          A manga collection on the NAS that keeps itself up to date, without checking every series by hand and
          without leaving a PC switched on. It runs in Docker, uses only the official MangaDex API, saves pages as
          images rather than archives, never touches what you edited by hand and recovers on its own after a restart
          or a NAS that goes away.
        </p>
        <p>
          With a MangaDex account, instead of asking about one title at a time the app reads the news of every manga
          you follow in one go, so chapters are downloaded minutes after release. Titles you follow on MangaDex but
          have not added yet wait in <Link to="/approvals">Approvals</Link>, where you choose languages, groups and the
          chapter to start from.
        </p>
        <p>
          To avoid remembering where you got to, the app can look into your existing CBZ collection: it finds the
          folder named after any MangaDex title (English, romaji, …), reads the newest chapter from the file names or
          from the index of the newest volume and suggests the chapter to continue from.
        </p>
        <p>
          The application was designed and written together with Claude, Anthropic's AI assistant.
        </p>
      </div>

      <h2 className="section-title">How it works</h2>
      <ol className="about-flow">
        {FLOW.map((s, i) => (
          <li key={s.name}>
            <span className="about-step">{i + 1}</span>
            <b>{s.name}</b>
            <span className="small muted">{s.text}</span>
          </li>
        ))}
      </ol>

      <h2 className="section-title">Rules it never breaks</h2>
      <ul className="card about-rules">
        {RULES.map((r) => <li key={r}>{r}</li>)}
      </ul>

      <h2 className="section-title">Technology</h2>
      <div className="about-stack">
        {STACK.map((g) => (
          <div className="card" key={g.area}>
            <h3>{g.area}</h3>
            <dl>
              {g.items.map(([k, v]) => (
                <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
              ))}
            </dl>
          </div>
        ))}
      </div>

      <h2 className="section-title">Containers of the stack</h2>
      <div className="card">
        <dl className="about-services">
          {SERVICES.map(([k, v]) => (
            <div key={k}><dt className="mono">{k}</dt><dd>{v}</dd></div>
          ))}
        </dl>
      </div>

      <h2 className="section-title">Credits and license</h2>
      <div className="card about-prose">
        <p>
          Chapters come from <a href="https://mangadex.org" target="_blank" rel="noreferrer">MangaDex</a> and are the
          work of the scanlation groups credited on every chapter; the manga belong to their authors and publishers.
          Support them, and buy the official releases when you can. This project is not affiliated with MangaDex and
          is meant for personal, non-commercial use only.
        </p>
        <p className="small">
          Source code:{" "}
          <a href="https://github.com/lainx84/mangadex-webdl" target="_blank" rel="noreferrer">github.com/lainx84/mangadex-webdl</a>
          {" "}· © 2026 LainX84 · GNU AGPL-3.0-or-later
        </p>
      </div>

      <h2 className="section-title">Tests</h2>
      <div className="card about-prose">
        <p className="small">
          MangaDex is simulated in the automated tests, so errors (429, 403, an unmounted NAS, a worker crashing
          halfway through a chapter) can be exercised without bothering the real site.
        </p>
      </div>
    </div>
  );
}
