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


def agent_catalog(default_agent_target_id="local:codex", agents=None):
    if agents is None:
        agents = [
            ("local:codex", "codex", "Primary Agent", "available"),
            ("local:reviewer", "review-provider", "Review Agent", "available"),
        ]
    return {
        "schemaVersion": 1,
        "defaultAgentTargetId": default_agent_target_id,
        "agents": [
            {
                "id": target_id,
                "provider": provider_id,
                "name": name,
                "availability": {"status": status},
            }
            for target_id, provider_id, name, status in agents
        ],
    }


def normalized_agent_catalog(default_agent_target_id="local:codex", agents=None, cli_contract="agent-id"):
    if agents is None:
        agents = [
            ("local:codex", "codex", "Primary Agent", "available"),
            ("local:reviewer", "review-provider", "Review Agent", "available"),
        ]
    return {
        "schemaVersion": 1,
        "cliContract": cli_contract,
        "defaultAgentTargetId": default_agent_target_id,
        "agents": [
            {
                "agentTargetId": target_id,
                "providerId": provider_id,
                "displayName": name,
                "status": status,
                "detail": "",
            }
            for target_id, provider_id, name, status in agents
        ],
    }


class TuttiCLIInvocationTest(unittest.TestCase):
    def test_native_cli_path_is_passed_directly_to_subprocess(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            module = load_server_module(root)
            try:
                executable = root / "Tutti CLI" / "tutti.exe"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"")
                with mock.patch.dict(
                    os.environ, {"TUTTI_CLI": str(executable)}, clear=False
                ), mock.patch.object(module.subprocess, "run") as run:
                    run.return_value = mock.Mock(returncode=0, stdout="{}", stderr="")
                    module.run_tutti_cli(["agent", "list"])

                self.assertEqual(
                    run.call_args.args[0],
                    [str(executable), "--json", "agent", "list"],
                )
                self.assertNotIn("shell", run.call_args.kwargs)
            finally:
                module.STORE.db.close()

    def test_windows_rejects_batch_tutti_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            try:
                shim = Path(temp_dir) / "tutti.cmd"
                shim.write_text("@echo off\r\n", encoding="utf-8")
                with mock.patch.dict(os.environ, {"TUTTI_CLI": str(shim)}):
                    with self.assertRaisesRegex(RuntimeError, "absolute .exe"):
                        module.tutti_cli_command(platform="nt")
            finally:
                module.STORE.db.close()


class AutomationHTTPServerTest(unittest.TestCase):
    def test_expected_client_disconnect_does_not_emit_a_server_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            try:
                server = object.__new__(module.AutomationHTTPServer)
                with mock.patch.object(module.ThreadingHTTPServer, "handle_error") as fallback:
                    try:
                        raise ConnectionAbortedError("client closed the connection")
                    except ConnectionAbortedError:
                        server.handle_error(None, ("127.0.0.1", 1234))
                fallback.assert_not_called()
            finally:
                module.STORE.db.close()

    def test_unexpected_request_error_uses_the_standard_error_handler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            try:
                server = object.__new__(module.AutomationHTTPServer)
                with mock.patch.object(module.ThreadingHTTPServer, "handle_error") as fallback:
                    try:
                        raise RuntimeError("unexpected")
                    except RuntimeError:
                        server.handle_error(None, ("127.0.0.1", 1234))
                fallback.assert_called_once_with(None, ("127.0.0.1", 1234))
            finally:
                module.STORE.db.close()


class RunnerOptionsPayloadTest(unittest.TestCase):
    def test_agent_catalog_falls_back_only_for_exact_unknown_agent_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    raise RuntimeError("unknown command: agent list")
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "legacy-runtime",
                        "providers": [
                            {
                                "agentTargetId": "local:legacy",
                                "providerId": "legacy-runtime",
                                "displayName": "Legacy Agent",
                                "availability": {"status": "available"},
                            }
                        ],
                    }
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                catalog = module.agent_catalog_payload()

            self.assertEqual(calls, [["agent", "list"], ["agent", "providers"]])
            self.assertEqual(catalog["cliContract"], "provider-compat")
            self.assertEqual(catalog["defaultAgentTargetId"], "local:legacy")

    def test_agent_catalog_does_not_fallback_for_ordinary_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                raise RuntimeError("daemon unavailable")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                with self.assertRaisesRegex(RuntimeError, "daemon unavailable"):
                    module.agent_catalog_payload()

            self.assertEqual(calls, [["agent", "list"]])

    def test_runner_options_returns_catalog_when_no_agent_is_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            with mock.patch.object(
                module,
                "run_tutti_cli",
                return_value=agent_catalog(
                    default_agent_target_id="local:offline",
                    agents=[
                        ("local:offline", "offline-runtime", "Offline Agent", "unavailable")
                    ],
                ),
            ):
                payload = module.runner_options_payload()

            self.assertFalse(payload["available"])
            self.assertEqual(payload["agentTargetId"], "")
            self.assertEqual(payload["agents"][0]["agentTargetId"], "local:offline")

    def test_legacy_provider_resolution_fails_closed_when_full_catalog_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            catalog = module.normalize_agent_catalog_entries(
                agent_catalog(
                    agents=[
                        ("local:one", "shared-runtime", "One", "available"),
                        ("local:two", "shared-runtime", "Two", "unavailable"),
                    ]
                )["agents"],
                "id",
                "provider",
                "name",
            )
            normalized = module.normalize_agent_catalog(catalog, "local:one", "agent-id")

            with self.assertRaisesRegex(ValueError, "multiple Agent Targets"):
                module.resolve_agent_target_from_catalog(
                    normalized,
                    legacy_provider="shared-runtime",
                    require_available=True,
                )

    def test_old_daemon_cannot_downgrade_an_exact_target_to_ambiguous_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            agents = module.normalize_agent_catalog_entries(
                agent_catalog(
                    agents=[
                        ("local:one", "shared-runtime", "One", "available"),
                        ("local:two", "shared-runtime", "Two", "unavailable"),
                    ]
                )["agents"],
                "id",
                "provider",
                "name",
            )
            catalog = module.normalize_agent_catalog(agents, "local:one", "provider-compat")

            self.assertEqual(catalog["defaultAgentTargetId"], "")
            with self.assertRaisesRegex(ValueError, "old daemon cannot select"):
                module.resolve_agent_target_from_catalog(
                    catalog,
                    agent_target_id="local:one",
                    require_available=True,
                )

    def test_runner_options_uses_cli_locale_and_structured_configs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    return agent_catalog()
                if args == [
                    "agent",
                    "composer-options",
                    "--agent-id",
                    "local:codex",
                    "--cwd",
                    str(module.AGENT_WORK_DIR),
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
                payload = module.runner_options_payload(agent_target_id="local:codex", locale="zh-CN")

            self.assertEqual(
                calls[1],
                [
                    "agent",
                    "composer-options",
                    "--agent-id",
                    "local:codex",
                    "--cwd",
                    str(module.AGENT_WORK_DIR),
                    "--locale",
                    "zh-CN",
                ],
            )
            self.assertEqual(payload["defaultAgentTargetId"], "local:codex")
            self.assertEqual(payload["agentTargetId"], "local:codex")
            self.assertEqual(payload["currentModel"], "gpt-5")
            self.assertEqual(payload["currentReasoningLevel"], "high")
            self.assertEqual(payload["permissionMode"], "full-access")
            self.assertEqual(payload["models"][0]["label"], "GPT-5")
            self.assertEqual(payload["models"][0]["reasoningLevels"][0]["label"], "高")
            self.assertEqual(
                payload["permissionConfig"]["modes"][0]["label"],
                "代我批准",
            )

    def test_default_cwd_uses_a_runtime_agent_workspace_not_app_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex"},
                    "runnerArgs": [],
                }
            )

            self.assertEqual(item["cwd"], str(module.AGENT_WORK_DIR))
            self.assertTrue(module.AGENT_WORK_DIR.is_dir())
            self.assertEqual(module.AGENT_WORK_DIR.parent, module.RUNTIME_DIR)
            self.assertNotEqual(module.AGENT_WORK_DIR, module.DATA_DIR)
            self.assertEqual(module.DB_PATH.parent, module.DATA_DIR)
            self.assertEqual(module.cwd_options_payload()["cwd"], str(module.AGENT_WORK_DIR))
            self.assertEqual(module.context_payload()["agentWorkDir"], str(module.AGENT_WORK_DIR))

    def test_explicit_cwd_is_canonical_and_may_use_an_existing_external_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            explicit_cwd = module.AGENT_WORK_DIR / "project"
            explicit_cwd.mkdir()

            self.assertEqual(module.clean_cwd(str(explicit_cwd)), str(explicit_cwd.resolve()))
            self.assertEqual(module.clean_cwd("project"), str(explicit_cwd.resolve()))
            with self.assertRaisesRegex(ValueError, "cwd must be an existing directory"):
                module.clean_cwd("missing")

            sibling = module.AGENT_WORK_DIR.parent / "outside"
            sibling.mkdir()
            for value in (str(sibling), "../outside"):
                self.assertEqual(module.clean_cwd(value), str(sibling.resolve()))

            escape = module.AGENT_WORK_DIR / "escape"
            escape.symlink_to(sibling, target_is_directory=True)
            self.assertEqual(module.clean_cwd(str(escape)), str(sibling.resolve()))

    def test_runner_options_prefers_runtime_context_model_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "list"]:
                    return agent_catalog(agents=[("local:codex", "codex", "Primary Agent", "available")])
                if args[:4] == ["agent", "composer-options", "--agent-id", "local:codex"]:
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
                payload = module.runner_options_payload(agent_target_id="local:codex", locale="en")

            self.assertEqual([item["id"] for item in payload["models"]], ["gpt-5", "gpt-5.1"])
            self.assertEqual(payload["currentModel"], "gpt-5")

    def test_runner_options_falls_back_when_composer_options_times_out(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            timeouts = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "list"]:
                    return agent_catalog()
                if args[:4] == ["agent", "composer-options", "--agent-id", "local:codex"]:
                    timeouts.append(timeout)
                    raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
                raise AssertionError(f"unexpected CLI args: {args!r}")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                payload = module.runner_options_payload(agent_target_id="local:codex", locale="en")

            self.assertEqual(timeouts, [module.RUNNER_OPTIONS_COMPOSER_TIMEOUT_SECONDS])
            self.assertTrue(payload["available"])
            self.assertEqual(payload["agentTargetId"], "local:codex")
            self.assertTrue(payload["optionsUnavailable"])
            self.assertEqual(payload["models"], [])
            self.assertEqual(payload["currentModel"], "")

    def test_runner_options_fallback_does_not_invent_a_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            payload = module.fallback_agent_composer_options("local:reviewer")

            self.assertTrue(payload["optionsUnavailable"])
            self.assertEqual(payload["models"], [])
            self.assertEqual(payload["currentModel"], "")

    def test_runner_options_prefers_runtime_context_over_model_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "list"]:
                    return agent_catalog("local:reviewer", [("local:reviewer", "review-provider", "Review Agent", "available")])
                if args[:4] == ["agent", "composer-options", "--agent-id", "local:reviewer"]:
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
                payload = module.runner_options_payload(agent_target_id="local:reviewer", locale="en")

            self.assertEqual(
                [item["id"] for item in payload["models"]],
                ["claude-sonnet-4-20250514", "claude-opus-4-20250514"],
            )
            self.assertEqual(payload["currentModel"], "claude-sonnet-4-20250514")

    def test_runner_options_claude_code_returns_empty_models_from_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "list"]:
                    return agent_catalog("local:reviewer", [("local:reviewer", "review-provider", "Review Agent", "available")])
                if args[:4] == ["agent", "composer-options", "--agent-id", "local:reviewer"]:
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
                payload = module.runner_options_payload(agent_target_id="local:reviewer", locale="en")

            self.assertEqual([item["id"] for item in payload["models"]], ["default"])
            self.assertEqual(payload["currentModel"], "default")

    def test_normalize_automation_requires_agent_target_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "agentTargetId is required"):
                module.normalize_automation(
                    {
                        "name": "Review",
                        "prompt": "Review the workspace.",
                        "cwd": str(module.AGENT_WORK_DIR),
                        "runnerSettings": {},
                        "runnerArgs": [],
                    }
                )

    def test_normalize_automation_allows_agent_default_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(module.AGENT_WORK_DIR),
                    "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex"},
                    "runnerArgs": [],
                }
            )

            self.assertEqual(item["runnerSettings"]["agentTargetId"], "local:codex")
            self.assertEqual(item["runnerSettings"]["model"], "")

    def test_normalize_automation_allows_another_agent_default_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(module.AGENT_WORK_DIR),
                    "runnerSettings": {"agentTargetId": "local:reviewer", "providerId": "review-provider"},
                    "runnerArgs": [],
                }
            )

            self.assertEqual(item["runnerSettings"]["agentTargetId"], "local:reviewer")
            self.assertEqual(item["runnerSettings"]["model"], "")

    def test_normalize_automation_allows_platform_catalog_agents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(module.AGENT_WORK_DIR),
                    "runnerSettings": {"agentTargetId": "local:opencode", "providerId": "opencode"},
                    "runnerArgs": [],
                }
            )
            self.assertEqual(item["runnerSettings"]["agentTargetId"], "local:opencode")


class ScheduleNormalizationTest(unittest.TestCase):
    def test_daily_time_of_day_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            item = module.normalize_automation(
                {
                    "name": "Review",
                    "prompt": "Review the workspace.",
                    "cwd": str(module.AGENT_WORK_DIR),
                    "scheduleType": "daily",
                    "schedule": {"timeOfDay": "17:30"},
                    "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex", "model": "gpt-5"},
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
                        "cwd": str(module.AGENT_WORK_DIR),
                        "scheduleType": "daily",
                        "schedule": {"timeOfDay": "25:00"},
                        "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex", "model": "gpt-5"},
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

    def test_cli_create_defaults_agent_target_from_host_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))

            def fake_run_tutti_cli(args, timeout=None):
                if args == ["agent", "list"]:
                    return agent_catalog("local:reviewer")
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

            self.assertEqual(item["runnerSettings"]["agentTargetId"], "local:reviewer")
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
    def test_old_daemon_uses_provider_only_after_unique_target_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    raise RuntimeError("unknown command: agent list")
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "legacy-runtime",
                        "providers": [
                            {
                                "agentTargetId": "local:legacy",
                                "providerId": "legacy-runtime",
                                "displayName": "Legacy Agent",
                                "availability": {"status": "available"},
                            }
                        ],
                    }
                return {
                    "session": {
                        "agentSessionId": "agent-session-1",
                        "provider": "legacy-runtime",
                    }
                }

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"provider": "legacy-runtime"},
                "runnerArgs": [],
                "_agentCliContract": "provider-compat",
            }
            run = {
                "id": "run_123",
                "trigger": "manual",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "local:legacy",
                "agentProvider": "legacy-runtime",
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                session = module.start_agent_session(automation, run, log_file=None)

            start_call = calls[-1]
            self.assertEqual(start_call[start_call.index("--provider") + 1], "legacy-runtime")
            self.assertNotIn("--agent-id", start_call)
            self.assertEqual(session["agentTargetId"], "local:legacy")

    def test_open_agent_session_rejects_a_different_target_before_opening(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args[:2] == ["agent", "get"]:
                    return {
                        "session": {
                            "agentSessionId": "agent-session-1",
                            "agentTargetId": "local:other",
                        }
                    }
                raise AssertionError("open must not be called after an identity mismatch")

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                with self.assertRaisesRegex(RuntimeError, "target mismatch"):
                    module.open_agent_session("agent-session-1", "local:expected")

            self.assertEqual(len(calls), 1)

    def test_legacy_run_provider_maps_to_one_exact_target_before_opening(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with mock.patch.object(
                module,
                "agent_catalog_payload",
                return_value=module.normalize_agent_catalog(
                    module.normalize_agent_catalog_entries(
                        agent_catalog(
                            agents=[
                                ("local:legacy", "legacy-runtime", "Legacy Agent", "available")
                            ]
                        )["agents"],
                        "id",
                        "provider",
                        "name",
                    ),
                    "local:legacy",
                    "agent-id",
                ),
            ):
                target_id = module.resolve_run_agent_target_id(
                    {"agentTargetId": None, "agentProvider": "legacy-runtime"}
                )

            self.assertEqual(target_id, "local:legacy")

    def test_manual_run_starts_agent_session_with_show_for_gui_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    return agent_catalog()
                return {
                    "turnId": "turn-1",
                    "session": {
                        "agentSessionId": "agent-session-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                }

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex"},
                "runnerArgs": [],
                "_agentCliContract": "agent-id",
            }
            run = {
                "id": "run_123",
                "trigger": "manual",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "local:codex",
                "agentProvider": "codex",
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                session = module.start_agent_session(automation, run, log_file=None)

            self.assertEqual(session["agentSessionId"], "agent-session-1")
            self.assertEqual(session["turnId"], "turn-1")
            start_call = next(call for call in calls if call[:2] == ["agent", "start"])
            self.assertEqual(start_call[:2], ["agent", "start"])
            self.assertEqual(start_call[start_call.index("--agent-id") + 1], "local:codex")
            self.assertEqual(start_call[start_call.index("--title") + 1], "Review")
            self.assertEqual(
                start_call[start_call.index("--display-prompt") + 1],
                "Review the workspace.",
            )
            self.assertIn("--show", start_call)

    def test_scheduled_run_stays_visible_without_activating_agent_gui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    return agent_catalog()
                return {"session": {"agentSessionId": "agent-session-1", "agentTargetId": "local:codex", "provider": "codex"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex"},
                "runnerArgs": [],
                "_agentCliContract": "agent-id",
            }
            run = {
                "id": "run_123",
                "trigger": "schedule",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "local:codex",
                "agentProvider": "codex",
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                session = module.start_agent_session(automation, run, log_file=None)

            self.assertEqual(session["agentSessionId"], "agent-session-1")
            start_call = next(call for call in calls if call[:2] == ["agent", "start"])
            self.assertEqual(start_call[:2], ["agent", "start"])
            self.assertEqual(start_call[start_call.index("--agent-id") + 1], "local:codex")
            self.assertEqual(start_call[start_call.index("--title") + 1], "Review")
            self.assertEqual(
                start_call[start_call.index("--display-prompt") + 1],
                "Review the workspace.",
            )
            self.assertNotIn("--show", start_call)

    def test_second_agent_uses_generic_agent_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    return agent_catalog("local:reviewer")
                return {"session": {"agentSessionId": "agent-session-1", "agentTargetId": "local:reviewer", "provider": "review-provider"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"agentTargetId": "local:reviewer", "providerId": "review-provider"},
                "runnerArgs": [],
                "_agentCliContract": "agent-id",
            }
            run = {
                "id": "run_123",
                "trigger": "manual",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "local:reviewer",
                "agentProvider": "review-provider",
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                session = module.start_agent_session(automation, run, log_file=None)

            self.assertEqual(session["agentSessionId"], "agent-session-1")
            start_call = next(call for call in calls if call[:2] == ["agent", "start"])
            self.assertEqual(start_call[start_call.index("--agent-id") + 1], "local:reviewer")

    def test_runner_args_are_forwarded_without_duplicate_structured_flags(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    return agent_catalog()
                return {"session": {"agentSessionId": "agent-session-1", "agentTargetId": "local:codex", "provider": "codex"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex", "model": "gpt-5", "reasoningEffort": "high"},
                "runnerArgs": ["--model", "gpt-4", "--speed", "fast", "--reasoning-effort=low"],
                "_agentCliContract": "agent-id",
            }
            run = {
                "id": "run_123",
                "trigger": "schedule",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "local:codex",
                "agentProvider": "codex",
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                module.start_agent_session(automation, run, log_file=None)

            start_call = next(call for call in calls if call[:2] == ["agent", "start"])
            self.assertEqual(start_call.count("--model"), 1)
            self.assertEqual(start_call[start_call.index("--model") + 1], "gpt-5")
            self.assertEqual(start_call.count("--reasoning-effort"), 1)
            self.assertIn("--speed", start_call)
            self.assertIn("fast", start_call)

    def test_runner_args_keep_supported_flags_when_not_set_structurally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    return agent_catalog()
                return {"session": {"agentSessionId": "agent-session-1", "agentTargetId": "local:codex", "provider": "codex"}}

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex"},
                "runnerArgs": ["--reasoning-effort", "high", "--permission-mode", "full-access"],
                "_agentCliContract": "agent-id",
            }
            run = {
                "id": "run_123",
                "trigger": "schedule",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "local:codex",
                "agentProvider": "codex",
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                module.start_agent_session(automation, run, log_file=None)

            start_call = next(call for call in calls if call[:2] == ["agent", "start"])
            self.assertIn("--reasoning-effort", start_call)
            self.assertIn("high", start_call)
            self.assertIn("--permission-mode", start_call)
            self.assertIn("full-access", start_call)

    def test_manual_runner_reopens_created_agent_session_after_persisting_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued")
            automation = module.STORE.get_automation(run["automationId"])
            opened = []

            def fake_open_agent_session(agent_session_id, expected_agent_target_id=None, log_file=None):
                stored = module.STORE.get_run(run["id"])
                self.assertEqual(stored["agentSessionId"], agent_session_id)
                opened.append(agent_session_id)

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={
                        "agentSessionId": "agent-session-1",
                        "turnId": "turn-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                ),
                mock.patch.object(module, "open_agent_session", fake_open_agent_session),
                mock.patch.object(
                    module,
                    "wait_for_agent_stop",
                    return_value=("failed", "stop", "Stopped."),
                ),
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
                    return_value={
                        "agentSessionId": "agent-session-1",
                        "turnId": "turn-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                ),
                mock.patch.object(module, "open_agent_session") as open_mock,
                mock.patch.object(module, "COMPLETION_GRACE_SECONDS", 0),
                mock.patch.object(
                    module,
                    "wait_for_agent_stop",
                    return_value=("succeeded", None, None),
                ),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            open_mock.assert_not_called()


class RunAgentSnapshotTest(unittest.TestCase):
    def save_automation(self, module):
        return module.STORE.save_automation(
            module.normalize_automation(
                {
                    "name": "Snapshot test",
                    "prompt": "Review",
                    "cwd": str(module.AGENT_WORK_DIR),
                    "enabled": False,
                    "scheduleType": "daily",
                    "schedule": {"timeOfDay": "09:00"},
                    "concurrency": "queue",
                    "runnerSettings": {
                        "agentTargetId": "local:reviewer",
                        "providerId": "stale-provider",
                    },
                    "runnerArgs": [],
                    "env": {},
                }
            )
        )

    def enqueue_without_worker(self, module, runner, automation):
        thread = mock.Mock()
        with (
            mock.patch.object(
                module,
                "agent_catalog_payload",
                return_value=normalized_agent_catalog(),
            ),
            mock.patch.object(module.threading, "Thread", return_value=thread),
        ):
            run = runner.enqueue(automation, "manual")
        thread.start.assert_called_once_with()
        return run

    def test_start_validates_provider_against_enqueued_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {
                    "agentTargetId": "local:codex",
                    "providerId": "codex",
                },
                "runnerArgs": [],
                "_agentCliContract": "agent-id",
            }
            run = {
                "id": "run_123",
                "trigger": "manual",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "local:codex",
                "agentProvider": "codex",
            }

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                if args == ["agent", "list"]:
                    return agent_catalog()
                return {
                    "session": {
                        "agentSessionId": "agent-session-1",
                        "agentTargetId": "local:codex",
                        "provider": "different-provider",
                    }
                }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                with self.assertRaisesRegex(RuntimeError, "provider mismatch"):
                    module.start_agent_session(automation, run, log_file=None)

    def test_queued_legacy_target_revalidates_current_full_catalog_before_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            calls = []

            def fake_run_tutti_cli(args, timeout=30, log_file=None):
                calls.append(args)
                if args == ["agent", "list"]:
                    raise RuntimeError("unknown command: agent list")
                if args == ["agent", "providers"]:
                    return {
                        "schemaVersion": 2,
                        "defaultProviderId": "shared-runtime",
                        "providers": [
                            {
                                "agentTargetId": "team:queued",
                                "providerId": "shared-runtime",
                                "displayName": "Queued Agent",
                                "availability": {"status": "available"},
                            },
                            {
                                "agentTargetId": "team:new-sibling",
                                "providerId": "shared-runtime",
                                "displayName": "New Sibling",
                                "availability": {"status": "available"},
                            },
                        ],
                    }
                raise AssertionError("agent start must not run after provider ambiguity")

            automation = {
                "name": "Review",
                "prompt": "Review the workspace.",
                "runnerSettings": {"agentTargetId": "team:queued"},
                "runnerArgs": [],
                "_agentCliContract": "provider-compat",
            }
            run = {
                "id": "run_queued",
                "trigger": "schedule",
                "prompt": "Review the workspace.",
                "cwd": str(module.AGENT_WORK_DIR),
                "artifactDir": str(Path(temp_dir) / "artifacts"),
                "agentTargetId": "team:queued",
                "agentProvider": "shared-runtime",
            }

            with mock.patch.object(module, "run_tutti_cli", fake_run_tutti_cli):
                with self.assertRaisesRegex(ValueError, "maps to multiple Agent Targets"):
                    module.start_agent_session(automation, run, log_file=None)

            self.assertFalse(any(call[:2] == ["agent", "start"] for call in calls))

    def test_canceled_before_start_keeps_exact_agent_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)

            run = self.enqueue_without_worker(module, runner, automation)
            canceled = runner.cancel(run["id"])

            self.assertEqual(canceled["runStatus"], "canceled")
            self.assertEqual(canceled["agentTargetId"], "local:reviewer")
            self.assertEqual(canceled["agentProvider"], "review-provider")

    def test_running_cancel_resolves_and_cancels_the_active_turn(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)
            run = self.enqueue_without_worker(module, runner, automation)
            run["runStatus"] = "running"
            run["agentSessionId"] = "agent-session-1"
            module.STORE.save_run(run)

            with mock.patch.object(module, "cancel_active_agent_turn") as cancel_mock:
                canceled = runner.cancel(run["id"])

            self.assertEqual(canceled["runStatus"], "canceling")
            cancel_mock.assert_called_once_with(
                "agent-session-1",
                "local:reviewer",
            )

    def test_scheduled_enqueue_failure_is_persisted_with_requested_exact_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)

            failed = runner.record_enqueue_failure(
                automation,
                "schedule",
                ValueError("Agent Target is unavailable"),
            )

            self.assertEqual(failed["runStatus"], "failed")
            self.assertEqual(failed["agentTargetId"], "local:reviewer")
            self.assertIsNone(failed["agentProvider"])
            self.assertEqual(failed["error"], "Agent Target is unavailable")
            self.assertIsNotNone(failed["finishedAt"])

    def test_enqueue_accepts_existing_persisted_cwd_outside_agent_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            outside = Path(temp_dir) / "legacy-workspace"
            outside.mkdir()
            automation["cwd"] = str(outside)
            runner = module.Runner(module.STORE)

            run = self.enqueue_without_worker(module, runner, automation)

            self.assertEqual(run["cwd"], str(outside.resolve()))

    def test_enqueue_rolls_back_durable_queue_when_worker_cannot_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)

            with (
                mock.patch.object(
                    module,
                    "agent_catalog_payload",
                    return_value=normalized_agent_catalog(),
                ),
                mock.patch.object(
                    module.threading,
                    "Thread",
                    side_effect=RuntimeError("worker unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "worker unavailable"):
                    runner.enqueue(automation, "schedule")

            self.assertEqual(module.STORE.list_runs(automation["id"]), [])
            self.assertNotIn(automation["id"], runner.queues)
            self.assertNotIn(automation["id"], runner.running_automations)

    def test_enqueue_rolls_back_when_save_fails_after_commit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)
            save_run = module.STORE.save_run

            def save_then_fail(item):
                save_run(item)
                raise RuntimeError("saved projection unavailable")

            with (
                mock.patch.object(
                    module,
                    "agent_catalog_payload",
                    return_value=normalized_agent_catalog(),
                ),
                mock.patch.object(module.STORE, "save_run", side_effect=save_then_fail),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "saved projection unavailable"
                ):
                    runner.enqueue(automation, "schedule")

            self.assertEqual(module.STORE.list_runs(automation["id"]), [])
            self.assertNotIn(automation["id"], runner.queues)
            self.assertNotIn(automation["id"], runner.running_automations)

    def test_enqueue_keeps_one_committed_run_when_event_delivery_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)
            thread = mock.Mock()

            with (
                mock.patch.object(
                    module,
                    "agent_catalog_payload",
                    return_value=normalized_agent_catalog(),
                ),
                mock.patch.object(module.threading, "Thread", return_value=thread),
                mock.patch.object(
                    module,
                    "publish_run_started",
                    side_effect=RuntimeError("subscriber unavailable"),
                ),
            ):
                queued = runner.enqueue(automation, "schedule")

            runs = module.STORE.list_runs(automation["id"])
            self.assertEqual([run["id"] for run in runs], [queued["id"]])
            self.assertEqual(runs[0]["runStatus"], "queued")
            self.assertEqual(runner.queues[automation["id"]][0][0], queued["id"])

    def test_worker_error_fails_current_run_and_continues_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)
            first = self.enqueue_without_worker(module, runner, automation)
            with (
                mock.patch.object(
                    module,
                    "agent_catalog_payload",
                    return_value=normalized_agent_catalog(),
                ),
                mock.patch.object(module.threading, "Thread") as thread,
            ):
                second = runner.enqueue(automation, "manual")
            thread.assert_not_called()
            processed = []

            def run_with_first_failure(id_, _automation):
                if id_ == first["id"]:
                    raise RuntimeError("worker setup failed")
                processed.append(id_)
                run = module.STORE.get_run(id_)
                run.update(
                    {
                        "runStatus": "failed",
                        "finishedAt": module.now_iso(),
                        "error": "second run handled",
                        "taskStatus": "fail",
                    }
                )
                module.STORE.save_run(run)

            with mock.patch.object(runner, "run", side_effect=run_with_first_failure):
                runner.loop_automation(automation["id"])

            failed = module.STORE.get_run(first["id"])
            handled = module.STORE.get_run(second["id"])
            self.assertEqual(failed["runStatus"], "failed")
            self.assertEqual(failed["error"], "worker setup failed")
            self.assertEqual(failed["taskStatus"], "fail")
            self.assertEqual(handled["runStatus"], "failed")
            self.assertEqual(processed, [second["id"]])
            self.assertNotIn(automation["id"], runner.queues)
            self.assertNotIn(automation["id"], runner.running_automations)

    def test_start_failure_keeps_exact_agent_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)
            run = self.enqueue_without_worker(module, runner, automation)
            queued_automation = runner.queues[automation["id"]][0][1]

            with mock.patch.object(
                module,
                "start_agent_session",
                side_effect=RuntimeError("agent start failed"),
            ):
                runner.run(run["id"], queued_automation)

            failed = module.STORE.get_run(run["id"])
            self.assertEqual(failed["runStatus"], "failed")
            self.assertEqual(failed["agentTargetId"], "local:reviewer")
            self.assertEqual(failed["agentProvider"], "review-provider")
            self.assertEqual(failed["error"], "agent start failed")

    def test_cli_run_json_and_runs_table_include_exact_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = self.save_automation(module)
            runner = module.Runner(module.STORE)
            thread = mock.Mock()
            responses = []
            handler = object.__new__(module.Handler)
            handler.json = lambda status, payload: responses.append((status, payload))

            with (
                mock.patch.object(module, "RUNNER", runner),
                mock.patch.object(
                    module,
                    "agent_catalog_payload",
                    return_value=normalized_agent_catalog(),
                ),
                mock.patch.object(module.threading, "Thread", return_value=thread),
            ):
                handler.read_json = lambda: {
                    "input": {"automation-id": automation["id"]}
                }
                handler.handle_cli("/tutti/cli/run")

                run_payload = responses.pop()[1]["value"]["run"]
                self.assertEqual(run_payload["agentTargetId"], "local:reviewer")
                self.assertEqual(run_payload["agentProvider"], "review-provider")

                handler.read_json = lambda: {
                    "input": {"automation-id": automation["id"], "limit": 10}
                }
                handler.handle_cli("/tutti/cli/runs")

            table = responses.pop()[1]
            self.assertIn({"key": "agent-id", "label": "Agent"}, table["columns"])
            self.assertEqual(table["rows"][0]["agent-id"], "local:reviewer")


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
                        "cwd": str(module.AGENT_WORK_DIR),
                        "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex", "model": "gpt-5"},
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

    def test_enqueue_failure_is_recorded_and_does_not_block_later_due_tasks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime(2026, 1, 1, 3, 0, 0, tzinfo=module.timezone.utc)
            first = fake_interval_automation(module, now, automation_id="aut_bad")
            second = fake_interval_automation(module, now, automation_id="aut_good")
            store = FakeSchedulerStore(None, due_automations=[first, second])
            runner = FakeRunner(fail_ids={"aut_bad"})
            scheduler = module.Scheduler(store, runner, autostart=False)

            scheduler.run_due_once(now=now)

            self.assertEqual(runner.enqueued, [("aut_good", "schedule")])
            self.assertEqual(runner.recorded_failures, [("aut_bad", "schedule", "target unavailable")])
            self.assertEqual([item["id"] for item in store.saved], ["aut_bad", "aut_good"])
            self.assertTrue(all(item["nextRunAt"] for item in store.saved))

    def test_schedule_advance_does_not_overwrite_a_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime(2026, 1, 1, 3, 0, 0, tzinfo=module.timezone.utc)
            automation = module.STORE.save_automation(
                module.normalize_automation(
                    {
                        "name": "Before",
                        "prompt": "Review",
                        "cwd": str(module.AGENT_WORK_DIR),
                        "enabled": True,
                        "scheduleType": "interval",
                        "schedule": {"intervalMinutes": 15},
                        "concurrency": "queue",
                        "runnerSettings": {"agentTargetId": "local:reviewer"},
                        "runnerArgs": [],
                        "env": {},
                    }
                )
            )
            stale = scheduler_snapshot(module, automation)
            edited = {**automation, "name": "After", "updatedAt": module.now_iso()}
            module.STORE.save_automation(edited)

            advanced = module.STORE.advance_automation_schedule(
                stale,
                module.now_iso(),
                module.compute_next_run(stale, now),
            )

            self.assertFalse(advanced)
            current = module.STORE.get_automation(automation["id"])
            self.assertEqual(current["name"], "After")
            self.assertEqual(current["nextRunAt"], edited["nextRunAt"])

    def test_schedule_advance_does_not_restore_a_deleted_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            automation = module.STORE.save_automation(
                module.normalize_automation(
                    {
                        "name": "Delete me",
                        "prompt": "Review",
                        "cwd": str(module.AGENT_WORK_DIR),
                        "enabled": True,
                        "scheduleType": "interval",
                        "schedule": {"intervalMinutes": 15},
                        "concurrency": "queue",
                        "runnerSettings": {"agentTargetId": "local:reviewer"},
                        "runnerArgs": [],
                        "env": {},
                    }
                )
            )
            stale = scheduler_snapshot(module, automation)
            module.STORE.delete_automation(automation["id"])

            advanced = module.STORE.advance_automation_schedule(
                stale,
                module.now_iso(),
                automation["nextRunAt"],
            )

            self.assertFalse(advanced)
            self.assertIsNone(module.STORE.get_automation(automation["id"]))

    def test_schedule_advance_uses_raw_legacy_runner_settings_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime(2026, 1, 1, 3, 0, 0, tzinfo=module.timezone.utc)
            automation = module.STORE.save_automation(
                module.normalize_automation(
                    {
                        "name": "Legacy provider task",
                        "prompt": "Review",
                        "cwd": str(module.AGENT_WORK_DIR),
                        "enabled": True,
                        "scheduleType": "interval",
                        "schedule": {"intervalMinutes": 15},
                        "concurrency": "queue",
                        "runnerSettings": {"agentTargetId": "local:reviewer"},
                        "runnerArgs": [],
                        "env": {},
                    }
                )
            )
            due_at = (now - module.timedelta(minutes=1)).isoformat()
            with module.STORE.lock:
                module.STORE.db.execute(
                    "UPDATE automations SET runner_settings_json=?, next_run_at=? WHERE id=?",
                    ('{"provider":"codex"}', due_at, automation["id"]),
                )
                module.STORE.db.commit()
            due = module.STORE.list_due_automations()[0]

            advanced = module.STORE.advance_automation_schedule(
                due,
                module.now_iso(),
                module.compute_next_run(due, now),
            )

            self.assertTrue(advanced)
            self.assertNotEqual(module.STORE.get_automation(automation["id"])["nextRunAt"], due_at)

    def test_failed_legacy_provider_occurrence_advances_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            now = module.datetime.now(module.timezone.utc)
            automation = module.STORE.save_automation(
                module.normalize_automation(
                    {
                        "name": "Unavailable legacy provider task",
                        "prompt": "Review",
                        "cwd": str(module.AGENT_WORK_DIR),
                        "enabled": True,
                        "scheduleType": "interval",
                        "schedule": {"intervalMinutes": 15},
                        "concurrency": "queue",
                        "runnerSettings": {"agentTargetId": "local:reviewer"},
                        "runnerArgs": [],
                        "env": {},
                    }
                )
            )
            due_at = (now - module.timedelta(minutes=1)).isoformat()
            with module.STORE.lock:
                module.STORE.db.execute(
                    "UPDATE automations SET runner_settings_json=?, next_run_at=? WHERE id=?",
                    ('{"provider":"codex"}', due_at, automation["id"]),
                )
                module.STORE.db.commit()
            runner = module.Runner(module.STORE)
            scheduler = module.Scheduler(module.STORE, runner, autostart=False)
            catalog = normalized_agent_catalog(
                agents=[("local:codex", "codex", "Legacy Agent", "unavailable")]
            )

            with mock.patch.object(module, "agent_catalog_payload", return_value=catalog):
                scheduler.run_due_once(now=now)
                scheduler.run_due_once(now=now)

            runs = module.STORE.list_runs(automation["id"])
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["runStatus"], "failed")
            self.assertIn("unavailable", runs[0]["error"].lower())
            current = module.STORE.get_automation(automation["id"])
            self.assertNotEqual(current["nextRunAt"], due_at)
            self.assertGreater(module.parse_iso(current["nextRunAt"]), now)


class FakeSchedulerStore:
    def __init__(self, next_run_at, due_automations=None):
        self.next_run_at = next_run_at
        self.due_automations = due_automations or []
        self.saved = []

    def list_due_automations(self):
        return [dict(automation) for automation in self.due_automations]

    def next_scheduled_run_at(self):
        return self.next_run_at

    def advance_automation_schedule(self, automation, updated_at, next_run_at):
        saved = {**automation, "updatedAt": updated_at, "nextRunAt": next_run_at}
        self.saved.append(saved)
        return automation


def scheduler_snapshot(module, automation):
    with module.STORE.lock:
        row = module.STORE.db.execute(
            "SELECT * FROM automations WHERE id=?",
            (automation["id"],),
        ).fetchone()
    return {**automation, "_schedulerSnapshot": module.scheduler_row_snapshot(row)}


class FakeRunner:
    def __init__(self, fail_ids=None):
        self.enqueued = []
        self.fail_ids = set(fail_ids or [])
        self.recorded_failures = []

    def enqueue(self, automation, trigger):
        if automation["id"] in self.fail_ids:
            raise ValueError("target unavailable")
        self.enqueued.append((automation["id"], trigger))
        return {"id": "run_1"}

    def record_enqueue_failure(self, automation, trigger, error):
        self.recorded_failures.append((automation["id"], trigger, str(error)))


def fake_interval_automation(module, next_run_at, automation_id="aut_1"):
    return {
        "id": automation_id,
        "enabled": True,
        "scheduleType": "interval",
        "schedule": {"intervalMinutes": 15},
        "nextRunAt": next_run_at.isoformat(),
    }


class AgentGetLogCompactionTest(unittest.TestCase):
    def test_compact_agent_get_log_stdout_keeps_current_session_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            stdout = json.dumps(
                {
                    "session": {
                        "agentSessionId": "agent-session-1",
                        "agentTargetId": "local:codex",
                        "activeTurnId": "turn-1",
                        "activeTurn": {"turnId": "turn-1", "phase": "running"},
                        "latestTurn": {"turnId": "turn-0", "phase": "settled", "outcome": "completed"},
                        "pendingInteractions": [],
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
            self.assertIn('"agentTargetId": "local:codex"', compacted)
            self.assertIn('"activeTurnId": "turn-1"', compacted)
            self.assertIn('"phase": "running"', compacted)
            self.assertIn('"outcome": "completed"', compacted)
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

    def test_run_tutti_cli_decodes_cli_output_as_utf8(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"providers": []}',
                stderr="",
            )

            with mock.patch.object(module.subprocess, "run", return_value=completed) as run_mock:
                module.run_tutti_cli(["agent", "list"])

            self.assertEqual(run_mock.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run_mock.call_args.kwargs["errors"], "replace")


class AgentWaitProtocolTest(unittest.TestCase):
    def test_wait_uses_current_cli_protocol_and_returns_final_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            payload = {
                "agentSessionId": "agent-session-1",
                "turnId": "turn-1",
                "session": {
                    "agentSessionId": "agent-session-1",
                    "agentTargetId": "local:codex",
                    "provider": "codex",
                    "activeTurnId": None,
                    "latestTurn": {"turnId": "turn-1", "phase": "settled", "outcome": "completed"},
                },
                "reason": "completed",
                "timedOut": False,
                "finalMessage": {"turnId": "turn-1", "text": "Done."},
            }

            with mock.patch.object(module, "run_tutti_cli", return_value=payload) as run_mock:
                result = module.wait_for_agent_stop(
                    "agent-session-1",
                    "local:codex",
                    "turn-1",
                )

            self.assertEqual(result, ("succeeded", None, "Done."))
            run_mock.assert_called_once_with(
                [
                    "agent",
                    "wait",
                    "--session-id",
                    "agent-session-1",
                    "--timeout-ms",
                    "2000",
                ],
                timeout=30,
                log_file=None,
            )

    def test_wait_timeout_keeps_run_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with mock.patch.object(
                module,
                "run_tutti_cli",
                return_value={
                    "reason": "wait_timeout",
                    "timedOut": True,
                    "executionContinues": True,
                },
            ):
                result = module.wait_for_agent_stop(
                    "agent-session-1",
                    "local:codex",
                    "turn-1",
                )
            self.assertEqual(result, (None, None, None))

    def test_wait_rejects_a_different_terminal_turn(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with (
                mock.patch.object(
                    module,
                    "run_tutti_cli",
                    return_value={
                        "turnId": "turn-other",
                        "reason": "completed",
                        "session": {
                            "agentSessionId": "agent-session-1",
                            "agentTargetId": "local:codex",
                        },
                    },
                ),
                self.assertRaisesRegex(RuntimeError, "returned turn turn-other, expected turn-1"),
            ):
                module.wait_for_agent_stop(
                    "agent-session-1",
                    "local:codex",
                    "turn-1",
                )

    def test_wait_maps_terminal_non_success_stop_reasons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            cases = {
                "failed": ("failed", None),
                "canceled": ("canceled", "Canceled by user."),
            }
            for reason, expected in cases.items():
                with self.subTest(reason=reason), mock.patch.object(
                    module,
                    "run_tutti_cli",
                    return_value={
                        "turnId": "turn-1",
                        "reason": reason,
                        "session": {
                            "agentSessionId": "agent-session-1",
                            "agentTargetId": "local:codex",
                        },
                    },
                ):
                    status, error, summary = module.wait_for_agent_stop(
                        "agent-session-1",
                        "local:codex",
                        "turn-1",
                    )
                    self.assertEqual((status, error), expected)
                    self.assertIsNone(summary)

    def test_wait_keeps_interactive_reasons_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            for reason in ("waiting_approval", "waiting_input", "waiting"):
                with (
                    self.subTest(reason=reason),
                    mock.patch.object(module, "AGENT_WAIT_POLL_TIMEOUT_MS", 0),
                    mock.patch.object(
                        module,
                        "run_tutti_cli",
                        return_value={
                            "turnId": "turn-1",
                            "reason": reason,
                            "session": {
                                "agentSessionId": "agent-session-1",
                                "agentTargetId": "local:codex",
                            },
                        },
                    ),
                ):
                    result = module.wait_for_agent_stop(
                        "agent-session-1",
                        "local:codex",
                        "turn-1",
                    )

                    self.assertEqual(result, (None, None, None))

    def test_cancel_uses_exact_turn_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            with mock.patch.object(module, "run_tutti_cli") as run_mock:
                module.cancel_agent_turn("agent-session-1", "turn-1")
            run_mock.assert_called_once_with(
                [
                    "agent",
                    "cancel-turn",
                    "--session-id",
                    "agent-session-1",
                    "--turn-id",
                    "turn-1",
                ],
                timeout=30,
                log_file=None,
            )


class RunCompletionTest(unittest.TestCase):
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

    def test_runner_uses_wait_terminal_result_and_final_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])

            def fake_wait(agent_session_id, expected_agent_target_id, expected_turn_id, log_file=None):
                self.assertEqual(agent_session_id, "agent-session-1")
                self.assertEqual(expected_agent_target_id, "local:codex")
                self.assertEqual(expected_turn_id, "turn-1")
                module.complete_run_from_cli({"run-id": run["id"], "status": "success"})
                return "succeeded", None, "Done."

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={
                        "agentSessionId": "agent-session-1",
                        "turnId": "turn-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "wait_for_agent_stop", side_effect=fake_wait),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(stored["runStatus"], "succeeded")
            self.assertEqual(stored["taskStatus"], "success")
            self.assertEqual(stored["summary"], "Done.")

    def test_runner_retries_after_wait_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])
            waits = []

            def fake_wait(*_args, **_kwargs):
                waits.append(True)
                if len(waits) == 1:
                    return None, None, None
                module.complete_run_from_cli({"run-id": run["id"], "status": "success"})
                return "succeeded", None, "Done."

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={
                        "agentSessionId": "agent-session-1",
                        "turnId": "turn-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "wait_for_agent_stop", side_effect=fake_wait),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            self.assertEqual(len(waits), 2)
            self.assertEqual(stored["runStatus"], "succeeded")
            self.assertEqual(stored["taskStatus"], "success")
            self.assertEqual(stored["summary"], "Done.")

    def test_runner_times_out_and_cancels_exact_turn(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_server_module(Path(temp_dir))
            run = make_run(module, "queued", trigger="schedule")
            automation = module.STORE.get_automation(run["automationId"])

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={
                        "agentSessionId": "agent-session-1",
                        "turnId": "turn-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(
                    module,
                    "wait_for_agent_stop",
                    return_value=(None, None, None),
                ),
                mock.patch.object(module, "cancel_agent_turn") as cancel_mock,
                mock.patch.object(module, "RUN_TIMEOUT_SECONDS", 1),
                mock.patch.object(module, "run_has_timed_out", side_effect=[False, True]),
            ):
                module.Runner(module.STORE).run(run["id"], automation)

            stored = module.STORE.get_run(run["id"])
            cancel_mock.assert_called_once()
            self.assertEqual(cancel_mock.call_args.args[:2], ("agent-session-1", "turn-1"))
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
                    return_value={
                        "agentSessionId": "agent-session-1",
                        "turnId": "turn-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "COMPLETION_GRACE_SECONDS", 0),
                mock.patch.object(
                    module,
                    "wait_for_agent_stop",
                    return_value=("succeeded", None, "Finished normally."),
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

            def fake_wait(*_args, **_kwargs):
                ready_seen.set()
                return "succeeded", None, "Nothing to do."

            with (
                mock.patch.object(
                    module,
                    "start_agent_session",
                    return_value={
                        "agentSessionId": "agent-session-1",
                        "turnId": "turn-1",
                        "agentTargetId": "local:codex",
                        "provider": "codex",
                    },
                ),
                mock.patch.object(module, "open_agent_session"),
                mock.patch.object(module, "wait_for_agent_stop", fake_wait),
                mock.patch.object(module, "COMPLETION_GRACE_SECONDS", 2),
                mock.patch.object(module, "COMPLETION_GRACE_POLL_SECONDS", 0.05),
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
                "cwd": str(module.AGENT_WORK_DIR),
                "enabled": False,
                "scheduleType": "daily",
                "schedule": {"timeOfDay": "09:00"},
                "concurrency": "queue",
                "runnerSettings": {"agentTargetId": "local:codex", "providerId": "codex", "model": "gpt-5"},
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
            "cwd": str(module.AGENT_WORK_DIR),
            "queuedAt": module.now_iso(),
            "startedAt": module.now_iso() if run_status != "queued" else None,
            "artifactDir": str(Path(module.LOG_DIR) / "runs" / automation["id"] / "run_123"),
            "agentTargetId": "local:codex",
            "agentProvider": "codex",
        }
    )


if __name__ == "__main__":
    unittest.main()
