# Changelog

All notable changes to `wechat-localmind` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-13

### Added
- **Zero-Dependency SQLCipher 3/4 Decryption**: Native macOS `CommonCrypto` (`CCCrypt`) and `hashlib.pbkdf2_hmac` page-by-page AES-256-CBC verification and decryption supporting WeChat 4.x WCDB (`reserve=80`) and WeChat 3.x (`reserve=48`).
- **WeChat 4.x `zstd` & Multi-Shard Aggregation**: Automatic discovery and merging across `message_0.db ~ message_N.db` and `contact.db` with monotonic `msg_id` cursors and `Name2Id` resolution.
- **Triple-Layer `@chatroom` Privacy Guard**: Physical blocking of 1-on-1 `wxid_*` private chats and `gh_*` official accounts with configurable group blacklist/whitelist policies (`EXIT_POLICY_DENIED` = `3`).
- **4-Tier Real-Name Mapping & Noise Cleaning**: Resolves `remark > group_nickname > nick_name > masked wxid`, normalizes `@wxid_` mentions, parses 150-char `<refermsg>` quote chains, rich-media XML/Protobuf payloads, and strips system noise.
- **`.dat` XOR Image Decoder**: Adaptive magic-header XOR key derivation and vectorized decoding to `.jpg` / `.png` / `.gif` / `.webp` / `.heic`.
- **11 CLI Subcommands & 4 Read-Only MCP Stdio Tools**: Full JSON-RPC 2.0 MCP server (`mcp-serve`) and POSIX exit-code contracts.
- **Agent Skill Bundle & `llms.txt`**: Standard `skills/wechat-localmind/SKILL.md` and `llms.txt` documentation for AI agents.
