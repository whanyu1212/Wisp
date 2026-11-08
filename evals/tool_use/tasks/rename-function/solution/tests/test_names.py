import unittest

from accounts import display_name, get_user_name_parts
from accounts.greetings import greeting

ADA = {"given": "Ada", "family": "Lovelace", "email": "ada@example.com"}
ANON = {"email": "anon@example.com"}


class NameTests(unittest.TestCase):
    def test_full_name(self):
        self.assertEqual(display_name(ADA), "Ada Lovelace")

    def test_falls_back_to_email(self):
        self.assertEqual(display_name(ANON), "anon@example.com")

    def test_parts(self):
        self.assertEqual(get_user_name_parts(ADA), ("Ada", "Lovelace"))

    def test_greeting(self):
        self.assertEqual(greeting(ADA), "Hello, Ada Lovelace!")


if __name__ == "__main__":
    unittest.main()
