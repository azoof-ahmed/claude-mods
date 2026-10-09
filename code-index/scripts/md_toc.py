"""Print a markdown file's heading tree with line ranges, so you Read only the section you need.

  python md_toc.py <file.md> [--depth N] [--grep TEXT]

Each row: [start-end] heading. Then Read(file, offset=start, limit=end-start+1).
--grep keeps only headings containing TEXT (case-insensitive).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import utf8_stdout  # noqa: E402
from build_code_index import md_headings, with_ranges  # noqa: E402


def toc(text: str, depth: int = 6, grep: str | None = None) -> list[str]:
    n = len(text.splitlines())
    rows = []
    for lv, t, s, e in with_ranges(md_headings(text), n):
        if lv > depth or (grep and grep.lower() not in t.lower()):
            continue
        rows.append(f'{"  " * (lv - 1)}[{s}-{e}] {"#" * lv} {t}')
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('--depth', type=int, default=6)
    ap.add_argument('--grep')
    a = ap.parse_args(argv)
    utf8_stdout()
    try:
        text = Path(a.file).read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        print(f'md_toc: {e}', file=sys.stderr)
        return 2
    rows = toc(text, a.depth, a.grep)
    print(f'{a.file}: {len(text.splitlines())} lines, {len(md_headings(text))} headings')
    print('\n'.join(rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
