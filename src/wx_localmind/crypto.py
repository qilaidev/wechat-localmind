"""SQLCipher 3 (WeChat 3.x) & SQLCipher 4 (WeChat 4.x WCDB) pure-Python decryptor and key resolver."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import struct
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


def _cc_aes_cbc(op: int, key: bytes, iv: bytes, data: bytes) -> bytes | None:
    """Zero-dependency macOS native CommonCrypto (`CCCrypt`) AES-256-CBC engine."""
    try:
        import ctypes

        lib = getattr(_cc_aes_cbc, "_lib", None)
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
            setattr(_cc_aes_cbc, "_lib", lib)
            setattr(_cc_aes_cbc, "_fn", fn)
        fn = getattr(_cc_aes_cbc, "_fn")
        n = len(data)
        out = ctypes.create_string_buffer(n)
        moved = ctypes.c_size_t(0)
        # op: 0=kCCEncrypt, 1=kCCDecrypt; 0=kCCAlgorithmAES; options=0 (CBC, no PKCS7 padding)
        st = fn(op, 0, 0, key, len(key), iv, data, n, out, n, ctypes.byref(moved))
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

    cc_res = _cc_aes_cbc(1, key, iv, ciphertext)
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

    cc_res = _cc_aes_cbc(0, key, iv, plaintext)
    if cc_res is not None:
        return cc_res

    raise RuntimeError("No AES-256-CBC backend available for encryption.")


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


def verify_page1_sqlcipher4(page1: bytes, key_material: bytes, is_raw_key: bool = True) -> tuple[bool, bytes]:
    """Verify Page 1 HMAC-SHA512 for SQLCipher 4 (WeChat 4.x). Returns (valid, enc_key)."""
    if len(page1) < SQLCIPHER_PAGE_SIZE:
        return False, b""
    salt = page1[:SQLCIPHER_SALT_SIZE]
    enc_key, mac_key = derive_sqlcipher4_keys(key_material, salt, is_raw_key=is_raw_key)
    # Page 1 authenticated data: bytes [16 : 4096 - 80 + 16] (ciphertext + IV) + little-endian page_no (1)
    auth_end = SQLCIPHER_PAGE_SIZE - SQLCIPHER4_RESERVE_SIZE + SQLCIPHER_IV_SIZE
    mac_data = page1[SQLCIPHER_SALT_SIZE:auth_end] + struct.pack("<I", 1)
    expected_hmac = hmac.new(mac_key, mac_data, hashlib.sha512).digest()
    stored_hmac = page1[auth_end : auth_end + SQLCIPHER4_HMAC_SIZE]
    return hmac.compare_digest(expected_hmac, stored_hmac), enc_key


def verify_page1_sqlcipher3(page1: bytes, key_material: bytes, is_raw_key: bool = True) -> tuple[bool, bytes]:
    """Verify Page 1 HMAC-SHA1 for SQLCipher 3 (WeChat 3.x). Returns (valid, enc_key)."""
    if len(page1) < SQLCIPHER_PAGE_SIZE:
        return False, b""
    salt = page1[:SQLCIPHER_SALT_SIZE]
    enc_key, mac_key = derive_sqlcipher3_keys(key_material, salt, is_raw_key=is_raw_key)
    auth_end = SQLCIPHER_PAGE_SIZE - SQLCIPHER3_RESERVE_SIZE + SQLCIPHER_IV_SIZE
    mac_data = page1[SQLCIPHER_SALT_SIZE:auth_end] + struct.pack("<I", 1)
    expected_hmac = hmac.new(mac_key, mac_data, hashlib.sha1).digest()
    stored_hmac = page1[auth_end : auth_end + SQLCIPHER3_HMAC_SIZE]
    return hmac.compare_digest(expected_hmac, stored_hmac), enc_key


def detect_cipher_version_and_key(page1: bytes, candidate_hex_keys: list[str]) -> tuple[int, bytes] | None:
    """Probe Page 1 against candidate hex keys across SQLCipher 4 and SQLCipher 3 (raw & passphrase KDF).

    Returns (cipher_version, derived_enc_key) or None if no key matches.
    """
    for raw_hex in candidate_hex_keys:
        try:
            key_bytes, key_salt = parse_hex_key(raw_hex)
        except UsageError:
            continue

        # If key includes 16-byte salt suffix, fast-check salt match first
        if key_salt is not None and page1[:SQLCIPHER_SALT_SIZE] != key_salt:
            continue

        for is_raw in (True, False):
            ok4, enc4 = verify_page1_sqlcipher4(page1, key_bytes, is_raw_key=is_raw)
            if ok4:
                return 4, enc4
            ok3, enc3 = verify_page1_sqlcipher3(page1, key_bytes, is_raw_key=is_raw)
            if ok3:
                return 3, enc3
    return None


def decrypt_sqlcipher_database(
    src_path: Path,
    dst_path: Path,
    candidate_hex_keys: list[str],
) -> dict[str, Any]:
    """Decrypt a WeChat 3.x (SQLCipher 3) or 4.x (SQLCipher 4) database to a standard SQLite 3 file."""
    if not src_path.is_file():
        raise EnvOrPermissionError(f"Database file not found: {src_path}", error_code="DB_FILE_NOT_FOUND")

    file_size = src_path.stat().st_size
    if file_size == 0:
        raise InternalDatabaseError(f"Database file is empty: {src_path}", error_code="EMPTY_DB_FILE")

    if is_plaintext_sqlite(src_path):
        import shutil

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        return {
            "source": str(src_path),
            "output": str(dst_path),
            "mode": "plaintext_copy",
            "cipher_version": 0,
            "pages": file_size // SQLCIPHER_PAGE_SIZE,
        }

    if not candidate_hex_keys:
        raise EnvOrPermissionError(
            f"Database '{src_path.name}' is encrypted with SQLCipher, but no key was configured. "
            "Run `wechat-localmind key --set <64-hex-key>` or `wechat-localmind key --import <keys.json>`.",
            error_code="SQLCIPHER_KEY_MISSING",
        )

    with open(src_path, "rb") as f_in:
        page1 = f_in.read(SQLCIPHER_PAGE_SIZE)
        if len(page1) < SQLCIPHER_PAGE_SIZE:
            raise InternalDatabaseError(
                f"Encrypted database '{src_path.name}' is smaller than 4096 bytes.",
                error_code="CORRUPT_DB_HEADER",
            )

        matched = detect_cipher_version_and_key(page1, candidate_hex_keys)
        if matched is None:
            salt_hex = page1[:SQLCIPHER_SALT_SIZE].hex()
            raise EnvOrPermissionError(
                f"SQLCipher HMAC verification failed for '{src_path.name}' (salt={salt_hex}). "
                "Ensure the provided key matches this database.",
                error_code="SQLCIPHER_KEY_MISMATCH",
            )

        cipher_ver, enc_key = matched
        reserve_sz = SQLCIPHER4_RESERVE_SIZE if cipher_ver == 4 else SQLCIPHER3_RESERVE_SIZE
        usable_sz = SQLCIPHER_PAGE_SIZE - reserve_sz
        total_pages = file_size // SQLCIPHER_PAGE_SIZE

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dst = dst_path.with_suffix(f".{os.getpid()}.tmp.sqlite")

        try:
            with open(tmp_dst, "wb") as f_out:
                for pgno in range(1, total_pages + 1):
                    page_buf = page1 if pgno == 1 else f_in.read(SQLCIPHER_PAGE_SIZE)
                    if len(page_buf) < SQLCIPHER_PAGE_SIZE:
                        break

                    # If an entire page is all zeros, preserve it as zeros
                    if page_buf == b"\x00" * SQLCIPHER_PAGE_SIZE:
                        f_out.write(page_buf)
                        continue

                    iv = page_buf[usable_sz : usable_sz + SQLCIPHER_IV_SIZE]
                    if pgno == 1:
                        ciphertext = page_buf[SQLCIPHER_SALT_SIZE:usable_sz]
                        decrypted = _aes_cbc_decrypt(enc_key, iv, ciphertext)
                        out_page = bytearray(SQLITE_FILE_HEADER + decrypted + b"\x00" * reserve_sz)
                        f_out.write(bytes(out_page))
                    else:
                        ciphertext = page_buf[:usable_sz]
                        decrypted = _aes_cbc_decrypt(enc_key, iv, ciphertext)
                        f_out.write(decrypted + b"\x00" * reserve_sz)

            tmp_dst.replace(dst_path)
        except Exception as exc:
            if tmp_dst.exists():
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
    }


def encrypt_sqlite_to_sqlcipher(
    plaintext_db_path: Path,
    encrypted_db_path: Path,
    raw_key_hex: str,
    cipher_version: int = 4,
    salt: bytes | None = None,
) -> dict[str, Any]:
    """Encrypt a standard plaintext SQLite database into SQLCipher 4 or 3 format (for testing & verification)."""
    import sqlite3
    import tempfile

    reserve_sz = SQLCIPHER4_RESERVE_SIZE if cipher_version == 4 else SQLCIPHER3_RESERVE_SIZE
    usable_sz = SQLCIPHER_PAGE_SIZE - reserve_sz

    raw_bytes = plaintext_db_path.read_bytes()
    if len(raw_bytes) < SQLCIPHER_PAGE_SIZE or not raw_bytes.startswith(SQLITE_FILE_HEADER):
        raise ValueError("Source must be a valid 4096-byte-page SQLite 3 database.")

    # If the source SQLite database was created with reserve=0, clone it into a database with
    # reserve=reserve_sz (80 for SQLCipher 4, 48 for SQLCipher 3) so B-tree cells never overflow usable_sz.
    if raw_bytes[20] != reserve_sz:
        tmp_reserved = Path(tempfile.mktemp(suffix=".sqlite"))
        try:
            conn_init = sqlite3.connect(str(tmp_reserved))
            conn_init.execute(f"PRAGMA page_size = {SQLCIPHER_PAGE_SIZE}")
            conn_init.execute("VACUUM")
            conn_init.close()

            hdr_page = bytearray(tmp_reserved.read_bytes())
            hdr_page[20] = reserve_sz
            struct.pack_into(">H", hdr_page, 105, usable_sz)
            tmp_reserved.write_bytes(bytes(hdr_page))

            # Copy all tables & data from plaintext_db_path into tmp_reserved
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
        finally:
            if tmp_reserved.exists():
                tmp_reserved.unlink(missing_ok=True)

    key_bytes, _ = parse_hex_key(raw_key_hex)
    db_salt = salt if (salt and len(salt) == 16) else os.urandom(16)

    if cipher_version == 4:
        enc_key, mac_key = derive_sqlcipher4_keys(key_bytes, db_salt, is_raw_key=True)
        hmac_algo = hashlib.sha512
        hmac_sz = SQLCIPHER4_HMAC_SIZE
    else:
        enc_key, mac_key = derive_sqlcipher3_keys(key_bytes, db_salt, is_raw_key=True)
        hmac_algo = hashlib.sha1
        hmac_sz = SQLCIPHER3_HMAC_SIZE

    total_pages = len(raw_bytes) // SQLCIPHER_PAGE_SIZE
    out_chunks: list[bytes] = []

    for pgno in range(1, total_pages + 1):
        offset = (pgno - 1) * SQLCIPHER_PAGE_SIZE
        page = bytearray(raw_bytes[offset : offset + SQLCIPHER_PAGE_SIZE])
        iv = os.urandom(SQLCIPHER_IV_SIZE)

        if pgno == 1:
            plain_payload = bytes(page[SQLCIPHER_SALT_SIZE:usable_sz])
            ciphertext = _aes_cbc_encrypt(enc_key, iv, plain_payload)
            auth_body = ciphertext + iv
            mac = hmac.new(mac_key, auth_body + struct.pack("<I", pgno), hmac_algo).digest()
            pad_len = reserve_sz - SQLCIPHER_IV_SIZE - hmac_sz
            out_page = db_salt + auth_body + mac + (b"\x00" * pad_len)
        else:
            plain_payload = bytes(page[:usable_sz])
            ciphertext = _aes_cbc_encrypt(enc_key, iv, plain_payload)
            auth_body = ciphertext + iv
            mac = hmac.new(mac_key, auth_body + struct.pack("<I", pgno), hmac_algo).digest()
            pad_len = reserve_sz - SQLCIPHER_IV_SIZE - hmac_sz
            out_page = auth_body + mac + (b"\x00" * pad_len)

        out_chunks.append(out_page)

    encrypted_db_path.parent.mkdir(parents=True, exist_ok=True)
    encrypted_db_path.write_bytes(b"".join(out_chunks))
    return {
        "encrypted_path": str(encrypted_db_path),
        "cipher_version": cipher_version,
        "salt_hex": db_salt.hex(),
        "pages": total_pages,
    }


def extract_keys_from_memory_dump(
    dump_bytes: bytes,
    db_files: list[Path],
) -> dict[str, str]:
    """Scan a binary/text memory dump or LLDB log for 64-hex / 96-hex keys and match against local DB salts."""
    # Map local DB file relative/basename and salt -> Path
    salt_to_dbs: dict[bytes, list[Path]] = {}
    for db_p in db_files:
        try:
            if not db_p.is_file() or db_p.stat().st_size < SQLCIPHER_PAGE_SIZE:
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

    # 2. Also check if each DB's 16-byte binary salt appears in memory right after a 32-byte binary key
    for salt, paths in salt_to_dbs.items():
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
            if not db_p.is_file() or db_p.stat().st_size < SQLCIPHER_PAGE_SIZE or is_plaintext_sqlite(db_p):
                continue
            with open(db_p, "rb") as f:
                p1 = f.read(SQLCIPHER_PAGE_SIZE)
            matched = detect_cipher_version_and_key(p1, cand_list)
            if matched is not None:
                _, enc_key = matched
                found_keys[db_p.name] = enc_key.hex()
        except Exception:
            continue

    return found_keys
