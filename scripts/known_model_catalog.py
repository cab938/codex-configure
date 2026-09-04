#!/usr/bin/env python3
"""Maintain and probe the owned local-model catalog.

The script is intentionally repository-only and standard-library-only.  It
never accepts a credential value on the command line and never records model
prompts, outputs, endpoint payloads, or secrets in probe reports.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPOSITORY_ROOT / "catalog" / "v1" / "local-models.json"
PROBE_VERSION = "1"
UPSTREAM_MAX_BYTES = 16 * 1024 * 1024
PROBE_MAX_BYTES = 4 * 1024 * 1024
API_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TEXT_MARKER = "CODEX_CATALOG_TEXT_OK"
TOOL_ARGUMENT = "catalog-tool-token"
TOOL_MARKER = "CODEX_CATALOG_TOOL_OK"
VISION_MARKER = "BLUE"
# Deterministic 1x1 blue PNG.
BLUE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _load_schema_module() -> ModuleType:
    path = REPOSITORY_ROOT / "src" / "codex-configure" / "known_catalog.py"
    spec = importlib.util.spec_from_file_location("codex_configure_known_catalog", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load catalog schema from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCHEMA = _load_schema_module()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_catalog(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read catalog {path}: {exc}") from exc
    return SCHEMA.validate_known_catalog(payload)


def _write_catalog(path: Path, payload: Mapping[str, Any]) -> None:
    validated = SCHEMA.validate_known_catalog(payload)
    _atomic_write(path, json.dumps(validated, indent=2, ensure_ascii=True) + "\n")


def _fetch(url: str, *, timeout: float, max_bytes: int) -> tuple[bytes, Mapping[str, str]]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "codex-configure-catalog/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"response from {url} exceeds {max_bytes} bytes")
        return raw, getattr(response, "headers", {})


def command_import(args: argparse.Namespace) -> int:
    catalog = _read_catalog(args.catalog)
    raw, _ = _fetch(
        SCHEMA.MODELS_DEV_MODELS_URL,
        timeout=args.timeout,
        max_bytes=UPSTREAM_MAX_BYTES,
    )
    upstream = json.loads(raw.decode("utf-8"))
    if not isinstance(upstream, dict):
        raise ValueError("Models.dev models.json must be an object")
    current = {model["endpoint_id"]: model for model in catalog["models"]}
    for mapping in args.model:
        endpoint_id, separator, models_dev_id = mapping.partition("=")
        if not separator or not endpoint_id.strip() or not models_dev_id.strip():
            raise ValueError("--model must be ENDPOINT_ID=MODELS_DEV_ID")
        source = upstream.get(models_dev_id)
        if not isinstance(source, dict) or source.get("id") != models_dev_id:
            raise ValueError(f"Models.dev has no exact model {models_dev_id}")
        modalities = source.get("modalities")
        modalities = modalities.get("input") if isinstance(modalities, dict) else None
        reported_modalities = [
            value for value in (modalities if isinstance(modalities, list) else [])
            if value in {"text", "image"}
        ]
        if "text" not in reported_modalities:
            raise ValueError(f"Models.dev model {models_dev_id} does not report text input")
        reported: dict[str, Any] = {"input_modalities": list(dict.fromkeys(reported_modalities))}
        limits = source.get("limit")
        context_window = limits.get("context") if isinstance(limits, dict) else None
        if isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0:
            reported["context_window"] = context_window
        updated = source.get("last_updated")
        if isinstance(updated, str) and updated:
            reported["models_dev_updated_at"] = updated
        prior = current.get(endpoint_id)
        model: dict[str, Any] = {
            "endpoint_id": endpoint_id,
            "models_dev_id": models_dev_id,
            "display_name": str(source.get("name") or endpoint_id),
            "description": str(source.get("description") or f"Local {source.get('name') or endpoint_id}"),
            "reported": reported,
        }
        if isinstance(prior, dict) and prior.get("models_dev_id") == models_dev_id and "tested" in prior:
            model["tested"] = prior["tested"]
        current[endpoint_id] = model

    now = _utc_now()
    catalog["generated_at"] = now
    catalog["models_dev_source"] = {
        "url": SCHEMA.MODELS_DEV_MODELS_URL,
        "retrieved_at": now,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    catalog["models"] = sorted(
        current.values(), key=lambda model: (model["endpoint_id"].casefold(), model["endpoint_id"])
    )
    _write_catalog(args.catalog, catalog)
    print(f"updated {args.catalog} with {len(args.model)} Models.dev mapping(s)")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    catalog = _read_catalog(args.catalog)
    print(f"valid catalog schema v{catalog['schema_version']}: {len(catalog['models'])} model(s)")
    return 0


def command_certify(args: argparse.Namespace) -> int:
    catalog = _read_catalog(args.catalog)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read probe report {args.report}: {exc}") from exc
    if not isinstance(report, dict) or set(report) != {"schema_version", "endpoint_id", "tested"}:
        raise ValueError("probe report has an invalid top-level shape")
    if report.get("schema_version") != 1 or not isinstance(report.get("endpoint_id"), str):
        raise ValueError("probe report has an invalid schema version or endpoint ID")
    found = False
    for model in catalog["models"]:
        if model["endpoint_id"] == report["endpoint_id"]:
            model["tested"] = report["tested"]
            found = True
            break
    if not found:
        raise ValueError(
            f"catalog has no exact endpoint_id {report['endpoint_id']}; import it before certifying"
        )
    catalog["generated_at"] = _utc_now()
    _write_catalog(args.catalog, catalog)
    print(f"certified {report['endpoint_id']} in {args.catalog}")
    return 0


def _endpoint_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "codex-configure-catalog-probe/1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _get_model_ids(base_url: str, api_key: str | None, timeout: float) -> set[str]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models", headers=_endpoint_headers(api_key)
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(PROBE_MAX_BYTES + 1)
    if len(raw) > PROBE_MAX_BYTES:
        raise ValueError("/models response is too large")
    payload = json.loads(raw.decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("/models response has no data array")
    return {
        item["id"] for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _stream_response(
    base_url: str,
    body: Mapping[str, Any],
    api_key: str | None,
    timeout: float,
) -> list[dict[str, Any]]:
    headers = _endpoint_headers(api_key)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/responses",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    events: list[dict[str, Any]] = []
    total = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            total += len(raw_line)
            if total > PROBE_MAX_BYTES:
                raise ValueError("Responses stream is too large")
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            if isinstance(event, dict):
                events.append(event)
    if not events:
        raise ValueError("Responses stream contained no JSON events")
    return events


def _response_from_events(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for event in reversed(events):
        response = event.get("response")
        if isinstance(response, Mapping):
            return response
    return None


def _response_text(events: Sequence[Mapping[str, Any]]) -> str:
    deltas = [
        event["delta"] for event in events
        if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str)
    ]
    if deltas:
        return "".join(deltas).strip()
    response = _response_from_events(events)
    if response is None:
        return ""
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()
    parts: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            content = item.get("content") if isinstance(item, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts).strip()


def _function_call(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    candidates: list[Any] = []
    for event in events:
        if event.get("type") == "response.output_item.done":
            candidates.append(event.get("item"))
    response = _response_from_events(events)
    if response is not None and isinstance(response.get("output"), list):
        candidates.extend(response["output"])
    for item in candidates:
        if isinstance(item, dict) and item.get("type") == "function_call":
            return item
    return None


def _basic_body(model: str, input_value: Any) -> dict[str, Any]:
    return {
        "model": model,
        "input": input_value,
        "stream": True,
        "store": False,
    }


def _probe_text(base_url: str, model: str, key: str | None, timeout: float) -> bool:
    body = _basic_body(model, [_message(f"Reply with exactly {TEXT_MARKER}")])
    return _response_text(_stream_response(base_url, body, key, timeout)) == TEXT_MARKER


def _probe_tools(base_url: str, model: str, key: str | None, timeout: float) -> bool:
    prompt = (
        f"Call catalog_probe with token {TOOL_ARGUMENT}. After its output, reply with exactly "
        f"{TOOL_MARKER}."
    )
    initial = _message(prompt)
    tool = {
        "type": "function",
        "name": "catalog_probe",
        "description": "Return a deterministic token to the catalog compatibility probe.",
        "parameters": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
        "strict": True,
    }
    first = _basic_body(model, [initial])
    first.update({"tools": [tool], "tool_choice": "required", "parallel_tool_calls": False})
    call = _function_call(_stream_response(base_url, first, key, timeout))
    if call is None or call.get("name") != "catalog_probe":
        return False
    arguments = call.get("arguments")
    call_id = call.get("call_id")
    if not isinstance(arguments, str) or not isinstance(call_id, str):
        return False
    try:
        parsed_arguments = json.loads(arguments)
    except json.JSONDecodeError:
        return False
    if parsed_arguments != {"token": TOOL_ARGUMENT}:
        return False
    transcript_call = {
        "type": "function_call",
        "name": "catalog_probe",
        "arguments": arguments,
        "call_id": call_id,
    }
    tool_output = {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps({"token": TOOL_ARGUMENT}),
    }
    second = _basic_body(model, [initial, transcript_call, tool_output])
    second.update({"tools": [tool], "tool_choice": "none", "parallel_tool_calls": False})
    return _response_text(_stream_response(base_url, second, key, timeout)) == TOOL_MARKER


def _probe_vision(base_url: str, model: str, key: str | None, timeout: float) -> bool:
    data_url = "data:image/png;base64," + base64.b64encode(BLUE_PNG).decode("ascii")
    input_value = [{
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": f"Name the pixel color. Reply with exactly {VISION_MARKER}."},
            {"type": "input_image", "image_url": data_url, "detail": "low"},
        ],
    }]
    return _response_text(
        _stream_response(base_url, _basic_body(model, input_value), key, timeout)
    ) == VISION_MARKER


def _probe_reasoning(
    base_url: str,
    model: str,
    key: str | None,
    timeout: float,
    *,
    effort: str,
    summary: bool = False,
) -> bool:
    marker = f"CODEX_REASONING_{effort.upper()}_OK"
    body = _basic_body(model, [_message(f"Reply with exactly {marker}")])
    reasoning: dict[str, Any] = {"effort": effort}
    if summary:
        reasoning["summary"] = "auto"
    body["reasoning"] = reasoning
    return _response_text(_stream_response(base_url, body, key, timeout)) == marker


def _safe_check(operation: Any) -> str:
    try:
        return "pass" if operation() else "fail"
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        return "fail"


def command_probe(args: argparse.Namespace) -> int:
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--base-url must be an http:// or https:// URL")
    if args.api_key_env and not API_ENV_RE.fullmatch(args.api_key_env):
        raise ValueError("--api-key-env must be an uppercase environment variable name")
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    if args.api_key_env and not api_key:
        raise ValueError(f"environment variable {args.api_key_env} is empty or missing")
    if args.reasoning_effort and not args.default_reasoning_effort:
        raise ValueError("--default-reasoning-effort is required with --reasoning-effort")
    if args.default_reasoning_effort and args.default_reasoning_effort not in args.reasoning_effort:
        raise ValueError("--default-reasoning-effort must be one of the requested efforts")

    checks: dict[str, Any] = {
        "model_list": _safe_check(
            lambda: args.model in _get_model_ids(args.base_url, api_key, args.timeout)
        ),
        "responses_streaming": "not_run",
        "standard_tools": "not_run",
        "vision": "not_run",
        "reasoning_efforts": {},
        "reasoning_summary": "not_run",
    }
    if checks["model_list"] == "pass":
        checks["responses_streaming"] = _safe_check(
            lambda: _probe_text(args.base_url, args.model, api_key, args.timeout)
        )
    if checks["responses_streaming"] == "pass":
        checks["standard_tools"] = _safe_check(
            lambda: _probe_tools(args.base_url, args.model, api_key, args.timeout)
        )
        if args.vision:
            checks["vision"] = _safe_check(
                lambda: _probe_vision(args.base_url, args.model, api_key, args.timeout)
            )
        for effort in args.reasoning_effort:
            checks["reasoning_efforts"][effort] = _safe_check(
                lambda effort=effort: _probe_reasoning(
                    args.base_url, args.model, api_key, args.timeout, effort=effort
                )
            )
        if args.reasoning_summary:
            summary_effort = args.default_reasoning_effort or "medium"
            checks["reasoning_summary"] = _safe_check(
                lambda: _probe_reasoning(
                    args.base_url,
                    args.model,
                    api_key,
                    args.timeout,
                    effort=summary_effort,
                    summary=True,
                )
            )

    tested: dict[str, Any] = {
        "tested_at": _utc_now(),
        "probe_version": PROBE_VERSION,
        "runtime": {"name": args.runtime_name, "version": args.runtime_version},
        "checks": checks,
    }
    if (
        args.default_reasoning_effort
        and checks["reasoning_efforts"].get(args.default_reasoning_effort) == "pass"
    ):
        tested["default_reasoning_effort"] = args.default_reasoning_effort
    report = {"schema_version": 1, "endpoint_id": args.model, "tested": tested}
    print(json.dumps(report, indent=2, ensure_ascii=True))

    requested_states = [
        checks["model_list"],
        checks["responses_streaming"],
        checks["standard_tools"],
    ]
    if args.vision:
        requested_states.append(checks["vision"])
    requested_states.extend(checks["reasoning_efforts"].values())
    if args.reasoning_summary:
        requested_states.append(checks["reasoning_summary"])
    return 0 if all(state == "pass" for state in requested_states) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import selected Models.dev facts")
    import_parser.add_argument(
        "--model",
        action="append",
        required=True,
        metavar="ENDPOINT_ID=MODELS_DEV_ID",
    )
    import_parser.add_argument("--timeout", type=float, default=10.0)
    import_parser.set_defaults(handler=command_import)

    validate_parser = subparsers.add_parser("validate", help="validate the checked-in catalog")
    validate_parser.set_defaults(handler=command_validate)

    certify_parser = subparsers.add_parser("certify", help="merge one sanitized probe report")
    certify_parser.add_argument("--report", type=Path, required=True)
    certify_parser.set_defaults(handler=command_certify)

    probe_parser = subparsers.add_parser("probe", help="probe one exact endpoint model")
    probe_parser.add_argument("--base-url", required=True)
    probe_parser.add_argument("--model", required=True)
    probe_parser.add_argument("--runtime-name", required=True)
    probe_parser.add_argument("--runtime-version", required=True)
    probe_parser.add_argument("--api-key-env")
    probe_parser.add_argument("--timeout", type=float, default=60.0)
    probe_parser.add_argument("--vision", action="store_true")
    probe_parser.add_argument(
        "--reasoning-effort",
        action="append",
        default=[],
        choices=SCHEMA.REASONING_EFFORTS,
    )
    probe_parser.add_argument(
        "--default-reasoning-effort",
        choices=SCHEMA.REASONING_EFFORTS,
    )
    probe_parser.add_argument("--reasoning-summary", action="store_true")
    probe_parser.set_defaults(handler=command_probe)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"known_model_catalog.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
