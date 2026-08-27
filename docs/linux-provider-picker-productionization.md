# Linux provider-picker productionization

This is the next phase after the qualified provider-model Core spike. The first supported release target is Linux. The completed Core behavior should remain surgical: keep the upstream pin, patch only the provider/model boundaries already exercised by the spike, and avoid a Desktop renderer fork.

## Linux Desktop variants

There are two Linux packages worth considering:

- **ChatGPT**, OpenAI's official Linux preview, installs the `chatgpt` command and the complete signed Electron and Codex runtime.
- **ChatGPT Community**, maintained at [`ilysenko/codex-desktop-linux`](https://github.com/ilysenko/codex-desktop-linux), installs the `codex-desktop` command. It verifies and repackages OpenAI's signed Linux payload, adds a separate package identity and update path, and offers disabled-by-default Linux features. With no ASAR-changing feature enabled, its `resources/app.asar` is byte-for-byte identical to the official package.

These are therefore not two independent Desktop implementations. The default Community application is the official runtime inside a community-owned verification, packaging, updater, and optional-feature layer. Both intentionally use the same upstream Codex profile and single-instance lock, so they must not run simultaneously.

Inspection of the unmodified official `chatgpt` 26.820.71523 package confirmed that its own `app.asar` reads `CODEX_CLI_PATH` before falling back to the bundled `resources/codex` executable. Its `/usr/bin/chatgpt` launcher preserves the child environment. The earlier VM acceptance run also exercised this boundary end to end with official Desktop 26.820.60940. The official [stable environment-variable reference](https://learn.chatgpt.com/docs/config-file/environment-variables) still does not list `CODEX_CLI_PATH`, so this remains an observed compatibility surface rather than a public API promise.

The recommended productionization boundary is to support both packages through one small Desktop-launch adapter, with the official package as the baseline and default when only one is installed. The Community package remains a first-class target because it uses the same upstream runtime and provides useful packaging and optional Linux features. If both are installed, the user should choose explicitly; the launcher must not silently select one. Neither path requires a renderer fork.

## Installed Desktop boundary

The Linux ChatGPT launcher accepts `CODEX_CLI_PATH=/absolute/path/to/codex` and uses that executable as Desktop's Codex backend. The variable selects the CLI/App Server binary; it is not a Codex home or configuration directory.

`CODEX_CLI_PATH` was not introduced by `codex-configure` or by the provider-model patch. It was already present in the community Linux Desktop launcher's initial commit, `dd18ce945bca4305bf03c63e9955a9845e0240b4`, before the `cab938` fork, and the current official Linux payload also reads it. The [official OpenAI Linux Desktop guide](https://learn.chatgpt.com/docs/linux/linux-app) and stable environment-variable reference do not document this variable, so it must be verified against every supported Desktop update.

The production launcher should set the variable only in the Desktop child environment. It should not add it globally to shell startup files. CLI launches should execute the same patched binary directly. A launch-time check must confirm the expected binary checksum or build identity and that `model/list` contains both qualified providers.

## Next implementation tasks

1. Create a pinned Linux build script that applies the tracked patch, builds the Core executable, and emits a small manifest containing the upstream commit, patch checksum, binary checksum, architecture, and build timestamp. Reuse an existing matching artifact; rebuild only when the pin, patch, target architecture, or toolchain input changes.
2. Create a reversible Linux install/launch script that puts the patched executable in a tool-owned location, preserves the previously selected executable, launches CLI directly, and launches either official `chatgpt` or Community `codex-desktop` with the exact `CODEX_CLI_PATH`. If both Desktop packages are installed, ask the user which one to launch. It must not overwrite either distro package, global `codex`, personal authentication, or unrelated `CODEX_HOME` state.
3. Write Linux user documentation for installation, Desktop selection, U-M key setup, first launch, qualified model selection, `doctor`, upgrade, rollback, and uninstall. Document the official preview as the baseline and ChatGPT Community as a supported alternative. The documented happy path should not require Docker, a teaching environment, a VM, or a source checkout.
4. Add a surgical upstream refresh procedure: update the pinned commit, regenerate the patch with the smallest necessary conflict resolutions, run focused Core/App Server tests, build once per architecture, and run the manual VM acceptance check. Do not replace the patch with a vendored upstream tree.
5. Implement the lightweight manual VM acceptance runner described in [Manual VM acceptance](manual-vm-acceptance.md). It should accept the Desktop variant as an input, prove the official package after every upstream refresh, and run the same acceptance against Community before claiming that variant is supported. It remains agent-invoked and does not require CI.

## Release gate

Linux user support begins only when a clean Linux account can install or select the patched binary without changing either Desktop package, run both CLI and the selected Desktop variant, see qualified OpenAI and U-M models, switch providers in one task, resume that task after restart, and restore the previous backend through a documented rollback. Support is claimed separately for official and Community Desktop after each has passed this gate.
