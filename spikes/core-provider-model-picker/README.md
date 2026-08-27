# Core provider-model picker spike harness

This directory keeps the reproducibility layer for the pinned Codex proof of
concept. It does not vendor Codex. `upstream-pin.env` records the official
source and exact upstream commit; the root agent adds
`codex-provider-model-picker.patch` after the upstream changes are integrated.

## Prepare and build

Choose a dedicated checkout directory outside this repository and pass it
explicitly:

```sh
spike_dir=/tmp/codex-provider-spike
mkdir -p /tmp/codex-provider-spike
spikes/core-provider-model-picker/prepare.sh \
  --work-dir "$spike_dir/codex" --build
```

Use `--prepare-only` to clone, pin, and apply the tracked patch without
building. The script checks the origin URL, refuses a dirty or non-git reuse,
checks out the exact commit, verifies the patch before applying it, and builds
`codex-rs/target/release/codex` with Cargo. It never deletes the supplied
directory or performs a broad cleanup. A rerun against the patched (dirty)
checkout is intentionally refused; choose a fresh dedicated work directory.

## App Server canary

Create a separate dedicated Codex home and copy `config.example.toml` to its
`config.toml`:

```sh
mkdir -p /tmp/codex-provider-spike/home
cp spikes/core-provider-model-picker/config.example.toml \
  /tmp/codex-provider-spike/home/config.toml
```

The checked-in example sets the default to `openai::gpt-5.6-sol`, defines the
U-M provider at `https://api.portkey.ai/v1`, and maps
`x-portkey-api-key` to `UMICH_TOOLKIT_API_KEY` without storing a key value.

Supply OpenAI authentication and `UMICH_TOOLKIT_API_KEY` in the parent
environment. The key is inherited by Codex only; the Python canary does not
take it as an argument, read its value, print it, or write it to a file. Keep
the home dedicated because Codex writes its state and logs there. The canary
requires the explicit config path to be exactly `$CODEX_HOME/config.toml` and
starts only the explicitly supplied patched binary.

First validate the catalog without spending model budget:

```sh
python3 spikes/core-provider-model-picker/app_server_canary.py \
  --codex /tmp/codex-provider-spike/codex/codex-rs/target/release/codex \
  --codex-home /tmp/codex-provider-spike/home \
  --catalog-only
```

The canary always reads `$CODEX_HOME/config.toml`; there is no alternate
config path. If the VM has limited disk space, building on the host and
copying the resulting binary into the isolated VM spike directory is also a
valid preparation arrangement, provided the live canary still receives the
explicit binary path and dedicated home.

After catalog validation, omit `--catalog-only` for two short turns. The
canary requests an exact `CANARY_OPENAI_OK` response and then an exact
`CANARY_UM_OK` response using `openai::<listed-model>` followed by
`umich-toolkit::gpt-5.6-terra`; it checks the same task ID in both completed
turns. It then restarts App Server, resumes that persisted task without model
overrides, and checks that the qualified U-M model and provider are restored.
JSON-RPC is newline-delimited JSON, and timeout/process-exit failures identify
the operation without dumping server diagnostics or credentials.

## Focused checks

The harness tests use only Python's standard library:

```sh
python3 -m unittest discover -s spikes/core-provider-model-picker \
  -p 'test_*.py'
python3 -m py_compile spikes/core-provider-model-picker/app_server_canary.py
```

These checks cover JSONL framing, qualified catalog selection, exact marker
parsing, and task/turn identity continuity. They do not replace the live
catalog-only and two-turn canaries.

## Manual Linux Desktop acceptance

The full Desktop provider-switch and restart test is intentionally a manual,
agent-run acceptance check rather than CI. Its lightweight runner contract,
GUI sequence, safety boundaries, and evidence requirements are documented in
[Manual VM acceptance](../../docs/manual-vm-acceptance.md). Linux packaging
work is tracked in the [provider-picker productionization plan](../../docs/linux-provider-picker-productionization.md).
