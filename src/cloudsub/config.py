"""Configuration contains environment variable names, never embedded credentials."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib

from .sources import CloudSubError


PROVIDER_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "openai": "http://127.0.0.1:1234/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "kimi": "https://api.moonshot.cn/v1",
    "hunyuan": "https://tokenhub.tencentcloudmaas.com/v1",
    "qianfan": "https://qianfan.baidubce.com/v2",
    "minimax": "https://api.minimax.cn/v1",
}
OPENAI_COMPATIBLE_PROVIDERS = frozenset(PROVIDER_URLS) - {"ollama", "anthropic", "gemini"}


@dataclass(frozen=True)
class SourceConfig:
    type: str
    url: str = field(repr=False)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    folder: str = ""
    recursive: bool = False
    allow_http: bool = False


@dataclass(frozen=True)
class ASRConfig:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str = ""
    offline: bool = False


@dataclass(frozen=True)
class TranslationConfig:
    provider: str = "ollama"
    url: str = field(default="http://127.0.0.1:11434", repr=False)
    model: str = ""
    api_key: str = field(default="", repr=False)
    allow_remote: bool = False
    json_mode: bool = True


@dataclass(frozen=True)
class Config:
    source: SourceConfig
    transcription: ASRConfig
    translation: TranslationConfig


def _section(data: dict, name: str, allowed: set[str]) -> dict:
    section = data.get(name, {})
    if not isinstance(section, dict) or set(section) - allowed:
        raise CloudSubError(f"Invalid {name} configuration fields.")
    return section


def _text(section: dict, key: str, default: str = "") -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise CloudSubError(f"Invalid configuration field: {key}.")
    return value


def _flag(section: dict, key: str) -> bool:
    value = section.get(key, False)
    if not isinstance(value, bool):
        raise CloudSubError(f"Invalid boolean configuration field: {key}.")
    return value


def _environment(section: dict, key: str, required: bool = False, allow_missing: bool = False) -> str:
    name = _text(section, key)
    if not name and not required:
        return ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise CloudSubError(f"Invalid environment variable name for {key}.")
    value = os.environ.get(name, "")
    if not value and allow_missing:
        return ""
    if not value or any(ord(c) < 32 for c in value):
        raise CloudSubError(f"Missing or invalid environment value for {key}.")
    return value


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        raise CloudSubError("Cannot read configuration; use config.example.toml as a template.") from None
    if set(data) - {"source", "transcription", "translation"}:
        raise CloudSubError("Unknown configuration section.")
    source = _section(data, "source", {
        "type", "url_env", "username_env", "password_env", "folder", "recursive", "allow_http",
    })
    kind = _text(source, "type", "webdav")
    if kind not in {"webdav", "http"}:
        raise CloudSubError("Source type must be webdav or http.")
    username = _environment(source, "username_env")
    password = _environment(source, "password_env")
    if bool(username) != bool(password):
        raise CloudSubError("Configure both username_env and password_env, or neither.")
    asr = _section(data, "transcription", {"model", "device", "compute_type", "language", "offline"})
    device = _text(asr, "device", "cpu")
    if device not in {"cpu", "cuda", "auto"}:
        raise CloudSubError("Transcription device must be cpu, cuda or auto.")
    language = _text(asr, "language")
    if language and not re.fullmatch(r"[a-z]{2,3}", language):
        raise CloudSubError("Language must be empty or a two/three-letter language code.")
    model = _text(asr, "model", "small")
    compute = _text(asr, "compute_type", "int8")
    if not model or compute not in {"int8", "float32", "float16", "int8_float16", "default"}:
        raise CloudSubError("Invalid transcription model or compute_type.")
    translation = _section(data, "translation", {"provider", "url", "model", "api_key_env", "allow_remote", "json_mode"})
    provider = _text(translation, "provider", "ollama")
    if provider not in PROVIDER_URLS:
        raise CloudSubError("Unknown translation provider; see config.example.toml and README.")
    json_mode = translation.get("json_mode", True)
    if not isinstance(json_mode, bool):
        raise CloudSubError("Invalid boolean configuration field: json_mode.")
    return Config(
        SourceConfig(kind, _environment(source, "url_env", required=True), username, password,
                     _text(source, "folder"), _flag(source, "recursive"), _flag(source, "allow_http")),
        ASRConfig(model, device, compute, language, _flag(asr, "offline")),
        TranslationConfig(provider, _text(translation, "url", PROVIDER_URLS[provider]),
                          _text(translation, "model"), _environment(translation, "api_key_env", allow_missing=True),
                          _flag(translation, "allow_remote"), json_mode),
    )
