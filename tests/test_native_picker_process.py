"""Real pipe pressure and child cancellation; no interaction with user files."""
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from stylelora.runtime.picker import NativePicker


class PickerProcessTests(unittest.TestCase):
    def test_stderr_pipe_pressure_does_not_block_picker_result(self):
        original = subprocess.Popen
        child = []
        def spawn(args, **options):
            process = original([sys.executable, '-c',
                "import sys;sys.stderr.write('x'*200000);sys.stderr.flush();print('{\"path\":\"fixture\"}')"], **options)
            child.append(process)
            return process
        cancel = threading.Event(); timer = threading.Timer(5, cancel.set); timer.start()
        try:
            with patch('stylelora.runtime.picker.subprocess.Popen', side_effect=spawn):
                self.assertEqual(NativePicker(None).choose('project_open', cancel), 'fixture')
        finally:
            timer.cancel()
        self.assertIsNotNone(child[0].poll())

    def test_cancel_terminates_and_reaps_owned_child(self):
        original = subprocess.Popen
        child = []
        def spawn(args, **options):
            process = original([sys.executable, '-c', 'import time;time.sleep(30)'], **options)
            child.append(process)
            return process
        cancel = threading.Event(); timer = threading.Timer(.2, cancel.set); timer.start()
        try:
            with patch('stylelora.runtime.picker.subprocess.Popen', side_effect=spawn):
                self.assertIsNone(NativePicker(None).choose('import_root', cancel))
        finally:
            timer.cancel()
        self.assertIsNotNone(child[0].poll())
