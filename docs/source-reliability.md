# Source reliability

This guide covers three deliberately separate responsibilities:

1. the optional WeChat source guard, which may request a normal macOS
   application launch;
2. the attachment catalog and private local content-addressed archive; and
3. an optional provider-neutral filesystem snapshot target.

They do not form a single always-on daemon. `TopicMonitor` reads messages and
owns checkpoints; it does not import or invoke the source guard. The Knowledge
transaction owns attachment mentions; a post-commit worker owns byte copying.
The backup command reads immutable local archive objects and writes only to the
configured filesystem target.

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

## 4. Optional filesystem backup target

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

`plan` takes a stable read view of `attachment_objects` and the privacy-bounded
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

## 5. Health and safe rollout

The redacted health check reports source-guard effective state, last result,
remaining restart budget, source freshness, attachment catalog counts/object
count, and whether an optional backup target has complete snapshots:

```bash
.venv/bin/python scripts/health_check.py
```

A safe first rollout is:

1. inspect `wechat_source_guard.py status` and `attachment_archive.py status`;
2. leave source guard disabled until its policy values are reviewed;
3. run attachment historical `backfill` in plan mode;
4. configure a backup target and run `attachment_backup.py plan`;
5. use each explicit apply/run/load action only after reviewing the plan and
   local privacy implications; and
6. run health plus backup `verify` after any activated change.

Installing or loading the source guard, applying historical backfill, writing a
real backup target, pruning WeChat cache data, or asserting provider upload are
all separate operational actions. Source availability, local preservation,
target-byte verification, and remote sync are distinct facts.
