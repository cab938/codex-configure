from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from .catalog import CatalogResult, CatalogService, ModelChoice
from .errors import UserFacingError
from .runtime import ConfigManager


class Console:
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

    def choose(self, title: str, options: Sequence[str], default: int = 1) -> int:
        self.write(title)
        self.write()
        for index, option in enumerate(options, start=1):
            self.write(f"  {index}. {option}")
        self.write()
        while True:
            raw = self.ask(f"> [{default}] ")
            if raw == "":
                return default
            try:
                selected = int(raw)
            except ValueError:
                selected = 0
            if 1 <= selected <= len(options):
                return selected
            self.write(f"Enter a number from 1 to {len(options)}.")


class Launcher:
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
            command = shlex.split(override)
            if not command:
                raise UserFacingError("CODEX_DESKTOP_COMMAND is empty.")
            if Path(command[0]).name.lower() == "open" and requires_environment:
                raise UserFacingError(
                    "CODEX_DESKTOP_COMMAND uses macOS open, which cannot reliably pass the "
                    "U-M credential. Point it at the ChatGPT app executable instead."
                )
            return command
        if sys.platform == "darwin":
            home = Path(self.environ.get("HOME", str(Path.home())))
            app_binaries = (
                Path("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
                home / "Applications" / "ChatGPT.app" / "Contents" / "MacOS" / "ChatGPT",
            )
            for app_binary in app_binaries:
                if app_binary.is_file():
                    return [str(app_binary)]
            command = shutil.which("open", path=self.environ.get("PATH"))
            if command:
                if requires_environment:
                    raise UserFacingError(
                        "Found ChatGPT only through macOS open, which cannot reliably pass the "
                        "U-M credential. Set CODEX_DESKTOP_COMMAND to the ChatGPT app executable."
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
            raise UserFacingError(
                "Could not verify that Codex clients are stopped because pgrep is unavailable."
            )
        names = (
            ("ChatGPT", "chatgpt", "Codex", "codex")
            if sys.platform == "darwin"
            else ("ChatGPT", "chatgpt", "codex-desktop", "codex-app", "codex", "codex-cli")
        )
        running: list[str] = []
        for name in names:
            result = subprocess.run(
                [pgrep, "-x", name],
                check=False,
                capture_output=True,
                text=True,
                env=self.environ,
            )
            if result.returncode == 0:
                running.append(name)
            elif result.returncode != 1:
                detail = result.stderr.strip() or f"exit status {result.returncode}"
                raise UserFacingError(f"Could not inspect running clients with pgrep: {detail}")
        return running

    def ensure_clients_stopped(self) -> None:
        running = self.running_clients()
        if running:
            names = ", ".join(running)
            raise UserFacingError(
                f"Codex or ChatGPT is running ({names}). Close it before switching environments."
            )

    def launch(self, command: list[str], extra_environment: Mapping[str, str]) -> int:
        child_env = self.environ.copy()
        child_env.update(extra_environment)
        executable = Path(command[0]).name.lower()
        if executable == "open" and extra_environment:
            raise UserFacingError(
                "The macOS open fallback cannot reliably pass the U-M credential. "
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


def choose_models(
    console: Console,
    result: CatalogResult,
    previous_ids: list[str],
) -> list[ModelChoice]:
    previous = set(previous_ids)
    defaults = [
        index
        for index, model in enumerate(result.models, start=1)
        if not previous or model.slug in previous
    ]
    if not defaults:
        defaults = list(range(1, len(result.models) + 1))

    console.write("Choose models to make available in Codex")
    console.write()
    for index, model in enumerate(result.models, start=1):
        marker = "x" if index in defaults else " "
        console.write(f"  [{marker}] {index}. {model.display_name:<24} {model.status}")
    console.write()
    console.write('Enter model numbers or ranges, "all", or press Enter to keep the checked models.')
    while True:
        raw = console.ask("> ")
        try:
            indexes = parse_model_selection(raw, len(result.models), defaults)
            return [result.models[index - 1] for index in indexes]
        except ValueError:
            console.write(f'Enter values from 1 to {len(result.models)}, ranges such as "1-3", or "all".')


def find_umich_credential(
    environ: Mapping[str, str],
    explicit_env_file: Path | None,
    default_env_file: Path,
) -> str | None:
    direct = environ.get("UMICH_TOOLKIT_API_KEY")
    if direct:
        return direct

    credential_file = explicit_env_file or default_env_file
    return _read_dotenv_value(credential_file, "UMICH_TOOLKIT_API_KEY")


def _read_dotenv_value(path: Path, key: str) -> str | None:
    try:
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise UserFacingError(f"Credential file {path} must not be accessible by group or other users.")
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value or None
    return None


def run_interactive(
    codex_home: Path,
    console: Console,
    environ: Mapping[str, str],
    env_file: Path | None = None,
    prepare_only: bool = False,
    manager: ConfigManager | None = None,
    catalog_service: CatalogService | None = None,
    launcher: Launcher | None = None,
) -> int:
    manager = manager or ConfigManager(codex_home)
    launcher = launcher or Launcher(environ)
    manager.initialize()

    environment = console.choose("Choose environment", ["OpenAI", "U-M GPT Toolkit"])
    extra_environment: dict[str, str] = {}

    if environment == 1:
        environment_name = "OpenAI"
        target = console.choose("Choose launch target", ["Codex Desktop", "Codex CLI"])
        launch_target = "desktop" if target == 1 else "cli"
        command = [] if prepare_only else launcher.validate(launch_target)
        launcher.ensure_clients_stopped()
        active_profile = manager.activate_openai()
    else:
        default_env_file = manager.paths.root / ".env"
        credential_file = env_file or default_env_file
        credential = find_umich_credential(environ, env_file, default_env_file)
        if not credential:
            raise UserFacingError(
                "UMICH_TOOLKIT_API_KEY is missing; set it in the environment or add it to "
                f"{credential_file} (mode 0600)."
            )
        console.choose("Choose provider", ["OpenAI / Azure"])
        service = catalog_service or CatalogService(codex_home)
        result = service.discover()
        if result.warning:
            console.write(f"Warning: {result.warning}")
            console.write()
        previous_ids, previous_default = manager.load_umich_preferences()
        selected_models = choose_models(console, result, previous_ids)
        default_ids = [model.slug for model in selected_models]
        default_index = 1
        preferred = previous_default or "gpt-5.6-terra"
        if preferred in default_ids:
            default_index = default_ids.index(preferred) + 1
        chosen_default = console.choose(
            "Choose the default model",
            [model.display_name for model in selected_models],
            default=default_index,
        )
        default_model = selected_models[chosen_default - 1].slug
        target = console.choose("Choose launch target", ["Codex Desktop", "Codex CLI"])
        launch_target = "desktop" if target == 1 else "cli"
        command = [] if prepare_only else launcher.validate(
            launch_target, requires_environment=True
        )
        launcher.ensure_clients_stopped()
        catalog = service.build_selected_catalog(selected_models)
        active_profile = manager.activate_umich(
            selected_models,
            default_model,
            catalog,
            result.source,
        )
        environment_name = "U-M GPT Toolkit / OpenAI / Azure"
        extra_environment["UMICH_TOOLKIT_API_KEY"] = credential

    console.write()
    console.write(f"Environment: {environment_name}")
    console.write(f"Profiles directory: {manager.paths.profiles}")
    console.write(f"Active profile: {active_profile}")
    if prepare_only:
        console.write("Launch skipped (--prepare-only).")
        return 0
    console.write(f"Launching {'Codex Desktop' if launch_target == 'desktop' else 'Codex CLI'}...")
    return launcher.launch(command, extra_environment)


def run_doctor(
    codex_home: Path,
    console: Console,
    environ: Mapping[str, str],
    env_file: Path | None = None,
    manager: ConfigManager | None = None,
    launcher: Launcher | None = None,
) -> int:
    manager = manager or ConfigManager(codex_home)
    launcher = launcher or Launcher(environ)
    credential_path = env_file or manager.paths.root / ".env"
    report = manager.doctor(credential_path)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Switch and launch a stock Codex environment.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("doctor", "restore"),
        help="Inspect the runtime or restore the managed OpenAI configuration.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex home to manage (defaults to CODEX_HOME or ~/.codex).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Override the U-M credential file (default: $CODEX_HOME/codex-configure/.env).",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Generate and activate the selected profile without starting Codex.",
    )
    parser.add_argument(
        "--original",
        action="store_true",
        help="With restore, use the immutable config captured on the first run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ.copy()
    codex_home = args.codex_home
    if codex_home is None:
        configured_home = environ.get("CODEX_HOME")
        codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    console = Console(sys.stdin, sys.stdout)
    try:
        if args.command == "doctor":
            if args.prepare_only or args.original:
                raise UserFacingError("doctor does not accept --prepare-only or --original.")
            return run_doctor(
                codex_home=codex_home,
                console=console,
                environ=environ,
                env_file=args.env_file,
            )
        if args.command == "restore":
            if args.prepare_only or args.env_file:
                raise UserFacingError("restore does not accept --prepare-only or --env-file.")
            return run_restore(
                codex_home=codex_home,
                console=console,
                environ=environ,
                original=args.original,
            )
        if args.original:
            raise UserFacingError("--original is only valid with restore.")
        return run_interactive(
            codex_home=codex_home,
            console=console,
            environ=environ,
            env_file=args.env_file,
            prepare_only=args.prepare_only,
        )
    except KeyboardInterrupt:
        console.write("\nCancelled.")
        return 130
    except UserFacingError as exc:
        console.write(f"Error: {exc}")
        return 2
