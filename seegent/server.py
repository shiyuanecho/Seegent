#!/usr/bin/env python3
"""Seegent local server — serves static files, persists state, proxies LLM calls."""

import json
import os
import re
import importlib.util
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
import uuid
import urllib.request
import urllib.error
import urllib.parse
import html
import ssl
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

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
TRACKS_FILE = os.path.join(DATA_DIR, '.seegent-tracks.json')
PROJECTS_FILE = os.path.join(DATA_DIR, '.seegent-projects.json')
LOG_FILE = os.path.join(DATA_DIR, '.seegent-logs.json')
MAX_LOG_ENTRIES = 500
USAGE_FILE = os.path.join(DATA_DIR, '.seegent-usage.json')

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



# Default chat history

# Default system prompts


# Default Skills registry — installable SKILL.md workflow guides




# ===== 角色系统：聊天角色改从「个人团队文件夹」(拾元/syteam) 动态读取 =====


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


# ===== 操作日志聚合（跨 AI 工具本地记录，只读展示） =====
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
        # with 上下文确保连接在任何路径下都关闭，避免行处理异常时泄漏连接
        with sqlite3.connect(db) as con:
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


# ===== 文件夹树浏览通用实现（团队 / 技能复用，根目录经路径函数注入）=====
# 二进制/压缩文件不在文件夹浏览器里呈现（无法作为内容预览，且点开会显示乱码）
_FOLDER_FILE_SKIP_EXT = {'.zip', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.mp4', '.mov',
                         '.mp3', '.wav', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}


def _scan_folder_tree(rel, full_path_fn, root_path_fn):
    """返回 <root>/<rel> 目录下的直接子项（目录在前、文件在后，均按名排序）。
    目录 = 业务/工作流或子分组；.md 文件解析 frontmatter 取展示信息。"""
    rel = _team_safe_rel(rel)
    full = full_path_fn(rel)
    root = root_path_fn()
    if not os.path.isdir(full):
        return {'root': root, 'rel': rel, 'parent': '', 'entries': [], 'empty': True}
    try:
        names = sorted(os.listdir(full))
    except Exception:
        return {'root': root, 'rel': rel, 'parent': '', 'entries': [], 'empty': True}
    dirs = [n for n in names if os.path.isdir(os.path.join(full, n)) and n not in TEAM_SKIP]
    files = [n for n in names if os.path.isfile(os.path.join(full, n)) and n not in TEAM_SKIP
             and os.path.splitext(n)[1].lower() not in _FOLDER_FILE_SKIP_EXT]
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


def _read_folder_file(rel, full_path_fn, root_path_fn):
    """读取 <root>/<rel> 文件内容，带越界保护。返回 (data, error)。"""
    rel = _team_safe_rel(rel)
    if not rel:
        return None, '缺少文件路径'
    full = full_path_fn(rel)
    root = os.path.abspath(root_path_fn())
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


def _collect_folder_text(rel, full_path_fn, limit=200 * 1024):
    """递归收集 <root>/<rel> 下所有 .md/.txt 正文，拼成一段文本，供「召唤文件夹」的网页端指令内联。"""
    rel = _team_safe_rel(rel)
    full = full_path_fn(rel)
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


def _team_full_path(rel):
    root = _team_path()
    if not rel:
        return root
    return os.path.join(root, rel)


def _scan_team_tree(rel=''):
    """返回 syteam/<rel> 目录下的直接子项（目录在前、文件在后，均按名排序）。
    目录 = 业务/工作流或子分组；.md 文件解析 frontmatter 取展示信息。"""
    return _scan_folder_tree(rel, _team_full_path, _team_path)


def _read_team_file(rel):
    """读取 syteam/<rel> 文件内容，带越界保护。返回 (data, error)。"""
    return _read_folder_file(rel, _team_full_path, _team_path)


def _collect_team_folder_text(rel='', limit=200 * 1024):
    """递归收集 syteam/<rel> 下所有 .md/.txt 正文，拼成一段文本，供「召唤文件夹」的网页端指令内联。"""
    return _collect_folder_text(rel, _team_full_path, limit)


# ===== 技能（个人专属，syskill/）文件夹树接口：与团队同款，根目录换成技能目录 =====
def _skill_full_path(rel):
    root = _skill_path()
    if not rel:
        return root
    return os.path.join(root, rel)


def _scan_skill_tree(rel=''):
    """返回 syskill/<rel> 目录下的直接子项（目录在前、文件在后），供技能模块文件夹树浏览器。"""
    return _scan_folder_tree(rel, _skill_full_path, _skill_path)


def _read_skill_file(rel):
    """读取 syskill/<rel> 文件内容，带越界保护。返回 (data, error)。"""
    return _read_folder_file(rel, _skill_full_path, _skill_path)


def _collect_skill_folder_text(rel='', limit=200 * 1024):
    """递归收集 syskill/<rel> 下所有 .md/.txt 正文，供「召唤文件夹」的网页端指令内联。"""
    return _collect_folder_text(rel, _skill_full_path, limit)


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




class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
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
        if self.path.startswith('/api/logs'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            limit = int(qs.get('limit', [100])[0])
            agent_id = qs.get('agent', [None])[0]
            status = qs.get('status', [None])[0]
            source = qs.get('source', [None])[0]
            return self._serve_json({'logs': _merge_all_logs(limit=limit, agent_id=agent_id, status=status, source=source)})
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
        if self.path == '/api/health':
            return self._serve_json({'ok': True})
        if self.path.startswith('/api/datasources'):
            if self.path.startswith('/api/datasources/bind'):
                self.send_error(405)  # Method not allowed for GET
                return
            if self.command == 'DELETE':
                return self._handle_datasources_delete()
            return self._handle_datasources()
        return super().do_GET()

    def do_POST(self):
        # self.path 可能包含完整 URL（如 http://127.0.0.1:8765/api/datasources），提取纯路径
        if '://' in self.path:
            from urllib.parse import urlparse
            self.path = urlparse(self.path).path

        from urllib.parse import urlparse, parse_qs
        request_path = urlparse(self.path).path

        if self.path == '/api/state':
            return self._save_json_endpoint(STATE_FILE)
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
        if self.path.startswith('/api/convert-office'):
            return self._convert_office()
        if self.path.startswith('/api/dashboard'):
            return self._handle_dashboard_post()
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
        # basename 剥离目录分量，防止 ../ 路径穿越写入临时目录之外的路径
        filename = os.path.basename(qs.get('name', ['document'])[0])
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
            self._send_cors()
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

    # ========== 原有聊天代理方法 ==========

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

    def _send_cors(self):
        """仅放行同源请求的跨域头。

        服务绑定本机且无鉴权：若返回 Access-Control-Allow-Origin: *，
        用户浏览的任意恶意网页都能跨域读取本机接口数据。
        同源请求浏览器不发送 Origin 头，直接放行；
        跨域请求仅当 Origin 主机与请求 Host 一致时才回显该 Origin，否则不发任何 ACAO 头。
        """
        origin = self.headers.get('Origin')
        if not origin:
            return
        host = self.headers.get('Host', '')
        try:
            from urllib.parse import urlparse
            origin_host = urlparse(origin).netloc
        except Exception:
            origin_host = ''
        if origin_host and origin_host == host:
            self.send_header('Access-Control-Allow-Origin', origin)

    def _serve_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)


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

        parts = self.path.rstrip('/').split('/')

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
        """POST /api/dashboard/config — 保存看板卡片配置（不存数据源）。其余路径返回 404。"""
        path = self.path
        if path == '/api/dashboard/config' or path.startswith('/api/dashboard/config?'):
            return self._dashboard_save_config()
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

    # --- Web tools ---
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
    global DATA_DIR, STATE_FILE

    if data_dir:
        os.environ['SEEGENT_DATA_DIR'] = data_dir
        DATA_DIR = _get_data_dir()
        STATE_FILE = os.path.join(DATA_DIR, '.seegent-state.json')

    # 确保数据目录存在
    os.makedirs(DATA_DIR, exist_ok=True)

    # 初始化默认配置文件
    _load_json(STATE_FILE, {})

    url = f'http://localhost:{port}' if host == '127.0.0.1' else f'http://{host}:{port}'
    print(f'Seegent → {url}')

    if open_browser:
        import webbrowser
        webbrowser.open(url)

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == '__main__':
    run_server(open_browser=True)
