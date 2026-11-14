"""SQLCipher 3 (WeChat 3.x) & SQLCipher 4 (WeChat 4.x WCDB) pure-Python decryptor and key resolver."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import Any

from .constants import (
    SQLCIPHER3_HMAC_SIZE,
    SQLCIPHER3_KDF_ITER,
    SQLCIPHER3_RESERVE_SIZE,
    SQLCIPHER4_HMAC_SIZE,
    SQLCIPHER4_KDF_ITER,
    SQLCIPHER4_RESERVE_SIZE,
    SQLCIPHER_IV_SIZE,
    SQLCIPHER_PAGE_SIZE,
    SQLCIPHER_SALT_SIZE,
    SQLITE_FILE_HEADER,
)
from .errors import EnvOrPermissionError, InternalDatabaseError, UsageError

HEX_KEY_RE = re.compile(r"^(?:0x|x')?([0-9a-fA-F]{64}|[0-9a-fA-F]{96})'?$")
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24


def _cc_aes_crypt(op: int, options: int, key: bytes, iv: bytes | None, data: bytes) -> bytes | None:
    """Zero-dependency macOS native CommonCrypto (`CCCrypt`) AES engine (CBC & ECB)."""
    try:
        import ctypes

        lib = getattr(_cc_aes_crypt, "_lib", None)
        if lib is None:
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            fn = lib.CCCrypt
            fn.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_size_t),
            ]
            fn.restype = ctypes.c_int32
            setattr(_cc_aes_crypt, "_lib", lib)
            setattr(_cc_aes_crypt, "_fn", fn)
        fn = getattr(_cc_aes_crypt, "_fn")
        n = len(data)
        out = ctypes.create_string_buffer(n)
        moved = ctypes.c_size_t(0)
        # op: 0=kCCEncrypt, 1=kCCDecrypt; alg: 0=kCCAlgorithmAES; options: 0=CBC, 2=kCCOptionECBMode
        st = fn(op, 0, options, key, len(key), iv, data, n, out, n, ctypes.byref(moved))
        if st == 0 and moved.value == n:
            return out.raw
    except Exception:
        pass
    return None


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Decrypt AES-256-CBC using PyCryptodome, cryptography, or macOS native CommonCrypto."""
    try:
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_CBC, iv)
        return cipher.decrypt(ciphertext)
    except ImportError:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()
    except ImportError:
        pass

    cc_res = _cc_aes_crypt(1, 0, key, iv, ciphertext)
    if cc_res is not None:
        return cc_res

    raise EnvOrPermissionError(
        "No AES-256-CBC backend available (checked PyCryptodome, cryptography, and macOS CommonCrypto).",
        error_code="CRYPTO_DEPENDENCY_MISSING",
    )


def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Encrypt AES-256-CBC (used in unit tests to construct real SQLCipher 3 & 4 fixtures)."""
    try:
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_CBC, iv)
        return cipher.encrypt(plaintext)
    except ImportError:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        return encryptor.update(plaintext) + encryptor.finalize()
    except ImportError:
        pass

    cc_res = _cc_aes_crypt(0, 0, key, iv, plaintext)
    if cc_res is not None:
        return cc_res

    raise RuntimeError("No AES-256-CBC backend available for encryption.")


def _aes_ecb_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """Decrypt AES-128/256-ECB (used for WeChat 4.x V1/V2 `.dat` image headers)."""
    if not ciphertext:
        return b""
    try:
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.decrypt(ciphertext)
    except ImportError:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key), modes.ECB())
        decryptor = cipher.decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()
    except ImportError:
        pass

    cc_res = _cc_aes_crypt(1, 2, key, None, ciphertext)
    if cc_res is not None:
        return cc_res

    raise EnvOrPermissionError(
        "No AES-ECB backend available (checked PyCryptodome, cryptography, and macOS CommonCrypto).",
        error_code="CRYPTO_DEPENDENCY_MISSING",
    )


def _aes_ecb_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt AES-128/256-ECB (used in unit tests for WeChat 4.x V1/V2 `.dat` fixtures)."""
    if not plaintext:
        return b""
    try:
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.encrypt(plaintext)
    except ImportError:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key), modes.ECB())
        encryptor = cipher.encryptor()
        return encryptor.update(plaintext) + encryptor.finalize()
    except ImportError:
        pass

    cc_res = _cc_aes_crypt(0, 2, key, None, plaintext)
    if cc_res is not None:
        return cc_res

    raise RuntimeError("No AES-ECB backend available for encryption.")


def is_plaintext_sqlite(file_path: Path) -> bool:
    """Return True if file begins with standard 'SQLite format 3\\x00' header."""
    try:
        with open(file_path, "rb") as f:
            hdr = f.read(16)
        return hdr == SQLITE_FILE_HEADER
    except Exception:
        return False


def parse_hex_key(raw_hex: str) -> tuple[bytes, bytes | None]:
    """Parse 64-char hex key (or 96-char hex key+salt) into (32_byte_key, optional_16_byte_salt)."""
    cleaned = (raw_hex or "").strip()
    m = HEX_KEY_RE.match(cleaned)
    if not m:
        raise UsageError(
            f"Invalid SQLCipher key format '{raw_hex}'. Expected 64-char hex (32 bytes) or 96-char hex (key+salt)."
        )
    hex_body = m.group(1)
    raw_bytes = bytes.fromhex(hex_body)
    if len(raw_bytes) == 32:
        return raw_bytes, None
    return raw_bytes[:32], raw_bytes[32:48]


def derive_sqlcipher4_keys(
    key_material: bytes,
    salt: bytes,
    is_raw_key: bool = True,
) -> tuple[bytes, bytes]:
    """Derive (enc_key, mac_key) for WeChat 4.x SQLCipher 4 (HMAC-SHA512, reserve=80)."""
    if is_raw_key:
        enc_key = key_material[:32]
    else:
        enc_key = hashlib.pbkdf2_hmac("sha512", key_material, salt, SQLCIPHER4_KDF_ITER, dklen=32)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    return enc_key, mac_key


def derive_sqlcipher3_keys(
    key_material: bytes,
    salt: bytes,
    is_raw_key: bool = True,
) -> tuple[bytes, bytes]:
    """Derive (enc_key, mac_key) for WeChat 3.x SQLCipher 3 (HMAC-SHA1, reserve=48)."""
    if is_raw_key:
        enc_key = key_material[:32]
    else:
        enc_key = hashlib.pbkdf2_hmac("sha1", key_material, salt, SQLCIPHER3_KDF_ITER, dklen=32)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha1", enc_key, mac_salt, 2, dklen=32)
    return enc_key, mac_key


def _verify_page_hmac(
    page_buf: bytes,
    pgno: int,
    mac_key: bytes,
    cipher_version: int,
    page_size: int = SQLCIPHER_PAGE_SIZE,
) -> bool:
    """Verify per-page SQLCipher 4 (HMAC-SHA512) or SQLCipher 3 (HMAC-SHA1) MAC."""
    if len(page_buf) < page_size:
        return False
    reserve_sz = SQLCIPHER4_RESERVE_SIZE if cipher_version == 4 else SQLCIPHER3_RESERVE_SIZE
    hmac_sz = SQLCIPHER4_HMAC_SIZE if cipher_version == 4 else SQLCIPHER3_HMAC_SIZE
    hmac_algo = hashlib.sha512 if cipher_version == 4 else hashlib.sha1
    start_off = SQLCIPHER_SALT_SIZE if pgno == 1 else 0
    auth_end = page_size - reserve_sz + SQLCIPHER_IV_SIZE
    mac_data = page_buf[start_off:auth_end] + struct.pack("<I", pgno)
    expected_hmac = hmac.new(mac_key, mac_data, hmac_algo).digest()
    stored_hmac = page_buf[auth_end : auth_end + hmac_sz]
    return hmac.compare_digest(expected_hmac, stored_hmac)


def verify_page1_sqlcipher4(
    page1: bytes,
    key_material: bytes,
    is_raw_key: bool = True,
    page_size: int = SQLCIPHER_PAGE_SIZE,
) -> tuple[bool, bytes]:
    """Verify Page 1 HMAC-SHA512 for SQLCipher 4 (WeChat 4.x). Returns (valid, enc_key)."""
    if len(page1) < page_size:
        return False, b""
    salt = page1[:SQLCIPHER_SALT_SIZE]
    enc_key, mac_key = derive_sqlcipher4_keys(key_material, salt, is_raw_key=is_raw_key)
    return _verify_page_hmac(page1[:page_size], 1, mac_key, 4, page_size), enc_key


def verify_page1_sqlcipher3(
    page1: bytes,
    key_material: bytes,
    is_raw_key: bool = True,
    page_size: int = SQLCIPHER_PAGE_SIZE,
) -> tuple[bool, bytes]:
    """Verify Page 1 HMAC-SHA1 for SQLCipher 3 (WeChat 3.x). Returns (valid, enc_key)."""
    if len(page1) < page_size:
        return False, b""
    salt = page1[:SQLCIPHER_SALT_SIZE]
    enc_key, mac_key = derive_sqlcipher3_keys(key_material, salt, is_raw_key=is_raw_key)
    return _verify_page_hmac(page1[:page_size], 1, mac_key, 3, page_size), enc_key


def _extract_sqlite_header_page_size(decrypted_page1: bytes) -> int:
    if len(decrypted_page1) < 24:
        return 0
    raw_sz = struct.unpack(">H", decrypted_page1[16:18])[0]
    return 65536 if raw_sz == 1 else raw_sz


def _detect_cipher_params(
    initial_bytes: bytes,
    candidate_hex_keys: list[str],
) -> tuple[int, bytes, bytes, str, bool, int] | None:
    """Probe Page 1 against candidate hex keys across SQLCipher 4/3, raw/KDF, and SQLite page sizes.

    Returns (cipher_version, enc_key, mac_key, matched_hex_64, is_raw_key, page_size) or None.
    """
    if len(initial_bytes) < 1024:
        return None

    salt = initial_bytes[:SQLCIPHER_SALT_SIZE]
    candidate_page_sizes = [
        sz for sz in (4096, 1024, 8192, 16384, 32768, 65536) if len(initial_bytes) >= sz
    ]

    for raw_hex in candidate_hex_keys:
        try:
            key_bytes, key_salt = parse_hex_key(raw_hex)
        except UsageError:
            continue

        if key_salt is not None and salt != key_salt:
            continue

        norm_hex = key_bytes.hex()
        for page_sz in candidate_page_sizes:
            p1 = initial_bytes[:page_sz]
            for is_raw in (True, False):
                enc4, mac4 = derive_sqlcipher4_keys(key_bytes, salt, is_raw_key=is_raw)
                if _verify_page_hmac(p1, 1, mac4, 4, page_sz):
                    usable = page_sz - SQLCIPHER4_RESERVE_SIZE
                    iv = p1[usable : usable + SQLCIPHER_IV_SIZE]
                    dec = SQLITE_FILE_HEADER + _aes_cbc_decrypt(enc4, iv, p1[SQLCIPHER_SALT_SIZE:usable])
                    hdr_pgsz = _extract_sqlite_header_page_size(dec)
                    if hdr_pgsz in (0, page_sz):
                        return 4, enc4, mac4, norm_hex, is_raw, page_sz

                enc3, mac3 = derive_sqlcipher3_keys(key_bytes, salt, is_raw_key=is_raw)
                if _verify_page_hmac(p1, 1, mac3, 3, page_sz):
                    usable = page_sz - SQLCIPHER3_RESERVE_SIZE
                    iv = p1[usable : usable + SQLCIPHER_IV_SIZE]
                    dec = SQLITE_FILE_HEADER + _aes_cbc_decrypt(enc3, iv, p1[SQLCIPHER_SALT_SIZE:usable])
                    hdr_pgsz = _extract_sqlite_header_page_size(dec)
                    if hdr_pgsz in (0, page_sz):
                        return 3, enc3, mac3, norm_hex, is_raw, page_sz
    return None


def detect_cipher_version_and_key(page1: bytes, candidate_hex_keys: list[str]) -> tuple[int, bytes] | None:
    """Probe Page 1 against candidate hex keys across SQLCipher 4 and SQLCipher 3 (raw & passphrase KDF).

    Returns (cipher_version, derived_enc_key) or None if no key matches.
    """
    params = _detect_cipher_params(page1, candidate_hex_keys)
    if params is None:
        return None
    return params[0], params[1]


def _sqlite_wal_checksum(data: bytes, s0: int, s1: int, big_endian_words: bool) -> tuple[int, int]:
    """Compute cumulative SQLite WAL 64-bit checksum over 8-byte-aligned buffer."""
    word_count = len(data) // 4
    fmt = f">{word_count}I" if big_endian_words else f"<{word_count}I"
    words = struct.unpack(fmt, data)
    for idx in range(0, word_count, 2):
        s0 = (s0 + words[idx] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[idx + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def _parse_committed_sqlcipher_wal(
    wal_path: Path,
    expected_page_size: int | None = None,
) -> tuple[dict[int, bytes], int | None]:
    """Parse an offline-copied SQLite/SQLCipher `-wal` file and extract valid committed page frames.

    Verifies WAL magic, header checksum, per-frame salt continuity, cumulative frame checksums,
    and transaction commit boundaries (`db_size_after_commit > 0`), discarding uncommitted tail frames.
    """
    if not wal_path.is_file():
        return {}, None

    raw_wal = wal_path.read_bytes()
    if len(raw_wal) < WAL_HEADER_SIZE:
        return {}, None

    magic, _fmt_ver, wal_pgsz, _ckpt_seq, hdr_s1, hdr_s2, hdr_c0, hdr_c1 = struct.unpack(
        ">IIIIIIII", raw_wal[:WAL_HEADER_SIZE]
    )
    if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE):
        return {}, None
    if wal_pgsz not in (1024, 2048, 4096, 8192, 16384, 32768, 65536):
        return {}, None
    if expected_page_size is not None and wal_pgsz != expected_page_size:
        return {}, None

    big_endian = magic == WAL_MAGIC_BE
    s0, s1 = _sqlite_wal_checksum(raw_wal[:24], 0, 0, big_endian)
    if (s0, s1) != (hdr_c0, hdr_c1):
        return {}, None

    committed_pages: dict[int, bytes] = {}
    committed_db_pages: int | None = None
    pending_tx_pages: dict[int, bytes] = {}

    frame_stride = WAL_FRAME_HEADER_SIZE + wal_pgsz
    offset = WAL_HEADER_SIZE
    total_len = len(raw_wal)

    while offset + frame_stride <= total_len:
        fhdr = raw_wal[offset : offset + WAL_FRAME_HEADER_SIZE]
        fpage = raw_wal[offset + WAL_FRAME_HEADER_SIZE : offset + frame_stride]
        pgno, db_sz_commit, fs1, fs2, fc0, fc1 = struct.unpack(">IIIIII", fhdr)

        # Salt mismatch indicates recycled frame from an older checkpoint generation -> stop WAL replay
        if (fs1, fs2) != (hdr_s1, hdr_s2) or pgno == 0:
            break

        s0, s1 = _sqlite_wal_checksum(fhdr[:8], s0, s1, big_endian)
        s0, s1 = _sqlite_wal_checksum(fpage, s0, s1, big_endian)
        if (s0, s1) != (fc0, fc1):
            break

        pending_tx_pages[pgno] = fpage
        if db_sz_commit > 0:
            committed_pages.update(pending_tx_pages)
            pending_tx_pages.clear()
            # Drop any pages truncated by a shrinking commit
            for old_pg in [p for p in committed_pages if p > db_sz_commit]:
                del committed_pages[old_pg]
            committed_db_pages = db_sz_commit

        offset += frame_stride

    return committed_pages, committed_db_pages


def _chmod_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def decrypt_sqlcipher_database(
    src_path: Path,
    dst_path: Path,
    candidate_hex_keys: list[str],
) -> dict[str, Any]:
    """Decrypt a WeChat 3.x (SQLCipher 3) or 4.x (SQLCipher 4) database (+ `-wal`) to a SQLite 3 file."""
    if not src_path.is_file():
        raise EnvOrPermissionError(f"Database file not found: {src_path}", error_code="DB_FILE_NOT_FOUND")

    src_wal_path = Path(f"{src_path}-wal")
    has_src_wal = src_wal_path.is_file() and src_wal_path.stat().st_size >= WAL_HEADER_SIZE
    file_size = src_path.stat().st_size
    if file_size == 0 and not has_src_wal:
        raise InternalDatabaseError(f"Database file is empty: {src_path}", error_code="EMPTY_DB_FILE")

    dst_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)

    # Copy `.db` and `.db-wal` into a private offline staging sandbox first to minimize torn-read risk
    with tempfile.TemporaryDirectory(prefix=".wx_dec_stage_", dir=str(dst_path.parent)) as stage_dir_str:
        stage_dir = Path(stage_dir_str)
        try:
            os.chmod(stage_dir, 0o700)
        except OSError:
            pass

        staged_db = stage_dir / src_path.name
        shutil.copyfile(src_path, staged_db)
        staged_wal = stage_dir / f"{src_path.name}-wal"
        if src_wal_path.is_file():
            try:
                shutil.copyfile(src_wal_path, staged_wal)
            except OSError:
                pass

        staged_size = staged_db.stat().st_size

        if staged_size >= 16 and is_plaintext_sqlite(staged_db):
            tmp_plain = dst_path.with_suffix(f".{os.getpid()}.tmp.sqlite")
            try:
                src_uri = f"{staged_db.resolve().as_uri()}?mode=ro"
                s_conn = sqlite3.connect(src_uri, uri=True, timeout=3.0)
                d_conn = sqlite3.connect(str(tmp_plain))
                try:
                    s_conn.backup(d_conn)
                finally:
                    d_conn.close()
                    s_conn.close()
                _chmod_private_file(tmp_plain)
                tmp_plain.replace(dst_path)
                _chmod_private_file(dst_path)
            except Exception:
                tmp_plain.unlink(missing_ok=True)
                shutil.copy2(staged_db, dst_path)
                _chmod_private_file(dst_path)
            return {
                "source": str(src_path),
                "output": str(dst_path),
                "mode": "plaintext_copy",
                "cipher_version": 0,
                "pages": dst_path.stat().st_size // SQLCIPHER_PAGE_SIZE,
                "wal_frames_applied": 0,
            }

        if not candidate_hex_keys:
            raise EnvOrPermissionError(
                f"Database '{src_path.name}' is encrypted with SQLCipher, but no key was configured. "
                "Run `wechat-localmind key --set <64-hex-key>` or `wechat-localmind key --import <keys.json>`.",
                error_code="SQLCIPHER_KEY_MISSING",
            )

        pre_wal_pages, pre_wal_db_pages = _parse_committed_sqlcipher_wal(staged_wal)
        with open(staged_db, "rb") as f_in:
            initial_chunk = f_in.read(65536)

        if 1 in pre_wal_pages and len(initial_chunk) < len(pre_wal_pages[1]):
            probe_chunk = pre_wal_pages[1]
        elif 1 in pre_wal_pages:
            # Prefer committed WAL Page 1 if present (carries same 16B salt and latest schema header)
            probe_chunk = pre_wal_pages[1]
        else:
            probe_chunk = initial_chunk

        if len(probe_chunk) < 1024:
            raise InternalDatabaseError(
                f"Encrypted database '{src_path.name}' is smaller than minimum SQLCipher page size.",
                error_code="CORRUPT_DB_HEADER",
            )

        params = _detect_cipher_params(probe_chunk, candidate_hex_keys)
        if params is None and 1 in pre_wal_pages and len(initial_chunk) >= 1024:
            params = _detect_cipher_params(initial_chunk, candidate_hex_keys)

        if params is None:
            salt_hex = probe_chunk[:SQLCIPHER_SALT_SIZE].hex()
            raise EnvOrPermissionError(
                f"SQLCipher HMAC verification failed for '{src_path.name}' (salt={salt_hex}). "
                "Ensure the provided key matches this database.",
                error_code="SQLCIPHER_KEY_MISMATCH",
            )

        cipher_ver, enc_key, mac_key, _matched_hex, _is_raw, page_size = params
        wal_pages, wal_db_pages = _parse_committed_sqlcipher_wal(staged_wal, expected_page_size=page_size)

        reserve_sz = SQLCIPHER4_RESERVE_SIZE if cipher_ver == 4 else SQLCIPHER3_RESERVE_SIZE
        usable_sz = page_size - reserve_sz
        base_pages = staged_size // page_size
        total_pages = wal_db_pages if wal_db_pages is not None else max(base_pages, max(wal_pages.keys(), default=0))

        tmp_dst = dst_path.with_suffix(f".{os.getpid()}.tmp.sqlite")
        try:
            with open(staged_db, "rb") as f_in, open(tmp_dst, "wb") as f_out:
                for pgno in range(1, total_pages + 1):
                    if pgno <= base_pages:
                        disk_page = f_in.read(page_size)
                    else:
                        disk_page = b"\x00" * page_size

                    page_buf = wal_pages.get(pgno, disk_page)
                    if len(page_buf) < page_size:
                        break

                    # Preserve uninitialized all-zero pages
                    if page_buf == b"\x00" * page_size:
                        f_out.write(page_buf)
                        continue

                    if not _verify_page_hmac(page_buf, pgno, mac_key, cipher_ver, page_size):
                        # If WAL page failed HMAC unexpectedly, fall back to base page if valid; else fail closed
                        if pgno in wal_pages and len(disk_page) == page_size and _verify_page_hmac(
                            disk_page, pgno, mac_key, cipher_ver, page_size
                        ):
                            page_buf = disk_page
                        else:
                            raise InternalDatabaseError(
                                f"HMAC integrity check failed on page {pgno} of '{src_path.name}'.",
                                error_code="SQLCIPHER_PAGE_HMAC_FAILED",
                            )

                    iv = page_buf[usable_sz : usable_sz + SQLCIPHER_IV_SIZE]
                    if pgno == 1:
                        ciphertext = page_buf[SQLCIPHER_SALT_SIZE:usable_sz]
                        decrypted = _aes_cbc_decrypt(enc_key, iv, ciphertext)
                        out_page_arr = bytearray(SQLITE_FILE_HEADER + decrypted + (b"\x00" * reserve_sz))
                        hdr_pgsz = _extract_sqlite_header_page_size(out_page_arr)
                        if hdr_pgsz not in (0, page_size):
                            raise InternalDatabaseError(
                                f"Decrypted Page 1 SQLite page_size ({hdr_pgsz}) mismatches frame size ({page_size}).",
                                error_code="CORRUPT_DB_HEADER",
                            )
                        # Normalize standalone merged SQLite header from WAL (2,2) to rollback (1,1)
                        # and sync in-header database size (offset 28..32) with merged WAL page count.
                        if out_page_arr[18] == 2:
                            out_page_arr[18] = 1
                        if out_page_arr[19] == 2:
                            out_page_arr[19] = 1
                        if wal_pages and total_pages > 0:
                            struct.pack_into(">I", out_page_arr, 28, total_pages)
                        f_out.write(bytes(out_page_arr))
                    else:
                        ciphertext = page_buf[:usable_sz]
                        decrypted = _aes_cbc_decrypt(enc_key, iv, ciphertext)
                        f_out.write(decrypted + (b"\x00" * reserve_sz))

            _chmod_private_file(tmp_dst)
            tmp_dst.replace(dst_path)
            _chmod_private_file(dst_path)
        except (EnvOrPermissionError, InternalDatabaseError):
            tmp_dst.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp_dst.unlink(missing_ok=True)
            raise InternalDatabaseError(
                f"Failed while decrypting '{src_path.name}': {exc}",
                error_code="SQLCIPHER_DECRYPT_FAILED",
            ) from exc

    return {
        "source": str(src_path),
        "output": str(dst_path),
        "mode": f"sqlcipher_v{cipher_ver}",
        "cipher_version": cipher_ver,
        "pages": total_pages,
        "wal_frames_applied": len(wal_pages),
    }


def _encrypt_sqlcipher_page(
    plain_page: bytes,
    pgno: int,
    enc_key: bytes,
    mac_key: bytes,
    db_salt: bytes,
    cipher_version: int = 4,
    page_size: int = SQLCIPHER_PAGE_SIZE,
) -> bytes:
    """Encrypt a single 4096B SQLite page into SQLCipher 4 or 3 page format."""
    reserve_sz = SQLCIPHER4_RESERVE_SIZE if cipher_version == 4 else SQLCIPHER3_RESERVE_SIZE
    hmac_sz = SQLCIPHER4_HMAC_SIZE if cipher_version == 4 else SQLCIPHER3_HMAC_SIZE
    hmac_algo = hashlib.sha512 if cipher_version == 4 else hashlib.sha1
    usable_sz = page_size - reserve_sz
    iv = os.urandom(SQLCIPHER_IV_SIZE)
    pad_len = reserve_sz - SQLCIPHER_IV_SIZE - hmac_sz

    if pgno == 1:
        plain_payload = plain_page[SQLCIPHER_SALT_SIZE:usable_sz]
        ciphertext = _aes_cbc_encrypt(enc_key, iv, plain_payload)
        auth_body = ciphertext + iv
        mac = hmac.new(mac_key, auth_body + struct.pack("<I", pgno), hmac_algo).digest()
        return db_salt + auth_body + mac + (b"\x00" * pad_len)

    plain_payload = plain_page[:usable_sz]
    ciphertext = _aes_cbc_encrypt(enc_key, iv, plain_payload)
    auth_body = ciphertext + iv
    mac = hmac.new(mac_key, auth_body + struct.pack("<I", pgno), hmac_algo).digest()
    return auth_body + mac + (b"\x00" * pad_len)


def encrypt_sqlite_to_sqlcipher(
    plaintext_db_path: Path,
    encrypted_db_path: Path,
    raw_key_hex: str,
    cipher_version: int = 4,
    salt: bytes | None = None,
    is_raw_key: bool = True,
) -> dict[str, Any]:
    """Encrypt a standard plaintext SQLite database into SQLCipher 4 or 3 format (for testing & verification)."""
    reserve_sz = SQLCIPHER4_RESERVE_SIZE if cipher_version == 4 else SQLCIPHER3_RESERVE_SIZE
    usable_sz = SQLCIPHER_PAGE_SIZE - reserve_sz

    raw_bytes = plaintext_db_path.read_bytes()
    if len(raw_bytes) < SQLCIPHER_PAGE_SIZE or not raw_bytes.startswith(SQLITE_FILE_HEADER):
        raise ValueError("Source must be a valid 4096-byte-page SQLite 3 database.")

    # If the source SQLite database was created with reserve=0, clone it into a database with
    # reserve=reserve_sz (80 for SQLCipher 4, 48 for SQLCipher 3) so B-tree cells never overflow usable_sz.
    if raw_bytes[20] != reserve_sz:
        with tempfile.TemporaryDirectory(prefix="wx_sqlcipher_res_") as tmp_dir_str:
            tmp_reserved = Path(tmp_dir_str) / "reserved.sqlite"
            conn_init = sqlite3.connect(str(tmp_reserved))
            conn_init.execute(f"PRAGMA page_size = {SQLCIPHER_PAGE_SIZE}")
            conn_init.execute("VACUUM")
            conn_init.close()

            hdr_page = bytearray(tmp_reserved.read_bytes())
            hdr_page[20] = reserve_sz
            struct.pack_into(">H", hdr_page, 105, usable_sz)
            tmp_reserved.write_bytes(bytes(hdr_page))

            dst_conn = sqlite3.connect(str(tmp_reserved))
            dst_cur = dst_conn.cursor()
            dst_cur.execute("ATTACH DATABASE ? AS src_db", (str(plaintext_db_path),))
            dst_cur.execute(
                "SELECT name, sql FROM src_db.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            for tbl_name, create_sql in dst_cur.fetchall():
                if create_sql:
                    dst_cur.execute(create_sql)
                    dst_cur.execute(f'INSERT INTO "{tbl_name}" SELECT * FROM src_db."{tbl_name}"')
            dst_conn.commit()
            dst_conn.close()
            raw_bytes = tmp_reserved.read_bytes()

    key_bytes, _ = parse_hex_key(raw_key_hex)
    db_salt = salt if (salt and len(salt) == 16) else os.urandom(16)

    if cipher_version == 4:
        enc_key, mac_key = derive_sqlcipher4_keys(key_bytes, db_salt, is_raw_key=is_raw_key)
    else:
        enc_key, mac_key = derive_sqlcipher3_keys(key_bytes, db_salt, is_raw_key=is_raw_key)

    total_pages = len(raw_bytes) // SQLCIPHER_PAGE_SIZE
    out_chunks: list[bytes] = []

    for pgno in range(1, total_pages + 1):
        offset = (pgno - 1) * SQLCIPHER_PAGE_SIZE
        page = raw_bytes[offset : offset + SQLCIPHER_PAGE_SIZE]
        out_chunks.append(
            _encrypt_sqlcipher_page(page, pgno, enc_key, mac_key, db_salt, cipher_version, SQLCIPHER_PAGE_SIZE)
        )

    encrypted_db_path.parent.mkdir(parents=True, exist_ok=True)
    encrypted_db_path.write_bytes(b"".join(out_chunks))
    return {
        "encrypted_path": str(encrypted_db_path),
        "cipher_version": cipher_version,
        "salt_hex": db_salt.hex(),
        "pages": total_pages,
    }


def encrypt_sqlite_wal_to_sqlcipher(
    plaintext_wal_path: Path,
    encrypted_wal_path: Path,
    raw_key_hex: str,
    db_salt: bytes,
    cipher_version: int = 4,
    is_raw_key: bool = True,
) -> int:
    """Encrypt a plaintext SQLite `-wal` file (with reserved-byte pages) into a valid SQLCipher `-wal` file."""
    raw_wal = plaintext_wal_path.read_bytes()
    if len(raw_wal) < WAL_HEADER_SIZE:
        raise ValueError("Plaintext WAL file is smaller than 32-byte WAL header.")

    magic, fmt_ver, wal_pgsz, ckpt_seq, hdr_s1, hdr_s2, _c0, _c1 = struct.unpack(
        ">IIIIIIII", raw_wal[:WAL_HEADER_SIZE]
    )
    big_endian = magic == WAL_MAGIC_BE
    hdr_prefix = struct.pack(">IIIIII", magic, fmt_ver, wal_pgsz, ckpt_seq, hdr_s1, hdr_s2)
    s0, s1 = _sqlite_wal_checksum(hdr_prefix, 0, 0, big_endian)
    out_parts = [hdr_prefix + struct.pack(">II", s0, s1)]

    key_bytes, _ = parse_hex_key(raw_key_hex)
    if cipher_version == 4:
        enc_key, mac_key = derive_sqlcipher4_keys(key_bytes, db_salt, is_raw_key=is_raw_key)
    else:
        enc_key, mac_key = derive_sqlcipher3_keys(key_bytes, db_salt, is_raw_key=is_raw_key)

    stride = WAL_FRAME_HEADER_SIZE + wal_pgsz
    offset = WAL_HEADER_SIZE
    frames = 0
    while offset + stride <= len(raw_wal):
        fhdr = raw_wal[offset : offset + WAL_FRAME_HEADER_SIZE]
        plain_page = raw_wal[offset + WAL_FRAME_HEADER_SIZE : offset + stride]
        pgno, db_sz_commit, fs1, fs2, _fc0, _fc1 = struct.unpack(">IIIIII", fhdr)
        if (fs1, fs2) == (hdr_s1, hdr_s2) and pgno > 0:
            enc_page = _encrypt_sqlcipher_page(
                plain_page, pgno, enc_key, mac_key, db_salt, cipher_version, wal_pgsz
            )
            fhdr_prefix = struct.pack(">IIII", pgno, db_sz_commit, fs1, fs2)
            s0, s1 = _sqlite_wal_checksum(fhdr_prefix[:8], s0, s1, big_endian)
            s0, s1 = _sqlite_wal_checksum(enc_page, s0, s1, big_endian)
            out_parts.append(fhdr_prefix + struct.pack(">II", s0, s1) + enc_page)
            frames += 1
        offset += stride

    encrypted_wal_path.write_bytes(b"".join(out_parts))
    return frames


def extract_keys_from_memory_dump(
    dump_bytes: bytes,
    db_files: list[Path],
) -> dict[str, str]:
    """Scan a binary/text memory dump or LLDB log for 64-hex / 96-hex keys and match against local DB salts."""
    salt_to_dbs: dict[bytes, list[Path]] = {}
    for db_p in db_files:
        try:
            if not db_p.is_file() or db_p.stat().st_size < 1024:
                continue
            if is_plaintext_sqlite(db_p):
                continue
            with open(db_p, "rb") as f:
                p1 = f.read(SQLCIPHER_PAGE_SIZE)
            salt = p1[:SQLCIPHER_SALT_SIZE]
            salt_to_dbs.setdefault(salt, []).append(db_p)
        except Exception:
            continue

    found_keys: dict[str, str] = {}
    # 1. Scan for ASCII hex strings of format x'<64/96 hex>' or raw 64/96 hex tokens
    hex_pattern = re.compile(rb"(?:0x|x')?([0-9a-fA-F]{64,96})'?")
    candidates: set[str] = set()
    for m in hex_pattern.finditer(dump_bytes):
        token = m.group(1).decode("ascii", errors="ignore")
        if len(token) in (64, 96):
            candidates.add(token[:64].lower())

    # 2. Check if each DB's 16-byte binary salt appears in memory right after a 32-byte binary key
    for salt in salt_to_dbs:
        pos = 0
        while True:
            idx = dump_bytes.find(salt, pos)
            if idx == -1:
                break
            if idx >= 32:
                raw_k = dump_bytes[idx - 32 : idx]
                candidates.add(raw_k.hex())
            pos = idx + 1

    cand_list = list(candidates)
    if not cand_list:
        return found_keys

    for db_p in db_files:
        try:
            if not db_p.is_file() or db_p.stat().st_size < 1024 or is_plaintext_sqlite(db_p):
                continue
            with open(db_p, "rb") as f:
                p1 = f.read(65536)
            params = _detect_cipher_params(p1, cand_list)
            if params is not None:
                _ver, enc_key, _mac_key, matched_hex, is_raw, _pgsz = params
                # Preserve pre-PBKDF2 master key (`matched_hex`) when `is_raw=False` so newly rotated
                # `message_N.db` shards with fresh salts can also be decrypted without re-dumping memory.
                found_keys[db_p.name] = enc_key.hex() if is_raw else matched_hex
        except Exception:
            continue

    return found_keys
