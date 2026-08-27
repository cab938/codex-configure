#!/usr/bin/env python3
"""Focused stdlib checks for the spike canary's framing and state checks."""

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

    def test_catalog_requires_qualified_openai_and_umich(self) -> None:
        result = {
            "data": [
                {"id": "openai::gpt-5.6-sol", "model": "openai::gpt-5.6-sol"},
                {"id": canary.UMICH_MODEL, "model": canary.UMICH_MODEL},
            ]
        }
        self.assertEqual(
            canary.catalog_models(result),
            ("openai::gpt-5.6-sol", canary.UMICH_MODEL),
        )

    def test_catalog_rejects_unqualified_entries(self) -> None:
        with self.assertRaises(canary.CanaryError):
            canary.catalog_models({"data": [{"id": "gpt-5.6-sol", "model": "gpt-5.6-sol"}]})

    def test_completed_marker_checks_task_continuity_and_exact_text(self) -> None:
        notification = {
            "method": "turn/completed",
            "params": {
                "threadId": "task-1",
                "turn": {
                    "id": "turn-2",
                    "items": [{"type": "agentMessage", "text": canary.UMICH_MARKER}],
                },
            },
        }
        self.assertEqual(
            canary.completed_marker(
                notification,
                thread_id="task-1",
                turn_id="turn-2",
                expected=canary.UMICH_MARKER,
            ),
            canary.UMICH_MARKER,
        )
        with self.assertRaises(canary.CanaryError):
            canary.completed_marker(
                notification,
                thread_id="other-task",
                turn_id="turn-2",
                expected=canary.UMICH_MARKER,
            )

    def test_resume_requires_same_task_and_qualified_umich_selection(self) -> None:
        result = {
            "thread": {"id": "task-1"},
            "model": canary.UMICH_MODEL,
            "modelProvider": "umich-toolkit",
        }
        canary.validate_resumed_selection(result, thread_id="task-1")
        result["model"] = "gpt-5.6-terra"
        with self.assertRaises(canary.CanaryError):
            canary.validate_resumed_selection(result, thread_id="task-1")


if __name__ == "__main__":
    unittest.main()
