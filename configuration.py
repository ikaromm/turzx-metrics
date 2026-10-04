# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable defaults; machine-specific integrations are opt-in."""
from copy import deepcopy
from pathlib import Path
import math
import tomllib

CONFIG = Path.home() / '.config/turzx-metrics/config.toml'
DEFAULTS = {
    'brightness': 55, 'orientation': 'landscape', 'interval': 2,
    'llm_interval': 60, 'page_interval': 12, 'environment_interval': 30,
    'llm_enabled': False, 'llm_command': ['ai-usagebar', 'usage', '--json'],
    'nas_host': '', 'nas_port': 445, 'nas_mounts': [],
    'sync_service': '', 'sync_mount': '', 'sync_log': '',
    'vpn_status_command': [], 'tailscale_enabled': False,
    'agents_enabled': False, 'agents_db': '~/.t3/userdata/state.sqlite',
    'network_interface': '', 'disk_path': '~', 'regular_font': '', 'bold_font': '',
}


def load_config(path=CONFIG):
    path = Path(path).expanduser()
    config = deepcopy(DEFAULTS)
    if path.exists():
        values = tomllib.loads(path.read_text())
        unknown = values.keys() - DEFAULTS.keys()
        if unknown:
            raise ValueError('Unknown configuration keys: ' + ', '.join(sorted(unknown)))
        config.update(values)
    for key, default in DEFAULTS.items():
        value = config[key]
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else:
            valid = isinstance(value, type(default))
        if not valid:
            raise ValueError(f'Invalid type for {key}')
    for key in ['llm_command', 'vpn_status_command', 'nas_mounts']:
        if any(not isinstance(item, str) or not item.strip() for item in config[key]):
            raise ValueError(f'{key} must contain nonempty strings')
    if config['llm_enabled'] and not config['llm_command']:
        raise ValueError('llm_enabled requires llm_command')
    if config['orientation'] not in {'landscape', 'reverse_landscape'}:
        raise ValueError('orientation must be landscape or reverse_landscape')
    if not 0 <= config['brightness'] <= 100:
        raise ValueError('brightness must be 0..100')
    for key, minimum in [('interval', 1), ('llm_interval', 60), ('page_interval', 5),
                         ('environment_interval', 15)]:
        if not config[key] >= minimum:
            raise ValueError(f'{key} must be >= {minimum}')
    if not isinstance(config['nas_port'], int) or not 1 <= config['nas_port'] <= 65535:
        raise ValueError('nas_port must be an integer in 1..65535')
    if config['sync_service'] and not config['sync_mount']:
        raise ValueError('sync_service requires sync_mount')
    for key in ['sync_mount', 'sync_log', 'agents_db', 'disk_path', 'regular_font', 'bold_font']:
        if config[key]:
            config[key] = str(Path(config[key]).expanduser().resolve())
    config['nas_mounts'] = [str(Path(item).expanduser().resolve()) for item in config['nas_mounts']]
    return config
