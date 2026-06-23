# 首次启动向导设计文档

> 日期: 2026-06-22 | 优先级: P2 | 范围: CLI 交互式配置向导

---

## 一、功能概述

新用户首次启动 MyCodeAgent 时，需要手动：
1. 创建 `.env` 文件
2. 配置 LLM provider + API key
3. 了解有哪些功能可以开启
4. 查阅文档了解 slash commands

首次启动向导将这些步骤自动化：检测首次运行 → 交互式引导配置 → 生成 `.env` → 显示快速入门。

---

## 二、触发条件

```
检测顺序:
1. .env 文件不存在 → 触发向导
2. .env 存在但 LLM_API_KEY 为空 → 触发向导
3. .env 存在且配置完整 → 跳过向导，正常启动
```

可通过 `--skip-wizard` 跳过，`--wizard` 强制触发。

---

## 三、向导流程

```
Step 1: 欢迎页面
  ┌─────────────────────────────────────────┐
  │  🐱 Welcome to MyCodeAgent!              │
  │                                          │
  │  Let's set up your environment.          │
  │  You can skip with --skip-wizard.        │
  │                                          │
  │  Press Enter to continue...              │
  └─────────────────────────────────────────┘

Step 2: LLM Provider 选择
  ┌─────────────────────────────────────────┐
  │  Choose your LLM provider:               │
  │                                           │
  │  1. DeepSeek (recommended)                │
  │  2. OpenAI                                │
  │  3. Zhipu (智谱)                          │
  │  4. Kimi (月之暗面)                        │
  │  5. Qwen (通义千问)                        │
  │  6. Ollama (local)                        │
  │  7. Custom (manual URL)                   │
  │                                           │
  │  Choice [1]:                              │
  └─────────────────────────────────────────┘

Step 3: API Key 输入
  ┌─────────────────────────────────────────┐
  │  Provider: DeepSeek                       │
  │  Base URL: https://api.deepseek.com       │
  │                                           │
  │  Enter your API key:                      │
  │  > sk-xxxxxxxxxxxxxxxxxxxx                │
  └─────────────────────────────────────────┘

Step 4: 功能开关（可选）
  ┌─────────────────────────────────────────┐
  │  Optional features (all can be changed    │
  │  later in .env):                          │
  │                                           │
  │  [y] Enable AgentTeams (multi-agent)      │
  │  [n] Enable MCP tools (web search, etc)   │
  │  [n] Enable Output Style switching        │
  │                                           │
  │  Press Enter to accept defaults...        │
  └─────────────────────────────────────────┘

Step 5: 确认 + 生成
  ┌─────────────────────────────────────────┐
  │  Configuration summary:                   │
  │                                           │
  │  LLM: DeepSeek (deepseek-v4-pro)          │
  │  API Key: sk-xxxx...c928c                 │
  │  AgentTeams: disabled                     │
  │  MCP: disabled                            │
  │                                           │
  │  Write to .env? [Y/n]                     │
  └─────────────────────────────────────────┘

Step 6: 完成
  ┌─────────────────────────────────────────┐
  │  ✅ Setup complete!                       │
  │                                           │
  │  Quick start:                             │
  │    - Type anything to chat with the agent │
  │    - /help to see all commands            │
  │    - /model to switch models              │
  │    - /sessions to manage conversations    │
  │    - init to generate CODE_LAW.md         │
  │                                           │
  │  Your .env has been created. Edit it      │
  │  anytime to change settings.              │
  └─────────────────────────────────────────┘
```

---

## 四、Provider 预设

```python
PROVIDER_PRESETS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-pro",
        "env_prefix": "DEEPSEEK",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "env_prefix": "OPENAI",
    },
    "zhipu": {
        "name": "Zhipu (智谱)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4",
        "env_prefix": "ZHIPU",
    },
    "kimi": {
        "name": "Kimi (月之暗面)",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-auto",
        "env_prefix": "KIMI",
    },
    "qwen": {
        "name": "Qwen (通义千问)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-max",
        "env_prefix": "DASHSCOPE",
    },
    "ollama": {
        "name": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3",
        "env_prefix": "OLLAMA",
    },
    "custom": {
        "name": "Custom",
        "base_url": "",
        "default_model": "",
        "env_prefix": "LLM",
    },
}
```

---

## 五、生成的 .env 模板

```bash
# ===== LLM Configuration =====
LLM_PROVIDER={provider}
LLM_API_KEY={api_key}
LLM_BASE_URL={base_url}
LLM_MODEL_ID={model}

# ===== Model Profiles =====
MODEL_PROFILES=main
MODEL_MAIN_ID={model}
MODEL_MAIN_PROVIDER={provider}
MODEL_MAIN_API_KEY={api_key}
MODEL_MAIN_BASE_URL={base_url}
MODEL_POINTER_MAIN=main

# ===== Features (change anytime) =====
# ENABLE_AGENT_TEAMS=false
# MCP_CONNECT_MODE=disabled
# AGENT_OUTPUT_STYLE=default

# ===== Advanced (usually leave as default) =====
# CONTEXT_WINDOW=128000
# COMPRESSION_THRESHOLD=0.8
# TRACE_ENABLED=true
```

---

## 六、实现

### 文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `scripts/first_run_wizard.py` | **新建** | 向导实现 (~250 行) |
| `scripts/chat_test_agent.py` | **修改** | 启动时检测并触发向导 (+15 行) |
| `tests/test_first_run_wizard.py` | **新建** | 向导测试 (~100 行) |

### 核心代码

```python
# scripts/first_run_wizard.py
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.table import Table

console = Console()

def run_wizard() -> bool:
    """Run interactive setup wizard. Returns True if .env was created."""
    _show_welcome()

    # Step 2: Provider
    provider = _choose_provider()

    # Step 3: API Key
    api_key = _prompt_api_key(provider)

    # Step 4: Features
    features = _choose_features()

    # Step 5: Confirm
    if not _confirm(provider, api_key, features):
        console.print("[yellow]Setup cancelled. Run with --wizard to try again.[/yellow]")
        return False

    # Step 6: Write .env
    _write_env(provider, api_key, features)
    _show_complete()
    return True


def should_run_wizard() -> bool:
    """Check if the wizard should be triggered."""
    from pathlib import Path
    env_path = Path(".env")
    if not env_path.exists():
        return True
    # Check if API key is configured
    content = env_path.read_text()
    if "LLM_API_KEY=" not in content and "DEEPSEEK_API_KEY=" not in content:
        # Has .env but no API key configured
        return True
    return False
```

---

## 七、CLI 集成

```bash
# 正常启动（自动检测）
python scripts/chat_test_agent.py

# 强制向导
python scripts/chat_test_agent.py --wizard

# 跳过向导
python scripts/chat_test_agent.py --skip-wizard
```

---

## 八、测试

| 测试 | 说明 |
|------|------|
| `test_should_run_when_no_env` | 无 .env → 应触发 |
| `test_should_run_when_no_api_key` | 有 .env 但无 API key → 应触发 |
| `test_should_skip_when_configured` | 配置完整 → 不触发 |
| `test_provider_presets_have_required_fields` | 所有预设包含必要字段 |
| `test_generated_env_contains_required_keys` | 生成的 .env 含必要配置 |
| `test_skip_wizard_flag` | --skip-wizard 跳过 |
| `test_force_wizard_flag` | --wizard 强制触发 |
