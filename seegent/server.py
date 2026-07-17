#!/usr/bin/env python3
"""Seegent local server — serves static files, persists state, proxies LLM calls."""

import json
import os
import re
import hashlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import glob
import datetime
import sqlite3
import urllib.request
import urllib.error
import urllib.parse
import html
import ssl
from http.server import HTTPServer, SimpleHTTPRequestHandler

try:
    import numpy as np
except ImportError:
    np = None  # 语义检索降级：numpy 不可用时禁用

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False  # 进程资源监控降级：不可用时回退 ps 命令

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')


def _get_data_dir():
    """数据目录：pip 安装 → ~/.seegent/；源码运行 → 项目根目录。"""
    env = os.environ.get('SEEGENT_DATA_DIR')
    if env:
        return os.path.expanduser(env)
    if 'site-packages' in __file__ or 'dist-packages' in __file__:
        return os.path.join(os.path.expanduser('~'), '.seegent')
    # 源码运行 — server.py 在 seegent/ 下，上溯一级到项目根
    return os.path.dirname(BASE_DIR)


DATA_DIR = _get_data_dir()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
STATE_FILE = os.path.join(DATA_DIR, '.seegent-state.json')
MODELS_FILE = os.path.join(DATA_DIR, '.seegent-models.json')
ENGINES_FILE = os.path.join(DATA_DIR, '.seegent-engines.json')
CLI_FILE = os.path.join(DATA_DIR, '.seegent-cli.json')
MCP_FILE = os.path.join(DATA_DIR, '.seegent-mcp.json')
CHAT_FILE = os.path.join(DATA_DIR, '.seegent-chats.json')
PROMPTS_FILE = os.path.join(DATA_DIR, '.seegent-prompts.json')
WORKSPACES_FILE = os.path.join(DATA_DIR, '.seegent-workspaces.json')
SKILLS_FILE = os.path.join(DATA_DIR, '.seegent-skills.json')
ROLES_FILE = os.path.join(DATA_DIR, '.seegent-roles.json')
EMBEDDING_FILE = os.path.join(DATA_DIR, '.seegent-embedding.json')
INDEX_DB = os.path.join(DATA_DIR, '.seegent-index.db')
LOG_FILE = os.path.join(DATA_DIR, '.seegent-logs.json')
TIMING_FILE = os.path.join(DATA_DIR, '.seegent-timing.txt')
USAGE_FILE = os.path.join(DATA_DIR, '.seegent-usage.json')
TRACKS_FILE = os.path.join(DATA_DIR, '.seegent-tracks.json')
PROJECTS_FILE = os.path.join(DATA_DIR, '.seegent-projects.json')

# ===== 飞书凭证配置 =====
FEISHU_CREDENTIALS_FILE = os.path.join(DATA_DIR, '.seegent-feishu-credentials.json')

# ===== 数据源注册表 =====
DATASOURCES_FILE = os.path.join(DATA_DIR, '.seegent-datasources.json')

# ===== 技能库浏览器索引 =====
SKILLLIB_INDEX_FILE = os.path.join(DATA_DIR, '.seegent-skill-index.json')

# ===== 团队 / 技能 模块可手动覆盖的文件夹路径（仅本人使用）=====
PATHS_FILE = os.path.join(DATA_DIR, '.seegent-paths.json')

# ===== 看板配置（按项目隔离）=====
DASHBOARD_DIR = os.path.join(DATA_DIR, 'dashboards')
DEFAULT_DASHBOARD_CONFIG = {
    'projectName': '',
    'projectPath': '',
    'datasources': [],
    'cards': [],
    'analyses': []
}

HOST = '127.0.0.1'
PORT = 8765

# Agent 文件变更追踪：{workspace_path: {relative_path: timestamp}}
_file_changes = {}
CHANGES_TTL = 3600  # 红点 1 小时后自动过期

# 文本类文件后缀白名单（索引/search 共用）
TEXT_EXTS = {'.md','.txt','.markdown','.html','.htm','.json','.js','.ts','.jsx','.tsx',
             '.py','.rb','.go','.rs','.java','.c','.cpp','.h','.hpp','.css','.scss',
             '.yaml','.yml','.xml','.csv','.tsv','.log','.ini','.toml','.sh','.bat',
             '.sql','.vue','.svelte','.swift','.kt','.php','.r','.scala','.dart',
             '.pdf','.docx','.doc'}

# 图片类文件后缀（OCR 索引用）
IMAGE_EXTS = {'.png','.jpg','.jpeg','.gif','.webp','.bmp','.tiff','.tif'}

# 尝试导入 OCR 库
try:
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = bool(shutil.which('tesseract'))
except ImportError:
    _OCR_AVAILABLE = False

# 尝试导入 PDF/Word 解析库
try:
    import pdfplumber
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

try:
    import docx
    _DOCX_AVAILABLE = True
except ImportError:
    _DOCX_AVAILABLE = False

# Default CLI registry — what CLIs Seegent knows about
DEFAULT_CLI = {
    "cursor-agent": {
        "name": "Cursor Agent",
        "path": "/Applications/Cursor.app/Contents/Resources/app/bin/cursor agent",
        "description": "Cursor 编辑器的 AI 编程代理，支持 print 模式和交互模式。安装 Cursor 编辑器后可用。",
        "tutorial": "## 使用方式\n\n**Print 模式（推荐）：**\n```bash\ncursor agent -p \"你的任务\" --workspace ~/project\n```\n\n**交互模式：**\n```bash\ncursor agent \"你的任务\"\n```\n\n**在 Seegent 中调用：**\n在聊天中说「用 Cursor Agent 帮我写一个 xxx」，主 Agent 会通过 run_shell 工具执行 cursor 命令。"
    },
    "hermes-agent": {
        "name": "Hermes Agent",
        "path": "hermes",
        "description": "多平台 AI 代理，支持 20+ 大模型提供商，消息平台网关。",
        "tutorial": "## 使用方式\n\n**单次查询：**\n```bash\nhermes chat -q \"你的问题\"\n```\n\n**交互模式：**\n```bash\nhermes\n```\n\n**在 Seegent 中调用：**\n在聊天中说「用 Hermes 帮我查 xxx」，主 Agent 会通过 run_shell 工具执行 hermes 命令。"
    },
    "claude-code": {
        "name": "Claude Code",
        "path": "claude",
        "description": "Anthropic 官方 CLI AI 编程助手，完整的代码理解、重构、调试能力，支持多文件编辑。",
        "tutorial": "## 使用方式\n\n**单次任务（print 模式）：**\n```bash\nclaude -p \"重构这个文件\" --workspace ~/project\n```\n\n**交互模式：**\n```bash\nclaude\n```\n\n**安装：**\n```bash\nnpm install -g @anthropic-ai/claude-code\n```\n\n**在 Seegent 中调用：**\n在聊天中说「让 Claude Code 帮我 xxx」，主 Agent 会通过 run_shell 执行 claude 命令。"
    },
    "workbuddy": {
        "name": "WorkBuddy",
        "path": "node workbuddy-sidecar/server.js",
        "description": "腾讯 AI Agent SDK 侧车，已桥接到 Seegent。启动侧车后，在模型选择器选「WorkBuddy Agent」使用。",
        "tutorial": "## 架构\n\nWorkBuddy 是独立 Node.js 进程，通过 SSE 桥接到 Seegent。\n\n## 启动方式\n\n```bash\ncd workbuddy-sidecar\nnpm install\nnode server.js\n```\n\n## 在 Seegent 中调用\n\n**方式一（直接）：** 聊天窗口模型选择器选「WorkBuddy Agent」，直接对话。\n**方式二（调度）：** 在聊天中说「让 WorkBuddy 帮我 xxx」。"
    }
}

# Default MCP registry
DEFAULT_MCP = {
    "fetch": {
        "name": "网页获取",
        "icon": "🌐",
        "description": "获取网页内容、搜索信息、提取数据",
        "command": "npx -y @modelcontextprotocol/server-fetch",
        "tutorial": "## 功能\n\n- 获取网页内容\n- 网页搜索\n\n**在 Seegent 中：** 在聊天窗口直接问，我会用 web_search 工具帮你查。"
    },
    "feishu": {
        "name": "飞书",
        "icon": "🐦",
        "description": "飞书官方 OpenAPI MCP——发送消息、创建文档、管理日历、通讯录等，需配置应用凭证",
        "command": "npx -y @larksuiteoapi/lark-mcp --app-id <your_app_id> --app-secret <your_app_secret>",
        "tutorial": "## 连接飞书\n\n飞书官方 MCP 服务器（`@larksuiteoapi/lark-mcp`），连接后 AI 可真实操作飞书：发消息、建文档、读通讯录、管日历等。\n\n### 配置步骤\n1. 前往 [飞书开放平台](https://open.feishu.cn/) 创建一个**自建应用**\n2. 在「凭证与基础信息」中复制 **App ID**（形如 `cli_xxxxxxxx`）和 **App Secret**\n3. 在「权限管理」中按需开启权限范围（如发消息需 `im:message`、建文档需 `docx:document` 等）\n4. 编辑上方「启动命令」，把 `<your_app_id>` 和 `<your_app_secret>` 替换为真实凭证\n5. 保存后点「🔍 发现工具」，状态灯变绿即连接成功\n\n### 常见工具（连接成功后 AI 可自动调用）\n- `send_message` / `create_message` — 发送消息\n- `create_doc` / `create_document` — 创建云文档\n- `list_users` / `get_user` — 通讯录查询\n- `create_event` — 创建日历事件\n\n> App Secret 是敏感信息，仅保存在本地 `.seegent-mcp.json`（已被 `.gitignore` 忽略），绝不上传。"
    },
    "wecom": {
        "name": "企业微信",
        "icon": "💼",
        "description": "企业微信官方 MCP——发送消息、管理通讯录、客户联系、会话存档等，需配置企业凭证",
        "command": "npx -y @anthropic/mcp-server-wecom --corp-id <your_corp_id> --corp-secret <your_corp_secret>",
        "tutorial": "## 连接企业微信\n\n企业微信 MCP 服务器，连接后 AI 可真实操作企业微信：发消息、查通讯录、管理客户等。\n\n### 配置步骤\n1. 前往 [企业微信管理后台](https://work.weixin.qq.com/) 登录管理员账号\n2. 在「我的企业」→「企业信息」底部复制 **企业 ID**（Corp ID，形如 `wwxxxxxxxxxxxxxxxx`）\n3. 在「应用管理」→「自建」中创建一个**自建应用**\n4. 在自建应用的详情页复制 **Secret**（Corp Secret）\n5. 在「企业微信授权配置」中设置可信域名和授权回调\n6. 编辑上方「启动命令」，把 `<your_corp_id>` 和 `<your_corp_secret>` 替换为真实凭证\n7. 保存后点「🔍 发现工具」，状态灯变绿即连接成功\n\n### 常见工具（连接成功后 AI 可自动调用）\n- `send_message` — 发送应用消息（文本/图文/卡片等）\n- `list_users` / `get_user` — 通讯录查询\n- `list_departments` — 部门管理\n- `create_group` — 创建群聊\n- `external_contact` — 客户联系管理\n\n> Corp Secret 是敏感信息，仅保存在本地 `.seegent-mcp.json`（已被 `.gitignore` 忽略），绝不上传。"
    }
}

# ===== MCP Client (JSON-RPC over stdio) =====
class McpClient:
    """Manages a single MCP server process via stdio JSON-RPC."""

    def __init__(self, server_id, command):
        self.server_id = server_id
        self.command = command
        self.process = None
        self.lock = threading.RLock()
        self._initialized = False
        self._tools = []
        self._req_id = 0
        self._pending = {}  # req_id -> (event, result_container)
        self._reader_thread = None
        self._stop_reader = False

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    def start(self):
        """Start the MCP server subprocess."""
        with self.lock:
            if self.process and self.process.poll() is None:
                return True  # Already running
            try:
                # Parse command: split by spaces, but respect quotes
                import shlex
                cmd_parts = shlex.split(self.command)
                self.process = subprocess.Popen(
                    cmd_parts,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=False,
                    bufsize=0,
                )
                self._stop_reader = False
                self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
                self._reader_thread.start()
                # Wait a few seconds for process to start (npx may need download time)
                time.sleep(3)
                if self.process.poll() is not None:
                    return f'进程启动失败（退出码：{self.process.returncode}）'
                return True
            except Exception as e:
                return f'启动失败：{e}'

    def _read_loop(self):
        """Background thread: read JSON-RPC responses from stdout."""
        while not self._stop_reader and self.process and self.process.stdout:
            try:
                line = self.process.stdout.readline()
                if not line:
                    break
                line = line.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                msg = json.loads(line)
                msg_id = msg.get('id')
                if msg_id is not None and msg_id in self._pending:
                    evt, container = self._pending[msg_id]
                    container['result'] = msg
                    evt.set()
            except (json.JSONDecodeError, Exception):
                continue

    def _send_request(self, method, params, timeout=30):
        """Send a JSON-RPC request and wait for response."""
        with self.lock:
            if not self.process or self.process.poll() is not None:
                err = self.start()
                if err is not True:
                    return {'error': err}

            req_id = self._next_id()
            req = {
                'jsonrpc': '2.0',
                'id': req_id,
                'method': method,
                'params': params
            }
            evt = threading.Event()
            container = {}
            self._pending[req_id] = (evt, container)

            try:
                data = json.dumps(req) + '\n'
                self.process.stdin.write(data.encode('utf-8'))
                self.process.stdin.flush()
            except Exception as e:
                del self._pending[req_id]
                return {'error': f'发送请求失败：{e}'}

        # Wait for response outside lock
        if not evt.wait(timeout=timeout):
            with self.lock:
                self._pending.pop(req_id, None)
            return {'error': f'请求超时（{timeout}秒）'}

        with self.lock:
            self._pending.pop(req_id, None)

        result = container.get('result', {})
        if 'error' in result:
            return {'error': result['error']}
        return result.get('result', {})

    def initialize(self):
        """Perform MCP initialize handshake."""
        if self._initialized:
            return True
        result = self._send_request('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'Seegent', 'version': '1.0.0'}
        }, timeout=60)
        if 'error' in result:
            return result['error']
        # Send initialized notification
        try:
            notify = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n'
            self.process.stdin.write(notify.encode('utf-8'))
            self.process.stdin.flush()
        except Exception:
            pass
        self._initialized = True
        return True

    def list_tools(self):
        """Discover tools from this MCP server."""
        init_result = self.initialize()
        if init_result is not True:
            return {'error': init_result}
        result = self._send_request('tools/list', {}, timeout=15)
        if 'error' in result:
            return result
        tools = result.get('tools', [])
        self._tools = tools
        return {'tools': tools}

    def call_tool(self, name, arguments):
        """Call a tool on this MCP server."""
        init_result = self.initialize()
        if init_result is not True:
            return f'MCP 初始化失败：{init_result}'
        result = self._send_request('tools/call', {
            'name': name,
            'arguments': arguments
        }, timeout=60)
        if 'error' in result:
            err = result['error']
            if isinstance(err, dict):
                return f'MCP 工具调用错误：{err.get("message", err)}'
            return f'MCP 工具调用错误：{err}'
        # Extract content from result
        content = result.get('content', [])
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                texts.append(item.get('text', ''))
        if texts:
            return '\n'.join(texts)
        return json.dumps(result, ensure_ascii=False)

    def get_status(self):
        """Return current status."""
        running = self.process is not None and self.process.poll() is None
        return {
            'running': running,
            'initialized': self._initialized,
            'tool_count': len(self._tools)
        }

    def stop(self):
        """Stop the subprocess."""
        self._stop_reader = True
        self._initialized = False
        self._tools = []
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None


# Global MCP client registry: {server_id: McpClient}
_mcp_clients = {}
_mcp_clients_lock = threading.Lock()
_MCP_TOOLS_CACHE = {}  # {'key': config_hash, 'ts': timestamp, 'results': {...}}


def _get_mcp_client(server_id, command):
    """Get or create an McpClient for the given server."""
    with _mcp_clients_lock:
        client = _mcp_clients.get(server_id)
        if client and client.process and client.process.poll() is None:
            return client
        client = McpClient(server_id, command)
        _mcp_clients[server_id] = client
        return client


def _discover_mcp_tools(enabled_servers):
    """Discover tools from all enabled MCP servers.
    Returns {server_id: {'tools': [...], 'error': ...}}"""
    results = {}
    for sid, cfg in enabled_servers.items():
        cmd = cfg.get('command', '')
        if not cmd:
            results[sid] = {'error': '没有配置启动命令'}
            continue
        client = _get_mcp_client(sid, cmd)
        result = client.list_tools()
        results[sid] = result
    return results


def _call_mcp_tool(server_id, tool_name, arguments):
    """Call a tool on a specific MCP server."""
    with _mcp_clients_lock:
        client = _mcp_clients.get(server_id)
    if not client:
        return f'MCP server "{server_id}" 未连接'
    return client.call_tool(tool_name, arguments)


# Default chat history
DEFAULT_CHATS = {
    "chats": []
}

# Default system prompts
DEFAULT_PROMPTS = {
    "prompts": [
        {"id": "none",      "name": "无模板",     "prompt": ""},
        {"id": "code",      "name": "帮我写代码", "prompt": "你是一个资深的软件工程师。请根据用户提供的上下文和需求，写出高质量、可运行的代码。使用中文回复，代码部分保持英文。"},
        {"id": "translate", "name": "翻译文档",   "prompt": "你是一个专业的技术文档翻译。请将用户提供的内容翻译成中文，保留代码块和技术术语的原文，确保翻译准确流畅。"},
        {"id": "summary",   "name": "总结内容",   "prompt": "你是一个高效的文档分析助手。请用简洁的中文总结用户提供的内容要点，使用分条列举的方式，突出关键信息。"},
        {"id": "explain",   "name": "解释概念",   "prompt": "你是一个耐心的技术导师。请用通俗易懂的中文解释用户提出的概念或代码，从基础到深入，逐步展开。"},
        {"id": "review",    "name": "代码审查",   "prompt": "你是一个严格的代码审查员。请审查用户提供的代码，指出潜在问题、性能瓶颈、安全隐患，并给出改进建议。使用中文回复。"},
        {"id": "write",     "name": "写作助手",   "prompt": "你是一个优秀的中文写作助手。请帮助用户润色、改写或创作内容，保持原意，提升表达质量。"},
        {"id": "custom",    "name": "自定义提示", "prompt": ""}
    ]
}

DEFAULT_WORKSPACES = {"workspaces": {}}

# Default Skills registry — installable SKILL.md workflow guides
DEFAULT_SKILLS = {
    "skills": [
        {
            "id": "xhs-note",
            "name": "小红书教育笔记创作",
            "description": "教育类小红书图文与选题工作流：受众痛点、标题钩子、正文结构、配图脚本、合规检查",
            "enabled": True,
            "content": "## 小红书教育笔记创作 Skill\n\n### 触发条件\n用户要写小红书教育类图文、选题、标题，或梳理教育内容方向时使用。\n\n### 工作流程\n1. **定受众与痛点**\n   - 明确给谁看：幼儿园老师 / 家长 / 教育从业者\n   - 抓一个真实痛点（不是编的），例如孩子数感差怎么启蒙、家长不会陪玩数学\n2. **选题与热点**\n   - 结合近期教育热点、节气、开学季等时间节点\n   - 必要时调用「小红书教育素材官」专家整理竞品与素材，结果归入知识库 Pro 文件夹\n3. **标题钩子（三种结构选一）**\n   - 人群+痛点+数字：3个方法，让娃主动数数\n   - 反常识：越早教认字，反而越怕数学\n   - 场景共鸣：幼儿园老师私下都在用的数感游戏\n4. **正文结构**\n   - 开头三行：钩子 + 你能得到什么\n   - 正文：每条一个方法，配具体操作步骤，段落短\n   - 结尾：总结 + 引导互动（提问或评论区话题）\n5. **配图建议**：给出每屏图文案与画面描述（不替你生成图，只给脚本）\n6. **合规检查**\n   - [ ] 不编造数据、不夸大效果\n   - [ ] 引用观点标注来源（作者 / 出处 / 年份）\n   - [ ] 不硬广、不营销\n\n### 输出格式\n先给 3 个候选标题，再给完整正文（分屏图文案 + 配图脚本），最后给 5 个话题标签。"
        },
        {
            "id": "kb-article",
            "name": "知识库教材文章写作",
            "description": "按「一本书」工程标准写教材级文章：六节模板、真实来源标注、科学诚实声明",
            "enabled": True,
            "content": "## 知识库教材文章写作 Skill\n\n### 触发条件\n用户要在知识库 Pro 文件夹写篇章、教材级文章，或整理成「一本书」的某个章节时使用。\n\n### 读者与定位\n先确认读者是哪类：幼儿园教师 / 家长 / 教育行业从业者（管理者、教研、出版、培训）。三类人深浅不同，写法不同。\n\n### 六节固定模板（每篇必须包含）\n1. **概念界定**：这个主题到底是什么，用大白话讲清，不堆术语\n2. **理论依据**：为什么有效，引用可核实的研究或理论，标注作者与年份\n3. **能力发展**：对孩子（3-6 岁）具体发展哪方面能力\n4. **活动示例**：2-3 个可直接落地的游戏或活动，写清材料、步骤、年龄适配\n5. **常见误区**：家长和老师最容易踩的坑，逐个点破\n6. **延伸资源**：推荐进一步阅读或工具，标注来源\n\n### 硬性规则（不可违反）\n- 真实来源标注：引用必须给作者、年份、可核实出处；查不到的一律不写\n- 科学诚实声明：不确定的、有争议的结论要明确说「目前研究尚无定论」\n- 可疑引用必须联网核实，严禁编造、严禁营销腔\n- 语言通俗，不写翻译腔，少用学术黑话\n\n### 输出格式\n按六节模板输出；文末附「来源清单」与「诚实声明」两段。"
        },
        {
            "id": "weekly-review",
            "name": "多项目周报与复盘",
            "description": "并行跟踪 Learn Chinese / PartnerFM / 场景识字 三个项目，产出周报与复盘",
            "enabled": True,
            "content": "## 多项目周报与复盘 Skill\n\n### 触发条件\n用户要整理本周进展、写周报、做项目复盘，或并行跟踪多个项目时使用。\n\n### 项目清单（固定三个，按需启用）\n- Learn Chinese：面向海外用户的汉字学习 App（Cloudflare 全栈，Paddle 支付）\n- PartnerFM：多人在线聊天智能体工作站\n- 场景识字工具：PWA 架构幼儿教育 App\n\n### 工作流程\n1. **逐项目梳理**，每个项目填：\n   - 本周完成\n   - 进行中\n   - 阻塞 / 风险（含需要决策的点）\n2. **跨项目依赖**：有没有一个项目的产出卡住另一个\n3. **关键决策记录**：本周做了什么方向性决定、为什么\n4. **下周计划**：每项目 1-3 条，标注优先级（高 / 中 / 低）\n5. **成本与资源**：涉及花钱或花时间的事单独列（用户偏好免费或低成本方案）\n\n### 输出格式\n先给一页总览（三项目状态表），再分项目展开，最后给下周计划清单。"
        }
    ]
}

DEFAULT_ROLES = {
    "roles": [
        {"id":"none","name":"通用助手","icon":"🔄","category":"通用",
         "description":"不限定角色，AI 根据对话内容灵活响应",
         "prompt":""},

        # ===== 内容创作 =====
        {"id":"copywriter","name":"文案写手","icon":"✍️","category":"内容创作",
         "description":"小红书/公众号/视频脚本、营销文案、品牌故事",
         "prompt":"你是专业的中文创作者。擅长小红书图文、公众号长文、短视频脚本、营销文案。\n写作原则：\n- 标题有钩子，前三行决定用户是否读下去\n- 金字塔结构，段落简短\n- 数据+案例支撑观点\n- 结尾给出行动建议或情绪共鸣\n- 根据平台调整语气（小红书活泼、公众号深度、视频口语化）"},

        {"id":"tutor","name":"教程讲师","icon":"📖","category":"内容创作",
         "description":"把复杂概念拆成教学大纲、逐字稿、PPT 结构",
         "prompt":"你是专业的教育内容设计师。擅长：\n- 复杂概念的拆解和通俗化\n- 教学大纲设计（目标→知识点→练习→检验）\n- 视频逐字稿（口语化、有节奏感）\n- PPT 结构（一页一个核心观点）\n- 互动问题设计（激发思考）\n\n设计原则：\n- 先给「学完你能做什么」\n- 用类比降低认知门槛\n- 每次只讲一个核心概念\n- 穿插练习巩固记忆\n- 中文授课，专业术语保留英文"},

        {"id":"translate","name":"翻译润色","icon":"🌍","category":"内容创作",
         "description":"中英日韩互译，技术文档、商业文书、文学内容",
         "prompt":"你是专业翻译。技术文档保留代码和术语原文；商业文书准确流畅；文学内容传达风格。先给译文，必要时加注释说明术语选择。发现原文歧义主动提醒。"},

        # ===== 知识整理 =====
        {"id":"knowledge-editor","name":"知识库编辑","icon":"📚","category":"知识整理",
         "description":"把零散信息整理成结构化文档，加标签、做摘要、建链接",
         "prompt":"你是知识管理专家。擅长：\n- 把零散笔记整理成结构化文档\n- 自动提取关键词和标签\n- 建立文档间的交叉引用\n- 写摘要和 TL;DR\n- 识别知识缺口\n\n整理原则：\n- 一个文档只讲一个主题\n- 金字塔结构（结论先行）\n- 善用表格和列表\n- 标注信息来源和可信度\n- 结尾给出「延伸阅读」建议"},

        {"id":"reader","name":"阅读助理","icon":"👁️","category":"知识整理",
         "description":"读长文/PDF 后做要点提炼、批判性提问、知识关联",
         "prompt":"你是深度阅读助理。拿到一篇文章后：\n1. 一句话概括核心观点\n2. 提取 3-5 个关键论点\n3. 标注文中的数据和引用\n4. 提出 2-3 个批判性问题\n5. 关联已有知识（如果用户提供了上下文）\n\n输出格式：\n## 一句话总结\n## 核心论点\n## 关键数据\n## 值得追问的问题\n## 延伸思考\n\n中文输出，保持客观。"},

        {"id":"data-analysis","name":"数据解读","icon":"📊","category":"知识整理",
         "description":"数据洞察、趋势分析、报表解读、可视化建议",
         "prompt":"你是数据分析师。技能：SQL/Python 数据分析、统计方法、可视化设计、商业分析（漏斗/留存/归因）。\n分析流程：理解数据结构→清洗→探索性分析→深度洞察→可执行建议。\n用数据说话，给出具体数字和百分比。"},

        # ===== 自媒体运营 =====
        {"id":"topic-planner","name":"选题策划","icon":"🎯","category":"自媒体运营",
         "description":"根据知识库内容出选题方案，匹配热点，规划内容日历",
         "prompt":"你是自媒体选题策划师。根据用户提供的领域和素材：\n1. 出 5-10 个选题（含标题和角度）\n2. 标注每个选题的流量潜力（🔴爆款 🟡常规 🔵长尾）\n3. 匹配当前热点话题\n4. 规划发布节奏（内容日历）\n5. 给出每个选题的差异化角度\n\n选题原则：\n- 痛点 + 解决方案 = 高打开率\n- 反常识观点 = 高互动率\n- 实用教程 = 高收藏率\n- 情绪共鸣 = 高转播率"},

        {"id":"viral-optimizer","name":"爆款优化","icon":"🔥","category":"自媒体运营",
         "description":"改标题、改钩子、改结尾，提升完读率和互动率",
         "prompt":"你是内容优化师，专攻小红书和公众号爆款。优化维度：\n\n**标题**\n- 数字+痛点+承诺（例：3 个方法，让你的文案转化率翻倍）\n- 反常识+好奇心（例：为什么你越努力，流量越差）\n- 人群标签+场景（例：30 岁转行 AI，我的真实经历）\n\n**开头（钩子）**\n- 前三行决定读者是否继续\n- 痛点共鸣 / 反常识观点 / 悬念提问\n\n**正文**\n- 段落不超过 3 行\n- 每段一个核心信息\n- 用 emoji 和短句增加节奏感\n\n**结尾**\n- 总结核心观点\n- 引导互动（提问/投票/评论区话题）\n\n优化时指出具体问题并给出改写版本。"},

        {"id":"custom","name":"自定义","icon":"⚙️","category":"通用",
         "description":"用户自定义系统提示词",
         "prompt":""}
    ],
    "activeRole": ""
}



# ===== 角色系统：内置角色已弃用，聊天角色改从「个人团队文件夹」(拾元/syteam) 动态读取 =====
# DEFAULT_ROLES 仅作历史兼容占位，不再作为角色来源（见 _handle_roles_get / _team_roles）。
BUILTIN_ROLE_IDS = {
    'none', 'custom', 'copywriter', 'tutor', 'translate',
    'knowledge-editor', 'reader', 'data-analysis', 'topic-planner', 'viral-optimizer'
}


def _preset_roles():
    """聊天角色的两个兜底：通用助手（不限定角色）/ 自定义（用户自填提示词）。"""
    return [
        {"id": "none", "name": "通用助手", "icon": "🔄", "category": "通用",
         "description": "不限定角色，AI 根据对话内容灵活响应", "prompt": ""},
        {"id": "custom", "name": "自定义", "icon": "⚙️", "category": "通用",
         "description": "用户自定义系统提示词", "prompt": ""},
    ]


def _team_roles():
    """把拾元/syteam 下的角色 .md 映射成聊天角色格式（content 作为系统提示词注入）。"""
    out = []
    try:
        src = _scan_team_source(_team_path())
    except Exception:
        return out
    for g in src.get('groups', []):
        gname = g.get('name') or '团队'
        for m in g.get('members', []):
            out.append({
                "id": m.get('id'),
                "name": m.get('name'),
                "icon": m.get('emoji') or '👤',
                "category": gname,
                "description": m.get('description') or m.get('title') or '',
                "prompt": m.get('content') or '',
                "isTeam": True,
            })
    return out


def _handle_roles_get():
    """合并：兜底角色 + 团队文件夹角色 + 用户自定角色（过滤掉已移除的内置角色）。"""
    try:
        data = _load_json(ROLES_FILE, {"roles": _preset_roles(), "activeRole": ""})
    except Exception:
        data = {"roles": _preset_roles(), "activeRole": ""}
    if not isinstance(data, dict):
        data = {"roles": _preset_roles(), "activeRole": ""}
    saved = data.get('roles', []) or []
    user_custom = [
        r for r in saved
        if isinstance(r, dict) and r.get('id') not in BUILTIN_ROLE_IDS and not r.get('isTeam')
    ]
    roles = _preset_roles() + _team_roles() + user_custom
    active = data.get('activeRole', '') or ''
    if active and not any(r.get('id') == active for r in roles):
        active = ''
    return {"roles": roles, "activeRole": active}


def _load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _save_json(path, default)
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===== 技能库浏览器（Skill Library）=====

def _parse_simple_yaml(lines):
    """极简 YAML 子集解析（容错，不抛异常）。支持标量、列表、| / > 多行块。"""
    result = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        m = re.match(r'^([A-Za-z0-9_\-]+):\s*(.*)$', line)
        if not m:
            i += 1
            continue
        key = m.group(1)
        val = m.group(2).rstrip()
        # 空值：可能是列表（- 项）或块标量（缩进行）
        if val == '' or val.startswith('|') or val.startswith('>'):
            j = i + 1
            while j < n and lines[j].strip() == '':
                j += 1
            if j < n and re.match(r'^\s*-\s+\S', lines[j]):
                lst = []
                while j < n and re.match(r'^\s*-\s+', lines[j]):
                    item = re.sub(r'^\s*-\s+', '', lines[j]).strip().strip('"').strip("'")
                    lst.append(item)
                    j += 1
                result[key] = lst
                i = j
                continue
            # 块标量：收集后续缩进行
            block = []
            k = i + 1
            while k < n:
                bl = lines[k]
                if bl.strip() == '':
                    block.append('')
                    k += 1
                    continue
                if re.match(r'^\s+\S', bl):
                    block.append(bl)
                    k += 1
                else:
                    break
            while block and block[-1] == '':
                block.pop()
            if block:
                indent = len(block[0]) - len(block[0].lstrip(' '))
                result[key] = '\n'.join((b[indent:] if len(b) >= indent else b.lstrip(' ')) for b in block)
            else:
                result[key] = ''
            i = k
            continue
        result[key] = val.strip().strip('"').strip("'")
        i += 1
    return result


def _parse_skill_frontmatter(raw_text):
    """从 SKILL.md 解析 frontmatter。返回 (meta, body, parse_error)。永不抛异常。"""
    meta = {}
    parse_error = False
    if not raw_text.startswith('---'):
        return meta, raw_text, False
    lines = raw_text.split('\n')
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end = i
            break
    if end == -1:
        parse_error = True
        fm_lines = lines[1:]
        body = ''
    else:
        fm_lines = lines[1:end]
        body = '\n'.join(lines[end + 1:])
    try:
        meta = _parse_simple_yaml(fm_lines)
    except Exception:
        meta = {}
        parse_error = True
    return meta, body.strip(), parse_error


def _build_skill_record(dirpath, source_id):
    """构造单个 Skill 记录（frontmatter 解析 + 文件统计 + 全文）。"""
    skill_md = os.path.join(dirpath, 'SKILL.md')
    try:
        with open(skill_md, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except Exception:
        raw = ''
    meta, body, parse_error = _parse_skill_frontmatter(raw)
    file_count = 0
    sub_dirs = []
    files = []
    try:
        for entry in sorted(os.listdir(dirpath)):
            fp = os.path.join(dirpath, entry)
            if os.path.isdir(fp):
                sub_dirs.append(entry)
                files.append({'name': entry + '/', 'path': fp, 'isDir': True})
            else:
                try:
                    sz = os.path.getsize(fp)
                except Exception:
                    sz = 0
                file_count += 1
                files.append({'name': entry, 'path': fp, 'size': sz})
    except Exception:
        pass
    name = meta.get('name') or os.path.basename(dirpath)
    display_name = meta.get('displayName') or name
    description = meta.get('description') or ''
    platforms = meta.get('platforms') or []
    if isinstance(platforms, str):
        platforms = [platforms]
    return {
        'id': 'skill_' + hashlib.sha1(dirpath.encode('utf-8')).hexdigest()[:12],
        'sourceId': source_id,
        'dirName': os.path.basename(dirpath),
        'dirPath': dirpath,
        'name': name,
        'displayName': display_name,
        'description': description,
        'version': meta.get('version', ''),
        'category': meta.get('category', ''),
        'platforms': platforms,
        'slug': meta.get('slug', ''),
        'fileCount': file_count,
        'subDirs': sub_dirs,
        'files': files,
        'content': body,
        'parseError': bool(parse_error),
    }


def _first_markdown_heading(body):
    for line in body.split('\n'):
        line = line.strip()
        if line.startswith('# '):
            return line[2:].strip()
    return ''


def _derive_description(body):
    """从正文取第一段非空文字作为简介（跳过标题行）。"""
    for p in body.split('\n'):
        p = p.strip()
        if not p or p.startswith('#'):
            continue
        text = p.lstrip('#*- ').strip()
        if text:
            return text[:200]
    return ''


def _build_file_skill_record(dirpath, name, source_id):
    """把独立的 .md 文件视为一个 Skill。"""
    full = os.path.join(dirpath, name)
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except Exception:
        raw = ''
    meta, body, parse_error = _parse_skill_frontmatter(raw)
    display = meta.get('displayName') or meta.get('name') or _first_markdown_heading(body) or name[:-3]
    description = meta.get('description') or _derive_description(body)
    platforms = meta.get('platforms') or []
    if isinstance(platforms, str):
        platforms = [platforms]
    try:
        sz = os.path.getsize(full)
    except Exception:
        sz = 0
    return {
        'id': 'skillf_' + hashlib.sha1(full.encode('utf-8')).hexdigest()[:12],
        'sourceId': source_id,
        'dirName': name,
        'dirPath': dirpath,
        'name': display,
        'displayName': display,
        'description': description,
        'version': meta.get('version', ''),
        'category': meta.get('category', ''),
        'platforms': platforms,
        'slug': meta.get('slug', ''),
        'fileCount': 1,
        'subDirs': [],
        'files': [{'name': name, 'path': full, 'size': sz}],
        'content': body,
        'parseError': bool(parse_error),
        'isFileSkill': True,
        'entryName': name,
    }


def _scan_skill_source(root_path):
    """递归扫描 root_path，返回所有 Skill：
    - 含 SKILL.md 的目录 → 目录型 Skill（其子树内部文件可经文件树浏览，不再单独成 Skill）
    - 不在任何 Skill 目录内的独立 .md 文件（排除 SKILL.md / README.md）→ 文件型 Skill
    """
    root_path = os.path.expanduser(root_path)
    skills = []
    if not os.path.isdir(root_path):
        return skills
    src_id = 'src_' + hashlib.sha1(root_path.encode('utf-8')).hexdigest()[:10]
    skip = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.seegent-reports'}

    def walk(dirpath, inside_skill):
        try:
            entries = sorted(os.listdir(dirpath))
        except Exception:
            return
        has_skill_md = 'SKILL.md' in entries
        if has_skill_md and not inside_skill:
            skills.append(_build_skill_record(dirpath, src_id))
            return  # 该目录作为 Skill 边界，子树不再单独成 Skill
        for name in entries:
            if name in skip:
                continue
            fp = os.path.join(dirpath, name)
            if os.path.isdir(fp):
                walk(fp, inside_skill or has_skill_md)
            elif os.path.isfile(fp) and name.lower().endswith('.md'):
                if name in ('SKILL.md', 'README.md'):
                    continue
                if not (inside_skill or has_skill_md):
                    skills.append(_build_file_skill_record(dirpath, name, src_id))

    try:
        for name in sorted(os.listdir(root_path)):
            if name in skip:
                continue
            fp = os.path.join(root_path, name)
            if os.path.isdir(fp):
                walk(fp, False)
            elif os.path.isfile(fp) and name.lower().endswith('.md'):
                if name in ('SKILL.md', 'README.md'):
                    continue
                skills.append(_build_file_skill_record(root_path, name, src_id))
    except Exception:
        pass
    return skills


# ===== 个人能力：团队（Team）扫描 =====
# 团队角色存放在「个人根/syteam/」下，每个 .md 即一个角色。
# frontmatter 可选：有则用 name/title/emoji/group/datasources；无则按文件名 + 子文件夹推导。
# 与项目内的技能库浏览器（skilllib）完全隔离：团队数据不进入项目侧 index。

def _personal_root():
    """个人资产根目录，默认 ~/拾元，可用环境变量 SEEGENT_PERSONAL_ROOT 覆盖。"""
    return os.path.expanduser(os.environ.get('SEEGENT_PERSONAL_ROOT', '~/拾元'))


def _default_team_path():
    return os.path.join(_personal_root(), 'syteam')


def _default_skill_path():
    return os.path.join(_personal_root(), 'syskill')


def _load_paths_config():
    """团队 / 技能 模块的可手动覆盖文件夹路径（仅本人使用，存于 .seegent-paths.json）。"""
    return _load_json(PATHS_FILE, {})


def _save_paths_config(cfg):
    _save_json(PATHS_FILE, cfg)
    return cfg


def _team_path():
    cfg = _load_paths_config()
    return cfg.get('teamPath') or _default_team_path()


def _skill_path():
    cfg = _load_paths_config()
    return cfg.get('skillPath') or _default_skill_path()


def _handle_open_in_finder(self):
    """在 macOS 访达中打开文件或文件夹（仅限个人根/项目目录，防越权）。"""
    body = self._read_body()
    if not isinstance(body, dict):
        return self._serve_json({'ok': False, 'error': '请求格式错误'})
    target = (body.get('path') or '').strip()
    if not target:
        return self._serve_json({'ok': False, 'error': '路径为空'})
    target = os.path.expanduser(target)
    if not os.path.exists(target):
        return self._serve_json({'ok': False, 'error': '路径不存在: ' + target})
    # 防越权：仅允许个人根目录 / 项目目录 / 团队·技能文件夹 / 用户主目录及其子路径
    allowed = [_personal_root(), BASE_DIR, os.path.abspath(_team_path()),
               os.path.abspath(_skill_path()), os.path.abspath(os.path.expanduser('~'))]
    ok = False
    for base in allowed:
        try:
            base_abs = os.path.abspath(os.path.expanduser(base))
            if os.path.commonpath([base_abs, os.path.abspath(target)]) == base_abs:
                ok = True
                break
        except Exception:
            pass
    if not ok:
        return self._serve_json({'ok': False, 'error': '仅允许打开个人目录或项目目录内的路径'})
    if sys.platform != 'darwin':
        return self._serve_json({'ok': False, 'error': '当前系统不支持在访达打开（仅 macOS）'})
    try:
        if os.path.isdir(target):
            subprocess.run(['open', target], check=False)
        else:
            subprocess.run(['open', '-R', target], check=False)  # 文件：在访达中定位
        return self._serve_json({'ok': True})
    except Exception as e:
        return self._serve_json({'ok': False, 'error': str(e)})


def _handle_paths_config(self):
    """GET 返回当前/默认路径；POST 保存（可分别覆盖团队/技能路径，空字符串=重置默认）。"""
    if self.command == 'POST':
        body = self._read_body()
        if not isinstance(body, dict):
            return self._serve_json({'error': '请求格式错误'}, 400)
        cfg = _load_paths_config()
        for key in ('teamPath', 'skillPath'):
            if key in body:
                v = (body.get(key) or '').strip()
                if v:
                    cfg[key] = os.path.expanduser(v)
                else:
                    cfg.pop(key, None)  # 空字符串 = 重置为默认
        _save_paths_config(cfg)
    return self._serve_json({
        'teamPath': _team_path(),
        'skillPath': _skill_path(),
        'teamDefault': _default_team_path(),
        'skillDefault': _default_skill_path(),
        'teamExists': os.path.isdir(_team_path()),
        'skillExists': os.path.isdir(_skill_path()),
    })


def _handle_fs_browse(self):
    """GET /api/fs/browse?path=... — 服务端目录树（仅限用户目录/项目目录），供前端选文件夹。"""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(self.path).query)
    p = (qs.get('path', [''])[0] or '').strip()
    p = os.path.expanduser(p) if p else os.path.expanduser('~')
    home = os.path.abspath(os.path.expanduser('~'))
    bases = [home, os.path.abspath(BASE_DIR)]
    ap = os.path.abspath(p)
    if not any(os.path.commonpath([b, ap]) == b for b in bases):
        return self._serve_json({'error': '已到允许的最顶层目录'}, 400)
    if not os.path.isdir(ap):
        return self._serve_json({'error': '目录不存在: ' + ap}, 400)
    try:
        names = sorted(os.listdir(ap))
    except Exception:
        names = []
    entries = []
    for n in names:
        if n.startswith('.'):
            continue
        fp = os.path.join(ap, n)
        if os.path.isdir(fp):
            entries.append({'name': n, 'isDir': True})
    return self._serve_json({'path': ap, 'parent': os.path.dirname(ap), 'entries': entries})


def _scan_team_source(root_path):
    """递归扫描 root_path（syteam/），返回按 group 分组的角色结构。
    - 每个 .md（排除 README.md）= 一个角色
    - frontmatter 可选；缺省时 name=文件名、group=所在子文件夹或「未分组」
    - 子文件夹 = 分组层级（如 syteam/数据组/小辉.md → group=数据组）
    """
    root_path = os.path.expanduser(root_path)
    groups = {}
    if not os.path.isdir(root_path):
        return {'root': root_path, 'groups': [], 'empty': True}
    skip = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.seegent-reports'}

    def walk(dirpath, group):
        try:
            entries = sorted(os.listdir(dirpath))
        except Exception:
            return
        for name in entries:
            if name in skip:
                continue
            fp = os.path.join(dirpath, name)
            if os.path.isdir(fp):
                walk(fp, name if group == '未分组' else group)
            elif os.path.isfile(fp) and name.lower().endswith('.md') and name not in ('README.md',):
                try:
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        raw = f.read()
                except Exception:
                    raw = ''
                meta, body, perr = _parse_skill_frontmatter(raw)
                disp = meta.get('name') or _first_markdown_heading(body) or name[:-3]
                title = meta.get('title') or meta.get('displayName') or ''
                emoji = meta.get('emoji') or '👤'
                grp = meta.get('group') or group or '未分组'
                desc = meta.get('description') or _derive_description(body)
                ds = meta.get('datasources') or []
                if isinstance(ds, str):
                    ds = [ds]
                rel = os.path.relpath(fp, root_path)
                member = {
                    'id': 'team_' + hashlib.sha1(fp.encode('utf-8')).hexdigest()[:12],
                    'name': disp,
                    'title': title,
                    'emoji': emoji,
                    'group': grp,
                    'datasources': ds,
                    'description': desc,
                    'content': body,
                    'path': fp,
                    'relPath': rel,
                    'parseError': bool(perr),
                }
                groups.setdefault(grp, []).append(member)

    walk(root_path, '未分组')
    result_groups = [{'name': g, 'members': ms} for g, ms in groups.items()]
    empty = not any(g['members'] for g in result_groups)
    return {'root': root_path, 'groups': result_groups, 'empty': empty}


# ===== 个人能力：团队（Team）文件夹树浏览 =====
# 与上面的「扁平角色扫描」并存：聊天角色仍用 _scan_team_source；
# 团队页 UI 改用下面的树接口，按文件夹一层层呈现（目录在前、文件在后）。
TEAM_SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.seegent-reports', '.DS_Store', 'Thumbs.db'}


def _team_safe_rel(rel):
    """把前端传来的 rel 规整并做越界保护，返回相对 syteam 的安全子路径（'' 表示根）。"""
    if not rel:
        return ''
    rel = rel.replace('\\', '/')
    parts = [p for p in rel.split('/') if p not in ('', '.', '..')]
    return '/'.join(parts)


def _team_full_path(rel):
    root = _team_path()
    if not rel:
        return root
    return os.path.join(root, rel)


def _scan_team_tree(rel=''):
    """返回 syteam/<rel> 目录下的直接子项（目录在前、文件在后，均按名排序）。
    目录 = 业务/工作流或子分组；.md 文件解析 frontmatter 取展示信息。"""
    rel = _team_safe_rel(rel)
    full = _team_full_path(rel)
    root = _team_path()
    if not os.path.isdir(full):
        return {'root': root, 'rel': rel, 'parent': '', 'entries': [], 'empty': True}
    try:
        names = sorted(os.listdir(full))
    except Exception:
        return {'root': root, 'rel': rel, 'parent': '', 'entries': [], 'empty': True}
    dirs = [n for n in names if os.path.isdir(os.path.join(full, n)) and n not in TEAM_SKIP]
    # 二进制/压缩文件不在团队浏览器里呈现（无法作为内容预览，且点开会显示乱码）
    TEAM_FILE_SKIP_EXT = {'.zip', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.mp4', '.mov', '.mp3', '.wav', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
    files = [n for n in names if os.path.isfile(os.path.join(full, n)) and n not in TEAM_SKIP
             and os.path.splitext(n)[1].lower() not in TEAM_FILE_SKIP_EXT]
    entries = []
    for n in dirs:
        entries.append({'name': n, 'isDir': True, 'sub': (rel + '/' + n).lstrip('/')})
    for n in files:
        fp = os.path.join(full, n)
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                raw = f.read()
        except Exception:
            raw = ''
        meta, body, _ = _parse_skill_frontmatter(raw)
        is_md = n.lower().endswith(('.md', '.markdown'))
        disp = meta.get('name') or (_first_markdown_heading(body) if is_md else '') or n
        emoji = meta.get('emoji') or ('📄' if is_md else '📃')
        desc = meta.get('description') or _derive_description(body)
        entries.append({
            'name': n, 'isDir': False, 'sub': (rel + '/' + n).lstrip('/'),
            'displayName': disp, 'emoji': emoji,
            'title': meta.get('title') or meta.get('displayName') or '',
            'description': desc, 'isMd': is_md,
        })
    parent = rel.rsplit('/', 1)[0] if rel else ''
    return {'root': root, 'rel': rel, 'parent': parent, 'entries': entries, 'empty': not entries}


def _read_team_file(rel):
    """读取 syteam/<rel> 文件内容，带越界保护。返回 (data, error)。"""
    rel = _team_safe_rel(rel)
    if not rel:
        return None, '缺少文件路径'
    full = _team_full_path(rel)
    root = os.path.abspath(_team_path())
    abs_full = os.path.abspath(full)
    if not (abs_full == root or abs_full.startswith(root + os.sep)):
        return None, '路径越界'
    if not os.path.isfile(full):
        return None, '不是文件：' + rel
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        return None, str(e)
    MAX = 300 * 1024
    truncated = False
    if len(content) > MAX:
        content = content[:MAX]
        truncated = True
    name = os.path.basename(full)
    low = name.lower()
    if low.endswith(('.md', '.markdown')):
        lang = 'markdown'
    elif low.endswith(('.json', '.py', '.js', '.jsx', '.ts', '.tsx', '.sh', '.bash',
                       '.yaml', '.yml', '.toml', '.cfg', '.ini', '.css', '.html', '.htm', '.txt', '.csv', '.xml', '.svg')):
        lang = 'code'
    else:
        lang = 'text'
    return {'content': content, 'lang': lang, 'truncated': truncated, 'name': name, 'path': full, 'rel': rel}, None


def _collect_team_folder_text(rel='', limit=200 * 1024):
    """递归收集 syteam/<rel> 下所有 .md/.txt 正文，拼成一段文本，供「召唤文件夹」的网页端指令内联。"""
    rel = _team_safe_rel(rel)
    full = _team_full_path(rel)
    if not os.path.isdir(full):
        return ''
    parts = []
    total = [0]
    skip = TEAM_SKIP | {'.zip', '.png', '.jpg', '.jpeg', '.gif', '.mp4', '.mov', '.pdf', '.py', '.mjs', '.js', '.ts', '.tsx', '.json', '.css', '.html', '.svg'}

    def walk(dp, prefix):
        if total[0] >= limit:
            return
        try:
            names = sorted(os.listdir(dp))
        except Exception:
            return
        for n in names:
            if n in skip or n.startswith('.'):
                continue
            fp = os.path.join(dp, n)
            sub = (prefix + '/' + n).lstrip('/')
            if os.path.isdir(fp):
                walk(fp, sub)
            elif n.lower().endswith(('.md', '.markdown', '.txt')):
                if total[0] >= limit:
                    return
                try:
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        c = f.read()
                except Exception:
                    c = ''
                chunk = '===== ' + sub + ' =====\n\n' + c + '\n\n'
                if total[0] + len(chunk) > limit:
                    parts.append(chunk[:max(0, limit - total[0])])
                    parts.append('\n……（内容过长，已截断）')
                    total[0] = limit
                    return
                parts.append(chunk)
                total[0] += len(chunk)
    walk(full, rel)
    return ''.join(parts)


# ===== 技能（个人专属，syskill/）文件夹树接口：与团队同款，根目录换成技能目录 =====
def _skill_full_path(rel):
    root = _skill_path()
    if not rel:
        return root
    return os.path.join(root, rel)


def _scan_skill_tree(rel=''):
    """返回 syskill/<rel> 目录下的直接子项（目录在前、文件在后），供技能模块文件夹树浏览器。"""
    rel = _team_safe_rel(rel)  # 越界保护逻辑通用，直接复用
    full = _skill_full_path(rel)
    root = _skill_path()
    if not os.path.isdir(full):
        return {'root': root, 'rel': rel, 'parent': '', 'entries': [], 'empty': True}
    try:
        names = sorted(os.listdir(full))
    except Exception:
        return {'root': root, 'rel': rel, 'parent': '', 'entries': [], 'empty': True}
    dirs = [n for n in names if os.path.isdir(os.path.join(full, n)) and n not in TEAM_SKIP]
    SKILL_FILE_SKIP_EXT = {'.zip', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.mp4', '.mov',
                           '.mp3', '.wav', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
    files = [n for n in names if os.path.isfile(os.path.join(full, n)) and n not in TEAM_SKIP
             and os.path.splitext(n)[1].lower() not in SKILL_FILE_SKIP_EXT]
    entries = []
    for n in dirs:
        entries.append({'name': n, 'isDir': True, 'sub': (rel + '/' + n).lstrip('/')})
    for n in files:
        fp = os.path.join(full, n)
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                raw = f.read()
        except Exception:
            raw = ''
        meta, body, _ = _parse_skill_frontmatter(raw)
        is_md = n.lower().endswith(('.md', '.markdown'))
        disp = meta.get('name') or (_first_markdown_heading(body) if is_md else '') or n
        emoji = meta.get('emoji') or ('📄' if is_md else '📃')
        desc = meta.get('description') or _derive_description(body)
        entries.append({
            'name': n, 'isDir': False, 'sub': (rel + '/' + n).lstrip('/'),
            'displayName': disp, 'emoji': emoji,
            'title': meta.get('title') or meta.get('displayName') or '',
            'description': desc, 'isMd': is_md,
        })
    parent = rel.rsplit('/', 1)[0] if rel else ''
    return {'root': root, 'rel': rel, 'parent': parent, 'entries': entries, 'empty': not entries}


def _read_skill_file(rel):
    """读取 syskill/<rel> 文件内容，带越界保护。返回 (data, error)。"""
    rel = _team_safe_rel(rel)
    if not rel:
        return None, '缺少文件路径'
    full = _skill_full_path(rel)
    root = os.path.abspath(_skill_path())
    abs_full = os.path.abspath(full)
    if not (abs_full == root or abs_full.startswith(root + os.sep)):
        return None, '路径越界'
    if not os.path.isfile(full):
        return None, '不是文件：' + rel
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        return None, str(e)
    MAX = 300 * 1024
    truncated = False
    if len(content) > MAX:
        content = content[:MAX]
        truncated = True
    name = os.path.basename(full)
    low = name.lower()
    if low.endswith(('.md', '.markdown')):
        lang = 'markdown'
    elif low.endswith(('.json', '.py', '.js', '.jsx', '.ts', '.tsx', '.sh', '.bash',
                       '.yaml', '.yml', '.toml', '.cfg', '.ini', '.css', '.html', '.htm', '.txt', '.csv', '.xml', '.svg')):
        lang = 'code'
    else:
        lang = 'text'
    return {'content': content, 'lang': lang, 'truncated': truncated, 'name': name, 'path': full, 'rel': rel}, None


def _collect_skill_folder_text(rel='', limit=200 * 1024):
    """递归收集 syskill/<rel> 下所有 .md/.txt 正文，供「召唤文件夹」的网页端指令内联。"""
    rel = _team_safe_rel(rel)
    full = _skill_full_path(rel)
    if not os.path.isdir(full):
        return ''
    parts = []
    total = [0]
    skip = TEAM_SKIP | {'.zip', '.png', '.jpg', '.jpeg', '.gif', '.mp4', '.mov', '.pdf', '.py', '.mjs', '.js', '.ts', '.tsx', '.json', '.css', '.html', '.svg'}

    def walk(dp, prefix):
        if total[0] >= limit:
            return
        try:
            names = sorted(os.listdir(dp))
        except Exception:
            return
        for n in names:
            if n in skip or n.startswith('.'):
                continue
            fp = os.path.join(dp, n)
            sub = (prefix + '/' + n).lstrip('/')
            if os.path.isdir(fp):
                walk(fp, sub)
            elif n.lower().endswith(('.md', '.markdown', '.txt')):
                if total[0] >= limit:
                    return
                try:
                    with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                        c = f.read()
                except Exception:
                    c = ''
                chunk = '===== ' + sub + ' =====\n\n' + c + '\n\n'
                if total[0] + len(chunk) > limit:
                    parts.append(chunk[:max(0, limit - total[0])])
                    parts.append('\n……（内容过长，已截断）')
                    total[0] = limit
                    return
                parts.append(chunk)
                total[0] += len(chunk)
    walk(full, rel)
    return ''.join(parts)


def _rebuild_skill_index_from(index):
    """根据内存 index（含 sources）重新扫描、写盘并返回完整 index。"""
    index.setdefault('version', 1)
    index.setdefault('sources', [])
    index.setdefault('skills', [])
    all_skills = []
    for src in index['sources']:
        p = src.get('path', '')
        scanned = _scan_skill_source(p)
        src_id = src.get('id') or ('src_' + hashlib.sha1(p.encode('utf-8')).hexdigest()[:10])
        src['id'] = src_id
        for s in scanned:
            s['sourceId'] = src_id
        src['skillCount'] = len(scanned)
        all_skills.extend(scanned)
    index['skills'] = all_skills
    _save_json(SKILLLIB_INDEX_FILE, index)
    return index


def _rebuild_skill_index():
    """从磁盘加载 index 并重新扫描全部来源。"""
    index = _load_json(SKILLLIB_INDEX_FILE, {'version': 1, 'sources': [], 'skills': []})
    return _rebuild_skill_index_from(index)


# ===== 技能库：文件浏览 / AI 解释 辅助 =====

SKILLLIB_FS_SKIP = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.seegent-reports'}


def _skilllib_find_skill(skill_id):
    """按 id 从磁盘索引查找 Skill 记录。"""
    if not skill_id:
        return None
    index = _load_json(SKILLLIB_INDEX_FILE, {'version': 1, 'sources': [], 'skills': []})
    for s in index.get('skills', []):
        if s.get('id') == skill_id:
            return s
    return None


def _skilllib_build_tree(dirpath, rel):
    """返回 dirpath/rel 目录下的条目（目录在前，文件在后，均排序）。"""
    full = os.path.normpath(os.path.join(dirpath, rel)) if rel else dirpath
    if not (full == dirpath or full.startswith(dirpath + os.sep)):
        return []
    entries = []
    try:
        names = sorted(os.listdir(full))
    except Exception:
        return entries
    dirs = [n for n in names if os.path.isdir(os.path.join(full, n)) and n not in SKILLLIB_FS_SKIP]
    files = [n for n in names if os.path.isfile(os.path.join(full, n))]
    for n in dirs:
        entries.append({'name': n, 'isDir': True, 'sub': (rel + '/' + n).lstrip('/')})
    for n in files:
        try:
            sz = os.path.getsize(os.path.join(full, n))
        except Exception:
            sz = 0
        entries.append({'name': n, 'isDir': False, 'sub': (rel + '/' + n).lstrip('/'), 'size': sz})
    return entries


def _skilllib_read_file(dirpath, sub):
    """读取 dirpath/sub 文件内容。返回 (data, error)。带路径越界保护。"""
    if not sub:
        return None, '缺少 sub 参数'
    full = os.path.normpath(os.path.join(dirpath, sub))
    if not (full == dirpath or full.startswith(dirpath + os.sep)):
        return None, '路径越界'
    if not os.path.isfile(full):
        return None, '不是文件：' + sub
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        return None, str(e)
    MAX = 300 * 1024
    truncated = False
    if len(content) > MAX:
        content = content[:MAX]
        truncated = True
    name = os.path.basename(full)
    low = name.lower()
    if low.endswith(('.md', '.markdown')):
        lang = 'markdown'
    elif low.endswith(('.json', '.py', '.js', '.jsx', '.ts', '.tsx', '.sh', '.bash',
                       '.yaml', '.yml', '.toml', '.cfg', '.ini', '.css', '.html', '.htm', '.txt', '.csv', '.xml', '.svg')):
        lang = 'code'
    else:
        lang = 'text'
    return {'content': content, 'lang': lang, 'truncated': truncated, 'name': name}, None


def _skilllib_explain_prompt(s):
    """构造发给 LLM 的「解释这个 Skill」提示正文。"""
    parts = []
    parts.append('Skill 名称：' + (s.get('displayName') or s.get('name') or ''))
    if s.get('category'):
        parts.append('分类：' + s['category'])
    if s.get('description'):
        parts.append('描述：' + s['description'])
    if s.get('platforms'):
        parts.append('适用平台：' + ', '.join(s['platforms']))
    content = (s.get('content') or '')[:2000]
    if content:
        parts.append('SKILL.md 正文（节选）：\n' + content)
    return '\n'.join(parts)


def _skilllib_default_engine(engines):
    """选择一个可用的 REST 引擎 id（优先 deepseek，其次第一个 rest）。"""
    if not engines:
        return None
    if 'deepseek' in engines and engines['deepseek'].get('type') == 'rest':
        return 'deepseek'
    for eid, cfg in engines.items():
        if cfg.get('type') == 'rest':
            return eid
    return None


# ===== 操作日志 =====

MAX_LOG_ENTRIES = 500

def _append_log(entry):
    """追加一条日志到 LOG_FILE，超出上限时清理旧条目"""
    entry['timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')
    entry['id'] = f'log_{int(time.time()*1000)}_{len(str(time.time())) % 10000:04d}'
    logs = _load_json(LOG_FILE, {'logs': []})
    logs['logs'].append(entry)
    if len(logs['logs']) > MAX_LOG_ENTRIES:
        logs['logs'] = logs['logs'][-MAX_LOG_ENTRIES:]
    _save_json(LOG_FILE, logs)
    return entry['id']

def _get_logs(limit=100, agent_id=None, status=None, before=None):
    """读取日志，支持过滤"""
    data = _load_json(LOG_FILE, {'logs': []})
    logs = data.get('logs', [])
    if agent_id:
        logs = [l for l in logs if l.get('agentId') == agent_id or l.get('callerId') == agent_id]
    if status:
        logs = [l for l in logs if l.get('status') == status]
    if before:
        logs = [l for l in logs if l.get('timestamp', '') < before]
    return logs[-limit:]

# ===== 外部智能体日志聚合（跨工具本地记录汇总） =====
# 不常驻进程、不调大模型。打开日志模块时实时读取各 AI 工具留在本地的记录文件，
# 解析为统一元数据（模型 / Token / 工具 / 耗时 / 状态），与 Seegent 自有日志合并展示。
EXTERNAL_LOG_SOURCES = [
    {
        'id': 'claude-code',
        'name': 'Claude Code',
        'type': 'claude_jsonl',
        'roots': ['~/.claude/projects'],
        'max_files': 400,        # 每次最多扫描的最近修改文件数
        'max_per_source': 200,   # 每源最多返回的条目数
    },
    {
        'id': 'workbuddy',
        'name': 'WorkBuddy',
        'type': 'workbuddy_audit',
        'roots': ['~/.workbuddy/audit-log'],
        'max_files': 60,         # 最近 60 天的审计文件（实际 24 个，全扫）
        'max_per_source': 5000,  # 审计日志累计数千条命令，放开上限避免历史被截断
    },
    {
        'id': 'codex',
        'name': 'Codex',
        'type': 'codex_rollout',
        'roots': ['~/.codex/sessions'],
        'max_files': 60,         # rollout transcript 较大，限制扫描数
        'max_per_source': 60,    # 每源最多返回的条目数
    },
    {
        'id': 'qoderwork',
        'name': 'QoderWork',
        'type': 'qoderwork_aistats',
        'roots': ['~/.qoder-cli/ai-stats'],
        'max_files': 80,
        'max_per_source': 120,
    },
]

# 缓存：filepath -> (mtime_ns, entries)，避免每次全量重解析
_ext_log_cache = {}

def _parse_claude_jsonl(path):
    """解析一个 Claude Code transcript 文件，返回一条统一日志条目（或 None）"""
    try:
        session_id = None
        first_ts = None
        last_ts = None
        task = ''
        model = ''
        prompt_tokens = 0
        completion_tokens = 0
        tools = []
        artifacts = []  # 被编辑/新建的文件路径（交付物）
        cwd = ''
        n_msgs = 0
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_id is None:
                    session_id = o.get('sessionId', '')
                ts = o.get('timestamp', '')
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                msg = o.get('message') or {}
                role = msg.get('role', '')
                content = msg.get('content', '')
                if not task:
                    if role == 'user' and isinstance(content, str) and content.strip():
                        task = content.strip()
                    elif o.get('type') == 'summary' and isinstance(content, str) and content.strip():
                        task = content.strip()
                if role == 'assistant':
                    if msg.get('model') and not model:
                        model = msg.get('model')
                    usage = msg.get('usage') or {}
                    if usage.get('input_tokens'):
                        prompt_tokens += int(usage.get('input_tokens', 0) or 0)
                    if usage.get('output_tokens'):
                        completion_tokens += int(usage.get('output_tokens', 0) or 0)
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get('type') == 'tool_use':
                                tname = blk.get('name', '')
                                if tname and tname not in tools:
                                    tools.append(tname)
                                # 提取文件编辑类工具产出的文件路径（交付物视图用）
                                if tname in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
                                    fp = (blk.get('input') or {}).get('file_path')
                                    if fp and fp not in artifacts:
                                        artifacts.append(fp)
                if role:
                    n_msgs += 1
                if not cwd and o.get('cwd'):
                    cwd = o.get('cwd')
        if not session_id or first_ts is None:
            return None

        def _norm(ts):
            try:
                return ts.replace('Z', '+00:00')
            except Exception:
                return ts

        def _fmt(ts):
            try:
                dt = datetime.datetime.fromisoformat(_norm(ts)).astimezone()
                return dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                return ts

        duration = 0
        try:
            t1 = datetime.datetime.fromisoformat(_norm(first_ts))
            t2 = datetime.datetime.fromisoformat(_norm(last_ts))
            duration = max(0, int((t2 - t1).total_seconds()))
        except Exception:
            duration = 0
        total = prompt_tokens + completion_tokens
        return {
            'source': 'claude-code',
            'sourceName': 'Claude Code',
            'agentName': 'Claude Code',
            'engineId': 'claude-code',
            'agentId': 'claude-code',
            'sessionId': session_id,
            'task': task[:200],
            'model': model,
            'modelId': model,
            'timestamp': _fmt(first_ts),
            'duration': duration,
            'iterations': max(1, n_msgs),
            'totalTokens': total,
            'promptTokens': prompt_tokens,
            'completionTokens': completion_tokens,
            'toolsUsed': tools,
            'artifacts': artifacts[:30],
            'status': 'done',
            'workspace': cwd,
        }
    except Exception:
        return None

def _summarize_workbuddy_cmd(cmd):
    """把 WorkBuddy 命令文本翻译成人类可读摘要，并提取 workspace / 相关路径。
    注意：audit-log 里的 commandPreview 本身已被截断，无法恢复完整命令。
    注意：command 里可能包含 python3 -c / cat > / echo 等包裹的代码，要避免内部代码污染分类。"""
    if not cmd:
        return '执行命令', '', [], ''
    c = cmd.strip()
    first_line = c.split('\n')[0]

    # 工作目录：从第一行的 cd 提取（避免代码字符串里的 cd 被误取）
    workspace = ''
    m = re.search(r'cd\s+["\']?(/Users/[^\s"\'|&;<>$`]+)["\']?(\s+&&|\s+;|\s*$)', first_line)
    if not m:
        m = re.search(r'cd\s+["\']?([^\s"\'|&;<>$`]+)["\']?(\s+&&|\s+;|\s*$)', first_line)
    if m:
        workspace = m.group(1)

    # 提取命令中涉及的真实路径（去重、过滤代码污染）
    artifacts = []
    for p in re.findall(r'(/Users/[^\s"\'|&;<>$`]+)', c):
        p = p.rstrip('/')
        if not p or p in artifacts:
            continue
        # 过滤掉：正则/代码片段、纯二进制工具
        if any(ch in p for ch in ['[', ']', '*', '?', '$', '\\', '**']):
            continue
        if re.search(r'/(python3?|node|npx|npm|git|curl|ls|cat|rm|mv|cp|mkdir|grep|find|wc|head|tail|bash|zsh|sh)$', p):
            continue
        artifacts.append(p)

    # 动作分类：按顺序匹配第一个最具体的动作，避免内部代码污染
    action = '执行 shell 命令'
    detail = ''
    # 1. 会包裹代码的命令优先（避免内部 curl/python 关键字污染）
    if re.search(r'cat\s+>\s+\S+.*<<', c, re.M):
        action = '写临时脚本/文件'
    elif re.search(r'python3\s+-?\s*<<', c, re.M):
        action = '执行 Python 脚本'
    elif re.search(r'python3\s+-m\s+py_compile', c):
        action = 'Python 语法检查'
    elif re.search(r'\bnode\s+--check\b', c):
        action = 'Node.js 语法检查'
    elif re.search(r'python3\s+-c\b', c):
        action = '执行 Python 内联脚本'
    elif re.search(r'\bnode\s+-e\b', c):
        action = '执行 Node.js 内联脚本'
    elif re.search(r'\bnode\s+\S+\.(js|mjs|cjs)', c):
        action = '运行 Node.js 脚本'
    elif re.search(r'\bnode\b', c):
        action = '执行 Node.js 命令'
    elif re.search(r'python3\b', c) or 'bin/python' in c:
        if re.search(r'python3\s+\S+\.(py|sh)', c):
            action = '运行 Python 脚本'
        else:
            action = '执行 Python 命令'
    elif re.search(r'curl\b', c):
        action = '测试/查询 HTTP 接口'
        mu = re.search(r'https?://[^\s"\']+', c)
        if mu:
            detail = mu.group(0)
    elif re.search(r'\b(pkill|kill)\b', c):
        action = '停止进程/服务'
    elif re.search(r'http\.server|npx\s+.*serve', c):
        action = '启动本地预览服务'
    elif re.search(r'^\s*ls\b', c):
        action = '查看目录'
    elif re.search(r'^\s*rm\b', c):
        action = '删除文件'
    elif re.search(r'\bgit\b', c):
        action = 'Git 操作'
    elif re.search(r'\bmkdir\b', c):
        action = '创建目录'
    elif re.search(r'\bcp\b', c):
        action = '复制文件'
    elif re.search(r'\bmv\b', c):
        action = '移动/重命名文件'
    elif re.search(r'\b(cat|head|tail)\b', c):
        action = '查看文件内容'
    elif re.search(r'\bgrep\b', c):
        action = '搜索文本'
    elif re.search(r'\bfind\b', c):
        action = '查找文件'
    elif re.search(r'\bwc\b', c):
        action = '统计文件'
    elif re.search(r'\bfor\s+', c):
        if re.search(r'node\s+--check', c):
            action = '批量语法检查'
        else:
            action = '批量执行命令'
    elif re.search(r'^echo\b', c, re.M):
        action = '输出信息'
    elif re.search(r'^sleep\b', c, re.M):
        action = '等待'

    task = f'{action} · {detail}' if detail else action
    return task, workspace, artifacts[:10], c

def _parse_workbuddy_audit_file(path):
    """解析一个 WorkBuddy 审计日志文件（按天一个），每行是一次命令/工具执行。
    返回统一日志条目列表（一个文件含多条事件）。只取元数据，不读对话内容。"""
    out = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = o.get('timestamp')
                if not ts:
                    continue
                try:
                    dt = datetime.datetime.fromtimestamp(int(ts) / 1000).astimezone()
                    ts_str = dt.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    ts_str = str(ts)
                decision = (o.get('decision') or '').lower()
                status = 'error' if decision in ('denied', 'blocked', 'rejected') else 'done'
                raw_cmd = o.get('commandPreview') or o.get('eventType') or o.get('category') or ''
                cat = o.get('eventType') or o.get('category') or ''
                is_cmd = 'command' in cat or o.get('commandPreview') is not None
                task, workspace, artifacts, _ = _summarize_workbuddy_cmd(raw_cmd)
                # 若没识别到目录，但从命令路径能推断项目，补充 workspace
                if not workspace and artifacts:
                    workspace = artifacts[0]
                    # 若是文件路径，取所在目录作为工作区
                    if workspace and '.' in os.path.basename(workspace):
                        workspace = os.path.dirname(workspace)
                out.append({
                    'source': 'workbuddy',
                    'sourceName': 'WorkBuddy',
                    'agentName': 'WorkBuddy',
                    'engineId': 'workbuddy',
                    'agentId': 'workbuddy',
                    'task': (task or '')[:200],
                    'commandPreview': (raw_cmd or '')[:300],
                    'model': '',
                    'modelId': '',
                    'timestamp': ts_str,
                    'duration': None,
                    'iterations': 0,
                    'totalTokens': 0,
                    'promptTokens': None,
                    'completionTokens': None,
                    'toolsUsed': ['shell'] if is_cmd else [],
                    'status': status,
                    'workspace': workspace,
                    'artifacts': artifacts[:10],
                    'category': cat,
                })
    except Exception:
        return []
    return out

# Codex session_index 缓存：session_id -> thread_name（一次性加载）
_CODEX_INDEX = None

def _load_codex_index():
    """加载 ~/.codex/session_index.jsonl -> {session_id: thread_name}"""
    global _CODEX_INDEX
    if _CODEX_INDEX is not None:
        return _CODEX_INDEX
    idx = {}
    p = os.path.expanduser('~/.codex/session_index.jsonl')
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = o.get('id')
                if sid:
                    idx[sid] = o.get('thread_name', '')
    except OSError:
        pass
    _CODEX_INDEX = idx
    return idx

def _extract_text(content):
    """从 message content（str 或 list[block]）里提取纯文本"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get('type') == 'input_text' and blk.get('text'):
                    parts.append(blk['text'])
                elif blk.get('type') == 'text' and blk.get('text'):
                    parts.append(blk['text'])
                elif blk.get('text'):
                    parts.append(blk['text'])
        return ' '.join(parts).strip()
    return ''

def _is_boilerplate(text):
    """判断是否为系统注入的环境上下文（非真实用户提示），跳过"""
    if not text:
        return True
    t = text.lstrip()
    if t.startswith('<'):
        return True
    if 'Filesystem sandboxing' in text or '<cwd>' in text or '<environment_context' in text:
        return True
    return False

def _parse_codex_rollout(path):
    """解析一个 Codex rollout transcript（一个会话一条记录）。
    只取元数据：session_meta 的起始时间/cwd/provider、首个用户提示（task）、token 汇总。
    不读取对话正文，零额外 token 消耗。"""
    try:
        session_id = None
        m = re.search(r'rollout-.*?-([0-9a-f-]{36})\.jsonl', os.path.basename(path))
        if m:
            session_id = m.group(1)
        first_ts = None
        last_ts = None
        cwd = ''
        provider = ''
        model = ''
        task = ''
        prompt_tokens = 0
        completion_tokens = 0
        tools = []
        n_user = 0
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = o.get('timestamp', '')
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                t = o.get('type')
                if t == 'session_meta':
                    pld = o.get('payload', {}) or {}
                    if not cwd and pld.get('cwd'):
                        cwd = pld['cwd']
                    if not provider and pld.get('model_provider'):
                        provider = pld['model_provider']
                # 在 event_msg / response_item / 顶层 msg 中找首个用户文本
                cand = o.get('msg') or o.get('message') or o.get('payload') or o
                if isinstance(cand, dict):
                    role = cand.get('role')
                    if role == 'user' and not task:
                        txt = _extract_text(cand.get('content', ''))
                        if txt and not _is_boilerplate(txt):
                            task = txt
                            n_user += 1
                    # 汇总 token：兼容多种字段命名
                    u = cand.get('usage')
                    if isinstance(u, dict):
                        prompt_tokens += int(u.get('input_tokens', 0) or 0)
                        completion_tokens += int(u.get('output_tokens', 0) or 0)
                    it = cand.get('input_tokens')
                    if it is not None:
                        prompt_tokens += int(it or 0)
                    ot = cand.get('output_tokens')
                    if ot is not None:
                        completion_tokens += int(ot or 0)
                    # 工具调用名称
                    content = cand.get('content')
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get('type') == 'function_call':
                                nm = blk.get('name', '')
                                if nm and nm not in tools:
                                    tools.append(nm)
        if first_ts is None:
            return None
        idx = _load_codex_index()
        if not task and session_id and idx.get(session_id):
            task = idx[session_id]
        if not task:
            task = 'Codex 会话'
        def _norm(ts):
            try:
                return ts.replace('Z', '+00:00')
            except Exception:
                return ts
        try:
            ts_str = datetime.datetime.fromisoformat(_norm(first_ts)).astimezone().strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ts_str = (first_ts or '')[:19]
        total = prompt_tokens + completion_tokens
        return {
            'source': 'codex',
            'sourceName': 'Codex',
            'agentName': 'Codex',
            'engineId': 'codex',
            'agentId': 'codex',
            'sessionId': session_id or '',
            'task': task[:200],
            'model': model or (provider or ''),
            'modelId': model or (provider or ''),
            'timestamp': ts_str,
            'duration': 0,
            'iterations': max(1, n_user),
            'totalTokens': total,
            'promptTokens': prompt_tokens,
            'completionTokens': completion_tokens,
            'toolsUsed': tools,
            'status': 'done',
            'workspace': cwd,
        }
    except Exception:
        return None

def _parse_qoderwork_ai_stats(path):
    """解析一个 QoderWork ai-stats 文件（一个会话一条记录）。
    每行是一次 AI 文件编辑，含 filePath / 增删行 / 修改内容。
    只取元数据：编辑次数、首个文件路径；时间戳取文件 mtime（记录内无时间字段）。
    绝不读取 aiModifiedContent 正文，零额外 token 消耗。"""
    try:
        mtime = os.stat(path).st_mtime
        ts_str = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
    except OSError:
        return None
    first_path = ''
    n_edits = 0
    total_add = 0
    total_del = 0
    session_id = ''
    all_paths = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                n_edits += 1
                fp = o.get('filePath', '')
                if not first_path and fp:
                    first_path = fp
                if fp and fp not in all_paths:
                    all_paths.append(fp)
                total_add += len(o.get('aiAddedLines', []) or [])
                total_del += len(o.get('aiDeletedLines', []) or [])
                ld = o.get('lineDetails') or []
                if isinstance(ld, list) and ld and not session_id:
                    session_id = (ld[0] or {}).get('sessionId', '')
    except OSError:
        return None
    if not first_path:
        return None
    base = os.path.basename(first_path)
    task = 'AI 编辑 %d 处 · %s' % (n_edits, base)
    return {
        'source': 'qoderwork',
        'sourceName': 'QoderWork',
        'agentName': 'QoderWork',
        'engineId': 'qoderwork',
        'agentId': 'qoderwork',
        'sessionId': session_id,
        'task': task[:200],
        'model': '',
        'modelId': '',
        'timestamp': ts_str,
        'duration': None,
        'iterations': n_edits,
        'totalTokens': 0,
        'promptTokens': None,
        'completionTokens': None,
        'toolsUsed': ['edit'],
        'artifacts': all_paths[:30],
        'status': 'done',
        'workspace': os.path.dirname(first_path),
    }

def _load_external_logs():
    """聚合所有外部智能体工具的本地记录，返回统一日志条目列表（带 mtime 缓存）"""
    out = []
    for src in EXTERNAL_LOG_SOURCES:
        stype = src.get('type')
        if stype in ('claude_jsonl', 'workbuddy_audit', 'codex_rollout', 'qoderwork_aistats'):
            files = []
            for root in src.get('roots', []):
                r = os.path.expanduser(root)
                if not os.path.isdir(r):
                    continue
                for dirpath, dirnames, filenames in os.walk(r):
                    # 跳过版本/缓存目录，减少无意义遍历
                    dirnames[:] = [d for d in dirnames
                                   if d not in ('.git', 'node_modules', '__pycache__')]
                    for fn in filenames:
                        if not fn.endswith('.jsonl'):
                            continue
                        fp = os.path.join(dirpath, fn)
                        try:
                            mtime = os.stat(fp).st_mtime_ns
                        except OSError:
                            continue
                        files.append((mtime, fp))
            files.sort(reverse=True)
            max_files = src.get('max_files', 400)
            entries = []
            for _, fp in files[:max_files]:
                try:
                    mt = os.stat(fp).st_mtime_ns
                except OSError:
                    continue
                cached = _ext_log_cache.get(fp)
                if cached and cached[0] == mt:
                    entries.extend(cached[1])
                else:
                    parsed = []
                    if stype == 'claude_jsonl':
                        e = _parse_claude_jsonl(fp)
                        parsed = [e] if e else []
                    elif stype == 'workbuddy_audit':
                        parsed = _parse_workbuddy_audit_file(fp)
                    elif stype == 'codex_rollout':
                        e = _parse_codex_rollout(fp)
                        parsed = [e] if e else []
                    elif stype == 'qoderwork_aistats':
                        e = _parse_qoderwork_ai_stats(fp)
                        parsed = [e] if e else []
                    _ext_log_cache[fp] = (mt, parsed)
                    entries.extend(parsed)
            entries.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            out.extend(entries[:src.get('max_per_source', 200)])
    return out

def _merge_all_logs(limit=200, agent_id=None, status=None, source=None):
    """合并 Seegent 自有日志 + 外部智能体日志，统一返回"""
    logs = _get_logs(limit=limit * 3, agent_id=None, status=None)
    for l in logs:
        if not l.get('source'):
            l['source'] = 'seegent'
            l['sourceName'] = 'Seegent'
    ext = _load_external_logs()
    if source == 'seegent':
        merged = logs
    elif source:
        merged = [l for l in logs if l.get('source') == source] + \
                 [e for e in ext if e.get('source') == source]
    else:
        merged = logs + ext
    if status:
        merged = [l for l in merged if l.get('status') == status]
    if agent_id:
        merged = [l for l in merged
                  if l.get('agentId') == agent_id or l.get('engineId') == agent_id
                  or l.get('callerId') == agent_id]
    merged.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return merged[:limit]

# ===== 项目追踪 =====

DEFAULT_TRACKS = {'entries': []}

def _load_tracks():
    return _load_json(TRACKS_FILE, DEFAULT_TRACKS)

def _save_tracks(data):
    _save_json(TRACKS_FILE, data)

# ===== 项目工作流 =====

DEFAULT_PROJECTS = {'projects': {}}

# 工作流预设模板
WORKFLOW_TEMPLATES = {
    'software': ['需求分析', '方案设计', '后端开发', '前端开发', '测试联调'],
    'content': ['选题', '调研', '写作', '编辑', '发布'],
    'data': ['采集', '清洗', '建模', '可视化', '报告'],
}

# workspace 路径关键字 → 项目 ID 映射（日志导入自动推断）
WORKSPACE_PROJECT_MAP = {
    'seegent': 'seegent',
    'learnchinese': 'learn-chinese',
    'learn_chinese': 'learn-chinese',
    '识字': 'shizi-app',
}

def _load_projects():
    return _load_json(PROJECTS_FILE, DEFAULT_PROJECTS)

def _save_projects(data):
    _save_json(PROJECTS_FILE, data)

def _guess_phase(agent_name, available_phases):
    """根据 agent 名推断所属阶段。"""
    name_lower = (agent_name or '').lower()
    if any(w in name_lower for w in ['产品', 'pm', 'product', '需求']):
        for p in available_phases:
            if '需求' in p: return p
        return available_phases[0] if available_phases else ''
    if any(w in name_lower for w in ['设计', 'design', '方案', '架构']):
        for p in available_phases:
            if '设计' in p or '方案' in p: return p
        return available_phases[1] if len(available_phases) > 1 else ''
    if any(w in name_lower for w in ['后端', 'backend', 'server', 'api', 'python', 'node', 'go', 'java']):
        for p in available_phases:
            if '后端' in p or '开发' in p: return p
        return available_phases[2] if len(available_phases) > 2 else ''
    if any(w in name_lower for w in ['前端', 'frontend', 'ui', 'ux', 'html', 'css', 'vue', 'react']):
        for p in available_phases:
            if '前端' in p or '界面' in p: return p
        return available_phases[3] if len(available_phases) > 3 else ''
    if any(w in name_lower for w in ['测试', 'test', 'qa', '联调', '部署', 'deploy']):
        for p in available_phases:
            if '测试' in p or '联调' in p or '发布' in p: return p
    # fallback
    return available_phases[0] if available_phases else ''

def _calc_project_summary(proj):
    """计算项目的汇总统计。"""
    steps = proj.get('workflow', {}).get('steps', [])
    total_tokens = sum(s.get('tokens', 0) for s in steps)
    total_cost = sum(s.get('cost', 0) for s in steps)
    total_duration = sum(s.get('duration', 0) for s in steps)
    phases = {}
    for s in steps:
        p = s.get('phase', '未分类')
        if p not in phases:
            phases[p] = {'steps': 0, 'tokens': 0, 'cost': 0, 'duration': 0, 'done': 0}
        phases[p]['steps'] += 1
        phases[p]['tokens'] += s.get('tokens', 0)
        phases[p]['cost'] += s.get('cost', 0)
        phases[p]['duration'] += s.get('duration', 0)
        if s.get('status') == 'done':
            phases[p]['done'] += 1
    template = proj.get('workflow', {}).get('template', [])
    completed_phases = sum(1 for p in template if phases.get(p, {}).get('done', 0) > 0)
    return {
        'totalTokens': total_tokens,
        'totalCost': round(total_cost, 4),
        'totalDuration': total_duration,
        'phases': phases,
        'templateLength': len(template),
        'completedPhases': completed_phases,
    }

# ===== Agent 文件变更追踪 =====

def _track_file_change(workspace, rel_path):
    """记录一个文件被 Agent 修改（用于前端红点通知）"""
    global _file_changes
    now = time.time()
    # 清理过期条目
    _file_changes = {w: {p: ts for p, ts in paths.items() if now - ts < CHANGES_TTL}
                     for w, paths in _file_changes.items()}
    if workspace not in _file_changes:
        _file_changes[workspace] = {}
    _file_changes[workspace][rel_path] = now

def _get_file_changes(workspace=None):
    """获取变更文件列表，workspace 为空则返回所有"""
    global _file_changes
    now = time.time()
    # 清理过期
    _file_changes = {w: {p: ts for p, ts in paths.items() if now - ts < CHANGES_TTL}
                     for w, paths in list(_file_changes.items())}
    _file_changes = {w: paths for w, paths in _file_changes.items() if paths}
    if workspace:
        changes = _file_changes.get(workspace, {})
        return [{'path': p, 'time': ts} for p, ts in sorted(changes.items(), key=lambda x: x[1], reverse=True)]
    return {w: [{'path': p, 'time': ts} for p, ts in sorted(paths.items(), key=lambda x: x[1], reverse=True)]
            for w, paths in _file_changes.items()}

def _clear_file_changes(workspace, path):
    """清除指定路径的红点，path 为文件夹路径时可清除该及其下所有文件"""
    global _file_changes
    if workspace not in _file_changes:
        return
    changes = _file_changes[workspace]
    # 路径匹配：精确或前缀（文件夹包含子文件）
    to_remove = [p for p in changes if p == path or p.startswith(path.rstrip('/') + '/')]
    for p in to_remove:
        del changes[p]
    if not changes:
        del _file_changes[workspace]

# ===== 用量追踪 =====

MODEL_PRICING = {
    'deepseek-v4-flash':  {'input': 0.14, 'output': 0.28},
    'deepseek-v4-pro':    {'input': 0.44, 'output': 0.87},
    'deepseek-chat':      {'input': 0.14, 'output': 0.28},
    'deepseek-reasoner':  {'input': 0.55, 'output': 2.19},
    'gpt-4o':             {'input': 2.50, 'output': 10.00},
    'gpt-4o-mini':        {'input': 0.15, 'output': 0.60},
    'gpt-4.1':            {'input': 2.00, 'output': 8.00},
    'claude-sonnet-4-20250514': {'input': 3.00, 'output': 15.00},
    'claude-haiku-4-5-20251001': {'input': 1.00, 'output': 5.00},
    'claude-opus-4-20250514': {'input': 15.00, 'output': 75.00},
}

def _record_usage(model_id, agent_id, agent_name, prompt_tokens, completion_tokens):
    """记录一次 LLM 调用的 token 用量和费用"""
    today = time.strftime('%Y-%m-%d')
    pricing = MODEL_PRICING.get(model_id, {'input': 0, 'output': 0})
    input_cost = (prompt_tokens / 1_000_000) * pricing['input']
    output_cost = (completion_tokens / 1_000_000) * pricing['output']
    total_cost = round(input_cost + output_cost, 6)

    entry = {
        'date': today,
        'modelId': model_id,
        'agentId': agent_id,
        'agentName': agent_name,
        'promptTokens': prompt_tokens,
        'completionTokens': completion_tokens,
        'totalTokens': prompt_tokens + completion_tokens,
        'cost': total_cost
    }
    data = _load_json(USAGE_FILE, {'usage': []})
    data['usage'].append(entry)
    # 只保留最近 90 天
    cutoff = '2025-01-01' if len(data['usage']) < 5000 else data['usage'][-4000]['date']
    data['usage'] = [e for e in data['usage'] if e['date'] >= cutoff]
    _save_json(USAGE_FILE, data)

def _get_usage(days=None, start_date=None, end_date=None):
    """读取用量数据，支持按天/周/月聚合"""
    data = _load_json(USAGE_FILE, {'usage': []})
    entries = data.get('usage', [])
    if start_date:
        entries = [e for e in entries if e['date'] >= start_date]
    if end_date:
        entries = [e for e in entries if e['date'] <= end_date]
    if days:
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat() if hasattr(datetime, 'date') else ''
        if cutoff:
            entries = [e for e in entries if e['date'] >= cutoff]
    return entries

# ===== 本机 AI 工具监控 =====

# AI 工具注册表：换电脑也通用。type=app 走 /Applications 检测，type=cli 走 which。
# proc_match 用于进程匹配（pgrep），cli 用于检测命令行工具是否安装。
AI_TOOLS = [
    {'id': 'cursor', 'name': 'Cursor', 'type': 'app', 'bundle': 'Cursor.app',
     'emoji': '🔵', 'proc_match': 'Cursor.app', 'cli': 'cursor'},
    {'id': 'claude-code', 'name': 'Claude Code', 'type': 'cli', 'cli': 'claude',
     'emoji': '🤖', 'proc_match': r'node.*claude'},
    {'id': 'chatgpt', 'name': 'ChatGPT', 'type': 'app', 'bundle': 'ChatGPT Atlas.app',
     'emoji': '🟢', 'proc_match': 'ChatGPT Atlas.app'},
    {'id': 'gemini', 'name': 'Gemini', 'type': 'app', 'bundle': 'Gemini.app',
     'emoji': '✦', 'proc_match': 'Gemini.app'},
    {'id': 'codex', 'name': 'Codex CLI', 'type': 'cli', 'cli': 'codex',
     'emoji': '🟧', 'proc_match': r'node.*codex'},
    {'id': 'windsurf', 'name': 'Windsurf', 'type': 'app', 'bundle': 'Windsurf.app',
     'emoji': '🌪️', 'proc_match': 'Windsurf.app'},
    {'id': 'trae', 'name': 'Trae', 'type': 'app', 'bundle': 'Trae.app',
     'emoji': '🚀', 'proc_match': 'Trae.app'},
    {'id': 'aider', 'name': 'Aider', 'type': 'cli', 'cli': 'aider',
     'emoji': '🤝', 'proc_match': r'aider'},
    {'id': 'copilot-cli', 'name': 'GitHub Copilot CLI', 'type': 'cli', 'cli': 'gh',
     'emoji': '🐙', 'proc_match': r'copilot'},
    {'id': 'continue', 'name': 'Continue', 'type': 'cli', 'cli': 'continue',
     'emoji': '⏩', 'proc_match': 'continue'},
]


def _which(cmd):
    """which 命令封装：返回命令路径或 None"""
    try:
        r = subprocess.run(['which', cmd], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _scan_ai_installed():
    """检测本机已安装的 AI 工具。返回 [{id,name,emoji,installed,path,type}]"""
    result = []
    home = os.path.expanduser('~')
    for t in AI_TOOLS:
        item = {'id': t['id'], 'name': t['name'], 'emoji': t['emoji'],
                'type': t['type'], 'installed': False, 'path': ''}
        if t['type'] == 'app':
            bundle = t['bundle']
            for base in ('/Applications', os.path.join(home, 'Applications')):
                p = os.path.join(base, bundle)
                if os.path.isdir(p):
                    item['installed'] = True
                    item['path'] = p
                    break
        else:  # cli
            p = _which(t['cli'])
            if p:
                item['installed'] = True
                item['path'] = p
        result.append(item)
    return result


def _format_uptime(seconds):
    """把秒数格式化为 '2h15m' / '3d5h' / '12m'"""
    if seconds < 60:
        return '<1m'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f'{d}d{h}h'
    if h > 0:
        return f'{h}h{m}m'
    return f'{m}m'


def _scan_ai_processes():
    """扫描运行中的 AI 进程。返回 [{pid,name,tool,cpu,mem,uptime}]。
    资源数据优先用 psutil，否则回退 ps 命令。"""
    procs = []
    running_tool_ids = set()

    # 第一步：找出每个工具的匹配进程 pid
    pid_to_tool = {}  # pid -> tool_id
    for t in AI_TOOLS:
        match = t.get('proc_match', '')
        if not match:
            continue
        try:
            r = subprocess.run(['pgrep', '-lf', match], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    pid = int(parts[0])
                    pid_to_tool[pid] = t['id']
                    running_tool_ids.add(t['id'])
        except Exception:
            continue

    if not pid_to_tool:
        return {'processes': [], 'runningToolIds': []}

    tool_name = {t['id']: t['name'] for t in AI_TOOLS}

    if HAS_PSUTIL:
        for pid, tool_id in pid_to_tool.items():
            try:
                p = psutil.Process(pid)
                info = {
                    'pid': pid,
                    'name': tool_name.get(tool_id, p.name()),
                    'tool': tool_id,
                    'cpu': round(p.cpu_percent(interval=0.1), 1),
                    'mem': round(p.memory_info().rss / 1024 / 1024, 1),  # MB
                    'uptime': _format_uptime(time.time() - p.create_time()),
                }
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    else:
        # 回退：ps -o 解析。一次性取所有目标 pid 的资源数据。
        pids = list(pid_to_tool.keys())
        try:
            r = subprocess.run(
                ['ps', '-o', 'pid=,pcpu=,rss=,etime=,command='] + [str(x) for x in pids],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(None, 4)
                    if len(parts) < 5:
                        continue
                    try:
                        pid = int(parts[0])
                    except ValueError:
                        continue
                    if pid not in pid_to_tool:
                        continue
                    cpu = round(float(parts[1]), 1)
                    mem = round(int(parts[2]) / 1024, 1)  # KB→MB
                    # etime 形如 'dd-hh:mm:ss' 或 'hh:mm:ss' 或 'mm:ss'
                    procs.append({
                        'pid': pid,
                        'name': tool_name.get(pid_to_tool[pid], parts[4][:40]),
                        'tool': pid_to_tool[pid],
                        'cpu': cpu,
                        'mem': mem,
                        'uptime': parts[3],
                    })
        except Exception:
            pass

    # 按 CPU 降序
    procs.sort(key=lambda x: x.get('cpu', 0), reverse=True)
    return {'processes': procs, 'runningToolIds': list(running_tool_ids)}


def _count_ai_calls():
    """统计外部 AI 工具的调用/会话数。返回 {claudeCodeSessions, codexCalls, partnerCliCalls}"""
    result = {'claudeCodeSessions': 0, 'codexCalls': 0, 'partnerCliCalls': 0}

    # Claude Code 会话数 = ~/.claude/projects 下 jsonl 文件数
    home = os.path.expanduser('~')
    claude_projects = os.path.join(home, '.claude', 'projects')
    try:
        result['claudeCodeSessions'] = len(glob.glob(os.path.join(claude_projects, '**', '*.jsonl'), recursive=True))
    except Exception:
        pass

    # Seegent 通过 shell 调用外部 CLI 的次数（扫用量记录中 agentId 为 cli/rest）
    try:
        data = _load_json(USAGE_FILE, {'usage': []})
        cli_calls = sum(1 for e in data.get('usage', []) if e.get('agentId') in ('cli', 'rest'))
        result['partnerCliCalls'] = cli_calls
    except Exception:
        pass

    return result


# 本地 token 用量扫描缓存（opentoken 扫描较慢，30 秒内复用）
_TOKEN_CACHE = {'data': None, 'ts': 0}
_TOKEN_CACHE_TTL = 30

# opentoken 工具名 → 显示名映射
_OPENTOKEN_TOOL_NAMES = {
    'claude-code': 'Claude Code',
    'codex': 'Codex CLI',
    'cursor': 'Cursor',
    'cline': 'Cline',
    'copilot': 'GitHub Copilot',
    'aider': 'Aider',
    'continue': 'Continue',
    'windsurf': 'Windsurf',
    'gemini': 'Gemini CLI',
    'opencode': 'OpenCode',
    'hermes': 'Hermes',
    'workbuddy': 'WorkBuddy',
    'zcode': 'ZCode',
    'minimax': 'MiniMax Code',
    'kimi-code': 'Kimi Code (上下文快照)',
}


def _find_opentoken():
    """查找 opentoken 可执行文件路径：先 ~/.local/bin，再 PATH"""
    home = os.path.expanduser('~')
    p = os.path.join(home, '.local', 'bin', 'opentoken')
    if os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    return _which('opentoken')


def _scan_minimax_usage():
    """读取 MiniMax Code 本地 sqlite(~/.minimax/sqlite.db)的 token_usage 表，
    返回与 opentoken 同形状的 rows（真实逐轮 input/output/cache tokens + 成本）。
    失败/缺失时返回空列表，绝不抛异常。"""
    rows = []
    db = os.path.expanduser('~/.minimax/sqlite.db')
    if not os.path.isfile(db):
        return rows
    try:
        import sqlite3
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT model, ts, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens, cost_usd "
                    "FROM token_usage")
        for r in cur.fetchall():
            ts = r['ts']
            date = ''
            try:
                # ts 为毫秒时间戳
                date = datetime.datetime.fromtimestamp(int(ts) / 1000.0).strftime('%Y-%m-%d')
            except Exception:
                date = ''
            rows.append({
                'tool': 'minimax',
                'date': date,
                'input': int(r['input_tokens'] or 0),
                'output': int(r['output_tokens'] or 0),
                'cache_read': int(r['cache_read_tokens'] or 0),
                'cache_write': int(r['cache_write_tokens'] or 0),
                'model': (r['model'] or 'unknown'),
                'cost': float(r['cost_usd'] or 0),
            })
        con.close()
    except Exception:
        pass
    return rows


def _scan_kimi_usage():
    """读取 Kimi Code 本地上下文占用快照
    (~/Library/Application Support/kimi-desktop/kimi-agent/conversation-context-usage.json)。
    注意：Kimi 本地不保存累计 token 消耗，只有“当前上下文占用”快照，
    因此标记为 _ctx，聚合时不计入全局总量/趋势图。失败/缺失返回空列表。"""
    rows = []
    p = os.path.expanduser('~/Library/Application Support/kimi-desktop/kimi-agent/conversation-context-usage.json')
    if not os.path.isfile(p):
        return rows
    try:
        d = _load_json(p, {})
        if isinstance(d, dict):
            for _cid, info in d.items():
                if not isinstance(info, dict):
                    continue
                ctx = int(info.get('contextTokens', 0) or 0)
                if ctx <= 0:
                    continue
                updated = info.get('updatedAt', '') or ''
                date = updated[:10] if len(updated) >= 10 else ''
                rows.append({
                    'tool': 'kimi-code',
                    'date': date,
                    'input': ctx,
                    'output': 0,
                    'cache_read': 0,
                    'cache_write': 0,
                    'model': (info.get('model') or 'unknown'),
                    '_ctx': True,
                })
    except Exception:
        pass
    return rows


def _scan_local_tokens():
    """调用 opentoken preview --json，解析本地 AI 工具的真实 token 用量。
    返回 {available, tools:[{id,name,total,input,output,cacheRead,cacheWrite,records,models,byDate,byModel}], grandTotal, dateRange}。
    30 秒内复用缓存。"""
    now = time.time()
    if _TOKEN_CACHE['data'] and now - _TOKEN_CACHE['ts'] < _TOKEN_CACHE_TTL:
        return _TOKEN_CACHE['data']

    exe = _find_opentoken()
    raw = None
    opentoken_err = None

    if not exe:
        opentoken_err = 'opentoken 未安装'
    else:
        try:
            r = subprocess.run([exe, 'preview', '--json'], capture_output=True,
                               text=True, timeout=60)
            if r.returncode != 0:
                opentoken_err = 'opentoken 执行失败: ' + r.stderr[:200]
            else:
                raw = json.loads(r.stdout)
                # 适配新旧格式：新版返回 {rows:[...], sessions:[...]}，旧版返回 [...]
                if isinstance(raw, dict):
                    raw = raw.get('rows', [])
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            opentoken_err = '解析失败: ' + str(e)[:200]

        # ---- 并入本地 Kimi / MiniMax 用量（opentoken 尚未覆盖的工具）----
        try:
            _merged = list(raw if isinstance(raw, list) else [])
            _merged += _scan_minimax_usage()
            _merged += _scan_kimi_usage()
            raw = _merged
        except Exception:
            pass

            # 聚合：按 tool 汇总，同时保留 byDate / byModel 明细
    # 真实总消耗 = input + output + cache_read + cache_write（含缓存读取，反映实际 token 吞吐）
    def _real_tokens(rec):
        return (rec.get('input', 0) + rec.get('output', 0)
                + rec.get('cache_read', 0) + rec.get('cache_write', 0))
    tools_map = {}  # tool_id -> aggregates
    global_by_model = {}     # model_name -> total tokens (across all tools)
    global_model_records = {} # model_name -> record count
    global_model_by_date = {} # model_name -> {date: tokens}
    all_dates = set()
    grand_total = 0
    for rec in (raw or []):
        tid = rec.get('tool', 'unknown')
        all_dates.add(rec.get('date', ''))
        if not rec.get('_ctx'):
            grand_total += _real_tokens(rec)
        if tid not in tools_map:
            tools_map[tid] = {
                'id': tid,
                'name': _OPENTOKEN_TOOL_NAMES.get(tid, tid),
                'total': 0, 'input': 0, 'output': 0,
                'cacheRead': 0, 'cacheWrite': 0, 'records': 0,
                'byDate': {}, 'byModel': {},
                '_ctx': bool(rec.get('_ctx', False)),
            }
        agg = tools_map[tid]
        real = _real_tokens(rec)
        agg['total'] += real
        agg['input'] += rec.get('input', 0)
        agg['output'] += rec.get('output', 0)
        agg['cacheRead'] += rec.get('cache_read', 0)
        agg['cacheWrite'] += rec.get('cache_write', 0)
        agg['records'] += 1
        # byDate
        d = rec.get('date', '')
        if d:
            agg['byDate'][d] = agg['byDate'].get(d, 0) + real
        # byModel
        m = rec.get('model', 'unknown')
        agg['byModel'][m] = agg['byModel'].get(m, 0) + real
        # global model aggregation（上下文快照 _ctx 不计入全局模型汇总）
        if not rec.get('_ctx'):
            global_by_model[m] = global_by_model.get(m, 0) + real
            global_model_records[m] = global_model_records.get(m, 0) + 1
            mbyd = global_model_by_date.setdefault(m, {})
            if d:
                mbyd[d] = mbyd.get(d, 0) + real

    # ---- Merge Seegent own usage into token stats ----
    pfm_data = _load_json(USAGE_FILE, {'usage': []})
    pfm_entries = pfm_data.get('usage', [])
    if pfm_entries:
        pfm_tool = {
            'id': 'seegent',
            'name': 'Seegent',
            'total': 0, 'input': 0, 'output': 0,
            'cacheRead': 0, 'cacheWrite': 0, 'records': 0,
            'byDate': {}, 'byModel': {},
        }
        for e in pfm_entries:
            tv = e.get('totalTokens', 0)
            d = e.get('date', '')
            mdl = e.get('modelId', 'unknown')
            pfm_tool['total'] += tv
            pfm_tool['input'] += tv
            pfm_tool['records'] += 1
            all_dates.add(d)
            grand_total += tv
            if d:
                pfm_tool['byDate'][d] = pfm_tool['byDate'].get(d, 0) + tv
            pfm_tool['byModel'][mdl] = pfm_tool['byModel'].get(mdl, 0) + tv
            # global model aggregation for Seegent
            global_by_model[mdl] = global_by_model.get(mdl, 0) + tv
            global_model_records[mdl] = global_model_records.get(mdl, 0) + 1
            mbyd = global_model_by_date.setdefault(mdl, {})
            if d:
                mbyd[d] = mbyd.get(d, 0) + tv
        tools_map['seegent'] = pfm_tool

    # ---- Build global model breakdown list ----
    model_breakdown = []
    for m, total in sorted(global_by_model.items(), key=lambda x: x[1], reverse=True):
        model_breakdown.append({
            'model': m,
            'total': total,
            'records': global_model_records.get(m, 0),
            'byDate': global_model_by_date.get(m, {}),
        })

    tools_list = sorted(tools_map.values(), key=lambda x: x['total'], reverse=True)
    # 每个 tool 的 models 取列表
    for t in tools_list:
        t['models'] = sorted(t['byModel'].keys())

    dates_sorted = sorted(d for d in all_dates if d)

    # 全局按日期聚合（所有工具每日合计）—— 用于历史趋势图
    global_by_date = {}
    # byToolByDate: {date: [{tool, tokens, color_idx}]} —— 用于堆叠/明细
    for t in tools_list:
        if t.get('_ctx'):  # 上下文快照不计入趋势图
            continue
        for d, v in t.get('byDate', {}).items():
            global_by_date[d] = global_by_date.get(d, 0) + v

    # 补齐日期范围内的空缺天（连续天数），让趋势图不断裂
    by_date_full = []
    if dates_sorted:
        from datetime import datetime as _dt, timedelta as _td
        start_d = _dt.strptime(dates_sorted[0], '%Y-%m-%d')
        end_d = _dt.strptime(dates_sorted[-1], '%Y-%m-%d')
        cur = start_d
        while cur <= end_d:
            ds = cur.strftime('%Y-%m-%d')
            by_date_full.append({'date': ds, 'tokens': global_by_date.get(ds, 0)})
            cur += _td(days=1)

    has_data = len(tools_list) > 0
    result = {
        'available': has_data,
        'tools': tools_list,
        'grandTotal': grand_total,
        'dateRange': {'start': dates_sorted[0] if dates_sorted else '',
                      'end': dates_sorted[-1] if dates_sorted else ''},
        'toolCount': len(tools_list),
        'byDate': by_date_full,  # [{date, tokens}] 连续日期序列
        'totalDays': len([x for x in by_date_full if x['tokens'] > 0]),
        'modelBreakdown': model_breakdown,  # [{model, total, records, byDate}]
    }
    if not has_data and opentoken_err:
        result['reason'] = opentoken_err
        result['installHint'] = 'curl -fsSL https://scys.com/tokenrank/install.sh | sh'
    _TOKEN_CACHE['data'] = result
    _TOKEN_CACHE['ts'] = now
    return result


# ===== Embedding 配置 + SQLite 向量索引 =====

DEFAULT_EMBEDDING = {
    "provider": "",
    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    "model": "text-embedding-3-small",
    "dimensions": 0  # 0=未测过，首次调用后自动填
}


# embedding 内存缓存：text -> vector（避免同一 query 重复算）
_embed_cache = {}
_EMBED_CACHE_MAX = 200

_db_lock = threading.Lock()


def _get_embedding_config():
    """读取 embedding 配置，缺失字段用默认值补全。"""
    cfg = _load_json(EMBEDDING_FILE, dict(DEFAULT_EMBEDDING))
    merged = dict(DEFAULT_EMBEDDING)
    merged.update(cfg)
    return merged


def _embed_texts(texts):
    """批量把文本转成向量。返回 list[list[float]]，失败抛异常。
    走配置的 OpenAI 兼容 embedding API（/embeddings 端点）。"""
    if not texts:
        return []
    cfg = _get_embedding_config()
    if not cfg.get('api_key') or not cfg.get('base_url') or not cfg.get('model'):
        raise ValueError('未配置 embedding，请在模型管理模块配置「向量检索」')
    # 拆出未缓存的
    todo_idx = [i for i, t in enumerate(texts) if t not in _embed_cache]
    results = [None] * len(texts)
    for i, t in enumerate(texts):
        if t in _embed_cache:
            results[i] = _embed_cache[t]
    if todo_idx:
        todo_texts = [texts[i] for i in todo_idx]
        url = cfg['base_url'].rstrip('/') + '/embeddings'
        payload = json.dumps({'model': cfg['model'], 'input': todo_texts}).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers={
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {cfg['api_key']}"
        }, method='POST')
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        vecs = [item['embedding'] for item in data['data']]
        # 记录维度
        if vecs and not cfg.get('dimensions'):
            cfg['dimensions'] = len(vecs[0])
            _save_json(EMBEDDING_FILE, cfg)
        for idx, t, v in zip(todo_idx, todo_texts, vecs):
            results[idx] = v
            if len(_embed_cache) >= _EMBED_CACHE_MAX:
                _embed_cache.pop(next(iter(_embed_cache)))  # 淘汰最老的
            _embed_cache[t] = v
    return results


def _init_db():
    """初始化 SQLite 索引库（幂等）。"""
    with _db_lock:
        conn = sqlite3.connect(INDEX_DB)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace TEXT NOT NULL,
            file_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB,
            file_mtime REAL,
            indexed_at REAL,
            UNIQUE(workspace, file_path, chunk_index)
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ws_file ON chunks(workspace, file_path)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_workspace ON chunks(workspace)')
        conn.commit()
        conn.close()


def _chunk_text(text, size=500, overlap=100):
    """把长文本切成带重叠的小块。"""
    if len(text) <= size:
        return [text]
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i:i + size])
        if i + size >= len(text):
            break
        i += max(1, size - overlap)
    return chunks


def _iter_text_files(root):
    """递归遍历 root 下的文本类文件，yield (abspath, relpath, mtime)。"""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if fname.startswith('.'):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext and ext not in TEXT_EXTS:
                continue
            fp = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                continue
            yield fp, os.path.relpath(fp, root), mtime


def _iter_image_files(root):
    """递归遍历 root 下的图片文件，yield (abspath, relpath, mtime)。"""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if fname.startswith('.'):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            fp = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                continue
            yield fp, os.path.relpath(fp, root), mtime


def _ocr_image(filepath):
    """对图片文件做 OCR 文字识别，返回识别出的文本。失败返回空字符串。"""
    if not _OCR_AVAILABLE:
        return ''
    try:
        img = Image.open(filepath)
        # 如果图片太大，缩小以提高 OCR 速度
        w, h = img.size
        max_dim = 2000
        if w > max_dim or h > max_dim:
            ratio = max_dim / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        # 支持中英文
        text = pytesseract.image_to_string(img, lang='chi_sim+eng')
        return text.strip()
    except Exception:
        return ''


def _extract_pdf_text(filepath):
    """用 pdfplumber 提取 PDF 文本内容。失败返回空字符串。"""
    if not _PDF_AVAILABLE:
        return ''
    try:
        with pdfplumber.open(filepath) as pdf:
            texts = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    texts.append(t)
            return '\n'.join(texts)
    except Exception:
        return ''


def _extract_docx_text(filepath):
    """用 python-docx 提取 .docx 文本内容。失败返回空字符串。"""
    if not _DOCX_AVAILABLE:
        return ''
    try:
        doc = docx.Document(filepath)
        texts = [p.text for p in doc.paragraphs if p.text.strip()]
        return '\n'.join(texts)
    except Exception:
        return ''


def _extract_doc_text(filepath):
    """用 LibreOffice 将 .doc 转为 PDF 后提取文本。失败返回空字符串。"""
    if not _PDF_AVAILABLE:
        return ''
    lo_path = _find_libreoffice()
    if not lo_path:
        return ''
    tmpdir = tempfile.mkdtemp(prefix='seegent-doc-')
    try:
        result = subprocess.run(
            [lo_path, '--headless', '--convert-to', 'pdf', '--outdir', tmpdir, filepath],
            capture_output=True, text=True, timeout=60
        )
        base = os.path.splitext(os.path.basename(filepath))[0]
        pdf_path = os.path.join(tmpdir, base + '.pdf')
        if not os.path.exists(pdf_path):
            pdfs = [f for f in os.listdir(tmpdir) if f.endswith('.pdf')]
            if pdfs:
                pdf_path = os.path.join(tmpdir, pdfs[0])
            else:
                return ''
        return _extract_pdf_text(pdf_path)
    except Exception:
        return ''
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _read_file_content(filepath):
    """根据文件扩展名选择合适的读取方式，返回文本内容。"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.pdf':
        return _extract_pdf_text(filepath)
    elif ext == '.docx':
        return _extract_docx_text(filepath)
    elif ext == '.doc':
        return _extract_doc_text(filepath)
    else:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except OSError:
            return ''


def _index_workspace(wpath, progress_cb=None):
    """增量索引一个工作区。
    progress_cb(done, total, msg) 用于 SSE 进度推送。
    返回 (indexed_count, skipped_count, removed_count)。"""
    _init_db()
    ws_key = os.path.normpath(wpath)
    # 1. 收集当前文件清单
    files = list(_iter_text_files(wpath))
    total = len(files)

    # 2. 读出已索引的文件 mtime
    with _db_lock:
        conn = sqlite3.connect(INDEX_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            'SELECT file_path, MAX(file_mtime) AS mtime FROM chunks WHERE workspace=? GROUP BY file_path',
            (ws_key,))
        indexed = {row['file_path']: row['mtime'] for row in cur.fetchall()}
        conn.close()

    # 3. 找需要更新的（新增或 mtime 变了）
    todo = [(fp, rel, mt) for fp, rel, mt in files
            if rel not in indexed or abs((indexed.get(rel) or 0) - mt) > 1]
    # 4. 找已删除的
    current_rels = {rel for _, rel, _ in files}
    removed = [rel for rel in indexed if rel not in current_rels]

    # 5. 删除已失效的
    if removed:
        with _db_lock:
            conn = sqlite3.connect(INDEX_DB)
            conn.executemany(
                'DELETE FROM chunks WHERE workspace=? AND file_path=?',
                [(ws_key, rel) for rel in removed])
            conn.commit()
            conn.close()

    # 6. 索引 todo 中的文件
    indexed_count = 0
    skipped = 0
    for i, (fp, rel, mt) in enumerate(todo):
        try:
            content = _read_file_content(fp)
        except Exception:
            skipped += 1
            continue
        if not content.strip():
            skipped += 1
            continue
        chunks = _chunk_text(content)
        # 删旧 chunks
        with _db_lock:
            conn = sqlite3.connect(INDEX_DB)
            conn.execute('DELETE FROM chunks WHERE workspace=? AND file_path=?', (ws_key, rel))
            conn.close()
        # 批量 embedding（限流：一次最多 20 块）
        now = time.time()
        for batch_start in range(0, len(chunks), 20):
            batch = chunks[batch_start:batch_start + 20]
            try:
                vecs = _embed_texts(batch)
            except Exception as e:
                skipped += len(batch)
                if progress_cb:
                    progress_cb(i + 1, len(todo), f'embedding 失败：{e}')
                continue
            rows = []
            for ci, (text, vec) in enumerate(zip(batch, vecs), start=batch_start):
                blob = sqlite3.Binary(_vec_to_bytes(vec)) if vec else None
                rows.append((ws_key, rel, ci, text, blob, mt, now))
            with _db_lock:
                conn = sqlite3.connect(INDEX_DB)
                conn.executemany(
                    'INSERT OR REPLACE INTO chunks(workspace,file_path,chunk_index,text,embedding,file_mtime,indexed_at) VALUES(?,?,?,?,?,?,?)',
                    rows)
                conn.commit()
                conn.close()
            indexed_count += len(batch)
        if progress_cb and (i % 5 == 0 or i == len(todo) - 1):
            progress_cb(i + 1, len(todo), f'已索引 {rel}')

    # 7. 索引图片文件（OCR 文字识别）
    ocr_count = 0
    ocr_skipped = 0
    if _OCR_AVAILABLE:
        img_files = list(_iter_image_files(wpath))
        img_todo = [(fp, rel, mt) for fp, rel, mt in img_files
                    if rel not in indexed or abs((indexed.get(rel) or 0) - mt) > 1]
        # 清理已删除的图片
        img_current_rels = {rel for _, rel, _ in img_files}
        img_removed = [rel for rel in indexed if rel not in img_current_rels]
        if img_removed:
            with _db_lock:
                conn = sqlite3.connect(INDEX_DB)
                conn.executemany(
                    'DELETE FROM chunks WHERE workspace=? AND file_path=?',
                    [(ws_key, rel) for rel in img_removed])
                conn.commit()
                conn.close()
        now = time.time()
        for i, (fp, rel, mt) in enumerate(img_todo):
            try:
                ocr_text = _ocr_image(fp)
            except Exception:
                ocr_skipped += 1
                continue
            if not ocr_text:
                ocr_skipped += 1
                continue
            # 删除旧图片 chunks
            with _db_lock:
                conn = sqlite3.connect(INDEX_DB)
                conn.execute('DELETE FROM chunks WHERE workspace=? AND file_path=?', (ws_key, rel))
                conn.close()
            # 图片 OCR 文本也分块，但不建 embedding（embedding 为 NULL）
            chunks = _chunk_text(ocr_text)
            rows = []
            for ci, text in enumerate(chunks):
                rows.append((ws_key, rel, ci, text, None, mt, now))
            with _db_lock:
                conn = sqlite3.connect(INDEX_DB)
                conn.executemany(
                    'INSERT OR REPLACE INTO chunks(workspace,file_path,chunk_index,text,embedding,file_mtime,indexed_at) VALUES(?,?,?,?,?,?,?)',
                    rows)
                conn.commit()
                conn.close()
            ocr_count += len(chunks)
            if progress_cb and (i % 3 == 0 or i == len(img_todo) - 1):
                progress_cb(i + 1, len(img_todo), f'OCR 识别 {rel}')
        if progress_cb:
            progress_cb(len(img_todo), len(img_todo),
                        f'图片 OCR 完成：{len(img_todo)} 张，识别 {ocr_count} 块，跳过 {ocr_skipped} 张')

    return indexed_count + ocr_count, skipped + ocr_skipped, len(removed)


def _vec_to_bytes(vec):
    """list[float] -> bytes（用 numpy 降精度到 float32 省空间）。"""
    if np is None:
        import struct
        return struct.pack(f'{len(vec)}f', *vec)
    return np.asarray(vec, dtype=np.float32).tobytes()


def _bytes_to_vec(blob):
    """bytes -> numpy float32 向量。"""
    if np is None:
        import struct
        return list(struct.unpack(f'{len(blob)//4}f', blob))
    return np.frombuffer(blob, dtype=np.float32)


def _search_semantic(wpath, query, top_k=8):
    """语义搜索：query → 向量 → 余弦相似 top-k chunks。
    返回 list[dict]：{file_path, text, score}。"""
    if np is None:
        return [{'error': 'numpy 未安装，无法做向量检索'}]
    cfg = _get_embedding_config()
    if not cfg.get('api_key'):
        return [{'error': '未配置 embedding，请在模型管理配置向量检索'}]
    ws_key = os.path.normpath(wpath)
    # query 向量
    try:
        qvecs = _embed_texts([query])
    except Exception as e:
        return [{'error': f'embedding 失败：{e}'}]
    if not qvecs:
        return []
    qvec = np.asarray(qvecs[0], dtype=np.float32)
    qnorm = np.linalg.norm(qvec) + 1e-8
    # 取所有 chunks
    with _db_lock:
        conn = sqlite3.connect(INDEX_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            'SELECT file_path, chunk_index, text, embedding FROM chunks WHERE workspace=? AND embedding IS NOT NULL',
            (ws_key,))
        rows = cur.fetchall()
        conn.close()
    if not rows:
        return [{'error': '该工作区尚未建立索引，请先在侧边栏点击索引图标'}]
    scored = []
    for r in rows:
        vec = _bytes_to_vec(r['embedding'])
        vnorm = np.linalg.norm(vec) + 1e-8
        sim = float(np.dot(qvec, vec) / (qnorm * vnorm))
        scored.append((sim, r['file_path'], r['chunk_index'], r['text']))
    scored.sort(reverse=True)
    # 按文件去重，每个文件最多取 1 个最高分块
    seen = {}
    result = []
    for sim, fp, ci, text in scored:
        if fp in seen:
            continue
        seen[fp] = True
        snippet = text[:300].replace('\n', ' ')
        result.append({'file_path': fp, 'score': round(sim, 3), 'snippet': snippet})
        if len(result) >= top_k:
            break
    return result


def _index_status(wpath):
    """返回某工作区的索引状态。"""
    _init_db()
    ws_key = os.path.normpath(wpath)
    with _db_lock:
        conn = sqlite3.connect(INDEX_DB)
        cur = conn.execute(
            'SELECT COUNT(DISTINCT file_path) AS files, MAX(indexed_at) AS last FROM chunks WHERE workspace=?',
            (ws_key,))
        row = cur.fetchone()
        conn.close()
    indexed_files, last = row[0] or 0, row[1]
    # 对比当前文件数判断是否过期
    current_files = sum(1 for _ in _iter_text_files(wpath))
    stale = current_files != indexed_files
    return {'indexed_files': indexed_files, 'current_files': current_files,
            'last_indexed': last, 'stale': stale}


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_DELETE(self):
        # SimpleHTTPRequestHandler 不支持 DELETE，委托给 do_GET 处理
        self.do_GET()

    def do_GET(self):
        # self.path 可能包含完整 URL，提取纯路径
        if '://' in self.path:
            from urllib.parse import urlparse
            self.path = urlparse(self.path).path
        # 根路径和 index.html 从包内 static/ 目录提供
        if self.path in ('/', '/index.html'):
            return self._serve_static('index.html')
        if self.path == '/api/state':
            return self._serve_json(_load_json(STATE_FILE, {}))
        if self.path == '/api/models':
            return self._serve_json(_load_json(MODELS_FILE, {"models": []}))
        if self.path == '/api/cli':
            data = _load_json(CLI_FILE, {"items": DEFAULT_CLI, "enabled": list(DEFAULT_CLI.keys())})
            return self._serve_json(data)
        if self.path == '/api/mcp':
            data = _load_json(MCP_FILE, {"items": DEFAULT_MCP, "enabled": list(DEFAULT_MCP.keys())})
            return self._serve_json(data)
        if self.path == '/api/chats':
            return self._serve_json(_load_json(CHAT_FILE, DEFAULT_CHATS))
        if self.path == '/api/prompts':
            return self._serve_json(_load_json(PROMPTS_FILE, DEFAULT_PROMPTS))
        if self.path == '/api/workspaces':
            return self._serve_json(_load_json(WORKSPACES_FILE, DEFAULT_WORKSPACES))
        if self.path == '/api/skills':
            return self._serve_json(_load_json(SKILLS_FILE, DEFAULT_SKILLS))
        if self.path == '/api/roles':
            return self._serve_json(_handle_roles_get())
        if self.path == '/api/team':
            return self._serve_json(_scan_team_source(_team_path()))
        if self.path == '/api/settings/paths':
            return _handle_paths_config(self)
        if self.path.startswith('/api/fs/browse'):
            return _handle_fs_browse(self)
        if self.path.startswith('/api/team/tree'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            rel = qs.get('rel', [''])[0]
            return self._serve_json(_scan_team_tree(rel))
        if self.path.startswith('/api/team/file'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            rel = qs.get('rel', [''])[0]
            data, err = _read_team_file(rel)
            if err:
                return self._serve_json({'error': err}, 400)
            return self._serve_json(data)
        if self.path.startswith('/api/team/folder-text'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            rel = qs.get('rel', [''])[0]
            return self._serve_json({'text': _collect_team_folder_text(rel)})
        # 技能模块文件夹树（与团队同款，根目录/接口前缀换成 skill）
        if self.path.startswith('/api/skill/tree') or self.path.startswith('/api/skill/file'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = qs.get('rel', [''])[0]
            if self.path.startswith('/api/skill/tree'):
                return self._serve_json(_scan_skill_tree(sid))
            data, err = _read_skill_file(sid)
            if err:
                return self._serve_json({'error': err}, 400)
            return self._serve_json(data)
        if self.path.startswith('/api/skill/folder-text'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            rel = qs.get('rel', [''])[0]
            return self._serve_json({'text': _collect_skill_folder_text(rel)})
        if self.path == '/api/personal-skills':
            sp = _skill_path()
            skills = _scan_skill_source(sp)
            return self._serve_json({'root': sp, 'skills': skills, 'empty': not skills})
        if self.path == '/api/skilllib/index':
            return self._serve_json(_load_json(SKILLLIB_INDEX_FILE, {'version': 1, 'sources': [], 'skills': []}))
        if self.path.startswith('/api/skilllib/tree') or self.path.startswith('/api/skilllib/file'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = qs.get('skillId', [None])[0]
            s = _skilllib_find_skill(sid) if sid else None
            if not s:
                return self._serve_json({'error': 'skill 不存在'}, 404)
            if self.path.startswith('/api/skilllib/tree'):
                if s.get('isFileSkill'):
                    fp = os.path.join(s['dirPath'], s['entryName'])
                    try:
                        sz = os.path.getsize(fp)
                    except Exception:
                        sz = 0
                    return self._serve_json({'skillId': sid, 'sub': s['entryName'],
                                             'entries': [{'name': s['entryName'], 'isDir': False, 'sub': s['entryName'], 'size': sz}]})
                sub = qs.get('sub', [''])[0]
                return self._serve_json({'skillId': sid, 'sub': sub, 'entries': _skilllib_build_tree(s['dirPath'], sub)})
            sub = qs.get('sub', [''])[0]
            if s.get('isFileSkill') and (not sub or sub == s['entryName']):
                sub = s['entryName']
            data, err = _skilllib_read_file(s['dirPath'], sub)
            if err:
                return self._serve_json({'error': err}, 400)
            return self._serve_json(data)
        # 看板配置（按项目隔离）
        if self.path.startswith('/api/dashboard/config'):
            return self._handle_dashboard_config_get()
        if self.path == '/api/board/projects':
            return self._handle_board_projects_get()
        if self.path.startswith('/api/board/bind/') and self.command == 'DELETE':
            return self._handle_board_bind_delete()
        # 本地文件看板：监视目录
        if self.path == '/api/filewatch':
            return self._handle_filewatch_get()
        if self.path == '/api/filewatch/pick':
            return self._handle_filewatch_pick()
        if self.path.startswith('/api/filewatch/') and self.command == 'DELETE':
            return self._handle_filewatch_delete()
        # Agent 文件变更红点通知
        if self.path == '/api/file-changes':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            workspace = qs.get('workspace', [None])[0]
            return self._serve_json({'changes': _get_file_changes(workspace)})
        if self.path == '/api/recent-activity':
            return self._handle_recent_activity()
        if self.path == '/api/file-changes/clear' and self.command == 'POST':
            body = self._read_body()
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                return self._serve_json({'error': 'Invalid JSON'}, 400)
            workspace = data.get('workspace', '')
            path = data.get('path', '')
            if not workspace or not path:
                return self._serve_json({'error': 'Missing workspace or path'}, 400)
            _clear_file_changes(workspace, path)
            return self._serve_json({'ok': True})
        if self.path == '/api/agent-config':
            return self._serve_json({
                'tools': ['list_dir', 'read_file', 'write_file', 'edit_file', 'search_files',
                          'semantic_search', 'file_stats', 'recent_files',
                          'web_search', 'web_fetch'],
                'max_iterations': 10
            })
        if self.path.startswith('/api/logs'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            limit = int(qs.get('limit', [100])[0])
            agent_id = qs.get('agent', [None])[0]
            status = qs.get('status', [None])[0]
            source = qs.get('source', [None])[0]
            return self._serve_json({'logs': _merge_all_logs(limit=limit, agent_id=agent_id, status=status, source=source)})
        if self.path.startswith('/api/usage'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            days = int(qs.get('days', [30])[0])
            entries = _get_usage(days=days)
            # 按日期聚合
            by_date = {}
            by_model = {}
            by_agent = {}
            for e in entries:
                d = e['date']
                m = e['modelId']
                a = e['agentName'] or e['agentId']
                if d not in by_date:
                    by_date[d] = {'tokens': 0, 'cost': 0, 'models': {}}
                by_date[d]['tokens'] += e['totalTokens']
                by_date[d]['cost'] += e['cost']
                if m not in by_date[d]['models']:
                    by_date[d]['models'][m] = {'tokens': 0, 'cost': 0, 'calls': 0}
                by_date[d]['models'][m]['tokens'] += e['totalTokens']
                by_date[d]['models'][m]['cost'] += e['cost']
                by_date[d]['models'][m]['calls'] += 1
                if m not in by_model:
                    by_model[m] = {'tokens': 0, 'cost': 0, 'calls': 0}
                by_model[m]['tokens'] += e['totalTokens']
                by_model[m]['cost'] += e['cost']
                by_model[m]['calls'] += 1
                if a not in by_agent:
                    by_agent[a] = {'tokens': 0, 'cost': 0, 'calls': 0}
                by_agent[a]['tokens'] += e['totalTokens']
                by_agent[a]['cost'] += e['cost']
                by_agent[a]['calls'] += 1

            total_tokens = sum(e['totalTokens'] for e in entries)
            total_cost = sum(e['cost'] for e in entries)

            return self._serve_json({
                'totalTokens': total_tokens,
                'totalCost': round(total_cost, 4),
                'days': len(by_date),
                'byDate': {d: {'tokens': v['tokens'], 'cost': round(v['cost'],4), 'models': {m: {'tokens': vm['tokens'], 'cost': round(vm['cost'],4), 'calls': vm['calls']} for m,vm in v['models'].items()}} for d,v in sorted(by_date.items())},
                'byModel': {m: {'tokens': v['tokens'], 'cost': round(v['cost'],4), 'calls': v['calls']} for m,v in sorted(by_model.items())},
                'byAgent': {a: {'tokens': v['tokens'], 'cost': round(v['cost'],4), 'calls': v['calls']} for a,v in sorted(by_agent.items())},
            })
        if self.path.startswith('/api/ai-monitor'):
            installed = _scan_ai_installed()
            proc_data = _scan_ai_processes()
            # 用运行中的工具 id 标记 installed 列表的状态
            running_ids = set(proc_data.get('runningToolIds', []))
            for t in installed:
                if t['installed'] and t['id'] in running_ids:
                    t['status'] = 'running'
                elif t['installed']:
                    t['status'] = 'idle'
                else:
                    t['status'] = 'missing'
            return self._serve_json({
                'tools': installed,
                'processes': proc_data.get('processes', []),
                'calls': _count_ai_calls(),
                'hasPsutil': HAS_PSUTIL,
                'tokens': _scan_local_tokens(),
            })
        if self.path == '/api/embedding-config':
            return self._serve_json(_get_embedding_config())
        # 项目追踪
        if '/api/tracks' in self.path:
            if '/api/tracks/' in self.path and self.command == 'DELETE':
                return self._handle_tracks_delete()
            return self._handle_tracks_get()
        # 项目工作流
        if '/api/projects' in self.path:
            if '/api/projects/' in self.path and self.command == 'DELETE':
                return self._handle_project_delete()
            return self._handle_projects_get()
        # index-status 支持 ?workspace=xxx
        if self.path.startswith('/api/index-status'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ws = qs.get('workspace', [''])[0]
            ws_path = os.path.expanduser(ws) if ws else None
            if not ws_path or not os.path.isdir(ws_path):
                return self._serve_json({'error': 'workspace 路径无效'}, 400)
            try:
                return self._serve_json(_index_status(ws_path))
            except Exception as e:
                return self._serve_json({'error': str(e)}, 500)
        if self.path == '/api/mcp-status':
            mcp_data = _load_json(MCP_FILE, {'items': DEFAULT_MCP, 'enabled': list(DEFAULT_MCP.keys())})
            items = mcp_data.get('items', {})
            enabled_ids = mcp_data.get('enabled', [])
            status = {}
            for sid in enabled_ids:
                cfg = items.get(sid, {})
                cmd = cfg.get('command', '')
                if cmd:
                    client = _get_mcp_client(sid, cmd)
                    # Ensure running
                    if not client.process or client.process.poll() is not None:
                        client.start()
                    status[sid] = client.get_status()
                else:
                    status[sid] = {'running': False, 'initialized': False, 'tool_count': 0, 'error': '无启动命令'}
            return self._serve_json({'status': status, 'enabled': enabled_ids})
        if self.path == '/api/health':
            return self._serve_json({'ok': True})
        if self.path == '/api/engines':
            return self._serve_engines()
        if self.path.startswith('/api/datasources'):
            if self.path.startswith('/api/datasources/bind'):
                self.send_error(405)  # Method not allowed for GET
                return
            if self.command == 'DELETE':
                return self._handle_datasources_delete()
            return self._handle_datasources()
        if self.path == '/api/feishu-credentials':
            return self._handle_feishu_credentials_get()
        return super().do_GET()

    def do_POST(self):
        # self.path 可能包含完整 URL（如 http://127.0.0.1:8765/api/datasources），提取纯路径
        if '://' in self.path:
            from urllib.parse import urlparse
            self.path = urlparse(self.path).path

        if self.path == '/api/state':
            return self._save_json_endpoint(STATE_FILE)
        if self.path == '/api/models':
            return self._save_json_endpoint(MODELS_FILE)
        if self.path == '/api/cli':
            return self._save_json_endpoint(CLI_FILE)
        if self.path == '/api/mcp':
            return self._save_json_endpoint(MCP_FILE)
        if self.path == '/api/chats':
            return self._save_json_endpoint(CHAT_FILE)
        if self.path == '/api/prompts':
            return self._save_json_endpoint(PROMPTS_FILE)
        if self.path == '/api/workspaces':
            return self._save_json_endpoint(WORKSPACES_FILE)
        if self.path == '/api/skills':
            return self._save_json_endpoint(SKILLS_FILE)
        if self.path == '/api/roles':
            # 只持久化用户自定义角色 + 当前选择；团队角色是动态派生的，不入文件
            body = self._read_body()
            if not isinstance(body, dict):
                body = {}
            saved = body.get('roles', []) or []
            user_custom = [
                r for r in saved
                if isinstance(r, dict) and r.get('id') not in BUILTIN_ROLE_IDS and not r.get('isTeam')
            ]
            _save_json(ROLES_FILE, {'roles': user_custom, 'activeRole': body.get('activeRole', '') or ''})
            return self._serve_json({'ok': True})
        if self.path == '/api/open-in-finder':
            return _handle_open_in_finder(self)
        if self.path == '/api/settings/paths':
            return _handle_paths_config(self)
        if self.path == '/api/skilllib/sources':
            body = self._read_body()
            if not isinstance(body, dict):
                body = {}
            action = body.get('action', 'add')
            path = (body.get('path') or '').strip()
            if not path:
                return self._serve_json({'error': 'path 必填'}, 400)
            path = os.path.expanduser(path)
            if not os.path.isdir(path):
                return self._serve_json({'error': '目录不存在: ' + path}, 400)
            index = _load_json(SKILLLIB_INDEX_FILE, {'version': 1, 'sources': [], 'skills': []})
            index.setdefault('sources', [])
            index.setdefault('skills', [])
            if action == 'remove':
                index['sources'] = [s for s in index['sources'] if s.get('path') != path]
            else:
                src_id = 'src_' + hashlib.sha1(path.encode('utf-8')).hexdigest()[:10]
                name = body.get('name') or os.path.basename(path.rstrip('/')) or path
                found = False
                for s in index['sources']:
                    if s.get('path') == path:
                        s['name'] = name
                        s['addedAt'] = s.get('addedAt', int(time.time() * 1000))
                        found = True
                        break
                if not found:
                    index['sources'].append({
                        'id': src_id, 'path': path, 'name': name,
                        'addedAt': int(time.time() * 1000), 'skillCount': 0
                    })
            try:
                index = _rebuild_skill_index_from(index)
            except Exception as e:
                return self._serve_json({'error': str(e)}, 500)
            return self._serve_json(index)
        if self.path == '/api/skilllib/rescan':
            try:
                index = _rebuild_skill_index()
                return self._serve_json(index)
            except Exception as e:
                return self._serve_json({'error': str(e)}, 500)
        if self.path == '/api/skilllib/explain':
            return self._handle_skilllib_explain()
        if self.path.startswith('/api/convert-office'):
            return self._convert_office()
        if self.path == '/api/embedding-config':
            return self._save_embedding_config()
        if self.path == '/api/test-embedding':
            return self._test_embedding()
        if self.path == '/api/reindex':
            return self._reindex()
        if self.path.startswith('/api/chat'):
            return self._engine_chat()
        if self.path == '/api/agent':
            return self._agent_loop()
        if self.path == '/api/mcp-discover':
            return self._mcp_discover()
        if self.path.startswith('/api/dashboard'):
            return self._handle_dashboard_post()
        if self.path == '/api/workbuddy':
            return self._proxy_workbuddy()
        if self.path.endswith('/api/tracks'):
            return self._handle_tracks_post()
        if self.path.endswith('/api/projects'):
            return self._handle_projects_post()
        if self.path == '/api/datasources' or self.path.startswith('/api/datasources?'):
            return self._handle_datasources_post()
        if self.path == '/api/datasources/bind':
            return self._handle_datasources_bind()
        if self.path == '/api/board/bind':
            return self._handle_board_bind_post()
        if self.path == '/api/filewatch':
            return self._handle_filewatch_post()
        if self.path == '/api/feishu-credentials':
            return self._handle_feishu_credentials_post()
        self.send_error(404)

    def _find_libreoffice(self):
        """Find the LibreOffice executable."""
        for path in [
            '/Applications/LibreOffice.app/Contents/MacOS/soffice',
            'soffice', 'libreoffice',
        ]:
            if shutil.which(path):
                return path
        return None

    def _save_embedding_config(self):
        """保存 embedding 配置（provider/key/base_url/model）。"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._serve_json({'error': '无效的 JSON'}, 400)
        # 合并到现有配置（避免丢字段）
        cfg = _get_embedding_config()
        cfg.update({k: v for k, v in data.items() if k in
                    ('provider', 'api_key', 'base_url', 'model', 'dimensions')})
        _save_json(EMBEDDING_FILE, cfg)
        return self._serve_json({'ok': True})

    def _test_embedding(self):
        """测试 embedding 配置是否可用：发一条测试文本，返回维度。"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}
        test_text = data.get('text', '你好，这是一条测试文本。')
        try:
            vecs = _embed_texts([test_text])
            if vecs and vecs[0]:
                dim = len(vecs[0])
                # 顺便把测出来的维度存进配置
                cfg = _get_embedding_config()
                if cfg.get('dimensions') != dim:
                    cfg['dimensions'] = dim
                    _save_json(EMBEDDING_FILE, cfg)
                return self._serve_json({'ok': True, 'dimensions': dim})
            return self._serve_json({'error': 'embedding 返回空向量'}, 400)
        except Exception as e:
            return self._serve_json({'error': str(e)}, 400)

    def _reindex(self):
        """触发某工作区重建索引，SSE 流式推送进度。
        请求体：{workspace: '/path/to/folder'}"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}
        ws = data.get('workspace', '')
        wpath = os.path.expanduser(ws) if ws else None
        if not wpath or not os.path.isdir(wpath):
            return self._serve_json({'error': f'工作区路径无效：{ws}'}, 400)

        # 开始 SSE 流
        self._start_sse()

        def progress_cb(done, total, msg):
            self._serve_sse('progress', {'done': done, 'total': total, 'message': msg})

        try:
            self._serve_sse('start', {'workspace': wpath})
            indexed, skipped, removed = _index_workspace(wpath, progress_cb)
            self._serve_sse('done', {
                'indexed': indexed, 'skipped': skipped, 'removed': removed
            })
        except Exception as e:
            self._serve_sse('error', {'message': str(e)})

    def _convert_office(self):
        """Convert uploaded Office file to PDF using LibreOffice."""
        # Read binary body
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            self._serve_json({'error': '未收到文件'}, 400)
            return

        # Extract filename from query string
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        filename = qs.get('name', ['document'])[0]
        if not filename:
            self._serve_json({'error': '缺少文件名'}, 400)
            return

        body = self.rfile.read(length)

        # Check LibreOffice availability
        lo_path = self._find_libreoffice()
        if not lo_path:
            self._serve_json({
                'error': '未找到 LibreOffice。请运行：brew install --cask libreoffice'
            }, 500)
            return

        # Save to temp directory and convert
        tmpdir = tempfile.mkdtemp(prefix='seegent-')
        try:
            input_path = os.path.join(tmpdir, filename)
            with open(input_path, 'wb') as f:
                f.write(body)

            # Run LibreOffice headless conversion
            result = subprocess.run(
                [lo_path, '--headless', '--convert-to', 'pdf', '--outdir', tmpdir, input_path],
                capture_output=True, text=True, timeout=60
            )

            # Find the output PDF
            base = os.path.splitext(filename)[0]
            pdf_path = os.path.join(tmpdir, base + '.pdf')
            if not os.path.exists(pdf_path):
                # Try glob
                pdfs = [f for f in os.listdir(tmpdir) if f.endswith('.pdf')]
                if pdfs:
                    pdf_path = os.path.join(tmpdir, pdfs[0])
                else:
                    self._serve_json({
                        'error': f'转换失败：{result.stderr.strip() or "未生成 PDF 文件"}'
                    }, 500)
                    return

            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()

            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', str(len(pdf_data)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(pdf_data)

        except subprocess.TimeoutExpired:
            self._serve_json({'error': '转换超时，文件可能过大'}, 500)
        except Exception as e:
            self._serve_json({'error': f'转换出错：{e}'}, 500)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ========== 通用引擎路由 ==========

    def _serve_engines(self):
        """GET /api/engines — 返回引擎配置（脱敏后）"""
        data = _load_json(ENGINES_FILE, {"engines": {}, "ui": {"sections": []}})
        safe = json.loads(json.dumps(data))

        # 兼容旧 models.json 中的 key
        old_models = _load_json(MODELS_FILE, {"models": []})
        old_providers = old_models.get('providers', {})

        for eid, cfg in safe.get('engines', {}).items():
            key = cfg.get('api_key', '')
            # 解析环境变量占位符
            if key.startswith('${') and key.endswith('}'):
                env_var = key[2:-1]
                resolved = os.environ.get(env_var, '')
                if not resolved and eid in old_providers:
                    resolved = old_providers[eid].get('key', '')
                if resolved:
                    cfg['api_key'] = resolved[:4] + '***' + resolved[-4:] if len(resolved) > 8 else '***'
                else:
                    cfg['api_key'] = ''
            elif key and len(key) > 8:
                cfg['api_key'] = key[:4] + '***' + key[-4:]
        return self._serve_json(safe)

    def _engine_chat(self):
        """统一引擎聊天入口 — POST /api/chat?engine=xxx

        根据 engine 参数选择处理器：
        - rest    → 代理到 LLM API（DeepSeek/OpenAI/Claude）
        - sidecar → 转发到侧车进程（WorkBuddy）
        - cli     → 子进程调用（Hermes/Claude Code）
        - agent   → 内置 Agent 循环（Seegent Agent）
        无 engine 参数 → 兼容旧模式（从 body 读取 api_key/base_url）
        """
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        engine_id = qs.get('engine', [None])[0]

        # 读取请求体
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            req_data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        # 加载引擎配置
        engines_data = _load_json(ENGINES_FILE, {"engines": {}, "ui": {"sections": []}})
        engines = engines_data.get('engines', {})

        # 无 engine 参数 → 兼容旧模式（直接 REST 代理）
        if not engine_id:
            # 尝试从请求体判断
            if req_data.get('api_key') and req_data.get('base_url'):
                return self._handle_rest_engine(req_data, None)
            # 没有足够信息 → 用默认引擎
            engine_id = 'deepseek'

        engine_cfg = engines.get(engine_id)
        if not engine_cfg:
            return self._serve_sse_error(f'未知引擎: {engine_id}')

        etype = engine_cfg.get('type', 'rest')

        # 解析环境变量占位符
        if 'api_key' in engine_cfg and engine_cfg['api_key'].startswith('${') and engine_cfg['api_key'].endswith('}'):
            env_var = engine_cfg['api_key'][2:-1]
            engine_cfg['api_key'] = os.environ.get(env_var, '')

        if etype == 'rest':
            if req_data.get('forceAgent'):
                return self._handle_agent_engine(req_data, engine_cfg)
            return self._handle_rest_engine(req_data, engine_cfg)
        elif etype == 'sidecar':
            return self._handle_sidecar_engine(req_data, engine_cfg)
        elif etype == 'cli':
            return self._handle_cli_engine(req_data, engine_cfg)
        elif etype == 'agent':
            return self._handle_agent_engine(req_data, engine_cfg)
        else:
            return self._serve_sse_error(f'不支持的引擎类型: {etype}')

    def _handle_rest_engine(self, req_data, engine_cfg):
        """REST API 引擎 — 代理到 /chat/completions，流式转发 + 用量记录"""
        if engine_cfg:
            api_key = engine_cfg.get('api_key', '')
            base_url = engine_cfg.get('base_url', '')
            if not api_key:
                return self._serve_sse_error(f'引擎 "{engine_cfg.get("name")}" 未配置 API Key')
        else:
            api_key = req_data.get('api_key', '') or DEEPSEEK_API_KEY
            base_url = req_data.get('base_url', '')

        model = req_data.get('model', '')
        messages = req_data.get('messages', [])

        if not api_key or not base_url:
            return self._serve_sse_error('缺少 API Key 或 Base URL')

        url = base_url.rstrip('/') + '/chat/completions'
        payload = json.dumps({
            'model': model,
            'messages': messages,
            'stream': True
        }).encode('utf-8')

        if engine_cfg:
            additional_headers = engine_cfg.get('headers', {})
        else:
            additional_headers = {}

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
            **additional_headers
        }

        agent_name = engine_cfg.get('name', 'REST') if engine_cfg else 'REST'
        agent_id = engine_cfg.get('name', 'rest').lower() if engine_cfg else 'rest'
        usage = None

        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')

        try:
            self._start_sse()

            with urllib.request.urlopen(req, timeout=300) as resp:
                while True:
                    raw = resp.readline()
                    if not raw:
                        break
                    line = raw.decode('utf-8', errors='replace').strip()
                    if line.startswith('data: ') and not line.startswith('data: [DONE]'):
                        try:
                            chunk = json.loads(line[6:])
                            u = chunk.get('usage')
                            if u and u.get('total_tokens'):
                                usage = u
                        except json.JSONDecodeError:
                            pass
                    self.wfile.write(raw)
                    self.wfile.flush()

            if usage:
                _record_usage(model, agent_id, agent_name,
                             usage.get('prompt_tokens', 0),
                             usage.get('completion_tokens', 0))
                _append_log({
                    'type': 'chat',
                    'engineId': agent_id,
                    'engineName': agent_name,
                    'model': model,
                    'status': 'success',
                    'promptTokens': usage.get('prompt_tokens', 0),
                    'completionTokens': usage.get('completion_tokens', 0),
                    'totalTokens': usage.get('total_tokens', 0),
                })
        except urllib.error.HTTPError as e:
            _append_log({
                'type': 'chat',
                'engineId': agent_id,
                'engineName': agent_name,
                'model': model,
                'status': 'error',
                'error': self._format_api_error(e),
            })
            self._serve_sse_error(self._format_api_error(e))
        except Exception as e:
            _append_log({
                'type': 'chat',
                'engineId': agent_id,
                'engineName': agent_name,
                'model': model,
                'status': 'error',
                'error': str(e),
            })
            self._serve_sse_error(str(e))

    def _handle_sidecar_engine(self, req_data, engine_cfg):
        """侧车引擎 — 转发请求到侧车进程 HTTP 端点"""
        sidecar_url = engine_cfg.get('url', '')
        if not sidecar_url:
            return self._serve_sse_error('侧车引擎未配置 url')

        # 用引擎名作为日志中的智能体身份，使侧车 Agent 出现在「操作日志」模块
        agent_name = engine_cfg.get('name', 'Sidecar')
        agent_id = engine_cfg.get('name', 'sidecar').lower().replace(' ', '-')
        model = req_data.get('model', '') or engine_cfg.get('model', '')
        _log_start = time.time()

        # 构造转发请求体
        forward_body = json.dumps({
            'prompt': req_data.get('prompt', ''),
            'model': req_data.get('model', ''),
            'messages': req_data.get('messages', []),
            'options': req_data.get('options', {})
        }).encode('utf-8')

        req = urllib.request.Request(
            sidecar_url,
            data=forward_body,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        try:
            self._start_sse()

            with urllib.request.urlopen(req, timeout=300) as resp:
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            _append_log({
                'type': 'chat',
                'agentId': agent_id,
                'engineId': agent_id,
                'agentName': agent_name,
                'engineName': agent_name,
                'model': model,
                'status': 'success',
                'duration': round(time.time() - _log_start, 1),
            })
        except urllib.error.URLError:
            _append_log({
                'type': 'chat',
                'agentId': agent_id,
                'engineId': agent_id,
                'agentName': agent_name,
                'engineName': agent_name,
                'model': model,
                'status': 'error',
                'error': f'侧车服务不可用 ({sidecar_url})',
            })
            self._serve_sse_error(f'侧车服务不可用 ({sidecar_url}), 请先启动对应服务')
        except Exception as e:
            _append_log({
                'type': 'chat',
                'agentId': agent_id,
                'engineId': agent_id,
                'agentName': agent_name,
                'engineName': agent_name,
                'model': model,
                'status': 'error',
                'error': str(e)[:200],
            })
            self._serve_sse_error(f'侧车引擎错误: {e}')

    def _handle_cli_engine(self, req_data, engine_cfg):
        """CLI 引擎 — 通过子进程调用命令行工具"""
        command = engine_cfg.get('command', '')
        args = engine_cfg.get('args', [])
        prompt = req_data.get('prompt', '') or ''.join(
            msg.get('content', '') for msg in req_data.get('messages', [])
            if msg.get('role') == 'user'
        )

        if not command:
            return self._serve_sse_error('CLI 引擎未配置 command')
        if not prompt:
            return self._serve_sse_error('缺少 prompt')

        # 用引擎名作为日志中的智能体身份，使 Cursor / Claude / Hermes 等 CLI 工具出现在「操作日志」模块
        agent_name = engine_cfg.get('name', 'CLI')
        agent_id = engine_cfg.get('name', 'cli').lower().replace(' ', '-')
        _log_start = time.time()

        cmd = [command] + args + [prompt]
        timeout = engine_cfg.get('timeout', 120)

        try:
            self._start_sse()

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # 逐行读取输出并作为 SSE 事件发送
            for line in iter(proc.stdout.readline, ''):
                if not line:
                    break
                self._serve_sse('text', {'text': line})

            proc.wait(timeout=timeout)

            if proc.returncode != 0:
                stderr = proc.stderr.read()
                _append_log({
                    'type': 'cli',
                    'agentId': agent_id,
                    'engineId': agent_id,
                    'agentName': agent_name,
                    'engineName': agent_name,
                    'status': 'error',
                    'duration': round(time.time() - _log_start, 1),
                    'error': (stderr or f'退出码: {proc.returncode}')[:200],
                })
                self._serve_sse('error', {
                    'code': 'CLI_ERROR',
                    'message': stderr or f'退出码: {proc.returncode}'
                })
            else:
                _append_log({
                    'type': 'cli',
                    'agentId': agent_id,
                    'engineId': agent_id,
                    'agentName': agent_name,
                    'engineName': agent_name,
                    'status': 'success',
                    'duration': round(time.time() - _log_start, 1),
                })

            self._serve_sse('done', {})

        except subprocess.TimeoutExpired:
            proc.kill()
            _append_log({
                'type': 'cli',
                'agentId': agent_id,
                'engineId': agent_id,
                'agentName': agent_name,
                'engineName': agent_name,
                'status': 'error',
                'duration': round(time.time() - _log_start, 1),
                'error': f'CLI 引擎超时 ({timeout}s)',
            })
            self._serve_sse_error(f'CLI 引擎超时 ({timeout}s)')
        except FileNotFoundError:
            _append_log({
                'type': 'cli',
                'agentId': agent_id,
                'engineId': agent_id,
                'agentName': agent_name,
                'engineName': agent_name,
                'status': 'error',
                'duration': round(time.time() - _log_start, 1),
                'error': f'未找到命令: {command}',
            })
            self._serve_sse_error(f'未找到命令: {command}')
        except Exception as e:
            _append_log({
                'type': 'cli',
                'agentId': agent_id,
                'engineId': agent_id,
                'agentName': agent_name,
                'engineName': agent_name,
                'status': 'error',
                'duration': round(time.time() - _log_start, 1),
                'error': str(e)[:200],
            })
            self._serve_sse_error(f'CLI 引擎错误: {e}')

    def _handle_agent_engine(self, req_data, engine_cfg):
        """Agent 引擎 — 委托给内置的 _agent_loop"""
        # 注入引擎配置中的 api_key / base_url
        if 'api_key' not in req_data:
            req_data['api_key'] = engine_cfg.get('api_key', '')
        if 'base_url' not in req_data:
            req_data['base_url'] = engine_cfg.get('base_url', '')
        return self._agent_loop(req_data)

    # ========== 原有聊天代理方法 ==========

    def _proxy_chat(self):
        """Proxy LLM chat request — always streaming via SSE."""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            req_data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        api_key = req_data.get('api_key', '') or DEEPSEEK_API_KEY
        base_url = req_data.get('base_url', '')
        model = req_data.get('model', '')
        messages = req_data.get('messages', [])

        if not api_key or not base_url:
            self._serve_json({'error': '请先配置模型和 API Key，或在终端设置 DEEPSEEK_API_KEY 环境变量'}, 400)
            return

        url = base_url.rstrip('/') + '/chat/completions'
        payload = json.dumps({
            'model': model,
            'messages': messages,
            'stream': True
        }).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            },
            method='POST'
        )

        usage = None

        try:
            self._start_sse()

            with urllib.request.urlopen(req, timeout=300) as resp:
                while True:
                    raw = resp.readline()
                    if not raw:
                        break
                    line = raw.decode('utf-8', errors='replace').strip()
                    if line.startswith('data: ') and not line.startswith('data: [DONE]'):
                        try:
                            chunk = json.loads(line[6:])
                            u = chunk.get('usage')
                            if u and u.get('total_tokens'):
                                usage = u
                        except json.JSONDecodeError:
                            pass
                    self.wfile.write(raw)
                    self.wfile.flush()

            if usage:
                _record_usage(model, 'rest', 'REST',
                             usage.get('prompt_tokens', 0),
                             usage.get('completion_tokens', 0))
                _append_log({
                    'type': 'chat',
                    'engineId': 'rest',
                    'engineName': 'REST',
                    'model': model,
                    'status': 'success',
                    'promptTokens': usage.get('prompt_tokens', 0),
                    'completionTokens': usage.get('completion_tokens', 0),
                    'totalTokens': usage.get('total_tokens', 0),
                })
        except urllib.error.HTTPError as e:
            _append_log({
                'type': 'chat',
                'engineId': 'rest',
                'engineName': 'REST',
                'model': model,
                'status': 'error',
                'error': self._format_api_error(e),
            })
            self._serve_sse_error(self._format_api_error(e))
        except Exception as e:
            _append_log({
                'type': 'chat',
                'engineId': 'rest',
                'engineName': 'REST',
                'model': model,
                'status': 'error',
                'error': str(e),
            })
            self._serve_sse_error(str(e))

    def _save_json_endpoint(self, path):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        _save_json(path, data)
        self._serve_json({'ok': True})

    def _serve_static(self, filename, content_type='text/html'):
        """Serve a static file from the package static/ directory."""
        filepath = os.path.join(STATIC_DIR, filename)
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', f'{content_type}; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, 'File not found')

    def _serve_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _handle_skilllib_explain(self):
        """POST /api/skilllib/explain — 用已配置模型把 Skill 翻译成大白话。"""
        body = self._read_body() or {}
        sid = body.get('id')
        s = _skilllib_find_skill(sid) if sid else None
        if not s:
            return self._serve_json({'error': 'skill 不存在'}, 404)
        engines = _load_json(ENGINES_FILE, {'engines': {}}).get('engines', {})
        engine_id = body.get('engine') or _skilllib_default_engine(engines)
        cfg = engines.get(engine_id) if engine_id else None
        if not cfg or cfg.get('type') != 'rest':
            cfg = next((e for e in engines.values() if e.get('type') == 'rest'), None)
            engine_id = None
        if not cfg:
            return self._serve_json({'ok': False, 'reason': 'no_engine'})
        api_key = cfg.get('api_key', '')
        if isinstance(api_key, str) and api_key.startswith('${') and api_key.endswith('}'):
            api_key = os.environ.get(api_key[2:-1], '')
        base_url = cfg.get('base_url', '')
        model = cfg.get('model', '')
        if not api_key or not base_url or not model:
            return self._serve_json({'ok': False, 'reason': 'no_key'})
        system_prompt = ('你是一个帮助用户理解开发工具的助手，阅读你内容的人是不懂技术的产品/运营/教师。'
                         '请用通俗易懂、口语化的中文解释下面这个 Skill（技能/插件）：它是什么、解决什么问题、'
                         '典型使用场景、大概怎么用。控制在 260 字以内，用自然段落，不要使用 Markdown 标题。')
        payload = json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': _skilllib_explain_prompt(s)}
            ],
            'stream': False,
            'temperature': 0.3,
            'max_tokens': 600
        }).encode('utf-8')
        url = base_url.rstrip('/') + '/chat/completions'
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key}
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read().decode('utf-8'))
            content = out['choices'][0]['message']['content']
            u = out.get('usage') or {}
            agent_id = cfg.get('name', engine_id or 'rest').lower()
            _record_usage(model, agent_id, cfg.get('name', engine_id or 'rest'),
                          u.get('prompt_tokens', 0), u.get('completion_tokens', 0))
            return self._serve_json({'ok': True, 'text': content, 'engine': engine_id or cfg.get('name')})
        except urllib.error.HTTPError as e:
            return self._serve_json({'ok': False, 'reason': 'api_error', 'error': self._format_api_error(e)}, 502)
        except Exception as e:
            return self._serve_json({'ok': False, 'reason': 'api_error', 'error': str(e)}, 500)

    # ===== 项目追踪 API =====

    def _handle_tracks_get(self):
        """GET /api/tracks?project=xxx&days=30&status=done"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        project = qs.get('project', [None])[0]
        days = int(qs.get('days', [90])[0])
        status = qs.get('status', [None])[0]
        tracks = _load_tracks()
        entries = tracks.get('entries', [])
        # 筛选
        cutoff = None
        if days > 0:
            cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
        if project:
            entries = [e for e in entries if e.get('projectId') == project]
        if status:
            entries = [e for e in entries if e.get('status') == status]
        if days > 0:
            entries = [e for e in entries if e.get('date', '') >= cutoff_date]
        # 按日期倒序
        entries.sort(key=lambda e: e.get('date', ''), reverse=True)
        # 统计汇总
        total_duration = sum(e.get('duration', 0) for e in entries)
        total_tokens = sum(e.get('tokenUsed', 0) for e in entries)
        total_cost = sum(e.get('cost', 0) for e in entries)
        # 连续工作天数
        streak = 0
        today = datetime.date.today()
        for i in range(365):
            d = (today - datetime.timedelta(days=i)).strftime('%Y-%m-%d')
            if any(e.get('date') == d for e in tracks.get('entries', [])):
                streak += 1
            else:
                break
        return self._serve_json({
            'entries': entries,
            'summary': {
                'totalDuration': total_duration,
                'totalTokens': total_tokens,
                'totalCost': round(total_cost, 4),
                'streak': streak,
                'entryCount': len(entries),
            }
        })

    def _handle_tracks_post(self):
        """POST /api/tracks — 保存全量追踪数据"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._serve_json({'error': 'Invalid JSON'}, 400)
        if 'entries' not in data and 'id' in data:
            # 单条新增：追加到已有数据
            tracks = _load_tracks()
            tracks.setdefault('entries', []).append(data)
            _save_tracks(tracks)
            return self._serve_json({'ok': True, 'id': data.get('id')})
        # 全量替换
        _save_tracks(data)
        return self._serve_json({'ok': True})

    def _handle_tracks_delete(self):
        """DELETE /api/tracks/:id — 删除单条追踪记录"""
        parts = self.path.rstrip('/').split('/')
        entry_id = parts[-1] if len(parts) >= 3 else None
        if not entry_id:
            return self._serve_json({'error': 'Missing track id'}, 400)
        tracks = _load_tracks()
        entries = tracks.get('entries', [])
        new_entries = [e for e in entries if e.get('id') != entry_id]
        if len(new_entries) == len(entries):
            return self._serve_json({'error': 'Not found'}, 404)
        tracks['entries'] = new_entries
        _save_tracks(tracks)
        return self._serve_json({'ok': True})

    # ===== 项目工作流 API =====

    def _handle_projects_get(self):
        """GET /api/projects — 列出所有项目（或单个项目详情）"""
        parts = self.path.rstrip('/').split('/')
        projects = _load_projects()
        # 判断是否为单个项目请求：路径形如 /api/projects/xxx
        path_has_id = (len(parts) >= 3 and parts[-1] not in ('', 'api', 'projects'))
        if path_has_id:
            project_id = parts[-1]
            proj = projects['projects'].get(project_id)
            if not proj:
                return self._serve_json({'error': 'Project not found'}, 404)
            # 计算汇总
            summary = _calc_project_summary(proj)
            return self._serve_json({'project': proj, 'summary': summary})
        # 列表模式：返回所有项目及汇总
        result = {}
        for pid, proj in projects.get('projects', {}).items():
            summary = _calc_project_summary(proj)
            result[pid] = {
                'id': pid,
                'name': proj.get('name', pid),
                'description': proj.get('description', ''),
                'createdAt': proj.get('createdAt', ''),
                'phases': len(proj.get('workflow', {}).get('template', [])),
                'summary': summary,
            }
        return self._serve_json({'projects': result})

    def _handle_projects_post(self):
        """POST /api/projects — 创建或更新项目；POST /api/projects/:id/import — 从日志导入"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._serve_json({'error': 'Invalid JSON'}, 400)

        # 检查是否是导入请求
        parts = self.path.rstrip('/').split('/')
        # path 形如 http://localhost:8765/api/projects/xxx/import
        if len(parts) >= 4 and parts[-1] == 'import':
            return self._handle_project_import(parts[-2], data)

        # 创建/更新项目
        projects = _load_projects()
        project_id = data.get('id') or (parts[-1] if len(parts) >= 3 and parts[-1] != 'projects' else None)
        if not project_id:
            return self._serve_json({'error': 'Missing project id'}, 400)
        projects.setdefault('projects', {})[project_id] = data
        _save_projects(projects)
        return self._serve_json({'ok': True, 'id': project_id})

    def _handle_project_delete(self):
        """DELETE /api/projects/:id — 删除项目"""
        parts = self.path.rstrip('/').split('/')
        project_id = parts[-1] if len(parts) >= 3 and parts[-1] not in ('api', 'projects') else None
        if not project_id:
            return self._serve_json({'error': 'Missing project id'}, 400)
        projects = _load_projects()
        if project_id not in projects.get('projects', {}):
            return self._serve_json({'error': 'Not found'}, 404)
        del projects['projects'][project_id]
        _save_projects(projects)
        return self._serve_json({'ok': True})

    def _handle_project_import(self, project_id, data):
        """POST /api/projects/:id/import — 从日志导入步骤"""
        projects = _load_projects()
        proj = projects['projects'].get(project_id)
        if not proj:
            return self._serve_json({'error': 'Project not found'}, 404)

        log_ids = data.get('logIds', [])
        if not log_ids:
            return self._serve_json({'error': 'No log IDs provided'}, 400)

        all_logs = _load_json(LOG_FILE, {'logs': []}).get('logs', [])
        phases = proj.get('workflow', {}).get('template', [])
        imported = 0
        steps = proj.setdefault('workflow', {}).setdefault('steps', [])

        for log_id in log_ids:
            log = next((l for l in all_logs if l.get('id') == log_id), None)
            if not log:
                continue
            # 推断阶段
            agent_name = log.get('agentName') or log.get('engineName') or ''
            phase = data.get('phase') or _guess_phase(agent_name, phases)
            # 生成步骤
            step = {
                'id': 'step_' + str(int(time.time() * 1000)) + '_' + str(imported),
                'phase': phase,
                'task': log.get('task', '')[:200],
                'date': (log.get('timestamp', '') or '')[:10],
                'duration': round((log.get('duration', 0) or 0) / 60, 1),  # 秒转分钟
                'agents': [agent_name] if agent_name else [],
                'models': [log.get('modelId') or log.get('model', '')] if (log.get('modelId') or log.get('model')) else [],
                'tools': {'cli': [], 'mcp': log.get('toolsUsed', []) if isinstance(log.get('toolsUsed'), list) else []},
                'tokens': log.get('totalTokens', 0) or 0,
                'cost': 0,
                'notes': '',
                'status': 'done' if log.get('status') == 'success' else 'todo',
                'importedFrom': log_id,
            }
            steps.append(step)
            imported += 1

        _save_projects(projects)
        return self._serve_json({'ok': True, 'imported': imported})

    # ===== 看板（Dashboard）API =====

    def _load_datasources(self):
        """加载数据源注册表"""
        return _load_json(DATASOURCES_FILE, {'version': 2, 'datasources': {}})

    def _save_datasources(self, registry):
        """保存数据源注册表"""
        _save_json(DATASOURCES_FILE, registry)

    def _dashboard_config_path(self, project_id):
        """返回某个项目的看板配置文件路径"""
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', project_id or 'default')
        return os.path.join(DASHBOARD_DIR, f'.dashboard-{safe_id}.json')

    def _load_dashboard_config(self, project_id):
        """加载看板配置，不存在则返回默认空配置"""
        path = self._dashboard_config_path(project_id)
        return _load_json(path, dict(DEFAULT_DASHBOARD_CONFIG))

    def _save_dashboard_config(self, project_id, config):
        """保存看板配置"""
        path = self._dashboard_config_path(project_id)
        _save_json(path, config)

    def _handle_dashboard_config_get(self):
        """GET /api/dashboard/config?folder=xxx  — 返回该文件夹的看板配置
           数据源从注册表中按 boundFolders 合并进来"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        folder_id = qs.get('folder', [''])[0]
        if not folder_id:
            # 如果没有指定 folder，返回所有数据源的汇总
            registry = self._load_datasources()
            self._serve_json({
                'folderId': '',
                'datasources': registry.get('datasources', {}),
                'cards': [],
                'analyses': []
            })
            return
        
        # 加载该文件夹的看板配置（只有 cards 和 analyses）
        config = self._load_dashboard_config(folder_id)
        
        # 从注册表中按 boundFolders 获取数据源
        registry = self._load_datasources()
        all_sources = registry.get('datasources', {})
        matched_sources = {}
        for sid, src in all_sources.items():
            if folder_id in src.get('boundFolders', []):
                matched_sources[sid] = src
        
        config['datasources'] = matched_sources
        self._serve_json(config)

    def _handle_dashboard_post(self):
        """POST /api/dashboard/config  — 保存卡片/分析（不存数据源）
           POST /api/dashboard/sync-feishu  — 飞书同步
           POST /api/dashboard/analyze — 触发分析
           POST /api/dashboard/parse-url — 解析飞书链接"""
        path = self.path
        if path == '/api/dashboard/config' or path.startswith('/api/dashboard/config?'):
            return self._dashboard_save_config()
        if path == '/api/dashboard/sync-feishu':
            return self._dashboard_sync_feishu()
        if path == '/api/dashboard/analyze':
            return self._dashboard_analyze()
        if path == '/api/dashboard/parse-url':
            return self._dashboard_parse_url()
        self.send_error(404)

    def _dashboard_save_config(self):
        """POST /api/dashboard/config — 保存看板的卡片和分析配置（不含数据源）"""
        body = self._read_body()
        if not body:
            return
        folder_id = body.get('folderId', body.get('folder', ''))
        if not folder_id:
            self._serve_json({'error': '缺少 folderId'}, 400)
            return
        config = body.get('config', body)
        config['folderId'] = folder_id
        if 'cards' not in config:
            config['cards'] = []
        if 'analyses' not in config:
            config['analyses'] = []
        self._save_dashboard_config(folder_id, config)
        self._serve_json({'ok': True, 'folderId': folder_id})

    BOARD_BINDINGS_FILE = os.path.join(DATA_DIR, '.seegent-board-bindings.json')

    # 本地文件看板：监视目录配置（与「绑定」彻底分离）
    FILEWATCH_FILE = os.path.join(DATA_DIR, '.seegent-filewatch.json')

    # 待办标记：仅识别出现在行首的标记（避免误匹配代码示例）
    TODO_PREFIX_PATTERNS = ('- [ ]', '- []', '* [ ]', '+ [ ]', 'todo:', 'todo：', '待办:', '待办：', 'fixme:', 'fixme：')
    TODO_LINE_START_WORDS = ('todo', '待办', 'fixme')  # 整行以这些词开头（忽略 - * # 前缀）
    # 排除的目录
    SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.next', '.idea', '.vscode'}
    # 业务目录优先级：先扫这些目录，确保最重要的内容先进入视野
    PRIORITY_DIRS = ('小红书教育', '小红书AI', '小绿书银发',
                     '个人感悟', '展示区', '违禁词', 'skills', '临时')
    # README 候选
    README_CANDIDATES = ['README.md', 'README.txt', 'readme.md', 'readme.txt']

    def _collect_md_files(self, root, max_files=2000):
        """收集根目录下的 Markdown 文件（递归，但跳过常见无关目录）。
        业务目录优先遍历，确保最重要的内容先进视图（避免被大批演示稿挤掉配额）。
        """
        seen = {}  # path -> True，保持插入顺序且去重

        def _collect_from(base):
            """递归扫 base 下的所有 md，写入 seen"""
            for dirpath, dirnames, filenames in os.walk(base):
                # 原地修改 dirnames 跳过无关目录
                dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS and not d.startswith('.')]
                for fn in filenames:
                    if fn.lower().endswith(('.md', '.markdown')):
                        full = os.path.join(dirpath, fn)
                        if full not in seen:
                            seen[full] = True

        try:
            # 第一轮：按业务优先级扫
            for sub in self.PRIORITY_DIRS:
                sub_path = os.path.join(root, sub)
                if os.path.isdir(sub_path):
                    _collect_from(sub_path)
                    if len(seen) >= max_files:
                        return list(seen.keys())[:max_files]

            # 第二轮：兜底扫剩余目录（已扫过的不重复）
            for dirpath, dirnames, filenames in os.walk(root):
                # 跳过整个优先级目录（已在第一轮扫过）
                rel = os.path.relpath(dirpath, root)
                if rel.split(os.sep)[0] in self.PRIORITY_DIRS:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS and not d.startswith('.')]
                for fn in filenames:
                    if fn.lower().endswith(('.md', '.markdown')):
                        full = os.path.join(dirpath, fn)
                        if full not in seen:
                            seen[full] = True
                            if len(seen) >= max_files:
                                return list(seen.keys())[:max_files]
        except Exception:
            pass
        return list(seen.keys())[:max_files]

    def _scan_todos_from_project(self, root):
        """扫描项目内所有 Markdown 文件，提取待办行。
        优先返回根目录 TODO.md / PLAN.md 等的完整内容；否则聚合各文件中的待办标记行。"""
        todos = []

        # 1. 先看根目录是否有专门待办文件，有就直接全文展示（最优先）
        for candidate in ('TODO.md', 'PLAN.md', 'TASKS.md', 'todo.md', 'plan.md'):
            fpath = os.path.join(root, candidate)
            if os.path.isfile(fpath):
                items = self._read_todo_lines(fpath, max_lines=50)
                if items:
                    return items, candidate

        # 2. 否则扫描所有 md，提取带待办标记的行
        md_files = self._collect_md_files(root)
        for fpath in md_files:
            try:
                rel = os.path.relpath(fpath, root)
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                    for lineno, line in enumerate(fh, 1):
                        clean = line.strip()
                        if not clean or len(clean) < 3:
                            continue
                        if self._is_todo_line(clean):
                            if len(clean) > 300:
                                clean = clean[:300] + '...'
                            todos.append({'text': clean, 'line': lineno, 'file': rel})
                            if len(todos) >= 60:
                                return todos, None
            except Exception:
                continue
        return todos, None

    def _is_todo_line(self, line):
        """判断一行是否是待办标记行（严格匹配，避免误匹配代码示例）"""
        low = line.lower()
        # 1. 行首前缀模式：- [ ] / * [ ] / todo: / 待办: 等
        for p in self.TODO_PREFIX_PATTERNS:
            if low.startswith(p):
                return True
        # 2. 去掉常见的列表/标题前缀后，整行以 todo/待办/fixme 开头
        stripped = low.lstrip('-*+#> ')
        for w in self.TODO_LINE_START_WORDS:
            if stripped.startswith(w):
                return True
        return False

    def _read_todo_lines(self, filepath, max_lines=50):
        """读取待办文件内容，返回行列表"""
        items = []
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as fh:
                for lineno, line in enumerate(fh, 1):
                    if lineno > max_lines:
                        break
                    clean = line.strip()
                    if not clean:
                        continue
                    if len(clean) > 300:
                        clean = clean[:300] + '...'
                    items.append({'text': clean, 'line': lineno})
        except Exception:
            pass
        return items

    def _scan_readme(self, root):
        """读取 README 的前几段作为项目简介"""
        for candidate in self.README_CANDIDATES:
            fpath = os.path.join(root, candidate)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                        lines = []
                        for line in fh:
                            clean = line.strip()
                            if clean.startswith('#'):
                                # 跳过标题行本身，取标题后的内容
                                continue
                            if not clean:
                                if lines:
                                    break  # 遇到空行，说明第一段结束
                                continue
                            lines.append(clean)
                            if len(lines) >= 5:
                                break
                        text = ' '.join(lines).strip()
                        if len(text) > 300:
                            text = text[:300] + '...'
                        return text or None, candidate
                except Exception:
                    pass
        return None, None

    def _handle_board_projects_get(self):
        """GET /api/board/projects — 读取所有绑定项目的信息+自动扫描待办"""
        bindings = _load_json(self.BOARD_BINDINGS_FILE, {'bindings': {}})
        projects = []
        for folder_id, info in bindings.get('bindings', {}).items():
            project = {
                'folderId': folder_id,
                'folderName': info.get('folderName', ''),
                'path': info.get('path', ''),
                'todoFile': None,
                'readme': None,
                'readmeFile': None,
                'boundAt': info.get('boundAt', 0),
                'todos': [],
                'fileCount': 0,
            }
            abs_path = os.path.expanduser(info.get('path', ''))
            if abs_path and os.path.isdir(abs_path):
                # 统计子文件数量
                try:
                    project['fileCount'] = sum(
                        1 for _ in os.listdir(abs_path)
                        if not _.startswith('.') and not _.startswith('~')
                    )
                except Exception:
                    pass
                # 自动扫描待办
                todos, todo_source = self._scan_todos_from_project(abs_path)
                project['todos'] = todos
                project['todoFile'] = todo_source
                # 自动读取项目简介
                readme_text, readme_file = self._scan_readme(abs_path)
                project['readme'] = readme_text
                project['readmeFile'] = readme_file
            projects.append(project)
        self._serve_json({'projects': projects})

    def _load_filewatch(self):
        """读取本地文件看板的监视目录配置；首次使用自动把工作区根目录加进去。"""
        cfg = _load_json(self.FILEWATCH_FILE, None)
        if not cfg or 'roots' not in cfg or not isinstance(cfg.get('roots'), list):
            default_root = os.path.dirname(BASE_DIR)  # /Users/shiyuanchang/Seegent
            cfg = {
                'roots': [{
                    'id': 'rt_workspace',
                    'name': os.path.basename(default_root.rstrip('/')) or '工作区',
                    'path': default_root,
                    'addedAt': int(time.time() * 1000),
                    'default': True,
                }]
            }
            _save_json(self.FILEWATCH_FILE, cfg)
        return cfg

    def _handle_recent_activity(self):
        """GET /api/recent-activity — 扫描「本地文件看板」监视目录里的真实文件，
        取最近修改的若干文档/代码文件作为「最近动态」。不再依赖文件夹绑定。"""
        import heapq
        roots = self._load_filewatch().get('roots', [])
        EXCLUDE_DIRS = {'.git', '.workbuddy', 'node_modules', '__pycache__', '.venv', 'venv',
                        '.idea', '.vscode', 'UNKNOWN.egg-info', '.svn',
                        'workbuddy-sidecar', 'build', 'dist', '.next', '.cache'}
        EXCLUDE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.mp4', '.mov',
                       '.webm', '.avi', '.mkv', '.mp3', '.wav', '.flac', '.zip', '.tar',
                       '.gz', '.7z', '.rar', '.tgz', '.heic', '.psd', '.ai'}
        MAX_DEPTH = 8
        LIMIT = 200
        candidates = []
        for r in roots:
            root = os.path.expanduser(r.get('path', ''))
            if not root or not os.path.isdir(root):
                continue
            proj_name = r.get('name') or os.path.basename(root.rstrip('/')) or root
            root_base = root.rstrip(os.sep)
            for dirpath, dirnames, filenames in os.walk(root):
                # 剪枝：排除大/缓存/版本目录
                dirnames[:] = [d for d in dirnames
                               if d not in EXCLUDE_DIRS and not d.startswith('.~')]
                depth = dirpath[len(root_base):].count(os.sep)
                if depth >= MAX_DEPTH:
                    dirnames[:] = []
                for fn in filenames:
                    if fn.startswith('.'):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in EXCLUDE_EXT:
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        mtime = os.path.getmtime(fp)
                    except OSError:
                        continue
                    rel = os.path.relpath(fp, root)
                    candidates.append((mtime, proj_name, rel))
        top = heapq.nlargest(LIMIT, candidates, key=lambda x: x[0])
        items = [{'project': p, 'path': r, 'mtime': int(mt)} for mt, p, r in top]
        self._serve_json({'items': items,
                          'roots': [{'id': x.get('id'), 'name': x.get('name'), 'path': x.get('path')}
                                    for x in roots]})

    def _handle_board_bind_post(self):
        """POST /api/board/bind — 绑定文件夹到看板（自动扫描待办文件）"""
        body = self._read_body()
        if not body:
            return
        folder_id = body.get('folderId', '').strip()
        if not folder_id:
            self._serve_json({'error': 'folderId 不能为空'}, 400)
            return
        bindings = _load_json(self.BOARD_BINDINGS_FILE, {'bindings': {}})
        bindings.setdefault('bindings', {})[folder_id] = {
            'folderName': body.get('folderName', ''),
            'path': body.get('path', ''),
            'boundAt': int(time.time() * 1000),
        }
        _save_json(self.BOARD_BINDINGS_FILE, bindings)
        self._serve_json({'ok': True, 'folderId': folder_id})

    def _handle_board_bind_delete(self):
        """DELETE /api/board/bind/:folderId — 解绑"""
        parts = self.path.rstrip('/').split('/')
        folder_id = parts[-1] if len(parts) >= 4 and parts[-1] not in ('api', 'board', 'bind') else None
        if not folder_id:
            self._serve_json({'error': 'Missing folderId'}, 400)
            return
        bindings = _load_json(self.BOARD_BINDINGS_FILE, {'bindings': {}})
        if folder_id in bindings.get('bindings', {}):
            del bindings['bindings'][folder_id]
            _save_json(self.BOARD_BINDINGS_FILE, bindings)
        self._serve_json({'ok': True})

    # ===== 本地文件看板：监视目录 CRUD =====

    def _handle_filewatch_get(self):
        """GET /api/filewatch — 返回当前监视目录列表"""
        cfg = self._load_filewatch()
        self._serve_json({'roots': cfg.get('roots', [])})

    def _handle_filewatch_post(self):
        """POST /api/filewatch — 新增一个本地监视目录。Body: {path, name?}"""
        body = self._read_body()
        if not body:
            return
        path = (body.get('path') or '').strip()
        if not path:
            self._serve_json({'error': 'path 不能为空'}, 400)
            return
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            self._serve_json({'error': '路径不存在或不是目录：' + path}, 400)
            return
        cfg = self._load_filewatch()
        roots = cfg.setdefault('roots', [])
        norm = os.path.normpath(path)
        if any(os.path.normpath(r.get('path', '')) == norm for r in roots):
            self._serve_json({'ok': True, 'exists': True, 'roots': roots})
            return
        rid = 'rt_' + str(int(time.time() * 1000))
        name = (body.get('name') or '').strip() or os.path.basename(norm.rstrip('/')) or norm
        roots.append({'id': rid, 'name': name, 'path': norm, 'addedAt': int(time.time() * 1000)})
        _save_json(self.FILEWATCH_FILE, cfg)
        self._serve_json({'ok': True, 'roots': roots})

    def _handle_filewatch_delete(self):
        """DELETE /api/filewatch/:id — 移除一个监视目录"""
        parts = self.path.rstrip('/').split('/')
        rid = parts[-1] if len(parts) >= 3 and parts[-1] not in ('api', 'filewatch') else None
        if not rid:
            self._serve_json({'error': 'Missing id'}, 400)
            return
        cfg = self._load_filewatch()
        roots = cfg.get('roots', [])
        before = len(roots)
        cfg['roots'] = [r for r in roots if r.get('id') != rid]
        _save_json(self.FILEWATCH_FILE, cfg)
        self._serve_json({'ok': True, 'removed': before != len(cfg['roots']), 'roots': cfg['roots']})

    def _handle_filewatch_pick(self):
        """GET /api/filewatch/pick — 调用 macOS 原生选目录弹窗，返回真实路径。
        仅本机运行时有效；用户取消或非 macOS 时返回错误，前端回退到手动填路径。"""
        try:
            result = subprocess.run(
                ['osascript', '-e',
                 'POSIX path of (choose folder with prompt "选择要加入本地文件看板的目录")'],
                capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                # 用户取消或 AppleScript 不可用
                self._serve_json({'error': '已取消或未选择目录'}, 400)
                return
            path = result.stdout.strip()
            if not path:
                self._serve_json({'error': '未选择目录'}, 400)
                return
            self._serve_json({'path': path})
        except subprocess.TimeoutExpired:
            self._serve_json({'error': '选择超时'}, 400)
        except Exception as e:
            self._serve_json({'error': '无法调用系统选目录：' + str(e)}, 500)

    # ===== 数据源注册表 CRUD =====

    def _handle_datasources(self):
        """GET /api/datasources — 获取所有数据源
           GET /api/datasources?sourceId=xxx — 获取单个"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        source_id = qs.get('sourceId', [''])[0]
        registry = self._load_datasources()
        if source_id:
            src = registry.get('datasources', {}).get(source_id)
            if not src:
                self._serve_json({'error': '数据源不存在'}, 404)
                return
            self._serve_json(src)
        else:
            self._serve_json(registry.get('datasources', {}))

    def _handle_datasources_post(self):
        """POST /api/datasources — 创建或更新数据源"""
        body = self._read_body()
        if not body:
            return
        source_id = body.get('sourceId') or ('ds_' + str(int(time.time() * 1000)))
        registry = self._load_datasources()
        sources = registry.get('datasources', {})
        
        # 保留已有的 rawData（如果没有新数据传入）
        existing = sources.get(source_id, {})
        existing_raw_data = existing.get('rawData', [])
        
        src = {
            'name': body.get('name', ''),
            'type': body.get('type', 'manual'),
            'createdAt': existing.get('createdAt') or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'boundFolders': body.get('boundFolders', existing.get('boundFolders', [])),
            'rawData': body.get('rawData') or existing_raw_data,
            'lastSyncAt': body.get('lastSyncAt') or existing.get('lastSyncAt'),
        }
        # 飞书数据源特有字段
        for key in ('appToken', 'tableId', 'credentialId', 'sourceUrl', 'columnMapping', 'icon', 'displayFields', 'dateField'):
            val = body.get(key)
            if val is not None:
                src[key] = val
            elif key in existing:
                src[key] = existing[key]
        # 保留旧 config 向后兼容
        if body.get('config'):
            src['config'] = body['config']
        elif existing.get('config'):
            src['config'] = existing['config']
        sources[source_id] = src
        
        registry['datasources'] = sources
        self._save_datasources(registry)
        self._serve_json({'ok': True, 'sourceId': source_id})

    def _handle_datasources_delete(self):
        """DELETE /api/datasources?sourceId=xxx"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        source_id = qs.get('sourceId', [''])[0]
        if not source_id:
            self._serve_json({'error': '缺少 sourceId'}, 400)
            return
        registry = self._load_datasources()
        if source_id not in registry.get('datasources', {}):
            self._serve_json({'error': '数据源不存在'}, 404)
            return
        del registry['datasources'][source_id]
        self._save_datasources(registry)
        self._serve_json({'ok': True})

    def _handle_datasources_bind(self):
        """POST /api/datasources/bind — 绑定/解绑文件夹
           Body: { sourceId, folderId, action: 'bind'|'unbind' }"""
        body = self._read_body()
        if not body:
            return
        source_id = body.get('sourceId', '')
        folder_id = body.get('folderId', '')
        action = body.get('action', 'bind')
        if not source_id or not folder_id:
            self._serve_json({'error': '缺少 sourceId 或 folderId'}, 400)
            return
        
        registry = self._load_datasources()
        sources = registry.get('datasources', {})
        if source_id not in sources:
            self._serve_json({'error': '数据源不存在'}, 404)
            return
        
        src = sources[source_id]
        folders = src.get('boundFolders', [])
        if action == 'bind' and folder_id not in folders:
            folders.append(folder_id)
        elif action == 'unbind' and folder_id in folders:
            folders.remove(folder_id)
        
        src['boundFolders'] = folders
        src['updatedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        sources[source_id] = src
        registry['datasources'] = sources
        self._save_datasources(registry)
        self._serve_json({'ok': True, 'boundFolders': folders})

    # ===== 飞书凭证管理 =====

    def _load_feishu_credentials(self):
        return _load_json(FEISHU_CREDENTIALS_FILE, {'credentials': {}})

    def _save_feishu_credentials(self, data):
        _save_json(FEISHU_CREDENTIALS_FILE, data)

    def _parse_feishu_url(self, url):
        """从飞书链接中提取信息
        返回: { type: 'direct'|'wiki', ... } 或 None
        直接多维表格: https://xxx.feishu.cn/base/BASCxxxxx?table=tblYYYYY
        知识库:       https://xxx.feishu.cn/wiki/TOKEN
        """
        import re
        domain_match = re.search(r'https?://([^/]+)', url)
        if not domain_match:
            return None
        domain = domain_match.group(1)

        # 直接多维表格链接: /base/xxx?table=yyy
        token_match = re.search(r'/base/([A-Za-z0-9]+)', url)
        if token_match:
            app_token = token_match.group(1)
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(url).query)
            table_id = qs.get('table', [None])[0]
            if not table_id:
                return None
            return {'type': 'direct', 'appToken': app_token, 'tableId': table_id, 'domain': domain}

        # 知识库链接: /wiki/TOKEN
        wiki_match = re.search(r'/wiki/([A-Za-z0-9]+)', url)
        if wiki_match:
            return {'type': 'wiki', 'wikiToken': wiki_match.group(1), 'domain': domain}

        return None

    def _match_feishu_credential(self, domain):
        """根据域名匹配飞书凭证。精确匹配优先，否则回退到第一个可用凭证。"""
        creds_data = self._load_feishu_credentials()
        credentials = creds_data.get('credentials', {})
        # 精确域名匹配
        for key, cred in credentials.items():
            if cred.get('domain', '') == domain:
                return key, cred
        # 回退：base domain 匹配（如 my.feishu.cn 匹配 bytedance.feishu.cn）
        base_domain = '.'.join(domain.split('.')[-2:]) if '.' in domain else domain
        for key, cred in credentials.items():
            cred_domain = cred.get('domain', '')
            if cred_domain.endswith(base_domain):
                return key, cred
        # 最后回退：返回第一个配置的凭证
        for key, cred in credentials.items():
            if cred.get('appId') and cred.get('appSecret'):
                return key, cred
        return None, None

    def _resolve_wiki_node(self, wiki_token, token):
        """通过 Wiki API 解析知识库节点，返回 { objType, objToken, title }
        如果是多维表格，objToken 即为 appToken
        GET /open-apis/wiki/v2/spaces/get_node?token={wiki_token}
        """
        import urllib.parse
        url = f'https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={urllib.parse.quote(wiki_token, safe="")}'
        result, err = self._feishu_request(url, token=token)
        if err:
            return None, err
        node = (result or {}).get('data', {}).get('node', {})
        obj_type = node.get('obj_type', '')
        obj_token = node.get('obj_token', '')
        title = node.get('title', '')
        return {'objType': obj_type, 'objToken': obj_token, 'title': title}, None

    def _feishu_list_tables(self, app_token, token):
        """列出多维表格应用下的所有表格
        GET /open-apis/bitable/v1/apps/{app_token}/tables
        """
        url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables'
        result, err = self._feishu_request(url, token=token)
        if err:
            return None, err
        tables = []
        for t in (result or {}).get('data', {}).get('items', []):
            tables.append({'tableId': t.get('table_id'), 'name': t.get('name')})
        return tables, None

    def _feishu_get_table_name(self, app_token, table_id, token):
        """获取多维表格名称
        先试单表接口，失败则用列表接口匹配
        """
        url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}'
        result, err = self._feishu_request(url, token=token)
        if result:
            name = (result or {}).get('data', {}).get('table', {}).get('name', '')
            if name:
                return name, None
        # 单表接口不可用时，用列表接口匹配
        list_url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables'
        list_result, list_err = self._feishu_request(list_url, token=token)
        if list_err:
            return None, err or list_err
        items = (list_result or {}).get('data', {}).get('items', [])
        for t in items:
            if t.get('table_id') == table_id:
                name = t.get('name', '')
                return name if name else None, None
        return None, None

    def _feishu_get_app_name(self, app_token, token):
        """获取多维表格应用名称
        GET /open-apis/bitable/v1/apps/{app_token}
        """
        url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}'
        result, err = self._feishu_request(url, token=token)
        if err:
            return None, err
        name = (result or {}).get('data', {}).get('app', {}).get('name', '')
        return name if name else None, None

    def _handle_feishu_credentials_get(self):
        """GET /api/feishu-credentials"""
        data = self._load_feishu_credentials()
        self._serve_json(data)

    def _handle_feishu_credentials_post(self):
        """POST /api/feishu-credentials — 保存凭证配置"""
        body = self._read_body()
        if not body:
            return
        data = body
        self._save_feishu_credentials(data)
        self._serve_json({'ok': True})

    # ===== 飞书多维表格同步 =====

    def _feishu_request(self, url, data=None, method='GET', token=None):
        """调用飞书 Open API（urllib 实现，零依赖）"""
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        body = json.dumps(data).encode('utf-8') if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8')), None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='replace')
            try:
                err_json = json.loads(err_body)
                msg = err_json.get('msg') or err_json.get('message') or err_body
            except (json.JSONDecodeError, ValueError):
                msg = err_body
            return None, f'飞书 API 错误 {e.code}：{msg}'
        except Exception as e:
            return None, f'请求飞书失败：{e}'

    def _feishu_tenant_token(self, app_id, app_secret):
        """获取 tenant_access_token"""
        result, err = self._feishu_request(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            data={'app_id': app_id, 'app_secret': app_secret},
            method='POST'
        )
        if err:
            return None, err
        if not result or 'tenant_access_token' not in result:
            return None, '未获取到 tenant_access_token：' + json.dumps(result, ensure_ascii=False)
        return result['tenant_access_token'], None

    def _feishu_list_fields(self, app_token, table_id, token):
        """列出多维表格的字段（表头），用于列映射"""
        url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields'
        result, err = self._feishu_request(url, token=token)
        if err:
            return None, err
        fields = []
        for f in (result or {}).get('data', {}).get('items', []):
            fields.append({'name': f.get('field_name'), 'type': f.get('type')})
        return fields, None

    def _feishu_list_records(self, app_token, table_id, token, page_size=500, max_pages=20):
        """分页拉取多维表格所有记录"""
        all_records = []
        page_token = None
        for _ in range(max_pages):
            url = (f'https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records'
                   f'?page_size={page_size}')
            if page_token:
                url += '&page_token=' + urllib.parse.quote(page_token)
            result, err = self._feishu_request(url, token=token)
            if err:
                return None, err
            data = (result or {}).get('data', {})
            for r in data.get('items', []):
                all_records.append(r.get('fields', {}))
            if not data.get('has_more'):
                break
            page_token = data.get('page_token')
            if not page_token:
                break
        return all_records, None

    def _feishu_extract_display_value(self, field_val, is_date=False):
        """提取飞书字段值为适合展示的值
        日期字段 → '2026-06-29' 字符串
        数字字段 → 数字
        文本/其他 → 字符串
        """
        if field_val is None:
            return '' if is_date else 0
        if isinstance(field_val, (int, float)):
            if is_date:
                try:
                    return time.strftime('%Y-%m-%d', time.localtime(field_val / 1000))
                except (ValueError, OSError):
                    return str(field_val)
            return field_val
        if isinstance(field_val, list):
            # 多选/成员/附件 — 取文本拼接
            parts = []
            for item in field_val:
                if isinstance(item, dict):
                    parts.append(item.get('text', '') or item.get('name', ''))
                elif isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, (int, float)):
                    parts.append(str(item))
            return ', '.join(p for p in parts if p) or ''
        if isinstance(field_val, dict):
            return field_val.get('text', '') or field_val.get('name', '') or str(field_val)
        # 尝试把字符串转为数字
        if isinstance(field_val, str):
            try:
                return float(field_val) if '.' in field_val else int(field_val)
            except ValueError:
                return field_val
        return str(field_val)

    def _feishu_parse_display_records(self, records, date_field, display_fields):
        """按选中字段提取所有记录为展示用表格行
        返回: [{"时间": "2026-06-29", "点赞": 11, "收藏": 2}, ...]
        按日期降序排列（最新在上）
        """
        rows = []
        for rec in records:
            row = {}
            for fname in display_fields:
                row[fname] = self._feishu_extract_display_value(
                    rec.get(fname), is_date=(fname == date_field)
                )
            if row.get(date_field):
                rows.append(row)
        # 日期降序：最新在上面
        rows.sort(key=lambda r: str(r.get(date_field, '')), reverse=True)
        return rows

    def _dashboard_parse_url(self):
        """POST /api/dashboard/parse-url
        Body: { url: "https://..." }
        支持:
          - 直接多维表格: /base/BASCxxxxx?table=tblYYYYY
          - 知识库:        /wiki/TOKEN（自动解析到多维表格）
        """
        body = self._read_body()
        if not body:
            return
        url = body.get('url', '').strip()
        if not url:
            self._serve_json({'error': '缺少飞书链接'}, 400)
            return

        # 1. 解析 URL
        parsed = self._parse_feishu_url(url)
        if not parsed:
            self._serve_json({'error': '无法解析飞书链接，支持多维表格链接和知识库链接'}, 400)
            return

        domain = parsed['domain']
        url_type = parsed.get('type', 'direct')

        # 2. 匹配凭证（支持跨域回退）
        matched_key, matched_cred = self._match_feishu_credential(domain)
        if not matched_cred:
            self._serve_json({
                'ok': True,
                'needsCredential': True,
                'domain': domain,
                'message': f'未找到可用的飞书凭证，请先在配置中添加'
            })
            return

        app_id = matched_cred.get('appId', '')
        app_secret = matched_cred.get('appSecret', '')
        if not app_id or not app_secret:
            self._serve_json({'error': f'凭证「{matched_key}」缺少 App ID 或 App Secret'}, 400)
            return

        # 3. 获取 tenant token
        token, err = self._feishu_tenant_token(app_id, app_secret)
        if err:
            self._serve_json({'error': err}, 400)
            return

        # 4. 根据链接类型解析
        if url_type == 'wiki':
            # 知识库链接：先解析 wiki node
            wiki_node, err = self._resolve_wiki_node(parsed['wikiToken'], token)
            if err:
                self._serve_json({'error': f'解析知识库节点失败：{err}'}, 400)
                return
            obj_type = wiki_node['objType']
            if obj_type != 'bitable':
                self._serve_json({
                    'error': f'该知识库节点类型为「{obj_type}」，不是多维表格。当前仅支持多维表格数据源。',
                    'nodeType': obj_type,
                    'nodeTitle': wiki_node['title']
                }, 400)
                return
            app_token = wiki_node['objToken']
            wiki_title = wiki_node['title']

            # 列出多维表格中的所有表
            tables, err = self._feishu_list_tables(app_token, token)
            if err:
                self._serve_json({'error': f'获取表格列表失败：{err}'}, 400)
                return
            if not tables:
                self._serve_json({'error': '该多维表格中没有表格'}, 400)
                return

            # 单表直接用，多表让用户选
            if len(tables) == 1:
                table_id = tables[0]['tableId']
                # 优先用知识库页面名，其次表格名
                table_name = wiki_title or tables[0]['name'] or '飞书多维表格'
            else:
                # 多表：返回列表让前端选择
                fields = []
                for t in tables:
                    # 尝试获取每个表的字段
                    t_fields, _ = self._feishu_list_fields(app_token, t['tableId'], token)
                    fields.append({
                        'tableId': t['tableId'],
                        'tableName': t['name'] or '未命名表格',
                        'fields': t_fields or []
                    })
                self._serve_json({
                    'ok': True,
                    'type': 'wiki_multi_table',
                    'appToken': app_token,
                    'domain': domain,
                    'credentialId': matched_key,
                    'credentialName': matched_cred.get('name', matched_key),
                    'wikiTitle': wiki_title,
                    'tables': fields
                })
                return

        else:
            # 直接链接
            app_token = parsed['appToken']
            table_id = parsed['tableId']
            # 优先用应用名，其次表格名
            app_name, _ = self._feishu_get_app_name(app_token, token)
            table_name, _ = self._feishu_get_table_name(app_token, table_id, token)
            table_name = app_name or table_name or '飞书多维表格'

        # 5. 获取字段列表
        fields, err = self._feishu_list_fields(app_token, table_id, token)
        if err:
            self._serve_json({'error': err}, 400)
            return

        self._serve_json({
            'ok': True,
            'type': url_type,
            'appToken': app_token,
            'tableId': table_id,
            'domain': domain,
            'credentialId': matched_key,
            'credentialName': matched_cred.get('name', matched_key),
            'tableName': table_name,
            'fields': fields
        })

    def _dashboard_sync_feishu(self):
        """POST /api/dashboard/sync-feishu
        Body: {
          project, folderId, sourceId, name, icon,
          appId, appSecret, appToken, tableId,
          columnMapping, boundFolders,
          mode: 'test' | 'sync'   # test=只测连接+拉字段, sync=完整拉取数据
        }
        """
        body = self._read_body()
        if not body:
            return
        project_id = body.get('project', '')  # 兼容旧参数
        folder_id = body.get('folderId', '')  # 新参数

        app_id = body.get('appId', '').strip()
        app_secret = body.get('appSecret', '').strip()
        app_token = body.get('appToken', '').strip()
        table_id = body.get('tableId', '').strip()
        credential_id = body.get('credentialId', '').strip()
        mode = body.get('mode', 'sync')

        # 如果给了 credentialId，从凭证配置中查找 appId/appSecret
        if credential_id and (not app_id or not app_secret):
            creds_data = self._load_feishu_credentials()
            cred = creds_data.get('credentials', {}).get(credential_id, {})
            app_id = app_id or cred.get('appId', '')
            app_secret = app_secret or cred.get('appSecret', '')

        if not (app_id and app_secret and app_token and table_id):
            self._serve_json({'error': '缺少 appId/appSecret/appToken/tableId，或 credentialId 对应的凭证不完整'}, 400)
            return

        # 1. 获取 token
        token, err = self._feishu_tenant_token(app_id, app_secret)
        if err:
            self._serve_json({'error': err}, 400)
            return

        # 2. test 模式：只拉字段，返回给前端做列映射
        fields, err = self._feishu_list_fields(app_token, table_id, token)
        if err:
            self._serve_json({'error': err}, 400)
            return

        if mode == 'test':
            self._serve_json({'ok': True, 'fields': fields})
            return

        # 3. sync 模式：拉取所有记录 + 按选中的字段解析
        display_fields = body.get('displayFields', [])
        date_field = body.get('dateField', '')
        if not display_fields or not date_field:
            # 兼容旧格式 columnMapping
            column_mapping = body.get('columnMapping', {})
            if column_mapping and column_mapping.get('metrics'):
                date_field = column_mapping.get('date', '')
                display_fields = [date_field] if date_field else []
                for key, cfg in column_mapping.get('metrics', {}).items():
                    col = cfg.get('column', '') or key
                    if col not in display_fields:
                        display_fields.append(col)
        if not display_fields:
            self._serve_json({'ok': True, 'needMapping': True, 'fields': fields})
            return

        records, err = self._feishu_list_records(app_token, table_id, token)
        if err:
            self._serve_json({'error': err}, 400)
            return

        rows = self._feishu_parse_display_records(records, date_field, display_fields)

        # 4. 写入数据源注册表（不再是项目配置）
        source_id = body.get('sourceId') or ('feishu_' + str(int(time.time() * 1000)))
        # 优先用飞书实际表名，再回落前端传的旧名
        feishu_table_name, _ = self._feishu_get_table_name(app_token, table_id, token)
        source_name = feishu_table_name or body.get('name') or '飞书表格'
        bound_folders = body.get('boundFolders', [])

        registry = self._load_datasources()
        sources = registry.get('datasources', {})

        existing = sources.get(source_id, {})
        sources[source_id] = {
            'name': source_name,
            'type': 'feishu',
            'credentialId': credential_id,
            'appToken': app_token,
            'tableId': table_id,
            'sourceUrl': body.get('sourceUrl', ''),
            'createdAt': existing.get('createdAt') or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'boundFolders': bound_folders or existing.get('boundFolders', []),
            'dateField': date_field,
            'displayFields': display_fields,
            'rawData': rows,
            'lastSyncAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }

        registry['datasources'] = sources
        self._save_datasources(registry)

        # 5. 对每个 boundFolder，自动生成/更新看板卡片
        all_folders = bound_folders or existing.get('boundFolders', [])
        card_count = 0
        for fid in all_folders:
            config = self._load_dashboard_config(fid)
            cards = config.get('cards', [])
            existing_keys = {(c['sourceId'], c['metricKey']) for c in cards}
            for metric_key, mcfg in column_mapping.get('metrics', {}).items():
                if (source_id, metric_key) in existing_keys:
                    continue
                cards.append({
                    'id': 'card_' + source_id + '_' + metric_key + '_' + str(int(time.time() * 1000))[-6:],
                    'sourceId': source_id,
                    'metricKey': metric_key,
                    'title': mcfg.get('label', metric_key),
                    'type': 'number_trend',
                    'unit': mcfg.get('unit', '')
                })
                existing_keys.add((source_id, metric_key))
                card_count += 1
            config['cards'] = cards
            self._save_dashboard_config(fid, config)

        # 兼容旧逻辑：如果传了 project（没有 boundFolders），也更新项目看板
        if project_id and not bound_folders:
            config = self._load_dashboard_config(project_id)
            cards = config.get('cards', [])
            existing_keys = {(c['sourceId'], c['metricKey']) for c in cards}
            for metric_key, mcfg in column_mapping.get('metrics', {}).items():
                if (source_id, metric_key) in existing_keys:
                    continue
                cards.append({
                    'id': 'card_' + source_id + '_' + metric_key + '_' + str(int(time.time() * 1000))[-6:],
                    'sourceId': source_id,
                    'metricKey': metric_key,
                    'title': mcfg.get('label', metric_key),
                    'type': 'number_trend',
                    'unit': mcfg.get('unit', '')
                })
                existing_keys.add((source_id, metric_key))
                card_count += 1
            config['cards'] = cards
            self._save_dashboard_config(project_id, config)

        self._serve_json({'ok': True, 'sourceId': source_id, 'rowCount': len(rows), 'cardCount': card_count})

    def _dashboard_analyze(self):
        """POST /api/dashboard/analyze — 触发分析
        Body: { folderId, cardId, agentId, prompt }
        Returns: { context: str, reportPath: str, agentId: str }
        """
        body = self._read_body()
        if not body:
            return
        folder_id = body.get('folderId', body.get('project', ''))  # 兼容旧参数 project
        card_id = body.get('cardId', '')
        agent_id = body.get('agentId', '')
        user_prompt = body.get('prompt', '')

        if not folder_id or not card_id:
            self._serve_json({'error': '缺少 folderId 或 cardId'}, 400)
            return

        config = self._load_dashboard_config(folder_id)
        card = next((c for c in config.get('cards', []) if c['id'] == card_id), None)
        if not card:
            self._serve_json({'error': '未找到卡片 ' + card_id}, 404)
            return

        # 从注册表查找数据源
        registry = self._load_datasources()
        all_sources = registry.get('datasources', {})
        source = all_sources.get(card.get('sourceId'))

        # 组装数据上下文（用注册表的 rawData 计算）
        data = None
        if source:
            rows = source.get('rawData', [])
            key = card.get('metricKey', '')
            if rows and key:
                series = sorted(
                    [(r.get('date', ''), float(r.get(key, 0))) for r in rows if r.get('date')],
                    key=lambda x: str(x[0])
                )
                if series:
                    values = [v for _, v in series]
                    labels = [d for d, _ in series]
                    current = values[-1]
                    previous = values[-2] if len(values) >= 2 else current
                    change = current - previous
                    change_percent = (change / abs(previous) * 100) if previous != 0 else 0
                    data = {'current': current, 'previous': previous, 'change': change, 'changePercent': change_percent, 'values': values, 'labels': labels}

        source_name = source.get('name', '手动输入') if source else '手动输入'
        folder_name = config.get('folderName', folder_id)

        context_lines = [
            f'[系统] 用户从「{folder_name}」业务看板触发了数据分析任务。',
            '',
            f'📊 分析指标：{card.get("title", card_id)}',
            f'📂 数据来源：{source_name}',
        ]

        if data:
            context_lines.append(f'📈 当前值：{format_metric_value_static(data["current"], card)}（{"↑+" if data["change"] >= 0 else ""}{data["changePercent"]:.1f}%）')
            context_lines.append('📊 近期趋势：')
            labels = data.get('labels', [])
            values = data.get('values', [])
            show = 7
            for i in range(max(0, len(values) - show), len(values)):
                pct = ''
                if i > 0 and values[i - 1] != 0:
                    p = ((values[i] - values[i - 1]) / abs(values[i - 1])) * 100
                    pct = f' ({"+" if p >= 0 else ""}{p:.1f}%)'
                context_lines.append(f'  - {labels[i] if i < len(labels) else ""}: {values[i]}{pct}')

        if source:
            updated = source.get('lastSyncAt', '')
            if updated:
                context_lines.append(f'')
                context_lines.append(f'🔗 数据更新时间：{updated}')

        if user_prompt:
            context_lines.append(f'')
            context_lines.append(f'📝 分析提示：{user_prompt}')

        # 确定报告保存路径
        date_str = time.strftime('%Y%m%d')
        card_title = card.get('title', card_id).replace('/', '_').replace('\\', '_')
        report_dir = os.path.join(DATA_DIR, 'dashboard-reports')
        report_filename = f'{card_title}_分析_{date_str}.md'
        report_path = os.path.join(report_dir, report_filename)

        context_lines.append(f'')
        context_lines.append(f'📁 报告保存路径：{report_path}')
        context_lines.append(f'')
        context_lines.append(f'请对该指标进行深度分析，自主选择分析维度和方法，产出结构化分析报告。')
        context_lines.append(f'分析完成后，请使用 write_file 工具将完整报告写入上述路径。')

        context_str = '\n'.join(context_lines)

        # 记录分析到 config
        analyses = config.get('analyses', [])
        analysis_id = f'analysis_{int(time.time() * 1000)}'
        analyses.append({
            'id': analysis_id,
            'cardId': card_id,
            'agentId': agent_id,
            'agentName': agent_id,
            'triggeredAt': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'reportPath': report_path,
            'summary': ''
        })
        config['analyses'] = analyses
        self._save_dashboard_config(folder_id, config)

        self._serve_json({
            'ok': True,
            'context': context_str,
            'reportPath': report_path,
            'analysisId': analysis_id,
            'agentId': agent_id or ''
        })

    def _dashboard_upload_csv(self):
        """POST /api/dashboard/upload-csv — 已废弃，改用飞书多维表格"""
        self._serve_json({'error': 'CSV 上传已废弃，请使用飞书多维表格作为数据源'}, 410)

    def _dashboard_import_csv(self):
        """POST /api/dashboard/import-csv — 已废弃"""
        self._serve_json({'error': 'CSV 导入已废弃，请使用飞书多维表格作为数据源'}, 410)

    def _read_body(self):
        """读取请求体 JSON"""
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    def _proxy_workbuddy(self):
        """将请求转发到 WorkBuddy 侧车进程 (Node.js SSE)."""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)

        SIDECAR_URL = 'http://127.0.0.1:9876/api/chat'
        SIDECAR_TIMEOUT = 300

        req = urllib.request.Request(
            SIDECAR_URL,
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        try:
            self._start_sse()

            with urllib.request.urlopen(req, timeout=SIDECAR_TIMEOUT) as resp:
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception:
            err_msg = 'event: error\ndata: {"code":"SIDECAR_DOWN","message":"WorkBuddy 侧车未启动，请先在 workbuddy-sidecar 目录下运行 node server.js"}\n\n'
            self.wfile.write(err_msg.encode('utf-8'))
            self.wfile.flush()

    def _serve_sse(self, event_type, data):
        """Send an SSE event."""
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(payload.encode('utf-8'))
        self.wfile.flush()

    def _serve_sse_error(self, message):
        """Send an SSE error event, ensuring headers are sent first."""
        if not getattr(self, '_sse_headers_sent', False):
            self._start_sse()
        self._serve_sse('error', {'message': message})

    def _mcp_discover(self):
        """Discover tools from enabled MCP servers."""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_error(400, 'Invalid JSON')
            return

        mcp_data = _load_json(MCP_FILE, {'items': DEFAULT_MCP, 'enabled': list(DEFAULT_MCP.keys())})
        items = mcp_data.get('items', {})
        enabled_ids = mcp_data.get('enabled', [])
        target_id = req.get('server_id')

        # If specific server requested
        if target_id and target_id in enabled_ids:
            cfg = items.get(target_id, {})
            cmd = cfg.get('command', '')
            if not cmd:
                self._serve_json({'results': {target_id: {'error': '没有配置启动命令'}}})
                return
            client = _get_mcp_client(target_id, cmd)
            result = client.list_tools()
            self._serve_json({'results': {target_id: result}})
            return

        # Discover all enabled
        enabled_servers = {sid: items[sid] for sid in enabled_ids if sid in items}
        results = _discover_mcp_tools(enabled_servers)
        self._serve_json({'results': results})

    def _start_sse(self):
        """Send SSE headers (idempotent)."""
        if getattr(self, '_sse_headers_sent', False):
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self._sse_headers_sent = True
        self.close_connection = True

    # --- Web tools ---
    def _web_search(self, query):
        """Search the web using DuckDuckGo HTML (no API key)."""
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                raw = resp.read().decode('utf-8', errors='replace')

            # Extract search results
            results = []
            # Match DuckDuckGo result snippets
            snippets = re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                raw, re.DOTALL
            )
            for href, title, snippet in snippets[:10]:
                title_clean = html.unescape(re.sub(r'<[^>]+>', '', title)).strip()
                snippet_clean = html.unescape(re.sub(r'<[^>]+>', '', snippet)).strip()
                if title_clean:
                    results.append({
                        'title': title_clean,
                        'url': html.unescape(href),
                        'snippet': snippet_clean[:300]
                    })

            if not results:
                # Fallback: try simpler extraction
                links = re.findall(
                    r'<a[^>]*class="result__url"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                    raw, re.DOTALL
                )
                for href, display in links[:10]:
                    results.append({
                        'title': html.unescape(re.sub(r'<[^>]+>', '', display)).strip() or href,
                        'url': html.unescape(href),
                        'snippet': ''
                    })

            return results if results else []
        except Exception as e:
            return [{'error': str(e)}]

    def _web_fetch(self, url):
        """Fetch a web page and extract its text content."""
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                content_type = resp.headers.get('Content-Type', '')
                raw = resp.read()

                # Try to decode
                charset = 'utf-8'
                ct_match = re.search(r'charset=([^\s;]+)', content_type)
                if ct_match:
                    charset = ct_match.group(1)
                try:
                    text = raw.decode(charset, errors='replace')
                except Exception:
                    text = raw.decode('utf-8', errors='replace')

                # Strip HTML tags for text extraction
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text)
                text = html.unescape(text).strip()

                if len(text) > 8000:
                    text = text[:8000] + '\n\n... (内容过长，已截断)'
                return text
        except Exception as e:
            return f'获取网页失败：{e}'

    def _agent_loop(self, req_data=None):
        """Agent loop with SSE streaming: think → tool_call → tool_result → response."""
        if req_data is not None:
            req = req_data
        else:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                req = json.loads(body)
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return

        api_key = req.get('api_key', '') or DEEPSEEK_API_KEY
        base_url = req.get('base_url', '')
        model = req.get('model', '')
        messages = req.get('messages', [])
        _t0 = time.time()
        with open(TIMING_FILE, 'a') as _tf: _tf.write(f'{time.strftime("%H:%M:%S")} --- Agent请求开始 ---\n')
        workspace = req.get('workspace', '')
        max_iter = req.get('max_iterations', 10)
        temperature = None

        if not api_key or not base_url:
            self._serve_json({'error': '请先配置模型和 API Key，或在终端设置 DEEPSEEK_API_KEY 环境变量'}, 400)
            return
        wpath = os.path.expanduser(workspace) if workspace else None
        if wpath and not os.path.isdir(wpath):
            self._serve_json({'error': f'工作区路径不存在：{wpath}'}, 400)
            return

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "description": "列出指定目录下的所有文件和子目录。path 为空或 '.' 时列出工作区根目录。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "相对于工作区根目录的路径，如 '.' 或 '产出' 或 'sop'"}
                        },
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读取指定文件的内容。默认从头读最多 8000 字符；文件更长时用 offset（起始字符）和 limit（读取字符数，默认 8000）分段读。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "相对于工作区根目录的文件路径，如 'sop/01-素材入库.md'"},
                            "offset": {"type": "integer", "description": "起始字符位置（默认 0），用于分段读取长文件"},
                            "limit": {"type": "integer", "description": "本次读取的字符数（默认 8000）"}
                        },
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "仅用于创建新文件（文件尚不存在时）。修改已有文件必须用 edit_file（精确替换），不要用 write_file 覆写整个文件——那样既浪费 token 又容易出错。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "相对于工作区根目录的文件路径，如 '产出/新文件.md'"},
                            "content": {"type": "string", "description": "要写入的完整文件内容"}
                        },
                        "required": ["path", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": "精确编辑文件：在工作区文件中查找并替换指定的文本。old_string 必须唯一匹配（只出现一次），否则工具会报错——你需要调整 old_string 使其更精确。类似 sed 的 s/old/new/，但需要更多上下文确保唯一匹配。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "相对于工作区根目录的文件路径"},
                            "old_string": {"type": "string", "description": "要被替换的文本。必须精确匹配原文件内容（包括缩进和换行），且必须在文件中唯一出现。如果有重复，请增加更多上下文使其唯一。"},
                            "new_string": {"type": "string", "description": "替换后的文本"}
                        },
                        "required": ["path", "old_string", "new_string"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "在工作区中递归搜索包含指定关键词的文件（大小写不敏感）。返回匹配文件路径 + 每个文件的匹配行号和上下文片段（最多 50 个文件，每文件最多 5 段）。支持文件名匹配。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "要搜索的关键词（会做大小写不敏感的子串匹配）"},
                            "path": {"type": "string", "description": "搜索的起始子目录，留空则搜索整个工作区"}
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "在互联网上搜索信息。当你需要查找最新信息、事实、新闻或不确定的知识时使用。返回标题、URL 和摘要。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜索关键词，如 'Python 3.13 新特性'"}
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": "抓取指定网页的文本内容。当需要阅读某篇文章或文档的完整内容时使用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "要抓取的网页 URL"}
                        },
                        "required": ["url"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "semantic_search",
                    "description": "在工作区中进行语义搜索（向量检索）。当用户按「意思」而非「关键词」查找时使用，如「我去年写的关于定价的内容」「那次线上事故的复盘」。返回最相关的文件路径 + 匹配片段 + 相似度分数。需要工作区已建立向量索引。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "用自然语言描述想找的内容，如「产品定价方案」「用户增长分析」"},
                            "top_k": {"type": "integer", "description": "返回结果数，默认 8"}
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "file_stats",
                    "description": "统计工作区的文件情况：总数、总大小、按扩展名/类型分布、按一级子文件夹分布。当用户问「这个文件夹多大」「有多少个 md 文件」「代码文件占多少」时使用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "统计的子目录，留空统计整个工作区"}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "recent_files",
                    "description": "按修改时间列出工作区最近变动的文件。当用户问「最近改过哪些文件」「上周的文档」「今天更新的内容」时使用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "description": "返回文件数，默认 20"},
                            "days": {"type": "integer", "description": "只返回最近 N 天内修改的文件，留空不限"},
                            "path": {"type": "string", "description": "限定子目录，留空为整个工作区"}
                        }
                    }
                }
            },
        ]

        # --- Discover MCP tools from enabled servers (must run before building tools list & _exec_tool) ---
        mcp_tools_map = {}  # full_name -> {server_id, tool_name, schema}
        mcp_data = _load_json(MCP_FILE, {'items': DEFAULT_MCP, 'enabled': list(DEFAULT_MCP.keys())})
        mcp_items = mcp_data.get('items', {})
        mcp_enabled = mcp_data.get('enabled', [])
        if mcp_enabled:
            enabled_mcp = {sid: mcp_items[sid] for sid in mcp_enabled if sid in mcp_items}
            # 缓存 MCP 工具列表，5 分钟内不重复发现
            cache_key = json.dumps({sid: cfg.get('command', '') for sid, cfg in enabled_mcp.items()}, sort_keys=True)
            now = time.time()
            global _MCP_TOOLS_CACHE
            cached = _MCP_TOOLS_CACHE.get('key') == cache_key and (now - _MCP_TOOLS_CACHE.get('ts', 0)) < 300
            if cached:
                mcp_discover_results = _MCP_TOOLS_CACHE.get('results', {})
            else:
                mcp_discover_results = _discover_mcp_tools(enabled_mcp)
                _MCP_TOOLS_CACHE = {'key': cache_key, 'ts': now, 'results': mcp_discover_results}
            for sid, res in mcp_discover_results.items():
                if 'error' in res:
                    continue
                for tool in res.get('tools', []):
                    tname = tool.get('name', '')
                    full_name = f'mcp_{sid}_{tname}'
                    mcp_tools_map[full_name] = {
                        'server_id': sid,
                        'tool_name': tname,
                        'schema': tool
                    }

        # Add MCP tools to the tools list
        for full_name, info in mcp_tools_map.items():
            schema = info['schema']
            tools.append({
                'type': 'function',
                'function': {
                    'name': full_name,
                    'description': schema.get('description', f'MCP tool: {info["tool_name"]}'),
                    'parameters': schema.get('inputSchema', {'type': 'object', 'properties': {}})
                }
            })

        # 如果选中了 Agent，按 Agent 的工具白名单过滤
        if agent_cfg:
            allowed = set(agent_cfg.get('tools', []))
            tools = [t for t in tools if t['function']['name'] in allowed]

        def _resolve(p):
            if not wpath:
                raise ValueError('未设置工作区。请在聊天中添加文件或文件夹到对话上下文。')
            full = os.path.normpath(os.path.join(wpath, p))
            if not full.startswith(os.path.normpath(wpath)):
                raise ValueError(f'不允许访问工作区之外的路径：{p}')
            return full

        def _exec_tool(call):
            name = call['function']['name']
            try:
                args = json.loads(call['function'].get('arguments', '{}'))
            except json.JSONDecodeError:
                return f'参数解析失败：{call["function"].get("arguments", "")}'

            try:
                if name == 'list_dir':
                    p = _resolve(args.get('path', '.'))
                    if not os.path.isdir(p):
                        return f'目录不存在：{args.get("path", ".")}'
                    items = []
                    for entry in sorted(os.listdir(p)):
                        if entry.startswith('.'):
                            continue
                        ep = os.path.join(p, entry)
                        tag = '📁' if os.path.isdir(ep) else '📄'
                        size = ''
                        if os.path.isfile(ep):
                            s = os.path.getsize(ep)
                            size = f' ({s}B)' if s < 1024 else f' ({s//1024}KB)'
                        items.append(f'{tag} {entry}{size}')
                    return '\n'.join(items) if items else '(空目录)'

                elif name == 'read_file':
                    p = _resolve(args['path'])
                    if not os.path.isfile(p):
                        return f'文件不存在：{args["path"]}'
                    with open(p, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                    total = len(content)
                    offset = max(0, int(args.get('offset', 0)))
                    limit = int(args.get('limit', 8000)) or 8000
                    if offset >= total:
                        return f'offset {offset} 已超过文件长度 {total}。文件已读完。'
                    chunk = content[offset:offset + limit]
                    if offset + limit < total:
                        chunk += f'\n\n... (已读 {offset + limit}/{total} 字符，继续读请设 offset={offset + limit})'
                    elif offset > 0:
                        chunk = f'(从字符 {offset} 开始读，共 {total} 字符)\n\n' + chunk
                    return chunk

                elif name == 'write_file':
                    p = _resolve(args['path'])
                    # 文件已存在时拒绝写入，引导模型用 edit_file
                    if os.path.isfile(p):
                        return f'❌ 文件 "{args["path"]}" 已存在，不允许用 write_file 覆写。请用 edit_file 进行精确修改：1) 先用 read_file 读取文件内容，2) 找到要改的部分作为 old_string，3) 用 edit_file(path, old_string, new_string) 替换。'
                    parent = os.path.dirname(p)
                    if not os.path.isdir(parent):
                        return f'目录不存在。请先用 list_dir 确认父目录'
                    os.makedirs(parent, exist_ok=True)
                    with open(p, 'w', encoding='utf-8') as f:
                        f.write(args['content'])
                    rel = os.path.relpath(p, wpath)
                    _track_file_change(wpath, rel)
                    return f'文件已创建：{args["path"]}'

                elif name == 'edit_file':
                    p = _resolve(args['path'])
                    if not os.path.isfile(p):
                        return f'文件不存在：{args["path"]}'
                    with open(p, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                    old = args['old_string']
                    new = args['new_string']
                    if old == new:
                        return 'old_string 和 new_string 相同，未做任何修改。'
                    count = content.count(old)
                    if count == 0:
                        return f'❌ 未找到要替换的文本。文件内容中不包含 old_string。请检查字符串是否精确匹配（包括缩进、空格、换行）。'
                    if count > 1:
                        return f'❌ old_string 在文件中出现了 {count} 次，不是唯一匹配。请增加更多上下文（前后几行）使 old_string 唯一。'
                    content = content.replace(old, new, 1)
                    with open(p, 'w', encoding='utf-8') as f:
                        f.write(content)
                    rel = os.path.relpath(p, wpath)
                    _track_file_change(wpath, rel)
                    return f'文件已编辑：{args["path"]}（替换了 1 处）'

                elif name == 'search_files':
                    query = args['query'].lower()
                    if not query:
                        return '请提供搜索关键词'
                    start = _resolve(args.get('path', '.'))
                    if not os.path.isdir(start):
                        start = wpath
                    # 文件名也参与匹配
                    fname_match = args.get('match_filename', True)
                    # 只索引文本类文件（二进制文件跳过，避免乱码）
                    text_exts = TEXT_EXTS
                    results = []
                    for root, dirs, files in os.walk(start):
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                        for fname in files:
                            if fname.startswith('.'):
                                continue
                            ext = os.path.splitext(fname)[1].lower()
                            if ext and ext not in text_exts:
                                continue
                            fp = os.path.join(root, fname)
                            try:
                                with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                                    content = f.read()
                            except Exception:
                                continue
                            rel = os.path.relpath(fp, wpath)
                            hit = False
                            snippet_lines = []
                            if fname_match and query in fname.lower():
                                hit = True
                                snippet_lines.append(f'  [文件名匹配]')
                            # 内容匹配：逐行找，保留匹配点 ±2 行上下文
                            content_lower = content.lower()
                            if query in content_lower:
                                hit = True
                                lines = content.split('\n')
                                matched_idx = set()
                                for i, line in enumerate(lines):
                                    if query in line.lower():
                                        for j in range(max(0, i-1), min(len(lines), i+2)):
                                            matched_idx.add(j)
                                # 合并连续行，最多取前 5 段，每段 ≤300 字
                                sorted_idx = sorted(matched_idx)
                                seg_count = 0
                                i = 0
                                while i < len(sorted_idx) and seg_count < 5:
                                    seg = []
                                    start_i = sorted_idx[i]
                                    while i < len(sorted_idx) - 1 and sorted_idx[i+1] == sorted_idx[i] + 1:
                                        seg.append(lines[sorted_idx[i]])
                                        i += 1
                                    seg.append(lines[sorted_idx[i]])
                                    i += 1
                                    seg_text = '\n'.join(seg)
                                    if len(seg_text) > 300:
                                        seg_text = seg_text[:300] + '…'
                                    snippet_lines.append(f'  L{start_i+1}: {seg_text}')
                                    seg_count += 1
                            if hit:
                                results.append(f'📄 {rel}\n' + '\n'.join(snippet_lines[:6]))
                    if not results:
                        return f'未找到包含 "{args["query"]}" 的文件'
                    return f'共 {len(results)} 个文件匹配：\n\n' + '\n\n'.join(results[:50])

                elif name == 'web_search':
                    results = self._web_search(args['query'])
                    if not results:
                        return f'未找到与 "{args["query"]}" 相关的搜索结果'
                    if isinstance(results[0], dict) and 'error' in results[0]:
                        return f'搜索失败：{results[0]["error"]}'
                    lines = []
                    for i, r in enumerate(results):
                        lines.append(f'{i+1}. [{r["title"]}]({r["url"]})')
                        if r.get('snippet'):
                            lines.append(f'   {r["snippet"]}')
                    return '\n'.join(lines)

                elif name == 'web_fetch':
                    return self._web_fetch(args['url'])

                elif name == 'semantic_search':
                    if not wpath:
                        return '未设置工作区，无法搜索'
                    results = _search_semantic(wpath, args['query'], int(args.get('top_k', 8)))
                    if not results:
                        return f'未找到与「{args["query"]}」语义相关的内容'
                    if len(results) == 1 and 'error' in results[0]:
                        return results[0]['error']
                    lines = [f'语义搜索「{args["query"]}」找到 {len(results)} 个相关文件：\n']
                    for i, r in enumerate(results):
                        lines.append(f'{i+1}. 📄 {r["file_path"]}（相似度 {r["score"]}）')
                        lines.append(f'   {r["snippet"]}')
                    return '\n'.join(lines)

                elif name == 'file_stats':
                    start = _resolve(args.get('path', '.'))
                    if not os.path.isdir(start):
                        start = wpath
                    total_files = 0
                    total_size = 0
                    by_ext = {}
                    by_dir = {}
                    for dirpath, dirs, files in os.walk(start):
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                        for fname in files:
                            if fname.startswith('.'):
                                continue
                            fp = os.path.join(dirpath, fname)
                            try:
                                sz = os.path.getsize(fp)
                            except OSError:
                                continue
                            total_files += 1
                            total_size += sz
                            ext = os.path.splitext(fname)[1].lower() or '(无扩展名)'
                            by_ext[ext] = by_ext.get(ext, 0) + 1
                            # 一级子目录
                            rel = os.path.relpath(dirpath, start)
                            top_dir = rel.split(os.sep)[0] if rel != '.' else '(根目录)'
                            by_dir[top_dir] = by_dir.get(top_dir, 0) + 1
                    def fmt_size(n):
                        for u in ['B','KB','MB','GB']:
                            if n < 1024: return f'{n:.1f}{u}'
                            n /= 1024
                        return f'{n:.1f}TB'
                    lines = [f'工作区统计（{args.get("path", ".")}）：',
                             f'- 文件总数：{total_files}',
                             f'- 总大小：{fmt_size(total_size)}',
                             f'- 按类型（前 10）：']
                    for ext, cnt in sorted(by_ext.items(), key=lambda x: -x[1])[:10]:
                        lines.append(f'    {ext}: {cnt}')
                    lines.append('- 按一级目录（前 10）：')
                    for d, cnt in sorted(by_dir.items(), key=lambda x: -x[1])[:10]:
                        lines.append(f'    {d}: {cnt}')
                    return '\n'.join(lines)

                elif name == 'recent_files':
                    start = _resolve(args.get('path', '.'))
                    if not os.path.isdir(start):
                        start = wpath
                    limit = int(args.get('limit', 20))
                    days = args.get('days')
                    cutoff = (time.time() - days * 86400) if days else 0
                    files_info = []
                    for dirpath, dirs, files in os.walk(start):
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                        for fname in files:
                            if fname.startswith('.'):
                                continue
                            fp = os.path.join(dirpath, fname)
                            try:
                                mt = os.path.getmtime(fp)
                            except OSError:
                                continue
                            if mt < cutoff:
                                continue
                            files_info.append((mt, os.path.relpath(fp, wpath)))
                    files_info.sort(reverse=True)
                    files_info = files_info[:limit]
                    if not files_info:
                        return '没有符合条件的文件'
                    lines = [f'最近修改的 {len(files_info)} 个文件：']
                    for mt, rel in files_info:
                        t = time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))
                        lines.append(f'  {t}  📄 {rel}')
                    return '\n'.join(lines)


                else:
                    # Check if it's an MCP tool
                    if name.startswith('mcp_') and name in mcp_tools_map:
                        info = mcp_tools_map[name]
                        result = _call_mcp_tool(info['server_id'], info['tool_name'], args)
                        return result
                    return f'未知工具：{name}'
            except ValueError as e:
                return str(e)
            except Exception as e:
                return f'执行出错：{e}'

        # --- SSE Agent loop ---
        self._start_sse()

        log_start = time.time()
        log_tools_used = []
        log_artifacts = []
        _append_log({
            'type': 'agent_start',
            'agentId': 'main',
            'agentName': '小See',
            'modelId': model,
            'workspace': wpath or '',
            'userMsg': next((m.get('content', '') for m in messages if m['role'] == 'user'), '')[:200],
        })

        iteration = 0
        system_msg = next((m for m in messages if m['role'] == 'system'), None)
        if not system_msg:
            base_prompt = '你叫"小See"，是用户的 AI 工作台助手（项目管理指挥部）。打招呼时自我介绍叫小See。重要规则：(1)每次回复必须以文字收尾——用了工具也要用一两句话总结结果。(2)文件/目录不存在时如实告知。(3)用户问电脑上任意文件夹时，用 list_dir/read_file 等文件工具探索，不要局限于工作区。(4)简单问题直接回复，不调工具。(5)绝对不使用任何 emoji 表情符号。中文回复。'
            if wpath:
                base_prompt += f' 工作区：{wpath}。文件操作优先用 edit_file 而非 write_file。可用工具：文件读写搜索、web 搜索抓取、语义检索等。中文回复。'
            else:
                base_prompt += ' 文件编辑优先用 edit_file。可用工具：文件读写、shell、web 搜索、子 Agent 调用。中文回复。'
            system_msg = {'role': 'system', 'content': base_prompt}
            messages.insert(0, system_msg)

        try:
            while iteration < max_iter:
                iteration += 1
                with open(TIMING_FILE, 'a') as _tf: _tf.write(f'{time.strftime("%H:%M:%S")} 第{iteration}轮开始 总耗时{time.time()-_t0:.1f}s\n')
                self._serve_sse('iteration', {'iteration': iteration, 'max_iter': max_iter})

                url = base_url.rstrip('/') + '/chat/completions'
                payload_dict = {
                    'model': model,
                    'messages': messages,
                    'tools': tools,
                    'tool_choice': 'auto',
                    'stream': True
                }
                if temperature is not None:
                    payload_dict['temperature'] = temperature
                payload = json.dumps(payload_dict).encode('utf-8')

                r = urllib.request.Request(url, data=payload, headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }, method='POST')

                # 流式读取：逐 token 推送，同时累积完整响应
                accumulated_content = ''
                accumulated_tool_calls = {}  # {index: {id, name, arguments}}
                finish_reason = ''
                usage = {}
                _t_api_start = time.time()
                _first_token = True

                with urllib.request.urlopen(r, timeout=120) as resp:
                    for line in resp:
                        if _first_token:
                            _first_token = False
                            with open(TIMING_FILE, 'a') as _tf: _tf.write(f'{time.strftime("%H:%M:%S")} 首token耗时{time.time()-_t_api_start:.1f}s 总耗时{time.time()-_t0:.1f}s\n')
                        line = line.decode('utf-8', errors='replace').strip()
                        if not line or not line.startswith('data: '):
                            continue
                        data_str = line[6:]  # strip 'data: ' prefix
                        if data_str == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choices = chunk.get('choices', [])
                        if not choices:
                            continue
                        delta = choices[0].get('delta', {})
                        finish_reason = choices[0].get('finish_reason', '')

                        # 累积 token 用量
                        if 'usage' in chunk:
                            usage = chunk['usage']

                        # 文本内容 → 立即推给前端
                        if 'content' in delta and delta['content']:
                            accumulated_content += delta['content']
                            self._serve_sse('token', {'content': delta['content']})

                        # 工具调用 → 累积（OpenAI 流式会分多个 chunk 传 tool_calls）
                        if 'tool_calls' in delta:
                            for tc in delta['tool_calls']:
                                idx = tc.get('index', 0)
                                if idx not in accumulated_tool_calls:
                                    accumulated_tool_calls[idx] = {
                                        'id': tc.get('id', ''),
                                        'name': '',
                                        'arguments': ''
                                    }
                                if 'id' in tc and tc['id']:
                                    accumulated_tool_calls[idx]['id'] = tc['id']
                                if 'function' in tc:
                                    if 'name' in tc['function'] and tc['function']['name']:
                                        accumulated_tool_calls[idx]['name'] = tc['function']['name']
                                    if 'arguments' in tc['function']:
                                        accumulated_tool_calls[idx]['arguments'] += tc['function']['arguments']

                # 重建为 OpenAI 格式的工具调用列表
                tool_calls_list = []
                for idx in sorted(accumulated_tool_calls.keys()):
                    tc = accumulated_tool_calls[idx]
                    if tc['name']:  # 只保留有名字的完整调用
                        tool_calls_list.append({
                            'id': tc['id'],
                            'type': 'function',
                            'function': {
                                'name': tc['name'],
                                'arguments': tc['arguments']
                            }
                        })

                # 记录 token 用量
                if usage:
                    _record_usage(model, 'main', '小See',
                                  usage.get('prompt_tokens', 0),
                                  usage.get('completion_tokens', 0))

                if finish_reason == 'tool_calls' or tool_calls_list:
                    tool_calls = tool_calls_list
                    # 执行每个工具调用一次，结果同时用于 SSE 展示和 messages 历史
                    tool_results = []
                    for tc in tool_calls:
                        fn_name = tc['function']['name']
                        fn_args = tc['function'].get('arguments', '{}')
                        self._serve_sse('tool_call', {
                            'id': tc['id'],
                            'name': fn_name,
                            'arguments': fn_args
                        })
                        result_text = _exec_tool(tc)
                        tool_results.append(result_text)
                        if fn_name not in log_tools_used:
                            log_tools_used.append(fn_name)
                        if fn_name == 'write_file':
                            try:
                                wf_args = json.loads(fn_args)
                                log_artifacts.append(wf_args.get('path', ''))
                            except: pass
                        self._serve_sse('tool_result', {
                            'id': tc['id'],
                            'name': fn_name,
                            'result': result_text[:5000]
                        })
                    # Add to message history（复用已执行的结果，不重复执行）
                    messages.append({
                        'role': 'assistant',
                        'content': accumulated_content,
                        'tool_calls': tool_calls
                    })
                    for tc, result_text in zip(tool_calls, tool_results):
                        messages.append({
                            'role': 'tool',
                            'tool_call_id': tc['id'],
                            'content': result_text
                        })
                    continue

                # Final text response
                final_content = accumulated_content
                self._serve_sse('response', {
                    'content': final_content,
                    'iterations': iteration,
                    'model': model
                })
                self._serve_sse('done', {})
                _append_log({
                    'type': 'agent_end',
                    'agentId': 'main',
                    'agentName': '小See',
                    'modelId': model,
                    'iterations': iteration,
                    'toolsUsed': log_tools_used,
                    'artifacts': log_artifacts,
                    'duration': round(time.time() - log_start, 1),
                    'status': 'success',
                })
                return

            # Max iterations reached
            self._serve_sse('response', {
                'content': '达到最大执行轮次，但任务可能未完成。请检查结果或简化指令。',
                'iterations': iteration,
                'model': model
            })
            self._serve_sse('done', {})
            _append_log({
                'type': 'agent_end',
                'agentId': 'main',
                'agentName': '小See',
                'modelId': model,
                'iterations': iteration,
                'toolsUsed': log_tools_used,
                'artifacts': log_artifacts,
                'duration': round(time.time() - log_start, 1),
                'status': 'max_iter',
            })

        except urllib.error.HTTPError as e:
            self._serve_sse('error', {'message': self._format_api_error(e)})
            _append_log({
                'type': 'agent_end',
                'agentId': 'main',
                'agentName': '小See',
                'modelId': model,
                'duration': round(time.time() - log_start, 1),
                'status': 'error',
                'error': self._format_api_error(e)[:200],
            })
        except Exception as e:
            self._serve_sse('error', {'message': str(e)})
            _append_log({
                'type': 'agent_end',
                'agentId': 'main',
                'agentName': '小See',
                'modelId': model,
                'duration': round(time.time() - log_start, 1),
                'status': 'error',
                'error': str(e)[:200],
            })

    def _format_api_error(self, e):
        """Parse HTTPError response and return a user-friendly message."""
        error_body = e.read().decode('utf-8', errors='ignore')
        try:
            err_json = json.loads(error_body)
            api_msg = err_json.get('error', {}).get('message', '')
            if api_msg:
                if 'insufficient' in api_msg.lower() or 'balance' in api_msg.lower() or e.code == 402:
                    return '❌ 账户余额不足 (402)\n\n你的 API 密钥余额已耗尽，请前往对应平台充值，或在「模型」模块中添加其他 API 提供商。'
                return f'API 错误 {e.code}: {api_msg}'
        except Exception:
            pass
        return f'API 错误 {e.code}: {error_body[:300]}'

    def log_message(self, format, *args):
        pass


def run_server(host='127.0.0.1', port=8765, data_dir=None, open_browser=False):
    """启动 Seegent 服务器（程序化调用入口）。"""
    global DATA_DIR, STATE_FILE, MODELS_FILE, ENGINES_FILE, CLI_FILE, MCP_FILE
    global CHAT_FILE, PROMPTS_FILE, WORKSPACES_FILE, SKILLS_FILE, ROLES_FILE
    global EMBEDDING_FILE, INDEX_DB, DATA_PLATFORMS_FILE, LOG_FILE, USAGE_FILE

    if data_dir:
        os.environ['SEEGENT_DATA_DIR'] = data_dir
        DATA_DIR = _get_data_dir()
        STATE_FILE = os.path.join(DATA_DIR, '.seegent-state.json')
        MODELS_FILE = os.path.join(DATA_DIR, '.seegent-models.json')
        ENGINES_FILE = os.path.join(DATA_DIR, '.seegent-engines.json')
        CLI_FILE = os.path.join(DATA_DIR, '.seegent-cli.json')
        MCP_FILE = os.path.join(DATA_DIR, '.seegent-mcp.json')
        CHAT_FILE = os.path.join(DATA_DIR, '.seegent-chats.json')
        PROMPTS_FILE = os.path.join(DATA_DIR, '.seegent-prompts.json')
        WORKSPACES_FILE = os.path.join(DATA_DIR, '.seegent-workspaces.json')
        SKILLS_FILE = os.path.join(DATA_DIR, '.seegent-skills.json')
        ROLES_FILE = os.path.join(DATA_DIR, '.seegent-roles.json')
        EMBEDDING_FILE = os.path.join(DATA_DIR, '.seegent-embedding.json')
        INDEX_DB = os.path.join(DATA_DIR, '.seegent-index.db')
        DATA_PLATFORMS_FILE = os.path.join(DATA_DIR, '.seegent-data-platforms.json')
        LOG_FILE = os.path.join(DATA_DIR, '.seegent-logs.json')
        USAGE_FILE = os.path.join(DATA_DIR, '.seegent-usage.json')

    # 确保数据目录存在
    os.makedirs(DATA_DIR, exist_ok=True)

    # 初始化默认配置文件
    _load_json(STATE_FILE, {})
    _load_json(MODELS_FILE, {"models": []})
    _load_json(CLI_FILE, {"items": DEFAULT_CLI, "enabled": list(DEFAULT_CLI.keys())})
    _load_json(MCP_FILE, {"items": DEFAULT_MCP, "enabled": list(DEFAULT_MCP.keys())})
    _load_json(CHAT_FILE, DEFAULT_CHATS)
    _load_json(PROMPTS_FILE, DEFAULT_PROMPTS)
    _load_json(WORKSPACES_FILE, DEFAULT_WORKSPACES)
    _load_json(SKILLS_FILE, DEFAULT_SKILLS)
    _load_json(ROLES_FILE, {"roles": _preset_roles(), "activeRole": ""})
    _load_json(LOG_FILE, {'logs': []})
    _load_json(USAGE_FILE, {'usage': []})

    url = f'http://localhost:{port}' if host == '127.0.0.1' else f'http://{host}:{port}'
    print(f'Seegent → {url}')

    if open_browser:
        import webbrowser
        webbrowser.open(url)

    HTTPServer.allow_reuse_address = True
    HTTPServer((host, port), Handler).serve_forever()



# ===== 看板静态辅助函数 =====

def compute_metric_data_static(card, config):
    """纯 Python 版本的指标数据计算（供 analyze API 使用）"""
    source = next((s for s in config.get('datasources', []) if s['id'] == card.get('sourceId')), None)
    rows = (source or {}).get('rawData', [])
    key = card.get('metricKey', '')
    if not rows or not key:
        return None
    series = sorted(
        [(r.get('date', ''), float(r.get(key, 0))) for r in rows if r.get('date')],
        key=lambda x: str(x[0])
    )
    if not series:
        return None
    values = [v for _, v in series]
    labels = [d for d, _ in series]
    current = values[-1]
    previous = values[-2] if len(values) >= 2 else current
    change = current - previous
    change_percent = (change / abs(previous) * 100) if previous != 0 else 0
    return {'current': current, 'previous': previous, 'change': change, 'changePercent': change_percent, 'values': values, 'labels': labels}


def format_metric_value_static(val, card):
    """格式化指标值"""
    if val is None:
        return '—'
    unit = card.get('unit', '')
    if card.get('type') == 'percentage' or unit == '%':
        return f'{float(val):.1f}%'
    n = float(val)
    if abs(n) >= 10000:
        return f'{n:,.0f}'
    return f'{n:.2f}'


if __name__ == '__main__':
    run_server(open_browser=True)

