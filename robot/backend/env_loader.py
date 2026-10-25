"""Load KEY=VALUE pairs from the project .env into os.environ (existing variables win)."""
import os
import re
from pathlib import Path


def load_env(path=None):
    path = Path(path) if path else Path(__file__).resolve().parents[1] / '.env'
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', line)
        if not match or line.lstrip().startswith('#'):
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
