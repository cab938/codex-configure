# codex-configure

`codex-configure` is a small terminal launcher for people who use both the Codex CLI and Codex in the ChatGPT desktop app. It lets you choose between:

- your normal OpenAI account; and
- U-M GPT Toolkit through its OpenAI / Azure route.

The launcher uses the same Codex home, tasks, settings, skills, and plugins in both environments. It preserves your existing OpenAI login and original `config.toml`, stores the U-M key in a protected local file, and switches the active provider before starting the client you choose.

## Before you begin

You need all of the following:

- an OpenAI account that can use Codex;
- a U-M GPT Toolkit API key assigned to you;
- the Codex CLI;
- the ChatGPT desktop app;
- Python 3.11 or newer; and
- Git, to download and update this project.

This project currently targets the Linux distributions supported by the ChatGPT Linux preview: Ubuntu 24.04 or 26.04, Debian 13, and Fedora 43 or 44, on x64 or ARM64.

Do not share a U-M key or commit it to this repository. Each person should use their own key.

## Linux setup

The ChatGPT Linux app is currently a preview. Use an officially supported distribution and download the matching `.deb` or `.rpm` package from the [ChatGPT Linux installation guide](https://learn.chatgpt.com/docs/linux/linux-app).

### 1. Install system prerequisites and the desktop app

On Ubuntu or Debian:

```bash
sudo apt update
sudo apt install python3 python3-venv git
cd ~/Downloads
sudo apt install ./chatgpt_amd64.deb
```

On an ARM64 machine, install `./chatgpt_arm64.deb` instead.

On Fedora:

```bash
sudo dnf install python3 git
cd ~/Downloads
sudo dnf install ./chatgpt.x86_64.rpm
```

On an ARM64 machine, install `./chatgpt.aarch64.rpm` instead.

### 2. Install and sign in to Codex

Install the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli):

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Open a new terminal, run `codex`, and choose **Sign in with ChatGPT** on the first run. Then run `chatgpt` and sign in to the desktop app.

Fully quit the CLI and desktop app before continuing.

## Install codex-configure

Choose a convenient directory, then clone and install the project into its own Python environment:

```bash
cd ~
git clone https://github.com/cab938/codex-configure.git
cd codex-configure
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

The `.venv` directory belongs only to this project. It does not replace your system Python or either Codex client.

## Add your U-M key

The default Codex home is `~/.codex`. Create the launcher directory and its private credential file:

```bash
mkdir -p ~/.codex/codex-configure
chmod 700 ~/.codex/codex-configure
touch ~/.codex/codex-configure/.env
chmod 600 ~/.codex/codex-configure/.env
nano ~/.codex/codex-configure/.env
```

Add one line, replacing the placeholder with your own key:

```dotenv
UMICH_TOOLKIT_API_KEY=YOUR_UMICH_TOOLKIT_KEY
```

In `nano`, press Control-O, Enter, then Control-X to save and exit.

If you use a custom `CODEX_HOME`, put the file at `$CODEX_HOME/codex-configure/.env` instead. The launcher also accepts `UMICH_TOOLKIT_API_KEY` from the current shell or a different private file supplied with `--env-file PATH`.

## Launch Codex

First, fully quit the ChatGPT desktop app and exit every running Codex CLI session. Then run:

```bash
cd ~/codex-configure
.venv/bin/codex-configure
```

The launcher asks you to choose an environment:

1. **OpenAI** keeps your normal OpenAI login and moves directly to the client choice.
2. **U-M GPT Toolkit** shows the OpenAI / Azure provider, lets you select one or more available models, and asks which selected model should be the default.

Finally, choose **Codex Desktop** or **Codex CLI**. The launcher prints the environment and profile directory it activated, then starts that client.

Run the same command again whenever you want to:

- switch between OpenAI and U-M;
- change the U-M models shown in Codex;
- change the default U-M model; or
- start the other client.

Always quit both Codex clients before switching. The launcher will refuse to overwrite the active configuration while it detects a Codex or ChatGPT process.

## What the launcher changes

On its first run, `codex-configure`:

- preserves the original configuration at `~/.codex/codex-configure/base/original-config.toml`;
- maintains a shared OpenAI base configuration;
- creates profiles under `~/.codex/codex-configure/profiles/`; and
- records a last-known-good configuration and recovery state.

It does **not** replace `~/.codex/auth.json` or your OpenAI login. The U-M key remains in the mode-`0600` `.env` file and is passed only to a U-M Codex process. Unrelated Codex settings written by the desktop app are retained; an unexpected external change to provider-routing keys is rejected instead of overwritten.

Tasks and history remain shared. A resumed task uses whichever environment is active at that moment, even if the task originally used the other provider. Select the intended environment before resuming an existing task.

## Check or recover the setup

After at least one successful launcher run, inspect the active configuration and recovery files without changing them:

```bash
cd ~/codex-configure
.venv/bin/codex-configure doctor
```

Restore the maintained OpenAI configuration without launching a client:

```bash
.venv/bin/codex-configure restore
```

Restore the immutable `config.toml` captured on the very first run:

```bash
.venv/bin/codex-configure restore --original
```

Both restore commands require the CLI and desktop app to be closed. A configuration-only switch is also available:

```bash
.venv/bin/codex-configure --prepare-only
```

## Update codex-configure

```bash
cd ~/codex-configure
git pull --ff-only
.venv/bin/python -m pip install -e .
```

Your key, profiles, and backups are stored under `CODEX_HOME`, not in the cloned repository, so updating the checkout does not replace them.

## Troubleshooting

### "Codex or ChatGPT is running"

Quit the ChatGPT app completely, not just its window, and exit all Codex terminals. Then run the launcher again.

### "Could not find the Codex CLI on PATH"

Open a new terminal after installing the CLI and check:

```bash
command -v codex
codex --version
```

### "Could not find Codex Desktop"

Install the official Linux package so the `chatgpt` command is available. For a nonstandard installation, set `CODEX_DESKTOP_COMMAND` to the actual executable before running the launcher.

### U-M credential permission error

```bash
chmod 700 ~/.codex/codex-configure
chmod 600 ~/.codex/codex-configure/.env
```

### Linux virtual machine graphics errors

Some virtual machines need Chromium software rendering. This launch override worked in the project's test VM:

```bash
CODEX_DESKTOP_COMMAND='chatgpt --use-angle=swiftshader' .venv/bin/codex-configure
```

Use this only for a VM that fails during normal desktop launch. For Wayland-specific behavior, see the [official Linux app guidance](https://learn.chatgpt.com/docs/linux/linux-app).

## Project documentation

- [Architecture and safety model](docs/architecture.md)
- [Experimental Core provider-model picker spike](docs/spike-core-provider-model-picker.md)
- [Linux provider-picker productization plan](docs/linux-provider-picker-productization.md)
- [Manual VM acceptance design](docs/manual-vm-acceptance.md)

## Current limits

- Only OpenAI and the U-M GPT Toolkit OpenAI / Azure route are exposed.
- The first supported packaging target is Linux.
- U-M's advertised catalog is not proof that every listed model is enabled for every key. Terra is marked `verified`; other discovered models are marked `listed` until they have a recorded canary.
- Only one materialized configuration can be active in a given `CODEX_HOME` at a time. The qualified provider-picker build can expose both configured providers within that configuration.
- Managed Codex settings supplied by an organization can override the user configuration and are not changed by this tool.
