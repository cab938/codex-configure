# Core provider-model picker patch

This directory is the canonical maintenance boundary for the patched Codex
Core binary. It does not vendor the upstream repository. `upstream-pin.env`
records the tested upstream commit, `codex-provider-model-picker.patch` is the
complete patch, and `prepare.sh` checks out that commit, verifies and applies
the patch, and optionally builds `codex` plus `codex-code-mode-host`.

## Build

Use a fresh dedicated checkout directory. The script refuses a dirty checkout
or a checkout with a different origin. The pinned Core currently requires
Rust 1.94 or newer:

```sh
src/core-provider-model-picker/prepare.sh \
  --work-dir /tmp/codex-core-provider-picker/codex --build
```

The release executables are written to:

```text
/tmp/codex-core-provider-picker/codex/codex-rs/target/release/codex
/tmp/codex-core-provider-picker/codex/codex-rs/target/release/codex-code-mode-host
```

The build strips release symbols and uses the pinned upstream repository's checksum-verifying resolver for
the matching OpenAI-hosted V8 archive and generated binding. Cargo's default
`rusty_v8` release URL does not publish the Codex-specific build variant.

The patch is intentionally generated against the pinned commit. When upstream
changes, update `upstream-pin.env`, apply the patch to a fresh checkout, make
the smallest required source edits, and regenerate the patch from that clean
pin. Do not vendor or hand-edit an upstream checkout in this repository.

## External provider descriptors

At startup the patched Core reads valid descriptors in deterministic filename
order from:

```text
$CODEX_HOME/codex-configure/providers.d/*.toml
```

Each descriptor has exactly one provider table and a required static catalog
reference. The catalog path is relative to the descriptor unless absolute:

```toml
schema_version = 1
kind = "external"
model_catalog_json = "../catalogs/research.json"

[model_providers.research]
name = "Research"
base_url = "https://example.invalid/v1"
wire_api = "responses"
requires_openai_auth = false

[model_providers.research.env_http_headers]
x-example-api-key = "RESEARCH_API_KEY"
```

The referenced file must be a non-empty Codex `ModelsResponse` JSON catalog.
Catalogs are loaded once during startup and are authoritative for external
providers: Core never falls back to that provider's `/models` endpoint. A
missing, malformed, empty, reserved, or duplicate provider descriptor is
skipped with a startup warning. Selecting a provider that was skipped returns
an actionable configuration error.

External picker values use `provider::model`; Core strips the provider prefix
only at the provider request boundary. OpenAI remains the built-in provider.

## Focused checks

From a prepared upstream checkout, using the path to this repository's patch:

```sh
git apply --check /path/to/codex-configure/src/core-provider-model-picker/codex-provider-model-picker.patch
cargo test -p codex-core external_provider_catalogs::tests
cargo check -p codex-app-server
```

The two unit tests cover relative catalog loading, deterministic descriptor
handling, and skip behavior. The app-server check verifies that the picker
aggregation and startup-loaded catalog wiring compile against the pinned Core.

For authenticated Linux acceptance, use `app_server_canary.py` with an isolated
`CODEX_HOME`; it verifies qualified catalog discovery, one OpenAI-to-external
task, and provider restoration after App Server restart without printing the
credential.
