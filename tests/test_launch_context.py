from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_configure.cli import Console, run_init_command, run_launch, run_launch_context, run_status
from codex_configure.errors import UserFacingError
from codex_configure.launch_context import (
    LaunchSettings,
    copy_openai_auth,
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
                Console(io.StringIO("1\n1\n3\n2\n1\n"), output),
                {"HOME": str(home)},
            )

            context = load_launch_context(cwd / ".codex-configure")
            self.assertEqual(result, 0)
            self.assertTrue(ConfigManager(context.codex_home).is_initialized())
            self.assertEqual(context.settings, LaunchSettings("stock", "openai"))
            self.assertFalse((home / ".codex").exists())
            self.assertIn("Launch root:", output.getvalue())

    def test_dynamic_choice_installs_core_for_only_that_launch_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            cwd.mkdir()
            home.mkdir()
            installer = mock.Mock()
            install_directory = cwd / ".codex-configure" / "cores" / "test-core"
            installer.install.return_value = mock.Mock(
                reused=False,
                version="0.4.0",
                target="linux-x86_64",
                install_directory=install_directory,
            )

            result = run_init_command(
                cwd,
                home,
                Console(io.StringIO("1\n3\n1\n"), io.StringIO()),
                {"HOME": str(home)},
                installer=installer,
            )

            context = load_launch_context(cwd / ".codex-configure")
            self.assertEqual(result, 0)
            self.assertEqual(context.settings, LaunchSettings("dynamic", "openai"))
            installer.install.assert_called_once_with()
            self.assertTrue(str(install_directory).startswith(str(cwd / ".codex-configure")))
            self.assertFalse((home / ".codex-configure").exists())

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
            self.assertIn("Launch root (exact current directory)", output.getvalue())
            self.assertNotIn("Global configuration", output.getvalue())

    def test_status_ignores_legacy_global_launch_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            global_state = home / ".codex-configure"
            cwd.mkdir()
            global_state.mkdir(parents=True)
            write_launch_configuration(
                global_state,
                home / ".codex",
                LaunchSettings("stock", "openai"),
                root=False,
            )
            output = io.StringIO()

            result = run_status(cwd, home, Console(io.StringIO(), output))

            self.assertEqual(result, 0)
            self.assertIn("Status: not configured", output.getvalue())
            self.assertNotIn(str(global_state / "launch.sh"), output.getvalue())

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
    def test_legacy_global_launcher_is_never_a_fallback(
        self, execv: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            cwd.mkdir()
            home.mkdir()
            global_state = home / ".codex-configure"
            write_launch_configuration(
                global_state,
                home / ".codex",
                LaunchSettings("stock", "openai"),
                root=False,
            )

            with self.assertRaisesRegex(UserFacingError, "exact current directory"):
                run_launch(cwd, home, ("desktop",))
            execv.assert_not_called()

            (cwd / ".codex-configure").mkdir()
            with self.assertRaisesRegex(UserFacingError, "is not a launch root"):
                run_launch(cwd, home, ("desktop",))

    def test_init_cancel_does_not_create_a_launch_root(self) -> None:
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
                Console(io.StringIO("2\n"), output),
                {"HOME": str(home)},
            )

            self.assertEqual(result, 0)
            self.assertFalse((cwd / ".codex-configure").exists())
            self.assertIn("Cancelled.", output.getvalue())

    @mock.patch("codex_configure.cli._detected_openai_auth_home")
    def test_init_copies_only_detected_openai_authentication(
        self,
        detected: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            home = base / "home"
            source_home = home / ".codex"
            cwd.mkdir()
            source_home.mkdir(parents=True)
            auth_payload = {"tokens": {"access_token": "synthetic-test-token"}}
            (source_home / "auth.json").write_text(
                json.dumps(auth_payload),
                encoding="utf-8",
            )
            (source_home / "config.toml").write_text("model = 'source-only'\n", encoding="utf-8")
            (source_home / "skills").mkdir()
            (source_home / "skills" / "source-only.md").write_text("not copied\n", encoding="utf-8")
            detected.return_value = source_home
            output = io.StringIO()

            result = run_init_command(
                cwd,
                home,
                Console(io.StringIO("1\n2\n3\n2\n1\n"), output),
                {"HOME": str(home)},
            )

            target_home = cwd / ".codex-configure" / "codex-home"
            self.assertEqual(result, 0)
            self.assertEqual(
                json.loads((target_home / "auth.json").read_text(encoding="utf-8")),
                auth_payload,
            )
            self.assertEqual(stat.S_IMODE((target_home / "auth.json").stat().st_mode), 0o600)
            self.assertFalse((target_home / "skills").exists())
            self.assertFalse((target_home / "config.toml").exists())
            self.assertIn(f"detected: {source_home} -> copy auth", output.getvalue())

    def test_auth_copy_refuses_to_overwrite_existing_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            target.mkdir()
            (source / "auth.json").write_text('{"source": true}\n', encoding="utf-8")
            destination = target / "auth.json"
            destination.write_text('{"target": true}\n', encoding="utf-8")

            with self.assertRaisesRegex(UserFacingError, "refusing to overwrite"):
                copy_openai_auth(source, target)

            self.assertEqual(destination.read_text(encoding="utf-8"), '{"target": true}\n')

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
            self.assertEqual(run.call_args.kwargs["core_home"], root)
            child_environment = run.call_args.args[3]
            self.assertEqual(child_environment["PWD"], "/callers/workspace")
            self.assertEqual(child_environment["CODEX_HOME"], str(context.codex_home))


if __name__ == "__main__":
    unittest.main()
