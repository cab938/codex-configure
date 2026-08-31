from __future__ import annotations

import io
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_configure.cli import Console, run_init_command, run_launch, run_launch_context, run_status
from codex_configure.errors import UserFacingError
from codex_configure.launch_context import (
    LaunchSettings,
    initialize_root,
    load_launch_context,
    rooted_environment,
    write_launch_configuration,
)
from codex_configure.runtime import ConfigManager


class LaunchRootTests(unittest.TestCase):
    def test_root_layout_and_environment_are_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            context = initialize_root(root)
            write_launch_configuration(
                context.state_dir,
                context.codex_home,
                LaunchSettings("stock", "openai"),
                root=True,
            )

            loaded = load_launch_context(context.state_dir)
            short_runtime = Path(temporary) / "short"
            environment = rooted_environment(
                loaded,
                {
                    "PATH": "/usr/bin",
                    "CODEX_CONFIGURE_RUNTIME_ROOT": str(short_runtime),
                },
            )

            self.assertEqual(loaded.root, root)
            self.assertEqual(loaded.codex_home, root / ".codex-configure" / "codex-home")
            self.assertEqual(environment["CODEX_HOME"], str(loaded.codex_home))
            self.assertEqual(environment["XDG_CONFIG_HOME"], str(context.state_dir / "xdg" / "config"))
            self.assertEqual(environment["XDG_RUNTIME_DIR"], str(short_runtime / "xdg"))
            self.assertEqual(environment["TMPDIR"], str(short_runtime / "tmp"))
            self.assertEqual(
                environment["CODEX_CHROME_NATIVE_HOSTS_MANIFEST"],
                str(context.state_dir / "chrome" / "chrome-native-hosts-v2.json"),
            )
            self.assertTrue((context.state_dir / "root.toml").is_file())
            self.assertEqual(
                stat.S_IMODE((context.state_dir / "launch.sh").stat().st_mode),
                0o700,
            )
            self.assertEqual(stat.S_IMODE((context.state_dir / "xdg").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((context.state_dir / "chrome").stat().st_mode), 0o700)

    def test_init_can_create_an_openai_only_launch_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            cwd.mkdir()
            home.mkdir()
            output = io.StringIO()

            result = run_init_command(
                cwd,
                home,
                Console(io.StringIO("1\n1\n2\n"), output),
                {"HOME": str(home)},
            )

            context = load_launch_context(cwd / ".codex-configure")
            self.assertEqual(result, 0)
            self.assertTrue(ConfigManager(context.codex_home).is_initialized())
            self.assertEqual(context.settings, LaunchSettings("stock", "openai"))
            self.assertFalse((home / ".codex").exists())
            self.assertIn("Launch root:", output.getvalue())

    def test_status_is_read_only_when_nothing_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            cwd.mkdir()
            home.mkdir()
            output = io.StringIO()

            result = run_status(cwd, home, Console(io.StringIO(), output))

            self.assertEqual(result, 0)
            self.assertFalse((cwd / ".codex-configure").exists())
            self.assertFalse((home / ".codex").exists())
            self.assertFalse((home / ".codex-configure").exists())
            self.assertIn("Local launch root", output.getvalue())
            self.assertIn("Global configuration", output.getvalue())

    def test_status_rejects_incomplete_global_launch_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            global_state = home / ".codex-configure"
            cwd.mkdir()
            global_state.mkdir(parents=True)
            (global_state / "launch.toml").write_text(
                'schema_version = 1\nkind = "codex-configure-launch"\n'
                '[launch]\ncore = "stock"\nprovider = "openai"\n',
                encoding="utf-8",
            )
            output = io.StringIO()

            result = run_status(cwd, home, Console(io.StringIO(), output))

            self.assertEqual(result, 2)
            self.assertIn("Launch mode: invalid", output.getvalue())
            self.assertIn("does not declare context.codex_home", output.getvalue())

    @mock.patch("codex_configure.cli.os.execv")
    def test_launch_execs_exact_local_script_and_forwards_arguments(self, execv: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            cwd.mkdir()
            home.mkdir()
            context = initialize_root(cwd)
            write_launch_configuration(
                context.state_dir,
                context.codex_home,
                LaunchSettings("stock", "openai"),
                root=True,
            )

            run_launch(cwd, home, ("cli", "login"))

            launcher = str(context.state_dir / "launch.sh")
            execv.assert_called_once_with(launcher, [launcher, "cli", "login"])

    @mock.patch("codex_configure.cli.os.execv")
    def test_global_launcher_is_not_a_root_and_invalid_local_state_blocks_it(
        self, execv: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            cwd.mkdir()
            home.mkdir()
            result = run_init_command(
                cwd,
                home,
                Console(io.StringIO("2\n1\n2\n"), io.StringIO()),
                {"HOME": str(home)},
            )

            global_state = home / ".codex-configure"
            global_context = load_launch_context(global_state)
            self.assertEqual(result, 0)
            self.assertIsNone(global_context.root)
            self.assertFalse((global_state / "root.toml").exists())
            run_launch(cwd, home, ("desktop",))
            launcher = str(global_state / "launch.sh")
            execv.assert_called_once_with(launcher, [launcher, "desktop"])

            (cwd / ".codex-configure").mkdir()
            with self.assertRaisesRegex(UserFacingError, "refusing global fallback"):
                run_launch(cwd, home, ("desktop",))

    def test_internal_launch_maps_config_and_forwards_cli_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            context = initialize_root(root)
            write_launch_configuration(
                context.state_dir,
                context.codex_home,
                LaunchSettings("stock", "openai"),
                root=True,
            )
            output = io.StringIO()

            with mock.patch("codex_configure.cli.run_run", return_value=0) as run:
                result = run_launch_context(
                    context.state_dir,
                    ("cli", "login", "--device-auth"),
                    Console(io.StringIO(), output),
                    {"PWD": "/callers/workspace", "PATH": "/usr/bin"},
                )

            self.assertEqual(result, 0)
            self.assertEqual(run.call_args.args[1], "openai/cli")
            self.assertEqual(run.call_args.kwargs["app_args"], ("login", "--device-auth"))
            child_environment = run.call_args.args[3]
            self.assertEqual(child_environment["PWD"], "/callers/workspace")
            self.assertEqual(child_environment["CODEX_HOME"], str(context.codex_home))


if __name__ == "__main__":
    unittest.main()
