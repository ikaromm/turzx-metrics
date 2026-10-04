# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from PIL import Image
from monitor import packet, rgb565, llm_rows, Renderer, Display, History, select_view


class ProtocolTests(unittest.TestCase):
    def test_full_landscape_bounds(self):
        self.assertEqual(packet(197, 0, 0, 479, 319), bytes([0, 0, 7, 125, 63, 197]))

    def test_rgb_primary_colors_little_endian(self):
        image = Image.new('RGB', (3, 1))
        image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        self.assertEqual(rgb565(image), bytes.fromhex('00f8e0071f00'))

    def test_provider_error_does_not_become_zero(self):
        self.assertEqual(llm_rows({'entries': [{'status': 'error', 'error': 'unavailable'}]}), [])

    def test_missing_freshness_is_cache(self):
        rows = llm_rows({'entries': [{'id': 'openai', 'status': 'ready',
                                     'metrics': [{'percent': 26}]}]})
        self.assertEqual(rows[0]['percent'], 26)
        self.assertTrue(rows[0]['stale'])

    def test_unavailable_gpu_and_llm_render(self):
        frame = Renderer().render({'cpu': 4, 'ram': 50, 'ram_used': 16, 'ram_total': 32,
                                   'cpu_temp': None, 'gpu': None})
        self.assertEqual(frame.size, (480, 320))

    def display_with_response(self, response):
        port = MagicMock()
        port.read.return_value = response
        port.write.side_effect = len
        device = SimpleNamespace(serial_number='USB35INCHIPSV2', vid=0x1a86,
                                 pid=0x5722, device='/dev/test')
        with patch('monitor.comports', return_value=[device]), patch('monitor.serial.Serial', return_value=port):
            return Display({'orientation': 'landscape', 'brightness': 55}), port

    def test_original_turing_without_hello_reply(self):
        display, port = self.display_with_response(b'')
        self.assertTrue(port.write.called)
        self.assertEqual(port.write.call_args_list[1].args[0][6:11], bytes.fromhex('6601e00140'))

    def test_different_size_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unconfirmed'):
            self.display_with_response(bytes([2]) * 6)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = History(Path(self.temp.name) / 'history.sqlite3')
        self.now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)

    def row(self, percent=26, stale=False, when=None):
        return {'series': 'openai|Codex weekly', 'percent': percent, 'stale': stale,
                'fetched_at': (when or self.now).isoformat(),
                'reset_at': (self.now + timedelta(days=5)).isoformat()}

    def test_gaps_remain_unknown_and_history_survives_reopen(self):
        self.history.record([self.row()])
        saved = History(self.history.path).days('openai|Codex weekly', self.now.date())
        self.assertEqual([value for _, value in saved], [None] * 6 + [26])

    def test_stale_reading_does_not_overwrite_real_observation(self):
        self.history.record([self.row()])
        self.history.record([self.row(0, stale=True, when=self.now + timedelta(minutes=1))])
        self.assertEqual(self.history.days('openai|Codex weekly', self.now.date())[-1][1], 26)

    def test_latest_observation_can_drop_after_quota_reset(self):
        self.history.record([self.row()])
        self.history.record([self.row(2, when=self.now + timedelta(minutes=1))])
        self.history.record([self.row(40, when=self.now - timedelta(minutes=1))])
        self.assertEqual(self.history.days('openai|Codex weekly', self.now.date())[-1][1], 2)

    def test_source_date_prevents_cache_becoming_new_day(self):
        yesterday = self.now - timedelta(days=1)
        self.history.record([self.row(0, when=yesterday)])
        days = self.history.days('openai|Codex weekly', self.now.date())
        self.assertEqual(days[-2][1], 0)
        self.assertIsNone(days[-1][1])

    def test_page_rotation_and_unavailable_provider(self):
        self.assertEqual(select_view(11.9, 12, 1), ('llm', 0))
        self.assertEqual(select_view(12, 12, 1), ('hardware', 0))
        self.assertEqual(select_view(24, 12, 1), ('environment', 0))
        self.assertEqual(select_view(36, 12, 2), ('llm', 1))
        self.assertEqual(select_view(72, 12, 2), ('llm', 0))
        self.assertEqual(select_view(0, 12, 0), ('llm', 0))

    def test_history_and_unavailable_llm_render(self):
        rows = [dict(self.row(), provider='Codex', label='Codex weekly')]
        renderer = Renderer()
        self.assertEqual(renderer.render_llm(rows, self.history.days(rows[0]['series'])).size,
                         (480, 320))
        self.assertEqual(renderer.render_llm([], []).size, (480, 320))


if __name__ == '__main__':
    unittest.main()
