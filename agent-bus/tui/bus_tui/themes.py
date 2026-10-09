"""Colour themes: tokyonight (default), catppuccin, claude (orange accent).

Each palette feeds both a Textual Theme (widget CSS) and the Rich styles of rendered rows and the statusline."""
from textual.theme import Theme

PALETTES = {
    'tokyonight': {
        'bg': '#1a1b26', 'bg2': '#16161e', 'surface': '#24283b', 'panel': '#1f2335', 'sel': '#2e3c64',
        'fg': '#c0caf5', 'muted': '#565f89', 'faint': '#414868',
        'accent': '#7aa2f7', 'accent2': '#bb9af7', 'cyan': '#7dcfff', 'green': '#9ece6a',
        'yellow': '#e0af68', 'orange': '#ff9e64', 'red': '#f7768e', 'on_accent': '#15161e',
    },
    'catppuccin': {
        'bg': '#1e1e2e', 'bg2': '#181825', 'surface': '#313244', 'panel': '#181825', 'sel': '#45475a',
        'fg': '#cdd6f4', 'muted': '#7f849c', 'faint': '#585b70',
        'accent': '#89b4fa', 'accent2': '#cba6f7', 'cyan': '#89dceb', 'green': '#a6e3a1',
        'yellow': '#f9e2af', 'orange': '#fab387', 'red': '#f38ba8', 'on_accent': '#11111b',
    },
    'claude': {
        'bg': '#1f1e1d', 'bg2': '#191817', 'surface': '#2b2a27', 'panel': '#262624', 'sel': '#4a3328',
        'fg': '#ece9e4', 'muted': '#8f8a82', 'faint': '#5c5853',
        'accent': '#D97757', 'accent2': '#E8A27C', 'cyan': '#9fc3c7', 'green': '#9cbf7a',
        'yellow': '#e5c07b', 'orange': '#C15F3C', 'red': '#e06c75', 'on_accent': '#1f1e1d',
    },
}
DEFAULT = 'tokyonight'
ALIASES = {'tokyo': 'tokyonight', 'tokyo-night': 'tokyonight', 'catppuccin-mocha': 'catppuccin', 'mocha': 'catppuccin',
           'anthropic': 'claude', 'orange': 'claude'}


def resolve(name):
    name = (name or DEFAULT).strip().lower()
    name = ALIASES.get(name, name)
    return name if name in PALETTES else None


def theme_name(name):
    return f'bus-{name}'


def textual_theme(name):
    p = PALETTES[name]
    return Theme(
        name=theme_name(name), primary=p['accent'], secondary=p['accent2'], accent=p['accent'],
        foreground=p['fg'], background=p['bg'], surface=p['surface'], panel=p['panel'],
        warning=p['yellow'], error=p['red'], success=p['green'], dark=True,
        variables={'block-cursor-background': p['sel'], 'block-cursor-foreground': p['fg'],
                   'footer-key-foreground': p['accent'], 'input-selection-background': p['sel']},
    )


def all_themes():
    return [textual_theme(n) for n in PALETTES]
