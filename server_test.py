import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


def load_server_module(temp_root):
    package_dir = Path(__file__).resolve().parent
    os.environ["TUTTI_APP_PACKAGE_DIR"] = str(package_dir)
    os.environ["TUTTI_APP_DATA_DIR"] = str(temp_root / "data")
    os.environ["TUTTI_APP_LOG_DIR"] = str(temp_root / "logs")
    os.environ["TUTTI_APP_RUNTIME_DIR"] = str(temp_root / "runtime")
    os.environ["TUTTI_WORKSPACE_ID"] = "workspace-1"
    os.environ["TUTTI_APP_PORT"] = "0"
    os.environ["TUTTI_CLI"] = "/usr/local/bin/tutti"

    spec = importlib.util.spec_from_file_location(
        f"automation_server_test_{id(temp_root)}",
        package_dir / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerOptionsPayloadTest(unittest.TestCase):
    def test_runner_options_uses_cli_locale_and_structured_configs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "codex",
                        "providers": [
                            {"providerId": "codex", "availability": {"status": "available"}},
                            {"providerId": "claude-code", "availability": {"status": "available"}},
                        ],
                    }
                if args == [
                    "agent",
                    "composer-options",
                    "--provider",
                    "codex",
                    "--locale",
                    "zh-CN",
                ]:
                    return {
                        "effectiveSettings": {
                            "model": "gpt-5",
                            "permissionModeId": "full-access",
                            "reasoningEffort": "high",
                        },
                        "modelConfig": {
                            "currentValue": "gpt-5",
                            "options": [
                                {
                                    "id": "gpt-5",
                                    "label": "GPT-5",
                                    "value": "gpt-5",
                                }
                            ],
                        },
                        "reasoningConfig": {
                            "currentValue": "high",
                            "options": [
                                {
                                    "id": "high",
                                    "label": "高",
                                    "value": "high",
                                }
                            ],
                        },
                        "permissionConfig": {
                            "configurable": True,
                            "defaultValue": "auto",
                            "modes": [
                                {
                                    "id": "auto",
                                    "label": "代我批准",
                                    "semantic": "auto",
                                },
                                {
                                    "id": "full-access",
                                    "label": "完全访问",
                                    "semantic": "full-access",
                                },
                            ],
                        },
                        "runtimeContext": {
                            "configOptions": [
                                {
                                    "id": "effort",
                                    "currentValue": "high",
                                }
                            ]
                        },
                    }
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                payload = module.runner_options_payload(provider="codex", locale="zh-CN")

            self.assertEqual(
                calls[1],
                [
                    "agent",
                    "composer-options",
                    "--provider",
                    "codex",
                    "--locale",
                    "zh-CN",
                ],
            )
            self.assertEqual(payload["defaultProvider"], "codex")
            self.assertEqual(payload["provider"], "codex")
            self.assertEqual(payload["currentModel"], "gpt-5")
            self.assertEqual(payload["currentReasoningLevel"], "high")
            self.assertEqual(payload["permissionMode"], "full-access")
            self.assertEqual(payload["models"][0]["label"], "GPT-5")
            self.assertEqual(payload["models"][0]["reasoningLevels"][0]["label"], "高")
            self.assertEqual(
                payload["permissionConfig"]["modes"][0]["label"],
                "代我批准",
            )

    def test_runner_options_prefers_runtime_context_model_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "codex",
                        "providers": [{"providerId": "codex", "availability": {"status": "available"}}],
                    }
                if args[:4] == ["agent", "composer-options", "--provider", "codex"]:
                    return {
                        "effectiveSettings": {"model": "gpt-5"},
                        "modelConfig": {"options": []},
                        "reasoningConfig": {"options": []},
                        "permissionConfig": {"configurable": False, "modes": []},
                        "runtimeContext": {
                            "configOptions": [
                                {
                                    "id": "model",
                                    "currentValue": "gpt-5",
                                    "options": [
                                        {"value": "gpt-5", "name": "GPT-5"},
                                        {"value": "gpt-5.1", "name": "GPT-5.1"},
                                    ],
                                }
                            ]
                        },
                    }
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                payload = module.runner_options_payload(provider="codex", locale="en")

            self.assertEqual([item["id"] for item in payload["models"]], ["gpt-5", "gpt-5.1"])
            self.assertEqual(payload["currentModel"], "gpt-5")

    def test_runner_options_falls_back_when_composer_options_times_out(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            timeouts = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "codex",
                        "providers": [
                            {"providerId": "codex", "availability": {"status": "available"}},
                            {"providerId": "claude-code", "availability": {"status": "available"}},
                        ],
                    }
                if args[:4] == ["agent", "composer-options", "--provider", "codex"]:
                    timeouts.append(timeout)
                    raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                payload = module.runner_options_payload(provider="codex", locale="en")

            self.assertEqual(timeouts, [module.RUNNER_OPTIONS_COMPOSER_TIMEOUT_SECONDS])
            self.assertTrue(payload["available"])
            self.assertEqual(payload["provider"], "codex")
            self.assertTrue(payload["optionsUnavailable"])
            self.assertEqual(payload["models"], [])
            self.assertEqual(payload["currentModel"], "")

    def test_runner_options_fallback_does_not_invent_claude_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            payload = module.fallback_agent_composer_options("claude-code")

            self.assertTrue(payload["optionsUnavailable"])
            self.assertEqual(payload["models"], [])
            self.assertEqual(payload["currentModel"], "")

    def test_runner_options_prefers_runtime_context_over_model_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "claude-code",
                        "providers": [{"providerId": "claude-code", "availability": {"status": "available"}}],
                    }
                if args[:4] == ["agent", "composer-options", "--provider", "claude-code"]:
                    return {
                        "effectiveSettings": {"model": "claude-sonnet-4-20250514"},
                        "modelConfig": {
                            "options": [
                                {"id": "claude-3-5-sonnet", "label": "Claude 3.5 Sonnet"},
                            ]
                        },
                        "reasoningConfig": {"options": []},
                        "permissionConfig": {"configurable": False, "modes": []},
                        "runtimeContext": {
                            "configOptions": [
                                {
                                    "id": "model",
                                    "currentValue": "claude-sonnet-4-20250514",
                                    "options": [
                                        {"value": "claude-sonnet-4-20250514", "name": "Sonnet 4"},
                                        {"value": "claude-opus-4-20250514", "name": "Opus 4"},
                                    ],
                                }
                            ]
                        },
                    }
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                payload = module.runner_options_payload(provider="claude-code", locale="en")

            self.assertEqual(
                [item["id"] for item in payload["models"]],
                ["claude-sonnet-4-20250514", "claude-opus-4-20250514"],
            )
            self.assertEqual(payload["currentModel"], "claude-sonnet-4-20250514")

    def test_runner_options_claude_code_returns_empty_models_from_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "claude-code",
                        "providers": [{"providerId": "claude-code", "availability": {"status": "available"}}],
                    }
                if args[:4] == ["agent", "composer-options", "--provider", "claude-code"]:
                    return {
                        "effectiveSettings": {
                            "model": "default",
                            "permissionModeId": "default",
                            "reasoningEffort": "high",
                        },
                        "modelConfig": {},
                        "reasoningConfig": {
                            "currentValue": "high",
                            "options": [{"id": "high", "label": "High", "value": "high"}],
                        },
                        "permissionConfig": {
                            "configurable": True,
                            "modes": [{"id": "default", "label": "Default"}],
                        },
                        "runtimeContext": {
                            "configOptions": [
                                {
                                    "id": "effort",
                                    "currentValue": "high",
                                    "options": [{"value": "high", "name": "High"}],
                                }
                            ]
                        },
                    }
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                payload = module.runner_options_payload(provider="claude-code", locale="en")

            self.assertEqual([item["id"] for item in payload["models"]], ["default"])
            self.assertEqual(payload["currentModel"], "default")

    def test_normalize_automation_requires_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "provider is required"):
                module.normalize_automation(
                    {
                        "name": "Review",
                        "prompt": "Review the workspace.",
                        "cwd": str(Path.cwd()),
                        "runnerSettings": {},
                        "runnerArgs": [],
                    }
                )

    def test_normalize_automation_allows_codex_default_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(Path.cwd()),
                    "runnerSettings": {"provider": "codex"},
                    "runnerArgs": [],
                }
            )

            self.assertEqual(item["runnerSettings"]["provider"], "codex")
            self.assertEqual(item["runnerSettings"]["model"], "")

    def test_normalize_automation_allows_claude_code_default_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(Path.cwd()),
                    "runnerSettings": {"provider": "claude-code"},
                    "runnerArgs": [],
                }
            )

            self.assertEqual(item["runnerSettings"]["provider"], "claude-code")
            self.assertEqual(item["runnerSettings"]["model"], "")

    def test_normalize_automation_allows_platform_catalog_providers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(Path.cwd()),
                    "runnerSettings": {"provider": "opencode"},
                    "runnerArgs": [],
                }
            )
            self.assertEqual(item["runnerSettings"]["provider"], "opencode")


class ScheduleNormalizationTest(unittest.TestCase):
    def test_daily_time_of_day_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(Path.cwd()),
                    "scheduleType": "daily",
                    "schedule": {"timeOfDay": "17:30"},
                    "runnerSettings": {"provider": "codex", "model": "gpt-5"},
                    "runnerArgs": [],
                }
            )

            self.assertEqual(item["schedule"], {"timeOfDay": "17:30"})

    def test_invalid_time_of_day_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "time-of-day must be HH:MM"):
                module.normalize_automation(
                    {
                        "name": "Review",
                        "prompt": "Review the workspace.",
                        "cwd": str(Path.cwd()),
                        "scheduleType": "daily",
                        "schedule": {"timeOfDay": "25:00"},
                        "runnerSettings": {"provider": "codex", "model": "gpt-5"},
                        "runnerArgs": [],
                    }
                )

    def test_cron_expression_is_preserved_from_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            payload = module.automation_payload_from_cli(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cron": "30 17 * * 1-5",
                    "provider": "codex",
                    "model": "gpt-5",
                }
            )
            item = module.normalize_automation(payload)

            self.assertEqual(item["scheduleType"], "cron")
            self.assertEqual(item["schedule"], {"expression": "30 17 * * 1-5"})

    def test_cron_schedule_requires_cron_expression_from_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "cron expression is required"):
                module.automation_payload_from_cli(
                    {
                        "name": "Review",
                        "prompt": "Review the workspace.",
                        "schedule-type": "cron",
                        "provider": "codex",
                        "model": "gpt-5",
                    }
                )

    def test_cron_schedule_rejects_time_of_day_from_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "cron schedules must use --cron"):
                module.automation_payload_from_cli(
                    {
                        "name": "Review",
                        "prompt": "Review the workspace.",
                        "schedule-type": "cron",
                        "time-of-day": "17:30",
                        "provider": "codex",
                        "model": "gpt-5",
                    }
                )

    def test_cli_defaults_to_ui_schedule_when_schedule_arguments_are_omitted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            payload = module.automation_payload_from_cli(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "schedule": "30 12 * * *",
                    "provider": "codex",
                    "model": "gpt-5",
                }
            )
            item = module.normalize_automation(payload)

            self.assertEqual(item["scheduleType"], "daily")
            self.assertEqual(item["schedule"], {"timeOfDay": "09:00"})

    def test_cli_create_defaults_provider_from_host_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=None):
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "claude-code",
                        "providers": [
                            {"providerId": "codex", "availability": {"status": "available"}},
                            {"providerId": "claude-code", "availability": {"status": "available"}},
                        ],
                    }
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                item = module.normalize_automation(
                    module.automation_payload_from_cli(
                        {
                            "name": "Review",
                            "prompt": "Review the workspace.",
                            "model": "gpt-5",
                        }
                    )
                )

            self.assertEqual(item["runnerSettings"]["provider"], "claude-code")
            self.assertEqual(item["runnerSettings"]["model"], "gpt-5")

    def test_cli_enabled_false_string_disables_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                module.automation_payload_from_cli(
                    {
                        "name": "Review",
                        "prompt": "Review the workspace.",
                        "enabled": "false",
                        "provider": "codex",
                    }
                )
            )

            self.assertFalse(item["enabled"])


class AgentSessionLaunchTest(unittest.TestCase):
    def test_manual_run_starts_agent_session_with_show_for_gui_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                return {"session": {"agentSessionId": "agent-session-1", "provider": "codex"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"provider": "codex"},
                "runnerArgs": [],
            }
            run = {
                "id": "run_123",
                "trigger": "manual",
                "prompt": "Review the workspace.",
                "cwd": str(Path.cwd()),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                session = module.start_agent_session(automation, run, log_file=None)

            self.assertEqual(session["agentSessionId"], "agent-session-1")
            self.assertEqual(calls[0][:2], ["agent", "start"])
            self.assertEqual(calls[0][calls[0].index("--provider") + 1], "codex")
            self.assertEqual(calls[0][calls[0].index("--title") + 1], "Review")
            self.assertEqual(
                calls[0][calls[0].index("--display-prompt") + 1],
                "Review the workspace.",
            )
            self.assertEqual(calls[0][calls[0].index("--show") + 1], "true")

    def test_scheduled_run_stays_visible_without_activating_agent_gui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                return {"session": {"agentSessionId": "agent-session-1", "provider": "codex"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"provider": "codex"},
                "runnerArgs": [],
            }
            run = {
                "id": "run_123",
                "trigger": "schedule",
                "prompt": "Review the workspace.",
                "cwd": str(Path.cwd()),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                session = module.start_agent_session(automation, run, log_file=None)

            self.assertEqual(session["agentSessionId"], "agent-session-1")
            self.assertEqual(calls[0][:2], ["agent", "start"])
            self.assertEqual(calls[0][calls[0].index("--provider") + 1], "codex")
            self.assertEqual(calls[0][calls[0].index("--title") + 1], "Review")
            self.assertEqual(
                calls[0][calls[0].index("--display-prompt") + 1],
                "Review the workspace.",
            )
            self.assertEqual(calls[0][calls[0].index("--show") + 1], "false")

    def test_claude_code_run_uses_generic_provider_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                return {"session": {"agentSessionId": "agent-session-1", "provider": "claude-code"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"provider": "claude-code"},
                "runnerArgs": [],
            }
            run = {
                "id": "run_123",
                "trigger": "manual",
                "prompt": "Review the workspace.",
                "cwd": str(Path.cwd()),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                session = module.start_agent_session(automation, run, log_file=None)

            self.assertEqual(session["agentSessionId"], "agent-session-1")
            self.assertEqual(calls[0][:2], ["agent", "start"])
            self.assertEqual(calls[0][calls[0].index("--provider") + 1], "claude-code")

    def test_runner_args_are_forwarded_without_duplicate_structured_flags(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                return {"session": {"agentSessionId": "agent-session-1", "provider": "codex"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"provider": "codex", "model": "gpt-5", "reasoningEffort": "high"},
                "runnerArgs": ["--model", "gpt-4", "--speed", "fast", "--reasoning-effort=low"],
            }
            run = {
                "id": "run_123",
                "trigger": "schedule",
                "prompt": "Review the workspace.",
                "cwd": str(Path.cwd()),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                module.start_agent_session(automation, run, log_file=None)

            self.assertEqual(calls[0].count("--model"), 1)
            self.assertEqual(calls[0][calls[0].index("--model") + 1], "gpt-5")
            self.assertEqual(calls[0].count("--reasoning-effort"), 1)
            self.assertIn("--speed", calls[0])
            self.assertIn("fast", calls[0])

    def test_runner_args_keep_supported_flags_when_not_set_structurally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                return {"session": {"agentSessionId": "agent-session-1", "provider": "codex"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"provider": "codex"},
                "runnerArgs": ["--reasoning-effort", "high", "--permission-mode", "full-access"],
            }
            run = {
                "id": "run_123",
                "trigger": "schedule",
                "prompt": "Review the workspace.",
                "cwd": str(Path.cwd()),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                module.start_agent_session(automation, run, log_file=None)

            self.assertIn("--reasoning-effort", calls[0])
            self.assertIn("high", calls[0])
            self.assertIn("--permission-mode", calls[0])
            self.assertIn("full-access", calls[0])

    def test_manual_runner_reopens_created_agent_session_after_persisting_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued")
            automation = module.STORE.get_automation(run["automationId"])
            opened = []

            def fake_open_agent_session(agent_session_id, log_file=None):
                stored = module.STORE.get_run(run["id"])
                self.assertEqual(stored["agentSessionId"], agent_session_id)
                opened.append(agent_session_id)

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session", fake_open_agent_session),
                mock.patch.object(module, "get_agent_session", return_value={"status": "running"}),
                mock.patch.object(
                    module,
                    "terminal_agent_status",
                    side_effect=[(None, None), ("failed", "stop")],
                ),
                mock.patch.object(module, "latest_agent_summary", return_value="Stopped."),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            self.assertEqual(opened, ["agent-session-1", "agent-session-1", "agent-session-1"])

    def test_scheduled_runner_does_not_open_created_agent_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session") as open_mock,
                mock.patch.object(module, "get_agent_session", return_value={"status": "ready"}),
                mock.patch.object(module, "COMPLETION_GRACE_SECONDS", 0),
                mock.patch.object(module, "agent_session_messages", return_value=[]),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            open_mock.assert_not_called()


class RunEventHubTest(unittest.TestCase):
    def test_run_event_hub_notifies_subscribers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            hub = module.RunEventHub()
            subscriber = hub.subscribe()
            hub.publish({"type": "run_started", "runId": "run_1"})
            self.assertEqual(subscriber.get_nowait()["runId"], "run_1")
            hub.unsubscribe(subscriber)
            hub.publish({"type": "run_finished", "runId": "run_1"})
            self.assertTrue(subscriber.empty())

    def test_automation_mutations_publish_change_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            subscriber = module.RUN_EVENTS.subscribe()
            try:
                automation = module.normalize_automation(
                    {
                        "name": "Review",
                        "prompt": "Review the workspace.",
                        "cwd": str(Path.cwd()),
                        "runnerSettings": {"provider": "codex", "model": "gpt-5"},
                    }
                )

                saved = module.save_automation_and_wake(automation)
                created = subscriber.get_nowait()

                self.assertEqual(created["type"], "automation_changed")
                self.assertEqual(created["action"], "created")
                self.assertEqual(created["automationId"], saved["id"])

                self.assertTrue(module.delete_automation_and_wake(saved["id"]))
                deleted = subscriber.get_nowait()

                self.assertEqual(deleted["type"], "automation_changed")
                self.assertEqual(deleted["action"], "deleted")
                self.assertEqual(deleted["automationId"], saved["id"])
            finally:
                module.RUN_EVENTS.unsubscribe(subscriber)


class SchedulerTest(unittest.TestCase):
    def test_scheduler_waits_until_next_scheduled_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime(2026, 1, 1, 3, 0, 0, tzinfo=module.timezone.utc)
            store = FakeSchedulerStore(now + module.timedelta(seconds=2.5))
            scheduler = module.Scheduler(store, FakeRunner(), autostart=False)

            self.assertEqual(scheduler.next_wait_seconds(now), 2.5)

    def test_scheduler_waits_without_timeout_when_no_scheduled_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime(2026, 1, 1, 3, 0, 0, tzinfo=module.timezone.utc)
            scheduler = module.Scheduler(FakeSchedulerStore(None), FakeRunner(), autostart=False)

            self.assertIsNone(scheduler.next_wait_seconds(now))

    def test_startup_due_scan_advances_without_enqueueing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime(2026, 1, 1, 3, 0, 0, tzinfo=module.timezone.utc)
            automation = fake_interval_automation(module, now - module.timedelta(minutes=30))
            store = FakeSchedulerStore(None, due_automations=[automation])
            runner = FakeRunner()
            scheduler = module.Scheduler(store, runner, autostart=False)

            scheduler.run_due_once(enqueue_due=False, now=now)

            self.assertEqual(runner.enqueued, [])
            self.assertEqual(len(store.saved), 1)
            self.assertEqual(
                store.saved[0]["nextRunAt"],
                (now + module.timedelta(minutes=15)).isoformat(),
            )

    def test_regular_due_scan_enqueues_scheduled_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime(2026, 1, 1, 3, 0, 0, tzinfo=module.timezone.utc)
            automation = fake_interval_automation(module, now)
            store = FakeSchedulerStore(None, due_automations=[automation])
            runner = FakeRunner()
            scheduler = module.Scheduler(store, runner, autostart=False)

            scheduler.run_due_once(now=now)

            self.assertEqual(runner.enqueued, [("aut_1", "schedule")])
            self.assertEqual(len(store.saved), 1)


class FakeSchedulerStore:
    def __init__(self, next_run_at, due_automations=None):
        self.next_run_at = next_run_at
        self.due_automations = due_automations or []
        self.saved = []

    def list_due_automations(self):
        return [dict(automation) for automation in self.due_automations]

    def next_scheduled_run_at(self):
        return self.next_run_at

    def save_automation(self, automation):
        self.saved.append(dict(automation))
        return automation


class FakeRunner:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, automation, trigger):
        self.enqueued.append((automation["id"], trigger))
        return {"id": "run_1"}


def fake_interval_automation(module, next_run_at):
    return {
        "id": "aut_1",
        "enabled": True,
        "scheduleType": "interval",
        "schedule": {"intervalMinutes": 15},
        "nextRunAt": next_run_at.isoformat(),
    }


class AgentGetLogCompactionTest(unittest.TestCase):
    def test_compact_agent_get_log_stdout_keeps_poll_summary_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            stdout = json.dumps(
                {
                    "session": {
                        "agentSessionId": "agent-session-1",
                        "status": "running",
                        "updatedAt": "2026-06-16T12:00:00+00:00",
                        "lastError": None,
                        "provider": "codex",
                        "runtimeContext": {
                            "skills": [{"name": "skill-a", "description": "x" * 5000}],
                            "configOptions": [{"id": "model", "currentValue": "gpt-5"}],
                        },
                        "messages": [{"role": "assistant", "payload": {"text": "hello"}}],
                    }
                }
            )

            compacted, meta = module.compact_agent_get_log_stdout(stdout)

            self.assertEqual(
                meta,
                {
                    "originalBytes": len(stdout.encode("utf-8")),
                    "omitted": ["runtimeContext", "messages"],
                },
            )
            self.assertIn('"agentSessionId": "agent-session-1"', compacted)
            self.assertIn('"status": "running"', compacted)
            self.assertIn('"updatedAt": "2026-06-16T12:00:00+00:00"', compacted)
            self.assertIn('"lastError": null', compacted)
            self.assertNotIn("skill-a", compacted)
            summary_json = compacted.split("[automation] stdout compacted:", 1)[0]
            self.assertNotIn("runtimeContext", summary_json)
            self.assertNotIn("messages", summary_json)
            self.assertIn("[automation] stdout compacted:", compacted)
            self.assertIn("omitted=runtimeContext,messages", compacted)

    def test_run_tutti_cli_compacts_agent_get_poll_stdout_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            large_stdout = json.dumps(
                {
                    "session": {
                        "agentSessionId": "agent-session-1",
                        "status": "running",
                        "runtimeContext": {"skills": [{"name": "skill-a"}]},
                    }
                }
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=large_stdout,
                stderr="",
            )

            with tempfile.NamedTemporaryFile(mode="w+b") as log_file:
                with mock.patch.object(module.subprocess, "run", return_value=completed):
                    module.run_tutti_cli(
                        ["agent", "get", "--session-id", "agent-session-1"],
                        log_file=log_file,
                    )
                log_file.seek(0)
                log_text = log_file.read().decode("utf-8")

            self.assertIn("Command: /usr/local/bin/tutti --json agent get --session-id agent-session-1", log_text)
            summary_json = log_text.split("[automation] stdout compacted:", 1)[0]
            self.assertNotIn("runtimeContext", summary_json)
            self.assertIn("[automation] stdout compacted:", log_text)

    def test_run_tutti_cli_keeps_full_stdout_for_non_poll_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            large_stdout = json.dumps(
                {
                    "session": {
                        "agentSessionId": "agent-session-1",
                        "runtimeContext": {"skills": [{"name": "skill-a"}]},
                    }
                }
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=large_stdout,
                stderr="",
            )

            with tempfile.NamedTemporaryFile(mode="w+b") as log_file:
                with mock.patch.object(module.subprocess, "run", return_value=completed):
                    module.run_tutti_cli(
                        ["codex", "start", "--prompt", "Review"],
                        log_file=log_file,
                    )
                log_file.seek(0)
                log_text = log_file.read().decode("utf-8")

            self.assertIn("runtimeContext", log_text)
            self.assertNotIn("[automation] stdout compacted:", log_text)


class AgentSessionSummaryTest(unittest.TestCase):
    def test_agent_session_messages_uses_session_summary_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "messages": [
                            {
                                "role": "assistant",
                                "version": 2,
                                "text": "Finished normally.",
                            }
                        ]
                    }
                ),
                stderr="",
            )

            with mock.patch.object(module.subprocess, "run", return_value=completed) as run_mock:
                messages = module.agent_session_messages("agent-session-1")

            self.assertEqual(
                messages,
                [{"role": "assistant", "version": 2, "text": "Finished normally."}],
            )
            command = run_mock.call_args.args[0]
            self.assertEqual(
                command,
                [
                    "/usr/local/bin/tutti",
                    "--json",
                    "agent",
                    "session-summary",
                    "--session-id",
                    "agent-session-1",
                    "--limit",
                    "80",
                ],
            )

    def test_latest_agent_summary_reads_session_summary_text_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            summary = module.latest_agent_summary_from_messages(
                [
                    {"role": "user", "version": 1, "text": "Do the task."},
                    {"role": "assistant", "version": 2, "text": "All done."},
                ]
            )
            self.assertEqual(summary, "All done.")

    def test_latest_agent_summary_uses_assistant_text_after_tool_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            summary = module.latest_agent_summary_from_messages(
                [
                    {"role": "user", "version": 1, "text": "Do the task."},
                    {
                        "kind": "text",
                        "role": "assistant",
                        "version": 2,
                        "text": "I’ll submit the automation run result as success.",
                    },
                    {
                        "kind": "tool_call",
                        "role": "assistant",
                        "version": 4,
                        "text": "tool_call: Bash",
                    },
                    {
                        "kind": "text",
                        "role": "assistant",
                        "version": 6,
                        "text": "All done.",
                    },
                ]
            )
            self.assertEqual(summary, "All done.")

    def test_latest_agent_summary_ignores_process_text_before_tool_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            summary = module.latest_agent_summary_from_messages(
                [
                    {"role": "user", "version": 1, "text": "Do the task."},
                    {
                        "kind": "text",
                        "role": "assistant",
                        "version": 2,
                        "text": "Understood. I’ll report the status now and then reply.",
                    },
                    {
                        "kind": "tool_call",
                        "role": "assistant",
                        "version": 4,
                        "text": "tool_call: Bash",
                    },
                ]
            )
            self.assertIsNone(summary)

    def test_wait_for_final_agent_summary_waits_for_text_after_tool_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            responses = [
                [
                    {
                        "kind": "text",
                        "role": "assistant",
                        "version": 2,
                        "text": "Understood. I’ll report the status now.",
                    },
                    {
                        "kind": "tool_call",
                        "role": "assistant",
                        "version": 4,
                        "text": "tool_call: Bash",
                    },
                ],
                [
                    {
                        "kind": "text",
                        "role": "assistant",
                        "version": 2,
                        "text": "Understood. I’ll report the status now.",
                    },
                    {
                        "kind": "tool_call",
                        "role": "assistant",
                        "version": 4,
                        "text": "tool_call: Bash",
                    },
                    {
                        "kind": "text",
                        "role": "assistant",
                        "version": 6,
                        "text": "Hi.",
                    },
                ],
            ]

            def fake_agent_session_messages(agent_session_id, log_file=None):
                self.assertEqual(agent_session_id, "agent-session-1")
                if len(responses) > 1:
                    return responses.pop(0)
                return responses[0]

            with (
                mock.patch.object(module, "agent_session_messages", fake_agent_session_messages),
                mock.patch.object(module, "FINAL_SUMMARY_GRACE_SECONDS", 1),
                mock.patch.object(module, "FINAL_SUMMARY_POLL_SECONDS", 0.01),
            ):
                summary = module.wait_for_final_agent_summary("agent-session-1")

            self.assertEqual(summary, "Hi.")

    def test_run_keeps_success_when_agent_summary_fetch_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued")
            automation = module.STORE.get_automation(run["automationId"])

            def fake_get_agent_session(agent_session_id, log_file=None):
                module.complete_run_from_cli({"run-id": run["id"], "status": "success"})
                return {"status": "ready"}

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "get_agent_session", side_effect=fake_get_agent_session),
                mock.patch.object(
                    module,
                    "latest_agent_summary",
                    side_effect=RuntimeError("unknown command: agent session messages"),
                ),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(stored["runStatus"], "succeeded")
            self.assertEqual(stored["taskStatus"], "success")
            self.assertIsNone(stored["summary"])
            self.assertIsNone(stored["error"])


class RunCompletionTest(unittest.TestCase):
    def test_agent_status_classification_treats_created_as_initial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            self.assertEqual(module.terminal_agent_status("ready"), ("succeeded", None))
            self.assertEqual(module.terminal_agent_status("idle"), ("succeeded", None))
            self.assertEqual(module.terminal_agent_status("created"), (None, None))
            self.assertTrue(module.initial_agent_status("created"))

    def test_turn_lifecycle_status_classification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            self.assertEqual(
                module.turn_lifecycle_agent_status(
                    {"phase": "running", "activeTurnId": "turn-1"}
                ),
                (None, None, True),
            )
            self.assertEqual(
                module.turn_lifecycle_agent_status(
                    {"phase": "waiting_approval", "activeTurnId": "turn-1"}
                ),
                ("failed", module.APPROVAL_REQUIRED_ERROR, True),
            )
            self.assertEqual(
                module.turn_lifecycle_agent_status({"phase": "settled", "outcome": "completed"}),
                ("succeeded", None, True),
            )
            self.assertEqual(
                module.turn_lifecycle_agent_status({"phase": "settled", "outcome": "failed"}),
                ("failed", None, True),
            )
            self.assertEqual(
                module.turn_lifecycle_agent_status({"phase": "settled", "outcome": "canceled"}),
                ("canceled", "Canceled by user.", True),
            )
            self.assertEqual(module.turn_lifecycle_agent_status(None), (None, None, False))

    def test_complete_run_updates_running_run_task_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_running_run(module)

            completed = module.complete_run_from_cli(
                {
                    "run-id": run["id"],
                    "status": "fail",
                }
            )

            self.assertEqual(completed["runStatus"], "running")
            self.assertEqual(completed["taskStatus"], "fail")
            self.assertIsNone(completed["summary"])
            self.assertIsNone(completed["error"])
            self.assertIsNone(completed["finishedAt"])
            self.assertNotIn("status", completed)
            self.assertNotIn("resultStatus", completed)
            self.assertNotIn("completionToken", completed)

    def test_complete_run_rejects_non_running_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_running_run(module)
            run["runStatus"] = "succeeded"
            run["finishedAt"] = module.now_iso()
            module.STORE.save_run(run)

            with self.assertRaisesRegex(ValueError, "run is not accepting completion"):
                module.complete_run_from_cli(
                    {
                        "run-id": run["id"],
                        "status": "success",
                    }
                )

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(stored["runStatus"], "succeeded")
            self.assertIsNone(stored["taskStatus"])

    def test_runner_ignores_initial_created_status_until_agent_responds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])
            polls = []
            message_reads = []

            def fake_get_agent_session(agent_session_id, log_file=None):
                polls.append(agent_session_id)
                if len(polls) == 1:
                    return {"status": "created"}
                if len(polls) == 2:
                    return {"status": "running"}
                module.complete_run_from_cli({"run-id": run["id"], "status": "success"})
                return {"status": "ready"}

            def fake_agent_session_messages(agent_session_id, log_file=None):
                message_reads.append(agent_session_id)
                if len(message_reads) == 1:
                    return [
                        {
                            "role": "user",
                            "version": 1,
                            "text": "Review",
                        }
                    ]
                return [
                    {
                        "role": "assistant",
                        "version": 2,
                        "text": "Done.",
                    }
                ]

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "get_agent_session", side_effect=fake_get_agent_session),
                mock.patch.object(module, "agent_session_messages", side_effect=fake_agent_session_messages),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(len(polls), 3)
            self.assertEqual(stored["runStatus"], "succeeded")
            self.assertEqual(stored["taskStatus"], "success")
            self.assertEqual(stored["summary"], "Done.")

    def test_runner_waits_for_active_turn_lifecycle_before_ready_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])
            polls = []

            def fake_get_agent_session(agent_session_id, log_file=None):
                polls.append(agent_session_id)
                if len(polls) == 1:
                    return {
                        "status": "ready",
                        "turnLifecycle": {"phase": "running", "activeTurnId": "turn-1"},
                    }
                module.complete_run_from_cli({"run-id": run["id"], "status": "success"})
                return {
                    "status": "ready",
                    "turnLifecycle": {"phase": "settled", "outcome": "completed"},
                }

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "get_agent_session", side_effect=fake_get_agent_session),
                mock.patch.object(
                    module,
                    "agent_session_messages",
                    return_value=[
                        {
                            "role": "assistant",
                            "version": 1,
                            "text": "Done.",
                        }
                    ],
                ),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(len(polls), 2)
            self.assertEqual(stored["runStatus"], "succeeded")
            self.assertEqual(stored["taskStatus"], "success")
            self.assertEqual(stored["summary"], "Done.")

    def test_runner_fails_approval_wait_even_with_active_turn_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])
            polls = []

            def fake_get_agent_session(agent_session_id, log_file=None):
                polls.append(agent_session_id)
                return {
                    "status": "waiting_approval",
                    "turnLifecycle": {"phase": "waiting", "activeTurnId": "turn-1"},
                }

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "get_agent_session", side_effect=fake_get_agent_session),
                mock.patch.object(module, "agent_session_messages", return_value=[]),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(len(polls), 1)
            self.assertEqual(stored["runStatus"], "failed")
            self.assertEqual(stored["taskStatus"], "fail")
            self.assertEqual(stored["error"], module.APPROVAL_REQUIRED_ERROR)

    def test_runner_times_out_active_turn_and_cancels_agent_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])
            polls = []

            def fake_get_agent_session(agent_session_id, log_file=None):
                polls.append(agent_session_id)
                return {
                    "status": "running",
                    "turnLifecycle": {"phase": "running", "activeTurnId": "turn-1"},
                }

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "get_agent_session", side_effect=fake_get_agent_session),
                mock.patch.object(module, "cancel_agent_session") as cancel_mock,
                mock.patch.object(module, "RUN_TIMEOUT_SECONDS", 1),
                mock.patch.object(module, "run_has_timed_out", side_effect=[False, True]),
                mock.patch.object(module, "latest_agent_summary", return_value=None),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(polls, ["agent-session-1"])
            cancel_mock.assert_called_once_with("agent-session-1")
            self.assertEqual(stored["runStatus"], "timed_out")
            self.assertEqual(stored["taskStatus"], "fail")
            self.assertEqual(stored["error"], module.run_timeout_error(1))

    def test_complete_run_repairs_missing_task_status_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_running_run(module)
            finished_at = module.now_iso()
            run["runStatus"] = "failed"
            run["finishedAt"] = finished_at
            run["error"] = module.MISSING_TASK_STATUS_ERROR
            run["taskStatus"] = "fail"
            module.STORE.save_run(run)

            completed = module.complete_run_from_cli(
                {
                    "run-id": run["id"],
                    "status": "success",
                }
            )

            self.assertEqual(completed["runStatus"], "succeeded")
            self.assertEqual(completed["taskStatus"], "success")
            self.assertIsNone(completed["error"])
            self.assertEqual(completed["finishedAt"], finished_at)

    def test_final_run_save_does_not_overwrite_submitted_task_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_running_run(module)
            run["agentSessionId"] = "agent-session-1"
            module.STORE.save_run(run)

            module.complete_run_from_cli(
                {
                    "run-id": run["id"],
                    "status": "success",
                }
            )
            run["runStatus"] = "succeeded"
            run["finishedAt"] = module.now_iso()
            run["summary"] = "Done"
            run["taskStatus"] = "fail"
            saved = module.STORE.save_run(run)

            self.assertEqual(saved["runStatus"], "succeeded")
            self.assertEqual(saved["taskStatus"], "success")
            self.assertEqual(saved["summary"], "Done")

    def test_runner_fails_succeeded_session_without_submitted_task_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued")
            automation = module.STORE.get_automation(run["automationId"])

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "get_agent_session", return_value={"status": "ready"}),
                mock.patch.object(module, "COMPLETION_GRACE_SECONDS", 0),
                mock.patch.object(
                    module,
                    "agent_session_messages",
                    return_value=[
                        {
                            "role": "assistant",
                            "version": 1,
                            "payload": {"text": "Finished normally."},
                        }
                    ],
                ),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(stored["runStatus"], "failed")
            self.assertEqual(stored["taskStatus"], "fail")
            self.assertEqual(stored["summary"], "Finished normally.")
            self.assertEqual(
                stored["error"],
                "Automation task did not submit a task status.",
            )

    def test_runner_keeps_run_running_until_late_completion_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued")
            automation = module.STORE.get_automation(run["automationId"])
            ready_seen = threading.Event()

            def fake_get_agent_session(agent_session_id, log_file=None):
                ready_seen.set()
                return {"status": "ready"}

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={"agentSessionId": "agent-session-1", "provider": "codex"},
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "get_agent_session", fake_get_agent_session),
                mock.patch.object(module, "COMPLETION_GRACE_SECONDS", 2),
                mock.patch.object(module, "COMPLETION_GRACE_POLL_SECONDS", 0.05),
                mock.patch.object(
                    module,
                    "agent_session_messages",
                    return_value=[
                        {
                            "role": "assistant",
                            "version": 1,
                            "payload": {"text": "Nothing to do."},
                        }
                    ],
                ),
            ):
                thread = threading.Thread(
                    target=module.Runner(module.STORE).run,
                    args=(run["id"], automation),
                )
                thread.start()
                self.assertTrue(ready_seen.wait(timeout=5))
                time.sleep(0.1)

                stored = module.STORE.get_run(run["id"])
                self.assertEqual(stored["runStatus"], "running")
                self.assertIsNone(stored["taskStatus"])

                module.complete_run_from_cli(
                    {
                        "run-id": run["id"],
                        "status": "skip",
                    }
                )
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(stored["runStatus"], "succeeded")
            self.assertEqual(stored["taskStatus"], "skip")
            self.assertEqual(stored["summary"], "Nothing to do.")
            self.assertIsNone(stored["error"])

    def test_run_prompt_instructs_markdown_final_response_and_completion_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = {
                "id": "run_123",
                "artifactDir": str(Path(temp_dir) / "artifacts"),
            }

            prompt = module.build_run_prompt("Review the repository.", run)

            self.assertIn("automation complete-run", prompt)
            self.assertIn("--run-id run_123", prompt)
            self.assertNotIn("--token", prompt)
            self.assertIn("--status <status>", prompt)
            self.assertIn("send the user-facing result directly", prompt)
            self.assertIn("Do not wrap the final response in JSON", prompt)
            self.assertIn("do not write it to an intermediate file", prompt)
            self.assertNotIn("summary-file", prompt)


def make_running_run(module):
    return make_run(module, "running")


def make_run(module, run_status, trigger="manual"):
    automation = module.STORE.save_automation(
        module.normalize_automation(
            {
                "name": "Test automation",
                "prompt": "Review",
                "cwd": str(Path.cwd()),
                "enabled": False,
                "scheduleType": "daily",
                "schedule": {"timeOfDay": "09:00"},
                "concurrency": "queue",
                "runnerSettings": {"provider": "codex", "model": "gpt-5"},
                "runnerArgs": [],
                "env": {},
            }
        )
    )
    return module.STORE.save_run(
        {
            "id": "run_123",
            "automationId": automation["id"],
            "trigger": trigger,
            "runStatus": run_status,
            "prompt": "Review",
            "cwd": str(Path.cwd()),
            "queuedAt": module.now_iso(),
            "startedAt": module.now_iso() if run_status != "queued" else None,
            "artifactDir": str(Path(module.LOG_DIR) / "runs" / automation["id"] / "run_123"),
        }
    )


if __name__ == "__main__":
    unittest.main()
