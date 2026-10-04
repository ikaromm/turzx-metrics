# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from environment import parse_sync_log, read_sync_log, read_agents, sync_state, transfer_rate
from monitor import Renderer


class EnvironmentTests(unittest.TestCase):
    def test_nas_unmounted_is_deferred_even_after_zero_exit(self):
        self.assertEqual(sync_state({'ActiveState': 'inactive', 'Result': 'success',
                                     'ExecMainStatus': '0'}, False, 'OK'), 'ADIADO')
        self.assertEqual(sync_state({'ActiveState': 'activating'}, True, 'OK'), 'RODANDO')
        self.assertEqual(sync_state({'Result': 'exit-code'}, True, 'OK'), 'ERRO')

    def test_skipped_and_failed_items_are_not_success(self):
        rows = ['Unison 2.53 log started at 2026-10-03 at 10:00:00\n',
                'Synchronization complete at 10:00:01 (2 items transferred, 0 skipped, 0 failed)\n',
                'Unison 2.53 log started at 2026-10-04 at 10:00:00\n',
                'Synchronization complete at 10:00:01 (0 items transferred, 2 skipped, 0 failed)\n']
        result = parse_sync_log(rows)
        self.assertEqual(result['status'], 'PARCIAL')
        self.assertTrue(result['last_ok'].startswith('2026-10-03'))
        rows.append('Synchronization incomplete at 10:00:02 (0 items transferred, 0 skipped, 1 failed)\n')
        self.assertEqual(parse_sync_log(rows)['status'], 'ERRO')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sync.log'
            path.write_text(''.join(rows))
            self.assertEqual(read_sync_log(path), parse_sync_log(rows))
            path.write_text('')
            self.assertEqual(read_sync_log(path), {'status': 'SEM REGISTRO', 'last_ok': None})

    def test_bounded_log_tail_does_not_invent_missing_date(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sync.log'
            path.write_text('Unison 2.53 log started at 2026-10-03 at 10:00:00\n' + 'x\n' * 200 +
                            'Synchronization complete at 10:00:01 (2 items transferred, 0 skipped, 0 failed)\n')
            result = read_sync_log(path, limit=150)
            self.assertEqual(result['status'], 'OK')
            self.assertIsNone(result['last_ok'])

    def test_network_baseline_reset_and_rates(self):
        before = SimpleNamespace(bytes_recv=1000, bytes_sent=500)
        after = SimpleNamespace(bytes_recv=5000, bytes_sent=1500)
        self.assertIsNone(transfer_rate(None, after, 2))
        self.assertEqual(transfer_rate(before, after, 2), {'down': 2000, 'up': 500})
        self.assertIsNone(transfer_rate(after, before, 2))

    def test_only_active_sessions_and_completed_turns_today_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite'
            now = datetime.now().astimezone()
            with sqlite3.connect(path) as db:
                db.executescript('''
                    CREATE TABLE projection_threads(thread_id TEXT, deleted_at TEXT, archived_at TEXT,
                        pending_approval_count INTEGER, pending_user_input_count INTEGER,
                        has_actionable_proposed_plan INTEGER);
                    CREATE TABLE projection_thread_sessions(thread_id TEXT, status TEXT, active_turn_id TEXT);
                    CREATE TABLE projection_turns(thread_id TEXT, state TEXT, completed_at TEXT);
                ''')
                db.executemany('INSERT INTO projection_threads VALUES (?, ?, ?, ?, ?, ?)',
                               [('active', None, None, 0, 0, 0), ('waiting', None, None, 1, 0, 0),
                                ('ready', None, None, 0, 0, 0), ('archived', None, 'yes', 0, 0, 0),
                                ('deleted', 'yes', None, 0, 0, 0)])
                db.executemany('INSERT INTO projection_thread_sessions VALUES (?, ?, ?)',
                               [('active', 'running', 'turn'), ('waiting', 'running', 'turn'),
                                ('ready', 'ready', None), ('archived', 'running', 'turn'),
                                ('deleted', 'running', 'turn')])
                db.executemany('INSERT INTO projection_turns VALUES (?, ?, ?)',
                               [('active', 'completed', now.isoformat()),
                                ('active', 'error', now.isoformat()),
                                ('active', 'completed', (now - timedelta(days=1)).isoformat()),
                                ('deleted', 'completed', now.isoformat())])
            self.assertEqual(read_agents(path), {'running': 1, 'waiting': 1, 'completed_today': 1})
            self.assertIsNone(read_agents(Path(folder) / 'missing.sqlite'))
            self.assertFalse((Path(folder) / 'missing.sqlite').exists())

    def test_missing_environment_sources_render_unknown(self):
        self.assertEqual(Renderer().render_environment({}).size, (480, 320))


if __name__ == '__main__':
    unittest.main()
