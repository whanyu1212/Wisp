import unittest
from project.runtime.result_state import status_for

class StateTests(unittest.TestCase):
    def test_ready_values(self):
        self.assertEqual(status_for("done"), "ready")
        self.assertEqual(status_for("ok"), "ready")
        self.assertEqual(status_for("failed"), "blocked")
