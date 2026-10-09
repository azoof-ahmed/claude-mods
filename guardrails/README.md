# guardrails

A Claude Code plugin that holds Claude's edits to your project's own lint rules. You write the rules; the
plugin supplies the runner, a baseline ratchet and a gate in front of every edit.

- **Rules** live in your project, in `.claude/guardrails.toml` (or `.json`): regex rules with an id, a message,
  path globs and a severity, and command rules that run your own script or linter over the files.
- **Baseline ratchet.** `guard.py baseline` records today's violations. From then on only NEW ones fail, so you
  can adopt a rule on a codebase that breaks it today and burn the old ones down at your own pace. Violations are
  matched by rule, file and the line's text, not the line number, so moving code around is not "new".
- **The gate.** Before every Edit, Write, MultiEdit or NotebookEdit, the plugin computes the file as it would be
  after the edit, in memory, and refuses the edit when it adds a violation that is not in the baseline. Claude
  gets the list and the instruction to fix the code, not the rules.

Python 3.9+ with the standard library only (TOML rules need 3.11+; JSON works everywhere).

## Install

```
/plugin install guardrails --marketplace azoof-ahmed/claude-mods
```

Then, in your project:

```bash
cp <plugin>/examples/guardrails.toml .claude/guardrails.toml   # edit the rules
python <plugin>/scripts/guard.py check --all                   # see what they catch today
python <plugin>/scripts/guard.py baseline                      # record it; only new violations fail from now on
```

Commit the rules file and the baseline so everyone (and every agent) is held to the same line.

## Options

Set them in `/config`, or in settings under `pluginConfigs["guardrails"].options`.

| Option | Default | What it does |
| --- | --- | --- |
| `mode` | `block` | `block` refuses an edit that adds a violation; `warn` lets it through with a toast. |
| `paths` | `**` | Comma-separated globs of the files the gate covers. Each rule has its own `paths` too. |
| `rulesFile` | (empty) | The rules file. Empty: `.claude/guardrails.toml`, else `.claude/guardrails.json`. |
| `baselineFile` | `.claude/guardrails.baseline.json` | Where the baseline is kept. |
| `python` | `python` | The Python executable (`python3`, `py`, or a full path). |

The options reach the runner as environment variables (`GUARDRAILS_PATHS`, `GUARDRAILS_RULES`,
`GUARDRAILS_BASELINE`, `GUARDRAILS_PYTHON`, plus `GUARDRAILS_CLI`, the runner's path), so a project's own file
beats them. Order, highest first: a flag typed on the command line > the project's `.claude/claude-mods.json`
section `"guardrails"` (`rulesFile`, `baselineFile`, `paths`) > environment > defaults. The project root is the
nearest folder holding `.git` or `.claude`.

**When the runner itself breaks** (Python missing, a rules file that does not parse, a command rule that
crashes) the gate fails open: the edit goes through, a toast says it was not checked, and the reason is logged.
A broken rules file should not lock every edit; run `guard.py check` to see the error.

## Usage

```toml
# .claude/guardrails.toml
[[rules]]
id = "no-console-log"
pattern = 'console\.(log|debug)\('
message = "console.log in src. Use the project's logger."
paths = ["src/**/*.{ts,tsx}"]
exclude = ["**/*.test.ts"]

[[rules]]
id = "flake8"
type = "command"                       # prints path:line: rule: message
command = ["python", "-m", "flake8", "--format=%(path)s:%(row)d: %(code)s: %(text)s"]
paths = ["**/*.py"]
```

```bash
python <plugin>/scripts/guard.py check            # whole project, exit 1 on new errors (use it in CI too)
python <plugin>/scripts/guard.py check src/api    # some files or folders
python <plugin>/scripts/guard.py check --all      # everything, baseline ignored
python <plugin>/scripts/guard.py baseline         # re-record after fixing violations
python <plugin>/scripts/guard.py rules            # what is loaded
```

`examples/guardrails.toml` and `examples/guardrails.json` hold generic starting rules: no TODO/FIXME in `src`,
no `console.log` in `src`, no hardcoded secrets, and a warning for leftover debugger statements.

A line may carry `guardrails-ok: <reason>` to exempt it from regex rules; a bare `guardrails-ok` does not count.
The bundled skill tells Claude to use it only with the owner's agreement.

## Develop

```
claude plugin validate .
claude plugin test .
python -m unittest discover -s scripts/tests
claude --plugin-dir .
```

## License

MIT
