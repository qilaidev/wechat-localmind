# WeChat LocalMind (`wechat-localmind`)

**WeChat LocalMind (`wechat-localmind`)** is a local-first, strictly read-only CLI and JSON-RPC 2.0 MCP Stdio Server that extracts and cleans **WeChat 3.x and 4.x (WCDB) group chats (`@chatroom`)** across macOS and Windows database layouts for AI Agents such as Claude Desktop, Cursor, and Google Antigravity. Operating solely on offline SQLCipher snapshots with native macOS `CommonCrypto` and `zstd` support, it physically blocks 1-on-1 private chats (`wxid_*`) and official accounts (`gh_*`), auto-decodes `.dat` XOR images, and makes zero network requests.

[![CI](https://github.com/qilaidev/wechat-localmind/actions/workflows/ci.yml/badge.svg)](https://github.com/qilaidev/wechat-localmind/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Zero Deps on macOS](https://img.shields.io/badge/Dependencies-Zero%20Pip%20Deps%20(macOS)-brightgreen.svg)](./pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20DBs-lightgrey.svg)](#installation--quickstart)
[![WeChat](https://img.shields.io/badge/WeChat-3.x%20%2F%204.x%20WCDB-07C160.svg)](#6-pillar-architecture)
[![MCP Server](https://img.shields.io/badge/MCP-Stdio%20Read--Only-6E56CF.svg)](#mcp-stdio-server--tool-contracts)

**[简体中文](./README.md)** | **English** | **[llms.txt](./llms.txt)** | **[codemeta.json](./codemeta.json)** | **[SKILL.md](./skills/wechat-localmind/SKILL.md)** | **[SECURITY](./SECURITY.md)** | **[CONTRIBUTING](./CONTRIBUTING.md)** | **[CHANGELOG](./CHANGELOG.md)** | **[LICENSE](./LICENSE)**

> **`*-localmind` Local Messaging Read-Only Extraction Family**
>
> Together with its sister repository [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind) (macOS WhatsApp local group chat extraction layer) under `qilaidev`, both projects share five unified architectural principles:
> **100% Offline Read-Only Snapshot + Physical 1-on-1 DM Blocking + 4-Tier Real-Name Resolution + Monotonic Incremental Cursors + Dual CLI & MCP Delivery**.

---

## Entity Snapshot

| Attribute | Specification & Fact |
| :--- | :--- |
| **Identifier** | `wechat-localmind` (Python package `wx_localmind`, version `1.0.0`) |
| **License** | [MIT License](./LICENSE) |
| **Runtime & Dependencies** | Python `>= 3.10`; **zero third-party pip packages on macOS** (native `CommonCrypto`, `libzstd.dylib` / Python 3.14 `compression.zstd`, and stdlib `sqlite3`); optional `cryptography` or `PyCryptodome` for AES-256-CBC on Linux / non-macOS |
| **Supported WeChat DBs** | macOS & Windows **WeChat 4.x WCDB** (`reserve=80`, `xwechat_files/db_storage/message_*.db`, `contact.db`, `zstd` XML) and **WeChat 3.x** (`reserve=48`, `msg_*.db` / `MSG*.db` + `MicroMsg.db`) |
| **Privacy Boundary** | **Group chats (`@chatroom`) only**; physically blocks 1-on-1 private chats (`wxid_*`) and official accounts (`gh_*`) across extraction, SQL, and media export (`EXIT_POLICY_DENIED` = `3`) |
| **Delivery Interfaces** | 11 POSIX CLI subcommands (+ `list-groups` alias) + 4 read-only JSON-RPC 2.0 MCP Stdio tools + `SKILL.md` agent bundle |
| **Default Snapshot Path** | `~/Library/Application Support/WeChat-LocalMind/snapshot` (overridable via `WECHAT_LOCALMIND_SNAPSHOT_DIR`) |
| **Maintainer** | [`qilaidev`](https://github.com/qilaidev) · [qilai Engineering Studio (`qilaidav.com`)](https://qilaidav.com/en/) |

---

## Positioning: What It Is / What It Is Not

| Dimension | What It Is | What It Is NOT |
| :--- | :--- | :--- |
| **Execution** | **100% Offline Read-Only Snapshot** opened via `?mode=ro` with zero network requests | **NOT** a WeChat hook, runtime injector, or protocol bot |
| **Scope** | **Group Chats Only (`@chatroom`)** with allow/block policies & monotonic cursors | **NEVER** reads 1-on-1 private chats (`wxid_*`) or official accounts (`gh_*`) |
| **Mutation** | **Strictly Read-Only**: never modifies original DBs or sends messages | **NOT** an auto-responder, mass-messaging tool, or desktop RPA controller |
| **Key Boundary** | **User-Supplied Key Verification**: `--set`, `--import`, or `--scan-dump` matched against 16B Salt + HMAC | **NOT** an encryption-bypass or credential-stealing tool |
| **Delivery** | **CLI & MCP Stdio Server** emitting clean JSON / Markdown for AI Agents | **NOT** a standalone GUI app or cloud-hosted SaaS |

---

## Use Cases & Alternatives Comparison

### Common Workflows

| Use Case | Recommended Entry / Command | Outcome |
| :--- | :--- | :--- |
| **AI Agent Daily Group Digest & Action Items** | MCP `wechat_fetch_messages` or `fetch --chats ALL_ALLOWED --since 24h` | Resolves 4-tier real names, 150-char quote chains, and folds consecutive ack noise |
| **Idempotent Incremental Message Sync** | `fetch --chats ALL_ALLOWED --after-id <msg_id> --format json` | Advances monotonically with a 10-message context buffer and 24h out-of-bounds fallback |
| **Cross-Group Contract & Decision Search** | `search --query "invoice" --context 3` or MCP `wechat_search_messages` | Searches text and `zstd`-compressed XML attachment titles with +/- N context lines |
| **Group Media & `.dat` Image Archiving** | `export-media --msg-ids 15,16` or MCP `wechat_export_media` | Derives per-file XOR keys and exports `.dat` files into standard `.jpg`/`.png`/etc. |

### Architectural Comparison

> Honest architectural comparison across approaches — no fabricated benchmarks or telemetry claims.

| Capability / Boundary | **WeChat LocalMind** | Generic `sqlcipher` CLI + Manual SQL | Desktop GUI WeChat Exporters | WeChat Hooks / Protocol Bots |
| :--- | :---: | :---: | :---: | :---: |
| **Ban & Runtime Interference Risk** | Zero (offline snapshot copy, zero injection, zero network calls) | Zero (offline manual read) | Varies (some attach to memory) | High (runtime hooking or unofficial protocol) |
| **Physical 1-on-1 DM Blocking** | Triple-layer `@chatroom` guard (`EXIT_POLICY_DENIED = 3`) | Manual SQL filter every query (error-prone) | Usually exports all DMs and groups | Unbounded access to all contacts |
| **WeChat 4.x WCDB (`reserve=80` + `zstd`)** | Built-in multi-shard aggregation + `zstd` decompression | Manual pragma per shard + external `zstd` decoding | Partial support in newer GUI tools | N/A |
| **Zero Pip Deps on macOS** | Native `CommonCrypto` + `libzstd` via `ctypes` | Requires compiling/installing `sqlcipher` binary | Requires heavy GUI runtime | Requires protocol/runtime packages |
| **`.dat` XOR Image Auto-Decoding** | Built-in magic-header XOR derivation + vectorized decode | Not included | Usually supported | Live download only |
| **AI Agent Delivery (MCP + JSON/MD + Cursor)** | 4 read-only MCP tools + 11 CLI commands + `SKILL.md` | Raw SQL rows only | Human-oriented HTML/PDF/CSV | Event-driven bot APIs; no monotonic read-only cursor |
| **Sending Messages / Automation** | None (strictly read-only by design) | None | None | Yes |

---

## Legal & Privacy Compliance Notice

- **Intended Use**: Strictly designed for **personal local data backup and authorized offline research on the user's own device**. Users are responsible for complying with applicable laws and participant privacy expectations.
- **100% Read-Only Offline Snapshot**: All decryption and aggregation occur inside an isolated snapshot directory (`~/Library/Application Support/WeChat-LocalMind/snapshot` by default, overridable via `WECHAT_LOCALMIND_SNAPSHOT_DIR`). It never modifies original WeChat databases, never injects into the WeChat client, makes zero network calls, and never sends messages.
- **Triple-Layer `@chatroom` Guard**: Physically blocks 1-on-1 private chats (`wxid_*`) and official accounts (`gh_*`) across the shard extractor (`snapshot.py`), SQL query layer (`db.py` enforcing `chat_id LIKE '%@chatroom'`), and policy/media layer (`config.py`, `media.py`), returning `EXIT_POLICY_DENIED` (`3`).

---

## 6-Pillar Architecture

1. **Native `CommonCrypto` SQLCipher 3 / 4 Offline Decryption**
   - On macOS, calls native `CommonCrypto` (`/usr/lib/libSystem.B.dylib` `CCCrypt`) and stdlib `hashlib.pbkdf2_hmac` for AES-256-CBC page verification and decryption without pip dependencies (automatically falling back to `cryptography` or `PyCryptodome` on Linux / non-macOS), supporting both WeChat 4.x WCDB (`reserve=80`) and WeChat 3.x (`reserve=48`).
2. **WeChat 4.x `zstd` Decompression & Multi-Shard Aggregation**
   - Uses Python 3.14+ built-in `compression.zstd`, `zstandard`, macOS `libzstd.dylib` (`ctypes`), or `zstd` CLI fallback; automatically merges `message_0.db ~ message_N.db` (`Msg_<md5>` / `Chat_<md5>` / `MSG` tables and `Name2Id` mappings) into a unified snapshot with a globally monotonic `msg_id` cursor.
3. **Triple-Layer `@chatroom` Privacy Guard**
   - Enforces `chat_id LIKE '%@chatroom'` across extraction, SQL queries, and media export, physically blocking `wxid_*` private chats and `gh_*` official accounts with configurable group blacklists/whitelists.
4. **4-Tier Real-Name Mapping & `@wxid_` Normalization**
   - Joins `contact.db` and `chatroom_member` to resolve senders in priority order: **Contact Remark (`remark`) > Group Nickname (`group_nickname`) > WeChat Nickname (`nick_name`) > Masked ID (`微信群友(xxxx)`)**, and rewrites `@wxid_xxx` / `\u2005` tokens into `@RealName` or `[@Me(RealName)]`.
5. **XML / Protobuf Rich-Media & 150-Char Quote Chain Parser**
   - Zero-dependency Protobuf Wire and XML parsers reconstruct `<appmsg type="57"><refermsg>` **150-character parent quote chains**, file cards (`type=6`), web links (`type=5`), mini-programs (`type=33/36`), Channels/live cards (`type=51/63`), merged forwards (`type=19`), and group announcements (`type=87`), while stripping revokes/pats/system noise and folding consecutive acks.
6. **Vectorized `.dat` XOR Image Restoration**
   - `export-media` includes an adaptive magic-header XOR key deriver and vectorized bitwise decoder that automatically restores encrypted `.dat` files into standard `.jpg`, `.png`, `.gif`, `.webp`, `.bmp`, or `.heic` images.

---

## Installation & Quickstart

Requirements: Python `>= 3.10` (**zero third-party pip dependencies on macOS**; on Linux or environments without macOS `CommonCrypto`, install `pip install cryptography` or `pycryptodome` for AES-256-CBC).

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
| `decrypt` | Decrypt a single DB (`--src`/`--dst`) or rebuild unified snapshot | `wechat-localmind decrypt` |
| `config` | Configure identity anchor (`set-identity`) & group policies | `wechat-localmind config block "88888888@chatroom"` |
| `groups` / `list-groups` | List accessible `@chatroom` groups & `latest_msg_id` cursors | `wechat-localmind groups --format json` |
| `fetch` | Fetch cleaned group messages by cursor (`--after-id`) or `--since` | `wechat-localmind fetch --chats ALL_ALLOWED --after-id 200 --format json` |
| `search` | Search across allowed groups (including `zstd` XML) with context | `wechat-localmind search --query "invoice" --context 3 --format md` |
| `export-media` | Export group attachments & auto-decode `.dat` XOR images | `wechat-localmind export-media --msg-ids 15,16 --format json` |
| `clean` | Clean exported media (`--media-only`) or destroy snapshot (`--all`) | `wechat-localmind clean --media-only` |
| `mcp-serve` | Start the read-only JSON-RPC 2.0 MCP Stdio Server | `wechat-localmind mcp-serve` |
| `install` | Install CLI symlink to `~/.local/bin` and optional `SKILL.md` | `./bin/wechat-localmind install` |

<details>
<summary>Expand full CLI flag examples</summary>

```bash
# Configure SQLCipher keys (global, per-shard, memory dump scan, or HMAC check)
wechat-localmind key --set "<64-hex-key>"
wechat-localmind key --set "contact.db:<64-hex-key>"
wechat-localmind key --scan-dump ./wechat_memory.dump --verify

# Configure self identity anchor
wechat-localmind config set-identity --name "Alex" --wxid "wxid_example123456"

# Switch to whitelist mode and allow a group
wechat-localmind config set-mode whitelist
wechat-localmind config allow "88888888@chatroom"

# Fetch last 12h with ack folding disabled and save to file
wechat-localmind fetch --chats ALL_ALLOWED --since 12h --no-fold --limit 200 -o ./digest.md
```

</details>

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

Both macOS and Windows **WeChat 3.x** (`reserve=48`, `msg_*.db` / `MSG*.db` + `MicroMsg.db`) and **WeChat 4.x WCDB** (`reserve=80`, `xwechat_files/db_storage/message_*.db` + `contact.db`) database layouts are supported. `wechat-localmind` auto-detects shard layouts and `zstd`-compressed payloads and merges them into a unified read-only snapshot.

</details>

<details>
<summary><strong>2. Is there any risk of account ban or interfering with WeChat?</strong></summary>

**Zero ban risk.** `wechat-localmind` operates strictly on copied offline snapshots, never injects into or modifies the running WeChat client, makes zero network calls, and contains no message-sending APIs.

</details>

<details>
<summary><strong>3. How are 1-on-1 private DMs (<code>wxid_*</code>) physically blocked?</strong></summary>

A triple-layer guard across shard extraction (`snapshot.py`), SQL queries (`db.py` enforcing `chat_id LIKE '%@chatroom'`), and policy/media validation (`config.py`, `media.py`) rejects any `wxid_*` private chat or `gh_*` official account with `EXIT_POLICY_DENIED` (`3`).

</details>

<details>
<summary><strong>4. How does it achieve zero third-party pip dependencies on macOS? What about Linux/Windows?</strong></summary>

On macOS it calls native `CommonCrypto` (`CCCrypt`) and `libzstd.dylib` via `ctypes` (preferring Python 3.14+ built-in `compression.zstd`) alongside stdlib `hashlib.pbkdf2_hmac`, delivering AES-256-CBC decryption without `pysqlcipher3`. On Linux or non-macOS systems without `CommonCrypto`, installing `cryptography` or `PyCryptodome` supplies the AES-256-CBC backend.

</details>

<details>
<summary><strong>5. How are encrypted <code>.dat</code> images decrypted?</strong></summary>

`export-media` (and `wechat_export_media`) derives the per-file single-byte XOR key from known image magic headers (`JPG`, `PNG`+`IHDR`, `GIF`, `WEBP`, `BMP`, `HEIC`) and decodes `.dat` files via vectorized bitwise operations into standard image formats.

</details>

<details>
<summary><strong>6. How do I integrate with Claude Desktop, Cursor, or Google Antigravity?</strong></summary>

Configure `wechat-localmind` with `args: ["mcp-serve"]` in your MCP client config (see [MCP Stdio Server](#mcp-stdio-server--tool-contracts)), or run `./bin/wechat-localmind install` to install the `SKILL.md` bundle.

</details>

<details>
<summary><strong>7. How do I configure or verify SQLCipher decryption keys?</strong></summary>

Provide your key via `WECHAT_LOCALMIND_KEY`, `wechat-localmind key --set "<64-hex-key>"`, `wechat-localmind key --import ./keys.json`, or `wechat-localmind key --scan-dump <file>` (which matches raw 32-byte keys against each shard's 16-byte Salt and Page 1 HMAC).

</details>

<details>
<summary><strong>8. What happens if <code>--after-id</code> exceeds the current database cursor?</strong></summary>

If `after_msg_id` is higher than `global_latest_msg_id` (e.g., after a database rebuild), `fetch` automatically falls back to the past 24 hours (`query_mode: "cursor_reset_fallback_24h"`) and emits a `CURSOR_RESET_FALLBACK` warning in `meta.warnings`.

</details>

<details>
<summary><strong>9. How do I purge local decrypted snapshots and exported media?</strong></summary>

Run `wechat-localmind clean --all` to remove both the unified plaintext SQLite snapshot (`WeChat_Unified_snapshot.sqlite`) and `snapshot/media/`, or pass `--media-only` to clear only exported media attachments.

</details>

---

## Development & Unit Tests

```bash
# Run all unit tests (uses synthetic SQLCipher, Protobuf/XML, and .dat fixtures)
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

<details>
<summary><strong>Search Intent & Query Mapping</strong></summary>

| User Intent / Problem | Common Queries (EN & ZH) | Relevant Capability & Section |
| :--- | :--- | :--- |
| Connect Claude / Cursor to WeChat groups | `wechat mcp server` · `AI Agent 读取微信群聊` · `微信 MCP Server` | `mcp-serve` & 4 read-only tools ([MCP Contracts](#mcp-stdio-server--tool-contracts)) |
| Decrypt WeChat 4.x WCDB & `zstd` offline | `wechat 4.x wcdb decrypt python` · `sqlcipher commoncrypto python` | `CommonCrypto` + `zstd` shard engine ([6-Pillar Architecture](#6-pillar-architecture)) |
| Enforce 1-on-1 DM exclusion for privacy | `read-only wechat chatroom exporter` · `禁止读取私聊 微信提取工具` | Triple-layer `@chatroom` guard, exit code `3` ([Compliance Notice](#legal--privacy-compliance-notice)) |
| Decode WeChat `.dat` XOR image attachments | `wechat dat xor image decoder` · `微信 .dat 图片解密` | `export-media` adaptive XOR decoder ([CLI Subcommands](#cli-subcommands)) |
| Build idempotent group digest pipelines | `wechat group chat extractor` · `微信聊天记录增量拉取` | Monotonic `--after-id` cursor + context buffer ([CLI Subcommands](#cli-subcommands)) |

</details>

<sub>**Keywords**: wechat-localmind · WeChat 3.x 4.x WCDB · SQLCipher CommonCrypto · zstd XML decompression · WeChat `.dat` XOR decoder · `@chatroom` group-only isolation · MCP Stdio Server · AI Agent CLI · local-first privacy · read-only chat extractor · 微信群聊提取 · 微信本地数据库解密 · 微信 MCP 服务器</sub>

---

## Architecture & Ecosystem · qilai (`qilaidav.com`)

This open-source project is engineered and maintained by **[qilai · qilaidav](https://qilaidav.com/en/)** — an independent systems engineering studio focused on **Deterministic Systems Engineering & High-Signal Technical Diagnostics**.

- **Related System / Pillar**: [Telegram Channel Ops & Persona Engine (OASH)](https://qilaidav.com/en/oash/) · [中文主站](https://qilaidav.com/oash/)
- **Sister Repository (`*-localmind`)**: [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind)
- **Studio Website**: [https://qilaidav.com/en/](https://qilaidav.com/en/) ([ZH](https://qilaidav.com))
- **Technical Notes & Field Reports**: [https://qilaidav.com/notes/](https://qilaidav.com/notes/)
- **Direct Engineering Contact**: [Telegram @BD8888](https://t.me/BD8888) · [Public Channel @AIKB8](https://t.me/AIKB8) · `hello@qilaidav.com`
