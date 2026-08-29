# Repository agent contract

## Source ownership

- `app.py` is the shared macOS menu-bar / Windows tray application entrypoint
  and remains the py2app target on macOS. `ui/windows_rumps.py` is only a thin
  compatibility surface; do not fork the application workflow by platform.
- `mcp_server.py` is the direct FastMCP entrypoint.
- `setup.py` is the py2app packaging entrypoint. These are the only Python
  files that belong at repository root.
- `core/` owns domain behavior, durable state, privacy boundaries, recovery,
  backup and projection contracts.
- `core/config.py::ConfigStore` is the sole main-config write authority. Writers
  patch the latest locked revision; do not reintroduce whole-snapshot UI/CLI
  saves or non-atomic config writes. `core/app_runtime.py` owns the menu-app
  process singleton.
- `ai/` owns provider adapters; `ui/` owns reusable UI components.
- Windows platform boundaries live in narrowly named `core/windows_*.py` or
  generic `core/platform_*.py` adapters. Windows raw-key import must feed the
  original verified per-database key map and `WeChatDB` decryption path. The raw
  key may be retained only after explicit autostart enrollment and only in
  Windows Credential Manager; never place it in config, logs, task arguments or
  the repository, and never replace the canonical database/summary implementation.
- `scripts/` contains thin operator entrypoints and compatibility cleanup
  commands. Put
  reusable behavior in the owning package rather than duplicating it in a CLI.
- Source-guard and mounted-resource timers run inside the long-lived menu-bar
  process on supported platforms. Source guard and its App Data consent contract
  are macOS-only, so Windows must keep that timer disabled. Their
  retired short-lived LaunchAgent modes must remain no-op cleanup surfaces and
  must not be reintroduced as Python or app-bundle interval workers.
- Explicit resource CLI source operations remain operator entrypoints, but app
  and CLI capture/backfill runs share the resource capture operation lock.
  Historical backfill is staged: plan writes bounded keyset pages and apply
  requires the exact unexpired `run_id`; never restore a confirm-then-rescan
  path.
- `launchers/` owns the canonical Finder-friendly `.command` entrypoints. The
  root `启动.command` is a compatibility stub for deployed source-mode
  LaunchAgents and must not grow a second implementation.
- `launchers/启动.ps1` owns Windows setup/start orchestration and root `启动.cmd`
  is its thin double-click stub. Keep `.cmd` as UTF-8 without BOM plus CRLF and
  `.ps1` as UTF-8 with BOM plus CRLF for Windows PowerShell 5.1/non-ASCII paths.
  Windows login autostart is a current-user Task Scheduler definition managed
  only by `scripts/windows_autostart.py`; it must keep duplicate instances out,
  use bounded abnormal-exit restart, and contain no credential. Implementation
  and verification must not silently install it.
- `tests/` is an importable unittest package. New tests belong there and use
  `tests.<module>` for focused invocation.

## Durable and generated boundaries

- SQLite/CAS ledgers are authoritative for durable derived state. Markdown,
  indexes, digests, target views and SVG exports are rebuildable projections.
- Attachment-byte consent is process/session-local and must never be persisted.
  WeChat decrypted caches and source shard/message identities are namespaced by
  source root; plaintext SQLite snapshots use Online Backup so WAL state is not
  lost.
- Resource projection manifests own generated-path GC. Empty selections still
  render an explicit root; GC may remove only app-owned generated files and must
  hold canonical capture/selection authority, then the DB-scoped backup lock
  plus the real-path-keyed output-root lock. Mounted handoff also takes its
  target-side lock. Distinct capture databases/path aliases cannot concurrently
  own the same projection or mount, and local generated descendants must not
  follow symlinks.
- Editable architecture truth lives in `docs/architecture/*.excalidraw`; SVGs
  under `docs/assets/architecture/` are portable generated exports and must be
  regenerated and visually inspected after source changes.
- Runtime config, WeChat keys/cache, chat-derived databases, logs, Obsidian
  output, OAuth material, mounted-target receipts and attachment bytes never
  belong in Git.
- Public publication must preserve public-safe defaults and exclude raw chat
  identities/bodies, account identifiers, local paths, credentials, private
  continuity and live runtime data.
- `scripts/build_share_package.py` packages the exact Git commit tree. Its
  generated guide comes from an exact-commit tracked template and is mode/hash
  bound under manifest `controls`; the no-Git path is manifest-only and
  hash-verifies regular non-symlink payload/control entries. Do not reintroduce
  recursive fallback scanning or live-runtime control text.

## Verification and deployment

Run from repository root:

```bash
.venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'
.venv/bin/python -m compileall -q app.py mcp_server.py setup.py ai core ui scripts tests
for launcher in 启动.command launchers/*.command; do bash -n "$launcher"; done
```

On Windows, also run from the checkout root:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_windows_key_extractor tests.test_windows_rumps tests.test_windows_launcher tests.test_windows_autostart tests.test_windows_runtime tests.test_windows_console
.\.venv\Scripts\python.exe -m compileall -q app.py mcp_server.py setup.py ai core ui scripts tests
cmd.exe /d /c "启动.cmd --setup-only --yes --no-pause"
```

Windows source acceptance is separate from synthetic decryption coverage: the
privacy-safe readiness command may exit `2` until the operator privately supplies
a valid raw key. Never print or commit raw keys, page keys, account paths, chat
content or runtime config while diagnosing that gate.

Use focused `tests.<module>` runs while iterating, then the full suite for
shared code, packaging, public-boundary or runtime changes. Source completion,
private/public publication, app-bundle rebuild, LaunchAgent reload and live
acceptance are separate gates. Do not mutate live config/data or reload a live
agent merely because source tests pass.

## Review ref resolution

Before producing a code-review finding, resolve and print `repository`,
`requested_ref`, `resolved_sha`, `default_branch_sha`, `merge_base`, and
`applies_to`. When Faye supplies a named active branch, PR branch, or review
URL, inspect that exact immutable SHA. Never transfer a finding from the
default branch to the active branch, and never fall back to `main` when the
requested ref cannot be resolved; fail closed instead. Every finding title
must include `applies_to=<ref>@<sha>`. If a default-branch defect is already
closed on a verified active branch, label both facts explicitly.

## Documentation triggers

Update README EN/ZH when entrypoints, supported behavior, privacy boundaries,
installation, repository layout or ordinary commands change. Update operator
guides for changed procedures and this file when source ownership, required
verification or deployment gates change.
