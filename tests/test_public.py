from contextlib import redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "check_public", Path(__file__).resolve().parents[1] / "scripts" / "check_public.py"
)
public = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = public
SPEC.loader.exec_module(public)


class PublicCheckTests(unittest.TestCase):
    def test_examples_and_discussion_are_allowed(self):
        for line in (
            'password = "<webdav-password>"',
            'token = "test-token"',
            'secret = "${MY_SECRET}"',
            'token = "${{ secrets.GITHUB_TOKEN }}"',
            'CLOUDSUB_PASSWORD = "$env:CLOUDSUB_PASSWORD"',
            'password: str',
            'token = function(argument)',
            'token = os.environ["MY_TOKEN"]',
            'Contact dev@example.com or dev@service.invalid.',
            'https://user:password@example.com/video.mp4?token=example-token',
            'Never log a password, token, or Authorization header.',
        ):
            with self.subTest(line=line):
                self.assertEqual(public.line_rules(line), set())

    def test_prefixed_credentials_and_unquoted_yaml_are_blocked(self):
        injected_value = "private" + "-value-892347951"
        key = "CLOUDSUB_PASS" + "WORD"
        self.assertIn("credential-assignment", public.line_rules(key + '="' + injected_value + '"'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".github" / "workflows" / "check.yml"
            path.parent.mkdir(parents=True)
            path.write_text("env:\n  " + key + ": " + injected_value, encoding="utf-8")
            with patch.object(public, "candidates", return_value=[".github/workflows/check.yml"]):
                self.assertEqual(public.scan(root), [public.Finding(".github/workflows/check.yml", 2, "credential-assignment")])

    def test_yaml_environment_and_github_references_are_allowed(self):
        for value in ("${MY_TOKEN}", "${{ secrets.GITHUB_TOKEN }}", "$MY_TOKEN", "test-token", "null"):
            with self.subTest(value=value):
                self.assertEqual(public.line_rules("  token: " + value, is_yaml=True), set())

    def test_official_translation_key_is_private_but_generic_keys_are_allowed(self):
        injected_value = "private" + "-provider-credential-94672831"
        key = "CLOUDSUB_TRANSLATION_" + "KEY"
        self.assertIn("credential-assignment", public.line_rules(key + '="' + injected_value + '"'))
        self.assertIn("credential-assignment", public.line_rules(key + ": " + injected_value, is_yaml=True))
        self.assertEqual(public.line_rules(key + '="${{ secrets.TRANSLATION_KEY }}"'), set())
        self.assertEqual(public.line_rules('key = "ordinary-dictionary-key"'), set())

    def test_real_values_are_blocked_without_printing_them(self):
        injected_value = "private" + "-credential-value-948235"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment = "pass" + 'word = "' + injected_value + '"'
            (root / "README.md").write_text(assignment, encoding="utf-8")
            with patch.object(public, "candidates", return_value=["README.md"]), patch.object(
                public, "history_counts", return_value=(0, 0)
            ):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(public.main(root), 1)
                self.assertNotIn(injected_value, output.getvalue())
                self.assertIn("README.md", output.getvalue())
                self.assertIn("credential-assignment", output.getvalue())

    def test_private_paths_emails_and_signed_links(self):
        for line, rule in (
            ("C:" + "\\Users\\personal\\media", "private-absolute-path"),
            ("/home/" + "personal/media", "private-absolute-path"),
            ("personal" + "@real-domain.org", "personal-email"),
            ("https://cloud.real-domain.org/video?" + "to" + "ken=" + "private-value", "signed-url"),
            ("https://account:" + "private-value" + "@cloud.real-domain.org", "url-credentials"),
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, public.line_rules(line))

    def test_tracked_runtime_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(public, "candidates", return_value=[".env", "tasks.sqlite", "outputs/private.srt"]):
                self.assertEqual([f.rule for f in public.scan(root)], ["path-not-allowed"] * 3)

    def test_git_ignores_runtime_files_but_lists_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            (root / ".gitignore").write_text(
                (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
            )
            for name in (".env", "config.local.toml", "private.srt", "tasks.sqlite", "private.mp4"):
                (root / name).write_text("local runtime data", encoding="utf-8")
            (root / "README.md").write_text("Public example", encoding="utf-8")
            self.assertEqual(public.candidates(root), [".gitignore", "README.md"])
            self.assertEqual(public.scan(root), [])

    def test_whitelist_has_no_extra_depth_or_traversal(self):
        for path in ("src/cloudsub/main.py", "tests/test_public.py", ".github/workflows/check.yml"):
            self.assertIsNotNone(public.ALLOWED.fullmatch(path))
        for path in ("docs/private/data.md", "src/cloudsub/nested/private.py", "../README.md", ".git/config"):
            self.assertIsNone(public.ALLOWED.fullmatch(path))

    def test_history_reports_counts_without_identities(self):
        identity = ("Private Author <" + "private" + "@real-domain.org>").encode()
        responses = [
            subprocess.CompletedProcess([], 0, b"2\n", b""),
            subprocess.CompletedProcess([], 0, identity + b"\n" + identity + b"\n", b""),
        ]
        with patch.object(public, "candidates", return_value=[]), patch.object(public, "git", side_effect=responses):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(public.main(Path.cwd()), 0)
            self.assertIn("2 commit(s), 1 author identity/identities", output.getvalue())
            self.assertNotIn(identity.decode(), output.getvalue())


if __name__ == "__main__":
    unittest.main()
