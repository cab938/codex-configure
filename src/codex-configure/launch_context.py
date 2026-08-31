"""Persistent launch-root metadata and environment construction."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import tomlkit

from .errors import UserFacingError


SCHEMA_VERSION = 1
ROOT_KIND = "codex-configure-launch-root"
LAUNCH_KIND = "codex-configure-launch"
STATE_DIRECTORY = ".codex-configure"
MAX_AUTH_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class LaunchSettings:
    core: str
    provider: str

    @property
    def description(self) -> str:
        if self.core == "dynamic":
            return "Dynamic picker (patched Core)"
        return f"{self.provider} (stock Core)"


@dataclass(frozen=True)
class LaunchContext:
    state_dir: Path
    codex_home: Path
    root: Path | None
    settings: LaunchSettings

    @property
    def launcher(self) -> Path:
        return self.state_dir / "launch.sh"


def local_state(cwd: Path) -> Path:
    return cwd.resolve() / STATE_DIRECTORY


def _read_toml(path: Path, label: str) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserFacingError(f"Could not read {label} at {path}: {exc}") from exc
    try:
        return tomlkit.parse(text)
    except (tomlkit.exceptions.ParseError, TypeError) as exc:
        raise UserFacingError(f"Invalid {label} at {path}: {exc}") from exc


def _read_root_marker(state_dir: Path) -> None:
    path = state_dir / "root.toml"
    if not path.is_file():
        raise UserFacingError(f"Missing launch-root marker: {path}")
    document = _read_toml(path, "launch-root marker")
    if document.get("schema_version") != SCHEMA_VERSION or document.get("kind") != ROOT_KIND:
        raise UserFacingError(f"Unrecognized launch-root marker: {path}")


def load_launch_settings(state_dir: Path) -> LaunchSettings:
    path = state_dir / "launch.toml"
    if not path.is_file():
        raise UserFacingError(f"Missing launch configuration: {path}")
    document = _read_toml(path, "launch configuration")
    if document.get("schema_version") != SCHEMA_VERSION or document.get("kind") != LAUNCH_KIND:
        raise UserFacingError(f"Unrecognized launch configuration: {path}")
    launch = document.get("launch")
    if not isinstance(launch, dict):
        raise UserFacingError(f"Launch configuration has no [launch] table: {path}")
    core = launch.get("core")
    provider = launch.get("provider")
    if core not in {"stock", "dynamic"}:
        raise UserFacingError(f"Launch core must be `stock` or `dynamic`: {path}")
    if not isinstance(provider, str) or not provider:
        raise UserFacingError(f"Launch provider is missing: {path}")
    return LaunchSettings(core=core, provider=provider)


def load_launch_context(state_dir: Path) -> LaunchContext:
    state_dir = state_dir.expanduser().resolve()
    marker = state_dir / "root.toml"
    if marker.exists():
        _read_root_marker(state_dir)
        root = state_dir.parent
        codex_home = state_dir / "codex-home"
    else:
        root = None
        document = _read_toml(state_dir / "launch.toml", "launch configuration")
        context = document.get("context")
        codex_home_value = context.get("codex_home") if isinstance(context, dict) else None
        if not isinstance(codex_home_value, str) or not codex_home_value:
            raise UserFacingError(
                f"Global launch configuration does not declare context.codex_home: {state_dir / 'launch.toml'}"
            )
        codex_home = Path(codex_home_value).expanduser().resolve()
    return LaunchContext(
        state_dir=state_dir,
        codex_home=codex_home,
        root=root,
        settings=load_launch_settings(state_dir),
    )


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise UserFacingError(f"Refusing symlinked managed path: {path}")


def _ensure_directory(path: Path) -> None:
    _refuse_symlink(path)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_managed(path: Path, text: str, mode: int) -> None:
    _refuse_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


def copy_openai_auth(source_home: Path, target_home: Path) -> Path:
    """Copy only Codex-owned OpenAI authentication into a fresh launch root."""

    source = source_home.expanduser().resolve() / "auth.json"
    target_home = target_home.expanduser().resolve()
    destination = target_home / "auth.json"
    if source.is_symlink():
        raise UserFacingError(f"Refusing symlinked OpenAI authentication source: {source}")
    source_descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, flags)
        source_stat = os.fstat(source_descriptor)
    except FileNotFoundError as exc:
        raise UserFacingError(f"OpenAI authentication was not found at {source}.") from exc
    except OSError as exc:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        raise UserFacingError(f"Could not inspect OpenAI authentication at {source}: {exc}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        os.close(source_descriptor)
        raise UserFacingError(f"OpenAI authentication source is not a regular file: {source}")
    try:
        with os.fdopen(source_descriptor, "rb") as source_file:
            source_descriptor = -1
            payload = source_file.read(MAX_AUTH_FILE_BYTES + 1)
    except OSError as exc:
        raise UserFacingError(f"Could not read OpenAI authentication at {source}: {exc}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
    if len(payload) > MAX_AUTH_FILE_BYTES:
        raise UserFacingError(f"OpenAI authentication file is unexpectedly large: {source}")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserFacingError(f"OpenAI authentication is not valid JSON: {source}") from exc
    if not isinstance(document, dict):
        raise UserFacingError(f"OpenAI authentication must contain a JSON object: {source}")

    _ensure_directory(target_home)
    if destination.exists() or destination.is_symlink():
        raise UserFacingError(
            f"OpenAI authentication already exists at {destination}; refusing to overwrite it."
        )

    descriptor, temporary_name = tempfile.mkstemp(prefix=".auth.json.", dir=target_home)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as target_file:
            descriptor = -1
            target_file.write(payload)
            target_file.flush()
            os.fsync(target_file.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise UserFacingError(
                f"OpenAI authentication appeared at {destination}; refusing to overwrite it."
            ) from exc
    except OSError as exc:
        raise UserFacingError(f"Could not copy OpenAI authentication to {destination}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def _launcher_text() -> str:
    return """#!/bin/sh
set -eu
state_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec codex-configure _launch-context "$state_dir" "$@"
"""


def initialize_root(root: Path) -> LaunchContext:
    root = root.resolve()
    state_dir = root / STATE_DIRECTORY
    for directory in (
        state_dir,
        state_dir / "codex-home",
        state_dir / "xdg",
        state_dir / "xdg" / "config",
        state_dir / "xdg" / "data",
        state_dir / "xdg" / "state",
        state_dir / "xdg" / "cache",
        state_dir / "electron-user-data",
        state_dir / "chrome",
        state_dir / "chrome" / "home",
        state_dir / "chrome" / "profile",
    ):
        _ensure_directory(directory)
    _write_managed(state_dir / ".gitignore", "*\n", 0o600)
    marker = state_dir / "root.toml"
    if marker.exists():
        _read_root_marker(state_dir)
    else:
        _write_managed(
            marker,
            f'schema_version = {SCHEMA_VERSION}\nkind = "{ROOT_KIND}"\n',
            0o600,
        )
    manifest = state_dir / "chrome" / "chrome-native-hosts-v2.json"
    if not manifest.exists():
        _write_managed(manifest, '{\n  "schemaVersion": 2,\n  "entries": []\n}\n', 0o600)
    launcher = state_dir / "launch.sh"
    if not launcher.exists():
        _write_managed(launcher, _launcher_text(), 0o700)
    return LaunchContext(
        state_dir=state_dir,
        codex_home=state_dir / "codex-home",
        root=root,
        settings=LaunchSettings("stock", "openai"),
    )


def write_launch_configuration(
    state_dir: Path,
    codex_home: Path,
    settings: LaunchSettings,
    *,
    root: bool,
) -> None:
    state_dir = state_dir.resolve()
    _ensure_directory(state_dir)
    if root:
        _read_root_marker(state_dir)
    document = tomlkit.document()
    document.add("schema_version", SCHEMA_VERSION)
    document.add("kind", LAUNCH_KIND)
    if not root:
        context = tomlkit.table()
        context.add("codex_home", str(codex_home.resolve()))
        document.add("context", context)
    launch = tomlkit.table()
    launch.add("core", settings.core)
    launch.add("provider", settings.provider)
    document.add("launch", launch)
    _write_managed(state_dir / "launch.toml", tomlkit.dumps(document), 0o600)
    _write_managed(state_dir / "launch.sh", _launcher_text(), 0o700)


def rooted_environment(context: LaunchContext, environ: Mapping[str, str]) -> dict[str, str]:
    child = dict(environ)
    child["CODEX_HOME"] = str(context.codex_home)
    if context.root is None:
        return child

    state = context.state_dir
    root_id = hashlib.sha256(str(context.root).encode("utf-8")).hexdigest()[:12]
    uid = os.getuid()
    run_user = Path("/run/user") / str(uid)
    if run_user.is_dir() and os.access(run_user, os.W_OK):
        runtime_root = run_user / "codex-configure" / root_id
    else:
        runtime_root = Path("/tmp") / f"codex-configure-{uid}" / root_id
    override = child.get("CODEX_CONFIGURE_RUNTIME_ROOT", "").strip()
    if override:
        runtime_root = Path(override).expanduser()
        if not runtime_root.is_absolute():
            raise UserFacingError("CODEX_CONFIGURE_RUNTIME_ROOT must be an absolute path.")
    _ensure_directory(runtime_root)
    _ensure_directory(runtime_root / "xdg")
    _ensure_directory(runtime_root / "tmp")
    socket_samples = (
        runtime_root / "tmp" / ".org.chromium.Chromium.XXXXXX" / "SingletonSocket",
        runtime_root / "xdg" / "codex-desktop" / "launch-action.sock",
    )
    if any(len(os.fsencode(str(sample))) >= 108 for sample in socket_samples):
        raise UserFacingError(
            f"Runtime root is too long for application sockets: {runtime_root}"
        )

    values = {
        "CODEX_CONFIGURE_ROOT": context.root,
        "CODEX_CONFIGURE_STATE_ROOT": state,
        # The Linux desktop integration currently recognizes the older names.
        # Keep them as environment aliases while codex-isolated itself retires.
        "CODEX_ISOLATED_ROOT": context.root,
        "CODEX_ISOLATED_STATE_ROOT": state,
        "CODEX_XDG_CONFIG_HOME": state / "xdg" / "config",
        "XDG_CONFIG_HOME": state / "xdg" / "config",
        "XDG_DATA_HOME": state / "xdg" / "data",
        "XDG_STATE_HOME": state / "xdg" / "state",
        "XDG_CACHE_HOME": state / "xdg" / "cache",
        "XDG_RUNTIME_DIR": runtime_root / "xdg",
        "CODEX_ELECTRON_USER_DATA_DIR": state / "electron-user-data",
        "CODEX_CHROME_NATIVE_HOSTS_MANIFEST": state / "chrome" / "chrome-native-hosts-v2.json",
        "CODEX_CONFIGURE_CHROME_HOME": state / "chrome" / "home",
        "CODEX_CONFIGURE_CHROME_USER_DATA_DIR": state / "chrome" / "profile",
        "CODEX_ISOLATED_CHROME_HOME": state / "chrome" / "home",
        "CODEX_ISOLATED_CHROME_USER_DATA_DIR": state / "chrome" / "profile",
        "TMPDIR": runtime_root / "tmp",
    }
    child.update({key: str(value) for key, value in values.items()})
    webview_port = child.get(
        "CODEX_CONFIGURE_WEBVIEW_PORT",
        child.get("CODEX_WEBVIEW_PORT", "5275"),
    )
    try:
        port_number = int(webview_port)
    except ValueError as exc:
        raise UserFacingError("The Codex webview port must be an integer from 1 through 65535.") from exc
    if not 1 <= port_number <= 65535:
        raise UserFacingError("The Codex webview port must be an integer from 1 through 65535.")
    child["CODEX_WEBVIEW_PORT"] = str(port_number)
    child.pop("CODEX_MULTI_LAUNCH", None)
    child.pop("CODEX_LINUX_MULTI_LAUNCH", None)
    child.pop("CODEX_MULTI_LAUNCH_PORT_RANGE", None)
    return child


def launch_chrome(
    context: LaunchContext,
    args: Sequence[str],
    environ: Mapping[str, str],
) -> int:
    if context.root is None:
        raise UserFacingError("The Chrome target is available only in a launch root.")
    override = environ.get("CODEX_CHROME_COMMAND", "").strip()
    command: list[str] | None = None
    if override:
        import shlex

        try:
            command = shlex.split(override)
        except ValueError as exc:
            raise UserFacingError(f"Invalid CODEX_CHROME_COMMAND: {exc}") from exc
    elif sys.platform == "darwin":
        candidate = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if candidate.is_file():
            command = [str(candidate)]
    else:
        for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            resolved = shutil.which(candidate, path=environ.get("PATH"))
            if resolved:
                command = [resolved]
                break
    if not command:
        raise UserFacingError(
            "Could not find Chrome or Chromium; set CODEX_CHROME_COMMAND to its launch command."
        )
    child = rooted_environment(context, environ)
    child["HOME"] = str(context.state_dir / "chrome" / "home")
    profile = context.state_dir / "chrome" / "profile"
    command = [*command, f"--user-data-dir={profile}", *args]
    subprocess.Popen(command, env=child, start_new_session=True)
    return 0
