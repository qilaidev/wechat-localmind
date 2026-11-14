---
name: wechat-localmind
description: macOS & Windows 微信 (WeChat 3.x/4.x) 本地群聊信息只读提取与结构化清洗能力层 (CLI & MCP)。提供 SQLCipher 3/4 离线解密与统一快照、四级实名映射 (备注 > 群昵称 > 微信昵称 > 脱敏wxid)、zstd/XML 引用链还原、增量游标拉取 (--after-id) 及 .dat 异或加密图片自动还原导出。
---

# WeChat-LocalMind 微信本地提取能力层 (`wechat-localmind` CLI & MCP)

本技能是纯粹的 **macOS 与 Windows 微信（WeChat 3.x / 4.x）本地只读数据提取能力底座**，与 WhatsApp 提取层（`whatsapp-localmind`）100% 物理隔离、绝对解耦。仅以离线只读模式解密并聚合微信群聊（`@chatroom`）数据与沙盒内媒体附件，**100% 拦截一对一私聊（`wxid_*`）与公众号（`gh_*`），绝不包含任何消息发送或写回逻辑**，零网络请求、零封号风险。

## 可执行程序调用方式

按以下优先级调用 CLI（内置 macOS 原生 `CommonCrypto` 与 `compression.zstd` 引擎，零外部脆弱依赖）：
1. 全局命令：直接运行 `wechat-localmind <subcommand>`（或 `~/.local/bin/wechat-localmind <subcommand>`）。
2. 技能内置脚本：运行本技能目录下的 `scripts/wechat-localmind <subcommand>`。

---

## 核心子命令与参数契约

### 1. 环境与密钥状态自检 (`doctor`)
检查本地微信账号目录（自动适配 WeChat 4.x `xwechat_files/db_storage` 与 3.x `Message/Msg`）、SQLCipher 密钥验证状态与全局最新游标 `global_latest_msg_id`：
```bash
wechat-localmind doctor --format json
```

### 2. 配置或导入 SQLCipher 数据库密钥 (`key` & `decrypt`)
```bash
# 设置全局 64位 Hex 密钥，或为特定分片库设置密钥 (contact.db:xxxx)
wechat-localmind key --set "<64-hex-key>"

# 从 keys.json 批量导入 WeChat 4.x 各分片库密钥
wechat-localmind key --import ./keys.json

# 扫描内存转储或 LLDB 日志文件，自动通过各分片库 16字节 Salt + HMAC 匹配提取密钥
wechat-localmind key --scan-dump ./wechat_memory.dump

# （可选）配置 WeChat 4.x V2 加密高清 .dat 图片的 16字节 ASCII 或 32位 Hex AES 密钥
wechat-localmind key --dat-key "<16-byte-ascii-or-32-hex>"

# 手动触发全分片离线解密并生成统一只读快照
wechat-localmind decrypt
```

### 3. 获取可访问微信群聊列表与最新游标 (`groups`)
列出所有符合当前黑白名单策略的微信群聊（`@chatroom`）及其 `chat_id`、`latest_msg_id`、过去 24h 消息数、群成员数：
```bash
wechat-localmind groups [--keyword <关键词>] [--include-blocked] --format json
```

### 4. 提取群聊清洗后消息流 (`fetch`)
支持 **全局增量游标模式 (`--after-id`)** 与 **时间窗口模式 (`--since`)**：
```bash
# 按时间窗口提取（默认带前 10 条历史上下文缓冲窗，输出标准 Markdown 对话流并自动折叠连续“收到/OK/红包”噪音）
wechat-localmind fetch --chats ALL_ALLOWED --since 12h --format md

# 按全局单调递增游标无重无漏增量提取（输出结构化 JSON）
wechat-localmind fetch --chats ALL_ALLOWED --after-id 500 --format json

# 提取指定单个或多个微信群聊并强制立即刷新快照
wechat-localmind fetch --chats "123456789@chatroom" --since 24h --fresh --format md
```

### 5. 跨群关键词检索 (`search`)
在所有放行微信群聊中搜索正文（含 zstd 压缩的富文本 XML）、引用消息或文档文件名包含关键词的消息（自动附带前后各 3 条上下文）：
```bash
wechat-localmind search --query "合同" --since 30d --context 3 --format md
```

### 6. 按需导出群聊附件与 `.dat` 图片自动解密 (`export-media`)
将目标 `msg_id` 对应的微信 `.dat` 异或加密图片自动还原为标准 `.jpg`/`.png`/`.gif`/`.webp`，或将语音（`.silk`/`.aud`）、办公文档（`.pdf`/`.xlsx`）安全导出至缓存目录：
```bash
wechat-localmind export-media --msg-ids 108,109 --format json
```

### 7. 身份锚点与群黑白名单配置 (`config`)
```bash
# 查看当前配置
wechat-localmind config show

# 设置本人在群内的显示名与微信号（用于将本人消息标记为 [我(姓名)] 及还原 [@我(姓名)]）
wechat-localmind config set-identity --name "张工" --wxid "wxid_example123456"

# 屏蔽特定生活/亲友群（黑名单模式默认开启）
wechat-localmind config block "88888888@chatroom"
```

### 8. 清理导出媒体缓存与解密快照 (`clean`)
清理导出的媒体缓存或销毁本地已解密与统一 SQLite 快照文件：
```bash
# 仅清理 snapshot/media/ 下导出的媒体文件，保留 SQLite 快照
wechat-localmind clean --media-only --format json

# 同时删除导出的媒体缓存与本地 SQLite 快照文件（默认 --all）
wechat-localmind clean --all --format json
```

### 9. 启动只读 MCP Stdio Server (`mcp-serve`)
通过标准输入输出（Stdio）启动只读 Model Context Protocol (MCP) Server：
```bash
wechat-localmind mcp-serve [--config <config.json>]
```

---

## 标准流与退出码契约

- **`stdout`**：100% 仅输出纯净的 JSON 或 Markdown 结构化结果（可直接通过管道传给 `jq`）。
- **`stderr`**：输出错误详情 JSON（含 `error_code`、`exit_code`、`message`）。
- **POSIX 退出码**：
  - `0` (`EXIT_SUCCESS`)：执行成功
  - `1` (`EXIT_USAGE_ERROR`)：参数或密钥格式错误
  - `2` (`EXIT_ENV_OR_PERM_ERROR`)：微信目录不可读、缺少 SQLCipher 密钥或密钥验证失败
  - `3` (`EXIT_POLICY_DENIED`)：隐私策略拦截（尝试访问一对一私聊 `wxid_*` 或黑名单群聊）
  - `4` (`EXIT_RESOURCE_NOT_FOUND`)：目标 `msg_id` 不存在或附件尚未在客户端下载到本地
  - `5` (`EXIT_INTERNAL_DB_ERROR`)：SQLite / SQLCipher 内部异常
