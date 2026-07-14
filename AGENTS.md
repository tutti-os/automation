# AGENTS.md

## Scope

This file applies to the whole `automation` repository.

## Layout

- `web/`: React frontend (Vite)
- `static/`: Vite build output consumed by `server.py` (generated, not committed)
- `server.py`: Python HTTP server and automation runtime
- `bootstrap.sh`: Tutti app entrypoint
- `tutti.app.json` / `tutti.cli.json`: App Center and CLI manifests

## Development

- Install: `pnpm install`
- Browser UI dev with mock JSB + local API, best default: `pnpm dev:full`
- Frontend-only dev server: `pnpm dev`
- Local API server only: `pnpm dev:server`
- Tutti host local catalog dev loop, recommended for App Center/runtime validation: `pnpm dev:tutti`
- Repackage the local catalog app after edits: `pnpm dev:tutti:reload`
- Tutti host static watch for the normal app id: `pnpm dev:host`
- Tutti host source-backed debug package, recommended for host validation: `pnpm dev:host:next`
- Build frontend into `static/`: `pnpm build:web`
- Package Tutti app: `pnpm package:tutti-app`
- Package source-backed debug app: `pnpm package:tutti-app:next`
- Python server tests: `pnpm test:server`

### Dev modes

Use `pnpm dev:full` first unless the change specifically needs the real Tutti
Desktop host, webview bridge, App Center install flow, or workspace app runtime.
It runs:

- API/runtime server: `server.py` on `http://127.0.0.1:8787`
- Frontend: Vite on `http://127.0.0.1:5173`
- Data/log/runtime state under `.dev/`

Vite proxies `/api` and `/tutti` to the local server. Frontend edits hot reload
in the browser. `server.py` edits require restarting `pnpm dev:full`. In dev,
the frontend installs a mock `window.tuttiExternal` only when the host has not
already injected one.

Use `pnpm dev:tutti` for realistic Tutti Desktop validation through a local App
Center catalog. It packages the app as a dev version, serves the archive from a
local HTTP server, writes `dist/tutti-app/dev-catalog.json`, and starts Tutti
with `TUTTI_APP_CATALOG_FILE` pointing at that catalog. Use
`pnpm dev:tutti:reload` after frontend or backend edits, then refresh the App
Center catalog and update/reinstall Automation when prompted.

Use `pnpm dev:host:next` for realistic Tutti Desktop validation while keeping
backend edits source-backed. It packages and watches a separate debug app:

- package root: `dist/tutti-app/automation-next`
- import archive: `dist/tutti-app/automation-next.zip`
- app id: `automation-next`
- display name: `Automation Next`

Import `dist/tutti-app/automation-next.zip` once from Tutti Desktop App Center.
Keep `pnpm dev:host:next` running. Frontend edits rebuild into `static/`; refresh
the app webview after each rebuild. Backend edits run this repo's `server.py`,
so restart the Automation Next app after changing server code.

Use `pnpm dev:host` only when validating the normal `automation` app id. It
rebuilds `static/` on file changes. If the installed package is a copied archive
from `dist/tutti-app/automation`, set:

```sh
export TUTTI_AUTOMATION_STATIC_DIR=/Users/ryan/dev/nexight/automation/static
```

Then launch Tutti Desktop from the same shell so the app process inherits the
override. This makes frontend static assets come from the repository. Backend
edits still require restarting the app, and copied package installs may require
repackaging/reimporting unless the host import flow points at the repo source.

`server.py` serves `TUTTI_AUTOMATION_STATIC_DIR` when present; otherwise it
serves `TUTTI_APP_PACKAGE_DIR/static`.

Use the smaller commands only for focused work:

- `pnpm dev`: Vite frontend only; expects an API target at
  `AUTOMATION_DEV_API_TARGET` or `http://127.0.0.1:8787`
- `pnpm dev:server`: server only, with Tutti runtime env vars filled for local
  browser testing
- `pnpm dev:static`: static frontend watch into `static/`

### UI System

The frontend uses `@tutti-os/ui-system` for shared primitives (Button, Dialog, DropdownMenu, Popover, etc.).

- Entry imports: `@tutti-os/ui-system/styles.css`, then `web/src/style.css` (Tailwind v4), then `web/src/styles.css` (Automation layout).
- Vite plugins: `@tailwindcss/vite` and `tuttiUISystemDev()` from `@tutti-os/ui-system/dev-vite`.
- Optional live ui-system source sync: run `pnpm --filter @tutti-os/ui-system dev:server` in the nextop repo, then start `pnpm dev`. Without the dev server, Vite falls back to the linked package in `node_modules`.

When changing user-visible copy, update `web/src/i18n/messages/en.json` and `web/src/i18n/messages/zh-CN.json` together.

When changing CLI commands, keep `tutti.cli.json`, `COMMANDS.md`, and the `/tutti/cli/*` handlers in `server.py` synchronized.

### Agent identity

Automation task definitions and runs use the exact `agentTargetId` returned by
`tutti agent list --json` as their durable Agent identity. Provider ids are
runtime metadata and a deprecated compatibility input only. A legacy provider
may be resolved only when it maps to exactly one Agent Target in the full
catalog; ambiguous mappings must fail closed. Starting and composing use
`--agent-id`, except when an old daemon rejects exactly `agent list` and the
legacy catalog has a unique provider mapping. Resume, summary, and open flows
must validate that the session's `agentTargetId` matches the persisted run.
