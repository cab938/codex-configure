from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_configure.errors import UserFacingError
from codex_configure.init_tui import InitState, ProfileDraft
from codex_configure.launch_context import LaunchSettings
from codex_configure.runtime import ConfigManager


def _catalog(slug: str = "local-coder") -> dict[str, object]:
    return {
        "models": [
            {
                "slug": slug,
                "display_name": slug,
                "description": "test model",
                "input_modalities": ["text"],
            }
        ]
    }


class InitStateTests(unittest.TestCase):
    def make_manager(self) -> tuple[tempfile.TemporaryDirectory[str], ConfigManager]:
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name)
        (home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        manager = ConfigManager(home)
        manager.initialize()
        return temporary, manager

    @staticmethod
    def local_draft(name: str = "local", key: str | None = "local-secret") -> ProfileDraft:
        return ProfileDraft(
            id=name,
            kind="local-responses",
            base_url="http://127.0.0.1:1337/v1",
            api_key=key,
            catalog=_catalog(),
            selected_models=("local-coder",),
            default_model="local-coder",
            catalog_source="http://127.0.0.1:1337/v1/models",
        )

    def test_new_root_starts_with_empty_external_profile_state(self) -> None:
        temporary, manager = self.make_manager()
        self.addCleanup(temporary.cleanup)

        state = InitState.load(manager, {})

        self.assertEqual(state.profiles(), ())
        self.assertFalse(state.dirty)

    def test_added_profile_is_proposed_until_save_and_cancel_discards_it(self) -> None:
        temporary, manager = self.make_manager()
        self.addCleanup(temporary.cleanup)
        state = InitState.load(manager, {})

        state.stage_add(self.local_draft())

        self.assertTrue(state.dirty)
        self.assertEqual(state.inspect("local").persisted, False)
        self.assertFalse(manager.provider_registry.descriptor_path("local").exists())
        self.assertFalse(manager.provider_registry.catalog_path("local").exists())
        self.assertFalse((manager.paths.profiles / "local").exists())

        state.discard()

        self.assertFalse(state.dirty)
        self.assertEqual(state.profiles(), ())
        self.assertFalse(manager.provider_registry.descriptor_path("local").exists())

    def test_save_materializes_a_staged_profile(self) -> None:
        temporary, manager = self.make_manager()
        self.addCleanup(temporary.cleanup)
        state = InitState.load(manager, {})
        state.stage_add(self.local_draft())

        state.save(manager)

        loaded = InitState.load(manager, {})
        self.assertFalse(state.dirty)
        self.assertEqual([profile.id for profile in loaded.profiles()], ["local"])
        self.assertTrue(manager.provider_registry.descriptor_path("local").is_file())
        self.assertTrue(manager.provider_registry.catalog_path("local").is_file())
        self.assertTrue((manager.paths.profiles / "local" / "profile.toml").is_file())
        self.assertEqual(manager.load_credentials({})["LOCAL_API_KEY"], "local-secret")

    def test_saved_removal_deletes_only_owned_profile_catalog_descriptor_and_key(self) -> None:
        temporary, manager = self.make_manager()
        self.addCleanup(temporary.cleanup)
        manager.save_local_provider(
            "local",
            "http://127.0.0.1:1337/v1",
            "local-secret",
            _catalog(),
            selected_models=["local-coder"],
            default_model="local-coder",
        )
        unrelated = manager.paths.root / "unrelated.txt"
        unrelated.write_text("keep", encoding="utf-8")
        state = InitState.load(manager, {})

        state.stage_remove("local")
        self.assertEqual(state.profiles(), ())
        self.assertTrue(manager.provider_registry.descriptor_path("local").exists())

        state.save(manager)

        self.assertFalse(manager.provider_registry.descriptor_path("local").exists())
        self.assertFalse(manager.provider_registry.catalog_path("local").exists())
        self.assertFalse((manager.paths.profiles / "local").exists())
        self.assertNotIn("LOCAL_API_KEY", manager.load_credentials({}))
        self.assertNotIn("LOCAL_API_KEY", manager.paths.env_file.read_text(encoding="utf-8"))
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_removal_refuses_an_active_profile_without_deleting_anything(self) -> None:
        temporary, manager = self.make_manager()
        self.addCleanup(temporary.cleanup)
        manager.save_local_provider(
            "local",
            "http://127.0.0.1:1337/v1",
            None,
            _catalog(),
            selected_models=["local-coder"],
            default_model="local-coder",
        )
        manager.activate_provider("local")
        state = InitState.load(manager, {})
        state.stage_remove("local")

        with self.assertRaisesRegex(UserFacingError, "still selected"):
            state.save(manager)

        self.assertTrue(manager.provider_registry.descriptor_path("local").exists())
        self.assertTrue(manager.provider_registry.catalog_path("local").exists())
        self.assertTrue((manager.paths.profiles / "local").is_dir())

    def test_launch_default_can_switch_before_removing_its_prior_profile(self) -> None:
        temporary, manager = self.make_manager()
        self.addCleanup(temporary.cleanup)
        manager.save_local_provider(
            "local",
            "http://127.0.0.1:1337/v1",
            None,
            _catalog(),
            selected_models=["local-coder"],
            default_model="local-coder",
        )
        state = InitState.load(manager, {}, LaunchSettings("stock", "local"))

        with self.assertRaisesRegex(UserFacingError, "launch default"):
            state.stage_remove("local")
        state.stage_launch_settings(LaunchSettings("stock", "openai"))
        state.stage_remove("local")
        state.save(manager)

        self.assertEqual(state.launch_settings, LaunchSettings("stock", "openai"))
        self.assertFalse(manager.provider_registry.descriptor_path("local").exists())

    def test_detected_openai_auth_copy_is_staged_and_never_overwrites(self) -> None:
        temporary, manager = self.make_manager()
        self.addCleanup(temporary.cleanup)
        source_home = manager.paths.codex_home / "normal-codex"
        source_home.mkdir()
        (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
        state = InitState.load(manager, {}, auth_source_home=source_home)

        self.assertEqual(state.openai_auth_state, "available to copy")
        state.stage_openai_auth_copy()
        self.assertEqual(state.openai_auth_state, "copy staged")
        self.assertFalse((manager.paths.codex_home / "auth.json").exists())

        state.save(manager)

        target = manager.paths.codex_home / "auth.json"
        self.assertEqual(target.read_text(encoding="utf-8"), "{}\n")
        second = InitState.load(manager, {}, auth_source_home=source_home)
        with self.assertRaisesRegex(UserFacingError, "refusing to overwrite"):
            second.stage_openai_auth_copy()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
