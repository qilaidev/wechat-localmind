# Security & Privacy Policy

## Architectural Privacy & Safety Guarantees

`wechat-localmind` is designed with strict local-first, read-only, and privacy-preserving boundaries:

1. **100% Offline Read-Only Snapshot & Staging Hygiene**: All SQLCipher 3/4 (+ `.db-wal`) decryption and shard aggregation operations occur strictly on copied snapshots inside the local snapshot directory (`~/Library/Application Support/WeChat-LocalMind/snapshot` on macOS or `%LOCALAPPDATA%\WeChat-LocalMind\snapshot` on Windows). Temporary `snapshot/decrypted_raw/` shard copies are purged immediately after assembly, `0600`/`0700` file/directory permissions are enforced on POSIX, original WeChat databases are never modified, and zero network requests are ever made.
2. **Configurable Session Scope & Policy Guard**: Although `chat_scope = "all"` enables full-account extraction by default (1-on-1 direct chats, group chats, and official accounts), users can restrict accessible sessions at any time via `config set-scope <all|groups|direct|official>` (or `WECHAT_LOCALMIND_CHAT_SCOPE`) and `blacklist` / `whitelist` rules (`config block|unblock|allow|disallow`). Any out-of-scope or blocked access fails closed with `EXIT_POLICY_DENIED` (`3`, `CHAT_SCOPE_DENIED` or `CHAT_BLOCKED_BY_POLICY`).
3. **Cross-Chat Quote Isolation & Sandbox Path Guard**: Parent `<refermsg>` quote resolution strictly binds `chat_id` in SQL to prevent cross-chat message spoofing leaks, and media attachment resolution rejects any path escaping the local WeChat account sandbox (`INVALID_SANDBOX_PATH`, exit code `3`).
4. **No Message Sending**: There are no write-back, automation, or message-sending APIs anywhere in the codebase.

## Intended Use & Compliance

This tool is strictly intended for **personal local data backup and authorized offline research on the user's own device**. Users are responsible for complying with applicable local laws, workplace policies, and chat participant privacy expectations when processing exported chat data.

## Reporting a Vulnerability

If you discover a security vulnerability, policy bypass (e.g., circumventing `chat_scope` or blacklist/whitelist guards), or path-traversal issue in `wechat-localmind`:

- **Please do NOT open a public GitHub issue** with exploit details.
- Report the issue privately via [GitHub Security Advisories](https://github.com/qilaidev/wechat-localmind/security/advisories/new) or contact the maintainers through the repository's security tab.
- Include a minimal reproduction, affected version, and OS/Python environment details. We aim to acknowledge reports within 72 hours.
