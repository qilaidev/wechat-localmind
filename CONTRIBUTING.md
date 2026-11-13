# Contributing to WeChat LocalMind

Thank you for your interest in contributing to `wechat-localmind`!

## Core Design Principles

All contributions must preserve the four core architectural guarantees:
1. **Zero Runtime Dependencies**: Keep `dependencies = []` in `pyproject.toml`. Use Python 3.10+ stdlib and native OS libraries (`CommonCrypto`, `hashlib.pbkdf2_hmac`, `compression.zstd` / `libzstd`, `sqlite3`).
2. **100% Read-Only & Offline**: Never add network calls, WeChat client hooks, or message-sending capabilities.
3. **Strict `@chatroom` Isolation**: Never weaken the triple-layer guard blocking `wxid_*` private chats and `gh_*` official accounts.
4. **English & Chinese Docs Parity**: Update `README.md`, `README.en.md`, and `llms.txt` together when CLI flags or MCP schemas change.

## Development Setup

```bash
git clone https://github.com/qilaidev/wechat-localmind.git
cd wechat-localmind

# Run unit tests (uses synthetic SQLCipher, Protobuf/XML, and .dat fixtures)
PYTHONPATH=src python3 -m unittest discover -s tests -v
# Or with pytest
PYTHONPATH=src python3 -m pytest

# Verify CLI entrypoint
./bin/wechat-localmind --help
```

## Pull Request Checklist

- [ ] All unit tests pass (`PYTHONPATH=src python3 -m unittest discover -s tests -v`).
- [ ] Zero third-party runtime dependencies introduced.
- [ ] No real `wxid_*`, personal names, local paths, or SQLCipher keys in code, tests, or docs.
- [ ] `README.md`, `README.en.md`, and `llms.txt` updated if CLI flags, MCP tools, or exit codes changed.
