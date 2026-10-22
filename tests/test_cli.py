import argparse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from cloudsub.cli import main, run
from cloudsub.config import ASRConfig, Config, SourceConfig, TranslationConfig
from cloudsub.sources import CloudSubError
from cloudsub.subtitles import Segment, Translator


class Handler(BaseHTTPRequestHandler):
    writes = {}
    methods = []
    fail_chinese_once = False

    def log_message(self, *_):
        pass

    def do_PROPFIND(self):
        self.methods.append("PROPFIND")
        body = b'''<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/test-private.mp4</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>4</d:getcontentlength></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
        self.send_response(207)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.methods.append("GET")
        body = self.writes.get(self.path, b"test")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        self.methods.append("PUT")
        content = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.endswith(".zh.srt") and self.fail_chinese_once:
            type(self).fail_chinese_once = False
            self.send_response(500)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") != "*" or self.path in self.writes:
            self.send_response(412)
            self.end_headers()
            return
        self.writes[self.path] = content
        self.send_response(201)
        self.end_headers()


class CLITests(unittest.TestCase):
    def setUp(self):
        Handler.writes = {}
        Handler.methods = []
        Handler.fail_chinese_once = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def arguments(self, directory, **changes):
        values = dict(upload=False, limit=None, id=None, output=Path(directory) / "subtitles", original_only=False)
        values.update(changes)
        return argparse.Namespace(**values)

    def test_webdav_end_to_end_with_fake_models_and_real_http(self):
        config = Config(SourceConfig("webdav", self.url + "/dav/", allow_http=True), ASRConfig(), TranslationConfig(model="test-model"))
        with tempfile.TemporaryDirectory() as directory, patch("cloudsub.cli.Transcriber") as asr, patch("cloudsub.cli.Translator") as translate:
            asr.return_value.transcribe.return_value = [Segment(0, 1, "hello")]
            translate.return_value.__enter__.return_value.translate.return_value = [Segment(0, 1, "你好")]
            output, errors = io.StringIO(), io.StringIO()
            args = self.arguments(directory, upload=True)
            with redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(run(config, args), 0)
            self.assertNotIn("test-private", output.getvalue() + errors.getvalue())
            self.assertEqual(set(Handler.writes), {"/dav/test-private.original.srt", "/dav/test-private.zh.srt"})
            self.assertEqual(len(list(args.output.glob("*.srt"))), 2)
            self.assertNotIn("DELETE", Handler.methods)
            local_media = asr.return_value.transcribe.call_args.args[0]
            self.assertFalse(local_media.exists())
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(run(config, self.arguments(directory)), 0)
            self.assertEqual(asr.return_value.transcribe.call_count, 1)
            self.assertEqual(translate.return_value.__enter__.return_value.translate.call_count, 1)

    def test_translation_failure_keeps_original_and_cleans_media(self):
        config = Config(SourceConfig("http", self.url + "/test?token=test-secret", allow_http=True), ASRConfig(), TranslationConfig(model="test-model"))
        with tempfile.TemporaryDirectory() as directory, patch("cloudsub.cli.Transcriber") as asr, patch("cloudsub.cli.Translator") as translate:
            asr.return_value.transcribe.return_value = [Segment(0, 1, "hello")]
            translate.return_value.__enter__.return_value.translate.side_effect = CloudSubError("Local translation failed.")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(run(config, self.arguments(directory)), 1)
            self.assertEqual(len(list((Path(directory) / "subtitles").glob("*.original.srt"))), 1)
            self.assertFalse(asr.return_value.transcribe.call_args.args[0].exists())
            with self.assertRaises(CloudSubError):
                run(config, self.arguments(directory, upload=True))

    def test_retry_finishes_partial_upload_without_replacing_original(self):
        config = Config(SourceConfig("webdav", self.url + "/dav/", allow_http=True), ASRConfig(), TranslationConfig(model="test-model"))
        Handler.fail_chinese_once = True
        with tempfile.TemporaryDirectory() as directory, patch("cloudsub.cli.Transcriber") as asr, patch("cloudsub.cli.Translator") as translate:
            asr.return_value.transcribe.return_value = [Segment(0, 1, "hello")]
            translate.return_value.__enter__.return_value.translate.return_value = [Segment(0, 1, "你好")]
            args = self.arguments(directory, upload=True)
            errors = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                self.assertEqual(run(config, args), 1)
                original = Handler.writes["/dav/test-private.original.srt"]
                self.assertEqual(run(config, args), 0, errors.getvalue())
            self.assertEqual(Handler.writes["/dav/test-private.original.srt"], original)
            self.assertIn("/dav/test-private.zh.srt", Handler.writes)
            self.assertEqual(asr.return_value.transcribe.call_count, 1)

    def test_http_original_only_and_list_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "config.local.toml"
            settings.write_text('[source]\ntype="http"\nurl_env="CLOUDSUB_URL"\nallow_http=true', encoding="utf-8")
            with patch.dict("os.environ", {"CLOUDSUB_URL": self.url + "/test-private?token=test-secret"}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["list", "--config", str(settings)]), 0)
                self.assertNotIn("test-private", output.getvalue())
                self.assertNotIn("test-secret", output.getvalue())
                with patch("cloudsub.cli.Transcriber") as asr, patch("cloudsub.cli.Translator") as translate:
                    asr.return_value.transcribe.return_value = [Segment(0, 1, "hello")]
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["run", "--config", str(settings), "--original-only", "--output", str(Path(directory) / "subtitles")]), 0)
                    translate.assert_not_called()

    def test_accounts_with_same_endpoint_and_path_do_not_share_subtitles(self):
        with tempfile.TemporaryDirectory() as directory, patch("cloudsub.cli.Transcriber") as asr:
            asr.return_value.transcribe.side_effect = [[Segment(0, 1, "first account")], [Segment(0, 1, "second account")]]
            args = self.arguments(directory, original_only=True)
            for username in ("test-account-one", "test-account-two"):
                config = Config(SourceConfig("webdav", self.url + "/dav/", username, "test-secret", allow_http=True), ASRConfig(), TranslationConfig())
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(run(config, args), 0)
            self.assertEqual(asr.return_value.transcribe.call_count, 2)
            files = list(args.output.glob("*.original.srt"))
            self.assertEqual(len(files), 2)
            self.assertTrue(any("first account" in path.read_text(encoding="utf-8") for path in files))
            self.assertTrue(any("second account" in path.read_text(encoding="utf-8") for path in files))

    def test_external_translation_warns_and_sends_only_subtitle_text(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"translations":["你好"]}'}}]})

        settings = TranslationConfig("openai", "https://api.example.com/v1", "test-model", "test-api-key", True)
        config = Config(SourceConfig("http", self.url + "/test-private?token=test-secret", allow_http=True), ASRConfig(), settings)
        translator = Translator(settings, transport=httpx.MockTransport(handle))
        with tempfile.TemporaryDirectory() as directory, patch("cloudsub.cli.Transcriber") as asr, patch("cloudsub.cli.Translator", return_value=translator):
            asr.return_value.transcribe.return_value = [Segment(0, 1, "hello")]
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(run(config, self.arguments(directory)), 0)
        self.assertIn("external API", errors.getvalue())
        for private in ("test-secret", "test-private", "test-api-key"):
            self.assertNotIn(private, output.getvalue() + errors.getvalue())
            self.assertNotIn(private.encode(), requests[0].content)
        self.assertIn(b"hello", requests[0].content)

    def test_missing_translation_key_does_not_block_list_or_original_only(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "config.local.toml"
            settings.write_text('[source]\ntype="http"\nurl_env="CLOUDSUB_URL"\nallow_http=true\n'
                                '[translation]\nprovider="deepseek"\nmodel="test-model"\n'
                                'api_key_env="CLOUDSUB_TRANSLATION_KEY"\nallow_remote=true', encoding="utf-8")
            with patch.dict("os.environ", {"CLOUDSUB_URL": self.url + "/media", "CLOUDSUB_TRANSLATION_KEY": ""}), patch("cloudsub.cli.Transcriber") as asr:
                asr.return_value.transcribe.return_value = [Segment(0, 1, "hello")]
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["list", "--config", str(settings)]), 0)
                    self.assertEqual(main(["run", "--config", str(settings), "--original-only", "--output", str(Path(directory) / "subtitles")]), 0)
                    self.assertEqual(main(["run", "--config", str(settings), "--output", str(Path(directory) / "subtitles")]), 1)
                self.assertEqual(asr.return_value.transcribe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
