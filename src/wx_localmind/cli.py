"""CLI entrypoint and subcommand dispatcher for `wechat-localmind`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig
from .constants import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_FETCH_LIMIT,
    EXIT_INTERNAL_DB_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
)
from .crypto import (
    decrypt_sqlcipher_database,
    extract_keys_from_memory_dump,
    parse_hex_key,
)
from .db import QueryEngine
from .errors import EnvOrPermissionError, UsageError, WXLocalMindError
from .formatter import (
    format_fetch_markdown,
    format_groups_markdown,
    format_json_output,
    format_search_markdown,
)
from .mcp_server import run_mcp_stdio_server
from .media import MediaExporter
from .snapshot import SnapshotManager, discover_wechat_account


class _RigorousArgumentParser(argparse.ArgumentParser):
    """ArgumentParser subclass that raises UsageError (Exit Code 1) instead of default exit(2)."""

    def error(self, message: str) -> None:
        raise UsageError(message)


def _split_csv_list(items: list[str] | str | None) -> list[str]:
    if not items:
        return []
    if isinstance(items, str):
        items = [items]
    out: list[str] = []
    for item in items:
        for part in item.split(","):
            clean = part.strip()
            if clean:
                out.append(clean)
    return out


def _add_common_options(parser: argparse.ArgumentParser, default_format: str = "json") -> None:
    parser.add_argument(
        "--format",
        choices=["json", "md", "markdown"],
        default=default_format,
        help=f"Output format: json or md (default: {default_format}).",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore TTL cache and force immediate SQLCipher decryption and snapshot refresh.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config.json (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress non-fatal diagnostic messages on stderr.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _RigorousArgumentParser(
        prog="wechat-localmind",
        description="WeChat-LocalMind: macOS & Windows WeChat Local Group Data Extraction Capability Layer (CLI & MCP).",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"wechat-localmind {__version__}",
    )

    subparsers = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")

    # 1. doctor
    p_doc = subparsers.add_parser(
        "doctor",
        help="Run environment, WeChat 3.x/4.x database discovery, and SQLCipher key health checks.",
    )
    _add_common_options(p_doc, default_format="json")

    # 2. key
    p_key = subparsers.add_parser(
        "key",
        help="Manage SQLCipher 3/4 decryption keys, import keys.json, or scan a memory dump.",
    )
    p_key.add_argument(
        "--set",
        dest="set_key",
        default=None,
        help="Set global 64-hex (32-byte) SQLCipher key, or '<db_name>:<64-hex-key>' for a specific shard.",
    )
    p_key.add_argument(
        "--import-file",
        "--import",
        dest="import_file",
        type=Path,
        default=None,
        help="Import per-database keys from a JSON file (e.g. all_keys.json / keys.json).",
    )
    p_key.add_argument(
        "--scan-dump",
        type=Path,
        default=None,
        help="Scan a memory dump or LLDB log file to auto-match SQLCipher keys against local WeChat DB salts.",
    )
    p_key.add_argument(
        "--verify",
        action="store_true",
        help="Verify configured keys against all discovered local WeChat databases via Page 1 HMAC.",
    )
    _add_common_options(p_key, default_format="json")

    # 3. decrypt
    p_dec = subparsers.add_parser(
        "decrypt",
        help="Decrypt a single SQLCipher 3/4 database or force-build the unified plaintext SQLite snapshot.",
    )
    p_dec.add_argument("--src", type=Path, default=None, help="Optional single encrypted .db file to decrypt.")
    p_dec.add_argument("--dst", type=Path, default=None, help="Optional output path for single decrypted .db file.")
    p_dec.add_argument("--key", default=None, help="Optional explicit 64-hex key for single-file decryption.")
    _add_common_options(p_dec, default_format="json")

    # 4. config
    p_cfg = subparsers.add_parser(
        "config",
        help="Inspect or update identity anchor and group blacklist/whitelist rules.",
    )
    cfg_sub = p_cfg.add_subparsers(dest="config_action", metavar="<action>")

    p_cfg_show = cfg_sub.add_parser("show", help="Print current configuration as JSON.")
    _add_common_options(p_cfg_show, default_format="json")

    p_cfg_id = cfg_sub.add_parser("set-identity", help="Set user's display name and WeChat wxid/alias anchor.")
    p_cfg_id.add_argument("--name", required=True, help="Your display name in groups (e.g. '张工').")
    p_cfg_id.add_argument("--wxid", default="", help="Your WeChat ID (e.g. 'wxid_xxx').")
    p_cfg_id.add_argument("--alias", default="", help="Your custom WeChat alias ID.")
    _add_common_options(p_cfg_id, default_format="json")

    p_cfg_mode = cfg_sub.add_parser("set-mode", help="Switch group filtering mode (blacklist | whitelist).")
    p_cfg_mode.add_argument("mode", choices=["blacklist", "whitelist"], help="Target filter mode.")
    _add_common_options(p_cfg_mode, default_format="json")

    for act, help_txt in (
        ("block", "Add group chat_id(s) to blacklist."),
        ("unblock", "Remove group chat_id(s) from blacklist."),
        ("allow", "Add group chat_id(s) to whitelist."),
        ("disallow", "Remove group chat_id(s) from whitelist."),
    ):
        p_act = cfg_sub.add_parser(act, help=help_txt)
        p_act.add_argument("chats", nargs="+", help="One or more group chat_ids (@chatroom), comma-separated.")
        _add_common_options(p_act, default_format="json")

    # 5. groups / list-groups
    for cmd_name in ("groups", "list-groups"):
        p_grp = subparsers.add_parser(
            cmd_name,
            help="List accessible WeChat group chats (@chatroom) and their latest message cursors.",
        )
        p_grp.add_argument("--keyword", default=None, help="Filter groups by name or chat_id keyword.")
        p_grp.add_argument(
            "--include-blocked",
            action="store_true",
            help="Include groups blocked by current blacklist/whitelist policy.",
        )
        _add_common_options(p_grp, default_format="json")

    # 6. fetch
    p_fetch = subparsers.add_parser(
        "fetch",
        help="Fetch cleaned group chat messages by incremental cursor (--after-id) or time window (--since).",
    )
    p_fetch.add_argument(
        "--chats",
        required=True,
        action="append",
        help="Comma-separated group chat_ids (@chatroom) or 'ALL_ALLOWED'.",
    )
    p_fetch.add_argument(
        "--after-id",
        type=int,
        default=None,
        help="Global monotonic message cursor (msg_id). Fetches only messages with msg_id > after-id.",
    )
    p_fetch.add_argument(
        "--since",
        default=None,
        help="Relative time window (e.g. '12h', '24h', '7d') or ISO-8601 timestamp when --after-id is not set.",
    )
    p_fetch.add_argument(
        "--until",
        default=None,
        help="Optional upper bound ISO-8601 timestamp or relative time.",
    )
    p_fetch.add_argument(
        "--context-buffer",
        type=int,
        default=None,
        help="Number of preceding messages per group to include as context buffer (default: 10).",
    )
    p_fetch.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_FETCH_LIMIT,
        help=f"Maximum messages per group chat (default: {DEFAULT_FETCH_LIMIT}, max: 3000).",
    )
    fold_grp = p_fetch.add_mutually_exclusive_group()
    fold_grp.add_argument(
        "--fold",
        dest="fold_override",
        action="store_true",
        default=None,
        help="Force enable folding of consecutive short acknowledgment messages.",
    )
    fold_grp.add_argument(
        "--no-fold",
        dest="fold_override",
        action="store_false",
        default=None,
        help="Disable folding of short acknowledgment messages.",
    )
    p_fetch.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Optional file path to save the output.",
    )
    _add_common_options(p_fetch, default_format="md")

    # 7. search
    p_search = subparsers.add_parser(
        "search",
        help="Search messages across allowed WeChat groups with surrounding context window.",
    )
    p_search.add_argument("--query", "-k", required=True, help="Keyword to search in messages and attachment titles.")
    p_search.add_argument(
        "--chats",
        action="append",
        default=None,
        help="Optional group chat_ids (@chatroom) or 'ALL_ALLOWED'.",
    )
    p_search.add_argument("--since", default=None, help="Optional time window filter (e.g. '30d', '7d').")
    p_search.add_argument(
        "--context",
        type=int,
        default=3,
        help="Number of before/after context messages around each match (default: 3).",
    )
    p_search.add_argument("--limit", type=int, default=50, help="Maximum matched messages to return (default: 50).")
    _add_common_options(p_search, default_format="md")

    # 8. export-media
    p_media = subparsers.add_parser(
        "export-media",
        help="Safely export group media attachments (auto-decoding .dat XOR images, voice, documents) by msg_id.",
    )
    p_media.add_argument(
        "--msg-ids",
        "--msg-id",
        dest="msg_ids",
        required=True,
        action="append",
        help="Comma-separated target message msg_id(s) to export.",
    )
    p_media.add_argument(
        "--dest-dir",
        type=Path,
        default=None,
        help="Optional destination directory (default: snapshot/media/).",
    )
    _add_common_options(p_media, default_format="json")

    # 9. clean
    p_clean = subparsers.add_parser(
        "clean",
        help="Clean exported media cache or destroy local decrypted & unified SQLite snapshot files.",
    )
    p_clean.add_argument(
        "--media-only",
        action="store_true",
        help="Only remove exported media files under snapshot/media/, keeping SQLite snapshot.",
    )
    p_clean.add_argument(
        "--all",
        action="store_true",
        help="Remove both exported media cache and SQLite snapshot files (default).",
    )
    _add_common_options(p_clean, default_format="json")

    # 10. mcp-serve
    p_mcp = subparsers.add_parser(
        "mcp-serve",
        help="Start read-only Model Context Protocol (MCP) Server over Stdio.",
    )
    p_mcp.add_argument("--config", type=Path, default=None, help="Optional path to config.json.")

    # 11. install
    p_inst = subparsers.add_parser(
        "install",
        help="Install `wechat-localmind` CLI symlink to ~/.local/bin and optionally install SKILL.md bundle.",
    )
    p_inst.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local" / "bin",
        help="Target directory for CLI symlink (default: ~/.local/bin).",
    )
    p_inst.add_argument(
        "--skill-dir",
        type=Path,
        default=None,
        help="Optional target skills parent directory (e.g. ~/.gemini/config/skills or ~/.claude/skills).",
    )

    return parser


def _handle_doctor(args: argparse.Namespace) -> str:
    cfg = AppConfig.load(args.config)
    snap_mgr = SnapshotManager(cfg)
    src_check = snap_mgr.check_source_access()

    snapshot_status: dict[str, Any] = {"ok": False}
    warnings: list[dict[str, Any]] = []
    if src_check.get("source_db_readable"):
        snap = snap_mgr.ensure_snapshot(force_fresh=args.fresh)
        with QueryEngine(cfg, snap) as engine:
            warnings = engine.build_common_warnings()
        snapshot_status = {
            "ok": True,
            "snapshot_db_path": str(snap.snapshot_db_path),
            "snapshot_time": snap.snapshot_time_iso,
            "snapshot_age_sec": snap.snapshot_age_sec,
            "db_latest_msg_time": snap.db_latest_msg_time_iso,
            "global_latest_msg_id": snap.global_latest_msg_id,
            "backup_duration_ms": snap.backup_duration_ms,
        }

    overall_healthy = bool(src_check.get("source_db_readable") and snapshot_status.get("ok"))
    report = {
        "healthy": overall_healthy,
        "version": __version__,
        "source_database": src_check,
        "snapshot_engine": snapshot_status,
        "config": {
            "config_path": str(args.config or DEFAULT_CONFIG_PATH),
            "filter_mode": cfg.filter_mode,
            "identity_label": cfg.my_display_label,
            "blacklist_count": len(cfg.blacklist_chats),
            "whitelist_count": len(cfg.whitelist_chats),
        },
        "warnings": warnings,
    }
    if not overall_healthy:
        raise EnvOrPermissionError(
            src_check.get("error") or "WeChat local database check failed.",
            error_code="DOCTOR_CHECK_FAILED",
        )
    if args.format in ("md", "markdown"):
        lines = [
            "# WeChat-LocalMind 微信本地提取环境自检报告 (Doctor)",
            f"- 整体状态: {'🟢 正常就绪 (Healthy)' if overall_healthy else '🔴 异常'}",
            f"- 账号目录: `{src_check.get('account_dir')}` (微信号: `{src_check.get('wxid')}`, 架构: `{src_check.get('arch_version')}`)",
            f"- 消息分片库数量: `{src_check.get('message_db_count', 0)}` 个 (已验证密钥/可读: `{src_check.get('key_verified_db_count', 0)}` 个)",
            f"- 统一快照路径: `{snapshot_status.get('snapshot_db_path')}` (构建耗时: `{snapshot_status.get('backup_duration_ms')} ms`)",
            f"- 全局最新消息游标 (`global_latest_msg_id`): `{snapshot_status.get('global_latest_msg_id')}`",
            f"- 原库最后活跃时间: `{snapshot_status.get('db_latest_msg_time')}`",
            f"- 当前身份锚点: `{cfg.my_display_label}` | 过滤模式: `{cfg.filter_mode}`",
        ]
        for w in warnings:
            lines.append(f"- [⚠️ 预警 {w.get('code')}]: {w.get('message')}")
        return "\n".join(lines) + "\n"
    return format_json_output(report)


def _handle_key(args: argparse.Namespace) -> str:
    cfg = AppConfig.load(args.config)
    updated = False

    if args.set_key:
        val = args.set_key.strip()
        if ":" in val and not val.lower().startswith("0x"):
            db_name, k_hex = val.split(":", 1)
            parse_hex_key(k_hex.strip())
            cfg.db_keys[db_name.strip()] = k_hex.strip()
        else:
            parse_hex_key(val)
            cfg.raw_key_hex = val
        updated = True

    if args.import_file:
        imp_path = args.import_file.expanduser().resolve()
        if not imp_path.is_file():
            raise UsageError(f"Key file not found: {imp_path}")
        raw_json = json.loads(imp_path.read_text(encoding="utf-8"))
        if not isinstance(raw_json, dict):
            raise UsageError("Key file must be a JSON object mapping db_name -> hex_key.")
        for k, v in raw_json.items():
            if isinstance(v, str):
                parse_hex_key(v)
                cfg.db_keys[k] = v.strip()
            elif isinstance(v, dict) and ("key" in v or "enc_key" in v):
                k_str = str(v.get("key") or v.get("enc_key") or "").strip()
                parse_hex_key(k_str)
                cfg.db_keys[k] = k_str
        updated = True

    if args.scan_dump:
        dump_path = args.scan_dump.expanduser().resolve()
        if not dump_path.is_file():
            raise UsageError(f"Dump file not found: {dump_path}")
        acc = discover_wechat_account(cfg)
        if acc is None:
            raise EnvOrPermissionError("Cannot scan dump: no WeChat account directory found.")
        all_dbs = list(acc.message_dbs)
        if acc.contact_db:
            all_dbs.append(acc.contact_db)
        if acc.session_db and acc.session_db not in all_dbs:
            all_dbs.append(acc.session_db)
        matched_map = extract_keys_from_memory_dump(dump_path.read_bytes(), all_dbs)
        for db_n, hex_k in matched_map.items():
            cfg.db_keys[db_n] = hex_k
        updated = True

    if updated:
        cfg.save(args.config)

    snap_mgr = SnapshotManager(cfg)
    status = snap_mgr.check_source_access()
    return format_json_output(
        {
            "status": "updated" if updated else "checked",
            "has_global_key": bool(cfg.raw_key_hex),
            "db_keys_count": len(cfg.db_keys),
            "db_keys": {k: f"{v[:8]}...{v[-4:]}" for k, v in cfg.db_keys.items()},
            "verification": status,
        }
    )


def _handle_decrypt(args: argparse.Namespace) -> str:
    cfg = AppConfig.load(args.config)
    if args.src:
        src_p = args.src.expanduser().resolve()
        dst_p = (
            args.dst.expanduser().resolve()
            if args.dst
            else src_p.with_name(f"{src_p.stem}_decrypted.sqlite")
        )
        cands: list[str] = []
        if args.key:
            cands.append(args.key.strip())
        snap_mgr = SnapshotManager(cfg)
        cands.extend(snap_mgr._collect_candidate_keys_for_db(src_p))
        res = decrypt_sqlcipher_database(src_p, dst_p, cands)
        return format_json_output(res)

    snap_mgr = SnapshotManager(cfg)
    snap = snap_mgr.ensure_snapshot(force_fresh=True)
    return format_json_output(
        {
            "status": "unified_snapshot_ready",
            "snapshot_db_path": str(snap.snapshot_db_path),
            "account_wxid": snap.wxid,
            "arch_version": snap.arch_version,
            "global_latest_msg_id": snap.global_latest_msg_id,
            "backup_duration_ms": snap.backup_duration_ms,
        }
    )


def _handle_config(args: argparse.Namespace) -> str:
    action = args.config_action or "show"
    cfg_path = getattr(args, "config", None)
    cfg = AppConfig.load(cfg_path)

    if action == "show":
        return format_json_output(cfg.to_dict())

    if action == "set-identity":
        cfg.identity.name = args.name.strip() or "我"
        if args.wxid:
            cfg.identity.wxid = args.wxid.strip()
        if args.alias:
            cfg.identity.alias = args.alias.strip()
        saved = cfg.save(cfg_path)
        return format_json_output(
            {"status": "updated", "config_path": str(saved), "identity": cfg.to_dict()["identity"]}
        )

    if action == "set-mode":
        cfg.filter_mode = args.mode
        saved = cfg.save(cfg_path)
        return format_json_output(
            {"status": "updated", "config_path": str(saved), "filter_mode": cfg.filter_mode}
        )

    chats = _split_csv_list(getattr(args, "chats", []))
    for c in chats:
        if not AppConfig.is_group_chat_id(c):
            raise UsageError(f"Invalid WeChat group chat_id '{c}': group IDs must end with '@chatroom'.")

    if action == "block":
        for c in chats:
            if c not in cfg.blacklist_chats:
                cfg.blacklist_chats.append(c)
    elif action == "unblock":
        cfg.blacklist_chats = [x for x in cfg.blacklist_chats if x not in chats]
    elif action == "allow":
        for c in chats:
            if c not in cfg.whitelist_chats:
                cfg.whitelist_chats.append(c)
    elif action == "disallow":
        cfg.whitelist_chats = [x for x in cfg.whitelist_chats if x not in chats]
    else:
        raise UsageError(f"Unknown config action: {action}")

    saved = cfg.save(cfg_path)
    return format_json_output(
        {
            "status": "updated",
            "action": action,
            "config_path": str(saved),
            "filter_mode": cfg.filter_mode,
            "blacklist_chats": cfg.blacklist_chats,
            "whitelist_chats": cfg.whitelist_chats,
        }
    )


def _handle_install(args: argparse.Namespace) -> str:
    import os
    import shutil

    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parent.parent
    bin_script = repo_root / "bin" / "wechat-localmind"
    if not bin_script.exists():
        bin_script = pkg_dir.parent / "wechat-localmind"

    bin_dir = args.bin_dir.expanduser().resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    target_bin = bin_dir / "wechat-localmind"
    if target_bin.is_symlink() or target_bin.exists():
        target_bin.unlink()
    os.symlink(str(bin_script), str(target_bin))

    skill_installed_to: str | None = None
    if args.skill_dir:
        src_skill_dir = repo_root / "skills" / "wechat-localmind"
        dst_skill_dir = args.skill_dir.expanduser().resolve() / "wechat-localmind"
        if dst_skill_dir.exists():
            shutil.rmtree(dst_skill_dir)
        dst_skill_dir.mkdir(parents=True, exist_ok=True)
        if (src_skill_dir / "SKILL.md").exists():
            shutil.copy2(src_skill_dir / "SKILL.md", dst_skill_dir / "SKILL.md")
        dst_scripts = dst_skill_dir / "scripts"
        dst_scripts.mkdir(parents=True, exist_ok=True)
        if (src_skill_dir / "scripts" / "wechat-localmind").exists():
            shutil.copy2(src_skill_dir / "scripts" / "wechat-localmind", dst_scripts / "wechat-localmind")
            (dst_scripts / "wechat-localmind").chmod(0o755)
        shutil.copytree(
            pkg_dir,
            dst_scripts / "wx_localmind",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        skill_installed_to = str(dst_skill_dir)

    return format_json_output(
        {
            "status": "installed",
            "cli_symlink": str(target_bin),
            "cli_target": str(bin_script),
            "skill_bundle_dir": skill_installed_to,
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if not args.subcommand:
            parser.print_help(sys.stderr)
            return EXIT_USAGE_ERROR

        if args.subcommand == "mcp-serve":
            return run_mcp_stdio_server(args.config)

        if args.subcommand == "install":
            sys.stdout.write(_handle_install(args))
            return EXIT_SUCCESS

        if args.subcommand == "doctor":
            sys.stdout.write(_handle_doctor(args))
            return EXIT_SUCCESS

        if args.subcommand == "key":
            sys.stdout.write(_handle_key(args))
            return EXIT_SUCCESS

        if args.subcommand == "decrypt":
            sys.stdout.write(_handle_decrypt(args))
            return EXIT_SUCCESS

        if args.subcommand == "config":
            sys.stdout.write(_handle_config(args))
            return EXIT_SUCCESS

        cfg = AppConfig.load(args.config)
        snap_mgr = SnapshotManager(cfg)

        if args.subcommand == "clean":
            res = snap_mgr.clean(media_only=bool(args.media_only and not args.all))
            sys.stdout.write(format_json_output(res))
            return EXIT_SUCCESS

        snap = snap_mgr.ensure_snapshot(force_fresh=args.fresh)

        if args.subcommand in ("groups", "list-groups"):
            with QueryEngine(cfg, snap) as engine:
                res = engine.list_groups(
                    keyword=args.keyword,
                    include_blocked=args.include_blocked,
                )
            if args.format in ("md", "markdown"):
                out = format_groups_markdown(res)
            else:
                out = format_json_output(res)
            sys.stdout.write(out)
            return EXIT_SUCCESS

        if args.subcommand == "fetch":
            chat_ids = _split_csv_list(args.chats)
            with QueryEngine(cfg, snap) as engine:
                res = engine.fetch_messages(
                    chat_ids=chat_ids,
                    after_msg_id=args.after_id,
                    since_str=args.since,
                    until_str=args.until,
                    context_buffer_count=args.context_buffer,
                    limit_per_chat=args.limit,
                )
            if args.format == "json":
                out = format_json_output(res)
            else:
                fold = (
                    cfg.cleaning.fold_noise_markdown
                    if args.fold_override is None
                    else bool(args.fold_override)
                )
                out = format_fetch_markdown(
                    res,
                    fold_noise=fold,
                    long_audio_threshold_sec=cfg.cleaning.long_audio_threshold_sec,
                )

            if args.output:
                out_path = args.output.expanduser().resolve()
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(out, encoding="utf-8")
            sys.stdout.write(out)
            return EXIT_SUCCESS

        if args.subcommand == "search":
            chat_ids = _split_csv_list(args.chats) if args.chats else None
            with QueryEngine(cfg, snap) as engine:
                res = engine.search_messages(
                    query=args.query,
                    chat_ids=chat_ids,
                    since_str=args.since,
                    context_window=args.context,
                    limit=args.limit,
                )
            if args.format == "json":
                out = format_json_output(res)
            else:
                out = format_search_markdown(
                    res,
                    long_audio_threshold_sec=cfg.cleaning.long_audio_threshold_sec,
                )
            sys.stdout.write(out)
            return EXIT_SUCCESS

        if args.subcommand == "export-media":
            raw_ids = _split_csv_list(args.msg_ids)
            try:
                msg_ids = [int(x) for x in raw_ids]
            except ValueError as exc:
                raise UsageError(f"--msg-ids must be integers, got: {raw_ids}") from exc

            exporter = MediaExporter(cfg, snap)
            res = exporter.export_media_by_msg_ids(msg_ids, dest_dir=args.dest_dir)
            sys.stdout.write(format_json_output(res))
            return EXIT_SUCCESS

        raise UsageError(f"Unknown subcommand: {args.subcommand}")

    except WXLocalMindError as exc:
        err_obj = {
            "error": True,
            "error_code": exc.error_code,
            "exit_code": exc.exit_code,
            "message": exc.message,
        }
        sys.stderr.write(json.dumps(err_obj, ensure_ascii=False) + "\n")
        return exc.exit_code
    except Exception as exc:
        err_obj = {
            "error": True,
            "error_code": "UNEXPECTED_INTERNAL_ERROR",
            "exit_code": EXIT_INTERNAL_DB_ERROR,
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_obj, ensure_ascii=False) + "\n")
        return EXIT_INTERNAL_DB_ERROR


if __name__ == "__main__":
    sys.exit(main())
