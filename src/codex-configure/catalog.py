from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .errors import UserFacingError
from .known_catalog import (
    KNOWN_CATALOG_MAX_BYTES,
    KNOWN_LOCAL_CATALOG_URL,
    REASONING_EFFORTS,
    KnownCatalogError,
    KnownCatalogProvenance,
    catalog_model_status,
    catalog_models_by_endpoint,
    validate_known_catalog,
)
from .providers import UMICH_TOOLKIT_DISCOVERY_URL


UMICH_MODELS_URL = UMICH_TOOLKIT_DISCOVERY_URL
FALLBACK_MODEL_IDS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
VERIFIED_MODEL_IDS = {"gpt-5.6-terra"}
LOCAL_NON_GENERATION_MARKERS = ("embedding", "reranker", "rerank")


def is_default_model_slug(slug: str) -> bool:
    """Whether setup should check this compatible model by default."""

    return slug == "gpt-5.6" or slug.startswith("gpt-5.6-")


@dataclass(frozen=True)
class ModelChoice:
    slug: str
    display_name: str
    status: str
    catalog_entry: dict[str, Any]
    selectable: bool = True
    badges: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogResult:
    models: tuple[ModelChoice, ...]
    source: str
    warning: str | None = None
    known_catalog: KnownCatalogProvenance | None = None

    @property
    def selectable_models(self) -> tuple[ModelChoice, ...]:
        return tuple(model for model in self.models if model.selectable)

    @property
    def advertised_ids(self) -> tuple[str, ...]:
        return tuple(model.slug for model in self.models)


class CatalogService:
    def __init__(
        self,
        codex_home: Path,
        codex_command: str = "codex",
        models_url: str = UMICH_MODELS_URL,
        timeout_seconds: float = 10.0,
        known_catalog_url: str = KNOWN_LOCAL_CATALOG_URL,
    ) -> None:
        self.codex_home = codex_home
        self.codex_command = codex_command
        self.models_url = models_url
        self.timeout_seconds = timeout_seconds
        self.known_catalog_url = known_catalog_url

    def discover(self, api_key: str | None = None) -> CatalogResult:
        """Discover all endpoint IDs and mark entries absent from Core metadata.

        The Toolkit endpoint is key-scoped even though it currently advertises
        the same public list for many keys.  Always send the key when one is
        available.  A key-backed discovery failure is fatal: writing a
        provider catalog from a maintained fallback would make the catalog
        claim models the user's endpoint did not advertise.

        The no-key fallback remains for the legacy OpenAI/U-M interactive flow;
        new named-provider setup always supplies a key.
        """
        bundled = self._load_bundled_catalog()
        bundled_by_slug = {
            entry.get("slug"): entry
            for entry in bundled
            if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
        }

        warning = None
        try:
            advertised_ids = self._fetch_umich_model_ids(api_key=api_key)
            source = self.models_url
        except (OSError, ValueError, urllib.error.URLError) as exc:
            if api_key:
                raise UserFacingError(
                    f"Could not discover models for the U-M Toolkit key ({type(exc).__name__})."
                ) from exc
            advertised_ids = set(FALLBACK_MODEL_IDS)
            source = "maintained fallback"
            warning = f"U-M model discovery failed ({type(exc).__name__}); using the maintained fallback."

        if not advertised_ids:
            advertised_ids = set(FALLBACK_MODEL_IDS).intersection(bundled_by_slug)
            source = "maintained fallback"
            warning = "U-M returned no models recognized by this Codex build; using the maintained fallback."

        # Preserve every endpoint ID in the result so the setup UI can explain
        # why a model is unavailable.  Only a bundled, API-supported entry is
        # selectable and therefore eligible for the authoritative JSON we
        # write to disk.
        choices = []
        for slug in sorted(advertised_ids, key=lambda value: (value.casefold(), value)):
            entry = bundled_by_slug.get(slug)
            selectable = isinstance(entry, dict) and entry.get("supported_in_api", True)
            if not selectable:
                choices.append(
                    ModelChoice(
                        slug=slug,
                        display_name=slug,
                        # The CLI treats this exact status as non-selectable;
                        # the explanatory wording belongs in its label/help.
                        status="unsupported",
                        catalog_entry={},
                        selectable=False,
                    )
                )
                continue
            choices.append(
                ModelChoice(
                    slug=slug,
                    display_name=str(entry.get("display_name") or slug),
                    status="verified" if slug in VERIFIED_MODEL_IDS else "listed",
                    catalog_entry=entry,
                    selectable=True,
                )
            )

        if not choices:
            raise UserFacingError(
                "The U-M endpoint returned no model identifiers."
            )

        return CatalogResult(models=tuple(choices), source=source, warning=warning)

    def build_selected_catalog(self, models: list[ModelChoice]) -> dict[str, Any]:
        selected = []
        for priority, model in enumerate(models, start=1):
            if not model.selectable or not model.catalog_entry:
                raise UserFacingError(
                    f"Model `{model.slug}` is not supported by this Codex build and cannot be selected."
                )
            entry = copy.deepcopy(model.catalog_entry)
            entry["visibility"] = "list"
            entry["priority"] = priority
            selected.append(entry)
        return {"models": selected}

    def discover_local(self, base_url: str, api_key: str | None = None) -> CatalogResult:
        """Discover model IDs from a local OpenAI-compatible `/models` endpoint."""

        known_models, provenance, warning = self._load_known_local_catalog()
        models_url = f"{base_url.rstrip('/')}/models"
        try:
            advertised = self._fetch_local_models(models_url, api_key)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                detail = "authentication failed"
            else:
                detail = f"HTTP {exc.code}"
            raise UserFacingError(f"Could not discover local models at {models_url}: {detail}.") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise UserFacingError(
                f"Could not discover local models at {models_url} ({type(exc).__name__})."
            ) from exc

        choices: list[ModelChoice] = []
        for item in sorted(advertised, key=lambda value: (value["id"].casefold(), value["id"])):
            slug = item["id"]
            metadata = item.get("meta")
            metadata = metadata if isinstance(metadata, dict) else {}
            context_window = metadata.get("n_ctx")
            if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
                context_window = None
            known = known_models.get(slug)
            reported = known.get("reported") if isinstance(known, dict) else None
            reported = reported if isinstance(reported, dict) else {}
            known_context = reported.get("context_window")
            if (
                isinstance(known_context, bool)
                or not isinstance(known_context, int)
                or known_context <= 0
            ):
                known_context = None
            context_window = _minimum_known(context_window, known_context)
            is_generation_model = not any(
                marker in slug.casefold() for marker in LOCAL_NON_GENERATION_MARKERS
            )
            status = catalog_model_status(known) if isinstance(known, dict) else "unverified"
            display_name = (
                str(known.get("display_name"))
                if isinstance(known, dict) and known.get("display_name")
                else slug
            )
            entry: dict[str, Any] = {
                "slug": slug,
                "display_name": display_name,
                "description": (
                    str(known.get("description"))
                    if isinstance(known, dict) and known.get("description")
                    else f"Local model advertised by {base_url.rstrip('/')}"
                ),
                "input_modalities": ["text"],
            }
            if context_window is not None:
                entry["context_window"] = context_window
            badges: list[str] = []
            if context_window is not None:
                badges.append(f"context {context_window:,}")
            tested = known.get("tested") if isinstance(known, dict) else None
            checks = tested.get("checks") if isinstance(tested, dict) else None
            checks = checks if isinstance(checks, dict) else {}
            if checks.get("standard_tools") == "pass":
                badges.append("tools tested")
            if checks.get("vision") == "pass":
                entry["input_modalities"].append("image")
                badges.append("vision")
            effort_checks = checks.get("reasoning_efforts")
            effort_checks = effort_checks if isinstance(effort_checks, dict) else {}
            passed_efforts = [
                effort for effort in REASONING_EFFORTS if effort_checks.get(effort) == "pass"
            ]
            default_effort = (
                tested.get("default_reasoning_effort")
                if isinstance(tested, dict)
                else None
            )
            if passed_efforts and default_effort in passed_efforts:
                entry["supported_reasoning_levels"] = [
                    {"effort": effort, "description": _reasoning_description(effort)}
                    for effort in passed_efforts
                ]
                entry["default_reasoning_level"] = default_effort
                badges.append(f"reasoning {','.join(passed_efforts)}")
            if checks.get("reasoning_summary") == "pass":
                entry["supports_reasoning_summary_parameter"] = True
            choices.append(
                ModelChoice(
                    slug=slug,
                    display_name=display_name,
                    status=status if is_generation_model else "non-generation",
                    catalog_entry=entry if is_generation_model else {},
                    selectable=is_generation_model,
                    badges=tuple(badges),
                )
            )
        if not choices:
            raise UserFacingError(f"The local endpoint at {models_url} returned no model identifiers.")
        return CatalogResult(
            models=tuple(choices),
            source=models_url,
            warning=warning,
            known_catalog=provenance,
        )

    def build_local_catalog(self, models: list[ModelChoice]) -> dict[str, Any]:
        """Build the narrow catalog schema expanded by the patched Core."""

        selected: list[dict[str, Any]] = []
        for priority, model in enumerate(models, start=1):
            if not model.selectable or not model.catalog_entry:
                raise UserFacingError(
                    f"Model `{model.slug}` is not a generation model and cannot be selected."
                )
            entry = copy.deepcopy(model.catalog_entry)
            entry["priority"] = priority
            selected.append(entry)
        return {"models": selected}

    def _load_bundled_catalog(self) -> list[dict[str, Any]]:
        child_env = os.environ.copy()
        child_env["CODEX_HOME"] = str(self.codex_home)
        try:
            completed = subprocess.run(
                [self.codex_command, "debug", "models", "--bundled"],
                check=False,
                capture_output=True,
                text=True,
                env=child_env,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UserFacingError(f"Could not inspect the installed Codex model catalog: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown error"
            raise UserFacingError(f"Could not inspect the installed Codex model catalog: {detail}")
        try:
            payload = json.loads(completed.stdout)
            models = payload["models"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise UserFacingError("The installed Codex command returned an invalid model catalog.") from exc
        if not isinstance(models, list):
            raise UserFacingError("The installed Codex command returned an invalid model catalog.")
        return models

    def _fetch_umich_model_ids(self, api_key: str | None = None) -> set[str]:
        headers = {"Accept": "application/json", "User-Agent": f"codex-configure/{__version__}"}
        if api_key:
            headers["x-portkey-api-key"] = api_key
        request = urllib.request.Request(
            self.models_url,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.load(response)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("model response has no data list")
        ids = {
            item["id"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if not ids:
            raise ValueError("model response contains no identifiers")
        return ids

    def _fetch_local_models(
        self,
        models_url: str,
        api_key: str | None,
    ) -> list[dict[str, Any]]:
        headers = {"Accept": "application/json", "User-Agent": f"codex-configure/{__version__}"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(models_url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.load(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("model response has no data list")
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in data:
            model_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
                continue
            seen.add(model_id)
            models.append(item)
        if not models:
            raise ValueError("model response contains no identifiers")
        return models

    def _load_known_local_catalog(
        self,
    ) -> tuple[dict[str, dict[str, Any]], KnownCatalogProvenance, str | None]:
        """Fetch the owned advisory catalog, falling back to a valid cache."""

        cached = self._read_known_catalog_cache()
        if urlsplit(self.known_catalog_url).scheme != "https":
            return self._known_catalog_fallback(cached, "catalog URL is not HTTPS")
        headers = {
            "Accept": "application/json",
            "User-Agent": f"codex-configure/{__version__}",
        }
        if cached is not None and cached[1].etag:
            headers["If-None-Match"] = cached[1].etag
        request = urllib.request.Request(self.known_catalog_url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(KNOWN_CATALOG_MAX_BYTES + 1)
                if len(raw) > KNOWN_CATALOG_MAX_BYTES:
                    raise KnownCatalogError("catalog response exceeds 2 MiB")
                payload = validate_known_catalog(json.loads(raw.decode("utf-8")))
                sha256 = hashlib.sha256(raw).hexdigest()
                response_headers = getattr(response, "headers", {})
                etag = response_headers.get("ETag") if hasattr(response_headers, "get") else None
                provenance = KnownCatalogProvenance(
                    state="fresh",
                    url=self.known_catalog_url,
                    sha256=sha256,
                    etag=etag,
                    fetched_at=_utc_now(),
                )
                self._write_known_catalog_cache(raw.decode("utf-8"), provenance)
                return catalog_models_by_endpoint(payload), provenance, None
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached is not None:
                payload, cached_provenance = cached
                provenance = KnownCatalogProvenance(
                    state="fresh",
                    url=cached_provenance.url,
                    sha256=cached_provenance.sha256,
                    etag=cached_provenance.etag,
                    fetched_at=cached_provenance.fetched_at,
                )
                return catalog_models_by_endpoint(payload), provenance, None
            reason = f"HTTP {exc.code}"
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KnownCatalogError,
            urllib.error.URLError,
        ) as exc:
            reason = type(exc).__name__
        return self._known_catalog_fallback(cached, reason)

    def _known_catalog_fallback(
        self,
        cached: tuple[dict[str, Any], KnownCatalogProvenance] | None,
        reason: str,
    ) -> tuple[dict[str, dict[str, Any]], KnownCatalogProvenance, str]:
        if cached is not None:
            payload, old = cached
            provenance = KnownCatalogProvenance(
                state="cached",
                url=old.url,
                sha256=old.sha256,
                etag=old.etag,
                fetched_at=old.fetched_at,
            )
            return (
                catalog_models_by_endpoint(payload),
                provenance,
                f"Known-model catalog refresh failed ({reason}); using the last valid cache.",
            )
        return (
            {},
            KnownCatalogProvenance(state="unavailable", url=self.known_catalog_url),
            f"Known-model catalog is unavailable ({reason}); endpoint models remain unverified.",
        )

    def _read_known_catalog_cache(
        self,
    ) -> tuple[dict[str, Any], KnownCatalogProvenance] | None:
        cache_path, metadata_path = self._known_catalog_cache_paths()
        try:
            raw = cache_path.read_bytes()
            if len(raw) > KNOWN_CATALOG_MAX_BYTES:
                return None
            payload = validate_known_catalog(json.loads(raw.decode("utf-8")))
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KnownCatalogError,
        ):
            return None
        metadata_fields = {
            "schema_version",
            "url",
            "sha256",
            "etag",
            "fetched_at",
        }
        if not isinstance(metadata, dict) or set(metadata) != metadata_fields:
            return None
        if metadata.get("schema_version") != 1 or metadata.get("url") != self.known_catalog_url:
            return None
        sha256 = hashlib.sha256(raw).hexdigest()
        if metadata.get("sha256") != sha256:
            return None
        fetched_at = metadata.get("fetched_at")
        if not isinstance(fetched_at, str) or not fetched_at:
            return None
        etag = metadata.get("etag")
        if etag is not None and not isinstance(etag, str):
            return None
        return (
            payload,
            KnownCatalogProvenance(
                state="cached",
                url=self.known_catalog_url,
                sha256=sha256,
                etag=etag,
                fetched_at=fetched_at,
            ),
        )

    def _write_known_catalog_cache(
        self,
        text: str,
        provenance: KnownCatalogProvenance,
    ) -> None:
        cache_path, metadata_path = self._known_catalog_cache_paths()
        metadata = {
            "schema_version": 1,
            "url": provenance.url,
            "etag": provenance.etag,
            "sha256": provenance.sha256,
            "fetched_at": provenance.fetched_at,
        }
        _atomic_private_write(cache_path, text)
        _atomic_private_write(metadata_path, json.dumps(metadata, indent=2) + "\n")

    def _known_catalog_cache_paths(self) -> tuple[Path, Path]:
        directory = self.codex_home / "codex-configure" / "cache"
        return (
            directory / "known-local-models-v1.json",
            directory / "known-local-models-v1.meta.json",
        )


def _minimum_known(left: int | None, right: int | None) -> int | None:
    values = [value for value in (left, right) if value is not None]
    return min(values) if values else None


def _reasoning_description(effort: str) -> str:
    return "XHigh" if effort == "xhigh" else effort.replace("_", " ").title()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "CatalogResult",
    "CatalogService",
    "FALLBACK_MODEL_IDS",
    "LOCAL_NON_GENERATION_MARKERS",
    "ModelChoice",
    "KnownCatalogProvenance",
    "UMICH_MODELS_URL",
    "VERIFIED_MODEL_IDS",
    "is_default_model_slug",
]
