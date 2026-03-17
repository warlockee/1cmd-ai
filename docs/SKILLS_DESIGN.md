# Skills Design

## Principles

- Skills mode should not preload a large library of default domain skills.
- The default install should ship only one bootstrap creator skill: `new-skill`.
- New skills should be added intentionally through the registry, then exposed with `/reload`.

## File Separation

- `SKILL.json` is machine-facing metadata and the deterministic workflow executed by `run_skill`.
- `README.md` is human-facing guidance: scope, decision rules, templates, and extension notes.
- Keep the JSON short and stable. Put explanation and author guidance in the README.

## Discovery And Reload

- Skills live under `.onecmd/skills`.
- `.onecmd/skills/skills.json` is the registry of enabled skills and slash-command settings.
- `/reload` re-reads the registry and rebuilds Telegram slash commands from the current skill set.
- If no registry exists, discovery can still fall back to scanning the directory, but isolated mode should prefer an explicit registry.

## Execution Model

- A skill is a deterministic short SOP: ordered `steps`, each with a `tool` and `args`.
- Runtime behavior is intentionally simple: variable substitution, bounded step count, no conditional branching.
- Skills may call existing manager tools through `run_skill`; they should rely on stable tool actions and minimal prompt interpretation.
- Doc-first skills are the default. Add resources or scripts only when the workflow needs reusable context or exact side effects.

## Governance

- Keep enabled skills narrow, reviewable, and explicit in `skills.json`.
- Prefer bootstrap plus opt-in additions over silent default loading.
- Respect existing tool guardrails. Any action that already requires approval or confirmation keeps those constraints inside a skill.
- Document assumptions, required inputs, and extension points in the skill README so maintenance stays auditable.

## Approval Model

- Approval in skills reuses the existing 1cmd safety model rather than introducing a new approval engine.
- There are two current enforcement paths:
  - **Tool-level confirmation flags** (for example, `restart_service` requires `confirmed=true`).
  - **Dispatch guardrails** (for example, dangerous `send_command` patterns are blocked and require explicit user confirmation before retry).
- Practical policy:
  - **Low-risk read/documentation steps** run directly.
  - **State-changing or destructive steps** must pass existing confirmation/guard checks.
- Skill authors should mark risky steps clearly in `README.md` and keep confirmation points explicit in step arguments.
- Future extension (compatible with current model): add per-step metadata like `approval: none|required` and map it to chat-level `/approve` or button flows without changing core tool guardrails.
