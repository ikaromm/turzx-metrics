# SPDX-License-Identifier: GPL-3.0-or-later
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from configuration import load_config
from environment import Environment
from install_user_service import service_text, unit_quote
from monitor import LLM, llm_rows, main

PROJECT = Path(__file__).resolve().parent


class PortableConfigurationTests(unittest.TestCase):
    def test_example_disables_private_sources_and_does_not_query_them(self):
        config = load_config(PROJECT / 'config.example.toml')
        self.assertFalse(config['llm_enabled'])
        self.assertFalse(config['agents_enabled'])
        self.assertEqual(config['nas_host'], '')
        environment = Environment(threading.Event(), config)
        with patch('environment.command') as command, \
                patch('environment.socket.create_connection') as connection, \
                patch('environment.read_agents') as agents, \
                patch('environment.read_sync_log') as log:
            environment.refresh()
            environment.sample()
        command.assert_not_called()
        connection.assert_not_called()
        agents.assert_not_called()
        log.assert_not_called()
        self.assertIsNone(environment.snapshot()['nas_online'])
        self.assertEqual(environment.snapshot()['sync'], 'DESATIVADO')

    def test_config_rejects_shell_strings_and_sync_without_mount(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.toml'
            for content in ['vpn_status_command = "echo active"',
                            'llm_enabled = "false"', 'sync_service = "my-sync.service"',
                            'llm_enabled = true\nllm_command = []', 'nas_port = 0', 'interval = inf']:
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_config(path)

    def test_disabled_llm_never_executes_reader(self):
        llm = LLM(threading.Event(), 60, enabled=False)
        with patch('monitor.subprocess.run') as run:
            llm.refresh()
        run.assert_not_called()
        self.assertEqual(llm.snapshot(), [])

    def test_bad_percentages_are_unknown_instead_of_crashing_renderer(self):
        report = {'entries': [{'id': 'demo', 'status': 'ready',
                              'metrics': [{'percent': value} for value in ['bad', None, -1, 101, float('nan')]]}]}
        self.assertEqual(llm_rows(report), [])

    def test_demo_does_not_open_usb_read_hardware_or_create_history(self):
        with tempfile.TemporaryDirectory() as folder:
            for page in ['hardware', 'llm', 'environment']:
                path = Path(folder) / (page + '.png')
                argv = ['monitor.py', '--config', str(PROJECT / 'config.example.toml'),
                        '--demo', '--page', page, '--preview', str(path)]
                with patch('monitor.sys.argv', argv), redirect_stdout(io.StringIO()), \
                        patch('monitor.Display', side_effect=AssertionError('USB queried')), \
                        patch('monitor.read_hardware', side_effect=AssertionError('Hardware queried')), \
                        patch('monitor.History', side_effect=AssertionError('History created')), \
                        patch('monitor.Environment', side_effect=AssertionError('Environment queried')), \
                        patch('monitor.LLM', side_effect=AssertionError('LLM queried')):
                    main()
                with Image.open(path) as image:
                    self.assertEqual(image.size, (480, 320))

    def test_service_handles_spaces_and_specifiers_rejects_invalid_paths(self):
        body = service_text(Path('/tmp/demo user/100% monitor'))
        self.assertIn('100%% monitor', body)
        self.assertIn('NoNewPrivileges=yes', body)
        self.assertIn('WorkingDirectory=/tmp/demo user/100%% monitor', body)
        for invalid in ['/tmp/path\nInjectedDirective=value', '/tmp/"quoted"', '/tmp/back\\slash']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                unit_quote(invalid)


if __name__ == '__main__':
    unittest.main()
