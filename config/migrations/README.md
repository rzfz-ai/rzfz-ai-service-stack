# migrations/

This directory contains the **version migration manifest** for the razzfazz.ai stack.

## env-changes.json

Declarative manifest describing `.env` and `.env.dify` changes between stack versions.
Used by `razzfazz-upgrade.sh` to automatically migrate environment files during upgrades.

Version format: `YYYY.MM-rcN` (release candidate), `YYYY.MM-ga` (general availability), `YYYY.MM-ga.N` (patch).

### Structure

```json
{
  "versions": [
    {
      "version": "2026.03-rc1",
      "date": "2026-04-01",
      "env_changes": [
        {"action": "add",    "file": ".env",      "key": "NEW_VAR",   "default": "value",  "comment": "Description"},
        {"action": "remove", "file": ".env",      "key": "OLD_VAR"},
        {"action": "rename", "file": ".env",      "old_key": "FOO",   "new_key": "BAR"},
        {"action": "change_default", "file": ".env.dify", "key": "SOME_KEY", "old_default": "old", "new_default": "new"}
      ],
      "requires_build": true,
      "requires_pull": true,
      "breaking_changes": ["Description of breaking change"],
      "notes": "Release notes"
    }
  ]
}
```

### Actions

| Action | Fields | Behavior |
|--------|--------|----------|
| `add` | `file`, `key`, `default`, `comment` | Adds key with default value if not present |
| `remove` | `file`, `key` | Removes key (warns if user-modified) |
| `rename` | `file`, `old_key`, `new_key` | Copies value from old to new key, removes old |
| `change_default` | `file`, `key`, `old_default`, `new_default` | Changes value only if it matches old_default |

### Adding a Migration

When releasing a new version:

1. Add a new entry to `env-changes.json` with the target version
2. Document all `.env` / `.env.dify` variable changes
3. Set `requires_build` / `requires_pull` flags
4. Document any breaking changes
5. Run `./scripts/prepare-release.sh` to validate
