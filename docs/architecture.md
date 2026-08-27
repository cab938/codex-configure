# Architecture

This document records the durable design and safety boundaries for `codex-configure`. Installation and everyday use belong in the project [README](../README.md).

## Scope

`codex-configure` is an external launcher for Codex clients. It supports two environments in one shared `CODEX_HOME`:

- OpenAI, using the user's existing Codex authentication and upstream model catalog; and
- U-M GPT Toolkit, using the OpenAI / Azure route and a provider-specific API key.

It launches either the Codex CLI or Codex in the ChatGPT desktop app on Linux. The settled launcher does not modify the installed ChatGPT package or renderer. The qualified provider-model extension selects a pinned patched Codex backend while preserving the existing App Server wire schema; its design and packaging boundary are recorded in the [spike](spike-core-provider-model-picker.md) and [Linux productization plan](linux-provider-picker-productization.md).

The UI calls these choices **environments** because switching affects provider routing, credentials, catalog, and launch behavior, not just the default model.

## System boundary

Codex keeps user state under `CODEX_HOME`, normally `~/.codex`. The materialized active configuration at `$CODEX_HOME/config.toml` remains the shared credential, provider-definition, and recovery boundary. The qualified provider-model backend additionally supplies both configured providers through the existing model-list and string-valued model-selection APIs.

The launcher owns only the model-routing fields it installs:

- `model`;
- `model_provider`;
- `model_catalog_json`; and
- `[model_providers.umich-toolkit]`.

It structurally merges those fields with the user's configuration. Unknown settings and unrelated provider definitions are preserved. If Desktop changes non-routing preferences while an environment is active, the next switch adopts those changes into the shared base. If another process changes the routing owned by `codex-configure`, the launcher refuses to overwrite it.

Official Codex references:

- [Advanced configuration and profiles](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Authentication](https://learn.chatgpt.com/docs/auth)
- [Managed configuration](https://learn.chatgpt.com/docs/enterprise/managed-configuration)

Managed configuration has higher precedence than the user configuration. `doctor` reports known `/etc/codex/managed_config.toml` and `/etc/codex/requirements.toml` files. Other organization-managed layers may also override user settings; the launcher does not modify or try to bypass any of them.

## Components and data flow

```text
User
  |
  v
Interactive CLI
  |-- ConfigManager ----> base + environment overlay ----> active config.toml
  |-- CatalogService ---> U-M aliases + bundled catalog --> selected catalog
  `-- Launcher ---------> Codex CLI or ChatGPT Desktop
                              |
                              `-- U-M key only for a U-M child process
```

### Interactive CLI

The entry point owns the user choices and their order:

- OpenAI goes directly to the Desktop/CLI choice.
- U-M displays the single OpenAI / Azure provider, a persistent multi-model selector, a default-model selector, and the Desktop/CLI choice.

Immediately before launch, it prints the active environment, profile directory, and selected profile.

### ConfigManager

`ConfigManager` inventories and initializes the runtime, preserves the original configuration, reconciles safe external edits, writes profiles and catalogs, activates environments transactionally, restores OpenAI, and supplies read-only health checks.

### CatalogService

`CatalogService` intersects:

1. model aliases advertised by U-M; and
2. model metadata bundled with the installed Codex CLI.

This gives stock Codex complete metadata for each model exposed by the launcher. If live U-M discovery fails or has no compatible entries, a bounded maintained fallback is used. The service currently marks `gpt-5.6-terra` as `verified`; compatible U-M-advertised entries without a recorded canary are `listed`.

The resulting selection is written as an immutable content-addressed catalog. Catalog presence means only that the launcher can describe a model. It is not an entitlement guarantee, and a provider can still reject a listed model for account, deployment, or budget reasons.

### Launcher

The launcher verifies that all known Codex CLI and ChatGPT/Desktop processes are stopped before switching. It then starts the selected client with the activated configuration.

On Linux it normally resolves `chatgpt` from `PATH`. `CODEX_DESKTOP_COMMAND` is an explicit override for nonstandard installations or VM flags. The provider-picker build is executed directly for CLI launches and supplied to the Linux Desktop child through `CODEX_CLI_PATH`; that variable is scoped to the child process rather than installed globally.

## Runtime layout

The project preserves Codex's existing home and adds one tool-owned subtree:

```text
$CODEX_HOME/
|-- config.toml                         # effective active configuration
|-- auth.json                           # existing Codex auth; never tool-owned
|-- sessions/                           # shared tasks; never tool-owned
|-- skills/                             # shared skills; never tool-owned
|-- ...                                 # other shared Codex state
`-- codex-configure/
    |-- .env                            # U-M key, mode 0600, never backed up
    |-- state.toml                      # active environment and config hash
    |-- base/
    |   |-- config.toml                 # maintained shared OpenAI base
    |   `-- original-config.toml        # immutable first-run config
    |-- profiles/
    |   |-- openai/
    |   |   |-- profile.toml
    |   |   `-- config.toml
    |   `-- umich/
    |       |-- profile.toml
    |       `-- config.toml
    |-- catalogs/
    |   `-- umich-openai-azure-HASH.json
    |-- locks/
    |   `-- activate.lock
    `-- recovery/
        |-- last-good-config.toml
        |-- last-good-state.toml
        |-- pending-previous-config.toml
        |-- pending-previous-state.toml
        `-- transaction.json
```

The pending files and transaction marker exist only during a promotion. Directories use mode `0700`; tool-owned state, profiles, catalogs, and credentials use mode `0600`.

Backups are deliberately narrow. The launcher never recursively copies `CODEX_HOME`, so recovery data does not sweep up `auth.json`, the `.env`, task databases, logs, plugins, or other unrelated state.

## Credential boundary

OpenAI authentication remains under Codex's ownership. `codex-configure` does not edit or replace `auth.json`.

The U-M key is resolved in this order:

1. `UMICH_TOOLKIT_API_KEY` in the launch environment;
2. a private file supplied with `--env-file PATH`; or
3. `$CODEX_HOME/codex-configure/.env`.

The generated U-M provider configuration contains the environment-variable name, not its value:

```toml
[model_providers.umich-toolkit]
name = "U-M GPT Toolkit - OpenAI / Azure"
base_url = "https://api.portkey.ai/v1"
wire_api = "responses"
env_http_headers = { "x-portkey-api-key" = "UMICH_TOOLKIT_API_KEY" }
```

The launcher supplies the key only to the U-M child process. It never writes the value into a profile, catalog, state file, active `config.toml`, backup, or diagnostic message. A keychain is not required.

The institutional setup originally used Codex's shared OpenAI credential slot. Investigation demonstrated that the U-M route works with a distinct Portkey header, so replacing the user's OpenAI authentication is unnecessary.

## Activation and recovery

Every switch or restore uses this sequence:

1. acquire the per-home activation lock;
2. recover or finalize an interrupted earlier transaction;
3. reconcile safe non-routing changes to the active configuration;
4. validate the selected overlay and catalog;
5. save the previous consistent config/state pair;
6. write a transaction marker;
7. atomically replace the active configuration and state;
8. save the new pair as last known good; and
9. remove the transaction files.

If execution stops between the live-file writes, the next invocation restores the saved prior pair. If both target files were committed, it finalizes that pair. The launcher does not silently kill running clients and does not guess when required recovery state is missing or inconsistent.

`restore` activates the maintained OpenAI base. `restore --original` activates the immutable first-run snapshot. Both use the same guarded transaction path as a normal switch.

## Shared state behavior

Tasks, history, skills, plugins, MCP configuration, memories, trust settings, and other non-routing state remain shared because both providers use one `CODEX_HOME`.

With the qualified provider-model backend, the selected provider and model are committed to the task between turns and restored on resume. A user can change providers through the existing model picker without changing the execution host or forking the task. Provider-bound reasoning is removed at a provider boundary, while user messages, assistant messages, and tool history remain available.

## Durable decisions

- Remain an external launcher; do not modify the installed ChatGPT package or renderer.
- Support both CLI and Desktop through one interaction.
- Preserve one shared `CODEX_HOME` and materialize one active configuration.
- Keep OpenAI authentication untouched.
- Store the U-M key in a protected file by default; do not require a keychain.
- Limit the current U-M interface to OpenAI / Azure.
- Let users select multiple compatible models and one default.
- Treat U-M aliases and catalog entries as discovery, not entitlement.
- Treat activation as a recoverable transaction.
- Preserve unrelated user configuration and refuse ambiguous routing overwrites.
- Detect managed configuration conflicts without trying to override policy.
- Make Linux the first supported packaging and documentation target.

## Non-goals and current constraints

- AWS Bedrock, Google, local providers, and Portkey administration are outside the current interface.
- Concurrent environments in one `CODEX_HOME` are not supported.
- The launcher does not manage all Codex settings or patch the Desktop renderer's model-picker behavior.
- It does not guarantee that every advertised U-M model is callable by every key.
- It does not distribute or change administrator-managed Codex policy.
- Packaging and documentation for other operating systems are outside the first release scope.
