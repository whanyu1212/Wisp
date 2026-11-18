import unittest
from pathlib import Path
from worker import attempts

class WorkerTests(unittest.TestCase):
    def test_attempts(self):
        self.assertEqual(attempts(), 3)
        self.assertIn("return (3)", Path("worker.py").read_text())
        self.assertEqual(Path("formatted.txt").read_text(), "formatted\n")
