# Architecture

This document records the durable design and safety boundaries for `codex-configure`. Installation and everyday use belong in the project [README](../README.md).

## Scope

`codex-configure` is an external launcher for stock Codex clients. It supports two environments in one shared `CODEX_HOME`:

- OpenAI, using the user's existing Codex authentication and upstream model catalog; and
- U-M GPT Toolkit, using the OpenAI / Azure route and a provider-specific API key.

It launches either the Codex CLI or Codex in the ChatGPT desktop app on macOS and Linux. It does not patch either client, replace the Codex App Server, or operate more than one active environment concurrently.

The UI calls these choices **environments** because switching affects provider routing, credentials, catalog, and launch behavior, not just the default model.

## System boundary

Codex keeps user state under `CODEX_HOME`, normally `~/.codex`. The CLI supports named profile overlays, but stock Desktop has no corresponding user-facing environment selector. The compatibility surface shared by both clients is therefore the materialized active configuration at `$CODEX_HOME/config.toml`.

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

On macOS it prefers the executable inside `/Applications/ChatGPT.app` or `~/Applications/ChatGPT.app`; direct execution is required to pass the U-M environment reliably. On Linux it normally resolves `chatgpt` from `PATH`. `CODEX_DESKTOP_COMMAND` is an explicit override for nonstandard installations or VM flags.

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

Tasks, history, skills, plugins, MCP configuration, memories, trust settings, and other non-routing state remain shared because both environments use one `CODEX_HOME`.

A task is not pinned to the provider that created it. When a task is resumed, the currently active environment controls the next turn. This was exercised in the CLI by creating a task under OpenAI/Sol, switching to U-M/Terra, and resuming successfully through U-M. Users therefore select the intended environment before resuming a task.

## Durable decisions

- Remain an external launcher for stock clients.
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

## Non-goals and current constraints

- AWS Bedrock, Google, local providers, and Portkey administration are outside the current interface.
- Concurrent environments in one `CODEX_HOME` are not supported.
- The launcher does not manage all Codex settings or patch native model-picker behavior.
- It does not guarantee that every advertised U-M model is callable by every key.
- It does not distribute or change administrator-managed Codex policy.
- Linux has been exercised in the project VM. macOS launch behavior is covered by focused tests but still requires acceptance testing on a real Mac before a broad release.
