"""Which project a command works on, and which paths belong to it.

The same file ships with coordinator, agent-bus and task-tracker (each plugin installs alone); keep the copies equal.

- The project is the nearest folder with .git or .claude. The home folder's own .claude holds user settings, so it
  does not make the home folder a project.
- A git worktree without its own section in .claude/claude-mods.json uses its main checkout, where that file
  (usually untracked) and the project's state live.
- A worker started by a coordinator carries COORD_LANE=<project root>#<role>. That hand-over names its project and wins
  over the folder's own section and state, for a mod the project uses, in that project's repository (its worktrees
  included), in the repository its lane record names and in the project's `repos`. Nowhere else, and never for `init`.
- A path that comes from the environment (the plugins' options arrive there) must lie in the project or its main
  checkout. One that doesn't was inherited from another project's session (a terminal, multiplexer or daemon started
  there) or exported by hand: it is ignored with a note, and the setting falls back to the project's file or the
  default. Paths in the project's own file, in the user's per-workspace map and typed flags may point anywhere.
- Per-workspace options kept at user level: the user's ~/.claude/claude-mods.json (CLAUDE_CONFIG_DIR is honoured)
  {"workspaces": {"<abs path or glob>": {"<mod>": {...}}}}. An entry applies only inside its folder (or to folders the
  glob matches); a longer key beats a shorter one, and the project's own file beats them all.
- AGENT_BUS_ROLE that arrives with a COORD_LANE naming another project belongs to that project's lane: ignored here.

Precedence, first match wins: typed flags > the project's file > the user's per-workspace map > this session's plugin
options (environment) > defaults.
"""
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

FILE = Path('.claude') / 'claude-mods.json'
HANDOVER = 'COORD_LANE'


class Refused(SystemExit):
    """A setting that was not used; str() is the message. Uncaught, it prints and exits 1."""


def home():
    return Path(os.path.expanduser('~')).resolve()


def is_project(d):
    return (d / '.git').exists() or ((d / '.claude').is_dir() and d != home())


def find_root(start=None):
    here = Path(start or os.getcwd()).resolve()
    for d in [here, *here.parents]:
        if is_project(d):
            return d
    return here


def git_top(d):
    """The nearest folder with .git at or above `d`, or None."""
    return next((p for p in [d, *d.parents] if (p / '.git').exists()), None)


def common_dir(top):
    """What `git rev-parse --git-common-dir` gives in `top`: the folder every worktree of a repository shares."""
    dot = top / '.git'
    if dot.is_dir():
        return dot.resolve()
    try:
        text = dot.read_text(encoding='utf-8').strip()
        if not text.startswith('gitdir:'):
            return None
        gitdir = (top / text[len('gitdir:'):].strip()).resolve()
    except (OSError, ValueError):
        return None
    try:
        return (gitdir / (gitdir / 'commondir').read_text(encoding='utf-8').strip()).resolve()
    except (OSError, ValueError):  # a submodule: its own repository
        return gitdir


def main_checkout(top):
    """The main checkout of the linked worktree `top`, or None: `top` is not a linked worktree, or its repository is
    bare (wherever it lies, a `.git` folder beside the worktrees included)."""
    common = None if (top / '.git').is_dir() else common_dir(top)
    if common is None or common.name != '.git' or not common.is_dir():
        return None
    try:
        if re.search(r'^\s*bare\s*=\s*true\s*$', (common / 'config').read_text(encoding='utf-8'), re.M | re.I):
            return None
    except (OSError, ValueError):
        pass
    return common.parent


def same_place(a, b):
    """Whether two folders are in one repository; without git, whether `a` lies under `b`."""
    ta, tb = git_top(a), git_top(b)
    if ta and tb:
        ca = common_dir(ta)
        return ca is not None and ca == common_dir(tb)
    return tb is None and under(a, b)


def under(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    if path == folder or path.startswith(folder.rstrip(os.sep) + os.sep):
        return True
    for p in (Path(path), *Path(path).parents):  # the same folder spelled in another case
        try:
            if os.path.samefile(p, folder):
                return True
        except OSError:
            pass
    return False


class Project:
    """root: where relative paths resolve. file: its claude-mods.json. places: folders that count as the project."""

    def __init__(self, root, places=()):
        self.root = Path(root)
        self.file = self.root / FILE
        self.places = []
        linked = self.root / '.claude'  # a .claude kept elsewhere and linked in is the project's own
        extra = [linked.resolve()] if linked.is_symlink() and linked.resolve() != home() / '.claude' else []
        for p in (self.root, *places, *extra):
            wide = under(home(), p) and not (p / '.git').exists()  # the home folder, or one above it
            if p not in self.places and not wide:
                self.places.append(p)

    def holds(self, value):
        """Whether a path setting stays in the project (relative: from its root)."""
        p = Path(os.path.expanduser(str(value)))
        if not p.is_absolute() and under(self.root / p, self.root):
            return True
        return any(under(self.root / p, d) for d in self.places)

    def check_env(self, mod, section, key, var, value, default, flag=None):
        """Whether an environment path may be used: False (with a note) for one outside the project, which another
        project's session or a shell export left behind; True, with a warning, for one inside it."""
        if value == default:
            return True
        fix = f'set "{key}" in the "{section}" section of {self.file}'
        if not self.holds(value):
            print(f'{mod}: ignoring {key}={value} (the {key} plugin option, or {var} in the environment, maybe '
                  f'inherited from another project\'s session): it is outside this project ({self.root}). To use it '
                  f'here, {fix}'
                  + (f', or pass `{flag} {value}`' if flag else '')
                  + f'; and remove the {key} option from your user settings (pluginConfigs["{section}"].options).',
                  file=sys.stderr)
            return False
        print(f'{mod}: {key} comes from a plugin option or {var}; paths belong to one project, and the next version '
              f'stops reading them there: {fix}.', file=sys.stderr)
        return True

    def user_section(self, section, env=None):
        """This workspace's entries for `section` in the user's per-workspace map (see the module doc)."""
        return workspace_options(section, [self.root, *self.places], env)

    def refuse_outside(self, mod, files):
        """`init` writes only in the project."""
        out = [str(f) for f in files if not self.holds(f)]
        if out:
            raise Refused(f'{mod}: init writes nothing: these are outside this project ({self.root}):\n  '
                          + '\n  '.join(out) + f'\nRun init in the project they belong to, or change its paths in {self.file}.')


def old_options(section, keys, env=None):
    """Path options left in the user's settings (pluginConfigs["<mod>"] or ["<mod>@<marketplace>"]). The plugins no
    longer declare them, so Claude Code does not hand them over; read for one more version, checked like the
    environment."""
    folder = user_folder(env)
    out = {}
    try:
        configs = json.loads((folder / 'settings.json').read_text(encoding='utf-8'))['pluginConfigs']
        for name, conf in configs.items():
            if name.split('@')[0] == section:
                out.update({k: v for k, v in (conf.get('options') or {}).items() if k in keys and v and isinstance(v, str)})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return out


def user_folder(env=None):
    env = os.environ if env is None else env
    base = env.get('HOME') or env.get('USERPROFILE')
    folder = env.get('CLAUDE_CONFIG_DIR') or (base and os.path.join(base, '.claude'))
    return Path(folder) if folder else None


def workspace_matches(key, folders):
    """Whether a per-workspace key (an absolute path, `~` allowed, or a glob) covers one of `folders`."""
    key = os.path.expanduser(str(key))
    if not os.path.isabs(key):
        return False
    if any(c in key for c in '*?['):
        return any(fnmatch.fnmatchcase(os.path.realpath(f), key) or fnmatch.fnmatchcase(str(f), key) for f in folders)
    return any(under(f, key) for f in folders)


def workspace_options(section, folders, env=None):
    """The user's per-workspace options for `section` that cover `folders`; a longer key wins."""
    folder = user_folder(env)
    try:
        spaces = json.loads((folder / FILE.name).read_text(encoding='utf-8')).get('workspaces') or {}
        items = sorted(spaces.items(), key=lambda kv: len(str(kv[0])))
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    out = {}
    for key, entry in items:
        mine = entry.get(section) if isinstance(entry, dict) else None
        if isinstance(mine, dict) and workspace_matches(key, folders):
            out.update(mine)
    return out


def env_role(start=None, env=None):
    """AGENT_BUS_ROLE, unless it came with a COORD_LANE for another project (an inherited lane identity)."""
    env = os.environ if env is None else env
    role = env.get('AGENT_BUS_ROLE') or None
    if role and env.get(HANDOVER) and handed_over(find_root(start), env) is None:
        print(f'ignoring AGENT_BUS_ROLE={role}: it belongs to the lane {env.get(HANDOVER)}, another project\'s',
              file=sys.stderr)
        return None
    return role


def section_of(root, section):
    """The section of root's claude-mods.json, or None."""
    try:
        data = json.loads((root / FILE).read_text(encoding='utf-8')).get(section)
    except (OSError, ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def handed_over(here, env=None):
    """The project a coordinator named when it started this worker, when `here` is a place it works in; else None."""
    root, _, role = (os.environ if env is None else env).get(HANDOVER, '').rpartition('#')
    p = Path(root)
    if not root or not p.is_absolute() or not p.is_dir() or not is_project(p.resolve()):
        return None
    p = p.resolve()
    if same_place(here, p):
        return p
    coord = section_of(p, 'coordinator') or {}
    try:
        lanes = json.loads((p / str(coord.get('cardsDir') or '.claude/coord/lanes') / 'lanes.json')
                           .read_text(encoding='utf-8'))
        lane = lanes.get(role)
    except (OSError, ValueError, AttributeError):
        lane = None
    repos = [r.strip() for r in str(coord.get('repos') or '').split(',') if r.strip()]
    if isinstance(lane, dict) and lane.get('repo'):
        repos.append(str(lane['repo']))
    return p if any(same_place(here, (p / r).resolve()) for r in repos) else None


def resolve(section, state, start=None, env=None, init=False):
    """The Project for a command run in `start`. `state`: the mod's default path, which marks a project that uses the
    mod without a section."""
    def used(d):
        return section_of(d, section) is not None or (d / state).exists()

    def located(own):
        top = git_top(own)
        main = main_checkout(top) if top else None
        shared = main
        if main and own != top:
            inner = main / own.relative_to(top)
            shared = inner if inner.is_dir() and is_project(inner) else main
        root = own
        if section_of(own, section) is None and shared and (used(shared) or not used(own)):
            root = shared
        return Project(root, [p for p in (own, top, main) if p])

    own = find_root(start)
    handed = None if init else handed_over(own, env)
    if handed:  # found from there as the coordinator itself finds it
        theirs = located(handed)
        if used(theirs.root):
            return theirs
    return located(own)
