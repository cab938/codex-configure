# Architecture

This document records the durable design, patch-maintenance boundary, and platform acceptance procedures for `codex-configure`. Installation and ordinary commands belong in the project [README](../README.md).

## Scope

`codex-configure` supports one stock provider and any number of named U-M GPT Toolkit services in either the normal `~/.codex` home or a directory-scoped launch root:

- OpenAI uses Codex's existing ChatGPT authentication and bundled model catalog.
- Each U-M service uses its own Portkey API key, static selected catalog, and short provider name.

It deliberately provides two operating modes.

| Invocation | Core | Provider choice |
| --- | --- | --- |
| `launch [desktop|cli]` | Configured default | Configured default |
| `launch chrome` | N/A | Isolated root browser profile |
| `run openai/cli` or `run openai/desktop` | Stock | OpenAI fixed for that launch |
| `run name/cli` or `run name/desktop` | Stock | Named U-M service fixed for that launch |
| `run cli` or `run desktop` | Patched | Qualified provider/model in the existing picker |

Bare `codex-configure` is a read-only status operation. `init` owns context creation and provider/default-launch configuration. `launch` resolves only the exact current directory: a valid local root wins, an invalid local `.codex-configure` blocks fallback, and an absent local directory permits the global launcher. Per-launch profiles support macOS and Linux. Dynamic Picker is acceptance-tested on Linux x86_64 with the Ubuntu 22.04 / glibc 2.35 baseline. Native macOS arm64 installation and launch are available as an experimental path pending the same real-Desktop acceptance boundary.

## System Boundary

Desktop and CLI remain clients of Codex Core. The patch changes provider/model routing, not execution-host routing:

```text
Desktop or CLI
    |
    | existing string model field: provider::model
    v
Codex App Server / Core task
    |-- existing execution environment --> local or remote execution host
    `-- selected provider runtime -------> OpenAI or named U-M service
```

The execution host, working directory, sandbox, approvals, collaboration mode, and task identity are independent of the selected model provider.

The desktop renderer is not patched. `CODEX_CLI_PATH` tells a compatible desktop package which Codex CLI/App Server executable to start. This hook was observed in Linux applications and is the experimental handoff under test on macOS; it is not a documented stable OpenAI interface and must be retested after desktop updates.

## Runtime Layout

A rooted context places all persistent launch state in one ignored envelope:

```text
ROOT/.codex-configure/
|-- .gitignore
|-- root.toml                         # schema marker; directory presence is insufficient
|-- launch.toml                       # default Core/provider, never credentials
|-- launch.sh                         # generated dispatcher
|-- codex-home/                       # context-specific CODEX_HOME
|-- xdg/{config,data,state,cache}/
|-- electron-user-data/
`-- chrome/{home,profile,chrome-native-hosts-v2.json}
```

The caller's `PWD` remains the task workspace. Runtime sockets and temporary files use a short private path under `/run/user/$UID/codex-configure/<root-hash>/`, falling back to `/tmp/codex-configure-$UID/<root-hash>/`. This is configuration, identity, and binary isolation rather than a security boundary.

Within either the global or rooted Codex home, the tool adds one managed subtree:

```text
$CODEX_HOME/
|-- auth.json                          # Codex-owned; never modified
|-- config.toml                        # materialized active configuration
|-- sessions/                          # shared task history; never copied
`-- codex-configure/
    |-- .env                           # all named provider keys, mode 0600
    |-- state.toml                     # active profile and config hash
    |-- providers.d/
    |   `-- <shortname>.toml           # one non-secret provider descriptor
    |-- catalogs/
    |   `-- <shortname>.json           # selected Codex ModelsResponse
    |-- profiles/
    |   |-- openai/
    |   `-- <shortname>/
    |-- base/
    |   |-- config.toml                # maintained OpenAI base
    |   `-- original-config.toml       # immutable first-run snapshot
    |-- locks/
    `-- recovery/
```

Tool-owned directories use mode `0700`; state, descriptors, catalogs, profiles, recovery data, and credentials use mode `0600`.

Prebuilt Dynamic Core releases live outside `CODEX_HOME` so custom Codex homes share one installation:

```text
~/.codex-configure/cores/
|-- codex-configure-core-<version>-<target>/
|   |-- codex
|   |-- codex-code-mode-host
|   |-- manifest.json
|   |-- LICENSE
|   `-- NOTICE
`-- current -> codex-configure-core-<version>-<target>
```

Global initialization also writes `~/.codex-configure/launch.toml` and `launch.sh` beside `cores/`. It never writes `root.toml` there, which keeps the shared global store distinct from a directory launch root.

Installations are immutable by convention and versioned for rollback. Runtime accepts `current` only when it selects the installed Python package's version; rollback therefore means installing the matching earlier Python package and rerunning setup. `setup dynamic` verifies a release SHA-256 sidecar, a manifest tied to the installed Python package's upstream pin and patch hash, and both executable hashes before atomically replacing the `current` symlink. It rejects unexpected archive members, links, special files, and unsafe paths. The release checksum detects corruption but is distributed with the asset and is not an independent signature.

Initialization is considered complete when the base/recovery state exists and every external descriptor present has a valid catalog. An OpenAI-only home is valid. A user may copy a complete non-secret provider layout and supply its declared keys through the environment instead of repeating interactive setup.

## Provider Contract

A short name is lowercase ASCII alphanumeric text separated by hyphens or underscores. `openai`, `ollama`, and `lmstudio` are reserved. Hyphens normalize to underscores for the credential name, so `research-2026` uses `RESEARCH_2026_API_KEY`; names that would collide after normalization are rejected.

Each descriptor is a Core configuration fragment with exactly one provider table and one mandatory catalog:

```toml
schema_version = 1
kind = "umich-toolkit"
model_catalog_json = "../catalogs/teaching.json"

[model_providers.teaching]
name = "U-M GPT Toolkit - teaching"
base_url = "https://api.portkey.ai/v1"
wire_api = "responses"
requires_openai_auth = false
env_http_headers = { "x-portkey-api-key" = "TEACHING_API_KEY" }
```

The provider ID is inferred from the sole table key. A descriptor never contains a key value.

`init` gets the key-scoped list from `https://api.toolkit.umgpt.umich.edu/v1/models` using `x-portkey-api-key`. It then joins endpoint IDs to complete metadata from `codex debug models --bundled`. All advertised IDs are shown, but only models with metadata supported by the installed Core can be selected. The generated JSON includes only selected complete entries.

Static catalogs are authoritative. Neither `codex-configure` nor the patch treats a missing catalog as "all models," because doing so would require inventing model capabilities and prompt metadata. A bad descriptor or catalog is warned about and skipped at Core startup.

Catalog presence is discovery, not entitlement. A provider can still reject a listed model because of deployment access, account policy, or budget.

## Credentials

OpenAI authentication remains entirely under Codex's ownership. This project does not edit or replace `auth.json`.

Named keys live in `$CODEX_HOME/codex-configure/.env`. An explicitly exported variable with the descriptor's exact name overrides its stored value for that process. Credential loading is filtered to names declared by installed descriptors; unrelated API keys from the shell are not copied into the tool's credential map.

A stock OpenAI child receives no U-M credentials. A stock named-provider child receives only its key. A Dynamic Picker child receives the keys for all providers it exposes and refuses to launch if one is missing. Secrets are never written into active configuration, descriptors, catalogs, profiles, backups, diagnostics, patch resources, or canary output.

## Activation And Recovery

The first initialization snapshots the existing `config.toml` before activation. Every switch or restore then uses the same transaction:

1. Acquire the per-home activation lock.
2. Recover or finalize an interrupted earlier transaction.
3. Reconcile safe non-routing changes written by Codex.
4. Verify that tool-owned routing fields were not changed externally.
5. Write the prior consistent config/state pair to recovery.
6. Atomically promote the new config and state.
7. Record the new pair as last known good and clear the transaction.

The tool owns only the routing fields it materializes: `model`, `model_provider`, `model_catalog_json`, and the selected provider table. Other settings and unrelated provider definitions are retained. An ambiguous routing edit is rejected rather than overwritten.

Patched Core may persist a qualified model while Dynamic Picker is active. On a stock OpenAI launch, an `openai::` prefix is removed and an external-qualified model is omitted, allowing stock Core to choose a supported OpenAI default instead of sending an external namespace to ChatGPT.

`restore` activates the maintained OpenAI base. `restore --original` activates the immutable first-run snapshot. Backups are narrow and never recursively copy `CODEX_HOME`, so credentials, authentication, sessions, logs, skills, and plugins are not swept into recovery data.

## Dynamic Core Patch

The canonical patch, upstream pin, build helper, and canary live under `src/core-provider-model-picker/`. The upstream repository is not vendored.

Most new startup discovery logic is isolated in the added Core module `external_provider_catalogs.rs`. Surgical hooks:

- load descriptors once while `Config` is constructed;
- merge valid provider definitions and retain their static catalogs;
- aggregate OpenAI and external catalogs for App Server `model/list`;
- parse the first `::` in the existing string-valued model field;
- resolve a provider-specific model manager/client at committed turn boundaries; and
- persist and restore the selected provider with existing task settings.

The picker displays and returns the same qualified value, for example `teaching::gpt-5.6-terra`. Core sends only `gpt-5.6-terra` to that provider. Unqualified models retain upstream behavior and use the task's current provider.

A user may change provider between completed turns without forking. Provider transport, authentication, sticky-routing state, and prompt-cache state are turn-scoped and rebuilt for the selected provider. Provider reasoning items can contain opaque state another provider cannot validate, so they are removed at a provider boundary while user, assistant, and tool history remain. Resume reconstructs the same boundaries from persisted settings.

## Dynamic Core Release

The native binaries and Python package share a version. Release preparation is intentionally two-stage so PyPI never points users at an absent Core asset:

1. Bump the package version and complete the patch refresh and targeted checks.
2. Push the version tag and create a GitHub draft release for that tag.
3. Manually run both `Build Linux Dynamic Core` and `Build macOS Dynamic Core` with the draft tag. They check out that tag, build native release executables, create target-specific archives and checksums, and upload them to the draft without replacing an existing asset.
4. Inspect both workflow results, retrieve the draft assets, verify their checksums, and run the applicable platform acceptance against the candidates.
5. Publish the draft release. The `Publish to PyPI` workflow downloads and verifies both required target assets before publishing the wheel and source distribution through PyPI Trusted Publishing.
6. On clean compatible accounts, run the documented `pipx install codex-configure && codex-configure setup dynamic` path as a public-download smoke check.

Keeping draft publication as the human gate prevents an uninspected binary build from publishing the Python installer. A failed or repeated binary workflow leaves the draft unpublished and does not overwrite existing assets.

GitHub Actions artifacts remain temporary CI evidence. The copies attached to a published GitHub release are the durable user downloads and do not use the workflow artifact retention period.

## Patch Refresh

OpenAI publishes Codex frequently, so the patch is deliberately pinned and source-based. To refresh it:

1. Choose an official `openai/codex` commit and update `src/core-provider-model-picker/upstream-pin.env` only as part of the same change as the patch.
2. Prepare a fresh dedicated checkout; do not develop against an installed desktop package or vendor the upstream tree.
3. Apply the prior patch, resolve only upstream conflicts required by the existing behavior, and keep new registry logic in its own Rust module.
4. Regenerate `codex-provider-model-picker.patch` as a binary-capable Git diff from the clean pin, including added files.
5. Run `git apply --check`, the focused external-catalog/parser/session/App Server checks recorded beside the patch, and one release build.
6. Run the Linux VM acceptance below before claiming the refreshed pin works with Desktop.

`codex-configure patch [PATH]` remains the one-command source-build fallback. It validates the Git root and canonical remote, checks out the exact pin without reset or broad cleanup, applies the patch to the index, checks the pinned minimum Rust version, builds stripped release `codex` and `codex-code-mode-host` executables, verifies both, and safely recognizes an idempotent rerun. Runtime resolution prefers an explicit `CODEX_CLI_PATH`, then the installed `cores/current` release, then the default source build. The builder refuses symlinks, wrong remotes, unrelated dirty state, and a patch that no longer applies.

## Manual Linux VM Acceptance

This is an agent-invoked release check, not CI. Use a dedicated Linux VM account and an isolated acceptance directory. Do not point tests at that account's normal `~/.codex`.

1. Confirm the VM is running, SSH is reachable, the desktop package is installed, and enough disk is available. Discover live VM state rather than assuming an address or libvirt alias.
2. Install the candidate Python package in the VM account. Use an authenticated `gh release download` to retrieve the draft archive and sidecar, run `sha256sum --check`, and unpack it in the isolated acceptance directory. For a patch-refresh diagnosis, a host source build made with `codex-configure patch /absolute/dedicated/path` may be copied instead. Keep `codex` and `codex-code-mode-host` together. Copy only the intended `auth.json` into the isolated acceptance home, set it to mode `0600`, and never copy the whole normal Codex home.
3. Set an isolated `CODEX_HOME` in the VM acceptance directory. Run `codex-configure init` twice with disposable low-budget keys and distinct short names. Confirm two descriptors, two catalogs, two distinct `.env` variables, mode `0600`, and no key text outside `.env`.
4. Run the stdlib canary in `src/core-provider-model-picker/app_server_canary.py` once for each external provider. Supply the patched binary, isolated Codex home, provider short name, and an isolated task directory. The canary proves qualified catalog entries, OpenAI-to-external continuity in one task, and provider restoration after App Server restart without printing credentials.
5. Start `codex-configure run desktop` with `CODEX_CLI_PATH` set to the unpacked draft candidate. For a graphics-limited VM, set `CODEX_DESKTOP_COMMAND='chatgpt --use-angle=swiftshader'`.
6. In a private VM display, verify the real picker shows `openai::...` and both external namespaces. Make one exact-marker turn with OpenAI, change to each named provider between turns, return to OpenAI, restart Desktop, and resume the same task. Confirm the semantic markers survive and the last provider/model is restored.
7. Run `run first/cli`, `run second/cli`, and `run openai/cli` with exact-marker prompts to cover stock Core profile launches. Confirm stock launches remove an inherited `CODEX_CLI_PATH`.
8. Compare the normal Codex home's pre/post hashes and permissions. It must be unchanged. Retain only non-secret logs, task IDs, versions, and screenshots needed to identify the tested pin and desktop build.

The automated canary is the fast regression signal. Desktop acceptance is intentionally one bounded visual pass because picker rendering and the `CODEX_CLI_PATH` handoff cannot be proven by a Core build alone.

## Manual macOS Apple Silicon Acceptance

Use an Apple Silicon Mac with the ChatGPT desktop app and a testable U-M Toolkit allocation. On a managed Mac, stop and report any policy block rather than disabling security controls.

1. Confirm `uname -m` reports `arm64`, record `sw_vers`, and fully quit ChatGPT.
2. Install the candidate package, run `codex-configure setup dynamic`, and confirm it installs a verified `macos-arm64` Core without Git or Rust.
3. Run `codex-configure init` with the tester's own Toolkit key. Confirm the key is absent from `config.toml`, catalogs, and command output.
4. Run `codex-configure run desktop`. Confirm ChatGPT remains signed in and the picker shows qualified OpenAI and named U-M entries.
5. Complete one turn with OpenAI, switch to the named U-M provider for another turn, return to OpenAI, restart ChatGPT through `codex-configure run desktop`, and resume the same task.
6. Report the package version, ChatGPT version, picker result, turn results, and any launch or managed-security error. Do not send keys, `auth.json`, or the protected `.env` file.

## Prior Acceptance Evidence

The original Linux spike demonstrated a stock desktop loading the patched Core through `CODEX_CLI_PATH`, qualified OpenAI and U-M entries in the real picker, OpenAI to U-M to OpenAI turns in one task, and restart/resume restoration. That run also exposed the provider-reasoning boundary that the final patch now handles.

A Desktop `GET /backend-api/accounts/.../settings` 401 with `Must use workspace account for this operation` occurred with both stock and patched Core before any U-M selection. Desktop stayed signed in and both providers continued working. The A/B result treats that account-settings request as independent of binary patching; it is not evidence of patch attestation or an OpenAI sign-out.

## Durable Decisions

- Keep one `CODEX_HOME` per launch context; providers within that context share its task universe.
- Recognize roots by `root.toml`, not by `.codex-configure` directory presence, because the global Core store uses the same directory name.
- Preserve the caller's working directory and isolate persistent Codex, XDG, Electron, and browser state under the root.
- Keep the ordinary interface to read-only status, `init`, and `launch`; retain `run` as explicit advanced control.
- Leave execution-host routing unchanged.
- Keep provider/model in the existing string field as `provider::model`.
- Allow provider changes only at committed turn boundaries.
- Keep the desktop renderer and installed package unchanged.
- Require complete static external catalogs.
- Keep keys in one protected tool-owned file rather than a keychain or `auth.json`.
- Preserve and recover `config.toml` transactionally.
- Pin and rebuild the smallest maintainable Core patch instead of vendoring Codex.
- Keep the validated Dynamic Picker claim on Linux x86_64 with the Ubuntu 22.04 / glibc 2.35 baseline; expose macOS arm64 as experimental until it passes the same acceptance boundary.
