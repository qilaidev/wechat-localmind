"""WeChat message decompression (zstd / lz4), XML <msg>/<appmsg> parser, and Protobuf wire parser."""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from .constants import APPMSG_SUBTYPE_MAP

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SENDER_PREFIX_RE = re.compile(r"^([A-Za-z0-9_\-@.\u4e00-\u9fff]{2,48}):\n(.*)$", re.DOTALL)
XML_TAG_RE = re.compile(r"<(msg|appmsg|sysmsg|voicemsg|img|videomsg)\b", re.IGNORECASE)


@dataclass
class ParsedWeChatPayload:
    text: str = ""
    sender_wxid_from_prefix: str | None = None
    appmsg_subtype: int | None = None
    normalized_type: str | None = None
    quoted_svrid: str | None = None
    quoted_sender_wxid: str | None = None
    quoted_display_name: str | None = None
    quoted_text_snippet: str | None = None
    mentioned_wxids: list[str] = field(default_factory=list)
    media_title: str | None = None
    media_ext: str | None = None
    media_size_bytes: int | None = None
    media_duration_sec: int | None = None
    media_rel_path: str | None = None
    link_url: str | None = None
    is_revoked: bool = False
    is_pat_message: bool = False


def decompress_zstd_or_raw(blob: bytes | str | None) -> str:
    """Decompress WeChat 4.x zstd-compressed `compress_content` or decode raw UTF-8/UTF-16 text."""
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob.strip("\x00").strip()
    if not blob:
        return ""

    data = bytes(blob)
    # Check Zstandard magic number (0xFD2FB528 in little-endian -> 28 B5 2F FD)
    if data.startswith(ZSTD_MAGIC):
        decompressed = _decompress_zstd_bytes(data)
        if decompressed:
            data = decompressed

    # Strip trailing null bytes
    data = data.rstrip(b"\x00")
    for enc in ("utf-8", "utf-16le", "gb18030"):
        try:
            decoded = data.decode(enc)
            if "\x00" not in decoded[:32]:
                return decoded.strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def _decompress_zstd_bytes(data: bytes) -> bytes | None:
    # 1. Python 3.14+ built-in C-level standard library `compression.zstd` (fastest, zero external dependency)
    try:
        import compression.zstd as std_zstd  # type: ignore

        return std_zstd.decompress(data)
    except Exception:
        pass

    # 2. Optional third-party `zstandard` C-extension
    try:
        import zstandard as zstd  # type: ignore

        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data, max_output_size=10 * 1024 * 1024)
    except Exception:
        pass

    # 3. Native `libzstd.dylib` via ctypes on macOS
    try:
        import ctypes

        for lib_path in ("/opt/homebrew/lib/libzstd.dylib", "/usr/local/lib/libzstd.dylib"):
            if os.path.exists(lib_path):
                lib = ctypes.CDLL(lib_path)
                get_size = lib.ZSTD_getFrameContentSize
                get_size.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                get_size.restype = ctypes.c_ulonglong
                frame_sz = get_size(data, len(data))
                out_cap = int(frame_sz) if (0 < frame_sz < 10 * 1024 * 1024) else 512 * 1024
                out_buf = ctypes.create_string_buffer(out_cap)
                dec_fn = lib.ZSTD_decompress
                dec_fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
                dec_fn.restype = ctypes.c_size_t
                res_sz = dec_fn(out_buf, out_cap, data, len(data))
                if 0 < res_sz <= out_cap:
                    return out_buf.raw[:res_sz]
    except Exception:
        pass

    # 4. Fallback to system `zstd -d -q -c` binary
    try:
        proc = subprocess.run(
            ["zstd", "-d", "-q", "-c"],
            input=data,
            capture_output=True,
            timeout=3.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    except Exception:
        pass
    return None


def split_group_sender_prefix(content: str) -> tuple[str | None, str]:
    """Split group message prefix `wxid_xxx:\\nActual message` into (sender_wxid, body)."""
    if not content:
        return None, ""
    m = SENDER_PREFIX_RE.match(content)
    if m:
        candidate_sender = m.group(1).strip()
        # Ensure prefix is not an XML tag or URL scheme
        if not candidate_sender.startswith("<") and candidate_sender.lower() not in ("http", "https", "mailto"):
            return candidate_sender, m.group(2).strip()
    return None, content


def parse_protobuf_strings(blob: bytes | None) -> dict[int, list[str]]:
    """Extract UTF-8 strings keyed by field_num from WeChat `packed_info_data` / `BytesExtra` Protobuf blobs."""
    out: dict[int, list[str]] = {}
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return out

    data = bytes(blob)
    if data.startswith(ZSTD_MAGIC):
        dec = _decompress_zstd_bytes(data)
        if dec:
            data = dec

    i = 0
    n = len(data)
    while i < n:
        tag = 0
        shift = 0
        while i < n:
            b = data[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 63:
                return out

        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 0:
            break

        if wire_type == 0:  # Varint
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                if not (b & 0x80):
                    break
                shift += 7
                if shift > 63:
                    return out
        elif wire_type == 1:  # 64-bit
            i += 8
        elif wire_type == 2:  # Length-delimited
            length = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
                if shift > 63:
                    return out
            if length < 0 or i + length > n:
                break
            chunk = data[i : i + length]
            i += length

            # Check if chunk is a nested protobuf message or a readable UTF-8 string
            try:
                s = chunk.decode("utf-8").strip()
                if s and all(ord(c) >= 0x20 or c in "\n\r\t" for c in s):
                    out.setdefault(field_num, []).append(s)
                    continue
            except UnicodeDecodeError:
                pass

            # Recurse one level for nested submessages (like Windows BytesExtra key-value items)
            nested = parse_protobuf_strings(chunk)
            for k, vals in nested.items():
                for v in vals:
                    out.setdefault(k, []).append(v)
        elif wire_type == 5:  # 32-bit
            i += 4
        else:
            break

    return out


def extract_media_path_from_blob(blob: bytes | None) -> str | None:
    """Find relative attachment/image/voice file path stored inside `packed_info_data` or `BytesExtra`."""
    fields = parse_protobuf_strings(blob)
    path_candidates: list[str] = []
    for vals in fields.values():
        for s in vals:
            clean = s.replace("\\", "/").strip()
            if (
                any(
                    ext in clean.lower()
                    for ext in (".dat", ".jpg", ".png", ".mp4", ".silk", ".aud", ".opus", ".pdf", ".xlsx", ".docx", ".zip")
                )
                or clean.startswith(("msg/", "attach/", "file/", "video/", "voice/", "FileStorage/"))
            ):
                path_candidates.append(clean)
    return path_candidates[0] if path_candidates else None


def _extract_xml_fragment(text: str) -> str | None:
    idx = text.find("<")
    if idx == -1:
        return None
    xml_part = text[idx:].strip()
    if not XML_TAG_RE.search(xml_part):
        return None
    return xml_part


def _find_text(elem: ET.Element | None, path: str) -> str:
    if elem is None:
        return ""
    node = elem.find(path)
    if node is not None and node.text:
        return node.text.strip()
    return ""


def parse_wechat_message_payload(
    local_type: int,
    raw_content: str | bytes | None,
    compress_content: bytes | str | None = None,
    packed_blob: bytes | None = None,
) -> ParsedWeChatPayload:
    """Parse WeChat message text, zstd-compressed XML, quote replies (type 49/57), files (49/6), and revocations."""
    result = ParsedWeChatPayload()
    primary_str = decompress_zstd_or_raw(compress_content) or decompress_zstd_or_raw(raw_content)
    sender_prefix, body_str = split_group_sender_prefix(primary_str)
    result.sender_wxid_from_prefix = sender_prefix

    # Extract optional media path from protobuf auxiliary blob
    result.media_rel_path = extract_media_path_from_blob(packed_blob)

    # Check if protobuf blob also carries a sender wxid (Windows BytesExtra often stores wxid)
    if not result.sender_wxid_from_prefix and packed_blob:
        pb_strs = parse_protobuf_strings(packed_blob)
        for vals in pb_strs.values():
            for v in vals:
                if (v.startswith("wxid_") or "@" not in v) and "/" not in v and "." not in v and 4 <= len(v) <= 36:
                    if v.startswith("wxid_"):
                        result.sender_wxid_from_prefix = v
                        break

    # 1. Plain text message (local_type == 1)
    if local_type == 1:
        result.normalized_type = "text"
        # Sometimes <msgsource><atuserlist> is attached or stored in packed_blob
        xml_frag = _extract_xml_fragment(body_str)
        if xml_frag and body_str.startswith("<"):
            _parse_xml_into_payload(xml_frag, local_type, result)
        else:
            result.text = body_str
        return result

    # 2. System XML message (local_type == 10002: revokemsg / pat / sysmsg)
    if local_type == 10002:
        xml_frag = _extract_xml_fragment(body_str)
        if xml_frag:
            _parse_sysmsg_xml(xml_frag, result)
            return result
        if "撤回了一条消息" in body_str or "recalled a message" in body_str.lower():
            result.is_revoked = True
            result.normalized_type = "revoked"
            return result
        # Any other 10002 system notification without business payload is system_event noise
        result.normalized_type = "system_event"
        result.text = body_str
        return result

    # 3. Rich XML / Media messages (3=image, 34=audio, 42=contact, 43=video, 47=sticker, 48=location, 49=appmsg)
    xml_frag = _extract_xml_fragment(body_str)
    if xml_frag:
        _parse_xml_into_payload(xml_frag, local_type, result)
        if result.normalized_type:
            return result

    # Fallback by integer type
    if local_type == 3:
        result.normalized_type = "image"
        result.media_title = result.media_title or "图片"
    elif local_type == 34:
        result.normalized_type = "audio"
        result.media_title = result.media_title or "语音"
    elif local_type in (43, 62):
        result.normalized_type = "video"
        result.media_title = result.media_title or "视频"
    elif local_type == 47:
        result.normalized_type = "sticker"
        result.text = body_str if (body_str and not body_str.startswith("<")) else ""
    elif local_type == 42:
        result.normalized_type = "contact"
        result.text = f"[名片分享: {body_str or '微信名片'}]"
    elif local_type == 48:
        result.normalized_type = "location"
        result.text = f"[位置分享: {body_str or '位置'}]"
    else:
        result.normalized_type = "text"
        result.text = body_str

    return result


def _parse_sysmsg_xml(xml_str: str, result: ParsedWeChatPayload) -> None:
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        if "revokemsg" in xml_str or "撤回了一条消息" in xml_str:
            result.is_revoked = True
            result.normalized_type = "revoked"
        return

    sys_type = (root.attrib.get("type") or "").lower()
    if sys_type == "revokemsg" or root.find("revokemsg") is not None:
        result.is_revoked = True
        result.normalized_type = "revoked"
        return

    if sys_type == "pat" or root.find("pat") is not None:
        result.is_pat_message = True
        result.normalized_type = "pat"
        tmpl = _find_text(root, ".//template")
        result.text = f"[拍一拍: {tmpl}]" if tmpl else "[拍一拍]"
        return

    tmpl = _find_text(root, ".//content_template/template") or _find_text(root, ".//plain")
    result.normalized_type = "system_event"
    result.text = tmpl or ""


def _parse_xml_into_payload(xml_str: str, local_type: int, result: ParsedWeChatPayload) -> None:
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        # Fallback regex extraction if XML contains unescaped characters
        _regex_fallback_xml(xml_str, local_type, result)
        return

    # Extract @mentions from <msgsource><atuserlist>wxid_1,wxid_2</atuserlist></msgsource>
    at_list_str = _find_text(root, ".//atuserlist")
    if at_list_str:
        for wxid in at_list_str.split(","):
            w = wxid.strip()
            if w and w not in result.mentioned_wxids:
                result.mentioned_wxids.append(w)

    # Voice message (<voicemsg voicelength="15000" ... />)
    voicemsg = root.find(".//voicemsg") if root.tag != "voicemsg" else root
    if local_type == 34 or voicemsg is not None:
        result.normalized_type = "audio"
        if voicemsg is not None:
            vlen_ms = voicemsg.attrib.get("voicelength") or "0"
            try:
                result.media_duration_sec = max(1, round(int(vlen_ms) / 1000))
            except ValueError:
                result.media_duration_sec = 0
        return

    # Image message (<img hdlength="..." length="..." />)
    img_node = root.find(".//img") if root.tag != "img" else root
    if local_type == 3 or (img_node is not None and local_type != 49):
        result.normalized_type = "image"
        if img_node is not None:
            size_str = img_node.attrib.get("hdlength") or img_node.attrib.get("length") or "0"
            try:
                result.media_size_bytes = int(size_str)
            except ValueError:
                pass
        return

    # Video message (<videomsg playlength="12" length="1048576" />)
    vid_node = root.find(".//videomsg") if root.tag != "videomsg" else root
    if local_type in (43, 62) or (vid_node is not None and local_type != 49):
        result.normalized_type = "video"
        if vid_node is not None:
            try:
                result.media_duration_sec = int(vid_node.attrib.get("playlength") or "0")
                result.media_size_bytes = int(vid_node.attrib.get("length") or "0")
            except ValueError:
                pass
        return

    # Location (<location x="22.54" y="114.05" label="..." poiname="..." />)
    loc_node = root.find(".//location") if root.tag != "location" else root
    if local_type == 48 and loc_node is not None:
        result.normalized_type = "location"
        lat = loc_node.attrib.get("x") or ""
        lon = loc_node.attrib.get("y") or ""
        label = loc_node.attrib.get("poiname") or loc_node.attrib.get("label") or ""
        result.text = f"[位置分享: ({lat}, {lon}) {label}]".strip()
        return

    # Contact card (<msg username="..." nickname="..." />)
    if local_type == 42:
        result.normalized_type = "contact"
        cname = root.attrib.get("nickname") or root.attrib.get("alias") or "微信名片"
        result.text = f"[名片分享: {cname}]"
        return

    # Emoji / Sticker (<emoji ... />)
    if local_type == 47:
        result.normalized_type = "sticker"
        return

    # AppMsg (<appmsg><type>...</type><title>...</title>...)
    appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
    if appmsg is not None:
        type_str = _find_text(appmsg, "type")
        try:
            sub_type = int(type_str)
        except ValueError:
            sub_type = 0
        result.appmsg_subtype = sub_type
        mapped = APPMSG_SUBTYPE_MAP.get(sub_type, "app_msg")

        title = _find_text(appmsg, "title")
        des = _find_text(appmsg, "des")
        url = _find_text(appmsg, "url")
        result.link_url = url or None

        if sub_type == 57:  # Quote / Reply message (<refermsg>)
            result.normalized_type = "text"
            result.text = title
            refer = appmsg.find("refermsg")
            if refer is not None:
                result.quoted_svrid = _find_text(refer, "svrid") or None
                result.quoted_sender_wxid = _find_text(refer, "chatusr") or _find_text(refer, "fromusr") or None
                result.quoted_display_name = _find_text(refer, "displayname") or None
                ref_content = _find_text(refer, "content")
                ref_type_str = _find_text(refer, "type")
                if ref_type_str == "1" or not ref_content.startswith("<"):
                    _, clean_ref = split_group_sender_prefix(ref_content)
                    result.quoted_text_snippet = clean_ref
                else:
                    # Quoted parent might itself be an appmsg or media
                    sub_parsed = parse_wechat_message_payload(int(ref_type_str or 49), ref_content)
                    result.quoted_text_snippet = (
                        sub_parsed.text or sub_parsed.media_title or f"[{sub_parsed.normalized_type or '附件'}]"
                    )
            return

        if sub_type == 6:  # File / Document attachment (<appattach><totallen>...</totallen><fileext>...</fileext>)
            result.normalized_type = "document"
            result.media_title = title or "未命名文档"
            attach = appmsg.find("appattach")
            if attach is not None:
                ext = _find_text(attach, "fileext")
                result.media_ext = ext or None
                tlen = _find_text(attach, "totallen")
                try:
                    result.media_size_bytes = int(tlen)
                except ValueError:
                    pass
            return

        if sub_type == 5:  # Web Link share
            result.normalized_type = "link"
            link_part = f" ({url})" if url else ""
            desc_part = f" - {des}" if des else ""
            result.text = f"[链接分享: {title or '网页'}{desc_part}{link_part}]"
            return

        if sub_type in (33, 36):  # WeChat Mini-Program
            result.normalized_type = "link"
            src_display = _find_text(appmsg, "sourcedisplayname")
            prefix = f"{src_display} · " if src_display else ""
            result.text = f"[小程序: {prefix}{title or des or '小程序卡片'}]"
            return

        if sub_type == 19:  # Merged chat history (<recorditem>)
            result.normalized_type = "text"
            result.text = f"[合并转发的聊天记录: {title or '聊天记录'} ({des})]" if des else f"[合并转发的聊天记录: {title}]"
            return

        if sub_type == 87:  # Group announcement (@所有人)
            result.normalized_type = "text"
            edit_text = _find_text(appmsg, "textannouncement") or title or des
            result.text = f"[群公告] {edit_text}".strip()
            if "all" not in result.mentioned_wxids:
                result.mentioned_wxids.append("notify@all")
            return

        if sub_type in (51, 63):  # WeChat Channels video / live stream (视频号)
            result.normalized_type = "link"
            finder_nick = _find_text(appmsg, ".//finderFeed/nickname") or _find_text(appmsg, ".//finderLive/nickname")
            finder_desc = _find_text(appmsg, ".//finderFeed/desc") or _find_text(appmsg, ".//finderLive/desc") or title or des
            prefix = f"{finder_nick}: " if finder_nick else ""
            tag_label = "视频号直播" if sub_type == 63 else "视频号"
            result.text = f"[{tag_label}: {prefix}{finder_desc or '视频号动态'}]"
            return

        if sub_type == 2001:  # WeChat Red Packet (微信红包)
            result.normalized_type = "red_packet"
            pay_title = _find_text(appmsg, ".//wcpayinfo/sendertitle") or _find_text(appmsg, ".//wcpayinfo/scenetext") or title or "恭喜发财，大吉大利"
            result.text = f"[微信红包: {pay_title}]"
            return

        if sub_type == 2000:  # WeChat Transfer (微信转账)
            result.normalized_type = "transfer"
            fee_desc = _find_text(appmsg, ".//wcpayinfo/feedesc") or title or ""
            pay_memo = _find_text(appmsg, ".//wcpayinfo/pay_memo") or des or ""
            memo_part = f" ({pay_memo})" if pay_memo else ""
            result.text = f"[微信转账: {fee_desc}{memo_part}]".strip()
            return

        result.normalized_type = mapped if mapped in ("image", "audio", "video", "document", "sticker") else "text"
        result.text = title or des or f"[应用消息: type={sub_type}]"


def _regex_fallback_xml(xml_str: str, local_type: int, result: ParsedWeChatPayload) -> None:
    m_title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", xml_str, re.DOTALL)
    title = (m_title.group(1) or m_title.group(2) or "").strip() if m_title else ""
    m_type = re.search(r"<type>(\d+)</type>", xml_str)
    sub_type = int(m_type.group(1)) if m_type else 0

    if sub_type == 57:
        result.normalized_type = "text"
        result.text = title
        m_svrid = re.search(r"<svrid>(\d+)</svrid>", xml_str)
        if m_svrid:
            result.quoted_svrid = m_svrid.group(1)
        m_disp = re.search(r"<displayname><!\[CDATA\[(.*?)\]\]></displayname>|<displayname>(.*?)</displayname>", xml_str)
        if m_disp:
            result.quoted_display_name = (m_disp.group(1) or m_disp.group(2) or "").strip()
        m_ref = re.search(r"<content><!\[CDATA\[(.*?)\]\]></content>|<content>(.*?)</content>", xml_str, re.DOTALL)
        if m_ref:
            result.quoted_text_snippet = (m_ref.group(1) or m_ref.group(2) or "").strip()
    elif sub_type == 6:
        result.normalized_type = "document"
        result.media_title = title or "未命名文档"
    else:
        result.normalized_type = "text"
        result.text = title or xml_str
