# SPDX-License-Identifier: GPL-3.0-or-later
"""Install a user service pointing to this checkout; no root or auto-start."""
import argparse
from pathlib import Path
import shutil


def unit_path(path):
    value = str(path)
    if any(ord(char) < 32 or ord(char) == 127 or char in '\\\"\'' for char in value):
        raise ValueError('Unsupported character in installation path')
    return value.replace('%', '%%')


def unit_quote(path):
    value = unit_path(path)
    return '"' + value + '"'


def service_text(project):
    project = Path(project).resolve()
    return f'''[Unit]
Description=TURZX Metrics - hardware, LLM and environment
After=graphical-session.target
PartOf=graphical-session.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={unit_quote(project / '.venv/bin/python')} {unit_quote(project / 'monitor.py')}
WorkingDirectory={unit_path(project)}
Restart=on-failure
RestartSec=5
TimeoutStopSec=12
NoNewPrivileges=yes
UMask=0077

[Install]
WantedBy=graphical-session.target
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Print service without writing files')
    parser.add_argument('--force', action='store_true', help='Replace an existing service (not configuration)')
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    try:
        body = service_text(project)
    except ValueError as error:
        parser.error(str(error))
    if args.dry_run:
        print(body, end='')
        return
    if not (project / '.venv/bin/python').is_file():
        parser.error('Create .venv and install requirements first')
    unit = Path.home() / '.config/systemd/user/turzx-metrics.service'
    if unit.exists() and not args.force:
        parser.error('Service exists; use --force to replace it')
    config = Path.home() / '.config/turzx-metrics/config.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        shutil.copyfile(project / 'config.example.toml', config)
        config.chmod(0o600)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(body)
    print('User service installed; existing configuration preserved.')
    print('Review ~/.config/turzx-metrics/config.toml, then run:')
    print('systemctl --user daemon-reload')
    print('systemctl --user enable --now turzx-metrics.service')


if __name__ == '__main__':
    main()
