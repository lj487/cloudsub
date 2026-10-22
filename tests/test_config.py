from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloudsub.config import OPENAI_COMPATIBLE_PROVIDERS, PROVIDER_URLS, load_config
from cloudsub.sources import CloudSubError


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    @patch.dict("os.environ", {"CLOUDSUB_URL": "https://example.com/dav/", "CLOUDSUB_USERNAME": "test-user", "CLOUDSUB_PASSWORD": "test-secret"})
    def test_environment_credentials_are_not_in_repr(self):
        config = self.load('[source]\nurl_env="CLOUDSUB_URL"\nusername_env="CLOUDSUB_USERNAME"\npassword_env="CLOUDSUB_PASSWORD"')
        self.assertEqual(config.source.password, "test-secret")
        for private in ("test-user", "test-secret", "example.com"):
            self.assertNotIn(private, repr(config))

    @patch.dict("os.environ", {"CLOUDSUB_URL": "https://example.com/"})
    def test_rejects_invalid_config_and_embedded_credentials(self):
        for extra in ('password="test-secret"', 'recursive="false"', 'type="unknown"',
                      'unknown=true', 'username_env="NOT_PRESENT_TEST_VALUE"'):
            with self.subTest(extra=extra), self.assertRaises(CloudSubError):
                self.load('[source]\nurl_env="CLOUDSUB_URL"\n' + extra)
        for text in ('[source]\n', '[source]\nurl_env="MISSING_TEST_VALUE"', '[bad]\nx=1', '[broken'):
            with self.subTest(text=text), self.assertRaises(CloudSubError):
                self.load(text)

    @patch.dict("os.environ", {"CLOUDSUB_URL": "https://example.com/", "CLOUDSUB_TRANSLATION_KEY": "test-api-secret"})
    def test_external_translation_config_keeps_key_out_of_repr(self):
        for provider in ("openai", "anthropic", "gemini"):
            config = self.load('[source]\nurl_env="CLOUDSUB_URL"\n[translation]\n'
                               f'provider="{provider}"\nurl="https://api.example.com/v1"\n'
                               'model="test-model"\napi_key_env="CLOUDSUB_TRANSLATION_KEY"\nallow_remote=true')
            self.assertTrue(config.translation.allow_remote)
            self.assertEqual(config.translation.api_key, "test-api-secret")
            self.assertNotIn("test-api-secret", repr(config))
            self.assertNotIn("api.example.com", repr(config))

    @patch.dict("os.environ", {"CLOUDSUB_URL": "https://example.com/"})
    def test_embedded_api_key_is_not_a_supported_field(self):
        with self.assertRaises(CloudSubError):
            self.load('[source]\nurl_env="CLOUDSUB_URL"\n[translation]\napi_key="test-secret"')

    @patch.dict("os.environ", {"CLOUDSUB_URL": "https://example.com/", "CLOUDSUB_TRANSLATION_KEY": "test-api-secret"})
    def test_chinese_provider_defaults_and_region_override(self):
        vendors = {"deepseek", "qwen", "doubao", "zhipu", "kimi", "hunyuan", "qianfan", "minimax"}
        self.assertTrue(vendors <= OPENAI_COMPATIBLE_PROVIDERS)
        for provider in vendors:
            text = ('[source]\nurl_env="CLOUDSUB_URL"\n[translation]\n'
                    f'provider="{provider}"\nmodel="test-model"\n'
                    'api_key_env="CLOUDSUB_TRANSLATION_KEY"\nallow_remote=true\njson_mode=false')
            config = self.load(text)
            self.assertEqual(config.translation.url, PROVIDER_URLS[provider])
            self.assertTrue(config.translation.url.startswith("https://"))
            self.assertFalse(config.translation.json_mode)
            override = self.load(text + '\nurl="https://regional.example.com/v1"')
            self.assertEqual(override.translation.url, "https://regional.example.com/v1")


if __name__ == "__main__":
    unittest.main()
