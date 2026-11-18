# WeChat LocalMind (`wechat-localmind`)

**WeChat LocalMind (`wechat-localmind`)** is a local-first, strictly read-only CLI and JSON-RPC 2.0 MCP Stdio Server that extracts and cleans **full-account WeChat 3.x and 4.x (WCDB) chat records** across macOS and Windows database layouts for AI Agents such as Claude Desktop, Cursor, and Google Antigravity. Out of the box (`chat_scope = "all"`), it covers **1-on-1 direct chats (`wxid_*` / custom WeChat aliases / `@openim` / `filehelper`), group chats (`@chatroom` / `@im.chatroom`), and official accounts / service feeds (`gh_*` / `notifymessage` / `weixin`)**, operating solely on offline SQLCipher snapshots (+ `.db-wal` replay) with native macOS `CommonCrypto` and `zstd` support, optional `chat_scope`/blacklist/whitelist controls, automatic 3.x XOR & 4.x V1/V2 `.dat` image decoding, and zero network requests.

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
> Together with its sister repository [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind) (macOS WhatsApp full-account local chat extraction layer) under `qilaidev`, both projects share five unified architectural principles:
> **100% Offline Read-Only Snapshot + Full-Account Coverage with Configurable Privacy Scope (`chat_scope` / Allow-Block Lists) + 4-Tier Real-Name Resolution + Persistent Monotonic Cursors + Dual CLI & MCP Delivery**.

---

## Entity Snapshot

| Attribute | Specification & Fact |
| :--- | :--- |
| **Identifier** | `wechat-localmind` (Python package `wx_localmind`, version `2.0.0`, snapshot schema `v3`) |
| **License** | [MIT License](./LICENSE) |
| **Runtime & Dependencies** | Python `>= 3.10`; **zero third-party pip packages on macOS** (native `CommonCrypto`, `libzstd.dylib` / Python 3.14 `compression.zstd`, and stdlib `sqlite3`); optional `cryptography` or `PyCryptodome` for AES on Linux / non-macOS |
| **Supported WeChat DBs** | macOS & Windows **WeChat 4.x WCDB** (`reserve=80`, `xwechat_files/db_storage/message_*.db` + `.db-wal`, `contact.db`, `session.db`, `zstd` XML) and **WeChat 3.x** (`reserve=48`, `msg_*.db` / `MSG*.db` + `MicroMsg.db`) |
| **Session Scope & Policy** | **Full-account by default (`chat_scope = "all"`)**: 1-on-1 DMs (`wxid_*`, aliases, `@openim`, `filehelper`), groups (`@chatroom`, `@im.chatroom`), and official accounts (`gh_*`, `notifymessage`); configurable via `config set-scope <all\|groups\|direct\|official>` and blacklist/whitelist (`EXIT_POLICY_DENIED` = `3`) |
| **Delivery Interfaces** | **12 POSIX CLI subcommands** (including `chats`/`list-chats` & `groups`/`list-groups`) + **5 read-only JSON-RPC 2.0 MCP Stdio tools** (`readOnlyHint: true`) + `SKILL.md` agent bundle |
| **Default Snapshot Path** | `~/Library/Application Support/WeChat-LocalMind/snapshot` (overridable via `WECHAT_LOCALMIND_SNAPSHOT_DIR`) |
| **Maintainer** | [`qilaidev`](https://github.com/qilaidev) · [qilai Engineering Studio (`qilaidav.com`)](https://qilaidav.com/en/) |

---

## Positioning: What It Is / What It Is Not

| Dimension | What It Is | What It Is NOT |
| :--- | :--- | :--- |
| **Execution** | **100% Offline Read-Only Snapshot** opened via `?mode=ro` with zero network requests | **NOT** a WeChat hook, runtime injector, or protocol bot |
| **Scope** | **Full-Account Chats + Configurable Policy**: reads DMs, groups, `filehelper`, and official accounts by default, or restricts via `chat_scope` & allow/block lists | **NEVER** bypasses configured scope or blacklist/whitelist rules (denied chats exit `3`) |
| **Mutation** | **Strictly Read-Only**: never modifies original DBs or sends messages | **NOT** an auto-responder, mass-messaging tool, or desktop RPA controller |
| **Key Boundary** | **User-Supplied Key Verification**: `--set`, `--import`, or `--scan-dump` matched against 16B Salt + HMAC, plus `--dat-key` for V1/V2 images | **NOT** an encryption-bypass or credential-stealing tool |
| **Delivery** | **CLI & MCP Stdio Server** emitting clean JSON / Markdown for AI Agents | **NOT** a standalone GUI app or cloud-hosted SaaS |

---

## Use Cases & Alternatives Comparison

### Common Workflows

| Use Case | Recommended Entry / Command | Outcome |
| :--- | :--- | :--- |
| **AI Agent Full-Account Daily Digest & Action Items** | MCP `wechat_fetch_messages` or `fetch --chats ALL_ALLOWED --since 24h` | Fetches allowed DMs, groups, and service feeds with 4-tier real names, 150-char quote chains, and ack folding |
| **Type-Filtered Extraction or Group-Only Lockdown** | `fetch --chats ALL_DIRECT` / `ALL_GROUPS` / `ALL_OFFICIAL` or `config set-scope groups` | Isolate direct messages, groups, or official accounts on demand with strict policy enforcement |
| **Idempotent Incremental Message Sync** | `fetch --chats ALL_ALLOWED --after-id <msg_id> --format json` | Persistent watermarks keep `msg_id` monotonic across deletions/recalls/backdated syncs with a 10-msg context buffer |
| **Cross-Chat FTS5 Search Across Text & Rich XML** | `search --query "invoice" --type all --context 3` or MCP `wechat_search_messages` | FTS5 trigram + `instr` search across `zstd` XML, `<recorditem>` forwards, `<voicetrans>` transcripts, and filenames |
| **Media & 3.x / 4.x `.dat` Image Archiving** | `export-media --msg-ids 15,16` or MCP `wechat_export_media` | Decodes 3.x single-byte XOR and 4.x V1/V2 AES-128-ECB+XOR `.dat` files into `.jpg`/`.png`/etc. |

### Architectural Comparison

> Honest architectural comparison across approaches — no fabricated benchmarks or telemetry claims.

| Capability / Boundary | **WeChat LocalMind** | Generic `sqlcipher` CLI + Manual SQL | Desktop GUI WeChat Exporters | WeChat Hooks / Protocol Bots |
| :--- | :---: | :---: | :---: | :---: |
| **Ban & Runtime Interference Risk** | Zero (offline snapshot copy, zero injection, zero network calls) | Zero (offline manual read) | Varies (some attach to memory) | High (runtime hooking or unofficial protocol) |
| **Full-Account + Configurable Policy Guard** | Full-account by default + `chat_scope` & allow/block enforcement (`EXIT_POLICY_DENIED = 3`) | Manual SQL table MD5 lookup & filtering | Usually full export without agent policy guards | Unbounded live access to all contacts |
| **WeChat 4.x WCDB (`reserve=80` + `.db-wal` + `zstd`)** | Built-in `.db-wal` replay + multi-shard merge + `zstd` decompression | Manual pragma, WAL handling, and `zstd` decoding per shard | Partial support in newer GUI tools | N/A |
| **Zero Pip Deps on macOS** | Native `CommonCrypto` + `libzstd` via `ctypes` | Requires compiling/installing `sqlcipher` binary | Requires heavy GUI runtime | Requires protocol/runtime packages |
| **3.x & 4.x V1/V2 `.dat` Image Auto-Decoding** | Built-in XOR magic-header derivation + 4.x V1/V2 AES+XOR decode | Not included | Usually supported | Live download only |
| **AI Agent Delivery (MCP + FTS5 + Cursor)** | 5 read-only MCP tools + 12 CLI commands + FTS5 + persistent watermarks | Raw SQL rows only | Human-oriented HTML/PDF/CSV | Event-driven bot APIs; no monotonic read-only cursor |
| **Sending Messages / Automation** | None (strictly read-only by design) | None | None | Yes |

---

## Legal & Privacy Compliance Notice

- **Intended Use**: Strictly designed for **personal local data backup and authorized offline research on the user's own device**. Users are responsible for complying with applicable laws and participant privacy expectations.
- **100% Read-Only Offline Snapshot & Staging Hygiene**: All decryption and incremental sync occur inside an isolated snapshot directory (`~/Library/Application Support/WeChat-LocalMind/snapshot` by default, overridable via `WECHAT_LOCALMIND_SNAPSHOT_DIR`). Temporary `snapshot/decrypted_raw/` shard staging is unconditionally purged after assembly and `0600`/`0700` file permissions are enforced. It never modifies original WeChat databases, never injects into the WeChat client, makes zero network calls, and never sends messages.
- **Configurable Privacy Boundaries & Cross-Chat Isolation**: Use `chat_scope` (`all` | `groups` | `direct` | `official`) and `blacklist` / `whitelist` policies to restrict accessible sessions. SQL quote lookups bind `chat_id` to block cross-chat `<refermsg>` spoofing, media exports validate paths inside the account sandbox, and policy violations fail closed with `EXIT_POLICY_DENIED` (`3`).

---

## 6-Pillar Architecture

1. **Native `CommonCrypto` SQLCipher 3 / 4 & `.db-wal` Offline Decryption**
   - On macOS, calls native `CommonCrypto` (`/usr/lib/libSystem.B.dylib` `CCCrypt`) and stdlib `hashlib.pbkdf2_hmac` for AES-256-CBC page verification, decryption, and uncommitted `.db-wal` frame replay without pip dependencies (automatically falling back to `cryptography` or `PyCryptodome` on Linux / non-macOS), supporting both WeChat 4.x WCDB (`reserve=80`) and WeChat 3.x (`reserve=48`).
2. **WeChat 4.x `zstd` Decompression, Persistent Watermarks & FTS5**
   - Uses Python 3.14+ built-in `compression.zstd`, `zstandard`, macOS `libzstd.dylib` (`ctypes`), or `zstd` CLI fallback; merges `message_0.db ~ message_N.db` (`Msg_<md5>` / `Chat_<md5>` / `MSG` tables and `Name2Id` mappings). Persistent `wx_shard_state` and `wx_table_watermarks` tables preserve monotonic `msg_id` cursors across deletions/recalls and keep `wx_messages_fts` (FTS5 trigram) synchronized.
3. **Full-Account Coverage with Configurable Privacy Boundaries**
   - Unified `wx_chats` table and backward-compatible `wx_groups` view index `direct` (`wxid_*`, aliases, `@openim`, `filehelper`), `group` (`@chatroom`, `@im.chatroom`), and `official` (`gh_*`, `notifymessage`, `weixin`) sessions with configurable `chat_scope`, blacklist/whitelist policies, and cross-chat/sandbox isolation.
4. **4-Tier Real-Name Mapping & `@wxid_` Normalization**
   - Joins `contact.db` and `chatroom_member` to resolve senders in priority order: **Contact Remark (`remark`) > Group Nickname (`group_nickname`) > WeChat Nickname (`nick_name`) / Built-in Name > Masked ID (`微信用户(xxxx)` / `公众号(xxxx)` / `联系人(xxxx)`)**, and rewrites `@wxid_xxx` / `\u2005` tokens into `@RealName` or `[@我(RealName)]`.
5. **XML / Protobuf Rich-Media & 150-Char Quote Chain Parser**
   - Zero-dependency Protobuf Wire and XML parsers reconstruct `<appmsg type="57"><refermsg>` **150-character parent quote chains**, merged history (`type=19` `<recorditem>`), voice transcripts (`<voicetrans>`), file cards (`type=6`), web links (`type=5`), mini-programs (`type=33/36`), Channels/live cards (`type=51/63`), and group announcements (`type=87`), while stripping revokes/pats/system noise and folding consecutive acks.
6. **WeChat 3.x XOR & 4.x V1/V2 AES+XOR `.dat` Image Restoration**
   - `export-media` pairs adaptive magic-header vectorized XOR key recovery with **WeChat 4.x `\x07\x08V1` / `\x07\x08V2` hybrid AES-128-ECB + XOR decryption** to restore encrypted `.dat` files into standard `.jpg`, `.png`, `.gif`, `.webp`, `.bmp`, or `.heic` images.

---

## Installation & Quickstart

Requirements: Python `>= 3.10` (**zero third-party pip dependencies on macOS**; on Linux or environments without macOS `CommonCrypto`, install `pip install cryptography` or `pycryptodome` for AES backends).

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

# 2. Import/set SQLCipher key, list all accessible chats, and fetch the last 24 hours
wechat-localmind chats --format md
wechat-localmind fetch --chats ALL_ALLOWED --since 24h --format md
```

---

## CLI Subcommands (12 Subcommands)

| Subcommand | Purpose | Example |
| :--- | :--- | :--- |
| `doctor` | Environment, WeChat 3.x/4.x DB discovery & key health check | `wechat-localmind doctor --format md` |
| `key` | Set/import SQLCipher keys, `--dat-key`, or scan a memory dump | `wechat-localmind key --import ./keys.json` |
| `decrypt` | Decrypt a single DB (`--src`/`--dst`) or rebuild unified snapshot | `wechat-localmind decrypt` |
| `config` | Configure identity anchor (`set-identity`), `set-scope` & policies | `wechat-localmind config set-scope all` |
| `chats` / `list-chats` | List accessible chats (`--type all\|direct\|group\|official`) & cursors | `wechat-localmind chats --type direct --format json` |
| `groups` / `list-groups` | Backward-compatible listing of `@chatroom` / `@im.chatroom` groups | `wechat-localmind groups --format json` |
| `fetch` | Fetch cleaned messages by cursor (`--after-id`) or `--since` | `wechat-localmind fetch --chats ALL_ALLOWED --after-id 200 --format json` |
| `search` | Search across allowed chats or by `--type` (including `zstd` XML) | `wechat-localmind search --query "invoice" --context 3 --format md` |
| `export-media` | Export attachments & auto-decode 3.x / 4.x `.dat` images | `wechat-localmind export-media --msg-ids 15,16 --format json` |
| `clean` | Clean exported media (`--media-only`) or destroy snapshot (`--all`) | `wechat-localmind clean --media-only` |
| `mcp-serve` | Start the read-only JSON-RPC 2.0 MCP Stdio Server (5 tools) | `wechat-localmind mcp-serve` |
| `install` | Install CLI symlink to `~/.local/bin` and optional `SKILL.md` | `./bin/wechat-localmind install` |

<details>
<summary>Expand full CLI flag examples</summary>

```bash
# Configure SQLCipher keys and WeChat 4.x V2 .dat AES key
wechat-localmind key --set "<64-hex-key>"
wechat-localmind key --set "contact.db:<64-hex-key>"
wechat-localmind key --scan-dump ./wechat_memory.dump --verify
wechat-localmind key --dat-key "<16-byte-ascii-or-32-hex>"

# Configure self identity anchor
wechat-localmind config set-identity --name "Alex" --wxid "wxid_example123456" --alias "alex_eng"

# Restrict scope to groups only, or keep 'all' and block/allow specific chats
wechat-localmind config set-scope groups
wechat-localmind config set-scope all
wechat-localmind config block "wxid_private_secret,88888888@chatroom"

# Switch to whitelist mode and allow specific DMs, groups, or filehelper
wechat-localmind config set-mode whitelist
wechat-localmind config allow "10001@chatroom,wxid_alice,filehelper"

# Fetch direct chats by alias or wildcard, disable ack folding, and save to file
wechat-localmind fetch --chats "alice88,filehelper" --since 24h --format md
wechat-localmind fetch --chats ALL_DIRECT --since 12h --no-fold --limit 200 -o ./dm_digest.md
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

### Exposed Read-Only MCP Tools (5 Tools, `readOnlyHint: true`)

| Tool | Required Args | Optional Args & Defaults | Description |
| :--- | :--- | :--- | :--- |
| `wechat_list_chats` | None | `chat_type` (`"all"` \| `"direct"` \| `"group"` \| `"official"`), `keyword` (`string`), `include_blocked` (`false`), `format` (`"json"` \| `"markdown"`), `fresh` (`false`) | List accessible WeChat DMs, groups, and official accounts with `chat_type`, member counts, 24h counts, and `latest_msg_id` |
| `wechat_list_groups` | None | `keyword` (`string`), `include_blocked` (`false`), `format` (`"json"` \| `"markdown"`), `fresh` (`false`) | Backward-compatible helper listing `@chatroom` / `@im.chatroom` group chats and `latest_msg_id` |
| `wechat_fetch_messages` | `chat_ids` (`string[]` of IDs/aliases or `["ALL_ALLOWED"]` / `["ALL_DIRECT"]` / `["ALL_GROUPS"]` / `["ALL_OFFICIAL"]`) | `chat_type` (`"all"`), `after_msg_id` (`int`), `since` (`"24h"`), `until` (`string`), `context_buffer` (`10`), `limit_per_chat` (`500`, max `3000`), `fold_noise` (`true`), `format` (`"markdown"` \| `"json"`), `fresh` (`false`) | Fetch cleaned messages across any allowed chat with 4-tier real names, `@mention` normalization, 150-char quote chains, and revoke filtering |
| `wechat_search_messages` | `query` (`string`) | `chat_ids` (`string[]`), `chat_type` (`"all"`), `since` (`string`), `context` (`3`), `limit` (`50`), `format` (`"markdown"` \| `"json"`), `fresh` (`false`) | Search text and attachment titles (including `zstd` XML & `<recorditem>`) across allowed chats with context window |
| `wechat_export_media` | `msg_ids` (`int[]`) | `fresh` (`false`) | Export attachments from allowed chats to `snapshot/media`, auto-decoding 3.x XOR & 4.x V1/V2 `.dat` images and returning absolute paths |

---

## Environment Variables & POSIX Exit Codes

- `WECHAT_LOCALMIND_DATA_DIR`: Override WeChat data root (`xwechat_files` or `WeChat Files`)
- `WECHAT_LOCALMIND_ACCOUNT_DIR`: Pin a specific WeChat account directory
- `WECHAT_LOCALMIND_SNAPSHOT_DIR`: Override snapshot & media export directory
- `WECHAT_LOCALMIND_KEY`: Global 64-char Hex SQLCipher key
- `WECHAT_LOCALMIND_DAT_KEY`: 16-byte ASCII or 32-char Hex AES key for WeChat 4.x V1/V2 `.dat` images
- `WECHAT_LOCALMIND_CHAT_SCOPE`: Override session scope (`all` | `groups` | `direct` | `official`)

| Exit Code | Constant | Meaning |
| :--- | :--- | :--- |
| `0` | `EXIT_SUCCESS` | Success |
| `1` | `EXIT_USAGE_ERROR` | Invalid CLI arguments, time window, or Hex key format |
| `2` | `EXIT_ENV_OR_PERM_ERROR` | WeChat directory unreadable, missing SQLCipher / V2 `.dat` key, or key mismatch |
| `3` | `EXIT_POLICY_DENIED` | Policy guard triggered (`CHAT_SCOPE_DENIED`, `CHAT_BLOCKED_BY_POLICY`, or `INVALID_SANDBOX_PATH`) |
| `4` | `EXIT_RESOURCE_NOT_FOUND` | Target `chat_id` / `msg_id` not found, or media not downloaded locally |
| `5` | `EXIT_INTERNAL_DB_ERROR` | Internal SQLite / SQLCipher parsing error |

---

## Frequently Asked Questions (FAQ)

<details>
<summary><strong>1. Which WeChat versions and chat types are supported? How do 3.x and 4.x WCDB differ?</strong></summary>

Both macOS and Windows **WeChat 3.x** (`reserve=48`, `msg_*.db` / `MSG*.db` + `MicroMsg.db`) and **WeChat 4.x WCDB** (`reserve=80`, `xwechat_files/db_storage/message_*.db` + `.db-wal` + `contact.db` + `session.db`) database layouts are supported. As of `v2.0.0`, `chat_scope = "all"` is enabled by default, covering **1-on-1 direct chats (`wxid_*`, aliases, `@openim`, `filehelper`), group chats (`@chatroom`, `@im.chatroom`), and official accounts / service feeds (`gh_*`, `notifymessage`, `weixin`)**.

</details>

<details>
<summary><strong>2. Is there any risk of account ban or interfering with WeChat?</strong></summary>

**Zero ban risk.** `wechat-localmind` operates strictly on copied offline snapshots, never injects into or modifies the running WeChat client, makes zero network calls, and contains no message-sending APIs.

</details>

<details>
<summary><strong>3. How can I restrict AI Agents to group chats only (or block specific 1-on-1 DMs)?</strong></summary>

Run `wechat-localmind config set-scope groups` (or set `WECHAT_LOCALMIND_CHAT_SCOPE=groups`) to restrict access exclusively to `@chatroom` / `@im.chatroom` groups—any direct chat or official account request will fail closed with `CHAT_SCOPE_DENIED` (exit code `3`). You can also block individual `wxid_*`, `gh_*`, or `@chatroom` IDs via `config block`, or switch to `config set-mode whitelist`.

</details>

<details>
<summary><strong>4. How does it achieve zero third-party pip dependencies on macOS? What about Linux/Windows?</strong></summary>

On macOS it calls native `CommonCrypto` (`CCCrypt`) and `libzstd.dylib` via `ctypes` (preferring Python 3.14+ built-in `compression.zstd`) alongside stdlib `hashlib.pbkdf2_hmac`, delivering AES-256-CBC (DB) and AES-128-ECB (V1/V2 `.dat`) decryption without `pysqlcipher3`. On Linux or non-macOS systems without `CommonCrypto`, installing `cryptography` or `PyCryptodome` supplies the AES backend.

</details>

<details>
<summary><strong>5. How are encrypted <code>.dat</code> images decrypted (including WeChat 4.x V1/V2)?</strong></summary>

`export-media` (and `wechat_export_media`) first checks for WeChat 4.x `\x07\x08V1` / `\x07\x08V2` hybrid AES-128-ECB + XOR headers using your configured `dat_aes_key` (`key --dat-key` or `WECHAT_LOCALMIND_DAT_KEY`). For 3.x XOR `.dat` files, it derives the per-file single-byte XOR key from strict image magic headers (`JPG`, `PNG`+`IHDR`, `GIF`, `WEBP`, `BMP`, `HEIC`) via vectorized bitwise operations.

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

Persistent `wx_table_watermarks` prevent `msg_id` drift across deletions, in-place recalls, and backdated syncs. If `after_msg_id` still exceeds `global_latest_msg_id` (e.g., after a full database wipe/rebuild), `fetch` automatically falls back to the past 24 hours (`query_mode: "cursor_reset_fallback_24h"`) and emits a `CURSOR_RESET_FALLBACK` warning in `meta.warnings`.

</details>

<details>
<summary><strong>9. How do I purge local decrypted snapshots and exported media?</strong></summary>

Run `wechat-localmind clean --all` to remove both the unified plaintext SQLite snapshot (`WeChat_Unified_snapshot.sqlite`) and `snapshot/media/`, or pass `--media-only` to clear only exported media attachments while preserving snapshot watermarks.

</details>

---

## Development & Unit Tests

```bash
# Run all unit tests (uses synthetic SQLCipher, WAL, Protobuf/XML, and .dat fixtures)
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

<details>
<summary><strong>Search Intent & Query Mapping</strong></summary>

| User Intent / Problem | Common Queries (EN & ZH) | Relevant Capability & Section |
| :--- | :--- | :--- |
| Connect Claude / Cursor to WeChat | `wechat mcp server` · `AI Agent 读取微信聊天记录` · `微信 MCP Server` | `mcp-serve` & 5 read-only tools ([MCP Contracts](#mcp-stdio-server--tool-contracts)) |
| Decrypt WeChat 4.x WCDB & `zstd` offline | `wechat 4.x wcdb decrypt python` · `sqlcipher commoncrypto python` | `CommonCrypto` + `.db-wal` + `zstd` engine ([6-Pillar Architecture](#6-pillar-architecture)) |
| Extract all WeChat chats with optional scope guards | `read-only wechat chat extractor` · `微信私聊群聊只读提取` | Full-account `chat_scope` & allow/block guard, exit code `3` ([Compliance Notice](#legal--privacy-compliance-notice)) |
| Decode WeChat `.dat` (3.x XOR & 4.x V1/V2) images | `wechat dat xor image decoder` · `WeChat 4.x V2 dat` · `微信 .dat 图片解密` | `export-media` XOR + AES decoder ([CLI Subcommands](#cli-subcommands-12-subcommands)) |
| Build idempotent chat digest pipelines | `wechat chat extractor --after-id` · `微信聊天记录增量拉取` | Persistent watermarks + `--after-id` cursor ([CLI Subcommands](#cli-subcommands-12-subcommands)) |

</details>

<sub>**Keywords**: wechat-localmind · WeChat 3.x 4.x WCDB · SQLCipher CommonCrypto · zstd XML decompression · WeChat `.dat` V1/V2 AES XOR decoder · FTS5 trigram search · full-account chat extraction · MCP Stdio Server · AI Agent CLI · local-first privacy · read-only chat extractor · 微信聊天提取 · 微信本地数据库解密 · 微信 MCP 服务器</sub>

---

## Architecture & Ecosystem · qilai (`qilaidav.com`)

This open-source project is engineered and maintained by **[qilai · qilaidav](https://qilaidav.com/en/)** — an independent systems engineering studio focused on **Deterministic Systems Engineering & High-Signal Technical Diagnostics**.

- **Related System / Pillar**: [Telegram Channel Ops & Persona Engine (OASH)](https://qilaidav.com/en/oash/) · [中文主站](https://qilaidav.com/oash/)
- **Sister Repository (`*-localmind`)**: [`whatsapp-localmind`](https://github.com/qilaidev/whatsapp-localmind)
- **Studio Website**: [https://qilaidav.com/en/](https://qilaidav.com/en/) ([ZH](https://qilaidav.com))
- **Technical Notes & Field Reports**: [https://qilaidav.com/notes/](https://qilaidav.com/notes/)
- **Direct Engineering Contact**: [Telegram @BD8888](https://t.me/BD8888) · [Public Channel @AIKB8](https://t.me/AIKB8) · `hello@qilaidav.com`
