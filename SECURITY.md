# Security & Privacy Policy

## Architectural Privacy & Safety Guarantees

`wechat-localmind` is designed with strict local-first, read-only, and privacy-preserving boundaries:

1. **100% Offline Read-Only Snapshot**: All SQLCipher 3/4 decryption and shard aggregation operations occur strictly on copied snapshots inside the local snapshot directory (`~/Library/Application Support/WeChat-LocalMind/snapshot` on macOS or `%LOCALAPPDATA%\WeChat-LocalMind\snapshot` on Windows). It never modifies original WeChat databases, never injects into or alters the running WeChat client, and makes zero network requests.
2. **Triple-Layer `@chatroom` Privacy Guard**: Access to 1-on-1 private chats (`wxid_*`) and official accounts (`gh_*`) is physically blocked across three independent layers:
   - Shard extraction layer (`snapshot.py`)
   - SQL query layer (`db.py` enforcing `chat_id LIKE '%@chatroom'`)
   - Media export & CLI/MCP policy layer (`config.py`, `media.py`, exiting with `EXIT_POLICY_DENIED` = `3`)
3. **No Message Sending**: There are no write-back, automation, or message-sending APIs anywhere in the codebase.

## Intended Use & Compliance

This tool is strictly intended for **personal local data backup and authorized offline research on the user's own device**. Users are responsible for complying with applicable local laws, workplace policies, and chat participant privacy expectations when processing exported group chat data.

## Reporting a Vulnerability

If you discover a security vulnerability, privacy bypass (e.g., a way to read non-`@chatroom` chats), or path-traversal issue in `wechat-localmind`:

- **Please do NOT open a public GitHub issue** with exploit details.
- Report the issue privately via [GitHub Security Advisories](https://github.com/qilaidev/wechat-localmind/security/advisories/new) or contact the maintainers through the repository's security tab.
- Include a minimal reproduction, affected version, and OS/Python environment details. We aim to acknowledge reports within 72 hours.
