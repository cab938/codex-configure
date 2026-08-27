# Spike: Core provider-model picker

This branch tests whether stock Codex clients can use one model picker for more than one provider when the provider is encoded in the existing model string. It is an experimental companion to the launcher architecture, not a replacement for the settled behavior on `main`.

The proof of concept is pinned to OpenAI Codex commit `b68acc4d4b56fdfa1d5b6a2c36102c66876e0c46`. The branch will retain a patch and build harness rather than vendoring the upstream repository.

The current artifact validates the CLI, App Server, and stock Linux Desktop boundary. Codex Desktop 26.820.60940 honored `CODEX_CLI_PATH`, launched the patched Core executable, accepted its `0.0.0` development version, and rendered its catalog without a Desktop renderer patch. `CODEX_CLI_PATH` is still an experimental packaging hook rather than a documented compatibility promise, so sharing the binary remains a later decision.

## Intended interaction

For the first proof of concept, the user launches the U-M environment through `codex-configure`. That environment already supplies the private U-M key to the child process and defines both the built-in OpenAI provider and the `umich-toolkit` provider.

The existing model picker then shows qualified values such as:

```text
openai::gpt-5.6-sol
openai::gpt-5.6-terra
umich-toolkit::gpt-5.6-terra
```

The qualified value is both the picker label and the value sent through the existing `model` field. No new Desktop control or App Server request field is required for the spike.

This deliberately stays within the existing [Codex App Server API](https://learn.chatgpt.com/docs/app-server): clients list models and continue returning a string-valued model selection through the existing thread and turn methods.

The expected behavior is:

1. The user selects a qualified model before the first turn.
2. Core resolves the prefix to a configured provider and sends only the suffix as the upstream model ID.
3. The task keeps its current execution host, working directory, permissions, history, and collaboration mode.
4. The user may choose another qualified model before a later turn. A same-provider change selects another model; a different-provider change selects a new provider runtime for that turn and future turns.
5. The task is not forked merely because the provider changes.
6. Resuming the task restores the last committed provider-model selection.

`Default` and `Plan` remain Codex collaboration modes. The spike does not introduce another meaning of mode and does not alter their behavior.

## Qualified model contract

- The separator is `::`. A model ID may contain `/`, so `/` is not a safe provider separator.
- The prefix is the exact key in `model_providers`, initially `openai` or `umich-toolkit`.
- The suffix is the exact model ID sent to the provider.
- The first `::` separates provider and model. Empty prefixes or suffixes are invalid.
- An unqualified model remains valid and uses the task's current provider. This preserves existing CLI, App Server, and configuration behavior.
- A qualified picker/startup override may replace a provider default from `config.toml`. If it conflicts with a provider override supplied in the same request, startup fails instead of silently choosing one.
- An unknown provider or malformed qualified model fails before the task's current provider-model selection is changed.

The qualified name is an application-facing identifier. Provider requests, model metadata lookups, and provider-specific caches use the unqualified model ID plus the resolved provider identity.

## Runtime boundary

Execution routing and model routing remain separate:

```text
Desktop or CLI
    |
    | existing model string, for example umich-toolkit::gpt-5.6-terra
    v
App Server
    |
    v
Core task
    |-- existing execution environment --> local or remote execution host
    `-- resolved provider runtime -------> OpenAI or U-M GPT Toolkit
```

Provider selection is committed only between turns. An attempt to change providers inside an active turn is rejected for this spike because request transport, auth, prompt-cache state, and sticky-routing state are turn-scoped. Ordinary model and effort changes supported by Core within the same provider retain their current behavior.

Each resolved provider runtime owns:

- the configured provider definition and auth/header behavior;
- a provider-specific models manager and catalog view; and
- a fresh model client session for each turn.

Switching provider must not reuse another provider's WebSocket, sticky-routing token, HTTP fallback state, or model-catalog cache.

### Cross-provider history boundary

Provider reasoning items are not portable history. They may contain opaque or encrypted state that only the provider that created them can verify. When a committed provider change occurs between live turns, Core therefore removes all reasoning items from the in-memory history before the new provider is used. User messages, assistant messages, and tool interactions remain available, so the semantic conversation continues without forwarding another provider's private reasoning payload.

Resume performs the same filtering while reconstructing a rollout. It begins with the provider recorded in session metadata and replays persisted `ThreadSettingsApplied` provider boundaries. Each provider boundary discards reasoning created under the previous provider while preserving reasoning created under the final active provider. Same-provider model changes retain their existing history behavior.

## Catalog behavior

`model/list` aggregates OpenAI and `umich-toolkit` for this proof of concept. Returned `id`, `model`, `displayName`, and upgrade references are qualified so the existing picker both displays and sends an unambiguous value. Qualifying only `id` and `model` produces visually indistinguishable duplicate labels because stock Desktop renders `displayName`. The U-M catalog is still discovery, not an entitlement promise.

The first spike deliberately names the two provider IDs instead of inventing a durable provider-discovery policy. A later design can add explicit picker visibility configuration when vLLM packaging is in scope.

## Proof-of-concept development plan

1. Add and test one qualified-model parser in the model-provider layer.
2. Resolve a qualified startup model to its provider while preserving unqualified backward compatibility.
3. Give each committed task selection a provider-specific models manager and make model metadata lookup strip the namespace before calling the provider.
4. Select a provider-matched model client at turn start, with provider-private connection state reset on a provider change.
5. Remove provider-bound reasoning at committed provider changes and reconstruct the same boundary from persisted settings events on resume.
6. Persist the resolved provider ID with thread settings and restore it on resume.
7. Aggregate and qualify the OpenAI and U-M entries returned by `model/list` without changing the App Server wire schema.
8. Keep a reproducible patch and scripts in `spikes/core-provider-model-picker/`, pinned to the upstream commit above.
9. Run focused parser, configuration, Core session, history-reconstruction, and App Server catalog tests.
10. Build the patched `codex` binary on a Linux host, strip only the test artifact, and copy it to the limited-disk project VM under the dedicated spike directory.
11. Run App Server and Desktop canaries with a separate VM `CODEX_HOME` that demonstrate catalog listing, OpenAI and U-M turns, an OpenAI-to-U-M-to-OpenAI sequence in one task, and restart/resume restoration. No canary prints or records the U-M key.

## Acceptance evidence

The proof of concept is useful when all of the following are observed:

- `model/list` returns at least one qualified OpenAI entry and `umich-toolkit::gpt-5.6-terra`.
- A qualified OpenAI selection sends the unqualified OpenAI model to the OpenAI route.
- A qualified U-M selection sends `gpt-5.6-terra` to the U-M Responses endpoint with the configured Portkey header.
- A single task ID and semantic history survive an OpenAI-to-U-M-to-OpenAI provider sequence between turns.
- Provider-bound reasoning is removed at a provider boundary rather than sent to a provider that cannot verify it.
- A failed or unknown provider selection leaves the previous provider-model selection usable.
- Restarting the App Server and resuming the task restores the last committed qualified selection.
- All patched Core build and live-test state is under the isolated VM spike directory and a dedicated `CODEX_HOME`; the VM user's normal Codex home and the stale test checkout are not modified.

### Isolated VM observation, 2026-08-27

The Linux acceptance run used `/home/codex/projects/codex-provider-model-picker-spike` and its dedicated `codex-home`; the authenticated stock Desktop shell reused the VM account's existing `/home/codex/.config/Codex` app profile. Desktop spawned `/home/codex/projects/codex-provider-model-picker-spike/bin/codex` through `CODEX_CLI_PATH`; the App Server handshake and `model/list` completed successfully. The real picker displayed distinct `openai::...` and `umich-toolkit::...` entries. Selecting `umich-toolkit::gpt-5.6-terra` created a persisted rollout whose session metadata recorded `model_provider: "umich-toolkit"` and whose exact response was `DESKTOP_UMICH_OK`.

A controlled follow-up compared the bundled Core with the patched Core from the same signed-in Desktop starting profile. The bundled `0.150.0-alpha.8` Core completed `DESKTOP_STOCK_OK`, then Desktop received a 401 from `GET /backend-api/accounts/{account_id}/settings` with the response `Must use workspace account for this operation`. Desktop remained signed in. The patched `0.0.0` Core completed `PATCHED_OPENAI_OK` and produced the same account-settings 401 before any U-M model was selected. It also remained signed in, switched the same task to `umich-toolkit::gpt-5.6-terra`, and completed `PATCHED_UMICH_OK`. Neither run logged `account/updated` or `App server account changed`, and the isolated Core still reported `Logged in using ChatGPT` afterward.

This A/B falsifies a patch-specific or binary-attestation explanation for the observed 401. The failure is a Desktop account/workspace settings request, independent of `CODEX_CLI_PATH` and independent of U-M routing. The earlier return-to-sign-in was therefore a separate Desktop session-state event whose exact trigger remains unproven.

The first full visual OpenAI-to-U-M-to-OpenAI run exposed a separate Core history defect: the return OpenAI turn received a U-M reasoning item whose encrypted content OpenAI could not verify. That failure occurred after the reverse picker selection and `thread/settings/update` had both succeeded. Core now removes provider-bound reasoning at live provider changes and reconstructs persisted provider boundaries on resume, while retaining the semantic transcript.

The rebuilt acceptance run used a fresh isolated Desktop profile clone under `e2e-test-fixed` and task `01a043db-c04d-7bf1-a267-e9d0a3ce8435`. OpenAI stored `OPENAI-FIXED-5C4A9E`; after selection of `umich-toolkit::gpt-5.6-terra`, U-M recalled it and generated `UMICH-7KQ9XZ`; after selecting `openai::gpt-5.6-sol`, OpenAI returned the U-M token exactly. Desktop was then quit normally, relaunched with the same isolated Core and Desktop homes, and resumed the same task on the OpenAI-qualified model. The resumed turn returned `OPENAI-FIXED-5C4A9E|UMICH-7KQ9XZ` exactly. Screenshots and Desktop logs are retained under `/home/codex/projects/codex-provider-model-picker-spike/e2e-test-fixed`; the persisted rollout is in its dedicated `codex-home`. The account-settings 401 remained nonfatal and independent of provider routing.

## Non-goals

- Packaging a replacement CLI or Desktop app for other users.
- Patching Desktop visuals beyond the values supplied by `model/list`.
- A general provider visibility policy or vLLM discovery.
- Changing execution-host routing.
- Switching providers during an active turn, realtime conversation, compaction already in flight, or a tool continuation.
- Guaranteeing that every U-M-advertised model is enabled for a particular key.

Packaging and upstreamability will be reconsidered only after the live harness establishes that the routing model works.
