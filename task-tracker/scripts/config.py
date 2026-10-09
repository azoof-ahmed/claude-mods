"""Configuration for the task tracker.

Resolution order, highest first:
  1. command-line flags typed by a person (--db, --roles, --prefix, --labels, --importers, --notify-cmd)
  2. the project file <project root>/.claude/claude-mods.json, section "task-tracker"
     (keys: idPrefix, dbPath, rolesFile, labels, importers, notifyCommand)
  3. environment variables (TASKS_DB, TASKS_ROLES, TASKS_PREFIX, TASKS_LABELS, TASKS_IMPORTERS, TASKS_NOTIFY_CMD);
     the Claude Code plugin sets these from its options at session start
  4. the defaults below

The project file beats the environment: a project's own settings win over one user's plugin options.

The project root is the nearest ancestor of the working directory holding .git or .claude, else the working
directory. Relative paths (dbPath, rolesFile, an importer's .py path) resolve against the project root.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULTS = {
    'idPrefix': 'TASK',
    'dbPath': '.claude/tasks/tasks.db',
    'rolesFile': '.claude/tasks/roles.toml',
    'labels': 'bug,feature,chore,docs,test,blocked-external',
    'importers': '',
    'notifyCommand': '',
}

ENV = {
    'idPrefix': 'TASKS_PREFIX',
    'dbPath': 'TASKS_DB',
    'rolesFile': 'TASKS_ROLES',
    'labels': 'TASKS_LABELS',
    'importers': 'TASKS_IMPORTERS',
    'notifyCommand': 'TASKS_NOTIFY_CMD',
}

PROJECT_FILE = Path('.claude') / 'claude-mods.json'
SECTION = 'task-tracker'


def find_root(start: Optional[Path] = None) -> Path:
    start = Path(start or os.getcwd()).resolve()
    for p in [start, *start.parents]:
        if (p / '.git').exists() or (p / '.claude').is_dir():
            return p
    return start


def _split(s: str) -> List[str]:
    return [x.strip() for x in (s or '').split(',') if x.strip()]


@dataclass
class Config:
    root: Path
    db_path: Path
    roles_path: Path
    prefix: str
    labels: List[str] = field(default_factory=list)
    importers: List[str] = field(default_factory=list)
    notify_command: str = ''
    sources: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {'root': str(self.root), 'db': str(self.db_path), 'db_exists': self.db_path.exists(),
                'roles': str(self.roles_path), 'roles_exists': self.roles_path.exists(), 'prefix': self.prefix,
                'labels': self.labels, 'importers': self.importers, 'notify_command': self.notify_command,
                'sources': self.sources}


def load(flags: Optional[dict] = None, env: Optional[dict] = None, cwd: Optional[Path] = None) -> Config:
    """flags: {option name: value or None}, keyed like DEFAULTS."""
    flags = flags or {}
    env = os.environ if env is None else env
    root = find_root(cwd)
    project = {}
    pf = root / PROJECT_FILE
    if pf.exists():
        try:
            data = json.loads(pf.read_text(encoding='utf-8'))
            project = data.get(SECTION, {}) if isinstance(data, dict) else {}
        except (OSError, ValueError) as e:
            raise SystemExit(f'error: {pf} is not valid JSON: {e}')
    values, sources = {}, {}
    for key, default in DEFAULTS.items():
        if flags.get(key) is not None:
            values[key], sources[key] = flags[key], 'flag'
        elif project.get(key) is not None:
            v = project[key]
            values[key] = ','.join(v) if isinstance(v, list) else str(v)
            sources[key] = 'project'
        elif env.get(ENV[key]) is not None:
            values[key], sources[key] = env[ENV[key]], 'env'
        else:
            values[key], sources[key] = default, 'default'

    def path(v: str) -> Path:
        p = Path(os.path.expanduser(v))
        return p if p.is_absolute() else root / p

    prefix = values['idPrefix'].strip().rstrip('-') or DEFAULTS['idPrefix']
    return Config(root=root, db_path=path(values['dbPath']), roles_path=path(values['rolesFile']), prefix=prefix,
                  labels=_split(values['labels']), importers=_split(values['importers']),
                  notify_command=values['notifyCommand'].strip(), sources=sources)
