# Repository agent contract

## Source ownership

- `app.py` is the macOS menu-bar and py2app application entrypoint.
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
- `scripts/` contains thin operator entrypoints and compatibility cleanup
  commands. Put
  reusable behavior in the owning package rather than duplicating it in a CLI.
- Source-guard and mounted-resource timers run inside the long-lived py2app
  menu-bar process. macOS App Data consent is process-lifetime access, so their
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
- `tests/` is an importable unittest package. New tests belong there and use
  `tests.<module>` for focused invocation.

## Windows port staging

- The full Windows programme contract, `WGO-WIN-SPEC-1`, is owner-authorized
  external material. For `PR-W0.1`, `docs/WINDOWS-PORT-MAP.md` is the sole
  in-repository executable authority: portability mapping, platform
  contracts/factory, dependency markers, Windows import/compile CI and
  documentation only.
- `app.py` remains the macOS shell. W0.1 must not add Windows source reads, key
  acquisition, monitor activation, attachment/backup behavior, tray UI,
  autostart, packaging or message sending.
- `core/platform/` owns behavior-free platform contracts and fail-closed
  provider selection. Concrete lock/path/private-storage adapters begin in
  W0.2; secrets/open/notification adapters begin in W0.3; source adapters begin
  in W1.
- `docs/WINDOWS-PORT-MAP.md` is the W0.1 module inventory. Every root,
  `ai/`, `core/`, `ui/` and `scripts/` Python module must remain classified,
  and only modules marked `windows-import-safe` enter the Windows import gate.
  Import success is not evidence of Windows feature support.

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

Use focused `tests.<module>` runs while iterating, then the full suite for
shared code, packaging, public-boundary or runtime changes. Source completion,
private/public publication, app-bundle rebuild, LaunchAgent reload and live
acceptance are separate gates. Do not mutate live config/data or reload a live
agent merely because source tests pass.

For W0.1 on Windows, also run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.windows tests.test_repository_layout
.\.venv\Scripts\python.exe -m compileall -q mcp_server.py ai core ui scripts tests
```

The portability workflow is the macOS regression authority; a Windows host
cannot waive or simulate that gate.

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
