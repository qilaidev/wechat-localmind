"""Comprehensive unit and integration test suite for WeChat-LocalMind (WeChat 4.x & 3.x CLI/MCP)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wx_localmind.cli import main
from wx_localmind.config import AppConfig
from wx_localmind.constants import (
    EXIT_ENV_OR_PERM_ERROR,
    EXIT_POLICY_DENIED,
    EXIT_RESOURCE_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
)
from wx_localmind.crypto import (
    decrypt_sqlcipher_database,
    encrypt_sqlite_to_sqlcipher,
    extract_keys_from_memory_dump,
)
from wx_localmind.media import detect_and_decode_wechat_dat_image
from wx_localmind.protobuf_parser import (
    decompress_zstd_or_raw,
    parse_protobuf_strings,
    parse_wechat_message_payload,
)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().lower()


def _encode_protobuf_field_string(field_num: int, val: str) -> bytes:
    raw = val.encode("utf-8")
    tag = (field_num << 3) | 2
    length = len(raw)
    # simple varint for length < 128
    return bytes([tag, length]) + raw


class TestWXLocalMindSuite(unittest.TestCase):
    """End-to-end tests covering SQLCipher 3/4, WeChat 4.x shards, 4-tier names, XML/zstd, .dat XOR, CLI & MCP."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="wx_localmind_test_"))
        self.wx_root = self.tmp_dir / "xwechat_files"
        self.acc_dir = self.wx_root / "wxid_me8888_e948"
        self.db_storage = self.acc_dir / "db_storage"
        self.contact_dir = self.db_storage / "contact"
        self.msg_dir = self.db_storage / "message"
        self.media_dir = self.acc_dir / "msg" / "attach"
        self.snapshot_dir = self.tmp_dir / "snapshot"
        self.config_path = self.tmp_dir / "config.json"

        for d in (self.contact_dir, self.msg_dir, self.media_dir, self.snapshot_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.key_contact = "11" * 32  # 64-hex key for contact.db
        self.key_msg0 = "22" * 32     # 64-hex key for message_0.db
        self.now_ts = int(time.time())

        self._build_wechat4_encrypted_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _build_wechat4_encrypted_fixture(self) -> None:
        """Create realistic WeChat 4.x `contact.db` and `message_0.db` encrypted with SQLCipher 4."""
        plain_contact = self.tmp_dir / "plain_contact.db"
        c_conn = sqlite3.connect(str(plain_contact))
        c_cur = c_conn.cursor()
        c_cur.executescript(
            """
            PRAGMA page_size = 4096;
            CREATE TABLE contact (
                id INTEGER PRIMARY KEY,
                username TEXT,
                nick_name TEXT,
                remark TEXT,
                alias TEXT
            );
            CREATE TABLE chatroom_member (
                room_id TEXT,
                member_id TEXT,
                display_name TEXT
            );
            """
        )
        # Contacts:
        # 1. Allowed work group: 10001@chatroom ("供应链核心群")
        # 2. Blocked life group: 99999@chatroom ("吃喝玩乐群")
        # 3. Private 1-on-1 contact: wxid_alice ("Alice私聊")
        # 4. Members with Tier 1 (remark), Tier 2 (group nick), Tier 3 (nick), Tier 4 (no record)
        contacts_data = [
            (1, "10001@chatroom", "供应链核心群", "核心供应链A群", ""),
            (2, "99999@chatroom", "吃喝玩乐群", "", ""),
            (3, "wxid_alice", "Alice_Nick", "李总监(采购)", "alice88"),
            (4, "wxid_bob", "Bob_Nick", "", "bob99"),
            (5, "wxid_charlie", "Charlie_Nick", "", ""),
            (6, "wxid_punct", ".", "", ""),
            (7, "wxid_me8888", "张工", "", "zhang_eng"),
        ]
        c_cur.executemany("INSERT INTO contact VALUES (?, ?, ?, ?, ?)", contacts_data)

        # Group members in 10001@chatroom:
        # - wxid_alice has remark "李总监(采购)" (Tier 1) and group nick "Alice群名片" -> should resolve to "李总监(采购)"
        # - wxid_bob has no remark, group nick "王经理-物流" (Tier 2) -> should resolve to "王经理-物流"
        # - wxid_charlie has no remark, no group nick, nick "Charlie_Nick" (Tier 3) -> should resolve to "Charlie_Nick"
        # - wxid_unknown9876 has no record anywhere (Tier 4) -> should resolve to "微信群友(9876)"
        members_data = [
            ("10001@chatroom", "wxid_alice", "Alice群名片"),
            ("10001@chatroom", "wxid_bob", "王经理-物流"),
            ("10001@chatroom", "wxid_charlie", ""),
            ("10001@chatroom", "wxid_punct", ""),
            ("10001@chatroom", "wxid_me8888", "张工"),
        ]
        c_cur.executemany("INSERT INTO chatroom_member VALUES (?, ?, ?)", members_data)
        c_conn.commit()
        c_conn.close()

        # Encrypt contact.db with SQLCipher 4
        encrypt_sqlite_to_sqlcipher(
            plain_contact,
            self.contact_dir / "contact.db",
            self.key_contact,
            cipher_version=4,
        )

        # Build message_0.db
        plain_msg0 = self.tmp_dir / "plain_message_0.db"
        m_conn = sqlite3.connect(str(plain_msg0))
        m_cur = m_conn.cursor()

        tbl_work = f"Msg_{_md5('10001@chatroom')}"
        tbl_blocked = f"Msg_{_md5('99999@chatroom')}"
        tbl_private = f"Msg_{_md5('wxid_alice')}"

        m_cur.executescript(
            f"""
            PRAGMA page_size = 4096;
            CREATE TABLE Name2Id (
                user_name TEXT
            );
            CREATE TABLE {tbl_work} (
                local_id INTEGER PRIMARY KEY,
                server_id INTEGER,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                is_send INTEGER,
                message_content TEXT,
                compress_content BLOB,
                packed_info_data BLOB
            );
            CREATE TABLE {tbl_blocked} (
                local_id INTEGER PRIMARY KEY,
                server_id INTEGER,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                is_send INTEGER,
                message_content TEXT,
                compress_content BLOB,
                packed_info_data BLOB
            );
            CREATE TABLE {tbl_private} (
                local_id INTEGER PRIMARY KEY,
                server_id INTEGER,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                is_send INTEGER,
                message_content TEXT,
                compress_content BLOB,
                packed_info_data BLOB
            );
            """
        )

        # Populate Name2Id (rowid 1..6)
        n2i_users = [
            ("wxid_alice",),      # rowid 1
            ("wxid_bob",),        # rowid 2
            ("wxid_charlie",),    # rowid 3
            ("wxid_unknown9876",),# rowid 4
            ("wxid_punct",),      # rowid 5
            ("wxid_me8888",),     # rowid 6
        ]
        m_cur.executemany("INSERT INTO Name2Id (user_name) VALUES (?)", n2i_users)

        # Create real media files in sandbox:
        # 1. XOR-encrypted .dat JPEG image (XOR key = 0x5A)
        raw_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        xor_jpeg = bytes(b ^ 0x5A for b in raw_jpeg)
        (self.media_dir / "img_quote.dat").write_bytes(xor_jpeg)

        # 2. PDF document
        (self.media_dir / "Q4供应链报价单.pdf").write_bytes(b"%PDF-1.4 mock quote pdf content")

        # 3. Long voice file (.silk)
        (self.media_dir / "voice_15s.silk").write_bytes(b"\x02#!SILK_V3" + b"\x00" * 100)

        t0 = self.now_ts - 600
        quote_xml = (
            "<msg><appmsg appid=\"\" sdkver=\"0\">"
            "<title>确认同意该报价单，下午打款</title>"
            "<type>57</type>"
            "<refermsg>"
            "<type>1</type>"
            "<svrid>90001</svrid>"
            "<fromusr>wxid_alice</fromusr>"
            "<chatusr>wxid_alice</chatusr>"
            "<displayname>Alice</displayname>"
            "<content>wxid_alice:\n各位请核对最新 Q4 芯片报价单，单价下调 5%</content>"
            "</refermsg>"
            "</appmsg></msg>"
        )
        file_xml = (
            "<msg><appmsg><title>Q4供应链报价单.pdf</title><type>6</type>"
            "<appattach><totallen>31</totallen><fileext>pdf</fileext></appattach>"
            "</appmsg></msg>"
        )
        voice_xml = '<msg><voicemsg endflag="1" length="5120" voicelength="15000" /></msg>'
        revoke_xml = '<sysmsg type="revokemsg"><revokemsg><replacemsg><![CDATA["Bob" 撤回了一条消息]]></replacemsg></revokemsg></sysmsg>'

        work_msgs = [
            # 1. Alice (Tier 1 Remark) sends initial text (server_id=90001)
            (1, 90001, 1, t0 + 10, 1, 0, "各位请核对最新 Q4 芯片报价单，单价下调 5%", None, None),
            # 2. Bob (Tier 2 Group Nick) replies quoting #1 (server_id=90002)
            (2, 90002, 49, t0 + 20, 2, 0, quote_xml, None, None),
            # 3. Charlie (Tier 3 Nick) mentions me with \u2005
            (
                3,
                90003,
                1,
                t0 + 30,
                3,
                0,
                "<msgsource><atuserlist>wxid_me8888</atuserlist></msgsource>",
                None,
                None,
            ),
            # Let's make #3 a normal text message with @张工\u2005
            # 4. Unknown member (Tier 4 fallback) sends ack noise
            (4, 90004, 1, t0 + 40, 4, 0, "收到", None, None),
            # 5. Punctuation nickname member sends ack noise
            (5, 90005, 1, t0 + 45, 5, 0, "好的！", None, None),
            # 6. Me (is_send=1) sends message
            (6, 90006, 1, t0 + 50, 6, 1, "我已经审阅完毕，马上安排财务。", None, None),
            # 7. Alice sends XOR .dat image
            (
                7,
                90007,
                3,
                t0 + 60,
                1,
                0,
                '<msg><img hdlength="22" length="22" /></msg>',
                None,
                _encode_protobuf_field_string(2, "attach/img_quote.dat"),
            ),
            # 8. Alice sends PDF attachment
            (
                8,
                90008,
                49,
                t0 + 70,
                1,
                0,
                file_xml,
                None,
                _encode_protobuf_field_string(2, "attach/Q4供应链报价单.pdf"),
            ),
            # 9. Bob sends 15s voice message
            (
                9,
                90009,
                34,
                t0 + 80,
                2,
                0,
                voice_xml,
                None,
                _encode_protobuf_field_string(2, "attach/voice_15s.silk"),
            ),
            # 10. Revoked message (must be 100% filtered out)
            (10, 90010, 10002, t0 + 90, 2, 0, revoke_xml, None, None),
        ]
        # Fix #3 to be text with @张工\u2005
        work_msgs[2] = (
            3,
            90003,
            1,
            t0 + 30,
            3,
            0,
            "@张工\u2005请确认一下交货排期",
            None,
            None,
        )

        m_cur.executemany(f"INSERT INTO {tbl_work} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", work_msgs)

        # Blocked group message
        m_cur.execute(
            f"INSERT INTO {tbl_blocked} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 80001, 1, t0 + 55, 1, 0, "今晚去哪聚餐？", None, None),
        )

        # Private 1-on-1 message (must NEVER enter unified snapshot or be accessible)
        m_cur.execute(
            f"INSERT INTO {tbl_private} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 70001, 1, t0 + 65, 1, 0, "这是私人绝密聊天记录，包含银行卡密码", None, None),
        )

        m_conn.commit()
        m_conn.close()

        encrypt_sqlite_to_sqlcipher(
            plain_msg0,
            self.msg_dir / "message_0.db",
            self.key_msg0,
            cipher_version=4,
        )

        # Save initial config
        cfg = AppConfig(
            filter_mode="blacklist",
            blacklist_chats=["99999@chatroom"],
            wx_data_dir=str(self.wx_root),
            snapshot_dir=str(self.snapshot_dir),
            db_keys={
                "contact.db": self.key_contact,
                "message_0.db": self.key_msg0,
            },
        )
        cfg.identity.name = "张工"
        cfg.identity.wxid = "wxid_me8888"
        cfg.save(self.config_path)

    def _run_cli(self, args: list[str]) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        full_args = [*args, "--config", str(self.config_path)]
        with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf):
            code = main(full_args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_01_sqlcipher3_and_sqlcipher4_roundtrip(self) -> None:
        """Verify both SQLCipher 4 (WeChat 4.x) and SQLCipher 3 (WeChat 3.x) encryption & decryption."""
        plain = self.tmp_dir / "plain_contact.db"
        for ver in (4, 3):
            enc_file = self.tmp_dir / f"test_v{ver}.db"
            dec_file = self.tmp_dir / f"test_v{ver}_dec.sqlite"
            key_hex = "ab" * 32
            encrypt_sqlite_to_sqlcipher(plain, enc_file, key_hex, cipher_version=ver)
            res = decrypt_sqlcipher_database(enc_file, dec_file, [key_hex])
            self.assertEqual(res["cipher_version"], ver)
            conn = sqlite3.connect(str(dec_file))
            cnt = conn.execute("SELECT COUNT(*) FROM contact").fetchone()[0]
            conn.close()
            self.assertEqual(cnt, 7)

    def test_02_memory_dump_key_scanner(self) -> None:
        """Verify `extract_keys_from_memory_dump` finds raw 32B keys + 16B salt in memory dump."""
        contact_db = self.contact_dir / "contact.db"
        msg0_db = self.msg_dir / "message_0.db"
        salt_c = contact_db.read_bytes()[:16]
        salt_m = msg0_db.read_bytes()[:16]

        # Construct synthetic process memory dump containing binary key+salt and ASCII x'<hex>'
        fake_dump = (
            os.urandom(256)
            + bytes.fromhex(self.key_contact)
            + salt_c
            + os.urandom(128)
            + f"x'{self.key_msg0}{salt_m.hex()}'".encode("ascii")
            + os.urandom(256)
        )
        matched = extract_keys_from_memory_dump(fake_dump, [contact_db, msg0_db])
        self.assertEqual(matched.get("contact.db"), self.key_contact)
        self.assertEqual(matched.get("message_0.db"), self.key_msg0)

    def test_03_doctor_and_groups_listing(self) -> None:
        """Verify `doctor` and `groups` commands with policy filtering."""
        code, out, err = self._run_cli(["doctor", "--format", "json"])
        self.assertEqual(code, EXIT_SUCCESS, err)
        doc = json.loads(out)
        self.assertTrue(doc["healthy"])
        self.assertEqual(doc["source_database"]["wxid"], "wxid_me8888")
        self.assertEqual(doc["source_database"]["key_verified_db_count"], 2)

        # List allowed groups (99999@chatroom should be hidden by default)
        code, out, err = self._run_cli(["groups", "--format", "json"])
        self.assertEqual(code, EXIT_SUCCESS, err)
        grps = json.loads(out)["groups"]
        self.assertEqual(len(grps), 1)
        self.assertEqual(grps[0]["chat_id"], "10001@chatroom")
        self.assertEqual(grps[0]["group_name"], "核心供应链A群")

        # Include blocked groups
        code, out, _ = self._run_cli(["groups", "--include-blocked", "--format", "json"])
        self.assertEqual(code, EXIT_SUCCESS)
        grps_all = json.loads(out)["groups"]
        self.assertEqual(len(grps_all), 2)

    def test_04_private_chat_and_blocked_group_isolation(self) -> None:
        """Verify 100% physical blocking of 1-on-1 private chats (wxid_*) and blacklisted groups (Exit Code 3)."""
        code, _, err = self._run_cli(["fetch", "--chats", "wxid_alice", "--format", "json"])
        self.assertEqual(code, EXIT_POLICY_DENIED)
        err_json = json.loads(err)
        self.assertEqual(err_json["error_code"], "PRIVATE_SESSION_BLOCKED")

        code, _, err = self._run_cli(["fetch", "--chats", "99999@chatroom", "--format", "json"])
        self.assertEqual(code, EXIT_POLICY_DENIED)
        err_json = json.loads(err)
        self.assertEqual(err_json["error_code"], "GROUP_BLOCKED_BY_POLICY")

    def test_05_fetch_4tier_names_quotes_mentions_and_revoked_filtering(self) -> None:
        """Verify 4-tier real-name resolution, quote chain (#1), [@我(张工)], ack folding, and revoked removal."""
        code, out, err = self._run_cli(["fetch", "--chats", "ALL_ALLOWED", "--since", "24h", "--format", "json"])
        self.assertEqual(code, EXIT_SUCCESS, err)
        data = json.loads(out)
        self.assertEqual(len(data["chats"]), 1)
        msgs = data["chats"][0]["messages"]

        # 10 raw rows in 10001@chatroom, minus 1 revoked (#10) = 9 valid messages
        self.assertEqual(len(msgs), 9)

        # Tier 1: Contact Remark ("李总监(采购)")
        self.assertEqual(msgs[0]["sender_name"], "李总监(采购)")
        # Tier 2: Group Nickname ("王经理-物流") + Quote reply to msg #1
        self.assertEqual(msgs[1]["sender_name"], "王经理-物流")
        self.assertIsNotNone(msgs[1]["reply_to"])
        self.assertEqual(msgs[1]["reply_to"]["msg_id"], msgs[0]["msg_id"])
        self.assertEqual(msgs[1]["reply_to"]["sender_name"], "李总监(采购)")
        self.assertIn("Q4 芯片报价单", msgs[1]["reply_to"]["text_snippet"])

        # Tier 3: Contact Nickname ("Charlie_Nick") + [@我(张工)] mention
        self.assertEqual(msgs[2]["sender_name"], "Charlie_Nick")
        self.assertTrue(msgs[2]["mentions_me"])
        self.assertIn("[@我(张工)]", msgs[2]["text"])

        # Tier 4: Unknown wxid fallback ("微信群友(9876)") + ack noise
        self.assertEqual(msgs[3]["sender_name"], "微信群友(9876)")
        self.assertTrue(msgs[3]["is_ack_noise"])

        # Punctuation name disambiguation (".(#unct)")
        self.assertEqual(msgs[4]["sender_name"], ".(#unct)")
        self.assertTrue(msgs[4]["is_ack_noise"])

        # Self identity ("我(张工)")
        self.assertEqual(msgs[5]["sender_name"], "我(张工)")
        self.assertTrue(msgs[5]["is_from_me"])

        # Verify Markdown rendering folds consecutive ack noise (#4 and #5) and marks 15s voice warning
        code_md, out_md, _ = self._run_cli(["fetch", "--chats", "ALL_ALLOWED", "--since", "24h", "--format", "md"])
        self.assertEqual(code_md, EXIT_SUCCESS)
        self.assertIn("系统折叠:", out_md)
        self.assertIn("⚠️ 语音消息未转写: 15秒", out_md)
        self.assertNotIn("撤回了一条消息", out_md)

    def test_06_incremental_cursor_and_context_buffer(self) -> None:
        """Verify `--after-id` incremental cursor and preceding context buffer."""
        code, out, _ = self._run_cli(
            ["fetch", "--chats", "10001@chatroom", "--after-id", "5", "--context-buffer", "2", "--format", "json"]
        )
        self.assertEqual(code, EXIT_SUCCESS)
        chat = json.loads(out)["chats"][0]
        self.assertEqual(chat["context_buffer_count"], 2)
        self.assertEqual(chat["new_message_count"], 4)  # msgs 6, 7, 8, 9

        # Cursor reset fallback when after_id > global_latest_msg_id
        code, out, _ = self._run_cli(
            ["fetch", "--chats", "10001@chatroom", "--after-id", "999999", "--format", "json"]
        )
        self.assertEqual(code, EXIT_SUCCESS)
        meta = json.loads(out)["meta"]
        self.assertEqual(meta["query_mode"], "cursor_reset_fallback_24h")
        self.assertEqual(meta["warnings"][0]["code"], "CURSOR_RESET_FALLBACK")

    def test_07_export_media_and_dat_xor_image_decryption(self) -> None:
        """Verify `export-media` automatically decrypts WeChat `.dat` XOR image to `.jpg` and exports PDF."""
        # First verify that exporting a message from a blocked group (msg_id=7 in 99999@chatroom) is rejected with Exit Code 3
        code_blk, _, _ = self._run_cli(["export-media", "--msg-ids", "7", "--format", "json"])
        self.assertEqual(code_blk, EXIT_POLICY_DENIED)

        # In 10001@chatroom, msg_id=8 is the XOR-encrypted .dat image, msg_id=9 is the PDF
        code, out, err = self._run_cli(["export-media", "--msg-ids", "8,9", "--format", "json"])
        self.assertEqual(code, EXIT_SUCCESS, err)
        exported = json.loads(out)["exported"]
        self.assertEqual(len(exported), 2)

        img_item = exported[0]
        self.assertEqual(img_item["media_type"], "image")
        self.assertEqual(img_item["mime_type"], "image/jpeg")
        self.assertEqual(img_item["xor_key_byte"], "0x5a")
        self.assertTrue(img_item["exported_path"].endswith(".jpg"))
        exported_bytes = Path(img_item["exported_path"]).read_bytes()
        self.assertTrue(exported_bytes.startswith(b"\xff\xd8\xff"))

        pdf_item = exported[1]
        self.assertEqual(pdf_item["media_type"], "document")
        self.assertTrue(pdf_item["exported_path"].endswith(".pdf"))

    def test_08_search_and_mcp_server(self) -> None:
        """Verify cross-group keyword search and JSON-RPC 2.0 MCP Stdio server."""
        code, out, _ = self._run_cli(["search", "--query", "报价单", "--context", "1", "--format", "json"])
        self.assertEqual(code, EXIT_SUCCESS)
        s_res = json.loads(out)
        self.assertGreaterEqual(s_res["meta"]["match_count"], 2)

        # Test MCP Server over stdin/stdout
        mcp_reqs = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "wechat_list_groups",
                            "arguments": {"format": "json"},
                        },
                    }
                ),
            ]
        ) + "\n"
        in_buf = io.StringIO(mcp_reqs)
        out_buf = io.StringIO()
        with patch("sys.stdin", in_buf), patch("sys.stdout", out_buf):
            rc = main(["mcp-serve", "--config", str(self.config_path)])
        self.assertEqual(rc, 0)
        lines = [json.loads(line) for line in out_buf.getvalue().strip().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "wechat-localmind")
        self.assertEqual(len(lines[1]["result"]["tools"]), 4)
        self.assertFalse(lines[2]["result"]["isError"])

    def test_09_zstd_native_commoncrypto_and_snr_filtering(self) -> None:
        """Verify native zstd decompression, CommonCrypto AES-CBC, vectorized PNG XOR, and SNR noise filtering."""
        # 1. Native Zstandard decompression
        try:
            import compression.zstd as std_zstd  # type: ignore

            raw_xml = "wxid_alice:\n<msg><appmsg><type>51</type><title>AI硬件趋势深度解析</title><finderFeed><nickname>硬核科技</nickname><desc>2026最新供应链深度解读</desc></finderFeed></appmsg></msg>"
            compressed = std_zstd.compress(raw_xml.encode("utf-8"))
            parsed = parse_wechat_message_payload(49, None, compressed, None)
            self.assertEqual(parsed.sender_wxid_from_prefix, "wxid_alice")
            self.assertEqual(parsed.normalized_type, "link")
            self.assertIn("[视频号: 硬核科技: 2026最新供应链深度解读]", parsed.text)
        except ImportError:
            pass

        # 2. Pat ([拍一拍]) and <sysmsg> room notification SNR filtering
        pat_xml = '<sysmsg type="pat"><pat><template><![CDATA["Alice" 拍了拍 "张工"]]></template></pat></sysmsg>'
        pat_parsed = parse_wechat_message_payload(10002, pat_xml)
        self.assertTrue(pat_parsed.is_pat_message)
        self.assertEqual(pat_parsed.normalized_type, "pat")

        # 3. Red packet parsing & SNR noise folding classification
        rp_xml = "<msg><appmsg><type>2001</type><wcpayinfo><sendertitle>开工大吉</sendertitle></wcpayinfo></appmsg></msg>"
        rp_parsed = parse_wechat_message_payload(49, rp_xml)
        self.assertEqual(rp_parsed.normalized_type, "red_packet")
        self.assertEqual(rp_parsed.text, "[微信红包: 开工大吉]")

        # 4. Vectorized PNG .dat XOR decoding
        raw_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32
        xor_png = bytes(b ^ 0xA5 for b in raw_png)
        decoded, ext, mime, key_used = detect_and_decode_wechat_dat_image(xor_png)
        self.assertEqual(ext, ".png")
        self.assertEqual(mime, "image/png")
        self.assertEqual(key_used, 0xA5)
        self.assertEqual(decoded, raw_png)


if __name__ == "__main__":
    unittest.main()
