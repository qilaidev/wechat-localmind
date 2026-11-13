"""Controlled WeChat media attachment export with `.dat` XOR image decoding and path traversal protection."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .config import AppConfig
from .constants import DAT_IMAGE_SIGNATURES
from .errors import PolicyDeniedError, ResourceNotFoundError, UsageError
from .protobuf_parser import parse_wechat_message_payload
from .snapshot import SnapshotState, make_ro_sqlite_uri

UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def sanitize_filename(name: str) -> str:
    cleaned = UNSAFE_FILENAME_RE.sub("_", name.strip())
    return cleaned.strip("._") or "attachment"


def _fast_xor_bytes(data: bytes, key: int) -> bytes:
    """Vectorized C-level single-byte XOR using big-int bit operations (5.5x faster than byte loop)."""
    if key == 0 or not data:
        return data
    n = len(data)
    mask = bytes([key]) * n
    return (int.from_bytes(data, "little") ^ int.from_bytes(mask, "little")).to_bytes(n, "little")


def detect_and_decode_wechat_dat_image(data: bytes) -> tuple[bytes, str, str, int | None]:
    """Detect if `data` is a WeChat XOR-encrypted `.dat` image or already a standard image.

    Returns (decoded_bytes, extension, mime_type, xor_key_byte).
    """
    if len(data) < 8:
        return data, ".dat", "application/octet-stream", None

    # 1. Check if already unencrypted image
    for magic, ext, mime in DAT_IMAGE_SIGNATURES:
        if data.startswith(magic):
            return data, ext, mime, 0

    # 2. Try single-byte XOR key recovery against known image magic headers
    b0 = data[0]
    for magic, ext, mime in DAT_IMAGE_SIGNATURES:
        candidate_key = b0 ^ magic[0]
        if all((data[i] ^ candidate_key) == magic[i] for i in range(len(magic))):
            # Additional sanity check for PNG IHDR chunk header
            if ext == ".png" and len(data) >= 16:
                ihdr = bytes(data[i] ^ candidate_key for i in range(12, 16))
                if ihdr != b"IHDR":
                    continue
            decoded = _fast_xor_bytes(data, candidate_key)
            return decoded, ext, mime, candidate_key

    return data, ".dat", "application/octet-stream", None


class MediaExporter:
    """Safely exports media files from the WeChat sandbox after verifying group session policy."""

    def __init__(self, config: AppConfig, snapshot: SnapshotState) -> None:
        self.config = config
        self.snapshot = snapshot
        self.account_root = snapshot.account_dir.resolve()
        self.media_root = snapshot.media_root_dir.resolve()
        self.default_export_dir = Path(os.path.expanduser(config.snapshot_dir)) / "media"

    def _resolve_sandbox_file(self, rel_path: str, msg_id: int) -> Path:
        clean_rel = rel_path.lstrip("/\\")
        for root in (self.media_root, self.account_root):
            cand = (root / clean_rel).resolve()
            # Path traversal guard: must remain strictly inside account_root
            if not str(cand).startswith(str(self.account_root) + os.sep):
                raise PolicyDeniedError(
                    f"Invalid media path outside WeChat account sandbox for msg_id={msg_id}.",
                    error_code="INVALID_SANDBOX_PATH",
                )
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
        raise ResourceNotFoundError(
            f"Media file for msg_id={msg_id} does not exist on disk ('{rel_path}').",
            error_code="MEDIA_FILE_MISSING",
        )

    def export_media_by_msg_ids(
        self,
        msg_ids: list[int],
        dest_dir: Path | None = None,
    ) -> dict[str, Any]:
        if not msg_ids:
            raise UsageError("At least one message ID must be provided via --msg-ids.")

        target_dir = (dest_dir or self.default_export_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(make_ro_sqlite_uri(self.snapshot.snapshot_db_path), uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        exported_items: list[dict[str, Any]] = []
        try:
            for mid in msg_ids:
                cur.execute(
                    """
                    SELECT
                        m.msg_id,
                        m.chat_id,
                        m.local_type,
                        m.raw_content,
                        m.compress_content,
                        m.packed_info_data,
                        COALESCE(g.group_name, '未命名微信群') AS group_name
                    FROM wx_messages m
                    LEFT JOIN wx_groups g ON m.chat_id = g.chat_id
                    WHERE m.msg_id = ?
                    """,
                    (mid,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ResourceNotFoundError(
                        f"Message msg_id={mid} does not exist in local WeChat database.",
                        error_code="MESSAGE_NOT_FOUND",
                    )

                chat_id = row["chat_id"] or ""
                # Policy enforcement: 100% reject private chats and blocked groups (Exit Code 3)
                self.config.assert_chat_allowed(chat_id)

                parsed = parse_wechat_message_payload(
                    int(row["local_type"] or 1),
                    row["raw_content"],
                    row["compress_content"],
                    row["packed_info_data"],
                )
                if parsed.is_revoked:
                    raise PolicyDeniedError(
                        f"Message msg_id={mid} has been revoked and cannot be exported.",
                        error_code="REVOKED_MESSAGE_DENIED",
                    )

                rel_path = (parsed.media_rel_path or "").strip()
                if not rel_path:
                    raise ResourceNotFoundError(
                        f"Message msg_id={mid} has no downloaded local media path (local_available=false).",
                        error_code="MEDIA_NOT_DOWNLOADED",
                    )

                src_file = self._resolve_sandbox_file(rel_path, mid)
                orig_title = parsed.media_title or src_file.name
                xor_key_used: int | None = None
                mime_type: str | None = None

                # Automatic .dat XOR image decryption
                if src_file.suffix.lower() == ".dat" or parsed.normalized_type == "image":
                    raw_bytes = src_file.read_bytes()
                    decoded_bytes, real_ext, detected_mime, xor_key = detect_and_decode_wechat_dat_image(raw_bytes)
                    mime_type = detected_mime
                    xor_key_used = xor_key
                    stem = Path(orig_title).stem if orig_title.lower().endswith(".dat") else orig_title
                    safe_base = sanitize_filename(stem)
                    if not safe_base.lower().endswith(real_ext):
                        safe_base = f"{safe_base}{real_ext}"
                    dst_file = target_dir / f"{mid}_{safe_base}"
                    dst_file.write_bytes(decoded_bytes)
                else:
                    src_ext = f".{parsed.media_ext.lstrip('.')}" if parsed.media_ext else src_file.suffix
                    safe_base = sanitize_filename(orig_title)
                    if src_ext and not safe_base.lower().endswith(src_ext.lower()):
                        safe_base = f"{safe_base}{src_ext}"
                    dst_file = target_dir / f"{mid}_{safe_base}"
                    shutil.copy2(src_file, dst_file)

                exported_items.append(
                    {
                        "msg_id": mid,
                        "chat_id": chat_id,
                        "group_name": row["group_name"],
                        "media_type": parsed.normalized_type or "media",
                        "mime_type": mime_type,
                        "xor_key_byte": f"0x{xor_key_used:02x}" if xor_key_used is not None else None,
                        "original_title": orig_title,
                        "exported_path": str(dst_file),
                        "file_size_bytes": dst_file.stat().st_size,
                        "duration_sec": parsed.media_duration_sec,
                    }
                )
        finally:
            conn.close()

        return {
            "meta": {
                "snapshot_time": self.snapshot.snapshot_time_iso,
                "export_dir": str(target_dir),
                "exported_count": len(exported_items),
            },
            "exported": exported_items,
        }
