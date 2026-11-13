# WeChat LocalMind (`wechat-localmind`)

**Zero-Dependency macOS & Windows WeChat (3.x / 4.x) Local Group Chat Read-Only Extractor, Cleaner, CLI & MCP Server for AI Agents**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Zero Runtime Deps](https://img.shields.io/badge/Dependencies-Zero%20Runtime%20Deps-brightgreen.svg)](./pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-lightgrey.svg)](#installation--quickstart)
[![WeChat](https://img.shields.io/badge/WeChat-3.x%20%2F%204.x%20WCDB-07C160.svg)](#6-pillar-architecture)
[![MCP Server](https://img.shields.io/badge/MCP-Stdio%20Read--Only-6E56CF.svg)](#mcp-stdio-server--tool-contracts)
[![Agent CLI](https://img.shields.io/badge/CLI-Agent--Ready%20JSON%2FMD-orange.svg)](#cli-subcommands)

**English** | **[简体中文](./README.md)** | **[llms.txt](./llms.txt)** | **[SKILL.md](./skills/wechat-localmind/SKILL.md)** | **[CONTRIBUTING](./CONTRIBUTING.md)** | **[SECURITY](./SECURITY.md)** | **[LICENSE](./LICENSE)**

---

**WeChat LocalMind (`wechat-localmind`)** is a zero-dependency, local-first, strictly read-only data extraction layer for macOS and Windows WeChat (3.x / 4.x WCDB) designed for AI Agents, automation workflows, and personal data archiving. It decrypts local encrypted database shards in an isolated offline snapshot, extracts and cleans **group chats (`@chatroom`)** and sandboxed attachments while **physically blocking 1-on-1 private chats (`wxid_*`) and official accounts (`gh_*`)**, and delivers structured JSON / Markdown via CLI or a zero-dependency MCP Stdio Server.

> **`*-localmind` Local Messaging Read-Only Extraction Family**
>
> Together with its sister repository [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind) (macOS WhatsApp local group chat extraction layer) under `qilaidev`, both projects share five unified architectural principles:
> **100% Offline Read-Only Snapshot + Physical 1-on-1 DM Blocking + 4-Tier Real-Name Resolution + Monotonic Incremental Cursors + Dual CLI & MCP Delivery**.

---

## Positioning: What It Is / What It Is Not

| Dimension | What It Is | What It Is NOT |
| :--- | :--- | :--- |
| **Execution** | **100% Offline Read-Only Snapshot** with zero network requests | **NOT** a WeChat hook, runtime injector, or protocol bot |
| **Scope** | **Group Chats Only (`@chatroom`)** with allow/block policies & monotonic cursors | **NEVER** reads 1-on-1 private chats (`wxid_*`) or official accounts (`gh_*`) |
| **Mutation** | **Strictly Read-Only**: never modifies original DBs or sends messages | **NOT** an auto-responder, mass-messaging tool, or RPA controller |
| **Delivery** | **CLI & MCP Stdio Server** emitting clean JSON / Markdown for AI Agents | **NOT** a standalone GUI app or cloud-hosted SaaS |
| **Dependencies** | **Zero Third-Party Pip Dependencies** via native `CommonCrypto` / `zstd` / `sqlite3` | **NOT** dependent on heavy C++ build chains or external SQLCipher binaries |

---

## Legal & Privacy Compliance Notice

- **Intended Use**: Strictly designed for **personal local data backup and authorized offline research on the user's own device**. Users are responsible for complying with applicable laws and participant privacy expectations.
- **100% Read-Only Offline Snapshot**: All decryption and aggregation occur inside an isolated snapshot directory (`~/Library/Application Support/WeChat-LocalMind/snapshot` on macOS; `%LOCALAPPDATA%\WeChat-LocalMind\snapshot` on Windows). It never modifies original WeChat databases, never injects into the WeChat client, makes zero network calls, and never sends messages.
- **Triple-Layer `@chatroom` Guard**: Physically blocks 1-on-1 private chats (`wxid_*`) and official accounts (`gh_*`) across the shard extractor (`snapshot.py`), SQL query layer (`db.py` enforcing `chat_id LIKE '%@chatroom'`), and policy/media layer (`config.py`, `media.py`), returning `EXIT_POLICY_DENIED` (`3`).

---

## 6-Pillar Architecture

1. **Native `CommonCrypto` SQLCipher 3 / 4 Offline Decryption**
   - Calls macOS native `CommonCrypto` (`/usr/lib/libSystem.B.dylib` `CCCrypt`) and stdlib `hashlib.pbkdf2_hmac` for C-speed AES-256-CBC page verification and decryption without any pip dependencies, supporting both WeChat 4.x WCDB (`reserve=80`) and WeChat 3.x (`reserve=48`).
2. **WeChat 4.x `zstd` Decompression & Multi-Shard Aggregation**
   - Uses Python 3.14+ built-in `compression.zstd` or native `libzstd.dylib` (`ctypes`) in-memory; automatically merges `message_0.db ~ message_N.db` (`Msg_<md5>` / `Chat_<md5>` / `MSG` tables and `Name2Id` mappings) into a unified snapshot with a globally monotonic `msg_id` cursor.
3. **Triple-Layer `@chatroom` Privacy Guard**
   - Enforces `chat_id LIKE '%@chatroom'` across extraction, SQL queries, and media export, physically blocking `wxid_*` private chats and `gh_*` official accounts with configurable group blacklists/whitelists.
4. **4-Tier Real-Name Mapping & `@wxid_` Normalization**
   - Joins `contact.db` and `chatroom_member` to resolve senders in priority order: **Contact Remark (`remark`) > Group Nickname (`group_nickname`) > WeChat Nickname (`nick_name`) > Masked ID (`微信群友(xxxx)`)**, and rewrites `@wxid_xxx` / `\u2005` tokens into `@RealName` or `[@Me(RealName)]`.
5. **XML / Protobuf Rich-Media & 150-Char Quote Chain Parser**
   - Zero-dependency Protobuf Wire and XML parsers reconstruct `<appmsg type="57"><refermsg>` **150-character parent quote chains**, file cards (`type=6`), web links (`type=5`), mini-programs (`type=33/36`), Channels/live cards (`type=51/63`), merged forwards (`type=19`), and group announcements (`type=87`), while stripping revokes/pats/system noise and folding consecutive acks.
6. **Vectorized `.dat` XOR Image Restoration**
   - `export-media` includes an adaptive magic-header XOR key deriver and vectorized bitwise decoder that automatically restores encrypted `.dat` files into standard `.jpg`, `.png`, `.gif`, `.webp`, or `.heic` images.

---

## Installation & Quickstart

Requirements: Python `>= 3.10` (zero third-party runtime dependencies).

```bash
git clone https://github.com/qilaidev/wechat-localmind.git
cd wechat-localmind

# Symlink CLI to ~/.local/bin and sync the AI Agent Skill bundle
./bin/wechat-localmind install --bin-dir ~/.local/bin --skill-dir ~/.gemini/config/skills
```

### 30-Second Quickstart

```bash
# 1. Inspect local WeChat 3.x/4.x databases and key health
wechat-localmind doctor --format md

# 2. Import/set SQLCipher key, then fetch the last 24 hours of allowed group chats
wechat-localmind fetch --chats ALL_ALLOWED --since 24h --format md
```

---

## CLI Subcommands

| Subcommand | Purpose | Example |
| :--- | :--- | :--- |
| `doctor` | Environment, WeChat 3.x/4.x DB discovery & key health check | `wechat-localmind doctor --format md` |
| `key` | Set/import SQLCipher keys or scan a memory dump | `wechat-localmind key --import ./keys.json` |
| `decrypt` | Decrypt all shards and rebuild the unified read-only snapshot | `wechat-localmind decrypt` |
| `config` | Configure identity anchor (`set-identity`) & group policies | `wechat-localmind config block "88888888@chatroom"` |
| `groups` / `list-groups` | List accessible `@chatroom` groups & `latest_msg_id` cursors | `wechat-localmind groups --format json` |
| `fetch` | Fetch cleaned group messages by cursor (`--after-id`) or `--since` | `wechat-localmind fetch --chats ALL_ALLOWED --after-id 200 --format json` |
| `search` | Search across allowed groups (including `zstd` XML) with context | `wechat-localmind search --query "invoice" --context 3 --format md` |
| `export-media` | Export group attachments & auto-decode `.dat` XOR images | `wechat-localmind export-media --msg-ids 15,16 --format json` |
| `clean` | Clean exported media cache or destroy local decrypted snapshots | `wechat-localmind clean --media-only` |
| `mcp-serve` | Start the read-only JSON-RPC 2.0 MCP Stdio Server | `wechat-localmind mcp-serve` |
| `install` | Install CLI symlink to `~/.local/bin` and optional `SKILL.md` | `./bin/wechat-localmind install` |

---

## MCP Stdio Server & Tool Contracts

Add `wechat-localmind` to Claude Desktop, Cursor, Google Antigravity, or Codex:

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

### Exposed Read-Only MCP Tools (4)

| Tool | Required Args | Optional Args & Defaults | Description |
| :--- | :--- | :--- | :--- |
| `wechat_list_groups` | None | `keyword` (`string`), `include_blocked` (`false`), `format` (`"json"` \| `"markdown"`), `fresh` (`false`) | List accessible `@chatroom` groups, member counts, 24h message counts, and `latest_msg_id` |
| `wechat_fetch_messages` | `chat_ids` (`string[]` or `["ALL_ALLOWED"]`) | `after_msg_id` (`int`), `since` (`"24h"`), `until` (`string`), `context_buffer` (`10`), `limit_per_chat` (`500`, max `3000`), `fold_noise` (`true`), `format` (`"markdown"` \| `"json"`), `fresh` (`false`) | Fetch cleaned group messages with 4-tier real names, `@mention` normalization, 150-char quote chains, and revoke filtering |
| `wechat_search_messages` | `query` (`string`) | `chat_ids` (`string[]`), `since` (`string`), `context` (`3`), `limit` (`50`), `format` (`"markdown"` \| `"json"`) | Search text and attachment titles (including `zstd` XML) across allowed groups with context window |
| `wechat_export_media` | `msg_ids` (`int[]`) | None | Export group attachments to `snapshot/media`, auto-decoding `.dat` XOR images and returning absolute paths |

---

## Environment Variables & POSIX Exit Codes

- `WECHAT_LOCALMIND_DATA_DIR`: Override WeChat data root (`xwechat_files` or `WeChat Files`)
- `WECHAT_LOCALMIND_ACCOUNT_DIR`: Pin a specific WeChat account directory
- `WECHAT_LOCALMIND_SNAPSHOT_DIR`: Override snapshot & media export directory
- `WECHAT_LOCALMIND_KEY`: Global 64-char Hex SQLCipher key

| Exit Code | Constant | Meaning |
| :--- | :--- | :--- |
| `0` | `EXIT_SUCCESS` | Success |
| `1` | `EXIT_USAGE_ERROR` | Invalid CLI arguments, time window, or Hex key format |
| `2` | `EXIT_ENV_OR_PERM_ERROR` | WeChat directory unreadable, missing SQLCipher key, or key mismatch |
| `3` | `EXIT_POLICY_DENIED` | Privacy guard triggered (1-on-1 `wxid_*` chat, `gh_*` account, or blocked group) |
| `4` | `EXIT_RESOURCE_NOT_FOUND` | Target `chat_id` / `msg_id` not found, or media not downloaded locally |
| `5` | `EXIT_INTERNAL_DB_ERROR` | Internal SQLite / SQLCipher parsing error |

---

## Frequently Asked Questions (FAQ)

<details>
<summary><strong>1. Which WeChat versions are supported? How do 3.x and 4.x WCDB differ?</strong></summary>

Both macOS and Windows **WeChat 3.x** (`reserve=48`, `msg_*.db` / `MSG*.db`) and **WeChat 4.x WCDB** (`reserve=80`, `xwechat_files/db_storage/message_*.db` + `contact.db`) are supported. `wechat-localmind` auto-detects shard layouts and `zstd`-compressed payloads.

</details>

<details>
<summary><strong>2. Is there any risk of account ban or interfering with WeChat?</strong></summary>

**Zero ban risk.** `wechat-localmind` operates strictly on copied offline snapshots, never injects into or modifies the running WeChat client, makes zero network calls, and contains no message-sending APIs.

</details>

<details>
<summary><strong>3. How are 1-on-1 private DMs (<code>wxid_*</code>) physically blocked?</strong></summary>

A triple-layer guard across shard extraction, SQL queries (`chat_id LIKE '%@chatroom'`), and policy/media validation rejects any `wxid_*` private chat or `gh_*` official account with `EXIT_POLICY_DENIED` (`3`).

</details>

<details>
<summary><strong>4. How does it achieve zero third-party pip dependencies?</strong></summary>

On macOS it calls native `CommonCrypto` (`CCCrypt`) and `libzstd.dylib` via `ctypes` (preferring Python 3.14+ built-in `compression.zstd`) alongside stdlib `hashlib.pbkdf2_hmac`, delivering C-speed AES-256-CBC decryption without `pysqlcipher3` or `cryptography`.

</details>

<details>
<summary><strong>5. How are encrypted <code>.dat</code> images decrypted?</strong></summary>

`export-media` (and `wechat_export_media`) derives the per-file XOR key from known image magic headers and decodes `.dat` files into standard `.jpg`, `.png`, `.gif`, `.webp`, or `.heic` files.

</details>

<details>
<summary><strong>6. How do I integrate with Claude Desktop, Cursor, or Google Antigravity?</strong></summary>

Configure `wechat-localmind` with `args: ["mcp-serve"]` in your MCP client config (see [MCP Stdio Server](#mcp-stdio-server--tool-contracts)), or run `./bin/wechat-localmind install` to install the `SKILL.md` bundle.

</details>

---

## Development & Unit Tests

```bash
# Run all unit tests (uses synthetic SQLCipher, Protobuf/XML, and .dat fixtures)
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
