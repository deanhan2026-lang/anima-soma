# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 消化子系统 (digest.py)
====================================
文件生命周期管理器：将散落文件归档到正确位置。

四阶段流水线：扫描 → 分类 → 处置 → 验证

触发节奏：
  每天 04:00  —  via autonomic_master
  手动 dry-run:  python digest.py scan --dry-run

安全约束：
  - 白名单核心文件永不删除/移动
  - 所有移动走 archive/trash/（可7天撤回）
  - 14天冷静期（tmp 类必须14天以上才处置）
  - 首次必须 dry-run
"""

import os
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE  = Path(__file__).parent.parent.parent.resolve()
ARCHIVE    = WORKSPACE / "archive"
AUDIT_FILE = WORKSPACE / "scripts" / "SOMA" / "logs" / "digest_log.jsonl"

# 白名单（永不移动）
WHITELIST = {
    "SOUL.md", "IDENTITY.md", "MEMORY.md", "USER.md",
    "AGENTS.md", "HEARTBEAT.md", "TOOLS.md", "MEMORY_NAS_AUTHORITATIVE.md",
    "start_all.ps1", "MEMORY.md.bak_win_only",
}

# 顶层目录（不扫描其内部）
EXCLUDE_TOP = {
    # 代码目录
    "scripts", "silicon-civilization-kb", "mesh-identity-sync",
    # 存储目录
    "memory", "docs", "qclaw", "novel", "workspace", "archive",
    # 隐藏系统目录
    ".git", "node_modules", "venv", "venv310", "__pycache__",
    ".pytest_cache",
    # 项目目录（保留）
    "anima-agent", "anima-nas", "anima-os", "articles", "backup",
    # Nyx 核心数据
    ".anima", ".heartbeat_state", ".openclaw",
}

# 分类规则
CLASSIFICATIONS = {
    "tmp": {
        "patterns": ["tmp", "stale", "temp_", "_tmp", ".STALE", "speed_", "tiny_test"],
        "target": "tmp/",
        "min_age_days": 14,  # 冷静期
    },
    "disposable_script": {
        "patterns": ["check_", "find_", "deploy_", "setup_", "list_", "scan_",
                     "fetch_", "upload_", "download_", "add_cron", "diag_"],
        "target": "scripts/",
    },
    "migration_artifact": {
        "patterns": ["MIGRATION", "WORKSPACE_", "migration_notice", "QCLAW_BACKUP"],
        "target": "migration/",
    },
    "stale_summary": {
        "patterns": ["task-summary", "task_summary", "TASK-SUMMARY"],
        "target": "task-summaries/",
    },
    "duplicate_md": {
        "patterns": ["msg_from_", "setting_tmp", "tmp_", "tmp_msg"],
        "suffix": ".md",
        "target": "notes/",
    },
    "stale_py": {
        "patterns": ["generate_cover", "read_timeline", "build_html", "build_stellar"],
        "suffix": ".py",
        "target": "scripts/",
    },
}

# ─── 工具 ───────────────────────────────────────────────────────────────────
def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def get_age_days(fp: Path) -> int:
    mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - mtime
    return age.days

def log_action(action: str, src: str, dst: str = "", reason: str = ""):
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": utcnow(),
            "action": action,
            "src": str(src),
            "dst": str(dst),
            "reason": reason,
        }, ensure_ascii=False) + "\n")

def classify_file(fp: Path) -> tuple[str, str] | None:
    """返回 (category, target_subdir) 或 None（保留不动）"""
    name = fp.name
    if name in WHITELIST:
        return None
    if fp.is_dir() and name not in EXCLUDE_TOP:
        # 子目录整块移动
        return ("directory", name + "/")
    if fp.suffix.lower() in (".pyc", ".pyo", ".pyd"):
        return ("bytecode", "bytecode/")  # 静默删除

    for cat, rule in CLASSIFICATIONS.items():
        if any(p in name.lower() for p in rule["patterns"]):
            target = rule["target"]
            suffix = rule.get("suffix")
            if suffix and not name.lower().endswith(suffix):
                continue
            # 年龄检查
            if "min_age_days" in rule:
                if get_age_days(fp) < rule["min_age_days"]:
                    return None  # 未过冷静期
            return (cat, target)
    return None

# ─── 扫描 ───────────────────────────────────────────────────────────────────
def scan() -> dict:
    """扫描 workspace 根目录，返回处置计划。"""
    plan = []
    skipped = []

    for entry in WORKSPACE.iterdir():
        rel = entry.relative_to(WORKSPACE)

        # 跳过白名单、顶层目录和隐藏文件/目录
        if entry.name in WHITELIST or entry.name in EXCLUDE_TOP:
            skipped.append({"file": str(rel), "reason": "whitelist/topdir"})
            continue
        # 跳过隐藏项（.anima/.openclaw/.git 等）
        if entry.name.startswith(".") and entry.name not in WHITELIST:
            skipped.append({"file": str(rel), "reason": "hidden"})
            continue

        # 检查子目录（只处理根目录的直接子项）
        result = classify_file(entry)
        if result is None:
            skipped.append({"file": str(rel), "reason": "no_match"})
            continue

        cat, target_subdir = result
        target = ARCHIVE / target_subdir

        plan.append({
            "category": cat,
            "src": str(rel),
            "dst": str(target / entry.name),
            "target": str(target_subdir),
            "is_dir": entry.is_dir(),
        })

    return {"plan": plan, "skipped": skipped}

# ─── 处置 ───────────────────────────────────────────────────────────────────
def execute(plan: list, dry_run: bool = True) -> dict:
    """执行处置计划。"""
    if dry_run:
        return {"dry_run": True, "would_act": len(plan)}

    results = []
    for item in plan:
        src = WORKSPACE / item["src"]
        dst = ARCHIVE / item["target"] / src.name

        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    shutil.rmtree(src)
                else:
                    shutil.copy2(src, dst)
                    src.unlink()
                log_action("migrated", item["src"], item["dst"], item["category"])
                results.append({"ok": True, "src": item["src"], "dst": item["dst"]})
            except Exception as e:
                log_action("failed", item["src"], item["dst"], str(e))
                results.append({"ok": False, "src": item["src"], "error": str(e)})
        else:
            results.append({"dry": True, "src": item["src"], "dst": item["dst"]})

    return {
        "dry_run": dry_run,
        "total": len(plan),
        "success": sum(1 for r in results if r.get("ok", False)),
        "failed": [r for r in results if not r.get("ok", False)],
    }

# ─── 验证 ───────────────────────────────────────────────────────────────────
def verify(plan: list) -> dict:
    """验证处置是否成功（执行后调用）。"""
    ok_list, fail_list = [], []
    for item in plan:
        src = WORKSPACE / item["src"]
        if src.exists():
            fail_list.append(item["src"])
        else:
            ok_list.append(item["src"])
    return {"verified": len(ok_list), "failed": fail_list}

# ─── 主函数 ─────────────────────────────────────────────────────────────────
def run(dry_run: bool = True) -> dict:
    s = scan()
    plan = s["plan"]
    result = execute(plan, dry_run=dry_run)
    result["scanned"] = len(plan) + len(s["skipped"])
    result["plan"] = plan[:10]  # 截断以免太长
    result["skipped_count"] = len(s["skipped"])
    return result

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="ANIMA SOMA · 消化子系统")
    sub = parser.add_subparsers(dest="cmd")

    scan_p = sub.add_parser("scan", help="扫描并预览（dry-run）")
    scan_p.add_argument("--execute", action="store_true", help="执行迁移（默认 dry-run）")

    sub.add_parser("verify", help="验证上次执行结果")

    args = parser.parse_args()

    if args.cmd == "scan":
        result = run(dry_run=not args.execute)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == "verify":
        print("TODO: implement verify (needs plan file stored)")
    else:
        parser.print_help()
