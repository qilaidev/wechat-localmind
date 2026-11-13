# WeChat LocalMind (`wechat-localmind`)

**WeChat LocalMind（`wechat-localmind`）** 是一个本地优先、严格只读的 CLI 与 JSON-RPC 2.0 MCP Stdio Server，专为 Claude Desktop、Cursor、Google Antigravity 等 AI Agent 提取并清洗 **微信 3.x / 4.x（WCDB）群聊（`@chatroom`）** 对话与沙盒附件。它仅在独立离线快照目录解密 SQLCipher 分片库（macOS 原生调用 `CommonCrypto` 与 `zstd`，零第三方 pip 依赖），在架构层物理拦截一对一私聊（`wxid_*`）与公众号（`gh_*`），自动还原 `.dat` 异或加密图片，且零网络请求、零封号风险。

[![CI](https://github.com/qilaidev/wechat-localmind/actions/workflows/ci.yml/badge.svg)](https://github.com/qilaidev/wechat-localmind/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Zero Deps on macOS](https://img.shields.io/badge/Dependencies-Zero%20Pip%20Deps%20(macOS)-brightgreen.svg)](./pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20DBs-lightgrey.svg)](#安装与快速上手)
[![WeChat](https://img.shields.io/badge/WeChat-3.x%20%2F%204.x%20WCDB-07C160.svg)](#六大核心架构支柱)
[![MCP Server](https://img.shields.io/badge/MCP-Stdio%20Read--Only-6E56CF.svg)](#mcp-stdio-server-接入与工具契约)

**简体中文** | **[English](./README.en.md)** | **[llms.txt](./llms.txt)** | **[codemeta.json](./codemeta.json)** | **[SKILL.md](./skills/wechat-localmind/SKILL.md)** | **[SECURITY](./SECURITY.md)** | **[CONTRIBUTING](./CONTRIBUTING.md)** | **[CHANGELOG](./CHANGELOG.md)** | **[LICENSE](./LICENSE)**

> **`*-localmind` 本地即时通讯只读提取能力层家族**
>
> 本仓库与姊妹仓 [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind)（macOS WhatsApp 本地群聊提取能力层）同属 `qilaidev` 下的 **`*-localmind` 本地聊天提取家族**，两仓严格遵循统一的五项架构哲学：
> **100% 离线只读快照 + 物理拦截一对一私聊 + 四级实名还原 + 单调增量游标 + CLI & MCP 双协议交付**。

---

## 核心事实卡（Entity Snapshot）

| 属性 | 规格与事实 |
| :--- | :--- |
| **项目标识** | `wechat-localmind`（Python 包名 `wx_localmind`，当前版本 `v1.0.0`） |
| **开源协议** | [MIT License](./LICENSE) |
| **运行环境与依赖** | Python `>= 3.10`；**macOS 零第三方 pip 依赖**（直接调用系统原生 `CommonCrypto`、`libzstd.dylib` / Python 3.14 `compression.zstd` 与 `sqlite3`）；Linux / 非 macOS 环境支持 `cryptography` 或 `PyCryptodome` 提供 AES-256-CBC 后端 |
| **支持微信版本与库布局** | macOS & Windows **WeChat 4.x WCDB**（`reserve=80`、`xwechat_files/db_storage/message_*.db`、`contact.db`、`zstd` 压缩载荷）与 **WeChat 3.x**（`reserve=48`、`msg_*.db` / `MSG*.db` + `MicroMsg.db`） |
| **隐私边界** | **仅限群聊（`@chatroom`）**；在分片提取、SQL 查询与媒体导出三层物理拦截 `wxid_*` 一对一私聊与 `gh_*` 公众号（触发直接返回退出码 `3`） |
| **交付协议** | 11 个 POSIX CLI 子命令（含 `list-groups` 别名）+ 4 个只读 JSON-RPC 2.0 MCP Stdio 工具 + `SKILL.md` 智能体技能包 |
| **默认快照目录** | `~/Library/Application Support/WeChat-LocalMind/snapshot`（可通过 `WECHAT_LOCALMIND_SNAPSHOT_DIR` 自定义） |
| **维护方与生态归属** | [`qilaidev`](https://github.com/qilaidev) · [qilai 独立工程工作室 (`qilaidav.com`)](https://qilaidav.com) |

---

## 定位边界：它是什么 / 它不是什么

| 维度 | 它是什么（What it is） | 它不是什么（What it is NOT） |
| :--- | :--- | :--- |
| **运行模式** | **100% 离线只读快照**：仅在本地独立快照目录解密与聚合（`?mode=ro` 打开），零网络请求 | **不是**微信外挂、运行时 Hook 注入器或协议逆向客户端 |
| **访问范围** | **仅限群聊（`@chatroom`）**：支持黑白名单策略与单调增量游标拉取 | **绝对禁止**读取一对一私聊（`wxid_*`）与公众号（`gh_*`） |
| **写入能力** | **纯只读（Read-Only）**：绝不修改微信原库，绝不包含任何发送消息 API | **不是**自动回复机器人、群发工具或桌面 RPA 控制端 |
| **密钥边界** | **用户自持密钥离线验证**：支持 `--set`、`--import`、`--scan-dump` 匹配本地 16B Salt + HMAC | **不是**绕过加密或远程窃密工具（无有效密钥无法解密） |
| **交付形态** | **CLI & MCP Stdio Server**：输出纯净 JSON / Markdown 供 AI Agent 消费 | **不是**独立桌面 GUI 客户端或云端 SaaS 托管服务 |

---

## 适用场景与方案对比（Alternatives Comparison）

### 典型适用场景

| 使用场景 | 推荐入口 / 命令 | 核心收益 |
| :--- | :--- | :--- |
| **AI Agent 每日群聊摘要与待办提取** | MCP `wechat_fetch_messages` 或 `fetch --chats ALL_ALLOWED --since 24h` | 自动完成四级实名、150 字引用链还原，并折叠「收到 / OK」噪音 |
| **增量式群消息同步管道（无重无漏）** | `fetch --chats ALL_ALLOWED --after-id <msg_id> --format json` | 基于单调递增游标拉取并附带前 10 条上下文缓冲窗，越界自动回退 24h |
| **跨群检索合同 / 报价单 / 历史决策** | `search --query "报价单" --context 3` 或 MCP `wechat_search_messages` | 支持检索 WeChat 4.x `zstd` 压缩 XML 富文本与附件标题并附带上下文 |
| **批量归档群图片与办公文档** | `export-media --msg-ids 15,16` 或 MCP `wechat_export_media` | 自动推导出单字节异或密钥，将 `.dat` 图片还原为标准 `.jpg`/`.png` 等格式 |

### 同类方案架构对比

> 基于真实架构与能力边界对比不同路径，不虚构跑分与性能数字。

| 对比维度 | **WeChat LocalMind** | 通用 `sqlcipher` CLI + 手写 SQL | 桌面 GUI 微信导出工具 | 微信 Hook / Web 协议机器人 |
| :--- | :---: | :---: | :---: | :---: |
| **账号封禁与运行时干扰风险** | 零风险（纯离线快照、无进程注入、无网络请求） | 零风险（纯离线手工读取） | 视具体工具而定（部分需读取内存） | 高风险（依赖注入或非官方协议心跳） |
| **一对一私聊物理隔离** | 原生三层硬隔离（仅 `@chatroom`，私聊直接报错 `3`） | 需每次人工手写 SQL 过滤，易漏 | 通常全量导出个人私聊与群聊 | 无隔离，默认暴露全部联系人会话 |
| **WeChat 4.x WCDB（`reserve=80` + `zstd`）** | 原生聚合 `message_*.db` + `zstd` 内存解压 | 需手动逐片设置 pragma，且需自行解压 `zstd` BLOB | 部分新版本支持，常依赖重型 GUI | 不适用 |
| **macOS 零 pip 依赖原生解密** | 直接通过 `ctypes` 调用 `CommonCrypto` + `libzstd` | 需编译安装外部 `sqlcipher` 二进制 | 需安装完整桌面应用与捆绑运行时 | 需大量第三方协议/GUI 依赖 |
| **`.dat` 异或加密图片自动还原** | 内置魔数头推导与向量化异或还原 | 不支持，需额外脚本处理 | 通常支持导出 | 依赖实时下载 |
| **AI Agent 原生交付（MCP + JSON/MD + 游标）** | 原生 4 个只读 MCP Tool + 11 个 CLI 子命令 + `SKILL.md` | 仅原始 SQL 行输出 | 面向人工查阅（HTML/PDF/CSV） | 面向事件回调编程，无标准只读增量游标 |
| **主动发送消息 / 自动回复** | 不支持（架构层严格只读） | 不支持 | 不支持 | 支持 |

---

## 合规与隐私安全声明

- **合法授权用途**：本工具严格仅用于**用户本人设备上的个人本地数据备份与经授权的离线研究**。使用本工具处理群聊数据时，请遵守所在地法律法规及群聊参与方的隐私约定。
- **100% 离线只读快照**：所有解密与聚合操作均在独立快照目录（默认 `~/Library/Application Support/WeChat-LocalMind/snapshot`，可通过 `WECHAT_LOCALMIND_SNAPSHOT_DIR` 覆盖）中完成，**绝不修改微信原始数据库、绝不注入或干扰微信客户端运行态、绝不发起任何网络请求、绝不发送任何消息**。
- **三重 `@chatroom` 物理隔离**：在分片提取层（`snapshot.py`）、SQL 查询层（`db.py` 强制 `chat_id LIKE '%@chatroom'`）与附件导出/策略层（`config.py` / `media.py`）三重物理拦截一对一私聊（`wxid_*`）与公众号（`gh_*`），越权访问直接返回 `EXIT_POLICY_DENIED`（退出码 `3`）。

---

## 六大核心架构支柱

1. **原生 `CommonCrypto` SQLCipher 3 / 4 离线解密引擎**
   - macOS 下优先调用系统原生 `CommonCrypto`（`/usr/lib/libSystem.B.dylib` `CCCrypt`）与标准库 `hashlib.pbkdf2_hmac` 实现 AES-256-CBC 逐页验证与解密（Linux / 非 macOS 自动兼容 `cryptography` 或 `PyCryptodome`），全面适配 WeChat 4.x WCDB（`reserve=80`）与 WeChat 3.x（`reserve=48`）。
2. **WeChat 4.x `zstd` 内存级解压与多片库自动聚合**
   - 依次探测 Python 3.14+ 内置 `compression.zstd`、`zstandard`、macOS 原生 `libzstd.dylib`（`ctypes`）及 `zstd` 命令实现内存级解压；自动跨 `message_0.db ~ message_N.db` 解析 `Msg_<md5>` / `Chat_<md5>` / `MSG` 表与分片内 `Name2Id` 映射，按时间序生成全局单调递增游标 `msg_id`。
3. **三重 `@chatroom` 隐私隔离护栏**
   - 提取、查询、导出三层硬编码校验 `chat_id LIKE '%@chatroom'`，物理隔离 `wxid_*` 私聊与 `gh_*` 公众号，支持群聊黑/白名单精细化控制与个人身份锚点标记。
4. **四级实名还原与 `@wxid_` 自动转实名**
   - 自动关联 `contact.db` 与 `chatroom_member`，按 **通讯录备注 (`remark`) > 群内昵称 (`group_nickname`) > 微信昵称 (`nick_name`) > 脱敏 wxid (`微信群友(xxxx)`)** 四级优先级还原发言人身份，并将正文中残留的 `@wxid_xxx` 与 `\u2005` 特殊空格自动替换为 `@实名` 或 `[@我(实名)]`。
5. **XML / Protobuf 富媒体深度解析与 150 字引用链还原**
   - 内置纯 Python 零依赖 Protobuf Wire 解析器与 XML 解析器，精准还原 `<appmsg type="57"><refermsg>` **150 字父消息引用链**、文件卡片（`type=6`）、网页链接（`type=5`）、小程序（`type=33/36`）、视频号/直播卡片（`type=51/63`）、合并转发（`type=19`）与群公告（`type=87`）；自动剔除撤回消息（`revokemsg`）、拍一拍与系统通知，并自动折叠连续「收到 / OK / +1」短确认噪音。
6. **微信 `.dat` 异或加密图片向量化还原**
   - `export-media` 内置自适应 Magic Header 异或密钥推导器与向量化大整数位运算引擎，导出微信 `.dat` 加密图片时**自动解密还原为标准 `.jpg` / `.png` / `.gif` / `.webp` / `.bmp` / `.heic`**。

---

## 安装与快速上手

前置要求：Python `>= 3.10`（**macOS 零第三方 pip 依赖**；若在 Linux 或未内置 `CommonCrypto` 的系统上运行，安装 `pip install cryptography` 或 `pycryptodome` 即可提供 AES-256-CBC 解密后端）。

```bash
git clone https://github.com/qilaidev/wechat-localmind.git
cd wechat-localmind

# 一键将 wechat-localmind 软链挂载到 ~/.local/bin 并同步 AI Skill 技能包
./bin/wechat-localmind install --bin-dir ~/.local/bin --skill-dir ~/.gemini/config/skills
```

### 30 秒快速体验

```bash
# 1. 检查本地微信 3.x/4.x 数据库与密钥状态
wechat-localmind doctor --format md

# 2. 导入或设置 SQLCipher 密钥后，拉取最近 24 小时群聊记录
wechat-localmind fetch --chats ALL_ALLOWED --since 24h --format md
```

---

## CLI 子命令速查

| 子命令 | 核心用途 | 示例 |
| :--- | :--- | :--- |
| `doctor` | 环境、WeChat 3.x/4.x 数据库发现与密钥健康检查 | `wechat-localmind doctor --format md` |
| `key` | 设置/导入 SQLCipher 密钥，或从内存转储扫描密钥 | `wechat-localmind key --import ./keys.json` |
| `decrypt` | 离线解密单库（`--src`/`--dst`）或强制刷新统一只读快照 | `wechat-localmind decrypt` |
| `config` | 配置本人身份锚点（`set-identity`）与群黑白名单 | `wechat-localmind config block "88888888@chatroom"` |
| `groups` / `list-groups` | 列出可访问群聊（`@chatroom`）及 `latest_msg_id` 游标 | `wechat-localmind groups --format json` |
| `fetch` | 按游标（`--after-id`）或时间窗（`--since`）拉取清洗后消息 | `wechat-localmind fetch --chats ALL_ALLOWED --after-id 200 --format json` |
| `search` | 跨允许群聊检索文本与 `zstd` 富媒体（含上下文） | `wechat-localmind search --query "报价单" --context 3 --format md` |
| `export-media` | 导出群附件并自动解密 `.dat` 异或图片 | `wechat-localmind export-media --msg-ids 15,16 --format json` |
| `clean` | 清理导出媒体缓存（`--media-only`）或销毁本地解密快照（`--all`） | `wechat-localmind clean --media-only` |
| `mcp-serve` | 启动只读 JSON-RPC 2.0 MCP Stdio Server | `wechat-localmind mcp-serve` |
| `install` | 挂载 CLI 软链到 `~/.local/bin` 并可选安装 `SKILL.md` | `./bin/wechat-localmind install` |

<details>
<summary>展开查看完整 CLI 参数示例</summary>

```bash
# 配置 SQLCipher 密钥（支持全局密钥、分片密钥、内存转储扫描与 HMAC 校验）
wechat-localmind key --set "<64-hex-key>"
wechat-localmind key --set "contact.db:<64-hex-key>"
wechat-localmind key --scan-dump ./wechat_memory.dump --verify

# 设置本人身份锚点（将本人消息标记为 [我(姓名)]）
wechat-localmind config set-identity --name "张工" --wxid "wxid_example123456"

# 切换白名单模式并放行指定群聊
wechat-localmind config set-mode whitelist
wechat-localmind config allow "88888888@chatroom"

# 按关键词筛选群聊（包含已屏蔽群）
wechat-localmind groups --keyword "项目" --include-blocked --format json

# 按时间窗拉取、禁用噪音折叠并输出到文件
wechat-localmind fetch --chats ALL_ALLOWED --since 12h --no-fold --limit 200 -o ./digest.md
```

</details>

---

## MCP Stdio Server 接入与工具契约

在 Claude Desktop、Cursor、Google Antigravity 或 Codex 的 MCP 配置中添加：

```json
{
  "mcpServers": {
    "wechat-localmind": {
      "command": "wechat-localmind",
      "args": ["mcp-serve"]
    }
  }
}
```

### 暴露的 4 个只读 MCP Tools

| Tool 名称 | 必填参数 | 可选参数与默认值 | 能力说明 |
| :--- | :--- | :--- | :--- |
| `wechat_list_groups` | 无 | `keyword` (`string`)、`include_blocked` (`false`)、`format` (`"json"` \| `"markdown"`)、`fresh` (`false`) | 列出可访问微信群聊（`@chatroom`）、成员数、24h 消息量及最新单调游标 `latest_msg_id` |
| `wechat_fetch_messages` | `chat_ids` (`string[]`，群 ID 数组或 `["ALL_ALLOWED"]`) | `after_msg_id` (`int`)、`since` (`"24h"`)、`until` (`string`)、`context_buffer` (`10`)、`limit_per_chat` (`500`，上限 `3000`)、`fold_noise` (`true`)、`format` (`"markdown"` \| `"json"`)、`fresh` (`false`) | 按全局单调游标或时间窗提取清洗后的群聊记录，自动完成四级实名、`@提及` 与 150 字 `<refermsg>` 引用链还原并剔除撤回消息 |
| `wechat_search_messages` | `query` (`string`) | `chat_ids` (`string[]`)、`since` (`string`)、`context` (`3`)、`limit` (`50`)、`format` (`"markdown"` \| `"json"`) | 跨允许群聊检索文本与附件标题（含 `zstd` 压缩 XML 消息），返回匹配项及前后各 N 条语境上下文 |
| `wechat_export_media` | `msg_ids` (`int[]`) | 无 | 按 `msg_id` 安全导出群内附件至 `snapshot/media` 缓存目录，自动将 `.dat` 异或加密图片解密为 `.jpg`/`.png` 并返回绝对路径 |

---

## 环境变量与 POSIX 退出码契约

- `WECHAT_LOCALMIND_DATA_DIR`：自定义微信数据根目录（如 `xwechat_files` 或 `WeChat Files`）
- `WECHAT_LOCALMIND_ACCOUNT_DIR`：显式指定单个登录账号目录
- `WECHAT_LOCALMIND_SNAPSHOT_DIR`：自定义统一只读快照与附件导出目录
- `WECHAT_LOCALMIND_KEY`：全局 64 位 Hex SQLCipher 密钥

| 退出码 | 常量名 | 含义 |
| :--- | :--- | :--- |
| `0` | `EXIT_SUCCESS` | 执行成功 |
| `1` | `EXIT_USAGE_ERROR` | 命令行参数、时间格式或 Hex 密钥格式错误 |
| `2` | `EXIT_ENV_OR_PERM_ERROR` | 微信目录不可读、缺少 SQLCipher 密钥或密钥不匹配 |
| `3` | `EXIT_POLICY_DENIED` | 隐私策略拦截（尝试访问一对一私聊 `wxid_*`、公众号 `gh_*` 或黑名单屏蔽群聊） |
| `4` | `EXIT_RESOURCE_NOT_FOUND` | 目标群聊/`msg_id` 不存在，或附件尚未在微信客户端下载到本地 |
| `5` | `EXIT_INTERNAL_DB_ERROR` | SQLite / SQLCipher 内部解析异常 |

---

## 常见问题（FAQ）

<details>
<summary><strong>1. 支持哪些微信版本？WeChat 3.x 和 4.x（WCDB）有什么区别？</strong></summary>

同时支持 macOS 与 Windows 数据布局的 **WeChat 3.x**（`reserve=48`，如 `msg_*.db` / `MSG*.db` + `MicroMsg.db`）和 **WeChat 4.x WCDB**（`reserve=80`，如 `xwechat_files/db_storage/message_*.db` + `contact.db`）。工具会自动探测数据目录格式、分片结构与 `zstd` 压缩载荷并统一聚合为只读快照。

</details>

<details>
<summary><strong>2. 使用本工具会导致微信封号或影响微信正常运行吗？</strong></summary>

**零封号风险。** `wechat-localmind` 100% 在离线独立快照目录中工作，绝不向微信客户端进程注入代码、绝不修改原始数据库文件、绝不发起任何网络请求，也绝不包含任何自动发送消息或模拟操作逻辑。

</details>

<details>
<summary><strong>3. 如何确保一对一私聊（<code>wxid_*</code>）不会被 AI Agent 误读？</strong></summary>

系统在**分片提取层、SQL 查询层与策略导出层**实施三重物理拦截：所有查询强制附带 `chat_id LIKE '%@chatroom'`，一旦检测到 `wxid_*` 私聊或 `gh_*` 公众号标识，立即拒绝执行并返回 `EXIT_POLICY_DENIED`（退出码 `3`）。

</details>

<details>
<summary><strong>4. 为什么能做到 macOS 零第三方 pip 依赖？非 macOS 环境如何运行？</strong></summary>

在 macOS 上直接通过 `ctypes` 调用系统原生 `CommonCrypto`（`CCCrypt`）与 `libzstd.dylib`（Python 3.14+ 优先使用内置 `compression.zstd`），配合标准库 `hashlib.pbkdf2_hmac` 实现 AES-256-CBC 逐页验证与解密，无需安装 `pysqlcipher3`。在 Linux 等无 `CommonCrypto` 的环境中，只需安装 `cryptography` 或 `PyCryptodome` 即可提供 AES-256-CBC 后端。

</details>

<details>
<summary><strong>5. 微信加密的 <code>.dat</code> 图片如何还原？</strong></summary>

运行 `wechat-localmind export-media --msg-ids <id>` 或调用 MCP 工具 `wechat_export_media` 时，内置的自适应 Magic Header 异或密钥推导器会自动识别文件头特征，通过向量化大整数位运算将 `.dat` 还原为标准 `.jpg` / `.png` / `.gif` / `.webp` / `.bmp` / `.heic` 图片。

</details>

<details>
<summary><strong>6. 如何接入 Claude Desktop、Cursor 或 Google Antigravity？</strong></summary>

只需在客户端 MCP 配置中将 `command` 指向 `wechat-localmind`、参数设为 `["mcp-serve"]` 即可（详见上方 [MCP Stdio Server 接入](#mcp-stdio-server-接入与工具契约)）。若使用支持 Agent Skills 的客户端，可通过 `./bin/wechat-localmind install` 一键同步 `skills/wechat-localmind/SKILL.md`。

</details>

<details>
<summary><strong>7. 如何安全地配置或导入 SQLCipher 解密密钥？</strong></summary>

可通过环境变量 `WECHAT_LOCALMIND_KEY`、命令 `wechat-localmind key --set "<64-hex-key>"`、`wechat-localmind key --import ./keys.json`，或 `wechat-localmind key --scan-dump <file>` 从本地内存转储中按 16 字节 Salt 与 Page 1 HMAC 自动匹配。匹配校验通过后才可解密分片库。

</details>

<details>
<summary><strong>8. 如果本地微信数据库重建或 <code>--after-id</code> 游标越界会怎样？</strong></summary>

当传入的 `--after-id` 大于当前统一快照的 `global_latest_msg_id` 时，`fetch` 会自动降级为拉取过去 24 小时群聊记录，并在返回的 `meta.warnings` 中附加 `CURSOR_RESET_FALLBACK` 预警提示上游 Agent 重置游标。

</details>

<details>
<summary><strong>9. 用完后如何彻底清理本地解密后的快照和导出附件？</strong></summary>

执行 `wechat-localmind clean --all` 会立即销毁本地统一明文快照（`WeChat_Unified_snapshot.sqlite`）及已导出的 `snapshot/media/` 文件。如果只想清理导出附件而保留快照索引，可运行 `wechat-localmind clean --media-only`。

</details>

---

## 仓库结构与单元测试

```text
wechat-localmind/
├── README.md                        # 中文文档
├── README.en.md                     # 英文文档
├── llms.txt                         # AI Agent / LLM 检索规范索引 (llmstxt.org)
├── codemeta.json                    # CodeMeta 2.0 结构化软件元数据
├── LICENSE                          # MIT 开源协议
├── SECURITY.md                      # 隐私护栏与安全漏洞报告策略
├── CONTRIBUTING.md                  # 贡献指南与 PR 自检清单
├── CHANGELOG.md                     # 版本变更日志
├── pyproject.toml                   # Python 包配置（>=3.10，零第三方运行时依赖）
├── bin/wechat-localmind             # 免安装 CLI 可执行入口
├── skills/wechat-localmind/         # AI Agent 标准技能包 (SKILL.md + scripts/)
├── src/wx_localmind/                # 核心源码（crypto, snapshot, db, cleaner, media, mcp_server 等）
└── tests/test_wx_localmind.py       # 合成 SQLCipher / Protobuf / .dat 单元测试套件
```

```bash
# 运行全部单元测试（无需真实微信数据库）
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

<details>
<summary><strong>检索意图与高频词索引（Search Intent & Query Mapping）</strong></summary>

| 用户问题 / 检索意图 | 典型检索词（中 / 英） | 对应能力与文档定位 |
| :--- | :--- | :--- |
| 让 Claude / Cursor 安全读取微信群聊 | `微信 MCP Server` · `AI Agent 读取微信群聊` · `wechat mcp server` | `mcp-serve` 与 4 个只读 Tools（[MCP 契约](#mcp-stdio-server-接入与工具契约)） |
| 离线解密 macOS / Windows 微信 4.x WCDB | `WeChat 4.x WCDB 解密` · `zstd 解压` · `wechat 4.x wcdb decrypt python` | `CommonCrypto` + `zstd` 分片聚合引擎（[六大核心架构支柱](#六大核心架构支柱)） |
| 仅提取群聊并确保不泄露私聊给大模型 | `禁止读取私聊 微信提取工具` · `read-only wechat chatroom exporter` | 三重 `@chatroom` 物理隔离，退出码 `3`（[合规与隐私安全声明](#合规与隐私安全声明)） |
| 还原微信群内 `.dat` 加密图片 | `微信 .dat 图片解密` · `wechat dat xor image decoder` | `export-media` 自适应魔数异或还原（[CLI 子命令速查](#cli-子命令速查)） |
| 无重无漏的定时群聊汇总自动化 | `微信聊天记录增量拉取` · `wechat group chat extractor --after-id` | 单调游标 `--after-id` + 上下文缓冲窗（[CLI 子命令速查](#cli-子命令速查)） |

</details>

<sub>**Keywords / 关键词**: wechat-localmind · WeChat 3.x 4.x WCDB · SQLCipher CommonCrypto · zstd XML decompression · WeChat `.dat` XOR decoder · `@chatroom` group-only isolation · MCP Stdio Server · AI Agent CLI · local-first privacy · read-only chat extractor · 微信群聊只读提取 · 微信本地数据库解密 · 微信 MCP 服务器 · 离线快照防私聊泄露</sub>

---

## Architecture & Ecosystem · qilai (`qilaidav.com`)

This open-source project is engineered and maintained by **[qilai · qilaidav](https://qilaidav.com)** — an independent systems engineering studio focused on **Deterministic Systems Engineering & High-Signal Technical Diagnostics**.

- **Related System / Pillar**: [Telegram Channel Ops & Persona Engine (OASH)](https://qilaidav.com/oash/) · [English Hub](https://qilaidav.com/en/oash/)
- **Sister Repository (`*-localmind`)**: [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind)
- **Studio Website**: [https://qilaidav.com](https://qilaidav.com) ([EN](https://qilaidav.com/en/))
- **Technical Notes & Field Reports**: [https://qilaidav.com/notes/](https://qilaidav.com/notes/)
- **Direct Engineering Contact**: [Telegram @BD8888](https://t.me/BD8888) · [Public Channel @AIKB8](https://t.me/AIKB8) · `hello@qilaidav.com`
