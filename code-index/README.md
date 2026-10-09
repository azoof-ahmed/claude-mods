# code-index

A Claude Code plugin that helps Claude read a codebase without reading whole files. It builds a compact index of
every class, function and method (with its line and the comment above it) and of every markdown heading (with its
line range), so Claude can `rg` the index, then read only the lines it needs.

It ships:

- `scripts/build_code_index.py`: the index builder. Regex based, so it needs no compiler or language server:
  Python, TypeScript/JavaScript, C/C++, C#, Go, Rust, Java, Kotlin and Markdown. Incremental: unchanged files come
  from a cache. Markdown output by default, JSON when the index path ends in `.json`.
- `scripts/md_toc.py`: prints any markdown file's headings with `[start-end]` line ranges.
- `scripts/status_tags.py`: lists, summarises and strips `STATUS(<task>)` comments, one-line notes that say
  where a task stands at that spot in the code.
- A skill (`code-index`) that teaches the discipline: index, then rg, then ranged reads.
- A small hooks module: adds two lines to the system prompt naming the index and the commands, and registers
  `/index` to rebuild it.

Python 3.9+ with the standard library only.

## Install

```
/plugin install code-index --marketplace azoof-ahmed/claude-mods
```

Then run `/index` once in each project to build the index.

## Options

Set them in `/config`, or in settings under `pluginConfigs["code-index"].options`.

| Option | Default | What it does |
| --- | --- | --- |
| `indexPath` | `.claude/index/code_index.md` | Where the index is written, relative to the project root. `.json` writes JSON. |
| `include` | `**/*.{py,pyi,ts,tsx,mts,cts,js,jsx,mjs,cjs,c,h,cc,cpp,cxx,hpp,hh,hxx,inl,cs,go,rs,java,kt,kts,md}` | Comma-separated globs of the files to index. |
| `exclude` | `node_modules`, `.git`, `dist`, `build`, `out`, `target`, `bin`, `obj`, `vendor`, `third_party`, virtualenvs, `__pycache__`, `.next`, `coverage`, `.claude`, `*.min.js` | Comma-separated globs to skip. In a git repository `.gitignore` is honoured too. |
| `statusTag` | `STATUS` | The tag name of task comments. |
| `python` | `python` | The Python executable (`python3`, `py`, or a full path). |

The options reach the scripts as environment variables (`CODE_INDEX_PATH`, `CODE_INDEX_INCLUDE`,
`CODE_INDEX_EXCLUDE`, `CODE_INDEX_STATUS_TAG`, `CODE_INDEX_PYTHON`, plus the script paths `CODE_INDEX_CLI`,
`CODE_INDEX_TOC`, `CODE_INDEX_TAGS`), so a project's own settings beat them:

```json
{ "code-index": { "include": "src/**/*.ts,docs/**/*.md", "indexPath": "docs/index/code.md" } }
```

in `<project>/.claude/claude-mods.json`. Order, highest first: a flag typed on the command line (`--index-path`,
`--include`, `--exclude`, `--status-tag`) > `.claude/claude-mods.json` > environment > default. The project root
is the nearest folder holding `.git` or `.claude`.

## Usage

```bash
/index                                  # rebuild (incremental); /index --full reparses everything
rg -n "parseConfig" .claude/index/code_index.md
python <plugin>/scripts/md_toc.py docs/DESIGN.md --grep auth
python <plugin>/scripts/status_tags.py list --task auth-42
```

A tag looks like `// STATUS(auth-42): PARTIAL -- refresh done, revoke left [ana]` (`DONE`, `PARTIAL`, `OPEN`,
`FROZEN`), on its own line in the file's comment style. Strip a task's tags when it closes:
`status_tags.py strip --task auth-42`.

## Develop

```
claude plugin validate .
claude plugin test .
python -m unittest discover -s scripts/tests
claude --plugin-dir .
```

## License

MIT
