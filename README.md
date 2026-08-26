# codex-configure

`codex-configure` is a small terminal launcher for switching stock Codex clients between an existing OpenAI setup and U-M GPT Toolkit.

The first executable prototype targets macOS and Linux. It preserves the user's original `config.toml`, stores tool-owned profiles under `$CODEX_HOME/codex-configure/profiles/`, activates the selected environment atomically, and launches Codex Desktop or the Codex CLI.

An environment may describe:

- a Codex model provider and default model;
- an optional generated model catalog;
- a reference to provider-specific credentials;
- client launch and diagnostic behavior.

The tool should leave shared Codex state such as tasks, history, skills, plugins, memories, and MCP configuration in place. Configuration changes must be inspectable, reversible, and atomic. Secrets must not be copied into ordinary profile files, logs, or project artifacts.

## Install and run

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
codex-configure
```

For a configuration-only run that does not start Codex:

```bash
codex-configure --prepare-only
```

Use `--codex-home PATH` for an isolated or non-default Codex home. U-M authentication is resolved in this order:

1. `UMICH_TOOLKIT_API_KEY` in the launch environment;
2. a private file supplied with `--env-file PATH`;
3. `$CODEX_HOME/codex-configure/.env` (normally `~/.codex/codex-configure/.env`).

The standard file contains `UMICH_TOOLKIT_API_KEY=...`, must have mode `0600` on macOS and Linux, and is excluded from profiles and backups. The value is never written into a profile, catalog, state file, active `config.toml`, or diagnostic message.

## Interaction

OpenAI proceeds directly from environment selection to the Desktop/CLI launch choice and restores the preserved OpenAI configuration.

U-M GPT Toolkit shows the single `OpenAI / Azure` provider, then a multi-select list of models recognized by both the U-M alias endpoint and the installed Codex catalog. The user may enter individual numbers, ranges such as `1-3`, or `all`, and then chooses one selected model as the default. Selections persist for later runs.

Immediately before launch, the tool prints the environment, the shared profile directory, and the active profile path:

```text
Environment: U-M GPT Toolkit / OpenAI / Azure
Profiles directory: /home/USER/.codex/codex-configure/profiles
Active profile: /home/USER/.codex/codex-configure/profiles/umich
Launching Codex Desktop...
```

The U-M catalog is a selectable allowlist, not an entitlement claim. Models are marked `verified` only after a recorded canary; other U-M-listed models remain labeled `listed`. Stock Desktop picker visibility is still an explicit compatibility check.

## Documentation

- [Design](docs/design.md)
- [Project and runtime layout](docs/layout.md)
- [U-M Portkey capability investigation](docs/umich-portkey-investigation.md)

## Current scope

The prototype supports an existing OpenAI-authenticated installation and one U-M GPT Toolkit route, `OpenAI / Azure`. AWS Bedrock, Google, local providers, and Portkey control-plane management are deliberately excluded. The launcher reads the U-M key from the protected credential file and supplies it through the provider-specific `x-portkey-api-key` environment header without replacing the user's OpenAI credential.

The tool adopts non-routing changes that ChatGPT writes while an environment is active, such as Desktop preferences, plugins, and trusted projects. It still refuses to overwrite an external change to the active model/provider routing. The first-run configuration remains preserved at `$CODEX_HOME/codex-configure/base/original-config.toml`. Codex Desktop must be closed before switching environments.
