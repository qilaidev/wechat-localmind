"""Read-only SQLite query engine, 4-tier WeChat real-name resolver, and full-account message pipeline."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .cleaner import (
    clean_unicode_display_name,
    disambiguate_punctuation_name,
    format_fallback_sender_name,
    is_short_ack_noise,
    replace_wechat_mentions,
    truncate_reply_snippet,
)
from .config import AppConfig, classify_wx_chat_id
from .constants import (
    BUILTIN_WECHAT_NAMES,
    DEFAULT_FETCH_LIMIT,
    EXCLUDED_MSG_TYPES,
    MAX_FETCH_LIMIT,
    MEDIA_MSG_TYPES,
    SYNC_LAG_WARNING_SEC,
    VALID_CHAT_TYPES,
)
from .errors import ResourceNotFoundError, UsageError
from .media import resolve_safe_sandbox_path
from .protobuf_parser import get_zstd_backend_name, parse_wechat_message_payload
from .snapshot import SnapshotState, epoch_to_local_iso, make_ro_sqlite_uri, normalize_epoch_sec

DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)


def parse_since_to_epoch(since_str: str | None, now_epoch: float | None = None) -> float | None:
    """Parse relative duration ('30m', '12h', '7d') or ISO-8601 timestamp into Unix epoch seconds."""
    if not since_str:
        return None
    raw = since_str.strip()
    now = now_epoch if now_epoch is not None else time.time()

    m = DURATION_RE.match(raw)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        return now - (val * multipliers[unit])

    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except ValueError as exc:
        raise UsageError(
            f"Invalid time format '{since_str}'. Expected relative duration (e.g. '12h', '7d') "
            "or ISO-8601 timestamp (e.g. '2026-11-07T09:00:00')."
        ) from exc


def normalize_chat_type_filter(chat_type: str | None) -> str:
    if not chat_type:
        return "all"
    ct = chat_type.strip().lower()
    if ct == "groups":
        ct = "group"
    if ct not in VALID_CHAT_TYPES:
        raise UsageError(
            f"Invalid chat type filter '{chat_type}'. Expected one of {VALID_CHAT_TYPES}."
        )
    return ct


class QueryEngine:
    """Executes read-only queries against the unified WeChat SQLite snapshot across all chat sessions."""

    def __init__(self, config: AppConfig, snapshot: SnapshotState) -> None:
        self.config = config
        self.snapshot = snapshot
        self._conn: sqlite3.Connection | None = None
        self._global_contact_names: dict[str, str] | None = None
        self._group_member_names: dict[tuple[str, str], str] | None = None
        self._contact_remarks: dict[str, str] = {}
        self._contact_nicknames: dict[str, str] = {}
        self._wxid_to_alias: dict[str, str] = {}
        self._alias_to_wxid: dict[str, str] = {}
        self._has_wx_chats_table: bool | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            uri = make_ro_sqlite_uri(self.snapshot.snapshot_db_path)
            self._conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _check_has_wx_chats(self) -> bool:
        if self._has_wx_chats_table is None:
            cur = self._get_conn().cursor()
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wx_chats'")
            self._has_wx_chats_table = cur.fetchone() is not None
        return self._has_wx_chats_table

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> QueryEngine:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def build_common_warnings(self) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if self.snapshot.arch_version == "v4_wcdb" and get_zstd_backend_name() is None:
            warnings.append(
                {
                    "code": "ZSTD_BACKEND_MISSING",
                    "message": (
                        "当前 Python 环境缺少 Zstandard 解压后端（Python <3.14 且未安装 zstandard / libzstd），"
                        "WeChat 4.x 压缩富文本消息可能无法解码。"
                    ),
                }
            )
        if self.snapshot.db_latest_msg_epoch is not None:
            lag_sec = int(round(time.time() - self.snapshot.db_latest_msg_epoch))
            if lag_sec > SYNC_LAG_WARNING_SEC:
                hours = round(lag_sec / 3600.0, 1)
                warnings.append(
                    {
                        "code": "CLIENT_SYNC_LAG",
                        "lag_seconds": lag_sec,
                        "message": f"微信客户端本地数据库已超过 {hours} 小时未收到新消息，请确认微信客户端是否在线同步。",
                    }
                )
        return warnings

    def _ensure_name_maps(self) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
        """Build 4-tier real-name dictionaries:
        Tier 1: Contact Remark (`wx_contacts.remark`)
        Tier 2: Group Display Nickname (`wx_group_members.group_nickname`, in group chats)
        Tier 3: Contact Nickname (`wx_contacts.nick_name`) / Builtin Name
        Tier 4: Fallback anonymized wxid (`微信用户(xxxx)` / `公众号(xxxx)`)
        """
        if self._global_contact_names is not None and self._group_member_names is not None:
            return self._global_contact_names, self._group_member_names

        conn = self._get_conn()
        cur = conn.cursor()

        remarks: dict[str, str] = {}
        nicknames: dict[str, str] = {}
        wxid_to_alias: dict[str, str] = {}
        alias_to_wxid: dict[str, str] = {}

        cur.execute("PRAGMA table_info(wx_contacts)")
        c_cols = {r[1] for r in cur.fetchall()}
        alias_sel = "alias" if "alias" in c_cols else "NULL AS alias"
        cur.execute(f"SELECT username, nick_name, remark, {alias_sel} FROM wx_contacts")
        for r in cur.fetchall():
            u = (r["username"] or "").strip()
            if not u:
                continue
            rem = clean_unicode_display_name(r["remark"])
            nick = clean_unicode_display_name(r["nick_name"])
            al = (r["alias"] or "").strip()
            if rem:
                remarks[u] = rem
            if nick:
                nicknames[u] = nick
            if al:
                wxid_to_alias[u] = al
                alias_to_wxid[al] = u

        group_nicks: dict[tuple[str, str], str] = {}
        cur.execute("SELECT chat_id, member_wxid, group_nickname FROM wx_group_members")
        for r in cur.fetchall():
            cid = (r["chat_id"] or "").strip()
            mem = (r["member_wxid"] or "").strip()
            gn = clean_unicode_display_name(r["group_nickname"])
            if cid and mem and gn:
                group_nicks[(cid, mem)] = gn

        global_names: dict[str, str] = {}
        for u in set(remarks.keys()) | set(nicknames.keys()):
            raw = remarks.get(u) or nicknames.get(u) or ""
            global_names[u] = disambiguate_punctuation_name(raw, u)

        self._contact_remarks = remarks
        self._contact_nicknames = nicknames
        self._wxid_to_alias = wxid_to_alias
        self._alias_to_wxid = alias_to_wxid
        self._global_contact_names = global_names
        self._group_member_names = group_nicks
        return global_names, group_nicks

    def resolve_chat_name(self, chat_id: str, stored_name: str | None = None) -> str:
        """Resolve display title for any WeChat session (group, 1-on-1 direct, official, builtin)."""
        self._ensure_name_maps()
        ctype = classify_wx_chat_id(chat_id)
        if chat_id in self._contact_remarks:
            return disambiguate_punctuation_name(self._contact_remarks[chat_id], chat_id)
        clean_stored = clean_unicode_display_name(stored_name)
        if clean_stored and clean_stored not in ("未命名微信群", "未知联系人"):
            return disambiguate_punctuation_name(clean_stored, chat_id) if ctype != "group" else clean_stored
        if chat_id in self._contact_nicknames:
            return disambiguate_punctuation_name(self._contact_nicknames[chat_id], chat_id) if ctype != "group" else self._contact_nicknames[chat_id]
        if chat_id in BUILTIN_WECHAT_NAMES:
            return BUILTIN_WECHAT_NAMES[chat_id]
        if ctype == "group":
            return "未命名微信群"
        return format_fallback_sender_name(chat_id)

    def resolve_sender_name(
        self,
        is_from_me: bool,
        chat_id: str,
        sender_wxid: str | None,
    ) -> str:
        if is_from_me or (
            sender_wxid
            and (
                sender_wxid == self.snapshot.wxid
                or (self.config.identity.wxid and sender_wxid == self.config.identity.wxid)
            )
        ):
            return self.config.my_display_label

        eff_wxid = (sender_wxid or "").strip()
        if not eff_wxid and not AppConfig.is_group_chat_id(chat_id):
            eff_wxid = chat_id

        if not eff_wxid:
            return "未知联系人"

        self._ensure_name_maps()
        # Tier 1: Contact Remark
        if eff_wxid in self._contact_remarks:
            return disambiguate_punctuation_name(self._contact_remarks[eff_wxid], eff_wxid)
        # Tier 2: Group Display Name (when in a group chat)
        if self._group_member_names and (chat_id, eff_wxid) in self._group_member_names:
            return disambiguate_punctuation_name(self._group_member_names[(chat_id, eff_wxid)], eff_wxid)
        # Tier 3: Contact Nickname or Built-in Name
        if eff_wxid in self._contact_nicknames:
            return disambiguate_punctuation_name(self._contact_nicknames[eff_wxid], eff_wxid)
        if eff_wxid in BUILTIN_WECHAT_NAMES:
            return BUILTIN_WECHAT_NAMES[eff_wxid]
        # Tier 4: Fallback
        return format_fallback_sender_name(eff_wxid)

    def _query_all_raw_sessions(self) -> list[dict[str, Any]]:
        """Query all sessions with latest cursor and 24h stats across v3 (`wx_chats`) or legacy (`wx_groups`) snapshots."""
        self._ensure_name_maps()
        conn = self._get_conn()
        cur = conn.cursor()
        since_24h_epoch = int(time.time() - 86400)
        excl_placeholders = ",".join("?" for _ in EXCLUDED_MSG_TYPES)
        m_ctime_expr = (
            "(CASE WHEN m.create_time > 100000000000 THEN m.create_time / 1000 ELSE m.create_time END)"
            if self.snapshot.arch_version == "unified_sqlite"
            else "m.create_time"
        )

        if self._check_has_wx_chats():
            sql = f"""
                SELECT
                    c.chat_id,
                    c.chat_name,
                    c.chat_type,
                    c.member_count,
                    c.last_active_epoch,
                    COALESCE(MAX(m.msg_id), 0) AS latest_msg_id,
                    MAX({m_ctime_expr}) AS latest_msg_time,
                    SUM(CASE WHEN {m_ctime_expr} >= ? THEN 1 ELSE 0 END) AS msg_count_24h
                FROM wx_chats c
                LEFT JOIN wx_messages m
                    ON m.chat_id = c.chat_id
                    AND m.local_type NOT IN ({excl_placeholders})
                GROUP BY c.chat_id
                ORDER BY COALESCE(MAX({m_ctime_expr}), c.last_active_epoch, 0) DESC
            """
            cur.execute(sql, [since_24h_epoch, *EXCLUDED_MSG_TYPES])
            rows = cur.fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                cid = (r["chat_id"] or "").strip()
                if not cid:
                    continue
                ctype = r["chat_type"] or classify_wx_chat_id(cid)
                out.append(
                    {
                        "chat_id": cid,
                        "chat_name": self.resolve_chat_name(cid, r["chat_name"]),
                        "chat_type": ctype,
                        "member_count": int(r["member_count"] or 0),
                        "latest_msg_id": int(r["latest_msg_id"] or 0),
                        "last_ts": r["latest_msg_time"] if r["latest_msg_time"] is not None else r["last_active_epoch"],
                        "msg_count_24h": int(r["msg_count_24h"] or 0),
                    }
                )
            return out

        # Fallback for external legacy `WeChat_Unified.sqlite` files without `wx_chats`
        sql_legacy = f"""
            WITH all_ids AS (
                SELECT chat_id, group_name AS raw_name, member_count, last_active_epoch FROM wx_groups
                UNION
                SELECT DISTINCT m.chat_id, NULL AS raw_name, 0 AS member_count, 0 AS last_active_epoch
                FROM wx_messages m
                WHERE m.chat_id NOT IN (SELECT chat_id FROM wx_groups)
            )
            SELECT
                a.chat_id,
                a.raw_name,
                a.member_count,
                a.last_active_epoch,
                COALESCE(MAX(m.msg_id), 0) AS latest_msg_id,
                MAX({m_ctime_expr}) AS latest_msg_time,
                SUM(CASE WHEN {m_ctime_expr} >= ? THEN 1 ELSE 0 END) AS msg_count_24h
            FROM all_ids a
            LEFT JOIN wx_messages m
                ON m.chat_id = a.chat_id
                AND m.local_type NOT IN ({excl_placeholders})
            GROUP BY a.chat_id
            ORDER BY COALESCE(MAX({m_ctime_expr}), a.last_active_epoch, 0) DESC
        """
        cur.execute(sql_legacy, [since_24h_epoch, *EXCLUDED_MSG_TYPES])
        out = []
        for r in cur.fetchall():
            cid = (r["chat_id"] or "").strip()
            if not cid:
                continue
            ctype = classify_wx_chat_id(cid)
            m_cnt = int(r["member_count"] or 0)
            if ctype == "direct" and m_cnt == 0:
                m_cnt = 1 if cid in ("filehelper", self.snapshot.wxid) else 2
            out.append(
                {
                    "chat_id": cid,
                    "chat_name": self.resolve_chat_name(cid, r["raw_name"]),
                    "chat_type": ctype,
                    "member_count": m_cnt,
                    "latest_msg_id": int(r["latest_msg_id"] or 0),
                    "last_ts": r["latest_msg_time"] if r["latest_msg_time"] is not None else r["last_active_epoch"],
                    "msg_count_24h": int(r["msg_count_24h"] or 0),
                }
            )
        return out

    def list_chats(
        self,
        keyword: str | None = None,
        include_blocked: bool = False,
        chat_type: str = "all",
    ) -> dict[str, Any]:
        """List WeChat sessions (1-on-1 direct, groups, official accounts) with cursors and 24h message counts."""
        type_filter = normalize_chat_type_filter(chat_type)
        raw_sessions = self._query_all_raw_sessions()
        kw_lower = keyword.strip().lower() if keyword else None

        chats: list[dict[str, Any]] = []
        for item in raw_sessions:
            chat_id = item["chat_id"]
            ctype = item["chat_type"]
            if type_filter != "all" and ctype != type_filter:
                continue

            chat_name = item["chat_name"]
            alias_id = self._wxid_to_alias.get(chat_id)
            if kw_lower:
                haystack = f"{chat_name} {chat_id} {alias_id or ''}".lower()
                if kw_lower not in haystack:
                    continue

            allowed = self.config.is_chat_allowed(chat_id, alias_id=alias_id)
            if not allowed and not include_blocked:
                continue

            chats.append(
                {
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "group_name": chat_name,
                    "chat_type": ctype,
                    "allowed": allowed,
                    "latest_msg_id": item["latest_msg_id"],
                    "last_active_time": epoch_to_local_iso(item["last_ts"]),
                    "msg_count_24h": item["msg_count_24h"],
                    "member_count": item["member_count"],
                }
            )

        return {
            "meta": {
                "snapshot_time": self.snapshot.snapshot_time_iso,
                "snapshot_age_sec": self.snapshot.snapshot_age_sec,
                "db_latest_msg_time": self.snapshot.db_latest_msg_time_iso,
                "global_latest_msg_id": self.snapshot.global_latest_msg_id,
                "account_wxid": self.snapshot.wxid,
                "arch_version": self.snapshot.arch_version,
                "chat_scope": self.config.chat_scope,
                "chat_type_filter": type_filter,
                "filter_mode": self.config.filter_mode,
                "warnings": self.build_common_warnings(),
            },
            "chats": chats,
        }

    def list_groups(
        self,
        keyword: str | None = None,
        include_blocked: bool = False,
    ) -> dict[str, Any]:
        """Backward-compatible helper listing WeChat group chats (@chatroom / @im.chatroom) with `groups` and `chats` keys."""
        res = self.list_chats(keyword=keyword, include_blocked=include_blocked, chat_type="group")
        res["groups"] = res["chats"]
        return res

    def _resolve_target_chats(self, chat_ids: list[str], chat_type: str = "all") -> list[dict[str, Any]]:
        if not chat_ids:
            raise UsageError(
                "At least one chat_id or 'ALL_ALLOWED' / 'ALL_DIRECT' / 'ALL_GROUPS' / 'ALL_OFFICIAL' must be specified via --chats."
            )

        type_filter = normalize_chat_type_filter(chat_type)
        raw_sessions = self._query_all_raw_sessions()
        by_id = {s["chat_id"]: s for s in raw_sessions}

        upper_single = chat_ids[0].strip().upper() if len(chat_ids) == 1 else ""
        if upper_single in ("ALL_ALLOWED", "ALL", "ALL_GROUPS", "ALL_DIRECT", "ALL_OFFICIAL"):
            effective_type = type_filter
            if upper_single == "ALL_GROUPS":
                effective_type = "group"
            elif upper_single == "ALL_DIRECT":
                effective_type = "direct"
            elif upper_single == "ALL_OFFICIAL":
                effective_type = "official"

            out_all: list[dict[str, Any]] = []
            for s in raw_sessions:
                cid = s["chat_id"]
                if effective_type != "all" and s["chat_type"] != effective_type:
                    continue
                if not self.config.is_chat_allowed(cid, alias_id=self._wxid_to_alias.get(cid)):
                    continue
                out_all.append(s)
            return out_all

        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_cid in chat_ids:
            req = raw_cid.strip()
            resolved_cid = self._alias_to_wxid.get(req, req)
            alias_id = self._wxid_to_alias.get(resolved_cid) or (req if resolved_cid != req else None)

            # Enforce policy first so out-of-scope or blacklisted chats raise Exit Code 3
            self.config.assert_chat_allowed(resolved_cid, alias_id=alias_id)

            sess = by_id.get(resolved_cid)
            if sess is None:
                # Check if contact exists in wx_contacts with zero messages yet
                if resolved_cid in self._contact_remarks or resolved_cid in self._contact_nicknames:
                    sess = {
                        "chat_id": resolved_cid,
                        "chat_name": self.resolve_chat_name(resolved_cid),
                        "chat_type": classify_wx_chat_id(resolved_cid),
                    }
                else:
                    raise ResourceNotFoundError(
                        f"WeChat chat '{req}' not found in local database.",
                        error_code="CHAT_NOT_FOUND",
                    )

            if type_filter != "all" and sess["chat_type"] != type_filter:
                continue

            if resolved_cid not in seen_ids:
                seen_ids.add(resolved_cid)
                result.append(sess)
        return result

    def _lookup_parent_reply(
        self,
        chat_id: str,
        quoted_svrid: str | None,
        quoted_sender_wxid: str | None,
        quoted_display_name: str | None,
        quoted_text_snippet: str | None,
    ) -> dict[str, Any] | None:
        if not quoted_svrid and not quoted_text_snippet:
            return None

        conn = self._get_conn()
        cur = conn.cursor()
        row = None
        if quoted_svrid:
            # Security boundary: bind `chat_id = ?` so spoofed `<refermsg><svrid>` cannot leak across chats
            cur.execute(
                """
                SELECT msg_id, sender_wxid, is_from_me, local_type, raw_content, compress_content, packed_info_data
                FROM wx_messages
                WHERE chat_id = ? AND server_id = ?
                LIMIT 1
                """,
                (chat_id, str(quoted_svrid)),
            )
            row = cur.fetchone()

        if row is not None:
            p_mid = int(row["msg_id"])
            p_parsed = parse_wechat_message_payload(
                int(row["local_type"] or 1),
                row["raw_content"],
                row["compress_content"],
                row["packed_info_data"],
            )
            p_is_me = bool(row["is_from_me"])
            p_sender_wxid = row["sender_wxid"] or p_parsed.sender_wxid_from_prefix
            if not p_sender_wxid and not p_is_me and not AppConfig.is_group_chat_id(chat_id):
                p_sender_wxid = chat_id
            p_sender_name = self.resolve_sender_name(p_is_me, chat_id, p_sender_wxid)
            if p_parsed.is_revoked:
                return {
                    "msg_id": p_mid,
                    "sender_name": p_sender_name,
                    "text_snippet": "[原消息已撤回]",
                    "status": "revoked",
                }
            snippet = p_parsed.text or p_parsed.media_title or quoted_text_snippet or ""
            return {
                "msg_id": p_mid,
                "sender_name": p_sender_name,
                "text_snippet": truncate_reply_snippet(snippet),
                "status": "ok",
            }

        fb_sender = (
            self.resolve_sender_name(False, chat_id, quoted_sender_wxid)
            if quoted_sender_wxid
            else (clean_unicode_display_name(quoted_display_name) or "历史消息")
        )
        if quoted_text_snippet:
            return {
                "msg_id": None,
                "sender_name": fb_sender,
                "text_snippet": truncate_reply_snippet(quoted_text_snippet),
                "status": "ok_from_refermsg",
            }
        return {
            "msg_id": None,
            "sender_name": fb_sender,
            "text_snippet": "[原消息不在本地库]",
            "status": "not_found",
        }

    def _resolve_media_availability(self, rel_path: str | None) -> tuple[bool, Path | None, int | None]:
        if not rel_path:
            return False, None, None
        found, within_sandbox = resolve_safe_sandbox_path(
            rel_path,
            self.snapshot.account_dir,
            self.snapshot.media_root_dir,
        )
        if not within_sandbox or found is None:
            return False, None, None
        try:
            return True, found, found.stat().st_size
        except OSError:
            return False, None, None

    def _transform_message_row(
        self,
        row: sqlite3.Row,
        is_context_buffer: bool,
    ) -> dict[str, Any] | None:
        msg_id = int(row["msg_id"])
        local_type = int(row["local_type"] or 1)
        if local_type in EXCLUDED_MSG_TYPES:
            return None

        chat_id = row["chat_id"]
        parsed = parse_wechat_message_payload(
            local_type,
            row["raw_content"],
            row["compress_content"],
            row["packed_info_data"],
        )

        if parsed.is_revoked or parsed.is_pat_message or parsed.normalized_type in ("revoked", "system_event", "pat"):
            return None

        sender_wxid = (row["sender_wxid"] or parsed.sender_wxid_from_prefix or "").strip()
        is_from_me = bool(row["is_from_me"]) or (
            bool(sender_wxid)
            and (
                sender_wxid == self.snapshot.wxid
                or (self.config.identity.wxid and sender_wxid == self.config.identity.wxid)
            )
        )
        if not is_from_me and not sender_wxid and not AppConfig.is_group_chat_id(chat_id):
            sender_wxid = chat_id

        sender_name = self.resolve_sender_name(is_from_me, chat_id, sender_wxid)
        norm_type = parsed.normalized_type or "text"

        media_obj: dict[str, Any] | None = None
        if norm_type in MEDIA_MSG_TYPES or parsed.media_rel_path:
            avail, disk_path, disk_size = self._resolve_media_availability(parsed.media_rel_path)
            within_sandbox = True
            if parsed.media_rel_path:
                _, within_sandbox = resolve_safe_sandbox_path(
                    parsed.media_rel_path,
                    self.snapshot.account_dir,
                    self.snapshot.media_root_dir,
                )
            title = (
                parsed.media_title
                or (disk_path.name if disk_path else None)
                or (os.path.basename(parsed.media_rel_path.replace("\\", "/")) if parsed.media_rel_path else None)
                or norm_type
            )
            media_obj = {
                "media_type": "video" if norm_type == "micro_video" else norm_type,
                "title": title,
                "file_ext": parsed.media_ext,
                "file_size_bytes": (parsed.media_size_bytes or disk_size) if within_sandbox else None,
                "duration_sec": parsed.media_duration_sec,
                "local_available": avail,
            }

        raw_text = (parsed.text or "").strip()
        if not raw_text and media_obj is None:
            return None

        cleaned_text, mentions_me = replace_wechat_mentions(
            raw_text,
            parsed.mentioned_wxids,
            self.config,
            auto_self_wxid=self.snapshot.wxid,
            name_resolver=lambda w: self.resolve_sender_name(False, chat_id, w),
        )

        reply_to = self._lookup_parent_reply(
            chat_id,
            parsed.quoted_svrid,
            parsed.quoted_sender_wxid,
            parsed.quoted_display_name,
            parsed.quoted_text_snippet,
        )

        msg_epoch = normalize_epoch_sec(row["create_time"])

        ack_noise = is_short_ack_noise(
            cleaned_text,
            norm_type,
            has_reply=(reply_to is not None),
            has_media=(media_obj is not None),
            mentions_me=mentions_me,
        )

        return {
            "msg_id": msg_id,
            "server_id": row["server_id"] or None,
            "timestamp": epoch_to_local_iso(msg_epoch),
            "timestamp_epoch": msg_epoch,
            "sender_wxid": "me" if is_from_me else (sender_wxid or None),
            "sender_name": sender_name,
            "is_from_me": is_from_me,
            "is_context_buffer": is_context_buffer,
            "is_ack_noise": ack_noise,
            "msg_type": norm_type,
            "text": cleaned_text,
            "mentions_me": mentions_me,
            "reply_to": reply_to,
            "media": media_obj,
        }

    def fetch_messages(
        self,
        chat_ids: list[str],
        after_msg_id: int | None = None,
        since_str: str | None = None,
        until_str: str | None = None,
        context_buffer_count: int | None = None,
        limit_per_chat: int = DEFAULT_FETCH_LIMIT,
        chat_type: str = "all",
    ) -> dict[str, Any]:
        """Fetch cleaned WeChat messages across any allowed sessions by cursor (--after-id) or time window (--since)."""
        limit = max(1, min(int(limit_per_chat), MAX_FETCH_LIMIT))
        ctx_count = (
            self.config.cleaning.context_buffer_count
            if context_buffer_count is None
            else max(0, int(context_buffer_count))
        )

        warnings = self.build_common_warnings()
        now_epoch = time.time()
        query_mode = "time_window"
        effective_after_id: int | None = None
        since_epoch: float | None = None
        until_epoch: float | None = parse_since_to_epoch(until_str, now_epoch) if until_str else None

        if after_msg_id is not None:
            if after_msg_id < 0:
                raise UsageError("--after-id must be a non-negative integer.")
            if (
                self.snapshot.global_latest_msg_id > 0
                and after_msg_id > self.snapshot.global_latest_msg_id
            ):
                query_mode = "cursor_reset_fallback_24h"
                since_epoch = now_epoch - 86400
                warnings.append(
                    {
                        "code": "CURSOR_RESET_FALLBACK",
                        "request_after_msg_id": after_msg_id,
                        "global_latest_msg_id": self.snapshot.global_latest_msg_id,
                        "message": (
                            f"传入的游标 after_msg_id ({after_msg_id}) 大于当前数据库最大消息 ID "
                            f"({self.snapshot.global_latest_msg_id})，已自动降级拉取过去 24 小时消息。"
                        ),
                    }
                )
            else:
                query_mode = "incremental_cursor"
                effective_after_id = after_msg_id
        else:
            since_epoch = parse_since_to_epoch(since_str or "24h", now_epoch)

        target_sessions = self._resolve_target_chats(chat_ids, chat_type=chat_type)
        is_wildcard = (
            len(chat_ids) == 1
            and chat_ids[0].strip().upper() in ("ALL_ALLOWED", "ALL", "ALL_GROUPS", "ALL_DIRECT", "ALL_OFFICIAL")
        )

        conn = self._get_conn()
        cur = conn.cursor()
        excl_placeholders = ",".join("?" for _ in EXCLUDED_MSG_TYPES)

        chats_result: list[dict[str, Any]] = []

        for sess in target_sessions:
            chat_id = sess["chat_id"]
            chat_name = sess["chat_name"]
            sess_type = sess["chat_type"]

            where_clauses = [
                "chat_id = ?",
                f"local_type NOT IN ({excl_placeholders})",
            ]
            params: list[Any] = [chat_id, *EXCLUDED_MSG_TYPES]

            ctime_col = (
                "(CASE WHEN create_time > 100000000000 THEN create_time / 1000 ELSE create_time END)"
                if self.snapshot.arch_version == "unified_sqlite"
                else "create_time"
            )
            if effective_after_id is not None:
                where_clauses.append("msg_id > ?")
                params.append(effective_after_id)
            if since_epoch is not None:
                where_clauses.append(f"{ctime_col} >= ?")
                params.append(int(since_epoch))
            if until_epoch is not None:
                where_clauses.append(f"{ctime_col} <= ?")
                params.append(int(until_epoch))

            order_dir = "ASC" if effective_after_id is not None else "DESC"
            sql = f"""
                SELECT
                    msg_id, server_id, chat_id, sender_wxid, is_from_me,
                    local_type, create_time, raw_content, compress_content, packed_info_data
                FROM wx_messages
                WHERE {" AND ".join(where_clauses)}
                ORDER BY msg_id {order_dir}
                LIMIT ?
            """
            params.append(limit + 1)
            cur.execute(sql, params)
            raw_rows = cur.fetchall()

            has_more = len(raw_rows) > limit
            if has_more:
                raw_rows = raw_rows[:limit]
            if order_dir == "DESC":
                raw_rows.reverse()

            new_messages: list[dict[str, Any]] = []
            for r in raw_rows:
                item = self._transform_message_row(r, is_context_buffer=False)
                if item is not None:
                    new_messages.append(item)

            if not new_messages and is_wildcard:
                continue

            buffer_messages: list[dict[str, Any]] = []
            if new_messages and ctx_count > 0:
                first_new_id = new_messages[0]["msg_id"]
                buf_sql = f"""
                    SELECT
                        msg_id, server_id, chat_id, sender_wxid, is_from_me,
                        local_type, create_time, raw_content, compress_content, packed_info_data
                    FROM wx_messages
                    WHERE chat_id = ?
                      AND local_type NOT IN ({excl_placeholders})
                      AND msg_id < ?
                    ORDER BY msg_id DESC
                    LIMIT ?
                """
                cur.execute(buf_sql, [chat_id, *EXCLUDED_MSG_TYPES, first_new_id, ctx_count * 2])
                for r in cur.fetchall():
                    item = self._transform_message_row(r, is_context_buffer=True)
                    if item is not None:
                        buffer_messages.append(item)
                    if len(buffer_messages) >= ctx_count:
                        break
                buffer_messages.reverse()

            cur.execute(
                f"SELECT COALESCE(MAX(msg_id), 0) FROM wx_messages WHERE chat_id = ? AND local_type NOT IN ({excl_placeholders})",
                [chat_id, *EXCLUDED_MSG_TYPES],
            )
            chat_overall_latest = int(cur.fetchone()[0] or 0)

            buffer_messages.sort(key=lambda m: (m["timestamp_epoch"], m["msg_id"]))
            new_messages.sort(key=lambda m: (m["timestamp_epoch"], m["msg_id"]))

            all_chat_msgs = buffer_messages + new_messages
            chat_min_id = min(m["msg_id"] for m in new_messages) if new_messages else None
            chat_max_id = max(m["msg_id"] for m in new_messages) if new_messages else chat_overall_latest

            chats_result.append(
                {
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "group_name": chat_name,
                    "chat_type": sess_type,
                    "chat_min_msg_id": chat_min_id,
                    "chat_latest_msg_id": chat_max_id,
                    "new_message_count": len(new_messages),
                    "context_buffer_count": len(buffer_messages),
                    "has_more": has_more,
                    "messages": all_chat_msgs,
                }
            )

        return {
            "meta": {
                "extracted_at": epoch_to_local_iso(now_epoch),
                "snapshot_time": self.snapshot.snapshot_time_iso,
                "snapshot_age_sec": self.snapshot.snapshot_age_sec,
                "query_mode": query_mode,
                "request_after_msg_id": after_msg_id,
                "global_latest_msg_id": self.snapshot.global_latest_msg_id,
                "account_wxid": self.snapshot.wxid,
                "chat_scope": self.config.chat_scope,
                "my_identity": self.config.my_display_label,
                "warnings": warnings,
            },
            "chats": chats_result,
        }

    def search_messages(
        self,
        query: str,
        chat_ids: list[str] | None = None,
        since_str: str | None = None,
        context_window: int = 3,
        limit: int = 50,
        chat_type: str = "all",
    ) -> dict[str, Any]:
        """Search messages across allowed WeChat sessions (including zstd-compressed XML/text) with +/- N context."""
        q_clean = (query or "").strip()
        if not q_clean:
            raise UsageError("Search --query cannot be empty.")

        target_chats = chat_ids if chat_ids else ["ALL_ALLOWED"]
        sessions = self._resolve_target_chats(target_chats, chat_type=chat_type)
        if not sessions:
            return {
                "meta": {
                    "query": q_clean,
                    "snapshot_time": self.snapshot.snapshot_time_iso,
                    "global_latest_msg_id": self.snapshot.global_latest_msg_id,
                    "match_count": 0,
                    "warnings": self.build_common_warnings(),
                },
                "matches": [],
            }

        since_epoch = parse_since_to_epoch(since_str, time.time()) if since_str else None
        sess_map = {s["chat_id"]: s for s in sessions}
        sess_placeholders = ",".join("?" for _ in sess_map)
        excl_placeholders = ",".join("?" for _ in EXCLUDED_MSG_TYPES)

        ctime_col = (
            "(CASE WHEN create_time > 100000000000 THEN create_time / 1000 ELSE create_time END)"
            if self.snapshot.arch_version == "unified_sqlite"
            else "create_time"
        )
        where_clauses = [
            f"chat_id IN ({sess_placeholders})",
            f"local_type NOT IN ({excl_placeholders})",
        ]
        params: list[Any] = [*sess_map.keys(), *EXCLUDED_MSG_TYPES]
        if since_epoch is not None:
            where_clauses.append(f"{ctime_col} >= ?")
            params.append(int(since_epoch))

        conn = self._get_conn()
        cur = conn.cursor()

        q_lower = q_clean.lower()
        max_hits = max(1, min(int(limit), 500))
        ctx_n = max(0, int(context_window))

        cur.execute("PRAGMA table_info(wx_messages)")
        msg_cols = {r[1] for r in cur.fetchall()}
        has_search_text = "search_text" in msg_cols

        has_fts = False
        if has_search_text and len(q_lower) >= 3:
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wx_messages_fts'")
            has_fts = cur.fetchone() is not None

        candidate_rows: list[sqlite3.Row] | None = None
        if has_fts:
            escaped_phrase = q_lower.replace('"', '""')
            fts_sql = f"""
                SELECT
                    msg_id, server_id, chat_id, sender_wxid, is_from_me,
                    local_type, create_time, raw_content, compress_content, packed_info_data
                FROM wx_messages
                WHERE msg_id IN (SELECT rowid FROM wx_messages_fts WHERE wx_messages_fts MATCH ?)
                  AND {" AND ".join(where_clauses)}
                ORDER BY msg_id DESC
                LIMIT ?
            """
            try:
                cur.execute(fts_sql, [f'"{escaped_phrase}"', *params, max_hits * 4])
                candidate_rows = cur.fetchall()
            except sqlite3.OperationalError:
                candidate_rows = None

        if candidate_rows is None and has_search_text:
            instr_sql = f"""
                SELECT
                    msg_id, server_id, chat_id, sender_wxid, is_from_me,
                    local_type, create_time, raw_content, compress_content, packed_info_data
                FROM wx_messages
                WHERE search_text IS NOT NULL
                  AND instr(search_text, ?) > 0
                  AND {" AND ".join(where_clauses)}
                ORDER BY msg_id DESC
                LIMIT ?
            """
            cur.execute(instr_sql, [q_lower, *params, max_hits * 4])
            candidate_rows = cur.fetchall()

        if candidate_rows is None:
            sql = f"""
                SELECT
                    msg_id, server_id, chat_id, sender_wxid, is_from_me,
                    local_type, create_time, raw_content, compress_content, packed_info_data
                FROM wx_messages
                WHERE {" AND ".join(where_clauses)}
                ORDER BY msg_id DESC
            """
            cur.execute(sql, params)
            candidate_rows = cur.fetchall()

        matches: list[dict[str, Any]] = []

        for row in candidate_rows:
            msg_obj = self._transform_message_row(row, is_context_buffer=False)
            if msg_obj is None:
                continue
            text_val = (msg_obj.get("text") or "").lower()
            media_title = ((msg_obj.get("media") or {}).get("title") or "").lower()
            if q_lower not in text_val and q_lower not in media_title:
                continue

            chat_id = row["chat_id"]
            sess_info = sess_map[chat_id]
            hit_id = msg_obj["msg_id"]
            before_msgs: list[dict[str, Any]] = []
            after_msgs: list[dict[str, Any]] = []

            if ctx_n > 0:
                cur2 = conn.cursor()
                cur2.execute(
                    f"""
                    SELECT msg_id, server_id, chat_id, sender_wxid, is_from_me,
                           local_type, create_time, raw_content, compress_content, packed_info_data
                    FROM wx_messages
                    WHERE chat_id = ? AND local_type NOT IN ({excl_placeholders}) AND msg_id < ?
                    ORDER BY msg_id DESC LIMIT ?
                    """,
                    [chat_id, *EXCLUDED_MSG_TYPES, hit_id, ctx_n],
                )
                for r in reversed(cur2.fetchall()):
                    b_obj = self._transform_message_row(r, is_context_buffer=True)
                    if b_obj:
                        before_msgs.append(b_obj)

                cur2.execute(
                    f"""
                    SELECT msg_id, server_id, chat_id, sender_wxid, is_from_me,
                           local_type, create_time, raw_content, compress_content, packed_info_data
                    FROM wx_messages
                    WHERE chat_id = ? AND local_type NOT IN ({excl_placeholders}) AND msg_id > ?
                    ORDER BY msg_id ASC LIMIT ?
                    """,
                    [chat_id, *EXCLUDED_MSG_TYPES, hit_id, ctx_n],
                )
                for r in cur2.fetchall():
                    a_obj = self._transform_message_row(r, is_context_buffer=True)
                    if a_obj:
                        after_msgs.append(a_obj)

            matches.append(
                {
                    "chat_id": chat_id,
                    "chat_name": sess_info["chat_name"],
                    "group_name": sess_info["chat_name"],
                    "chat_type": sess_info["chat_type"],
                    "matched_message": msg_obj,
                    "context_before": before_msgs,
                    "context_after": after_msgs,
                }
            )
            if len(matches) >= max_hits:
                break

        return {
            "meta": {
                "query": q_clean,
                "snapshot_time": self.snapshot.snapshot_time_iso,
                "global_latest_msg_id": self.snapshot.global_latest_msg_id,
                "match_count": len(matches),
                "warnings": self.build_common_warnings(),
            },
            "matches": matches,
        }
