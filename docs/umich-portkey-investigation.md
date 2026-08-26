# U-M Portkey capability investigation

Date: 2026-08-26

## Question

Determine whether the U-M GPT Toolkit workspace exposes Portkey Model Catalog features, whether the existing U-M development key can use them, and which parts are usable from stock Codex.

All probes were read-only except for low-token inference canaries. The key was loaded from `UMICH_TOOLKIT_API_KEY`; it was not written into request artifacts or logs.

## Result

The upstream workspace has usable Portkey Model Catalog data-plane features. The same U-M key can:

- list Portkey AI Provider objects;
- retrieve provider-scoped integrated model catalogs;
- override its attached default query-router config for an individual request; and
- complete inference through Azure OpenAI, AWS Bedrock, and Google with that request-scoped override.

The key cannot list configs or integrations and therefore cannot administer the Model Catalog. Provider/catalog discovery and inference are available; control-plane management is not.

## Evidence

### U-M gateway model list is not the native integrated catalog

`GET https://api.toolkit.umgpt.umich.edu/v1/models` returned the same 33-entry body:

- without authentication;
- with the U-M key;
- with `x-portkey-fetch-integrated-models: true`;
- with `provider` and `ai_service` filters; and
- with `limit` and `offset` pagination parameters.

The response identifiers are bare model names rather than Portkey's `@provider/model` identifiers. This endpoint is useful as a U-M alias list, but it is not a key-scoped entitlement list or a direct implementation of Portkey's documented integrated Model Catalog behavior.

### Portkey's API recognizes the U-M key and workspace

Against `https://api.portkey.ai/v1`:

| Probe | Result |
|---|---|
| `GET /providers` | HTTP 200; 12 providers in workspace `umich-codex` |
| Provider composition | `@aws`, `@google`, and ten Azure OpenAI provider objects |
| `GET /integrations` | HTTP 403, Portkey `AB03` insufficient permissions |
| `GET /integrations/aws/models` | HTTP 403, `AB03` |
| `GET /configs` | HTTP 403, `AB03` |

The provider objects use legacy-style integration identifiers matching their provider slugs. This is consistent with Portkey's documented migration of Virtual Keys into Model Catalog AI Providers.

### Native integrated catalog retrieval works

The default config attached to the key is a query router. A plain direct-Portkey `GET /models` entered that router and returned `no-match`. Adding the documented integrated-catalog override changed the behavior:

```text
x-portkey-fetch-integrated-models: true
```

It returned 278 provider-qualified entries across the ten Azure provider objects. The set contained 29 unique model slugs repeated across provider objects. Catalog membership alone was not proof that each Azure deployment name was callable.

Using a request-scoped config produced clean provider catalogs:

```text
x-portkey-config: {"strategy":{"mode":"single"},"targets":[{"provider":"@aws"}]}
x-portkey-fetch-integrated-models: true
```

| Request-scoped provider | Catalog result |
|---|---|
| `@aws` | 286 Bedrock model entries |
| `@google` | 8 Google model entries |

Portkey documents `x-portkey-config` as accepting a JSON config object. The key accepted this request-level override, so its default config is not locked against overrides.

### Cross-provider inference works with the same key

Minimal Responses API canaries against `https://api.portkey.ai/v1/responses` produced:

| Route | Model | Result | Reported provider |
|---|---|---|---|
| Attached U-M default config | `gpt-5.6-terra` | HTTP 200, completed | `azure-openai` |
| Inline `@aws` config | `amazon.nova-micro-v1:0` | HTTP 200, completed | `bedrock` |
| Inline `@aws` config | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | HTTP 200, completed | `bedrock` |
| Inline `@google` config | `gemini-3.5-flash` | HTTP 200, completed | `google` |

Provider-qualified model strings or `x-portkey-provider` alone did not replace the key's attached query-router config. They returned `no-match`. The explicit request-scoped `x-portkey-config` did replace it.

One unqualified Bedrock catalog entry, `anthropic.claude-haiku-4-5-20251001-v1:0`, failed because Bedrock requires an inference-profile identifier for that model. The catalog's `us.anthropic...` entry succeeded. This confirms that generated catalogs must still be validated with model canaries.

### Stock Codex can use the override

An isolated `codex-cli 0.149.1` run used this secret-free provider shape:

```toml
model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
model_provider = "portkey-aws"

[model_providers.portkey-aws]
name = "U-M Portkey AWS"
base_url = "https://api.portkey.ai/v1"
wire_api = "responses"
env_http_headers = { "x-portkey-api-key" = "UMICH_TOOLKIT_API_KEY" }
http_headers = { "x-portkey-config" = '{"strategy":{"mode":"single"},"targets":[{"provider":"@aws"}]}' }
request_max_retries = 0
stream_max_retries = 0
```

The streaming Codex task completed with the exact requested output and reported 8,689 tokens. No `auth.json` replacement or shared OpenAI credential was required.

The model was unknown to Codex's bundled catalog, so Codex used fallback metadata. The colon in the Bedrock identifier also caused non-fatal telemetry-tag warnings. A normalized local Codex catalog is still needed for model metadata, picker presentation, and model-specific capability settings.

## Implications for `codex-configure`

1. Keep the U-M alias endpoint and the native Portkey catalog as separate sources. They answer different questions.
2. Use `https://api.portkey.ai/v1/models` with both the integrated override and a request-scoped provider config to discover `@aws` or `@google` models.
3. Do not copy the raw Portkey response directly into `model_catalog_json`. Codex accepts a local catalog path, and Portkey's entries contain only `id`, `slug`, `canonical_slug`, and `object`.
4. Generate a bounded catalog per Codex environment, normalize the metadata, and retain only canary-demonstrated models or clearly mark untested entries.
5. Define separate Codex provider configurations for the U-M default router, AWS, and Google. They can share the same `UMICH_TOOLKIT_API_KEY` environment variable while using different static `x-portkey-config` headers.
6. Do not depend on integration/config administration. The current key has provider-list and inference access but lacks the required control-plane scopes.

This changes the earlier hypothesis from "Model Catalog may be unavailable" to "Model Catalog discovery and request-scoped routing are available, while administration is unavailable." It does not make Portkey's catalog a release dependency; a last-known-good normalized local catalog remains the safer client contract.

## Documentation references

- [Portkey Models endpoint](https://portkey.ai/docs/api-reference/inference-api/models/models)
- [Portkey Model Catalog](https://portkey.ai/docs/product/model-catalog)
- [Portkey default config precedence](https://portkey.ai/docs/product/administration/enforce-default-config)
- [Portkey permissions and `AB03`](https://portkey.ai/docs/help-center/you-do-not-have-enough-permissions)
- [Portkey upgrade and feature-flag behavior](https://portkey.ai/docs/support/upgrade-to-model-catalog)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
