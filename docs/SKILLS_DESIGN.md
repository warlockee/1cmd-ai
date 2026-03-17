# Skills Design

## Principles

- Skills mode should not preload a large library of default domain skills.
- The default install should ship only one bootstrap creator skill: `new-skill`.
- A skill is an abstraction of **capability boundaries** (`resource`, `script`, `approval`, policy), not just a hardcoded SOP.
- New skills should be added intentionally through the registry, then exposed with `/reload`.

## Core Skill Elements

- **Skill metadata (`SKILL.json`)**: name, mode, constraints, parameters, limits.
- **Resource**: reusable context read by the model (docs, examples, memory snippets, local files, dynamic context providers).
- **Script / Tool action**: concrete executable actions (existing `tools/dispatch`, optional scripts).
- **Approval**: risk gate for sensitive actions, reusing existing confirmation/guardrail mechanisms.
- **README.md**: human-facing purpose, boundaries, examples, and risk notes.

## Skill Types

### 1) Domain Skill (task-oriented)

- Goal: solve a concrete domain task with bounded autonomy.
- Uses resource and/or script, with LLM planning inside a constrained loop.
- Recommended defaults:
  - `max_rounds <= 3`
  - `failure_policy = stop_and_report`
- Behavior: if it cannot finish safely within bounds, stop and return a clear conclusion and next steps (no hard forcing).

### 2) Capability Skill (ability package)

- Goal: describe and expose what the skill can do, with examples and optional tools.
- README/description explains scope and usage; resources provide background and examples.
- Scripts/tools are optional selectable actions; model chooses based on user intent.
- Step/round limits still come from `SKILL.json` policy fields.

## File Separation

- `SKILL.json` is machine-facing policy and metadata.
- `README.md` is human-facing guidance (what/when/why/how to use).
- Keep JSON stable and enforceable; keep prose and rationale in README.

## Discovery And Reload

- Skills live under `.onecmd/skills`.
- `.onecmd/skills/skills.json` is the registry of enabled skills and slash-command settings.
- `/reload` re-reads the registry and rebuilds Telegram slash commands from the current skill set.
- If no registry exists, discovery can still fall back to scanning the directory, but isolated mode should prefer an explicit registry.

## Execution Model

- Avoid treating every skill as a fixed SOP.
- Skills can run in bounded LLM-guided loops constrained by policy (`max_rounds`, `max_steps`, failure policy).
- `resource` provides context; `script/tool` provides action surface.
- Recommended policy fields in `SKILL.json`:
  - `mode: domain | capability`
  - `max_rounds` (e.g., 3)
  - `max_steps` (optional hard cap)
  - `failure_policy: stop_and_report | fallback`

## Approval Model

- Approval reuses existing 1cmd safety mechanisms rather than introducing a second engine.
- Current enforcement paths:
  - **Tool-level confirmation flags** (for example, `restart_service` requires `confirmed=true`).
  - **Dispatch guardrails** (dangerous `send_command` payloads are blocked and require explicit confirmation before retry).
- Policy:
  - Low-risk read/documentation steps can run directly.
  - State-changing, privileged, or destructive actions must pass confirmation/guard checks.
- Skill authors should mark risky actions in `README.md` and keep approval expectations explicit in skill metadata/arguments.

## Governance

- Keep enabled skills narrow, reviewable, and explicit in `skills.json`.
- Prefer bootstrap plus opt-in additions over silent default loading.
- Respect existing tool guardrails; skills must not bypass them.
- Make assumptions, required inputs, failure limits, and escalation paths explicit for auditability.
- Design target: "bounded autonomy, explicit risk control, clear user-facing outcomes."
