import unittest
from status import normalize

class StatusTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize(" Done "), "done")
        self.assertEqual(normalize(""), "")
