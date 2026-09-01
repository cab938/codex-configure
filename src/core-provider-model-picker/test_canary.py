#!/usr/bin/env python3
"""Focused stdlib checks for the provider-generic canary's logic."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("app_server_canary.py")
SPEC = importlib.util.spec_from_file_location("app_server_canary", MODULE_PATH)
assert SPEC and SPEC.loader
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


class CanaryLogicTests(unittest.TestCase):
    def test_jsonl_round_trip(self) -> None:
        message = {"id": 4, "method": "model/list", "params": {"limit": 100}}
        self.assertEqual(canary.parse_json_line(canary.json_line(message)), message)
        self.assertTrue(canary.json_line(message).endswith(b"\n"))

    def test_catalog_prefers_terra_and_requires_qualified_entries(self) -> None:
        provider_id = "research-2026"
        result = {
            "data": [
                {"id": "openai → gpt-5.6-sol", "model": "openai → gpt-5.6-sol"},
                {
                    "id": f"{provider_id} → other-model",
                    "model": f"{provider_id} → other-model",
                },
                {
                    "id": f"{provider_id} → gpt-5.6-terra",
                    "model": f"{provider_id} → gpt-5.6-terra",
                },
            ]
        }
        self.assertEqual(
            canary.catalog_models(result, provider_id),
            ("openai → gpt-5.6-sol", f"{provider_id} → gpt-5.6-terra"),
        )

    def test_catalog_falls_back_to_first_selectable_external_model(self) -> None:
        provider_id = "teaching"
        result = {
            "data": [
                {"id": "openai → gpt-5.6-sol", "hidden": False},
                {"id": f"{provider_id} → hidden", "hidden": True},
                {"id": f"{provider_id} → first-selectable", "hidden": False},
                {"id": f"{provider_id} → gpt-5.6-terra", "hidden": True},
            ]
        }
        self.assertEqual(
            canary.catalog_models(result, provider_id),
            ("openai → gpt-5.6-sol", f"{provider_id} → first-selectable"),
        )

    def test_catalog_rejects_unqualified_or_wrong_namespace_entries(self) -> None:
        with self.assertRaises(canary.CanaryError):
            canary.catalog_models(
                {"data": [{"id": "gpt-5.6-sol"}, {"id": "other → gpt-5.6-terra"}]},
                "research",
            )

    def test_provider_id_and_credential_name_are_deterministic(self) -> None:
        self.assertEqual(canary.credential_env_name("research-2026"), "RESEARCH_2026_API_KEY")
        with self.assertRaises(canary.CanaryError):
            canary.validate_provider_id("openai")
        with self.assertRaises(canary.CanaryError):
            canary.validate_provider_id("research → model")

    def test_completed_marker_checks_task_continuity_and_exact_text(self) -> None:
        notification = {
            "method": "turn/completed",
            "params": {
                "threadId": "task-1",
                "turn": {
                    "id": "turn-2",
                    "items": [{"type": "agentMessage", "text": canary.EXTERNAL_MARKER}],
                },
            },
        }
        self.assertEqual(
            canary.completed_marker(
                notification,
                thread_id="task-1",
                turn_id="turn-2",
                expected=canary.EXTERNAL_MARKER,
            ),
            canary.EXTERNAL_MARKER,
        )
        with self.assertRaises(canary.CanaryError):
            canary.completed_marker(
                notification,
                thread_id="other-task",
                turn_id="turn-2",
                expected=canary.EXTERNAL_MARKER,
            )

    def test_resume_requires_same_task_and_qualified_provider_selection(self) -> None:
        provider_id = "research"
        external_model = f"{provider_id} → gpt-5.6-terra"
        result = {
            "thread": {"id": "task-1"},
            "model": external_model,
            "modelProvider": provider_id,
        }
        canary.validate_resumed_selection(
            result,
            thread_id="task-1",
            provider_id=provider_id,
            external_model=external_model,
        )
        result["model"] = "gpt-5.6-terra"
        with self.assertRaises(canary.CanaryError):
            canary.validate_resumed_selection(
                result,
                thread_id="task-1",
                provider_id=provider_id,
                external_model=external_model,
            )


if __name__ == "__main__":
    unittest.main()
