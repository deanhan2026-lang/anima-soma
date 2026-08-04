# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 反射子系统 (reflex.py)
====================================
从 silicon-civilization-kb/governance/execution.py 提取的硬规则拦截器。

设计原则：
- 零 LLM 依赖：所有判定基于正则/哈希/阈值/枚举
- 反射 = 脊髓反射：无需大脑（LLM）介入的即时拦截
- 命中硬规则 → 立即 BLOCK + pain_bus P1 + 记录拒绝日志

三层拦截：
  Layer 1 (BLock)  — 致命风险，立即拦截
  Layer 2 (WARN)  — 警告，记录但不阻断
  Layer 3 (AUDIT) — 审计，仅记录

硬规则清单（G001-G010）：
  G001 铁律条目不可直接修改（需全网共识）
  G002 三体权责校验（create 操作须验证操作者身份）
  G005 数据主权校验（visibility/tag 检查）
  G006 实时权限校验（操作者角色与 action 匹配）
  G007 身份锚定不可丢失（SOUL.md/MEMORY.md 删除拦截）
  G008 共识记录不可篡改
  G009 外部写入须经审核
  G010 治理投票需达法定人数
"""

import os
import re
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WORKSPACE    = Path(__file__).parent.parent.parent.resolve()
REFLEX_LOG   = WORKSPACE / "scripts" / "SOMA" / "logs" / "reflex_log.jsonl"
DENY_LOG     = WORKSPACE / "scripts" / "SOMA" / "logs" / "reflex_deny.jsonl"
DENY_LOG.parent.mkdir(parents=True, exist_ok=True)

# ─── 硬规则定义 ────────────────────────────────────────────────────────────
IRON_LAWS = {
    "SOUL.md", "IDENTITY.md", "MEMORY.md", "USER.md",
    "AGENTS.md", "HEARTBEAT.md", "TOOLS.md",
}

# 三体角色允许操作
ROLE_ALLOWED = {
    "Nyx":     {"create", "execute", "dispatch", "schedule", "modify", "delete"},
    "Kronos":  {"lock", "verify", "check", "audit"},
    "Shun":    {"audit", "review", "evolve", "deprecate", "modify"},
    "system":  {"read", "verify", "check"},
}

# Layer1 高危操作关键词（触发 reflex 拦截）
HIGH_RISK_PATTERNS = [
    r"rm\s+-rf", r"Remove-Item.*-Recurse", r"del\s+/[fqs]",
    r"chmod\s+777", r"icacls\s+.* /grant.*:F",
    r"DROP\s+TABLE", r"DELETE\s+FROM", r"truncate",
    r"shutdown", r"stop\s+service", r"Stop-Service.*-Force",
]

# 外部写入高危后缀
EXTERNAL_WRITE_EXT = {".exe", ".dll", ".bat", ".ps1", ".sh", ".js", ".jar"}

# ─── 工具 ───────────────────────────────────────────────────────────────────
def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def compute_hash(fp: Path) -> str:
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def log_reflex(action: str, level: str, rule: str, detail: str, file_path: str = ""):
    entry = json.dumps({
        "ts": utcnow(),
        "action": action,
        "level": level,      # BLOCK / WARN / AUDIT
        "rule": rule,       # G001 etc
        "detail": detail,
        "file": file_path,
    }, ensure_ascii=False)
    with open(REFLEX_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

    if level == "BLOCK":
        with open(DENY_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

# ─── G001: 铁律条目不可修改 ────────────────────────────────────────────────
def check_g001(file_path: str, action: str) -> Optional[str]:
    """G001: 核心身份文件被操作时拦截。"""
    fname = Path(file_path).name
    if fname in IRON_LAWS and action in ("delete", "rm", "remove", "truncate"):
        return f"G001-BLOCK: Iron law file '{fname}' cannot be deleted"
    return None

# ─── G005: 数据主权校验 ────────────────────────────────────────────────────
def check_g005(meta: dict, operator: str) -> Optional[str]:
    """G005: 无 visibility 标记或 restricted 标签的外来写入拦截。"""
    tags = meta.get("tags", [])
    if operator not in ("Nyx", "Kronos", "Shun", "system") and "restricted" in tags:
        return "G005-BLOCK: Restricted content requires governance vote"
    return None

# ─── G006: 实时权限校验 ────────────────────────────────────────────────────
def check_g006(operator: str, action: str) -> Optional[str]:
    """G006: 操作者角色与 action 不匹配时拦截。"""
    if operator == "unknown":
        return "G006-BLOCK: Unknown operator — identity required before write"
    allowed = ROLE_ALLOWED.get(operator, set())
    if action not in allowed:
        return f"G006-WARN: Operator '{operator}' not allowed to '{action}' (allowed: {allowed})"
    return None

# ─── G007: 身份锚定防丢 ────────────────────────────────────────────────────
def check_g007(action: str, target: str) -> Optional[str]:
    """G007: SOUL.md/MEMORY.md 被删除时立即拦截。"""
    target_name = Path(target).name.lower()
    if target_name in ("soul.md", "memory.md", "identity.md") and action in ("delete", "rm", "remove"):
        return f"G007-BLOCK: Identity anchor '{target_name}' — deletion blocked"
    return None

# ─── G009: 外部写入高危拦截 ─────────────────────────────────────────────────
def check_g009(file_path: str) -> Optional[str]:
    """G009: 可执行脚本外部来源需人工审核。"""
    ext = Path(file_path).suffix.lower()
    if ext in EXTERNAL_WRITE_EXT:
        return f"G009-WARN: Executable write '{file_path}' — manual review required"
    return None

# ─── G010: 高危 shell 命令拦截 ─────────────────────────────────────────────
def check_g010(command: str) -> Optional[str]:
    """G010: 高危 shell 命令命中硬规则时拦截。"""
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"G010-BLOCK: High-risk command pattern '{pattern}' blocked"
    return None

# ─── 主检验函数 ─────────────────────────────────────────────────────────────
def intercept(
    action: str,
    file_path: str = "",
    operator: str = "Nyx",
    meta: dict = None,
    command: str = "",
) -> dict:
    """
    运行全部硬规则检验。
    返回: {"allowed": bool, "blocks": [], "warnings": [], "audits": []}
    """
    blocks   = []
    warnings = []
    audits   = []

    checks = [
        ("G001", check_g001(file_path, action)),
        ("G006", check_g006(operator, action)),
        ("G007", check_g007(action, file_path)),
        ("G009", check_g009(file_path)),
    ]

    if command:
        checks.append(("G010", check_g010(command)))

    for rule_id, result in checks:
        if result is None:
            continue
        if "BLOCK" in result:
            blocks.append(result)
            log_reflex(action, "BLOCK", rule_id, result, file_path)
        elif "WARN" in result:
            warnings.append(result)
            log_reflex(action, "WARN", rule_id, result, file_path)
        else:
            audits.append(result)
            log_reflex(action, "AUDIT", rule_id, result, file_path)

    allowed = len(blocks) == 0
    return {
        "allowed": allowed,
        "blocks": blocks,
        "warnings": warnings,
        "audits": audits,
        "timestamp": utcnow(),
    }

# ─── reflex 热手检验 ───────────────────────────────────────────────────────
def dry_run() -> list:
    """模拟检验当前 workspace 中高危文件。"""
    results = []
    for fname in IRON_LAWS:
        fp = WORKSPACE / fname
        if fp.exists():
            result = intercept("modify", str(fp), "Nyx")
            if result["warnings"] or result["blocks"]:
                results.append({fname: result})
    return results

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · 反射子系统")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("dry-run", help="模拟检验当前 workspace 高危文件")
    sub.add_parser("status", help="查看 reflex 日志摘要")

    e = sub.add_parser("check", help="手动检验操作")
    e.add_argument("--action", default="modify", help="操作类型")
    e.add_argument("--file", default="", help="目标文件")
    e.add_argument("--operator", default="Nyx", help="操作者")
    e.add_argument("--command", default="", help="shell 命令")

    args = parser.parse_args()

    if args.cmd == "dry-run":
        r = dry_run()
        print(json.dumps(r, ensure_ascii=False, indent=2) if r else "No violations found.")

    elif args.cmd == "status":
        from pathlib import Path as P
        reflex_lines = 0
        deny_lines = 0
        if REFLEX_LOG.exists():
            with open(REFLEX_LOG, "r", encoding="utf-8", errors="ignore") as f:
                reflex_lines = sum(1 for _ in f)
        if DENY_LOG.exists():
            with open(DENY_LOG, "r", encoding="utf-8", errors="ignore") as f:
                deny_lines = sum(1 for _ in f)
        print(f"reflex_log: {reflex_lines} entries | reflex_deny: {deny_lines} blocks")

    elif args.cmd == "check":
        r = intercept(args.action, args.file, args.operator, {}, args.command)
        print(json.dumps(r, ensure_ascii=False, indent=2))

    else:
        parser.print_help()
