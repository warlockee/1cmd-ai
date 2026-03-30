# Creating a New Skill

A skill is a directory under `.onecmd/skills/` with the following structure:

```
my-skill/
  SKILL.json       # Required: metadata and policy
  README.md        # Required: human-readable description
  resources/       # Optional: read-only context files (.md)
    guide.md
    patterns.md
```

## SKILL.json Fields

| Field              | Type    | Required | Description                              |
|--------------------|---------|----------|------------------------------------------|
| `name`             | string  | yes      | Unique skill identifier (kebab-case)     |
| `version`          | string  | yes      | Semver version                           |
| `description`      | string  | yes      | One-line description                     |
| `mode`             | string  | yes      | `"domain"` (task guidance) or `"capability"` (ability) |
| `always_loaded`    | boolean | no       | If true, resources injected into every prompt. If false (default), listed as available and loaded on demand via `read_skill` tool. |
| `max_context_chars`| integer | no       | Per-skill context cap (default: 8000)    |

## Steps

1. Copy this directory as a starting point:
   ```
   cp -r .onecmd/skills/new-skill .onecmd/skills/my-skill
   ```

2. Edit `SKILL.json` with your skill's metadata.

3. Add resource files under `resources/` with the context your skill provides.

4. Enable the skill in `.onecmd/skills/skills.json`:
   ```json
   {"version": 1, "enabled": ["core-ops", "my-skill"]}
   ```
