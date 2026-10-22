import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from cloudsub.config import ASRConfig, TranslationConfig
from cloudsub.sources import CloudSubError
from cloudsub.subtitles import Segment, Transcriber, Translator, read_srt, render_srt, write_srt


class SubtitleTests(unittest.TestCase):
    def test_srt_round_trip_and_exclusive_output(self):
        segments = [Segment(0, 1.2345, " Hello\n\nworld "), Segment(3599.9996, 3601, "中文")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.srt"
            write_srt(path, segments)
            self.assertIn("01:00:00,000", path.read_text(encoding="utf-8"))
            self.assertEqual(read_srt(path)[0], Segment(0, 1.234, "Hello\nworld"))
            with self.assertRaises(FileExistsError):
                write_srt(path, [Segment(0, 1, "replacement")])
            self.assertIn("Hello", path.read_text(encoding="utf-8"))

    def test_empty_invalid_or_sub_millisecond_segments_fail(self):
        for segments in ([], [Segment(-1, 1, "test")], [Segment(1, 1, "test")],
                         [Segment(0, float("nan"), "test")], [Segment(0, 1, " ")],
                         [Segment(0, .0001, "test")]):
            with self.subTest(segments=segments), self.assertRaises(CloudSubError):
                render_srt(segments)

    def test_translation_adapters_keep_timing_and_do_not_follow_redirects(self):
        for provider in ("ollama", "openai"):
            def handle(request):
                payload = json.loads(request.content)
                self.assertFalse(payload["stream"])
                self.assertNotIn("authorization", request.headers)
                translated = json.dumps({"translations": ["你好", "世界"]})
                body = {"message": {"content": translated}} if provider == "ollama" else {
                    "choices": [{"message": {"content": translated}}]}
                return httpx.Response(200, json=body)
            settings = TranslationConfig(provider, "http://127.0.0.1:1234", "test-model")
            with Translator(settings, transport=httpx.MockTransport(handle)) as translator:
                actual = translator.translate([Segment(1, 2, "Hello"), Segment(2, 3, "world")])
                self.assertEqual(actual, [Segment(1, 2, "你好"), Segment(2, 3, "世界")])

    def test_translation_invalid_results_never_include_response_text(self):
        responses = [
            httpx.Response(302, headers={"location": "https://example.com/"}),
            httpx.Response(500, text="test-private-error"),
            httpx.Response(200, json={"message": {"content": '{"translations": []}'}}),
            httpx.Response(200, json={"message": {"content": "test-private-response"}}),
        ]
        for response in responses:
            settings = TranslationConfig(model="test-model")
            with Translator(settings, transport=httpx.MockTransport(lambda _: response)) as translator:
                with self.assertRaises(CloudSubError) as error:
                    translator.translate([Segment(0, 1, "test-private-input")])
                self.assertNotIn("test-private", str(error.exception))

    def test_translation_refuses_non_loopback_or_credential_urls(self):
        credential_url = "http://" + "test-user:test-secret" + "@127.0.0.1:1234"
        for url in ("https://example.com", "http://localhost:11434", "http://127.0.0.1:1234?token=test-token",
                    credential_url, "file:///test"):
            with self.subTest(url=url), self.assertRaises(CloudSubError):
                Translator(TranslationConfig(url=url, model="test-model"))

    def test_transcriber_is_lazy_and_reuses_model(self):
        model = Mock()
        segment = Mock(start=0, end=1, text=" hello ")
        model.transcribe.side_effect = [([segment], None), ([segment], None)]
        constructor = Mock(return_value=model)
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            snapshot.mkdir()
            (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
            resolver = Mock(return_value=str(snapshot))
            with patch.dict("sys.modules", {
                "faster_whisper": Mock(WhisperModel=constructor),
                "faster_whisper.utils": Mock(download_model=resolver),
            }):
                transcriber = Transcriber(ASRConfig(offline=True), Path(".local/models"))
                self.assertIsNone(transcriber.model)
                self.assertEqual(transcriber.transcribe(Path("input.media")), [Segment(0, 1, "hello")])
                transcriber.transcribe(Path("input.media"))
            resolver.assert_called_once_with("small", cache_dir=str(Path(".local/models")), local_files_only=True)
            self.assertEqual(constructor.call_args.args[0], str(snapshot))
        self.assertEqual(constructor.call_count, 1)
        self.assertTrue(constructor.call_args.kwargs["local_files_only"])
        self.assertTrue(model.transcribe.call_args.kwargs["vad_filter"])

    def test_offline_model_without_tokenizer_cannot_construct_or_download_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            for model_name in (directory, "small"):
                with self.subTest(model_name=model_name):
                    constructor = Mock()
                    resolver = Mock(return_value=directory)
                    with patch.dict("sys.modules", {
                        "faster_whisper": Mock(WhisperModel=constructor),
                        "faster_whisper.utils": Mock(download_model=resolver),
                    }):
                        transcriber = Transcriber(ASRConfig(model=model_name, offline=True), Path(".local/models"))
                        with self.assertRaisesRegex(CloudSubError, "missing tokenizer.json") as error:
                            transcriber.transcribe(Path("input.media"))
                        self.assertNotIn(directory, str(error.exception))
                        constructor.assert_not_called()
                        self.assertIsNone(transcriber.model)
                        if model_name == directory:
                            resolver.assert_not_called()
                        else:
                            resolver.assert_called_once_with("small", cache_dir=str(Path(".local/models")), local_files_only=True)

    def test_complete_local_offline_model_does_not_resolve_a_hub_model(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "tokenizer.json").write_text("{}", encoding="utf-8")
            model = Mock()
            model.transcribe.return_value = ([Mock(start=0, end=1, text="hello")], None)
            constructor, resolver = Mock(return_value=model), Mock()
            with patch.dict("sys.modules", {
                "faster_whisper": Mock(WhisperModel=constructor),
                "faster_whisper.utils": Mock(download_model=resolver),
            }):
                transcriber = Transcriber(ASRConfig(model=directory, offline=True), Path(".local/models"))
                self.assertEqual(transcriber.transcribe(Path("input.media")), [Segment(0, 1, "hello")])
            resolver.assert_not_called()
            self.assertEqual(constructor.call_args.args[0], directory)


if __name__ == "__main__":
    unittest.main()
