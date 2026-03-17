# new-skill

Bootstrap a new skill as a short, deterministic SOP.

Use `doc` when the skill mostly selects context and gives the model a fixed sequence of tool calls. This is the default and should cover most skills.

Use `ops` when the skill needs repeatable operational actions that are clearer as scripts or tightly scoped terminal commands. Keep the skill small and let the script handle the mechanics.

Use `full` only when the skill needs both reusable reference material and executable helpers. Start from `doc`, then add the extra pieces intentionally.

Add a `resource` when the model needs stable reference context that would otherwise be repeated across prompts: checklists, command recipes, policy notes, templates, or mappings.

Add a `script` when the workflow has side effects or repeatable logic that should be executed exactly the same way every time: file generation, validation, formatting, deployment, or data collection.

Current runtime has no step branching. Keep `SKILL.json` doc-first and deterministic, then note any `ops` or `full` extensions here so the next edit stays explicit.
