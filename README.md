# OneCmd

An AI-powered terminal manager — control and automate your machines from Telegram.

Works on **macOS** and **Linux**.

> **One bot per machine.** Each machine needs its own Telegram bot token. Create a separate bot for each machine you want to control (e.g. `@my_macbook_bot`, `@my_server_bot`). Only one onecmd instance can use a given bot token at a time.

## Quick Start

Use the one-line installer (clones repo, configures everything interactively):

```bash
curl -sL https://1cmd.ai/setup.sh | bash
```
Then start the bot:

```bash
onecmd --apikey YOUR_BOT_TOKEN
```

Or install from source:

```bash
git clone https://github.com/warlockee/1cmd-ai.git
cd 1cmd-ai
python3 -m venv .venv
.venv/bin/pip install ".[macos]"   # macOS (or just "." for Linux)
.venv/bin/onecmd --apikey YOUR_BOT_TOKEN
```

## AI Manager

The AI manager is what makes OneCmd powerful. It's an LLM-powered agent that monitors, controls, and automates your terminals — so you can manage servers, run deployments, and debug issues all from a Telegram chat.

Send `.mgr` to enter manager mode. Your messages go to the AI agent, which can see and interact with all your terminals. Dot commands (`.list`, `.1`, etc.) still work normally. Send `.exit` to leave manager mode.

### What it can do

- List, read, and send commands to any terminal
- Name terminals for easy identification (proactively suggests names based on content)
- Execute commands asynchronously and notify you when they finish
- Queue commands to the same terminal so they don't overlap
- Auto-detect pending commands at prompts and submit them
- Follow up on completed commands (results feed back to the LLM)
- Run repeating background tasks ("watch this terminal until X happens")
- Detect and recover stuck terminals (Smart Diff — probe, compare before/after)
- Summarize long conversations to preserve context within token limits
- Auto-fallback between Gemini and Claude on rate limits, timeouts, and errors
- Anti-stuck detection: automatically recovers when Enter key doesn't register on laggy terminals
- Smart task results feed back to the AI for intelligent summaries instead of raw output
- Remember things across restarts (persistent memory)

### Providers

The manager supports **Gemini** (Google), **Claude** (Anthropic), and **OpenAI Codex**.

Switch providers live from Telegram with `.model`:

```
.model              — show current provider + what's configured
.model gemini       — switch to Gemini
.model claude       — switch to Claude (prompts for API key if not set)
.model codex        — switch to Codex
.model claude sk-ant-xxx  — set API key and switch in one command
```

Or configure via environment variables:

```bash
# Gemini (recommended — fast and free tier available)
GOOGLE_API_KEY=... onecmd --apikey YOUR_BOT_TOKEN

# Claude
ANTHROPIC_API_KEY=sk-... onecmd --apikey YOUR_BOT_TOKEN

# Codex (OAuth — run `codex` CLI to login first, then ./setup.sh to import)
ONECMD_MGR_PROVIDER=openai-codex onecmd --apikey YOUR_BOT_TOKEN
```

Auto-detection priority: Gemini > Claude > Codex. Override with `ONECMD_MGR_PROVIDER` or `ONECMD_MGR_MODEL`.

### Standard Operating Procedure

On first run, the manager copies the default SOP to `.onecmd/agent_sop.md`. This file guides the AI on decision-making and stuck terminal recovery.

To add your own rules, create `.onecmd/custom_rules.md`:

```markdown
- Always run tests before deploying
- Never restart the database without asking me first
- Prefer yarn over npm
```

Custom rules are appended to the default SOP automatically — no need to edit the base file.

### Manager commands

| Command | Action |
|---------|--------|
| `.mgr` | Enter AI manager mode |
| `.ceo` | Enter CEO mode (multi-agent orchestration) |
| `.model` | Show/switch LLM provider (gemini, claude, codex) |
| `.exit` | Leave manager/CEO mode |
| `.debug` | Toggle verbose smart task output |
| `.health` | Health report (uptime, provider, stats) |

## Manual Mode

Manual mode is always available as a stable, reliable fallback. It works without any AI provider — just you and your terminals over Telegram. No API keys, no token limits, no network dependencies beyond Telegram itself. When the AI is down or you need direct control, manual mode is always there.

In manual mode, any text you send is typed directly into the connected terminal as keystrokes.

| Command | Action |
|---------|--------|
| `.list` | List available terminal sessions |
| `.1` `.2` ... | Connect to a session by number |
| `.rename <N> <name>` | Name a terminal for easy identification |
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
| `--verbose` | Enable debug logging |
| `--agent-mode <legacy\|skills>` | Choose orchestration mode (default: legacy). `skills` uses only skill tools (`list_skills`, `run_skill`) and does not run legacy SOP+tools together. |
| `--skills-dir <path>` | Skill JSON directory for skills mode (default: `.onecmd/skills`) |
| `--skills-max-steps <N>` | Max steps per skill execution (default: 20) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | (none) | Google API key for the AI manager (Gemini) |
| `ANTHROPIC_API_KEY` | (none) | Anthropic API key for the AI manager (Claude) |
| `ONECMD_MGR_PROVIDER` | (auto) | Force provider (`google`, `anthropic`, `openai-codex`) |
| `ONECMD_MGR_MODEL` | (auto) | LLM model override |
| `ONECMD_AUTH_FILE` | `~/.onecmd/auth.json` | Auth profile file path (used for Codex OAuth creds) |
| `OPENAI_CODEX_TOKEN` | (none) | Optional direct Codex access token override |
| `OPENAI_CODEX_ACCOUNT_ID` | (none) | Optional direct Codex account id override |
| `OPENAI_CODEX_TOKEN_URL` | `https://auth.openai.com/oauth/token` | Refresh endpoint for Codex OAuth tokens |
| `ONECMD_VISIBLE_LINES` | `40` | Number of terminal lines to include in output |
| `ONECMD_SPLIT_MESSAGES` | off | Set to `1` to split long output across multiple messages |
| `ONECMD_AGENT_MODE` | `legacy` | Agent mode switch: `legacy` or `skills` |
| `ONECMD_SKILLS_DIR` | `.onecmd/skills` | Directory containing `*.json` skill workflows |

Terminal output is sent as a single message by default. Each new command or refresh **deletes the previous output messages** and sends fresh ones, creating a clean "live terminal" view rather than spamming the chat.

If your terminal produces very long output (e.g. build logs) and you want to see all of it, enable splitting:

```bash
ONECMD_SPLIT_MESSAGES=1 onecmd --apikey YOUR_BOT_TOKEN
```

### Prerequisites

- **Python 3.11+**
- **macOS:** Accessibility permission (prompted on first use)
- **Linux:** `tmux`

### Manual Run

```bash
# Create venv and install
python3 -m venv .venv
.venv/bin/pip install ".[macos]"   # macOS
.venv/bin/pip install .            # Linux

# Run with AI manager
GOOGLE_API_KEY=... .venv/bin/onecmd --apikey YOUR_BOT_TOKEN

# Run without AI manager (manual mode only)
.venv/bin/onecmd --apikey YOUR_BOT_TOKEN
```

### Run as a user systemd service (Linux)

For persistent background run with auto-restart:

```bash
cd ~/tools/1cmd-ai
./install-user-service.sh
```

Service commands:

```bash
systemctl --user status onecmd.service
journalctl --user -u onecmd.service -f
systemctl --user restart onecmd.service
systemctl --user stop onecmd.service
```

Optional (start even when not logged in):

```bash
sudo loginctl enable-linger $USER
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

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed technical documentation.

## License

MIT -- see [LICENSE](LICENSE).
