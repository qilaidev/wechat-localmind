"""Semantic text cleaning for WeChat: @mention resolution, [@我] tagging, and ack noise folding."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .config import AppConfig
from .constants import ACK_NOISE_PHRASES, DEFAULT_REPLY_SNIPPET_MAX_LEN

BIDI_CONTROL_CHARS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# WeChat uses \u2005 (Four-Per-Em Space) after @mentions in group chat text
WECHAT_AT_SPACE = "\u2005"


def clean_unicode_display_name(val: str | None) -> str:
    """Strip invisible Unicode directional marks and normalize non-breaking spaces/hyphens."""
    if not val:
        return ""
    s = BIDI_CONTROL_CHARS_RE.sub("", val)
    s = s.replace("\xa0", " ").replace("\u2005", " ").replace("\u2011", "-")
    return " ".join(s.split()).strip()


def format_fallback_sender_name(wxid: str | None) -> str:
    """Format anonymized fallback name when contact remark, group nickname, and nickname are all empty."""
    if not wxid:
        return "未知群友"
    clean = wxid.strip()
    if clean.startswith("wxid_"):
        core = clean[5:]
        tail = core[-4:] if len(core) >= 4 else core
        return f"微信群友({tail})"
    tail = clean[-4:] if len(clean) >= 4 else clean
    return f"群友({tail})"


def disambiguate_punctuation_name(name: str, wxid: str | None) -> str:
    """If a user's nickname consists purely of punctuation (e.g. '.'), append a short wxid suffix tag."""
    if not name:
        return format_fallback_sender_name(wxid)
    has_word_or_emoji = any(
        ch.isalnum() or unicodedata.category(ch) in ("Lo", "So") or ord(ch) >= 0x1F000
        for ch in name
    )
    if has_word_or_emoji:
        return name
    if wxid:
        tail = wxid[-4:] if len(wxid) >= 4 else wxid
        return f"{name}(#{tail})"
    return name


def is_my_wxid(
    wxid: str | None,
    config: AppConfig,
    auto_self_wxid: str | None = None,
) -> bool:
    """Determine whether a wxid belongs to the user identity."""
    if not wxid:
        return False
    w = wxid.strip()
    if auto_self_wxid and w == auto_self_wxid:
        return True
    if config.identity.wxid and w == config.identity.wxid.strip():
        return True
    if config.identity.alias and w == config.identity.alias.strip():
        return True
    return False


def replace_wechat_mentions(
    text: str,
    mentioned_wxids: list[str],
    config: AppConfig,
    auto_self_wxid: str | None = None,
    name_resolver: Any | None = None,
) -> tuple[str, bool]:
    """Normalize WeChat @mentions (\u2005 delimiter), resolve raw @wxid_xxx to real names, and tag [@我(Name)]."""
    mentions_me = False
    for m_wxid in mentioned_wxids:
        if m_wxid in ("notify@all", "all", "@all") or is_my_wxid(m_wxid, config, auto_self_wxid):
            mentions_me = True

    if not text:
        return "", mentions_me

    # Normalize WeChat \u2005 space after @name into standard space
    cleaned = text.replace(WECHAT_AT_SPACE, " ")
    my_label = f"[@{config.my_display_label}]"

    if "@所有人" in cleaned or "@All" in cleaned:
        mentions_me = True

    my_name = (config.identity.name or "").strip()
    if my_name and my_name != "我":
        if f"@{my_name}" in cleaned:
            mentions_me = True
            cleaned = cleaned.replace(f"@{my_name}", my_label)

    # Resolve any raw @wxid_xxx tokens in text to real names or [@我(...)]
    if "@wxid_" in cleaned:
        def _sub_wxid(match: re.Match[str]) -> str:
            nonlocal mentions_me
            raw_wxid = match.group(1)
            if is_my_wxid(raw_wxid, config, auto_self_wxid):
                mentions_me = True
                return my_label
            if callable(name_resolver):
                resolved = name_resolver(raw_wxid)
                if resolved and not resolved.startswith("微信群友("):
                    return f"@{resolved}"
            return match.group(0)

        cleaned = re.sub(r"@(wxid_[A-Za-z0-9_\-]{4,32})", _sub_wxid, cleaned)

    if mentions_me and my_label not in cleaned and "@所有人" not in cleaned and "@All" not in cleaned:
        cleaned = f"{my_label} {cleaned}".strip()

    return cleaned.strip(), mentions_me


def is_pure_emoji_text(s: str) -> bool:
    """Return True if string consists purely of emojis, symbols, or WeChat bracketed sticker tags like [呲牙]."""
    stripped = "".join(ch for ch in s if not ch.isspace())
    if not stripped or len(stripped) > 12:
        return False
    # Check WeChat single built-in emoji tag e.g. [OK], [握手], [强]
    if re.fullmatch(r"(\[[A-Za-z\u4e00-\u9fff]{1,4}\]){1,2}", stripped):
        return True
    for ch in stripped:
        if ch in ("\ufe0f", "\ufe0e", "\u200d", "\u20e3"):
            continue
        cat = unicodedata.category(ch)
        if cat not in ("So", "Sk") and ord(ch) < 0x1F000:
            return False
    return True


def is_short_ack_noise(
    text: str,
    msg_type: str,
    has_reply: bool,
    has_media: bool,
    mentions_me: bool,
) -> bool:
    """Determine if a message is a short acknowledgment noise ('收到', 'OK', '+1', '[强]', pure emoji, or red packet)."""
    if has_reply or has_media or mentions_me:
        return False
    if msg_type == "red_packet":
        return True
    if msg_type != "text":
        return False
    clean = (text or "").strip()
    if not clean or "@" in clean or len(clean) > 10:
        return False
    normalized = clean.lower().rstrip("!！.。~～,，")
    if normalized in ACK_NOISE_PHRASES or clean.lower() in ACK_NOISE_PHRASES:
        return True
    return is_pure_emoji_text(clean)


def truncate_reply_snippet(text: str | None, max_len: int = DEFAULT_REPLY_SNIPPET_MAX_LEN) -> str:
    """Truncate quoted parent message text to 150 characters with single-line normalization."""
    if not text:
        return ""
    single_line = " ".join(text.splitlines()).strip()
    if len(single_line) <= max_len:
        return single_line
    return single_line[:max_len] + "..."


def fold_consecutive_ack_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group consecutive non-buffer ack noise messages into folded summary entries for Markdown rendering."""
    result: list[dict[str, Any]] = []
    ack_run: list[dict[str, Any]] = []

    def _flush_ack_run() -> None:
        nonlocal ack_run
        if not ack_run:
            return
        if len(ack_run) == 1:
            result.append(ack_run[0])
        else:
            senders: list[str] = []
            for m in ack_run:
                s = m["sender_name"]
                if s not in senders:
                    senders.append(s)
            sender_preview = "、".join(senders[:3])
            if len(senders) > 3:
                sender_preview += f"等 {len(senders)} 人"
            else:
                sender_preview += f" ({len(ack_run)}条)"
            first_msg = ack_run[0]
            last_msg = ack_run[-1]
            sample_texts = []
            for m in ack_run:
                t = m["text"].strip()
                if t and t not in sample_texts:
                    sample_texts.append(t)
                if len(sample_texts) >= 2:
                    break
            sample_str = "/".join(sample_texts) if sample_texts else "收到/OK"
            result.append(
                {
                    "_folded_ack_summary": True,
                    "start_msg_id": first_msg["msg_id"],
                    "end_msg_id": last_msg["msg_id"],
                    "timestamp": last_msg["timestamp"],
                    "is_context_buffer": first_msg["is_context_buffer"],
                    "count": len(ack_run),
                    "sender_preview": sender_preview,
                    "sample_text": sample_str,
                }
            )
        ack_run = []

    for msg in messages:
        if msg.get("is_ack_noise") and not msg.get("is_context_buffer"):
            ack_run.append(msg)
        else:
            _flush_ack_run()
            result.append(msg)
    _flush_ack_run()
    return result
