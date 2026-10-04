# SPDX-License-Identifier: GPL-3.0-or-later
"""Fictional preview data. Does not read telemetry, accounts or databases."""
from datetime import datetime, timedelta


def demo_frame(renderer, page):
    now = datetime.now().astimezone()
    if page == 'hardware':
        return renderer.render({
            'cpu': 23, 'cpu_temp': 51, 'ram': 48, 'ram_used': 15.4, 'ram_total': 32,
            'gpu': {'name': 'RTX 4060', 'usage': 42, 'temp': 58, 'used': 3482,
                    'total': 8192, 'power': 49, 'power_limit': 115},
        })
    if page == 'llm':
        rows = [{'provider': 'DEMO', 'series': 'demo|weekly', 'label': 'COTA SEMANAL',
                 'percent': 34, 'reset_at': (now + timedelta(days=4, hours=2)).isoformat(),
                 'window_secs': 604800, 'stale': False}]
        values = [None, 6, 12, 18, 25, 30, 34]
        days = [(now.date() - timedelta(days=6-i), value) for i, value in enumerate(values)]
        return renderer.render_llm(rows, days,
                                   agents={'running': 2, 'waiting': 1, 'completed_today': 12})
    return renderer.render_environment({
        'nas_online': True, 'nas_ms': 3, 'mounted': 2, 'mount_count': 2, 'sync': 'OK',
        'last_sync': (now - timedelta(minutes=5)).isoformat(), 'vpn': False, 'tailscale': True,
        'interface': 'ethernet', 'network': {'down': 3.2 * 2**20, 'up': 512 * 1024},
        'disk': {'free': 180, 'percent': 64},
    })
