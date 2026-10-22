# CloudSub

English | [简体中文](README.md)

Generate subtitles for videos on WebDAV, OpenList or a direct HTTP(S) link. The media is downloaded to your machine, transcribed locally with faster-whisper, and optionally translated into Simplified Chinese by a local model or by an external API you explicitly enable.

It is a command-line tool released under the MIT license. It talks to cloud drives through standard protocols (WebDAV / HTTP) and does not rely on any vendor's private login API.

## Supported sources

| Source | Scan folders | Download and transcribe | Local original / Chinese subtitles | Optional write-back |
| --- | --- | --- | --- | --- |
| WebDAV | Yes, recursive optional | Yes | Yes | Depends on server permissions |
| OpenList (WebDAV endpoint) | Yes, recursive optional | Yes | Yes | Depends on the mounted driver and write access |
| HTTP(S) direct link | Single file | Yes | Yes | No |

WebDAV scans pick up `.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.m4v`, `.ts`, `.mp3`, `.wav`, `.m4a`, `.flac` and `.ogg`. An HTTP link is treated as one media resource. Web player pages, DRM and native cloud-drive login APIs are out of scope for this first version.

## Install

Python 3.11–3.13 is recommended. CPU is the default; CUDA requires meeting faster-whisper's own runtime requirements. The ASR extra pins PyAV below 19 because faster-whisper 1.2 still calls `av.open(metadata_errors=...)`, which PyAV 19 removed (see the [PyAV 19.0.0 release notes](https://github.com/PyAV-Org/PyAV/releases/tag/v19.0.0)).

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e '.[asr]'
cp config.example.toml config.local.toml
```

To run the tests or view `--help` only, `python -m pip install -e .` is enough.

## Configure the source

Edit `config.local.toml`. The file only holds the **names** of environment variables; real URLs and credentials come from your shell environment. `config.local.toml` is git-ignored and the program never reads `.env` files.

```toml
[source]
type = "webdav"
url_env = "CLOUDSUB_URL"
username_env = "CLOUDSUB_USERNAME"
password_env = "CLOUDSUB_PASSWORD"
folder = ""
recursive = false
allow_http = false
```

- `url_env` must point to the full WebDAV endpoint, e.g. `https://files.example.com/dav/`. For OpenList, the WebDAV endpoint can differ from the web UI address; see the [OpenList WebDAV docs](https://doc.oplist.org/guide/advanced/webdav).
- `folder` is relative to the endpoint; an empty string means the endpoint root. Omit both `username_env` and `password_env` for services without authentication.
- For a single direct link set `type = "http"`, put the full (signed) URL in the variable, and drop `folder` / `recursive`. URLs with embedded credentials or fragments are rejected. HTTPS is required unless you set `allow_http = true` (including LAN services).

Enter secrets interactively so they stay out of your shell history:

```bash
read -rsp 'WebDAV endpoint or signed media URL: ' CLOUDSUB_URL; echo
read -rp 'Username: ' CLOUDSUB_USERNAME
read -rsp 'Password: ' CLOUDSUB_PASSWORD; echo
export CLOUDSUB_URL CLOUDSUB_USERNAME CLOUDSUB_PASSWORD
```

Environment variables are still readable by other programs running as the same user; use a trusted machine and clear them when done.

## Transcription

The default is the `small` model on CPU with `int8`. A model name is downloaded to `.local/models` on first use; use a local model directory, or a cached model with `offline = true`, to forbid downloads during recognition. Leave `language` empty for auto-detection or set a code such as `en`, `ja`, `ko`. Recognition always runs on your machine.

## Translation

Translation defaults to a local service. Only subtitle text is sent to the translation service; video is never uploaded through it. Use `--original-only` to skip translation.

Local service (Ollama, or an OpenAI-compatible server such as llama.cpp / LM Studio). Install and start it yourself and fill in an installed model name:

```toml
[translation]
provider = "ollama"            # or "openai" for OpenAI-compatible servers
url = "http://127.0.0.1:11434" # adjust the port to your server
model = "your-local-model"
allow_remote = false
```

Local services must use a loopback IP address (`127.0.0.1` or `[::1]`); the `localhost` alias is not treated as local.

External API (opt-in). Set `allow_remote = true`, an HTTPS URL and an API key environment variable (`api_key_env`). The CLI prints a warning before sending subtitle text to a remote provider. Supported protocols: OpenAI-compatible Chat Completions, native Anthropic Messages, native Gemini `generateContent`, plus presets that reuse the OpenAI-compatible protocol with a default base URL:

| `provider` | Default base URL |
| --- | --- |
| `openai` | your `/v1` base URL, e.g. `https://api.openai.com/v1` |
| `anthropic` | `https://api.anthropic.com/v1` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` |
| `deepseek` | `https://api.deepseek.com` |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `doubao` | `https://ark.cn-beijing.volces.com/api/v3` |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` |
| `kimi` | `https://api.moonshot.cn/v1` |
| `hunyuan` | `https://tokenhub.tencentcloudmaas.com/v1` |
| `qianfan` | `https://qianfan.baidubce.com/v2` |
| `minimax` | `https://api.minimax.cn/v1` |

An explicit `url` always overrides the preset. Model names have no presets; take them from your provider's console. Set `json_mode = false` if an OpenAI-compatible model rejects `response_format`; the reply must still be strict JSON. Subtitle text is sent to the chosen provider, and its pricing, retention and privacy rules apply. The API key is sent in a request header, never in a URL; translation requests do not follow redirects or read proxy environment variables. See `config.example.toml` and the Chinese README for provider-specific notes.

## Usage

```sh
cloudsub list --config config.local.toml
cloudsub run --config config.local.toml --limit 1
cloudsub run --config config.local.toml --original-only
cloudsub run --config config.local.toml --id TASK_ID
```

`list` shows task IDs and sizes; add `--show-paths` to print remote paths explicitly. `--id` takes an ID from the list. `--limit` caps how many tasks need new work in this run; tasks whose subtitles already exist do not count, so repeating `--limit 1` walks through the files one by one (with `--upload` every task counts). Recursive scanning is controlled by `recursive = true`; nothing polls or schedules itself.

Results go to `.local/subtitles` (override with `--output`), named by task ID: `ID.original.srt` and `ID.zh.srt`. Existing local subtitles are reused on re-runs; move them aside after changing the model or the source video. The task ID is derived from the source URL, account and path, so a re-signed URL produces a new ID. Temporary media is removed after each task, successful or not. Always review machine-generated subtitles.

### Optional write-back

Verify your server's behavior in an empty test folder first, then opt in:

```sh
cloudsub run --config config.local.toml --limit 1 --upload
```

WebDAV write-back stores `stem.original.srt` and `stem.zh.srt` next to the video and never deletes or modifies the video. Uploads use `If-None-Match: *` and are read back for verification; identical existing content counts as success, different content is never overwritten. If a gateway ignores conditional headers, the client cannot guarantee it will not overwrite a remote file of the same name. HTTP sources cannot use `--upload`.

## Privacy and limits

- Output files use task IDs rather than remote file names, and error messages never include URLs, auth headers, response bodies or subtitle text. See [SECURITY.md](SECURITY.md) for credential handling, redirects and data boundaries.
- Limits: 20 GiB per media file; 2 MiB per WebDAV XML response and at most 10,000 entries per scan; at most 5 redirects per download. No resume and no automatic retry in this version.
- The 20 GiB cap is a download limit, not a memory limit. faster-whisper decodes the whole recording into memory (about 220 MiB per hour of float32 audio, plus recognition and VAD overhead), so split long recordings first.
- Translation is requested in batches of 12 lines and the Chinese file is saved only when all batches finish. If a later batch fails, a re-run repeats earlier batches, which may be billed again by external APIs; try `--limit 1` first.

## Development

```sh
python -m unittest discover -s tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/adapters.md](docs/adapters.md) (Chinese) for the contribution workflow and protocol boundaries. The code is [MIT](LICENSE) licensed; third-party dependencies and models keep their own licenses.
