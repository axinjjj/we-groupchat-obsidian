# Source reliability

This guide covers four deliberately separate responsibilities:

1. the optional WeChat source guard, which may request a normal macOS
   application launch;
2. the attachment catalog and private local content-addressed archive;
3. direct selected-chat file sync to the user's authorized Google Drive; and
4. an optional provider-neutral filesystem snapshot target.

They do not form a single always-on daemon. `TopicMonitor` reads messages and
owns checkpoints; it does not import or invoke the source guard. The Knowledge
transaction owns attachment mentions; a post-commit worker owns byte copying.
The selected-chat scanner owns an independent cursor and queue and may reuse the
same local CAS without a Knowledge hit. The backup command reads immutable local
archive objects and writes only to the configured filesystem target.

## 1. Optional WeChat source guard

The source guard is disabled by default. Enabling its policy, installing its
LaunchAgent plist, and loading that LaunchAgent are separate actions:

```bash
.venv/bin/python scripts/wechat_source_guard.py status
.venv/bin/python scripts/wechat_source_guard.py enable
.venv/bin/python scripts/wechat_source_guard.py install-agent
.venv/bin/python scripts/wechat_source_guard.py install-agent --load-now
```

`install-agent` writes a separate one-shot `StartInterval` job. The plist has no
`KeepAlive` key. Without `--load-now`, it is not bootstrapped. Each invocation
runs one check, takes a non-blocking lock, persists a small private state file,
and exits. If `WE_GROUPCHAT_OBSIDIAN_DATA_DIR` is set when the plist is built,
the LaunchAgent preserves that override explicitly.

When process lookup confirms that WeChat is absent, the guard first enters a
grace period. After grace, and only while restart budget and exponential
backoff allow it, the sole launch request is equivalent to:

```text
open -g -a WeChat
```

The guard never kills WeChat, edits or re-signs the app, extracts keys, drives
UI, handles login, steals focus, or advances a monitor checkpoint. If macOS
process lookup is unavailable, the state is `process_lookup_unknown` and the
guard does not launch anything. A running but stale local database produces one
warning per stale episode; a fresh observation closes that episode. Staleness
is not treated as permission to restart.

Effective states are:

| State | Meaning |
| --- | --- |
| `disabled` | Policy is off; no launch can be requested. |
| `healthy` | WeChat was confirmed running. Source freshness is reported separately. |
| `missing_grace` | Absence was confirmed, but the grace period has not elapsed. |
| `restart_backoff` | A normal launch was requested or a retry is waiting for backoff. |
| `paused` | A timed or indefinite user pause blocks launches. |
| `degraded` | Restart budget was exhausted or the launch command failed repeatedly. |
| `process_lookup_unknown` | The process list could not be read; absence is unknown and fail-closed. |

Pause, resume, one-shot check, disable, and removal are explicit:

```bash
.venv/bin/python scripts/wechat_source_guard.py pause --hours 8
.venv/bin/python scripts/wechat_source_guard.py pause --indefinite
.venv/bin/python scripts/wechat_source_guard.py resume
.venv/bin/python scripts/wechat_source_guard.py check
.venv/bin/python scripts/wechat_source_guard.py disable
.venv/bin/python scripts/wechat_source_guard.py uninstall-agent
```

State and receipts live under the project runtime data directory with `0600`
file permissions inside `0700` directories. Receipts contain state transitions,
budget/backoff values, and error codes, not chat names, usernames, message
content, or attachment paths. Degraded/launch notifications remain keyed and
cooldown-throttled; stale-source notifications are episode-scoped.

## 2. Stable message and resource identity

Message reads inspect each actual WeChat table schema before building the
query. When present, the source envelope records `local_id`, `server_id`,
`sort_seq`, SQLite `rowid`, shard identity, `create_time`, and `local_type`.
Missing optional columns remain explicit nulls. A deterministic
`source_message_id` is derived from those values and a hash of the chat
username; it does not expose the username or `wxid`.

File XML and image packed metadata are parsed before message text is cleaned.
The resulting resource envelope can contain the original file name, declared
size, declared MD5/SHA-256, attach id, extension, or image hash. Only sender and
cleaned message text go to the AI formatter; source envelopes and internal IDs
do not.

## 3. Attachment catalog and local archive

For a Knowledge hit, `attachment_mentions` rows are inserted in the same SQLite
transaction as the corresponding `events` row. `attachment_objects` is the
canonical object catalog and `attachment_attempts` is a content-free attempt
history. After the event commits, the app may start a daemon thread that
consumes pending mentions. A process-level archive lock prevents duplicate
workers. Fresh `pending` rows are selected before retries. Failed retryable rows
record `attempt_count` and `next_retry_at`, use bounded exponential backoff, and
are eligible only when due. Each trigger persists a wake generation before it
tries the lock; the active worker drains successive batches until no due work
or unconsumed wake remains.

This separation is intentional:

- cache resolution or copying cannot roll back a committed event;
- failures do not call the AI again;
- failures do not rewind or advance a monitor checkpoint; and
- retry changes catalog state only.

The local archive is disabled by default. Enable it explicitly and choose the
accepted resource kinds; images remain a separate opt-in:

```json
{
  "attachment_archive_enabled": true,
  "attachment_archive_kinds": ["file", "image"],
  "attachment_archive_max_object_bytes": 536870912,
  "attachment_archive_min_free_bytes": 1073741824,
  "attachment_archive_retry_base_seconds": 300,
  "attachment_archive_retry_max_seconds": 21600
}
```

Newer WeChat V2 image objects also require the locally captured
`image_aes_key`; the key is not stored in the attachment catalog, archive
receipts, backup manifest, or documentation.

### File resolution

The resolver searches only the declared month in WeChat's `msg/file` cache. It
accepts the exact name and conventional `name (N).ext` variants. Declared size
and MD5/SHA-256, when available, filter candidates before selection.

- one valid candidate is selected;
- several candidates with identical bytes are equivalent duplicates and are
  safe to select deterministically;
- several candidates with different bytes are `ambiguous`; and
- no valid candidate is `missing_retryable`.

Directory order and modification time are never used as identity. Symlinks,
non-regular files, and candidates outside the expected WeChat cache root are
rejected.

### Image resolution

Images are located only by the structured image hash, hashed chat directory,
and source month. Full/original suffixes are preferred over thumbnail suffixes.
There is no modification-time fallback. Outcomes distinguish:

- `original_archived`;
- `thumbnail_only`;
- `decode_unavailable`;
- `missing_retryable`; and
- `ambiguous` or a source-rejection/failure state where applicable.

### Content-addressed storage

Objects live under:

```text
~/.we-groupchat-obsidian/attachment_archive/
  objects/sha256/<first-two-hex>/<sha256>--<safe-original-name>
  tmp/
```

The root and object directories are `0700`; final objects and the worker lock
are `0600`. Copying uses a private partial file, calculates SHA-256 while
writing, checks source identity/size/mtime before and after the read, `fsync`s,
and atomically renames the completed object. A final object without a catalog
row is reusable after a crash; worker-owned partial files are recoverable and
never treated as objects.

SHA-256 is the identity, so identical bytes referenced under different names
use one immutable object. Markdown resource sections show the catalog status,
local object link when available, and the existing month-folder hint.

Before copying, the worker rejects objects larger than
`attachment_archive_max_object_bytes` and preserves at least
`attachment_archive_min_free_bytes` after the proposed write. An oversized
object requires an explicit policy change/manual retry; low-space failures use
the normal due-only retry schedule.

Important storage boundary: the archive does **not** delete or prune WeChat's
own cache. It avoids creating multiple archive copies of identical bytes, but
it does not reclaim source-cache disk space. Destructive cache retention or
pruning is outside this tranche and would require a separately reviewed policy.

### Archive CLI

```bash
.venv/bin/python scripts/attachment_archive.py status
.venv/bin/python scripts/attachment_archive.py run --limit 50
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id>
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id> --run
```

Historical backfill is plan/apply separated:

```bash
.venv/bin/python scripts/attachment_archive.py backfill
.venv/bin/python scripts/attachment_archive.py backfill --apply
.venv/bin/python scripts/attachment_archive.py backfill --apply --run
```

The first command reads historical `events.files_json` and reports counts. It
does not insert mentions or copy bytes. `--apply` explicitly inserts missing
historical catalog rows; `--run` then consumes pending rows.
`--limit` is the worker batch size, not a total cap: an acquired worker keeps
draining fresh and currently due rows before it exits.

## 4. Direct selected-chat Google Drive file sync

This is the ordinary automatic file-backup lane. It is independent of
`TopicMonitor`, Knowledge selection, the source guard, and the filesystem
snapshot backend:

```text
selected WeChat chats
  -> per-chat x message-shard cursor + durable file-message queue
  -> exact file resolver + shared local SHA-256 CAS
  -> one Drive object per unique byte digest
  -> chat / source-month Drive shortcuts
```

Only resources whose source envelope says `kind=file` are eligible in this
release. Images, voice messages, videos, and stickers do not enter this queue.
The scanner advances over missing and non-file messages, so a file that has not
yet appeared in WeChat's cache cannot block later messages. Its bookmark is a
per-chat x privacy-safe message-shard cursor: a failed shard is persisted as
`source_degraded` without moving that shard cursor, while healthy shards may
advance. Recovery queues the previously unseen file exactly once. Ordinary
`WeChatDB.get_messages()` also uses a strict all-known-shards contract and never
returns remaining rows as a falsely complete page. Receipts and health expose
only content-free error codes and degraded-shard counts, never raw paths, chat
IDs, or database names.

`missing_retryable` uses due-only bounded exponential retry; `ambiguous` is
terminal until a human changes the local evidence and explicitly retries
through a future workflow.

The first enable initializes each currently selected chat at the current time.
It does not silently upload history. Historical discovery is a separate
plan/apply operation. Removing a chat from the selected set retains its cursor,
queue, and retry state but stops resolving or uploading its pending items until
the chat is selected again.

### Local configuration and ledger

Public defaults are off:

```json
{
  "google_drive_file_sync_enabled": false,
  "google_drive_file_sync_paused": false,
  "google_drive_file_sync_selected_chats": [],
  "google_drive_file_sync_interval_seconds": 300,
  "google_drive_file_sync_max_messages_per_scan": 500,
  "google_drive_file_sync_max_uploads_per_run": 20,
  "google_drive_file_sync_max_bytes_per_run": 536870912,
  "google_drive_file_sync_root_name": "微信群文件归档",
  "google_drive_file_sync_keep_local_objects": true
}
```

Selected chat usernames live only in private local config and the independent
local ledger. Each remote chat identity is a salted SHA-256 key derived from a
random local `archive_id`; raw `@chatroom` usernames are never Drive metadata.
The ledger stores a timestamp and complete same-timestamp message identity set
for each chat x shard cursor, making same-second pagination and restart
idempotent. `(source_message_id, resource_index)` is globally unique.
Raw chat bodies are not stored in this database or its run receipts.

`drive_scan_state` retains the enable-time chat seed; canonical incremental
positions live in `drive_scan_shards`. The other principal tables are
`drive_sync_items`, `drive_objects`, `drive_placements`, `drive_folders`, and
content-free `drive_sync_runs`. A non-blocking process lock makes menu timer, CLI, and app
startup recovery safe callers of the same one-shot worker; there is no new
infinite loop. Disabling or pausing prevents a new scan/upload. If the setting
changes during one file, that file finishes safely and the worker stops before
taking the next item.

`attempt_count` counts retry failures only. Successful transitions such as
`uploading -> shortcut_pending -> complete` no longer inflate exponential
backoff, and progress across a resolve, object, or shortcut phase resets that
phase's consecutive failure count.

### OAuth and credential boundary

Authentication is Installed desktop app OAuth 2.0 with a system browser,
loopback callback, and PKCE. The only requested scope is:

```text
https://www.googleapis.com/auth/drive.file
```

The user's own OAuth client JSON is normalized into the private runtime data
directory with mode `0600`; it is never ordinary config or tracked source. The
refresh token is stored only in macOS Keychain. Access tokens remain in memory.
Offline access is requested. Service accounts are unsupported. `auth-status`
validates the refresh token and distinguishes `token_present` from `connected`.
`invalid_grant` removes the already-invalid Keychain token so later status does
not keep reporting a connection. Queued work becomes `auth_required`, emits one
notification for that episode, and remains durable. `disconnect` removes the
Keychain refresh token but does not delete config, queue, CAS objects, or Drive
files.

### Drive identity and projection

The first successful remote run creates or adopts one app-owned root folder,
whose default visible name is `微信群文件归档`. Its Drive file ID is authority:
rename or move does not matter. If that known root is trashed, missing, invalid,
or inaccessible, the worker enters `remote_degraded` and does not create a
second root.

```text
微信群文件归档/
  群聊/<stable-local-alias>/<source YYYY-MM>/<Drive shortcut>
  _系统/objects/<sha256-prefix>/<full-sha256>--<safe-original-name>
```

Local SHA-256 is the canonical byte identity. The worker first checks the local
object ledger and then searches `appProperties` before uploading, so a crash
after a successful remote write but before local commit adopts the existing
object or shortcut. Duplicate remote object matches select one canonical Drive
ID, record `remote_duplicate_detected`, and never upload a third copy. Remote
verification prefers `sha256Checksum`; when unavailable it requires matching
size plus `md5Checksum` before recording `uploaded_verified`.

Objects larger than 5 MiB use a real resumable session. Every `308` advances
only to the server-confirmed `Range` offset. After network/429/5xx interruption,
an empty PUT with `Content-Range: bytes */<total>` probes the session. A completed
lost final response is adopted, while an expired 404 session is recreated at
most once in the same one-shot run. Missing, malformed, or regressing ranges
fail explicitly. Session URIs are not persisted across processes: a later run
starts a fresh session and still adopts any completed remote object through
`appProperties`. This is a documented bounded restart policy, not a blind
chunk restart.

One object is shared globally. The placement identity is
`(hashed_chat_key, source_month, sha256)`, so the same bytes in another chat or
month produce another shortcut, not another upload. A same-name/different-hash
collision uses `stem--<hash8>.ext`. A deleted ordinary shortcut or child folder
can be rebuilt by `reconcile`; the project never automatically deletes or
trashes any Drive item.

Remote `appProperties` contain only schema version, random archive ID, role,
SHA-256 where relevant, hashed chat key, and source month. Full source-message
provenance stays local. Google Drive receives the selected chat's configured
alias, original/safe file name, and attachment bytes. It does **not** receive
raw chat usernames, raw message XML/body, `source_message_id`, `wxid`, or local
WeChat cache paths through this lane.

### CLI and menu controls

Authentication, selected-chat choice, enablement, historical backfill, and a
real upload are separate actions:

```bash
.venv/bin/python scripts/google_drive_file_sync.py auth --client-secrets "<installed-desktop-client.json>"
.venv/bin/python scripts/google_drive_file_sync.py auth-status
.venv/bin/python scripts/google_drive_file_sync.py status
.venv/bin/python scripts/google_drive_file_sync.py enable
.venv/bin/python scripts/google_drive_file_sync.py disable
.venv/bin/python scripts/google_drive_file_sync.py pause
.venv/bin/python scripts/google_drive_file_sync.py resume
.venv/bin/python scripts/google_drive_file_sync.py scan
.venv/bin/python scripts/google_drive_file_sync.py run
.venv/bin/python scripts/google_drive_file_sync.py reconcile
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD --apply
.venv/bin/python scripts/google_drive_file_sync.py disconnect
```

`backfill` without `--apply` is read-only. `enable` initializes current selected
chat cursor seeds but does not authenticate, select chats, run backfill, or upload.
The menu bar's **Google Drive group-file backup** submenu provides status,
enable/disable, pause/resume, sync now, chat selection, open root, and
reauthorize. Selecting chats also does not enable or upload.

CLI `auth-status` validates the refresh token. `auth_required`, `retry_wait`,
`remote_degraded`, `source_degraded`, and failed one-shot results return a
non-zero exit status so operators and schedulers cannot mistake printed error
JSON for success.

## 5. Optional filesystem backup target

The backup layer knows only filesystem paths. It has no Google Drive, Dropbox,
iCloud, or other provider API; it stores no OAuth token or provider credential.
A target may be located inside a provider's desktop sync folder, but provider
upload happens outside this project's authority.

Configure or clear the path explicitly:

```bash
.venv/bin/python scripts/attachment_backup.py set-target "<filesystem-target>"
.venv/bin/python scripts/attachment_backup.py clear-target
```

The target layout is provider-neutral:

```text
<target>/v2/
  objects/sha256/<first-two-hex>/<sha256>
  snapshots/<snapshot-id>/manifest.json
  snapshots/<snapshot-id>/catalog.json
  snapshots/<snapshot-id>/COMPLETE
  receipts/<snapshot-id>.json
```

The archive root now owns a provider-neutral `cas_catalog.db` containing one
SHA-256 object identity and content-bounded source bindings. Both the Knowledge
attachment lane and the selected-chat Drive lane write that catalog without
duplicating bytes or creating a second object identity. Filesystem snapshots
take an authoritative union with the Knowledge attachment catalog, so a
Drive-only selected-chat object that never hit KnowledgeStore participates in
plan/run/verify and DB-free restore planning while existing topic/event bindings
and privacy-bounded exports remain intact.

`plan` takes a stable read view of provider-neutral CAS objects and the privacy-bounded
attachment catalog, then checks which target objects are missing,
`target_verified`, or `target_failed` without writing the target.
`run` copies missing immutable objects through partial files, verifies source
hashes, and atomically publishes target objects. It writes `manifest.json` plus
`catalog.json`, then publishes `COMPLETE` last only when every object succeeded.
The catalog contains object SHA-256/size, original name/type,
`source_message_id`, numeric topic/event bindings, status, and resolution
method. It deliberately excludes raw chat bodies, `wxid` values, and WeChat
cache/archive paths. A content-free failed receipt is still written for
reconciliation. Existing target objects are never overwritten when their bytes
conflict, and unmanaged target files are never pruned.

Both `plan` and `run` reject a filesystem root or a target that is the same as,
inside, or an ancestor of the local archive/database. The checks compare both
lexical and resolved paths, so symlink escapes into a protected source are also
rejected before any target write.

```bash
.venv/bin/python scripts/attachment_backup.py status
.venv/bin/python scripts/attachment_backup.py plan
.venv/bin/python scripts/attachment_backup.py run
.venv/bin/python scripts/attachment_backup.py verify
.venv/bin/python scripts/attachment_backup.py verify --snapshot-id <id>
.venv/bin/python scripts/attachment_backup.py restore-plan
.venv/bin/python scripts/attachment_backup.py restore-plan --snapshot-id <id>
```

`verify` re-hashes objects visible at the target. It reports `target_verified`
or `target_failed`; it does not report or imply cloud-upload verification. `restore-plan`
is read-only and reports how many verified target objects are absent or invalid
locally. It reads the snapshot catalog and scans the local CAS directly, so the
plan still works when the local Knowledge database/catalog is absent. This
tranche deliberately provides no automatic restore or deletion.

## 6. Health and safe rollout

The redacted health check reports source-guard effective state, last result,
remaining restart budget, source freshness, attachment catalog counts/object
count, optional backup snapshots, and privacy-safe direct Drive state: auth,
enabled/paused, selected-chat count, queue counts, last scan/upload, next retry,
root state, object/shortcut counts, and last error code:

```bash
.venv/bin/python scripts/health_check.py
```

A safe first direct-Drive rollout is:

1. obtain your own Google Cloud Installed desktop app OAuth client JSON;
2. run `auth`, then confirm `auth-status` without enabling sync;
3. choose chats in the menu and review the stable aliases that will be visible
   in Drive;
4. run historical `backfill --from ...` without `--apply` if history is wanted;
5. run `enable`, then one explicit `run`, and inspect `status` plus Drive root;
6. apply historical backfill only after reviewing its counts; and
7. run the redacted health check after activation.

The source guard and filesystem snapshot have their own rollouts: inspect their
status/plan first and keep them disabled or unconfigured until separately
reviewed.

Installing/loading the source guard, authorizing Google, choosing chats,
enabling direct sync, applying either historical backfill, writing a real
filesystem target, pruning WeChat cache data, and deleting Drive files are all
separate operational actions. Source availability, local preservation,
filesystem target-byte verification, and verified Drive object/shortcut state
are distinct facts.
