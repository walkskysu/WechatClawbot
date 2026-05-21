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

## 配置说明

### server.conf（可选）

控制最近对话上下文的保留条数（默认 20 条）：

```ini
[server]
latest_chat_limit = 30
```

### agent/agent.md

LLM 的系统角色提示，可自由修改以调整 AI 的人设和行为。

### agent/tools.json & agent/tools.md

定义 LLM 可调用的工具集及其说明。内置工具：

| 工具 | 说明 |
|------|------|
| `read` | 读取文件内容（支持分页） |
| `list` | 列出目录内容（`ls -al` 风格） |
| `write` | 创建或覆盖文件 |
| `edit` | 精确文本替换编辑文件 |
| `exec` | 执行 Shell 命令 |
| `web_search` | 网络搜索（通过 OpenRouter 内置工具） |

## 注意事项

- `cred/wechat.json` 包含登录凭证，请加入 `.gitignore` 避免泄露
- `exec` 工具允许 LLM 执行任意命令，请确保只在可信环境中运行
- 网络搜索功能依赖 OpenRouter 的 `openrouter:web_search` 插件
