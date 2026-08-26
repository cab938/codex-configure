from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomlkit

from codex_configure.catalog import CatalogResult, ModelChoice
from codex_configure.cli import (
    Console,
    Launcher,
    parse_model_selection,
    run_doctor,
    run_interactive,
    run_restore,
)
from codex_configure.errors import UserFacingError
from codex_configure.runtime import ConfigManager


def model(slug: str, display_name: str, status: str = "listed") -> ModelChoice:
    return ModelChoice(
        slug=slug,
        display_name=display_name,
        status=status,
        catalog_entry={
            "slug": slug,
            "display_name": display_name,
            "description": f"{display_name} test model",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "medium", "description": "Balanced"}],
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 1,
            "context_window": 272000,
        },
    )


class FakeCatalogService:
    def __init__(self) -> None:
        self.result = CatalogResult(
            models=(
                model("gpt-5.6-sol", "GPT-5.6 Sol"),
                model("gpt-5.6-terra", "GPT-5.6 Terra", "verified"),
                model("gpt-5.6-luna", "GPT-5.6 Luna"),
            ),
            source="test catalog",
        )

    def discover(self) -> CatalogResult:
        return self.result

    def build_selected_catalog(self, models: list[ModelChoice]) -> dict[str, object]:
        return {
            "models": [
                {**choice.catalog_entry, "priority": index, "visibility": "list"}
                for index, choice in enumerate(models, start=1)
            ]
        }


class FakeLauncher:
    def __init__(self) -> None:
        self.validated: list[str] = []
        self.launches: list[tuple[list[str], dict[str, str]]] = []
        self.stopped_checks = 0

    def validate(self, target: str, requires_environment: bool = False) -> list[str]:
        self.validated.append(target)
        return [f"fake-{target}"]

    def ensure_clients_stopped(self) -> None:
        self.stopped_checks += 1

    def running_clients(self) -> list[str]:
        return []

    def launch(self, command: list[str], extra_environment: dict[str, str]) -> int:
        self.launches.append((command, extra_environment))
        return 0


class CliFlowTests(unittest.TestCase):
    def make_home(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name)
        (home / "config.toml").write_text(
            'model = "gpt-5.5"\n\n[features]\nexample = true\n\n'
            '[model_providers.existing]\nname = "Existing provider"\nbase_url = "http://127.0.0.1:1337/v1"\n',
            encoding="utf-8",
        )
        return temporary, home

    def test_openai_skips_provider_and_model_steps(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        output = io.StringIO()
        launcher = FakeLauncher()

        result = run_interactive(
            codex_home=home,
            console=Console(io.StringIO("1\n2\n"), output),
            environ={},
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        text = output.getvalue()
        self.assertNotIn("Choose provider", text)
        self.assertNotIn("Choose models", text)
        self.assertIn(f"Profiles directory: {home / 'codex-configure' / 'profiles'}", text)
        self.assertEqual(launcher.validated, ["cli"])
        self.assertEqual(launcher.stopped_checks, 1)
        self.assertEqual(launcher.launches, [(["fake-cli"], {})])
        self.assertEqual(
            (home / "config.toml").read_text(encoding="utf-8"),
            'model = "gpt-5.5"\n\n[features]\nexample = true\n\n'
            '[model_providers.existing]\nname = "Existing provider"\nbase_url = "http://127.0.0.1:1337/v1"\n',
        )

    def test_umich_multi_select_writes_catalog_and_default(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        output = io.StringIO()
        launcher = FakeLauncher()
        manager = ConfigManager(home)

        result = run_interactive(
            codex_home=home,
            console=Console(io.StringIO("2\n1\n1,3\n2\n1\n"), output),
            environ={"UMICH_TOOLKIT_API_KEY": "test-secret"},
            manager=manager,
            catalog_service=FakeCatalogService(),
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        catalog_path = Path(str(active["model_catalog_json"]))
        self.assertTrue(catalog_path.name.startswith("umich-openai-azure-"))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.assertEqual([entry["slug"] for entry in catalog["models"]], ["gpt-5.6-sol", "gpt-5.6-luna"])

        self.assertEqual(active["model"], "gpt-5.6-luna")
        self.assertEqual(active["model_provider"], "umich-toolkit")
        self.assertTrue(active["features"]["example"])
        self.assertEqual(active["model_providers"]["existing"]["name"], "Existing provider")
        self.assertEqual(
            active["model_providers"]["umich-toolkit"]["env_http_headers"]["x-portkey-api-key"],
            "UMICH_TOOLKIT_API_KEY",
        )
        self.assertNotIn("test-secret", (home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(launcher.validated, ["desktop"])
        self.assertEqual(launcher.stopped_checks, 1)
        self.assertEqual(launcher.launches[0][1], {"UMICH_TOOLKIT_API_KEY": "test-secret"})
        text = output.getvalue()
        location = text.index(f"Profiles directory: {manager.paths.profiles}")
        launching = text.index("Launching Codex Desktop")
        self.assertLess(location, launching)

    def test_umich_reads_default_private_env_file(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        credential_file = home / "codex-configure" / ".env"
        credential_file.parent.mkdir(parents=True)
        credential_file.write_text("UMICH_TOOLKIT_API_KEY=file-secret\n", encoding="utf-8")
        credential_file.chmod(0o600)
        launcher = FakeLauncher()

        result = run_interactive(
            codex_home=home,
            console=Console(io.StringIO("2\n1\n\n\n2\n"), io.StringIO()),
            environ={},
            catalog_service=FakeCatalogService(),
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        self.assertEqual(launcher.launches[0][1], {"UMICH_TOOLKIT_API_KEY": "file-secret"})
        self.assertNotIn("file-secret", (home / "config.toml").read_text(encoding="utf-8"))

    def test_saved_selection_is_checked_on_next_run(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        catalog_service = FakeCatalogService()
        first_output = io.StringIO()
        run_interactive(
            codex_home=home,
            console=Console(io.StringIO("2\n1\n1,3\n1\n2\n"), first_output),
            environ={"UMICH_TOOLKIT_API_KEY": "test-secret"},
            prepare_only=True,
            manager=manager,
            catalog_service=catalog_service,
            launcher=FakeLauncher(),
        )

        second_output = io.StringIO()
        run_interactive(
            codex_home=home,
            console=Console(io.StringIO("2\n1\n\n\n2\n"), second_output),
            environ={"UMICH_TOOLKIT_API_KEY": "test-secret"},
            prepare_only=True,
            manager=manager,
            catalog_service=catalog_service,
            launcher=FakeLauncher(),
        )
        profile = tomlkit.parse(
            (home / "codex-configure" / "profiles" / "umich" / "profile.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(list(profile["selected_models"]), ["gpt-5.6-sol", "gpt-5.6-luna"])
        self.assertIn("[ ] 2. GPT-5.6 Terra", second_output.getvalue())

    def test_prepare_only_still_checks_for_running_clients(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        launcher = FakeLauncher()

        result = run_interactive(
            codex_home=home,
            console=Console(io.StringIO("1\n1\n"), io.StringIO()),
            environ={},
            prepare_only=True,
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        self.assertEqual(launcher.validated, [])
        self.assertEqual(launcher.stopped_checks, 1)

    def test_openai_restore_adopts_desktop_changes_without_losing_original(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        service = FakeCatalogService()
        original = (home / "config.toml").read_text(encoding="utf-8")

        manager.initialize()
        manager.activate_umich(
            list(service.result.models),
            "gpt-5.6-terra",
            service.build_selected_catalog(list(service.result.models)),
            service.result.source,
        )
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        active["desktop"] = {"conversationDetailMode": "STEPS_PROSE"}
        active["plugins"] = {"browser@openai-bundled": {"enabled": True}}
        active["projects"] = {"/tmp/example": {"trust_level": "trusted"}}
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")

        manager.activate_openai()

        restored_text = (home / "config.toml").read_text(encoding="utf-8")
        restored = tomlkit.parse(restored_text)
        self.assertEqual(restored["model"], "gpt-5.5")
        self.assertNotIn("model_provider", restored)
        self.assertNotIn("model_catalog_json", restored)
        self.assertEqual(restored["desktop"]["conversationDetailMode"], "STEPS_PROSE")
        self.assertTrue(restored["plugins"]["browser@openai-bundled"]["enabled"])
        self.assertEqual(restored["projects"]["/tmp/example"]["trust_level"], "trusted")
        self.assertNotIn("umich-toolkit", restored["model_providers"])
        self.assertEqual(manager.paths.base_config.read_text(encoding="utf-8"), restored_text)
        self.assertEqual(manager.paths.original_config.read_text(encoding="utf-8"), original)

    def test_external_provider_change_is_still_rejected(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        service = FakeCatalogService()
        manager.initialize()
        manager.activate_umich(
            list(service.result.models),
            "gpt-5.6-terra",
            service.build_selected_catalog(list(service.result.models)),
            service.result.source,
        )
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        active["model_provider"] = "unexpected-provider"
        changed = tomlkit.dumps(active)
        (home / "config.toml").write_text(changed, encoding="utf-8")

        with self.assertRaisesRegex(UserFacingError, "changed provider routing"):
            manager.activate_openai()

        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), changed)

    def test_interrupted_switch_rolls_back_on_next_start(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        service = FakeCatalogService()
        original = (home / "config.toml").read_text(encoding="utf-8")
        manager.initialize()
        real_atomic_write = manager._atomic_write

        def interrupt_before_state(path: Path, text: str) -> None:
            if path == manager.paths.state and manager.paths.transaction.exists():
                raise RuntimeError("simulated interruption")
            real_atomic_write(path, text)

        with mock.patch.object(manager, "_atomic_write", side_effect=interrupt_before_state):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                manager.activate_umich(
                    list(service.result.models),
                    "gpt-5.6-terra",
                    service.build_selected_catalog(list(service.result.models)),
                    service.result.source,
                )

        self.assertTrue(manager.paths.transaction.exists())
        self.assertEqual(
            tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))["model_provider"],
            "umich-toolkit",
        )

        recovered = ConfigManager(home)
        recovered.initialize()

        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), original)
        self.assertFalse(recovered.paths.transaction.exists())
        self.assertTrue(recovered.doctor().healthy)

    def test_interrupted_switch_after_commit_is_finalized_on_next_start(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        service = FakeCatalogService()
        manager.initialize()
        real_atomic_write = manager._atomic_write

        def interrupt_before_last_good(path: Path, text: str) -> None:
            if path == manager.paths.last_good_config and manager.paths.transaction.exists():
                raise RuntimeError("simulated interruption")
            real_atomic_write(path, text)

        with mock.patch.object(manager, "_atomic_write", side_effect=interrupt_before_last_good):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                manager.activate_umich(
                    list(service.result.models),
                    "gpt-5.6-terra",
                    service.build_selected_catalog(list(service.result.models)),
                    service.result.source,
                )

        recovered = ConfigManager(home)
        recovered.initialize()

        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(active["model_provider"], "umich-toolkit")
        self.assertFalse(recovered.paths.transaction.exists())
        self.assertTrue(recovered.doctor().healthy)

    def test_failed_umich_reselection_keeps_prior_catalog_and_routing(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        service = FakeCatalogService()
        all_models = list(service.result.models)
        manager.initialize()
        manager.activate_umich(
            all_models,
            "gpt-5.6-terra",
            service.build_selected_catalog(all_models),
            service.result.source,
        )
        prior_active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        prior_catalog = Path(str(prior_active["model_catalog_json"]))
        prior_catalog_text = prior_catalog.read_text(encoding="utf-8")
        real_atomic_write = manager._atomic_write

        def interrupt_before_state(path: Path, text: str) -> None:
            if path == manager.paths.state and manager.paths.transaction.exists():
                raise RuntimeError("simulated interruption")
            real_atomic_write(path, text)

        with mock.patch.object(manager, "_atomic_write", side_effect=interrupt_before_state):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                manager.activate_umich(
                    [all_models[0]],
                    "gpt-5.6-sol",
                    service.build_selected_catalog([all_models[0]]),
                    service.result.source,
                )

        recovered = ConfigManager(home)
        recovered.initialize()
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(active["model"], "gpt-5.6-terra")
        self.assertEqual(Path(str(active["model_catalog_json"])), prior_catalog)
        self.assertEqual(prior_catalog.read_text(encoding="utf-8"), prior_catalog_text)

        active["desktop"] = {"example": True}
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")
        recovered.activate_openai()
        restored = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertTrue(restored["desktop"]["example"])

    def test_restore_uses_managed_or_original_openai_snapshot(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        service = FakeCatalogService()
        original = (home / "config.toml").read_text(encoding="utf-8")
        manager.initialize()
        manager.activate_umich(
            list(service.result.models),
            "gpt-5.6-terra",
            service.build_selected_catalog(list(service.result.models)),
            service.result.source,
        )
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        active["desktop"] = {"example": True}
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")

        source = manager.restore_openai()
        restored = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(source, manager.paths.base_config)
        self.assertTrue(restored["desktop"]["example"])

        manager.restore_openai(original=True)
        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), original)

    def test_doctor_is_read_only_before_initialization(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)

        report = manager.doctor()

        self.assertFalse(report.healthy)
        self.assertFalse(manager.paths.root.exists())

    def test_doctor_and_restore_command_flows(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        manager.initialize()
        launcher = FakeLauncher()
        doctor_output = io.StringIO()

        self.assertEqual(
            run_doctor(home, Console(io.StringIO(), doctor_output), {}, manager=manager, launcher=launcher),
            0,
        )
        self.assertIn("Result: healthy", doctor_output.getvalue())

        restore_output = io.StringIO()
        self.assertEqual(
            run_restore(
                home,
                Console(io.StringIO(), restore_output),
                {},
                manager=manager,
                launcher=launcher,
            ),
            0,
        )
        self.assertIn("Environment: OpenAI", restore_output.getvalue())
        self.assertEqual(launcher.stopped_checks, 1)

    @unittest.skipIf(os.name == "nt", "POSIX permission behavior")
    def test_existing_codex_home_permissions_are_unchanged(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        home.chmod(0o755)

        ConfigManager(home).initialize()

        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((home / "codex-configure").stat().st_mode), 0o700)


class SelectionParserTests(unittest.TestCase):
    def test_all_ranges_and_defaults(self) -> None:
        self.assertEqual(parse_model_selection("all", 4, [2]), [1, 2, 3, 4])
        self.assertEqual(parse_model_selection("1,3-4", 4, [2]), [1, 3, 4])
        self.assertEqual(parse_model_selection("", 4, [2, 4]), [2, 4])

    def test_invalid_selection(self) -> None:
        with self.assertRaises(ValueError):
            parse_model_selection("0,2", 4, [1])
        with self.assertRaises(ValueError):
            parse_model_selection("3-1", 4, [1])


class LauncherTests(unittest.TestCase):
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    @mock.patch("codex_configure.cli.shutil.which")
    def test_linux_desktop_finds_chatgpt(self, which: mock.Mock) -> None:
        which.side_effect = lambda name, path=None: "/usr/bin/chatgpt" if name == "chatgpt" else None

        self.assertEqual(Launcher({"PATH": "/usr/bin"}).validate("desktop"), ["/usr/bin/chatgpt"])

    @mock.patch("codex_configure.cli.sys.platform", "darwin")
    @mock.patch("codex_configure.cli.Path.is_file", return_value=True)
    def test_macos_desktop_uses_chatgpt_bundle_executable(self, is_file: mock.Mock) -> None:
        self.assertEqual(
            Launcher({"PATH": "/usr/bin", "HOME": "/Users/test"}).validate("desktop"),
            ["/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"],
        )

    @mock.patch("codex_configure.cli.sys.platform", "darwin")
    @mock.patch("codex_configure.cli.Path.is_file", return_value=False)
    @mock.patch("codex_configure.cli.shutil.which", return_value="/usr/bin/open")
    def test_macos_open_fallback_is_rejected_for_umich(
        self, which: mock.Mock, is_file: mock.Mock
    ) -> None:
        with self.assertRaisesRegex(UserFacingError, "cannot reliably pass"):
            Launcher({"PATH": "/usr/bin", "HOME": "/Users/test"}).validate(
                "desktop", requires_environment=True
            )

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which", return_value="/usr/bin/pgrep")
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_running_clients_checks_desktop_and_cli_names(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        def result(command: list[str], **kwargs: object) -> mock.Mock:
            return mock.Mock(returncode=0 if command[-1] in {"chatgpt", "codex"} else 1)

        run.side_effect = result

        launcher = Launcher({"PATH": "/usr/bin"})
        self.assertEqual(launcher.running_clients(), ["chatgpt", "codex"])
        with self.assertRaisesRegex(UserFacingError, "chatgpt, codex"):
            launcher.ensure_clients_stopped()

    @mock.patch("codex_configure.cli.shutil.which", return_value=None)
    def test_missing_pgrep_blocks_switch(self, which: mock.Mock) -> None:
        with self.assertRaisesRegex(UserFacingError, "pgrep is unavailable"):
            Launcher({"PATH": "/empty"}).ensure_clients_stopped()

    @mock.patch("codex_configure.cli.subprocess.Popen")
    def test_chatgpt_launch_is_detached(self, popen: mock.Mock) -> None:
        launcher = Launcher({"PATH": "/usr/bin"})

        self.assertEqual(launcher.launch(["/usr/bin/chatgpt"], {"PROFILE": "umich"}), 0)
        popen.assert_called_once_with(
            ["/usr/bin/chatgpt"],
            env={"PATH": "/usr/bin", "PROFILE": "umich"},
            start_new_session=True,
        )


if __name__ == "__main__":
    unittest.main()
