# Seegent 项目记忆

## 定位

**带 AI 的本地可视化工作台。** 你的文件 + 你的 AI Key + 你的规则 = 你的工作台。没人能看到你的数据。

核心原则：
- 所有文件内容留在本地（浏览器 IndexedDB + 本地文件系统）
- LLM 调用直连用户配置的 provider，不经过 Seegent 中转存储
- API Key 由用户自己管理，绝不存明文在代码仓库
- 单 Agent + 丰富工具 + 可视化文件管理，不做多 Agent 编排

## 架构概览

- `index.html`：单文件前端，包含所有 JS/CSS/HTML
- `server.py`：Python HTTP 服务器，静态文件 + API 代理 + JSON 持久化
- 运行方式：`python3 server.py` → `http://localhost:8765`

## 模块总览

左侧导航共 5 个模块：

| 模块 | 数据源 | 说明 |
|------|--------|------|
| 文件 | IndexedDB + File System Access API | 文件夹树、标签页编辑、Markdown/HTML 预览 |
| 模型 | `.seegent-models.json` | Provider CRUD、API Key 管理、模型勾选、聊天选择器 |
| CLI | `.seegent-cli.json` | 已连接 CLI 工具展示（Cursor Agent、Hermes Agent） |
| MCP | `.seegent-mcp.json` | MCP CLI 服务器管理（CRUD + 启用/禁用 + 工具发现 + Agent 集成） |
| 角色 | `.seegent-roles.json` | 系统角色管理（单选 + CRUD），聊天时作为 system prompt 注入 |

> 社交平台接入（如飞书）走 MCP 模块：在 `DEFAULT_MCP`（server.py）+ `.seegent-mcp.json` 中维护预置条目，命令里留 `<your_app_id>`/`<your_app_secret>` 占位符，用户编辑卡片填入真实凭证后即可被 Agent 调用。新平台只需在这两处加条目，无需改 agent/前端。

模块切换通过 `switchModule(name)` → 对应 `#{name}-module` div 的 `.active` 类切换。activeModule 持久化到 localStorage，启动时 `setTimeout` + try-catch 恢复。

## 文件夹持久化机制

### 原理
文件夹通过 File System Access API 的 `FileSystemDirectoryHandle` 访问。Handle 对象可通过结构化克隆存入 IndexedDB，刷新/重启后从中恢复。

### 关键数据流
1. 用户添加文件夹 → `showDirectoryPicker()` → 获得 `FileSystemDirectoryHandle`
2. 立即存入 IndexedDB（`saveFolderHandle`）+ localStorage（元数据后备）
3. 页面加载时 `init()` → `getSavedFolders()` 从 IndexedDB 读取
4. 对每个 handle 调用 `queryPermission({ mode: 'readwrite' })`：
   - `'granted'` → 直接恢复到侧边栏
   - 其他值 → 加入待授权列表，显示"一键恢复"横幅
5. 用户点击恢复 → `requestPermission()`（需用户手势）→ 重新授权后写回 IndexedDB

### 重要原则
- **绝不在权限检查失败时删除 IndexedDB 记录**：浏览器重启后权限会被清除，`queryPermission()` 可能返回非 `'granted'`。此时应保留记录，等用户手势重新授权。只有用户明确拒绝或 handle 完全失效时才删除。
- **反序列化后验证 handle 有效性**：检查 `typeof handle.queryPermission === 'function'`，结构化克隆可能损坏 handle。
- **恢复成功后重新持久化**：`requestPermission()` 成功后，调用 `saveFolderHandle()` 更新 IndexedDB。
- **localStorage 作为元数据后备**：仅存 `{ id, name }`，IndexedDB 完全不可用时至少能提示用户之前添加过哪些文件夹。

## 文件夹排序

- 排序通过 localStorage key `seegent-folder-order` 持久化（`state.folders.map(f => f.id)` 数组）
- `saveFolderOrder()` 在添加/排序时写入，`applyFolderOrder()` 在 init 时恢复
- 新增文件夹追加到排序末尾，排序中不存在的旧文件夹保持原位置

## DOM 引用模式

### 规则：声明式映射 + 自动生成 + 启动校验

```javascript
const DOM_SELECTORS = {
  sidebar:    '#sidebar',
  folderList: '#folder-list',
  // 新增元素只需加一行
};

const dom = {};
for (const [key, sel] of Object.entries(DOM_SELECTORS)) {
  dom[key] = document.querySelector(sel);
  if (!dom[key]) console.warn('[Seegent] ⚠️ DOM 缺失: ' + key + ' ← ' + sel);
}
```

### 为什么
- 手写 `const dom = { sidebar: $('#sidebar'), ... }` 容易漏加属性
- `dom.undefined.addEventListener(...)` → TypeError → 中断整个 script 块 → 后续 `init()` 不执行
- 声明式映射 + 启动校验杜绝了这类问题

## JS 错误传播陷阱

两个 `<script>` 块在 `<body>` 末尾同步执行。若第一个 script 块中任意一行抛出未捕获错误，该块后续代码全部跳过。

常见场景：`dom.xxx.addEventListener(...)` 中 `dom.xxx` 为 undefined → TypeError → 后面的 `init()` 从未执行 → 即使 IndexedDB 中有数据也无法恢复。

## 聊天功能

### 会话数据模型
```javascript
{ id, name, contextItems: [{type,name,handle,icon}], messages: [{role,content}], modelId, systemPrompt, systemPromptText }
```

### 布局顺序（从上到下）
1. 对话 tabs 行（+ 按钮 + 对话标签）
2. 模型选择 + 系统提示选择 + 折叠按钮
3. 自定义提示文本框（选"自定义提示"时显示）
4. 文件上下文标签
5. 消息区
6. 输入框

### 每会话独立配置
- 模型选择 `onchange` 立即写入 `session.modelId` + `saveChatSessions()`
- 提示选择 `onchange` 立即写入 `session.systemPrompt` + `saveChatSessions()`
- `renderChatUI()` 每次渲染时恢复当前会话的选择器状态

### 系统提示数据流
- 提示词模块 → `promptConfig.prompts` → `refreshPromptSelect()` → 聊天下拉框
- 动态加载自 `/api/prompts`，CRUD 操作后自动同步
- 下拉框占位文字："选择模板"（`value=""` disabled）
- "无模板" 的 id 为 `"none"`（不是空字符串，避免和占位符冲突）

## 模型管理

### 数据模型
- 配置文件 `.seegent-models.json`，结构为 `{ providers: { pid: { key, models[] } }, default: "pid/model" }`
- Provider 注册表 `PROVIDER_REGISTRY` 定义内置 provider 及其模型元数据（ctx、cost）
- 旧格式 `{ models: [...] }` 自动迁移到新格式
- `getProviderModelMap()` 遍历所有已配置 provider 的启用模型，扁平化为聊天选择器选项

## 已修复的 Bug 及根因

### 1. 返回按钮不工作
- **现象**：点击 `← 返回` 无反应
- **根因**：`modelBackTarget` 存入字符串函数名，但 `modelGoBack()` 直接当函数调用
- **修复**：判断 `typeof`，字符串则通过 `window[name]` 查找函数

### 2. 浏览器刷新后回到首页
- **现象**：在模型管理页面刷新，自动跳回文件管理页
- **根因**：`activeModule` 默认写死 `'file'`，切换模块时未持久化
- **修复**：切换模块时 `localStorage.setItem('seegent-module', name)`，启动时从 localStorage 恢复

### 3. 刷新后页面全白、点不动
- **现象**：加了模块持久化后，刷新页面所有内容消失
- **根因**：IIFE 同步执行时 localStorage 值对应的模块 DOM 不存在，所有 `.active` 类被移除
- **修复**：用 `setTimeout(fn, 100)` + `getElementById` 校验 + try-catch

### 4. 模型列表中多出旧模型 ID
- **现象**：DeepSeek 配了两个 V4 模型，但聊天选择器显示 3 个
- **根因**：旧格式迁移时把 `deepseek-chat` 写入 `models` 数组，后续编辑时旧 ID 未被清理
- **修复**：清理 `.seegent-models.json` 中的旧 ID，`showProviderDetail` 做防御处理

### 5. 第二个 script 块崩溃导致部分功能失效
- **现象**：页面加载后右键菜单、模块切换等功能异常
- **根因**：死代码 `updateChatContext()` 调用了不存在的 `getFileContext()` → ReferenceError → 第二个 script 块中断
- **修复**：删除死代码（`updateChatContext` 函数 + 启动调用 + `switchModule` wrap）

### 6. 新建文件夹变成文档
- **现象**：右键菜单"新建文件夹"创建出来的图标是文档而非文件夹
- **根因**：`insertIntoTreeNode()` 写死 `kind: 'file'`，不传参时所有插入都当成文件
- **修复**：加 `kind` 参数（默认 `'file'`），文件夹创建时传 `'directory'`；另新增 `insertIntoTreeNodes()` 做 name 兜底匹配

### 7. 系统提示选择器刷新后空白
- **现象**：刷新页面后提示词下拉框不显示选中值，一片空白
- **根因**：`loadPromptConfig()` 异步加载，可能在 `renderChatUI()` 之后才完成；且"无模板"的 id 是空字符串 `""`，和占位符 value 冲突
- **修复**：
  - "无模板" id 从 `""` 改为 `"none"`
  - 占位改为 `<option value="" disabled selected>选择模板</option>`
  - `refreshPromptSelect()` 恢复当前会话的选中值
  - `renderChatUI()` 每次都恢复选择器状态

### 8. 聊天区横条左边缘不对齐
- **现象**：tabs 行、模型选择行、消息区左边缘不在同一条线上
- **根因**：各元素 padding 不统一（12px、14px、16px 混用）
- **修复**：统一所有聊天区横条左右内边距为 16px

## 遗留问题

### 聊天区横条与左侧模块区未对齐 ✅ 已修复（2026-06-13）
- **现象**：聊天区顶部横条（tabs 行、模型/提示选择行）与左侧模块内容区（mgmt-panel）存在肉眼可见的水平错位
- **根因**：`#module-nav` 的 `padding-top` 为 12px，而 `.chat-sessions` 的 `padding-top` 仅为 5px，两个 flex 子元素内第一个可见内容的顶部偏移量不一致
- **修复**：
  - `#module-nav` 的 `padding-top` 从 `12px` 改为 `8px`
  - `.chat-sessions` 的 `padding` 从 `5px 16px` 改为 `8px 16px 5px`（顶部从 5px 统一到 8px）
  - 两个 flex 子元素的首行内容现在起点一致

### 文件夹/文件拖拽排序和移动不工作 ✅ 已修复（2026-06-13）
- **现象**：拖拽文件树中的文件或文件夹，不会出现蓝色插入线或高亮，无法排序也无法移入子文件夹
- **根因**：`dragData` 全局变量被两种拖拽操作共享——文件夹标题拖拽设置 `{reorder:true,...}`（无 `node` 属性），树节点拖拽设置 `{node,...}`（无 `reorder` 属性）。树节点的 `dragover`/`drop` 处理函数直接访问 `dragData.node.handle`，当文件夹标题被拖拽时 `dragData.node` 为 `undefined`，导致 `TypeError` 崩溃，所有后续 `preventDefault()` 和 CSS 类添加均被跳过
- **修复**（4 处）：
  1. 树节点 `dragover`：添加 `if (dragData.reorder) return` 和 `if (!dragData.node)` 安全守卫
  2. 树节点 `dragleave`：添加 `if (item.contains(e.relatedTarget)) return` 防止进入子元素时闪烁
  3. 树节点 `drop`：添加 `if (!dragData || dragData.reorder || !dragData.node) return`
  4. 文件夹标题 `dragover`/`drop`/`dragleave`：添加 `else if (dragData.node)` 安全守卫，并修复 `dragleave` 的 `relatedTarget` 检查

### PPT 转 PDF 预览 ✅ 已修复（2026-06-13）
- **问题**：server.py 中 `/api/convert-office` 端点需要 LibreOffice，但未安装
- **修复**：安装 LibreOffice 26.2.4 到 `/Applications/LibreOffice.app`，`server._find_libreoffice()` 自动发现并可用


### DeepSeek API Key GitHub 泄露 ✅ 已修复（2026-06-13）
- **事故**：`.seegent-models.json` 含明文 DeepSeek API Key，commit `d2aa6d5`（2026-06-11 14:07）push 到公开 GitHub 仓库，key 被第三方盗用，6/11-6/12 余额耗尽
- **修复**：
  - `git filter-branch` 清除所有历史中的敏感配置文件 + force push
  - `.gitignore` 新增所有 `.seegent-*.json`，防止再次误提交
  - `server.py` 新增 `DEEPSEEK_API_KEY` 环境变量兜底：`api_key = req.get('api_key', '') or DEEPSEEK_API_KEY`
  - `.seegent-models.json` 中 key 字段留空，真实密钥不再存文件
- **教训**：密钥永远不进仓库，公开私有都不行。环境变量 + `.gitignore` 兜底是唯一安全方式

## 维护注意事项

- 修改 HTML 结构时，同步检查 `DOM_SELECTORS` 表是否完整
- 新增 id 元素后，在 `DOM_SELECTORS` 中加一行，表格按字母排序
- 修改文件夹持久化相关逻辑时，参照"重要原则"中的 4 条规则
- 不要删除 `.seegent-state.json`（服务器端状态，存 tabs/expandedNodes 等）
- `server.py` 中 `Cache-Control: no-store` 仅影响 HTTP 缓存，不影响 IndexedDB
- **页面级 IIFE 用 `setTimeout` + try-catch 包裹**，避免同步执行时操作未就绪 DOM 导致整页崩溃
- **字符串函数名用 `window[name]()` 调用**，不要直接当函数执行
- **`localStorage` 读取必须防御**：值可能不存在、被篡改、对应 DOM 已删除
- **绝对不要在代码、配置、对话中明文输出任何密钥**：包括但不限于 API Key、App ID、App Secret、Token、密码。配置文件中的敏感字段一律用占位符（如 `<你的AppToken>`），让人手动填写。教程示例中也不得出现真实凭证
- **配置文件是用户手动维护的**：`.seegent-models.json`、`.seegent-mcp.json`、`.seegent-cli.json` 中的密钥和 Token 由用户自己填写，工具绝不代填明文

## Phase 1 智能体升级（2026-06-13）

### 服务端：SSE 流式 Agent + 新工具
- `/api/agent` 改为 SSE 逐步推送，事件类型：`iteration`、`tool_call`、`tool_result`、`response`、`error`、`done`
- `/api/chat` 强制流式（stream=true），移除非流式分支
- 新增 2 个 agent 工具：
  - `web_search`：DuckDuckGo HTML 搜索，返回标题/URL/摘要（最多10条）
  - `web_fetch`：抓取网页文本内容，自动去 HTML 标签，截断 8000 字符
- 新增 `_serve_sse()` / `_start_sse()` / `_serve_sse_error()` 辅助方法
- 更新系统提示词：告知模型可使用 web_search/web_fetch
- `/api/agent-config` 返回 11 个内置工具名（list_dir, read_file, write_file, search_files, web_search, web_fetch, semantic_search, file_stats, recent_files, invoke_agent, run_shell）+ MCP 动态工具

### 前端：流式聊天 + Agent 可视化 + Markdown
- `sendChat()` 使用 `fetch` + `ReadableStream`（非 EventSource，支持 POST body）
- 新增 `handleChatStream()`：逐 token 解析 SSE `data:` 行，实时追加 Markdown 渲染
- 新增 `handleAgentStream()`：解析 agent SSE 事件，渲染可折叠工具调用卡片
- 工具卡片结构：`.tool-cards` > `.tool-card`（header 带图标/名称/状态徽章 + body 显示参数和结果），点击切换 `.open` 展开/折叠
- 集成 marked.js（GFM 表格/任务列表）+ highlight.js（GitHub 主题）
- 新增 `renderMarkdown()` 和 `hlAllCodeBlocks()` 辅助函数
- `addChatMessage()` 和 `renderChatMessages()` 支持 Markdown 渲染和 plain 纯文本模式
- 新增完整 Markdown 聊天样式（p/pre/code/table/blockquote/ul/ol/a/img）

## 角色系统（2026-06-13，合并了原「提示词」和「能力/技能」模块）

### 设计理念
- 参照 WorkBuddy"专家"模式，**单一选择**一个角色（替代原来的多技能叠加 + 提示词模板选择）
- 每个角色 = 名称 + 图标 + 分类 + 描述 + 系统提示词
- 选中的角色 prompt 自动注入到 agent 的 system message
- 角色数据持久化到 `.seegent-roles.json`
- 外部应用类技能（飞书/GitHub/Notion）已拆分到 MCP 模块管理

### 预置 12 个角色，分 4 类

**创作**：写代码、作图能力、写文章、翻译能力
**分析**：代码审查、数据分析、文档总结、排错调试
**效率**：会议纪要、提示词工程
**通用**：无角色（默认）、自定义

### 技术实现
- 服务端：`ROLES_FILE` + DEFAULT_ROLES + GET/POST `/api/roles`
- 前端：`loadRoleList()` / `renderRoleList()` / `selectRole()` / `getActiveRolePrompt()`
- 全局状态：`roleConfig` (所有角色 + activeRole)
- `sendChat()` 中调用 `getActiveRolePrompt()` 获取当前角色 prompt 作为 system message
- UI：导航栏"🎭 角色"按钮 → `#role-module` 面板，聊天栏头显示当前角色标签
- 弹窗样式统一化：`.prompt-modal .pm-box` 等泛化选择器

## MCP CLI 集成（2026-06-13）

### 设计理念
- MCP（Model Context Protocol）= 标准化的 AI 工具扩展协议
- 用户配置任意 MCP CLI 命令（飞书、ima 知识库、Notion 等），Seegent 自动管理子进程并转换工具为 Agent 可调用格式
- 工具命名：`mcp_{server_id}_{tool_name}`，避免不同 server 的同名工具冲突

### McpClient 类
- JSON-RPC 2.0 over stdio，`shlex.split` 解析命令
- 子进程 `stdin=PIPE, stdout=PIPE, stderr=DEVNULL`
- 后台线程 `_read_loop()` 持续读取 stdout，通过 `threading.Event` 与 `_send_request()` 同步
- **关键 bug**：使用 `threading.RLock`（重用锁），因为 `_send_request` 持锁后调用 `start()`，`start()` 也获取同一把锁
- **stderr 缓冲死锁**：必须用 DEVNULL 或读取 stderr，否则 npx 下载进度填满缓冲区导致进程阻塞
- **超时**：initialize 60 秒、tools/list 15 秒、tools/call 60 秒（适应 npx 首次下载）

### API 端点
- `GET /api/mcp-status` — 所有启用 server 的运行状态（running/initialized/tool_count）
- `POST /api/mcp-discover` — 按 server_id 或全部发现工具
- `GET/POST /api/mcp` — MCP 配置 CRUD（已有，格式 `{items: {}, enabled: []}`）

### Agent 集成
- `_agent_loop` 启动时调用 `_discover_mcp_tools()` 获取所有工具
- 将 `inputSchema` 转换为 OpenAI function calling 的 `parameters` 字段
- `_exec_tool` 中识别 `mcp_` 前缀 → 调用 `_call_mcp_tool()`
- `_call_mcp_tool()` 提取 `content[].text` 拼接为结果

### 前端 UI
- MCP 面板 = Skills 面板风格：卡片布局 + 状态指示灯 + 工具数 badge
- 启用/禁用开关持久化到 `.seegent-mcp.json`
- 🔍 发现工具按钮 → 发送 POST `/api/mcp-discover` → 展示工具列表
- 新增/编辑/删除弹窗表单（ID、名称、图标、命令、描述、说明）
- Agent 流中 MCP 工具卡片显示 🔌 图标 + 来源 server 标签
- 错误检测：包含 "MCP" 前缀的结果自动标记为失败

## AI 文件能力升级（2026-06-14）

### 架构断裂的修复：文件夹路径绑定
**背景**：侧边栏文件夹是 `FileSystemDirectoryHandle`，浏览器安全策略**不暴露绝对路径**；而后端 agent 工具（list_dir/read_file/search_files）需要绝对路径 `wpath`。两边断裂，导致用户必须手敲 `chat-workspace-input` 才能让 AI 读文件。

**修复**（用户绑一次路径，之后自动用）：
- folder 对象新增 `path` 字段（`{ id, name, handle, path, children, expanded }`，5 处定义）
- `bindFolderPath(folder)`：添加文件夹时弹窗引导，复用 `guessFolderPath` 预填，存到 `folder.path` + `/api/workspaces`
- `restoreFolderPaths()`：init 时从 `/api/workspaces` 按 folder.name 回填 path（异步，不阻塞渲染）
- 右键顶层文件夹 → "🔗 绑定 AI 路径" / "🔁 重绑"
- `resolveAutoWorkspace(session)`：sendChat 时优先级 = 手填输入框 > contextItems 已绑 folder > state.folders 首个已绑
- **添加文件夹后弹窗可跳过**（留空即可），没绑也能用，只是 AI 没工具能力

### search_files 升级为全文检索
- 旧：`if query in content`（子串匹配，全读进内存，返回前 20 条文件名）
- 新：按文本类后缀过滤（`.md/.txt/.py/.json/...`），逐行匹配，返回**行号 + ±2 行上下文段落**（每文件最多 5 段、每段 300 字），最多 50 个文件
- 文件名也参与匹配
- 旧版会把整个文件读进内存做 `.lower()`（大文件慢），新版仍读全文但只在匹配行附近保留上下文

### read_file 支持分段读
- 新增 `offset`（起始字符，默认 0）和 `limit`（字符数，默认 8000）参数
- 超出文件长度时返回"已读完"提示；未读完时返回 `继续读请设 offset=N`
- 解决旧版 8000 字硬截断丢失后续内容的问题

### 多模态消息通道
- 聊天输入框新增 🖼️ 按钮 + 粘贴（clipboard items）+ 拖拽图片
- `pendingImages` 数组 + 预览区（缩略图可删）
- 有图时 user message 的 content 从字符串改为 OpenAI 多模态数组：`[{type:'text',text:...},{type:'image_url',image_url:{url:'data:image/...'}}]`
- **后端零改动**：`_proxy_chat` / `_agent_loop` 原样透传 messages，content 数组直接发给 LLM
- 会话历史也存数组格式，`addChatMessage` / `renderChatMessages` 支持（文本 + 图片缩略图）
- 前提：用户选的模型得支持视觉（GPT-4o / GLM-4V / Claude 3.5 等）

### _exec_tool 重复执行 Bug 修复
- **根因**：agent loop 的 tool_calls 处理里，`_exec_tool(tc)` 被调用两次——第一次（旧 1236 行）结果塞进 SSE 展示，第二次（旧 1249 行）结果塞进 messages 喂 LLM。导致**每个工具执行两遍**（写文件写两次、搜索跑两次）
- **修复**：第一轮循环执行并存到 `tool_results` 列表，第二轮 `zip(tool_calls, tool_results)` 复用，只执行一次

### tool_result SSE 截断放宽
- `result_text[:2000]` → `[:5000]`（仅影响前端展示，不影响喂给 LLM 的完整内容）


## 语义检索 + 文件管理对话化（2026-06-15）

### 设计理念
Agent 可通过自然语言管理文件，不需要知道文件夹结构。"我去年写的关于定价的笔记"→ AI 语义检索找到对应文件。这是 Obsidian（纯关键词匹配）结构上做不到的护城河。

### embedding 架构
- **可配置云 API**：OpenAI / 302.AI / 智谱 / 任意 OpenAI 兼容厂商
- 配置文件 `.seegent-embedding.json`：`{ provider, api_key, base_url, model, dimensions }`
- `_embed_texts(texts)` 工具函数：批量调 `/embeddings` 端点，内存 LRU 缓存（200 条）
- 配置 UI 在「模型管理」模块底部

### SQLite 向量索引
- 数据库：`.seegent-index.db`（WAL 模式，单进程无并发问题）
- 表 `chunks`：workspace + file_path + chunk_index + text + embedding(BLOB) + file_mtime + indexed_at
- 文本分块：500 字一块，重叠 100 字
- 向量用 `numpy float32` 存储（`_vec_to_bytes`/`_bytes_to_vec`）
- 增量更新：对比 `file_mtime`，新增/修改的重索引，删除的清理
- 首次索引通过 SSE 推送进度

### 新增 3 个 agent 工具

| 工具 | 作用 | 关键技术 |
|------|------|---------|
| `semantic_search` | query → embedding → 入 SQLite → numpy 余弦相似 top-k | 需要已建索引 + embedding 已配 |
| `file_stats` | os.walk 统计：文件数/大小/按扩展名分布/按目录分布 | 纯本地，不需索引 |
| `recent_files` | 按 `os.path.getmtime` 排序/过滤 | 纯本地，不需索引 |

### 新增 API 端点

| 端点 | 方法 | 作用 |
|------|------|------|
| `/api/embedding-config` | GET/POST | 读写 embedding 配置 |
| `/api/test-embedding` | POST | 发测试文本 → 返回维度 |
| `/api/reindex` | POST | 触发重建索引，SSE 流式推送进度 |
| `/api/index-status?workspace=` | GET | 索引状态：indexed_files/current_files/stale |

### 前端联动
- 侧边栏：每个已绑 path 的文件夹旁显示索引图标（⚪未索引 / 🔵已索引 / 🟡过期 / ⏳索引中），点击触发 reindex
- 聊天空状态：引导文案提示自然语言查询示例
- 配置 UI：模型管理模块底部「🧭 向量检索配置」，含 provider 选择/API Key/测试连接

### 回溯兼容
- `semantic_search` 在未配 embedding 或未建索引时返回错误提示，不会崩溃
- `file_stats` / `recent_files` 无需索引，随时可用
- 如果 `numpy` 未装，语义检索降级为功能性不可用（但搜索关键词提示降级后路径）

### 成本与隐私
- embedding 调用按量计费：text-embedding-3-small ≈ $0.02/百万 token
- 只在索引建立/更新时调 API（文件内容发云端）；日常查询**不调 API**（query 向量 + 本地点积）
- 敏感文件建议用本地模型（配置留了扩展口）

### 维护注意
- `.seegent-index.db` 和 `.seegent-embedding.json` 已加入 `.gitignore`
- 索引数据库在 `server.py` 同目录，无额外路径依赖
- 修改 `TEXT_EXTS` 常量会同时影响 `search_files` 和索引范围
- `_embed_texts` 的 LRU 缓存上限 200 条，若 embedding 模型变更需手动清缓存（重启 server 或清内存）
- `_index_workspace` 的 `progress_cb` 用于 SSE 推送，不调用不影响核心逻辑
- 索引图标 `onIndexIconClick` 通过 `Reader.read()` 消费 SSE 流，逻辑与 `handleChatStream` 类似但独立
- `_search_semantic` 要求 `numpy`，`server.py` 顶部做了 try/except import，导入失败时禁能降级
- `file_stats` / `recent_files` 的 `os.walk` 遍历使用了 `dirs[:]` 来跳过隐藏目录，与 `search_files` 保持一致

### 维护注意
- folder.path 的权威来源是 `/api/workspaces`（key=路径，value 含 name）；IndexedDB 只存 handle，不存 path
- `bindFolderPath` 弹窗用 `prompt()`（同步），未来若改 UI 注意 `guessFolderPath` 是 async
- 多模态 content 数组格式必须兼容 OpenAI vision API；agent 模式下 system message 仍是字符串，只 user message 可能是数组
- 修改 search_files 的后缀白名单时，同步检查是否需要支持新格式

---

## 当前能力总览（2026-06-30）

### Agent 工具（12 个内置 + MCP 动态）

| 工具 | 说明 |
|------|------|
| `list_dir` | 列出目录结构 |
| `read_file` | 读取文件，支持 offset/limit 分段 |
| `write_file` | 创建/覆写文件 |
| `edit_file` | 精确字符串替换（唯一匹配才执行，否则报错） |
| `search_files` | 全文检索（行号 + 上下文） |
| `web_search` | DuckDuckGo 网页搜索 |
| `web_fetch` | 抓取网页文本内容 |
| `semantic_search` | 语义向量检索（需 embedding 配置 + 索引） |
| `file_stats` | 文件统计（数量/大小/按扩展名分布） |
| `recent_files` | 按修改时间排序/过滤 |
| `invoke_agent` | 调用子 Agent（独立模型+提示词+工具白名单，最大嵌套 3 层） |
| `run_shell` | 执行 shell 命令（安全沙箱 + 超时 + 输出截断） |
| `mcp_*` | MCP 协议扩展（飞书、企业微信、Notion 等） |

### 核心功能

- 文件树管理（浏览/编辑/预览/新建/删除/拖拽排序）
- Markdown 编辑预览 + HTML 实时预览
- 多模型支持（任意 OpenAI 兼容 provider）
- 多模态消息（粘贴/拖拽图片 → vision API）
- 角色系统（12 个预置角色 + 自定义）
- 语义搜索（SQLite 向量索引，增量更新）
- OCR 图片文字索引 + PDF/Word 全文索引
- MCP CLI 集成（飞书、企业微信等）
- 多会话独立管理（chatAbortMap 按 sessionId 隔离）

### 架构备忘（关键数据流）

```
用户添加文件夹 → bindFolderPath（绑路径）
    → folder.path → /api/workspaces 持久化
    → resolveAutoWorkspace(session) → workspace 传给 /api/agent
    → agent 拥有 11 个内置工具 + MCP 动态工具
    → 用户在聊天里说自然语言 → AI 调工具 → 返回结果

用户配置 embedding → /api/embedding-config 存磁盘
    → 点击侧边栏索引图标 → /api/reindex（SSE 进度）
    → _index_workspace(wpath) → SQLite chunks 表
    → _search_semantic(wpath, query) → 余弦相似 top-k
```

## 2026-06-15 新增功能维护注意

### 群聊功能已删除
- `server.py` 中不再有 `GROUP_CHAT_FILE`、`_group_chat()`、`_call_llm()` 及相关端点
- `index.html` 中不再有 `#chat-group-panel`、`chat-tab-bar`、所有 `group*` 函数
- 聊天窗口只有私聊模式，`addToChatContext` 直接操作 solo 会话

### OCR 图片索引
- `IMAGE_EXTS` 常量定义支持的图片格式
- `_OCR_AVAILABLE` 全局标志：需要 `pytesseract` + `PIL` + `tesseract` CLI 三者都可用
- OCR 文本块不建 embedding（`embedding` 字段为 NULL），只能关键词搜索，不能语义搜索
- tesseract 未安装时 OCR 功能静默降级，不影响文本文件索引
- 安装 tesseract：`brew install tesseract tesseract-lang`（需先安装 Homebrew）

### PDF/Word 全文索引
- `TEXT_EXTS` 已扩展 `.pdf`、`.docx`、`.doc`
- `_PDF_AVAILABLE` / `_DOCX_AVAILABLE` 全局标志
- `_read_file_content(filepath)` 根据扩展名自动选择提取方式，替代原来的 `open().read()`
- `.doc` 文件通过 LibreOffice 转 PDF 后提取（需要 `_find_libreoffice()` 能找到 soffice）
- `search_files` 中的文件读取仍用 `open().read()`（第 1722 行），PDF/Word 在那里不会匹配到内容，这是预期行为

### 企业微信 MCP
- `DEFAULT_MCP["wecom"]` 条目，命令使用占位符 `<your_corp_id>` 和 `<your_corp_secret>`
- 用户在 MCP 管理面板编辑卡片填入真实凭证后即可使用
- 教程说明包含完整的配置步骤和常见工具列表

## 看板待办扫描维护（2026-07-06）

### 问题
拾元绑定到看板后，最初显示 55 个待办，深入排查发现大部分是**噪音**：

1. **SOP 自检项混进待办**：拾元里多个 SOP / skills references 文件用 `- [ ] xxx？` 写自检清单，被 `_is_todo_line` 误判为待办
2. **扫描器配额不够**：`_collect_md_files` 默认 200 文件上限 + `os.walk` 顺序遍历，导致 `小红书AI/WorkBuddy小红书/`（演示稿件，154 个 md）占据全部配额，`小红书教育/` 重要文件一个都进不去视野——用户的 `项目进度追踪.md` 真待办从来看不到
3. **文章内行动建议混入**：有些 md 是给读者的实操建议（如"下一次孩子做出你不希望的行为时..."），被识别成待办

### 修复
**第一步：清理 SOP 自检项**（保持拾元文件本身清爽）
- 18 个文件、共 124 行的 `- [ ] xxx` 改成 `- xxx`
- 处理：仅在已知 SOP/白名单文件内操作，绝不动 `项目进度追踪.md` 这种真待办文件
- 用 git 仓库特性保证可回退

**第二步：扫描器优化**（治本）
- `seegent/server.py:2850` 的 `_collect_md_files`：
  - `max_files` 从 200 提到 **2000**
  - 新增 `PRIORITY_DIRS` 业务目录优先级（`小红书教育` / `小红书AI` / `小绿书银发` / `个人感悟` / `展示区` / `违禁词` / `skills` / `临时`）
  - 改为两轮遍历：第一轮按优先级扫，第二轮兜底扫剩余（用 dict 去重）
  - 不在 SKIP_DIRS 之外的隐藏目录也跳过（`not d.startswith('.')`）

### 维护注意
- **新增 SOP 文件可能再次触发**：用户在拾元里新增 SOP 类 `.md`（带 `- [ ] xxx？` 自检清单）时，看板会再次混入噪音。处理方式：
  - 文件级：用白名单脚本批量清掉（保留 `项目进度追踪.md` 类真待办）
  - 启发式：在 `_is_todo_line` 里加"行尾是 `）` 括号补充"判定，自动排除自检项
- **业务目录优先级**：新增顶级业务目录时，记得加到 `PRIORITY_DIRS` 元组里
- **文章内行动建议**：目前没自动化处理，文章里的 `- [ ] xxx` 仍会被识别成待办。若噪音变大，需更复杂的语义判断
- **server 重启**：改了 `seegent/server.py` 必须重启 server 进程才能生效：
  ```bash
  # 找进程
  lsof -i :8765
  # 重启
  kill <PID> && cd ~/Seegent && nohup python3 server.py > /tmp/seegent-server.log 2>&1 &
  ```
- **当前待办现状（2026-07-06）**：32 项 = 14 真待办（`项目进度追踪.md`）+ 15 PPT 制作 SOP（`小红书PPT.md`）+ 3 文章行动建议。接受当前状态，未再清理。
