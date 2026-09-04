from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from codex_configure.catalog import CatalogService
from codex_configure.known_catalog import (
    KNOWN_CATALOG_MAX_BYTES,
    KNOWN_LOCAL_CATALOG_URL,
    MODELS_DEV_MODELS_URL,
    KnownCatalogError,
    catalog_model_status,
    validate_known_catalog,
)


def known_catalog(*, endpoint_id: str = "known-model", tested: bool = True) -> dict[str, object]:
    model: dict[str, object] = {
        "endpoint_id": endpoint_id,
        "models_dev_id": "example/known-model",
        "display_name": "Known Model",
        "description": "Known model from the owned catalog",
        "reported": {
            "input_modalities": ["text", "image"],
            "context_window": 32768,
            "models_dev_updated_at": "2026-09-01",
        },
    }
    if tested:
        model["tested"] = {
            "tested_at": "2026-09-03T12:00:00Z",
            "probe_version": "1",
            "runtime": {"name": "llama.cpp", "version": "test"},
            "checks": {
                "model_list": "pass",
                "responses_streaming": "pass",
                "standard_tools": "pass",
                "vision": "pass",
                "reasoning_efforts": {"low": "pass", "medium": "pass", "high": "fail"},
                "reasoning_summary": "pass",
            },
            "default_reasoning_effort": "medium",
        }
    return {
        "schema_version": 1,
        "generated_at": "2026-09-03T12:00:00Z",
        "models_dev_source": {
            "url": MODELS_DEV_MODELS_URL,
            "retrieved_at": "2026-09-03T11:00:00Z",
            "sha256": "a" * 64,
        },
        "models": [model],
    }


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, etag: str | None = None) -> None:
        super().__init__(payload)
        self.headers = {} if etag is None else {"ETag": etag}


class KnownCatalogSchemaTests(unittest.TestCase):
    def test_accepts_tested_model_and_rejects_unknown_fields(self) -> None:
        payload = known_catalog()
        self.assertEqual(validate_known_catalog(payload)["schema_version"], 1)
        payload["surprise"] = True
        with self.assertRaisesRegex(KnownCatalogError, "unsupported field surprise"):
            validate_known_catalog(payload)

    def test_rejects_duplicate_exact_endpoint_ids(self) -> None:
        payload = known_catalog()
        payload["models"].append(dict(payload["models"][0]))  # type: ignore[union-attr,index]
        with self.assertRaisesRegex(KnownCatalogError, "duplicate endpoint_id known-model"):
            validate_known_catalog(payload)

    def test_rejects_invalid_contexts_and_modalities(self) -> None:
        invalid_values = (
            ("context_window", 0),
            ("input_modalities", ["text", "audio"]),
            ("input_modalities", ["text", "text"]),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                payload = known_catalog()
                payload["models"][0]["reported"][field] = value  # type: ignore[index]
                with self.assertRaises(KnownCatalogError):
                    validate_known_catalog(payload)

    def test_rejects_an_unsupported_reasoning_effort(self) -> None:
        payload = known_catalog()
        efforts = payload["models"][0]["tested"]["checks"]["reasoning_efforts"]  # type: ignore[index]
        efforts["gigantic"] = "pass"  # type: ignore[index]
        with self.assertRaisesRegex(KnownCatalogError, "unsupported effort gigantic"):
            validate_known_catalog(payload)

    def test_requires_streaming_evidence_for_promoted_capabilities(self) -> None:
        payload = known_catalog()
        tested = payload["models"][0]["tested"]  # type: ignore[index]
        tested["checks"]["responses_streaming"] = "fail"  # type: ignore[index]
        with self.assertRaisesRegex(KnownCatalogError, "model-list and streaming"):
            validate_known_catalog(payload)

    def test_rejects_invalid_or_timezone_free_timestamps(self) -> None:
        for timestamp in ("not-a-time", "2026-09-03T12:00:00"):
            with self.subTest(timestamp=timestamp):
                payload = known_catalog()
                payload["generated_at"] = timestamp
                with self.assertRaisesRegex(KnownCatalogError, "timestamp|timezone"):
                    validate_known_catalog(payload)

    def test_requires_a_passing_default_reasoning_effort(self) -> None:
        payload = known_catalog()
        tested = payload["models"][0]["tested"]  # type: ignore[index]
        tested["default_reasoning_effort"] = "high"  # type: ignore[index]
        with self.assertRaisesRegex(KnownCatalogError, "must name a passing effort"):
            validate_known_catalog(payload)

    def test_allows_pass_evidence_without_promoting_reasoning(self) -> None:
        payload = known_catalog()
        tested = payload["models"][0]["tested"]  # type: ignore[index]
        tested.pop("default_reasoning_effort")  # type: ignore[union-attr]
        validate_known_catalog(payload)


class KnownCatalogDiscoveryTests(unittest.TestCase):
    def make_service(self) -> tuple[tempfile.TemporaryDirectory[str], CatalogService]:
        temporary = tempfile.TemporaryDirectory()
        return temporary, CatalogService(Path(temporary.name))

    def test_exact_join_caps_context_and_promotes_only_tested_features(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        remote = json.dumps(known_catalog()).encode("utf-8")
        endpoint = json.dumps(
            {
                "data": [
                    {"id": "known-model", "meta": {"n_ctx": 16384}},
                    {"id": "KNOWN-MODEL", "meta": {"n_ctx": 8192}},
                    {"id": "known-model-embedding"},
                ]
            }
        ).encode("utf-8")
        requests: list[urllib.request.Request] = []

        def open_request(request: urllib.request.Request, timeout: float) -> FakeResponse:
            requests.append(request)
            if request.full_url == KNOWN_LOCAL_CATALOG_URL:
                return FakeResponse(remote, etag='"catalog-etag"')
            return FakeResponse(endpoint)

        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen", side_effect=open_request
        ):
            result = service.discover_local(
                "http://127.0.0.1:1337/v1", api_key="endpoint-secret"
            )

        self.assertEqual([model.status for model in result.models], ["unverified", "tested", "non-generation"])
        tested = next(model for model in result.models if model.slug == "known-model")
        self.assertEqual(tested.display_name, "Known Model")
        self.assertEqual(tested.catalog_entry["context_window"], 16384)
        self.assertEqual(tested.catalog_entry["input_modalities"], ["text", "image"])
        self.assertEqual(
            [preset["effort"] for preset in tested.catalog_entry["supported_reasoning_levels"]],
            ["low", "medium"],
        )
        self.assertEqual(tested.catalog_entry["default_reasoning_level"], "medium")
        self.assertTrue(tested.catalog_entry["supports_reasoning_summary_parameter"])
        self.assertIn("tools tested", tested.badges)
        self.assertEqual(result.known_catalog.state, "fresh")  # type: ignore[union-attr]
        self.assertIsNone(requests[0].get_header("Authorization"))
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer endpoint-secret")

    def test_invalid_refresh_uses_last_valid_cache(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        remote = json.dumps(known_catalog()).encode("utf-8")
        endpoint = json.dumps({"data": [{"id": "known-model"}]}).encode("utf-8")

        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[FakeResponse(remote, etag='"first"'), FakeResponse(endpoint)],
        ):
            first = service.discover_local("http://127.0.0.1:1337/v1")
        self.assertEqual(first.known_catalog.state, "fresh")  # type: ignore[union-attr]

        requests: list[urllib.request.Request] = []

        def fail_refresh(request: urllib.request.Request, timeout: float) -> FakeResponse:
            requests.append(request)
            if request.full_url == KNOWN_LOCAL_CATALOG_URL:
                raise urllib.error.URLError("offline")
            return FakeResponse(endpoint)

        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen", side_effect=fail_refresh
        ):
            second = service.discover_local("http://127.0.0.1:1337/v1")

        self.assertEqual(second.known_catalog.state, "cached")  # type: ignore[union-attr]
        self.assertEqual(second.models[0].status, "tested")
        self.assertIn("last valid cache", second.warning or "")
        self.assertEqual(requests[0].get_header("If-none-match"), '"first"')
        cache = Path(temporary.name) / "codex-configure" / "cache" / "known-local-models-v1.json"
        self.assertEqual(hashlib.sha256(cache.read_bytes()).hexdigest(), second.known_catalog.sha256)  # type: ignore[union-attr]
        metadata = cache.with_name("known-local-models-v1.meta.json")
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(
            set(metadata_value),
            {"schema_version", "url", "etag", "sha256", "fetched_at"},
        )
        self.assertEqual(metadata_value["etag"], '"first"')
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(cache.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)

    def test_not_modified_revalidates_the_cached_catalog(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        remote = json.dumps(known_catalog()).encode("utf-8")
        endpoint = json.dumps({"data": [{"id": "known-model"}]}).encode("utf-8")
        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[FakeResponse(remote, etag='"same"'), FakeResponse(endpoint)],
        ):
            service.discover_local("http://127.0.0.1:1337/v1")

        def not_modified(request: urllib.request.Request, timeout: float) -> FakeResponse:
            if request.full_url == KNOWN_LOCAL_CATALOG_URL:
                raise urllib.error.HTTPError(request.full_url, 304, "not modified", {}, None)
            return FakeResponse(endpoint)

        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen", side_effect=not_modified
        ):
            result = service.discover_local("http://127.0.0.1:1337/v1")

        self.assertEqual(result.known_catalog.state, "fresh")  # type: ignore[union-attr]
        self.assertIsNone(result.warning)
        self.assertEqual(result.models[0].status, "tested")

    def test_invalid_or_oversized_refresh_never_replaces_the_valid_cache(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        valid = json.dumps(known_catalog()).encode("utf-8")
        endpoint = json.dumps({"data": [{"id": "known-model"}]}).encode("utf-8")
        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[FakeResponse(valid), FakeResponse(endpoint)],
        ):
            initial = service.discover_local("http://127.0.0.1:1337/v1")
        original_hash = initial.known_catalog.sha256  # type: ignore[union-attr]

        newer_schema = known_catalog()
        newer_schema["schema_version"] = 2
        bad_payloads = (
            b"{not-json",
            json.dumps(newer_schema).encode("utf-8"),
            b"x" * (KNOWN_CATALOG_MAX_BYTES + 1),
        )
        for remote in bad_payloads:
            with self.subTest(size=len(remote)):
                with mock.patch(
                    "codex_configure.catalog.urllib.request.urlopen",
                    side_effect=[FakeResponse(remote), FakeResponse(endpoint)],
                ):
                    result = service.discover_local("http://127.0.0.1:1337/v1")
                self.assertEqual(result.known_catalog.state, "cached")  # type: ignore[union-attr]
                self.assertEqual(result.known_catalog.sha256, original_hash)  # type: ignore[union-attr]
                self.assertEqual(result.models[0].status, "tested")

    def test_timeout_uses_last_valid_cache(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        valid = json.dumps(known_catalog()).encode("utf-8")
        endpoint = json.dumps({"data": [{"id": "known-model"}]}).encode("utf-8")
        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[FakeResponse(valid), FakeResponse(endpoint)],
        ):
            service.discover_local("http://127.0.0.1:1337/v1")

        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[socket.timeout("timed out"), FakeResponse(endpoint)],
        ):
            result = service.discover_local("http://127.0.0.1:1337/v1")

        self.assertEqual(result.known_catalog.state, "cached")  # type: ignore[union-attr]
        self.assertEqual(result.models[0].status, "tested")

    def test_source_reported_image_does_not_promote_an_untested_model(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        remote = json.dumps(known_catalog(tested=False)).encode("utf-8")
        endpoint = json.dumps({"data": [{"id": "known-model"}]}).encode("utf-8")
        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[FakeResponse(remote), FakeResponse(endpoint)],
        ):
            result = service.discover_local("http://127.0.0.1:1337/v1")

        self.assertEqual(result.models[0].status, "known")
        self.assertEqual(result.models[0].catalog_entry["input_modalities"], ["text"])
        self.assertNotIn("supported_reasoning_levels", result.models[0].catalog_entry)

    def test_reasoning_passes_without_a_default_are_not_promoted(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        payload = known_catalog()
        tested = payload["models"][0]["tested"]  # type: ignore[index]
        tested.pop("default_reasoning_effort")  # type: ignore[union-attr]
        remote = json.dumps(payload).encode("utf-8")
        endpoint = json.dumps({"data": [{"id": "known-model"}]}).encode("utf-8")
        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[FakeResponse(remote), FakeResponse(endpoint)],
        ):
            result = service.discover_local("http://127.0.0.1:1337/v1")

        self.assertEqual(result.models[0].status, "tested")
        self.assertNotIn("supported_reasoning_levels", result.models[0].catalog_entry)
        self.assertNotIn("default_reasoning_level", result.models[0].catalog_entry)

    def test_no_cache_keeps_endpoint_models_unverified(self) -> None:
        temporary, service = self.make_service()
        self.addCleanup(temporary.cleanup)
        endpoint = json.dumps({"data": [{"id": "local-only", "meta": {"n_ctx": 4096}}]}).encode(
            "utf-8"
        )

        def fail_remote(request: urllib.request.Request, timeout: float) -> FakeResponse:
            if request.full_url == KNOWN_LOCAL_CATALOG_URL:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            return FakeResponse(endpoint)

        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen", side_effect=fail_remote
        ):
            result = service.discover_local("http://127.0.0.1:1337/v1")

        self.assertEqual(result.models[0].status, "unverified")
        self.assertEqual(result.models[0].catalog_entry["input_modalities"], ["text"])
        self.assertEqual(result.known_catalog.state, "unavailable")  # type: ignore[union-attr]
        self.assertIn("remain unverified", result.warning or "")

    def test_checked_in_catalog_is_valid_and_deterministically_sorted(self) -> None:
        path = Path(__file__).resolve().parents[1] / "catalog" / "v1" / "local-models.json"
        catalog = validate_known_catalog(json.loads(path.read_text(encoding="utf-8")))
        endpoint_ids = [model["endpoint_id"] for model in catalog["models"]]
        self.assertEqual(endpoint_ids, sorted(endpoint_ids, key=lambda value: (value.casefold(), value)))
        tested = next(
            model for model in catalog["models"]
            if model["endpoint_id"] == "qwen3.5-35b-a3b-q6"
        )
        self.assertEqual(catalog_model_status(tested), "tested")
        self.assertEqual(tested["tested"]["checks"]["vision"], "not_run")


if __name__ == "__main__":
    unittest.main()
