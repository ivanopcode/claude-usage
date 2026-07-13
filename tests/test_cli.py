import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

import cli


class CliTests(unittest.TestCase):
    def test_version(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main(["--version"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "0.1.0")

    def test_unknown_command(self):
        output = io.StringIO()
        with redirect_stderr(output):
            result = cli.main(["unknown"])
        self.assertEqual(result, 2)
        self.assertIn("Unknown command", output.getvalue())


if __name__ == "__main__":
    unittest.main()
