from __future__ import annotations

import copy
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import UserFacingError
from .providers import UMICH_TOOLKIT_DISCOVERY_URL


UMICH_MODELS_URL = UMICH_TOOLKIT_DISCOVERY_URL
FALLBACK_MODEL_IDS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
VERIFIED_MODEL_IDS = {"gpt-5.6-terra"}


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


@dataclass(frozen=True)
class CatalogResult:
    models: tuple[ModelChoice, ...]
    source: str
    warning: str | None = None

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
    ) -> None:
        self.codex_home = codex_home
        self.codex_command = codex_command
        self.models_url = models_url
        self.timeout_seconds = timeout_seconds

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
        headers = {"Accept": "application/json", "User-Agent": "codex-configure/0.1"}
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


__all__ = [
    "CatalogResult",
    "CatalogService",
    "FALLBACK_MODEL_IDS",
    "ModelChoice",
    "UMICH_MODELS_URL",
    "VERIFIED_MODEL_IDS",
    "is_default_model_slug",
]
