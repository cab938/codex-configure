"""State and full-screen interface for ``codex-configure init``.

The terminal interface deliberately keeps rendering separate from the changes
it proposes.  In particular, discovery may use a credential, but a newly
entered credential and its catalog remain in memory until the user explicitly
chooses Save.  This module is also useful without a terminal: ``InitState``
is the small, testable representation of the persisted and proposed profile
sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

import tomlkit

from .catalog import CatalogService, ModelChoice, is_default_model_slug
from .errors import UserFacingError
from .known_catalog import KnownCatalogProvenance
from .launch_context import LaunchSettings, copy_openai_auth
from .providers import ProviderDescriptor, validate_shortname
from .runtime import ConfigManager


class InitOutcome(str, Enum):
    SAVED = "saved"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class InitTuiResult:
    """The explicit terminal outcome and its proposed root launch settings."""

    outcome: InitOutcome
    settings: LaunchSettings | None = None


@dataclass(frozen=True)
class ProfileView:
    """Non-secret profile information displayed by the init manager."""

    id: str
    display_name: str
    kind: str
    base_url: str
    catalog_source: str
    selected_models: tuple[str, ...]
    default_model: str | None
    credential_state: str
    persisted: bool

    @property
    def model_count(self) -> int:
        return len(self.selected_models)


@dataclass(frozen=True)
class ProfileDraft:
    """A non-persisted profile assembled by the TUI's add flow."""

    id: str
    kind: str
    base_url: str
    api_key: str | None
    catalog: dict[str, Any]
    selected_models: tuple[str, ...]
    default_model: str
    catalog_source: str
    known_catalog: KnownCatalogProvenance | None = None

    @property
    def display_name(self) -> str:
        if self.kind == "local-responses":
            return f"Local Responses - {self.id}"
        return f"U-M GPT Toolkit - {self.id}"

    def view(self) -> ProfileView:
        credential_state = "not required" if self.api_key is None else "will be stored on Save"
        return ProfileView(
            id=self.id,
            display_name=self.display_name,
            kind=self.kind,
            base_url=self.base_url,
            catalog_source=self.catalog_source,
            selected_models=self.selected_models,
            default_model=self.default_model,
            credential_state=credential_state,
            persisted=False,
        )


def _profile_metadata(manager: ConfigManager, descriptor: ProviderDescriptor) -> tuple[
    tuple[str, ...], str | None, str
]:
    """Read only TUI metadata; fall back to the authoritative catalog."""

    catalog = manager.provider_registry.load_catalog(descriptor)
    selected = tuple(
        str(item["slug"])
        for item in catalog.get("models", [])
        if isinstance(item, Mapping) and isinstance(item.get("slug"), str)
    )
    default = selected[0] if selected else None
    source = str(descriptor.catalog_path)
    path = manager.paths.profiles / descriptor.id / "profile.toml"
    if not path.exists():
        return selected, default, source
    try:
        profile = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.ParseError) as exc:
        raise UserFacingError(f"Invalid profile metadata at {path}: {exc}") from exc
    configured = profile.get("selected_models")
    if isinstance(configured, list) and all(isinstance(item, str) for item in configured):
        selected = tuple(configured)
    configured_default = profile.get("default_model")
    if isinstance(configured_default, str) and configured_default:
        default = configured_default
    configured_source = profile.get("catalog_source")
    if isinstance(configured_source, str) and configured_source:
        source = configured_source
    return selected, default, source


class InitState:
    """Persisted profiles plus add/remove operations waiting for Save."""

    def __init__(
        self,
        persisted: Mapping[str, ProfileView],
        launch_settings: LaunchSettings | None = None,
        auth_source_home: Path | None = None,
        target_auth_path: Path | None = None,
    ) -> None:
        self._persisted = dict(persisted)
        self._added: dict[str, ProfileDraft] = {}
        self._removed: set[str] = set()
        self._persisted_launch = launch_settings
        self._proposed_launch = launch_settings
        self._auth_source_home = auth_source_home
        self._target_auth_path = target_auth_path
        self._copy_auth = False

    @classmethod
    def load(
        cls,
        manager: ConfigManager,
        environ: Mapping[str, str],
        launch_settings: LaunchSettings | None = None,
        auth_source_home: Path | None = None,
    ) -> "InitState":
        credentials = manager.load_credentials(environ)
        profiles: dict[str, ProfileView] = {}
        for descriptor in manager.list_providers(include_stock=False):
            selected, default, source = _profile_metadata(manager, descriptor)
            if not descriptor.env_key:
                credential_state = "not required"
            elif credentials.get(descriptor.env_key):
                credential_state = "available"
            else:
                credential_state = "missing"
            profiles[descriptor.id] = ProfileView(
                id=descriptor.id,
                display_name=descriptor.display_name,
                kind=descriptor.kind,
                base_url=descriptor.base_url,
                catalog_source=source,
                selected_models=selected,
                default_model=default,
                credential_state=credential_state,
                persisted=True,
            )
        return cls(
            profiles,
            launch_settings,
            auth_source_home,
            manager.paths.codex_home / "auth.json",
        )

    @property
    def dirty(self) -> bool:
        return bool(
            self._added
            or self._removed
            or self._copy_auth
            or self._proposed_launch != self._persisted_launch
        )

    @property
    def persisted_count(self) -> int:
        return len(self._persisted)

    @property
    def launch_settings(self) -> LaunchSettings | None:
        return self._proposed_launch

    @property
    def launch_changed(self) -> bool:
        return self._proposed_launch != self._persisted_launch

    @property
    def openai_auth_state(self) -> str:
        if self._target_auth_path is not None and (
            self._target_auth_path.exists() or self._target_auth_path.is_symlink()
        ):
            return "present (not replaced)"
        if self._copy_auth:
            return "copy staged"
        if self._auth_source_home is not None:
            return "available to copy"
        return "sign in on first launch"

    @property
    def auth_copy_staged(self) -> bool:
        return self._copy_auth

    @property
    def added_count(self) -> int:
        return len(self._added)

    @property
    def removed_count(self) -> int:
        return len(self._removed)

    def profiles(self) -> tuple[ProfileView, ...]:
        existing = [
            profile
            for profile_id, profile in self._persisted.items()
            if profile_id not in self._removed
        ]
        proposed = [draft.view() for draft in self._added.values()]
        return tuple(sorted((*existing, *proposed), key=lambda item: (item.id.casefold(), item.id)))

    def inspect(self, profile_id: str) -> ProfileView:
        if profile_id in self._added:
            return self._added[profile_id].view()
        if profile_id in self._removed:
            raise UserFacingError(f"Profile `{profile_id}` is staged for removal.")
        try:
            return self._persisted[profile_id]
        except KeyError as exc:
            raise UserFacingError(f"Unknown profile `{profile_id}`.") from exc

    def stage_add(self, draft: ProfileDraft) -> None:
        profile_id = validate_shortname(draft.id)
        if profile_id in self._persisted or profile_id in self._added or profile_id in self._removed:
            raise UserFacingError(f"A profile named `{profile_id}` already exists or is staged for removal.")
        if draft.default_model not in draft.selected_models:
            raise UserFacingError("The default model must be included in the selected models.")
        if not draft.selected_models:
            raise UserFacingError("Select at least one model for the profile.")
        self._added[profile_id] = draft

    def stage_remove(self, profile_id: str) -> None:
        if profile_id in self._added:
            del self._added[profile_id]
            return
        if profile_id not in self._persisted:
            raise UserFacingError(f"Unknown profile `{profile_id}`.")
        if (
            self._proposed_launch is not None
            and self._proposed_launch.core == "stock"
            and self._proposed_launch.provider == profile_id
        ):
            raise UserFacingError(
                f"Profile `{profile_id}` is the proposed stock-Core default. "
                "Use `l` to choose another launch default before removing it."
            )
        self._removed.add(profile_id)

    def stage_launch_settings(self, settings: LaunchSettings) -> None:
        if self._persisted_launch is None:
            raise UserFacingError("This init context has no project launch settings.")
        if settings.core not in {"stock", "dynamic"}:
            raise UserFacingError("Launch core must be `stock` or `dynamic`.")
        if settings.core == "dynamic":
            self._proposed_launch = LaunchSettings("dynamic", "openai")
            return
        profile_ids = {profile.id for profile in self.profiles()}
        if settings.provider != "openai" and settings.provider not in profile_ids:
            raise UserFacingError(f"Unknown proposed launch profile `{settings.provider}`.")
        self._proposed_launch = settings

    def stage_openai_auth_copy(self) -> None:
        if self._auth_source_home is None:
            raise UserFacingError("No authenticated normal Codex home was detected to copy from.")
        if self._target_auth_path is None:
            raise UserFacingError("This init context has no target OpenAI authentication path.")
        if self._target_auth_path.exists() or self._target_auth_path.is_symlink():
            raise UserFacingError(
                f"OpenAI authentication already exists at {self._target_auth_path}; refusing to overwrite it."
            )
        self._copy_auth = True

    def discard(self) -> None:
        self._added.clear()
        self._removed.clear()
        self._proposed_launch = self._persisted_launch
        self._copy_auth = False

    def save(self, manager: ConfigManager) -> None:
        """Apply the staged changes after preflighting every removal."""

        for profile_id in sorted(self._removed):
            manager.validate_provider_removal(profile_id, allow_active=True)
        if self._copy_auth:
            assert self._auth_source_home is not None
            copy_openai_auth(self._auth_source_home, manager.paths.codex_home)
        for profile_id in sorted(self._added):
            draft = self._added[profile_id]
            if draft.kind == "local-responses":
                manager.save_local_provider(
                    draft.id,
                    draft.base_url,
                    draft.api_key,
                    draft.catalog,
                    selected_models=list(draft.selected_models),
                    default_model=draft.default_model,
                    catalog_source=draft.catalog_source,
                    known_catalog=draft.known_catalog,
                )
            elif draft.kind == "umich-toolkit":
                if not draft.api_key:
                    raise UserFacingError("A U-M GPT Toolkit API key is required.")
                manager.save_umich_provider(
                    draft.id,
                    draft.api_key,
                    draft.catalog,
                    selected_models=list(draft.selected_models),
                    default_model=draft.default_model,
                    catalog_source=draft.catalog_source,
                )
            else:  # Defensive: drafts only originate in this module.
                raise UserFacingError(f"Unknown profile kind `{draft.kind}`.")
        if self._removed and self._proposed_launch is not None:
            if self._proposed_launch.core == "dynamic":
                manager.activate_dynamic()
            else:
                manager.activate_provider(self._proposed_launch.provider)
        for profile_id in sorted(self._removed):
            manager.remove_provider(profile_id)
        self._persisted = InitState.load(manager, {})._persisted
        self._persisted_launch = self._proposed_launch
        self.discard()


def terminal_available(input_stream: object, output_stream: object) -> bool:
    """Whether this process owns a real terminal suitable for curses."""

    return bool(
        getattr(input_stream, "isatty", lambda: False)()
        and getattr(output_stream, "isatty", lambda: False)()
    )


class _CursesInitApp:
    """Small keyboard-first renderer for an ``InitState`` instance."""

    def __init__(
        self,
        state: InitState,
        manager: ConfigManager,
        environ: Mapping[str, str],
        catalog_service: CatalogService,
    ) -> None:
        self.state = state
        self.manager = manager
        self.environ = environ
        self.catalog_service = catalog_service
        self.cursor = 0
        self.message = ""

    @staticmethod
    def _kind_label(kind: str) -> str:
        if kind == "local-responses":
            return "Local Responses"
        if kind == "umich-toolkit":
            return "U-M GPT Toolkit"
        return kind

    @staticmethod
    def _model_label(model: ModelChoice) -> str:
        label = model.display_name if model.display_name == model.slug else f"{model.display_name} ({model.slug})"
        details = "; ".join((model.status, *model.badges))
        return f"{label} [{details}]" if details else label

    @staticmethod
    def _write(window: Any, row: int, text: str, attribute: int = 0) -> None:
        height, width = window.getmaxyx()
        if 0 <= row < height:
            try:
                window.addnstr(row, 0, text, max(1, width - 1), attribute)
            except Exception:  # curses.error on a terminal resize is harmless.
                pass

    def _prompt(self, window: Any, label: str, *, default: str = "", secret: bool = False) -> str | None:
        import curses

        height, width = window.getmaxyx()
        prompt = f"{label}"
        if default:
            prompt += f" [{default}]"
        prompt += ": "
        self._write(window, height - 2, prompt)
        start = min(len(prompt), max(0, width - 2))
        window.move(height - 2, start)
        window.clrtoeol()
        window.refresh()
        curses.curs_set(1)
        if secret:
            curses.noecho()
        else:
            curses.echo()
        try:
            raw = window.getstr(height - 2, start, max(1, width - start - 1))
        finally:
            curses.noecho()
            curses.curs_set(0)
        value = raw.decode("utf-8", errors="replace").strip()
        return value or default

    def _choose(self, window: Any, title: str, options: Sequence[str]) -> int | None:
        import curses

        cursor = 0
        while True:
            window.erase()
            self._write(window, 0, title, curses.A_BOLD)
            self._write(window, 1, "Use arrows or j/k, Enter to choose, Esc to return.")
            height, _ = window.getmaxyx()
            visible = max(1, height - 4)
            first = max(0, min(cursor - visible // 2, max(0, len(options) - visible)))
            for row, index in enumerate(range(first, min(len(options), first + visible)), start=3):
                attribute = curses.A_REVERSE if index == cursor else curses.A_NORMAL
                self._write(window, row, options[index], attribute)
            window.refresh()
            key = window.getch()
            if key in (27, ord("q"), ord("Q")):
                return None
            if key in (curses.KEY_DOWN, ord("j")):
                cursor = min(len(options) - 1, cursor + 1)
            elif key in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                return cursor

    def _select_models(self, window: Any, models: Sequence[ModelChoice]) -> list[ModelChoice] | None:
        import curses

        selectable = {index for index, model in enumerate(models) if model.selectable}
        selected = {
            index
            for index, model in enumerate(models)
            if index in selectable and is_default_model_slug(model.slug)
        }
        if not selected and selectable:
            selected.add(min(selectable))
        cursor = min(selectable) if selectable else 0
        while True:
            window.erase()
            self._write(window, 0, "Choose models", curses.A_BOLD)
            self._write(window, 1, "Arrows/j/k move; Space toggles; Enter accepts; Esc cancels.")
            height, _ = window.getmaxyx()
            visible = max(1, height - 4)
            first = max(0, min(cursor - visible // 2, max(0, len(models) - visible)))
            for row, index in enumerate(range(first, min(len(models), first + visible)), start=3):
                model = models[index]
                marker = "x" if index in selected else " "
                suffix = " (unsupported)" if index not in selectable else ""
                attribute = curses.A_DIM if index not in selectable else curses.A_NORMAL
                if index == cursor:
                    attribute |= curses.A_REVERSE
                self._write(window, row, f"[{marker}] {self._model_label(model)}{suffix}", attribute)
            window.refresh()
            key = window.getch()
            if key in (27, ord("q"), ord("Q")):
                return None
            if key in (curses.KEY_DOWN, ord("j")):
                cursor = min(len(models) - 1, cursor + 1)
            elif key in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif key == ord(" ") and cursor in selectable:
                if cursor in selected:
                    selected.remove(cursor)
                else:
                    selected.add(cursor)
            elif key in (curses.KEY_ENTER, 10, 13) and selected:
                return [model for index, model in enumerate(models) if index in selected]

    def _choose_default(self, window: Any, models: Sequence[ModelChoice]) -> str | None:
        choice = self._choose(window, "Choose the default model", [self._model_label(model) for model in models])
        return models[choice].slug if choice is not None else None

    def _configure_launch(self, window: Any) -> None:
        if self.state.launch_settings is None:
            self.message = "This explicit Codex home has no project launch default."
            return
        choice = self._choose(
            window,
            "Default launch for this root",
            (
                "Dynamic Picker - all configured providers",
                "Stock Core - one fixed provider",
            ),
        )
        if choice is None:
            return
        if choice == 0:
            self.state.stage_launch_settings(LaunchSettings("dynamic", "openai"))
            self.message = "Proposed default: Dynamic Picker."
            return
        profiles = self.state.profiles()
        provider_choice = self._choose(
            window,
            "Choose the fixed provider for Stock Core",
            ("OpenAI (stock)", *(f"{profile.id} ({self._kind_label(profile.kind)})" for profile in profiles)),
        )
        if provider_choice is None:
            return
        provider = "openai" if provider_choice == 0 else profiles[provider_choice - 1].id
        self.state.stage_launch_settings(LaunchSettings("stock", provider))
        self.message = f"Proposed default: {provider} (stock Core)."

    def _stage_auth_copy(self) -> None:
        self.state.stage_openai_auth_copy()
        self.message = "OpenAI authentication copy staged; it will occur only on Save."

    def _add(self, window: Any) -> None:
        choice = self._choose(
            window,
            "Add a model-catalog profile",
            ("U-M GPT Toolkit", "Local Responses endpoint"),
        )
        if choice is None:
            return
        kind = "umich-toolkit" if choice == 0 else "local-responses"
        window.erase()
        self._write(window, 0, "Add a model-catalog profile", 0)
        self._write(window, 2, "Fields are held only in memory until Save.")
        name = self._prompt(window, "Profile name")
        if not name:
            self.message = "Add cancelled."
            return
        try:
            validate_shortname(name)
        except UserFacingError as exc:
            self.message = str(exc)
            return

        if kind == "umich-toolkit":
            self._write(window, 4, "Get a key at https://toolkit.umgpt.umich.edu/.")
            api_key = self._prompt(window, "API key", secret=True)
            if not api_key:
                self.message = "An API key is required to add a U-M GPT Toolkit profile."
                return
            base_url = "https://api.portkey.ai/v1"
            try:
                result = self.catalog_service.discover(api_key=api_key)
            except UserFacingError as exc:
                self.message = str(exc)
                return
        else:
            base_url = self._prompt(window, "Responses API base URL", default="http://127.0.0.1:1337/v1")
            if not base_url:
                self.message = "A Responses API base URL is required."
                return
            api_key = self._prompt(window, "Bearer API key (optional)", secret=True) or None
            try:
                result = self.catalog_service.discover_local(base_url, api_key=api_key)
            except UserFacingError as exc:
                self.message = str(exc)
                return

        selected = self._select_models(window, result.models)
        if not selected:
            self.message = "Add cancelled: select at least one model."
            return
        default = self._choose_default(window, selected)
        if default is None:
            self.message = "Add cancelled."
            return
        try:
            catalog = (
                self.catalog_service.build_local_catalog(selected)
                if kind == "local-responses"
                else self.catalog_service.build_selected_catalog(selected)
            )
            self.state.stage_add(
                ProfileDraft(
                    id=name,
                    kind=kind,
                    base_url=base_url,
                    api_key=api_key,
                    catalog=catalog,
                    selected_models=tuple(model.slug for model in selected),
                    default_model=default,
                    catalog_source=result.source,
                    known_catalog=getattr(result, "known_catalog", None),
                )
            )
        except UserFacingError as exc:
            self.message = str(exc)
            return
        warning = getattr(result, "warning", None)
        self.message = f"Added `{name}` to proposed state." + (f" Warning: {warning}" if warning else "")

    def _inspect(self, window: Any, profile: ProfileView) -> None:
        import curses

        first = 0
        lines = [
            f"Profile: {profile.id}",
            f"State: {'persisted' if profile.persisted else 'proposed addition'}",
            f"Type: {self._kind_label(profile.kind)}",
            f"Display name: {profile.display_name}",
            f"Endpoint: {profile.base_url}",
            f"Catalog source: {profile.catalog_source}",
            f"Credential: {profile.credential_state}",
            f"Default model: {profile.default_model or 'not set'}",
            "Models:",
            *(f"  - {model}" for model in profile.selected_models),
        ]
        while True:
            window.erase()
            height, _ = window.getmaxyx()
            visible = max(1, height - 3)
            for row, line in enumerate(lines[first : first + visible], start=0):
                self._write(window, row, line, curses.A_BOLD if row + first == 0 else curses.A_NORMAL)
            self._write(window, height - 1, "Up/Down scroll; Enter, Esc, or q returns.")
            window.refresh()
            key = window.getch()
            if key in (27, ord("q"), ord("Q"), curses.KEY_ENTER, 10, 13):
                return
            if key in (curses.KEY_DOWN, ord("j")):
                first = min(max(0, len(lines) - visible), first + 1)
            elif key in (curses.KEY_UP, ord("k")):
                first = max(0, first - 1)

    def _confirm(self, window: Any, question: str) -> bool:
        import curses

        height, _ = window.getmaxyx()
        self._write(window, height - 2, f"{question} [y/N]")
        window.refresh()
        key = window.getch()
        return key in (ord("y"), ord("Y"))

    def _render_main(self, window: Any) -> tuple[ProfileView, ...]:
        import curses

        profiles = self.state.profiles()
        self.cursor = min(self.cursor, max(0, len(profiles) - 1))
        window.erase()
        self._write(window, 0, "codex-configure init - model-catalog profiles", curses.A_BOLD)
        changes = "none" if not self.state.dirty else f"{self.state.added_count} add, {self.state.removed_count} remove"
        if self.state.launch_changed:
            changes += ", launch default"
        self._write(window, 1, f"Persisted profiles: {self.state.persisted_count}   Proposed changes: {changes}")
        settings = self.state.launch_settings
        if settings is not None:
            current = self.state._persisted_launch
            current_label = current.description if current is not None else "not set"
            self._write(window, 2, f"Launch default: {current_label} -> {settings.description}")
        self._write(window, 3, f"OpenAI auth: {self.state.openai_auth_state}")
        self._write(window, 4, "Enter inspect | a add | d remove | l launch | o auth | s save | q cancel")
        height, _ = window.getmaxyx()
        if not profiles:
            self._write(window, 6, "No model-catalog profiles yet. Press a to add one.")
        else:
            visible = max(1, height - 8)
            first = max(0, min(self.cursor - visible // 2, max(0, len(profiles) - visible)))
            for row, index in enumerate(range(first, min(len(profiles), first + visible)), start=6):
                profile = profiles[index]
                state = "saved" if profile.persisted else "new"
                line = (
                    f"{profile.id:<18} {self._kind_label(profile.kind):<18} "
                    f"{profile.model_count:>2} models  default={profile.default_model or '-'}  [{state}]"
                )
                attribute = curses.A_REVERSE if index == self.cursor else curses.A_NORMAL
                self._write(window, row, line, attribute)
        if self.message:
            self._write(window, height - 1, self.message, curses.A_BOLD)
        window.refresh()
        return profiles

    def run(self, window: Any) -> InitTuiResult:
        import curses

        curses.curs_set(0)
        while True:
            profiles = self._render_main(window)
            key = window.getch()
            if key in (curses.KEY_DOWN, ord("j")) and profiles:
                self.cursor = min(len(profiles) - 1, self.cursor + 1)
            elif key in (curses.KEY_UP, ord("k")) and profiles:
                self.cursor = max(0, self.cursor - 1)
            elif key in (curses.KEY_ENTER, 10, 13) and profiles:
                self._inspect(window, profiles[self.cursor])
            elif key in (ord("a"), ord("A")):
                self._add(window)
            elif key in (ord("l"), ord("L")):
                try:
                    self._configure_launch(window)
                except UserFacingError as exc:
                    self.message = str(exc)
            elif key in (ord("o"), ord("O")):
                try:
                    self._stage_auth_copy()
                except UserFacingError as exc:
                    self.message = str(exc)
            elif key in (ord("d"), ord("D")) and profiles:
                profile = profiles[self.cursor]
                question = (
                    f"Stage removal of `{profile.id}`? Save will remove only this tool-owned "
                    "profile, catalog, descriptor, and stored credential."
                )
                if self._confirm(window, question):
                    try:
                        self.state.stage_remove(profile.id)
                    except UserFacingError as exc:
                        self.message = str(exc)
                    else:
                        self.message = f"Staged removal of `{profile.id}`."
            elif key in (ord("s"), ord("S")):
                if not self.state.dirty:
                    if self._confirm(window, "Continue with the current persisted state?"):
                        return InitTuiResult(InitOutcome.SAVED, self.state.launch_settings)
                elif self._confirm(window, "Save all proposed changes?"):
                    try:
                        self.state.save(self.manager)
                    except UserFacingError as exc:
                        self.message = str(exc)
                    else:
                        return InitTuiResult(InitOutcome.SAVED, self.state.launch_settings)
            elif key in (27, ord("q"), ord("Q")):
                if not self.state.dirty or self._confirm(window, "Discard all proposed changes?"):
                    self.state.discard()
                    return InitTuiResult(InitOutcome.CANCELLED, self.state.launch_settings)


def run_fullscreen_init(
    manager: ConfigManager,
    environ: Mapping[str, str],
    catalog_service: CatalogService | None = None,
    launch_settings: LaunchSettings | None = None,
    auth_source_home: Path | None = None,
) -> InitTuiResult:
    """Run the curses UI after ``ConfigManager.initialize`` has completed."""

    import curses

    state = InitState.load(manager, environ, launch_settings, auth_source_home)
    service = catalog_service or CatalogService(manager.paths.codex_home)
    app = _CursesInitApp(state, manager, environ, service)
    return curses.wrapper(app.run)


__all__ = [
    "InitOutcome",
    "InitTuiResult",
    "InitState",
    "ProfileDraft",
    "ProfileView",
    "run_fullscreen_init",
    "terminal_available",
]
