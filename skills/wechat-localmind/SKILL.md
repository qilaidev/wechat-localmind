---
name: wechat-localmind
description: macOS & Windows 微信 (WeChat 3.x/4.x WCDB) 全量本地聊天信息只读提取与结构化清洗能力层 (CLI & MCP)。支持私聊 (wxid_*/微信号别名/filehelper)、群聊 (@chatroom/@im.chatroom) 与公众号/服务号 (gh_*/notifymessage)，提供 SQLCipher 3/4 (+ .db-wal) 离线解密、增量统一快照、四级实名映射 (备注 > 群昵称 > 微信昵称 > 脱敏wxid)、zstd/XML 引用链还原、FTS5 三元组极速检索、增量游标拉取 (--after-id)、WeChat 4.x V1/V2 .dat 图片解密导出，以及可选 chat_scope 与黑白名单隐私策略。
---

# WeChat-LocalMind 微信本地全量提取能力层 (`wechat-localmind` CLI & MCP v2.0.0)

本技能是纯粹的 **macOS 与 Windows 微信（WeChat 3.x / 4.x WCDB）全量本地只读数据提取能力底座**，与 WhatsApp 提取层（`whatsapp-localmind`）100% 物理隔离、绝对解耦。以离线只读模式解密并聚合全账号微信会话记录与沙盒内媒体附件，**绝不包含任何消息发送或写回逻辑**，零网络请求、零封号风险：

- **默认全账号会话支持 (`chat_scope = "all"`)**：
  - **一对一私聊 (`direct`)**：`wxid_*` 好友、自定义微信号 `alias` 别名直查、企业微信联系人 (`@openim`)、`filehelper`（文件传输助手）。
  - **微信群聊 (`group`)**：普通微信群 (`@chatroom`) 与企微互通群 (`@im.chatroom`)。
  - **公众号与服务通知 (`official`)**：`gh_*` 公众号/服务号、`notifymessage`、`weixin`（微信团队）等系统服务会话。
- **按需可配置隐私范围与过滤策略**：支持通过 `config set-scope <all|groups|direct|official>`（或环境变量 `WECHAT_LOCALMIND_CHAT_SCOPE`）限定可访问会话类型，并配合 `blacklist`（默认）/ `whitelist` 策略屏蔽或放行特定 `chat_id`（越权访问返回退出码 `3`，`CHAT_SCOPE_DENIED` 或 `CHAT_BLOCKED_BY_POLICY`）。

## 可执行程序调用方式

按以下优先级调用 CLI（内置 macOS 原生 `CommonCrypto` 与 `compression.zstd` / `libzstd` 引擎，零外部脆弱依赖）：
1. 全局命令：直接运行 `wechat-localmind <subcommand>`（或 `~/.local/bin/wechat-localmind <subcommand>`）。
2. 技能内置脚本：运行本技能目录下的 `scripts/wechat-localmind <subcommand>`。

---

## 核心子命令与参数契约（12 个 CLI 子命令）

### 1. 环境与密钥状态自检 (`doctor`)
检查本地微信账号目录（自动适配 WeChat 4.x `xwechat_files/db_storage` 与 3.x `Message/Msg`）、SQLCipher 分片库密钥验证状态、`zstd` 解压后端与全局最新游标 `global_latest_msg_id`：
```bash
wechat-localmind doctor --format json
```

### 2. 配置或导入 SQLCipher 与 `.dat` 密钥 (`key` & `decrypt`)
```bash
# 设置全局 64位 Hex 密钥，或为特定分片库设置密钥 (contact.db:xxxx)
wechat-localmind key --set "<64-hex-key>"
wechat-localmind key --set "contact.db:<64-hex-key>"

# 从 keys.json 批量导入 WeChat 4.x 各分片库密钥
wechat-localmind key --import ./keys.json

# 扫描内存转储或 LLDB 日志文件，自动通过各分片库 16字节 Salt + Page 1 HMAC 匹配提取密钥
wechat-localmind key --scan-dump ./wechat_memory.dump --verify

# （可选）配置 WeChat 4.x V1/V2 AES-128-ECB+XOR 加密高清 .dat 图片的 16字节 ASCII 或 32位 Hex AES 密钥
wechat-localmind key --dat-key "<16-byte-ascii-or-32-hex>"

# 手动触发全分片离线解密（含 .db-wal 日志合并）并增量/全量生成统一只读快照，或解密单库
wechat-localmind decrypt
wechat-localmind decrypt --src ./message_0.db --dst ./message_0_dec.sqlite --key "<64-hex-key>"
```

### 3. 获取可访问全量会话列表 (`chats` / `list-chats`) 与群聊兼容列表 (`groups` / `list-groups`)
列出所有符合当前 `chat_scope` 与黑白名单策略的微信会话（私聊、群聊、公众号）及其 `chat_id`、`chat_type`、`latest_msg_id`、过去 24h 消息数、参与人数：
```bash
# 列出全量可访问会话（默认 --type all）
wechat-localmind chats --format json

# 按会话类型过滤：all | direct | group | official
wechat-localmind chats --type direct --format md
wechat-localmind chats --type official --keyword "科技" --format json

# 包含被策略屏蔽或超出 scope 的会话
wechat-localmind chats --include-blocked --format json

# 向后兼容的仅群聊列表别名（等价于 chats --type group）
wechat-localmind groups [--keyword <关键词>] [--include-blocked] --format json
```

### 4. 提取清洗后消息流 (`fetch`)
支持 **全局增量游标模式 (`--after-id`)** 与 **时间窗口模式 (`--since`)**，支持指定单个/多个 `chat_id`（含微信号 `alias` 别名自动反查）或内置通配目标 `ALL_ALLOWED` / `ALL_DIRECT` / `ALL_GROUPS` / `ALL_OFFICIAL`：
```bash
# 按时间窗口提取全账号放行会话（默认带前 10 条历史上下文缓冲窗，输出 Markdown 并折叠短确认噪音）
wechat-localmind fetch --chats ALL_ALLOWED --since 12h --format md

# 仅提取私聊 / 群聊 / 公众号
wechat-localmind fetch --chats ALL_DIRECT --since 24h --format md
wechat-localmind fetch --chats ALL_GROUPS --after-id 500 --format json
wechat-localmind fetch --chats ALL_OFFICIAL --since 7d --format md

# 按微信号别名、wxid、filehelper 或群 ID 精确提取并强制刷新快照
wechat-localmind fetch --chats "wxid_alice,alice88,filehelper,10001@chatroom" --since 24h --fresh --format md

# 组合 --type 过滤并禁用噪音折叠，输出到文件
wechat-localmind fetch --chats ALL_ALLOWED --type direct --since 24h --no-fold --limit 200 -o ./dm_digest.md
```

### 5. 全量或按类型关键词检索 (`search`)
使用内置 SQLite **FTS5 trigram 索引 + `instr` 子串回退** 在所有放行会话（或指定 `--chats` / `--type`）中搜索正文（含 WeChat 4.x `zstd` 压缩 XML 与合并转发聊天记录 `<recorditem>`、语音转文字 `<voicetrans>`）及附件文件名（自动附带前后各 3 条上下文）：
```bash
# 全账号放行会话跨聊搜索
wechat-localmind search --query "报价单" --since 30d --context 3 --format md

# 仅在私聊或公众号中搜索
wechat-localmind search --query "银行卡" --type direct --format json
wechat-localmind search --query "端侧AI芯片" --type official --chats ALL_OFFICIAL --format md
```

### 6. 按需导出聊天附件与 `.dat` 图片自动解密 (`export-media`)
将目标 `msg_id` 对应的微信 3.x XOR 加密 `.dat` 与 **WeChat 4.x V1/V2 (`\x07\x08V1`/`V2`) AES-128-ECB + XOR 混合加密 `.dat`** 自动还原为标准 `.jpg`/`.png`/`.gif`/`.webp`/`.bmp`/`.heic`，或将语音（`.silk`/`.aud`）、办公文档（`.pdf`/`.xlsx`）安全导出至缓存目录（支持私聊、群聊与公众号附件）：
```bash
wechat-localmind export-media --msg-ids 108,109 --format json
wechat-localmind export-media --msg-ids 15 --dest-dir ./exported_files --format json
```

### 7. 身份锚点、会话范围 (`set-scope`) 与黑白名单配置 (`config`)
```bash
# 查看当前配置
wechat-localmind config show

# 设置本人在聊天内的显示名与微信号/别名（用于将本人消息标记为 [我(姓名)] 及还原 [@我(姓名)]）
wechat-localmind config set-identity --name "张工" --wxid "wxid_example123456" --alias "zhang_eng"

# 设置可访问会话范围：all (默认全量) | groups (仅群聊) | direct (仅私聊) | official (仅公众号)
wechat-localmind config set-scope all
wechat-localmind config set-scope groups

# 黑名单模式（默认）下屏蔽任意私聊 wxid、群聊 @chatroom 或公众号 gh_*
wechat-localmind config block "wxid_private_secret,88888888@chatroom"
wechat-localmind config unblock "wxid_private_secret"

# 切换白名单模式并放行指定会话
wechat-localmind config set-mode whitelist
wechat-localmind config allow "10001@chatroom,wxid_alice,filehelper"
wechat-localmind config disallow "wxid_alice"
```

### 8. 清理导出媒体缓存与解密快照 (`clean`)
```bash
# 仅清理 snapshot/media/ 下导出的媒体文件，保留 SQLite 快照与持久化游标水位线
wechat-localmind clean --media-only --format json

# 同时删除导出的媒体缓存与本地 SQLite 快照文件（默认 --all）
wechat-localmind clean --all --format json
```

### 9. 启动只读 MCP Stdio Server (`mcp-serve`) 与安装 (`install`)
```bash
# 通过标准输入输出启动只读 JSON-RPC 2.0 MCP Server（暴露 5 个只读工具，带 readOnlyHint: true）
wechat-localmind mcp-serve [--config <config.json>]

# 挂载 CLI 软链到 ~/.local/bin 并同步 SKILL.md 技能包
wechat-localmind install --bin-dir ~/.local/bin --skill-dir ~/.gemini/config/skills
```

---

## MCP Stdio Server 暴露的 5 个只读工具

所有工具均声明 `annotations.readOnlyHint = true`、`destructiveHint = false`：

1. `wechat_list_chats`：列出可访问全量会话（`chat_type`: `"all"` | `"direct"` | `"group"` | `"official"`、`keyword`、`include_blocked`、`format`、`fresh`）。
2. `wechat_list_groups`：向后兼容的仅群聊列表工具（`keyword`、`include_blocked`、`format`、`fresh`）。
3. `wechat_fetch_messages`：按 `chat_ids`（具体 ID / 别名或 `["ALL_ALLOWED"]` / `["ALL_DIRECT"]` / `["ALL_GROUPS"]` / `["ALL_OFFICIAL"]`）、`chat_type`、`after_msg_id` 或 `since` 拉取清洗后消息流。
4. `wechat_search_messages`：全量或按 `chat_type` / `chat_ids` 搜索消息及 `zstd` XML 附件标题并附带上下文。
5. `wechat_export_media`：按 `msg_ids` 安全导出并自动解密 `.dat` 图片（含 3.x XOR 与 4.x V1/V2 AES+XOR）与文档附件。

---

## 环境变量、标准流与 POSIX 退出码契约

- **环境变量**：
  - `WECHAT_LOCALMIND_DATA_DIR`：自定义微信数据根目录（`xwechat_files` 或 `WeChat Files`）
  - `WECHAT_LOCALMIND_ACCOUNT_DIR`：显式指定单个微信账号目录
  - `WECHAT_LOCALMIND_SNAPSHOT_DIR`：自定义统一快照与导出目录
  - `WECHAT_LOCALMIND_KEY`：全局 64 位 Hex SQLCipher 密钥
  - `WECHAT_LOCALMIND_DAT_KEY`：WeChat 4.x V1/V2 `.dat` 图片解密的 16 字节 ASCII / 32 位 Hex AES 密钥
  - `WECHAT_LOCALMIND_CHAT_SCOPE`：会话访问范围覆盖（`all` | `groups` | `direct` | `official`）
- **`stdout`**：100% 仅输出纯净的 JSON 或 Markdown 结构化结果（可直接通过管道传给 `jq`）。
- **`stderr`**：输出错误详情 JSON（含 `error_code`、`exit_code`、`message`）。
- **POSIX 退出码**：
  - `0` (`EXIT_SUCCESS`)：执行成功
  - `1` (`EXIT_USAGE_ERROR`)：参数、时间表达式或密钥格式错误
  - `2` (`EXIT_ENV_OR_PERM_ERROR`)：微信目录不可读、缺少 SQLCipher / WeChat 4.x V2 `.dat` 密钥（`WECHAT4_V2_DAT_KEY_REQUIRED`）或密钥验证失败
  - `3` (`EXIT_POLICY_DENIED`)：隐私策略拦截（超出 `chat_scope` 返回 `CHAT_SCOPE_DENIED`，命中黑名单/未入白名单返回 `CHAT_BLOCKED_BY_POLICY`，路径越界返回 `INVALID_SANDBOX_PATH`）
  - `4` (`EXIT_RESOURCE_NOT_FOUND`)：目标会话/`msg_id` 不存在或附件尚未在客户端下载到本地
  - `5` (`EXIT_INTERNAL_DB_ERROR`)：SQLite / SQLCipher 内部异常
