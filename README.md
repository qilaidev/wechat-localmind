# WeChat LocalMind (`wechat-localmind`)

**零依赖 macOS & Windows 微信（WeChat 3.x / 4.x）本地群聊只读提取、结构化清洗、CLI 与 MCP Server 能力层**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Zero Runtime Deps](https://img.shields.io/badge/Dependencies-Zero%20Runtime%20Deps-brightgreen.svg)](./pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-lightgrey.svg)](#安装与快速上手)
[![WeChat](https://img.shields.io/badge/WeChat-3.x%20%2F%204.x%20WCDB-07C160.svg)](#六大核心架构支柱)
[![MCP Server](https://img.shields.io/badge/MCP-Stdio%20Read--Only-6E56CF.svg)](#mcp-stdio-server-接入与工具契约)
[![Agent CLI](https://img.shields.io/badge/CLI-Agent--Ready%20JSON%2FMD-orange.svg)](#cli-子命令速查)

**[English](./README.en.md)** | **简体中文** | **[llms.txt](./llms.txt)** | **[SKILL.md](./skills/wechat-localmind/SKILL.md)** | **[CONTRIBUTING](./CONTRIBUTING.md)** | **[SECURITY](./SECURITY.md)** | **[LICENSE](./LICENSE)**

---

**WeChat LocalMind（`wechat-localmind`）** 是一个零第三方运行时依赖的本地优先（Local-First）只读数据提取能力层，专为 AI Agent、自动化工作流与个人数据归档设计。它通过离线快照解密 macOS 与 Windows 本地的微信 3.x / 4.x（WCDB）加密分片数据库，仅提取并清洗**微信群聊（`@chatroom`）**对话与沙盒媒体附件，**在架构层面 100% 物理拦截一对一私聊（`wxid_*`）与公众号（`gh_*`）**，输出高信噪比的 JSON / Markdown 结构化结果或通过标准 MCP Stdio 协议交付给 Claude、Cursor、Google Antigravity 等智能体消费。

> **`*-localmind` 本地即时通讯只读提取能力层家族**
>
> 本仓库与姊妹仓 [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind)（macOS WhatsApp 本地群聊提取能力层）同属 `qilaidev` 下的 **`*-localmind` 本地聊天提取家族**，两仓严格遵循统一的五项架构哲学：
> **100% 离线只读快照 + 物理拦截一对一私聊 + 四级实名还原 + 单调增量游标 + CLI & MCP 双协议交付**。

---

## 定位边界：它是什么 / 它不是什么

| 维度 | 它是什么（What it is） | 它不是什么（What it is NOT） |
| :--- | :--- | :--- |
| **运行模式** | **100% 离线只读快照**：仅在本地独立快照目录解密与聚合，零网络请求 | **不是**微信外挂、Hook 注入器或协议逆向客户端 |
| **访问范围** | **仅限群聊（`@chatroom`）**：支持黑白名单策略与单调增量游标拉取 | **绝对禁止**读取一对一私聊（`wxid_*`）与公众号（`gh_*`） |
| **写入能力** | **纯只读（Read-Only）**：绝不修改微信原库，绝不包含任何发送消息 API | **不是**自动回复机器人、群发工具或 RPA 控制端 |
| **交付形态** | **CLI & MCP Stdio Server**：输出纯净 JSON / Markdown 供 AI Agent 调用 | **不是**独立 GUI 客户端或云端 SaaS 托管服务 |
| **依赖体积** | **零第三方 pip 依赖**：直接调用系统原生 `CommonCrypto` / `zstd` / `sqlite3` | **不依赖**臃肿的 C++ 编译链或外部 SQLCipher 二进制包 |

---

## 合规与隐私安全声明

- **合法授权用途**：本工具严格仅用于**用户本人设备上的个人本地数据备份与经授权的离线研究**。使用本工具处理群聊数据时，请遵守所在地法律法规及群聊参与方的隐私约定。
- **100% 离线只读快照**：所有解密与聚合操作均在独立快照目录（macOS: `~/Library/Application Support/WeChat-LocalMind/snapshot`；Windows: `%LOCALAPPDATA%\WeChat-LocalMind\snapshot`）中完成，**绝不修改微信原始数据库、绝不注入或干扰微信客户端运行态、绝不发起任何网络请求、绝不发送任何消息**。
- **三重 `@chatroom` 物理隔离**：在分片提取层（`snapshot.py`）、SQL 查询层（`db.py` 强制 `chat_id LIKE '%@chatroom'`）与附件导出/策略层（`config.py` / `media.py`）三重物理拦截一对一私聊（`wxid_*`）与公众号（`gh_*`），越权访问直接返回 `EXIT_POLICY_DENIED`（退出码 `3`）。

---

## 六大核心架构支柱

1. **原生 `CommonCrypto` SQLCipher 3 / 4 离线解密引擎**
   - 优先调用 macOS 系统原生 `CommonCrypto`（`/usr/lib/libSystem.B.dylib` `CCCrypt`）与标准库 `hashlib.pbkdf2_hmac`，零 pip 依赖即可实现 C 语言级 AES-256-CBC 逐页验证与解密，全面兼容 WeChat 4.x WCDB（`reserve=80`）与 WeChat 3.x（`reserve=48`）。
2. **WeChat 4.x `zstd` 内存级解压与多片库自动聚合**
   - 优先调用 Python 3.14+ 内置 `compression.zstd` 与原生 `libzstd.dylib`（`ctypes`）实现零子进程开销的内存解压；自动跨 `message_0.db ~ message_N.db` 解析 `Msg_<md5>` / `Chat_<md5>` / `MSG` 表与分片内 `Name2Id` 映射，按时间序生成全局单调递增游标 `msg_id`。
3. **三重 `@chatroom` 隐私隔离护栏**
   - 提取、查询、导出三层硬编码校验 `chat_id LIKE '%@chatroom'`，物理隔离 `wxid_*` 私聊与 `gh_*` 公众号，支持群聊黑/白名单精细化控制与个人身份锚点标记。
4. **四级实名还原与 `@wxid_` 自动转实名**
   - 自动关联 `contact.db` 与 `chatroom_member`，按 **通讯录备注 (`remark`) > 群内昵称 (`group_nickname`) > 微信昵称 (`nick_name`) > 脱敏 wxid (`微信群友(xxxx)`)** 四级优先级还原发言人身份，并将正文中残留的 `@wxid_xxx` 与 `\u2005` 特殊空格自动替换为 `@实名` 或 `[@我(实名)]`。
5. **XML / Protobuf 富媒体深度解析与 150 字引用链还原**
   - 内置纯 Python 零依赖 Protobuf Wire 解析器与 XML 解析器，精准还原 `<appmsg type="57"><refermsg>` **150 字父消息引用链**、文件卡片（`type=6`）、网页链接（`type=5`）、小程序（`type=33/36`）、视频号/直播卡片（`type=51/63`）、合并转发（`type=19`）与群公告（`type=87`）；自动剔除撤回消息（`revokemsg`）、拍一拍与系统通知，并自动折叠连续「收到 / OK / +1 / 红包」噪音。
6. **微信 `.dat` 异或加密图片向量化还原**
   - `export-media` 内置自适应 Magic Header 异或密钥推导器与向量化大整数位运算引擎，导出微信 `.dat` 加密图片时**自动解密还原为标准 `.jpg` / `.png` / `.gif` / `.webp` / `.heic`**。

---

## 安装与快速上手

前置要求：Python `>= 3.10`（零第三方运行时依赖）。

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
| `decrypt` | 离线解密全部分片并强制刷新统一只读快照 | `wechat-localmind decrypt` |
| `config` | 配置本人身份锚点（`set-identity`）与群黑白名单 | `wechat-localmind config block "88888888@chatroom"` |
| `groups` / `list-groups` | 列出可访问群聊（`@chatroom`）及 `latest_msg_id` 游标 | `wechat-localmind groups --format json` |
| `fetch` | 按游标（`--after-id`）或时间窗（`--since`）拉取清洗后消息 | `wechat-localmind fetch --chats ALL_ALLOWED --after-id 200 --format json` |
| `search` | 跨允许群聊检索文本与 `zstd` 富媒体（含上下文） | `wechat-localmind search --query "报价单" --context 3 --format md` |
| `export-media` | 导出群附件并自动解密 `.dat` 异或图片 | `wechat-localmind export-media --msg-ids 15,16 --format json` |
| `clean` | 清理导出媒体缓存或销毁本地解密快照 | `wechat-localmind clean --media-only` |
| `mcp-serve` | 启动只读 JSON-RPC 2.0 MCP Stdio Server | `wechat-localmind mcp-serve` |
| `install` | 挂载 CLI 软链到 `~/.local/bin` 并安装 `SKILL.md` | `./bin/wechat-localmind install` |

<details>
<summary>展开查看完整 CLI 参数示例</summary>

```bash
# 配置 SQLCipher 密钥（支持全局密钥、分片密钥或内存转储扫描）
wechat-localmind key --set "<64-hex-key>"
wechat-localmind key --set "contact.db:<64-hex-key>"
wechat-localmind key --scan-dump ./wechat_memory.dump

# 设置本人身份锚点（将本人消息标记为 [我(姓名)]）
wechat-localmind config set-identity --name "张工" --wxid "wxid_example123456"

# 按关键词筛选群聊（包含已屏蔽群）
wechat-localmind groups --keyword "项目" --include-blocked --format json
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
| `3` | `EXIT_POLICY_DENIED` | 隐私策略拦截（尝试访问一对一私聊 `wxid_*` 或黑名单屏蔽群聊） |
| `4` | `EXIT_RESOURCE_NOT_FOUND` | 目标群聊/`msg_id` 不存在，或附件尚未在微信客户端下载到本地 |
| `5` | `EXIT_INTERNAL_DB_ERROR` | SQLite / SQLCipher 内部解析异常 |

---

## 常见问题（FAQ）

<details>
<summary><strong>1. 支持哪些微信版本？WeChat 3.x 和 4.x（WCDB）有什么区别？</strong></summary>

同时支持 macOS 与 Windows 的 **WeChat 3.x**（`reserve=48`，如 `msg_*.db` / `MSG*.db`）和 **WeChat 4.x WCDB**（`reserve=80`，如 `xwechat_files/db_storage/message_*.db` 与 `contact.db`）。工具会自动探测数据目录格式、分片结构与 `zstd` 压缩载荷并统一聚合。

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
<summary><strong>4. 为什么能做到零第三方 pip 依赖？解密性能如何？</strong></summary>

在 macOS 上直接通过 `ctypes` 调用系统原生 `CommonCrypto`（`CCCrypt`）与 `libzstd.dylib`（Python 3.14+ 优先使用内置 `compression.zstd`），配合标准库 `hashlib.pbkdf2_hmac` 实现 C 语言级 AES-256-CBC 逐页验证与解密，无需安装 `pysqlcipher3` 或 `cryptography` 等重型编译依赖。

</details>

<details>
<summary><strong>5. 微信加密的 <code>.dat</code> 图片如何还原？</strong></summary>

运行 `wechat-localmind export-media --msg-ids <id>` 或调用 MCP 工具 `wechat_export_media` 时，内置的自适应 Magic Header 异或密钥推导器会自动识别文件头特征，通过向量化大整数位运算将 `.dat` 还原为标准 `.jpg` / `.png` / `.gif` / `.webp` / `.heic` 图片。

</details>

<details>
<summary><strong>6. 如何接入 Claude Desktop、Cursor 或 Google Antigravity？</strong></summary>

只需在客户端 MCP 配置中将 `command` 指向 `wechat-localmind`、参数设为 `["mcp-serve"]` 即可（详见上方 [MCP Stdio Server 接入](#mcp-stdio-server-接入与工具契约)）。若使用支持 Agent Skills 的客户端，可通过 `./bin/wechat-localmind install` 一键同步 `skills/wechat-localmind/SKILL.md`。

</details>

---

## 仓库结构与单元测试

```text
wechat-localmind/
├── README.md                        # 中文文档
├── README.en.md                     # 英文文档
├── llms.txt                         # AI Agent / LLM 检索规范索引 (llmstxt.org)
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
