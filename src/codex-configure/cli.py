"""Command-line setup and launch entry points for codex-configure."""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO

from .catalog import CatalogService, ModelChoice, is_default_model_slug
from .errors import UserFacingError
from .init_tui import InitOutcome, run_fullscreen_init, terminal_available
from .launch_context import (
    CORE_PROJECT_ROOT_MARKER,
    LaunchContext,
    LaunchSettings,
    chrome_extension_installed,
    chrome_native_host_registered,
    copy_openai_auth,
    initialize_root,
    launch_chrome,
    load_launch_context,
    local_state,
    rooted_environment,
    sync_chrome_native_host_manifest,
    write_launch_configuration,
)
from .providers import ProviderDescriptor, validate_shortname
from .runtime import ConfigManager

try:
    import curses
except ImportError:  # pragma: no cover - standard on supported POSIX hosts
    _CURSES_FAILURES: tuple[type[BaseException], ...] = (OSError, RuntimeError)
else:
    _CURSES_FAILURES = (OSError, RuntimeError, curses.error)


TOOLKIT_URL = "https://toolkit.umgpt.umich.edu/"
INIT_CANCELLED = 3


class Console:
    """Small injectable console used by commands and non-TTY tests."""

    def __init__(self, input_stream: TextIO, output_stream: TextIO) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream

    def write(self, text: str = "") -> None:
        print(text, file=self.output_stream)

    def ask(self, prompt: str) -> str:
        self.output_stream.write(prompt)
        self.output_stream.flush()
        value = self.input_stream.readline()
        if value == "":
            raise UserFacingError("Input ended before a selection was made.")
        return value.strip()

    def ask_secret(self, prompt: str) -> str:
        """Read a key without echoing it, with a testable pipe fallback."""
        is_tty = bool(getattr(self.input_stream, "isatty", lambda: False)())
        if self.input_stream is sys.stdin and is_tty:
            try:
                return getpass.getpass(prompt, stream=self.output_stream).strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise UserFacingError("Input ended before a key was entered.") from exc
        return self.ask(prompt)

    def choose(self, title: str, options: Sequence[str], default: int = 1) -> int:
        if not options:
            raise UserFacingError("No choices are available.")
        self.write(title)
        self.write()
        for index, option in enumerate(options, start=1):
            self.write(f"  {index}. {option}")
        self.write()
        while True:
            raw = self.ask(f"> [{default}] ")
            if not raw:
                return default
            try:
                selected = int(raw)
            except ValueError:
                selected = 0
            if 1 <= selected <= len(options):
                return selected
            self.write(f"Enter a number from 1 to {len(options)}.")

    def checkbox(
        self,
        title: str,
        options: Sequence[str],
        defaults: Sequence[int] = (),
        disabled: Sequence[int] = (),
    ) -> list[int]:
        """Select models with an ncurses view when possible, text otherwise."""
        if not options:
            raise UserFacingError("The provider returned no models.")
        checked = {index for index in defaults if 1 <= index <= len(options)}
        blocked = {index for index in disabled if 1 <= index <= len(options)}
        checked -= blocked
        is_tty = bool(getattr(self.input_stream, "isatty", lambda: False)()) and bool(
            getattr(self.output_stream, "isatty", lambda: False)()
        )
        if is_tty and self.input_stream is sys.stdin and self.output_stream is sys.stdout:
            try:
                return self._curses_checkbox(title, options, checked, blocked)
            except (OSError, RuntimeError, UserFacingError):
                pass

        self.write(title)
        self.write()
        for index, option in enumerate(options, start=1):
            if index in blocked:
                self.write(f"  [-] {index}. {option} (unsupported by this Codex build)")
            else:
                marker = "x" if index in checked else " "
                self.write(f"  [{marker}] {index}. {option}")
        self.write()
        self.write('Enter model numbers or ranges, "all", or press Enter to keep the checked models.')
        while True:
            raw = self.ask("> ")
            try:
                if raw.strip().casefold() == "all":
                    selected = sorted(set(range(1, len(options) + 1)) - blocked)
                else:
                    selected = parse_model_selection(raw, len(options), sorted(checked))
                if not selected or any(index in blocked for index in selected):
                    raise ValueError("unsupported model")
                return selected
            except ValueError:
                self.write(
                    f'Choose supported model numbers from 1 to {len(options)}, ranges such as "1-3", or "all".'
                )

    def _curses_checkbox(
        self,
        title: str,
        options: Sequence[str],
        checked: set[int],
        blocked: set[int],
    ) -> list[int]:
        import curses

        selected = set(checked)
        cursor = next((index for index in range(1, len(options) + 1) if index not in blocked), 1)

        def render(window: Any) -> None:
            window.erase()
            window.addstr(0, 0, title)
            window.addstr(1, 0, "Use arrows, Space to toggle, Enter to accept, q to cancel")
            height, width = window.getmaxyx()
            width = max(1, width - 1)
            # Keep long endpoint lists in a bounded viewport.  The cursor is
            # always visible and unsupported rows remain visibly disabled.
            visible = max(1, height - 4)
            first = max(1, min(cursor - visible // 2, len(options) - visible + 1))
            last = min(len(options), first + visible - 1)
            for row, index in enumerate(range(first, last + 1), start=3):
                option = options[index - 1]
                if index in blocked:
                    marker, suffix, attribute = "-", " (unsupported by this Codex build)", curses.A_DIM
                else:
                    marker, suffix, attribute = ("x" if index in selected else " "), "", curses.A_NORMAL
                if index == cursor:
                    attribute |= curses.A_REVERSE
                window.addnstr(row, 0, f"[{marker}] {index}. {option}{suffix}", width, attribute)
            window.refresh()

        def loop(window: Any) -> list[int]:
            nonlocal cursor
            curses.curs_set(0)
            while True:
                render(window)
                key = window.getch()
                if key in (ord("q"), ord("Q"), 27):
                    raise UserFacingError("Model selection cancelled.")
                if key in (curses.KEY_DOWN, ord("j")):
                    cursor = min(len(options), cursor + 1)
                    while cursor in blocked and cursor < len(options):
                        cursor += 1
                elif key in (curses.KEY_UP, ord("k")):
                    cursor = max(1, cursor - 1)
                    while cursor in blocked and cursor > 1:
                        cursor -= 1
                elif key == ord(" ") and cursor not in blocked:
                    if cursor in selected:
                        selected.remove(cursor)
                    else:
                        selected.add(cursor)
                elif key in (curses.KEY_ENTER, 10, 13) and selected:
                    return sorted(selected)

        return curses.wrapper(loop)


def _fullscreen_console(console: Console) -> bool:
    return bool(
        console.input_stream is sys.stdin
        and console.output_stream is sys.stdout
        and terminal_available(console.input_stream, console.output_stream)
    )


@dataclass(frozen=True)
class _ClientProcess:
    name: str
    pid: str | None


class Launcher:
    """Resolve and launch stock Codex CLI/Desktop clients."""

    def __init__(self, environ: Mapping[str, str] | None = None, proc_root: Path = Path("/proc")) -> None:
        self.environ = dict(environ or os.environ)
        self.proc_root = proc_root

    def validate(self, target: str, requires_environment: bool = False) -> list[str]:
        if target == "cli":
            command = shutil.which("codex", path=self.environ.get("PATH"))
            if not command:
                raise UserFacingError("Could not find the Codex CLI on PATH.")
            return [command]
        override = self.environ.get("CODEX_DESKTOP_COMMAND")
        if override:
            try:
                command = shlex.split(override)
            except ValueError as exc:
                raise UserFacingError(f"Invalid CODEX_DESKTOP_COMMAND: {exc}") from exc
            if not command:
                raise UserFacingError("CODEX_DESKTOP_COMMAND is empty.")
            if Path(command[0]).name.lower() == "open" and requires_environment:
                raise UserFacingError(
                    "CODEX_DESKTOP_COMMAND uses macOS open, which cannot reliably pass the "
                    "credential. Point it at the ChatGPT app executable instead."
                )
            return command
        if sys.platform == "darwin":
            home = Path(self.environ.get("HOME", str(Path.home())))
            for app_binary in (
                Path("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
                home / "Applications" / "ChatGPT.app" / "Contents" / "MacOS" / "ChatGPT",
            ):
                if app_binary.is_file():
                    return [str(app_binary)]
            command = shutil.which("open", path=self.environ.get("PATH"))
            if command:
                if requires_environment:
                    raise UserFacingError(
                        "Found ChatGPT only through macOS open, which cannot reliably pass the "
                        "credential. Set CODEX_DESKTOP_COMMAND to the ChatGPT app executable."
                    )
                return [command, "-n", "-a", "ChatGPT"]
        for candidate in ("chatgpt", "codex-desktop", "codex-app"):
            command = shutil.which(candidate, path=self.environ.get("PATH"))
            if command:
                return [command]
        raise UserFacingError(
            "Could not find Codex Desktop; set CODEX_DESKTOP_COMMAND to its launch command."
        )

    def _running_client_processes(self) -> list[_ClientProcess]:
        pgrep = shutil.which("pgrep", path=self.environ.get("PATH"))
        if not pgrep:
            raise UserFacingError("Could not verify clients are stopped because pgrep is unavailable.")
        ps = shutil.which("ps", path=self.environ.get("PATH"))
        if not ps:
            raise UserFacingError("Could not verify clients are stopped because ps is unavailable.")
        names = (
            ("ChatGPT", "chatgpt", "Codex", "codex")
            if sys.platform == "darwin"
            else ("ChatGPT", "chatgpt", "codex-desktop", "codex-app", "codex", "codex-cli")
        )
        running: list[_ClientProcess] = []
        for name in names:
            result = subprocess.run(
                [pgrep, "-x", name], check=False, capture_output=True, text=True, env=self.environ
            )
            if result.returncode == 0:
                values = result.stdout.split()
                pids = [value for value in values if value.isdigit()]
                # Electron can leave reparented zombie entries behind. They
                # have no runnable client and must not permanently block a
                # profile switch. Anything other than a confirmed zombie is
                # treated conservatively as a live client.
                if not pids or len(pids) != len(values):
                    running.append(_ClientProcess(name=name, pid=None))
                    continue
                for pid in pids:
                    state = subprocess.run(
                        [ps, "-o", "stat=", "-p", pid],
                        check=False,
                        capture_output=True,
                        text=True,
                        env=self.environ,
                    )
                    if state.returncode == 1 and not state.stdout.strip():
                        continue  # The process exited between pgrep and ps.
                    if state.returncode != 0:
                        detail = state.stderr.strip() or f"exit status {state.returncode}"
                        raise UserFacingError(f"Could not inspect client process {pid} with ps: {detail}")
                    process_states = state.stdout.split()
                    if not process_states or any(
                        not process_state.upper().startswith("Z")
                        for process_state in process_states
                    ):
                        running.append(_ClientProcess(name=name, pid=pid))
            elif result.returncode != 1:
                detail = result.stderr.strip() or f"exit status {result.returncode}"
                raise UserFacingError(f"Could not inspect running clients with pgrep: {detail}")
        return running

    def running_clients(self) -> list[str]:
        """Return live client names for broad lifecycle diagnostics."""

        return list(dict.fromkeys(process.name for process in self._running_client_processes()))

    @staticmethod
    def _resolved_environment_path(
        environment: Mapping[str, str], key: str
    ) -> tuple[Path | None, str | None]:
        value = environment.get(key)
        if not value:
            return None, None
        candidate = Path(value)
        if not candidate.is_absolute():
            return None, f"{key} is not an absolute path"
        try:
            return candidate.resolve(), None
        except OSError as exc:
            return None, f"could not resolve {key}: {exc}"

    def _linux_conflict_boundary(
        self,
        process: _ClientProcess,
        target_codex_home: Path,
        target_root: Path | None,
    ) -> str | None:
        """Return a conflicting boundary, or None when this client is isolated."""

        if process.pid is None:
            return "its process identifier could not be read"
        try:
            raw_environment = (self.proc_root / process.pid / "environ").read_bytes()
        except FileNotFoundError:
            return None  # The process exited after its liveness check.
        except OSError as exc:
            detail = exc.strerror or str(exc)
            return f"its environment could not be read safely ({detail})"

        environment: dict[str, str] = {}
        relevant_keys = {
            "CODEX_HOME",
            "CODEX_CONFIGURE_ROOT",
            "CODEX_ISOLATED_ROOT",
            "CODEX_CONFIGURE_STATE_ROOT",
            "CODEX_ISOLATED_STATE_ROOT",
        }
        for item in raw_environment.split(b"\0"):
            if not item or b"=" not in item:
                continue
            raw_key, raw_value = item.split(b"=", 1)
            try:
                key = raw_key.decode("ascii")
            except UnicodeDecodeError:
                continue
            if key not in relevant_keys:
                continue
            try:
                value = raw_value.decode("utf-8")
            except UnicodeDecodeError:
                return f"{key} could not be decoded safely"
            previous = environment.get(key)
            if previous is not None and previous != value:
                return f"{key} has conflicting values"
            environment[key] = value

        paths: dict[str, Path] = {}
        for key in relevant_keys:
            resolved, problem = self._resolved_environment_path(environment, key)
            if problem is not None:
                return problem
            if resolved is not None:
                paths[key] = resolved

        if paths.get("CODEX_HOME") == target_codex_home:
            return f"CODEX_HOME {target_codex_home}"
        if target_root is None:
            return None
        if any(
            paths.get(key) == target_root
            for key in ("CODEX_CONFIGURE_ROOT", "CODEX_ISOLATED_ROOT")
        ):
            return f"launch root {target_root}"
        target_state = target_root / ".codex-configure"
        if any(
            paths.get(key) == target_state
            for key in ("CODEX_CONFIGURE_STATE_ROOT", "CODEX_ISOLATED_STATE_ROOT")
        ):
            return f"launch state {target_state}"
        # No codex-configure variables means the normal/global profile. Any
        # different recognized home or root has its own mutable state.
        return None

    def ensure_clients_stopped(
        self,
        target_codex_home: Path | None = None,
        *,
        target_root: Path | None = None,
    ) -> None:
        running = self._running_client_processes()
        if (
            running
            and sys.platform == "linux"
            and target_codex_home is not None
        ):
            target_codex_home = target_codex_home.expanduser().resolve()
            target_root = target_root.expanduser().resolve() if target_root is not None else None
            conflicts = [
                (process, boundary)
                for process in running
                if (boundary := self._linux_conflict_boundary(process, target_codex_home, target_root))
                is not None
            ]
            if not conflicts:
                return
            detail = "; ".join(
                f"{process.name}"
                f"{f' (pid {process.pid})' if process.pid is not None else ''}: {boundary}"
                for process, boundary in conflicts
            )
            raise UserFacingError(
                f"Cannot switch the target CODEX_HOME {target_codex_home}: a running Codex or "
                f"ChatGPT client conflicts with that state boundary ({detail}). Close it before "
                "switching environments."
            )

        names = list(dict.fromkeys(process.name for process in running))
        if names:
            raise UserFacingError(
                f"Codex or ChatGPT is running ({', '.join(names)}). Close it before switching environments."
            )

    def launch(
        self,
        command: list[str],
        extra_environment: Mapping[str, str],
        remove_environment: Iterable[str] = (),
    ) -> int:
        child_env = self.environ.copy()
        for key in remove_environment:
            child_env.pop(key, None)
        child_env.update(extra_environment)
        executable = Path(command[0]).name.lower()
        if executable == "open" and extra_environment:
            raise UserFacingError(
                "The macOS open fallback cannot reliably pass a credential. "
                "Set CODEX_DESKTOP_COMMAND to the ChatGPT app executable."
            )
        if executable in {"chatgpt", "codex-desktop", "codex-app", "open"}:
            subprocess.Popen(command, env=child_env, start_new_session=True)
            return 0
        return subprocess.call(command, env=child_env)


def parse_model_selection(raw: str, count: int, defaults: list[int]) -> list[int]:
    value = raw.strip().lower()
    if not value:
        return defaults
    if value == "all":
        return list(range(1, count + 1))
    selected: set[int] = set()
    for part in value.replace(" ", ",").split(","):
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            if len(bounds) != 2 or not all(bound.isdigit() for bound in bounds):
                raise ValueError(part)
            start, end = (int(bound) for bound in bounds)
            if start > end:
                raise ValueError(part)
            selected.update(range(start, end + 1))
        elif part.isdigit():
            selected.add(int(part))
        else:
            raise ValueError(part)
    if not selected or min(selected) < 1 or max(selected) > count:
        raise ValueError(raw)
    return sorted(selected)


def _supported(model: ModelChoice) -> bool:
    return bool(getattr(model, "selectable", True)) and model.status.casefold() not in {
        "unknown",
        "unsupported",
        "disabled",
        "unavailable",
        "unrecognized",
    }


def _select_models(
    console: Console,
    models: Sequence[ModelChoice],
    previous_ids: Sequence[str] = (),
) -> list[ModelChoice]:
    supported = [index for index, model in enumerate(models, start=1) if _supported(model)]
    if not supported:
        raise UserFacingError("No models from this provider are supported by this Codex build.")
    previous = set(previous_ids)
    defaults = [index for index, model in enumerate(models, start=1) if model.slug in previous]
    defaults = [index for index in defaults if index in supported]
    if not defaults:
        defaults = [
            index
            for index, model in enumerate(models, start=1)
            if index in supported and is_default_model_slug(model.slug)
        ]
    labels = [_model_label(model) for model in models]
    indexes = console.checkbox(
        "Choose models to make available in Codex",
        labels,
        defaults=defaults,
        disabled=[index for index in range(1, len(models) + 1) if index not in supported],
    )
    return [models[index - 1] for index in indexes]


def _choose_default(console: Console, selected: Sequence[ModelChoice], prior: str | None = None) -> str:
    if not selected:
        raise UserFacingError("Select at least one supported model.")
    default = next((index for index, model in enumerate(selected, start=1) if model.slug == prior), 0)
    if not default:
        default = next((index for index, model in enumerate(selected, start=1) if is_default_model_slug(model.slug)), 1)
    choice = console.choose(
        "Choose the default model",
        [_model_label(model) for model in selected],
        default=default,
    )
    return selected[choice - 1].slug


def _model_label(model: ModelChoice) -> str:
    label = (
        f"{model.display_name} ({model.slug})"
        if model.display_name != model.slug
        else model.slug
    )
    if model.status in {"tested", "known", "unverified"}:
        details = "; ".join((model.status, *model.badges))
        return f"{label} [{details}]"
    return label


def _profile_metadata(manager: ConfigManager, descriptor: ProviderDescriptor) -> tuple[tuple[str, ...], str | None]:
    path = manager.paths.profiles / descriptor.id / "profile.toml"
    if not path.exists():
        return (), None
    try:
        import tomlkit

        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError):
        return (), None
    selected = document.get("selected_models", [])
    default = document.get("default_model")
    return (
        tuple(str(value) for value in selected) if isinstance(selected, list) else (),
        str(default) if isinstance(default, str) else None,
    )


def _profiles(manager: ConfigManager) -> tuple[ProviderDescriptor, ...]:
    return tuple(manager.list_providers(include_stock=False))


def _provider_kind_label(descriptor: ProviderDescriptor) -> str:
    if descriptor.kind == "umich-toolkit":
        return "U-M GPT Toolkit"
    if descriptor.kind == "local-responses":
        return "local Responses endpoint"
    return descriptor.display_name


def _credential_values(
    manager: ConfigManager,
    environ: Mapping[str, str],
    provider: str | None = None,
) -> dict[str, str]:
    descriptors = _profiles(manager)
    if provider:
        descriptors = tuple(item for item in descriptors if item.id == provider)
    values = manager.load_credentials(environ)
    return {
        item.env_key: values[item.env_key]
        for item in descriptors
        if item.env_key and values.get(item.env_key)
    }


def _dynamic_credentials(
    manager: ConfigManager,
    environ: Mapping[str, str],
) -> dict[str, str]:
    descriptors = _profiles(manager)
    values = manager.load_credentials(environ)
    missing = [item.id for item in descriptors if item.env_key and not values.get(item.env_key)]
    if missing:
        raise UserFacingError(
            "Missing credentials for configured provider(s): "
            + ", ".join(missing)
            + ". Run `codex-configure init` to initialize each provider."
        )
    return {
        item.env_key: values[item.env_key]
        for item in descriptors
        if item.env_key
    }


def _detected_openai_auth_home(
    source_home: Path,
    target_home: Path,
    environ: Mapping[str, str],
) -> Path | None:
    """Return an authenticated source home without inspecting token contents."""

    source_home = source_home.expanduser().resolve()
    target_home = target_home.expanduser().resolve()
    auth_file = source_home / "auth.json"
    if source_home == target_home or auth_file.is_symlink() or not auth_file.is_file():
        return None
    codex = shutil.which("codex", path=environ.get("PATH"))
    if not codex:
        return None
    child_environment = dict(environ)
    child_environment["CODEX_HOME"] = str(source_home)
    try:
        result = subprocess.run(
            [codex, "login", "status"],
            check=False,
            capture_output=True,
            text=True,
            env=child_environment,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return source_home if result.returncode == 0 else None


def _write_provider_status(manager: ConfigManager, console: Console) -> None:
    descriptors = _profiles(manager)
    auth = manager.paths.codex_home / "auth.json"
    console.write("Profiles configured in this launch root:")
    console.write(
        "  - openai (stock): authentication present"
        if auth.is_file() and not auth.is_symlink()
        else "  - openai (stock): sign-in required"
    )
    if descriptors:
        for descriptor in descriptors:
            selected, _ = _profile_metadata(manager, descriptor)
            noun = "model" if len(selected) == 1 else "models"
            console.write(
                f"  - {descriptor.id} ({_provider_kind_label(descriptor)}; {len(selected)} {noun})"
            )
    else:
        console.write("  - external providers: none")
    console.write()


def _configure_umich_provider(
    manager: ConfigManager,
    console: Console,
    environ: Mapping[str, str],
    catalog_service: CatalogService | None,
    descriptor: ProviderDescriptor | None,
) -> None:
    descriptors = _profiles(manager)
    if descriptor is None:
        shortname = validate_shortname(console.ask("One-word profile name: ").strip())
        if shortname in {item.id for item in descriptors}:
            raise UserFacingError(f"A profile named `{shortname}` already exists.")
        manager.provider_registry.validate_env_collision(shortname)
        console.write(f"Get a key at {TOOLKIT_URL}")
        api_key = console.ask_secret(f"Paste the key for {shortname}: ")
        if not api_key:
            raise UserFacingError("An API key is required to create the profile.")
    else:
        shortname = descriptor.id
        env_key = descriptor.env_key
        api_key = manager.load_credentials(environ).get(env_key, "")
        if not api_key:
            console.write(f"Get a key at {TOOLKIT_URL}")
            api_key = console.ask_secret(f"Paste the key for {shortname}: ")
        if not api_key:
            raise UserFacingError(f"No API key found for `{shortname}` ({env_key}).")

    service = catalog_service or CatalogService(manager.paths.codex_home)
    result = service.discover(api_key=api_key)
    models = tuple(result.models)
    prior_ids, prior_default = (), None
    if descriptor is not None:
        prior_ids, prior_default = _profile_metadata(manager, descriptor)
    selected = _select_models(console, models, prior_ids)
    default_model = _choose_default(console, selected, prior_default)
    catalog = service.build_selected_catalog(selected)
    manager.save_umich_provider(
        shortname,
        api_key,
        catalog,
        selected_models=[model.slug for model in selected],
        default_model=default_model,
        catalog_source=getattr(result, "source", "U-M Toolkit model endpoint"),
    )
    console.write()
    console.write(f"Profile `{shortname}` is ready with {len(selected)} model(s).")
    console.write()


def _configure_local_provider(
    manager: ConfigManager,
    console: Console,
    environ: Mapping[str, str],
    catalog_service: CatalogService | None,
    descriptor: ProviderDescriptor | None,
) -> None:
    descriptors = _profiles(manager)
    if descriptor is None:
        shortname = validate_shortname(console.ask("One-word profile name: ").strip())
        if shortname in {item.id for item in descriptors}:
            raise UserFacingError(f"A profile named `{shortname}` already exists.")
        default_base_url = "http://127.0.0.1:1337/v1"
        prior_ids, prior_default = (), None
        existing_key = ""
    else:
        shortname = descriptor.id
        default_base_url = descriptor.base_url
        prior_ids, prior_default = _profile_metadata(manager, descriptor)
        existing_key = (
            manager.load_credentials(environ).get(descriptor.env_key, "")
            if descriptor.env_key
            else ""
        )

    base_url = console.ask(f"Responses API base URL [{default_base_url}]: ") or default_base_url
    entered_key = console.ask_secret(
        "Bearer API key (optional; press Enter to reuse the stored key or use no key): "
    )
    api_key = entered_key or existing_key or None
    service = catalog_service or CatalogService(manager.paths.codex_home)
    result = service.discover_local(base_url, api_key=api_key)
    if result.warning:
        console.write(f"Warning: {result.warning}")
        console.write()
    models = tuple(result.models)
    selected = _select_models(console, models, prior_ids)
    default_model = _choose_default(console, selected, prior_default)
    catalog = service.build_local_catalog(selected)
    manager.save_local_provider(
        shortname,
        base_url,
        api_key,
        catalog,
        selected_models=[model.slug for model in selected],
        default_model=default_model,
        catalog_source=getattr(result, "source", f"{base_url.rstrip('/')}/models"),
        known_catalog=getattr(result, "known_catalog", None),
    )
    console.write()
    console.write(f"Profile `{shortname}` is ready with {len(selected)} local model(s).")
    console.write()


def run_init(
    codex_home: Path,
    console: Console,
    environ: Mapping[str, str],
    manager: ConfigManager | None = None,
    catalog_service: CatalogService | None = None,
    auth_source_home: Path | None = None,
) -> int:
    manager = manager or ConfigManager(codex_home)
    manager.initialize()
    if _fullscreen_console(console):
        detected_auth_home = (
            _detected_openai_auth_home(auth_source_home, manager.paths.codex_home, environ)
            if auth_source_home is not None
            else None
        )
        try:
            tui_result = run_fullscreen_init(
                manager,
                environ,
                catalog_service,
                auth_source_home=detected_auth_home,
            )
        except _CURSES_FAILURES:
            # Preserve the accessible text flow on terminals where curses is
            # unavailable or cannot take control of the current display.
            console.write("Full-screen init is unavailable here; using the text interface.")
        else:
            if tui_result.outcome == InitOutcome.CANCELLED:
                console.write("Cancelled. Proposed profile changes were discarded.")
                return INIT_CANCELLED
            console.write("Saved proposed profile changes.")
            return 0
    return _run_init_text(
        manager,
        console,
        environ,
        catalog_service=catalog_service,
        auth_source_home=auth_source_home,
    )


def _run_init_text(
    manager: ConfigManager,
    console: Console,
    environ: Mapping[str, str],
    *,
    catalog_service: CatalogService | None,
    auth_source_home: Path | None,
) -> int:
    detected_auth_home = (
        _detected_openai_auth_home(auth_source_home, manager.paths.codex_home, environ)
        if auth_source_home is not None
        else None
    )

    while True:
        descriptors = _profiles(manager)
        _write_provider_status(manager, console)
        actions: list[tuple[str, str, ProviderDescriptor | None]] = [
            ("openai", "OpenAI (stock)", None)
        ]
        target_auth = manager.paths.codex_home / "auth.json"
        if detected_auth_home is not None and not target_auth.exists() and not target_auth.is_symlink():
            actions.append(
                (
                    "copy-auth",
                    f"OpenAI (detected: {detected_auth_home} -> copy auth)",
                    None,
                )
            )
        actions.extend(
            (
                "reconfigure",
                f"Reconfigure {_provider_kind_label(descriptor)} profile `{descriptor.id}`",
                descriptor,
            )
            for descriptor in descriptors
        )
        actions.append(("new-umich", "New U-M GPT Toolkit Service", None))
        actions.append(("new-local", "New local Responses endpoint", None))
        actions.append(("done", "Done configuring providers", None))
        chosen = console.choose(
            "Choose a model provider profile to initialize:",
            [label for _, label, _ in actions],
        )
        action, _, descriptor = actions[chosen - 1]
        if action == "done":
            return 0
        if action == "openai":
            if target_auth.is_file() and not target_auth.is_symlink():
                console.write("OpenAI is ready with authentication already present.")
            else:
                console.write("OpenAI is ready. Sign in when first launching this root.")
            console.write()
            continue
        if action == "copy-auth":
            assert detected_auth_home is not None
            destination = copy_openai_auth(detected_auth_home, manager.paths.codex_home)
            console.write(f"Copied only OpenAI authentication to {destination}.")
            console.write()
            continue
        if action == "new-local" or (
            action == "reconfigure" and descriptor is not None and descriptor.kind == "local-responses"
        ):
            _configure_local_provider(
                manager,
                console,
                environ,
                catalog_service,
                descriptor if action == "reconfigure" else None,
            )
        else:
            _configure_umich_provider(
                manager,
                console,
                environ,
                catalog_service,
                descriptor if action == "reconfigure" else None,
            )


def _run_target(target: str) -> tuple[str | None, str]:
    provider, separator, app = target.partition("/")
    if not separator:
        provider, app = None, provider
    if app not in {"cli", "desktop"}:
        raise UserFacingError("Launch target must be `cli` or `desktop`.")
    if provider == "":
        raise UserFacingError("Run target must be `provider/app`, for example `openai/cli`.")
    return provider, app


def _patched_binary(environ: Mapping[str, str], core_home: Path | None) -> str:
    value = environ.get("CODEX_CLI_PATH", "").strip()
    if value:
        binary = Path(value).expanduser()
        missing_message = f"CODEX_CLI_PATH is not an executable file: {binary}"
    else:
        if core_home is None:
            raise UserFacingError(
                "Dynamic Core is project-local. Run this command from an initialized "
                "launch root or set CODEX_CLI_PATH to an explicit custom build."
            )
        try:
            from .core_install import CoreInstaller
            from .patcher import CodexPatcher
        except ImportError as exc:
            raise UserFacingError(
                "The Dynamic Core backends are not installed in this codex-configure build."
            ) from exc
        core_home = core_home.expanduser().resolve()
        installer = CoreInstaller(home=core_home)
        installed = CoreInstaller.current_binary(core_home)
        installed_matches_package = False
        if installed.is_file() and os.access(installed, os.X_OK):
            try:
                installed_matches_package = (
                    installed.resolve().parent
                    == CoreInstaller.versioned_directory(
                        core_home,
                        target=installer.target,
                    ).resolve()
                )
            except (OSError, RuntimeError):
                pass
        candidates = (
            *((installed,) if installed_matches_package else ()),
            CodexPatcher.default_binary(core_home),
        )
        binary = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_file() and os.access(candidate, os.X_OK)
            ),
            candidates[0],
        )
        missing_message = (
            "Dynamic Core is not installed in this launch root. Run "
            "`codex-configure setup dynamic` from the root, "
            "use `codex-configure patch` for a source build, or set CODEX_CLI_PATH "
            "to a custom build."
        )
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise UserFacingError(missing_message)
    binary = binary.resolve()
    code_mode_host = binary.with_name("codex-code-mode-host")
    if not code_mode_host.is_file() or not os.access(code_mode_host, os.X_OK):
        raise UserFacingError(
            "Patched Core is missing the required adjacent executable: "
            f"{code_mode_host}. Re-run `codex-configure setup dynamic` or "
            "`codex-configure patch`."
        )
    return str(binary)


def run_run(
    codex_home: Path,
    target: str,
    console: Console,
    environ: Mapping[str, str],
    manager: ConfigManager | None = None,
    launcher: Launcher | None = None,
    app_args: Sequence[str] = (),
    core_home: Path | None = None,
    launch_root: Path | None = None,
) -> int:
    manager = manager or ConfigManager(codex_home)
    manager.require_initialized()
    launcher = launcher or Launcher(environ)
    provider, app = _run_target(target)
    if provider is not None:
        descriptor: ProviderDescriptor | None = None
        if provider != "openai":
            try:
                descriptor = manager.get_provider(provider)
            except UserFacingError as exc:
                available = ", ".join(["openai", *(item.id for item in _profiles(manager))])
                raise UserFacingError(
                    f"Unknown model provider `{provider}`. Available providers: {available}."
                ) from exc
        command = [
            *launcher.validate(
                app,
                requires_environment=bool(descriptor and descriptor.env_key),
            ),
            *app_args,
        ]
        launcher.ensure_clients_stopped(
            manager.paths.codex_home,
            target_root=launch_root,
        )
        if provider == "openai":
            active_profile = manager.activate_openai()
            credentials: dict[str, str] = {}
        else:
            assert descriptor is not None
            credentials = _credential_values(manager, environ, provider)
            if descriptor.env_key and not credentials:
                raise UserFacingError(
                    f"No credential found for `{provider}` ({descriptor.env_key}). Initialize it with `codex-configure init`."
                )
            active_profile = manager.activate_provider(provider)
        console.write(f"Profiles directory: {manager.paths.profiles}")
        console.write(f"Active profile: {active_profile}")
        console.write(f"Launching {provider}/{app} with stock Codex Core...")
        return launcher.launch(command, credentials, remove_environment=("CODEX_CLI_PATH",))

    if sys.platform not in {"linux", "darwin"}:
        raise UserFacingError("The dynamic provider picker is supported on Linux and macOS.")
    binary = _patched_binary(environ, core_home)
    command = (
        [binary, *app_args]
        if app == "cli"
        else [*launcher.validate("desktop", requires_environment=True), *app_args]
    )
    credentials = _dynamic_credentials(manager, environ)
    active_profile = manager.activate_dynamic()
    if app == "cli":
        child_environment = credentials
    else:
        child_environment = {**credentials, "CODEX_CLI_PATH": binary}
    console.write(f"Profiles directory: {manager.paths.profiles}")
    console.write(f"Active base profile: {active_profile}")
    console.write(f"Launching dynamic provider picker ({app})...")
    return launcher.launch(command, child_environment)


def run_patch(
    core_home: Path,
    console: Console,
    environ: Mapping[str, str],
    checkout_path: Path | None = None,
) -> int:
    del environ
    if sys.platform not in {"linux", "darwin"}:
        raise UserFacingError("Core patching for the dynamic provider picker is supported on Linux and macOS.")
    try:
        from .patcher import CodexPatcher
    except ImportError as exc:
        raise UserFacingError("The patch backend is not installed in this codex-configure build.") from exc
    core_home = core_home.expanduser().resolve()
    destination = (
        checkout_path.expanduser().resolve()
        if checkout_path
        else CodexPatcher.default_destination(core_home)
    )
    patcher = CodexPatcher(home=core_home)
    result = patcher.patch(destination)
    console.write("Codex Core patched and built.")
    if result.binary_path == CodexPatcher.default_binary(core_home):
        console.write("Dynamic runs will use this build automatically.")
    else:
        console.write("Select this custom build for dynamic runs with:")
        console.write(result.export_line)
    console.write(f"Code Mode host: {result.code_mode_host_path}")
    console.write(f"Build directory: {result.worktree}")
    return 0


def run_setup_dynamic(
    console: Console,
    environ: Mapping[str, str],
    *,
    core_home: Path,
    installer: Any | None = None,
) -> int:
    del environ
    if installer is None:
        try:
            from .core_install import CoreInstaller
        except ImportError as exc:
            raise UserFacingError(
                "The Dynamic Core installer is not included in this codex-configure build."
            ) from exc
        installer = CoreInstaller(home=core_home.expanduser().resolve())
    result = installer.install()
    action = "Verified existing" if result.reused else "Installed"
    console.write(f"{action} Dynamic Core {result.version} for {result.target}.")
    console.write(f"Core directory: {result.install_directory}")
    console.write("Dynamic runs will use this build automatically.")
    return 0


def run_doctor(
    codex_home: Path,
    console: Console,
    environ: Mapping[str, str],
    manager: ConfigManager | None = None,
    launcher: Launcher | None = None,
    *,
    launch_root: Path | None = None,
    launch_status: str | None = None,
) -> int:
    manager = manager or ConfigManager(codex_home)
    launcher = launcher or Launcher(environ)
    report = manager.doctor()
    if launch_root is not None and launch_status is not None:
        console.write("Launch root (exact current directory)")
        console.write(f"  Directory: {launch_root}")
        console.write(f"  Status: {launch_status}")
        console.write()
    console.write(f"Codex home: {manager.paths.codex_home}")
    console.write(f"Profiles directory: {manager.paths.profiles}")
    console.write()
    labels = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    for check in report.checks:
        console.write(f"[{labels[check.status]}] {check.name}: {check.detail}")
    console.write()
    console.write(
        "Managed configuration: healthy"
        if report.healthy
        else "Managed configuration: attention required"
    )
    try:
        running = launcher.running_clients()
    except UserFacingError as exc:
        client_state = "unknown"
        console.write(f"[WARN] Client lifecycle: could not determine whether clients are running: {exc}")
    else:
        client_state = "running" if running else "stopped"
        if running:
            labels_by_name = {
                "chatgpt": "ChatGPT Desktop",
                "codex-desktop": "Codex Desktop",
                "codex-app": "Codex Desktop",
                "codex": "Codex CLI",
                "codex-cli": "Codex CLI",
            }
            detected = ", ".join(
                labels_by_name.get(name.casefold(), name) for name in running
            )
            console.write(f"[ADVISORY] Client lifecycle: detected {detected}.")
            console.write(
                "             Profile switching and restore are blocked while clients run because "
                "they rewrite the active configuration."
            )
            console.write(
                "             Dynamic Picker launches do not have this blanket requirement, but an "
                "already-running Desktop retains the environment with which it started."
            )
            console.write(
                f"             Action: fully quit {detected}, then rerun `codex-configure doctor`."
            )
        else:
            console.write("[OK] Client lifecycle: no Codex or ChatGPT clients detected")

    console.write()
    if report.healthy and client_state == "stopped":
        console.write("Result: healthy")
        return 0
    if report.healthy and client_state == "running":
        console.write("Result: managed configuration healthy; client action required")
    elif report.healthy:
        console.write("Result: managed configuration healthy; client lifecycle could not be verified")
    elif client_state == "running":
        console.write("Result: managed configuration attention required; client action required")
    else:
        console.write("Result: attention required")
    return 1


def run_restore(
    codex_home: Path,
    console: Console,
    environ: Mapping[str, str],
    original: bool = False,
    manager: ConfigManager | None = None,
    launcher: Launcher | None = None,
) -> int:
    manager = manager or ConfigManager(codex_home)
    launcher = launcher or Launcher(environ)
    launcher.ensure_clients_stopped()
    source = manager.restore_openai(original=original)
    console.write("Environment: OpenAI")
    console.write(f"Restored from: {source}")
    console.write(f"Active config: {manager.paths.active_config}")
    return 0


def _choose_launch_settings(manager: ConfigManager, console: Console) -> LaunchSettings:
    providers = _profiles(manager)
    selected = console.choose(
        "Choose the Codex Core for this launch root:",
        (
            "Dynamic Picker - all configured providers (recommended)",
            "Stock Core - one fixed provider (advanced)",
        ),
    )
    if selected == 1:
        return LaunchSettings(core="dynamic", provider="openai")
    provider_choice = console.choose(
        "Choose the fixed provider for Stock Core:",
        (
            "OpenAI (stock)",
            *(f"{provider.id} ({_provider_kind_label(provider)})" for provider in providers),
        ),
    )
    provider = "openai" if provider_choice == 1 else providers[provider_choice - 2].id
    return LaunchSettings(core="stock", provider=provider)


def run_init_command(
    cwd: Path,
    user_home: Path,
    console: Console,
    environ: Mapping[str, str],
    *,
    explicit_codex_home: Path | None = None,
    installer: Any | None = None,
) -> int:
    normal_codex_home = (user_home / ".codex").resolve()
    if explicit_codex_home is not None:
        result = run_init(
            explicit_codex_home,
            console,
            environ,
            auth_source_home=normal_codex_home,
        )
        if result == INIT_CANCELLED:
            return 0
        if result == 0:
            console.write(
                "Explicit Codex home configured; no project launcher or Core was changed."
            )
        return result

    cwd = cwd.resolve()
    state_dir = local_state(cwd)

    fullscreen = _fullscreen_console(console)
    if (state_dir / "root.toml").exists():
        root_context = (
            load_launch_context(state_dir)
            if (state_dir / "launch.toml").exists()
            else initialize_root(cwd)
        )
    elif state_dir.exists():
        raise UserFacingError(
            f"{state_dir} exists but is not a codex-configure launch root (root.toml is missing)."
        )
    else:
        if cwd == user_home.resolve():
            raise UserFacingError(
                "Your home directory cannot be a launch root. Create or enter a project "
                "directory and run `codex-configure init` there."
            )
        if not fullscreen:
            selected = console.choose(
                "What would you like to configure?",
                (
                    f"Create a launch root in {cwd}",
                    "Cancel",
                ),
            )
            if selected == 2:
                console.write("Cancelled.")
                return 0
        root_context = initialize_root(cwd)

    manager = ConfigManager(root_context.codex_home)
    manager.initialize()
    if fullscreen:
        detected_auth_home = _detected_openai_auth_home(
            normal_codex_home,
            root_context.codex_home,
            environ,
        )
        try:
            tui_result = run_fullscreen_init(
                manager,
                environ,
                launch_settings=root_context.settings,
                auth_source_home=detected_auth_home,
            )
        except _CURSES_FAILURES:
            console.write("Full-screen init is unavailable here; using the text interface.")
            fullscreen = False
        else:
            if tui_result.outcome == InitOutcome.CANCELLED:
                console.write("Cancelled. Proposed profile and launch-default changes were discarded.")
                return 0
            settings = tui_result.settings
            assert settings is not None
    if not fullscreen:
        result = _run_init_text(
            manager,
            console,
            environ,
            catalog_service=None,
            auth_source_home=normal_codex_home,
        )
        if result == INIT_CANCELLED:
            return 0
        if result != 0:
            return result
        settings = _choose_launch_settings(manager, console)
    manager.ensure_project_root_marker(CORE_PROJECT_ROOT_MARKER)
    if settings.core == "dynamic":
        run_setup_dynamic(
            console,
            environ,
            core_home=cwd,
            installer=installer,
        )
    write_launch_configuration(
        root_context.state_dir,
        root_context.codex_home,
        settings,
        root=True,
    )
    console.write()
    console.write(f"Launch root: {cwd}")
    console.write(f"Default launch: {settings.description}")
    console.write(f"Launcher: {root_context.launcher}")
    return 0


def _provider_summary(manager: ConfigManager) -> str:
    try:
        external = manager.list_providers(include_stock=False)
    except UserFacingError as exc:
        return f"invalid ({exc})"
    return ", ".join(("openai", *(provider.id for provider in external)))


def _launcher_summary(path: Path) -> str:
    if not path.is_file():
        return f"missing ({path})"
    if not os.access(path, os.X_OK):
        return f"not executable ({path})"
    return str(path)


def _dynamic_core_summary(core_home: Path) -> str:
    try:
        from .core_install import CoreInstaller
    except ImportError:
        return "backend unavailable"
    binary = CoreInstaller.current_binary(core_home)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return "not installed"
    try:
        version = binary.resolve().parent.name
    except OSError:
        version = binary.parent.name
    return f"installed ({version})"


def run_status(cwd: Path, user_home: Path, console: Console) -> int:
    """Describe the exact-CWD launch root without mutating it."""

    del user_home
    cwd = cwd.resolve()
    state_dir = local_state(cwd)
    status = 0

    console.write("Launch root (exact current directory)")
    console.write(f"  Directory: {cwd}")
    if not state_dir.exists():
        console.write("  Status: not configured")
    elif not (state_dir / "root.toml").exists():
        console.write(f"  Status: not recognized (missing {state_dir / 'root.toml'})")
        status = 2
    else:
        try:
            context = load_launch_context(state_dir)
        except UserFacingError as exc:
            console.write(f"  Status: invalid ({exc})")
            status = 2
        else:
            manager = ConfigManager(context.codex_home)
            console.write(
                "  Status: ready"
                if manager.is_initialized()
                else "  Status: root exists; init incomplete"
            )
            console.write(f"  CODEX_HOME: {context.codex_home}")
            console.write(f"  Launch mode: {context.settings.description}")
            console.write(f"  Providers: {_provider_summary(manager)}")
            console.write(f"  Dynamic Core: {_dynamic_core_summary(cwd)}")
            console.write(f"  Launcher: {_launcher_summary(context.launcher)}")
    return status


def run_launch(cwd: Path, user_home: Path, args: Sequence[str]) -> int:
    del user_home
    state_dir = local_state(cwd)
    if not state_dir.exists():
        raise UserFacingError(
            "No launch root exists in the exact current directory. "
            "Run `codex-configure init` here first."
        )
    if not (state_dir / "root.toml").exists():
        raise UserFacingError(f"{state_dir} exists but is not a launch root.")
    context = load_launch_context(state_dir)
    launcher = context.launcher
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise UserFacingError(f"Launch script is missing or not executable: {launcher}")
    os.execv(str(launcher), [str(launcher), *args])
    return 0  # pragma: no cover - os.execv does not return on success


def run_launch_context(
    state_dir: Path,
    args: Sequence[str],
    console: Console,
    environ: Mapping[str, str],
) -> int:
    context = load_launch_context(state_dir)
    if context.root is None:
        raise UserFacingError(
            "Global launch configurations are no longer supported. "
            "Run `codex-configure init` inside a project directory."
        )
    if args and args[0] == "--":
        command_args = tuple(args[1:])
        if not command_args:
            raise UserFacingError(
                "Arbitrary launch requires a command after `launch --`."
            )
        child_environment = rooted_environment(context, environ)
        console.write(f"Launch root: {context.root}")
        os.execvpe(command_args[0], list(command_args), child_environment)
        return 0  # pragma: no cover - os.execvpe does not return on success
    target = args[0] if args else "desktop"
    app_args = tuple(args[1:])
    if target not in {"desktop", "cli", "chrome"}:
        raise UserFacingError("Launch target must be `desktop`, `cli`, or `chrome`.")
    child_environment = rooted_environment(context, environ)
    if context.root is not None:
        console.write(f"Launch root: {context.root}")
    if target == "chrome":
        manager = ConfigManager(context.codex_home)
        manager.require_initialized()
        if context.settings.core == "dynamic":
            if sys.platform not in {"linux", "darwin"}:
                raise UserFacingError(
                    "The dynamic provider picker is supported on Linux and macOS."
                )
            child_environment.update(_dynamic_credentials(manager, child_environment))
            binary = _patched_binary(
                child_environment,
                context.root,
            )
            child_environment["CODEX_CLI_PATH"] = binary
            console.write(f"Chrome native host Core: {binary}")
        else:
            child_environment.pop("CODEX_CLI_PATH", None)
            if context.settings.provider != "openai":
                try:
                    descriptor = manager.get_provider(context.settings.provider)
                except UserFacingError as exc:
                    available = ", ".join(
                        ["openai", *(item.id for item in _profiles(manager))]
                    )
                    raise UserFacingError(
                        f"Unknown model provider `{context.settings.provider}`. "
                        f"Available providers: {available}."
                    ) from exc
                credentials = _credential_values(
                    manager,
                    child_environment,
                    context.settings.provider,
                )
                if descriptor.env_key and not credentials:
                    raise UserFacingError(
                        f"No credential found for `{context.settings.provider}` "
                        f"({descriptor.env_key}). Initialize it with `codex-configure init`."
                    )
                child_environment.update(credentials)
            console.write("Chrome native host Core: stock")

        needs_extension = not chrome_extension_installed(context)
        if needs_extension:
            console.write(
                "The ChatGPT extension is not installed in this root's isolated "
                "Chrome profile."
            )
            console.write(
                "Opening its Chrome Web Store page; review and accept Chrome's "
                "permission prompt to install it."
            )
        if sys.platform == "linux":
            sync_chrome_native_host_manifest(context)
            if not chrome_native_host_registered(context):
                console.write(
                    "The root-scoped Chrome native host is not registered yet. Launch "
                    "Desktop from this root, then use Settings > Computer Use > Chrome "
                    "to install the required plugin."
                )
        console.write("Launching the isolated Chrome profile...")
        return launch_chrome(
            context,
            app_args,
            child_environment,
            open_extension_store=needs_extension,
        )
    run_target = (
        target
        if context.settings.core == "dynamic"
        else f"{context.settings.provider}/{target}"
    )
    return run_run(
        context.codex_home,
        run_target,
        console,
        child_environment,
        app_args=app_args,
        core_home=context.root,
        launch_root=context.root,
    )


def _require_local_context(cwd: Path) -> LaunchContext:
    state_dir = local_state(cwd)
    if not state_dir.exists():
        raise UserFacingError(
            "No launch root exists in the exact current directory. "
            "Run `codex-configure init` here first."
        )
    if not (state_dir / "root.toml").exists():
        raise UserFacingError(f"{state_dir} exists but is not a launch root.")
    context = load_launch_context(state_dir)
    if context.root is None:  # pragma: no cover - guarded by root.toml
        raise UserFacingError(f"{state_dir} is not a directory launch root.")
    return context


def _doctor_target(cwd: Path) -> tuple[Path, Path, str]:
    """Return the exact-CWD target for a read-only doctor report."""

    root = cwd.resolve()
    state_dir = local_state(root)
    fallback_home = state_dir / "codex-home"
    if not state_dir.exists():
        return root, fallback_home, "not configured"
    if not (state_dir / "root.toml").exists():
        return root, fallback_home, f"not recognized (missing {state_dir / 'root.toml'})"
    try:
        context = load_launch_context(state_dir)
    except UserFacingError as exc:
        return root, fallback_home, f"invalid ({exc})"
    return root, context.codex_home, "recognized"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure and launch an isolated Codex environment in the exact current directory."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Advanced: configure or operate on an explicit Codex home.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = subparsers.add_parser(
        "init",
        help="Initialize or reconfigure a launch context.",
        description="Create or reconfigure the exact-current-directory launch root.",
    )
    init.add_argument("--codex-home", type=Path, default=argparse.SUPPRESS)

    launch = subparsers.add_parser(
        "launch",
        help="Launch from the exact-current-directory root.",
        description=(
            "Launch desktop by default, pass a target and its remaining arguments, "
            "or run an arbitrary command with `launch -- COMMAND [ARGS...]`."
        ),
    )
    launch.add_argument("launch_args", nargs=argparse.REMAINDER, metavar="[desktop|cli|chrome] [ARGS...]")

    run = subparsers.add_parser("run", help="Run provider/app with stock Core or app with dynamic Core.")
    run.add_argument("target", help="provider/app or app (cli or desktop).")
    run.add_argument("--codex-home", type=Path, default=argparse.SUPPRESS)

    setup = subparsers.add_parser("setup", help="Install an optional codex-configure component.")
    setup.add_argument("component", choices=("dynamic",), help="Component to install.")

    patch = subparsers.add_parser("patch", help="Check out, patch, and build pinned Codex Core.")
    patch.add_argument("path", nargs="?", type=Path, help="Core checkout/build directory.")

    doctor = subparsers.add_parser("doctor", help="Inspect managed runtime health.")
    doctor.add_argument("--codex-home", type=Path, default=argparse.SUPPRESS)

    restore = subparsers.add_parser("restore", help="Restore the managed OpenAI configuration.")
    restore.add_argument("--codex-home", type=Path, default=argparse.SUPPRESS)
    restore.add_argument("--original", action="store_true")

    internal_launch = subparsers.add_parser("_launch-context", help=argparse.SUPPRESS)
    internal_launch.add_argument("state_dir", type=Path)
    internal_launch.add_argument("launch_args", nargs=argparse.REMAINDER)
    # argparse has no public hidden-subcommand API. Keep the generated-script
    # entry point parseable without advertising it as part of the user CLI.
    subparsers._choices_actions = [  # type: ignore[attr-defined]
        action
        for action in subparsers._choices_actions  # type: ignore[attr-defined]
        if action.dest != "_launch-context"
    ]
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ.copy()
    user_home = Path(environ.get("HOME", str(Path.home()))).expanduser().resolve()
    explicit_codex_home = getattr(args, "codex_home", None)
    if explicit_codex_home is not None:
        explicit_codex_home = explicit_codex_home.expanduser().resolve()
    console = Console(sys.stdin, sys.stdout)
    try:
        if args.command is None:
            if explicit_codex_home is not None:
                raise UserFacingError(
                    "Bare status is project-local and does not accept `--codex-home`."
                )
            return run_status(Path.cwd(), user_home, console)
        if args.command == "init":
            return run_init_command(
                Path.cwd(),
                user_home,
                console,
                environ,
                explicit_codex_home=explicit_codex_home,
            )
        if args.command == "launch":
            if explicit_codex_home is not None:
                raise UserFacingError(
                    "Launch is project-local and does not accept `--codex-home`."
                )
            return run_launch(Path.cwd(), user_home, args.launch_args)
        if args.command == "_launch-context":
            return run_launch_context(args.state_dir, args.launch_args, console, environ)
        if args.command == "run":
            if explicit_codex_home is not None:
                return run_run(
                    explicit_codex_home,
                    args.target,
                    console,
                    environ,
                )
            context = _require_local_context(Path.cwd())
            child_environment = rooted_environment(context, environ)
            return run_run(
                context.codex_home,
                args.target,
                console,
                child_environment,
                core_home=context.root,
                launch_root=context.root,
            )
        if args.command == "setup":
            if explicit_codex_home is not None:
                raise UserFacingError(
                    "Dynamic Core setup is project-local and does not accept `--codex-home`."
                )
            context = _require_local_context(Path.cwd())
            assert context.root is not None
            return run_setup_dynamic(console, environ, core_home=context.root)
        if args.command == "patch":
            if explicit_codex_home is not None:
                raise UserFacingError(
                    "Core patching is project-local and does not accept `--codex-home`."
                )
            context = _require_local_context(Path.cwd())
            assert context.root is not None
            return run_patch(context.root, console, environ, args.path)
        if args.command == "doctor":
            if explicit_codex_home is not None:
                return run_doctor(explicit_codex_home, console, environ)
            launch_root, codex_home, launch_status = _doctor_target(Path.cwd())
            return run_doctor(
                codex_home,
                console,
                environ,
                launch_root=launch_root,
                launch_status=launch_status,
            )
        if args.command == "restore":
            original = bool(getattr(args, "original", False))
            if explicit_codex_home is not None:
                return run_restore(explicit_codex_home, console, environ, original)
            context = _require_local_context(Path.cwd())
            return run_restore(
                context.codex_home,
                console,
                rooted_environment(context, environ),
                original,
            )
        raise UserFacingError(f"Unknown command: {args.command}")
    except KeyboardInterrupt:
        console.write("\nCancelled.")
        return 130
    except UserFacingError as exc:
        console.write(f"Error: {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
