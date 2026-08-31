from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import tomlkit

from .catalog import ModelChoice
from .errors import UserFacingError
from .providers import ProviderDescriptor, ProviderRegistry, validate_shortname

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
    providers: Path
    catalogs: Path
    env_file: Path
    locks: Path
    recovery: Path
    state: Path
    active_config: Path
    last_good_config: Path
    last_good_state: Path
    pending_config: Path
    pending_state: Path
    transaction: Path

    @classmethod
    def from_home(cls, codex_home: Path) -> "RuntimePaths":
        root = codex_home / "codex-configure"
        return cls(
            codex_home=codex_home,
            root=root,
            base_config=root / "base" / "config.toml",
            original_config=root / "base" / "original-config.toml",
            profiles=root / "profiles",
            providers=root / "providers.d",
            catalogs=root / "catalogs",
            env_file=root / ".env",
            locks=root / "locks",
            recovery=root / "recovery",
            state=root / "state.toml",
            active_config=codex_home / "config.toml",
            last_good_config=root / "recovery" / "last-good-config.toml",
            last_good_state=root / "recovery" / "last-good-state.toml",
            pending_config=root / "recovery" / "pending-previous-config.toml",
            pending_state=root / "recovery" / "pending-previous-state.toml",
            transaction=root / "recovery" / "transaction.json",
        )


@dataclass(frozen=True)
class DoctorCheck:
    status: str
    name: str
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.status != "error" for check in self.checks)


class ConfigManager:
    def __init__(self, codex_home: Path) -> None:
        self.paths = RuntimePaths.from_home(codex_home.resolve())
        self.registry = ProviderRegistry(self.paths.root, self.paths.codex_home)

    @property
    def provider_registry(self) -> ProviderRegistry:
        """The provider/catalog store associated with this Codex home."""

        return self.registry

    def is_initialized(self) -> bool:
        """Whether ``codex-configure init`` has materialized its safe state."""

        if not all(
            path.is_file()
            for path in (
                self.paths.base_config,
                self.paths.original_config,
                self.paths.state,
            )
        ):
            return False
        try:
            providers = self.registry.list_providers(include_stock=False)
            for provider in providers:
                self.registry.load_catalog(provider)
        except UserFacingError:
            return False
        return True

    def require_initialized(self) -> None:
        if not self.is_initialized():
            raise UserFacingError(
                "codex-configure is not initialized for this CODEX_HOME. "
                "Run `codex-configure init` in the launch root first, or pass "
                "`--codex-home` for an explicitly managed home."
            )

    def list_providers(self, include_stock: bool = True) -> tuple[ProviderDescriptor, ...]:
        return self.registry.list_providers(include_stock=include_stock)

    def load_credentials(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        return self.registry.load_credentials(environ)

    def get_provider(self, shortname: str) -> ProviderDescriptor:
        return self.registry.get(shortname)

    def save_umich_provider(
        self,
        shortname: str,
        api_key: str,
        catalog: dict[str, Any],
        *,
        selected_models: list[str] | None = None,
        default_model: str | None = None,
        catalog_source: str | None = None,
        display_name: str | None = None,
        credential_env: str | None = None,
    ) -> ProviderDescriptor:
        """Persist one named Toolkit service and its optional UI profile."""

        self.initialize()
        expected_env = self.registry.validate_env_collision(shortname)
        if credential_env is not None and credential_env != expected_env:
            raise UserFacingError(
                f"Credential variable for `{shortname}` must be {expected_env}."
            )
        model_ids = selected_models or [
            str(entry["slug"])
            for entry in catalog.get("models", [])
            if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
        ]
        if not model_ids:
            raise UserFacingError("The selected model catalog must contain at least one model.")
        catalog_ids = {
            str(entry["slug"])
            for entry in catalog.get("models", [])
            if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
        }
        if any(model_id not in catalog_ids for model_id in model_ids):
            raise UserFacingError("Selected models must all be present in the generated catalog.")
        if default_model is None:
            default_model = model_ids[0]
        if default_model not in model_ids:
            raise UserFacingError("The default model must be one of the selected models.")
        descriptor = self.registry.write_umich_provider(
            shortname,
            None if api_key == "" else api_key,
            catalog,
            display_name=display_name,
        )
        self._write_named_profile(
            descriptor,
            model_ids,
            default_model,
            catalog_source or "U-M Toolkit model endpoint",
        )
        return descriptor

    def initialize(self) -> None:
        codex_home_existed = self.paths.codex_home.exists()
        self.paths.codex_home.mkdir(parents=True, exist_ok=True)
        if not codex_home_existed:
            self.paths.codex_home.chmod(0o700)
        for directory in (
            self.paths.root,
            self.paths.base_config.parent,
            self.paths.profiles,
            self.paths.providers,
            self.paths.catalogs,
            self.paths.locks,
            self.paths.recovery,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        self.registry.ensure_layout()

        with self._activation_lock():
            self._recover_transaction()

            if not self.paths.base_config.exists():
                original = self._read_active_config()
                self._validate_toml(original, self.paths.active_config)
                state_text = self._build_state(original, "original")
                self._atomic_write(self.paths.base_config, original)
                self._atomic_write(self.paths.state, state_text)
                self._write_last_good_pair(original, state_text)
            elif not self.paths.state.exists():
                base = self.paths.base_config.read_text(encoding="utf-8")
                active = self._read_active_config()
                if self._hash_text(base) != self._hash_text(active):
                    raise UserFacingError(
                        f"Runtime state is missing while {self.paths.base_config} exists; "
                        "refusing to guess."
                    )
                state_text = self._build_state(active, "openai")
                self._atomic_write(self.paths.state, state_text)
                self._write_last_good_pair(active, state_text)

            if not self.paths.original_config.exists():
                self._atomic_write(
                    self.paths.original_config,
                    self.paths.base_config.read_text(encoding="utf-8"),
                )

            active = self._read_active_config()
            state_text = self.paths.state.read_text(encoding="utf-8")
            if self._consistent_pair(active, state_text):
                self._write_last_good_pair(active, state_text)

        self._write_openai_profile()

    def doctor(self, credential_path: Path | None = None) -> DoctorReport:
        """Inspect the managed runtime without creating or modifying any files."""
        checks: list[DoctorCheck] = []
        checks.append(
            DoctorCheck(
                "ok" if self.paths.codex_home.exists() else "error",
                "Codex home",
                str(self.paths.codex_home),
            )
        )

        active = self._read_active_config()
        try:
            self._validate_toml(active, self.paths.active_config)
        except UserFacingError as exc:
            checks.append(DoctorCheck("error", "Active config", str(exc)))
        else:
            description = str(self.paths.active_config)
            if not self.paths.active_config.exists():
                description += " (not present; Codex defaults apply)"
            checks.append(DoctorCheck("ok", "Active config", description))

        required = (
            ("Base snapshot", self.paths.base_config),
            ("Original snapshot", self.paths.original_config),
            ("Runtime state", self.paths.state),
            ("Last-known-good config", self.paths.last_good_config),
            ("Last-known-good state", self.paths.last_good_state),
        )
        for name, path in required:
            checks.append(
                DoctorCheck("ok" if path.exists() else "error", name, str(path))
            )

        for name, path in (
            ("Base snapshot TOML", self.paths.base_config),
            ("Original snapshot TOML", self.paths.original_config),
        ):
            if not path.exists():
                continue
            try:
                self._validate_toml(path.read_text(encoding="utf-8"), path)
            except (OSError, UserFacingError) as exc:
                checks.append(DoctorCheck("error", name, str(exc)))
            else:
                checks.append(DoctorCheck("ok", name, "valid"))

        if self.paths.last_good_config.exists() and self.paths.last_good_state.exists():
            try:
                last_config = self.paths.last_good_config.read_text(encoding="utf-8")
                last_state = self.paths.last_good_state.read_text(encoding="utf-8")
                consistent = self._consistent_pair(last_config, last_state)
            except OSError as exc:
                checks.append(DoctorCheck("error", "Recovery snapshot", str(exc)))
            else:
                checks.append(
                    DoctorCheck(
                        "ok" if consistent else "error",
                        "Recovery snapshot",
                        "config and state match" if consistent else "config and state do not match",
                    )
                )

        if self.paths.state.exists():
            try:
                state_text = self.paths.state.read_text(encoding="utf-8")
                state = tomlkit.parse(state_text)
                consistent = self._consistent_pair(active, state_text)
                environment = str(state.get("active_environment", "unknown"))
                detail = f"environment={environment}; active hash "
                detail += "matches" if consistent else "does not match"
                checks.append(
                    DoctorCheck("ok" if consistent else "error", "Active state", detail)
                )
            except (OSError, tomlkit.exceptions.ParseError) as exc:
                checks.append(DoctorCheck("error", "Active state", f"Unreadable state: {exc}"))

        if self.paths.transaction.exists():
            checks.append(
                DoctorCheck(
                    "error",
                    "Pending transaction",
                    f"Recovery is pending at {self.paths.transaction}; run codex-configure restore.",
                )
            )
        else:
            checks.append(DoctorCheck("ok", "Pending transaction", "none"))

        try:
            external_providers = self.registry.list_providers(include_stock=False)
        except UserFacingError as exc:
            checks.append(DoctorCheck("warning", "Provider descriptors", str(exc)))
            external_providers = ()
        for provider in external_providers:
            try:
                self.registry.load_catalog(provider)
            except UserFacingError as exc:
                checks.append(DoctorCheck("warning", f"Provider catalog ({provider.id})", str(exc)))
            else:
                checks.append(
                    DoctorCheck("ok", f"Provider catalog ({provider.id})", str(provider.catalog_path))
                )

        for managed_path in (
            Path("/etc/codex/managed_config.toml"),
            Path("/etc/codex/requirements.toml"),
        ):
            if managed_path.exists():
                checks.append(
                    DoctorCheck(
                        "warning",
                        "Managed configuration",
                        f"{managed_path} is present and may override user configuration",
                    )
                )

        credential = credential_path or self.paths.root / ".env"
        if credential.exists():
            mode = credential.stat().st_mode & 0o777
            status = "ok" if os.name == "nt" or mode & 0o077 == 0 else "error"
            detail = f"{credential} (mode {mode:04o})"
            checks.append(DoctorCheck(status, "U-M credential file", detail))
        else:
            checks.append(
                DoctorCheck(
                    "warning",
                    "U-M credential file",
                    f"not present at {credential} (OpenAI remains usable)",
                )
            )
        return DoctorReport(tuple(checks))

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
            base = tomlkit.parse(self.paths.base_config.read_text(encoding="utf-8"))
            model = base.get("model")
            if isinstance(model, str) and "::" in model:
                provider, unqualified_model = model.split("::", 1)
                if provider == "openai" and unqualified_model:
                    base["model"] = unqualified_model
                else:
                    # Stock Core cannot route an external-qualified value.
                    # Omitting it lets the current stock OpenAI default win.
                    del base["model"]
            self._promote_active_config(tomlkit.dumps(base), "openai")
        return self.paths.profiles / "openai"

    def activate_provider(self, shortname: str, default_model: str | None = None) -> Path:
        """Activate one stock or named provider through the safe switch path."""

        if shortname == "openai":
            return self.activate_openai()
        validate_shortname(shortname)
        self.require_initialized()
        descriptor = self.registry.get(shortname)
        catalog = self.registry.load_catalog(descriptor)
        models = catalog.get("models")
        if not isinstance(models, list) or not models:
            raise UserFacingError(f"Provider {shortname} has no usable model catalog.")
        model_ids = [
            str(entry["slug"])
            for entry in models
            if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
        ]
        if not model_ids:
            raise UserFacingError(f"Provider {shortname} has no usable model catalog.")
        profile_path = self.paths.profiles / shortname / "profile.toml"
        if default_model is None and profile_path.exists():
            try:
                profile = tomlkit.parse(profile_path.read_text(encoding="utf-8"))
                candidate = profile.get("default_model")
                if isinstance(candidate, str):
                    default_model = candidate
            except (OSError, tomlkit.exceptions.ParseError):
                pass
        default_model = default_model or model_ids[0]
        if default_model not in model_ids:
            raise UserFacingError(
                f"Default model `{default_model}` is not present in provider {shortname}'s catalog."
            )
        overlay = self._provider_overlay(descriptor, default_model)
        with self._activation_lock():
            self._reconcile_active_config()
            base = tomlkit.parse(self.paths.base_config.read_text(encoding="utf-8"))
            for key in ("model", "model_provider", "model_catalog_json"):
                base[key] = overlay[key]
            base_providers = base.get("model_providers")
            if base_providers is None:
                base_providers = tomlkit.table()
                base["model_providers"] = base_providers
            if not hasattr(base_providers, "__setitem__"):
                raise UserFacingError("Existing model_providers configuration is not a TOML table.")
            base_providers[shortname] = overlay["model_providers"][shortname]
            candidate = tomlkit.dumps(base)
            self._validate_toml(candidate, self.paths.active_config)
            self._promote_active_config(candidate, shortname)
        return self.paths.profiles / shortname

    def activate_dynamic(self) -> Path:
        """Activate the shared base for the patched Core's dynamic picker."""

        self.require_initialized()
        self.initialize()
        with self._activation_lock():
            self._reconcile_active_config()
            base = tomlkit.parse(self.paths.base_config.read_text(encoding="utf-8"))
            # The patched Core discovers providers.d itself.  Keep active
            # config on the stock/OpenAI route and remove only routing fields
            # codex-configure owns, so dynamic launches cannot stale-pin one
            # external provider or catalog.
            for key in ("model_provider", "model_catalog_json"):
                if key in base:
                    del base[key]
            candidate = tomlkit.dumps(base)
            self._validate_toml(candidate, self.paths.active_config)
            self._promote_active_config(candidate, "openai")
        return self.paths.profiles / "openai"

    def restore_openai(self, original: bool = False) -> Path:
        """Restore the managed OpenAI config, or the immutable first-run snapshot."""
        self.initialize()
        with self._activation_lock():
            self._reconcile_active_config()
            source = self.paths.original_config if original else self.paths.base_config
            text = source.read_text(encoding="utf-8")
            self._validate_toml(text, source)
            self._promote_active_config(text, "openai")
        return source

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
            self._validate_catalog(catalog, selected_ids)
            catalog_text = json.dumps(catalog, indent=2) + "\n"
            catalog_hash = self._hash_text(catalog_text)
            catalog_path = self.paths.catalogs / f"umich-openai-azure-{catalog_hash}.json"
            self._atomic_write(catalog_path, catalog_text)

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

    def _write_named_profile(
        self,
        descriptor: ProviderDescriptor,
        selected_ids: list[str],
        default_model: str,
        catalog_source: str,
    ) -> None:
        profile_path = self.paths.profiles / descriptor.id
        profile_path.mkdir(parents=True, exist_ok=True)
        profile_path.chmod(0o700)
        metadata = tomlkit.document()
        metadata["schema_version"] = 1
        metadata["id"] = descriptor.id
        metadata["display_name"] = descriptor.display_name
        metadata["provider_id"] = descriptor.id
        metadata["catalog_source"] = catalog_source
        metadata["catalog_path"] = str(descriptor.catalog_path)
        metadata["selected_models"] = selected_ids
        metadata["default_model"] = default_model
        self._atomic_write(profile_path / "profile.toml", tomlkit.dumps(metadata))
        self._atomic_write(
            profile_path / "config.toml",
            tomlkit.dumps(self._provider_overlay(descriptor, default_model)),
        )

    def _provider_overlay(self, descriptor: ProviderDescriptor, default_model: str) -> Any:
        overlay = tomlkit.document()
        overlay["model"] = default_model
        overlay["model_provider"] = descriptor.id
        overlay["model_catalog_json"] = str(descriptor.catalog_path)
        providers = tomlkit.table()
        provider = tomlkit.table()
        for key, value in descriptor.provider_config().items():
            if key == "env_http_headers":
                headers = tomlkit.inline_table()
                for header_name, env_key in value.items():
                    headers[header_name] = env_key
                provider[key] = headers
            else:
                provider[key] = value
        providers[descriptor.id] = provider
        overlay["model_providers"] = providers
        return overlay

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
        try:
            state = tomlkit.parse(self.paths.state.read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError) as exc:
            raise UserFacingError(f"Invalid runtime state at {self.paths.state}: {exc}") from exc
        expected = state.get("active_config_sha256")
        environment = state.get("active_environment")
        if not isinstance(expected, str) or not self._known_environment(environment):
            raise UserFacingError(
                f"Runtime state at {self.paths.state} is incomplete; refusing to guess."
            )
        active_text = self._read_active_config()
        self._validate_toml(active_text, self.paths.active_config)
        actual = self._hash_text(active_text)
        if expected != actual:
            current = tomlkit.parse(active_text)
            base = tomlkit.parse(self.paths.base_config.read_text(encoding="utf-8"))

            if environment not in {"original", "openai"}:
                expected_routing = self._expected_profile_routing(environment, str(expected))
                provider_id = PROVIDER_ID if environment == "umich" else environment
                if not self._routing_matches(current, expected_routing, provider_id):
                    raise UserFacingError(
                        f"{self.paths.active_config} changed provider routing outside "
                        "codex-configure; refusing to overwrite it."
                    )
                reconciled = self._restore_base_routing(current, base, provider_id)
            elif environment in {"original", "openai"}:
                reconciled = current

            candidate = tomlkit.dumps(reconciled)
            self._validate_toml(candidate, self.paths.base_config)
            self._atomic_write(self.paths.base_config, candidate)
            state["active_config_sha256"] = actual
            state_text = tomlkit.dumps(state)
            self._atomic_write(self.paths.state, state_text)
            self._write_last_good_pair(active_text, state_text)

    def _expected_umich_routing(self, expected_hash: str) -> Any:
        return self._expected_profile_routing(PROVIDER_ID, expected_hash)

    def _expected_profile_routing(self, provider_id: str, expected_hash: str) -> Any:
        try:
            last_good = self.paths.last_good_config.read_text(encoding="utf-8")
        except FileNotFoundError:
            last_good = ""
        if last_good and self._hash_text(last_good) == expected_hash:
            self._validate_toml(last_good, self.paths.last_good_config)
            return tomlkit.parse(last_good)

        overlay_path = self.paths.profiles / provider_id / "config.toml"
        if not overlay_path.exists():
            raise UserFacingError(
                f"U-M routing state is missing under {self.paths.recovery} and at "
                f"{overlay_path}; refusing to guess."
            )
        try:
            return tomlkit.parse(overlay_path.read_text(encoding="utf-8"))
        except (OSError, tomlkit.exceptions.ParseError) as exc:
            raise UserFacingError(f"Invalid U-M routing state at {overlay_path}: {exc}") from exc

    @staticmethod
    def _routing_matches(current: Any, overlay: Any, provider_id: str = PROVIDER_ID) -> bool:
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
        if provider_id not in current_providers or provider_id not in overlay_providers:
            return False
        return ConfigManager._unwrap(current_providers[provider_id]) == ConfigManager._unwrap(
            overlay_providers[provider_id]
        )

    @staticmethod
    def _restore_base_routing(current: Any, base: Any, provider_id: str = PROVIDER_ID) -> Any:
        reconciled = copy.deepcopy(current)
        for key in ("model", "model_provider", "model_catalog_json"):
            if key in base:
                reconciled[key] = copy.deepcopy(base[key])
            elif key in reconciled:
                del reconciled[key]

        reconciled_providers = reconciled.get("model_providers")
        base_providers = base.get("model_providers")
        base_has_provider = base_providers is not None and provider_id in base_providers
        if base_has_provider:
            if reconciled_providers is None:
                reconciled_providers = tomlkit.table()
                reconciled["model_providers"] = reconciled_providers
            reconciled_providers[provider_id] = copy.deepcopy(base_providers[provider_id])
        elif reconciled_providers is not None and provider_id in reconciled_providers:
            del reconciled_providers[provider_id]
            if len(reconciled_providers) == 0:
                del reconciled["model_providers"]
        return reconciled

    @staticmethod
    def _known_environment(environment: Any) -> bool:
        if environment in {"original", "openai", "umich"}:
            return True
        if not isinstance(environment, str):
            return False
        try:
            validate_shortname(environment)
        except UserFacingError:
            return False
        return True

    @staticmethod
    def _unwrap(value: Any) -> Any:
        unwrap = getattr(value, "unwrap", None)
        return unwrap() if unwrap else value

    def _promote_active_config(self, text: str, environment: str) -> None:
        self._validate_toml(text, self.paths.active_config)
        previous_config, previous_state = self._current_or_last_good_pair()
        state_text = self._build_state(text, environment)

        self._atomic_write(self.paths.pending_config, previous_config)
        self._atomic_write(self.paths.pending_state, previous_state)
        marker = {
            "schema_version": 1,
            "target_environment": environment,
            "target_config_sha256": self._hash_text(text),
        }
        self._atomic_write(self.paths.transaction, json.dumps(marker, indent=2) + "\n")

        self._atomic_write(self.paths.active_config, text)
        self._atomic_write(self.paths.state, state_text)
        self._write_last_good_pair(text, state_text)
        self._remove_file(self.paths.transaction)
        self._remove_file(self.paths.pending_config)
        self._remove_file(self.paths.pending_state)

    def _recover_transaction(self) -> None:
        if not self.paths.transaction.exists():
            if self.paths.state.exists():
                active = self._read_active_config()
                state_text = self.paths.state.read_text(encoding="utf-8")
                if self._consistent_pair(active, state_text):
                    self._remove_file(self.paths.pending_config)
                    self._remove_file(self.paths.pending_state)
            return

        target_hash: str | None = None
        try:
            marker = json.loads(self.paths.transaction.read_text(encoding="utf-8"))
            if isinstance(marker, dict) and isinstance(marker.get("target_config_sha256"), str):
                target_hash = marker["target_config_sha256"]
        except (OSError, json.JSONDecodeError):
            pass

        active = self._read_active_config()
        if self.paths.state.exists():
            state_text = self.paths.state.read_text(encoding="utf-8")
            if (
                target_hash is not None
                and self._hash_text(active) == target_hash
                and self._consistent_pair(active, state_text)
            ):
                self._write_last_good_pair(active, state_text)
                self._clear_transaction()
                return

        try:
            previous_config = self.paths.pending_config.read_text(encoding="utf-8")
            previous_state = self.paths.pending_state.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise UserFacingError(
                f"An incomplete configuration transaction exists at {self.paths.transaction}, "
                "but its recovery snapshot is missing."
            ) from exc
        if not self._consistent_pair(previous_config, previous_state):
            raise UserFacingError(
                f"The recovery snapshot under {self.paths.recovery} is inconsistent; "
                "refusing to guess."
            )
        self._atomic_write(self.paths.active_config, previous_config)
        self._atomic_write(self.paths.state, previous_state)
        self._write_last_good_pair(previous_config, previous_state)
        self._clear_transaction()

    def _clear_transaction(self) -> None:
        self._remove_file(self.paths.transaction)
        self._remove_file(self.paths.pending_config)
        self._remove_file(self.paths.pending_state)

    def _current_or_last_good_pair(self) -> tuple[str, str]:
        active = self._read_active_config()
        try:
            state_text = self.paths.state.read_text(encoding="utf-8")
        except FileNotFoundError:
            state_text = ""
        if self._consistent_pair(active, state_text):
            return active, state_text

        try:
            last_config = self.paths.last_good_config.read_text(encoding="utf-8")
            last_state = self.paths.last_good_state.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise UserFacingError(
                "The active configuration is inconsistent and no last-known-good snapshot exists."
            ) from exc
        if not self._consistent_pair(last_config, last_state):
            raise UserFacingError("The last-known-good configuration snapshot is inconsistent.")
        return last_config, last_state

    def _write_last_good_pair(self, config_text: str, state_text: str) -> None:
        if not self._consistent_pair(config_text, state_text):
            raise UserFacingError("Refusing to save an inconsistent last-known-good snapshot.")
        self._atomic_write(self.paths.last_good_config, config_text)
        self._atomic_write(self.paths.last_good_state, state_text)

    def _consistent_pair(self, config_text: str, state_text: str) -> bool:
        try:
            self._validate_toml(config_text, self.paths.active_config)
            state = tomlkit.parse(state_text)
        except (UserFacingError, tomlkit.exceptions.ParseError):
            return False
        expected = state.get("active_config_sha256")
        environment = state.get("active_environment")
        return (
            isinstance(expected, str)
            and ConfigManager._known_environment(environment)
            and expected == self._hash_text(config_text)
        )

    def _build_state(self, text: str, environment: str) -> str:
        state = tomlkit.document()
        state["schema_version"] = 1
        state["active_environment"] = environment
        state["active_config_sha256"] = self._hash_text(text)
        return tomlkit.dumps(state)

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
            ConfigManager._fsync_directory(path.parent)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        ConfigManager._fsync_directory(path.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
