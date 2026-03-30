# OneCmd Architecture

## Overview

OneCmd lets you control terminal sessions remotely via Telegram. You message a Telegram bot from your phone, and onecmd reads terminal output, injects keystrokes, and optionally delegates to an AI manager agent for autonomous terminal control.

```
 Phone (Telegram)                   Machine
 +-----------+                     +-------------------------------------------+
 | User      |   Telegram API      | onecmd (Python)                           |
 | messages  | ==================> | +---------------------------------------+ |
 | .list     |                     | | bot/poller.py  long-poll getUpdates   | |
 | .1        |                     | | bot/api.py     sendMessage/editMsg    | |
 | .mgr      |                     | | bot/handler.py dispatch .list/.N/.mgr | |
 | "ls -la"  |                     | | terminal/display.py  capture + format | |
 |           | <================== | +---------------------------------------+ |
 | terminal  |   Telegram API      |        |                     |             |
 | output    |                     |  terminal/macos.py    terminal/tmux.py     |
 +-----------+                     |  (Accessibility API)  (tmux CLI)           |
                                   |        |                     |             |
                                   |   Terminal windows      tmux panes        |
                                   +-------------------------------------------+
                                          |
                                   +------+------+
                                   | manager/    |  LLM agent (in-process)
                                   | agent.py    |  Claude / Gemini API
                                   | tools.py    |  direct backend calls
                                   +-------------+
```

## Two-Layer Architecture

### Infrastructure Layer (Sealed, Deterministic, Human-Guarded)

Everything that touches the real world — system calls, network I/O, terminal control, authentication, data persistence. This is the trust boundary.

```
onecmd/
├── main.py              # Orchestrator (<200 LOC)
├── config.py            # Pydantic schema + validation
├── store.py             # SQLite KV store
├── emoji.py             # Keystroke emoji parser (sealed, 100% tested)
├── bot/
│   ├── poller.py        # Telegram long-polling
│   ├── api.py           # Telegram message sending
│   └── handler.py       # Command dispatch + auth gate
├── terminal/
│   ├── backend.py       # Backend interface + ValidatedBackend wrapper
│   ├── tmux.py          # tmux subprocess calls (sealed)
│   ├── macos.py         # macOS CGWindowList/AX/CGEvent
│   ├── scope.py         # Session/PID detection (frozen)
│   └── display.py       # Terminal text → Telegram formatting
└── auth/
    ├── owner.py         # Owner registration
    └── totp.py          # TOTP generation + verification (sealed)
```

**Guarding rules:**
1. No raw string interpolation in shell commands — all subprocess args via list form
2. All inputs validated at boundary — terminal IDs checked against known list
3. Auth checked before any action — hard gate, no bypass
4. Scope enforced at backend level — AI layer receives pre-scoped instance
5. Terminal ID whitelist — reject IDs not in last `list()` result
6. Rate limiting — max 10 `send_keys` per second per terminal

### AI Layer (Probabilistic, Flexible, Bounded)

LLM orchestration, conversation management, tool selection, background monitoring. All terminal access goes through the ValidatedBackend wrapper.

```
onecmd/manager/
├── router.py            # Manager mode toggle
├── agent.py             # LLM conversation loop
├── llm.py               # Provider abstraction (Claude, Gemini)
├── tools.py             # Tool definitions + dispatch
├── queue.py             # Per-terminal command queue
├── tasks.py             # Background + smart task runners
├── memory.py            # Long-term memory (SQLite)
└── skills.py            # Modular skills loading
```

### The Interface Boundary

The AI layer talks to the infra layer through exactly two interfaces:

```python
# Interface 1: Backend (terminal operations)
class Backend(Protocol):
    def list() -> list[TermInfo]
    def connected(term_id: str) -> bool
    def capture(term_id: str) -> str | None
    def send_keys(term_id: str, text: str) -> bool
    def free_list() -> None

# Interface 2: Bot API (messaging)
send_message(bot, chat_id, text) -> int
edit_message(bot, chat_id, msg_id, text) -> bool
delete_message(bot, chat_id, msg_id) -> bool
```

The AI layer **cannot**: access terminals outside scoped session/PID, bypass authentication, execute arbitrary shell commands, or modify its own scope.

## ValidatedBackend

The factory wraps every real backend with validation:

```python
class ValidatedBackend:
    def send_keys(self, term_id, text):
        self._validate_id(term_id)      # Reject unknown IDs
        self._check_rate_limit(term_id)  # Max 10/sec
        self._validate_text(text)        # Max 10000 chars
        return self._inner.send_keys(term_id, text)
```

## Scope Detection

Scope is detected once at startup and frozen:

1. Check for tmux session (`tmux display-message`)
2. If found: use tmux backend, scoped to session
3. Else on macOS: walk process tree for terminal app PID
4. Return frozen `Scope` dataclass — immutable after creation

## Backend Interface

| Method | macOS Implementation | Linux Implementation |
|--------|---------------------|---------------------|
| `list()` | `CGWindowListCopyWindowInfo` + AX title lookup | `tmux list-panes` |
| `connected()` | Window ID in CGWindowList, fallback to same-PID | `tmux display-message` |
| `capture()` | AX tree traversal for AXTextArea/AXStaticText | `tmux capture-pane -p` |
| `send_keys()` | `CGEventPostToPid` with virtual keycodes | `tmux send-keys` |

## Security Model

- **Owner lock**: First Telegram user registered as owner (SQLite). All others silently ignored.
- **TOTP**: HMAC-SHA1 RFC 6238, 30-second steps, ±1 tolerance, constant-time comparison.
- **Scope isolation**: tmux session scoping (Linux), PID scoping (macOS). AI layer receives pre-scoped backend.

## Known Limitations

- Single user per bot instance, single terminal connection at a time
- Long-polling only (no webhooks)
- macOS: US keyboard layout hardcoded for virtual keycodes
- macOS: Text capture requires Accessibility permission
- Linux: All controlled terminals must be tmux panes
- Manager: 30-turn conversation cap, 15 tool rounds, 500MB RSS watchdog
- Background tasks: 100 iterations or 1 hour max
