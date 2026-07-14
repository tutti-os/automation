import json
import os
import queue
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PACKAGE_DIR = Path(os.environ["TUTTI_APP_PACKAGE_DIR"])
STATIC_DIR = Path(os.environ.get("TUTTI_AUTOMATION_STATIC_DIR") or (PACKAGE_DIR / "static")).expanduser().resolve()
DATA_DIR = Path(os.environ["TUTTI_APP_DATA_DIR"])
LOG_DIR = Path(os.environ["TUTTI_APP_LOG_DIR"])
RUNTIME_DIR = Path(os.environ["TUTTI_APP_RUNTIME_DIR"])
WORKSPACE_ROOT = os.environ.get("TUTTI_WORKSPACE_ROOT", "").strip()
WORKSPACE_ID = os.environ["TUTTI_WORKSPACE_ID"]
WORKSPACE_NAME = os.environ.get("TUTTI_WORKSPACE_NAME", WORKSPACE_ID)
DB_PATH = DATA_DIR / "automation.sqlite3"
LEGACY_TIMEOUT_SECONDS = 0
MISSING_TASK_STATUS_ERROR = "Automation task did not submit a task status."
COMPLETION_GRACE_SECONDS = int(os.environ.get("TUTTI_AUTOMATION_COMPLETION_GRACE_SECONDS", "60") or "60")
COMPLETION_GRACE_POLL_SECONDS = 0.5
FINAL_SUMMARY_GRACE_SECONDS = int(os.environ.get("TUTTI_AUTOMATION_FINAL_SUMMARY_GRACE_SECONDS", "10") or "10")
FINAL_SUMMARY_POLL_SECONDS = 0.5
RUN_TIMEOUT_SECONDS = int(os.environ.get("TUTTI_AUTOMATION_RUN_TIMEOUT_SECONDS", "1800") or "1800")
PROJECT_MARKERS = ("package.json", "go.mod", "pyproject.toml", "Cargo.toml", ".git")
RESULT_STATUS_VALUES = {"success", "fail", "skip"}
RUNNER_OPTIONS_COMPOSER_TIMEOUT_SECONDS = float(
    os.environ.get("TUTTI_AUTOMATION_RUNNER_OPTIONS_COMPOSER_TIMEOUT_SECONDS", "8") or "8"
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def automation_id():
    return "aut_" + uuid.uuid4().hex[:16]


def run_id():
    return "run_" + uuid.uuid4().hex[:16]


def run_artifact_dir(automation_id, run_id_):
    return LOG_DIR / "runs" / automation_id / run_id_


def connect_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.db = connect_db()
        self.migrate()
        self.recover_interrupted_runs()

    def migrate(self):
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS automations (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  prompt TEXT NOT NULL,
                  cwd TEXT NOT NULL,
                  enabled INTEGER NOT NULL,
                  schedule_type TEXT NOT NULL,
                  schedule_json TEXT NOT NULL,
                  concurrency TEXT NOT NULL,
                  timeout_seconds INTEGER NOT NULL,
                  runner_settings_json TEXT NOT NULL DEFAULT '{}',
                  runner_args_json TEXT NOT NULL,
                  env_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  next_run_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY,
                  automation_id TEXT NOT NULL,
                  trigger TEXT NOT NULL,
                  status TEXT NOT NULL,
                  prompt TEXT NOT NULL,
                  cwd TEXT NOT NULL,
                  queued_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  exit_code INTEGER,
                  summary TEXT,
                  error TEXT,
                  result_status TEXT,
                  artifact_dir TEXT NOT NULL,
                  agent_session_id TEXT,
                  agent_target_id TEXT,
                  agent_provider TEXT,
                  reviewed_at TEXT,
                  FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE
                );
                """
            )
            self.ensure_column("runs", "result_status", "TEXT")
            self.ensure_column("runs", "agent_session_id", "TEXT")
            self.ensure_column("runs", "agent_target_id", "TEXT")
            self.ensure_column("runs", "agent_provider", "TEXT")
            self.ensure_column("automations", "runner_settings_json", "TEXT NOT NULL DEFAULT '{}'")

    def ensure_column(self, table, column, definition):
        columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def recover_interrupted_runs(self):
        with self.lock:
            stamp = now_iso()
            self.db.execute(
                """
                UPDATE runs
                SET status='failed', finished_at=?, error='The app restarted before this run finished.'
                WHERE status IN ('queued', 'running', 'canceling')
                """,
                (stamp,),
            )
            self.db.commit()

    def list_automations(self):
        with self.lock:
            rows = self.db.execute(
                """
                SELECT automations.*,
                  (
                    SELECT started_at FROM runs
                    WHERE runs.automation_id=automations.id AND started_at IS NOT NULL
                    ORDER BY started_at DESC LIMIT 1
                  ) AS last_run_at,
                  (
                    SELECT id FROM runs
                    WHERE runs.automation_id=automations.id AND status IN ('queued', 'running', 'canceling')
                    ORDER BY queued_at DESC LIMIT 1
                  ) AS active_run_id,
                  (
                    SELECT status FROM runs
                    WHERE runs.automation_id=automations.id AND status IN ('queued', 'running', 'canceling')
                    ORDER BY queued_at DESC LIMIT 1
                  ) AS active_run_status,
                  (
                    SELECT COUNT(*) FROM runs
                    WHERE runs.automation_id=automations.id AND finished_at IS NOT NULL AND reviewed_at IS NULL
                      AND COALESCE(result_status, status) != 'skip'
                  ) AS unreviewed_run_count,
                  (
                    SELECT COUNT(*) FROM runs
                    WHERE runs.automation_id=automations.id AND finished_at IS NOT NULL AND reviewed_at IS NULL
                      AND COALESCE(result_status, status) IN ('fail', 'failed', 'timed_out')
                  ) AS unreviewed_failed_run_count
                FROM automations
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [decode_automation(row) for row in rows]

    def get_automation(self, id_):
        with self.lock:
            row = self.db.execute(
                """
                SELECT automations.*,
                  (
                    SELECT started_at FROM runs
                    WHERE runs.automation_id=automations.id AND started_at IS NOT NULL
                    ORDER BY started_at DESC LIMIT 1
                  ) AS last_run_at,
                  (
                    SELECT id FROM runs
                    WHERE runs.automation_id=automations.id AND status IN ('queued', 'running', 'canceling')
                    ORDER BY queued_at DESC LIMIT 1
                  ) AS active_run_id,
                  (
                    SELECT status FROM runs
                    WHERE runs.automation_id=automations.id AND status IN ('queued', 'running', 'canceling')
                    ORDER BY queued_at DESC LIMIT 1
                  ) AS active_run_status,
                  (
                    SELECT COUNT(*) FROM runs
                    WHERE runs.automation_id=automations.id AND finished_at IS NOT NULL AND reviewed_at IS NULL
                      AND COALESCE(result_status, status) != 'skip'
                  ) AS unreviewed_run_count,
                  (
                    SELECT COUNT(*) FROM runs
                    WHERE runs.automation_id=automations.id AND finished_at IS NOT NULL AND reviewed_at IS NULL
                      AND COALESCE(result_status, status) IN ('fail', 'failed', 'timed_out')
                  ) AS unreviewed_failed_run_count
                FROM automations
                WHERE id=?
                """,
                (id_,),
            ).fetchone()
            return decode_automation(row) if row else None

    def save_automation(self, item):
        with self.lock:
            self.db.execute(
                """
                INSERT INTO automations (
                  id, name, prompt, cwd, enabled, schedule_type, schedule_json,
                  concurrency, timeout_seconds, runner_settings_json, runner_args_json, env_json,
                  created_at, updated_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  prompt=excluded.prompt,
                  cwd=excluded.cwd,
                  enabled=excluded.enabled,
                  schedule_type=excluded.schedule_type,
                  schedule_json=excluded.schedule_json,
                  concurrency=excluded.concurrency,
                  timeout_seconds=excluded.timeout_seconds,
                  runner_settings_json=excluded.runner_settings_json,
                  runner_args_json=excluded.runner_args_json,
                  env_json=excluded.env_json,
                  updated_at=excluded.updated_at,
                  next_run_at=excluded.next_run_at
                """,
                (
                    item["id"],
                    item["name"],
                    item["prompt"],
                    item["cwd"],
                    1 if item["enabled"] else 0,
                    item["scheduleType"],
                    json.dumps(item["schedule"], separators=(",", ":")),
                    item["concurrency"],
                    LEGACY_TIMEOUT_SECONDS,
                    json.dumps(item["runnerSettings"], separators=(",", ":")),
                    json.dumps(item["runnerArgs"], separators=(",", ":")),
                    json.dumps(item["env"], separators=(",", ":")),
                    item["createdAt"],
                    item["updatedAt"],
                    item.get("nextRunAt"),
                ),
            )
            self.db.commit()
            return self.get_automation(item["id"])

    def advance_automation_schedule(self, snapshot, updated_at, next_run_at):
        """Advance only the unchanged row observed by the scheduler scan."""
        raw = snapshot.get("_schedulerSnapshot")
        if not isinstance(raw, dict):
            raise ValueError("automation is missing its scheduler snapshot")
        with self.lock:
            result = self.db.execute(
                """
                UPDATE automations
                SET updated_at=?, next_run_at=?
                WHERE id=?
                  AND name=?
                  AND prompt=?
                  AND cwd=?
                  AND enabled=?
                  AND schedule_type=?
                  AND schedule_json=?
                  AND concurrency=?
                  AND runner_settings_json=?
                  AND runner_args_json=?
                  AND env_json=?
                  AND created_at=?
                  AND updated_at=?
                  AND next_run_at IS ?
                """,
                (
                    updated_at,
                    next_run_at,
                    raw["id"],
                    raw["name"],
                    raw["prompt"],
                    raw["cwd"],
                    raw["enabled"],
                    raw["schedule_type"],
                    raw["schedule_json"],
                    raw["concurrency"],
                    raw["runner_settings_json"],
                    raw["runner_args_json"],
                    raw["env_json"],
                    raw["created_at"],
                    raw["updated_at"],
                    raw["next_run_at"],
                ),
            )
            self.db.commit()
            return result.rowcount > 0

    def delete_automation(self, id_):
        with self.lock:
            result = self.db.execute("DELETE FROM automations WHERE id=?", (id_,))
            self.db.commit()
            return result.rowcount > 0

    def set_automation_enabled(self, id_, enabled):
        item = self.get_automation(id_)
        if not item:
            return None
        item["enabled"] = bool(enabled)
        item["updatedAt"] = now_iso()
        item["nextRunAt"] = compute_next_run(item, datetime.now(timezone.utc))
        return self.save_automation(item)

    def list_due_automations(self):
        with self.lock:
            rows = self.db.execute(
                """
                SELECT * FROM automations
                WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now_iso(),),
            ).fetchall()
            return [
                {
                    **decode_automation(row),
                    "_schedulerSnapshot": scheduler_row_snapshot(row),
                }
                for row in rows
            ]

    def next_scheduled_run_at(self):
        with self.lock:
            rows = self.db.execute(
                """
                SELECT next_run_at FROM automations
                WHERE enabled=1 AND next_run_at IS NOT NULL
                """
            ).fetchall()
            times = [parse_iso(row["next_run_at"]) for row in rows]
            times = [value for value in times if value is not None]
            return min(times) if times else None

    def list_runs(self, automation_id=None, limit=100):
        with self.lock:
            if automation_id:
                rows = self.db.execute(
                    """
                    SELECT * FROM runs WHERE automation_id=?
                    ORDER BY queued_at DESC LIMIT ?
                    """,
                    (automation_id, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM runs ORDER BY queued_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [decode_run(row) for row in rows]

    def get_run(self, id_):
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (id_,)).fetchone()
            return decode_run(row) if row else None

    def save_run(self, item):
        with self.lock:
            self.db.execute(
                """
                INSERT INTO runs (
                  id, automation_id, trigger, status, prompt, cwd, queued_at,
                  started_at, finished_at, exit_code, summary, error, result_status, artifact_dir,
                  agent_session_id, agent_target_id, agent_provider, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  status=excluded.status,
                  started_at=excluded.started_at,
                  finished_at=excluded.finished_at,
                  exit_code=excluded.exit_code,
                  summary=excluded.summary,
                  error=excluded.error,
                  result_status=COALESCE(runs.result_status, excluded.result_status),
                  artifact_dir=excluded.artifact_dir,
                  agent_session_id=excluded.agent_session_id,
                  agent_target_id=excluded.agent_target_id,
                  agent_provider=excluded.agent_provider,
                  reviewed_at=excluded.reviewed_at
                """,
                (
                    item["id"],
                    item["automationId"],
                    item["trigger"],
                    item["runStatus"],
                    item["prompt"],
                    item["cwd"],
                    item["queuedAt"],
                    item.get("startedAt"),
                    item.get("finishedAt"),
                    item.get("exitCode"),
                    item.get("summary"),
                    item.get("error"),
                    item.get("taskStatus"),
                    item["artifactDir"],
                    item.get("agentSessionId"),
                    item.get("agentTargetId"),
                    item.get("agentProvider"),
                    item.get("reviewedAt"),
                ),
            )
            self.db.commit()
            return self.get_run(item["id"])

    def delete_queued_run(self, id_):
        with self.lock:
            result = self.db.execute(
                "DELETE FROM runs WHERE id=? AND status='queued'",
                (id_,),
            )
            self.db.commit()
            return result.rowcount > 0

    def complete_run(self, id_, result_status):
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (id_,)).fetchone()
            if not row:
                raise ValueError(f"run {id_} was not found")
            is_missing_status_failure = (
                row["status"] == "failed"
                and row["error"] == MISSING_TASK_STATUS_ERROR
            )
            if row["status"] != "running" and not is_missing_status_failure:
                raise ValueError("run is not accepting completion")
            if is_missing_status_failure:
                self.db.execute(
                    """
                    UPDATE runs
                    SET status='succeeded', error=NULL, result_status=?
                    WHERE id=?
                    """,
                    (result_status, id_),
                )
            else:
                self.db.execute(
                    """
                    UPDATE runs
                    SET result_status=?
                    WHERE id=?
                    """,
                    (result_status, id_),
                )
            self.db.commit()
            return self.get_run(id_)

    def has_active_run(self, automation_id):
        with self.lock:
            row = self.db.execute(
                """
                SELECT id FROM runs
                WHERE automation_id=? AND status IN ('queued', 'running', 'canceling')
                LIMIT 1
                """,
                (automation_id,),
            ).fetchone()
            return row["id"] if row else None


def decode_automation(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "prompt": row["prompt"],
        "cwd": row["cwd"],
        "enabled": bool(row["enabled"]),
        "scheduleType": row["schedule_type"],
        "schedule": json.loads(row["schedule_json"]),
        "concurrency": row["concurrency"],
        "runnerSettings": decode_runner_settings(
            row_get(row, "runner_settings_json"),
            row["runner_args_json"],
        ),
        "runnerArgs": json.loads(row["runner_args_json"]),
        "env": json.loads(row["env_json"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "nextRunAt": row["next_run_at"],
        "lastRunAt": row_get(row, "last_run_at"),
        "activeRunId": row_get(row, "active_run_id"),
        "activeRunStatus": row_get(row, "active_run_status"),
        "unreviewedRunCount": row_get(row, "unreviewed_run_count", 0),
        "unreviewedFailedRunCount": row_get(row, "unreviewed_failed_run_count", 0),
    }


def scheduler_row_snapshot(row):
    return {
        key: row[key]
        for key in (
            "id",
            "name",
            "prompt",
            "cwd",
            "enabled",
            "schedule_type",
            "schedule_json",
            "concurrency",
            "runner_settings_json",
            "runner_args_json",
            "env_json",
            "created_at",
            "updated_at",
            "next_run_at",
        )
    }


def decode_run(row):
    return {
        "id": row["id"],
        "automationId": row["automation_id"],
        "trigger": row["trigger"],
        "runStatus": row["status"],
        "prompt": row["prompt"],
        "cwd": row["cwd"],
        "queuedAt": row["queued_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "exitCode": row["exit_code"],
        "summary": row["summary"],
        "error": row["error"],
        "taskStatus": row_get(row, "result_status"),
        "artifactDir": row["artifact_dir"],
        "agentSessionId": row_get(row, "agent_session_id"),
        "agentTargetId": row_get(row, "agent_target_id"),
        "agentProvider": row_get(row, "agent_provider"),
        "reviewedAt": row["reviewed_at"],
    }


def row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def decode_runner_settings(settings_json, args_json=None):
    settings = {}
    if settings_json:
        try:
            parsed = json.loads(settings_json)
            if isinstance(parsed, dict):
                settings = parsed
        except Exception:
            settings = {}
    args = []
    if args_json:
        try:
            parsed_args = json.loads(args_json)
            if isinstance(parsed_args, list):
                args = parsed_args
        except Exception:
            args = []
    return normalize_runner_settings(settings, None, args)


def normalize_automation(payload, existing=None):
    stamp = now_iso()
    schedule_type = clean_choice(
        payload.get("scheduleType", existing["scheduleType"] if existing else "daily"),
        {"interval", "daily", "weekly", "cron"},
        "daily",
    )
    item = {
        "id": existing["id"] if existing else automation_id(),
        "name": clean_required(payload.get("name"), "name"),
        "prompt": clean_required(payload.get("prompt"), "prompt"),
        "cwd": clean_cwd(payload.get("cwd") or (existing["cwd"] if existing else "")),
        "enabled": clean_bool(
            payload.get("enabled", existing["enabled"] if existing else True),
            existing["enabled"] if existing else True,
        ),
        "scheduleType": schedule_type,
        "schedule": normalize_schedule(schedule_type, payload.get("schedule", {})),
        "concurrency": clean_choice(
            payload.get("concurrency", existing["concurrency"] if existing else "queue"),
            {"skip", "queue", "replace"},
            "queue",
        ),
        "runnerSettings": normalize_runner_settings(
            payload.get("runnerSettings"),
            existing["runnerSettings"] if existing else None,
            payload.get("runnerArgs", existing["runnerArgs"] if existing else []),
        ),
        "runnerArgs": clean_string_list(payload.get("runnerArgs", existing["runnerArgs"] if existing else [])),
        "env": clean_env(payload.get("env", existing["env"] if existing else {})),
        "createdAt": existing["createdAt"] if existing else stamp,
        "updatedAt": stamp,
    }
    item["nextRunAt"] = compute_next_run(item, datetime.now(timezone.utc))
    validate_runner_settings_for_automation(item["runnerSettings"])
    return item


def validate_runner_settings_for_automation(runner_settings):
    if not isinstance(runner_settings, dict):
        raise ValueError("agentTargetId is required for automation runner settings")
    agent_target_id = clean_agent_target_id(runner_settings.get("agentTargetId"))
    legacy_provider = normalize_provider_id(runner_settings.get("provider"))
    if not agent_target_id and not legacy_provider:
        raise ValueError("agentTargetId is required for automation runner settings")


def default_cli_runner_agent_target():
    catalog = agent_catalog_payload()
    agent_target_id = clean_agent_target_id(catalog.get("defaultAgentTargetId"))
    if not agent_target_id:
        raise ValueError(
            "no available Automation agents. Run `tutti agent list --json` to check Agent setup."
        )
    return agent_target_id


def normalize_runner_settings(value, existing=None, runner_args=None):
    value = value if isinstance(value, dict) else {}
    existing = existing if isinstance(existing, dict) else {}
    parsed_args = parse_runner_args(clean_string_list(runner_args or []))
    agent_target_id = clean_agent_target_id(
        value.get("agentTargetId") or existing.get("agentTargetId")
    )
    provider_id = normalize_provider_id(
        value.get("providerId") or existing.get("providerId")
    )
    legacy_provider = normalize_provider_id(
        value.get("provider") or (existing.get("provider") if not agent_target_id else None)
    )
    model = clean_optional_string(
        value.get("model") or existing.get("model") or parsed_args.get("model")
    )
    return {
        "agentTargetId": agent_target_id,
        "providerId": provider_id,
        **({"provider": legacy_provider} if legacy_provider and not agent_target_id else {}),
        "model": model,
        "reasoningEffort": clean_optional_string(
            value.get("reasoningEffort")
            or existing.get("reasoningEffort")
            or parsed_args.get("reasoningEffort")
        ),
        "permissionMode": clean_optional_string(
            value.get("permissionMode") or existing.get("permissionMode")
        ),
    }


def parse_runner_args(args):
    result = {"model": "", "reasoningEffort": ""}
    index = 0
    while index < len(args):
        arg = str(args[index] or "").strip()
        if arg in ("--model", "-m") and index + 1 < len(args):
            result["model"] = str(args[index + 1] or "").strip()
            index += 2
            continue
        if arg.startswith("--model="):
            result["model"] = arg.removeprefix("--model=").strip()
            index += 1
            continue
        if arg in ("--config", "-c") and index + 1 < len(args):
            read_reasoning_config(str(args[index + 1] or ""), result)
            index += 2
            continue
        if arg.startswith("--config="):
            read_reasoning_config(arg.removeprefix("--config="), result)
        index += 1
    return result


def read_reasoning_config(value, result):
    match = re.match(r"^model_reasoning_effort=(.*)$", str(value or "").strip())
    if match:
        result["reasoningEffort"] = match.group(1).strip().strip("\"'")


def normalize_schedule(schedule_type, value):
    value = value if isinstance(value, dict) else {}
    if schedule_type == "interval":
        return {"intervalMinutes": clean_int(value.get("intervalMinutes", 60), 1, 60 * 24 * 30)}
    if schedule_type == "daily":
        return {"timeOfDay": clean_time(value.get("timeOfDay"))}
    if schedule_type == "weekly":
        return {
            "timeOfDay": clean_time(value.get("timeOfDay")),
            "daysOfWeek": clean_days(value.get("daysOfWeek", [1])),
        }
    if schedule_type == "cron":
        return {"expression": clean_required(value.get("expression"), "cron expression")}
    return {}


def compute_next_run(item, after):
    if not item["enabled"]:
        return None
    schedule = item["schedule"]
    if item["scheduleType"] == "interval":
        if schedule["intervalMinutes"] == 60:
            local_after = after.astimezone()
            candidate = local_after.replace(
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(hours=1)
            return candidate.astimezone(timezone.utc).isoformat()
        return (after + timedelta(minutes=schedule["intervalMinutes"])).replace(microsecond=0).isoformat()
    if item["scheduleType"] in ("daily", "weekly"):
        local_after = after.astimezone()
        hour, minute = [int(part) for part in schedule["timeOfDay"].split(":")]
        days = schedule.get("daysOfWeek") if item["scheduleType"] == "weekly" else None
        for offset in range(0, 15):
            candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=offset)
            if candidate <= local_after:
                continue
            if days is None or candidate.isoweekday() in days:
                return candidate.astimezone(timezone.utc).isoformat()
    if item["scheduleType"] == "cron":
        return next_cron_time(schedule["expression"], after).isoformat()
    return None


def next_cron_time(expression, after):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression must contain five fields")
    current = after.astimezone().replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(0, 366 * 24 * 60):
        if cron_matches(fields, current):
            return current.astimezone(timezone.utc)
        current += timedelta(minutes=1)
    raise ValueError("cron expression has no match in the next year")


def cron_matches(fields, moment):
    values = [moment.minute, moment.hour, moment.day, moment.month, moment.isoweekday() % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(cron_field_matches(field, value, low, high) for field, value, (low, high) in zip(fields, values, ranges))


def cron_field_matches(field, value, low, high):
    if field == "*":
        return True
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = int(raw_step)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            raw_start, raw_end = part.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(part)
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def clean_required(value, label):
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} is required")
    return value


def clean_optional_string(value):
    if isinstance(value, bool):
        return ""
    return str(value or "").strip()


def clean_first_string(value, *keys):
    if not isinstance(value, dict):
        return ""
    for key in keys:
        text = clean_optional_string(value.get(key))
        if text:
            return text
    return ""


def clean_provider(value):
    return normalize_provider_id(value)


def clean_agent_target_id(value):
    return clean_optional_string(value)


def normalize_provider_id(value):
    return str(value or "").strip().lower()


def normalize_locale(value):
    value = str(value or "").strip().replace("_", "-").lower()
    if value == "zh" or value.startswith("zh-"):
        return "zh-CN"
    if value == "en" or value.startswith("en-"):
        return "en"
    return ""


def clean_cwd(value):
    value = str(value or WORKSPACE_ROOT or str(Path.home())).strip()
    if not value:
        raise ValueError("cwd is required")
    path = Path(value).expanduser()
    if not path.is_dir():
        raise ValueError("cwd must be an existing directory")
    return str(path)


def clean_choice(value, allowed, default):
    value = str(value or default).strip()
    return value if value in allowed else default


def clean_int(value, minimum, maximum):
    value = int(value)
    return max(minimum, min(maximum, value))


def clean_bool(value, default=False):
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError("boolean value must be true or false")


def clean_string_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def without_duplicate_runner_flags(args, flags):
    result = []
    index = 0
    while index < len(args):
        arg = str(args[index] or "").strip()
        if not arg:
            index += 1
            continue
        name = arg.split("=", 1)[0]
        if name in flags:
            index += 1
            if "=" not in arg and index < len(args) and not str(args[index]).startswith("-"):
                index += 1
            continue
        result.append(arg)
        index += 1
    return result


def tutti_cli_command():
    configured = os.environ.get("TUTTI_CLI", "").strip()
    if not configured:
        raise RuntimeError("TUTTI_CLI is not configured")
    return configured


AGENT_GET_POLL_LOG_FIELDS = (
    "agentSessionId",
    "status",
    "turnLifecycle",
    "submitAvailability",
    "taskStatus",
    "updatedAt",
    "lastError",
)
APPROVAL_REQUIRED_ERROR = (
    "Agent requested approval; automation runs cannot wait for interactive approval."
)
AGENT_GET_LOG_OMIT_FIELDS = (
    "runtimeContext",
    "messages",
    "skills",
    "configOptions",
    "settings",
    "permissionConfig",
)
MANUAL_AGENT_OPEN_RETRY_DELAYS_SECONDS = (0, 0.5, 1.5)


def is_agent_get_poll_args(args):
    return (
        len(args) >= 3
        and args[0] == "agent"
        and args[1] == "get"
        and args[2] == "--session-id"
    )


def compact_agent_get_log_stdout(stdout_text):
    original_bytes = len(stdout_text.encode("utf-8"))
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        return stdout_text, None

    session = payload.get("session") if isinstance(payload, dict) else None
    if not isinstance(session, dict):
        return stdout_text, None

    omitted = [field for field in AGENT_GET_LOG_OMIT_FIELDS if field in session]
    summary = {field: session.get(field) for field in AGENT_GET_POLL_LOG_FIELDS}
    compact_json = json.dumps(summary, ensure_ascii=False, indent=2)
    omitted_text = ",".join(omitted) if omitted else "none"
    meta_line = (
        f"[automation] stdout compacted: originalBytes={original_bytes} "
        f"omitted={omitted_text}\n"
    )
    return compact_json + "\n" + meta_line, {
        "originalBytes": original_bytes,
        "omitted": omitted,
    }


def write_cli_log_output(log_file, stdout_text, *, compact_stdout=False):
    if stdout_text:
        text_to_log = stdout_text
        if compact_stdout:
            text_to_log, _meta = compact_agent_get_log_stdout(stdout_text)
        log_file.write(text_to_log.encode("utf-8"))
        if not text_to_log.endswith("\n"):
            log_file.write(b"\n")


def run_tutti_cli(args, timeout=60, log_file=None):
    command_path = tutti_cli_command()
    command = [command_path, "--json", *args]
    compact_stdout = is_agent_get_poll_args(args)
    if log_file:
        log_file.write(("Command: " + " ".join(command) + "\n").encode("utf-8"))
        log_file.flush()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"TUTTI_CLI executable was not found: {command_path}") from exc
    if log_file:
        if result.stdout:
            write_cli_log_output(
                log_file,
                result.stdout,
                compact_stdout=compact_stdout and result.returncode == 0,
            )
        if result.stderr:
            log_file.write(result.stderr.encode("utf-8"))
            if not result.stderr.endswith("\n"):
                log_file.write(b"\n")
        log_file.flush()
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "tutti cli command failed").strip()
        raise RuntimeError(message)
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def start_agent_session(automation, run, log_file):
    title = automation["name"] or "Automation Task"
    runner_args = clean_string_list(automation.get("runnerArgs"))
    settings = normalize_runner_settings(
        automation.get("runnerSettings"),
        None,
        runner_args,
    )
    agent_target_id = clean_agent_target_id(run.get("agentTargetId"))
    provider_id = normalize_provider_id(run.get("agentProvider"))
    if not agent_target_id or not provider_id:
        raise RuntimeError("automation run does not contain an Agent Target snapshot")
    # The CLI contract and provider mapping may have changed while this run was
    # queued. Re-resolve the persisted exact target against the complete current
    # catalog immediately before launch; provider-compat must still be unique.
    catalog = agent_catalog_payload()
    current_target = resolve_agent_target_from_catalog(
        catalog,
        agent_target_id=agent_target_id,
        require_available=True,
    )
    if current_target["providerId"] != provider_id:
        raise RuntimeError(
            f"Agent Target provider changed while queued: expected {provider_id}, "
            f"got {current_target['providerId']}"
        )
    cli_contract = catalog["cliContract"]
    args = [
        "agent",
        "start",
        "--agent-id" if cli_contract == "agent-id" else "--provider",
        agent_target_id if cli_contract == "agent-id" else provider_id,
        "--cwd",
        run["cwd"],
        "--title",
        title,
        "--prompt",
        build_run_prompt(run["prompt"], run),
        "--display-prompt",
        build_run_display_prompt(automation, run),
    ]
    if run.get("trigger") == "manual":
        args.extend(["--show", "true"])
    else:
        args.extend(["--show", "false"])
    if settings.get("model"):
        args.extend(["--model", settings["model"]])
    if settings.get("reasoningEffort"):
        args.extend(["--reasoning-effort", settings["reasoningEffort"]])
    if settings.get("permissionMode"):
        args.extend(["--permission-mode", settings["permissionMode"]])
    duplicate_flags = {
        "--agent-id",
        "--provider",
        "--cwd",
        "--title",
        "--prompt",
        "--display-prompt",
        "--show",
        "--visible",
    }
    if settings.get("model"):
        duplicate_flags.update({"--model", "-m"})
    if settings.get("reasoningEffort"):
        duplicate_flags.update({"--reasoning-effort", "--config", "-c"})
    if settings.get("permissionMode"):
        duplicate_flags.add("--permission-mode")
    args.extend(
        without_duplicate_runner_flags(
            runner_args,
            duplicate_flags,
        )
    )
    session = run_tutti_cli(
        args,
        timeout=60,
        log_file=log_file,
    ).get("session") or {}
    assert_session_agent_target(
        session,
        agent_target_id,
        allow_provider_compat=cli_contract == "provider-compat",
        expected_provider_id=provider_id,
    )
    return {
        **session,
        "agentTargetId": agent_target_id,
        "provider": provider_id,
        "_automationCliContract": cli_contract,
    }


def get_agent_session(agent_session_id, expected_agent_target_id=None, log_file=None):
    session = run_tutti_cli(
        ["agent", "get", "--session-id", agent_session_id],
        timeout=30,
        log_file=log_file,
    ).get("session") or {}
    if expected_agent_target_id:
        assert_session_agent_target(session, expected_agent_target_id)
    return session


def assert_session_agent_target(
    session,
    expected_agent_target_id,
    allow_provider_compat=False,
    expected_provider_id=None,
):
    expected_agent_target_id = clean_agent_target_id(expected_agent_target_id)
    actual_agent_target_id = clean_agent_target_id(session.get("agentTargetId"))
    if actual_agent_target_id:
        if actual_agent_target_id != expected_agent_target_id:
            raise RuntimeError(
                f"agent session target mismatch: expected {expected_agent_target_id}, got {actual_agent_target_id}"
            )
        actual_provider_id = normalize_provider_id(session.get("provider"))
        normalized_expected_provider_id = normalize_provider_id(expected_provider_id)
        if (
            normalized_expected_provider_id
            and actual_provider_id
            and actual_provider_id != normalized_expected_provider_id
        ):
            raise RuntimeError(
                f"agent session provider mismatch: expected {normalized_expected_provider_id}, got {actual_provider_id}"
            )
        return
    provider_id = normalize_provider_id(session.get("provider"))
    if allow_provider_compat and provider_id == normalize_provider_id(expected_provider_id):
        return
    if provider_id:
        catalog = agent_catalog_payload()
        if catalog.get("cliContract") == "provider-compat":
            target = resolve_agent_target_from_catalog(
                catalog,
                legacy_provider=provider_id,
            )
            if target["agentTargetId"] == expected_agent_target_id:
                return
    raise RuntimeError("agent session response does not contain the expected Agent Target identity")


def resolve_run_agent_target_id(run):
    agent_target_id = clean_agent_target_id(run.get("agentTargetId"))
    if agent_target_id:
        return agent_target_id
    legacy_provider = normalize_provider_id(run.get("agentProvider"))
    if not legacy_provider:
        raise ValueError("run does not have an Agent Target identity")
    target = resolve_agent_target_from_catalog(
        agent_catalog_payload(),
        legacy_provider=legacy_provider,
    )
    return target["agentTargetId"]


def cancel_agent_session(agent_session_id):
    if not agent_session_id:
        return
    run_tutti_cli(["agent", "cancel", "--session-id", agent_session_id], timeout=30)


def open_agent_session(agent_session_id, expected_agent_target_id=None, log_file=None):
    if not agent_session_id:
        raise ValueError("run does not have an agent session")
    if not expected_agent_target_id:
        raise ValueError("run does not have an Agent Target identity")
    get_agent_session(agent_session_id, expected_agent_target_id, log_file=log_file)
    return run_tutti_cli(
        ["agent", "open", "--session-id", agent_session_id],
        timeout=30,
        log_file=log_file,
    )


def open_manual_agent_session_with_retries(agent_session_id, expected_agent_target_id, log_file):
    last_error = None
    opened_once = False
    for delay_seconds in MANUAL_AGENT_OPEN_RETRY_DELAYS_SECONDS:
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            open_agent_session(
                agent_session_id,
                expected_agent_target_id,
                log_file=log_file,
            )
        except Exception as exc:
            last_error = exc
            continue
        opened_once = True
    if not opened_once and last_error:
        raise last_error


def agent_session_messages(agent_session_id, expected_agent_target_id=None, log_file=None):
    result = run_tutti_cli(
        ["agent", "session-summary", "--session-id", agent_session_id, "--limit", "80"],
        timeout=30,
        log_file=log_file,
    )
    if expected_agent_target_id:
        assert_session_agent_target(result.get("session") or {}, expected_agent_target_id)
    return result.get("messages") or []


def latest_agent_summary(agent_session_id, expected_agent_target_id=None, log_file=None):
    return latest_agent_summary_from_messages(
        agent_session_messages(
            agent_session_id,
            expected_agent_target_id,
            log_file=log_file,
        )
    )


def wait_for_final_agent_summary(agent_session_id, expected_agent_target_id=None, log_file=None):
    summary = latest_agent_summary(
        agent_session_id, expected_agent_target_id, log_file=log_file
    )
    if summary or FINAL_SUMMARY_GRACE_SECONDS <= 0:
        return summary
    if log_file:
        log_file.write(
            f"[automation] waiting up to {FINAL_SUMMARY_GRACE_SECONDS}s for final agent summary\n".encode(
                "utf-8"
            )
        )
        log_file.flush()
    deadline = time.time() + FINAL_SUMMARY_GRACE_SECONDS
    while time.time() < deadline:
        remaining = max(0, deadline - time.time())
        time.sleep(min(FINAL_SUMMARY_POLL_SECONDS, remaining))
        summary = latest_agent_summary(
            agent_session_id, expected_agent_target_id, log_file=log_file
        )
        if summary:
            return summary
    return None


def latest_agent_summary_from_messages(messages):
    sorted_messages = sorted(
        [message for message in messages if isinstance(message, dict)],
        key=agent_message_order,
        reverse=True,
    )
    latest_tool_order = max(
        (agent_message_order(message) for message in sorted_messages if is_tool_message(message)),
        default=None,
    )
    for message in sorted_messages:
        if latest_tool_order is not None and agent_message_order(message) <= latest_tool_order:
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in {"assistant", "agent"}:
            continue
        if is_tool_message(message):
            continue
        text = extract_agent_message_text(message)
        if text:
            return text
    if latest_tool_order is not None:
        return None
    for message in sorted_messages:
        if is_tool_message(message):
            continue
        text = extract_agent_message_text(message)
        if text:
            return text
    return None


def agent_messages_have_response(messages):
    sorted_messages = sorted(
        [message for message in messages if isinstance(message, dict)],
        key=agent_message_order,
        reverse=True,
    )
    latest_tool_order = max(
        (agent_message_order(message) for message in sorted_messages if is_tool_message(message)),
        default=None,
    )
    for message in sorted_messages:
        if latest_tool_order is not None and agent_message_order(message) <= latest_tool_order:
            continue
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in {"assistant", "agent"}:
            continue
        if is_tool_message(message):
            continue
        if extract_agent_message_text(message):
            return True
    return False


def agent_message_order(message):
    for key in ("version", "id"):
        try:
            return int(message.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def is_tool_message(message):
    if not isinstance(message, dict):
        return False
    kind = str(message.get("kind") or "").strip().lower()
    if kind.startswith("tool") or kind in {"function_call", "call"}:
        return True
    text = extract_message_text(message.get("text")) or ""
    return text.strip().lower().startswith("tool_call:")


def extract_agent_message_text(message):
    if not isinstance(message, dict):
        return None
    text = extract_message_text(message.get("text"))
    if text:
        return text
    return extract_message_text(message.get("payload"))


def extract_message_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [extract_message_text(item) for item in value]
        text = "\n".join(part for part in parts if part)
        return text.strip() or None
    if isinstance(value, dict):
        for key in ("content", "text", "markdown", "message"):
            text = extract_message_text(value.get(key))
            if text:
                return text
        if "parts" in value:
            text = extract_message_text(value.get("parts"))
            if text:
                return text
        if "items" in value:
            text = extract_message_text(value.get("items"))
            if text:
                return text
        return None
    return str(value).strip() or None


def terminal_agent_status(status):
    value = str(status or "").strip().lower()
    if value in {"completed", "idle", "ready"}:
        return "succeeded", None
    if value == "failed":
        return "failed", None
    if value in {"waiting_approval", "awaiting_approval"}:
        return "failed", APPROVAL_REQUIRED_ERROR
    if value in {"canceled", "cancelled"}:
        return "canceled", "Canceled by user."
    return None, None


APPROVAL_TURN_LIFECYCLE_PHASES = {"waiting_approval", "awaiting_approval"}
ACTIVE_TURN_LIFECYCLE_PHASES = {
    "submitted",
    "running",
    "working",
    "streaming",
    "in_progress",
    "waiting",
    "waiting_input",
}
SETTLED_TURN_LIFECYCLE_PHASES = {"settled", "completed", "complete", "finished", "done"}
FAILED_TURN_LIFECYCLE_OUTCOMES = {"failed", "failure", "error"}
CANCELED_TURN_LIFECYCLE_OUTCOMES = {"canceled", "cancelled", "interrupted"}


def turn_lifecycle_agent_status(turn_lifecycle):
    if not isinstance(turn_lifecycle, dict):
        return None, None, False

    phase = clean_optional_string(turn_lifecycle.get("phase")).lower()
    active_turn_id = clean_optional_string(turn_lifecycle.get("activeTurnId"))
    if phase in {"failed"}:
        return "failed", None, True
    if phase in {"canceled", "cancelled"}:
        return "canceled", "Canceled by user.", True
    if phase in APPROVAL_TURN_LIFECYCLE_PHASES:
        return "failed", APPROVAL_REQUIRED_ERROR, True
    if phase in ACTIVE_TURN_LIFECYCLE_PHASES or (
        active_turn_id and phase not in SETTLED_TURN_LIFECYCLE_PHASES
    ):
        return None, None, True
    if phase in SETTLED_TURN_LIFECYCLE_PHASES:
        outcome = clean_optional_string(turn_lifecycle.get("outcome")).lower()
        if outcome in FAILED_TURN_LIFECYCLE_OUTCOMES:
            return "failed", None, True
        if outcome in CANCELED_TURN_LIFECYCLE_OUTCOMES:
            return "canceled", "Canceled by user.", True
        return "succeeded", None, True

    return None, None, False


def run_has_timed_out(started_at, timeout_seconds):
    return timeout_seconds > 0 and time.time() - started_at >= timeout_seconds


def run_timeout_error(timeout_seconds):
    return f"Automation run timed out after {timeout_seconds} seconds."


def initial_agent_status(status):
    return str(status or "").strip().lower() == "created"


def clean_env(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, val in value.items():
        key = str(key).strip()
        if key:
            result[key] = str(val)
    return result


def clean_time(value):
    if value is None:
        return "09:00"
    value = str(value).strip()
    if not value:
        raise ValueError("time-of-day must be HH:MM")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        raise ValueError("time-of-day must be HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("time-of-day must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def clean_days(value):
    if not isinstance(value, list):
        return [1]
    days = sorted({clean_int(day, 1, 7) for day in value})
    return days or [1]


def runner_options_payload(agent_target_id=None, locale=None, legacy_provider=None):
    catalog = agent_catalog_payload()
    agents = catalog["agents"]
    if not catalog.get("defaultAgentTargetId"):
        payload = empty_runner_options_payload()
        payload["agents"] = agents
        return payload
    target = resolve_agent_target_from_catalog(
        catalog,
        agent_target_id=agent_target_id,
        legacy_provider=legacy_provider,
        use_default=True,
        require_available=True,
    )
    options = agent_composer_options_payload(target, catalog, locale)
    legacy = project_legacy_agent_providers(catalog)
    return {
        "available": True,
        "agentTargetId": target["agentTargetId"],
        "providerId": target["providerId"],
        "defaultAgentTargetId": catalog.get("defaultAgentTargetId") or "",
        "agents": agents,
        # Deprecated compatibility projection for cached clients.
        "provider": legacy_provider_for_target(catalog, target["agentTargetId"]) or "",
        "defaultProvider": legacy["defaultProvider"],
        "providers": legacy["providers"],
        "models": options["models"],
        "currentModel": options["currentModel"],
        "currentReasoningLevel": options["currentReasoningLevel"],
        "permissionMode": options["permissionMode"],
        "permissionConfig": options["permissionConfig"],
        "optionsUnavailable": bool(options.get("optionsUnavailable")),
    }


def empty_runner_options_payload():
    return {
        "available": False,
        "agentTargetId": "",
        "providerId": "",
        "defaultAgentTargetId": "",
        "agents": [],
        "provider": "",
        "defaultProvider": "",
        "providers": [],
        "models": [],
        "currentModel": "",
        "currentReasoningLevel": "",
        "permissionMode": "",
        "permissionConfig": {"configurable": False, "modes": []},
    }


def agent_catalog_payload():
    try:
        result = run_tutti_cli(["agent", "list"], timeout=30)
    except RuntimeError as exc:
        if not is_exact_unknown_agent_list_error(exc):
            raise
        return legacy_agent_catalog_payload()
    if result.get("schemaVersion") != 1:
        raise RuntimeError("unsupported Tutti agent catalog schema")
    agents = normalize_agent_catalog_entries(
        result.get("agents"), "id", "provider", "name"
    )
    default_agent_target_id = clean_agent_target_id(result.get("defaultAgentTargetId"))
    if default_agent_target_id and not any(
        item["agentTargetId"] == default_agent_target_id for item in agents
    ):
        raise RuntimeError("invalid Tutti default Agent Target")
    return normalize_agent_catalog(
        agents,
        default_agent_target_id,
        "agent-id",
    )


def legacy_agent_catalog_payload():
    result = run_tutti_cli(["agent", "providers"], timeout=30)
    if result.get("schemaVersion") != 2:
        raise RuntimeError("unsupported Tutti legacy agent provider catalog schema")
    agents = normalize_agent_catalog_entries(
        result.get("providers"), "agentTargetId", "providerId", "displayName"
    )
    preferred_provider = normalize_provider_id(result.get("defaultProviderId"))
    matches = [item for item in agents if item["providerId"] == preferred_provider]
    default_agent_target_id = matches[0]["agentTargetId"] if len(matches) == 1 else ""
    return normalize_agent_catalog(agents, default_agent_target_id, "provider-compat")


def normalize_agent_catalog_entries(values, target_field, provider_field, name_field):
    agents = []
    seen = set()
    for item in values or []:
        if not isinstance(item, dict):
            continue
        agent_target_id = clean_agent_target_id(item.get(target_field))
        provider_id = normalize_provider_id(item.get(provider_field))
        if not agent_target_id or not provider_id or agent_target_id in seen:
            raise RuntimeError("invalid Tutti Agent Target catalog entry")
        seen.add(agent_target_id)
        availability = item.get("availability") if isinstance(item.get("availability"), dict) else {}
        agents.append(
            {
                "agentTargetId": agent_target_id,
                "providerId": provider_id,
                "displayName": clean_optional_string(item.get(name_field)) or agent_target_id,
                "status": str(availability.get("status") or "unknown").strip().lower(),
                "detail": str(availability.get("detail") or "").strip(),
            }
        )
    return agents


def normalize_agent_catalog(agents, preferred_target, cli_contract):
    provider_counts = {}
    for item in agents:
        provider_id = item["providerId"]
        provider_counts[provider_id] = provider_counts.get(provider_id, 0) + 1
    available = [
        item
        for item in agents
        if is_agent_available(item.get("status"))
        and (
            cli_contract == "agent-id"
            or provider_counts.get(item["providerId"]) == 1
        )
    ]
    default_target = next(
        (item for item in available if item["agentTargetId"] == preferred_target),
        available[0] if available else None,
    )
    return {
        "schemaVersion": 1,
        "cliContract": cli_contract,
        "defaultAgentTargetId": default_target["agentTargetId"] if default_target else "",
        "agents": agents,
    }


def is_exact_unknown_agent_list_error(error):
    return bool(re.fullmatch(r"unknown command:\s*agent list", str(error).strip(), re.IGNORECASE))


def resolve_agent_target_from_catalog(
    catalog,
    agent_target_id=None,
    legacy_provider=None,
    use_default=False,
    require_available=False,
):
    agent_target_id = clean_agent_target_id(agent_target_id)
    legacy_provider = normalize_provider_id(legacy_provider)
    if agent_target_id and legacy_provider:
        raise ValueError("provide agentTargetId or deprecated provider, not both")
    target = None
    if agent_target_id:
        target = next(
            (item for item in catalog["agents"] if item["agentTargetId"] == agent_target_id),
            None,
        )
        if not target:
            raise ValueError(f"Agent Target is not in the current catalog: {agent_target_id}")
    elif legacy_provider:
        matches = [item for item in catalog["agents"] if item["providerId"] == legacy_provider]
        if len(matches) != 1:
            if len(matches) > 1:
                raise ValueError(
                    f"multiple Agent Targets use provider {legacy_provider}; select an exact Agent Target ID"
                )
            raise ValueError(f"provider is not in the current Agent catalog: {legacy_provider}")
        target = matches[0]
    elif use_default:
        default_id = clean_agent_target_id(catalog.get("defaultAgentTargetId"))
        target = next(
            (item for item in catalog["agents"] if item["agentTargetId"] == default_id),
            None,
        )
    if not target:
        raise ValueError("no Agent Target is available")
    if catalog.get("cliContract") == "provider-compat":
        provider_matches = [
            item
            for item in catalog["agents"]
            if item["providerId"] == target["providerId"]
        ]
        if len(provider_matches) != 1:
            raise ValueError(
                f"the old daemon cannot select Agent Target {target['agentTargetId']} because provider "
                f"{target['providerId']} maps to multiple Agent Targets"
            )
    if require_available and not is_agent_available(target.get("status")):
        raise ValueError(target.get("detail") or f"Agent Target is unavailable: {target['agentTargetId']}")
    return target


def legacy_provider_for_target(catalog, agent_target_id):
    target = next(
        (item for item in catalog["agents"] if item["agentTargetId"] == agent_target_id),
        None,
    )
    if not target:
        return ""
    matches = [item for item in catalog["agents"] if item["providerId"] == target["providerId"]]
    return target["providerId"] if len(matches) == 1 else ""


def project_legacy_agent_providers(catalog):
    providers = []
    for target in catalog["agents"]:
        provider = legacy_provider_for_target(catalog, target["agentTargetId"])
        if provider and is_agent_available(target.get("status")):
            providers.append({"provider": provider, "status": target["status"], "detail": target["detail"]})
    default_provider = legacy_provider_for_target(catalog, catalog.get("defaultAgentTargetId"))
    return {"defaultProvider": default_provider, "providers": providers}


def canonicalize_runner_settings(settings, require_available=False):
    settings = normalize_runner_settings(settings)
    agent_target_id = clean_agent_target_id(settings.get("agentTargetId"))
    if agent_target_id and not require_available:
        return settings
    catalog = agent_catalog_payload()
    target = resolve_agent_target_from_catalog(
        catalog,
        agent_target_id=agent_target_id,
        legacy_provider=settings.get("provider") if not agent_target_id else None,
        require_available=require_available,
    )
    return {
        "agentTargetId": target["agentTargetId"],
        "providerId": target["providerId"],
        "model": settings.get("model") or "",
        "reasoningEffort": settings.get("reasoningEffort") or "",
        "permissionMode": settings.get("permissionMode") or "",
    }


def agent_providers_payload():
    return project_legacy_agent_providers(agent_catalog_payload())


def is_agent_available(status):
    return str(status or "").strip().lower() in {"available", "ready"}


def agent_composer_options_payload(target, catalog, locale=None):
    args = [
        "agent",
        "composer-options",
        "--agent-id" if catalog["cliContract"] == "agent-id" else "--provider",
        target["agentTargetId"] if catalog["cliContract"] == "agent-id" else target["providerId"],
    ]
    workspace_root = clean_optional_string(WORKSPACE_ROOT)
    if workspace_root:
        args.extend(["--cwd", workspace_root])
    locale = normalize_locale(locale)
    if locale:
        args.extend(["--locale", locale])
    try:
        result = run_tutti_cli(args, timeout=RUNNER_OPTIONS_COMPOSER_TIMEOUT_SECONDS)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return fallback_agent_composer_options(target["agentTargetId"])
    effective_settings = (
        result.get("effectiveSettings")
        if isinstance(result.get("effectiveSettings"), dict)
        else {}
    )
    model_config = result.get("modelConfig") if isinstance(result.get("modelConfig"), dict) else {}
    reasoning_config = (
        result.get("reasoningConfig")
        if isinstance(result.get("reasoningConfig"), dict)
        else {}
    )
    reasoning_levels = normalize_reasoning_options(reasoning_config.get("options"))
    current_model = clean_optional_string(effective_settings.get("model")) or config_option_selected_value(
        model_config
    )
    models = resolve_composer_models(result, current_model)
    current_reasoning = clean_optional_string(
        effective_settings.get("reasoningEffort")
    ) or config_option_selected_value(reasoning_config)
    return {
        "models": [
            {
                **model,
                "defaultReasoningLevel": current_reasoning
                or (reasoning_levels[0]["effort"] if reasoning_levels else ""),
                "reasoningLevels": reasoning_levels,
            }
            for model in models
        ],
        "currentModel": current_model,
        "currentReasoningLevel": current_reasoning,
        "permissionMode": clean_optional_string(
            effective_settings.get("permissionModeId")
        ),
        "permissionConfig": normalize_permission_config(
            result.get("permissionConfig")
        ),
    }


def fallback_agent_composer_options(_agent_target_id):
    return {
        "optionsUnavailable": True,
        "models": [],
        "currentModel": "",
        "currentReasoningLevel": "",
        "permissionMode": "",
        "permissionConfig": {"configurable": False, "modes": []},
    }


def normalize_permission_config(value):
    if not isinstance(value, dict):
        return {"configurable": False, "modes": []}
    modes = []
    for mode in value.get("modes") or []:
        if not isinstance(mode, dict):
            continue
        mode_id = clean_optional_string(mode.get("id"))
        if not mode_id:
            continue
        modes.append(
            {
                "id": mode_id,
                "label": clean_first_string(mode, "label", "name"),
                "description": clean_first_string(mode, "description"),
                "semantic": clean_optional_string(mode.get("semantic")),
                "name": clean_optional_string(mode.get("name")),
                "defaultValue": clean_first_string(
                    mode, "defaultValue", "default_value", "default"
                ),
                "current": clean_first_string(
                    mode, "current", "currentValue", "current_value"
                ),
                "effective": clean_first_string(
                    mode, "effective", "effectiveValue", "effective_value"
                ),
            }
        )
    return {
        "configurable": bool(value.get("configurable")) and bool(modes),
        "defaultValue": clean_first_string(
            value, "defaultValue", "default_value", "default"
        ),
        "modes": modes,
    }


def config_option_selected_value(option):
    if not isinstance(option, dict):
        return ""
    return clean_first_string(
        option,
        "effective",
        "effectiveValue",
        "effective_value",
        "current",
        "currentValue",
        "current_value",
        "defaultValue",
        "default_value",
        "default",
    )


def resolve_composer_models(result, current_model):
    runtime_context = result.get("runtimeContext") if isinstance(result.get("runtimeContext"), dict) else {}
    models_from_runtime = models_from_runtime_config_options(runtime_context)
    if models_from_runtime:
        return models_from_runtime
    model_config = result.get("modelConfig") if isinstance(result.get("modelConfig"), dict) else {}
    models_from_config = normalize_config_options(model_config.get("options"))
    if models_from_config:
        return append_current_model_option(models_from_config, current_model)
    return append_current_model_option([], current_model)


def models_from_runtime_config_options(runtime_context):
    config_options = runtime_context.get("configOptions")
    if not isinstance(config_options, list):
        return []
    for option in config_options:
        if not isinstance(option, dict):
            continue
        if clean_optional_string(option.get("id")) != "model":
            continue
        models = normalize_config_options(option.get("options"))
        current = clean_optional_string(option.get("currentValue") or option.get("current_value"))
        return append_current_model_option(models, current)
    return []


def append_current_model_option(models, current_model):
    current_model = clean_optional_string(current_model)
    if not current_model:
        return models
    if any(item.get("id") == current_model for item in models):
        return models
    return [
        *models,
        {
            "id": current_model,
            "name": current_model,
            "label": current_model,
        },
    ]


def normalize_config_options(options):
    result = []
    for option in options or []:
        if not isinstance(option, dict):
            continue
        value = clean_first_string(option, "value", "id", "model")
        if not value:
            continue
        label = clean_first_string(option, "label", "name", "displayName")
        result.append(
            {
                "id": value,
                "name": label or value,
                "label": label,
                "description": clean_first_string(option, "description"),
                "defaultValue": clean_first_string(
                    option, "defaultValue", "default_value", "default"
                ),
                "current": clean_first_string(
                    option, "current", "currentValue", "current_value"
                ),
                "effective": clean_first_string(
                    option, "effective", "effectiveValue", "effective_value"
                ),
            }
        )
    return result


def normalize_reasoning_options(options):
    result = []
    for option in options or []:
        if not isinstance(option, dict):
            continue
        value = clean_optional_string(option.get("value") or option.get("effort"))
        if not value:
            continue
        label = clean_first_string(option, "label", "name", "displayName")
        result.append(
            {
                "effort": value,
                "label": label,
                "description": clean_optional_string(
                    option.get("description")
                ),
                "defaultValue": clean_first_string(
                    option, "defaultValue", "default_value", "default"
                ),
                "current": clean_first_string(
                    option, "current", "currentValue", "current_value"
                ),
                "effective": clean_first_string(
                    option, "effective", "effectiveValue", "effective_value"
                ),
            }
        )
    return result


def cwd_options_payload():
    root = Path(WORKSPACE_ROOT or Path.home()).expanduser()
    options = []
    seen = set()

    def add(path, label, kind):
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            resolved = path.expanduser()
        key = str(resolved)
        if key in seen or not resolved.is_dir():
            return
        seen.add(key)
        options.append({"path": key, "label": label, "kind": kind})

    add(root, root.name or str(root), "workspace")
    for worktree in git_worktrees(root):
        label = worktree.name or str(worktree)
        add(worktree, label, "worktree")
    for project in discover_project_dirs(root):
        label = relative_label(root, project)
        add(project, label, "project")

    return {"cwd": str(root), "directories": options}


def git_worktrees(root):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return []
    worktrees = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            worktrees.append(Path(line.removeprefix("worktree ").strip()))
    return worktrees


def discover_project_dirs(root):
    if not root.is_dir():
        return []
    result = []
    ignored = {".git", ".cache", ".next", "node_modules", "dist", "build", "__pycache__"}
    def is_visible_project_candidate(entry):
        return entry.is_dir() and not entry.name.startswith(".") and entry.name not in ignored

    try:
        first_level = [entry for entry in root.iterdir() if is_visible_project_candidate(entry)]
    except Exception:
        return []
    candidates = list(first_level)
    for directory in first_level:
        try:
            candidates.extend(
                entry for entry in directory.iterdir() if is_visible_project_candidate(entry)
            )
        except Exception:
            continue
    for directory in candidates:
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            result.append(directory)
    return sorted(result, key=lambda path: relative_label(root, path).lower())[:80]


def relative_label(root, path):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


class RunEventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []

    def subscribe(self):
        subscriber = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def publish(self, event):
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass


RUN_EVENTS = RunEventHub()


def publish_run_started(run):
    if not isinstance(run, dict):
        return
    automation_id = clean_optional_string(run.get("automationId"))
    run_id = clean_optional_string(run.get("id"))
    if not automation_id or not run_id:
        return
    RUN_EVENTS.publish(
        {
            "type": "run_started",
            "automationId": automation_id,
            "runId": run_id,
            "runStatus": clean_optional_string(run.get("runStatus")) or "queued",
        }
    )


def publish_run_finished(run):
    if not isinstance(run, dict):
        return
    automation_id = clean_optional_string(run.get("automationId"))
    run_id = clean_optional_string(run.get("id"))
    if not automation_id or not run_id:
        return
    RUN_EVENTS.publish(
        {
            "type": "run_finished",
            "automationId": automation_id,
            "runId": run_id,
            "runStatus": clean_optional_string(run.get("runStatus")) or "",
        }
    )


def publish_automation_changed(action, automation=None, automation_id=None):
    id_ = automation_id
    if isinstance(automation, dict):
        id_ = id_ or automation.get("id")
    id_ = clean_optional_string(id_)
    if not id_:
        return
    RUN_EVENTS.publish(
        {
            "type": "automation_changed",
            "action": action,
            "automationId": id_,
        }
    )


class Runner:
    def __init__(self, store):
        self.store = store
        self.queues = {}
        self.running_automations = set()
        self.cv = threading.Condition()
        self.processes = {}

    def enqueue(self, automation, trigger):
        active = self.store.has_active_run(automation["id"])
        concurrency = "queue" if trigger == "schedule" else automation["concurrency"]
        if active and concurrency == "skip":
            return None
        settings = normalize_runner_settings(
            automation.get("runnerSettings"),
            None,
            automation.get("runnerArgs"),
        )
        catalog = agent_catalog_payload()
        target = resolve_agent_target_from_catalog(
            catalog,
            agent_target_id=settings.get("agentTargetId"),
            legacy_provider=settings.get("provider") if not settings.get("agentTargetId") else None,
            require_available=True,
        )
        if active and concurrency == "replace":
            self.cancel(active)
        item_id = run_id()
        item = {
            "id": item_id,
            "automationId": automation["id"],
            "trigger": trigger,
            "runStatus": "queued",
            "prompt": automation["prompt"],
            "cwd": automation["cwd"],
            "queuedAt": now_iso(),
            "agentTargetId": target["agentTargetId"],
            "agentProvider": target["providerId"],
            "artifactDir": str(
                run_artifact_dir(automation["id"], item_id).resolve()
            ),
        }
        try:
            saved = self.store.save_run(item)
        except Exception:
            # save_run commits before reading the saved projection back. If
            # that post-commit read fails, the caller observes an enqueue
            # failure even though a durable queued row already exists. Remove
            # that possible row so the scheduler can safely record one failed
            # occurrence instead of leaving an unowned duplicate behind.
            try:
                self.store.delete_queued_run(item["id"])
            except Exception as cleanup_exc:
                print(
                    f"failed to roll back queued run {item['id']}: {cleanup_exc}",
                    flush=True,
                )
            raise
        queued_automation = {
            **automation,
            "_agentCliContract": catalog["cliContract"],
        }
        try:
            with self.cv:
                automation_queue = self.queues.setdefault(automation["id"], [])
                automation_queue.append((item["id"], queued_automation))
                if automation["id"] not in self.running_automations:
                    self.running_automations.add(automation["id"])
                    threading.Thread(
                        target=self.loop_automation,
                        args=(automation["id"],),
                        daemon=True,
                    ).start()
        except Exception:
            # A worker cannot consume this entry until the condition lock is
            # released. Roll back both queue projections before reporting the
            # pre-commit failure to the scheduler.
            with self.cv:
                self.queues[automation["id"]] = [
                    (run_id_, queued)
                    for run_id_, queued in self.queues.get(automation["id"], [])
                    if run_id_ != item["id"]
                ]
                if not self.queues[automation["id"]]:
                    self.queues.pop(automation["id"], None)
                self.running_automations.discard(automation["id"])
            self.store.delete_queued_run(item["id"])
            raise
        try:
            publish_run_started(saved)
        except Exception as exc:
            # The run is already durably queued and owned by the worker. Event
            # delivery is best-effort and must not make the scheduler create a
            # second failed run for the same due occurrence.
            print(f"failed to publish queued run {item['id']}: {exc}", flush=True)
        return saved

    def record_enqueue_failure(self, automation, trigger, error):
        item_id = run_id()
        settings = normalize_runner_settings(
            automation.get("runnerSettings"),
            None,
            automation.get("runnerArgs"),
        )
        item = {
            "id": item_id,
            "automationId": automation["id"],
            "trigger": trigger,
            "runStatus": "failed",
            "prompt": automation.get("prompt") or "",
            "cwd": automation.get("cwd") or "",
            "queuedAt": now_iso(),
            "finishedAt": now_iso(),
            "error": str(error),
            "artifactDir": str(
                run_artifact_dir(automation["id"], item_id).resolve()
            ),
        }
        requested_target = clean_agent_target_id(settings.get("agentTargetId"))
        if requested_target:
            item["agentTargetId"] = requested_target
        saved = self.store.save_run(item)
        publish_run_started(saved)
        publish_run_finished(saved)
        return saved

    def cancel(self, id_):
        with self.cv:
            for automation_id in list(self.queues):
                self.queues[automation_id] = [
                    (run_id_, aut)
                    for run_id_, aut in self.queues[automation_id]
                    if run_id_ != id_
                ]
                if not self.queues[automation_id]:
                    del self.queues[automation_id]
        run = self.store.get_run(id_)
        if not run:
            return None
        if run["runStatus"] == "queued":
            run["runStatus"] = "canceled"
            run["finishedAt"] = now_iso()
            run["error"] = "Canceled before start."
            return self.store.save_run(run)
        if run.get("agentSessionId"):
            run["runStatus"] = "canceling"
            self.store.save_run(run)
            try:
                cancel_agent_session(run["agentSessionId"])
            except Exception as exc:
                run["error"] = str(exc)
                self.store.save_run(run)
        return self.store.get_run(id_)

    def loop_automation(self, automation_id):
        while True:
            with self.cv:
                automation_queue = self.queues.get(automation_id) or []
                if not automation_queue:
                    self.queues.pop(automation_id, None)
                    self.running_automations.discard(automation_id)
                    return
                id_, automation = automation_queue.pop(0)
                if not automation_queue:
                    self.queues.pop(automation_id, None)
            try:
                self.run(id_, automation)
            except Exception as exc:
                # A failure before run() enters its guarded execution block,
                # or a best-effort event publication failure after it exits,
                # must not terminate the only worker that owns the remaining
                # in-memory queue.
                self.record_worker_failure(id_, exc)

    def record_worker_failure(self, id_, error):
        try:
            run = self.store.get_run(id_)
            if not run or run["runStatus"] not in {
                "queued",
                "running",
                "canceling",
            }:
                print(f"run {id_} worker error after completion: {error}", flush=True)
                return run
            canceled = run["runStatus"] == "canceling"
            run.update(
                {
                    "runStatus": "canceled" if canceled else "failed",
                    "finishedAt": now_iso(),
                    "error": "Canceled by user." if canceled else str(error),
                    "taskStatus": run.get("taskStatus") or (None if canceled else "fail"),
                }
            )
            saved = self.store.save_run(run)
        except Exception as record_exc:
            print(
                f"failed to record worker error for run {id_}: {record_exc}",
                flush=True,
            )
            return None
        try:
            publish_run_finished(saved)
        except Exception as publish_exc:
            print(
                f"failed to publish worker error for run {id_}: {publish_exc}",
                flush=True,
            )
        return saved

    def run(self, id_, automation):
        run = self.store.get_run(id_)
        if not run or run["runStatus"] != "queued":
            return
        artifact_dir = run_artifact_dir(automation["id"], id_)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = artifact_dir / "run.log"
        run["runStatus"] = "running"
        run["startedAt"] = now_iso()
        run["artifactDir"] = str(artifact_dir)
        self.store.save_run(run)
        started = time.time()
        exit_code = None
        status = None
        error = None
        summary = None
        timeout_seconds = RUN_TIMEOUT_SECONDS
        try:
            with log_path.open("ab") as log_file:
                session = start_agent_session(automation, run, log_file)
                agent_session_id = clean_optional_string(session.get("agentSessionId"))
                if not agent_session_id:
                    raise RuntimeError("agent session was not created")
                agent_target_id = clean_agent_target_id(run.get("agentTargetId"))
                agent_provider = normalize_provider_id(run.get("agentProvider"))
                if not agent_target_id or not agent_provider:
                    raise RuntimeError("automation run does not contain an Agent Target snapshot")
                assert_session_agent_target(
                    session,
                    agent_target_id,
                    allow_provider_compat=session.get("_automationCliContract") == "provider-compat",
                    expected_provider_id=agent_provider,
                )
                run["agentSessionId"] = agent_session_id
                self.store.save_run(run)
                if run.get("trigger") == "manual":
                    open_manual_agent_session_with_retries(
                        agent_session_id, agent_target_id, log_file
                    )
                while True:
                    latest = self.store.get_run(id_)
                    if latest and latest["runStatus"] == "canceling":
                        try:
                            cancel_agent_session(agent_session_id)
                        except Exception:
                            pass
                        status = "canceled"
                        error = "Canceled by user."
                        break
                    if run_has_timed_out(started, timeout_seconds):
                        try:
                            cancel_agent_session(agent_session_id)
                        except Exception as exc:
                            log_file.write(
                                f"[automation] failed to cancel timed-out agent session: {exc}\n".encode(
                                    "utf-8"
                                )
                            )
                            log_file.flush()
                        status = "timed_out"
                        error = run_timeout_error(timeout_seconds)
                        break
                    session = get_agent_session(
                        agent_session_id,
                        agent_target_id,
                        log_file=log_file,
                    )
                    status, error = terminal_agent_status(session.get("status"))
                    if status in {"failed", "canceled"}:
                        break
                    lifecycle_status, lifecycle_error, has_turn_lifecycle = turn_lifecycle_agent_status(
                        session.get("turnLifecycle")
                    )
                    if lifecycle_status:
                        status = lifecycle_status
                        error = lifecycle_error
                        break
                    if has_turn_lifecycle:
                        time.sleep(2)
                        continue
                    if status:
                        break
                    if initial_agent_status(session.get("status")):
                        latest = self.store.get_run(id_)
                        if latest and latest.get("taskStatus"):
                            status = "succeeded"
                            break
                        messages = agent_session_messages(
                            agent_session_id,
                            agent_target_id,
                            log_file=log_file,
                        )
                        if agent_messages_have_response(messages):
                            summary = latest_agent_summary_from_messages(messages)
                            status = "succeeded"
                            break
                    time.sleep(2)
                if status == "succeeded":
                    latest = self.wait_for_completion_status(
                        id_,
                        agent_session_id,
                        log_file,
                    )
                    if latest and latest["runStatus"] == "canceling":
                        try:
                            cancel_agent_session(agent_session_id)
                        except Exception:
                            pass
                        status = "canceled"
                        error = "Canceled by user."
                try:
                    latest_for_summary = self.store.get_run(id_)
                    if latest_for_summary and latest_for_summary.get("taskStatus"):
                        final_summary = wait_for_final_agent_summary(
                            agent_session_id,
                            agent_target_id,
                            log_file=log_file,
                        )
                        if final_summary is not None:
                            summary = final_summary
                    elif summary is None:
                        summary = latest_agent_summary(
                            agent_session_id,
                            agent_target_id,
                            log_file=log_file,
                        )
                except Exception as exc:
                    if log_file:
                        log_file.write(
                            f"[automation] failed to fetch agent summary: {exc}\n".encode("utf-8")
                        )
                        log_file.flush()
        except Exception as exc:
            status = "failed"
            error = str(exc)
        latest = self.store.get_run(id_)
        if latest and latest["runStatus"] == "canceling":
            status = "canceled"
            error = "Canceled by user."
        result_summary = summary
        result_status = latest.get("taskStatus") if latest else None
        if not result_status and status == "succeeded":
            status = "failed"
            error = MISSING_TASK_STATUS_ERROR
            result_status = "fail"
        if not result_status and status in {"failed", "timed_out"}:
            result_status = "fail"
        latest = latest or run
        latest.update(
            {
                "runStatus": status,
                "finishedAt": now_iso(),
                "exitCode": exit_code,
                "summary": result_summary,
                "error": error,
                "taskStatus": result_status,
                "artifactDir": str(artifact_dir),
            }
        )
        self.store.save_run(latest)
        publish_run_finished(latest)
        print(f"run {id_} finished as {status} in {int(time.time() - started)}s", flush=True)

    def wait_for_completion_status(self, id_, agent_session_id, log_file):
        latest = self.store.get_run(id_)
        if not latest or latest.get("taskStatus") or COMPLETION_GRACE_SECONDS <= 0:
            return latest
        deadline = time.time() + COMPLETION_GRACE_SECONDS
        log_file.write(
            f"[automation] waiting up to {COMPLETION_GRACE_SECONDS}s for task status\n".encode("utf-8")
        )
        log_file.flush()
        while time.time() < deadline:
            latest = self.store.get_run(id_)
            if not latest:
                return None
            if latest["runStatus"] == "canceling" or latest.get("taskStatus"):
                return latest
            remaining = max(0, deadline - time.time())
            time.sleep(min(COMPLETION_GRACE_POLL_SECONDS, remaining))
        return self.store.get_run(id_)


def terminate_process(process):
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=8)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def read_text(path):
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def build_run_prompt(prompt, run):
    return (
        f"{prompt.rstrip()}\n\n---\n{automation_completion_instructions()}\n\n"
        f"Completion command template:\n{automation_completion_command(run)}\n"
    )


def automation_completion_instructions():
    return """
Automation completion contract:

1. Decide whether this automation task result is "success", "fail", or "skip".
2. Submit the result status with the completion command shown below, replacing <status> with success, fail, or skip.
3. If the completion command fails, fix the problem and retry it before finishing.
4. After submitting the status successfully, send the user-facing result directly as your normal final Markdown response. Do not wrap the final response in JSON and do not write it to an intermediate file.

Use "skip" when there is nothing actionable to report.
""".strip()


def automation_completion_command(run):
    cli = shlex.quote(tutti_cli_command())
    run_id_value = shlex.quote(run["id"])
    return (
        f"{cli} automation complete-run --run-id {run_id_value} "
        f"--status <status>"
    )


def build_run_display_prompt(automation, run):
    return (
        clean_optional_string(run.get("prompt"))
        or clean_optional_string(automation.get("prompt"))
        or clean_optional_string(automation.get("name"))
        or "Run automation task"
    )


class Scheduler:
    def __init__(self, store, runner, autostart=True):
        self.store = store
        self.runner = runner
        self.cv = threading.Condition()
        self.thread = None
        if autostart:
            self.start()

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def wake(self):
        with self.cv:
            self.cv.notify_all()

    def loop(self):
        startup_scan = True
        while True:
            wait_seconds = None
            try:
                self.run_due_once(enqueue_due=not startup_scan)
                startup_scan = False
            except Exception as exc:
                print(f"scheduler error: {exc}", flush=True)
                wait_seconds = 5
            with self.cv:
                if wait_seconds is None:
                    wait_seconds = self.next_wait_seconds(datetime.now(timezone.utc))
                self.cv.wait(timeout=wait_seconds)

    def run_due_once(self, enqueue_due=True, now=None):
        now = now or datetime.now(timezone.utc)
        for automation in self.store.list_due_automations():
            try:
                if enqueue_due:
                    self.runner.enqueue(automation, "schedule")
            except Exception as exc:
                try:
                    self.runner.record_enqueue_failure(
                        automation, "schedule", exc
                    )
                except Exception as record_exc:
                    print(
                        f"scheduler failed to record enqueue error for {automation.get('id')}: {record_exc}",
                        flush=True,
                    )
                print(
                    f"scheduler skipped {automation.get('id')}: {exc}",
                    flush=True,
                )
            finally:
                self.store.advance_automation_schedule(
                    automation,
                    now_iso(),
                    compute_next_run(automation, now),
                )

    def next_wait_seconds(self, now):
        next_run_at = self.store.next_scheduled_run_at()
        if next_run_at is None:
            return None
        return max(0, (next_run_at - now).total_seconds())


STORE = Store(DB_PATH)
RUNNER = Runner(STORE)
SCHEDULER = Scheduler(STORE, RUNNER)


def save_automation_and_wake(item):
    action = "updated" if STORE.get_automation(item.get("id")) else "created"
    item["runnerSettings"] = canonicalize_runner_settings(item.get("runnerSettings"))
    saved = STORE.save_automation(item)
    SCHEDULER.wake()
    publish_automation_changed(action, saved)
    return saved


def set_automation_enabled_and_wake(id_, enabled):
    item = STORE.set_automation_enabled(id_, enabled)
    if item:
        SCHEDULER.wake()
        publish_automation_changed("updated", item)
    return item


def delete_automation_and_wake(id_):
    deleted = STORE.delete_automation(id_)
    if deleted:
        SCHEDULER.wake()
        publish_automation_changed("deleted", automation_id=id_)
    return deleted


def cli_input(payload):
    value = payload.get("input") if isinstance(payload, dict) else {}
    return value if isinstance(value, dict) else {}


def cli_table(columns, rows):
    return {"kind": "table", "columns": columns, "rows": rows}


def cli_json(value):
    return {"kind": "json", "value": value}


def cli_error(code, message):
    return {"error": {"code": code, "message": message}}


def automation_cli_columns():
    return [
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "Name"},
        {"key": "enabled", "label": "Enabled"},
        {"key": "schedule", "label": "Schedule"},
        {"key": "next-run", "label": "Next run"},
        {"key": "active-run", "label": "Active run"},
        {"key": "unreviewed", "label": "Unreviewed"},
    ]


def automation_cli_rows(automations):
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "enabled": "yes" if item["enabled"] else "no",
            "schedule": schedule_label(item),
            "next-run": item.get("nextRunAt") or "",
            "active-run": item.get("activeRunId") or "",
            "unreviewed": item.get("unreviewedRunCount") or 0,
        }
        for item in automations
    ]


def run_cli_columns():
    return [
        {"key": "id", "label": "ID"},
        {"key": "automation-id", "label": "Task"},
        {"key": "agent-id", "label": "Agent"},
        {"key": "run-status", "label": "Run status"},
        {"key": "task-status", "label": "Task status"},
        {"key": "trigger", "label": "Trigger"},
        {"key": "queued", "label": "Queued"},
        {"key": "finished", "label": "Finished"},
    ]


def run_cli_rows(runs):
    return [
        {
            "id": item["id"],
            "automation-id": item["automationId"],
            "agent-id": item.get("agentTargetId") or "",
            "run-status": item["runStatus"],
            "task-status": item.get("taskStatus") or "",
            "trigger": item["trigger"],
            "queued": item["queuedAt"],
            "finished": item.get("finishedAt") or "",
        }
        for item in runs
    ]


def complete_run_from_cli(input_):
    run_id_value = clean_required(input_.get("run-id"), "run-id")
    result_status = clean_required(input_.get("status"), "status").lower()
    if result_status not in RESULT_STATUS_VALUES:
        raise ValueError("status must be success, fail, or skip")
    before = STORE.get_run(run_id_value)
    run = STORE.complete_run(run_id_value, result_status)
    if before and before.get("runStatus") != run.get("runStatus"):
        publish_run_finished(run)
    return run


def schedule_label(item):
    schedule_type = item.get("scheduleType") or "daily"
    schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
    if schedule_type == "interval":
        return f"every {schedule.get('intervalMinutes', '')}m"
    if schedule_type == "daily":
        return f"daily {schedule.get('timeOfDay', '')}".strip()
    if schedule_type == "weekly":
        days = ",".join(str(day) for day in schedule.get("daysOfWeek", []))
        return f"weekly {days} {schedule.get('timeOfDay', '')}".strip()
    if schedule_type == "cron":
        return f"cron {schedule.get('expression', '')}".strip()
    return "daily 09:00"


def resolve_cli_automation(input_):
    automation_id_value = str(input_.get("automation-id") or "").strip()
    name = str(input_.get("name") or "").strip()
    if automation_id_value:
        automation = STORE.get_automation(automation_id_value)
        if not automation:
            raise ValueError(f"automation {automation_id_value} was not found")
        return automation
    if not name:
        raise ValueError("automation-id or name is required")
    matches = [
        item
        for item in STORE.list_automations()
        if item["name"].strip().lower() == name.lower()
    ]
    if not matches:
        raise ValueError(f"automation named {name} was not found")
    if len(matches) > 1:
        raise ValueError(f"automation name {name} matches multiple automations; pass automation-id")
    return matches[0]


def clean_cli_limit(value):
    if value in (None, ""):
        return 50
    return clean_int(value, 1, 200)


def cli_has(input_, key):
    return key in input_ and input_.get(key) is not None and str(input_.get(key)).strip() != ""


def clean_cli_automation_id(input_):
    automation_id_value = str(input_.get("automation-id") or "").strip()
    if not automation_id_value:
        raise ValueError("automation-id is required")
    return automation_id_value


def automation_payload_from_cli(input_, existing=None):
    payload = {}
    if existing:
        payload = {
            "name": existing["name"],
            "prompt": existing["prompt"],
            "cwd": existing["cwd"],
            "enabled": existing["enabled"],
            "scheduleType": existing["scheduleType"],
            "schedule": existing["schedule"],
            "concurrency": existing["concurrency"],
            "runnerSettings": existing.get("runnerSettings") or {},
            "runnerArgs": existing.get("runnerArgs") or [],
            "env": existing.get("env") or {},
        }
    for cli_key, payload_key in (
        ("name", "name"),
        ("prompt", "prompt"),
        ("cwd", "cwd"),
        ("enabled", "enabled"),
        ("concurrency", "concurrency"),
    ):
        if cli_key in input_:
            payload[payload_key] = input_[cli_key]
    payload["scheduleType"] = cli_schedule_type(input_, payload)
    payload["schedule"] = cli_schedule(input_, payload["scheduleType"], payload.get("schedule"))

    runner_settings = dict(payload.get("runnerSettings") or {})
    if cli_has(input_, "agent-id") and cli_has(input_, "provider"):
        raise ValueError("provide --agent-id or deprecated --provider, not both")
    if "agent-id" in input_:
        runner_settings["agentTargetId"] = input_["agent-id"]
        runner_settings.pop("provider", None)
        runner_settings.pop("providerId", None)
    elif "provider" in input_:
        runner_settings["provider"] = input_["provider"]
        runner_settings.pop("agentTargetId", None)
        runner_settings.pop("providerId", None)
    for cli_key, settings_key in (
        ("model", "model"),
        ("reasoning-effort", "reasoningEffort"),
        ("permission-mode", "permissionMode"),
    ):
        if cli_key in input_:
            runner_settings[settings_key] = input_[cli_key]
    if (
        not existing
        and not clean_agent_target_id(runner_settings.get("agentTargetId"))
        and not normalize_provider_id(runner_settings.get("provider"))
    ):
        runner_settings["agentTargetId"] = default_cli_runner_agent_target()
    if runner_settings:
        payload["runnerSettings"] = runner_settings
    if "runner-args" in input_:
        payload["runnerArgs"] = clean_cli_runner_args(input_.get("runner-args"))
    if "env" in input_:
        payload["env"] = clean_cli_env(input_.get("env"))
    return payload


def cli_schedule_type(input_, payload):
    if cli_has(input_, "schedule-type"):
        return str(input_.get("schedule-type")).strip()
    current = str(payload.get("scheduleType") or "").strip()
    if cli_has(input_, "cron"):
        return "cron"
    if cli_has(input_, "interval-minutes"):
        return "interval"
    if cli_has(input_, "days-of-week"):
        return "weekly"
    if cli_has(input_, "time-of-day"):
        if current in {"daily", "weekly"}:
            return current
        return "daily"
    if current:
        return current
    return "daily"


def cli_schedule(input_, schedule_type, existing=None):
    schedule = dict(existing) if isinstance(existing, dict) else {}
    schedule_keys = {"interval-minutes", "time-of-day", "days-of-week", "cron"}
    provided_schedule_keys = {key for key in schedule_keys if key in input_}
    if schedule_type == "cron":
        incompatible = sorted(provided_schedule_keys - {"cron"})
        if incompatible:
            raise ValueError("cron schedules must use --cron, not --time-of-day, --days-of-week, or --interval-minutes")
    elif "cron" in provided_schedule_keys:
        raise ValueError("--cron cannot be used with a non-cron schedule type")
    if "interval-minutes" in input_:
        schedule["intervalMinutes"] = input_["interval-minutes"]
    if "time-of-day" in input_:
        schedule["timeOfDay"] = input_["time-of-day"]
    if "days-of-week" in input_:
        schedule["daysOfWeek"] = clean_cli_days(input_.get("days-of-week"))
    if "cron" in input_:
        schedule["expression"] = input_["cron"]
    if schedule_type == "interval":
        return {"intervalMinutes": schedule.get("intervalMinutes", 60)}
    if schedule_type == "daily":
        return {"timeOfDay": schedule.get("timeOfDay", "09:00")}
    if schedule_type == "weekly":
        return {
            "timeOfDay": schedule.get("timeOfDay", "09:00"),
            "daysOfWeek": schedule.get("daysOfWeek", [1]),
        }
    if schedule_type == "cron":
        return {"expression": clean_required(schedule.get("expression"), "cron expression")}
    return {}


def clean_cli_days(value):
    parts = [part.strip() for part in str(value or "").split(",")]
    days = [clean_int(part, 1, 7) for part in parts if part]
    return days or [1]


def clean_cli_runner_args(value):
    try:
        return [arg for arg in shlex.split(str(value or "")) if arg]
    except ValueError as exc:
        raise ValueError(f"runner-args is invalid: {exc}") from exc


def clean_cli_env(value):
    result = {}
    text = str(value or "").strip()
    if not text:
        return result
    for part in text.split(","):
        key, separator, val = part.partition("=")
        key = key.strip()
        if not key or not separator:
            raise ValueError("env must be comma-separated KEY=VALUE pairs")
        result[key] = val.strip()
    return result


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        try:
            path = urlparse(self.path).path
            if path == "/healthz":
                return self.text(200, "ok", send_body=False)
            if path == "/":
                return self.serve_static(
                    "index.html",
                    "text/html; charset=utf-8",
                    send_body=False,
                )
            if path.startswith("/assets/"):
                relative = path[len("/assets/") :]
                return self.serve_static_asset(relative, send_body=False)
            self.json(404, {"error": "not found"}, send_body=False)
        except Exception as exc:
            self.json(500, {"error": str(exc)}, send_body=False)

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query)
            if path == "/healthz":
                return self.text(200, "ok")
            if path == "/":
                return self.serve_static("index.html", "text/html; charset=utf-8")
            if path.startswith("/assets/"):
                relative = path[len("/assets/") :]
                return self.serve_static_asset(relative)
            if path == "/api/context":
                return self.json(200, context_payload())
            if path == "/api/agent-providers":
                return self.json(200, agent_providers_payload())
            if path == "/api/agent-targets":
                return self.json(200, agent_catalog_payload())
            if path == "/api/runner-options":
                return self.json(
                    200,
                    runner_options_payload(
                        agent_target_id=query.get("agentTargetId", [None])[0],
                        legacy_provider=query.get("provider", [None])[0],
                        locale=query.get("locale", [None])[0],
                    ),
                )
            if path == "/api/cwd-options":
                return self.json(200, cwd_options_payload())
            if path == "/api/automations":
                return self.json(200, {"automations": STORE.list_automations()})
            if path.startswith("/api/automations/"):
                id_ = path.split("/", 3)[3]
                item = STORE.get_automation(id_)
                return self.json(200, {"automation": item}) if item else self.json(404, {"error": "automation not found"})
            if path == "/api/runs":
                runs = STORE.list_runs(query.get("automationId", [None])[0])
                return self.json(200, {"runs": runs})
            if path.startswith("/api/runs/") and path.endswith("/log"):
                id_ = path.split("/")[3]
                run = STORE.get_run(id_)
                if not run:
                    return self.json(404, {"error": "run not found"})
                return self.text(200, read_text(Path(run["artifactDir"]) / "run.log") or "")
            if path.startswith("/api/runs/"):
                id_ = path.split("/", 3)[3]
                run = STORE.get_run(id_)
                return self.json(200, {"run": run}) if run else self.json(404, {"error": "run not found"})
            if path == "/api/review":
                runs = [run for run in STORE.list_runs(limit=200) if run["finishedAt"] and not run["reviewedAt"]]
                return self.json(200, {"runs": runs})
            if path == "/api/events":
                return self.serve_run_events()
            self.json(404, {"error": "not found"})
        except Exception as exc:
            self.json(500, {"error": str(exc)})

    def do_POST(self):
        self.handle_write("POST")

    def do_PUT(self):
        self.handle_write("PUT")

    def do_DELETE(self):
        self.handle_write("DELETE")

    def handle_write(self, method):
        try:
            path = urlparse(self.path).path
            if method == "POST" and path.startswith("/tutti/cli/"):
                return self.handle_cli(path)
            if method == "POST" and path == "/api/automations":
                item = normalize_automation(self.read_json())
                return self.json(201, {"automation": save_automation_and_wake(item)})
            if method == "POST" and path.startswith("/api/automations/") and path.endswith("/pause"):
                id_ = path.split("/")[3]
                item = set_automation_enabled_and_wake(id_, False)
                return self.json(200, {"automation": item}) if item else self.json(404, {"error": "automation not found"})
            if method == "POST" and path.startswith("/api/automations/") and path.endswith("/resume"):
                id_ = path.split("/")[3]
                item = set_automation_enabled_and_wake(id_, True)
                return self.json(200, {"automation": item}) if item else self.json(404, {"error": "automation not found"})
            if method == "PUT" and path.startswith("/api/automations/"):
                id_ = path.split("/", 3)[3]
                existing = STORE.get_automation(id_)
                if not existing:
                    return self.json(404, {"error": "automation not found"})
                return self.json(200, {"automation": save_automation_and_wake(normalize_automation(self.read_json(), existing))})
            if method == "DELETE" and path.startswith("/api/automations/"):
                deleted = delete_automation_and_wake(path.split("/", 3)[3])
                return self.json(200 if deleted else 404, {"deleted": deleted})
            if method == "POST" and path.startswith("/api/automations/") and path.endswith("/run"):
                id_ = path.split("/")[3]
                automation = STORE.get_automation(id_)
                if not automation:
                    return self.json(404, {"error": "automation not found"})
                run = RUNNER.enqueue(automation, "manual")
                return self.json(202, {"run": run})
            if method == "POST" and path.startswith("/api/runs/") and path.endswith("/cancel"):
                id_ = path.split("/")[3]
                run = RUNNER.cancel(id_)
                return self.json(200, {"run": run}) if run else self.json(404, {"error": "run not found"})
            if method == "POST" and path.startswith("/api/runs/") and path.endswith("/open-agent"):
                id_ = path.split("/")[3]
                run = STORE.get_run(id_)
                if not run:
                    return self.json(404, {"error": "run not found"})
                open_agent_session(
                    run.get("agentSessionId"),
                    resolve_run_agent_target_id(run),
                )
                return self.json(200, {"opened": True})
            if method == "POST" and path.startswith("/api/runs/") and path.endswith("/review"):
                id_ = path.split("/")[3]
                run = STORE.get_run(id_)
                if not run:
                    return self.json(404, {"error": "run not found"})
                run["reviewedAt"] = now_iso()
                return self.json(200, {"run": STORE.save_run(run)})
            self.json(404, {"error": "not found"})
        except ValueError as exc:
            if urlparse(self.path).path.startswith("/tutti/cli/"):
                return self.json(400, cli_error("invalid_input", str(exc)))
            self.json(400, {"error": str(exc)})
        except Exception as exc:
            if urlparse(self.path).path.startswith("/tutti/cli/"):
                return self.json(500, cli_error("handler_failed", str(exc)))
            self.json(500, {"error": str(exc)})

    def handle_cli(self, path):
        payload = self.read_json()
        input_ = cli_input(payload)
        if path == "/tutti/cli/list":
            automations = STORE.list_automations()
            return self.json(
                200,
                cli_table(automation_cli_columns(), automation_cli_rows(automations)),
            )
        if path == "/tutti/cli/get":
            automation = resolve_cli_automation(input_)
            return self.json(200, cli_json({"automation": automation}))
        if path == "/tutti/cli/create":
            item = normalize_automation(automation_payload_from_cli(input_))
            return self.json(
                200,
                cli_json({"automation": save_automation_and_wake(item)}),
            )
        if path == "/tutti/cli/update":
            automation_id_value = clean_cli_automation_id(input_)
            existing = STORE.get_automation(automation_id_value)
            if not existing:
                raise ValueError(f"automation {automation_id_value} was not found")
            item = normalize_automation(
                automation_payload_from_cli(input_, existing),
                existing,
            )
            return self.json(
                200,
                cli_json({"automation": save_automation_and_wake(item)}),
            )
        if path == "/tutti/cli/delete":
            automation_id_value = clean_cli_automation_id(input_)
            deleted = delete_automation_and_wake(automation_id_value)
            if not deleted:
                raise ValueError(f"automation {automation_id_value} was not found")
            return self.json(
                200,
                cli_json({"deleted": True, "automationId": automation_id_value}),
            )
        if path == "/tutti/cli/run":
            automation = resolve_cli_automation(input_)
            run = RUNNER.enqueue(automation, "manual")
            if not run:
                active_run_id = STORE.has_active_run(automation["id"])
                return self.json(
                    200,
                    cli_json(
                        {
                            "queued": False,
                            "automation": automation,
                            "activeRunId": active_run_id,
                            "message": "Automation task already has an active run.",
                        }
                    ),
                )
            return self.json(
                200,
                cli_json({"queued": True, "automation": automation, "run": run}),
            )
        if path == "/tutti/cli/runs":
            automation_id_value = str(input_.get("automation-id") or "").strip() or None
            if automation_id_value and not STORE.get_automation(automation_id_value):
                raise ValueError(f"automation {automation_id_value} was not found")
            runs = STORE.list_runs(
                automation_id_value,
                limit=clean_cli_limit(input_.get("limit")),
            )
            return self.json(200, cli_table(run_cli_columns(), run_cli_rows(runs)))
        if path == "/tutti/cli/complete-run":
            run = complete_run_from_cli(input_)
            return self.json(200, cli_json({"completed": True, "run": run}))
        return self.json(404, cli_error("command_not_found", "command not found"))

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def serve_run_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        subscriber = RUN_EVENTS.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = subscriber.get(timeout=25)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"data: ")
                self.wfile.write(payload)
                self.wfile.write(b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            RUN_EVENTS.unsubscribe(subscriber)

    def json(self, status, payload, send_body=True):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def text(self, status, body, send_body=True):
        body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def file(self, path, content_type, send_body=True):
        if not path.is_file():
            return self.json(404, {"error": "not found"})
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def serve_static(self, relative_path, content_type, send_body=True):
        path = (STATIC_DIR / relative_path).resolve()
        if not path.is_relative_to(STATIC_DIR):
            return self.json(404, {"error": "not found"})
        return self.file(path, content_type, send_body=send_body)

    def serve_static_asset(self, relative_path, send_body=True):
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".woff2": "font/woff2",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }
        suffix = Path(relative_path).suffix.lower()
        content_type = content_types.get(suffix, "application/octet-stream")
        return self.serve_static(
            Path("assets") / relative_path,
            content_type,
            send_body=send_body,
        )

    def log_message(self, format, *args):
        return


def context_payload():
    return {
        "workspaceId": WORKSPACE_ID,
        "workspaceName": WORKSPACE_NAME,
        "workspaceRoot": WORKSPACE_ROOT,
        "dataDir": str(DATA_DIR),
        "logDir": str(LOG_DIR),
        "runtimeDir": str(RUNTIME_DIR),
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("TUTTI_APP_HOST", "127.0.0.1")
    port = int(os.environ["TUTTI_APP_PORT"])
    print(f"Automation listening on {host}:{port}", flush=True)
    if os.environ.get("TUTTI_AUTOMATION_STATIC_DIR"):
        print(f"Automation static dir override: {STATIC_DIR}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
