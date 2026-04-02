# new-skill

Bootstrap a new skill with the bounded runtime schema.

Use `doc` when the skill mostly selects context and gives the model a fixed sequence of tool calls. This is the default and should cover most skills.

Use `ops` when the skill needs repeatable operational actions that are clearer as scripts or tightly scoped terminal commands. Keep the skill small and let the script handle the mechanics.

Use `full` only when the skill needs both reusable reference material and executable helpers. Start from `doc`, then add the extra pieces intentionally.

## Authoring rules

Emit the runtime policy fields in every generated `SKILL.json`:

- `mode`: `domain` or `capability` (`domain` is the default)
- `max_rounds`: default `3` for domain skills unless the author explicitly needs a different bound
- `max_steps`: optional hard cap for tool executions
- `failure_policy`: default `stop_and_report`
- `resources`: optional structured list of read-only context entries
- `scripts`: optional structured list of executable helper definitions

Require explicit inputs. Every variable used in `SKILL.json` should map to a named user-provided input or a fixed literal. Do not rely on implied values, hidden defaults, or open-ended placeholders.

Keep steps deterministic and bounded when you include them. Use a short linear sequence with explicit `tool` and `args`, no branching, no loops, no "keep trying until it works", and no instructions that depend on subjective judgment at execution time. Legacy `steps[]` skills still run directly, so generated steps must stay simple.

Use existing manager tools only. `SKILL.json` should remain a compact SOP, not a place to embed freeform shell playbooks or long procedural prose.

## Resources vs scripts

Add a `resource` when the model needs stable reference context that would otherwise be repeated across prompts: checklists, command recipes, policy notes, templates, or mappings. Use structured entries such as `{"name": "...", "type": "file|inline", "path": "...", "content": "...", "description": "..."}`.

Add a `resource` for information to read. Do not use a resource for side effects, generated output, or executable logic.

Add a `script` when the workflow has side effects or repeatable logic that should be executed exactly the same way every time: file generation, validation, formatting, deployment, or data collection. Use structured entries such as `{"name": "...", "path": "...", "tool": "...", "description": "..."}`.

Add a `script` when plain skill steps would otherwise require freeform command composition, repeated manual edits, or fragile multi-command execution. Scripts should have clear inputs and a bounded result.

If the workflow only needs reusable context, use a resource. If it must perform exact repeatable work, use a script. If neither is true, keep the skill doc-first.

## Approval and risky actions

Low-risk read-only or documentation steps can run directly.

State-changing, privileged, or destructive actions must rely on the existing approval model. Use guarded tools and explicit confirmation arguments where those tools require them. Do not invent a parallel approval system inside the skill.

Document risky steps in `README.md` and make the approval requirement explicit in the generated `SKILL.json` arguments or surrounding instructions. If a safe guarded tool exists, prefer it over a generic terminal command.

## Dangerous command patterns

Do not generate direct dangerous command payloads in skill steps. In particular, avoid raw `send_command` or equivalent steps that embed destructive patterns such as `rm -rf`, `rm -f`, `git push --force`, `git reset --hard`, `shutdown`, `reboot`, `kill -9`, `killall`, `systemctl stop`, `DROP TABLE`, or `DELETE FROM`.

If an action is risky enough to resemble one of those patterns, route it through an existing guarded tool or require an explicit user confirmation step outside the generated skill before anyone retries manually.

## Template guidance

Use `mode=domain` for a concrete bounded task and `mode=capability` when the skill is an ability package that lets the model choose tools within policy limits. If a domain skill cannot finish within `max_rounds` or `max_steps`, it must stop and report the current state and next safe step to the user.
