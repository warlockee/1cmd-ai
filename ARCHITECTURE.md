# OneCmd Architecture

## Overview

OneCmd lets you control terminal sessions remotely via Telegram. You message a Telegram bot from your phone, and onecmd reads terminal output, injects keystrokes, and optionally delegates to an AI manager agent (Claude) for autonomous terminal control.

```
 Phone (Telegram)                   Machine
 +-----------+                     +-------------------------------------------+
 | User      |   Telegram API      | onecmd (C binary)                         |
 | messages  | ==================> | +---------------------------------------+ |
 | .list     |                     | | bot_poll.c   long-poll getUpdates     | |
 | .1        |                     | | bot_api.c    sendMessage/editMessage  | |
 | .mgr      |                     | | commands.c   dispatch .list/.N/.mgr   | |
 | "ls -la"  |                     | | terminal_io.c  capture + format text  | |
 |           | <================== | +---------------------------------------+ |
 | terminal  |   Telegram API      |        |                     |             |
 | output    |                     |   backend_macos.c      backend_tmux.c     |
 +-----------+                     |   (Accessibility API)  (tmux CLI)         |
                                   |        |                     |             |
                                   |   Terminal windows      tmux panes        |
                                   +-------------------------------------------+
                                          |
                                   +------+------+
                                   | onecmd-mgr  |  (Python, Claude API)
                                   | stdin/stdout|  tool-use agent
                                   | JSON pipes  |
                                   +-------------+
                                          |
                                   +------+------+
                                   | onecmd-ctl  |  (C binary)
                                   | list/capture|  used by mgr
                                   | send/status |
                                   +-------------+
```

## Components

### onecmd (main bot binary)

The primary C binary. Runs as a long-polling Telegram bot that receives messages, dispatches commands, reads terminal text, and sends keystrokes. Built from `src/main.c`, `src/commands.c`, `src/terminal_io.c`, `src/bot_poll.c`, `src/bot_api.c`, `src/bot_http.c`, and one backend (`backend_macos.c` or `backend_tmux.c`).

Key responsibilities:
- Telegram Bot API long-polling loop
- Owner lock and TOTP authentication
- Command dispatch (`.list`, `.N`, `.mgr`, `.help`, `.health`, `.otptimeout`)
- Terminal text capture, formatting, and display with refresh buttons
- Keystroke injection with emoji modifier parsing
- Manager agent lifecycle (fork, pipes, reader thread)

### onecmd-ctl (CLI tool)

A standalone C binary that provides the same backend operations (list, capture, send, status) as a CLI. Used by the manager agent to interact with terminals without going through the bot.

```
onecmd-ctl list              # JSON array of terminals
onecmd-ctl capture <id>      # Print terminal text to stdout
onecmd-ctl send <id> <keys>  # Send keystrokes
onecmd-ctl status <id>       # Print "alive" or "dead"
```

Each invocation is a separate process with its own global state, so it does not conflict with the running onecmd bot.

### Manager Agent (Python)

An LLM-powered agent (`mgr/`) that uses Claude's tool-use API to autonomously monitor and control terminals. Communicates with onecmd via stdin/stdout JSON pipes. Calls `onecmd-ctl` for terminal operations.

Capabilities:
- List, read, and send commands to terminals
- Asynchronous command execution with output stability detection
- Per-terminal command queuing (prevents overlapping commands)
- Repeating background tasks (poll-and-check loops)
- Persistent long-term memory (SQLite)
- Health monitoring with auto-restart on memory limit

## Thread Model

```
Main Thread (bot_poll.c)
  |
  +-- botMain() infinite loop
  |     +-- botProcessUpdates() long-poll (1s timeout)
  |     +-- cron_callback() (currently no-op)
  |
  +-- Per-message handler threads (detached)
  |     +-- botHandleRequest()
  |           +-- handle_request()  [holds RequestLock]
  |
  +-- Manager reader thread (terminal_io.c)
        +-- mgr_reader_thread()
              reads mgr stdout, forwards to Telegram
```

**Request serialization**: All `handle_request()` calls acquire `RequestLock` (a global mutex), so commands are processed one at a time despite being dispatched to separate threads. This prevents concurrent access to connection state, terminal lists, and manager pipes.

**Manager reader thread**: A single background thread reads JSON lines from the manager's stdout pipe. When the manager process exits, the thread cleans up state (under `RequestLock`) so `mgr_start()` can restart it.

## Data Flow

### User sends a command

```
User -> Telegram -> getUpdates (bot_poll.c)
  -> parse JSON, create BotRequest
  -> spawn thread -> handle_request (commands.c)
  -> acquire RequestLock
  -> owner check, TOTP check
  -> dispatch: .list / .N / .mgr / keystrokes
  -> release RequestLock
```

### Terminal text display

```
commands.c: send_terminal_text(chat_id)
  -> backend_capture_text()           # platform-specific
  -> delete_terminal_messages()       # remove old tracked messages
  -> format_terminal_messages()       # HTML <pre>, split if needed
  -> send messages + Refresh button
  -> track message IDs for deletion on next update
```

### Manager IPC

```
onecmd -> manager (stdin):  {"chat_id": 547756577, "text": "list my terminals"}
manager -> onecmd (stdout): {"chat_id": 547756577, "text": "Here are your terminals..."}
```

One JSON object per line. The manager uses `onecmd-ctl` (subprocess) for terminal operations, not the pipes.

### Manager command flow

```
User: "run ls in terminal 1"
  -> onecmd sends JSON to mgr stdin
  -> Manager calls Claude API with tools
  -> Claude returns tool_use: send_command(terminal_id, keys)
  -> Manager calls onecmd-ctl send <id> <keys>
  -> Manager waits for output stability (terminal_queue.py)
  -> Manager sends result JSON to stdout
  -> mgr_reader_thread reads it, calls botSendMessage()
```

## Global State

All globals are defined in `main.c` and declared `extern` in `types.h` or `state.h`.

### Terminal State (types.h)

| Variable | Type | Owner | Description |
|----------|------|-------|-------------|
| `TermList` | `TermInfo*` | commands.c | Array of discovered terminal sessions |
| `TermCount` | `int` | commands.c | Number of terminals in list |
| `Connected` | `int` | commands.c | 1 if connected to a terminal |
| `ConnectedId` | `char[128]` | commands.c | ID of connected terminal |
| `ConnectedPid` | `pid_t` | commands.c | PID of connected terminal's process |
| `ConnectedName` | `char[128]` | commands.c | App name of connected terminal |
| `ConnectedTitle` | `char[256]` | commands.c | Window title of connected terminal |
| `DangerMode` | `int` | main.c | Show all windows, not just terminals (macOS) |

### Security State (state.h)

| Variable | Type | Owner | Description |
|----------|------|-------|-------------|
| `RequestLock` | `pthread_mutex_t` | commands.c | Serializes all request handling |
| `WeakSecurity` | `int` | main.c (set once) | Skip TOTP if 1 |
| `Authenticated` | `int` | commands.c | TOTP verified for current session |
| `LastActivity` | `time_t` | commands.c | Timestamp of last authenticated action |
| `OtpTimeout` | `int` | commands.c | Seconds before re-authentication required |

### Message Tracking (state.h)

| Variable | Type | Owner | Description |
|----------|------|-------|-------------|
| `TrackedMsgIds` | `int64_t[16]` | terminal_io.c | Message IDs of current terminal display |
| `TrackedMsgCount` | `int` | terminal_io.c | Count of tracked messages |

### Manager State (state.h)

| Variable | Type | Owner | Description |
|----------|------|-------|-------------|
| `MgrMode` | `int` | commands.c | 1 if in manager conversation mode |
| `MgrPid` | `pid_t` | terminal_io.c | PID of manager child process |
| `MgrWriteFd` | `int` | terminal_io.c | Pipe FD: onecmd writes to mgr stdin |
| `MgrReadFd` | `int` | terminal_io.c | Pipe FD: onecmd reads mgr stdout |
| `MgrReaderThread` | `pthread_t` | terminal_io.c | Reader thread handle |
| `MgrReaderRunning` | `int` | terminal_io.c | 1 if reader thread is active |
| `MgrPath` | `char[512]` | main.c (set once) | Path to manager Python script |

### Locking Requirements

All terminal state, connection state, manager state, and message tracking are accessed **only** under `RequestLock`. The one exception is `mgr_reader_thread`, which acquires `RequestLock` when cleaning up after the manager process exits. This is safe because the reader thread only writes to manager state fields, and the main request path checks those fields under the lock.

### Bot Framework State (bot.h / bot_poll.c)

| Variable | Type | Scope | Description |
|----------|------|-------|-------------|
| `Bot` | `OneCmdBot` | Global | API key, callbacks, triggers, debug flags |
| `DbHandle` | `sqlite3*` | Thread-local | Per-thread SQLite handle |
| `botStats` | struct | Global | Start time, query count (racy but intentional) |

## Manager IPC Protocol

### onecmd to manager (via stdin pipe)

```json
{"chat_id": 547756577, "text": "list my terminals"}
```

- `chat_id` (int): Telegram chat ID for routing replies
- `text` (string): User's message or internal command (`.health`)

### manager to onecmd (via stdout pipe)

```json
{"chat_id": 547756577, "text": "Here are your terminals:\n1. Terminal - ~/projects"}
```

- `chat_id` (int): Telegram chat ID to send the reply to
- `text` (string): Message text (Markdown-escaped by onecmd before sending)

One JSON object per line. Manager stderr goes to onecmd's stderr (log output).

### Manager startup

1. `mgr_start()` creates two pipes and forks
2. Child: redirects stdin/stdout to pipes, execs Python with `MgrPath`
3. Parent: stores pipe FDs and PID, starts `mgr_reader_thread`
4. Manager is started eagerly on boot (if `--mgr` is given) for fast first response

### Manager restart

If the manager process dies (or exceeds its memory limit and self-exits), the reader thread detects EOF, cleans up state under `RequestLock`, and sets `MgrPid = -1`. The next `mgr_send()` call will trigger `mgr_start()` to restart it. If a write fails, `mgr_send()` also attempts one restart.

## Backend Interface

`backend.h` defines five functions that abstract platform differences:

| Function | macOS Implementation | Linux Implementation |
|----------|---------------------|---------------------|
| `backend_list()` | `CGWindowListCopyWindowInfo` + AX title lookup | `tmux list-panes -a` |
| `backend_free_list()` | `free(TermList)` | `free(TermList)` |
| `backend_connected()` | Check window ID in CGWindowList, fallback to same-PID window | `tmux display-message -t <id>` |
| `backend_capture_text()` | AX tree traversal for AXTextArea/AXStaticText | `tmux capture-pane -t <id> -p` |
| `backend_send_keys()` | `CGEventPostToPid` with virtual keycodes | `tmux send-keys` (literal + special) |

### macOS specifics

- Uses private API `_AXUIElementGetWindow` to match CGWindowID to AXUIElement
- Raises the target window via `AXRaiseAction` + `kAXFrontmostAttribute` before sending keys
- Maps ASCII to virtual keycodes (US keyboard layout)
- Filters to known terminal apps unless `DangerMode` is set
- Strips null bytes from AX text (iTerm2 uses them for empty cells)

### Linux specifics

- All terminals must be tmux panes
- Literal text is sent via `tmux send-keys -l` for efficiency
- Special keys (Enter, Tab, Escape) and modified keys (Ctrl-C) use non-literal `tmux send-keys`
- Shell arguments are single-quote escaped to prevent injection

### Emoji keystroke modifiers

Both backends share emoji parsing (`emoji.c`):

| Emoji | UTF-8 Bytes | Action |
|-------|-------------|--------|
| Red heart | E2 9D A4 [EF B8 8F] | Ctrl modifier |
| Blue heart | F0 9F 92 99 | Alt modifier |
| Green heart | F0 9F 92 9A | Cmd modifier (macOS only) |
| Yellow heart | F0 9F 92 9B | Send Escape |
| Orange heart | F0 9F A7 A1 | Send Enter |
| Purple heart | F0 9F 92 9C | Suppress trailing newline (must be last) |

## Security Model

### Owner Lock

The first Telegram user to message the bot is registered as the owner (stored in SQLite KV store). All subsequent messages from non-owners are silently ignored. Reset by deleting `mybot.sqlite`.

### TOTP Authentication

Unless `--use-weak-security` is set:

1. On first run, a 20-byte secret is generated from `/dev/urandom` and stored as hex in SQLite
2. A QR code is printed to the terminal for Google Authenticator setup
3. Before any command is processed, onecmd checks `Authenticated` and `LastActivity`
4. If unauthenticated or timed out, user must enter a 6-digit OTP code
5. TOTP verification allows +/- 1 time step (30-second windows)
6. Timeout is configurable via `.otptimeout` (30-28800 seconds, default 300)

### Weak Security Mode

`--use-weak-security` disables all TOTP logic. The owner lock still applies -- only the registered owner can use the bot.

## File Map

### C Source (`src/`)

| File | LOC | Description |
|------|-----|-------------|
| `main.c` | 95 | Entry point, global variable definitions, argument parsing |
| `commands.c` | 277 | Command dispatch, owner/TOTP checks, connection management |
| `terminal_io.c` | 341 | Terminal text display, message tracking, manager IPC |
| `bot_poll.c` | 455 | Telegram long-polling loop, update parsing, thread dispatch |
| `bot_api.c` | 351 | Telegram Bot API wrappers (send, edit, keyboard, file download) |
| `bot_http.c` | 292 | HTTP/curl layer, pattern matching, allocator wrappers |
| `backend_macos.c` | 559 | macOS backend: Accessibility API text capture, CGEvent keystrokes |
| `backend_tmux.c` | 442 | Linux backend: tmux CLI for list, capture, send-keys |
| `onecmd_ctl.c` | 215 | CLI tool for manager agent (list/capture/send/status) |
| `format.c` | 140 | Markdown/HTML escaping, list/help builders, line extraction |
| `totp.c` | 200 | TOTP setup, QR code display, OTP verification |
| `emoji.c` | 53 | UTF-8 emoji heart parsing for keystroke modifiers |
| `sqlite_wrap.c` | 282 | SQLite query helpers, KV store operations |
| `json_wrap.c` | 140 | cJSON selector/query extension |

### C Headers (`include/`)

| File | LOC | Description |
|------|-----|-------------|
| `types.h` | 106 | Core types (TermInfo, BotRequest), constants, global externs |
| `bot.h` | 98 | OneCmdBot struct, Telegram API function declarations |
| `state.h` | 40 | Shared mutable state externs (locks, auth, manager, tracking) |
| `backend.h` | 37 | Backend interface (5 functions) |
| `terminal_io.h` | 44 | Terminal display and manager IPC declarations |
| `format.h` | 41 | Formatting function declarations |
| `commands.h` | 24 | Command handler declarations |
| `emoji.h` | 24 | Emoji parsing declarations |
| `totp.h` | 23 | TOTP function declarations |
| `sqlite_wrap.h` | 37 | SQLite wrapper declarations |

### Python Manager (`mgr/`)

| File | LOC | Description |
|------|-----|-------------|
| `main.py` | 59 | Entry point, stdin JSON reader, thread dispatch |
| `manager.py` | 428 | Manager class: Claude API loop, tool dispatch, health, memory |
| `tools.py` | 190 | Tool definitions (Claude API schema) and terminal helpers |
| `utils.py` | 83 | Config constants, send_response(), onecmd-ctl wrapper |
| `terminal_queue.py` | 206 | Per-terminal command queue with output stability detection |
| `background_task.py` | 95 | Repeating poll-and-check background tasks |
| `memory.py` | 77 | Long-term memory system (SQLite) |
| `stats.py` | 69 | Runtime statistics and content serialization |

### Vendor (`vendor/`)

| File | Description |
|------|-------------|
| `cJSON.c/h` | JSON parser (Salvatore Sanfilippo fork) |
| `sds.c/h` | Simple Dynamic Strings library |
| `qrcodegen.c/h` | QR code generator for TOTP setup |
| `sha1.c/h` | SHA-1 for HMAC-SHA1 TOTP computation |

### Build & Setup

| File | LOC | Description |
|------|-----|-------------|
| `Makefile` | 95 | Build system with platform detection |
| `setup.sh` | 291 | Interactive installer (clone, deps, build, bot setup) |

## Build

### Platform Detection

The Makefile uses `uname -s` to detect the platform:

- **Darwin (macOS)**: Uses `clang`, links CoreGraphics/CoreFoundation/CoreServices/ApplicationServices frameworks, builds `backend_macos.o`
- **Linux**: Uses `gcc`, no frameworks, builds `backend_tmux.o`

### Dependencies

- `libcurl` (HTTP for Telegram API)
- `libsqlite3` (KV store, TOTP secrets)
- macOS: Accessibility permission required
- Linux: `tmux` must be installed

### Build Commands

```bash
make              # Build onecmd and onecmd-ctl
make clean        # Remove binaries and object files
```

### Manager Dependencies

```bash
pip install anthropic   # Claude API client
# Or use the venv created by setup.sh
```

## Known Limitations

### Race Conditions

- `botStats.queries` is incremented without locking (documented as intentional -- stats are advisory)
- `RequestLock` serializes all request handling, which prevents parallelism but avoids races on shared state

### Single-User Design

- Only one owner per bot instance
- Only one terminal connection at a time (connecting to `.2` disconnects from `.1`)
- Only one manager conversation per bot

### No Webhook Support

- Uses long-polling only (`getUpdates`). No webhook endpoint
- 1-second polling timeout with 100ms backoff on empty responses

### Terminal Text Limitations

- macOS: US keyboard layout hardcoded for virtual keycodes
- macOS: Text capture requires Accessibility permission and depends on AX tree structure
- Linux: All controlled terminals must be tmux panes
- Terminal output is HTML-escaped and wrapped in `<pre>` tags; non-text content is not captured

### Manager Limitations

- Manager output truncated to 4000 chars per tool result
- Conversation sliding window caps at 60 messages (30 turns)
- Memory limit of 500MB RSS before auto-restart
- Background tasks limited to 100 iterations or 1 hour
- `onecmd-ctl` subprocess timeout of 15 seconds per call

### Message Limits

- Telegram message limit is 4096 characters
- Terminal output is truncated to fit by default (keeps tail)
- Optional split mode (`ONECMD_SPLIT_MESSAGES=1`) sends multiple messages
- Maximum 16 tracked messages for deletion-on-refresh
