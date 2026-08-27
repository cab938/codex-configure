# Linux provider-picker productization

This is the next phase after the qualified provider-model Core spike. The first supported release target is Linux. The completed Core behavior should remain surgical: keep the upstream pin, patch only the provider/model boundaries already exercised by the spike, and avoid a Desktop renderer fork.

## Installed Desktop boundary

The Linux ChatGPT launcher accepts `CODEX_CLI_PATH=/absolute/path/to/codex` and uses that executable as Desktop's Codex backend. The variable selects the CLI/App Server binary; it is not a Codex home or configuration directory.

`CODEX_CLI_PATH` was not introduced by `codex-configure` or by the provider-model patch. It was already present in the community Linux Desktop launcher's initial commit, `dd18ce945bca4305bf03c63e9955a9845e0240b4`, before the `cab938` fork. The [official OpenAI Linux Desktop guide](https://learn.chatgpt.com/docs/linux/linux-app) describes installing ChatGPT Desktop on Linux but does not document this variable, so it is a tested Linux packaging hook rather than a public compatibility promise.

The productized launcher should set the variable only in the Desktop child environment. It should not add it globally to shell startup files. CLI launches should execute the same patched binary directly. A launch-time check must confirm the expected binary checksum or build identity and that `model/list` contains both qualified providers.

## Next implementation tasks

1. Create a pinned Linux build script that applies the tracked patch, builds the Core executable, and emits a small manifest containing the upstream commit, patch checksum, binary checksum, architecture, and build timestamp. Reuse an existing matching artifact; rebuild only when the pin, patch, target architecture, or toolchain input changes.
2. Create a reversible Linux install/launch script that puts the patched executable in a tool-owned location, preserves the previously selected executable, launches CLI directly, and launches Desktop with the exact `CODEX_CLI_PATH`. It must not overwrite the distro package, global `codex`, personal authentication, or unrelated `CODEX_HOME` state.
3. Write Linux user documentation for installation, U-M key setup, first launch, qualified model selection, `doctor`, upgrade, rollback, and uninstall. The documented happy path should not require Docker, a teaching environment, a VM, or a source checkout.
4. Add a surgical upstream refresh procedure: update the pinned commit, regenerate the patch with the smallest necessary conflict resolutions, run focused Core/App Server tests, build once per architecture, and run the manual VM acceptance check. Do not replace the patch with a vendored upstream tree.
5. Implement the lightweight manual VM acceptance runner described in [Manual VM acceptance](manual-vm-acceptance.md). It remains agent-invoked and does not require CI.

## Release gate

Linux user support begins only when a clean Linux account can install or select the patched binary without changing the ChatGPT package, run both CLI and Desktop, see qualified OpenAI and U-M models, switch providers in one task, resume that task after restart, and restore the previous backend through a documented rollback.
