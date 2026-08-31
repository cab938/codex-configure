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
    run_init,
    run_run,
    run_setup_dynamic,
    run_restore,
)
from codex_configure.core_install import CoreInstaller
from codex_configure.errors import UserFacingError
from codex_configure.runtime import ConfigManager


def model(
    slug: str,
    display_name: str,
    status: str = "listed",
    *,
    selectable: bool = True,
) -> ModelChoice:
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
        selectable=selectable,
    )


class FakeCatalogService:
    def __init__(self) -> None:
        self.api_keys: list[str | None] = []
        self.result = CatalogResult(
            models=(
                model("gpt-5.6-sol", "GPT-5.6 Sol"),
                model("gpt-5.6-terra", "GPT-5.6 Terra", "verified"),
                model("gpt-5.6-luna", "GPT-5.6 Luna"),
            ),
            source="test catalog",
        )

    def discover(self, api_key: str | None = None) -> CatalogResult:
        self.api_keys.append(api_key)
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
        self.removed_environment: list[tuple[str, ...]] = []
        self.stopped_checks = 0

    def validate(self, target: str, requires_environment: bool = False) -> list[str]:
        self.validated.append(target)
        return [f"fake-{target}"]

    def ensure_clients_stopped(self) -> None:
        self.stopped_checks += 1

    def running_clients(self) -> list[str]:
        return []

    def launch(
        self,
        command: list[str],
        extra_environment: dict[str, str],
        remove_environment: tuple[str, ...] = (),
    ) -> int:
        self.launches.append((command, extra_environment))
        self.removed_environment.append(remove_environment)
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

    def save_profile(
        self,
        manager: ConfigManager,
        shortname: str,
        key: str,
        service: FakeCatalogService | None = None,
    ) -> None:
        service = service or FakeCatalogService()
        manager.initialize()
        choices = list(service.result.models)
        manager.save_umich_provider(
            shortname,
            key,
            service.build_selected_catalog(choices),
            selected_models=[choice.slug for choice in choices],
            default_model="gpt-5.6-terra",
            catalog_source=service.result.source,
        )

    def test_init_creates_named_provider_catalog_and_private_key(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        original = (home / "config.toml").read_text(encoding="utf-8")
        service = FakeCatalogService()
        output = io.StringIO()

        result = run_init(
            home,
            Console(io.StringIO("2\nteaching\ntest-secret\nall\n\n"), output),
            {},
            catalog_service=service,
        )

        self.assertEqual(result, 0)
        manager = ConfigManager(home)
        self.assertTrue(manager.is_initialized())
        self.assertEqual(service.api_keys, ["test-secret"])
        descriptor_path = manager.paths.providers / "teaching.toml"
        descriptor_text = descriptor_path.read_text(encoding="utf-8")
        descriptor = tomlkit.parse(descriptor_text)
        self.assertEqual(descriptor["model_catalog_json"], "../catalogs/teaching.json")
        self.assertEqual(
            descriptor["model_providers"]["teaching"]["env_http_headers"]["x-portkey-api-key"],
            "TEACHING_API_KEY",
        )
        catalog = json.loads((manager.paths.catalogs / "teaching.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["slug"] for entry in catalog["models"]],
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )
        self.assertEqual(stat.S_IMODE(manager.paths.env_file.stat().st_mode), 0o600)
        self.assertEqual(manager.load_credentials({}), {"TEACHING_API_KEY": "test-secret"})
        self.assertNotIn("test-secret", descriptor_text)
        self.assertNotIn("test-secret", json.dumps(catalog))
        self.assertNotIn("test-secret", output.getvalue())
        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), original)

    def test_two_profiles_have_distinct_keys_and_catalogs(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")
        self.save_profile(manager, "research-2026", "second-secret")

        self.assertEqual(
            [item.id for item in manager.list_providers(include_stock=False)],
            ["research-2026", "teaching"],
        )
        self.assertEqual(
            manager.load_credentials({}),
            {
                "RESEARCH_2026_API_KEY": "second-secret",
                "TEACHING_API_KEY": "first-secret",
            },
        )
        for shortname in ("teaching", "research-2026"):
            self.assertTrue((manager.paths.providers / f"{shortname}.toml").is_file())
            self.assertTrue((manager.paths.catalogs / f"{shortname}.json").is_file())

    def test_init_all_excludes_endpoint_models_without_codex_metadata(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        service = FakeCatalogService()
        service.result = CatalogResult(
            models=(
                model("gpt-5.6-terra", "GPT-5.6 Terra", "verified"),
                model("unrecognized-model", "unrecognized-model", "unsupported", selectable=False),
            ),
            source="test catalog",
        )
        output = io.StringIO()

        run_init(
            home,
            Console(io.StringIO("2\nsandbox\ntest-secret\nall\n\n"), output),
            {},
            catalog_service=service,
        )

        catalog = json.loads(
            (home / "codex-configure" / "catalogs" / "sandbox.json").read_text(encoding="utf-8")
        )
        self.assertEqual([entry["slug"] for entry in catalog["models"]], ["gpt-5.6-terra"])
        self.assertIn("unsupported by this Codex build", output.getvalue())

    def test_stock_named_run_uses_one_key_and_removes_patched_core(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "file-secret")
        launcher = FakeLauncher()
        output = io.StringIO()

        result = run_run(
            home,
            "teaching/cli",
            Console(io.StringIO(), output),
            {"CODEX_CLI_PATH": "/wrong/patched/codex"},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        self.assertEqual(launcher.stopped_checks, 1)
        self.assertEqual(launcher.launches, [(["fake-cli"], {"TEACHING_API_KEY": "file-secret"})])
        self.assertEqual(launcher.removed_environment, [("CODEX_CLI_PATH",)])
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(active["model_provider"], "teaching")
        self.assertNotIn("file-secret", (home / "config.toml").read_text(encoding="utf-8"))
        self.assertLess(
            output.getvalue().index("Profiles directory:"),
            output.getvalue().index("Launching teaching/cli"),
        )

    def test_missing_key_does_not_activate_named_profile(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "temporary-secret")
        manager.paths.env_file.unlink()
        before = (home / "config.toml").read_text(encoding="utf-8")

        with self.assertRaisesRegex(UserFacingError, "No credential found"):
            run_run(
                home,
                "teaching/cli",
                Console(io.StringIO(), io.StringIO()),
                {},
                manager=manager,
                launcher=FakeLauncher(),
            )

        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), before)

    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_dynamic_desktop_loads_keys_without_requiring_clients_stopped(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")
        self.save_profile(manager, "research", "second-secret")
        binary = home / "patched-codex"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o700)
        code_mode_host = home / "codex-code-mode-host"
        code_mode_host.write_text("#!/bin/sh\n", encoding="utf-8")
        code_mode_host.chmod(0o700)
        launcher = FakeLauncher()

        result = run_run(
            home,
            "desktop",
            Console(io.StringIO(), io.StringIO()),
            {"CODEX_CLI_PATH": str(binary)},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        self.assertEqual(launcher.stopped_checks, 0)
        self.assertEqual(launcher.validated, ["desktop"])
        self.assertEqual(
            launcher.launches,
            [
                (
                    ["fake-desktop"],
                    {
                        "RESEARCH_API_KEY": "second-secret",
                        "TEACHING_API_KEY": "first-secret",
                        "CODEX_CLI_PATH": str(binary.resolve()),
                    },
                )
            ],
        )
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertNotIn("model_provider", active)
        self.assertNotIn("model_catalog_json", active)

    @mock.patch("codex_configure.cli.sys.platform", "darwin")
    def test_macos_dynamic_desktop_launches_with_native_core(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")
        binary = home / "codex"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o700)
        code_mode_host = home / "codex-code-mode-host"
        code_mode_host.write_text("#!/bin/sh\n", encoding="utf-8")
        code_mode_host.chmod(0o700)
        launcher = FakeLauncher()

        result = run_run(
            home,
            "desktop",
            Console(io.StringIO(), io.StringIO()),
            {"CODEX_CLI_PATH": str(binary)},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        self.assertEqual(launcher.stopped_checks, 0)
        self.assertEqual(
            launcher.launches[0][1],
            {
                "TEACHING_API_KEY": "first-secret",
                "CODEX_CLI_PATH": str(binary.resolve()),
            },
        )

    @mock.patch("codex_configure.core_install.platform.machine", return_value="arm64")
    @mock.patch("codex_configure.core_install.platform.system", return_value="Darwin")
    @mock.patch("codex_configure.cli.sys.platform", "darwin")
    def test_macos_dynamic_desktop_finds_installed_release(
        self,
        system: mock.Mock,
        machine: mock.Mock,
    ) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")
        installed = CoreInstaller.versioned_directory(home, target="macos-arm64")
        installed.mkdir(parents=True)
        for name in ("codex", "codex-code-mode-host"):
            path = installed / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o700)
        (installed.parent / "current").symlink_to(installed.name, target_is_directory=True)
        launcher = FakeLauncher()

        run_run(
            home,
            "desktop",
            Console(io.StringIO(), io.StringIO()),
            {"HOME": str(home)},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(
            launcher.launches[0][1]["CODEX_CLI_PATH"],
            str((installed / "codex").resolve()),
        )

    @mock.patch("codex_configure.cli.sys.platform", "darwin")
    def test_macos_setup_dynamic_uses_platform_installer(self) -> None:
        installer = mock.Mock()
        installer.install.return_value = mock.Mock(
            reused=False,
            version="0.4.0",
            target="macos-arm64",
            install_directory=Path("/tmp/macos-core"),
        )
        output = io.StringIO()

        result = run_setup_dynamic(Console(io.StringIO(), output), {}, installer=installer)

        self.assertEqual(result, 0)
        installer.install.assert_called_once_with()
        self.assertIn("Installed Dynamic Core 0.4.0 for macos-arm64", output.getvalue())

    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_dynamic_desktop_finds_default_build_without_environment_override(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")
        release_dir = (
            home / ".codex-configure" / "codex-core" / "codex-rs" / "target" / "release"
        )
        release_dir.mkdir(parents=True)
        binary = release_dir / "codex"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o700)
        code_mode_host = release_dir / "codex-code-mode-host"
        code_mode_host.write_text("#!/bin/sh\n", encoding="utf-8")
        code_mode_host.chmod(0o700)
        launcher = FakeLauncher()

        result = run_run(
            home,
            "desktop",
            Console(io.StringIO(), io.StringIO()),
            {"HOME": str(home)},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            launcher.launches,
            [
                (
                    ["fake-desktop"],
                    {
                        "TEACHING_API_KEY": "first-secret",
                        "CODEX_CLI_PATH": str(binary.resolve()),
                    },
                )
            ],
        )

    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_dynamic_desktop_prefers_installed_release_over_source_build(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")

        installed = CoreInstaller.versioned_directory(home)
        installed.mkdir(parents=True)
        for name in ("codex", "codex-code-mode-host"):
            path = installed / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o700)
        (installed.parent / "current").symlink_to(installed.name, target_is_directory=True)

        source = home / ".codex-configure" / "codex-core" / "codex-rs" / "target" / "release"
        source.mkdir(parents=True)
        for name in ("codex", "codex-code-mode-host"):
            path = source / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o700)
        launcher = FakeLauncher()

        run_run(
            home,
            "desktop",
            Console(io.StringIO(), io.StringIO()),
            {"HOME": str(home)},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(
            launcher.launches[0][1]["CODEX_CLI_PATH"],
            str((installed / "codex").resolve()),
        )

    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_dynamic_run_rejects_missing_code_mode_host(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")
        binary = home / "patched-codex"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o700)

        with self.assertRaisesRegex(UserFacingError, "codex-code-mode-host"):
            run_run(
                home,
                "cli",
                Console(io.StringIO(), io.StringIO()),
                {"CODEX_CLI_PATH": str(binary)},
                manager=manager,
                launcher=FakeLauncher(),
            )

    def test_stock_openai_normalizes_model_saved_by_dynamic_core(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "first-secret")
        manager.activate_dynamic()

        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        active["model"] = "teaching::gpt-5.6-terra"
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")
        manager.activate_openai()

        restored = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertNotIn("model", restored)

        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        active["model"] = "openai::gpt-5.6-sol"
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")
        manager.activate_openai()
        restored = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(restored["model"], "gpt-5.6-sol")

    def test_run_requires_initialized_provider_layout(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)

        with self.assertRaisesRegex(UserFacingError, "codex-configure init"):
            run_run(
                home,
                "openai/cli",
                Console(io.StringIO(), io.StringIO()),
                {},
                launcher=FakeLauncher(),
            )

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

    def test_invalid_desktop_override_is_user_facing(self) -> None:
        with self.assertRaisesRegex(UserFacingError, "Invalid CODEX_DESKTOP_COMMAND"):
            Launcher({"CODEX_DESKTOP_COMMAND": "chatgpt\\"}).validate("desktop")

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which")
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_running_clients_checks_desktop_and_cli_names(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        which.side_effect = lambda name, path=None: f"/usr/bin/{name}"

        def result(command: list[str], **kwargs: object) -> mock.Mock:
            if command[0] == "/usr/bin/pgrep":
                active = command[-1] in {"chatgpt", "codex"}
                return mock.Mock(returncode=0 if active else 1, stdout="42\n" if active else "", stderr="")
            return mock.Mock(returncode=0, stdout="S\n", stderr="")

        run.side_effect = result

        launcher = Launcher({"PATH": "/usr/bin"})
        self.assertEqual(launcher.running_clients(), ["chatgpt", "codex"])
        with self.assertRaisesRegex(UserFacingError, "chatgpt, codex"):
            launcher.ensure_clients_stopped()

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which", side_effect=lambda name, path=None: f"/usr/bin/{name}")
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_running_clients_ignores_confirmed_zombies(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        def result(command: list[str], **kwargs: object) -> mock.Mock:
            if command[0] == "/usr/bin/pgrep":
                active = command[-1] == "ChatGPT"
                return mock.Mock(returncode=0 if active else 1, stdout="42\n" if active else "", stderr="")
            return mock.Mock(returncode=0, stdout="Z\n", stderr="")

        run.side_effect = result
        self.assertEqual(Launcher({"PATH": "/usr/bin"}).running_clients(), [])

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
