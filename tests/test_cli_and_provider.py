import unittest
from unittest.mock import patch

from astra import cli
from astra.tools.llm_providers import CmdProvider, make_provider


class MakeProviderCmdTests(unittest.TestCase):
    def test_cmd_provider_preserves_quoted_args(self):
        provider = make_provider("cmd", cmd='python -c "print(1)" --name "hello world"')
        self.assertIsInstance(provider, CmdProvider)
        self.assertEqual(
            provider.command,
            ["python", "-c", "print(1)", "--name", "hello world"],
        )

    def test_cmd_provider_supports_escaped_spaces(self):
        provider = make_provider("cmd", cmd=r"echo hello\ world")
        self.assertEqual(provider.command, ["echo", "hello world"])

    def test_cmd_provider_rejects_malformed_shell_string(self):
        with self.assertRaisesRegex(ValueError, "Invalid --cmd string"):
            make_provider("cmd", cmd='python -c "unterminated')

    def test_cmd_provider_rejects_empty_command_after_split(self):
        with self.assertRaisesRegex(ValueError, "non-empty command"):
            make_provider("cmd", cmd="   ")


class CliVersionDispatchTests(unittest.TestCase):
    def test_version_pseudo_command_dispatches(self):
        with patch("astra.cli._print_version_and_exit") as version_fn:
            rc = cli.main(["version"])
            version_fn.assert_called_once_with()
            self.assertEqual(rc, 0)

    def test_dash_version_dispatches(self):
        with patch("astra.cli._print_version_and_exit") as version_fn:
            rc = cli.main(["--version"])
            version_fn.assert_called_once_with()
            self.assertEqual(rc, 0)

    def test_upper_v_dispatches(self):
        with patch("astra.cli._print_version_and_exit") as version_fn:
            rc = cli.main(["-V"])
            version_fn.assert_called_once_with()
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
