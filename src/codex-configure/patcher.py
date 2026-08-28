"""Safely build the patched Codex Core binary."""

from __future__ import annotations

import ast
import importlib.resources
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence, TextIO

from .errors import UserFacingError


DEFAULT_DESTINATION = Path("~/.codex-configure/codex-core")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RUSTC_RE = re.compile(r"^rustc ([0-9]+\.[0-9]+\.[0-9]+)(?:[ -]|$)")
_RUSTC_HOST_RE = re.compile(r"^host: ([A-Za-z0-9_.-]+)$", re.MULTILINE)
_REMOTE_CREDENTIAL_RE = re.compile(r"(://)([^/@\s]+)@")
_V8_ENV_SCRIPT = """\
import json
import sys

from scripts.codex_package.targets import TARGET_SPECS
from scripts.codex_package.v8 import resolve_codex_v8_cargo_env

target = sys.argv[1]
if target not in TARGET_SPECS:
    raise SystemExit(f"Unsupported Codex package target: {target}")
print(json.dumps(resolve_codex_v8_cargo_env(TARGET_SPECS[target]), sort_keys=True))
"""


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    """Run commands without a shell, streaming the potentially long Cargo build."""

    def __init__(self, progress_output: TextIO | None = None) -> None:
        self.progress_output = progress_output if progress_output is not None else sys.stderr

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        command = tuple(str(arg) for arg in args)
        child_environment = {**os.environ, **environment} if environment else None
        try:
            if command and command[0] == "cargo" and "build" in command:
                return self._run_cargo(command, cwd, child_environment)
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                check=False,
                capture_output=True,
                text=True,
                env=child_environment,
            )
        except OSError as exc:
            raise UserFacingError(
                f"Could not run {shlex.join(command)}: {exc.strerror or exc}"
            ) from exc
        return CommandResult(
            command,
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )

    def _run_cargo(
        self,
        command: tuple[str, ...],
        cwd: Path | None,
        environment: Mapping[str, str] | None,
    ) -> CommandResult:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            raise UserFacingError(
                f"Could not run {shlex.join(command)}: {exc.strerror or exc}"
            ) from exc

        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            self.progress_output.write(line)
            self.progress_output.flush()
        return CommandResult(command, process.wait(), "".join(output), "")


@dataclass(frozen=True)
class PatchResources:
    """Checked-in patch and upstream pin used to build Core."""

    root: Path
    patch_file: Path
    upstream_url: str
    upstream_commit: str
    minimum_rust_version: str
    pin_file: Path | None = None

    @classmethod
    def from_root(cls, root: Path | str) -> "PatchResources":
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise UserFacingError(f"Core patch resource directory does not exist: {root}")
        patch_file = _find_file(
            root,
            ("codex-provider-model-picker.patch", "provider-model-picker.patch", "codex.patch"),
            suffix=".patch",
        )
        pin_file = _find_file(
            root,
            ("upstream-pin.env", "upstream-pin.toml", "pin.env", "pin.toml"),
            suffix=None,
            required=False,
        )
        if pin_file is None:
            raise UserFacingError(f"Core patch resource directory has no upstream pin: {root}")
        upstream_url, upstream_commit, minimum_rust_version = _read_pin(pin_file)
        resources = cls(
            root=root,
            patch_file=patch_file,
            upstream_url=upstream_url,
            upstream_commit=upstream_commit,
            minimum_rust_version=minimum_rust_version,
            pin_file=pin_file,
        )
        resources.validate()
        return resources

    @classmethod
    def discover(cls) -> "PatchResources":
        """Find source-tree or packaged canonical patch resources."""

        candidates: list[Path] = []
        override = os.environ.get("CODEX_CONFIGURE_PATCH_ROOT")
        if override:
            candidates.append(Path(override).expanduser())

        repository_root = Path(__file__).resolve().parents[2]
        candidates.append(repository_root / "src" / "core-provider-model-picker")
        try:
            candidates.append(Path(str(importlib.resources.files("core_provider_model_picker"))))
        except (ModuleNotFoundError, TypeError, ValueError):
            pass

        errors: list[str] = []
        seen: set[Path] = set()
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                return cls.from_root(candidate)
            except UserFacingError as exc:
                errors.append(str(exc))
        detail = errors[-1] if errors else "canonical resources were not found"
        raise UserFacingError(
            "Could not locate canonical Codex Core patch resources. "
            "Install core-provider-model-picker resources or set "
            f"CODEX_CONFIGURE_PATCH_ROOT. {detail}"
        )

    def validate(self) -> None:
        if not self.patch_file.is_file():
            raise UserFacingError(f"Codex Core patch file does not exist: {self.patch_file}")
        if not self.patch_file.stat().st_size:
            raise UserFacingError(f"Codex Core patch file is empty: {self.patch_file}")
        if not self.upstream_url.strip():
            raise UserFacingError("Codex Core upstream URL is empty in the upstream pin.")
        if not _COMMIT_RE.fullmatch(self.upstream_commit):
            raise UserFacingError(
                "Codex Core upstream commit must be a lowercase 40-character SHA."
            )
        if not _VERSION_RE.fullmatch(self.minimum_rust_version):
            raise UserFacingError(
                "Codex Core minimum Rust version must use MAJOR.MINOR.PATCH syntax."
            )


@dataclass(frozen=True)
class PatchResult:
    worktree: Path
    binary_path: Path
    code_mode_host_path: Path
    upstream_url: str
    upstream_commit: str
    patch_file: Path
    commands: tuple[tuple[str, ...], ...]
    messages: tuple[str, ...]

    @property
    def export_line(self) -> str:
        return f"export CODEX_CLI_PATH={shlex.quote(str(self.binary_path))}"


class CodexPatcher:
    """Clone, pin, patch, and build one safe Codex Core checkout."""

    def __init__(
        self,
        resources: PatchResources | None = None,
        runner: CommandRunner | None = None,
        home: Path | None = None,
    ) -> None:
        self.resources = resources
        self.runner = runner or SubprocessRunner()
        self.home = Path(home).expanduser().resolve() if home is not None else Path.home()
        self._commands: list[tuple[str, ...]] = []

    @staticmethod
    def default_destination(home: Path | None = None) -> Path:
        base = Path(home).expanduser() if home is not None else Path.home()
        return (base / DEFAULT_DESTINATION.relative_to("~")).resolve()

    def patch(self, destination: Path | str | None = None) -> PatchResult:
        resources = self.resources or PatchResources.discover()
        resources.validate()
        worktree = self._destination(destination)
        self._commands = []
        messages: list[str] = []

        if worktree.exists() or worktree.is_symlink():
            if worktree.is_symlink():
                raise UserFacingError(f"Refusing symlinked Core checkout: {worktree}")
            self._validate_existing_checkout(worktree, resources)
            status = self._command(
                ("git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all")
            )
            if status.returncode:
                raise UserFacingError(f"Could not inspect checkout state: {worktree}")
            if status.stdout.strip():
                if self._patch_is_already_applied(worktree, resources):
                    messages.append("Existing checkout already contains the canonical patch.")
                else:
                    raise UserFacingError(
                        "Refusing to modify a dirty Codex Core checkout. "
                        "Use a clean checkout or preserve the changes elsewhere."
                    )
            else:
                self._pin_checkout(worktree, resources)
                self._apply_patch(worktree, resources)
        else:
            self._prepare_destination_parent(worktree)
            self._clone_checkout(worktree, resources)
            self._pin_checkout(worktree, resources)
            self._apply_patch(worktree, resources)

        release_dir = worktree / "codex-rs" / "target" / "release"
        binary_path = release_dir / "codex"
        code_mode_host_path = release_dir / "codex-code-mode-host"
        self._build(worktree, resources)
        self._verify_binaries(binary_path, code_mode_host_path, worktree)
        messages.append(
            f"Built patched Codex Core at {binary_path} with Code Mode host at {code_mode_host_path}"
        )
        return PatchResult(
            worktree=worktree,
            binary_path=binary_path,
            code_mode_host_path=code_mode_host_path,
            upstream_url=resources.upstream_url,
            upstream_commit=resources.upstream_commit,
            patch_file=resources.patch_file,
            commands=tuple(self._commands),
            messages=tuple(messages),
        )

    def _destination(self, destination: Path | str | None) -> Path:
        if destination is None:
            return self.default_destination(self.home)
        path = Path(destination).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    def _prepare_destination_parent(self, worktree: Path) -> None:
        parent = worktree.parent
        if parent.is_symlink():
            raise UserFacingError(f"Refusing symlinked checkout parent: {parent}")
        if not parent.exists():
            try:
                parent.mkdir(parents=True, mode=0o700)
            except OSError as exc:
                raise UserFacingError(f"Could not create checkout parent {parent}: {exc}") from exc
        if not parent.is_dir():
            raise UserFacingError(f"Checkout parent is not a directory: {parent}")

    def _validate_existing_checkout(self, worktree: Path, resources: PatchResources) -> None:
        if not worktree.is_dir():
            raise UserFacingError(f"Refusing to overwrite existing non-directory: {worktree}")
        top = self._command(("git", "-C", str(worktree), "rev-parse", "--show-toplevel"))
        if top.returncode or not top.stdout.strip():
            raise UserFacingError(f"Refusing existing directory that is not a Git checkout: {worktree}")
        if Path(top.stdout.strip()).expanduser().resolve() != worktree:
            raise UserFacingError(f"Refusing Git worktree rooted elsewhere: {worktree}")
        remote = self._command(("git", "-C", str(worktree), "remote", "get-url", "origin"))
        if remote.returncode or not _same_remote(remote.stdout, resources.upstream_url):
            raise UserFacingError(f"Refusing checkout with a non-canonical origin remote: {worktree}")

    def _clone_checkout(self, worktree: Path, resources: PatchResources) -> None:
        self._checked(("git", "clone", "--no-tags", resources.upstream_url, str(worktree)))
        if not worktree.is_dir():
            raise UserFacingError(f"Git clone did not create checkout: {worktree}")
        self._validate_existing_checkout(worktree, resources)

    def _pin_checkout(self, worktree: Path, resources: PatchResources) -> None:
        self._checked(
            ("git", "-C", str(worktree), "fetch", "--no-tags", "origin", resources.upstream_commit)
        )
        self._checked(("git", "-C", str(worktree), "checkout", "--detach", resources.upstream_commit))
        head = self._checked(("git", "-C", str(worktree), "rev-parse", "HEAD")).stdout.strip().lower()
        if head != resources.upstream_commit:
            raise UserFacingError("Git checkout did not reach the pinned Core commit; refusing to build.")
        status = self._checked(
            ("git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all")
        )
        if status.stdout.strip():
            raise UserFacingError("Core checkout became dirty while selecting the pinned commit.")

    def _apply_patch(self, worktree: Path, resources: PatchResources) -> None:
        check = self._command(("git", "-C", str(worktree), "apply", "--check", str(resources.patch_file)))
        if check.returncode:
            detail = _command_detail(check)
            raise UserFacingError(
                "The canonical Codex Core patch does not apply to the pinned commit."
                + (f" ({detail})" if detail else "")
            )
        self._checked(("git", "-C", str(worktree), "apply", "--index", str(resources.patch_file)))

    def _patch_is_already_applied(self, worktree: Path, resources: PatchResources) -> bool:
        head = self._command(("git", "-C", str(worktree), "rev-parse", "HEAD"))
        if head.returncode or head.stdout.strip().lower() != resources.upstream_commit:
            return False
        reverse = self._command(
            (
                "git",
                "-C",
                str(worktree),
                "apply",
                "--cached",
                "--reverse",
                "--check",
                str(resources.patch_file),
            )
        )
        if reverse.returncode:
            return False
        unstaged = self._command(("git", "-C", str(worktree), "diff", "--binary"))
        if unstaged.returncode or unstaged.stdout:
            return False
        untracked = self._command(
            ("git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard")
        )
        if untracked.returncode or untracked.stdout.strip():
            return False
        staged = self._command(("git", "-C", str(worktree), "diff", "--cached", "--binary"))
        try:
            return staged.returncode == 0 and staged.stdout.encode() == resources.patch_file.read_bytes()
        except OSError:
            return False

    def _build(self, worktree: Path, resources: PatchResources) -> None:
        manifest = worktree / "codex-rs" / "Cargo.toml"
        if not manifest.is_file():
            raise UserFacingError(f"Core checkout is missing Cargo manifest: {manifest}")
        self._check_rust_version(resources)
        v8_environment = self._resolve_v8_environment(worktree)
        self._checked(
            (
                "cargo",
                "build",
                "--release",
                "--manifest-path",
                str(manifest),
                "--package",
                "codex-cli",
                "--package",
                "codex-code-mode-host",
            ),
            cwd=worktree,
            environment=v8_environment,
        )

    def _resolve_v8_environment(self, worktree: Path) -> dict[str, str]:
        rustc = self._checked(("rustc", "-vV"))
        host_match = _RUSTC_HOST_RE.search(rustc.stdout)
        if host_match is None:
            raise UserFacingError("Could not determine the installed Rust host target.")
        result = self._checked(
            (sys.executable, "-c", _V8_ENV_SCRIPT, host_match.group(1)),
            cwd=worktree,
            environment={"CODEX_REPO_ROOT": str(worktree)},
        )
        try:
            values = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise UserFacingError("Pinned Codex V8 artifact resolver returned invalid output.") from exc
        if not isinstance(values, dict):
            raise UserFacingError("Pinned Codex V8 artifact resolver returned invalid output.")
        expected = {"RUSTY_V8_ARCHIVE", "RUSTY_V8_SRC_BINDING_PATH"}
        if not values:
            return {}
        if set(values) != expected or not all(isinstance(values[key], str) for key in expected):
            raise UserFacingError("Pinned Codex V8 artifact resolver returned incomplete output.")
        resolved = {key: str(Path(values[key]).expanduser().resolve()) for key in expected}
        missing = [path for path in resolved.values() if not Path(path).is_file()]
        if missing:
            raise UserFacingError(f"Pinned Codex V8 artifact resolver did not produce: {missing[0]}")
        return resolved

    def _check_rust_version(self, resources: PatchResources) -> None:
        result = self._checked(("rustc", "--version"))
        match = _RUSTC_RE.match(result.stdout.strip())
        if match is None:
            raise UserFacingError("Could not determine the installed rustc version.")
        installed = _version_tuple(match.group(1))
        required = _version_tuple(resources.minimum_rust_version)
        if installed < required:
            raise UserFacingError(
                f"Pinned Codex Core requires Rust {resources.minimum_rust_version} or newer; "
                f"found {match.group(1)}. Run `rustup update stable` or select a compatible "
                "installed toolchain with RUSTUP_TOOLCHAIN."
            )

    def _verify_binaries(
        self,
        binary_path: Path,
        code_mode_host_path: Path,
        worktree: Path,
    ) -> None:
        for executable in (binary_path, code_mode_host_path):
            if not executable.is_file():
                raise UserFacingError(f"Build completed without expected executable: {executable}")
            if not os.access(executable, os.X_OK):
                raise UserFacingError(f"Build produced a non-executable file: {executable}")
        result = self._command((str(binary_path), "--version"), cwd=worktree)
        if result.returncode:
            detail = _command_detail(result)
            raise UserFacingError(
                "Built Codex Core failed its --version check." + (f" ({detail})" if detail else "")
            )
        host_result = self._command((str(code_mode_host_path), "--help"), cwd=worktree)
        if host_result.returncode:
            detail = _command_detail(host_result)
            raise UserFacingError(
                "Built Code Mode host failed its --help check."
                + (f" ({detail})" if detail else "")
            )

    def _command(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        command = tuple(str(arg) for arg in args)
        self._commands.append(command)
        try:
            result = self.runner.run(command, cwd=cwd, environment=environment)
        except UserFacingError:
            raise
        except OSError as exc:
            raise UserFacingError(
                f"Could not run {shlex.join(command)}: {exc.strerror or exc}"
            ) from exc
        if not isinstance(result, CommandResult):
            raise UserFacingError(
                f"Command runner returned an invalid result for {shlex.join(command)}."
            )
        return result

    def _checked(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        result = self._command(args, cwd, environment)
        if result.returncode:
            detail = _command_detail(result)
            raise UserFacingError(
                f"Command failed with exit status {result.returncode}: {shlex.join(result.args)}"
                + (f" ({detail})" if detail else "")
            )
        return result


def patch_codex_core(
    destination: Path | str | None = None,
    *,
    resources: PatchResources | None = None,
    runner: CommandRunner | None = None,
    home: Path | None = None,
) -> PatchResult:
    return CodexPatcher(resources=resources, runner=runner, home=home).patch(destination)


def _find_file(
    root: Path,
    names: Sequence[str],
    *,
    suffix: str | None,
    required: bool = True,
) -> Path | None:
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate.resolve()
    if suffix is not None:
        candidates = sorted(path for path in root.iterdir() if path.is_file() and path.suffix == suffix)
        if len(candidates) == 1:
            return candidates[0].resolve()
    if required:
        raise UserFacingError(f"Core patch resource is missing ({' or '.join(names)}): {root}")
    return None


def _read_pin(path: Path) -> tuple[str, str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserFacingError(f"Could not read upstream pin {path}: {exc}") from exc

    values: dict[str, str] = {}
    if path.suffix == ".toml":
        try:
            import tomllib

            parsed = tomllib.loads(text)
        except ValueError as exc:
            raise UserFacingError(f"Could not parse upstream pin {path}: {exc}") from exc
        for key in (
            "UPSTREAM_URL",
            "UPSTREAM_COMMIT",
            "MINIMUM_RUST_VERSION",
            "upstream_url",
            "upstream_commit",
            "minimum_rust_version",
        ):
            value = parsed.get(key)
            if isinstance(value, str):
                values[key.upper()] = value.strip()
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = (part.strip() for part in line.split("=", 1))
            if key not in {"UPSTREAM_URL", "UPSTREAM_COMMIT", "MINIMUM_RUST_VERSION"}:
                continue
            try:
                value = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                value = raw.strip().split(" #", 1)[0].strip()
            if isinstance(value, str):
                values[key] = value.strip()

    upstream_url = values.get("UPSTREAM_URL", "")
    upstream_commit = values.get("UPSTREAM_COMMIT", "")
    minimum_rust_version = values.get("MINIMUM_RUST_VERSION", "")
    if not upstream_url or not upstream_commit or not minimum_rust_version:
        raise UserFacingError(
            f"Upstream pin {path} must define UPSTREAM_URL, UPSTREAM_COMMIT, "
            "and MINIMUM_RUST_VERSION."
        )
    return upstream_url, upstream_commit, minimum_rust_version


def _version_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _same_remote(actual: str, expected: str) -> bool:
    def normalize(value: str) -> str:
        value = value.strip().rstrip("/")
        if value.endswith(".git"):
            return value
        if value.startswith(("https://", "http://")):
            return value + ".git"
        return value

    return normalize(actual) == normalize(expected)


def _command_detail(result: CommandResult) -> str:
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    detail = _REMOTE_CREDENTIAL_RE.sub(r"\1[redacted]@", detail)
    return detail[:237] + "..." if len(detail) > 240 else detail


__all__ = [
    "CommandResult",
    "CommandRunner",
    "CodexPatcher",
    "PatchResources",
    "PatchResult",
    "SubprocessRunner",
    "patch_codex_core",
]
