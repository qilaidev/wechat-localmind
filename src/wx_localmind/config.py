"""Configuration management for WeChat-LocalMind (Identity anchor, Keys, Blacklist/Whitelist, Cleaning rules)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CONTEXT_BUFFER_COUNT,
    DEFAULT_KEYS_PATH,
    DEFAULT_LONG_AUDIO_THRESHOLD_SEC,
    DEFAULT_MAC_WECHAT4_FILES_DIR,
    DEFAULT_SNAPSHOT_DIR,
    DEFAULT_SNAPSHOT_TTL_SEC,
)
from .errors import PolicyDeniedError, UsageError


@dataclass
class IdentityConfig:
    name: str = "我"
    wxid: str = ""
    alias: str = ""


@dataclass
class CleaningConfig:
    snapshot_ttl_sec: int = DEFAULT_SNAPSHOT_TTL_SEC
    context_buffer_count: int = DEFAULT_CONTEXT_BUFFER_COUNT
    long_audio_threshold_sec: int = DEFAULT_LONG_AUDIO_THRESHOLD_SEC
    fold_noise_markdown: bool = True
    fold_noise_json: bool = False


@dataclass
class AppConfig:
    filter_mode: str = "blacklist"  # "blacklist" | "whitelist"
    blacklist_chats: list[str] = field(default_factory=list)
    whitelist_chats: list[str] = field(default_factory=list)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    cleaning: CleaningConfig = field(default_factory=CleaningConfig)
    wx_data_dir: str = str(DEFAULT_MAC_WECHAT4_FILES_DIR)
    account_dir: str = ""  # Optional explicit account dir (e.g. wxid_xxx_e948)
    raw_key_hex: str = ""  # Global or default 64-hex SQLCipher key
    db_keys: dict[str, str] = field(default_factory=dict)  # Per-db or salt->key mapping
    keys_file: str = str(DEFAULT_KEYS_PATH)
    snapshot_dir: str = str(DEFAULT_SNAPSHOT_DIR)

    @classmethod
    def load(cls, config_path: Path | None = None) -> AppConfig:
        path = config_path or DEFAULT_CONFIG_PATH
        env_data_dir = (
            os.environ.get("WECHAT_LOCALMIND_DATA_DIR")
            or os.environ.get("WX_LOCALMIND_DATA_DIR")
        )
        env_account_dir = (
            os.environ.get("WECHAT_LOCALMIND_ACCOUNT_DIR")
            or os.environ.get("WX_LOCALMIND_ACCOUNT_DIR")
        )
        env_snapshot_dir = (
            os.environ.get("WECHAT_LOCALMIND_SNAPSHOT_DIR")
            or os.environ.get("WX_LOCALMIND_SNAPSHOT_DIR")
        )
        env_raw_key = (
            os.environ.get("WECHAT_LOCALMIND_KEY")
            or os.environ.get("WX_LOCALMIND_KEY")
        )

        if not path.exists():
            cfg = cls()
        else:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise UsageError(f"Failed to parse config file at {path}: {exc}") from exc

            identity_raw = raw.get("identity", {})
            cleaning_raw = raw.get("cleaning", {})
            cfg = cls(
                filter_mode=raw.get("filter_mode", "blacklist"),
                blacklist_chats=list(raw.get("blacklist_chats", [])),
                whitelist_chats=list(raw.get("whitelist_chats", [])),
                identity=IdentityConfig(
                    name=identity_raw.get("name", "我") or "我",
                    wxid=str(identity_raw.get("wxid", "")).strip(),
                    alias=str(identity_raw.get("alias", "")).strip(),
                ),
                cleaning=CleaningConfig(
                    snapshot_ttl_sec=int(cleaning_raw.get("snapshot_ttl_sec", DEFAULT_SNAPSHOT_TTL_SEC)),
                    context_buffer_count=int(
                        cleaning_raw.get("context_buffer_count", DEFAULT_CONTEXT_BUFFER_COUNT)
                    ),
                    long_audio_threshold_sec=int(
                        cleaning_raw.get("long_audio_threshold_sec", DEFAULT_LONG_AUDIO_THRESHOLD_SEC)
                    ),
                    fold_noise_markdown=bool(cleaning_raw.get("fold_noise_markdown", True)),
                    fold_noise_json=bool(cleaning_raw.get("fold_noise_json", False)),
                ),
                wx_data_dir=raw.get("wx_data_dir", str(DEFAULT_MAC_WECHAT4_FILES_DIR)),
                account_dir=raw.get("account_dir", ""),
                raw_key_hex=raw.get("raw_key_hex", ""),
                db_keys=dict(raw.get("db_keys", {})),
                keys_file=raw.get("keys_file", str(DEFAULT_KEYS_PATH)),
                snapshot_dir=raw.get("snapshot_dir", str(DEFAULT_SNAPSHOT_DIR)),
            )

        if env_data_dir:
            cfg.wx_data_dir = env_data_dir
        if env_account_dir:
            cfg.account_dir = env_account_dir
        if env_snapshot_dir:
            cfg.snapshot_dir = env_snapshot_dir
        if env_raw_key:
            cfg.raw_key_hex = env_raw_key.strip()

        # Also merge keys from keys_file if it exists
        keys_path = Path(os.path.expanduser(cfg.keys_file))
        if keys_path.is_file():
            try:
                ext_keys = json.loads(keys_path.read_text(encoding="utf-8"))
                if isinstance(ext_keys, dict):
                    for k, v in ext_keys.items():
                        if isinstance(v, str) and v.strip():
                            cfg.db_keys.setdefault(k, v.strip())
                        elif isinstance(v, dict) and ("key" in v or "enc_key" in v):
                            val = str(v.get("key") or v.get("enc_key") or "").strip()
                            if val:
                                cfg.db_keys.setdefault(k, val)
            except Exception:
                pass

        return cfg

    def save(self, config_path: Path | None = None) -> Path:
        path = config_path or DEFAULT_CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return path

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def my_display_label(self) -> str:
        name = (self.identity.name or "").strip()
        if not name or name == "我":
            return "我"
        return f"我({name})"

    @staticmethod
    def is_group_chat_id(chat_id: str) -> bool:
        return bool(chat_id and chat_id.endswith("@chatroom"))

    def is_chat_allowed(self, chat_id: str) -> bool:
        """Strictly enforce 100% private chat blocking and group blacklist/whitelist."""
        if not self.is_group_chat_id(chat_id):
            return False
        if self.filter_mode == "whitelist":
            return chat_id in self.whitelist_chats
        return chat_id not in self.blacklist_chats

    def assert_chat_allowed(self, chat_id: str) -> None:
        """Raise PolicyDeniedError (Exit Code 3) if session is private or blocked."""
        if not self.is_group_chat_id(chat_id):
            raise PolicyDeniedError(
                f"Access denied: '{chat_id}' is not a WeChat group chatroom (@chatroom). "
                "1-on-1 private chats (wxid_*) and official accounts (gh_*) are strictly blocked.",
                error_code="PRIVATE_SESSION_BLOCKED",
            )
        if not self.is_chat_allowed(chat_id):
            raise PolicyDeniedError(
                f"Access denied: group '{chat_id}' is blocked by current {self.filter_mode} policy.",
                error_code="GROUP_BLOCKED_BY_POLICY",
            )
