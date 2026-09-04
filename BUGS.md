# Bugs

## Open

None.

## Resolved

### `doctor` reports running clients as an unexplained error

- Reported: 2026-09-03
- Resolved: 2026-09-04
- Reproduction: Run `codex-configure doctor` while ChatGPT Desktop or a Codex CLI process is running.
- Actual: The report prints a line such as `[ERROR] Client lifecycle: ChatGPT, codex` and ends with `Result: attention required`. It does not explain that these are detected process names, why a running client affects the health result, whether the configuration itself passed, or what the user should do next.
- Expected: The output clearly distinguishes a healthy managed configuration from a live-client condition. It explains that profile-switching and restore operations require clients to be stopped because they rewrite active configuration, while Dynamic Picker launches do not have that blanket requirement and an already-running Desktop retains the environment with which it started.
- Acceptance: With one or more clients running, `doctor` names the detected clients in plain language, states whether the managed configuration checks passed independently, explains which operations are blocked or potentially stale, and gives a concrete action such as fully quitting the clients and rerunning `doctor`. The status label and final summary must not imply unexplained configuration corruption.

### `doctor` requires an initialized working directory

- Reported: 2026-09-03
- Resolved: 2026-09-04
- Reproduction: Run `codex-configure doctor` from a directory that has not been initialized with `codex-configure init`.
- Actual: The command stops with an error saying that no launch root exists and instructs the user to run `codex-configure init`.
- Expected: `doctor` runs from an uninitialized directory without requiring or performing initialization. It should diagnose and report the missing launch root as part of its read-only health report.
- Acceptance: The command produces a diagnostic report for both initialized and uninitialized exact-current-directory contexts, does not create `.codex-configure/`, and does not silently fall back to a parent or global Codex home.

### `init` is a linear prompt sequence instead of a state-driven TUI

- Reported: 2026-09-03
- Resolved: 2026-09-04
- Actual: `codex-configure init` walks the user through a sequence of individual prompts. It does not provide a single view of the launch root's current model-catalog profile state or direct profile-management actions.
- Expected: `init` uses an ncurses-style terminal interface driven by the launch root's current and proposed state. The interface lets the user see the configured model-catalog profiles, inspect an individual profile, add a profile, and remove a profile.
- Interaction model: Re-entering `init` loads the existing state into the same management interface rather than restarting a one-way setup wizard. Adding or removing a profile updates the displayed state so the user can continue managing the root in the same session.
- Acceptance: A new root opens with an empty profile state and an add action; an existing root lists its profiles and supports inspect, add, and remove actions; the interface clearly distinguishes persisted state from proposed changes; and leaving the TUI has an explicit save-or-cancel outcome.
- Design boundary: `ncurses-style` describes the full-screen, keyboard-navigable terminal experience. This issue does not prescribe a particular TUI library.

### A rooted stock-Core launch is blocked by an unrelated global Codex session

- Reported: 2026-09-04
- Resolved: 2026-09-04
- Observed context: The initialized launch root `/home/brooksch/sandboxes/brooksch/projects/teaching/siads505/fa26/python_quiz_results2` has `um` as its default custom profile using stock Core.
- Reproduction: Leave Codex or ChatGPT running in another window with the normal global profile, enter the initialized launch root, and run `codex-configure launch desktop`.
- Actual: The rooted launch stops with `Error: Codex or ChatGPT is running (ChatGPT, codex). Close it before switching environments.` The lifecycle check treats clients using unrelated global state as conflicts.
- Expected: A stock-Core launch from an isolated root proceeds when the detected clients use a different Codex home or launch root. An unrelated global-profile session must not block use of the rooted custom model profile.
- Acceptance: Client-conflict detection is scoped to the configuration state that the launch may rewrite; processes using the normal global profile or a different launch root do not block this launch; a process that actually shares the target root can still block an unsafe profile switch; and any blocking error identifies the conflicting state boundary rather than relying only on process names.
