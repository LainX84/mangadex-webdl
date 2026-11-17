export interface User {
  id: number;
  username: string;
  is_admin: boolean;
  csrf_token: string;
}

export interface GroupRef {
  id: string;
  name: string;
}

export interface MangaSummary {
  id: string;
  title: string;
  canonical_title: string;
  title_override: string | null;
  folder_name: string;
  cover_url: string | null;
  status: string | null;
  year: number | null;
  languages: string[];
  group_mode: "all" | "selected";
  groups: GroupRef[];
  downloaded: number;
  missing: number;
  external: number;
  active_jobs: number;
  failed_jobs: number;
  monitoring_enabled: boolean;
  auto_download: boolean;
  last_scan_at: string | null;
  last_success_at: string | null;
  next_scan_at: string | null;
  last_error: string | null;
  update_tier: 1 | 2 | 3 | null;
  upload_complete: boolean;
  last_chapter_at: string | null;
  prev_chapter_at: string | null;
  pending_approval: boolean;
  pending_since: string | null;
  added_via: string | null;
  tags: string[];
}

export interface MangaDetail extends MangaSummary {
  alt_titles: Record<string, string>[];
  description: string | null;
  content_rating: string | null;
  original_language: string | null;
  authors: string[];
  available_languages: string[];
  last_volume: string | null;
  last_chapter: string | null;
  mangadex_url: string;
  interval_minutes: number | null;
  include_unknown_group: boolean;
  start_chapter: number | null;
  one_release_per_chapter: boolean;
  /** Chapter numbering restarts every volume: start_chapter = volume * 1000 + chapter. */
  chapters_per_volume: boolean;
  consecutive_failures: number;
  metadata_refreshed_at: string | null;
  effective_interval_minutes: number;
}

export type JobStatus =
  | "pending"
  | "queued"
  | "downloading"
  | "paused"
  | "retry_scheduled"
  | "completed"
  | "failed"
  | "needs_attention"
  | "cancelled";

export interface ReleaseBrief {
  id: string;
  language: string;
  volume: string | null;
  chapter: string | null;
  title: string | null;
  groups: string[];
}

export interface Job {
  id: number;
  release_id: string;
  status: JobStatus;
  pause_reason: string | null;
  attempts: number;
  next_attempt_at: string | null;
  last_error: string | null;
  error_kind: string | null;
  pages_total: number | null;
  pages_done: number | null;
  is_redownload: boolean;
  trigger: string;
  target: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
  release?: ReleaseBrief;
  manga?: { id: string; title: string; cover_url: string | null; update_tier?: number | null; upload_complete?: boolean };
}

export interface Release extends ReleaseBrief {
  group_ids: string[];
  pages: number;
  publish_at: string | null;
  external: boolean;
  unavailable: boolean;
  local_status: "missing" | "downloaded" | "imported" | "modified" | "missing_on_disk" | "skipped" | "external" | "replaced" | "in_replaced";
  folder: string | null;
  local_page_count: number | null;
  downloaded_at: string | null;
  wanted: boolean;
  sort: number | null;
  job: Job | null;
  mangadex_url: string;
}

export interface Language {
  code: string;
  name: string;
  folder_code: string;
}

export interface GroupStat {
  id: string;
  name: string;
  languages: Record<string, number>;
  subscribed?: boolean;
}

export interface ScanRun {
  id: number;
  manga_id: string;
  manga_title: string | null;
  trigger: string;
  full_scan: boolean;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  releases_seen: number;
  new_releases: number;
  queued: number;
  requests_made: number;
  error: string | null;
}

export interface LibraryStatus {
  ok: boolean;
  path: string;
  exists: boolean;
  is_dir: boolean;
  sentinel_present: boolean;
  fs_type: string | null;
  fs_type_ok: boolean;
  writable: boolean;
  free_bytes: number | null;
  error: string | null;
  checked_at?: string;
}

export interface Cooldown {
  until: string;
  seconds_remaining: number;
  reason: string;
  /** "maintenance" while MangaDex shows its maintenance page, otherwise "rate_limit". */
  kind?: string;
  strikes: number;
}

export interface AccessBlock {
  status: number;
  message: string;
  url?: string;
  at?: string;
}

export interface Components {
  scheduler: { alive: boolean; last_seen: string | null };
  worker: { alive: boolean; last_seen: string | null };
}

export interface Dashboard {
  followed_manga: number;
  monitored_manga: number;
  completed_releases: number;
  jobs: { active: number; pending: number; paused: number; failed: number };
  active_downloads: Job[];
  recent_downloads?: Job[];
  last_successful_scan: string | null;
  next_scheduled_scan: string | null;
  recent_scans: ScanRun[];
  cooldown: Cooldown | null;
  access_block: AccessBlock | null;
  queue_paused: boolean;
  library: LibraryStatus | null;
  components: Components;
  open_reviews: number;
  open_revisions?: number;
  pending_approvals?: number;
  mangadex_account?: {
    configured: boolean;
    active: boolean;
    username: string | null;
    last_feed_sync_at: string | null;
    last_feed_result: FeedResult | null;
    last_error: string | null;
    auth_failed: boolean;
  } | null;
}

export interface FeedResult {
  seen: number;
  new: number;
  queued: number;
  ignored?: number;
  manga?: number;
  at?: string;
}

export interface AccountStatus {
  configured: boolean;
  secrets: { client_id: boolean; client_secret: boolean; username: boolean; password: boolean };
  enabled: boolean;
  active: boolean;
  username: string | null;
  followed: number;
  pending_approval: number;
  last_list_sync_at: string | null;
  last_feed_sync_at: string | null;
  feed_cursor: string | null;
  last_feed_result: FeedResult | null;
  last_list_result: { followed: number; added_pending: number; at?: string } | null;
  last_error: string | null;
  last_error_at: string | null;
  auth_failed: boolean;
  feed_interval_minutes: number;
  list_interval_minutes: number;
}

export type ReadingStatus = "reading" | "plan_to_read" | "on_hold" | "re_reading" | "completed" | "dropped";

export interface PendingManga {
  id: string;
  title: string;
  /** Main MangaDex title if English, else the first English alternative title. */
  english_title: string | null;
  canonical_title: string;
  cover_url: string | null;
  description: string;
  status: string | null;
  year: number | null;
  content_rating: string | null;
  original_language: string | null;
  last_chapter: string | null;
  available_languages: string[];
  languages: string[];
  tags: string[];
  pending_since: string | null;
  reading_status: ReadingStatus | null;
  followed: boolean;
  mangadex_url: string;
}

export interface Approvals {
  items: PendingManga[];
  total: number;
  dismissed: { id: string; title: string; reading_status: ReadingStatus | null; followed: boolean; dismissed_at: string | null }[];
}

export interface SearchResult {
  id: string;
  title: string;
  alt_titles: string[];
  year: number | null;
  status: string | null;
  content_rating: string | null;
  description: string;
  available_languages: string[];
  cover_url: string | null;
  followed: boolean;
  pending?: boolean;
  titles?: TitleChoice[];
}

export interface ReviewItem {
  id: number;
  kind: "unmatched_folder" | "ambiguous_match" | "unknown_manga_folder" | "destination_conflict" | "revision_after_move";
  path: string;
  status: string;
  details: Record<string, unknown>;
  candidates: {
    release_id: string;
    language?: string;
    volume?: string | null;
    chapter?: string | null;
    title?: string | null;
    groups?: string[];
    pages?: number;
    local_status?: string;
  }[];
  created_at: string | null;
  manga: { id: string; title: string } | null;
}

export type AppSettings = Record<string, number | boolean | string | string[] | { name: string; color: string }[]>;

export type TitleChoice = { title: string; language: string; source: "main" | "alt"; preferred: boolean };
export type MangaTitles = {
  title: string;
  canonical_title: string;
  title_override: string | null;
  folder_name: string;
  preferred_languages: string[];
  live: boolean;
  titles: TitleChoice[];
};
export type RenameReport = {
  title: string;
  title_override: string | null;
  folder_from: string;
  folder_to: string;
  folder_changes: boolean;
  chapters_on_disk: number;
  busy_downloads: number;
  moved: boolean;
  dry_run: boolean;
};

export type ChapterPreview = {
  chapter: string | null;
  volume: string | null;
  language: string;
  title: string | null;
  groups: string[]; // "" = released without a group
  published: string | null;
};

export interface ArchiveCandidate {
  folder: string;
  title: string;
  language: string | null;
  language_match: boolean;
  custom_only: boolean;
  archives: number;
  errors: string[];
  last_chapter: number | null;
  last_volume?: number | null;
  source?: "file" | "inside" | "volume" | "volume_unmapped" | null;
  evidence?: string | null;
  next_chapter?: number | null;
  after_count?: number | null;
  after_last?: number | null;
  up_to_date?: boolean;
}

export interface ArchiveSuggestion {
  configured: boolean;
  indexed_at?: string | null;
  candidates: ArchiveCandidate[];
  best: ArchiveCandidate | null;
}

export interface ArchiveStatus {
  configured: boolean;
  enabled: boolean;
  root: string | null;
  dirs: string[];
  folders: number;
  indexed_at: string | null;
  last_result: Record<string, unknown> | null;
  rescan_hours: number;
}
