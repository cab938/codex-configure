# Architecture

This document records the durable design, patch-maintenance boundary, and platform acceptance procedures for `codex-configure`. Installation and ordinary commands belong in the project [README](../README.md).

## Scope

`codex-configure` supports one stock provider and any number of named external services in a directory-scoped launch root:

- OpenAI uses Codex's existing ChatGPT authentication and bundled model catalog.
- Each U-M service uses its own Portkey API key, static selected catalog, and short provider name.
- Each local service uses an OpenAI-compatible Responses base URL, an optional bearer key, a narrow static catalog, and a short provider name.

It deliberately provides two operating modes.

| Invocation | Core | Provider choice |
| --- | --- | --- |
| `launch [desktop|cli]` | Configured default | Configured default |
| `launch chrome` | Configured Core in the extension native host | Configured fixed provider or Dynamic Picker |
| `launch -- COMMAND [ARGS...]` | None (arbitrary executable) | Rooted environment only; no provider activation |
| `run openai/cli` or `run openai/desktop` | Stock | OpenAI fixed for that launch |
| `run name/cli` or `run name/desktop` | Stock | Named external service fixed for that launch |
| `run cli` or `run desktop` | Patched | Qualified provider/model in the existing picker |

Bare `codex-configure` is a read-only status operation. `init` owns context creation, provider management, and Core/default-provider selection through one state-driven full-screen TUI on an interactive terminal. The TUI stages profile additions/removals, an optional auth-only copy, and launch-default changes until explicit Save; a non-TTY or unavailable-curses path retains the sequential text interface. Ordinary commands resolve only the exact current directory: they never search parents or fall back to a normal or legacy global Codex home. The explicit advanced `--codex-home PATH` option can manage a supplied Codex home but never creates a launcher or installs a project Core. Fixed-provider Stock Core launches support macOS and Linux. Dynamic Picker is acceptance-tested on Linux x86_64 with the Ubuntu 22.04 / glibc 2.35 baseline. Native macOS arm64 installation and launch are available as an experimental path pending the same real-Desktop acceptance boundary.

`launch -- COMMAND [ARGS...]` is the integration escape hatch for another launcher,
such as a teaching harness. It constructs the same rooted environment, preserves the
caller's `PWD`, and executes the command directly with its argument vector. It does
not select a provider, rewrite `config.toml`, or activate a patched Core. A command
is required after `--`.

## System Boundary

Desktop, CLI, and the Chrome extension native host remain clients of Codex Core. The patch changes provider/model routing, not execution-host routing:

```text
Desktop, CLI, or Chrome extension native host
    |
    | existing string model field: provider → model
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
|-- cores/                            # prebuilt Dynamic Core, when selected
|-- codex-core/                       # default patched source checkout, when requested
|-- xdg/{config,data,state,cache}/
|-- electron-user-data/
`-- chrome/{home,profile}              # profile receives the validated host manifest
```

The caller's `PWD` remains the task workspace. Runtime sockets and temporary files use a short private path under `/run/user/$UID/codex-configure/<root-hash>/`, falling back to `/tmp/codex-configure-$UID/<root-hash>/`. This is configuration, identity, and binary isolation rather than a security boundary.

`CODEX_CHROME_USER_DATA_DIR` and `CODEX_CHROMIUM_USER_DATA_DIR` identify the root's isolated browser profile to current Desktop and browser integration code. On a Dynamic Picker Chrome launch, `CODEX_CLI_PATH` and all configured provider credentials are inherited by the browser extension's native host. Stock Chrome launches remove `CODEX_CLI_PATH` and expose at most the configured fixed provider's key.

The Desktop plugin lifecycle, not `codex-configure`, owns native-host installation. On Linux it materializes the Chrome registration below rooted `XDG_CONFIG_HOME` and routing manifests below rooted `XDG_STATE_HOME` and `CODEX_HOME`:

```text
xdg/config/google-chrome/NativeMessagingHosts/com.openai.codexextension.json
xdg/state/openai-codex/chrome-native-hosts-v2.json
codex-home/chrome-native-hosts-v2.json
chrome/profile/NativeMessagingHosts/com.openai.codexextension.json
```

Desktop creates the first three artifacts. Before launching Chrome with its explicit custom user-data directory, `codex-configure` validates Desktop's native-host registration and atomically mirrors that exact manifest to the fourth path, where custom-profile Chrome resolves it. It does not fabricate plugin or routing state. It also detects the stable ChatGPT extension in the isolated profile and opens the official Chrome Web Store listing while it is absent. Browser permission acceptance remains a user action. The former empty `chrome/chrome-native-hosts-v2.json` placeholder is neither created nor exported as authoritative state; an existing copy is left untouched and ignored.

A launch root starts with an empty Codex home and isolated desktop state. During setup, a successful `codex login status` against normal `~/.codex` enables an explicit auth-only copy action. That action validates and atomically creates only `auth.json` at mode `0600`; it refuses symlinks and an existing destination. Tasks, settings, skills, plugins, sessions, U-M keys, and other files are never copied.

Within the rooted Codex home, the tool adds one managed subtree:

```text
$CODEX_HOME/
|-- auth.json                          # Codex-owned; optionally copied into a fresh root
|-- config.toml                        # materialized active configuration
|-- sessions/                          # shared task history; never copied
`-- codex-configure/
    |-- .env                           # all named provider keys, mode 0600
    |-- state.toml                     # active profile and config hash
    |-- providers.d/
    |   `-- <shortname>.toml           # one non-secret provider descriptor
    |-- catalogs/
    |   `-- <shortname>.json           # selected Codex ModelsResponse
    |-- cache/
    |   |-- known-local-models-v1.json # last valid owned catalog
    |   `-- known-local-models-v1.meta.json
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

Prebuilt Dynamic Core releases live outside `CODEX_HOME` but inside the owning launch root:

```text
ROOT/.codex-configure/cores/
|-- codex-configure-core-<version>-<target>/
|   |-- codex
|   |-- codex-code-mode-host
|   |-- manifest.json
|   |-- LICENSE
|   `-- NOTICE
`-- current -> codex-configure-core-<version>-<target>
```

Installations are immutable by convention and versioned inside the root. Runtime accepts `current` only when it selects the installed Python package's version. Selecting Dynamic Picker during `init` installs it immediately; a later root-local `setup dynamic` verifies or reinstalls it. Installation verifies a release SHA-256 sidecar, a manifest tied to the installed Python package's upstream pin and patch hash, and both executable hashes before atomically replacing the `current` symlink. It rejects unexpected archive members, links, special files, and unsafe paths. The release checksum detects corruption but is distributed with the asset and is not an independent signature.

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

The provider ID is inferred from the sole table key. A descriptor never contains a key value. A local Responses endpoint uses the same contract with `kind = "local-responses"`, its own `base_url`, and either `env_key = "LOCAL_API_KEY"` for bearer authentication or no credential field at all.

`init` gets the key-scoped list from `https://api.toolkit.umgpt.umich.edu/v1/models` using `x-portkey-api-key`. It then joins endpoint IDs to complete metadata from `codex debug models --bundled`. All advertised IDs are shown, but only models with metadata supported by the installed Core can be selected. The generated JSON includes only selected complete entries.

Static provider catalogs are authoritative. Neither `codex-configure` nor the patch treats a missing catalog as "all models." U-M catalogs contain complete entries joined to the pinned Core catalog. Local catalogs use a deliberately narrow schema: slug, display text, optional description, input modalities, priority, optional context window, optional supported/default reasoning levels, and optional reasoning-summary parameter support. Core expands each local entry from its unknown-model fallback and explicitly clears unrelated bundled capabilities; it never clones another model's metadata based on a similar slug. A bad descriptor or catalog is warned about and skipped at Core startup.

Catalog presence is discovery, not entitlement. A provider can still reject a listed model because of deployment access, account policy, or budget.

### Owned local-model catalog

Local setup and reconfiguration perform a bounded network join; launch, status, and doctor remain offline. Setup conditionally fetches `https://raw.githubusercontent.com/cab938/codex-configure/main/catalog/v1/local-models.json` over HTTPS with a 10-second timeout, 2 MiB response limit, and ETag support. That request has no endpoint credential. It then queries `<base_url>/models` with the endpoint's optional bearer credential, excludes obvious embedding and reranking IDs, and exact-matches the remaining case-sensitive IDs. There is no normalization, fuzzy matching, or alias pattern.

The checked-in remote document freezes `schema_version: 1`. It records generation time, the Models.dev source URL, retrieval time and SHA-256, and one record per endpoint ID. A record contains its Models.dev ID, display metadata, reported context and input modalities, and optional sanitized probe evidence. Models.dev seeds source-owned facts, but clients never contact Models.dev and its generic schema is not treated as a Codex catalog.

The join has three states:

- `tested`: `/models`, streamed Responses text, and the complete standard function-call/function-output continuation all passed for the exact endpoint ID;
- `known`: the exact ID is in the owned catalog but lacks that full baseline; and
- `unverified`: the endpoint advertises the ID but the owned catalog does not contain it.

Context is the smaller positive endpoint/catalog value, or the sole available value. Vision requires a passing image probe. Reasoning choices are exposed only for efforts whose requests passed and when a passing default is recorded; this certifies API acceptance, not a behavioral change in reasoning depth. Reasoning summaries require their separate passing check. Standard tools remain exposed for every generation model through Core's conservative fallback, while only passing evidence produces a `tools tested` claim. Native freeform patching, web search, service tiers, audio, advanced tools, multi-agent metadata, verbosity controls, and model instructions are not represented by the local schema.

The last valid remote response is cached at `$CODEX_HOME/codex-configure/cache/known-local-models-v1.json`; its adjacent metadata contains the URL, ETag, response SHA-256, and fetch time. A `304` revalidates and reuses it. Network, size, decoding, JSON, schema, or version failures fall back to that validated cache with a visible setup warning. Without a valid cache, setup continues with endpoint entries marked unverified. Local `profile.toml` files written by this flow use schema v2 and record `fresh`, `cached`, or `unavailable` provenance in an optional `known_catalog` table. Existing schema-v1 profiles remain readable and retain their materialized catalogs until explicitly reconfigured.

Catalog maintenance is manual. `scripts/known_model_catalog.py import` refreshes selected Models.dev records while preserving same-model evidence; `probe` emits a sanitized report; `certify` validates and merges one exact-ID report; and `validate` enforces the frozen schema and evidence gates. A reviewed commit to `main` publishes record changes without a wheel release. The live catalog is intentionally outside Python package data. A future incompatible schema receives a new URL and a coordinated client/Core release.

## Credentials

OpenAI authentication remains under Codex's ownership. This project may copy a valid `auth.json` into a fresh root only after the user selects that action; it never inspects token values, edits the document, or overwrites an existing destination.

Named keys live in `$CODEX_HOME/codex-configure/.env`. U-M keys are mapped to their required HTTP header; local keys use Codex's bearer `env_key` field. Local endpoints may omit authentication. An explicitly exported variable with the descriptor's exact name overrides its stored value for that process. Credential loading is filtered to names declared by installed descriptors; unrelated API keys from the shell are not copied into the tool's credential map.

A stock OpenAI child receives no U-M credentials. A stock named-provider child receives only its key. A Dynamic Picker child receives the keys for all providers it exposes and refuses to launch if one is missing. The same rule applies to Chrome so its native host can authenticate without writing keys into browser or host manifests. Secrets are never written into active configuration, descriptors, catalogs, profiles, backups, diagnostics, patch resources, or canary output.

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

Before a stock-provider launch rewrites that state, Linux lifecycle detection
reads each matching client's `/proc/<pid>/environ` and compares its effective
`CODEX_HOME` and root markers with the target launch root. Normal/global clients
and clients from another root are independent and do not block. A same-root
client or an environment that cannot be attributed safely remains blocking and
the error identifies the conflicting boundary. Platforms without a reliable
process-environment surface retain the conservative name-based guard. Restore
also retains the broad guard because it is not dispatched through a particular
launch root.

Patched Core may persist a qualified model while Dynamic Picker is active. On a stock OpenAI launch, the namespace is removed from an OpenAI-qualified model such as `openai → gpt-5.6-sol`; an external-qualified model is omitted, allowing stock Core to choose a supported OpenAI default instead of sending an external namespace to ChatGPT. The former `::` separator remains accepted as a migration input for existing persisted settings.

`restore` activates the maintained OpenAI base. `restore --original` activates the immutable first-run snapshot. Backups are narrow and never recursively copy `CODEX_HOME`, so credentials, authentication, sessions, logs, skills, and plugins are not swept into recovery data.

## Dynamic Core Patch

The canonical patch, upstream pin, build helper, and canary live under `src/core-provider-model-picker/`. The upstream repository is not vendored.

Most new startup discovery logic is isolated in the added Core module `external_provider_catalogs.rs`. Surgical hooks:

- load descriptors once while `Config` is constructed;
- merge valid provider definitions and retain their static catalogs;
- aggregate OpenAI and external catalogs for App Server `model/list`;
- parse the first ` → ` in the existing string-valued model field, with read compatibility for the former `::` separator;
- resolve a provider-specific model manager/client at committed turn boundaries; and
- persist and restore the selected provider with existing task settings.

The picker displays and returns the same qualified value, for example `teaching → gpt-5.6-terra`. Core sends only `gpt-5.6-terra` to that provider. Unqualified models retain upstream behavior and use the task's current provider.

A user may change provider between completed turns without forking. Provider transport, authentication, sticky-routing state, and prompt-cache state are turn-scoped and rebuilt for the selected provider. Provider reasoning items can contain opaque state another provider cannot validate, so they are removed at a provider boundary while user, assistant, and tool history remain. Resume reconstructs the same boundaries from persisted settings.

## Dynamic Core Release

The native binaries and Python package share a version. Release preparation is intentionally two-stage so PyPI never points users at an absent Core asset:

1. Bump the package version and complete the patch refresh and targeted checks.
2. Push the version tag and create a GitHub draft release for that tag.
3. Manually run both `Build Linux Dynamic Core` and `Build macOS Dynamic Core` with the draft tag. They check out that tag, build native release executables, create target-specific archives and checksums, and upload them to the draft without replacing an existing asset.
4. Inspect both workflow results, retrieve the draft assets, verify their checksums, and run the applicable platform acceptance against the candidates.
5. Publish the draft release. The `Publish to PyPI` workflow downloads and verifies both required target assets before publishing the wheel and source distribution through PyPI Trusted Publishing.
6. On clean compatible accounts, install with pipx, create a disposable project root with `codex-configure init`, select Dynamic Picker, and run the documented launch smoke check.

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

`codex-configure patch [PATH]` remains the one-command source-build fallback. Its default checkout is `ROOT/.codex-configure/codex-core/`. It validates the Git root and canonical remote, checks out the exact pin without reset or broad cleanup, applies the patch to the index, checks the pinned minimum Rust version, builds stripped release `codex` and `codex-code-mode-host` executables, verifies both, and safely recognizes an idempotent rerun. Runtime resolution prefers an explicit `CODEX_CLI_PATH`, then the root's installed `cores/current` release, then the root's default source build. The builder refuses symlinks, wrong remotes, unrelated dirty state, and a patch that no longer applies.

## Manual Linux VM Acceptance

This is an agent-invoked release check, not CI. Use a dedicated Linux VM account and an isolated acceptance directory. Do not point tests at that account's normal `~/.codex`.

1. Confirm the VM is running, SSH is reachable, the desktop package is installed, and enough disk is available. Discover live VM state rather than assuming an address or libvirt alias.
2. Install the candidate Python package in the VM account and create a disposable project directory. If testing a draft candidate directly, keep `codex` and `codex-code-mode-host` together and point only that run at them with `CODEX_CLI_PATH`.
3. Run `codex-configure init` in that directory. Exercise either sign-in-later or an auth-only copy made from synthetic acceptance credentials, then add disposable low-budget Toolkit profiles with distinct exact names in one repeated provider loop. Confirm two descriptors, two catalogs, two distinct `.env` variables, mode `0600`, no key text outside `.env`, and no copied normal-home files other than the deliberately selected `auth.json`.
4. Run the stdlib canary in `src/core-provider-model-picker/app_server_canary.py` once for each external provider. Supply the patched binary, isolated Codex home, provider short name, and an isolated task directory. The canary proves qualified catalog entries, OpenAI-to-external continuity in one task, and provider restoration after App Server restart without printing credentials.
5. Start `codex-configure run desktop` with `CODEX_CLI_PATH` set to the unpacked draft candidate. For a graphics-limited VM, set `CODEX_DESKTOP_COMMAND='chatgpt --use-angle=swiftshader'`.
6. In a private VM display, verify the real picker shows `openai → ...` and both external namespaces. Make one exact-marker turn with OpenAI, change to each named provider between turns, return to OpenAI, restart Desktop, and resume the same task. Confirm the semantic markers survive and the last provider/model is restored.
7. Run `run first/cli`, `run second/cli`, and `run openai/cli` with exact-marker prompts to cover stock Core profile launches. Confirm stock launches remove an inherited `CODEX_CLI_PATH`.
8. Compare the normal Codex home's pre/post hashes and permissions. It must be unchanged. Retain only non-secret logs, task IDs, versions, and screenshots needed to identify the tested pin and desktop build.

The automated canary is the fast regression signal. Desktop acceptance is intentionally one bounded visual pass because picker rendering and the `CODEX_CLI_PATH` handoff cannot be proven by a Core build alone.

## Manual macOS Apple Silicon Acceptance

Use an Apple Silicon Mac with the ChatGPT desktop app and a testable U-M Toolkit allocation. On a managed Mac, stop and report any policy block rather than disabling security controls.

1. Confirm `uname -m` reports `arm64`, record `sw_vers`, and fully quit ChatGPT.
2. Install the candidate package and create a disposable project directory.
3. Run `codex-configure init` with the tester's own Toolkit key, select Dynamic Picker, and confirm setup installs a verified root-local `macos-arm64` Core without Git or Rust. Confirm the key is absent from `config.toml`, catalogs, and command output.
4. Run `codex-configure run desktop`. Confirm ChatGPT remains signed in and the picker shows qualified OpenAI and named U-M entries.
5. Complete one turn with OpenAI, switch to the named U-M provider for another turn, return to OpenAI, restart ChatGPT through `codex-configure run desktop`, and resume the same task.
6. Report the package version, ChatGPT version, picker result, turn results, and any launch or managed-security error. Do not send keys, `auth.json`, or the protected `.env` file.

## Prior Acceptance Evidence

The original Linux spike demonstrated a stock desktop loading the patched Core through `CODEX_CLI_PATH`, qualified OpenAI and U-M entries in the real picker, OpenAI to U-M to OpenAI turns in one task, and restart/resume restoration. That run also exposed the provider-reasoning boundary that the final patch now handles.

A Desktop `GET /backend-api/accounts/.../settings` 401 with `Must use workspace account for this operation` occurred with both stock and patched Core before any U-M selection. Desktop stayed signed in and both providers continued working. The A/B result treats that account-settings request as independent of binary patching; it is not evidence of patch attestation or an OpenAI sign-out.

## Durable Decisions

- Keep one `CODEX_HOME` per launch context; providers within that context share its task universe.
- Resolve ordinary commands only from the exact current directory; provide no global or parent fallback.
- Recognize roots by `root.toml`, not by `.codex-configure` directory presence.
- Preserve the caller's working directory and isolate persistent Codex, XDG, Electron, and browser state under the root.
- Let Desktop own Chrome native-host registration; pass the configured Core and bounded credentials through the isolated Chrome process environment.
- Keep installed and source-built Dynamic Core binaries inside their owning project root for complete project-local removal.
- Permit only an explicit, non-overwriting auth-only copy from a detected normal Codex home.
- Keep the ordinary interface to read-only status, `init`, and `launch`; retain `run` as explicit advanced control.
- Leave execution-host routing unchanged.
- Keep provider/model in the existing string field as `provider → model`.
- Allow provider changes only at committed turn boundaries.
- Keep the desktop renderer and installed package unchanged.
- Require authoritative static provider catalogs, using complete bundled metadata for U-M and the narrow conservative schema for local models.
- Resolve local metadata only during setup by exact-joining `/models` to the owned, versioned catalog; never route or refresh it at startup.
- Keep keys in one protected tool-owned file rather than a keychain or `auth.json`.
- Preserve and recover `config.toml` transactionally.
- Pin and rebuild the smallest maintainable Core patch instead of vendoring Codex.
- Keep the validated Dynamic Picker claim on Linux x86_64 with the Ubuntu 22.04 / glibc 2.35 baseline; expose macOS arm64 as experimental until it passes the same acceptance boundary.
