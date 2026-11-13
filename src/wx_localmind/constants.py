"""Constants, exit codes, default paths, and WeChat 3.x / 4.x message type mappings."""

import os
from pathlib import Path

# POSIX Exit Codes
EXIT_SUCCESS = 0
EXIT_USAGE_ERROR = 1
EXIT_ENV_OR_PERM_ERROR = 2
EXIT_POLICY_DENIED = 3
EXIT_RESOURCE_NOT_FOUND = 4
EXIT_INTERNAL_DB_ERROR = 5

# Default macOS & Windows WeChat Data Paths
DEFAULT_MAC_WECHAT_CONTAINER_DIR = Path(
    os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data")
)
DEFAULT_MAC_WECHAT4_FILES_DIR = DEFAULT_MAC_WECHAT_CONTAINER_DIR / "Documents" / "xwechat_files"
DEFAULT_MAC_WECHAT3_SUPPORT_DIR = (
    DEFAULT_MAC_WECHAT_CONTAINER_DIR / "Library" / "Application Support" / "com.tencent.xinWeChat"
)

CANDIDATE_WECHAT_ROOTS = (
    # macOS WeChat 4.x (xwechat_files)
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
    "~/Documents/xwechat_files",
    # macOS WeChat 3.x (Application Support)
    "~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat",
    "~/Library/Application Support/com.tencent.xinWeChat",
    # Windows WeChat 4.x / 3.x (when mounted or running on Windows)
    "~/Documents/WeChat Files",
    "~/WeChat Files",
    "~/xwechat_files",
)

DEFAULT_APP_SUPPORT_DIR = Path(
    os.path.expanduser("~/Library/Application Support/WeChat-LocalMind")
)
DEFAULT_CONFIG_PATH = DEFAULT_APP_SUPPORT_DIR / "config.json"
DEFAULT_KEYS_PATH = DEFAULT_APP_SUPPORT_DIR / "keys.json"
DEFAULT_SNAPSHOT_DIR = DEFAULT_APP_SUPPORT_DIR / "snapshot"
DEFAULT_SNAPSHOT_DB_PATH = DEFAULT_SNAPSHOT_DIR / "WeChat_Unified_snapshot.sqlite"
DEFAULT_SNAPSHOT_META_PATH = DEFAULT_SNAPSHOT_DIR / "snapshot_meta.json"
DEFAULT_SNAPSHOT_LOCK_PATH = DEFAULT_SNAPSHOT_DIR / "snapshot.lock"
DEFAULT_EXPORT_MEDIA_DIR = DEFAULT_SNAPSHOT_DIR / "media"

# SQLCipher Page & Reserve Constants
SQLITE_FILE_HEADER = b"SQLite format 3\x00"
SQLCIPHER_PAGE_SIZE = 4096
SQLCIPHER_SALT_SIZE = 16
SQLCIPHER_IV_SIZE = 16
# WeChat 4.x (WCDB / SQLCipher 4): HMAC-SHA512 (64B) + IV (16B) = 80B reserve
SQLCIPHER4_HMAC_SIZE = 64
SQLCIPHER4_RESERVE_SIZE = 80
SQLCIPHER4_KDF_ITER = 256000
# WeChat 3.x (SQLCipher 3): HMAC-SHA1 (20B aligned to 32B) + IV (16B) = 48B reserve
SQLCIPHER3_HMAC_SIZE = 20
SQLCIPHER3_RESERVE_SIZE = 48
SQLCIPHER3_KDF_ITER = 64000

# WeChat Message Type (local_type / Type) Mapping
MSG_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "audio",
    42: "contact",
    43: "video",
    47: "sticker",
    48: "location",
    49: "app_msg",       # Rich XML: quote reply (57), file (6), link (5), chat history (19), mini-program (33/36)
    50: "voip",
    62: "micro_video",
    10000: "system_event",
    10002: "system_xml", # Revokemsg / Pat (拍一拍) / Room invitation
}

# AppMsg (<appmsg><type>N</type>) Subtype Mapping (when base type == 49)
APPMSG_SUBTYPE_MAP = {
    1: "text",
    2: "image",
    3: "audio",
    4: "video",
    5: "link",
    6: "document",
    8: "sticker",
    19: "merged_history",
    24: "note",
    33: "mini_program",
    36: "mini_program",
    51: "channels_video",
    57: "quote_reply",
    63: "live_stream",
    87: "group_announcement",
    2000: "transfer",
    2001: "red_packet",
}

# Excluded message types in SQL query layer (system events, VoIP calls)
EXCLUDED_MSG_TYPES = (50, 10000)

# Media-bearing normalized message types
MEDIA_MSG_TYPES = frozenset({
    "image",
    "video",
    "micro_video",
    "audio",
    "document",
    "sticker",
})

# Image .dat XOR magic header candidates: (magic_bytes, extension, mime_type)
DAT_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG", ".png", "image/png"),
    (b"GIF8", ".gif", "image/gif"),
    (b"RIFF", ".webp", "image/webp"),
    (b"BM", ".bmp", "image/bmp"),
    (b"\x00\x00\x00\x18ftyp", ".heic", "image/heic"),
    (b"\x00\x00\x00\x20ftyp", ".heic", "image/heic"),
)

# Default cleaning parameters
DEFAULT_SNAPSHOT_TTL_SEC = 15
DEFAULT_CONTEXT_BUFFER_COUNT = 10
DEFAULT_LONG_AUDIO_THRESHOLD_SEC = 10
DEFAULT_REPLY_SNIPPET_MAX_LEN = 150
DEFAULT_FETCH_LIMIT = 500
MAX_FETCH_LIMIT = 3000
SYNC_LAG_WARNING_SEC = 4 * 3600  # 4 hours

# Preset short acknowledgment phrases for reversible noise folding
ACK_NOISE_PHRASES = frozenset({
    "收到",
    "收到！",
    "收到。",
    "好的",
    "好的！",
    "好的。",
    "好",
    "好滴",
    "好嘞",
    "明白",
    "明白了",
    "已阅",
    "了解",
    "ok",
    "ok!",
    "okay",
    "kk",
    "k",
    "+1",
    "1",
    "谢谢",
    "谢谢！",
    "多谢",
    "感谢",
    "thanks",
    "thx",
    "ty",
    "roger",
    "copy",
    "noted",
})
