# Automation CLI Commands

The Automation app exposes commands under the `automation` scope.

## Commands

### `tutti automation list`

List automation task definitions.

### `tutti automation get`

Get one automation task definition by id or exact name.

Examples:

```sh
tutti automation get --automation-id aut_123
tutti automation get --name "Daily review"
```

### `tutti automation create`

Create an automation task definition.

Examples:

```sh
tutti automation create --name "Daily review" --prompt "Review today's changes"
tutti automation create --name "Hourly triage" --prompt "Triage open issues" --agent-id <agent-target-id> --schedule-type interval --interval-minutes 60
tutti automation create --name "Weekday report" --prompt "Write a status report" --agent-id <agent-target-id> --schedule-type weekly --days-of-week 1,2,3,4,5 --time-of-day 09:00
```

Schedule arguments:

- `--schedule-type interval|daily|weekly|cron`
- Omit schedule arguments to use the UI default schedule: daily at 09:00.
- `--interval-minutes 60`
- `--time-of-day 09:00`
- `--days-of-week 1,2,3,4,5`
- `--cron "0 9 * * 1"`

Runner arguments:

- `--agent-id <agent-target-id>` is optional when creating a task. Omit it to use the host default Agent.
- Discover currently supported Agent Targets and their availability with `tutti agent list --json`.
- `--model <model-id>` is optional when the selected Agent supports its runtime default. Discover Agent options with `tutti agent composer-options --agent-id <agent-target-id> --json`.
- `--provider <provider-id>` is deprecated compatibility input for old hosts. It is accepted only when the provider maps to exactly one Agent Target in the full catalog; otherwise Automation fails closed and requires `--agent-id`.
- `--reasoning-effort high`
- `--permission-mode full-access`
- `--runner-args "--model <model-id>"`
- `--env KEY=value,OTHER=value`

Automation app tasks must be persisted with `tutti automation` commands. Do not
substitute provider-native cron/reminder tools, OS cron/launchd, shell sleep
loops, or background scripts when the user asked to create an Automation task.

### `tutti automation update`

Update one automation task definition by id. Omitted fields keep their current values.

Examples:

```sh
tutti automation update --automation-id aut_123 --name "Daily repo review"
tutti automation update --automation-id aut_123 --enabled false
tutti automation update --automation-id aut_123 --schedule-type cron --cron "0 9 * * 1"
```

### `tutti automation delete`

Delete one automation task definition and its run history by id.

Examples:

```sh
tutti automation delete --automation-id aut_123
```

### `tutti automation run`

Trigger one automation task immediately by id or exact name.

Examples:

```sh
tutti automation run --automation-id aut_123
tutti automation run --name "Daily review"
```

### `tutti automation runs`

List recent automation task runs, optionally filtered by automation task id.

Examples:

```sh
tutti automation runs
tutti automation runs --automation-id aut_123 --limit 20
```

The table includes the exact Agent Target id snapshotted when each run was
queued. JSON output exposes the same value on each table row as `agent-id`.

### `tutti automation complete-run`

Submit the final structured status for a running automation task.

This command is intended for automation task runner prompts. It updates only the
matching running run's task status. The user-facing result should still be sent
as the agent's normal final Markdown response.
