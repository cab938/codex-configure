"""Provider descriptors, credentials, and model catalog storage.

This module is intentionally independent of the command-line interface.  The
CLI chooses what to ask the user; this module owns the on-disk contract used by
both the launcher and the patched Core:

    $CODEX_HOME/codex-configure/.env
    $CODEX_HOME/codex-configure/providers.d/<id>.toml
    $CODEX_HOME/codex-configure/catalogs/<id>.json

Secrets never occur in a provider descriptor, model catalog, or profile.  A
descriptor contains only the environment variable name Core should resolve.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import tomlkit

from .errors import UserFacingError


SHORTNAME_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
RESERVED_PROVIDER_IDS = frozenset({"openai", "ollama", "lmstudio"})
UMICH_TOOLKIT_DISCOVERY_URL = "https://api.toolkit.umgpt.umich.edu/v1/models"
UMICH_TOOLKIT_RUNTIME_URL = "https://api.portkey.ai/v1"
UMICH_HEADER_NAME = "x-portkey-api-key"


def validate_shortname(shortname: str) -> str:
    """Validate and return a provider shortname suitable for a filename."""

    if not isinstance(shortname, str) or not SHORTNAME_RE.fullmatch(shortname):
        raise UserFacingError(
            "Provider name must be one lowercase word using letters, digits, "
            "hyphens, or underscores (for example, `research-2026`)."
        )
    if shortname in RESERVED_PROVIDER_IDS:
        reserved = ", ".join(sorted(RESERVED_PROVIDER_IDS))
        raise UserFacingError(f"Provider name `{shortname}` is reserved ({reserved}).")
    return shortname


def normalized_env_name(shortname: str) -> str:
    """Return the deterministic private credential variable for ``shortname``."""

    validate_shortname(shortname)
    return shortname.replace("-", "_").upper() + "_API_KEY"


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a small tool-owned file without exposing a partially-written file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(temporary_fd, mode)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(mode)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    name, raw_value = stripped.split("=", 1)
    name = name.strip()
    if name.startswith("export "):
        name = name[7:].strip()
    if not ENV_NAME_RE.fullmatch(name):
        return None
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return name, value


class DotEnvStore:
    """Read and atomically update the tool-owned private ``.env`` file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, str]:
        try:
            mode = self.path.stat().st_mode & 0o777
            if os.name != "nt" and mode & 0o077:
                raise UserFacingError(
                    f"Credential file {self.path} must not be accessible by group or other users."
                )
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise UserFacingError(f"Could not read credential file {self.path}: {exc}") from exc
        values: dict[str, str] = {}
        for line in lines:
            parsed = _parse_dotenv_line(line)
            if parsed is not None:
                values[parsed[0]] = parsed[1]
        return values

    def get(self, name: str) -> str | None:
        return self.read().get(name)

    def update(self, name: str, value: str) -> None:
        if not ENV_NAME_RE.fullmatch(name):
            raise UserFacingError(f"Invalid credential environment variable name `{name}`.")
        if "\n" in value or "\r" in value or "\x00" in value:
            raise UserFacingError("Credential values may not contain newlines or NUL bytes.")
        try:
            old_lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            old_lines = []
        except OSError as exc:
            raise UserFacingError(f"Could not update credential file {self.path}: {exc}") from exc

        replacement = f"{name}={value}"
        replaced = False
        new_lines: list[str] = []
        for line in old_lines:
            parsed = _parse_dotenv_line(line)
            if parsed is not None and parsed[0] == name:
                if not replaced:
                    new_lines.append(replacement)
                    replaced = True
                continue
            new_lines.append(line)
        if not replaced:
            new_lines.append(replacement)
        _atomic_write(self.path, "\n".join(new_lines).rstrip("\n") + "\n")

    def load_environment(
        self,
        environ: Mapping[str, str] | None = None,
        names: set[str] | None = None,
    ) -> dict[str, str]:
        """Return declared credentials, with the process environment first."""

        stored = self.read()
        allowed = set(names) if names is not None else set(stored)
        values = {name: value for name, value in stored.items() if name in allowed}
        if environ is not None:
            for name in allowed:
                explicit = environ.get(name)
                if explicit:
                    values[name] = explicit
        return values


@dataclass(frozen=True)
class ProviderDescriptor:
    """A provider's non-secret runtime settings and catalog reference."""

    id: str
    display_name: str
    base_url: str
    wire_api: str
    env_key: str
    catalog_path: Path
    descriptor_path: Path | None = None
    kind: str = "external"
    header_name: str = UMICH_HEADER_NAME
    requires_openai_auth: bool = False
    stock: bool = False

    @property
    def shortname(self) -> str:
        return self.id

    @property
    def credential_env(self) -> str:
        return self.env_key

    @property
    def api_key_env(self) -> str:
        return self.env_key

    @property
    def model_catalog_json(self) -> Path:
        return self.catalog_path

    @property
    def env_http_headers(self) -> dict[str, str]:
        return {self.header_name: self.env_key}

    def provider_config(self) -> dict[str, Any]:
        """Return the ``[model_providers.<id>]`` mapping for Codex config."""

        return {
            "name": self.display_name,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "requires_openai_auth": self.requires_openai_auth,
            "env_http_headers": dict(self.env_http_headers),
        }

    def to_toml(self) -> str:
        document = tomlkit.document()
        document["schema_version"] = 1
        document["kind"] = self.kind
        # Keep the descriptor shaped like a Core config fragment.  The Core
        # loader can merge this directly, and the provider ID is unambiguous:
        # it is the one key under [model_providers].
        if self.descriptor_path is not None:
            try:
                catalog_reference = os.path.relpath(self.catalog_path, self.descriptor_path.parent)
            except ValueError:
                catalog_reference = str(self.catalog_path)
        else:
            catalog_reference = str(self.catalog_path)
        document["model_catalog_json"] = catalog_reference
        providers = tomlkit.table()
        provider = tomlkit.table()
        provider["name"] = self.display_name
        provider["base_url"] = self.base_url
        provider["wire_api"] = self.wire_api
        provider["requires_openai_auth"] = self.requires_openai_auth
        headers = tomlkit.inline_table()
        headers[self.header_name] = self.env_key
        provider["env_http_headers"] = headers
        providers[self.id] = provider
        document["model_providers"] = providers
        return tomlkit.dumps(document)

    @classmethod
    def from_toml(cls, path: Path) -> "ProviderDescriptor":
        try:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise UserFacingError(f"Provider descriptor does not exist: {path}") from exc
        except (OSError, tomlkit.exceptions.ParseError) as exc:
            raise UserFacingError(f"Invalid provider descriptor {path}: {exc}") from exc

        def required_string(value: Any, name: str) -> str:
            if not isinstance(value, str) or not value:
                raise UserFacingError(f"Provider descriptor {path} is missing `{name}`.")
            return value

        provider_tables = document.get("model_providers")
        if not isinstance(provider_tables, Mapping) or len(provider_tables) != 1:
            raise UserFacingError(
                f"Provider descriptor {path} must contain exactly one `[model_providers.<id>]` table."
            )
        provider_id = next(iter(provider_tables))
        if not isinstance(provider_id, str):
            raise UserFacingError(f"Provider descriptor {path} has an invalid provider id.")
        validate_shortname(provider_id)
        provider_table = provider_tables[provider_id]
        if not isinstance(provider_table, Mapping):
            raise UserFacingError(f"Provider descriptor {path} has an invalid provider table.")
        display_name = required_string(provider_table.get("name"), "name")
        base_url = required_string(provider_table.get("base_url"), "base_url")
        wire_api = required_string(provider_table.get("wire_api"), "wire_api")
        header_values = provider_table.get("env_http_headers")
        if not isinstance(header_values, Mapping) or len(header_values) != 1:
            raise UserFacingError(
                f"Provider descriptor {path} must define exactly one `env_http_headers` entry."
            )
        header_name, env_value = next(iter(header_values.items()))
        if not isinstance(header_name, str) or not isinstance(env_value, str):
            raise UserFacingError(f"Provider descriptor {path} has invalid `env_http_headers`.")
        env_key = env_value
        if not ENV_NAME_RE.fullmatch(env_key):
            raise UserFacingError(f"Provider descriptor {path} has invalid credential environment name.")
        catalog_raw = document.get("model_catalog_json")
        if not isinstance(catalog_raw, str) or not catalog_raw:
            raise UserFacingError(
                f"Provider descriptor {path} must declare `model_catalog_json`."
            )
        catalog_path = Path(catalog_raw).expanduser()
        if not catalog_path.is_absolute():
            catalog_path = (path.parent / catalog_path).resolve()
        kind = document.get("kind", "external")
        if not isinstance(kind, str):
            kind = "external"
        requires_openai_auth = provider_table.get("requires_openai_auth", False)
        if not isinstance(requires_openai_auth, bool):
            raise UserFacingError(f"Provider descriptor {path} has invalid `requires_openai_auth`.")
        return cls(
            id=provider_id,
            display_name=display_name,
            base_url=base_url,
            wire_api=wire_api,
            env_key=env_key,
            catalog_path=catalog_path,
            descriptor_path=path,
            kind=kind,
            header_name=header_name,
            requires_openai_auth=requires_openai_auth,
        )


def stock_openai_descriptor(codex_home: Path) -> ProviderDescriptor:
    """Return the built-in provider entry used in profile-selection UIs."""

    return ProviderDescriptor(
        id="openai",
        display_name="OpenAI",
        base_url="",
        wire_api="responses",
        env_key="",
        catalog_path=codex_home / "models_cache.json",
        kind="stock",
        stock=True,
    )


class ProviderRegistry:
    """Discover, validate, and persist stock/external provider definitions."""

    def __init__(self, root: Path, codex_home: Path | None = None) -> None:
        self.root = root.expanduser().resolve()
        self.codex_home = (codex_home or self.root.parent).expanduser().resolve()
        self.providers_dir = self.root / "providers.d"
        self.catalogs_dir = self.root / "catalogs"
        self.env_store = DotEnvStore(self.root / ".env")

    @property
    def env_path(self) -> Path:
        return self.env_store.path

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.providers_dir.mkdir(parents=True, exist_ok=True)
        self.catalogs_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.root, self.providers_dir, self.catalogs_dir):
            if os.name != "nt":
                directory.chmod(0o700)

    def descriptor_path(self, shortname: str) -> Path:
        validate_shortname(shortname)
        return self.providers_dir / f"{shortname}.toml"

    def catalog_path(self, shortname: str) -> Path:
        validate_shortname(shortname)
        return self.catalogs_dir / f"{shortname}.json"

    def _descriptor_paths(self) -> list[Path]:
        if not self.providers_dir.exists():
            return []
        return sorted(self.providers_dir.glob("*.toml"), key=lambda path: path.name)

    def list_providers(self, include_stock: bool = True) -> tuple[ProviderDescriptor, ...]:
        providers: list[ProviderDescriptor] = []
        if include_stock:
            providers.append(stock_openai_descriptor(self.codex_home))
        for path in self._descriptor_paths():
            descriptor = ProviderDescriptor.from_toml(path)
            if descriptor.id == "openai" or descriptor.stock:
                raise UserFacingError(f"External provider descriptor cannot use reserved id `{descriptor.id}`.")
            providers.append(descriptor)
        return tuple(providers)

    def get(self, shortname: str) -> ProviderDescriptor:
        if shortname == "openai":
            return stock_openai_descriptor(self.codex_home)
        validate_shortname(shortname)
        path = self.descriptor_path(shortname)
        return ProviderDescriptor.from_toml(path)

    def load_catalog(self, provider: ProviderDescriptor | str) -> dict[str, Any]:
        descriptor = self.get(provider) if isinstance(provider, str) else provider
        if descriptor.stock:
            raise UserFacingError("The stock OpenAI provider does not use a tool-owned catalog.")
        try:
            payload = json.loads(descriptor.catalog_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise UserFacingError(
                f"No model catalog detected for provider {descriptor.id}; "
                "use codex-configure to initialize model catalog for provider before use."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise UserFacingError(f"Invalid model catalog for provider {descriptor.id}: {exc}") from exc
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not models:
            raise UserFacingError(f"Model catalog for provider {descriptor.id} has no models.")
        ids: set[str] = set()
        for entry in models:
            if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
                raise UserFacingError(f"Model catalog for provider {descriptor.id} has an invalid model entry.")
            if entry["slug"] in ids:
                raise UserFacingError(f"Model catalog for provider {descriptor.id} contains duplicate model IDs.")
            ids.add(entry["slug"])
        return payload

    def validate_env_collision(self, shortname: str) -> str:
        """Validate a name and ensure its normalized credential key is unused."""

        validate_shortname(shortname)
        env_key = normalized_env_name(shortname)
        for path in self._descriptor_paths():
            try:
                existing = ProviderDescriptor.from_toml(path)
            except UserFacingError:
                continue
            if existing.id != shortname and existing.env_key == env_key:
                raise UserFacingError(
                    f"Provider name `{shortname}` collides with the credential variable "
                    f"`{env_key}` already used by `{existing.id}`."
                )
        return env_key

    def write_umich_provider(
        self,
        shortname: str,
        api_key: str | None,
        catalog: Mapping[str, Any],
        *,
        display_name: str | None = None,
    ) -> ProviderDescriptor:
        """Persist one named U-M Toolkit service and its authoritative catalog."""

        self.ensure_layout()
        env_key = self.validate_env_collision(shortname)
        if api_key is not None and (
            not api_key or "\n" in api_key or "\r" in api_key or "\x00" in api_key
        ):
            raise UserFacingError("A non-empty U-M Toolkit API key is required.")
        models = catalog.get("models") if isinstance(catalog, Mapping) else None
        if not isinstance(models, list) or not models:
            raise UserFacingError("The selected model catalog must contain at least one model.")
        model_ids: set[str] = set()
        for model in models:
            if not isinstance(model, Mapping) or not isinstance(model.get("slug"), str):
                raise UserFacingError("The selected model catalog contains an invalid model entry.")
            if model["slug"] in model_ids:
                raise UserFacingError(
                    f"The selected model catalog contains duplicate model `{model['slug']}`."
                )
            model_ids.add(model["slug"])
        catalog_text = json.dumps(dict(catalog), indent=2, sort_keys=False) + "\n"
        catalog_file = self.catalog_path(shortname)
        descriptor = ProviderDescriptor(
            id=shortname,
            display_name=display_name or f"U-M GPT Toolkit - {shortname}",
            base_url=UMICH_TOOLKIT_RUNTIME_URL,
            wire_api="responses",
            env_key=env_key,
            catalog_path=catalog_file.resolve(),
            descriptor_path=self.descriptor_path(shortname),
            kind="umich-toolkit",
        )

        # Save the catalog before the descriptor.  A descriptor is never
        # published pointing at a file that has not been fully written.
        _atomic_write(catalog_file, catalog_text)
        _atomic_write(descriptor.descriptor_path, descriptor.to_toml())  # type: ignore[arg-type]
        if api_key is not None:
            self.env_store.update(env_key, api_key)
        return descriptor

    def load_credentials(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        names: set[str] = set()
        for path in self._descriptor_paths():
            try:
                names.add(ProviderDescriptor.from_toml(path).env_key)
            except UserFacingError:
                continue
        return self.env_store.load_environment(environ or {}, names)

    def credential_for(self, provider: ProviderDescriptor | str, environ: Mapping[str, str] | None = None) -> str | None:
        descriptor = self.get(provider) if isinstance(provider, str) else provider
        if descriptor.stock or not descriptor.env_key:
            return None
        values = self.load_credentials(environ)
        return values.get(descriptor.env_key)

    def is_initialized(self) -> bool:
        """Return whether a usable provider layout has been installed."""

        if not self.providers_dir.is_dir() or not self.catalogs_dir.is_dir():
            return False
        descriptors = self.list_providers(include_stock=False)
        if not descriptors:
            return False
        for descriptor in descriptors:
            self.load_catalog(descriptor)
        return True


__all__ = [
    "DotEnvStore",
    "ENV_NAME_RE",
    "ProviderDescriptor",
    "ProviderRegistry",
    "RESERVED_PROVIDER_IDS",
    "SHORTNAME_RE",
    "UMICH_HEADER_NAME",
    "UMICH_TOOLKIT_DISCOVERY_URL",
    "UMICH_TOOLKIT_RUNTIME_URL",
    "normalized_env_name",
    "stock_openai_descriptor",
    "validate_shortname",
]
