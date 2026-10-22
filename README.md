# CloudSub

从 WebDAV、OpenList 或 HTTP 视频直链生成字幕。视频下载到本机，由 faster-whisper 识别原文，再通过本地模型或显式配置的外置 API 翻译为简体中文。

这是一个从零编写的命令行项目，使用 MIT 许可。它按协议接入网盘，不包含任何个人账号、历史任务或媒体资料。

## 支持范围

| 来源 | 扫描目录 | 下载识别 | 本地原文/中文字幕 | 可选字幕写回 |
| --- | --- | --- | --- | --- |
| WebDAV | 支持，可递归 | 支持 | 支持 | 取决于服务端权限与实现 |
| OpenList 的 WebDAV 接口 | 支持，可递归 | 支持 | 支持 | 取决于挂载驱动与写入权限 |
| HTTP(S) 直链 | 单文件 | 支持 | 支持 | 不支持 |

WebDAV 扫描识别 `.mp4`、`.mkv`、`.mov`、`.avi`、`.webm`、`.m4v`、`.ts`、`.mp3`、`.wav`、`.m4a`、`.flac`、`.ogg`。HTTP 直链按单个媒体资源处理。网页播放页、DRM 和网盘原生登录接口不在首版范围内；同样提供 WebDAV 的服务也需要验证其下载和写入能力。

## 安装

建议使用 Python 3.11–3.13，在项目根目录执行。CPU 是默认配置；CUDA 需要自行满足 faster-whisper 的运行要求。识别依赖限制为 PyAV `<19`，避开当前 faster-whisper 的解码接口不兼容问题，参见 [PyAV 19 变更](https://github.com/PyAV-Org/PyAV/releases/tag/v19.0.0)。

Windows PowerShell 7：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[asr]'
Copy-Item config.example.toml config.local.toml
```

macOS / Linux：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[asr]'
cp config.example.toml config.local.toml
```

仅运行协议测试或查看命令帮助时，安装 `python -m pip install -e .` 即可，无需下载识别依赖。

## 配置来源

编辑 `config.local.toml`。配置中只填写环境变量的名称，真实 URL 和凭据通过当前终端的环境变量传入。文件已被 Git 忽略；程序不会自动读取 `.env`。

WebDAV / OpenList 使用完整的 WebDAV 根端点，例如 `https://files.example.com/dav/`。OpenList 网页地址与 WebDAV 端点可能不同，参见 [OpenList WebDAV 文档](https://doc.oplist.org/guide/advanced/webdav)。

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

`folder` 是端点内的相对目录；空字符串表示端点根目录。公开示例中不要放私人目录名。无需认证的服务请同时省略 `username_env` 和 `password_env`。

在 PowerShell 7 中交互输入，避免把密码或签名直链写进命令历史：

```powershell
$env:CLOUDSUB_URL = Read-Host 'WebDAV endpoint or signed media URL' -MaskInput
$cloudsubCredential = Get-Credential
$env:CLOUDSUB_USERNAME = $cloudsubCredential.UserName
$env:CLOUDSUB_PASSWORD = $cloudsubCredential.GetNetworkCredential().Password
```

macOS / Linux 可在 Bash 中交互输入：

```bash
read -rsp 'WebDAV endpoint or signed media URL: ' CLOUDSUB_URL; echo
read -rp 'Username: ' CLOUDSUB_USERNAME
read -rsp 'Password: ' CLOUDSUB_PASSWORD; echo
export CLOUDSUB_URL CLOUDSUB_USERNAME CLOUDSUB_PASSWORD
```

环境变量仍可被同一用户权限下的程序读取，请在可信设备使用，结束后关闭终端或清除这些变量。

HTTP 直链只需将 `type` 改为 `"http"`，将完整签名 URL 放入 `CLOUDSUB_URL`，并移除不需要的认证字段。URL 不接受内嵌用户名/密码或 fragment。默认要求 HTTPS；确需明文 HTTP（含局域网服务）时，显式设置 `allow_http = true`。

## 配置识别与翻译

默认识别配置为 `small` 模型、CPU、`int8`。首次使用模型名会下载模型到 `.local/models`；使用本地模型目录，或已缓存的模型并设置 `offline = true`，可禁止识别阶段下载。模型下载需要网络，媒体识别在本机执行。识别语言留空自动检测，也可填写 `en`、`ja`、`ko` 等代码。

识别始终在本机执行。翻译默认使用本地服务，也可显式开启外置 API；只向翻译服务发送字幕正文，不通过翻译接口上传视频文件。

### 本地翻译

本地服务与模型需要自行安装并启动。填入已安装且支持 JSON 输出的模型名称，程序不会自动安装翻译模型。

Ollama：

```toml
[translation]
provider = "ollama"
url = "http://127.0.0.1:11434"
model = "your-local-model"
allow_remote = false
```

llama.cpp / LM Studio 的 OpenAI 兼容服务：

```toml
[translation]
provider = "openai"
url = "http://127.0.0.1:1234/v1"
model = "your-local-model"
allow_remote = false
```

端口按本机服务设置调整。本地服务使用回环 IP 地址（如 `127.0.0.1` 或 `[::1]`），可用 HTTP；`localhost` 别名不按本机地址处理。`openai` 是协议名称，实际请求目标取决于 `url`。本地服务需要密钥时，也可配置下面的 `api_key_env`。

### 外置 API 翻译

支持 OpenAI 兼容 Chat Completions、Anthropic 原生 Messages、Gemini 原生 generateContent 三种协议。外置服务必须同时设置 `allow_remote = true`、HTTPS 地址和 API 密钥环境变量。运行前 CLI 会显示外置 API 提醒；`--original-only` 跳过翻译。

| `provider` | `url` 基地址示例 | 请求协议 |
| --- | --- | --- |
| `openai` | `https://api.openai.com/v1`，或兼容服务商的 `/v1` 基地址 | Chat Completions |
| `anthropic` | `https://api.anthropic.com/v1` | Messages |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | generateContent |

在 `config.local.toml` 中使用所选服务商的配置；以下示例可按表格替换 `provider` 和 `url`：

```toml
[translation]
provider = "openai"
url = "https://api.openai.com/v1"
model = "your-provider-model"
api_key_env = "CLOUDSUB_TRANSLATION_KEY"
allow_remote = true
json_mode = true
```

`model` 填写服务商实际可用的模型名。Gemini 填原始模型名，不加 `models/` 或 `:generateContent`，程序会构造请求路径。OpenAI 兼容协议默认使用 JSON 输出设置；若所选模型不接受 `response_format`，可设 `json_mode = false`，省略该参数，但返回内容仍必须是严格 JSON。Gemini 使用 JSON mode，Anthropic 使用严格 JSON 提示；全部结果都会校验 JSON、条目数量与类型，不能据此保证兼容所有模型。

### 中国常用 API 预设

以下 `provider` 复用 OpenAI 兼容协议，并在未填写 `url` 时使用表中默认基地址。它们同样要求 `allow_remote = true`、API 密钥和自行填写的模型名。

| 服务 | `provider` | 默认基地址 | 官方说明 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek` | `https://api.deepseek.com` | [API 接入](https://api-docs.deepseek.com/guides/search_fetch) |
| 阿里云百炼 / 通义千问 | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | [模型调用](https://help.aliyun.com/en/model-studio/model-calling-in-sub-workspace) |
| 火山方舟 / 豆包 | `doubao` | `https://ark.cn-beijing.volces.com/api/v3` | [第三方工具接入](https://docs.volcengine.com/docs/ark/integrate-third-party-tools?lang=en) |
| 智谱 | `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | [OpenAI 兼容接口](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) |
| 月之暗面 / Kimi | `kimi` | `https://api.moonshot.cn/v1` | [快速开始](https://platform.kimi.com/docs/get-api-key) |
| 腾讯混元 / TokenHub | `hunyuan` | `https://tokenhub.tencentcloudmaas.com/v1` | [混元兼容接口与迁移说明](https://cloud.tencent.com/document/product/1729/111007)、[TokenHub 接入](https://intl.cloud.tencent.com/zh/document/product/1300/80632) |
| 百度千帆 | `qianfan` | `https://qianfan.baidubce.com/v2` | [接入说明](https://cloud.baidu.com/doc/RTC/s/0m9qgqj7h) |
| MiniMax | `minimax` | `https://api.minimax.cn/v1` | [OpenAI 兼容接口](https://platform.minimax.cn/docs/api-reference/text-openai-api) |

例如，使用预设时可省略 `url`：

```toml
[translation]
provider = "deepseek"
model = "your-provider-model"
api_key_env = "CLOUDSUB_TRANSLATION_KEY"
allow_remote = true
json_mode = true
```

切换服务商时，删除或注释旧 `url` 才会使用新预设；显式 `url` 始终覆盖默认地址。不同地区或账户的端点、密钥可能不互通，按控制台填写；豆包的 `model` 使用控制台允许的模型名或推理接入点 ID。混元预设使用 TokenHub，新旧平台凭据与模型权限分别确认；已有旧平台服务可显式覆盖为 `https://api.hunyuan.cloud.tencent.com/v1`。使用标准模型推理 API 的密钥，不要把仅限 Coding 订阅的密钥用于普通字幕翻译。

密钥交互输入，不写入 TOML 或命令历史。PowerShell 7：

```powershell
$env:CLOUDSUB_TRANSLATION_KEY = Read-Host 'Translation API key' -MaskInput
```

Bash：

```bash
read -rsp 'Translation API key: ' CLOUDSUB_TRANSLATION_KEY; echo
export CLOUDSUB_TRANSLATION_KEY
```

字幕正文会发送给所选外部服务商，可能包含敏感对话。费用、数据保留和处理规则以该服务商为准；本项目不保证外置服务的匿名性或保密性。密钥通过请求头发送，不放入 URL；翻译地址禁止 query、fragment 和内嵌凭据，请求不跟随重定向，也不读取代理环境变量。预设与协议模拟测试不代表已执行各家真实计费调用。

接口依据：[Ollama chat](https://docs.ollama.com/api/chat)、[LM Studio OpenAI 兼容接口](https://lmstudio.ai/docs/developer/openai-compat)、[OpenAI Chat API](https://platform.openai.com/docs/api-reference/chat/create)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[Gemini generateContent](https://ai.google.dev/api/generate-content)。识别安装与硬件要求见 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)。

## 使用

```sh
cloudsub list --config config.local.toml
cloudsub run --config config.local.toml --limit 1
cloudsub run --config config.local.toml --original-only
cloudsub run --config config.local.toml --id TASK_ID
```

`list` 默认显示任务 ID 与文件大小；显式加 `--show-paths` 才显示远端路径。`--id` 使用列表中的 ID，`--limit` 限制本次处理数量。递归扫描由配置中的 `recursive = true` 开启，不会自动轮询或创建系统定时任务。

默认结果在 `.local/subtitles`，以任务 ID 命名：`ID.original.srt` 为原文，`ID.zh.srt` 为简体中文。可用 `--output` 选择其他目录；请自行确保该目录不会被提交到 Git。

本地已有原文 SRT 时，重跑会复用它继续翻译；已有中文字幕也会复用。修改模型或源视频后，需要先把对应本地字幕移开再重跑。任务 ID 来自来源 URL、账号和路径，签名直链更换签名会生成新 ID；不同账号的同名资源使用独立缓存。成功或失败后都会清理下载的临时媒体；已生成的字幕保留。识别/翻译质量取决于音轨、语言与模型，字幕需要人工复核。

### 可选写回

先在空的测试目录确认服务端行为，再显式开启：

```sh
cloudsub run --config config.local.toml --limit 1 --upload
```

WebDAV 写回使用视频同目录的 `stem.original.srt` 和 `stem.zh.srt`，只生成原文时只写回原文。程序不删除或修改视频。上传使用 `If-None-Match: *`，并回读校验内容；已有字幕内容完全相同时视为成功，可重跑补齐上次失败的写回，不同则拒绝覆盖。写入后校验失败会保留本地结果，远端文件也不会自动删除。若网关忽略条件写入头，客户端不能保证它不会覆盖远端同名字幕，因此必须先验证服务端实现。HTTP 来源不能使用 `--upload`。

## 隐私与限制

- 真实 URL、认证信息、私人配置、字幕、媒体、模型、日志和数据库均不应进入公开仓库。默认输出用任务 ID，不用远端文件名。
- 错误信息不打印原始 URL、认证头、响应正文或字幕内容；跨来源下载重定向不携带认证与 Cookie，拒绝 HTTPS 降级。默认不读取系统代理环境变量。
- 单个媒体上限 20 GiB；WebDAV 单次 XML 上限 2 MiB、整次扫描最多 10,000 个条目；下载最多 5 次重定向。首版没有断点续传和自动重试。
- 20 GiB 是下载文件大小限制，不是识别内存上限。faster-whisper 会整段解码音频：每小时仅 float32 音频就约 220 MiB，识别与 VAD 还需额外内存；长录音请先分段处理。
- 翻译按每批 12 条请求，整份中文字幕完成后才保存。后续批次失败时重跑会重新请求此前批次，使用外置 API 时可能重复计费；可先用 `--limit 1` 验证模型与额度。
- 网盘连接与首次识别模型下载仍会联网；启用外置翻译后字幕正文也会出站。本地翻译服务需要由你确认实际使用本机模型、未启用云端转发。

发布前运行：

```sh
python -m unittest discover -s tests
python scripts/check_public.py
```

发布检查扫描 Git 已跟踪文件与未被忽略的未跟踪候选文件，采用路径白名单和敏感内容规则，不打印命中的值。它不扫描历史文件内容，也不能保证截图、Git 作者身份或账号关联完全匿名。详见 [隐私与安全说明](SECURITY.md)。

## 开源与适配

先完成本地审阅，再自行创建 GitHub 仓库与推送。提交前检查 `git diff --cached` 和发布检查结果；不要直接上传整个工作目录。尚未提交时可先选择公开署名与 GitHub noreply 邮箱；这只能减少邮箱公开，不能隐藏 GitHub 账号关联。

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，新来源的协议边界与测试要求见 [docs/adapters.md](docs/adapters.md)。本项目代码为 [MIT](LICENSE)；第三方依赖与模型按各自许可使用。
