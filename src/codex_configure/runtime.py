from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import tomlkit

from .catalog import ModelChoice
from .errors import UserFacingError

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS and Linux both provide fcntl
    fcntl = None


PROVIDER_ID = "umich-toolkit"
PROVIDER_URL = "https://api.portkey.ai/v1"


@dataclass(frozen=True)
class RuntimePaths:
    codex_home: Path
    root: Path
    base_config: Path
    original_config: Path
    profiles: Path
    catalogs: Path
    locks: Path
    state: Path
    active_config: Path

    @classmethod
    def from_home(cls, codex_home: Path) -> "RuntimePaths":
        root = codex_home / "codex-configure"
        return cls(
            codex_home=codex_home,
            root=root,
            base_config=root / "base" / "config.toml",
            original_config=root / "base" / "original-config.toml",
            profiles=root / "profiles",
            catalogs=root / "catalogs",
            locks=root / "locks",
            state=root / "state.toml",
            active_config=codex_home / "config.toml",
        )


class ConfigManager:
    def __init__(self, codex_home: Path) -> None:
        self.paths = RuntimePaths.from_home(codex_home.resolve())

    def initialize(self) -> None:
        codex_home_existed = self.paths.codex_home.exists()
        self.paths.codex_home.mkdir(parents=True, exist_ok=True)
        if not codex_home_existed:
            self.paths.codex_home.chmod(0o700)
        for directory in (
            self.paths.root,
            self.paths.base_config.parent,
            self.paths.profiles,
            self.paths.catalogs,
            self.paths.locks,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)

        if not self.paths.base_config.exists():
            original = self._read_active_config()
            self._validate_toml(original, self.paths.active_config)
            self._atomic_write(self.paths.base_config, original)
            state = tomlkit.document()
            state["schema_version"] = 1
            state["active_environment"] = "original"
            state["active_config_sha256"] = self._hash_text(original)
            self._atomic_write(self.paths.state, tomlkit.dumps(state))
        elif not self.paths.state.exists():
            raise UserFacingError(
                f"Runtime state is missing while {self.paths.base_config} exists; refusing to guess."
            )

        if not self.paths.original_config.exists():
            self._atomic_write(
                self.paths.original_config,
                self.paths.base_config.read_text(encoding="utf-8"),
            )

        self._write_openai_profile()

    def load_umich_preferences(self) -> tuple[list[str], str | None]:
        path = self.paths.profiles / "umich" / "profile.toml"
        if not path.exists():
            return [], None
        try:
            profile = tomlkit.parse(path.read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError):
            return [], None
        selected = profile.get("selected_models", [])
        default = profile.get("default_model")
        if not isinstance(selected, list):
            selected = []
        return [str(value) for value in selected], str(default) if default else None

    def activate_openai(self) -> Path:
        self.initialize()
        with self._activation_lock():
            self._reconcile_active_config()
            base = self.paths.base_config.read_text(encoding="utf-8")
            self._promote_active_config(base, "openai")
        return self.paths.profiles / "openai"

    def activate_umich(
        self,
        selected_models: list[ModelChoice],
        default_model: str,
        catalog: dict[str, Any],
        catalog_source: str,
    ) -> Path:
        self.initialize()
        selected_ids = [model.slug for model in selected_models]
        if not selected_ids or default_model not in selected_ids:
            raise UserFacingError("The U-M default model must be one of the selected models.")

        with self._activation_lock():
            self._reconcile_active_config()
            catalog_path = self.paths.catalogs / "umich-openai-azure.json"
            self._validate_catalog(catalog, selected_ids)
            self._atomic_write(catalog_path, json.dumps(catalog, indent=2) + "\n")

            overlay = self._umich_overlay(default_model, catalog_path)
            profile_path = self.paths.profiles / "umich"
            profile_path.mkdir(parents=True, exist_ok=True)
            profile_path.chmod(0o700)
            self._atomic_write(profile_path / "config.toml", tomlkit.dumps(overlay))
            self._write_umich_metadata(
                profile_path,
                selected_ids,
                default_model,
                catalog_path,
                catalog_source,
            )

            base = tomlkit.parse(self.paths.base_config.read_text(encoding="utf-8"))
            for key in ("model", "model_provider", "model_catalog_json"):
                base[key] = overlay[key]
            base_providers = base.get("model_providers")
            if base_providers is None:
                base_providers = tomlkit.table()
                base["model_providers"] = base_providers
            if not hasattr(base_providers, "__setitem__"):
                raise UserFacingError("Existing model_providers configuration is not a TOML table.")
            base_providers[PROVIDER_ID] = overlay["model_providers"][PROVIDER_ID]
            candidate = tomlkit.dumps(base)
            self._validate_toml(candidate, self.paths.active_config)
            self._promote_active_config(candidate, "umich")
        return profile_path

    def _write_openai_profile(self) -> None:
        profile_path = self.paths.profiles / "openai"
        profile_path.mkdir(parents=True, exist_ok=True)
        profile_path.chmod(0o700)
        metadata = tomlkit.document()
        metadata["schema_version"] = 1
        metadata["id"] = "openai"
        metadata["display_name"] = "OpenAI"
        metadata["catalog_strategy"] = "upstream"
        self._atomic_write(profile_path / "profile.toml", tomlkit.dumps(metadata))
        self._atomic_write(profile_path / "config.toml", "")

    def _write_umich_metadata(
        self,
        profile_path: Path,
        selected_ids: list[str],
        default_model: str,
        catalog_path: Path,
        catalog_source: str,
    ) -> None:
        metadata = tomlkit.document()
        metadata["schema_version"] = 1
        metadata["id"] = "umich"
        metadata["display_name"] = "U-M GPT Toolkit"
        metadata["provider"] = "OpenAI / Azure"
        metadata["provider_id"] = PROVIDER_ID
        metadata["catalog_source"] = catalog_source
        metadata["catalog_path"] = str(catalog_path)
        metadata["selected_models"] = selected_ids
        metadata["default_model"] = default_model
        self._atomic_write(profile_path / "profile.toml", tomlkit.dumps(metadata))

    def _umich_overlay(self, default_model: str, catalog_path: Path) -> Any:
        overlay = tomlkit.document()
        overlay["model"] = default_model
        overlay["model_provider"] = PROVIDER_ID
        overlay["model_catalog_json"] = str(catalog_path)

        providers = tomlkit.table()
        provider = tomlkit.table()
        provider["name"] = "U-M GPT Toolkit - OpenAI / Azure"
        provider["base_url"] = PROVIDER_URL
        provider["wire_api"] = "responses"
        headers = tomlkit.inline_table()
        headers["x-portkey-api-key"] = "UMICH_TOOLKIT_API_KEY"
        provider["env_http_headers"] = headers
        provider["request_max_retries"] = 2
        provider["stream_max_retries"] = 2
        providers[PROVIDER_ID] = provider
        overlay["model_providers"] = providers
        return overlay

    def _reconcile_active_config(self) -> None:
        state = tomlkit.parse(self.paths.state.read_text(encoding="utf-8"))
        expected = state.get("active_config_sha256")
        active_text = self._read_active_config()
        actual = self._hash_text(active_text)
        if expected != actual:
            current = tomlkit.parse(active_text)
            base = tomlkit.parse(self.paths.base_config.read_text(encoding="utf-8"))
            environment = state.get("active_environment")

            if environment == "umich":
                overlay_path = self.paths.profiles / "umich" / "config.toml"
                if not overlay_path.exists():
                    raise UserFacingError(
                        f"U-M profile state is missing at {overlay_path}; refusing to guess."
                    )
                overlay = tomlkit.parse(overlay_path.read_text(encoding="utf-8"))
                if not self._routing_matches(current, overlay):
                    raise UserFacingError(
                        f"{self.paths.active_config} changed provider routing outside "
                        "codex-configure; refusing to overwrite it."
                    )
                reconciled = self._restore_base_routing(current, base)
            elif environment in {"original", "openai"}:
                reconciled = current
            else:
                raise UserFacingError(
                    f"Unknown active environment {environment!r}; refusing to guess."
                )

            candidate = tomlkit.dumps(reconciled)
            self._validate_toml(candidate, self.paths.base_config)
            self._atomic_write(self.paths.base_config, candidate)

    @staticmethod
    def _routing_matches(current: Any, overlay: Any) -> bool:
        for key in ("model", "model_provider", "model_catalog_json"):
            if (key in current) != (key in overlay):
                return False
            if key in overlay and ConfigManager._unwrap(current[key]) != ConfigManager._unwrap(
                overlay[key]
            ):
                return False

        current_providers = current.get("model_providers")
        overlay_providers = overlay.get("model_providers")
        if current_providers is None or overlay_providers is None:
            return current_providers is overlay_providers
        if PROVIDER_ID not in current_providers or PROVIDER_ID not in overlay_providers:
            return False
        return ConfigManager._unwrap(current_providers[PROVIDER_ID]) == ConfigManager._unwrap(
            overlay_providers[PROVIDER_ID]
        )

    @staticmethod
    def _restore_base_routing(current: Any, base: Any) -> Any:
        reconciled = copy.deepcopy(current)
        for key in ("model", "model_provider", "model_catalog_json"):
            if key in base:
                reconciled[key] = copy.deepcopy(base[key])
            elif key in reconciled:
                del reconciled[key]

        reconciled_providers = reconciled.get("model_providers")
        base_providers = base.get("model_providers")
        base_has_provider = base_providers is not None and PROVIDER_ID in base_providers
        if base_has_provider:
            if reconciled_providers is None:
                reconciled_providers = tomlkit.table()
                reconciled["model_providers"] = reconciled_providers
            reconciled_providers[PROVIDER_ID] = copy.deepcopy(base_providers[PROVIDER_ID])
        elif reconciled_providers is not None and PROVIDER_ID in reconciled_providers:
            del reconciled_providers[PROVIDER_ID]
            if len(reconciled_providers) == 0:
                del reconciled["model_providers"]
        return reconciled

    @staticmethod
    def _unwrap(value: Any) -> Any:
        unwrap = getattr(value, "unwrap", None)
        return unwrap() if unwrap else value

    def _promote_active_config(self, text: str, environment: str) -> None:
        self._atomic_write(self.paths.active_config, text)
        state = tomlkit.document()
        state["schema_version"] = 1
        state["active_environment"] = environment
        state["active_config_sha256"] = self._hash_text(text)
        self._atomic_write(self.paths.state, tomlkit.dumps(state))

    @contextmanager
    def _activation_lock(self) -> Iterator[None]:
        lock_path = self.paths.locks / "activate.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        lock_path.chmod(0o600)
        with lock_path.open("r+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_active_config(self) -> str:
        try:
            return self.paths.active_config.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _validate_toml(text: str, path: Path) -> None:
        try:
            tomlkit.parse(text)
        except tomlkit.exceptions.ParseError as exc:
            raise UserFacingError(f"Invalid TOML in {path}: {exc}") from exc

    @staticmethod
    def _validate_catalog(catalog: dict[str, Any], expected_ids: list[str]) -> None:
        models = catalog.get("models")
        if not isinstance(models, list):
            raise UserFacingError("Generated U-M catalog has no models list.")
        actual_ids = [entry.get("slug") for entry in models if isinstance(entry, dict)]
        if actual_ids != expected_ids:
            raise UserFacingError("Generated U-M catalog does not match the selected models.")

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, path)
            path.chmod(0o600)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
