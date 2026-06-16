# WechatClawbot

一个基于 OpenAI 兼容 API 的微信个人号 AI 聊天机器人，支持工具调用（Agentic 循环）、对话历史持久化和网络搜索。

## 功能特性

- **微信登录**：通过二维码扫码登录微信个人号，凭证本地持久化
- **LLM 对话**：接入任意 OpenAI 兼容接口（如 OpenRouter），自动回复私聊消息
- **Agentic 工具循环**：LLM 可调用内置工具（读文件、写文件、列目录、执行命令、网络搜索）完成复杂任务
- **对话历史**：每轮对话追加到当日历史文件，并维护一个滑动窗口的最近上下文供下轮对话使用
- **详细日志**：每次 LLM 调用的完整请求/响应记录到带时间戳的日志文件

## 目录结构

```
WechatClawbot/
├── main.py              # 入口：微信登录 & 消息监听
├── llm.py               # LLM 客户端：工具循环、历史、日志
├── agent/
│   ├── agent.md         # 系统提示词（角色设定）
│   ├── tools.md         # 工具使用说明（注入系统提示）
│   └── tools.json       # 工具 Schema（OpenAI function calling 格式）
├── cred/
│   └── wechat.json      # 微信登录凭证（自动生成，请勿提交）
├── history/             # 对话历史（按日期归档 + latest_chat.json）
├── log/                 # LLM 调用日志
└── server.conf          # 可选配置（如 latest_chat_limit）
```

## 快速开始

### 1. 安装依赖

```bash
pip install openai httpx python-dotenv qrcode wechatbot
```

### 2. 配置环境变量

在项目根目录创建 `.env` 文件：

```env
LLM_API_KEY=你的API密钥
LLM_API_URL=https://openrouter.ai/api/v1   # 或其他 OpenAI 兼容地址
LLM_MODEL=openai/gpt-4o                    # 或你想使用的模型名称
```

### 3. 运行

```bash
python main.py
```

启动后终端会打印二维码，用微信扫码登录即可。登录后机器人开始自动回复私聊消息。

## 机器A/机器B 代理部署（推荐）

目标：让机器A运行 `main.py` 时，所有微信 API/CDN 请求都先到机器B，再由机器B转发出去。

### 1. 在机器B启动代理

在机器B的项目目录执行：

```bash
python proxy_server.py --host 0.0.0.0 --port 18080
```

可选环境变量（机器B）：

- `PROXY_ALLOW_HOST_SUFFIXES`：允许转发的目标域名后缀，默认 `weixin.qq.com`
- `PROXY_MAX_BODY_MB`：请求体大小上限（默认 200MB）
- `PROXY_TIMEOUT_SECONDS`：上游请求超时（默认 600 秒）

### 2. 在机器A配置走机器B代理

机器A在 `server.conf` 里配置 `[proxy]`：

```ini
[proxy]
enabled = true
base_url = http://<机器B_IP>:18080
```

然后在机器A直接运行：

python main.py

````

兼容说明：如果 `[proxy].enabled=false`，程序会回退到环境变量 `WECHAT_PROXY_BASE_URL`（若已设置）。

生效后，以下链路都会经由机器B：

- 登录/收消息/发消息等 iLink API 请求
- 文件上传到微信 CDN
- 媒体文件下载（图片、语音、视频、文件）

### 3. 连通性检查

机器A先验证机器B代理健康状态：

```bash
curl http://<机器B_IP>:18080/healthz
````

返回 `{"ok": true}` 即表示代理服务正常。

## 配置说明

### server.conf（可选）

控制最近对话上下文的保留条数（默认 20 条），以及工具/技能调用上限：

```ini
[server]
latest_chat_limit = 20

[agent]
# 每轮最多允许 LLM 批量调用多少次工具（每次完整 tool_calls 批次后自动重置，支持多轮循环）
max_tool_calls = 20
# 每轮最多允许读取多少次 SKILL.md（技能说明）
max_skill_reads = 5
```

### agent/agent.md

LLM 的系统角色提示，可自由修改以调整 AI 的人设和行为。

### agent/tools.json & agent/tools.md

定义 LLM 可调用的工具集及其说明。内置工具：

| 工具         | 说明                                 |
| ------------ | ------------------------------------ |
| `read`       | 读取文件内容（支持分页）             |
| `list`       | 列出目录内容（`ls -al` 风格）        |
| `write`      | 创建或覆盖文件                       |
| `edit`       | 精确文本替换编辑文件                 |
| `exec`       | 执行 Shell 命令                      |
| `web_search` | 网络搜索（通过 OpenRouter 内置工具） |

## 注意事项

- `cred/wechat.json` 包含登录凭证，请加入 `.gitignore` 避免泄露
- `exec` 工具允许 LLM 执行任意命令，请确保只在可信环境中运行
- 网络搜索功能依赖 OpenRouter 的 `openrouter:web_search` 插件

## 技能系统（skills 自动注入与同步）

- **系统提示自动注入**：每次新对话，`agent/skills.md` 和 `agent/available_skills.xml` 会自动注入到 LLM 的 system prompt，确保技能选择和调用规则始终生效。
- **技能目录自动同步**：每次启动时会自动扫描 `skills/` 目录下所有子文件夹，读取每个技能的 `SKILL.md` 前置元数据（name/description），并自动生成/更新 `agent/available_skills.xml`，无需手动维护。
- **调用上限可配置且自动重置**：`server.conf` 可配置 tools/skills 调用上限，每次完整的 tools 调用批次后计数器自动重置，支持多轮复杂推理。

- `cred/wechat.json` 包含登录凭证，请加入 `.gitignore` 避免泄露
- `exec` 工具允许 LLM 执行任意命令，请确保只在可信环境中运行
- 网络搜索功能依赖 OpenRouter 的 `openrouter:web_search` 插件
