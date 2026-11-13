"""Read-only SQLite query engine, 4-tier WeChat real-name resolver, and message pipeline."""

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
from .config import AppConfig
from .constants import (
    DEFAULT_FETCH_LIMIT,
    EXCLUDED_MSG_TYPES,
    MAX_FETCH_LIMIT,
    MEDIA_MSG_TYPES,
    SYNC_LAG_WARNING_SEC,
)
from .errors import ResourceNotFoundError, UsageError
from .protobuf_parser import parse_wechat_message_payload
from .snapshot import SnapshotState, epoch_to_local_iso, make_ro_sqlite_uri

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


class QueryEngine:
    """Executes read-only queries against the unified WeChat SQLite snapshot."""

    def __init__(self, config: AppConfig, snapshot: SnapshotState) -> None:
        self.config = config
        self.snapshot = snapshot
        self._conn: sqlite3.Connection | None = None
        self._global_contact_names: dict[str, str] | None = None
        self._group_member_names: dict[tuple[str, str], str] | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            uri = make_ro_sqlite_uri(self.snapshot.snapshot_db_path)
            self._conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
        return self._conn

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
        Tier 2: Group Display Nickname (`wx_group_members.group_nickname`)
        Tier 3: Contact Nickname (`wx_contacts.nick_name`)
        Tier 4: Fallback anonymized wxid (`微信群友(xxxx)`)
        """
        if self._global_contact_names is not None and self._group_member_names is not None:
            return self._global_contact_names, self._group_member_names

        conn = self._get_conn()
        cur = conn.cursor()

        remarks: dict[str, str] = {}
        nicknames: dict[str, str] = {}
        cur.execute("SELECT username, nick_name, remark FROM wx_contacts")
        for r in cur.fetchall():
            u = (r["username"] or "").strip()
            rem = clean_unicode_display_name(r["remark"])
            nick = clean_unicode_display_name(r["nick_name"])
            if rem:
                remarks[u] = rem
            if nick:
                nicknames[u] = nick

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

        # Store remarks separately so Tier 1 (remark) beats Tier 2 (group nickname)
        self._contact_remarks = remarks
        self._contact_nicknames = nicknames
        self._global_contact_names = global_names
        self._group_member_names = group_nicks
        return global_names, group_nicks

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

        if not sender_wxid:
            return "未知群友"

        self._ensure_name_maps()
        # Tier 1: Contact Remark
        if sender_wxid in self._contact_remarks:
            return disambiguate_punctuation_name(self._contact_remarks[sender_wxid], sender_wxid)
        # Tier 2: Group Display Name
        if self._group_member_names and (chat_id, sender_wxid) in self._group_member_names:
            return disambiguate_punctuation_name(self._group_member_names[(chat_id, sender_wxid)], sender_wxid)
        # Tier 3: Contact Nickname
        if sender_wxid in self._contact_nicknames:
            return disambiguate_punctuation_name(self._contact_nicknames[sender_wxid], sender_wxid)
        # Tier 4: Fallback
        return format_fallback_sender_name(sender_wxid)

    def list_groups(
        self,
        keyword: str | None = None,
        include_blocked: bool = False,
    ) -> dict[str, Any]:
        """List WeChat group sessions (@chatroom) with latest cursor, 24h message counts, and policy status."""
        conn = self._get_conn()
        cur = conn.cursor()
        since_24h_epoch = int(time.time() - 86400)

        excl_placeholders = ",".join("?" for _ in EXCLUDED_MSG_TYPES)
        sql = f"""
            SELECT
                g.chat_id,
                g.group_name,
                g.member_count,
                g.last_active_epoch,
                COALESCE(MAX(m.msg_id), 0) AS latest_msg_id,
                MAX(m.create_time) AS latest_msg_time,
                SUM(CASE WHEN m.create_time >= ? THEN 1 ELSE 0 END) AS msg_count_24h
            FROM wx_groups g
            LEFT JOIN wx_messages m
                ON m.chat_id = g.chat_id
                AND m.local_type NOT IN ({excl_placeholders})
            WHERE g.chat_id LIKE '%@chatroom'
            GROUP BY g.chat_id
            ORDER BY COALESCE(MAX(m.create_time), g.last_active_epoch, 0) DESC
        """
        cur.execute(sql, [since_24h_epoch, *EXCLUDED_MSG_TYPES])

        groups: list[dict[str, Any]] = []
        kw_lower = keyword.strip().lower() if keyword else None

        for row in cur.fetchall():
            chat_id = row["chat_id"]
            group_name = clean_unicode_display_name(row["group_name"]) or "未命名微信群"
            if kw_lower and kw_lower not in group_name.lower() and kw_lower not in chat_id.lower():
                continue

            allowed = self.config.is_chat_allowed(chat_id)
            if not allowed and not include_blocked:
                continue

            last_ts = row["latest_msg_time"] if row["latest_msg_time"] is not None else row["last_active_epoch"]
            groups.append(
                {
                    "chat_id": chat_id,
                    "group_name": group_name,
                    "allowed": allowed,
                    "latest_msg_id": int(row["latest_msg_id"] or 0),
                    "last_active_time": epoch_to_local_iso(last_ts),
                    "msg_count_24h": int(row["msg_count_24h"] or 0),
                    "member_count": int(row["member_count"] or 0),
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
                "filter_mode": self.config.filter_mode,
                "warnings": self.build_common_warnings(),
            },
            "groups": groups,
        }

    def _resolve_target_groups(self, chat_ids: list[str]) -> list[sqlite3.Row]:
        if not chat_ids:
            raise UsageError("At least one chat_id (@chatroom) or 'ALL_ALLOWED' must be specified via --chats.")

        conn = self._get_conn()
        cur = conn.cursor()

        if len(chat_ids) == 1 and chat_ids[0].upper() == "ALL_ALLOWED":
            cur.execute(
                """
                SELECT chat_id, COALESCE(group_name, '未命名微信群') AS group_name
                FROM wx_groups
                WHERE chat_id LIKE '%@chatroom'
                ORDER BY COALESCE(last_active_epoch, 0) DESC
                """
            )
            return [r for r in cur.fetchall() if self.config.is_chat_allowed(r["chat_id"])]

        for cid in chat_ids:
            self.config.assert_chat_allowed(cid)

        placeholders = ",".join("?" for _ in chat_ids)
        cur.execute(
            f"""
            SELECT chat_id, COALESCE(group_name, '未命名微信群') AS group_name
            FROM wx_groups
            WHERE chat_id IN ({placeholders}) AND chat_id LIKE '%@chatroom'
            """,
            chat_ids,
        )
        rows_by_id = {r["chat_id"]: r for r in cur.fetchall()}
        result: list[sqlite3.Row] = []
        for cid in chat_ids:
            if cid not in rows_by_id:
                raise ResourceNotFoundError(
                    f"WeChat group '{cid}' not found in local database.",
                    error_code="GROUP_NOT_FOUND",
                )
            result.append(rows_by_id[cid])
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
            cur.execute(
                """
                SELECT msg_id, sender_wxid, is_from_me, local_type, raw_content, compress_content, packed_info_data
                FROM wx_messages
                WHERE server_id = ?
                LIMIT 1
                """,
                (str(quoted_svrid),),
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
            p_sender_wxid = row["sender_wxid"] or p_parsed.sender_wxid_from_prefix
            p_sender_name = self.resolve_sender_name(bool(row["is_from_me"]), chat_id, p_sender_wxid)
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

        # Fallback to <refermsg> embedded snippet
        fb_sender = (
            self.resolve_sender_name(False, chat_id, quoted_sender_wxid)
            if quoted_sender_wxid
            else (clean_unicode_display_name(quoted_display_name) or "群成员")
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
        clean_rel = rel_path.lstrip("/\\")
        for base in (self.snapshot.media_root_dir, self.snapshot.account_dir):
            cand = (base / clean_rel).resolve()
            if cand.is_file() and cand.stat().st_size > 0:
                return True, cand, cand.stat().st_size
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

        # Skip revoked, pat ([拍一拍]), and system_event messages (100% filtered out from business stream)
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
        sender_name = self.resolve_sender_name(is_from_me, chat_id, sender_wxid)
        norm_type = parsed.normalized_type or "text"

        media_obj: dict[str, Any] | None = None
        if norm_type in MEDIA_MSG_TYPES or parsed.media_rel_path:
            avail, disk_path, disk_size = self._resolve_media_availability(parsed.media_rel_path)
            title = (
                parsed.media_title
                or (disk_path.name if disk_path else None)
                or (os.path.basename(parsed.media_rel_path) if parsed.media_rel_path else None)
                or norm_type
            )
            media_obj = {
                "media_type": "video" if norm_type == "micro_video" else norm_type,
                "title": title,
                "file_ext": parsed.media_ext,
                "file_size_bytes": parsed.media_size_bytes or disk_size,
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

        raw_ts = int(row["create_time"] or 0)
        msg_epoch = int(raw_ts // 1000) if raw_ts > 1e11 else raw_ts

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
    ) -> dict[str, Any]:
        """Fetch cleaned WeChat group messages by global cursor (--after-id) or time window (--since)."""
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

        groups = self._resolve_target_groups(chat_ids)
        is_explicit = not (len(chat_ids) == 1 and chat_ids[0].upper() == "ALL_ALLOWED")

        conn = self._get_conn()
        cur = conn.cursor()
        excl_placeholders = ",".join("?" for _ in EXCLUDED_MSG_TYPES)

        chats_result: list[dict[str, Any]] = []

        for grp in groups:
            chat_id = grp["chat_id"]
            group_name = clean_unicode_display_name(grp["group_name"]) or "未命名微信群"

            where_clauses = [
                "chat_id = ?",
                f"local_type NOT IN ({excl_placeholders})",
            ]
            params: list[Any] = [chat_id, *EXCLUDED_MSG_TYPES]

            if effective_after_id is not None:
                where_clauses.append("msg_id > ?")
                params.append(effective_after_id)
            if since_epoch is not None:
                where_clauses.append("create_time >= ?")
                params.append(int(since_epoch))
            if until_epoch is not None:
                where_clauses.append("create_time <= ?")
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

            if not new_messages and not is_explicit:
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
                    "group_name": group_name,
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
    ) -> dict[str, Any]:
        """Search messages across allowed WeChat groups (including zstd-compressed XML/text) with +/- N context."""
        q_clean = (query or "").strip()
        if not q_clean:
            raise UsageError("Search --query cannot be empty.")

        target_chats = chat_ids if chat_ids else ["ALL_ALLOWED"]
        groups = self._resolve_target_groups(target_chats)
        if not groups:
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
        grp_map = {g["chat_id"]: clean_unicode_display_name(g["group_name"]) or "未命名微信群" for g in groups}
        grp_placeholders = ",".join("?" for _ in grp_map)
        excl_placeholders = ",".join("?" for _ in EXCLUDED_MSG_TYPES)

        where_clauses = [
            f"chat_id IN ({grp_placeholders})",
            f"local_type NOT IN ({excl_placeholders})",
        ]
        params: list[Any] = [*grp_map.keys(), *EXCLUDED_MSG_TYPES]
        if since_epoch is not None:
            where_clauses.append("create_time >= ?")
            params.append(int(since_epoch))

        conn = self._get_conn()
        cur = conn.cursor()
        sql = f"""
            SELECT
                msg_id, server_id, chat_id, sender_wxid, is_from_me,
                local_type, create_time, raw_content, compress_content, packed_info_data
            FROM wx_messages
            WHERE {" AND ".join(where_clauses)}
            ORDER BY msg_id DESC
        """
        cur.execute(sql, params)

        q_lower = q_clean.lower()
        max_hits = max(1, min(int(limit), 500))
        ctx_n = max(0, int(context_window))
        matches: list[dict[str, Any]] = []

        for row in cur.fetchall():
            msg_obj = self._transform_message_row(row, is_context_buffer=False)
            if msg_obj is None:
                continue
            text_val = (msg_obj.get("text") or "").lower()
            media_title = ((msg_obj.get("media") or {}).get("title") or "").lower()
            if q_lower not in text_val and q_lower not in media_title:
                continue

            chat_id = row["chat_id"]
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
                    "group_name": grp_map[chat_id],
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
