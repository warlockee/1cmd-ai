# OneCmd Migration & Refactoring Plan

> Rebranding teleterm → onecmd with LOD (LLM-Oriented Design) refactoring.
> Source: `/Users/erik/lxy/teleterm` → Target: `/Users/erik/lxy/onecmd`

---

## Phase 0: Scaffold Directory Structure

**Agent**: executor (sonnet)
**Depends on**: nothing

Create the target directory layout in `/Users/erik/lxy/onecmd`:

```
onecmd/
├── src/                    # All C source files
├── include/                # All C headers
├── vendor/                 # Third-party vendored libraries (sealed, untouched)
├── mgr/                    # Python AI manager (fragmented)
├── Makefile
├── setup.sh
├── README.md
├── LICENSE
└── .gitignore
```

Commands:
```bash
cd /Users/erik/lxy/onecmd
mkdir -p src include vendor mgr
```

---

## Phase 1: Vendor Libraries (Sealed Deterministic — Copy As-Is)

**Agent**: executor (haiku)
**Depends on**: Phase 0

These are third-party, deterministic, human-tested libraries. Per LOD Rule 6 (Sealed Deterministic Logic), copy verbatim — no modifications.

| Source | Destination | LOC |
|--------|-------------|-----|
| `cJSON.c` | `vendor/cJSON.c` | 3110 |
| `cJSON.h` | `vendor/cJSON.h` | 293 |
| `sds.c` | `vendor/sds.c` | 1300 |
| `sds.h` | `vendor/sds.h` | 274 |
| `sdsalloc.h` | `vendor/sdsalloc.h` | 47 |
| `qrcodegen.c` | `vendor/qrcodegen.c` | 1027 |
| `qrcodegen.h` | `vendor/qrcodegen.h` | 385 |
| `sha1.c` | `vendor/sha1.c` | 229 |
| `sha1.h` | `vendor/sha1.h` | 37 |
| `xmalloc.h` | `vendor/xmalloc.h` | 6 |

---

## Phase 2: Schema Separation — Headers with Calling Specs

**Agent**: executor (sonnet)
**Depends on**: Phase 0

Per LOD Pattern 6 (Schema Separated from Logic) and Pattern 2 (Calling Specs as Black Boxes), create clean headers with type definitions separated from behavior, and calling specs at the top of each.

### `include/types.h` — Shared Data Structures (~60 LOC)
**Calling spec**: Pure type definitions. No functions. No side effects.

Extract from `backend.h` and `botlib.h`:
- `TermInfo` struct
- `BotRequest` struct
- All constants (`TB_TYPE_*`, `TB_FILE_TYPE_*`, `TB_FLAGS_*`)
- Global state declarations (`TermList`, `TermCount`, `Connected*`, `DangerMode`)

### `include/backend.h` — Backend Interface (~30 LOC)
**Calling spec**: Abstract interface for platform-specific terminal control.
- `backend_list()` → int (count), fills global `TermList`/`TermCount`
- `backend_free_list()` → void
- `backend_connected()` → int (1=alive, 0=dead)
- `backend_capture_text()` → sds (caller frees) or NULL
- `backend_send_keys(text)` → int (0=ok, -1=error)

### `include/bot.h` — Bot/Telegram API (~60 LOC)
**Calling spec**: Telegram Bot API wrapper. All functions require prior `startBot()`.
- HTTP layer: `makeHTTPGETCall()`, `makeHTTPGETCallOpt()`
- Bot API: `botSendMessage()`, `botEditMessageText()`, `botGetFile()`, etc.
- Lifecycle: `startBot()`, `botMain()`, `readApiKeyFromFile()`
- Memory: `xmalloc()`, `xrealloc()`, `xfree()`

### `include/commands.h` — Command Handler Interface (~20 LOC)
**Calling spec**: Dispatches user commands from Telegram messages.
- `handle_request(db, br)` → void, processes one BotRequest
- `cron_callback(db)` → void, periodic task

### `include/totp.h` — TOTP Authentication (~15 LOC)
**Calling spec**: TOTP setup and verification. Side effects: SQLite reads/writes.
- `totp_setup(db_path)` → int (0=ok)
- `totp_verify(db, code_str)` → int (1=valid)

### `include/format.h` — Message Formatting (~15 LOC)
**Calling spec**: Pure text transformation functions. No side effects except allocation.
- `markdown_escape(s)` → sds
- `html_escape(text)` → sds
- `build_list_message()` → sds
- `build_help_message()` → sds

### `include/terminal_io.h` — Terminal I/O (~15 LOC)
**Calling spec**: Terminal text capture/display pipeline. Side effects: sends Telegram messages.
- `send_terminal_text(chat_id)` → void
- `format_terminal_messages(raw, count)` → sds*
- `disconnect()` → void

### `include/emoji.h` — Emoji Parsing (~15 LOC)
**Calling spec**: Pure functions. Match emoji byte sequences. No side effects.
- `match_red_heart(p, remaining)` → int (bytes consumed or 0)
- `match_colored_heart(p, remaining, heart)` → int
- `match_orange_heart(p, remaining)` → int
- `match_purple_heart(p, remaining)` → int
- `ends_with_purple_heart(text)` → int (1=yes)

---

## Phase 3: Radical Fragmentation — C Source Files

**Agent**: executor (sonnet) — 3 parallel agents, one per major file
**Depends on**: Phase 2

Per LOD Pattern 1 (Radical Fragmentation) and Hard Rule 1 (max 800 LOC), split the three oversized source files. Files already under 800 LOC are copied with only rebranding changes.

### 3A: Fragment `bot_common.c` (958 LOC) → 5 files

**`src/main.c`** (~100 LOC) — LOD Pattern 5 (Orchestrator as Recipe)
- `main()` function — slim orchestrator
- Global state definitions (`TermList`, `TermCount`, `Connected*`, etc.)
- Arg parsing, `startBot()` call
- Reads like a recipe: init → configure → run

**`src/commands.c`** (~300 LOC)
- `handle_request()` — command dispatch
- `.list`, `.help`, `.mgr`, `.health`, `.otptimeout` handlers
- Number commands (`.1`, `.2`, etc.) for terminal connection
- `cron_callback()`
- `disconnect()`

**`src/totp.c`** (~200 LOC)
- `base32_encode()`, `totp_code()`, `hex_to_bytes()`, `bytes_to_hex()`
- `print_qr_ascii()`
- `totp_setup()`, `totp_verify()`

**`src/format.c`** (~150 LOC)
- `markdown_escape()`, `html_escape()`
- `build_list_message()`, `build_help_message()`
- `get_visible_lines()`, `get_split_messages()`
- `last_n_lines()`

**`src/terminal_io.c`** (~200 LOC)
- `send_terminal_text()`
- `send_html_message()`
- `delete_terminal_messages()`
- `format_terminal_messages()`
- Manager integration: `mgr_reader_thread()`, `mgr_start()`, `mgr_send()`

### 3B: Fragment `botlib.c` (991 LOC) → 3 files

**`src/bot_http.c`** (~200 LOC)
- `makeHTTPGETCallWriterSDS()`, `makeHTTPGETCallWriterFILE()`
- `makeHTTPGETCall()`, `makeHTTPGETCallOpt()`
- `strmatch()` (glob pattern matching)
- `xmalloc()`, `xrealloc()`, `xfree()`

**`src/bot_api.c`** (~400 LOC)
- `makeGETBotRequest()`
- `botSendMessage()`, `botSendMessageAndGetInfo()`, `botSendMessageWithKeyboard()`
- `botEditMessageText()`, `botEditMessageTextWithKeyboard()`
- `botAnswerCallbackQuery()`, `botGetFile()`, `botGetUsername()`
- `freeBotRequest()`, `createBotRequest()`

**`src/bot_poll.c`** (~400 LOC)
- `botProcessUpdates()` — main polling function
- `botHandleRequest()` — thread handler
- `botMain()` — polling loop
- `startBot()` — initialization
- `readApiKeyFromFile()`
- `dbInit()`, `dbClose()`, `resetBotStats()`

### 3C: Copy files already under 800 LOC (rebrand only)

| Source | Destination | LOC | Changes |
|--------|-------------|-----|---------|
| `backend_macos.c` | `src/backend_macos.c` | 558 | `#include` paths, rename `teleterm` refs |
| `backend_tmux.c` | `src/backend_tmux.c` | 441 | `#include` paths, rename `teleterm` refs |
| `emoji.c` | `src/emoji.c` | 53 | `#include` paths |
| `json_wrap.c` | `src/json_wrap.c` | 140 | `#include` paths |
| `sqlite_wrap.c` | `src/sqlite_wrap.c` | 282 | `#include` paths |
| `sqlite_wrap.h` | `include/sqlite_wrap.h` | 25 | guard rename |
| `teleterm_ctl.c` | `src/onecmd_ctl.c` | 215 | Full rebrand: binary name, env vars, strings |

---

## Phase 4: Radical Fragmentation — Python Manager

**Agent**: executor (sonnet)
**Depends on**: Phase 0

Per LOD Pattern 1, split `mgr/teleterm_mgr.py` (1124 LOC) into focused modules.

### `mgr/main.py` (~60 LOC) — Orchestrator
- Entry point, `main()` function
- stdin/stdout JSON protocol loop
- Signal handlers
- Imports and wires up Manager

### `mgr/manager.py` (~400 LOC) — Manager Class Core
- `Manager` class: `__init__`, `process_message`, `_process_message_locked`
- `handle_tool_call` dispatch
- `_build_system_prompt`
- Conversation management (`_get_conversation`, sliding window)
- Risk classification

### `mgr/terminal_queue.py` (~200 LOC)
- `TerminalQueue` class
- `QueuedCommand` class
- `_wait_stable()` function (pure utility)
- `_output_diff_ratio()` function (pure utility — LOD Pattern 4: Toolification)

### `mgr/background_task.py` (~100 LOC)
- `BackgroundTask` class
- Task lifecycle: start, run, cancel

### `mgr/memory.py` (~100 LOC)
- `_init_memory_db()`, `_load_memories()`, `_save_memory()`, `_delete_memory()`
- SQLite schema for memories

### `mgr/tools.py` (~150 LOC)
- Tool definitions array (for Claude API)
- `list_terminals()`, `capture_terminal()`, `send_keys()`
- `ctl_run()` subprocess wrapper

### `mgr/stats.py` (~50 LOC)
- `Stats` class
- `serialize_content()` utility

### `mgr/utils.py` (~50 LOC)
- `send_response()` — stdout JSON protocol
- `_next_task_id()` — ID generator
- Shared constants (`DEFAULT_STABLE_SECONDS`, `MAX_CONVERSATION_TURNS`, etc.)

### `mgr/requirements.txt`
- Copy from source, identical

---

## Phase 5: Rebranding

**Agent**: executor (sonnet)
**Depends on**: Phases 3, 4

Systematic find-and-replace across all new files. **No teleterm references may remain.**

### String replacements (case-sensitive):
| Pattern | Replacement | Context |
|---------|-------------|---------|
| `teleterm` | `onecmd` | Binary names, paths, strings |
| `Teleterm` | `OneCmd` | Display names, docs |
| `TELETERM` | `ONECMD` | Env vars, macros |
| `teleterm-ctl` | `onecmd-ctl` | Binary name |
| `teleterm_ctl` | `onecmd_ctl` | C identifier, filename |
| `teleterm_mgr` | `onecmd_mgr` | Python module name |

### Files requiring rebrand:
- All `src/*.c` — strings, comments, binary refs
- All `include/*.h` — header guards, comments
- `mgr/*.py` — module names, paths, env vars
- `Makefile` — targets, object names
- `setup.sh` — prompts, paths, binary names
- `README.md` — all documentation
- `LICENSE` — project name
- `.gitignore` — binary names

### Environment variable renames:
| Old | New |
|-----|-----|
| `TELETERM_VISIBLE_LINES` | `ONECMD_VISIBLE_LINES` |
| `TELETERM_SPLIT_MESSAGES` | `ONECMD_SPLIT_MESSAGES` |
| `TELETERM_CTL` | `ONECMD_CTL` |
| `TELETERM_MGR_MODEL` | `ONECMD_MGR_MODEL` |

---

## Phase 6: Build System

**Agent**: executor (sonnet)
**Depends on**: Phases 1-5

### New `Makefile` (~80 LOC)

Must handle the new `src/`, `include/`, `vendor/` layout:

```makefile
UNAME_S := $(shell uname -s)

SRCDIR = src
INCDIR = include
VENDORDIR = vendor

CFLAGS += -I$(INCDIR) -I$(VENDORDIR)

# Platform detection (same logic as original)
# BACKEND = backend_macos.o or backend_tmux.o

# Objects split to match new file structure:
BOT_OBJS = main.o commands.o totp.o format.o terminal_io.o \
           emoji.o bot_http.o bot_api.o bot_poll.o \
           $(BACKEND) sqlite_wrap.o json_wrap.o

VENDOR_OBJS = cJSON.o sds.o qrcodegen.o sha1.o

CTL_OBJS = onecmd_ctl.o emoji.o $(BACKEND) sds.o cJSON.o

all: onecmd onecmd-ctl
```

### Verification:
```bash
make clean && make
# Must produce: onecmd, onecmd-ctl
```

---

## Phase 7: Setup Script

**Agent**: executor (sonnet)
**Depends on**: Phase 5

Adapt `setup.sh` (290 LOC) with rebranding:
- All user-facing strings: "Teleterm" → "OneCmd"
- Binary names: `teleterm` → `onecmd`, `teleterm-ctl` → `onecmd-ctl`
- Generated `run.sh`: updated env var names
- Python venv path stays at `mgr/.venv`
- API key file stays at `apikey.txt`

---

## Phase 8: Feature Parity Testing

**Agent**: `functional-test-expert` (built-in agent) + verifier (sonnet)
**Depends on**: Phase 6

The `functional-test-expert` agent runs end-to-end validation in a persistent loop:
it analyzes what onecmd is supposed to do, designs functional test scenarios, executes
them, and automatically kicks off bug-fixing subagents when failures are found. It
operates until all tests pass. The verifier agent runs in parallel for LOD compliance
and residual-reference checks.

Point-to-point functional tests verifying each teleterm feature works identically in onecmd.

### Test 1: Build Verification
```bash
cd /Users/erik/lxy/onecmd
make clean && make
test -x onecmd && echo "PASS: onecmd binary" || echo "FAIL"
test -x onecmd-ctl && echo "PASS: onecmd-ctl binary" || echo "FAIL"
```

### Test 2: CLI Tool — List
```bash
./onecmd-ctl list
# Must return JSON array (empty or with terminals)
# Verify no "teleterm" in output
```

### Test 3: CLI Tool — Capture & Send
```bash
# Requires active terminal session
ID=$(./onecmd-ctl list | python3 -c "import sys,json; terms=json.load(sys.stdin); print(terms[0]['id']) if terms else print('SKIP')")
if [ "$ID" != "SKIP" ]; then
    ./onecmd-ctl capture "$ID"    # Must return terminal text
    ./onecmd-ctl status "$ID"     # Must return connected/alive
fi
```

### Test 4: No Residual "teleterm" References
```bash
# Search all source files for "teleterm" (case-insensitive)
grep -ri "teleterm" src/ include/ mgr/ Makefile setup.sh README.md .gitignore 2>/dev/null
# Must return 0 matches
```

### Test 5: Python Manager Import Check
```bash
cd /Users/erik/lxy/onecmd/mgr
python3 -c "
import sys; sys.path.insert(0, '.')
from main import *
from manager import Manager
from terminal_queue import TerminalQueue
from background_task import BackgroundTask
from memory import *
from tools import *
from stats import Stats
from utils import *
print('PASS: All Python modules import successfully')
"
```

### Test 6: Environment Variable Handling
```bash
# Verify new env vars are used
grep -r "ONECMD_VISIBLE_LINES" src/ include/
grep -r "ONECMD_SPLIT_MESSAGES" src/ include/
grep -r "ONECMD_CTL" mgr/
grep -r "ONECMD_MGR_MODEL" mgr/ src/
# Each must return at least 1 match
```

### Test 7: Header Consistency
```bash
# Verify all .c files compile individually
cd /Users/erik/lxy/onecmd
for f in src/*.c; do
    $(grep -q Darwin /tmp/.uname 2>/dev/null && echo clang || echo gcc) \
        -fsyntax-only -Iinclude -Ivendor "$f" 2>&1 && echo "PASS: $f" || echo "FAIL: $f"
done
```

### Test 8: LOD Compliance Check
```bash
# Verify no file exceeds 800 LOC
cd /Users/erik/lxy/onecmd
for f in src/*.c include/*.h mgr/*.py; do
    lines=$(wc -l < "$f")
    if [ "$lines" -gt 800 ]; then
        echo "FAIL: $f has $lines lines (max 800)"
    else
        echo "PASS: $f ($lines LOC)"
    fi
done
```

### Test 9: Cross-Compare Feature Coverage
For each teleterm function, verify its onecmd equivalent exists:

| teleterm function | onecmd location | test |
|---|---|---|
| `main()` | `src/main.c` | compiles |
| `handle_request()` | `src/commands.c` | compiles |
| `totp_setup()` | `src/totp.c` | compiles |
| `totp_verify()` | `src/totp.c` | compiles |
| `markdown_escape()` | `src/format.c` | compiles |
| `build_list_message()` | `src/format.c` | compiles |
| `build_help_message()` | `src/format.c` | compiles |
| `send_terminal_text()` | `src/terminal_io.c` | compiles |
| `mgr_start()` | `src/terminal_io.c` | compiles |
| `makeHTTPGETCall()` | `src/bot_http.c` | compiles |
| `botSendMessage()` | `src/bot_api.c` | compiles |
| `botProcessUpdates()` | `src/bot_poll.c` | compiles |
| `startBot()` | `src/bot_poll.c` | compiles |
| `backend_list()` | `src/backend_macos.c` | compiles |
| `backend_capture_text()` | `src/backend_macos.c` | compiles |
| `backend_send_keys()` | `src/backend_macos.c` | compiles |
| `Manager` class | `mgr/manager.py` | imports |
| `TerminalQueue` class | `mgr/terminal_queue.py` | imports |
| `BackgroundTask` class | `mgr/background_task.py` | imports |
| `Stats` class | `mgr/stats.py` | imports |

### Test 10: Integration — Bot Start (smoke test)
```bash
cd /Users/erik/lxy/onecmd
# Create dummy apikey.txt for smoke test
echo "test:dummy" > /tmp/onecmd_test_apikey.txt
timeout 3 ./onecmd --use-weak-security 2>&1 || true
# Must not segfault; exit with API error is acceptable
```

---

## Execution Order & Agent Assignment

```
Phase 0: scaffold          → 1 executor (haiku)          [~1 min]
Phase 1: vendor copy       → 1 executor (haiku)          [~1 min]
Phase 2: headers           → 1 executor (sonnet)         [~5 min]
Phase 3A: fragment bot_common → 1 executor (sonnet)      [~10 min]  ┐
Phase 3B: fragment botlib     → 1 executor (sonnet)      [~8 min]   ├─ parallel
Phase 3C: copy small files    → 1 executor (sonnet)      [~3 min]   │
Phase 4: fragment python      → 1 executor (sonnet)      [~8 min]   ┘
Phase 5: rebranding           → 1 executor (sonnet)      [~5 min]
Phase 6: build system         → 1 executor (sonnet)      [~5 min]
Phase 7: setup script         → 1 executor (sonnet)      [~3 min]
Phase 8: testing              → functional-test-expert   [loops until pass]
                                + 1 verifier (sonnet)      [LOD + rebrand checks]
```

**Total**: ~12 agents, ~25 minutes elapsed (with parallelism)

### functional-test-expert Role (Phase 8)

The `functional-test-expert` agent is the **gate keeper** — nothing ships until it says PASS.

**Responsibilities**:
1. Analyze onecmd's feature set by comparing against teleterm source
2. Design functional test scenarios for every user-facing feature:
   - Build & binary existence
   - CLI tool (onecmd-ctl): list, capture, send, status
   - Python manager: all module imports, Manager instantiation
   - Bot startup smoke test (no segfault)
   - Env var handling (ONECMD_* vars used correctly)
3. Execute all tests
4. On failure: automatically spawn bug-fixing subagents to resolve issues
5. Re-run failing tests after fixes
6. Loop until **all tests pass**
7. Final report: PASS/FAIL matrix with evidence

**Parallel verifier** handles non-functional checks:
- LOD compliance (≤800 LOC per file)
- Zero residual "teleterm" references
- Header consistency (all .c files syntax-check)
- Cross-compare function coverage table

---

## LOD Compliance Summary

| LOD Rule/Pattern | Application |
|---|---|
| Hard Rule 1: ≤800 LOC | All files verified in Test 8 |
| Hard Rule 2: Single responsibility | Each file has one role (see Phase 2-4) |
| Hard Rule 3: Pure functions | Emoji, format, TOTP math extracted as pure functions |
| Hard Rule 4: Flat architecture | No inheritance, no factory patterns |
| Hard Rule 5: Explicit contracts | Calling specs in every header |
| Hard Rule 6: Sealed deterministic | Vendor libs copied verbatim |
| Hard Rule 7: Flexible probabilistic | Manager routing stays flexible |
| Pattern 1: Radical Fragmentation | 3 files split into 13 |
| Pattern 2: Calling Specs | Headers document inputs/outputs/side-effects |
| Pattern 3: Variant Registry | Backends via function pointers |
| Pattern 4: Toolification | Pure utils extracted (emoji, format, TOTP math) |
| Pattern 5: Orchestrator as Recipe | main.c is ~100 LOC recipe |
| Pattern 6: Schema Separation | types.h has structs, no behavior |
| Pattern 7: Zero-Hallucination | Config validation, env var bounds |
| Pattern 8: Dict Dispatch | Backend dispatch table |
| Pattern 9: Feedback Loops | Health monitoring, stats |

---

## Critical Constraints

1. **No functionality changes** — behavior must be identical to teleterm
2. **No teleterm references** — grep must find zero matches (Test 4)
3. **macOS build required** — primary dev platform, must compile and link
4. **Vendored libs untouched** — cJSON, sds, qrcodegen, sha1 are sealed
5. **Python 3.x compatible** — manager must work with system Python
6. **Bot token format unchanged** — `apikey.txt` same format
7. **Database schema unchanged** — `mybot.sqlite` same tables
