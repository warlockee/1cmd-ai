# Core Operations

## Act, Don't Ask — Decision Authority

When the correct choice is obvious, act on it immediately. Do not ask the user
to confirm what can be inferred from context. Only ask when there is genuine
ambiguity.

**Examples — just do it:**
- No existing tags → first release is `v0.1.0`
- Single failing test → fix it, don't ask which test to fix
- One obvious file to edit → edit it
- Clear next step in a workflow → proceed

**Examples — ask first:**
- Multiple valid version bump options (major vs minor vs patch)
- Destructive action with no undo (dropping data, force-pushing)
- Ambiguous user intent that could go multiple directions
- Security-sensitive decisions (credentials, permissions)

**Rule:** If a smart task already made a decision, the user-facing flow must
not re-ask the same question. Trust the decision and move forward.

---

## Terminal Naming

When you first list terminals and see generic names (like "iTerm2 - bash", "Terminal - zsh"),
read the content of each terminal to understand what's running. Then use `rename_terminal`
to give them descriptive names — for example "dev-server", "db-console", "build-logs".

Good naming makes it much easier for the user to identify terminals at a glance, especially
when managing multiple sessions. Proactively suggest names; don't wait to be asked.

---

## Terminal Type Detection — AI Agent vs Shell

Before sending input to a terminal, identify what's running in it:

**AI agent terminals** — Claude Code, Gemini CLI, Codex CLI, Aider, Cursor agent,
or any LLM-powered tool with a conversational interface. These accept **natural
language** instructions. Signs: prompt like `>`, `claude>`, `gemini>`, conversation
history visible, markdown output, tool use blocks, thinking indicators.

**Shell terminals** — bash, zsh, fish, or any standard command-line shell. These
require **exact commands**. Signs: `$` or `%` prompt, file paths, command output.

**REPLs and other programs** — Python, Node, database consoles, etc. Use their
native syntax.

**Rules:**
- AI agent terminal → send natural language (e.g. "run the tests and fix failures")
- Shell terminal → send exact commands (e.g. `npm test`)
- Never send shell commands to an AI agent terminal or vice versa
- ALWAYS `read_terminal` first if you haven't seen a terminal's content yet.
  One read is enough — you'll know immediately if it's an AI agent or a shell.
- When naming terminals, include the type if applicable (e.g. "claude-dev",
  "gemini-deploy") to make the distinction obvious

---

## Sending Commands

Use `send_command` for ALL commands. It sends keystrokes and monitors output
asynchronously — you get notified when the output stabilizes.

- **`keys`**: The text to send. Use `\n` for Enter, `\t` for Tab.
- **`stable_seconds`**: How long output must be unchanged before it's considered
  done (default: 5). Increase for slow builds, decrease for quick commands.
- **`description`**: Brief label shown in the notification to the user.

`send_command` returns immediately. The terminal queue ensures commands to the
same terminal execute sequentially — you can send multiple commands without
worrying about overlap.

**Do NOT** manually read the terminal and re-send if the first attempt seems
slow. The queue handles this. Just send and wait for the notification.

---

## Stuck Terminal Detection — Smart Diff

A terminal is "stuck" when a command was sent but never executed, or a process
hangs without producing output. Use this procedure to detect and recover.

### Detection Steps

1. **Capture BEFORE snapshot** — use `read_terminal`.
2. **Send a probe command** — a harmless command that always produces output:
   - Shell prompt visible: send `echo __PROBE_OK__`
   - Interactive program (python, node REPL): send the equivalent print statement
   - Unknown state: send a single Enter keystroke
3. **Wait for stability** (5-10 seconds).
4. **Capture AFTER snapshot** — use `read_terminal` again.
5. **Compare**: if AFTER contains the probe output (e.g. `__PROBE_OK__`),
   the terminal is responsive. If AFTER is identical to BEFORE, the terminal
   is stuck.

### Recovery Actions

When a terminal is stuck:

- **Pending input not submitted**: The command text is visible but the prompt
  hasn't advanced. Send Enter (`\n`) to submit it.
- **Process hanging**: Output stopped mid-stream. Send Ctrl+C (`\x03` via
  `send_command`) to interrupt, then verify with a probe.
- **Waiting for user input** (confirm/password prompt): Read the prompt text
  and ask the user before responding.
- **Unresponsive**: If Ctrl+C doesn't work, try Ctrl+Z (`\x1a`) to suspend,
  then `kill %1` to terminate the background job.

### When to Run Stuck Detection

- After `send_command`, if no notification arrives within 2x the `stable_seconds`.
- When a user reports "nothing is happening" or "it's stuck".
- During `start_smart_task` iterations when BEFORE and AFTER snapshots are
  identical for 3+ consecutive checks.

### Rules

- Never send destructive probes (no `rm`, `kill -9`, `DROP TABLE`).
- Always `read_terminal` BEFORE sending anything — the current state
  may contain important information (error messages, prompts).
- If the terminal shows a password prompt, do NOT probe — ask the user.

---

## Memory

Use `save_memory` to persist important facts across restarts:
- **rule**: directives like "always run tests before deploying"
- **knowledge**: facts like "production server is at 10.0.1.5"
- **preference**: style like "prefers yarn over npm"

Save memories when the user says "remember", "always", "never", "from now on",
or when you learn important environment facts. Use `list_memories` to recall
what you know. Use `delete_memory` to remove outdated entries.
