# OneCmd

An AI-powered terminal manager — control and automate your machines from Telegram.

Works on **macOS** and **Linux**.

> **One bot per machine.** Each machine needs its own Telegram bot token. Create a separate bot for each machine you want to control (e.g. `@my_macbook_bot`, `@my_server_bot`). Only one onecmd instance can use a given bot token at a time.

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/warlockee/1cmd-ai/main/setup.sh | bash
```

The setup script handles everything: clones the repo, installs dependencies, builds the project, configures your Telegram bot and AI provider, and starts onecmd.

Or clone manually:

```bash
git clone https://github.com/warlockee/1cmd-ai.git
cd 1cmd-ai
./setup.sh
```

## AI Manager

The AI manager is what makes OneCmd powerful. It's an LLM-powered agent that monitors, controls, and automates your terminals — so you can manage servers, run deployments, and debug issues all from a Telegram chat.

Send `.mgr` to enter manager mode. Your messages go to the AI agent, which can see and interact with all your terminals. Dot commands (`.list`, `.1`, etc.) still work normally. Send `.exit` to leave manager mode.

### What it can do

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

### Providers

The manager supports **Gemini** (Google) and **Claude** (Anthropic). The provider is selected automatically based on which API key is set. If both are set, Gemini is preferred. Override the model with `ONECMD_MGR_MODEL`.

```bash
# Using Gemini (recommended — fast and free tier available)
GOOGLE_API_KEY=... ./onecmd --apikey YOUR_BOT_TOKEN

# Using Claude
ANTHROPIC_API_KEY=sk-... ./onecmd --apikey YOUR_BOT_TOKEN
```

### Standard Operating Procedure

On first run, the manager generates `.onecmd/agent_sop.md` — a Standard Operating Procedure that guides the AI on stuck terminal detection and recovery. You can edit this file to customize the agent's behavior.

### Manager commands

| Command | Action |
|---------|--------|
| `.mgr` | Enter AI manager mode (auto-enabled when `mgr/main.py` exists) |
| `.exit` | Leave manager mode |
| `.health` | Manager health report (uptime, memory, stats) |

### onecmd-ctl

`onecmd-ctl` is a standalone CLI tool used by the manager agent. You can also use it directly:

```bash
onecmd-ctl list                    # List terminals as JSON
onecmd-ctl capture <terminal_id>   # Capture visible text from a terminal
onecmd-ctl send <terminal_id> <keys>  # Send keystrokes to a terminal
onecmd-ctl status <terminal_id>    # Check if a terminal is alive
```

Terminal IDs come from the `list` output (e.g. `12399` on macOS, `%0` on tmux).

## Manual Mode

Manual mode is always available as a stable, reliable fallback. It works without any AI provider — just you and your terminals over Telegram. No API keys, no token limits, no network dependencies beyond Telegram itself. When the AI is down or you need direct control, manual mode is always there.

In manual mode, any text you send is typed directly into the connected terminal as keystrokes.

| Command | Action |
|---------|--------|
| `.list` | List available terminal sessions |
| `.1` `.2` ... | Connect to a session by number |
| `.help` | Show all commands |
| Any other text | Sent as keystrokes to the connected terminal |

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

## How It Works

On **macOS**, onecmd reads terminal window text via the Accessibility API (`AXUIElement`), injects keystrokes via `CGEvent`, and focuses windows using `AXUIElement`. It works with any terminal app — no Screen Recording permission needed.

On **Linux**, onecmd uses tmux: `tmux list-panes` to discover sessions, `tmux capture-pane` to read content, and `tmux send-keys` to inject keystrokes. All sessions you want to control must run inside tmux.

In both cases, terminal output is sent as monospace text to Telegram with a refresh button to update on demand.

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

## Configuration

### Options

| Flag | Description |
|------|-------------|
| `--apikey <token>` | Telegram bot API token |
| `--enable-otp` | Enable TOTP authentication (off by default) |
| `--use-weak-security` | Disable TOTP even if previously configured |
| `--dbfile <path>` | Custom database path (default: `./mybot.sqlite`) |
| `--dangerously-attach-to-any-window` | Show all windows, not just terminals (macOS only) |
| `--mgr <path>` | Path to the manager agent script (auto-detected if `mgr/main.py` exists) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | (none) | Google API key for the AI manager (Gemini) |
| `ANTHROPIC_API_KEY` | (none) | Anthropic API key for the AI manager (Claude) |
| `ONECMD_MGR_MODEL` | `gemini-3-flash-preview` / `claude-opus-4-6` | LLM model for the manager (default depends on provider) |
| `ONECMD_VISIBLE_LINES` | `40` | Number of terminal lines to include in output |
| `ONECMD_SPLIT_MESSAGES` | off | Set to `1` to split long output across multiple messages |
| `ONECMD_CTL` | `./onecmd-ctl` | Path to the onecmd-ctl binary (used by manager) |

Terminal output is sent as a single message by default. Each new command or refresh **deletes the previous output messages** and sends fresh ones, creating a clean "live terminal" view rather than spamming the chat.

If your terminal produces very long output (e.g. build logs) and you want to see all of it, enable splitting:

```bash
ONECMD_SPLIT_MESSAGES=1 ./onecmd
```

### Prerequisites

- **macOS:** Xcode Command Line Tools (`xcode-select --install`), `curl`, `sqlite3`
- **Linux:** `gcc`, `make`, `tmux`, `libcurl-dev`, `libsqlite3-dev`
- **AI Manager:** Python 3 with `pip install -r mgr/requirements.txt` (or let `setup.sh` handle it)

### Manual Run

```bash
# Build
make

# Run with AI manager
GOOGLE_API_KEY=... ./onecmd --apikey YOUR_BOT_TOKEN

# Run without AI manager (manual mode only)
./onecmd --apikey YOUR_BOT_TOKEN
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
