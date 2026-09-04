from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "known_model_catalog.py"
SPEC = importlib.util.spec_from_file_location("known_model_catalog_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)


def catalog_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-03T12:00:00Z",
        "models_dev_source": {
            "url": SCRIPT.SCHEMA.MODELS_DEV_MODELS_URL,
            "retrieved_at": "2026-09-03T12:00:00Z",
            "sha256": "a" * 64,
        },
        "models": [
            {
                "endpoint_id": "served-id",
                "models_dev_id": "source/id",
                "display_name": "Old name",
                "description": "Old description",
                "reported": {"input_modalities": ["text"], "context_window": 1024},
                "tested": {
                    "tested_at": "2026-09-03T12:00:00Z",
                    "probe_version": "1",
                    "runtime": {"name": "llama.cpp", "version": "old"},
                    "checks": {
                        "model_list": "pass",
                        "responses_streaming": "pass",
                        "standard_tools": "pass",
                        "vision": "not_run",
                        "reasoning_efforts": {},
                        "reasoning_summary": "not_run",
                    },
                },
            }
        ],
    }


class MaintainerScriptTests(unittest.TestCase):
    def test_import_refreshes_source_fields_and_preserves_test_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text(json.dumps(catalog_payload()), encoding="utf-8")
            upstream = {
                "source/id": {
                    "id": "source/id",
                    "name": "New name",
                    "description": "New description",
                    "last_updated": "2026-09-03",
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                    "limit": {"context": 32768},
                }
            }
            args = argparse.Namespace(
                catalog=path,
                model=["served-id=source/id"],
                timeout=10.0,
            )
            with mock.patch.object(
                SCRIPT,
                "_fetch",
                return_value=(json.dumps(upstream).encode("utf-8"), {}),
            ):
                SCRIPT.command_import(args)

            refreshed = json.loads(path.read_text(encoding="utf-8"))
            model = refreshed["models"][0]
            self.assertEqual(model["display_name"], "New name")
            self.assertEqual(model["reported"]["context_window"], 32768)
            self.assertEqual(model["reported"]["input_modalities"], ["text", "image"])
            self.assertEqual(model["tested"]["runtime"]["version"], "old")
            SCRIPT.SCHEMA.validate_known_catalog(refreshed)

    def test_certify_merges_only_an_exact_sanitized_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog_path = Path(temporary) / "catalog.json"
            report_path = Path(temporary) / "probe.json"
            catalog = catalog_payload()
            del catalog["models"][0]["tested"]  # type: ignore[index]
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            report = {
                "schema_version": 1,
                "endpoint_id": "served-id",
                "tested": catalog_payload()["models"][0]["tested"],  # type: ignore[index]
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")

            result = SCRIPT.command_certify(
                argparse.Namespace(catalog=catalog_path, report=report_path)
            )

            self.assertEqual(result, 0)
            certified = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(certified["models"][0]["tested"], report["tested"])
            SCRIPT.SCHEMA.validate_known_catalog(certified)

            report["endpoint_id"] = "different-case"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no exact endpoint_id"):
                SCRIPT.command_certify(
                    argparse.Namespace(catalog=catalog_path, report=report_path)
                )

    def test_probe_emits_only_sanitized_evidence(self) -> None:
        args = argparse.Namespace(
            base_url="http://127.0.0.1:1337/v1",
            model="served-id",
            runtime_name="llama.cpp",
            runtime_version="test-version",
            api_key_env="LOCAL_TEST_KEY",
            timeout=5.0,
            vision=True,
            reasoning_effort=["low", "medium"],
            default_reasoning_effort="medium",
            reasoning_summary=True,
        )
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"LOCAL_TEST_KEY": "top-secret"}, clear=False), mock.patch.object(
            SCRIPT, "_get_model_ids", return_value={"served-id"}
        ), mock.patch.object(SCRIPT, "_probe_text", return_value=True), mock.patch.object(
            SCRIPT, "_probe_tools", return_value=True
        ), mock.patch.object(SCRIPT, "_probe_vision", return_value=True), mock.patch.object(
            SCRIPT, "_probe_reasoning", return_value=True
        ), contextlib.redirect_stdout(output):
            result = SCRIPT.command_probe(args)

        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["endpoint_id"], "served-id")
        self.assertEqual(report["tested"]["checks"]["standard_tools"], "pass")
        self.assertEqual(
            report["tested"]["checks"]["reasoning_efforts"],
            {"low": "pass", "medium": "pass"},
        )
        self.assertNotIn("top-secret", output.getvalue())
        self.assertNotIn("127.0.0.1", output.getvalue())

    def test_probe_report_remains_valid_when_requested_default_fails(self) -> None:
        args = argparse.Namespace(
            base_url="http://127.0.0.1:1337/v1",
            model="served-id",
            runtime_name="llama.cpp",
            runtime_version="test-version",
            api_key_env=None,
            timeout=5.0,
            vision=False,
            reasoning_effort=["low", "medium"],
            default_reasoning_effort="medium",
            reasoning_summary=False,
        )
        output = io.StringIO()

        def reasoning_result(*args: object, effort: str, **kwargs: object) -> bool:
            return effort == "low"

        with mock.patch.object(
            SCRIPT, "_get_model_ids", return_value={"served-id"}
        ), mock.patch.object(SCRIPT, "_probe_text", return_value=True), mock.patch.object(
            SCRIPT, "_probe_tools", return_value=True
        ), mock.patch.object(
            SCRIPT, "_probe_reasoning", side_effect=reasoning_result
        ), contextlib.redirect_stdout(output):
            result = SCRIPT.command_probe(args)

        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertNotIn("default_reasoning_effort", report["tested"])
        catalog = catalog_payload()
        catalog["models"][0]["tested"] = report["tested"]  # type: ignore[index]
        SCRIPT.SCHEMA.validate_known_catalog(catalog)


if __name__ == "__main__":
    unittest.main()
