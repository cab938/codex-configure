"""Command-line setup and launch entry points for codex-configure."""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO

from .catalog import CatalogService, ModelChoice, is_default_model_slug
from .errors import UserFacingError
from .launch_context import (
    LaunchContext,
    LaunchSettings,
    default_global_state,
    initialize_root,
    launch_chrome,
    load_launch_context,
    local_state,
    rooted_environment,
    write_launch_configuration,
)
from .providers import ProviderDescriptor, validate_shortname
from .runtime import ConfigManager


TOOLKIT_URL = "https://toolkit.umgpt.umich.edu/"


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


class Launcher:
    """Resolve and launch stock Codex CLI/Desktop clients."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = dict(environ or os.environ)

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

    def running_clients(self) -> list[str]:
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
        running: list[str] = []
        for name in names:
            result = subprocess.run(
                [pgrep, "-x", name], check=False, capture_output=True, text=True, env=self.environ
            )
            if result.returncode == 0:
                pids = [value for value in result.stdout.split() if value.isdigit()]
                # Electron can leave reparented zombie entries behind. They
                # have no runnable client and must not permanently block a
                # profile switch. Anything other than a confirmed zombie is
                # treated conservatively as a live client.
                live = not pids
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
                        live = True
                        break
                if live:
                    running.append(name)
            elif result.returncode != 1:
                detail = result.stderr.strip() or f"exit status {result.returncode}"
                raise UserFacingError(f"Could not inspect running clients with pgrep: {detail}")
        return running

    def ensure_clients_stopped(self) -> None:
        running = self.running_clients()
        if running:
            raise UserFacingError(
                f"Codex or ChatGPT is running ({', '.join(running)}). Close it before switching environments."
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
    labels = [
        f"{model.display_name} ({model.slug})" if model.display_name != model.slug else model.slug
        for model in models
    ]
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
        [f"{model.display_name} ({model.slug})" if model.display_name != model.slug else model.slug for model in selected],
        default=default,
    )
    return selected[choice - 1].slug


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


def run_init(
    codex_home: Path,
    console: Console,
    environ: Mapping[str, str],
    manager: ConfigManager | None = None,
    catalog_service: CatalogService | None = None,
) -> int:
    manager = manager or ConfigManager(codex_home)
    manager.initialize()
    descriptors = _profiles(manager)
    labels = ["OpenAI (stock)"] + [f"{item.display_name} ({item.id})" for item in descriptors]
    labels.append("New U-M GPT Toolkit Service")
    chosen = console.choose("Choose a model provider profile to initialize", labels)
    if chosen == 1:
        console.write("OpenAI is the stock provider; no external profile was created.")
        console.write(f"Configuration home: {manager.paths.root}")
        return 0

    creating = chosen == len(labels)
    if creating:
        shortname = validate_shortname(console.ask("One-word profile name: ").strip())
        if shortname in {item.id for item in descriptors}:
            raise UserFacingError(f"A profile named `{shortname}` already exists.")
        manager.provider_registry.validate_env_collision(shortname)
        console.write(f"Get a key at {TOOLKIT_URL}")
        api_key = console.ask_secret(f"Paste the key for {shortname}: ")
        if not api_key:
            raise UserFacingError("An API key is required to create the profile.")
    else:
        descriptor = descriptors[chosen - 2]
        shortname = descriptor.id
        env_key = descriptor.env_key
        api_key = manager.load_credentials(environ).get(env_key, "")
        if not api_key:
            console.write(f"Get a key at {TOOLKIT_URL}")
            api_key = console.ask_secret(f"Paste the key for {shortname}: ")
        if not api_key:
            raise UserFacingError(f"No API key found for `{shortname}` ({env_key}).")

    service = catalog_service or CatalogService(codex_home)
    result = service.discover(api_key=api_key)
    models = tuple(result.models)
    prior_ids, prior_default = (), None
    for descriptor in descriptors:
        if descriptor.id == shortname:
            prior_ids, prior_default = _profile_metadata(manager, descriptor)
            break
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
    return 0


def _run_target(target: str) -> tuple[str | None, str]:
    provider, separator, app = target.partition("/")
    if not separator:
        provider, app = None, provider
    if app not in {"cli", "desktop"}:
        raise UserFacingError("Launch target must be `cli` or `desktop`.")
    if provider == "":
        raise UserFacingError("Run target must be `provider/app`, for example `openai/cli`.")
    return provider, app


def _patched_binary(environ: Mapping[str, str]) -> str:
    value = environ.get("CODEX_CLI_PATH", "").strip()
    if value:
        binary = Path(value).expanduser()
        missing_message = f"CODEX_CLI_PATH is not an executable file: {binary}"
    else:
        try:
            from .core_install import CoreInstaller
            from .patcher import CodexPatcher
        except ImportError as exc:
            raise UserFacingError(
                "The Dynamic Core backends are not installed in this codex-configure build."
            ) from exc
        configured_home = environ.get("HOME", "").strip()
        home = Path(configured_home) if configured_home else None
        installer = CoreInstaller(home=home)
        installed = CoreInstaller.current_binary(home)
        installed_matches_package = False
        if installed.is_file() and os.access(installed, os.X_OK):
            try:
                installed_matches_package = (
                    installed.resolve().parent
                    == CoreInstaller.versioned_directory(home, target=installer.target).resolve()
                )
            except (OSError, RuntimeError):
                pass
        candidates = (
            *((installed,) if installed_matches_package else ()),
            CodexPatcher.default_binary(home),
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
            "Dynamic Core is not installed. Run `codex-configure setup dynamic`, "
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
) -> int:
    manager = manager or ConfigManager(codex_home)
    manager.require_initialized()
    launcher = launcher or Launcher(environ)
    provider, app = _run_target(target)
    if provider is not None:
        command = [*launcher.validate(app, requires_environment=provider != "openai"), *app_args]
        launcher.ensure_clients_stopped()
        if provider == "openai":
            active_profile = manager.activate_openai()
            credentials: dict[str, str] = {}
        else:
            try:
                descriptor = manager.get_provider(provider)
            except UserFacingError as exc:
                available = ", ".join(["openai", *(item.id for item in _profiles(manager))])
                raise UserFacingError(
                    f"Unknown model provider `{provider}`. Available providers: {available}."
                ) from exc
            credentials = _credential_values(manager, environ, provider)
            if not credentials:
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
    binary = _patched_binary(environ)
    command = (
        [binary, *app_args]
        if app == "cli"
        else [*launcher.validate("desktop", requires_environment=True), *app_args]
    )
    descriptors = _profiles(manager)
    values = manager.load_credentials(environ)
    missing = [item.id for item in descriptors if item.env_key and not values.get(item.env_key)]
    if missing:
        raise UserFacingError(
            "Missing credentials for configured provider(s): "
            + ", ".join(missing)
            + ". Run `codex-configure init` to initialize each provider."
        )
    active_profile = manager.activate_dynamic()
    credentials = {
        item.env_key: values[item.env_key]
        for item in descriptors
        if item.env_key
    }
    if app == "cli":
        child_environment = credentials
    else:
        child_environment = {**credentials, "CODEX_CLI_PATH": binary}
    console.write(f"Profiles directory: {manager.paths.profiles}")
    console.write(f"Active base profile: {active_profile}")
    console.write(f"Launching dynamic provider picker ({app})...")
    return launcher.launch(command, child_environment)


def run_patch(
    codex_home: Path,
    console: Console,
    environ: Mapping[str, str],
    checkout_path: Path | None = None,
) -> int:
    del codex_home, environ
    if sys.platform not in {"linux", "darwin"}:
        raise UserFacingError("Core patching for the dynamic provider picker is supported on Linux and macOS.")
    try:
        from .patcher import CodexPatcher
    except ImportError as exc:
        raise UserFacingError("The patch backend is not installed in this codex-configure build.") from exc
    destination = checkout_path.expanduser().resolve() if checkout_path else CodexPatcher.default_destination()
    result = CodexPatcher().patch(destination)
    console.write("Codex Core patched and built.")
    if result.binary_path == CodexPatcher.default_binary():
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
    installer: Any | None = None,
) -> int:
    if installer is None:
        try:
            from .core_install import CoreInstaller
        except ImportError as exc:
            raise UserFacingError(
                "The Dynamic Core installer is not included in this codex-configure build."
            ) from exc
        configured_home = environ.get("HOME", "").strip()
        installer = CoreInstaller(home=Path(configured_home) if configured_home else None)
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
) -> int:
    manager = manager or ConfigManager(codex_home)
    launcher = launcher or Launcher(environ)
    report = manager.doctor()
    console.write(f"Codex home: {manager.paths.codex_home}")
    console.write(f"Profiles directory: {manager.paths.profiles}")
    console.write()
    labels = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    for check in report.checks:
        console.write(f"[{labels[check.status]}] {check.name}: {check.detail}")
    try:
        running = launcher.running_clients()
    except UserFacingError as exc:
        console.write(f"[ERROR] Client lifecycle: {exc}")
        clients_healthy = False
    else:
        clients_healthy = not running
        detail = ", ".join(running) if running else "no Codex or ChatGPT clients detected"
        console.write(f"[{'OK' if clients_healthy else 'ERROR'}] Client lifecycle: {detail}")
    healthy = report.healthy and clients_healthy
    console.write()
    console.write("Result: healthy" if healthy else "Result: attention required")
    return 0 if healthy else 1


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
    options = [
        "Dynamic picker (patched Core; all configured providers)",
        "OpenAI (stock Core)",
        *(f"{provider.display_name} ({provider.id}, stock Core)" for provider in providers),
    ]
    default = 1 if providers else 2
    selected = console.choose("Choose the default launch behavior", options, default=default)
    if selected == 1:
        return LaunchSettings(core="dynamic", provider="openai")
    if selected == 2:
        return LaunchSettings(core="stock", provider="openai")
    return LaunchSettings(core="stock", provider=providers[selected - 3].id)


def run_init_command(
    cwd: Path,
    user_home: Path,
    console: Console,
    environ: Mapping[str, str],
    *,
    explicit_codex_home: Path | None = None,
) -> int:
    normal_codex_home = (user_home / ".codex").resolve()
    if explicit_codex_home is not None and explicit_codex_home.resolve() != normal_codex_home:
        result = run_init(explicit_codex_home, console, environ)
        if result == 0:
            console.write("Custom Codex home configured; the default launch script was not changed.")
        return result

    cwd = cwd.resolve()
    state_dir = local_state(cwd)
    global_state = default_global_state(user_home).resolve()
    root_context: LaunchContext | None = None

    if (state_dir / "root.toml").exists():
        root_context = initialize_root(cwd)
    elif state_dir.exists() and state_dir != global_state:
        raise UserFacingError(
            f"{state_dir} exists but is not a codex-configure launch root (root.toml is missing)."
        )
    else:
        selected = console.choose(
            "What would you like to configure?",
            (
                f"Create a launch root in {cwd}",
                f"Configure the normal Codex home at {normal_codex_home}",
                "Cancel",
            ),
        )
        if selected == 3:
            console.write("Cancelled.")
            return 0
        if selected == 1:
            if state_dir == global_state and state_dir.exists():
                raise UserFacingError(
                    "The current directory is your home directory, where ~/.codex-configure "
                    "is reserved for global state. Choose the normal Codex home instead."
                )
            root_context = initialize_root(cwd)

    if root_context is not None:
        codex_home = root_context.codex_home
        state_dir = root_context.state_dir
        is_root = True
    else:
        codex_home = normal_codex_home
        state_dir = global_state
        is_root = False

    result = run_init(codex_home, console, environ)
    if result != 0:
        return result
    manager = ConfigManager(codex_home)
    settings = _choose_launch_settings(manager, console)
    write_launch_configuration(
        state_dir,
        codex_home,
        settings,
        root=is_root,
    )
    console.write()
    if is_root:
        console.write(f"Launch root: {cwd}")
    else:
        console.write(f"Global Codex home: {codex_home}")
    console.write(f"Default launch: {settings.description}")
    console.write(f"Launcher: {state_dir / 'launch.sh'}")
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


def _dynamic_core_summary(user_home: Path) -> str:
    try:
        from .core_install import CoreInstaller
    except ImportError:
        return "backend unavailable"
    binary = CoreInstaller.current_binary(user_home)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return "not installed"
    try:
        version = binary.resolve().parent.name
    except OSError:
        version = binary.parent.name
    return f"installed ({version})"


def run_status(cwd: Path, user_home: Path, console: Console) -> int:
    """Describe exact-CWD and normal-home configuration without mutating either."""

    cwd = cwd.resolve()
    state_dir = local_state(cwd)
    global_state = default_global_state(user_home).resolve()
    status = 0

    console.write("Local launch root (current directory)")
    console.write(f"  Directory: {cwd}")
    if state_dir == global_state and not (state_dir / "root.toml").exists():
        console.write("  Status: not configured (this is the global state directory)")
    elif not state_dir.exists():
        console.write("  Status: not configured")
    elif not (state_dir / "root.toml").exists():
        console.write(f"  Status: not recognized (missing {state_dir / 'root.toml'})")
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
            console.write(f"  Launcher: {_launcher_summary(context.launcher)}")

    normal_codex_home = (user_home / ".codex").resolve()
    manager = ConfigManager(normal_codex_home)
    console.write()
    console.write("Global configuration")
    console.write(f"  CODEX_HOME: {normal_codex_home}")
    console.write(f"  Managed: {'yes' if manager.is_initialized() else 'no'}")
    console.write(f"  Providers: {_provider_summary(manager)}")
    launch_file = global_state / "launch.toml"
    if launch_file.exists():
        try:
            context = load_launch_context(global_state)
        except UserFacingError as exc:
            console.write(f"  Launch mode: invalid ({exc})")
            status = 2
        else:
            console.write(f"  Launch mode: {context.settings.description}")
    else:
        console.write("  Launch mode: not configured")
    console.write(f"  Dynamic Core: {_dynamic_core_summary(user_home)}")
    console.write(f"  Launcher: {_launcher_summary(global_state / 'launch.sh')}")
    return status


def run_launch(cwd: Path, user_home: Path, args: Sequence[str]) -> int:
    state_dir = local_state(cwd)
    global_state = default_global_state(user_home).resolve()
    if state_dir == global_state:
        if not (global_state / "launch.toml").is_file():
            raise UserFacingError("No global launch configuration found. Run `codex-configure init` first.")
        selected = global_state
    elif state_dir.exists():
        if not (state_dir / "root.toml").exists():
            raise UserFacingError(
                f"{state_dir} exists but is not a launch root; refusing global fallback."
            )
        load_launch_context(state_dir)
        selected = state_dir
    elif (global_state / "launch.toml").is_file():
        selected = global_state
    else:
        raise UserFacingError("No launch configuration found. Run `codex-configure init` first.")

    context = load_launch_context(selected)
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
    target = args[0] if args else "desktop"
    app_args = tuple(args[1:])
    if target not in {"desktop", "cli", "chrome"}:
        raise UserFacingError("Launch target must be `desktop`, `cli`, or `chrome`.")
    child_environment = rooted_environment(context, environ)
    if context.root is not None:
        console.write(f"Launch root: {context.root}")
    if target == "chrome":
        return launch_chrome(context, app_args, child_environment)
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
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure and launch global or directory-rooted Codex environments."
    )
    parser.add_argument("--codex-home", type=Path, help="Codex home (defaults to CODEX_HOME or ~/.codex).")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = subparsers.add_parser(
        "init",
        help="Initialize or reconfigure a launch context.",
        description="Create or reconfigure the exact-CWD launch root or normal ~/.codex home.",
    )
    init.add_argument("--codex-home", type=Path, default=argparse.SUPPRESS)

    launch = subparsers.add_parser(
        "launch",
        help="Launch from the local root or global configuration.",
        description="Launch desktop by default, or pass a target and its remaining arguments.",
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
    configured_home = environ.get("CODEX_HOME")
    explicit_codex_home = getattr(args, "codex_home", None)
    codex_home = explicit_codex_home or (
        Path(configured_home).expanduser() if configured_home else user_home / ".codex"
    )
    console = Console(sys.stdin, sys.stdout)
    try:
        if args.command is None:
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
            return run_launch(Path.cwd(), user_home, args.launch_args)
        if args.command == "_launch-context":
            return run_launch_context(args.state_dir, args.launch_args, console, environ)
        if args.command == "run":
            return run_run(codex_home, args.target, console, environ)
        if args.command == "setup":
            return run_setup_dynamic(console, environ)
        if args.command == "patch":
            return run_patch(codex_home, console, environ, args.path)
        if args.command == "doctor":
            return run_doctor(codex_home, console, environ)
        if args.command == "restore":
            return run_restore(codex_home, console, environ, bool(getattr(args, "original", False)))
        raise UserFacingError(f"Unknown command: {args.command}")
    except KeyboardInterrupt:
        console.write("\nCancelled.")
        return 130
    except UserFacingError as exc:
        console.write(f"Error: {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
