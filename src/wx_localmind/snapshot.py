"""Multi-platform WeChat 4.x / 3.x account discovery, SQLCipher decryption, and Incremental Unified Snapshot builder."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import AppConfig
from .constants import (
    CANDIDATE_WECHAT_ROOTS,
    DEFAULT_MAC_WECHAT4_FILES_DIR,
    DEFAULT_WX_DATA_DIR,
    EXCLUDED_MSG_TYPES,
    SQLCIPHER_PAGE_SIZE,
)
from .crypto import decrypt_sqlcipher_database, detect_cipher_version_and_key, is_plaintext_sqlite
from .errors import EnvOrPermissionError
from .protobuf_parser import get_zstd_backend_name, parse_wechat_message_payload

MSG_TABLE_RE = re.compile(r"^(Msg|Chat)_([0-9a-fA-F]{32})$", re.IGNORECASE)
SNAPSHOT_SCHEMA_VERSION = 2
RECENT_RECALL_RECHECK_WINDOW = 25


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


def normalize_epoch_sec(raw_ts: int | float | str | None) -> int:
    """Normalize WeChat Unix timestamp (seconds, milliseconds, or microseconds) to integer Unix seconds."""
    if raw_ts is None:
        return 0
    try:
        val = int(float(raw_ts))
    except (ValueError, TypeError):
        return 0
    if val <= 0:
        return 0
    if val > 100_000_000_000_000:
        return val // 1_000_000
    if val > 100_000_000_000:
        return val // 1000
    return val


def epoch_to_local_iso(epoch_sec: float | int | None) -> str | None:
    if epoch_sec is None or epoch_sec <= 0:
        return None
    ts = float(epoch_sec)
    if ts > 1e14:
        ts = ts / 1_000_000.0
    elif ts > 1e11:
        ts = ts / 1000.0
    dt = datetime.fromtimestamp(ts).astimezone()
    return dt.isoformat(timespec="seconds")


def make_ro_sqlite_uri(db_path: Path) -> str:
    """Build cross-platform SQLite read-only URI (`file:///path?mode=ro` on POSIX & `file:///C:/...?mode=ro` on Windows)."""
    return f"{db_path.resolve().as_uri()}?mode=ro"


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().lower()


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


@contextmanager
def _acquire_snapshot_lock(lock_path: Path) -> Iterator[None]:
    """Cross-platform exclusive file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows)."""
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if sys.platform == "win32":
        import msvcrt

        with open(lock_path, "a+b") as lock_file:
            if lock_file.tell() == 0:
                lock_file.write(b" ")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
    else:
        import fcntl

        with open(lock_path, "w", encoding="utf-8") as lock_file:
            _chmod_best_effort(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _compute_file_digest_part(file_path: Path) -> tuple[int, int, str]:
    """Return (size, mtime_ns, fast_blake2b_hex) for change detection."""
    if not file_path.is_file():
        return 0, 0, ""
    st = file_path.stat()
    size = st.st_size
    h = hashlib.blake2b(digest_size=16)
    with open(file_path, "rb") as f:
        if size <= 4 * 1024 * 1024:
            while chunk := f.read(65536):
                h.update(chunk)
        else:
            h.update(f.read(65536))
            f.seek(max(0, size - 65536))
            h.update(f.read(65536))
    return size, st.st_mtime_ns, h.hexdigest()


def _compute_db_fingerprint(db_path: Path) -> tuple[str, str]:
    """Compute composite fingerprint of `.db` + `.db-wal` and extract 16-byte salt hex."""
    db_sz, db_mtime, db_hash = _compute_file_digest_part(db_path)
    wal_path = Path(f"{db_path}-wal")
    wal_sz, wal_mtime, wal_hash = _compute_file_digest_part(wal_path)

    salt_hex = "empty"
    if db_sz >= 16:
        if is_plaintext_sqlite(db_path):
            salt_hex = "plaintext"
        else:
            try:
                with open(db_path, "rb") as f:
                    salt_hex = f.read(16).hex()
            except OSError:
                salt_hex = "unreadable"

    fp = f"{db_sz}:{db_mtime}:{db_hash}|wal:{wal_sz}:{wal_mtime}:{wal_hash}"
    return fp, salt_hex


def _build_materialized_search_text(
    local_type: int,
    raw_content: str,
    compress_content: bytes | None,
    packed_blob: bytes | None,
    pre_parsed: Any | None = None,
) -> str | None:
    """Extract lowercase searchable text + media title/filename at ingestion time (None if revoked/noise)."""
    if local_type in EXCLUDED_MSG_TYPES:
        return None
    parsed = pre_parsed or parse_wechat_message_payload(local_type, raw_content, compress_content, packed_blob)
    if parsed.is_revoked or parsed.is_pat_message or parsed.normalized_type in ("revoked", "system_event", "pat"):
        return None

    parts: list[str] = []
    if parsed.text:
        parts.append(parsed.text.replace("\u2005", " ").strip())
    if parsed.media_title:
        parts.append(parsed.media_title.strip())
    if parsed.media_rel_path:
        parts.append(os.path.basename(parsed.media_rel_path.replace("\\", "/")).strip())

    combined = "\n".join(p for p in parts if p).lower().strip()
    return combined if combined else None


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
        msg_dbs = [p for p in msg_dbs if re.match(r"^message_\d+\.db$", p.name, re.IGNORECASE)]
        contact_db = db_storage / "contact" / "contact.db"
        session_db = db_storage / "session" / "session.db"
        if msg_dbs or contact_db.is_file():
            raw_name = acc_dir.name
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

    if primary in (DEFAULT_MAC_WECHAT4_FILES_DIR, DEFAULT_WX_DATA_DIR):
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

        direct = _probe_account_dir(root, root.parent)
        if direct:
            return direct

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
    """Manages SQLCipher decryption and unified read-only SQLite snapshot creation with cross-platform locking."""

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
        for k_name, k_val in self.config.db_keys.items():
            if k_name == db_path.name or k_name.endswith(f"/{db_path.name}") or k_name.endswith(f"\\{db_path.name}"):
                if k_val not in keys:
                    keys.append(k_val)
        if self.config.raw_key_hex and self.config.raw_key_hex not in keys:
            keys.append(self.config.raw_key_hex)
        for k_val in self.config.db_keys.values():
            if k_val not in keys:
                keys.append(k_val)
        return keys

    def check_source_access(self) -> dict[str, Any]:
        """Inspect local WeChat data directory, discovered account, zstd backend, and shard key verification."""
        zstd_backend = get_zstd_backend_name()
        acc = discover_wechat_account(self.config)
        if acc is None:
            return {
                "wx_data_dir": self.config.wx_data_dir,
                "account_found": False,
                "source_db_readable": False,
                "zstd_backend": zstd_backend,
                "error": (
                    f"No WeChat account directory found under '{self.config.wx_data_dir}'. "
                    "Ensure WeChat is installed and logged in, or set `--config` / `WECHAT_LOCALMIND_DATA_DIR`."
                ),
            }

        required_dbs = list(acc.message_dbs)
        if acc.contact_db:
            required_dbs.append(acc.contact_db)
        optional_dbs: list[Path] = []
        if acc.session_db and acc.session_db not in required_dbs:
            optional_dbs.append(acc.session_db)

        all_dbs = required_dbs + optional_dbs
        required_set = set(required_dbs)

        encrypted_count = 0
        plaintext_count = 0
        key_verified_count = 0
        unverified_required_dbs: list[str] = []
        sample_salts: dict[str, str] = {}

        for db_p in all_dbs:
            if not db_p.is_file():
                continue
            if is_plaintext_sqlite(db_p):
                plaintext_count += 1
                key_verified_count += 1
            else:
                encrypted_count += 1
                verified_this = False
                try:
                    with open(db_p, "rb") as f:
                        p1 = f.read(65536)
                    sample_salts[db_p.name] = p1[:16].hex()
                    cands = self._collect_candidate_keys_for_db(db_p)
                    if detect_cipher_version_and_key(p1, cands) is not None:
                        key_verified_count += 1
                        verified_this = True
                except Exception:
                    pass
                if not verified_this and db_p in required_set:
                    unverified_required_dbs.append(db_p.name)

        ready_to_decrypt = len(required_dbs) > 0 and len(unverified_required_dbs) == 0
        err_msg = None
        if not required_dbs:
            err_msg = f"Account directory '{acc.account_dir}' contains no message or contact databases."
        elif unverified_required_dbs:
            if key_verified_count == 0:
                err_msg = (
                    f"Found {encrypted_count} SQLCipher-encrypted WeChat database(s) for account '{acc.wxid}', "
                    "but no matching decryption key is configured. Run `wechat-localmind key --set <64-hex-key>` "
                    "or `wechat-localmind key --import <keys.json>`."
                )
            else:
                err_msg = (
                    f"Missing or invalid SQLCipher key for {len(unverified_required_dbs)} required database shard(s): "
                    f"{', '.join(unverified_required_dbs)}. Re-import complete shard keys or set raw master key."
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
            "unverified_dbs": unverified_required_dbs,
            "db_salts": sample_salts,
            "zstd_backend": zstd_backend,
            "source_db_readable": ready_to_decrypt,
            "error": err_msg,
        }

    def ensure_snapshot(self, force_fresh: bool = False) -> SnapshotState:
        """Ensure a fresh or valid cached unified SQLite snapshot exists using cross-platform file lock."""
        self.snapshot_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        _chmod_best_effort(self.snapshot_dir, 0o700)
        ttl_sec = max(0, self.config.cleaning.snapshot_ttl_sec)
        now = time.time()

        if not force_fresh and self._is_cache_valid(now, ttl_sec):
            return self._load_cached_state(now)

        with _acquire_snapshot_lock(self.lock_path):
            now = time.time()
            if not force_fresh and self._is_cache_valid(now, max(1, ttl_sec)):
                return self._load_cached_state(now)
            return self._build_unified_snapshot()

    def _is_cache_valid(self, now: float, ttl_sec: int) -> bool:
        if not self.snapshot_db_path.exists() or not self.meta_path.exists():
            return False
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if int(meta.get("schema_version", 0)) != SNAPSHOT_SCHEMA_VERSION:
                return False
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

    def _decrypt_or_copy_source_db(self, src_db: Path, dst_tag: str | None = None) -> Path:
        """Decrypt or copy a source WeChat .db (+ .db-wal) into temporary `decrypted_raw/` staging."""
        self.raw_decrypted_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        _chmod_best_effort(self.raw_decrypted_dir, 0o700)
        dst_db = self.raw_decrypted_dir / (dst_tag or src_db.name)
        if is_plaintext_sqlite(src_db):
            tmp_dst = self.raw_decrypted_dir / f"{dst_db.name}.{os.getpid()}.tmp"
            src_conn = sqlite3.connect(make_ro_sqlite_uri(src_db), uri=True, timeout=3.0)
            dst_conn = sqlite3.connect(str(tmp_dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
            _chmod_best_effort(tmp_dst, 0o600)
            tmp_dst.replace(dst_db)
            return dst_db

        cands = self._collect_candidate_keys_for_db(src_db)
        decrypt_sqlcipher_database(src_db, dst_db, cands)
        return dst_db

    def _purge_raw_decrypted_staging(self) -> None:
        """Unconditionally remove single-DB decrypted files so zero 1-on-1 private chats persist on disk."""
        if self.raw_decrypted_dir.exists():
            shutil.rmtree(self.raw_decrypted_dir, ignore_errors=True)

    def _can_use_incremental_base(self, acc: DiscoveredWeChatAccount) -> bool:
        if acc.arch_version == "unified_sqlite":
            return False
        if not self.snapshot_db_path.is_file() or not self.meta_path.is_file():
            return False
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if int(meta.get("schema_version", 0)) != SNAPSHOT_SCHEMA_VERSION:
                return False
            if meta.get("wxid") != acc.wxid or meta.get("arch_version") != acc.arch_version:
                return False
            if Path(meta.get("account_dir", "")).resolve() != acc.account_dir.resolve():
                return False
            return True
        except Exception:
            return False

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

        try:
            if acc.arch_version == "unified_sqlite" and acc.message_dbs:
                src_conn = sqlite3.connect(make_ro_sqlite_uri(acc.message_dbs[0]), uri=True)
                dst_conn = sqlite3.connect(str(tmp_unified))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
                    src_conn.close()
                _chmod_best_effort(tmp_unified, 0o600)
                tmp_unified.replace(self.snapshot_db_path)
            elif self._can_use_incremental_base(acc):
                rebuilt_or_updated = self._sync_unified_db_incrementally(acc, tmp_unified)
                if rebuilt_or_updated:
                    _chmod_best_effort(tmp_unified, 0o600)
                    tmp_unified.replace(self.snapshot_db_path)
            else:
                self._assemble_unified_db_from_shards(acc, tmp_unified)
                _chmod_best_effort(tmp_unified, 0o600)
                tmp_unified.replace(self.snapshot_db_path)
        finally:
            if tmp_unified.exists():
                tmp_unified.unlink(missing_ok=True)
            self._purge_raw_decrypted_staging()

        _chmod_best_effort(self.snapshot_db_path, 0o600)

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
                    norm_ts = normalize_epoch_sec(row[1])
                    db_latest_msg_epoch = norm_ts if norm_ts > 0 else None
        finally:
            conn.close()

        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        now_epoch = time.time()
        now_iso = epoch_to_local_iso(now_epoch) or ""
        latest_msg_iso = epoch_to_local_iso(db_latest_msg_epoch)

        meta = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
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
        _chmod_best_effort(tmp_meta, 0o600)
        tmp_meta.replace(self.meta_path)
        _chmod_best_effort(self.meta_path, 0o600)

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

    @staticmethod
    def _init_unified_schema(u_cur: sqlite3.Cursor) -> None:
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
                packed_info_data BLOB,
                search_text TEXT,
                UNIQUE(shard_name, chat_id, shard_local_id)
            );
            CREATE TABLE wx_shard_state (
                shard_key TEXT PRIMARY KEY,
                db_role TEXT,
                fingerprint TEXT,
                salt_hex TEXT
            );
            CREATE TABLE wx_table_watermarks (
                shard_name TEXT,
                chat_id TEXT,
                max_local_id INTEGER DEFAULT 0,
                row_count INTEGER DEFAULT 0,
                PRIMARY KEY (shard_name, chat_id)
            );
            CREATE INDEX idx_wx_messages_chat_id ON wx_messages(chat_id, msg_id);
            CREATE INDEX idx_wx_messages_chat_svrid ON wx_messages(chat_id, server_id);
            CREATE INDEX idx_wx_messages_time ON wx_messages(chat_id, create_time);
            """
        )
        try:
            u_cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS wx_messages_fts USING fts5(
                    search_text,
                    content='wx_messages',
                    content_rowid='msg_id',
                    tokenize='trigram'
                )
                """
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _finalize_groups_contacts_and_fts(
        u_cur: sqlite3.Cursor,
        my_account_wxid: str,
        fts_mode: str = "rebuild",
        fts_after_msg_id: int = 0,
    ) -> None:
        """Refresh `wx_groups` metadata, prune non-group 1-on-1 contacts, and sync `wx_messages_fts`."""
        u_cur.execute(
            """
            INSERT INTO wx_groups (chat_id, group_name, member_count, last_active_epoch)
            SELECT
                c.username,
                COALESCE(NULLIF(TRIM(c.remark), ''), NULLIF(TRIM(c.nick_name), ''), '未命名微信群'),
                (SELECT COUNT(*) FROM wx_group_members gm WHERE gm.chat_id = c.username),
                0
            FROM wx_contacts c
            WHERE c.username LIKE '%@chatroom'
            ON CONFLICT(chat_id) DO UPDATE SET
                group_name = CASE
                    WHEN excluded.group_name != '未命名微信群' THEN excluded.group_name
                    ELSE wx_groups.group_name
                END
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
                    0
                )
            """
        )

        # Privacy redline: purge 1-on-1 private contacts who are neither in any group nor self
        u_cur.execute(
            """
            DELETE FROM wx_contacts
            WHERE is_group = 0
              AND username NOT IN (
                  SELECT DISTINCT member_wxid FROM wx_group_members
                  WHERE member_wxid IS NOT NULL AND member_wxid != ''
              )
              AND username NOT IN (
                  SELECT DISTINCT sender_wxid FROM wx_messages
                  WHERE sender_wxid IS NOT NULL AND sender_wxid != ''
              )
              AND username != ?
            """,
            (my_account_wxid or "",),
        )

        # Sync optional FTS5 trigram index
        u_cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wx_messages_fts'")
        if u_cur.fetchone() is not None:
            try:
                if fts_mode == "append" and fts_after_msg_id >= 0:
                    u_cur.execute(
                        """
                        INSERT INTO wx_messages_fts(rowid, search_text)
                        SELECT msg_id, search_text FROM wx_messages
                        WHERE msg_id > ? AND search_text IS NOT NULL
                        """,
                        (fts_after_msg_id,),
                    )
                elif fts_mode == "rebuild":
                    u_cur.execute("INSERT INTO wx_messages_fts(wx_messages_fts) VALUES('rebuild')")
            except sqlite3.OperationalError:
                pass

    def _assemble_unified_db_from_shards(self, acc: DiscoveredWeChatAccount, target_db: Path) -> None:
        """Full initial build of unified SQLite snapshot across WeChat 4.x / 3.x shards."""
        u_conn = sqlite3.connect(str(target_db))
        u_cur = u_conn.cursor()
        self._init_unified_schema(u_cur)

        md5_to_chat_id: dict[str, str] = {}

        try:
            if acc.contact_db and acc.contact_db.is_file():
                c_fp, c_salt = _compute_db_fingerprint(acc.contact_db)
                dec_contact = self._decrypt_or_copy_source_db(acc.contact_db, "contact.db")
                self._ingest_contact_db(dec_contact, u_cur, md5_to_chat_id)
                u_cur.execute(
                    "INSERT OR REPLACE INTO wx_shard_state VALUES (?, 'contact', ?, ?)",
                    ("contact::" + acc.contact_db.name, c_fp, c_salt),
                )

            if acc.session_db and acc.session_db.is_file() and acc.session_db != acc.contact_db:
                try:
                    s_fp, s_salt = _compute_db_fingerprint(acc.session_db)
                    dec_sess = self._decrypt_or_copy_source_db(acc.session_db, "session.db")
                    self._ingest_session_db(dec_sess, u_cur, md5_to_chat_id)
                    u_cur.execute(
                        "INSERT OR REPLACE INTO wx_shard_state VALUES (?, 'session', ?, ?)",
                        ("session::" + acc.session_db.name, s_fp, s_salt),
                    )
                except Exception:
                    pass

            collected_msgs: list[
                tuple[int, int, str, str, str, str, int, int, str, bytes | None, bytes | None, str | None]
            ] = []
            watermarks: dict[tuple[str, str], tuple[int, int]] = {}

            for msg_db_path in acc.message_dbs:
                if not msg_db_path.is_file():
                    continue
                m_fp, m_salt = _compute_db_fingerprint(msg_db_path)
                dec_msg_db = self._decrypt_or_copy_source_db(msg_db_path, f"msg_{msg_db_path.name}")
                self._collect_from_message_shard(
                    dec_msg_db,
                    msg_db_path.name,
                    acc.wxid,
                    md5_to_chat_id,
                    u_cur,
                    collected_msgs,
                    watermarks=watermarks,
                )
                u_cur.execute(
                    "INSERT OR REPLACE INTO wx_shard_state VALUES (?, 'message', ?, ?)",
                    (msg_db_path.name, m_fp, m_salt),
                )

            collected_msgs.sort(key=lambda item: (item[0], item[1], item[2]))

            u_cur.executemany(
                """
                INSERT OR IGNORE INTO wx_messages (
                    create_time, shard_local_id, shard_name, server_id, chat_id,
                    sender_wxid, is_from_me, local_type, raw_content,
                    compress_content, packed_info_data, search_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                collected_msgs,
            )

            for (s_name, c_id), (max_lid, r_cnt) in watermarks.items():
                u_cur.execute(
                    "INSERT OR REPLACE INTO wx_table_watermarks VALUES (?, ?, ?, ?)",
                    (s_name, c_id, max_lid, r_cnt),
                )

            self._finalize_groups_contacts_and_fts(u_cur, acc.wxid, fts_mode="rebuild")
            u_conn.commit()
        finally:
            u_conn.close()
            self._purge_raw_decrypted_staging()

    def _sync_unified_db_incrementally(self, acc: DiscoveredWeChatAccount, tmp_unified: Path) -> bool:
        """Incrementally sync changed shards into persistent snapshot without re-numbering existing `msg_id`s.

        Returns True if `tmp_unified` was written and should replace `self.snapshot_db_path`,
        or False if zero source files changed on disk (fast no-op).
        """
        ro_conn = sqlite3.connect(make_ro_sqlite_uri(self.snapshot_db_path), uri=True)
        try:
            ro_cur = ro_conn.cursor()
            ro_cur.execute("SELECT shard_key, db_role, fingerprint, salt_hex FROM wx_shard_state")
            prev_states = {r[0]: (r[1], r[2], r[3]) for r in ro_cur.fetchall()}
        finally:
            ro_conn.close()

        contact_key = f"contact::{acc.contact_db.name}" if (acc.contact_db and acc.contact_db.is_file()) else None
        contact_fp = _compute_db_fingerprint(acc.contact_db) if contact_key and acc.contact_db else None
        contact_changed = bool(
            contact_key and contact_fp and prev_states.get(contact_key, ("", "", ""))[1] != contact_fp[0]
        )

        session_key = (
            f"session::{acc.session_db.name}"
            if (acc.session_db and acc.session_db.is_file() and acc.session_db != acc.contact_db)
            else None
        )
        session_fp = _compute_db_fingerprint(acc.session_db) if session_key and acc.session_db else None
        session_changed = bool(
            session_key and session_fp and prev_states.get(session_key, ("", "", ""))[1] != session_fp[0]
        )

        cur_msg_names = {p.name for p in acc.message_dbs if p.is_file()}
        prev_msg_names = {k for k, v in prev_states.items() if v[0] == "message"}
        removed_msg_shards = prev_msg_names - cur_msg_names

        changed_msg_dbs: list[tuple[Path, str, str]] = []
        for p in acc.message_dbs:
            if not p.is_file():
                continue
            fp, salt = _compute_db_fingerprint(p)
            if prev_states.get(p.name, ("", "", ""))[1] != fp:
                changed_msg_dbs.append((p, fp, salt))

        if not contact_changed and not session_changed and not removed_msg_shards and not changed_msg_dbs:
            return False

        shutil.copy2(self.snapshot_db_path, tmp_unified)
        u_conn = sqlite3.connect(str(tmp_unified))
        u_cur = u_conn.cursor()
        u_cur.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;")

        try:
            u_cur.execute("SELECT COALESCE(MAX(msg_id), 0) FROM wx_messages")
            prev_max_msg_id = int(u_cur.fetchone()[0] or 0)

            u_cur.execute("SELECT shard_name, chat_id, max_local_id, row_count FROM wx_table_watermarks")
            prev_watermarks: dict[tuple[str, str], tuple[int, int]] = {
                (r[0], r[1]): (int(r[2] or 0), int(r[3] or 0)) for r in u_cur.fetchall()
            }

            # Seed md5 -> group chat_id mapping from already-known groups so unchanged contact.db need not be re-read
            md5_to_chat_id: dict[str, str] = {}
            u_cur.execute("SELECT chat_id FROM wx_groups")
            for (cid,) in u_cur.fetchall():
                if cid:
                    md5_to_chat_id[md5_hex(cid)] = cid
            u_cur.execute("SELECT username FROM wx_contacts WHERE is_group = 1")
            for (cid,) in u_cur.fetchall():
                if cid:
                    md5_to_chat_id[md5_hex(cid)] = cid

            if contact_changed and acc.contact_db and contact_key and contact_fp:
                dec_contact = self._decrypt_or_copy_source_db(acc.contact_db, "contact.db")
                self._ingest_contact_db(dec_contact, u_cur, md5_to_chat_id)
                u_cur.execute(
                    "INSERT OR REPLACE INTO wx_shard_state VALUES (?, 'contact', ?, ?)",
                    (contact_key, contact_fp[0], contact_fp[1]),
                )

            if session_changed and acc.session_db and session_key and session_fp:
                try:
                    dec_sess = self._decrypt_or_copy_source_db(acc.session_db, "session.db")
                    self._ingest_session_db(dec_sess, u_cur, md5_to_chat_id)
                    u_cur.execute(
                        "INSERT OR REPLACE INTO wx_shard_state VALUES (?, 'session', ?, ?)",
                        (session_key, session_fp[0], session_fp[1]),
                    )
                except Exception:
                    pass

            had_deletes_or_updates = False
            for rem_shard in removed_msg_shards:
                u_cur.execute("DELETE FROM wx_messages WHERE shard_name = ?", (rem_shard,))
                u_cur.execute("DELETE FROM wx_table_watermarks WHERE shard_name = ?", (rem_shard,))
                u_cur.execute("DELETE FROM wx_shard_state WHERE shard_key = ?", (rem_shard,))
                had_deletes_or_updates = True

            new_msgs: list[
                tuple[int, int, str, str, str, str, int, int, str, bytes | None, bytes | None, str | None]
            ] = []
            updated_watermarks: dict[tuple[str, str], tuple[int, int]] = {}
            decrypted_shards: list[tuple[Path, Path, str, str]] = []

            for msg_db_path, m_fp, m_salt in changed_msg_dbs:
                dec_msg_db = self._decrypt_or_copy_source_db(msg_db_path, f"msg_{msg_db_path.name}")
                decrypted_shards.append((msg_db_path, dec_msg_db, m_fp, m_salt))

            # Detect full account DB wipe/reinstall: all message shards replaced with new salts AND all watermarks regressed
            if (
                prev_msg_names
                and len(changed_msg_dbs) == len(cur_msg_names)
                and all(prev_states.get(p.name, ("", "", ""))[2] != m_salt for p, _, m_salt in changed_msg_dbs)
                and self._did_all_watermarks_regress(decrypted_shards, md5_to_chat_id, prev_watermarks)
            ):
                u_conn.close()
                tmp_unified.unlink(missing_ok=True)
                self._assemble_unified_db_from_shards(acc, tmp_unified)
                return True

            for msg_db_path, dec_msg_db, m_fp, m_salt in decrypted_shards:
                mutated = self._sync_single_message_shard(
                    dec_msg_db,
                    msg_db_path.name,
                    acc.wxid,
                    md5_to_chat_id,
                    prev_watermarks,
                    updated_watermarks,
                    u_cur,
                    new_msgs,
                )
                if mutated:
                    had_deletes_or_updates = True
                u_cur.execute(
                    "INSERT OR REPLACE INTO wx_shard_state VALUES (?, 'message', ?, ?)",
                    (msg_db_path.name, m_fp, m_salt),
                )

            new_msgs.sort(key=lambda item: (item[0], item[1], item[2]))
            if new_msgs:
                u_cur.executemany(
                    """
                    INSERT OR IGNORE INTO wx_messages (
                        create_time, shard_local_id, shard_name, server_id, chat_id,
                        sender_wxid, is_from_me, local_type, raw_content,
                        compress_content, packed_info_data, search_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    new_msgs,
                )

            for (s_name, c_id), (max_lid, r_cnt) in updated_watermarks.items():
                u_cur.execute(
                    "INSERT OR REPLACE INTO wx_table_watermarks VALUES (?, ?, ?, ?)",
                    (s_name, c_id, max_lid, r_cnt),
                )

            fts_mode = "rebuild" if had_deletes_or_updates else ("append" if new_msgs else "none")
            self._finalize_groups_contacts_and_fts(
                u_cur,
                acc.wxid,
                fts_mode=fts_mode,
                fts_after_msg_id=prev_max_msg_id,
            )
            u_conn.commit()
            return True
        finally:
            u_conn.close()
            self._purge_raw_decrypted_staging()

    def _did_all_watermarks_regress(
        self,
        decrypted_shards: list[tuple[Path, Path, str, str]],
        md5_to_chat_id: dict[str, str],
        prev_watermarks: dict[tuple[str, str], tuple[int, int]],
    ) -> bool:
        if not prev_watermarks:
            return False
        saw_non_regressed = False
        saw_any_prev = False
        for msg_db_path, dec_path, _fp, _salt in decrypted_shards:
            conn = sqlite3.connect(make_ro_sqlite_uri(dec_path), uri=True)
            try:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [r[0] for r in cur.fetchall()]
                local_md5 = dict(md5_to_chat_id)
                for n2i in ("Name2Id", "Name2ID", "name2id"):
                    if n2i in tables:
                        cur.execute(f"PRAGMA table_info({n2i})")
                        cols = {r[1] for r in cur.fetchall()}
                        u_col = "user_name" if "user_name" in cols else ("UsrName" if "UsrName" in cols else "username")
                        if u_col in cols:
                            cur.execute(f"SELECT {u_col} FROM {n2i}")
                            for (u,) in cur.fetchall():
                                if u:
                                    local_md5[md5_hex(str(u).strip())] = str(u).strip()
                for tbl in tables:
                    m = MSG_TABLE_RE.match(tbl)
                    if not m:
                        continue
                    cid = local_md5.get(m.group(2).lower())
                    if not cid or not cid.endswith("@chatroom"):
                        continue
                    prev = prev_watermarks.get((msg_db_path.name, cid))
                    if prev is None:
                        continue
                    saw_any_prev = True
                    cur.execute(f"PRAGMA table_info({tbl})")
                    cols = {r[1] for r in cur.fetchall()}
                    lid_col = "local_id" if "local_id" in cols else ("mesLocalID" if "mesLocalID" in cols else "rowid")
                    cur.execute(f"SELECT COALESCE(MAX({lid_col}), 0) FROM {tbl}")
                    cur_max = int(cur.fetchone()[0] or 0)
                    if cur_max > 0 and cur_max >= prev[0]:
                        saw_non_regressed = True
            finally:
                conn.close()
        return saw_any_prev and not saw_non_regressed

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

    def _load_shard_name2id(
        self,
        m_cur: sqlite3.Cursor,
        tables: list[str],
        md5_to_chat_id: dict[str, str],
    ) -> dict[int, str]:
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
        return id_to_wxid

    def _normalize_v4_row(
        self,
        r: sqlite3.Row,
        shard_name: str,
        chat_id: str,
        my_account_wxid: str,
        id_to_wxid: dict[int, str],
        u_cur: sqlite3.Cursor,
    ) -> tuple[int, int, str, str, str, str, int, int, str, bytes | None, bytes | None, str | None]:
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
        comp_c = bytes(r["comp"]) if isinstance(r["comp"], (bytes, bytearray)) else (
            bytes(r["content"]) if isinstance(r["content"], (bytes, bytearray)) else None
        )
        packed_b = bytes(r["packed"]) if isinstance(r["packed"], (bytes, bytearray)) else None
        mtype = int(r["mtype"] or 1)

        parsed = parse_wechat_message_payload(mtype, raw_c, comp_c, packed_b)
        if not resolved_sender:
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

        search_txt = _build_materialized_search_text(mtype, raw_c, comp_c, packed_b, pre_parsed=parsed)
        return (
            normalize_epoch_sec(r["ctime"]),
            int(r["lid"] or 0),
            shard_name,
            str(r["svrid"] or ""),
            chat_id,
            resolved_sender,
            is_from_me,
            mtype,
            raw_c,
            comp_c,
            packed_b,
            search_txt,
        )

    def _collect_from_message_shard(
        self,
        dec_msg_path: Path,
        shard_name: str,
        my_account_wxid: str,
        md5_to_chat_id: dict[str, str],
        u_cur: sqlite3.Cursor,
        collected_msgs: list[
            tuple[int, int, str, str, str, str, int, int, str, bytes | None, bytes | None, str | None]
        ],
        watermarks: dict[tuple[str, str], tuple[int, int]] | None = None,
    ) -> None:
        m_conn = sqlite3.connect(make_ro_sqlite_uri(dec_msg_path), uri=True)
        m_conn.row_factory = sqlite3.Row
        m_cur = m_conn.cursor()
        try:
            m_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in m_cur.fetchall()]
            id_to_wxid = self._load_shard_name2id(m_cur, tables, md5_to_chat_id)

            # Windows 3.x unified `MSG` table
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
                    per_chat_stats: dict[str, list[int]] = {}
                    for r in m_cur.fetchall():
                        talker = str(r["talker"] or "").strip()
                        lid = int(r["lid"] or 0)
                        is_from_me = 1 if int(r["is_sender"] or 0) == 1 else 0
                        raw_c = r["content"] or ""
                        comp_c = bytes(r["comp"]) if isinstance(r["comp"], (bytes, bytearray)) else None
                        extra_b = bytes(r["extra"]) if isinstance(r["extra"], (bytes, bytearray)) else None
                        mtype = int(r["mtype"] or 1)
                        parsed = parse_wechat_message_payload(mtype, raw_c, comp_c, extra_b)
                        sender_wxid = my_account_wxid if is_from_me else (parsed.sender_wxid_from_prefix or "")
                        if sender_wxid and not sender_wxid.endswith("@chatroom"):
                            u_cur.execute(
                                "INSERT OR IGNORE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, '')",
                                (talker, sender_wxid),
                            )
                        search_txt = _build_materialized_search_text(mtype, raw_c, comp_c, extra_b, pre_parsed=parsed)
                        collected_msgs.append(
                            (
                                normalize_epoch_sec(r["ctime"]),
                                lid,
                                shard_name,
                                str(r["svrid"] or ""),
                                talker,
                                sender_wxid,
                                is_from_me,
                                mtype,
                                raw_c,
                                comp_c,
                                extra_b,
                                search_txt,
                            )
                        )
                        st = per_chat_stats.setdefault(talker, [0, 0])
                        st[0] = max(st[0], lid)
                        st[1] += 1
                    if watermarks is not None:
                        for cid, (mx, cnt) in per_chat_stats.items():
                            watermarks[(shard_name, cid)] = (mx, cnt)

            # WeChat 4.x (`Msg_<md5>`) and macOS 3.x (`Chat_<md5>`) tables
            for tbl in tables:
                m_match = MSG_TABLE_RE.match(tbl)
                if not m_match:
                    continue
                prefix_kind = m_match.group(1).lower()
                table_md5 = m_match.group(2).lower()
                chat_id = md5_to_chat_id.get(table_md5)
                if not chat_id or not chat_id.endswith("@chatroom"):
                    continue

                m_cur.execute(f"PRAGMA table_info({tbl})")
                cols = {r[1] for r in m_cur.fetchall()}
                max_lid = 0
                row_cnt = 0

                if prefix_kind == "msg" or "local_type" in cols or "message_content" in cols:
                    lid_col, sel_sql = self._build_v4_table_select_sql(tbl, cols)
                    m_cur.execute(sel_sql)
                    for r in m_cur.fetchall():
                        tup = self._normalize_v4_row(r, shard_name, chat_id, my_account_wxid, id_to_wxid, u_cur)
                        collected_msgs.append(tup)
                        max_lid = max(max_lid, tup[1])
                        row_cnt += 1
                else:
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
                        lid = int(r["lid"] or 0)
                        is_from_me = 1 if int(r["des"] or 1) == 0 else 0
                        raw_c = r["content"] or ""
                        mtype = int(r["mtype"] or 1)
                        parsed = parse_wechat_message_payload(mtype, raw_c, None, None)
                        sender_wxid = my_account_wxid if is_from_me else (parsed.sender_wxid_from_prefix or "")
                        if sender_wxid and not sender_wxid.endswith("@chatroom"):
                            u_cur.execute(
                                "INSERT OR IGNORE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, '')",
                                (chat_id, sender_wxid),
                            )
                        search_txt = _build_materialized_search_text(mtype, raw_c, None, None, pre_parsed=parsed)
                        collected_msgs.append(
                            (
                                normalize_epoch_sec(r["ctime"]),
                                lid,
                                shard_name,
                                str(r["svrid"] or ""),
                                chat_id,
                                sender_wxid,
                                is_from_me,
                                mtype,
                                raw_c,
                                None,
                                None,
                                search_txt,
                            )
                        )
                        max_lid = max(max_lid, lid)
                        row_cnt += 1

                if watermarks is not None:
                    watermarks[(shard_name, chat_id)] = (max_lid, row_cnt)
        finally:
            m_conn.close()

    @staticmethod
    def _build_v4_table_select_sql(tbl: str, cols: set[str], where_clause: str = "") -> tuple[str, str]:
        lid_col = "local_id" if "local_id" in cols else "rowid"
        svrid_col = "server_id" if "server_id" in cols else "0"
        type_col = "local_type" if "local_type" in cols else "message_type"
        time_col = "create_time" if "create_time" in cols else ("createTime" if "createTime" in cols else "timestamp")
        sender_id_col = "real_sender_id" if "real_sender_id" in cols else "NULL"
        is_me_col = "is_send" if "is_send" in cols else ("Des" if "Des" in cols else "NULL")
        content_col = "message_content" if "message_content" in cols else "content"
        comp_col = "compress_content" if "compress_content" in cols else "NULL"
        packed_col = "packed_info_data" if "packed_info_data" in cols else "NULL"
        where_sql = f" WHERE {where_clause}" if where_clause else ""
        sql = f"""
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
            FROM {tbl}{where_sql}
        """
        return lid_col, sql

    def _sync_single_message_shard(
        self,
        dec_msg_path: Path,
        shard_name: str,
        my_account_wxid: str,
        md5_to_chat_id: dict[str, str],
        prev_watermarks: dict[tuple[str, str], tuple[int, int]],
        updated_watermarks: dict[tuple[str, str], tuple[int, int]],
        u_cur: sqlite3.Cursor,
        new_msgs: list[
            tuple[int, int, str, str, str, str, int, int, str, bytes | None, bytes | None, str | None]
        ],
    ) -> bool:
        """Incrementally sync one changed shard, preserving existing `msg_id`s and reconciling recalls/deletes."""
        mutated_existing = False
        m_conn = sqlite3.connect(make_ro_sqlite_uri(dec_msg_path), uri=True)
        m_conn.row_factory = sqlite3.Row
        m_cur = m_conn.cursor()
        try:
            m_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in m_cur.fetchall()]
            id_to_wxid = self._load_shard_name2id(m_cur, tables, md5_to_chat_id)
            seen_chats_in_shard: set[str] = set()

            # 1. Windows 3.x unified `MSG` table
            if "MSG" in tables:
                m_cur.execute("PRAGMA table_info(MSG)")
                cols = {r[1] for r in m_cur.fetchall()}
                if "StrTalker" in cols:
                    m_cur.execute(
                        """
                        SELECT StrTalker AS talker, COALESCE(MAX(localId), 0) AS mx, COUNT(*) AS cnt
                        FROM MSG
                        WHERE StrTalker LIKE '%@chatroom'
                        GROUP BY StrTalker
                        """
                    )
                    for stat_row in m_cur.fetchall():
                        cid = str(stat_row["talker"] or "").strip()
                        cur_max = int(stat_row["mx"] or 0)
                        cur_cnt = int(stat_row["cnt"] or 0)
                        seen_chats_in_shard.add(cid)
                        prev_max, prev_cnt = prev_watermarks.get((shard_name, cid), (0, 0))
                        updated_watermarks[(shard_name, cid)] = (cur_max, cur_cnt)

                        if cur_cnt < prev_cnt or cur_max < prev_max:
                            m_cur.execute("SELECT localId FROM MSG WHERE StrTalker = ?", (cid,))
                            live_lids = {int(x[0]) for x in m_cur.fetchall()}
                            u_cur.execute(
                                "SELECT shard_local_id FROM wx_messages WHERE shard_name = ? AND chat_id = ?",
                                (shard_name, cid),
                            )
                            stale_lids = [int(x[0]) for x in u_cur.fetchall() if int(x[0]) not in live_lids]
                            if stale_lids:
                                u_cur.executemany(
                                    "DELETE FROM wx_messages WHERE shard_name = ? AND chat_id = ? AND shard_local_id = ?",
                                    [(shard_name, cid, lid) for lid in stale_lids],
                                )
                                mutated_existing = True

                        lower_lid = max(0, prev_max - RECENT_RECALL_RECHECK_WINDOW)
                        m_cur.execute(
                            """
                            SELECT
                                localId AS lid, CAST(MsgSvrID AS TEXT) AS svrid, StrTalker AS talker,
                                IsSender AS is_sender, Type AS mtype, CreateTime AS ctime,
                                StrContent AS content, CompressContent AS comp, BytesExtra AS extra
                            FROM MSG
                            WHERE StrTalker = ? AND localId > ?
                            """,
                            (cid, lower_lid if cur_max >= prev_max else 0),
                        )
                        u_cur.execute(
                            "SELECT shard_local_id, local_type, raw_content FROM wx_messages WHERE shard_name = ? AND chat_id = ? AND shard_local_id > ?",
                            (shard_name, cid, lower_lid if cur_max >= prev_max else 0),
                        )
                        existing_map = {int(x[0]): (int(x[1] or 1), x[2] or "") for x in u_cur.fetchall()}
                        for r in m_cur.fetchall():
                            lid = int(r["lid"] or 0)
                            is_from_me = 1 if int(r["is_sender"] or 0) == 1 else 0
                            raw_c = r["content"] or ""
                            comp_c = bytes(r["comp"]) if isinstance(r["comp"], (bytes, bytearray)) else None
                            extra_b = bytes(r["extra"]) if isinstance(r["extra"], (bytes, bytearray)) else None
                            mtype = int(r["mtype"] or 1)
                            parsed = parse_wechat_message_payload(mtype, raw_c, comp_c, extra_b)
                            sender_wxid = my_account_wxid if is_from_me else (parsed.sender_wxid_from_prefix or "")
                            search_txt = _build_materialized_search_text(mtype, raw_c, comp_c, extra_b, pre_parsed=parsed)
                            if lid in existing_map:
                                if existing_map[lid] != (mtype, raw_c):
                                    u_cur.execute(
                                        """
                                        UPDATE wx_messages
                                        SET local_type = ?, raw_content = ?, compress_content = ?, packed_info_data = ?, search_text = ?
                                        WHERE shard_name = ? AND chat_id = ? AND shard_local_id = ?
                                        """,
                                        (mtype, raw_c, comp_c, extra_b, search_txt, shard_name, cid, lid),
                                    )
                                    mutated_existing = True
                            else:
                                if sender_wxid and not sender_wxid.endswith("@chatroom"):
                                    u_cur.execute(
                                        "INSERT OR IGNORE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, '')",
                                        (cid, sender_wxid),
                                    )
                                new_msgs.append(
                                    (
                                        normalize_epoch_sec(r["ctime"]),
                                        lid,
                                        shard_name,
                                        str(r["svrid"] or ""),
                                        cid,
                                        sender_wxid,
                                        is_from_me,
                                        mtype,
                                        raw_c,
                                        comp_c,
                                        extra_b,
                                        search_txt,
                                    )
                                )

            # 2. WeChat 4.x (`Msg_<md5>`) and macOS 3.x (`Chat_<md5>`) tables
            for tbl in tables:
                m_match = MSG_TABLE_RE.match(tbl)
                if not m_match:
                    continue
                prefix_kind = m_match.group(1).lower()
                table_md5 = m_match.group(2).lower()
                chat_id = md5_to_chat_id.get(table_md5)
                if not chat_id or not chat_id.endswith("@chatroom"):
                    continue

                seen_chats_in_shard.add(chat_id)
                m_cur.execute(f"PRAGMA table_info({tbl})")
                cols = {r[1] for r in m_cur.fetchall()}
                is_v4 = prefix_kind == "msg" or "local_type" in cols or "message_content" in cols
                lid_col = ("local_id" if "local_id" in cols else "rowid") if is_v4 else "mesLocalID"

                m_cur.execute(f"SELECT COALESCE(MAX({lid_col}), 0), COUNT(*) FROM {tbl}")
                cur_max, cur_cnt = (int(x or 0) for x in m_cur.fetchone())
                prev_max, prev_cnt = prev_watermarks.get((shard_name, chat_id), (0, 0))
                updated_watermarks[(shard_name, chat_id)] = (cur_max, cur_cnt)

                # Reconcile deleted messages (e.g. user cleared chat history in this group)
                if cur_cnt < prev_cnt or cur_max < prev_max:
                    m_cur.execute(f"SELECT {lid_col} FROM {tbl}")
                    live_lids = {int(x[0]) for x in m_cur.fetchall()}
                    u_cur.execute(
                        "SELECT shard_local_id FROM wx_messages WHERE shard_name = ? AND chat_id = ?",
                        (shard_name, chat_id),
                    )
                    stale_lids = [int(x[0]) for x in u_cur.fetchall() if int(x[0]) not in live_lids]
                    if stale_lids:
                        u_cur.executemany(
                            "DELETE FROM wx_messages WHERE shard_name = ? AND chat_id = ? AND shard_local_id = ?",
                            [(shard_name, chat_id, lid) for lid in stale_lids],
                        )
                        mutated_existing = True

                # Query only newly appended rows + recent recall window
                scan_after_lid = max(0, prev_max - RECENT_RECALL_RECHECK_WINDOW) if cur_max >= prev_max else 0
                u_cur.execute(
                    "SELECT shard_local_id, local_type, raw_content FROM wx_messages WHERE shard_name = ? AND chat_id = ? AND shard_local_id > ?",
                    (shard_name, chat_id, scan_after_lid),
                )
                existing_recent = {int(x[0]): (int(x[1] or 1), x[2] or "") for x in u_cur.fetchall()}

                if is_v4:
                    _lc, sel_sql = self._build_v4_table_select_sql(tbl, cols, where_clause=f"{lid_col} > ?")
                    m_cur.execute(sel_sql, (scan_after_lid,))
                    for r in m_cur.fetchall():
                        tup = self._normalize_v4_row(r, shard_name, chat_id, my_account_wxid, id_to_wxid, u_cur)
                        lid = tup[1]
                        if lid in existing_recent:
                            if existing_recent[lid] != (tup[7], tup[8]):
                                u_cur.execute(
                                    """
                                    UPDATE wx_messages
                                    SET local_type = ?, raw_content = ?, compress_content = ?, packed_info_data = ?, search_text = ?
                                    WHERE shard_name = ? AND chat_id = ? AND shard_local_id = ?
                                    """,
                                    (tup[7], tup[8], tup[9], tup[10], tup[11], shard_name, chat_id, lid),
                                )
                                mutated_existing = True
                        else:
                            new_msgs.append(tup)
                else:
                    m_cur.execute(
                        f"""
                        SELECT
                            mesLocalID AS lid, CAST(mesSvrID AS TEXT) AS svrid, messageType AS mtype,
                            msgCreateTime AS ctime, mesDes AS des, msgContent AS content
                        FROM {tbl}
                        WHERE mesLocalID > ?
                        """,
                        (scan_after_lid,),
                    )
                    for r in m_cur.fetchall():
                        lid = int(r["lid"] or 0)
                        is_from_me = 1 if int(r["des"] or 1) == 0 else 0
                        raw_c = r["content"] or ""
                        mtype = int(r["mtype"] or 1)
                        parsed = parse_wechat_message_payload(mtype, raw_c, None, None)
                        sender_wxid = my_account_wxid if is_from_me else (parsed.sender_wxid_from_prefix or "")
                        search_txt = _build_materialized_search_text(mtype, raw_c, None, None, pre_parsed=parsed)
                        if lid in existing_recent:
                            if existing_recent[lid] != (mtype, raw_c):
                                u_cur.execute(
                                    """
                                    UPDATE wx_messages
                                    SET local_type = ?, raw_content = ?, search_text = ?
                                    WHERE shard_name = ? AND chat_id = ? AND shard_local_id = ?
                                    """,
                                    (mtype, raw_c, search_txt, shard_name, chat_id, lid),
                                )
                                mutated_existing = True
                        else:
                            if sender_wxid and not sender_wxid.endswith("@chatroom"):
                                u_cur.execute(
                                    "INSERT OR IGNORE INTO wx_group_members (chat_id, member_wxid, group_nickname) VALUES (?, ?, '')",
                                    (chat_id, sender_wxid),
                                )
                            new_msgs.append(
                                (
                                    normalize_epoch_sec(r["ctime"]),
                                    lid,
                                    shard_name,
                                    str(r["svrid"] or ""),
                                    chat_id,
                                    sender_wxid,
                                    is_from_me,
                                    mtype,
                                    raw_c,
                                    None,
                                    None,
                                    search_txt,
                                )
                            )

            # Reconcile any group table that was completely dropped from this shard
            for (w_shard, w_cid) in list(prev_watermarks.keys()):
                if w_shard == shard_name and w_cid not in seen_chats_in_shard:
                    u_cur.execute("DELETE FROM wx_messages WHERE shard_name = ? AND chat_id = ?", (shard_name, w_cid))
                    u_cur.execute(
                        "DELETE FROM wx_table_watermarks WHERE shard_name = ? AND chat_id = ?",
                        (shard_name, w_cid),
                    )
                    mutated_existing = True
        finally:
            m_conn.close()

        return mutated_existing

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
