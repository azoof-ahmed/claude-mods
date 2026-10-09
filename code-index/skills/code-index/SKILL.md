---
name: code-index
description: Read a codebase cheaply. Use before reading source or long markdown files in a project, when looking for where a symbol lives, or when leaving or reading task-status tags in code. Covers the code index, markdown section maps (md_toc), STATUS(<task>) tags and the ranged-read discipline.
---

# Reading code cheaply

Every whole-file read costs context. Find the lines first, then read only them.

## The order: index, rg, ranged Read

1. **The index.** `rg -n "<symbol or word>" .claude/index/code_index.md` (or the path the session context names).
   Each row is ``- L<line> <kind> `<name>` -- <summary>`` under a `### <file> (<n> lines)` heading. The table at
   the top lists every file with its line count and purpose. Markdown files list their H1/H2 headings as
   `[start-end]`.
2. **rg in the source** when the index does not have it: `rg -n "<pattern>" <dir>`; add `-g "*.py"` to narrow,
   `-l` for file names only, `-C 3` for a little context.
3. **Ranged Read.** Read the file with `offset` and `limit` around the line (40-120 lines is usually enough).
   Read a whole file only when it is under ~300 lines.
4. **Long markdown** (specs, plans, READMEs): map it first, then read one section:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/md_toc.py" docs/PLAN.md            # every heading as [start-end]
   python "${CLAUDE_PLUGIN_ROOT}/scripts/md_toc.py" docs/PLAN.md --grep api  # only matching headings
   ```

   Then Read with `offset=start`, `limit=end-start+1`.

## Keeping the index fresh

The index is regenerated, never edited by hand. Rebuild after edits that add, move or rename symbols:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/build_code_index.py"          # incremental (unchanged files are cached)
python "${CLAUDE_PLUGIN_ROOT}/scripts/build_code_index.py" --full   # reparse everything
```

or run `/index` (`/index --full`). Line numbers in the index drift as files change; when a row's line does not
hold the symbol, rg the source for it and rebuild.

## STATUS tags

A STATUS tag is one comment line that says where a task stands at that spot, so the next agent reads the tag
instead of the file:

```
// STATUS(<task-id>): DONE|PARTIAL|OPEN|FROZEN -- <one line> [owner]
# STATUS(auth-42): PARTIAL -- token refresh done, revoke still to do [ana]
```

- `DONE` finished there; `PARTIAL` started, the line says what is left; `OPEN` still to do; `FROZEN` do not touch.
- Its own line, in the file's comment style (`//`, `#`, `--`, `;`, `/* */`, `<!-- -->`). The tag name can be
  changed per project (`statusTag`).
- Tags are temporary: strip them when the task closes.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/status_tags.py" list                 # every tag as file:line
python "${CLAUDE_PLUGIN_ROOT}/scripts/status_tags.py" list --task auth-42 --state OPEN
python "${CLAUDE_PLUGIN_ROOT}/scripts/status_tags.py" summary              # counts per task and state
python "${CLAUDE_PLUGIN_ROOT}/scripts/status_tags.py" strip --task auth-42 --dry-run
```

The index lists the tags too, so `rg -n "STATUS\(auth-42\)" .claude/index/code_index.md` finds them.

## Configuration

With the plugin loaded, `$CODE_INDEX_CLI`, `$CODE_INDEX_TOC` and `$CODE_INDEX_TAGS` hold the script paths and
`$CODE_INDEX_PYTHON` the Python to run them with. Order, highest first: a flag you type (`--index-path`,
`--include`, `--exclude`, `--status-tag`) > the project's `.claude/claude-mods.json` > environment variables
(`CODE_INDEX_PATH`, `CODE_INDEX_INCLUDE`, `CODE_INDEX_EXCLUDE`, `CODE_INDEX_STATUS_TAG`, set from the plugin's
options) > defaults:

```json
{ "code-index": { "indexPath": "docs/index/code.md", "include": "src/**/*.ts,docs/**/*.md", "statusTag": "STATUS" } }
```
