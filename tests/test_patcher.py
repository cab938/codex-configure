from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_configure.patcher import CodexPatcher, CommandResult


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str] | None] = []

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        del cwd
        command = tuple(args)
        self.commands.append(command)
        self.environments.append(environment)
        if command == ("rustc", "--version"):
            return CommandResult(command, 0, "rustc 1.94.0 (test)\n")
        return CommandResult(command, 0)


class PatcherBuildTests(unittest.TestCase):
    def test_build_includes_code_mode_host_companion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            manifest = worktree / "codex-rs" / "Cargo.toml"
            manifest.parent.mkdir()
            manifest.write_text("[workspace]\n", encoding="utf-8")
            runner = RecordingRunner()
            patcher = CodexPatcher(runner=runner)

            v8_environment = {
                "RUSTY_V8_ARCHIVE": "/tmp/test-v8.a.gz",
                "RUSTY_V8_SRC_BINDING_PATH": "/tmp/test-v8.rs",
            }
            with mock.patch.object(
                patcher,
                "_resolve_v8_environment",
                return_value=v8_environment,
            ):
                patcher._build(  # noqa: SLF001 - focused regression check for the build contract.
                    worktree,
                    SimpleNamespace(minimum_rust_version="1.94.0"),
                )

        self.assertIn(
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
            runner.commands,
        )
        self.assertEqual(runner.environments[-1], v8_environment)


if __name__ == "__main__":
    unittest.main()
