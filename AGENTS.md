# Repository agent contract

## Source ownership

- `app.py` is the macOS menu-bar and py2app application entrypoint.
- `mcp_server.py` is the direct FastMCP entrypoint.
- `setup.py` is the py2app packaging entrypoint. These are the only Python
  files that belong at repository root.
- `core/` owns domain behavior, durable state, privacy boundaries, recovery,
  backup and projection contracts.
- `ai/` owns provider adapters; `ui/` owns reusable UI components.
- `scripts/` contains thin operator and LaunchAgent one-shot entrypoints. Put
  reusable behavior in the owning package rather than duplicating it in a CLI.
- Scheduled source-guard and mounted-resource jobs prefer one-shot modes of the
  local py2app executable when it exists, preserving the stable macOS app/TCC
  identity. The source Python path is a compatibility fallback, not the
  preferred installed runtime.
- `launchers/` owns the canonical Finder-friendly `.command` entrypoints. The
  root `启动.command` is a compatibility stub for deployed source-mode
  LaunchAgents and must not grow a second implementation.
- `tests/` is an importable unittest package. New tests belong there and use
  `tests.<module>` for focused invocation.

## Durable and generated boundaries

- SQLite/CAS ledgers are authoritative for durable derived state. Markdown,
  indexes, digests, target views and SVG exports are rebuildable projections.
- Editable architecture truth lives in `docs/architecture/*.excalidraw`; SVGs
  under `docs/assets/architecture/` are portable generated exports and must be
  regenerated and visually inspected after source changes.
- Runtime config, WeChat keys/cache, chat-derived databases, logs, Obsidian
  output, OAuth material, mounted-target receipts and attachment bytes never
  belong in Git.
- Public publication must preserve public-safe defaults and exclude raw chat
  identities/bodies, account identifiers, local paths, credentials, private
  continuity and live runtime data.

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

## Documentation triggers

Update README EN/ZH when entrypoints, supported behavior, privacy boundaries,
installation, repository layout or ordinary commands change. Update operator
guides for changed procedures and this file when source ownership, required
verification or deployment gates change.
