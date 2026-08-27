# Manual VM acceptance

This document defines the lightweight Linux Desktop acceptance runner that an agent should invoke manually after a provider-model patch or upstream pin changes. It is deliberately not a CI workflow and should not rebuild Core on every run.

## Planned runner interface

The next implementation should add `spikes/core-provider-model-picker/vm_acceptance.py` with two commands:

```bash
python3 spikes/core-provider-model-picker/vm_acceptance.py start \
  --host codex@141.213.182.91 \
  --binary /absolute/path/to/patched/codex \
  --remote-root /home/codex/projects/codex-provider-model-picker-spike

python3 spikes/core-provider-model-picker/vm_acceptance.py verify \
  --manifest /absolute/path/to/run-manifest.json \
  --task-id TASK_ID
```

`start` prepares one isolated run and prints the manifest path plus the short GUI script. An agent performs the visible picker interactions through the VM's private CUA display, then supplies the resulting task ID to `verify`. A separate explicit `--build` option may call the pinned build harness when no matching binary exists; normal acceptance receives `--binary` and does not compile.

## `start` responsibilities

The runner should:

1. Verify the local patch applies to the recorded upstream pin and record the patch and binary checksums.
2. Check VM connectivity, free space, the private CUA display, and the absence of an active acceptance Desktop process.
3. Use only the dedicated remote root and a unique run directory. Never use the VM account's normal `~/.codex` as the Core home.
4. Confirm that the protected U-M `.env` exists with mode `0600` without reading or printing its value.
5. Preserve the previous test binary as a rollback, copy the supplied binary, and verify its checksum on the VM.
6. Run the existing App Server catalog and two-provider canary unless `--skip-app-server` is explicitly supplied for an unchanged binary that already has a passing manifest.
7. Clone the known authenticated Desktop profile into the unique run directory, set the dedicated `CODEX_HOME` and XDG paths, and launch ChatGPT on the VM's private display with the patched backend selected through `CODEX_CLI_PATH`.
8. Generate unique continuity markers and write a JSON manifest containing paths, checksums, expected qualified models, prompts, start time, and cleanup state. It must contain no credentials.

## Agent GUI sequence

Use `/home/codex/bin/cua-display` over SSH. Prefer accessibility/window actions; use the local-only Desktop debugging helper only when the model menu is not exposed reliably through accessibility.

1. Confirm the picker shows `openai::gpt-5.6-sol`. Start one task that asks OpenAI to remember the manifest's OpenAI marker and return the exact seed acknowledgement.
2. Select `umich-toolkit::gpt-5.6-terra` in the same task. Ask U-M to return the OpenAI marker, then ask it to generate and return a new `UMICH-` marker.
3. Select `openai::gpt-5.6-sol` in the same task. Ask OpenAI to return the U-M marker. Record the task ID from its rollout.
4. Quit Desktop normally, relaunch it with the same isolated homes, reopen the task, and ask for `OPENAI_MARKER|UMICH_MARKER` exactly.
5. Quit Desktop normally before running `verify`.

Capture a screenshot after each provider selection, each exact response, and the resumed response. Do not paste credentials into the UI or command line.

## `verify` responsibilities

The runner should read the isolated rollout and Desktop logs and fail unless all of the following hold:

- one task ID contains the expected OpenAI, U-M, and return-OpenAI turn sequence;
- both picker changes persisted the expected qualified model and provider;
- every continuity response matches the manifest markers;
- the resumed turn uses the final OpenAI selection and returns both markers;
- no encrypted-content verification error occurred;
- the known account/workspace settings 401, if present, did not sign Desktop out or fail a model turn; and
- Desktop is closed, the pre-run isolated default is restored, and the key file still has mode `0600`.

It should write a concise Markdown result and machine-readable JSON beside the screenshots and logs. The result should name the upstream pin, patch and binary checksums, task ID, model sequence, evidence directory, rollback binary, and any nonfatal warnings.

## Agent invocation

A future agent request can be short:

> On vm00, follow `docs/manual-vm-acceptance.md` with the existing patched binary. Run `start`, perform the four Desktop steps on the private CUA display, run `verify`, restore the isolated default, and report the manifest, task ID, and evidence path. Do not rebuild unless the patch or upstream pin changed.

The runner must never kill unrelated ChatGPT or Codex processes, modify the VM's normal Codex home, overwrite a binary without a rollback, print the U-M key, or delete an evidence directory automatically.
