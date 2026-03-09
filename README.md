# OneCmd

A more specialized 'OpenClaw' for managing terminals — all from your phone.

Works on **macOS** and **Linux**.

> **One bot per machine.** Each machine needs its own Telegram bot token. Create a separate bot for each machine you want to control (e.g. `@my_macbook_bot`, `@my_server_bot`). Only one onecmd instance can use a given bot token at a time.
>

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/warlockee/1cmd-ai/main/setup.sh | bash
```

The setup script handles everything: clones the repo, installs dependencies, builds the project, walks you through creating a Telegram bot, and starts onecmd. Works on both macOS and Linux.

Or clone manually:

```bash
git clone https://github.com/warlockee/1cmd-ai.git
cd onecmd
./setup.sh
```

## How It Works

On **macOS**, onecmd reads terminal window text via the Accessibility API (`AXUIElement`), injects keystrokes via `CGEvent`, and focuses windows using `AXUIElement`. It works with any terminal app — no Screen Recording permission needed.

On **Linux**, onecmd uses tmux: `tmux list-panes` to discover sessions, `tmux capture-pane` to read content, and `tmux send-keys` to inject keystrokes. All sessions you want to control must run inside tmux.

In both cases, terminal output is sent as monospace text to Telegram with a refresh button to update on demand.

## Manual Setup

### Prerequisites

- **macOS:** Xcode Command Line Tools (`xcode-select --install`), `curl`, `sqlite3`
- **Linux:** `gcc`, `make`, `tmux`, `libcurl-dev`, `libsqlite3-dev`
- **AI Manager:** Python 3 with `pip install -r mgr/requirements.txt` (or let `setup.sh` handle it)

### Create a Telegram Bot

Each machine needs its own bot. To create one:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Name it something you'll recognize (e.g. "My Server Terminal")
4. Copy the API token

### Run

```bash
# Install Python deps for AI manager (skip if you only want terminal control)
pip install -r mgr/requirements.txt

# Build and run
make
GOOGLE_API_KEY=... ./onecmd --apikey YOUR_BOT_TOKEN
```

### Options

| Flag | Description |
|------|-------------|
| `--apikey <token>` | Telegram bot API token |
| `--enable-otp` | Enable TOTP authentication (off by default) |
| `--use-weak-security` | Disable TOTP even if previously configured |
| `--dbfile <path>` | Custom database path (default: `./mybot.sqlite`) |
| `--dangerously-attach-to-any-window` | Show all windows, not just terminals (macOS only) |
| `--mgr <path>` | Path to the manager agent script (auto-detected if `mgr/main.py` exists) |

## Usage

Message your bot on Telegram:

| Command | Action |
|---------|--------|
| `.list` | List available terminal sessions |
| `.1` `.2` ... | Connect to a session by number |
| `.help` | Show help |
| `.mgr` | Toggle AI manager mode (auto-enabled when `mgr/main.py` exists) |
| `.exit` | Leave manager mode |
| `.health` | Manager health report (uptime, memory, stats) |
| `.otptimeout <seconds>` | Set TOTP session timeout |
| Any other text | Sent as keystrokes to the connected terminal (or to the manager in manager mode) |

### Linux: tmux requirement

On Linux, onecmd controls tmux sessions. Make sure your work is running inside tmux:

```bash
# Start a named session
tmux new -s dev

# Or start detached sessions
tmux new -s server1 -d
tmux new -s server2 -d
```

Then run onecmd separately (outside tmux or in its own tmux window) and use `.list` to see your sessions.

### Keystroke Modifiers

Prefix your message with an emoji to add a modifier key:

| Emoji | Modifier | Example |
|-------|----------|---------|
| `❤️` | Ctrl | `❤️c` = Ctrl+C |
| `💙` | Alt | `💙x` = Alt+X |
| `💚` | Cmd | macOS only |
| `💛` | ESC | `💛` = send Escape |
| `🧡` | Enter | `🧡` = send Enter |
| `💜` | Suppress auto-newline | `ls -la💜` = no Enter appended |

### Escape Sequences

`\n` for Enter, `\t` for Tab, `\\` for literal backslash.

## AI Manager Agent

The manager is an LLM-powered agent that can autonomously monitor and control your terminals. It supports **Claude** (Anthropic) and **Gemini** (Google) as providers. The manager is auto-detected when `mgr/main.py` exists in the working directory — no `--mgr` flag needed:

```bash
# Using Gemini (default when both keys are set)
GOOGLE_API_KEY=... ./onecmd --apikey YOUR_BOT_TOKEN

# Using Claude
ANTHROPIC_API_KEY=sk-... ./onecmd --apikey YOUR_BOT_TOKEN
```

The provider is selected automatically based on which API key is set. If both are set, Gemini is preferred. Override the model with `ONECMD_MGR_MODEL`. Both `--apikey` and an LLM API key are validated at startup — onecmd shows a clear error and exits if either is missing.

Then send `.mgr` in Telegram to enter manager mode. In manager mode, your messages go to the AI agent instead of being sent as keystrokes. Dot commands (`.list`, `.1`, etc.) still work normally.

The manager can:
- List, read, and send commands to any terminal
- Execute commands asynchronously and notify you when they finish
- Queue commands to the same terminal so they don't overlap
- Auto-detect pending commands at prompts and submit them
- Follow up on completed commands (results feed back to the LLM)
- Run repeating background tasks ("watch this terminal until X happens")
- Detect and recover stuck terminals (Smart Diff — probe, compare before/after)
- Summarize long conversations to preserve context within token limits
- Auto-fallback between Gemini and Claude on rate limits
- Remember things across restarts (persistent memory)
- Auto-restart on crash (up to 5 retries with backoff)

On first run, the manager generates `.onecmd/agent_sop.md` — a Standard Operating Procedure that guides the AI on stuck terminal detection and recovery. You can edit this file to customize the agent's behavior.

Send `.exit` to leave manager mode, or `.health` for a status report.

### onecmd-ctl

`onecmd-ctl` is a standalone CLI tool used by the manager agent. You can also use it directly:

```bash
onecmd-ctl list                    # List terminals as JSON
onecmd-ctl capture <terminal_id>   # Capture visible text from a terminal
onecmd-ctl send <terminal_id> <keys>  # Send keystrokes to a terminal
onecmd-ctl status <terminal_id>    # Check if a terminal is alive
```

Terminal IDs come from the `list` output (e.g. `12399` on macOS, `%0` on tmux).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ONECMD_VISIBLE_LINES` | `40` | Number of terminal lines to include in output |
| `ONECMD_SPLIT_MESSAGES` | off | Set to `1` to split long output across multiple messages |
| `ONECMD_CTL` | `./onecmd-ctl` | Path to the onecmd-ctl binary (used by manager) |
| `ONECMD_MGR_MODEL` | `gemini-3-flash-preview` / `claude-opus-4-6` | LLM model for the manager agent (default depends on provider) |
| `ANTHROPIC_API_KEY` | (none) | Anthropic API key for the manager agent (Claude) |
| `GOOGLE_API_KEY` | (none) | Google API key for the manager agent (Gemini) |

Terminal output is sent as a single message by default. Each new command or refresh **deletes the previous output messages** and sends fresh ones, creating a clean "live terminal" view rather than spamming the chat.

If your terminal produces very long output (e.g. build logs) and you want to see all of it, enable splitting:

```bash
ONECMD_SPLIT_MESSAGES=1 ./onecmd
```

## Security

- **Owner lock**: The first Telegram user to message the bot becomes the owner. All other users are ignored.
- **TOTP**: OTP is **off by default** for a frictionless first-time experience. Use `--enable-otp` to set up Google Authenticator — a QR code is shown on first launch. Use `--use-weak-security` to disable OTP even if previously configured.
- **One bot = one machine**: Don't share a bot token across machines. Each machine should have its own bot.
- **Reset**: Delete `mybot.sqlite` to reset ownership and TOTP.

## Permissions

**macOS:** Requires Accessibility permission. macOS will prompt on first use, or grant it in System Settings > Privacy & Security > Accessibility.

**Linux:** No special permissions needed. Just ensure the user running onecmd can access the tmux socket.

## Supported Terminals

**macOS:** Terminal.app, iTerm2, Ghostty, kitty, Alacritty, Hyper, Warp, WezTerm, Tabby.

**Linux:** Any terminal running inside tmux.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed technical documentation: component design, thread model, data flow, IPC protocol, backend interface, global state, and known limitations.

## License

MIT -- see [LICENSE](LICENSE).
