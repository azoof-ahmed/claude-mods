---
name: guardrails
description: Work with the project's guardrails lint gate. Use when an edit was refused by guardrails, when writing or changing lint rules in .claude/guardrails.toml or .json, when taking or refreshing the baseline of existing violations, or when running the rules over the project.
---

# guardrails: the project's rules, enforced on every edit

The project writes its rules in `.claude/guardrails.toml` (or `.json`). Existing violations are recorded in a
baseline (`.claude/guardrails.baseline.json`); only NEW ones fail. Every Edit, Write, MultiEdit and
NotebookEdit is checked before it lands: the runner computes the file as it would be after the edit, in
memory, and refuses the edit when it adds a violation that is not in the baseline.

## When an edit is refused

**Fix the code, not the rules.** Rewrite the change so it no longer violates the rule the message names.

- Do not edit the rules file, the baseline or the plugin's settings to get past the gate, and do not reword the
  code only to dodge the pattern while keeping what the rule forbids. Rule changes and re-baselining need the
  project owner's (or the coordinator's) OK; ask, saying which rule and why.
- If you believe a rule is wrong for this line, say so to the user and stop; do not work around it.
- A line may carry `guardrails-ok: <reason>` as a recorded, reviewed exception, only when the owner agreed to
  it. A bare `guardrails-ok` without a reason does not count.

## Commands

The runner is `scripts/guard.py` in the plugin (`$GUARDRAILS_CLI` holds its path in a session with the plugin
loaded; `$GUARDRAILS_PYTHON` the Python to run it with).

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" check                 # whole project: exit 1 on NEW errors
python "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" check src/a.ts src/b  # some files or folders
python "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" check --all           # every violation, baseline ignored
python "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" baseline              # record today's errors (owner's OK first)
python "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" rules                 # list the loaded rules
```

Exit codes: 0 clean, 1 new errors, 2 the rules file or the runner is broken.

## Writing rules

```toml
[scan]                               # what a full `check` walks (node_modules, .git, build dirs always skipped)
include = ["**"]
exclude = ["**/fixtures/**"]

[[rules]]
id = "no-console-log"                # unique; part of every baseline key
pattern = 'console\.(log|debug)\('   # Python regex, matched against each line
message = "Use the project's logger." # say what to do instead
paths = ["src/**/*.{ts,tsx}"]        # globs from the project root: **, *, ?, {a,b}; default every file
exclude = ["**/*.test.ts"]
severity = "error"                   # "warn" prints but never blocks
ignore_case = false

[[rules]]
id = "project-checks"
type = "command"                     # a project script, given file paths as arguments
command = ["python", "tools/check.py"]
paths = ["**/*.py"]
timeout = 120
```

- A command rule prints one violation per line as `path:line: rule: message` (most linters can) and exits 0 or 1;
  any other exit with no violations is a broken runner. During an edit check it receives a temporary copy of the
  edited file; `GUARDRAILS_ORIGINAL_PATH` names the real one and `GUARDRAILS_ROOT` the project root.
- TOML needs Python 3.11+; on older Pythons write the same structure as `.claude/guardrails.json`
  (`{"scan": {...}, "rules": [...]}`).
- A violation is keyed by rule, file and the line's text with whitespace collapsed, never the line number:
  moving a baselined line is fine, adding another copy is new.
- After adding a rule, run `check --all` to see what it catches, then `baseline` (with the owner's OK) so only new
  violations fail. Re-baseline after FIXING violations, never to admit new ones.

## Configuration

Order, highest first: a flag you type (`--rules`, `--baseline`, `--paths`) > the project's
`.claude/claude-mods.json` section `"guardrails"` (`rulesFile`, `baselineFile`, `paths`) > environment
(`GUARDRAILS_RULES`, `GUARDRAILS_BASELINE`, `GUARDRAILS_PATHS`, set from the plugin's options) > defaults.
