# Design

Status: first OpenAI/U-M prototype implemented; broader provider and recovery work remains design scope.

## Implemented interaction

Version 0.1 exposes only two environments:

1. `OpenAI` proceeds directly to the Desktop/CLI launch choice.
2. `U-M GPT Toolkit` shows one provider, `OpenAI / Azure`, followed by persistent multi-model selection, default-model selection, and the Desktop/CLI launch choice.

The U-M model list is the intersection of U-M's advertised aliases and the installed Codex catalog. A generated catalog contains exactly the selected entries. Immediately before launch, the tool prints `$CODEX_HOME/codex-configure/profiles/` and the active profile directory.

Model discovery does not transmit the U-M credential because the investigated alias endpoint is public and returned the same list with and without authentication. The key is added only to the environment of the launched U-M Codex process.

The initial implementation deliberately excludes AWS Bedrock and Google even though the investigation demonstrated that the same key can route to them. They are evidence about the upstream account, not version-0.1 user-facing options.

## Problem

Codex users may need to work through several environments:

- the normal OpenAI service using an existing ChatGPT or OpenAI API login;
- an institutional OpenAI-compatible gateway such as U-M GPT Toolkit;
- another API-based provider;
- a local model service.

The Codex CLI can select named configuration profiles, but stock desktop clients do not currently document an equivalent profile selector. Existing institutional setup instructions may also overwrite `config.toml` and `auth.json`, making it awkward and risky to return to a user's original OpenAI setup.

`codex-configure` will provide one external environment selector for both CLI and desktop clients without modifying Codex core or the desktop application.

## Terms

- **Environment**: A user-facing combination of provider routing, catalog, credential reference, and launch behavior. Examples are `openai`, `umich`, and `local-lab`.
- **Codex profile**: A named Codex configuration layer. Official CLI profiles live next to `config.toml` and are selected with `codex --profile NAME`.
- **Active configuration**: The effective user configuration presented to a stock Codex client at startup.
- **Catalog**: The JSON model catalog selected by `model_catalog_json` or the upstream catalog used when that key is absent.

The user interface should say "environment" when the choice changes more than a model. Internally, the project may use Codex profile files where their behavior fits.

## Current Codex boundaries

The design is based on the following current behavior from the official OpenAI documentation:

- Codex state lives under `CODEX_HOME`, which defaults to `~/.codex`.
- CLI profiles are top-level TOML overlays stored as `$CODEX_HOME/NAME.config.toml` and selected with `--profile NAME`.
- A profile may override `model_catalog_json`.
- `model_catalog_json` is loaded when the Codex runtime starts.
- Custom provider routing belongs in user configuration, not project-local `.codex/config.toml`.
- Custom providers can use OpenAI authentication, an environment key, no authentication, or current command-backed bearer-token configuration.
- Official managed defaults and macOS managed preferences have higher precedence than user `config.toml`.

References:

- [Advanced configuration and profiles](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Authentication](https://learn.chatgpt.com/docs/auth)
- [Managed configuration](https://learn.chatgpt.com/docs/enterprise/managed-configuration)

At the time of this design, the official profile documentation describes CLI selection but not a stock desktop profile selector. The desktop path therefore requires activating an environment before launching the app.

## Current U-M evidence and hypotheses

The only available institutional authority is the user-facing setup material. It configures:

- provider identifier `toolkit`;
- base URL `https://api.toolkit.umgpt.umich.edu/v1`;
- default model `gpt-5.6-terra`;
- `requires_openai_auth = true` with the U-M key placed in Codex's shared OpenAI credential slot.

The initial unauthenticated request established only that Portkey was somewhere in the request path. A subsequent [U-M Portkey capability investigation](umich-portkey-investigation.md) established that the key is recognized by Portkey's API, the upstream workspace exposes Model Catalog providers, and request-scoped config overrides can route the same key through Azure OpenAI, AWS Bedrock, and Google.

The U-M gateway's unauthenticated `/models` endpoint returns a static 33-entry alias list and ignores documented Portkey filtering, pagination, and integrated-catalog controls. It is not an entitlement list. Portkey's direct integrated-catalog endpoint returns provider-qualified catalogs when the request explicitly overrides the key's attached query-router config. The configured `gpt-5.6-terra` default therefore reflects the default router rather than the complete usable provider set.

The key can list provider objects and use Model Catalog data-plane routing, but Portkey returns `AB03` for configs and integrations. Catalog administration is unavailable to this key. Portkey model-catalog functionality remains a discovery input rather than an MVP dependency; normalized last-known-good local catalogs and model canaries remain required.

## Goals

- Support stock Codex CLI and desktop clients on macOS and Linux.
- Discover an existing installation without changing it.
- Adopt the current setup as an initial environment and preserve a recoverable original snapshot.
- Support named OpenAI, institutional, API-based, and local environments.
- Generate, refresh, validate, cache, and select a model catalog per environment.
- Keep provider identifiers stable across switches.
- Preserve unrelated Codex configuration and shared task state.
- Keep the existing OpenAI login intact when provider-specific authentication is available.
- Make activation atomic, reversible, and diagnosable.
- Provide both an interactive terminal interface and scriptable commands.

## Non-goals for the first prototype

- Modifying or patching the stock desktop application.
- Reimplementing the Codex App Server or Responses API.
- Running several different active environments concurrently in one `CODEX_HOME`.
- Exposing U-M AWS Bedrock, Google, or local-provider routes.
- Guaranteeing that every arbitrary model identifier appears in the stock desktop picker before that behavior is tested.
- Distributing administrator policy through `managed_config.toml` or MDM.
- Managing all Codex settings or becoming a general-purpose TOML editor.

## General approach

### 1. Inventory before mutation

On first run, the tool should report:

- the resolved `CODEX_HOME`;
- whether `config.toml` and `auth.json` exist;
- the configured credential storage mode without printing credentials;
- `codex login status` when available;
- the Codex CLI version and required capabilities;
- configured provider identifiers and catalog paths;
- applicable managed configuration or requirements layers;
- whether a desktop client or App Server appears to be running.

The inventory should never print token values or copy credentials into diagnostic output.

### 2. Preserve a shared home

The prototype will keep one shared `CODEX_HOME`. Tasks, history, skills, plugins, memories, MCP state, logs, and databases remain in place. Only environment-owned routing and catalog configuration should change.

For the first prototype, skills, plugins, MCP servers, approval behavior, and other non-model settings are shared across environments. Per-environment capability visibility may be useful later, but it is deliberately deferred until model routing works.

Provider identifiers must be globally stable. For example, `umich-toolkit` must always mean the same endpoint. Every generated active configuration should retain the non-secret definitions required to understand previously used providers, even when one of them is not the active default.

### 3. Store a base plus environment overlays

The preferred model is:

1. Preserve the user's non-environment configuration as a base.
2. Store a small overlay for each environment.
3. Structurally merge the selected overlay into an effective configuration.
4. Materialize that configuration atomically as `$CODEX_HOME/config.toml` for stock desktop use.

The tool should own an explicit, narrow set of keys, initially including:

- `model`;
- `model_provider`;
- `model_catalog_json`;
- provider tables installed by `codex-configure`.

Unknown and unrelated keys should be preserved. Activation must use a TOML parser rather than regular-expression or line-oriented editing. The prototype should determine how to reconcile user edits made while an environment is active.

For CLI use, the tool may also project overlays into official `$CODEX_HOME/NAME.config.toml` files or activate an environment before starting the CLI. The prototype must test the interaction between official profile overlays and the effective configuration used by Desktop before choosing one canonical CLI path.

### 4. Keep catalogs environment-specific

The OpenAI environment should normally omit `model_catalog_json`, allowing Codex to use the normal upstream catalog and entitlements.

An institutional or local environment may declare a catalog source. The tool should fetch or generate into a temporary path, validate the result, and promote it only after validation. A failed refresh should retain the last-known-good catalog and report its age and source.

A catalog is not proof that the active credential can use a model. Prototype diagnostics should distinguish catalog presence, picker visibility, and a successful API request.

The U-M prototype should attempt model discovery in this order:

1. Make an authenticated request to the provider's standard model-list endpoint, expected initially to be `GET /v1/models`.
2. Record the response shape and normalize only fields that can be mapped safely into the Codex catalog schema.
3. If model listing is unsupported or incomplete, consume an institution-published allowlist or a locally maintained U-M catalog.
4. Retain the last-known-good catalog if discovery fails.

The tool must not infer access from the upstream OpenAI catalog, from a Portkey product capability that is not enabled, or from a model name observed in documentation. A small Responses API canary is still required for each model family that the profile exposes.

### 5. Separate provider credentials

The design leaves the user's existing OpenAI authentication cache untouched. Version 0.1 stores the U-M credential in `$CODEX_HOME/codex-configure/.env` with mode `0600`; `UMICH_TOOLKIT_API_KEY` and `--env-file PATH` are explicit overrides. The generated provider configuration contains only an `env_http_headers` reference, and the launcher supplies the value only to the U-M child process.

The U-M instructions currently use `requires_openai_auth = true` and replace the `OPENAI_API_KEY` entry in `auth.json`. In Codex, `requires_openai_auth = true` selects the shared OpenAI credential and ignores `env_key`. This appears to be an installation shortcut rather than evidence that the gateway requires Codex's shared OpenAI credential mechanism.

The demonstrated path is:

1. Read the key from the protected default file or an explicit override.
2. Configure a distinct custom provider whose `x-portkey-api-key` header references `UMICH_TOOLKIT_API_KEY` through `env_http_headers`.
3. Launch Desktop or CLI with that variable only for the U-M process.
4. Verify that the existing OpenAI OAuth or API login remains usable before and after the U-M run.

The existing `requires_openai_auth` configuration is a compatibility fallback to test only if the preferred path fails with evidence that the gateway requires it. Failure of one Codex authentication mechanism must not be interpreted automatically as failure of the underlying bearer token or API protocol.

If the gateway contract makes credential swapping unavoidable, it must be a clearly marked fallback:

- use predictable file-backed credential storage;
- treat `auth.json` as an opaque secret;
- stop all Codex processes before switching;
- save refreshed credentials on every switch-out;
- use mode `0700` directories and mode `0600` files;
- never include credentials in normal backups, logs, or catalogs.

### 6. Activate transactionally

An activation should follow this sequence:

1. Acquire a per-home lock.
2. Identify the current environment and detect external edits.
3. Require the desktop client and App Server to exit, offering a graceful quit rather than killing them silently.
4. Refresh the selected catalog when requested or required.
5. Materialize and validate the candidate configuration in a temporary file.
6. Save a last-known-good non-secret configuration snapshot.
7. Atomically replace the active configuration.
8. Record the active environment, hashes, and timestamps without secrets.
9. Launch the requested client.

If validation or promotion fails, the prior active configuration must remain usable.

### 7. Provide interactive and scriptable interfaces

The implemented terminal interface starts with:

```text
Codex environments

* OpenAI              active   ChatGPT login available
  U-M GPT Toolkit     ready    catalog: 6 models
```

The initial executable is an interactive launcher. It also provides `--prepare-only` and `--codex-home PATH` for isolated preparation and testing. Scriptable `list`, `doctor`, `use`, and `restore` commands remain future work:

```text
codex-configure list
codex-configure doctor
codex-configure use umich
codex-configure launch umich --desktop
codex-configure launch umich --cli
codex-configure restore
```

The proof of concept may be a portable shell program. A broadly distributed macOS/Linux tool will probably be safer as a single executable because structured TOML/JSON handling, cross-platform locking, native credential stores, and application lifecycle management are difficult to implement reliably in shell alone.

## Stock desktop picker risk

Current desktop builds may apply a separate client-side model availability allowlist after the App Server returns `model/list`. A custom catalog can therefore be valid while one of its model identifiers remains hidden from the native picker. This behavior was observed while developing the sibling Linux desktop project and is summarized in its `linux-features/api-key-model-visibility/README.md`.

The first stock-client canary must include:

- one upstream-known identifier exposed by U-M, such as the configured Codex model;
- one U-M-only or otherwise non-upstream identifier;
- confirmation of picker visibility;
- confirmation that a small task actually reaches the intended provider.

If the custom identifier is hidden, the prototype can still test an explicitly configured default model, but native arbitrary-model selection would remain an upstream Desktop limitation rather than a problem the external switcher can solve.

For version 1, arbitrary custom-model visibility is a recorded compatibility result rather than a release blocker. The minimum picker goal is to expose the U-M models whose identifiers the stock client already recognizes. A custom identifier remains in the canary so the project has evidence for future scope.

## Managed configuration boundary

Selectable environment values must not be installed as official managed defaults. On Unix systems, `/etc/codex/managed_config.toml` overrides the user's `config.toml`, and macOS MDM preferences have still higher precedence. The tool should detect these layers and explain conflicts, not try to overwrite or out-prioritize them.

Institutional deployment may distribute the `codex-configure` executable, profile definitions, catalog generators, and policy separately. Administrator-enforced requirements remain outside this tool's ownership.

## Prototype sequence

The first implementation should answer provider questions before building the interactive selector:

1. Implement read-only `doctor` and inventory behavior against synthetic Codex-home fixtures.
2. Add a `probe-provider` path that accepts the U-M key through an environment variable, never logs it, and attempts authenticated model discovery.
3. Exercise a minimal Responses API request for the documented Terra model, followed by streaming and a small tool-capability canary.
4. If discovery returns additional model identifiers, test one additional permitted model without assuming that higher-cost models are enabled.
5. Normalize the demonstrated model response into a local Codex catalog and validate it independently of Desktop.
6. Exercise the custom provider and catalog with the Codex CLI in a temporary home.
7. Implement transactional activation and restoration for the user's real home only after the isolated checks pass.
8. Run the stock Desktop picker canary and record known-identifier and custom-identifier behavior.
9. Build the interactive menu around the verified operations last.

This order keeps early failures attributable to authentication, provider protocol, catalog conversion, Codex configuration, or Desktop filtering instead of collapsing all five boundaries into one first run.

## Prototype acceptance criteria

The initial OpenAI/U-M prototype is successful when it can demonstrate all of the following on a stock client:

1. Inventory an existing OpenAI-authenticated home without mutation or secret disclosure.
2. Adopt and restore the original user configuration without semantic loss.
3. Activate a U-M provider and generated catalog while preserving the OpenAI credential.
4. Attempt authenticated U-M model discovery and record whether `/v1/models` is supported and complete.
5. Build a local Codex catalog from the demonstrated provider response or a bounded maintained fallback.
6. Show at least the expected upstream-known U-M model in the desktop picker.
7. Record the result of the non-upstream model picker canary without making it a version-1 blocker.
8. Complete a small request through the intended U-M endpoint and confirm the selected model reaches that environment.
9. Switch back and successfully use the original OpenAI authentication.
10. Launch the CLI and Desktop into the selected environment.
11. Retain the last-known-good state after an invalid config or failed catalog refresh.

Resuming a task created under one provider after another environment is activated also needs an explicit canary. The design expects stable provider identifiers and shared state to help, but this behavior must not be promised before it is exercised on stock clients.

## Decisions already made

- Build an external tool rather than modifying stock Codex core or Desktop.
- Target macOS and Linux first.
- Support both CLI and Desktop.
- Use named environments with generated per-environment catalogs.
- Preserve shared Codex state in one home for the first prototype.
- Treat configuration activation as a transaction.
- Prefer provider-specific secret retrieval over swapping `auth.json`.
- Use `$CODEX_HOME/codex-configure/.env` with mode `0600` as the standard U-M credential location; do not require an OS keychain.
- Treat native picker visibility as an acceptance result, not an assumption.
- Limit first-version environment overlays to model, provider, and catalog concerns; share skills, servers, approvals, and other Codex settings.
- Use `env_http_headers` for the U-M `x-portkey-api-key` header instead of the shared OpenAI credential slot.
- Attempt authenticated provider model discovery rather than assuming that `gpt-5.6-terra` is the complete permitted set.
- Do not depend on optional Portkey model-catalog functionality for the first prototype.
- Treat arbitrary custom model identifiers in the native picker as a canary rather than a version-1 requirement.

## Questions to resolve before or during the prototype

### U-M provider contract

- Does the U-M key work as a standard bearer token when supplied independently of Codex's shared OpenAI credential cache?
- Does authenticated `GET /v1/models` return the complete permitted model set, an alias list, or no usable catalog?
- Is `requires_openai_auth = true` technically required, or only the mechanism chosen by the current installation instructions?
- Does the gateway implement streaming Responses API requests and the tool behavior Codex requires?
- Which models are intentionally unavailable because of institutional policy or cost controls?

### Profile and state semantics

- Must old tasks resume through their original provider when another environment is active?
- How should the tool reconcile edits to model-routing keys made directly in Desktop while an environment is active?

Per-environment skills and MCP server visibility is deferred. It should be reconsidered only after the model-only environment boundary has been exercised.

### Distribution and user experience

- Is the prototype allowed to require a minimum Codex version?
- Is a simple numbered terminal menu sufficient, or is a richer TUI required?
- Which exact macOS and Linux Desktop launch targets must be discovered?
- What evidence and user-facing warning are sufficient when the gateway lists a model that later rejects a request because of account or cost policy?
