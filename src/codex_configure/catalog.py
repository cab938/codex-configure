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


UMICH_MODELS_URL = "https://api.toolkit.umgpt.umich.edu/v1/models"
FALLBACK_MODEL_IDS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
VERIFIED_MODEL_IDS = {"gpt-5.6-terra"}


@dataclass(frozen=True)
class ModelChoice:
    slug: str
    display_name: str
    status: str
    catalog_entry: dict[str, Any]


@dataclass(frozen=True)
class CatalogResult:
    models: tuple[ModelChoice, ...]
    source: str
    warning: str | None = None


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

    def discover(self) -> CatalogResult:
        bundled = self._load_bundled_catalog()
        bundled_by_slug = {
            entry.get("slug"): entry
            for entry in bundled
            if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
        }

        warning = None
        try:
            advertised_ids = self._fetch_umich_model_ids()
            source = self.models_url
        except (OSError, ValueError, urllib.error.URLError) as exc:
            advertised_ids = set(FALLBACK_MODEL_IDS)
            source = "maintained fallback"
            warning = f"U-M model discovery failed ({type(exc).__name__}); using the maintained fallback."

        eligible_ids = advertised_ids.intersection(bundled_by_slug)
        if not eligible_ids:
            eligible_ids = set(FALLBACK_MODEL_IDS).intersection(bundled_by_slug)
            source = "maintained fallback"
            warning = "U-M returned no models recognized by this Codex build; using the maintained fallback."

        choices = []
        for entry in bundled:
            slug = entry.get("slug") if isinstance(entry, dict) else None
            if slug not in eligible_ids or not entry.get("supported_in_api", True):
                continue
            choices.append(
                ModelChoice(
                    slug=slug,
                    display_name=str(entry.get("display_name") or slug),
                    status="verified" if slug in VERIFIED_MODEL_IDS else "listed",
                    catalog_entry=entry,
                )
            )

        if not choices:
            raise UserFacingError("No U-M models are compatible with the installed Codex catalog.")

        return CatalogResult(models=tuple(choices), source=source, warning=warning)

    def build_selected_catalog(self, models: list[ModelChoice]) -> dict[str, Any]:
        selected = []
        for priority, model in enumerate(models, start=1):
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

    def _fetch_umich_model_ids(self) -> set[str]:
        request = urllib.request.Request(
            self.models_url,
            headers={"Accept": "application/json", "User-Agent": "codex-configure/0.1"},
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
