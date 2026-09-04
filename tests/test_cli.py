from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import tomlkit

from codex_configure.catalog import CatalogResult, CatalogService, ModelChoice
from codex_configure.cli import (
    Console,
    Launcher,
    main,
    parse_model_selection,
    run_doctor,
    run_init,
    run_run,
    run_setup_dynamic,
    run_restore,
)
from codex_configure.core_install import CoreInstaller
from codex_configure.errors import UserFacingError
from codex_configure.known_catalog import KnownCatalogProvenance
from codex_configure.known_catalog import KNOWN_LOCAL_CATALOG_URL, MODELS_DEV_MODELS_URL
from codex_configure.launch_context import LaunchSettings, initialize_root, write_launch_configuration
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


class FakeLocalCatalogService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.result = CatalogResult(
            models=(
                ModelChoice(
                    slug="local-coder-small",
                    display_name="local-coder-small",
                    status="tested",
                    catalog_entry={
                        "slug": "local-coder-small",
                        "display_name": "local-coder-small",
                        "description": "Local test model",
                        "context_window": 8192,
                        "input_modalities": ["text"],
                    },
                    badges=("context 8,192", "tools tested"),
                ),
                ModelChoice(
                    slug="local-coder-large",
                    display_name="local-coder-large",
                    status="known",
                    catalog_entry={
                        "slug": "local-coder-large",
                        "display_name": "local-coder-large",
                        "description": "Local test model",
                        "input_modalities": ["text"],
                    },
                ),
                ModelChoice(
                    slug="local-coder-unverified",
                    display_name="local-coder-unverified",
                    status="unverified",
                    catalog_entry={
                        "slug": "local-coder-unverified",
                        "display_name": "local-coder-unverified",
                        "description": "Local endpoint-only model",
                        "input_modalities": ["text"],
                    },
                ),
                ModelChoice(
                    slug="local-embedding",
                    display_name="local-embedding",
                    status="non-generation",
                    catalog_entry={},
                    selectable=False,
                ),
            ),
            source="http://127.0.0.1:1337/v1/models",
            known_catalog=KnownCatalogProvenance(
                state="fresh",
                sha256="a" * 64,
                etag='"test-etag"',
                fetched_at="2026-09-03T12:00:00Z",
            ),
        )

    def discover_local(self, base_url: str, api_key: str | None = None) -> CatalogResult:
        self.calls.append((base_url, api_key))
        return self.result

    def build_local_catalog(self, models: list[ModelChoice]) -> dict[str, object]:
        return {
            "models": [
                {**choice.catalog_entry, "priority": index}
                for index, choice in enumerate(models, start=1)
            ]
        }


class FakeLauncher:
    def __init__(self) -> None:
        self.validated: list[str] = []
        self.launches: list[tuple[list[str], dict[str, str]]] = []
        self.removed_environment: list[tuple[str, ...]] = []
        self.stopped_checks = 0
        self.stop_boundaries: list[tuple[Path | None, Path | None]] = []

    def validate(self, target: str, requires_environment: bool = False) -> list[str]:
        self.validated.append(target)
        return [f"fake-{target}"]

    def ensure_clients_stopped(
        self,
        target_codex_home: Path | None = None,
        *,
        target_root: Path | None = None,
    ) -> None:
        self.stopped_checks += 1
        self.stop_boundaries.append((target_codex_home, target_root))

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
            Console(io.StringIO("2\nteaching\ntest-secret\nall\n\n5\n"), output),
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

    def test_init_repeats_until_two_named_profiles_are_ready(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        service = FakeCatalogService()
        output = io.StringIO()

        result = run_init(
            home,
            Console(
                io.StringIO(
                    "2\nteaching\nfirst-secret\nall\n\n"
                    "3\nresearch\nsecond-secret\nall\n\n"
                    "6\n"
                ),
                output,
            ),
            {},
            catalog_service=service,
        )

        manager = ConfigManager(home)
        self.assertEqual(result, 0)
        self.assertEqual(
            [item.id for item in manager.list_providers(include_stock=False)],
            ["research", "teaching"],
        )
        self.assertEqual(service.api_keys, ["first-secret", "second-secret"])
        self.assertIn("research (U-M GPT Toolkit", output.getvalue())
        self.assertIn("teaching (U-M GPT Toolkit", output.getvalue())

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
            Console(io.StringIO("2\nsandbox\ntest-secret\nall\n\n5\n"), output),
            {},
            catalog_service=service,
        )

        catalog = json.loads(
            (home / "codex-configure" / "catalogs" / "sandbox.json").read_text(encoding="utf-8")
        )
        self.assertEqual([entry["slug"] for entry in catalog["models"]], ["gpt-5.6-terra"])
        self.assertIn("unsupported by this Codex build", output.getvalue())

    def test_init_adds_keyed_local_responses_models(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        service = FakeLocalCatalogService()
        output = io.StringIO()

        result = run_init(
            home,
            Console(
                io.StringIO("3\nlocal\n\nlocal-secret\n1-2\n\n5\n"),
                output,
            ),
            {},
            catalog_service=service,  # type: ignore[arg-type]
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            service.calls,
            [("http://127.0.0.1:1337/v1", "local-secret")],
        )
        manager = ConfigManager(home)
        descriptor_text = (manager.paths.providers / "local.toml").read_text(encoding="utf-8")
        descriptor = tomlkit.parse(descriptor_text)
        local_provider = descriptor["model_providers"]["local"]
        self.assertEqual(descriptor["kind"], "local-responses")
        self.assertEqual(local_provider["base_url"], "http://127.0.0.1:1337/v1")
        self.assertEqual(local_provider["env_key"], "LOCAL_API_KEY")
        self.assertNotIn("env_http_headers", local_provider)
        self.assertEqual(manager.load_credentials({}), {"LOCAL_API_KEY": "local-secret"})
        catalog = json.loads((manager.paths.catalogs / "local.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["slug"] for entry in catalog["models"]],
            ["local-coder-small", "local-coder-large"],
        )
        self.assertNotIn("local-secret", descriptor_text)
        self.assertNotIn("local-secret", json.dumps(catalog))
        profile = tomlkit.parse(
            (manager.paths.profiles / "local" / "profile.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(profile["schema_version"], 2)
        self.assertEqual(profile["known_catalog"]["state"], "fresh")
        self.assertEqual(profile["known_catalog"]["sha256"], "a" * 64)
        self.assertIn("local-coder-small [tested; context 8,192; tools tested]", output.getvalue())
        self.assertIn("local-coder-large [known]", output.getvalue())
        self.assertIn("local-coder-unverified [unverified]", output.getvalue())

    def test_local_setup_joins_a_fake_responses_server_to_the_owned_catalog(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        requests: list[tuple[str, str | None]] = []
        endpoint_payload = json.dumps(
            {
                "data": [
                    {"id": "local-tested", "meta": {"n_ctx": 16384}},
                    {"id": "local-known"},
                    {"id": "local-unverified"},
                    {"id": "local-embedding"},
                ]
            }
        ).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(endpoint_payload)))
                self.end_headers()
                self.wfile.write(endpoint_payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}/v1"
        known_payload = {
            "schema_version": 1,
            "generated_at": "2026-09-03T12:00:00Z",
            "models_dev_source": {
                "url": MODELS_DEV_MODELS_URL,
                "retrieved_at": "2026-09-03T12:00:00Z",
                "sha256": "a" * 64,
            },
            "models": [
                {
                    "endpoint_id": "local-tested",
                    "models_dev_id": "example/tested",
                    "display_name": "Local Tested",
                    "description": "Tested local model",
                    "reported": {"input_modalities": ["text"], "context_window": 32768},
                    "tested": {
                        "tested_at": "2026-09-03T12:00:00Z",
                        "probe_version": "1",
                        "runtime": {"name": "fake", "version": "1"},
                        "checks": {
                            "model_list": "pass",
                            "responses_streaming": "pass",
                            "standard_tools": "pass",
                            "vision": "not_run",
                            "reasoning_efforts": {},
                            "reasoning_summary": "not_run",
                        },
                    },
                },
                {
                    "endpoint_id": "local-known",
                    "models_dev_id": "example/known",
                    "display_name": "Local Known",
                    "description": "Known local model",
                    "reported": {"input_modalities": ["text"]},
                },
            ],
        }
        remote_requests: list[urllib.request.Request] = []
        real_urlopen = urllib.request.urlopen

        def open_request(request: urllib.request.Request, timeout: float):
            if request.full_url == KNOWN_LOCAL_CATALOG_URL:
                remote_requests.append(request)
                response = io.BytesIO(json.dumps(known_payload).encode("utf-8"))
                response.headers = {"ETag": '"integration"'}  # type: ignore[attr-defined]
                return response
            return real_urlopen(request, timeout=timeout)

        output = io.StringIO()
        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen", side_effect=open_request
        ):
            result = run_init(
                home,
                Console(
                    io.StringIO(
                        f"3\nlocal\n{base_url}\nendpoint-key\n2-4\n2\n5\n"
                    ),
                    output,
                ),
                {},
            )

        self.assertEqual(result, 0)
        self.assertEqual(requests, [("/v1/models", "Bearer endpoint-key")])
        self.assertIsNone(remote_requests[0].get_header("Authorization"))
        catalog = json.loads(
            (home / "codex-configure/catalogs/local.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [entry["slug"] for entry in catalog["models"]],
            ["local-known", "local-tested", "local-unverified"],
        )
        self.assertEqual(catalog["models"][1]["context_window"], 16384)
        profile = tomlkit.parse(
            (home / "codex-configure/profiles/local/profile.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(profile["default_model"], "local-tested")
        self.assertEqual(profile["known_catalog"]["state"], "fresh")
        self.assertIn("Local Tested (local-tested) [tested", output.getvalue())
        self.assertIn("Local Known (local-known) [known]", output.getvalue())
        self.assertIn("local-unverified [unverified]", output.getvalue())

    def test_local_reconfiguration_preserves_v1_selection_and_default(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        service = FakeLocalCatalogService()
        manager = ConfigManager(home)
        manager.save_local_provider(
            "local",
            "http://127.0.0.1:1337/v1",
            "stored-secret",
            service.build_local_catalog(list(service.result.models[:2])),
            selected_models=["local-coder-small", "local-coder-large"],
            default_model="local-coder-large",
            catalog_source=service.result.source,
        )
        profile_path = manager.paths.profiles / "local" / "profile.toml"
        profile = tomlkit.parse(profile_path.read_text(encoding="utf-8"))
        profile["schema_version"] = 1
        profile.pop("known_catalog", None)
        profile_path.write_text(tomlkit.dumps(profile), encoding="utf-8")
        output = io.StringIO()

        result = run_init(
            home,
            Console(io.StringIO("2\n\n\n\n\n5\n"), output),
            {},
            catalog_service=service,  # type: ignore[arg-type]
        )

        self.assertEqual(result, 0)
        rewritten = tomlkit.parse(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(rewritten["schema_version"], 2)
        self.assertEqual(
            list(rewritten["selected_models"]),
            ["local-coder-small", "local-coder-large"],
        )
        self.assertEqual(rewritten["default_model"], "local-coder-large")
        self.assertEqual(rewritten["known_catalog"]["state"], "fresh")

    def test_unkeyed_local_profile_runs_without_a_credential(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        manager.save_local_provider(
            "local",
            "http://127.0.0.1:1337/v1",
            None,
            {
                "models": [
                    {
                        "slug": "local-coder",
                        "display_name": "local-coder",
                        "input_modalities": ["text"],
                        "priority": 1,
                    }
                ]
            },
        )
        launcher = FakeLauncher()

        result = run_run(
            home,
            "local/cli",
            Console(io.StringIO(), io.StringIO()),
            {},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(result, 0)
        self.assertEqual(launcher.launches, [(["fake-cli"], {})])
        self.assertEqual(launcher.validated, ["cli"])
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(active["model_provider"], "local")
        self.assertEqual(active["model"], "local-coder")
        self.assertNotIn("model_catalog_json", active)

    def test_discovery_filters_non_generation_models_and_preserves_known_context(self) -> None:
        payload = {
            "data": [
                {"id": "coder", "meta": {"n_ctx": 16384}},
                {"id": "text-embedding-model"},
                {"id": "reranker-model"},
            ]
        }
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        with mock.patch(
            "codex_configure.catalog.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("catalog offline"), response],
        ):
            result = CatalogService(Path("/tmp/codex-home")).discover_local(
                "http://127.0.0.1:1337/v1",
                api_key="test-key",
            )

        self.assertEqual(result.source, "http://127.0.0.1:1337/v1/models")
        self.assertEqual([model.slug for model in result.selectable_models], ["coder"])
        self.assertEqual(result.selectable_models[0].catalog_entry["context_window"], 16384)
        self.assertEqual(
            [model.status for model in result.models if not model.selectable],
            ["non-generation", "non-generation"],
        )

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
        self.assertEqual(launcher.stop_boundaries, [(home.resolve(), None)])
        self.assertEqual(launcher.launches, [(["fake-cli"], {"TEACHING_API_KEY": "file-secret"})])
        self.assertEqual(launcher.removed_environment, [("CODEX_CLI_PATH",)])
        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(active["model_provider"], "teaching")
        self.assertNotIn("file-secret", (home / "config.toml").read_text(encoding="utf-8"))
        self.assertLess(
            output.getvalue().index("Profiles directory:"),
            output.getvalue().index("Launching teaching/cli"),
        )

    def test_stock_named_run_scopes_lifecycle_to_its_launch_root(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        self.save_profile(manager, "teaching", "file-secret")
        launcher = FakeLauncher()
        launch_root = home.parent / "project-root"

        result = run_run(
            home,
            "teaching/cli",
            Console(io.StringIO(), io.StringIO()),
            {},
            manager=manager,
            launcher=launcher,
            launch_root=launch_root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            launcher.stop_boundaries,
            [(home.resolve(), launch_root.resolve())],
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
            core_home=home,
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

        result = run_setup_dynamic(
            Console(io.StringIO(), output),
            {},
            core_home=Path("/tmp/project-root"),
            installer=installer,
        )

        self.assertEqual(result, 0)
        installer.install.assert_called_once_with()
        self.assertIn("Installed Dynamic Core 0.4.0 for macos-arm64", output.getvalue())

    @mock.patch("codex_configure.core_install.CoreInstaller")
    def test_setup_dynamic_constructs_a_project_local_installer(
        self,
        installer_class: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            installer_class.return_value.install.return_value = mock.Mock(
                reused=False,
                version="0.4.0",
                target="linux-x86_64",
                install_directory=root / ".codex-configure" / "cores" / "test",
            )

            result = run_setup_dynamic(
                Console(io.StringIO(), io.StringIO()),
                {},
                core_home=root,
            )

            self.assertEqual(result, 0)
            installer_class.assert_called_once_with(home=root.resolve())

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
            core_home=home,
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
            core_home=home,
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
        active["model"] = "teaching → gpt-5.6-terra"
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")
        manager.activate_openai()

        restored = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertNotIn("model", restored)

        active = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        active["model"] = "openai → gpt-5.6-sol"
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")
        manager.activate_openai()
        restored = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(restored["model"], "gpt-5.6-sol")

        active["model"] = "openai::gpt-5.5"
        (home / "config.toml").write_text(tomlkit.dumps(active), encoding="utf-8")
        manager.activate_openai()
        restored = tomlkit.parse((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(restored["model"], "gpt-5.5")

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

    def test_doctor_keeps_live_client_advice_separate_from_configuration_health(self) -> None:
        temporary, home = self.make_home()
        self.addCleanup(temporary.cleanup)
        manager = ConfigManager(home)
        manager.initialize()
        launcher = FakeLauncher()
        launcher.running_clients = mock.Mock(return_value=["ChatGPT", "codex"])
        output = io.StringIO()

        result = run_doctor(
            home,
            Console(io.StringIO(), output),
            {},
            manager=manager,
            launcher=launcher,
        )

        self.assertEqual(result, 1)
        report = output.getvalue()
        self.assertIn("Managed configuration: healthy", report)
        self.assertIn(
            "[ADVISORY] Client lifecycle: detected ChatGPT Desktop, Codex CLI.",
            report,
        )
        self.assertIn("Profile switching and restore are blocked", report)
        self.assertIn("Dynamic Picker launches do not have this blanket requirement", report)
        self.assertIn(
            "Action: fully quit ChatGPT Desktop, Codex CLI, then rerun `codex-configure doctor`.",
            report,
        )
        self.assertIn("Result: managed configuration healthy; client action required", report)
        self.assertNotIn("[ERROR] Client lifecycle", report)

    def test_doctor_reports_an_uninitialized_exact_cwd_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = base / "project"
            parent_context = initialize_root(base)
            global_home = base / "home" / ".codex"
            global_home.mkdir(parents=True)
            (global_home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            cwd.mkdir()
            output = io.StringIO()

            with (
                mock.patch("codex_configure.cli.Path.cwd", return_value=cwd),
                mock.patch.object(Launcher, "running_clients", return_value=[]),
                mock.patch.object(sys, "stdout", output),
                mock.patch.dict(os.environ, {"HOME": str(base / "home")}, clear=True),
            ):
                result = main(["doctor"])

            self.assertEqual(result, 1)
            self.assertFalse((cwd / ".codex-configure").exists())
            report = output.getvalue()
            self.assertIn("Launch root (exact current directory)", report)
            self.assertIn("Status: not configured", report)
            self.assertIn(str(cwd / ".codex-configure" / "codex-home"), report)
            self.assertNotIn(str(parent_context.codex_home), report)
            self.assertNotIn(str(global_home), report)

    def test_doctor_checks_an_initialized_exact_cwd_without_building_a_launch_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary) / "project"
            cwd.mkdir()
            context = initialize_root(cwd)
            write_launch_configuration(
                context.state_dir,
                context.codex_home,
                LaunchSettings("stock", "openai"),
                root=True,
            )
            (context.codex_home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n', encoding="utf-8"
            )
            ConfigManager(context.codex_home).initialize()
            output = io.StringIO()

            with (
                mock.patch("codex_configure.cli.Path.cwd", return_value=cwd),
                mock.patch.object(Launcher, "running_clients", return_value=[]),
                mock.patch("codex_configure.cli.rooted_environment") as rooted,
                mock.patch.object(sys, "stdout", output),
            ):
                result = main(["doctor"])

            self.assertEqual(result, 0)
            rooted.assert_not_called()
            self.assertIn("Status: recognized", output.getvalue())
            self.assertIn("Result: healthy", output.getvalue())

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
    @staticmethod
    def _single_live_client(name: str):
        def result(command: list[str], **kwargs: object) -> mock.Mock:
            if command[0] == "/usr/bin/pgrep":
                active = command[-1] == name
                return mock.Mock(returncode=0 if active else 1, stdout="42\n" if active else "", stderr="")
            if command[0] == "/usr/bin/ps":
                return mock.Mock(returncode=0, stdout="S\n", stderr="")
            raise AssertionError(command)

        return result

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

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which", side_effect=lambda name, path=None: f"/usr/bin/{name}")
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_linux_scoped_lifecycle_ignores_global_and_other_root_clients(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        run.side_effect = self._single_live_client("chatgpt")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        proc_root = Path(temporary.name) / "proc"
        process = proc_root / "42"
        process.mkdir(parents=True)
        target_root = Path(temporary.name) / "target-root"
        target_home = target_root / ".codex-configure" / "codex-home"
        environment = process / "environ"
        launcher = Launcher({"PATH": "/usr/bin"}, proc_root=proc_root)

        environment.write_bytes(b"PATH=/usr/bin\0HOME=/home/test\0")
        launcher.ensure_clients_stopped(target_home, target_root=target_root)

        environment.write_bytes(
            f"CODEX_CONFIGURE_ROOT={temporary.name}/other-root\0".encode("utf-8")
        )
        launcher.ensure_clients_stopped(target_home, target_root=target_root)

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which", side_effect=lambda name, path=None: f"/usr/bin/{name}")
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_linux_scoped_lifecycle_blocks_shared_codex_home_with_boundary(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        run.side_effect = self._single_live_client("chatgpt")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        proc_root = Path(temporary.name) / "proc"
        process = proc_root / "42"
        process.mkdir(parents=True)
        target_home = Path(temporary.name) / "target-root" / ".codex-configure" / "codex-home"
        (process / "environ").write_bytes(f"CODEX_HOME={target_home}\0".encode("utf-8"))

        with self.assertRaisesRegex(UserFacingError, rf"CODEX_HOME {target_home.resolve()}"):
            Launcher({"PATH": "/usr/bin"}, proc_root=proc_root).ensure_clients_stopped(target_home)

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which", side_effect=lambda name, path=None: f"/usr/bin/{name}")
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_linux_scoped_lifecycle_blocks_shared_launch_root_with_boundary(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        run.side_effect = self._single_live_client("chatgpt")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        proc_root = Path(temporary.name) / "proc"
        process = proc_root / "42"
        process.mkdir(parents=True)
        target_root = Path(temporary.name) / "target-root"
        target_home = target_root / ".codex-configure" / "codex-home"
        (process / "environ").write_bytes(
            f"CODEX_CONFIGURE_ROOT={target_root}\0CODEX_HOME=/other/home\0".encode("utf-8")
        )

        with self.assertRaisesRegex(UserFacingError, rf"launch root {target_root.resolve()}"):
            Launcher({"PATH": "/usr/bin"}, proc_root=proc_root).ensure_clients_stopped(
                target_home,
                target_root=target_root,
            )

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which", side_effect=lambda name, path=None: f"/usr/bin/{name}")
    @mock.patch("codex_configure.cli.sys.platform", "linux")
    def test_linux_scoped_lifecycle_blocks_unattributable_client(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        run.side_effect = self._single_live_client("chatgpt")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        proc_root = Path(temporary.name) / "proc"
        (proc_root / "42").mkdir(parents=True)
        target_home = Path(temporary.name) / "target-root" / ".codex-configure" / "codex-home"
        launcher = Launcher({"PATH": "/usr/bin"}, proc_root=proc_root)

        with mock.patch("codex_configure.cli.Path.read_bytes", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(UserFacingError, "environment could not be read safely"):
                launcher.ensure_clients_stopped(target_home)

    @mock.patch("codex_configure.cli.subprocess.run")
    @mock.patch("codex_configure.cli.shutil.which", side_effect=lambda name, path=None: f"/usr/bin/{name}")
    @mock.patch("codex_configure.cli.sys.platform", "darwin")
    def test_non_linux_scoped_lifecycle_remains_conservative(
        self, which: mock.Mock, run: mock.Mock
    ) -> None:
        run.side_effect = self._single_live_client("ChatGPT")

        with self.assertRaisesRegex(UserFacingError, "Codex or ChatGPT is running \\(ChatGPT\\)"):
            Launcher({"PATH": "/usr/bin"}, proc_root=Path("/not-used")).ensure_clients_stopped(
                Path("/isolated/codex-home"),
                target_root=Path("/isolated"),
            )

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
