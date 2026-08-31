# codex-configure

`codex-configure` creates a self-contained Codex launch root in the exact current directory. Each root can use OpenAI and one or more named U-M GPT Toolkit services. Each U-M service has its own API key, billing allocation, selected model catalog, and one-line name such as `teaching` or `research`.

There are two ways to use it:

- **Stock Core (fixed provider)** works with the stock Codex CLI and stock desktop app on macOS and Linux. The root launches one chosen provider until you reconfigure it.
- **Dynamic Picker** uses a patched Codex Core on Linux or an Apple Silicon Mac. OpenAI and every configured U-M profile appear together in the desktop and CLI model picker.

Both modes preserve the OpenAI sign-in, tasks, settings, skills, and plugins belonging to the launch root's `CODEX_HOME`. During setup, a root may start unsigned-in or copy only the existing OpenAI `auth.json` from `~/.codex`. It never imports tasks, settings, skills, plugins, sessions, or U-M credentials from that home.

> **Important:** Codex launches through `codex-configure launch` or `codex-configure run` write the selected active configuration to that launch context's `$CODEX_HOME/config.toml` before launching. This is a persistent change to the file, not a process-local override. The `launch chrome` target does not change Codex configuration. Bare `codex-configure` is strictly read-only: it reports status and never initializes or launches anything.

## Install

All users need:

- Python 3.11 or newer;
- [pipx](https://pipx.pypa.io/latest/how-to/install-pipx.html);
- the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli).

Install the ChatGPT desktop app only if you want the `desktop` target. Linux users can follow the [ChatGPT Linux installation guide](https://learn.chatgpt.com/docs/linux/linux-app). macOS users can install the app from the [OpenAI desktop page](https://openai.com/chatgpt/desktop/).

Install `codex-configure` from PyPI:

```bash
pipx install codex-configure
```

`pipx` creates an isolated environment and exposes the `codex-configure` command. If the command is not found after installation, run `pipx ensurepath` and open a new terminal. To install a source checkout instead, run `pipx install .` from the repository root.

On macOS, install Python and pipx first if they are unavailable. Homebrew users can run `brew install python pipx`.

OpenAI authentication belongs to each launch root. During `init`, an authenticated normal `~/.codex` home is detected with `codex login status` and offered as an auth-only copy. If you choose stock OpenAI without copying it, sign in afterward with `codex-configure launch cli login`; the isolated desktop profile may also prompt on its first launch.

## Everyday Commands

The ordinary interface has three commands to remember:

```bash
codex-configure             # describe the exact-current-directory root; no changes
codex-configure init        # create or reconfigure this launch root
codex-configure launch      # launch the configured default (desktop when omitted)
```

Specific launch targets and their remaining arguments pass through the generated launcher:

```bash
codex-configure launch desktop
codex-configure launch cli
codex-configure launch cli login
codex-configure launch chrome          # launch roots only
```

From an initialized launch root, `launch` uses the exact current directory's `.codex-configure/launch.sh`. It never searches parent directories and never falls back to `~/.codex` or a legacy global launcher. An absent or invalid local root is an error.

`doctor`, `restore`, `setup dynamic`, `patch`, and the older explicit `run provider/app` form remain available for diagnosis and advanced control.

## Initialize

Run the setup wizard:

```bash
codex-configure init
```

If the exact current directory is not already a launch root, setup first asks whether to:

1. create a launch root in the current directory;
2. cancel without making changes.

It then displays every provider already configured in that root and repeatedly offers:

- **OpenAI (stock)**, which can be signed in later;
- **OpenAI (detected: `~/.codex` -> copy auth)** when a usable normal sign-in is found;
- each existing named U-M profile for reconfiguration;
- **New U-M GPT Toolkit Service**; and
- **Done configuring providers**.

The copy action creates only the new root's `auth.json`, refuses to overwrite one already there, and protects it with mode `0600`. OpenAI-only setup is valid. For a Toolkit profile setup asks for:

- a short name containing lowercase letters, digits, hyphens, or underscores, such as `teaching` or `research-2026`;
- a key from [U-M GPT Toolkit](https://toolkit.umgpt.umich.edu/); and
- the endpoint models to expose.

The model selector shows everything advertised for that key. Models for which the installed Codex build has metadata are selectable; other entries remain visible but disabled. Compatible `gpt-5.6` models are checked by default.

Finally, setup asks which Core the root should use:

1. **Dynamic Picker - all configured providers (recommended)**; or
2. **Stock Core - one fixed provider (advanced)**.

Choosing Dynamic Picker downloads and verifies that root's patched Core immediately. Choosing Stock Core asks which configured provider to fix for launches and uses the already-installed stock Codex executable. Run `init` again in the same root to add another service or change the Core/default provider. The short name becomes the exact profile name, descriptor filename, credential variable prefix, and Dynamic Picker namespace. For example, `teaching` creates `TEACHING_API_KEY` and models such as `teaching::gpt-5.6-terra`.

## Launch Roots

A launch root keeps persistent Codex and application state below the selected directory while leaving the caller's working directory unchanged:

```text
ROOT/.codex-configure/
|-- .gitignore                        # keeps the generated root out of Git
|-- root.toml                         # recognized-root marker
|-- launch.toml                       # default Core and provider
|-- launch.sh                         # generated pass-through launcher
|-- codex-home/                       # CODEX_HOME and managed profiles
|-- cores/                            # versioned prebuilt Dynamic Core, when selected
|-- codex-core/                       # default source checkout, when patch is used
|-- xdg/{config,data,state,cache}/
|-- electron-user-data/
`-- chrome/
    |-- home/
    |-- profile/
    `-- chrome-native-hosts-v2.json
```

Short-lived socket and temporary paths use `/run/user/$UID/codex-configure/<root-id>/` when available, with a private `/tmp` fallback. This is configuration and binary isolation, not a hard filesystem or security boundary. Each launch root has independent state and can either receive an auth-only copy during setup or be signed in independently. `launch chrome` starts Chrome or Chromium with the root's isolated browser home and profile; it does not claim full browser/native-host integration on every Codex Desktop build.

## Stock Core: Fixed Provider

Stock Core works on macOS and Linux without changing Codex Core. The provider and target are written as `provider/app` for advanced one-off launches:

```bash
# The selected CODEX_HOME's OpenAI sign-in, stock Core
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

Dynamic Picker is a research feature. It is acceptance-tested on Linux x86_64 with glibc 2.35 or newer (the Ubuntu 22.04 baseline), and an experimental native build is available for Apple Silicon Macs. The macOS path is intentionally available for real Desktop testing but is not yet a validated compatibility claim. Both paths keep the stock desktop renderer and patch the open-source Codex Core used behind it.

Selecting Dynamic Picker during `init` installs the matching prebuilt Core release automatically. To verify or reinstall it later, run this from the initialized launch root:

```bash
codex-configure setup dynamic
```

For a new machine, first install the package, enter the project directory you want to isolate, and run setup:

```bash
pipx install codex-configure
cd /path/to/project
codex-configure init
```

The installer selects the Linux x86_64 or macOS arm64 asset for the current machine, verifies the release checksum plus its pinned-patch manifest, and installs it under `ROOT/.codex-configure/cores/codex-configure-core-<version>-<target>/`. An atomic `ROOT/.codex-configure/cores/current` link selects the active version. Removing the project removes its Core and all of its isolated state. No Git or Rust installation is required for this path.

The installed Core is discovered automatically. Select Dynamic Picker during `init`, then no shell export is needed:

```bash
codex-configure launch desktop
# or
codex-configure launch cli
```

Dynamic launches do not require existing Codex clients to stop because they keep the shared base configuration and route each task through the patched Core. An already-running Desktop process still retains the environment from its first launch: if it was started with stock Core, close it once and restart it with `codex-configure launch desktop` before relying on Dynamic Picker.

On macOS, a Dynamic Picker desktop launch starts the executable inside `ChatGPT.app` with the installed native Core in `CODEX_CLI_PATH`. This integration hook is experimental: record the ChatGPT version and report any launch, sign-in, picker, or security-policy failure rather than changing managed security settings.

To build from source instead, install Git plus Rust 1.94 or newer from [rustup](https://rustup.rs/), with `cargo` on `PATH`, and run:

```bash
codex-configure patch
```

The fallback command checks out the pinned source, applies the packaged patch, and builds under `ROOT/.codex-configure/codex-core/`. To place that checkout elsewhere, pass the destination explicitly:

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

Ordinary commands always use the exact-current-directory root. The advanced `--codex-home PATH` option can initialize or operate on an explicit Codex home, but it does not create a launcher or install a project Core:

```text
$CODEX_HOME/
|-- auth.json                         # Codex auth; optionally copied into a fresh root
|-- config.toml                       # active materialized configuration
`-- codex-configure/
    |-- .env                          # provider keys, mode 0600
    |-- providers.d/<shortname>.toml  # provider configuration, no secrets
    |-- catalogs/<shortname>.json     # selected Codex model metadata
    |-- profiles/                     # stock-Core launch profiles
    |-- base/                         # original and maintained config snapshots
    `-- recovery/                     # last-known-good transaction state
```

On first initialization, the existing `config.toml` is preserved before any profile is activated. Normal `run` commands then write the selected materialized configuration to the active `config.toml`; atomic switching and recovery protect that operation, but do not make it temporary. The optional auth copy creates only a missing `auth.json` and never overwrites one. `codex-configure` never recursively backs up `CODEX_HOME` or copies credentials into descriptors, catalogs, profiles, diagnostics, or recovery files.

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

Named stock-profile switching, stock OpenAI selection, and restore commands refuse to proceed while a known Codex or ChatGPT process is running. Dynamic Picker launches do not require all clients to stop, although an already-running desktop process keeps the environment with which it started. Profile changes also reject unexpected outside edits to routing fields instead of overwriting them. Unrelated settings written by Codex are retained.

## Troubleshooting

If the CLI says setup is missing, enter the intended project directory and run `codex-configure init`. Commands never search a parent directory or fall back to a normal/global Codex home.

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
chmod 700 .codex-configure/codex-home/codex-configure
chmod 600 .codex-configure/codex-home/codex-configure/.env
```

Architecture, patch maintenance, and manual acceptance details are in [docs/architecture.md](https://github.com/cab938/codex-configure/blob/main/docs/architecture.md). U-M model discovery is not an entitlement guarantee: a provider may still reject an advertised model because of deployment access, account policy, or budget.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](https://github.com/cab938/codex-configure/blob/main/LICENSE) and [NOTICE](https://github.com/cab938/codex-configure/blob/main/NOTICE).
