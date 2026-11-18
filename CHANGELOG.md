# Changelog

All notable changes to `wechat-localmind` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-11-18

### Added
- **Full-Account Chat Support (`chat_scope = "all"` by default)**: Upgraded `wechat-localmind` (`SNAPSHOT_SCHEMA_VERSION = 3`) from group-only extraction to full-account macOS & Windows WeChat 3.x / 4.x extraction, covering 1-on-1 direct chats (`wxid_*`, custom WeChat `alias` lookup, `@openim`, `filehelper`), group chats (`@chatroom`, `@im.chatroom`), and official accounts / service feeds (`gh_*`, `notifymessage`, `weixin`).
- **Unified `wx_chats` Schema & `chats` / `wechat_list_chats`**: Added `wx_chats` table (`chat_id`, `chat_name`, `chat_type`, `member_count`, `last_active_epoch`) with backward-compatible `wx_groups` view, preserved all `wx_contacts`, and added `chats` (`list-chats`) CLI subcommand plus `--type` / `ALL_DIRECT` / `ALL_GROUPS` / `ALL_OFFICIAL` filters across `fetch` and `search`.
- **Configurable Scope & Filtering Policies**: Added `config set-scope <all|groups|direct|official>` (and `WECHAT_LOCALMIND_CHAT_SCOPE`), extending `config block|unblock|allow|disallow` to any valid WeChat `chat_id` (`CHAT_SCOPE_DENIED` / `CHAT_BLOCKED_BY_POLICY`, exit code `3`).
- **SQLCipher `.db-wal` Frame Replay & Persistent Incremental Watermarks**: Added encrypted `.db-wal` log merging during decryption and persistent `wx_shard_state` + `wx_table_watermarks` tracking so global `msg_id` cursors stay strictly monotonic across deletions, in-place recalls, and backdated syncs.
- **WeChat 4.x V1/V2 `.dat` AES+XOR Image Decoding**: Added hybrid AES-128-ECB + XOR `.dat` (`\x07\x08V1` / `V2`) decryption via `key --dat-key` / `WECHAT_LOCALMIND_DAT_KEY`, alongside tightened BMP/RIFF magic-header checks and HD non-thumbnail path priority.
- **FTS5 Trigram Search & Rich Payload Parsing**: Added materialized `search_text` with `wx_messages_fts` (`tokenize='trigram'`) + `instr()` fallback, `<recorditem>` merged chat history extraction, `<voicetrans>` transcript support, and anonymized sender fallbacks (`微信用户(xxxx)`, `公众号(xxxx)`, `文件传输助手`, `联系人(xxxx)`).
- **5 Read-Only MCP Stdio Tools**: Added `wechat_list_chats` alongside `wechat_list_groups`, `wechat_fetch_messages`, `wechat_search_messages`, and `wechat_export_media`, all annotated with `readOnlyHint: true` and hardened JSON-RPC 2.0 batch/error handling.

## [1.0.0] - 2026-11-13

### Added
- **Zero-Dependency SQLCipher 3/4 Decryption**: Native macOS `CommonCrypto` (`CCCrypt`) and `hashlib.pbkdf2_hmac` page-by-page AES-256-CBC verification and decryption supporting WeChat 4.x WCDB (`reserve=80`) and WeChat 3.x (`reserve=48`).
- **WeChat 4.x `zstd` & Multi-Shard Aggregation**: Automatic discovery and merging across `message_0.db ~ message_N.db` and `contact.db` with monotonic `msg_id` cursors and `Name2Id` resolution.
- **Triple-Layer `@chatroom` Privacy Guard**: Physical blocking of 1-on-1 `wxid_*` private chats and `gh_*` official accounts with configurable group blacklist/whitelist policies (`EXIT_POLICY_DENIED` = `3`).
- **4-Tier Real-Name Mapping & Noise Cleaning**: Resolves `remark > group_nickname > nick_name > masked wxid`, normalizes `@wxid_` mentions, parses 150-char `<refermsg>` quote chains, rich-media XML/Protobuf payloads, and strips system noise.
- **`.dat` XOR Image Decoder**: Adaptive magic-header XOR key derivation and vectorized decoding to `.jpg` / `.png` / `.gif` / `.webp` / `.heic`.
- **11 CLI Subcommands & 4 Read-Only MCP Stdio Tools**: Full JSON-RPC 2.0 MCP server (`mcp-serve`) and POSIX exit-code contracts.
- **Agent Skill Bundle & `llms.txt`**: Standard `skills/wechat-localmind/SKILL.md` and `llms.txt` documentation for AI agents.
