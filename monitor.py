# SPDX-License-Identifier: GPL-3.0-or-later
"""Local TURZX telemetry. Rev A protocol documented by turing-smart-screen-python."""
import argparse
from array import array
from datetime import datetime, timezone, timedelta
import fcntl
import json
import logging
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont
import psutil
import serial
from serial.tools.list_ports import comports
from environment import Environment
from configuration import CONFIG, load_config

HISTORY = Path.home() / '.local/state/turzx-metrics/history.sqlite3'
CYAN = '#55efff'
DIM = '#51929a'
BG = '#020a0d'
TRACK = '#123037'


def packet(command, x=0, y=0, end_x=0, end_y=0):
    # Four 10-bit coordinates packed in network order, then the command byte.
    coords = (x << 30) | (y << 20) | (end_x << 10) | end_y
    return coords.to_bytes(5, 'big') + bytes([command])


def rgb565(image):
    pixels = array('H', (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                         for r, g, b in image.convert('RGB').get_flattened_data()))
    if sys.byteorder != 'little':
        pixels.byteswap()
    return pixels.tobytes()


class Display:
    def __init__(self, config):
        candidates = [p for p in comports() if p.serial_number == 'USB35INCHIPSV2'
                      and p.vid == 0x1a86 and p.pid == 0x5722]
        if len(candidates) != 1:
            raise RuntimeError(f'Expected one USB35INCHIPSV2 display; found {len(candidates)}')
        self.port = serial.Serial(candidates[0].device, 115200, timeout=2,
                                  write_timeout=8, rtscts=True, exclusive=True)
        self.previous = None
        try:
            self.port.reset_input_buffer()
            self.write(bytes([69]) * 6)
            response = self.port.read(6)
            # Original Turing 3.5-inch units do not reply to HELLO. We already
            # require the exact VID/PID and 3.5-inch serial before opening.
            if response not in (b'', bytes([1]) * 6):
                raise RuntimeError(f'Unconfirmed 3.5-inch protocol: HELLO={response.hex()}')
            logging.info('3.5-inch USB35INCHIPSV2 on %s; HELLO=%s',
                         candidates[0].device, response.hex() or 'no reply (original Turing)')
            orientation = {'landscape': 102, 'reverse_landscape': 103}[config['orientation']]
            self.write(packet(121) + bytes([orientation]) + (480).to_bytes(2, 'big')
                       + (320).to_bytes(2, 'big') + bytes(5))
            self.write(packet(109))
            self.write(packet(110, round(255 * (1 - config['brightness'] / 100))))
        except Exception:
            self.port.close()
            raise

    def write(self, data):
        if self.port.write(data) != len(data):
            raise serial.SerialException('Incomplete display write')

    def show(self, frame):
        sent = 0
        # Independent regions avoid transmitting unchanged parts of the display.
        for region in [(0, 0, 480, 85), (0, 85, 480, 177), (0, 177, 480, 320)]:
            new = frame.crop(region)
            delta = None if self.previous is None else ImageChops.difference(
                new, self.previous.crop(region)).getbbox()
            if self.previous is not None and delta is None:
                continue
            box = (0, 0, new.width, new.height) if delta is None else delta
            x, y = region[0] + box[0], region[1] + box[1]
            patch = new.crop(box)
            payload = rgb565(patch)
            self.write(packet(197, x, y, x + patch.width - 1, y + patch.height - 1))
            for offset in range(0, len(payload), 480 * 8):
                self.write(payload[offset:offset + 480 * 8])
            sent += len(payload)
        self.previous = frame.copy()
        return sent

    def close(self):
        try:
            self.write(packet(108))
        except (OSError, serial.SerialException):
            pass
        self.port.close()


def read_hardware():
    memory = psutil.virtual_memory()
    result = {'cpu': psutil.cpu_percent(), 'ram': memory.percent,
              'ram_used': memory.used / 2**30, 'ram_total': memory.total / 2**30,
              'cpu_temp': None, 'gpu': None}
    temperatures = psutil.sensors_temperatures().get('coretemp', [])
    package = [t.current for t in temperatures if t.label.startswith('Package')]
    if package:
        result['cpu_temp'] = max(package)
    try:
        r = subprocess.run(['nvidia-smi', '--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw,power.limit',
                            '--format=csv,noheader,nounits'], capture_output=True, text=True,
                           timeout=3, check=True)
        name, usage, temp, used, total, power, limit = [s.strip() for s in r.stdout.splitlines()[0].split(',')]
        result['gpu'] = {'name': name.replace('NVIDIA GeForce ', ''), 'usage': float(usage),
                         'temp': float(temp), 'used': float(used), 'total': float(total)}
        for key, value in [('power', power), ('power_limit', limit)]:
            try:
                result['gpu'][key] = float(value)
            except ValueError:
                result['gpu'][key] = None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return result


def llm_rows(report):
    rows = []
    for entry in report.get('entries', []):
        if entry.get('error') or entry.get('status') != 'ready':
            continue
        fetched = entry.get('fetched_at')
        try:
            old = not fetched or (datetime.now(timezone.utc) - datetime.fromisoformat(
                fetched.replace('Z', '+00:00'))).total_seconds() > 600
        except (ValueError, TypeError):
            old = True
        for metric in entry.get('metrics', []):
            if metric.get('headline', 'percent') != 'percent':
                continue
            try:
                percent = float(metric['percent'])
                if not 0 <= percent <= 100:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            rows.append({'provider': entry.get('display_name', entry['id']),
                         'series': entry['id'] + '|' + metric.get('label', 'Cota'),
                         'label': metric.get('label', 'Cota'),
                         'percent': percent, 'reset_at': metric.get('reset_at'),
                         'window_secs': metric.get('window_secs'), 'fetched_at': fetched,
                         'stale': bool(entry.get('stale') or old)})
    return rows


class History:
    """One last observed quota per local calendar day; gaps stay unknown."""
    def __init__(self, path=HISTORY):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS quota ('
                       'series TEXT, day TEXT, percent REAL, reset_at TEXT, '
                       'observed_at TEXT, PRIMARY KEY (series, day))')
        self.path.chmod(0o600)

    def record(self, rows):
        with sqlite3.connect(self.path) as db:
            for row in rows:
                if row['stale'] or not row.get('fetched_at'):
                    continue
                try:
                    observed = datetime.fromisoformat(row['fetched_at'].replace('Z', '+00:00'))
                    percent = float(row['percent'])
                    if observed.tzinfo is None or not 0 <= percent <= 100:
                        continue
                except (ValueError, TypeError):
                    continue
                stamp = observed.astimezone(timezone.utc).isoformat()
                db.execute('INSERT INTO quota VALUES (?, ?, ?, ?, ?) '
                           'ON CONFLICT(series, day) DO UPDATE SET '
                           'percent=excluded.percent, reset_at=excluded.reset_at, '
                           'observed_at=excluded.observed_at '
                           'WHERE excluded.observed_at > quota.observed_at',
                           (row['series'], observed.astimezone().date().isoformat(),
                            percent, row['reset_at'], stamp))
            cutoff = (datetime.now().astimezone().date() - timedelta(days=30)).isoformat()
            db.execute('DELETE FROM quota WHERE day < ?', (cutoff,))

    def days(self, series, today=None):
        today = today or datetime.now().astimezone().date()
        dates = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        with sqlite3.connect(self.path) as db:
            values = dict(db.execute('SELECT day, percent FROM quota WHERE series=? '
                                     'AND day>=? AND day<=?',
                                     (series, dates[0].isoformat(), today.isoformat())))
        return [(day, values.get(day.isoformat())) for day in dates]


class LLM:
    def __init__(self, stop, interval, history=None, enabled=True, command=None):
        self.stop = stop
        self.interval = interval
        self.lock = threading.Lock()
        self.rows = []
        self.failed = False
        self.history = history
        self.enabled = enabled
        self.command = command if command is not None else ['ai-usagebar', 'usage', '--json']

    def refresh(self):
        if not self.enabled:
            return
        try:
            r = subprocess.run(self.command, capture_output=True,
                               text=True, timeout=35, check=True)
            rows = llm_rows(json.loads(r.stdout))
            with self.lock:
                self.rows = rows
                self.failed = not bool(rows)
            if self.history:
                try:
                    self.history.record(rows)
                except (OSError, sqlite3.Error):
                    logging.warning('Unable to record quota history')
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            with self.lock:
                self.failed = True

    def run(self):
        while not self.stop.is_set():
            self.refresh()
            self.stop.wait(self.interval)

    def snapshot(self):
        with self.lock:
            return [dict(row, stale=row['stale'] or self.failed) for row in self.rows]


def reset_in(value):
    try:
        seconds = max(0, int((datetime.fromisoformat(value.replace('Z', '+00:00'))
                              - datetime.now(timezone.utc)).total_seconds()))
        days, remaining = divmod(seconds, 86400)
        hours, remaining = divmod(remaining, 3600)
        return f'{days}d {hours}h' if days else f'{hours}h {remaining // 60}m'
    except (AttributeError, ValueError, TypeError):
        return '--'


def elapsed_percent(row):
    """Only compare pace when the provider reports an exact window length."""
    try:
        duration = float(row['window_secs'])
        if duration <= 0:
            return None
        reset = datetime.fromisoformat(row['reset_at'].replace('Z', '+00:00'))
        remaining = (reset - datetime.now(timezone.utc)).total_seconds()
        if not 0 <= remaining <= duration:
            return None
        return 100 * (1 - remaining / duration)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def select_view(elapsed, interval, row_count):
    step = int(elapsed // interval)
    view = ('llm', 'hardware', 'environment')[step % 3]
    return view, (step // 3) % max(1, row_count) if view == 'llm' else 0


class Renderer:
    def __init__(self, config=None):
        config = config or {}

        def font(size, bold=False):
            custom = config.get('bold_font' if bold else 'regular_font')
            if custom:
                return ImageFont.truetype(custom, size)
            suffix = '-Bold' if bold else ''
            paths = [f'/usr/share/fonts/truetype/dejavu/DejaVuSansMono{suffix}.ttf',
                     f'/usr/share/fonts/TTF/DejaVuSansMono{suffix}.ttf',
                     f'/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono{suffix}.ttf',
                     '/usr/share/fonts/TTF/JetBrainsMonoNerdFont-' + ('Bold' if bold else 'Regular') + '.ttf']
            for path in paths:
                if Path(path).is_file():
                    return ImageFont.truetype(path, size)
            return ImageFont.load_default(size=size)

        self.fonts = {size: font(size) for size in [12, 13, 15, 17, 19]}
        self.bold = {size: font(size, True) for size in [20, 23, 30, 38]}

    def render(self, hw):
        image = Image.new('RGB', (480, 320), BG)
        draw = ImageDraw.Draw(image)

        def text(x, y, value, size=17, color=CYAN, bold=False):
            draw.text((x, y), str(value), font=(self.bold if bold else self.fonts)[size], fill=color)

        def bar(x, y, width, percent):
            draw.rectangle((x, y, x + width, y + 4), fill=TRACK)
            if percent is not None and percent > 0:
                draw.rectangle((x, y, x + round(width * min(100, max(0, percent)) / 100), y + 4), fill=CYAN)

        text(14, 8, 'CPU', 17)
        text(14, 27, f"{hw['cpu']:.0f}%", 38, bold=True)
        text(140, 11, 'TEMP', 13, DIM)
        text(140, 33, '--' if hw['cpu_temp'] is None else f"{hw['cpu_temp']:.0f}°C", 23, bold=True)
        bar(14, 73, 208, hw['cpu'])
        draw.line((238, 10, 238, 79), fill=DIM)
        text(255, 8, 'RAM', 17)
        text(255, 27, f"{hw['ram']:.0f}%", 38, bold=True)
        text(375, 11, 'GiB', 13, DIM)
        text(375, 37, f"{hw['ram_used']:.0f}/{hw['ram_total']:.0f}", 17)
        bar(255, 73, 208, hw['ram'])
        draw.line((14, 85, 466, 85), fill=CYAN)

        gpu = hw['gpu']
        text(14, 91, 'GPU  ' + (gpu['name'] if gpu else 'INDISPONIVEL'), 20, bold=True)
        text(14, 119, 'USO', 13, DIM)
        text(168, 119, 'VRAM', 13, DIM)
        text(347, 119, 'TEMP', 13, DIM)
        text(14, 135, f"{gpu['usage']:.0f}%" if gpu else '--', 30, bold=True)
        text(168, 137, f"{gpu['used']/1024:.1f}/{gpu['total']/1024:.0f}G" if gpu else '--', 23, bold=True)
        text(347, 135, f"{gpu['temp']:.0f}°C" if gpu else '--', 30, bold=True)
        draw.line((14, 177, 466, 177), fill=CYAN)

        text(14, 194, 'POTENCIA GPU', 15, DIM)
        text(255, 194, 'VRAM LIVRE', 15, DIM)
        power = gpu.get('power') if gpu else None
        limit = gpu.get('power_limit') if gpu else None
        text(14, 219, '--' if power is None else f'{power:.0f} W', 38, bold=True)
        text(255, 219, '--' if not gpu else f"{(gpu['total']-gpu['used'])/1024:.1f} G", 38, bold=True)
        bar(14, 274, 208, None if power is None or not limit else power / limit * 100)
        bar(255, 274, 208, (gpu['total']-gpu['used'])/gpu['total']*100 if gpu and gpu['total'] else None)
        text(14, 297, 'METRICAS / TEMPO REAL', 13, DIM)
        text(414, 297, datetime.now().strftime('%H:%M'), 13, DIM)
        return image

    def render_llm(self, rows, days, page=0, agents=None, enabled=True, agents_enabled=True):
        image = Image.new('RGB', (480, 320), BG)
        draw = ImageDraw.Draw(image)

        def text(x, y, value, size=17, color=CYAN, bold=False):
            draw.text((x, y), str(value), font=(self.bold if bold else self.fonts)[size], fill=color)

        row = rows[page % len(rows)] if rows else None
        text(14, 10, 'LLM / ' + (row['provider'].upper() if row else 'CODEX'), 20, bold=True)
        text(395, 15, 'COTA', 13, DIM)
        draw.line((14, 43, 466, 43), fill=DIM)
        agent_label = (f"T3: {agents['running']} ATIVOS  {agents['waiting']} ESPERA  "
                       f"{agents['completed_today']} OK HOJE" if agents else
                       'T3: ESTADO INDISPONIVEL' if agents_enabled else 'T3: DESATIVADO')
        text(14, 299, agent_label, 12, DIM)
        if row is None:
            text(14, 85, 'USO INDISPONIVEL' if enabled else 'COLETA DESATIVADA', 23, bold=True)
            text(14, 133, 'Fonte: JSON de uso' if enabled else 'Habilite no config.toml', 17, DIM)
            return image

        label = row['label'].replace('Codex weekly', 'COTA SEMANAL').replace('Codex 5-hour', 'COTA 5 HORAS')
        text(14, 53, label.upper()[:24], 15, DIM)
        text(14, 72, f"{row['percent']:g}%", 38, bold=True)
        text(135, 91, 'USADO', 15, DIM)
        text(290, 54, 'RENOVA EM', 13, DIM)
        text(290, 77, reset_in(row['reset_at']), 23, bold=True)
        text(14, 121, f"{max(0, 100 - row['percent']):g}% DISPONIVEL", 17)

        draw.rectangle((14, 151, 465, 156), fill=TRACK)
        if row['percent'] > 0:
            draw.rectangle((14, 151, 14 + round(451 * min(100, row['percent']) / 100), 156), fill=CYAN)
        pace = elapsed_percent(row)
        if pace is not None:
            marker = 14 + round(451 * pace / 100)
            draw.line((marker, 146, marker, 161), fill='#d4faff', width=2)
            text(14, 167, f'TEMPO DA JANELA: {pace:.0f}%', 13, DIM)

        text(14, 196, 'COTA OBSERVADA / ULTIMOS 7 DIAS', 13, DIM)
        weekdays = ['SEG', 'TER', 'QUA', 'QUI', 'SEX', 'SAB', 'DOM']
        for index, (day, percent) in enumerate(days):
            x = 14 + index * 65
            text(x + 12, 215, weekdays[day.weekday()], 13, DIM)
            color = TRACK if percent is None else tuple(
                round(low + (high - low) * (0.12 + 0.78 * percent / 100))
                for low, high in zip((18, 48, 55), (85, 239, 255)))
            draw.rounded_rectangle((x, 237, x + 58, 274), radius=4, fill=color,
                                   outline=DIM if index == 6 else None)
            text(x + 7, 246, '--' if percent is None else f'{percent:.0f}%',
                 15, DIM if percent is None else '#e7feff')
        text(14, 280, 'CACHE / ULTIMA COTA DO DIA' if row['stale'] else 'ULTIMA COTA OBSERVADA DE CADA DIA', 12, DIM)
        return image

    def render_environment(self, state):
        image = Image.new('RGB', (480, 320), BG)
        draw = ImageDraw.Draw(image)

        def text(x, y, value, size=17, color=CYAN, bold=False):
            draw.text((x, y), str(value), font=(self.bold if bold else self.fonts)[size], fill=color)

        def status(value, on='ONLINE', off='OFF'):
            return '--' if value is None else on if value else off

        def rate(value):
            if value is None:
                return '--'
            return f'{value / 2**20:.1f} M/s' if value >= 2**20 else f'{value / 1024:.0f} K/s'

        text(14, 8, 'AMBIENTE', 20, bold=True)
        text(414, 14, datetime.now().strftime('%H:%M'), 13, DIM)
        text(14, 42, 'NAS', 15, DIM)
        text(14, 59, status(state.get('nas_online')) if state.get('nas_enabled', True) else 'DESATIVADO', 23, bold=True)
        if state.get('nas_ms') is not None:
            text(130, 68, f"SMB {state['nas_ms']} ms", 13, DIM)
        text(280, 42, 'MONTADOS', 13, DIM)
        mounted = state.get('mounted')
        total = state.get('mount_count', 5)
        text(280, 59, '--' if mounted is None else f'{mounted}/{total}', 23, bold=True)
        text(14, 91, 'SYNC: ' + state.get('sync', 'SEM DADOS'), 17)
        stamp = state.get('last_sync')
        try:
            stamp = datetime.fromisoformat(stamp).strftime('%d/%m %H:%M')
        except (TypeError, ValueError):
            stamp = '--'
        text(14, 116, 'ULTIMO OK NO LOG: ' + stamp, 13, DIM)
        draw.line((14, 142, 466, 142), fill=CYAN)
        text(14, 151, 'VPN', 13, DIM)
        text(255, 151, 'TAILSCALE', 13, DIM)
        text(14, 171, status(state.get('vpn'), 'LIGADA', 'DESLIGADA') if state.get('vpn_enabled', True)
             else 'DESATIVADA', 20, bold=True)
        text(255, 171, status(state.get('tailscale')) if state.get('tailscale_enabled', True)
             else 'DESATIVADO', 20, bold=True)
        draw.line((14, 204, 466, 204), fill=DIM)
        text(14, 214, 'REDE / ' + (state.get('interface') or '--'), 13, DIM)
        net = state.get('network') or {}
        text(14, 233, 'RX ' + rate(net.get('down')), 20, bold=True)
        text(255, 233, 'TX ' + rate(net.get('up')), 20, bold=True)
        draw.line((14, 266, 466, 266), fill=DIM)
        disk = state.get('disk')
        text(14, 282, 'SSD', 15, DIM)
        text(65, 278, '--' if disk is None else f"{disk['free']:.0f} GiB LIVRES", 20, bold=True)
        text(365, 284, '--' if disk is None else f"{disk['percent']:.0f}% USO", 13, DIM)
        return image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', type=Path, help='Render actual metrics to PNG without opening USB')
    parser.add_argument('--config', type=Path, default=CONFIG, help='Path to local TOML configuration')
    parser.add_argument('--demo', action='store_true', help='Render fictional data without querying any telemetry')
    parser.add_argument('--once', action='store_true', help='Send one frame to the display')
    parser.add_argument('--page', choices=['hardware', 'llm', 'environment'], default='hardware',
                        help='Choose the page for --preview or --once')
    args = parser.parse_args()
    if args.demo and (not args.preview or args.once):
        parser.error('--demo requires --preview and cannot be combined with --once')
    if args.config != CONFIG and not args.config.expanduser().is_file():
        parser.error('Configuration file not found')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config(args.config)
    renderer = Renderer(config)
    if args.demo:
        from demo import demo_frame
        demo_frame(renderer, args.page).save(args.preview)
        print('Demo image saved; all metrics are fictional.')
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    psutil.cpu_percent()
    history = History() if config['llm_enabled'] else None
    llm = LLM(stop, config['llm_interval'], history, config['llm_enabled'], config['llm_command'])
    environment = Environment(stop, config)
    if args.preview or args.once:
        llm.refresh()
        environment.refresh()
        environment.sample()
        stop.wait(0.2)
        environment.sample()
        state = environment.snapshot()
        hw = read_hardware()
        rows = llm.snapshot()
        if args.page == 'llm':
            days = history.days(rows[0]['series']) if rows and history else []
            frame = renderer.render_llm(rows, days, agents=state['agents'],
                                        enabled=config['llm_enabled'], agents_enabled=config['agents_enabled'])
        elif args.page == 'environment':
            frame = renderer.render_environment(state)
        else:
            frame = renderer.render(hw)
        if args.preview:
            frame.save(args.preview)
            print(json.dumps({'hardware': hw, 'llm': llm.snapshot(), 'environment': state}, ensure_ascii=False))
            return
        display = Display(config)
        try:
            print('Frame transmitted:', display.show(frame), 'bytes')
        finally:
            display.port.close()  # Keep the one-shot preview visible.
        return
    lock_path = Path.home() / '.cache/turzx-metrics.lock'
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        display = Display(config)
        if config['llm_enabled']:
            threading.Thread(target=llm.run, daemon=True).start()
        threading.Thread(target=environment.run, daemon=True).start()
        started = time.monotonic()
        previous_view = None
        try:
            while not stop.is_set():
                start = time.monotonic()
                rows = llm.snapshot()
                environment.sample()
                state = environment.snapshot()
                view, page = select_view(start - started, config['page_interval'], len(rows))
                if view == 'hardware':
                    frame = renderer.render(read_hardware())
                elif view == 'llm':
                    days = history.days(rows[page]['series']) if rows and history else []
                    frame = renderer.render_llm(rows, days, page, state['agents'],
                                                config['llm_enabled'], config['agents_enabled'])
                else:
                    frame = renderer.render_environment(state)
                sent = display.show(frame)
                if (view, page) != previous_view:
                    logging.info('Display page: %s %d; transmitted %d pixel bytes', view, page, sent)
                    previous_view = (view, page)
                if display.previous is not None:
                    logging.debug('Updated %d pixel bytes', sent)
                stop.wait(max(0, config['interval'] - (time.monotonic() - start)))
        finally:
            display.close()


if __name__ == '__main__':
    main()
