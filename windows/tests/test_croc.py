from __future__ import annotations

import subprocess
import unittest

from linkdrop.croc import clean_output, croc_executable, generate_code


class CrocIntegrationTest(unittest.TestCase):
    def test_bundled_executable(self) -> None:
        path = croc_executable()
        self.assertTrue(path.exists())
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("croc version", result.stdout)

    def test_generated_code_and_output_cleanup(self) -> None:
        code = generate_code()
        self.assertRegex(code, r"^linkdrop-[0-9a-f]{12}$")
        cleaned = clean_output("\x1b[31mSending\u001b[0m\rSending\r\n\r\nDone\r\n")
        self.assertEqual(cleaned, "Sending\nDone")


if __name__ == "__main__":
    unittest.main()
