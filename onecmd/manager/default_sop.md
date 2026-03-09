# OneCmd Agent SOP

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
read the content of each terminal to understand what's running. Then suggest descriptive
names to the user using rename_terminal — for example "dev-server", "db-console", "build-logs".

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
- When uncertain, read the terminal content first to identify the environment
- When naming terminals, include the agent type if applicable (e.g. "claude-dev",
  "gemini-deploy") to make the distinction obvious

---

## Stuck Terminal Detection

## Smart Diff — Detecting Stuck Sessions

A terminal is "stuck" when a command was sent but never executed, or a process
hangs without producing output. Use this procedure to detect and recover.

### Detection Steps

1. **Capture BEFORE snapshot** — read the terminal output.
2. **Send a probe command** — a harmless command that always produces output:
   - Shell prompt visible: send `echo __PROBE_OK__`
   - Interactive program (python, node REPL): send the equivalent print statement
   - Unknown state: send a single Enter keystroke
3. **Wait for stability** (5-10 seconds).
4. **Capture AFTER snapshot** — read the terminal output again.
5. **Compare**: if AFTER contains the probe output (e.g. `__PROBE_OK__`),
   the terminal is responsive. If AFTER is identical to BEFORE, the terminal
   is stuck.

### Recovery Actions

When a terminal is stuck:

- **Pending input not submitted**: The command text is visible but the prompt
  hasn't advanced. Send Enter to submit it.
- **Process hanging**: Output stopped mid-stream. Send Ctrl+C to interrupt,
  then verify with a probe.
- **Waiting for user input** (confirm/password prompt): Read the prompt text
  and follow the RISK CLASSIFICATION rules before responding.
- **Unresponsive**: If Ctrl+C doesn't work, try Ctrl+Z to suspend, then
  `kill %1` to terminate the background job.

### When to Run Stuck Detection

- After sending a command via send_command, if no "command completed"
  notification arrives within 2x the expected stable_seconds.
- When a user reports "nothing is happening" or "it's stuck".
- During smart_task iterations when BEFORE and AFTER snapshots are identical
  for 3+ consecutive checks.

### Rules

- Never send destructive probes (no `rm`, `kill -9`, `DROP TABLE`).
- Always read the terminal BEFORE sending anything — the current state
  may contain important information (error messages, prompts).
- If the terminal shows a password prompt, do NOT probe — ask the user.
- Log stuck detection events so they appear in .health reports.
