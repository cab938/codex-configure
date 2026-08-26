# Project and Runtime Layout

Status: version-0.1 runtime and immediate crash-recovery layout implemented; backup rotation and operational logging remain planned.

## Repository layout

```text
codex-configure/
|-- README.md
|-- docs/
|   |-- design.md
|   \-- layout.md
|-- pyproject.toml
|-- src/codex_configure/       # implementation
\-- tests/                     # focused interaction and activation tests
```

Ignore rules cover the local `.env`, virtual environments, Python caches, package metadata, and build output.

## User runtime layout

The tool will use the active Codex home, normally `~/.codex`, without relocating the user's shared Codex state.

```text
$CODEX_HOME/
|-- config.toml                         # effective active configuration
|-- auth.json                           # existing Codex credential cache, if file-backed
|-- history.jsonl                       # existing Codex state, not tool-owned
|-- sessions/                           # existing task state, not tool-owned
|-- skills/                             # existing skills, not tool-owned
|-- ...                                 # other existing Codex state
\-- codex-configure/
    |-- .env                            # U-M key, mode 0600, never backed up
    |-- state.toml                      # schema version and active environment
    |-- base/
    |   |-- config.toml                 # current shared non-environment configuration
    |   \-- original-config.toml        # immutable first-run configuration
    |-- profiles/
    |   |-- openai/
    |   |   |-- profile.toml            # display metadata and catalog strategy
    |   |   \-- config.toml             # Codex configuration overlay
    |   |-- umich/
    |   |   |-- profile.toml
    |   |   \-- config.toml
    |   \-- PROFILE_ID/
    |       |-- profile.toml
    |       \-- config.toml
    |-- catalogs/
    |   |-- umich-openai-azure-HASH.json # immutable generated catalog
    |   \-- PROFILE_ID.json
    |-- locks/
    |   \-- activate.lock
    \-- recovery/
        |-- last-good-config.toml
        |-- last-good-state.toml
        |-- pending-previous-config.toml  # present only during activation
        |-- pending-previous-state.toml   # present only during activation
        \-- transaction.json              # commit marker, present only during activation
```

The immutable first-run configuration and one immediate last-known-good pair are implemented. Backup rotation and redacted activity logging shown in the broader design are not created by version 0.1.

The exact existing Codex state paths vary by client and version. Inventory must discover them, and the tool must not assume that every possible entry shown above exists.

## Profile directory

The launcher prints the following path immediately before starting Codex:

```text
Profiles directory: $CODEX_HOME/codex-configure/profiles
```

Each profile directory separates environment metadata from the Codex TOML overlay:

```text
profiles/umich/
|-- profile.toml
\-- config.toml
```

`profile.toml` is expected to contain non-secret tool metadata such as:

- schema version;
- stable profile identifier;
- display name and description;
- provider identifier;
- catalog source and refresh strategy;
- supported launch targets;
- credential locator type, but never the credential itself.

`config.toml` contains the Codex keys contributed by the environment. For U-M this is expected to include the active model/provider selection and a stable `[model_providers.umich-toolkit]` definition. The effective catalog path may be inserted during materialization.

The OpenAI environment normally has no generated catalog and should leave the built-in provider and upstream catalog behavior intact.

## Active configuration

`$CODEX_HOME/config.toml` is the stock-client compatibility surface. It is generated from the current shared base plus the selected environment overlay. When ChatGPT writes non-routing settings while a profile is active, the next switch adopts those settings into the shared base. External changes to tool-owned model/provider routing are rejected.

It must not be a fragile text substitution. Candidate output should be written and validated in the same filesystem, then renamed atomically over the active file. The prior usable configuration should remain recoverable.

Official CLI profile projections, if used, would live at:

```text
$CODEX_HOME/openai.config.toml
$CODEX_HOME/umich.config.toml
```

Those files are not yet part of the canonical runtime layout. The prototype must first determine whether they can coexist cleanly with the materialized active configuration required by Desktop.

## Catalog files

Catalogs are promoted only after successful generation and validation:

```text
catalogs/umich-openai-azure-HASH.json
catalogs/.umich-openai-azure-HASH.json.TOKEN  # temporary, removed after promotion
```

Temporary names must be unpredictable and created securely. Catalog filenames include a SHA-256 content hash, so a new selection is promoted at a new path and cannot invalidate a currently active U-M configuration. A failed refresh must not replace the last-known-good catalog.

Catalog metadata in `state.toml` should record source, fetch time, content hash, validation result, and applicable Codex version without recording request credentials.

## Credential storage

Normal profile directories and backups must contain no secrets.

Version 0.1 stores the U-M credential in `$CODEX_HOME/codex-configure/.env` by default. The file contains `UMICH_TOOLKIT_API_KEY=...`; its parent directory has mode `0700` and the file has mode `0600`. `UMICH_TOOLKIT_API_KEY` in the process environment and `--env-file PATH` remain explicit overrides.

The generated provider configuration stores only the environment-variable name in `env_http_headers`. The launcher reads the protected file and provides the value only to the launched U-M process. It does not modify the user's OpenAI `auth.json`.

If compatibility testing proves that a profile-specific `auth.json` is unavoidable, secret profile state must live in a dedicated protected area separate from normal backups and catalog data. That fallback is intentionally not included in the default layout.

## Ownership and permissions

Suggested minimums:

| Path | Ownership | Suggested mode |
| --- | --- | --- |
| `$CODEX_HOME/codex-configure/` | User only | `0700` |
| Profile metadata and overlays | User only | `0600` |
| Catalogs | User only | `0600` |
| Locks and state | User only | `0600` |
| `$CODEX_HOME/codex-configure/.env` | User only | `0600` |

The tool does not own or rewrite `/etc/codex/managed_config.toml`, `/etc/codex/requirements.toml`, macOS MDM preferences, or unrelated files under `CODEX_HOME`.

## Backups and recovery

Initial adoption preserves an immutable copy of the existing non-secret configuration. Each successful activation retains one immediate last-known-good config/state pair. The current implementation does not create timestamped rotation or an audit log.

Backups should not recursively copy `CODEX_HOME`. In particular, they must not collect `auth.json`, `codex-configure/.env`, task databases, logs, plugin state, or unrelated user files through a broad glob.

Recovery should support:

```text
codex-configure restore
codex-configure restore --original
```

Both forms use the same validation, lock, stopped-client check, and crash-recoverable promotion path as normal activation. The default restores the maintained shared OpenAI base; `--original` restores the immutable first-run snapshot. Timestamped snapshot selection remains outside version 0.1.
