"""Strict schema for the remotely maintained local-model catalog.

This module deliberately uses only the Python standard library.  Both the
installed setup flow and the repository's maintainer script load the same
validator so the published data cannot drift from the runtime contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


KNOWN_LOCAL_CATALOG_URL = (
    "https://raw.githubusercontent.com/cab938/codex-configure/"
    "main/catalog/v1/local-models.json"
)
MODELS_DEV_MODELS_URL = "https://models.dev/models.json"
KNOWN_CATALOG_SCHEMA_VERSION = 1
KNOWN_CATALOG_MAX_BYTES = 2 * 1024 * 1024
CHECK_STATES = frozenset({"pass", "fail", "not_run"})
REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "persistent",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class KnownCatalogProvenance:
    """How the setup flow obtained the advisory catalog."""

    state: str
    url: str = KNOWN_LOCAL_CATALOG_URL
    sha256: str | None = None
    etag: str | None = None
    fetched_at: str | None = None

    def profile_values(self) -> dict[str, str]:
        values = {"state": self.state, "url": self.url}
        for name in ("sha256", "etag", "fetched_at"):
            value = getattr(self, name)
            if value:
                values[name] = value
        return values


class KnownCatalogError(ValueError):
    """Raised when an owned catalog violates its frozen public schema."""


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KnownCatalogError(f"{path} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    path: str,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = required - set(value)
    if missing:
        raise KnownCatalogError(f"{path} is missing {', '.join(sorted(missing))}")
    unknown = set(value) - required - optional
    if unknown:
        raise KnownCatalogError(f"{path} contains unsupported field {sorted(unknown)[0]}")


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnownCatalogError(f"{path} must be a non-empty string")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KnownCatalogError(f"{path} must be a positive integer")
    return value


def _timestamp(value: Any, path: str) -> str:
    timestamp = _nonempty_string(value, path)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnownCatalogError(f"{path} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise KnownCatalogError(f"{path} must include a timezone")
    return timestamp


def _check_state(value: Any, path: str) -> str:
    if value not in CHECK_STATES:
        allowed = ", ".join(sorted(CHECK_STATES))
        raise KnownCatalogError(f"{path} must be one of {allowed}")
    return str(value)


def validate_known_catalog(payload: Any) -> dict[str, Any]:
    """Validate and return a plain copy of one owned catalog document."""

    root = _mapping(payload, "catalog")
    _exact_keys(
        root,
        "catalog",
        required={"schema_version", "generated_at", "models_dev_source", "models"},
    )
    if root["schema_version"] != KNOWN_CATALOG_SCHEMA_VERSION:
        raise KnownCatalogError(
            f"catalog schema_version must be {KNOWN_CATALOG_SCHEMA_VERSION}"
        )
    _timestamp(root["generated_at"], "catalog.generated_at")

    source = _mapping(root["models_dev_source"], "catalog.models_dev_source")
    _exact_keys(
        source,
        "catalog.models_dev_source",
        required={"url", "retrieved_at", "sha256"},
    )
    if _nonempty_string(source["url"], "catalog.models_dev_source.url") != MODELS_DEV_MODELS_URL:
        raise KnownCatalogError(
            f"catalog.models_dev_source.url must be {MODELS_DEV_MODELS_URL}"
        )
    _timestamp(source["retrieved_at"], "catalog.models_dev_source.retrieved_at")
    source_hash = _nonempty_string(source["sha256"], "catalog.models_dev_source.sha256")
    if not _SHA256_RE.fullmatch(source_hash):
        raise KnownCatalogError("catalog.models_dev_source.sha256 must be lowercase SHA-256")

    models = root["models"]
    if not isinstance(models, list):
        raise KnownCatalogError("catalog.models must be an array")
    seen: set[str] = set()
    for index, raw_model in enumerate(models):
        path = f"catalog.models[{index}]"
        model = _mapping(raw_model, path)
        _exact_keys(
            model,
            path,
            required={
                "endpoint_id",
                "models_dev_id",
                "display_name",
                "description",
                "reported",
            },
            optional={"tested"},
        )
        endpoint_id = _nonempty_string(model["endpoint_id"], f"{path}.endpoint_id")
        if endpoint_id in seen:
            raise KnownCatalogError(f"catalog contains duplicate endpoint_id {endpoint_id}")
        seen.add(endpoint_id)
        _nonempty_string(model["models_dev_id"], f"{path}.models_dev_id")
        _nonempty_string(model["display_name"], f"{path}.display_name")
        _nonempty_string(model["description"], f"{path}.description")
        _validate_reported(model["reported"], f"{path}.reported")
        if "tested" in model:
            _validate_tested(model["tested"], f"{path}.tested")

    # Round-trip through ordinary containers so callers never retain a TOML or
    # custom Mapping implementation with surprising mutation semantics.
    return {
        "schema_version": root["schema_version"],
        "generated_at": root["generated_at"],
        "models_dev_source": dict(source),
        "models": [dict(model) for model in models],
    }


def _validate_reported(value: Any, path: str) -> None:
    reported = _mapping(value, path)
    _exact_keys(
        reported,
        path,
        required={"input_modalities"},
        optional={"context_window", "models_dev_updated_at"},
    )
    if "context_window" in reported:
        _positive_int(reported["context_window"], f"{path}.context_window")
    if "models_dev_updated_at" in reported:
        _nonempty_string(reported["models_dev_updated_at"], f"{path}.models_dev_updated_at")
    modalities = reported["input_modalities"]
    if (
        not isinstance(modalities, list)
        or any(modality not in {"text", "image"} for modality in modalities)
        or len(modalities) != len(set(modalities))
        or "text" not in modalities
    ):
        raise KnownCatalogError(
            f"{path}.input_modalities must be unique text/image values including text"
        )


def _validate_tested(value: Any, path: str) -> None:
    tested = _mapping(value, path)
    _exact_keys(
        tested,
        path,
        required={"tested_at", "probe_version", "runtime", "checks"},
        optional={"default_reasoning_effort"},
    )
    _timestamp(tested["tested_at"], f"{path}.tested_at")
    _nonempty_string(tested["probe_version"], f"{path}.probe_version")
    runtime = _mapping(tested["runtime"], f"{path}.runtime")
    _exact_keys(runtime, f"{path}.runtime", required={"name", "version"})
    _nonempty_string(runtime["name"], f"{path}.runtime.name")
    _nonempty_string(runtime["version"], f"{path}.runtime.version")

    checks = _mapping(tested["checks"], f"{path}.checks")
    _exact_keys(
        checks,
        f"{path}.checks",
        required={
            "model_list",
            "responses_streaming",
            "standard_tools",
            "vision",
            "reasoning_efforts",
            "reasoning_summary",
        },
    )
    states = {
        name: _check_state(checks[name], f"{path}.checks.{name}")
        for name in (
            "model_list",
            "responses_streaming",
            "standard_tools",
            "vision",
            "reasoning_summary",
        )
    }
    efforts = _mapping(checks["reasoning_efforts"], f"{path}.checks.reasoning_efforts")
    passed_efforts: list[str] = []
    for effort, state in efforts.items():
        if effort not in REASONING_EFFORTS:
            raise KnownCatalogError(
                f"{path}.checks.reasoning_efforts contains unsupported effort {effort}"
            )
        if _check_state(state, f"{path}.checks.reasoning_efforts.{effort}") == "pass":
            passed_efforts.append(str(effort))

    promoted = (
        states["standard_tools"] == "pass"
        or states["vision"] == "pass"
        or states["reasoning_summary"] == "pass"
        or bool(passed_efforts)
    )
    if promoted and (
        states["model_list"] != "pass" or states["responses_streaming"] != "pass"
    ):
        raise KnownCatalogError(
            f"{path} cannot promote capabilities without model-list and streaming evidence"
        )
    default = tested.get("default_reasoning_effort")
    if default is not None and default not in passed_efforts:
        raise KnownCatalogError(
            f"{path}.default_reasoning_effort must name a passing effort"
        )


def catalog_models_by_endpoint(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Index an already validated catalog by its exact endpoint identifier."""

    return {str(model["endpoint_id"]): dict(model) for model in payload["models"]}


def catalog_model_status(model: Mapping[str, Any]) -> str:
    tested = model.get("tested")
    if not isinstance(tested, Mapping):
        return "known"
    checks = tested.get("checks")
    if not isinstance(checks, Mapping):
        return "known"
    required = ("model_list", "responses_streaming", "standard_tools")
    return "tested" if all(checks.get(name) == "pass" for name in required) else "known"


__all__ = [
    "CHECK_STATES",
    "KNOWN_CATALOG_MAX_BYTES",
    "KNOWN_CATALOG_SCHEMA_VERSION",
    "KNOWN_LOCAL_CATALOG_URL",
    "MODELS_DEV_MODELS_URL",
    "REASONING_EFFORTS",
    "KnownCatalogError",
    "KnownCatalogProvenance",
    "catalog_model_status",
    "catalog_models_by_endpoint",
    "validate_known_catalog",
]
