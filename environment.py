# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional read-only telemetry; no credentials or conversation text."""
from datetime import datetime
import json
import mmap
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import threading
import time

import psutil


def command(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=4, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def transfer_rate(before, after, elapsed):
    if before is None or after is None or elapsed <= 0:
        return None
    if after.bytes_recv < before.bytes_recv or after.bytes_sent < before.bytes_sent:
        return None
    return {'down': (after.bytes_recv - before.bytes_recv) / elapsed,
            'up': (after.bytes_sent - before.bytes_sent) / elapsed}


def parse_sync_log(lines):
    day = None
    result = {'status': 'SEM REGISTRO', 'last_ok': None}
    for line in lines:
        started = re.match(r'Unison .* log started at (\d{4}-\d{2}-\d{2})', line)
        if started:
            day = started[1]
        complete = re.match(r'Synchronization (complete|incomplete) at (\d{2}:\d{2}:\d{2})'
                            r'.*?\((\d+) items transferred, (\d+) skipped, (\d+) failed\)', line)
        if complete:
            skipped, failed = int(complete[4]), int(complete[5])
            status = 'ERRO' if failed else 'PARCIAL' if skipped or complete[1] != 'complete' else 'OK'
            result['status'] = status
            if status == 'OK' and day:
                result['last_ok'] = datetime.fromisoformat(day + 'T' + complete[2]).astimezone().isoformat()
    return result


def read_sync_log(path, limit=None):
    # Logs can be large. Search summaries backwards without copying
    # the file into RAM or decoding filenames. Stop at the most recent OK.
    result = {'status': 'SEM REGISTRO', 'last_ok': None}
    with Path(path).open('rb') as stream:
        size = stream.seek(0, 2)
        if not size:
            return result
        floor = max(0, size - limit) if limit is not None else 0
        with mmap.mmap(stream.fileno(), size, access=mmap.ACCESS_READ) as data:
            end = size
            for _ in range(2000):
                pos = data.rfind(b'\nSynchronization ', floor, end)
                if pos < 0:
                    break
                finish = data.find(b'\n', pos + 1)
                finish = size if finish < 0 else finish
                line = data[pos + 1:finish].decode('utf-8', errors='replace')
                marker = data.rfind(b' log started at ', floor, pos)
                header = ''
                if marker >= 0:
                    beginning = data.rfind(b'\n', floor, marker)
                    if beginning >= 0 or floor == 0:
                        beginning = max(0, beginning + 1)
                        header_end = data.find(b'\n', beginning)
                        header = data[beginning:header_end].decode('utf-8', errors='replace')
                info = parse_sync_log([header, line])
                if result['status'] == 'SEM REGISTRO':
                    result['status'] = info['status']
                if info['last_ok']:
                    result['last_ok'] = info['last_ok']
                    break
                end = pos
    return result


def sync_state(properties, mounted, log_status):
    if properties.get('ActiveState') in {'active', 'activating'}:
        return 'RODANDO'
    if not mounted:
        return 'ADIADO'
    if properties.get('Result') not in {None, 'success'}:
        return 'ERRO'
    if properties.get('ExecMainStatus') not in {None, '0'}:
        return 'ERRO'
    return log_status if properties else 'SEM DADOS'


def read_agents(path):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        return None
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1) as db:
            db.execute('PRAGMA query_only=ON')
            # Read only state/counter columns. Titles, prompts and messages are excluded.
            active = db.execute('SELECT s.status, s.active_turn_id, t.pending_approval_count, '
                                't.pending_user_input_count, t.has_actionable_proposed_plan '
                                'FROM projection_threads t LEFT JOIN projection_thread_sessions s '
                                'ON s.thread_id=t.thread_id '
                                'WHERE t.deleted_at IS NULL AND t.archived_at IS NULL').fetchall()
            waiting = sum(bool(approvals or inputs or plan) for _, _, approvals, inputs, plan in active)
            running = sum(status in {'running', 'starting'} and bool(turn) and
                          not (approvals or inputs or plan)
                          for status, turn, approvals, inputs, plan in active)
            completed = db.execute('SELECT COUNT(*) FROM projection_turns v '
                                   'JOIN projection_threads t ON t.thread_id=v.thread_id '
                                   'WHERE t.deleted_at IS NULL AND v.state="completed" '
                                   'AND julianday(v.completed_at)>=julianday(?)', (today,)).fetchone()[0]
            return {'running': running, 'waiting': waiting, 'completed_today': completed}
    except (OSError, sqlite3.Error):
        return None


class Environment:
    def __init__(self, stop, config):
        self.stop = stop
        self.config = config
        self.lock = threading.Lock()
        self.state = {'nas_online': None, 'nas_ms': None, 'mounted': None,
                      'mount_count': len(config['nas_mounts']), 'nas_enabled': bool(config['nas_host']),
                      'sync': 'SEM DADOS' if config['sync_service'] else 'DESATIVADO',
                      'last_sync': None, 'vpn': None, 'tailscale': None,
                      'vpn_enabled': bool(config['vpn_status_command']),
                      'tailscale_enabled': config['tailscale_enabled'],
                      'disk': None, 'network': None, 'interface': config['network_interface'],
                      'agents': None}
        self.last_net = None
        self.last_net_time = None
        self.log_stamp = None
        self.log_info = {'status': 'SEM REGISTRO', 'last_ok': None}

    def refresh(self):
        nas_online = nas_ms = None
        if self.config['nas_host']:
            started = time.monotonic()
            try:
                with socket.create_connection((self.config['nas_host'], self.config['nas_port']), timeout=2):
                    nas_online, nas_ms = True, round((time.monotonic() - started) * 1000)
            except OSError:
                nas_online, nas_ms = False, None
        mounted = {p.mountpoint for p in psutil.disk_partitions(all=True)
                   if p.fstype in {'cifs', 'nfs', 'nfs4'}}
        properties = None
        if self.config['sync_service']:
            properties = command(['systemctl', '--user', 'show', self.config['sync_service'],
                                  '-p', 'ActiveState', '-p', 'Result', '-p', 'ExecMainStatus'])
        properties = dict(line.split('=', 1) for line in (properties or '').splitlines() if '=' in line)
        try:
            if self.config['sync_service'] and self.config['sync_log']:
                log = Path(self.config['sync_log'])
                stat = log.stat()
                stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if stamp != self.log_stamp:
                    self.log_info = read_sync_log(log)
                    self.log_stamp = stamp
        except OSError:
            self.log_info = {'status': 'SEM REGISTRO', 'last_ok': None}
        vpn = tailscale = None
        if self.config['vpn_status_command']:
            try:
                argv = [str(Path(part).expanduser()) if part.startswith('~/') else part
                        for part in self.config['vpn_status_command']]
                value = json.loads(command(argv) or '')['class']
                vpn = {'active': True, 'inactive': False}.get(value)
            except (ValueError, KeyError, TypeError):
                pass
        if self.config['tailscale_enabled']:
            try:
                data = json.loads(command(['tailscale', 'status', '--json']) or '')
                if isinstance(data, dict) and 'BackendState' in data:
                    tailscale = data['BackendState'] == 'Running' and bool(data.get('Self', {}).get('Online'))
            except (ValueError, TypeError):
                pass
        with self.lock:
            self.state.update(nas_online=nas_online, nas_ms=nas_ms,
                              mounted=(sum(path in mounted for path in self.config['nas_mounts'])
                                       if self.config['nas_mounts'] else None),
                              sync=(sync_state(properties, self.config['sync_mount'] in mounted,
                                               self.log_info['status'])
                                    if self.config['sync_service'] else 'DESATIVADO'),
                              last_sync=self.log_info['last_ok'], vpn=vpn, tailscale=tailscale)

    def sample(self):
        now = time.monotonic()
        interface = self.config['network_interface']
        counters = psutil.net_io_counters(pernic=True).get(interface)
        elapsed = now - self.last_net_time if self.last_net_time is not None else 0
        rates = transfer_rate(self.last_net, counters, elapsed)
        self.last_net, self.last_net_time = counters, now
        try:
            disk = psutil.disk_usage(self.config['disk_path'])
            disk = {'free': disk.free / 2**30, 'percent': disk.percent}
        except OSError:
            disk = None
        with self.lock:
            self.state.update(network=rates, disk=disk,
                              agents=read_agents(self.config['agents_db']) if self.config['agents_enabled'] else None)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def run(self):
        while not self.stop.is_set():
            try:
                self.refresh()
            except (OSError, ValueError, psutil.Error):
                with self.lock:
                    self.state.update(nas_online=None, nas_ms=None, sync='SEM DADOS',
                                      mounted=None, last_sync=None, vpn=None, tailscale=None)
            self.stop.wait(self.config['environment_interval'])
