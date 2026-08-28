# codex-configure

`codex-configure` lets one Codex home use OpenAI and one or more named U-M GPT Toolkit services. Each U-M service has its own API key, billing allocation, and selected model catalog.

There are two ways to use it:

- **Per-launch Profiles** works with the stock Codex CLI and stock desktop app on macOS and Linux. Choose one provider when you launch.
- **Dynamic Picker** uses a patched Codex Core on Linux. OpenAI and every configured U-M profile appear together in the desktop and CLI model picker.

Both modes preserve the existing OpenAI sign-in and share the same tasks, settings, skills, and plugins in `CODEX_HOME`.

> **Important:** Every `codex-configure run` command writes the selected active configuration to `$CODEX_HOME/config.toml` before launching Codex. This is a persistent change to the file, not a process-local override: it remains after Codex exits until another `codex-configure run` or `codex-configure restore` replaces it. Initialization preserves the original configuration, and switches are transactional and recoverable, but this tool does modify Codex's `config.toml`.

## Install

All users need:

- Python 3.11 or newer;
- [pipx](https://pipx.pypa.io/latest/how-to/install-pipx.html);
- the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), signed in with ChatGPT; and
- the ChatGPT desktop app, signed in with the same account.

Linux users can follow the [ChatGPT Linux installation guide](https://learn.chatgpt.com/docs/linux/linux-app). macOS users can install the ChatGPT app from the [OpenAI desktop page](https://openai.com/chatgpt/desktop/). Fully quit both clients before changing an active profile.

Install `codex-configure` from PyPI:

```bash
pipx install codex-configure
```

`pipx` creates an isolated environment and exposes the `codex-configure` command. If the command is not found after installation, run `pipx ensurepath` and open a new terminal. To install a source checkout instead, run `pipx install .` from the repository root.

On macOS, install Python and pipx first if they are unavailable. Homebrew users can run `brew install python pipx`.

## Initialize Providers

Run setup once for each U-M Toolkit key you want to use:

```bash
codex-configure init
```

Setup shows the stock and existing providers, then offers **New U-M GPT Toolkit Service**. It asks for:

- a short name containing lowercase letters, digits, hyphens, or underscores, such as `teaching` or `research-2026`;
- a key from [U-M GPT Toolkit](https://toolkit.umgpt.umich.edu/); and
- the endpoint models to expose.

The model selector shows everything advertised for that key. Models for which the installed Codex build has metadata are selectable; other entries remain visible but disabled. Compatible `gpt-5.6` models are checked by default.

Run `init` again to add another service. The short name becomes the profile name, descriptor filename, credential variable prefix, and Dynamic Picker namespace. For example, `teaching` creates `TEACHING_API_KEY` and models such as `teaching::gpt-5.6-terra`.

## Per-launch Profiles

Per-launch profiles work on macOS and Linux without changing Codex Core. The provider and target are written as `provider/app`:

```bash
# Existing OpenAI sign-in, stock Core
codex-configure run openai/cli
codex-configure run openai/desktop

# A named U-M profile, stock Core
codex-configure run teaching/cli
codex-configure run teaching/desktop
```

Before launching, this mode replaces the active `$CODEX_HOME/config.toml` with a configuration for the selected provider. The change is not automatically undone when the CLI or desktop app exits; it remains active until a later `codex-configure run` selects another configuration or `codex-configure restore` is run. The command prints the profile directory it used and removes any inherited `CODEX_CLI_PATH` so a global shell setting cannot accidentally select the patched Core.

Only the selected U-M key is added to that child process. OpenAI launches receive no U-M credentials.
When returning from Dynamic Picker, a saved `openai::` model is unqualified for stock Core; an external-qualified model is omitted so stock OpenAI can choose its own supported default.

On macOS, `codex-configure` launches the executable inside `ChatGPT.app` so the selected environment reaches Codex Core. Set `CODEX_DESKTOP_COMMAND` if the application is installed somewhere unusual. On Linux, the normal command is `chatgpt`; the same override supports another compatible desktop command or VM flags.

## Dynamic Picker

Dynamic Picker is a research feature currently supported and tested on Linux x86_64 with glibc 2.35 or newer (the Ubuntu 22.04 baseline). It keeps the stock desktop renderer and patches the open-source Codex Core used behind it.

Install the matching prebuilt Core release with one command:

```bash
codex-configure setup dynamic
```

For a new machine, the package and Core can be installed together:

```bash
pipx install codex-configure && codex-configure setup dynamic
```

The setup command downloads the Linux x86_64 asset for the installed `codex-configure` version, verifies the release checksum plus its pinned-patch manifest, and installs it under `~/.codex-configure/cores/codex-configure-core-<version>-linux-x86_64/`. An atomic `~/.codex-configure/cores/current` link selects the active version; older versioned installations remain available for rollback by reinstalling the matching Python package version and rerunning setup. No Git or Rust installation is required for this path.

The installed Core is discovered automatically. No shell export is needed:

```bash
codex-configure run desktop
# or
codex-configure run cli
```

Dynamic launches do not require existing Codex clients to stop because they keep the shared base configuration and route each task through the patched Core. An already-running Desktop process still retains the environment from its first launch: if it was started with stock Core, close it once and restart it with `codex-configure run desktop` before relying on Dynamic Picker.

To build from source instead, install Git plus Rust 1.94 or newer from [rustup](https://rustup.rs/), with `cargo` on `PATH`, and run:

```bash
codex-configure patch
```

The fallback command checks out the pinned source, applies the packaged patch, and builds under `~/.codex-configure/codex-core/`. To place that checkout elsewhere, pass the destination explicitly:

```bash
codex-configure patch /absolute/path/to/codex-core
```

For a custom destination, set the exact `export CODEX_CLI_PATH=...` line printed by `patch`, or set that variable only on the later `run` command. Resolution order is an explicit `CODEX_CLI_PATH`, the installed `cores/current` release, then the default source build.

The unqualified `desktop` and `cli` targets are intentionally different from `provider/app`: before launching, they replace `$CODEX_HOME/config.toml` with the shared OpenAI base, load all configured provider credentials, and use the resolved patched Core. This change to `config.toml` also remains after Codex exits. The desktop child receives `CODEX_CLI_PATH`; the CLI executes that binary directly. Both targets require an executable `codex-code-mode-host` beside the patched binary.

The existing picker shows qualified entries such as:

```text
openai::gpt-5.6-sol
teaching::gpt-5.6-terra
research::gpt-5.6-luna
```

You can change provider/model between turns in one task. The working directory, execution host, permissions, and semantic conversation stay with the task. Provider-private reasoning data is discarded at a provider boundary because another provider cannot safely consume it.

The patched Core builds its picker catalog at startup from:

- the current built-in OpenAI catalog; and
- each valid `$CODEX_HOME/codex-configure/providers.d/*.toml` descriptor and its required JSON catalog under `$CODEX_HOME/codex-configure/catalogs/`.

A missing or malformed external catalog is warned about and skipped. The patched Core does not query arbitrary provider `/models` endpoints or invent missing Codex metadata.

`CODEX_CLI_PATH` is an observed desktop integration hook, not a documented stable OpenAI interface. Re-run the documented acceptance checks after updating the desktop app or refreshing the pinned Core patch.

## Files And Safety

The default Codex home is `~/.codex`. A custom `CODEX_HOME` is respected without changing the layout:

```text
$CODEX_HOME/
|-- auth.json                         # owned by Codex; never changed here
|-- config.toml                       # active materialized configuration
`-- codex-configure/
    |-- .env                          # provider keys, mode 0600
    |-- providers.d/<shortname>.toml  # provider configuration, no secrets
    |-- catalogs/<shortname>.json     # selected Codex model metadata
    |-- profiles/                     # stock-Core launch profiles
    |-- base/                         # original and maintained config snapshots
    `-- recovery/                     # last-known-good transaction state
```

On first initialization, the existing `config.toml` is preserved before any profile is activated. Normal `run` commands then write the selected materialized configuration to the active `config.toml`; atomic switching and recovery protect that operation, but do not make it temporary. `codex-configure` never replaces `auth.json`, recursively backs up `CODEX_HOME`, or copies credentials into descriptors, catalogs, profiles, diagnostics, or recovery files.

The `.env` file is created with mode `0600`, and tool-owned directories use mode `0700`. An environment variable with the expected name can override a stored key for one launch.

## Check And Recover

Inspect the managed configuration without changing it:

```bash
codex-configure doctor
```

Restore the maintained OpenAI base, or the immutable first-run snapshot:

```bash
codex-configure restore
codex-configure restore --original
```

Named stock-profile switching and restore commands refuse to proceed while a known Codex or ChatGPT process is running. They also reject unexpected outside changes to routing fields instead of overwriting them. Unrelated settings written by Codex are retained.

## Troubleshooting

If the CLI says setup is missing, run `codex-configure init` with the intended `CODEX_HOME`. Copying a complete managed `$CODEX_HOME/codex-configure/` layout, including its base/state files and valid provider catalogs, also counts as initialized after validation.

If the desktop command cannot be found, set an explicit launch command:

```bash
CODEX_DESKTOP_COMMAND=/path/to/chatgpt codex-configure run openai/desktop
```

Some Linux virtual machines need Chromium software rendering:

```bash
CODEX_DESKTOP_COMMAND='chatgpt --use-angle=swiftshader' codex-configure run desktop
```

If a credential permission check fails, repair it with:

```bash
chmod 700 "${CODEX_HOME:-$HOME/.codex}/codex-configure"
chmod 600 "${CODEX_HOME:-$HOME/.codex}/codex-configure/.env"
```

Architecture, patch maintenance, and manual acceptance details are in [docs/architecture.md](https://github.com/cab938/codex-configure/blob/main/docs/architecture.md). U-M model discovery is not an entitlement guarantee: a provider may still reject an advertised model because of deployment access, account policy, or budget.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](https://github.com/cab938/codex-configure/blob/main/LICENSE) and [NOTICE](https://github.com/cab938/codex-configure/blob/main/NOTICE).
