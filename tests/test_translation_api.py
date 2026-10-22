import json
import unittest
from dataclasses import replace

import httpx

from cloudsub.config import OPENAI_COMPATIBLE_PROVIDERS, TranslationConfig
from cloudsub.sources import CloudSubError
from cloudsub.subtitles import Segment, Translator


def response_body(provider, translations):
    message = json.dumps({"translations": translations}, ensure_ascii=False)
    if provider == "ollama":
        return {"message": {"content": message}}
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        return {"choices": [{"message": {"content": message}}]}
    if provider == "anthropic":
        return {"content": [{"type": "thinking", "thinking": "test-thought"}, {"type": "text", "text": message[:10]}, {"type": "text", "text": message[10:]}]}
    return {"candidates": [{"content": {"parts": [{"thought": True, "text": "test-thought"}, {"text": message[:10]}, {"text": message[10:]}]}}]}


class TranslationAPITest(unittest.TestCase):
    segments = [Segment(0, 1, "test-private subtitle")]

    def settings(self, provider="openai", **changes):
        config = TranslationConfig(provider, "https://api.example.test/v1", "test-model", "test-key", True)
        return replace(config, **changes)

    def test_openai_endpoint_auth_and_json_mode(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=response_body("openai", ["测试"]))

        with Translator(self.settings(), transport=httpx.MockTransport(handler)) as translator:
            self.assertTrue(translator.remote)
            self.assertEqual(translator.translate(self.segments), [Segment(0, 1, "测试")])
        request = seen[0]
        self.assertEqual(str(request.url), "https://api.example.test/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer test-key")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["stream"])
        self.assertNotIn("temperature", payload)
        self.assertEqual(json.loads(payload["messages"][1]["content"]), [self.segments[0].text])
        self.assertNotIn("test-key", str(request.url))

    def test_anthropic_native_request_and_text_blocks(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=response_body("anthropic", ["测试"]))

        with Translator(self.settings("anthropic"), transport=httpx.MockTransport(handler)) as translator:
            self.assertEqual(translator.translate(self.segments)[0].text, "测试")
        request = seen[0]
        self.assertEqual(str(request.url), "https://api.example.test/v1/messages")
        self.assertEqual(request.headers["x-api-key"], "test-key")
        self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", request.headers)
        payload = json.loads(request.content)
        self.assertIn("JSON", payload["system"])
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual([message["role"] for message in payload["messages"]], ["user"])
        self.assertNotIn("response_format", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("test-key", str(request.url))

    def test_gemini_native_request_escaped_model_and_no_thought_text(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=response_body("gemini", ["测试"]))

        settings = self.settings("gemini", url="https://api.example.test/v1beta/", model="models/test/model?#")
        with Translator(settings, transport=httpx.MockTransport(handler)) as translator:
            self.assertEqual(translator.translate(self.segments)[0].text, "测试")
        request = seen[0]
        self.assertEqual(request.url.raw_path, b"/v1beta/models/test%2Fmodel%3F%23:generateContent")
        self.assertEqual(request.headers["x-goog-api-key"], "test-key")
        self.assertNotIn("authorization", request.headers)
        self.assertFalse(request.url.query)
        payload = json.loads(request.content)
        self.assertIn("JSON", payload["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(payload["generationConfig"], {"responseMimeType": "application/json"})
        self.assertEqual(json.loads(payload["contents"][0]["parts"][0]["text"]), [self.segments[0].text])
        self.assertNotIn("test-key", str(request.url))

    def test_local_ollama_and_openai_continue_without_key(self):
        for provider in ("ollama", "openai"):
            seen = []

            def handler(request):
                seen.append(request)
                return httpx.Response(200, json=response_body(provider, ["测试"]))

            with self.subTest(provider=provider), Translator(self.settings(provider, url="http://127.0.0.1:1234", api_key="", allow_remote=False), transport=httpx.MockTransport(handler)) as translator:
                self.assertFalse(translator.remote)
                self.assertEqual(translator.translate(self.segments)[0].text, "测试")
            self.assertNotIn("authorization", seen[0].headers)
            self.assertNotIn("x-api-key", seen[0].headers)

    def test_remote_requires_explicit_opt_in_https_and_key(self):
        for changes in ({"allow_remote": False}, {"api_key": ""}, {"url": "http://api.example.test/v1"}, {"url": "http://localhost:1234", "allow_remote": False}, {"model": ""}):
            with self.subTest(changes=changes), self.assertRaises(CloudSubError):
                Translator(self.settings(**changes))

    def test_keys_urls_and_embedded_auth_are_validated_without_echoing(self):
        for changes in ({"api_key": "test-key\nheader"}, {"api_key": "test key"}, {"api_key": "test-" + chr(0x6D4B)}, {"url": "https://test-user:test-key@api.example.test/v1"}, {"url": "https://api.example.test/v1?key=test-key"}, {"url": "https://api.example.test/v1#test-key"}, {"url": "file:///test-key"}):
            with self.subTest(changes=changes), self.assertRaises(CloudSubError) as raised:
                Translator(self.settings(**changes))
            self.assertNotIn("test-key", str(raised.exception))
            self.assertNotIn("api.example.test", str(raised.exception))

    def test_redirects_are_never_followed(self):
        for provider in ("openai", "anthropic", "gemini"):
            seen = []

            def handler(request):
                seen.append(request)
                return httpx.Response(302, headers={"location": "https://other.example.test/test-key"})

            with self.subTest(provider=provider), Translator(self.settings(provider), transport=httpx.MockTransport(handler)) as translator:
                with self.assertRaisesRegex(CloudSubError, "HTTP 302"):
                    translator.translate(self.segments)
            self.assertEqual(len(seen), 1)

    def test_errors_never_reveal_body_key_url_or_subtitle(self):
        def network_failure(request):
            raise httpx.ConnectError("test-key api.example.test test-private subtitle")

        for transport in (httpx.MockTransport(network_failure), httpx.MockTransport(lambda request: httpx.Response(401, content=b"test-key api.example.test test-private subtitle")), httpx.MockTransport(lambda request: httpx.Response(200, content=b"test-key api.example.test test-private subtitle"))):
            with Translator(self.settings(), transport=transport) as translator:
                with self.assertRaises(CloudSubError) as raised:
                    translator.translate(self.segments)
            for value in ("test-key", "api.example.test", "test-private subtitle"):
                self.assertNotIn(value, str(raised.exception))

    def test_response_shape_count_and_empty_translations_are_rejected(self):
        for provider in ("openai", "anthropic", "gemini"):
            for body in ({}, response_body(provider, []), response_body(provider, [" "]), response_body(provider, [4])):
                with self.subTest(provider=provider, body=body), Translator(self.settings(provider), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as translator:
                    with self.assertRaises(CloudSubError):
                        translator.translate(self.segments)

    def test_response_limit(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)))
        with Translator(self.settings(), transport=transport) as translator:
            with self.assertRaisesRegex(CloudSubError, "too large"):
                translator.translate(self.segments)

    def test_batches_more_than_twelve_keep_order_and_timestamps(self):
        for provider in ("openai", "anthropic", "gemini"):
            batches = []

            def handler(request):
                payload = json.loads(request.content)
                text = payload["contents"][0]["parts"][0]["text"] if provider == "gemini" else payload["messages"][-1]["content"]
                batch = json.loads(text)
                batches.append(batch)
                return httpx.Response(200, json=response_body(provider, ["译" + value for value in batch]))

            segments = [Segment(index, index + 1, f"test-{index}") for index in range(25)]
            with self.subTest(provider=provider), Translator(self.settings(provider), transport=httpx.MockTransport(handler)) as translator:
                translated = translator.translate(segments)
            self.assertEqual(list(map(len, batches)), [12, 12, 1])
            self.assertEqual(translated, [replace(segment, text="译" + segment.text) for segment in segments])

    def test_chinese_providers_share_openai_protocol(self):
        for provider in OPENAI_COMPATIBLE_PROVIDERS:
            seen = []

            def handler(request):
                seen.append(request)
                return httpx.Response(200, json=response_body(provider, ["测试"]))

            with self.subTest(provider=provider), Translator(self.settings(provider), transport=httpx.MockTransport(handler)) as translator:
                self.assertEqual(translator.translate(self.segments)[0].text, "测试")
            request = seen[0]
            self.assertEqual(str(request.url), "https://api.example.test/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            self.assertEqual(json.loads(request.content)["response_format"], {"type": "json_object"})

    def test_json_mode_can_be_disabled_but_output_is_still_validated(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=response_body("deepseek", ["测试"]))

        with Translator(self.settings("deepseek", json_mode=False), transport=httpx.MockTransport(handler)) as translator:
            self.assertEqual(translator.translate(self.segments)[0].text, "测试")
        self.assertNotIn("response_format", json.loads(seen[0].content))
        with Translator(self.settings("deepseek", json_mode=False), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_body("deepseek", [])))) as translator:
            with self.assertRaises(CloudSubError):
                translator.translate(self.segments)

    def test_complete_json_fence_and_minimax_think_prefix_only(self):
        message = json.dumps({"translations": ["测试"]}, ensure_ascii=False)
        for provider, content in (("openai", "```json\n" + message + "\n```"), ("minimax", "<think>test-thought</think>\n```json\n" + message + "\n```"), ("minimax", "<think>test-thought</think>\n" + message)):
            body = {"choices": [{"message": {"content": content}}]}
            with self.subTest(provider=provider, content=content), Translator(self.settings(provider), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as translator:
                self.assertEqual(translator.translate(self.segments)[0].text, "测试")
        for content in ("preamble " + message, "<think>incomplete " + message, "```json\n" + message + "\n``` trailing", message + " trailing", "<think>test-thought</think> accidental-text " + message):
            body = {"choices": [{"message": {"content": content}}]}
            with self.subTest(content=content), Translator(self.settings("minimax"), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as translator:
                with self.assertRaises(CloudSubError):
                    translator.translate(self.segments)


if __name__ == "__main__":
    unittest.main()
