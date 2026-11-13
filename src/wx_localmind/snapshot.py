"""Multi-platform WeChat 4.x / 3.x account discovery, SQLCipher decryption, and Unified Snapshot builder."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig
from .constants import CANDIDATE_WECHAT_ROOTS, DEFAULT_MAC_WECHAT4_FILES_DIR, SQLCIPHER_PAGE_SIZE
from .crypto import decrypt_sqlcipher_database, is_plaintext_sqlite
from .errors import EnvOrPermissionError, InternalDatabaseError
from .protobuf_parser import parse_wechat_message_payload

MSG_TABLE_RE = re.compile(r"^(Msg|Chat)_([0-9a-fA-F]{32})$", re.IGNORECASE)


@dataclass
class DiscoveredWeChatAccount:
    root_dir: Path
    account_dir: Path
    wxid: str
    arch_version: str  # "v4_wcdb" | "v3_sqlcipher" | "unified_sqlite"
    message_dbs: list[Path]
    contact_db: Path | None
    session_db: Path | None
    media_root_dir: Path


@dataclass
class SnapshotState:
    snapshot_db_path: Path
    account_dir: Path
    media_root_dir: Path
    wxid: str
    arch_version: str
    snapshot_time_iso: str
    snapshot_epoch: float
    snapshot_age_sec: int
    db_latest_msg_epoch: int | None
    db_latest_msg_time_iso: str | None
    global_latest_msg_id: int
    refreshed: bool
    backup_duration_ms: float


def epoch_to_local_iso(epoch_sec: float | int | None) -> str | None:
    if epoch_sec is None or epoch_sec <= 0:
        return None
    # Normalize milliseconds if timestamp > 1e11
    ts = float(epoch_sec)
    if ts > 1e11:
        ts = ts / 1000.0
    dt = datetime.fromtimestamp(ts).astimezone()
    return dt.isoformat(timespec="seconds")


def make_ro_sqlite_uri(db_path: Path) -> str:
    abs_path = str(db_path.resolve())
    return f"file:{quote(abs_path, safe='/')}?mode=ro"


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().lower()


def _probe_account_dir(acc_dir: Path, root_dir: Path) -> DiscoveredWeChatAccount | None:
    """Check if `acc_dir` is a valid WeChat 4.x (`db_storage`), 3.x (`Message`/`Msg`), or unified directory."""
    if not acc_dir.is_dir():
        return None

    # 0. Pre-unified or single-file test database
    unified_cand = acc_dir / "WeChat_Unified.sqlite"
    if unified_cand.is_file():
        return DiscoveredWeChatAccount(
            root_dir=root_dir,
            account_dir=acc_dir,
            wxid=acc_dir.name.split("_")[0] if acc_dir.name.startswith("wxid_") else acc_dir.name,
            arch_version="unified_sqlite",
            message_dbs=[unified_cand],
            contact_db=None,
            session_db=None,
            media_root_dir=acc_dir / "msg" if (acc_dir / "msg").is_dir() else acc_dir,
        )

    # 1. WeChat 4.x layout (macOS & Windows): <acc_dir>/db_storage/message/message_*.db
    db_storage = acc_dir / "db_storage"
    if db_storage.is_dir():
        msg_dir = db_storage / "message"
        msg_dbs = sorted(msg_dir.glob("message_*.db")) if msg_dir.is_dir() else []
        # Exclude message_fts.db and message_resource.db
        msg_dbs = [
            p for p in msg_dbs if re.match(r"^message_\d+\.db$", p.name, re.IGNORECASE)
        ]
        contact_db = db_storage / "contact" / "contact.db"
        session_db = db_storage / "session" / "session.db"
        if msg_dbs or contact_db.is_file():
            raw_name = acc_dir.name
            # Strip trailing 4-hex suffix on macOS 4.x (e.g. wxid_example123456_e948 -> wxid_example123456)
            m_wxid = re.match(r"^(wxid_[A-Za-z0-9_]+?)_[0-9a-fA-F]{4}$", raw_name)
            wxid = m_wxid.group(1) if m_wxid else raw_name
            media_root = acc_dir / "msg" if (acc_dir / "msg").is_dir() else acc_dir
            return DiscoveredWeChatAccount(
                root_dir=root_dir,
                account_dir=acc_dir,
                wxid=wxid,
                arch_version="v4_wcdb",
                message_dbs=msg_dbs,
                contact_db=contact_db if contact_db.is_file() else None,
                session_db=session_db if session_db.is_file() else None,
                media_root_dir=media_root,
            )

    # 2. WeChat 3.x macOS layout: <acc_dir>/Message/msg_*.db + Contact/wccontact_new2.db
    mac3_msg_dir = acc_dir / "Message"
    if mac3_msg_dir.is_dir():
        msg_dbs = sorted(mac3_msg_dir.glob("msg_*.db"))
        contact_db = acc_dir / "Contact" / "wccontact_new2.db"
        session_db = acc_dir / "Session" / "session_new.db"
        if msg_dbs or contact_db.is_file():
            return DiscoveredWeChatAccount(
                root_dir=root_dir,
                account_dir=acc_dir,
                wxid=acc_dir.name,
                arch_version="v3_sqlcipher",
                message_dbs=msg_dbs,
                contact_db=contact_db if contact_db.is_file() else None,
                session_db=session_db if session_db.is_file() else None,
                media_root_dir=acc_dir,
            )

    # 3. WeChat 3.x Windows layout: <acc_dir>/Msg/Multi/MSG*.db + <acc_dir>/Msg/MicroMsg.db
    win3_msg_dir = acc_dir / "Msg"
    if win3_msg_dir.is_dir():
        multi_dir = win3_msg_dir / "Multi"
        msg_dbs = sorted(multi_dir.glob("MSG*.db")) if multi_dir.is_dir() else []
        if not msg_dbs:
            msg_dbs = sorted(win3_msg_dir.glob("MSG*.db"))
        contact_db = win3_msg_dir / "MicroMsg.db"
        if msg_dbs or contact_db.is_file():
            media_root = acc_dir / "FileStorage" if (acc_dir / "FileStorage").is_dir() else acc_dir
            return DiscoveredWeChatAccount(
                root_dir=root_dir,
                account_dir=acc_dir,
                wxid=acc_dir.name,
                arch_version="v3_sqlcipher",
                message_dbs=msg_dbs,
                contact_db=contact_db if contact_db.is_file() else None,
                session_db=contact_db if contact_db.is_file() else None,
                media_root_dir=media_root,
            )

    return None


def discover_wechat_account(config: AppConfig) -> DiscoveredWeChatAccount | None:
    """Auto-discover the most recently active WeChat account directory across macOS/Windows 4.x & 3.x."""
    if config.account_dir:
        exp_acc = Path(os.path.expanduser(config.account_dir))
        found = _probe_account_dir(exp_acc, exp_acc.parent)
        if found:
            return found

    roots_to_check: list[Path] = []
    primary = Path(os.path.expanduser(config.wx_data_dir))
    roots_to_check.append(primary)

    if primary == DEFAULT_MAC_WECHAT4_FILES_DIR:
        for cand_str in CANDIDATE_WECHAT_ROOTS:
            p = Path(os.path.expanduser(cand_str))
            if p not in roots_to_check:
                roots_to_check.append(p)

    discovered_candidates: list[tuple[float, DiscoveredWeChatAccount]] = []

    for root in roots_to_check:
        try:
            if not root.exists():
                continue
        except PermissionError:
            continue

        # Check if `root` itself is directly an account dir
        direct = _probe_account_dir(root, root.parent)
        if direct:
            return direct

        # Scan child directories (1 or 2 levels deep for 3.x version subdirectories like 2.0b4.0.9/<hash>)
        try:
            children = list(root.iterdir())
        except Exception:
            continue

        for child in children:
            if not child.is_dir() or child.name in ("all_users", "Backup", "Applet", "WMPF"):
                continue
            acc = _probe_account_dir(child, root)
            if acc:
                mtime = max(
                    (db.stat().st_mtime for db in acc.message_dbs if db.exists()),
                    default=child.stat().st_mtime,
                )
                discovered_candidates.append((mtime, acc))
                continue

            # Probe 1 level deeper (for macOS WeChat 3.x versioned subfolder)
            try:
                for subchild in child.iterdir():
                    if subchild.is_dir():
                        sub_acc = _probe_account_dir(subchild, root)
                        if sub_acc:
                            mtime = max(
                                (db.stat().st_mtime for db in sub_acc.message_dbs if db.exists()),
                                default=subchild.stat().st_mtime,
                            )
                            discovered_candidates.append((mtime, sub_acc))
            except Exception:
                continue

    if not discovered_candidates:
        return None

    discovered_candidates.sort(key=lambda item: item[0], reverse=True)
    return discovered_candidates[0][1]


class SnapshotManager:
    """Manages SQLCipher decryption and unified read-only SQLite snapshot creation with POSIX flock."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.snapshot_dir = Path(os.path.expanduser(config.snapshot_dir))
        self.raw_decrypted_dir = self.snapshot_dir / "decrypted_raw"
        self.snapshot_db_path = self.snapshot_dir / "WeChat_Unified_snapshot.sqlite"
        self.meta_path = self.snapshot_dir / "snapshot_meta.json"
        self.lock_path = self.snapshot_dir / "snapshot.lock"
        self.media_dir = self.snapshot_dir / "media"

    def _collect_candidate_keys_for_db(self, db_path: Path) -> list[str]:
        """Gather candidate 64-hex / 96-hex keys for a specific DB file from config."""
        keys: list[str] = []
        # 1. Exact filename match or relative path match in db_keys
        for k_name, k_val in self.config.db_keys.items():
            if k_name == db_path.name or k_name.endswith(f"/{db_path.name}") or k_name.endswith(f"\\{db_path.name}"):
                if k_val not in keys:
                    keys.append(k_val)
        # 2. Global raw_key_hex
        if self.config.raw_key_hex and self.config.raw_key_hex not in keys:
            keys.append(self.config.raw_key_hex)
        # 3. Any other keys in db_keys dictionary
        for k_val in self.config.db_keys.values():
            if k_val not in keys:
                keys.append(k_val)
        return keys

    def check_source_access(self) -> dict[str, Any]:
        """Inspect local WeChat data directory, discovered account, and encryption status for `doctor`."""
        acc = discover_wechat_account(self.config)
        if acc is None:
            return {
                "wx_data_dir": self.config.wx_data_dir,
                "account_found": False,
                "source_db_readable": False,
                "error": (
                    f"No WeChat account directory found under '{self.config.wx_data_dir}'. "
                    "Ensure WeChat is installed and logged in, or set `--config` / `WECHAT_LOCALMIND_DATA_DIR`."
                ),
            }

        all_dbs = list(acc.message_dbs)
        if acc.contact_db:
            all_dbs.append(acc.contact_db)
        if acc.session_db and acc.session_db not in all_dbs:
            all_dbs.append(acc.session_db)

        encrypted_count = 0
        plaintext_count = 0
        key_verified_count = 0
        sample_salts: dict[str, str] = {}

        for db_p in all_dbs:
            if not db_p.is_file():
                continue
            if is_plaintext_sqlite(db_p):
                plaintext_count += 1
                key_verified_count += 1
            else:
                encrypted_count += 1
                try:
                    with open(db_p, "rb") as f:
                        p1 = f.read(SQLCIPHER_PAGE_SIZE)
                    sample_salts[db_p.name] = p1[:16].hex()
                    from .crypto import detect_cipher_version_and_key

                    cands = self._collect_candidate_keys_for_db(db_p)
                    if detect_cipher_version_and_key(p1, cands) is not None:
                        key_verified_count += 1
                except Exception:
                    pass

        ready_to_decrypt = len(all_dbs) > 0 and (plaintext_count == len(all_dbs) or key_verified_count > 0)
        err_msg = None
        if not all_dbs:
            err_msg = f"Account directory '{acc.account_dir}' contains no message or contact databases."
        elif not ready_to_decrypt:
            err_msg = (
                f"Found {encrypted_count} SQLCipher-encrypted WeChat database(s) for account '{acc.wxid}', "
                "but no matching decryption key is configured. Run `wechat-localmind key --set <64-hex-key>` "
                "or `wechat-localmind key --import <keys.json>`."
            )

        return {
            "wx_data_dir": str(acc.root_dir),
            "account_found": True,
            "account_dir": str(acc.account_dir),
            "wxid": acc.wxid,
            "arch_version": acc.arch_version,
            "message_db_count": len(acc.message_dbs),
            "contact_db_path": str(acc.contact_db) if acc.contact_db else None,
            "encrypted_db_count": encrypted_count,
            "plaintext_db_count": plaintext_count,
            "key_verified_db_count": key_verified_count,
            "db_salts": sample_salts,
            "source_db_readable": ready_to_decrypt,
            "error": err_msg,
        }

    def ensure_snapshot(self, force_fresh: bool = False) -> SnapshotState:
        """Ensure a fresh or valid cached unified SQLite snapshot exists using POSIX flock."""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        ttl_sec = max(0, self.config.cleaning.snapshot_ttl_sec)
        now = time.time()

        if not force_fresh and self._is_cache_valid(now, ttl_sec):
            return self._load_cached_state(now)

        with open(self.lock_path, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                now = time.time()
                if not force_fresh and self._is_cache_valid(now, max(1, ttl_sec)):
                    return self._load_cached_state(now)
                return self._build_unified_snapshot()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _is_cache_valid(self, now: float, ttl_sec: int) -> bool:
        if not self.snapshot_db_path.exists() or not self.meta_path.exists():
            return False
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            created_epoch = float(meta.get("snapshot_epoch", 0))
            return (now - created_epoch) <= ttl_sec
        except Exception:
            return False

    def _load_cached_state(self, now: float) -> SnapshotState:
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        created_epoch = float(meta["snapshot_epoch"])
        return SnapshotState(
            snapshot_db_path=self.snapshot_db_path,
            account_dir=Path(meta["account_dir"]),
            media_root_dir=Path(meta["media_root_dir"]),
            wxid=meta.get("wxid", ""),
            arch_version=meta.get("arch_version", "v4_wcdb"),
            snapshot_time_iso=meta["snapshot_time_iso"],
            snapshot_epoch=created_epoch,
            snapshot_age_sec=max(0, int(round(now - created_epoch))),
            db_latest_msg_epoch=meta.get("db_latest_msg_epoch"),
            db_latest_msg_time_iso=meta.get("db_latest_msg_time_iso"),
            global_latest_msg_id=int(meta.get("global_latest_msg_id", 0)),
            refreshed=False,
            backup_duration_ms=float(meta.get("backup_duration_ms", 0.0)),
        )

    def _decrypt_or_copy_source_db(self, src_db: Path) -> Path:
        """Decrypt or copy a source WeChat .db into `decrypted_raw/`."""
        self.raw_decrypted_dir.mkdir(parents=True, exist_ok=True)
        dst_db = self.raw_decrypted_dir / src_db.name
        if is_plaintext_sqlite(src_db):
            # Use SQLite online backup API to safely merge any active -wal file
            tmp_dst = self.raw_decrypted_dir / f"{src_db.name}.{os.getpid()}.tmp"
            src_conn = sqlite3.connect(make_ro_sqlite_uri(src_db), uri=True, timeout=3.0)
            dst_conn = sqlite3.connect(str(tmp_dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
            tmp_dst.replace(dst_db)
            return dst_db

        cands = self._collect_candidate_keys_for_db(src_db)
        decrypt_sqlcipher_database(src_db, dst_db, cands)
        return dst_db

    def _build_unified_snapshot(self) -> SnapshotState:
        acc = discover_wechat_account(self.config)
        if acc is None:
            raise EnvOrPermissionError(
                f"No WeChat account found under '{self.config.wx_data_dir}'.",
                error_code="ACCOUNT_NOT_FOUND",
            )

        t0 = time.perf_counter()
        tmp_unified = self.snapshot_dir / f"WeChat_Unified.{os.getpid()}.tmp.sqlite"
        if tmp_unified.exists():
            tmp_unified.unlink()

        # If source is already a pre-unified SQLite DB, back it up directly
        if acc.arch_version == "unified_sqlite" and acc.message_dbs:
            src_conn = sqlite3.connect(make_ro_sqlite_uri(acc.message_dbs[0]), uri=True)
            dst_conn = sqlite3.connect(str(tmp_unified))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
        else:
            self._assemble_unified_db_from_shards(acc, tmp_unified)

        tmp_unified.replace(self.snapshot_db_path)

        # Query global max msg_id and latest timestamp
        global_latest_msg_id = 0
        db_latest_msg_epoch: int | None = None
        conn = sqlite3.connect(make_ro_sqlite_uri(self.snapshot_db_path), uri=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(msg_id), 0), MAX(create_time) FROM wx_messages")
            row = cur.fetchone()
            if row:
                global_latest_msg_id = int(row[0] or 0)
                if row[1] is not None:
                    raw_ts = int(row[1])
                    db_latest_msg_epoch = int(raw_ts // 1000) if raw_ts > 1e11 else raw_ts
        finally:
            conn.close()

        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        now_epoch = time.time()
        now_iso = epoch_to_local_iso(now_epoch) or ""
        latest_msg_iso = epoch_to_local_iso(db_latest_msg_epoch)

        meta = {
            "snapshot_epoch": now_epoch,
            "snapshot_time_iso": now_iso,
            "account_dir": str(acc.account_dir),
            "media_root_dir": str(acc.media_root_dir),
            "wxid": acc.wxid,
            "arch_version": acc.arch_version,
            "db_latest_msg_epoch": db_latest_msg_epoch,
            "db_latest_msg_time_iso": latest_msg_iso,
            "global_latest_msg_id": global_latest_msg_id,
            "backup_duration_ms": duration_ms,
        }
        tmp_meta = self.snapshot_dir / f"snapshot_meta.{os.getpid()}.tmp.json"
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_meta.replace(self.meta_path)

        return SnapshotState(
            snapshot_db_path=self.snapshot_db_path,
            account_dir=acc.account_dir,
            media_root_dir=acc.media_root_dir,
            wxid=acc.wxid,
            arch_version=acc.arch_version,
            snapshot_time_iso=now_iso,
            snapshot_epoch=now_epoch,
            snapshot_age_sec=0,
            db_latest_msg_epoch=db_latest_msg_epoch,
            db_latest_msg_time_iso=latest_msg_iso,
            global_latest_msg_id=global_latest_msg_id,
            refreshed=True,
            backup_duration_ms=duration_ms,
        )

    def _assemble_unified_db_from_shards(self, acc: DiscoveredWeChatAccount, target_db: Path) -> None:
        """Decrypt WeChat 4.x / 3.x contact & message shards and normalize into unified SQLite tables."""
        u_conn = sqlite3.connect(str(target_db))
        u_cur = u_conn.cursor()
        u_cur.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE wx_contacts (
                username TEXT PRIMARY KEY,
                nick_name TEXT,
                remark TEXT,
                alias TEXT,
                is_group INTEGER DEFAULT 0
            );
            CREATE TABLE wx_group_members (
                chat_id TEXT,
                member_wxid TEXT,
                group_nickname TEXT,
                PRIMARY KEY (chat_id, member_wxid)
            );
            CREATE TABLE wx_groups (
                chat_id TEXT PRIMARY KEY,
                group_name TEXT,
                member_count INTEGER DEFAULT 0,
                last_active_epoch INTEGER DEFAULT 0
            );
            CREATE TABLE wx_messages (
                msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                shard_name TEXT,
                shard_local_id INTEGER,
                server_id TEXT,
                chat_id TEXT,
                sender_wxid TEXT,
                is_from_me INTEGER DEFAULT 0,
                local_type INTEGER,
                create_time INTEGER,
                raw_content TEXT,
                compress_content BLOB,
                packed_info_data BLOB
            );
            CREATE INDEX idx_wx_messages_chat_id ON wx_messages(chat_id, msg_id);
            CREATE INDEX idx_wx_messages_server_id ON wx_messages(server_id);
            CREATE INDEX idx_wx_messages_time ON wx_messages(chat_id, create_time);
            """
        )

        # Map md5(username) -> username for resolving `Msg_<md5>` / `Chat_<md5>` table names
        md5_to_chat_id: dict[str, str] = {}

        # 1. Ingest contact.db / wccontact_new2.db / MicroMsg.db
        try:
            if acc.contact_db and acc.contact_db.is_file():
                dec_contact = self._decrypt_or_copy_source_db(acc.contact_db)
                self._ingest_contact_db(dec_contact, u_cur, md5_to_chat_id)

            # 2. Ingest session.db if separate
            if acc.session_db and acc.session_db.is_file() and acc.session_db != acc.contact_db:
                try:
                    dec_sess = self._decrypt_or_copy_source_db(acc.session_db)
                    self._ingest_session_db(dec_sess, u_cur, md5_to_chat_id)
                except Exception:
                    pass

            # 3. Collect all raw messages across message shards, then insert sorted by (create_time, shard_local_id)
            # so that `msg_id` is strictly monotonic across all shards.
            collected_msgs: list[tuple[int, int, str, str, str, str, int, int, str, bytes | None, bytes | None]] = []

            for msg_db_path in acc.message_dbs:
                if not msg_db_path.is_file():
                    continue
                dec_msg_db = self._decrypt_or_copy_source_db(msg_db_path)
                self._collect_from_message_shard(
                    dec_msg_db,
                    msg_db_path.name,
                    acc.wxid,
                    md5_to_chat_id,
                    u_cur,
                    collected_msgs,
                )

            # Sort chronologically then by shard_local_id for deterministic global monotonic msg_id
            collected_msgs.sort(key=lambda item: (item[0], item[1]))

            u_cur.executemany(
                """
                INSERT INTO wx_messages (
                    create_time, shard_local_id, shard_name, server_id, chat_id,
                    sender_wxid, is_from_me, local_type, raw_content,
                    compress_content, packed_info_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                collected_msgs,
            )

            # Ensure every group chat_id present in wx_messages or wx_contacts has a row in wx_groups
            u_cur.execute(
                """
                INSERT OR IGNORE INTO wx_groups (chat_id, group_name, member_count, last_active_epoch)
                SELECT
                    c.username,
                    COALESCE(NULLIF(TRIM(c.remark), ''), NULLIF(TRIM(c.nick_name), ''), '未命名微信群'),
                    (SELECT COUNT(*) FROM wx_group_members gm WHERE gm.chat_id = c.username),
                    0
                FROM wx_contacts c
                WHERE c.username LIKE '%@chatroom'
                """
            )
            u_cur.execute(
                """
                INSERT OR IGNORE INTO wx_groups (chat_id, group_name, member_count, last_active_epoch)
                SELECT DISTINCT
                    m.chat_id,
                    '未命名微信群',
                    (SELECT COUNT(*) FROM wx_group_members gm WHERE gm.chat_id = m.chat_id),
                    MAX(m.create_time)
                FROM wx_messages m
                WHERE m.chat_id LIKE '%@chatroom'
                GROUP BY m.chat_id
                """
            )
            # Update member_count and last_active_epoch on wx_groups
            u_cur.execute(
                """
                UPDATE wx_groups
                SET
                    member_count = CASE
                        WHEN (SELECT COUNT(*) FROM wx_group_members gm WHERE gm.chat_id = wx_groups.chat_id) > member_count
                        THEN (SELECT COUNT(*) FROM wx_group_members gm WHERE gm.chat_id = wx_groups.chat_id)
                        ELSE member_count
                    END,
                    last_active_epoch = COALESCE(
                        (SELECT MAX(m.create_time) FROM wx_messages m WHERE m.chat_id = wx_groups.chat_id),
                        last_active_epoch
                    )
                """
            )

            u_conn.commit()
        finally:
            u_conn.close()

    def _ingest_contact_db(
        self,
        dec_contact_path: Path,
        u_cur: sqlite3.Cursor,
        md5_to_chat_id: dict[str, str],
    ) -> None:
        c_conn = sqlite3.connect(make_ro_sqlite_uri(dec_contact_path), uri=True)
        c_conn.row_factory = sqlite3.Row
        c_cur = c_conn.cursor()
        try:
            c_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in c_cur.fetchall()}

            # 1a. WeChat 4.x `contact` table
            if "contact" in tables:
                c_cur.execute("PRAGMA table_info(contact)")
                cols = {r[1] for r in c_cur.fetchall()}
                uname_col = "username" if "username" in cols else "user_name"
                nick_col = "nick_name" if "nick_name" in cols else ("nickname" if "nickname" in cols else "NULL")
                rem_col = "remark" if "remark" in cols else "NULL"
                alias_col = "alias" if "alias" in cols else "NULL"

                c_cur.execute(f"SELECT {uname_col} AS u, {nick_col} AS n, {rem_col} AS r, {alias_col} AS a FROM contact")
                for row in c_cur.fetchall():
                    u = (row["u"] or "").strip()
                    if not u:
                        continue
                    n = (row["n"] or "").strip()
                    r = (row["r"] or "").strip()
                    a = (row["a"] or "").strip()
                    is_grp = 1 if u.endswith("@chatroom") else 0
                    md5_to_chat_id[md5_hex(u)] = u
                    u_cur.execute(
                        "INSERT OR REPLACE INTO wx_contacts (username, nick_name, remark, alias, is_group) VALUES (?, ?, ?, ?, ?)",
                        (u, n, r, a, is_grp),
                    )

            # 1b. WeChat 3.x Windows `Contact` table
            if "Contact" in tables and "contact" not in tables:
                c_cur.execute("PRAGMA table_info(Contact)")
                cols = {r[1] for r in c_cur.fetchall()}
                if "UserName" in cols:
                    c_cur.execute("SELECT UserName AS u, NickName AS n, Remark AS r, Alias AS a FROM Contact")
                    for row in c_cur.fetchall():
                        u = (row["u"] or "").strip()
                        if not u:
                            continue
                        md5_to_chat_id[md5_hex(u)] = u
                        is_grp = 1 if u.endswith("@chatroom") else 0
                        u_cur.execute(
                            "INSERT OR REPLACE INTO wx_contacts (username, nick_name, remark, alias, is_group) VALUES (?, ?, ?, ?, ?)",
                            (u, (row["n"] or "").strip(), (row["r"] or "").strip(), (row["a"] or "").strip(), is_grp),
                        )

            # 1c. WeChat 3.x macOS `WCContact` table
            if "WCContact" in tables:
                c_cur.execute("PRAGMA table_info(WCContact)")
                cols = {r[1] for r in c_cur.fetchall()}
                if "m_nsUsrName" in cols:
                    nick_expr = "m_nsNickName" if "m_nsNickName" in cols else "''"
                    rem_expr = "m_nsRemark" if "m_nsRemark" in cols else "''"
                    alias_expr = "m_nsAliasName" if "m_nsAliasName" in cols else "''"
                    c_cur.execute(
                        f"SELECT m_nsUsrName AS u, {nick_expr} AS n, {rem_expr} AS r, {alias_expr} AS a FROM WCContact"
                    )
                    for row in c_cur.fetchall():
                        u = (row["u"] or "").strip()
                        if not u:
                            continue
                        md5_to_chat_id[md5_hex(u)] = u
                        is_grp = 1 if u.endswith("@chatroom") else 0
                        u_cur.execute(
                            "INSERT OR REPLACE INTO wx_contacts (username, nick_name, remark, alias, is_group) VALUES (?, ?, ?, ?, ?)",
                            (u, (row["n"] or "").strip(), (row["r"] or "").strip(), (row["a"] or "").strip(), is_grp),
                        )

            # 2a. WeChat 4.x `chatroom_member` / `chat_room` tables
            if "chatroom_member" in tables:
                c_cur.execute("PRAGMA table_info(chatroom_member)")
                cols = {r[1] for r in c_cur.fetchall()}
                room_col = "room_id" if "room_id" in cols else "chat_room"
                mem_col = "member_id" if "member_id" in cols else "username"
                disp_col = "display_name" if "display_name" in cols else ("nick_name" if "nick_name" in cols else "NULL")
                c_cur.execute(f"SELECT {room_col} AS room, {mem_col} AS mem, {disp_col} AS disp FROM chatroom_member")
                for row in c_cur.fetchall():
                    room = str(row["room"] or "").strip()
                    mem = str(row["mem"] or "").strip()
                    disp = str(row["disp"] or "").strip()
                    if room and mem:
                        if not room.endswith("@chatroom") and "@" not in room:
                            room = f"{room}@chatroom"
                        md5_to_chat_id[md5_hex(room)] = room
                        u_cur.execute(
                            "INSERT OR REPLACE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, ?)",
                            (room, mem, disp),
                        )

            if "chat_room" in tables or "ChatRoom" in tables:
                tbl = "chat_room" if "chat_room" in tables else "ChatRoom"
                c_cur.execute(f"PRAGMA table_info({tbl})")
                cols = {r[1] for r in c_cur.fetchall()}
                room_col = (
                    "username"
                    if "username" in cols
                    else ("username_" if "username_" in cols else ("ChatRoomName" if "ChatRoomName" in cols else "room_id"))
                )
                mem_list_col = (
                    "UserNameList"
                    if "UserNameList" in cols
                    else ("member_list" if "member_list" in cols else ("UsrNameList" if "UsrNameList" in cols else None))
                )
                disp_list_col = "DisplayNameList" if "DisplayNameList" in cols else None
                if room_col in cols:
                    sel_mem = f", {mem_list_col} AS mlist" if mem_list_col else ", NULL AS mlist"
                    sel_disp = f", {disp_list_col} AS dlist" if disp_list_col else ", NULL AS dlist"
                    c_cur.execute(f"SELECT {room_col} AS room {sel_mem} {sel_disp} FROM {tbl}")
                    for row in c_cur.fetchall():
                        room = str(row["room"] or "").strip()
                        if not room:
                            continue
                        md5_to_chat_id[md5_hex(room)] = room
                        mlist_raw = row["mlist"]
                        dlist_raw = row["dlist"]
                        if isinstance(mlist_raw, str) and mlist_raw.strip():
                            mems = [x.strip() for x in re.split(r"[;^,]", mlist_raw) if x.strip()]
                            disps = (
                                [x.strip() for x in re.split(r"[;^,]", dlist_raw)]
                                if isinstance(dlist_raw, str) and dlist_raw.strip()
                                else []
                            )
                            for idx, m_wxid in enumerate(mems):
                                g_nick = disps[idx] if idx < len(disps) else ""
                                u_cur.execute(
                                    "INSERT OR IGNORE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, ?)",
                                    (room, m_wxid, g_nick),
                                )
        finally:
            c_conn.close()

    def _ingest_session_db(
        self,
        dec_session_path: Path,
        u_cur: sqlite3.Cursor,
        md5_to_chat_id: dict[str, str],
    ) -> None:
        s_conn = sqlite3.connect(make_ro_sqlite_uri(dec_session_path), uri=True)
        s_conn.row_factory = sqlite3.Row
        s_cur = s_conn.cursor()
        try:
            s_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in s_cur.fetchall()}
            for tbl in ("SessionTable", "Session", "SessionAbstract"):
                if tbl in tables:
                    s_cur.execute(f"PRAGMA table_info({tbl})")
                    cols = {r[1] for r in s_cur.fetchall()}
                    u_col = next((c for c in ("username", "strUsrName", "m_nsUsrName") if c in cols), None)
                    if u_col:
                        s_cur.execute(f"SELECT {u_col} AS u FROM {tbl}")
                        for row in s_cur.fetchall():
                            u = str(row["u"] or "").strip()
                            if u:
                                md5_to_chat_id[md5_hex(u)] = u
        finally:
            s_conn.close()

    def _collect_from_message_shard(
        self,
        dec_msg_path: Path,
        shard_name: str,
        my_account_wxid: str,
        md5_to_chat_id: dict[str, str],
        u_cur: sqlite3.Cursor,
        collected_msgs: list[tuple[int, int, str, str, str, str, int, int, str, bytes | None, bytes | None]],
    ) -> None:
        m_conn = sqlite3.connect(make_ro_sqlite_uri(dec_msg_path), uri=True)
        m_conn.row_factory = sqlite3.Row
        m_cur = m_conn.cursor()
        try:
            m_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in m_cur.fetchall()]

            # 1. Load shard-local `Name2Id` / `Name2ID` mapping (rowid -> user_name)
            id_to_wxid: dict[int, str] = {}
            for n2i_tbl in ("Name2Id", "Name2ID", "name2id"):
                if n2i_tbl in tables:
                    m_cur.execute(f"PRAGMA table_info({n2i_tbl})")
                    cols = {r[1] for r in m_cur.fetchall()}
                    u_col = "user_name" if "user_name" in cols else ("UsrName" if "UsrName" in cols else "username")
                    if u_col in cols:
                        m_cur.execute(f"SELECT rowid AS rid, {u_col} AS uname FROM {n2i_tbl}")
                        for r in m_cur.fetchall():
                            uname = str(r["uname"] or "").strip()
                            if uname:
                                id_to_wxid[int(r["rid"])] = uname
                                md5_to_chat_id[md5_hex(uname)] = uname

            # 2. Check for Windows 3.x unified `MSG` table (with `StrTalker` column)
            if "MSG" in tables:
                m_cur.execute("PRAGMA table_info(MSG)")
                cols = {r[1] for r in m_cur.fetchall()}
                if "StrTalker" in cols:
                    m_cur.execute(
                        """
                        SELECT
                            localId AS lid,
                            CAST(MsgSvrID AS TEXT) AS svrid,
                            StrTalker AS talker,
                            IsSender AS is_sender,
                            Type AS mtype,
                            CreateTime AS ctime,
                            StrContent AS content,
                            CompressContent AS comp,
                            BytesExtra AS extra
                        FROM MSG
                        WHERE StrTalker LIKE '%@chatroom'
                        """
                    )
                    for r in m_cur.fetchall():
                        talker = str(r["talker"] or "").strip()
                        is_from_me = 1 if int(r["is_sender"] or 0) == 1 else 0
                        raw_c = r["content"] or ""
                        comp_c = r["comp"]
                        extra_b = r["extra"]
                        parsed = parse_wechat_message_payload(int(r["mtype"] or 1), raw_c, comp_c, extra_b)
                        sender_wxid = my_account_wxid if is_from_me else (parsed.sender_wxid_from_prefix or "")
                        collected_msgs.append(
                            (
                                int(r["ctime"] or 0),
                                int(r["lid"] or 0),
                                shard_name,
                                str(r["svrid"] or ""),
                                talker,
                                sender_wxid,
                                is_from_me,
                                int(r["mtype"] or 1),
                                raw_c,
                                comp_c,
                                extra_b,
                            )
                        )

            # 3. Scan WeChat 4.x (`Msg_<md5>`) and macOS 3.x (`Chat_<md5>`) tables
            for tbl in tables:
                m_match = MSG_TABLE_RE.match(tbl)
                if not m_match:
                    continue
                prefix_kind = m_match.group(1).lower()
                table_md5 = m_match.group(2).lower()
                chat_id = md5_to_chat_id.get(table_md5)

                # strictly skip 1-on-1 private chat tables if we know the username is not @chatroom
                if chat_id and not chat_id.endswith("@chatroom"):
                    continue

                # If chat_id wasn't in contact.db/Name2Id, only include if explicitly mapped or heuristic shows group chat
                if not chat_id:
                    continue

                m_cur.execute(f"PRAGMA table_info({tbl})")
                cols = {r[1] for r in m_cur.fetchall()}

                if prefix_kind == "msg" or "local_type" in cols or "message_content" in cols:
                    # WeChat 4.x Msg_<md5> schema
                    lid_col = "local_id" if "local_id" in cols else "rowid"
                    svrid_col = "server_id" if "server_id" in cols else "0"
                    type_col = "local_type" if "local_type" in cols else "message_type"
                    time_col = "create_time" if "create_time" in cols else ("createTime" if "createTime" in cols else "timestamp")
                    sender_id_col = "real_sender_id" if "real_sender_id" in cols else "NULL"
                    is_me_col = "is_send" if "is_send" in cols else ("Des" if "Des" in cols else "NULL")
                    content_col = "message_content" if "message_content" in cols else "content"
                    comp_col = "compress_content" if "compress_content" in cols else "NULL"
                    packed_col = "packed_info_data" if "packed_info_data" in cols else "NULL"

                    m_cur.execute(
                        f"""
                        SELECT
                            {lid_col} AS lid,
                            CAST({svrid_col} AS TEXT) AS svrid,
                            {type_col} AS mtype,
                            {time_col} AS ctime,
                            {sender_id_col} AS real_sid,
                            {is_me_col} AS is_me_raw,
                            {content_col} AS content,
                            {comp_col} AS comp,
                            {packed_col} AS packed
                        FROM {tbl}
                        """
                    )
                    for r in m_cur.fetchall():
                        real_sid = r["real_sid"]
                        resolved_sender = ""
                        if isinstance(real_sid, int) and real_sid in id_to_wxid:
                            resolved_sender = id_to_wxid[real_sid]
                        elif isinstance(real_sid, str) and real_sid.strip():
                            s_val = real_sid.strip()
                            if s_val.isdigit() and int(s_val) in id_to_wxid:
                                resolved_sender = id_to_wxid[int(s_val)]
                            else:
                                resolved_sender = s_val

                        raw_c = r["content"] if isinstance(r["content"], str) else ""
                        comp_c = r["comp"] if isinstance(r["comp"], (bytes, bytearray)) else (
                            r["content"] if isinstance(r["content"], (bytes, bytearray)) else None
                        )
                        packed_b = r["packed"] if isinstance(r["packed"], (bytes, bytearray)) else None

                        if not resolved_sender:
                            parsed = parse_wechat_message_payload(int(r["mtype"] or 1), raw_c, comp_c, packed_b)
                            resolved_sender = parsed.sender_wxid_from_prefix or ""

                        is_me_val = r["is_me_raw"]
                        if is_me_val is not None:
                            is_from_me = 1 if int(is_me_val) == 1 else 0
                        else:
                            is_from_me = 1 if (my_account_wxid and resolved_sender == my_account_wxid) else 0

                        if is_from_me and my_account_wxid and not resolved_sender:
                            resolved_sender = my_account_wxid

                        if resolved_sender and not resolved_sender.endswith("@chatroom"):
                            u_cur.execute(
                                "INSERT OR IGNORE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, '')",
                                (chat_id, resolved_sender),
                            )

                        collected_msgs.append(
                            (
                                int(r["ctime"] or 0),
                                int(r["lid"] or 0),
                                shard_name,
                                str(r["svrid"] or ""),
                                chat_id,
                                resolved_sender,
                                is_from_me,
                                int(r["mtype"] or 1),
                                raw_c,
                                comp_c,
                                packed_b,
                            )
                        )
                else:
                    # WeChat 3.x macOS Chat_<md5> schema (mesLocalID, mesSvrID, msgCreateTime, msgContent, messageType, mesDes)
                    m_cur.execute(
                        f"""
                        SELECT
                            mesLocalID AS lid,
                            CAST(mesSvrID AS TEXT) AS svrid,
                            messageType AS mtype,
                            msgCreateTime AS ctime,
                            mesDes AS des,
                            msgContent AS content
                        FROM {tbl}
                        """
                    )
                    for r in m_cur.fetchall():
                        # In macOS 3.x, mesDes == 0 means sent by me, mesDes == 1 means received
                        is_from_me = 1 if int(r["des"] or 1) == 0 else 0
                        raw_c = r["content"] or ""
                        parsed = parse_wechat_message_payload(int(r["mtype"] or 1), raw_c, None, None)
                        sender_wxid = my_account_wxid if is_from_me else (parsed.sender_wxid_from_prefix or "")
                        if sender_wxid and not sender_wxid.endswith("@chatroom"):
                            u_cur.execute(
                                "INSERT OR IGNORE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, '')",
                                (chat_id, sender_wxid),
                            )
                        collected_msgs.append(
                            (
                                int(r["ctime"] or 0),
                                int(r["lid"] or 0),
                                shard_name,
                                str(r["svrid"] or ""),
                                chat_id,
                                sender_wxid,
                                is_from_me,
                                int(r["mtype"] or 1),
                                raw_c,
                                None,
                                None,
                            )
                        )
        finally:
            m_conn.close()

    def clean(self, media_only: bool = False) -> dict[str, Any]:
        """Remove exported media cache and optionally destroy the decrypted & unified SQLite snapshot files."""
        removed_items: list[str] = []
        if self.media_dir.exists():
            shutil.rmtree(self.media_dir)
            removed_items.append(str(self.media_dir))

        if not media_only:
            if self.raw_decrypted_dir.exists():
                shutil.rmtree(self.raw_decrypted_dir)
                removed_items.append(str(self.raw_decrypted_dir))
            for p in (self.snapshot_db_path, self.meta_path):
                if p.exists():
                    p.unlink()
                    removed_items.append(str(p))

        return {
            "status": "cleaned",
            "media_only": media_only,
            "removed_paths": removed_items,
        }
