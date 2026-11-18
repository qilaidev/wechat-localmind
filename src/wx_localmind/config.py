"""Configuration management for WeChat-LocalMind (Identity anchor, Keys, Chat Scope, Blacklist/Whitelist, Cleaning rules)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    DEFAULT_APP_SUPPORT_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_CONTEXT_BUFFER_COUNT,
    DEFAULT_KEYS_PATH,
    DEFAULT_LONG_AUDIO_THRESHOLD_SEC,
    DEFAULT_SNAPSHOT_DIR,
    DEFAULT_SNAPSHOT_TTL_SEC,
    DEFAULT_WX_DATA_DIR,
    OFFICIAL_SYSTEM_IDS,
    VALID_CHAT_SCOPES,
)
from .errors import PolicyDeniedError, UsageError


def classify_wx_chat_id(chat_id: str | None) -> str:
    """Classify a WeChat username/talker ID into 'group', 'official', 'direct', or 'unknown'."""
    if not chat_id:
        return "unknown"
    s = chat_id.strip()
    if not s:
        return "unknown"
    if s.endswith("@chatroom") or s.endswith("@im.chatroom"):
        return "group"
    if s.startswith("gh_") or s in OFFICIAL_SYSTEM_IDS:
        return "official"
    return "direct"


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
    chat_scope: str = "all"          # "all" | "groups" | "direct" | "official"
    filter_mode: str = "blacklist"   # "blacklist" | "whitelist"
    blacklist_chats: list[str] = field(default_factory=list)
    whitelist_chats: list[str] = field(default_factory=list)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    cleaning: CleaningConfig = field(default_factory=CleaningConfig)
    wx_data_dir: str = str(DEFAULT_WX_DATA_DIR)
    account_dir: str = ""            # Optional explicit account dir (e.g. wxid_xxx_e948)
    raw_key_hex: str = ""            # Global or default 64-hex SQLCipher key
    db_keys: dict[str, str] = field(default_factory=dict)  # Per-db or salt->key mapping
    dat_aes_key: str = ""            # Optional 16-byte ASCII or 32-hex AES key for WeChat 4.x V1/V2 .dat images
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
        env_dat_key = (
            os.environ.get("WECHAT_LOCALMIND_DAT_KEY")
            or os.environ.get("WX_LOCALMIND_DAT_KEY")
        )
        env_scope = (
            os.environ.get("WECHAT_LOCALMIND_CHAT_SCOPE")
            or os.environ.get("WX_LOCALMIND_CHAT_SCOPE")
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
            raw_scope = str(raw.get("chat_scope", "all")).strip().lower() or "all"
            if raw_scope == "group":
                raw_scope = "groups"
            if raw_scope not in VALID_CHAT_SCOPES:
                raise UsageError(
                    f"Invalid chat_scope '{raw_scope}' in {path}. Expected one of {VALID_CHAT_SCOPES}."
                )
            cfg = cls(
                chat_scope=raw_scope,
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
                wx_data_dir=raw.get("wx_data_dir", str(DEFAULT_WX_DATA_DIR)),
                account_dir=raw.get("account_dir", ""),
                raw_key_hex=raw.get("raw_key_hex", ""),
                db_keys=dict(raw.get("db_keys", {})),
                dat_aes_key=str(raw.get("dat_aes_key", "")).strip(),
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
        if env_dat_key:
            cfg.dat_aes_key = env_dat_key.strip()
        if env_scope:
            norm_scope = env_scope.strip().lower()
            if norm_scope == "group":
                norm_scope = "groups"
            if norm_scope not in VALID_CHAT_SCOPES:
                raise UsageError(
                    f"Invalid WECHAT_LOCALMIND_CHAT_SCOPE '{env_scope}'. Expected one of {VALID_CHAT_SCOPES}."
                )
            cfg.chat_scope = norm_scope

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
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not parent_existed or path.parent == DEFAULT_APP_SUPPORT_DIR:
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        tmp_path.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
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
        return bool(chat_id and (chat_id.endswith("@chatroom") or chat_id.endswith("@im.chatroom")))

    @staticmethod
    def is_valid_chat_id(chat_id: str) -> bool:
        if not chat_id:
            return False
        s = chat_id.strip()
        return bool(s and "/" not in s and "\\" not in s and "\x00" not in s)

    def is_chat_in_scope(self, chat_id: str) -> bool:
        ctype = classify_wx_chat_id(chat_id)
        if ctype == "unknown":
            return False
        scope = (self.chat_scope or "all").lower()
        if scope == "all":
            return True
        if scope in ("groups", "group"):
            return ctype == "group"
        if scope == "direct":
            return ctype == "direct"
        if scope == "official":
            return ctype == "official"
        return True

    def is_chat_allowed(self, chat_id: str, alias_id: str | None = None) -> bool:
        """Check whether a WeChat chat session is allowed under `chat_scope` and blacklist/whitelist rules."""
        if not self.is_chat_in_scope(chat_id):
            return False
        candidates = {chat_id}
        if alias_id:
            candidates.add(alias_id)
        if self.filter_mode == "whitelist":
            return bool(candidates & set(self.whitelist_chats))
        return not bool(candidates & set(self.blacklist_chats))

    def assert_chat_allowed(self, chat_id: str, alias_id: str | None = None) -> None:
        """Raise PolicyDeniedError (Exit Code 3) if session is out of scope or blocked."""
        ctype = classify_wx_chat_id(chat_id)
        if not self.is_chat_in_scope(chat_id):
            raise PolicyDeniedError(
                f"Access denied: WeChat chat '{chat_id}' (type={ctype}) is outside configured chat_scope='{self.chat_scope}'.",
                error_code="CHAT_SCOPE_DENIED",
            )
        if not self.is_chat_allowed(chat_id, alias_id=alias_id):
            raise PolicyDeniedError(
                f"Access denied: WeChat chat '{chat_id}' is blocked by current {self.filter_mode} policy.",
                error_code="CHAT_BLOCKED_BY_POLICY",
            )
