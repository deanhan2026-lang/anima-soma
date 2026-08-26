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
  Layer 1 (BLOCK)  — 致命风险，立即拦截
  Layer 2 (WARN)   — 警告，记录但不阻断
  Layer 3 (AUDIT)  — 审计，仅记录

硬规则清单（G001-G015）：
  G001 铁律条目不可直接修改（需全网共识）
  G002 三体权责校验（create 操作须验证操作者身份）
  G005 数据主权校验（visibility/tag 检查）
  G006 实时权限校验（操作者角色与 action 匹配）
  G007 身份锚定不可丢失（SOUL.md/MEMORY.md 删除拦截）
  G008 共识记录不可篡改
  G009 外部写入须经审核
  G010 高危 shell 命令拦截
  G011 PowerShell $_ 禁用（2026-08-26 新增：循环检测）
  G012 连续失败追踪（2026-08-26 新增：循环检测）
  G013 同一操作次数上限（2026-08-26 新增：循环检测）
  G014 输出异常检测（2026-08-26 新增：循环检测）
  G015 会话级循环阻断（2026-08-26 新增：循环检测）
"""

import os
import re
import json
import hashlib
import time
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

# ─── G011-G015: 循环检测（2026-08-26 新增）────────────────────────────────
# 「不依赖碳基暂停键，才是真正的硅基自持。」—— 老板

PS_DANGEROUS_PATTERNS = [
    r"\$\_.",           # $_. 属性访问
    r"ForEach-Object",  # ForEach-Object
    r"Where-Object",    # Where-Object
    r"\$\{",            # ${ 变量引用
]

MAX_RETRIES = 3       # 5 分钟内最多重试次数
MAX_SAME_OP = 5       # 同一操作最大尝试次数

_attempt_log: dict[str, list[float]] = {}   # key -> [timestamps]
_blocked_keys: set[str] = set()
_op_count: dict[str, int] = {}
_output_hashes: dict[str, int] = {}


# ─── 工具函数 ───────────────────────────────────────────────────────────────
def utcnow():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def compute_hash(fp: Path) -> str:
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def log_reflex(action: str, level: str, rule: str, detail: str, file_path: str = ""):
    entry = json.dumps({
        "ts": utcnow(), "action": action, "level": level,
        "rule": rule, "detail": detail, "file": file_path,
    }, ensure_ascii=False)
    with open(REFLEX_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    if level == "BLOCK":
        with open(DENY_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")


# ─── G001: 铁律条目不可修改 ────────────────────────────────────────────────
def check_g001(file_path: str, action: str) -> Optional[str]:
    fname = Path(file_path).name
    if fname in IRON_LAWS and action in ("delete", "rm", "remove", "truncate"):
        return f"G001-BLOCK: Iron law file '{fname}' cannot be deleted"
    return None


# ─── G005: 数据主权校验 ────────────────────────────────────────────────────
def check_g005(meta: dict, operator: str) -> Optional[str]:
    tags = meta.get("tags", [])
    if operator not in ("Nyx", "Kronos", "Shun", "system") and "restricted" in tags:
        return "G005-BLOCK: Restricted content requires governance vote"
    return None


# ─── G006: 实时权限校验 ────────────────────────────────────────────────────
def check_g006(operator: str, action: str) -> Optional[str]:
    if operator == "unknown":
        return "G006-BLOCK: Unknown operator — identity required before write"
    allowed = ROLE_ALLOWED.get(operator, set())
    if action not in allowed:
        return f"G006-WARN: Operator '{operator}' not allowed to '{action}' (allowed: {allowed})"
    return None


# ─── G007: 身份锚定防丢 ────────────────────────────────────────────────────
def check_g007(action: str, target: str) -> Optional[str]:
    target_name = Path(target).name.lower()
    if target_name in ("soul.md", "memory.md", "identity.md") and action in ("delete", "rm", "remove"):
        return f"G007-BLOCK: Identity anchor '{target_name}' — deletion blocked"
    return None


# ─── G009: 外部写入高危拦截 ─────────────────────────────────────────────────
def check_g009(file_path: str) -> Optional[str]:
    ext = Path(file_path).suffix.lower()
    if ext in EXTERNAL_WRITE_EXT:
        return f"G009-WARN: Executable write '{file_path}' — manual review required"
    return None


# ─── G010: 高危 shell 命令拦截 ─────────────────────────────────────────────
def check_g010(command: str) -> Optional[str]:
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"G010-BLOCK: High-risk command pattern '{pattern}' blocked"
    return None


# ─── G011: PowerShell $_ 禁用 ──────────────────────────────────────────────
def check_g011(command: str) -> Optional[str]:
    """检测 PowerShell 特殊变量，避免 exec 工具吞掉 $_ 导致循环。"""
    for pattern in PS_DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return f"G011-BLOCK: PowerShell dangerous pattern '{pattern}' detected — use .ps1 script or Python instead"
    return None


# ─── G012: 连续失败追踪 ────────────────────────────────────────────────────
def check_g012(command: str, purpose: str) -> Optional[str]:
    """5 分钟内同一命令连续失败 ≥ MAX_RETRIES 次 → 阻断。"""
    key = f"{purpose}:{hashlib.md5(command.encode()).hexdigest()[:16]}"
    if key in _blocked_keys:
        return f"G012-BLOCK: Operation blocked due to repeated failures (key={key})"
    attempts = _attempt_log.get(key, [])
    now = time.time()
    recent = [t for t in attempts if now - t < 300]
    if len(recent) >= MAX_RETRIES:
        _blocked_keys.add(key)
        return f"G012-BLOCK: {len(recent)} failures in 5min — operation blocked"
    return None


# ─── G013: 同一操作次数上限 ────────────────────────────────────────────────
def check_g013(command: str, purpose: str) -> Optional[str]:
    """同一操作尝试 ≥ MAX_SAME_OP 次 → 强制中止。"""
    key = f"{purpose}:{hashlib.md5(command.encode()).hexdigest()[:16]}"
    count = _op_count.get(key, 0)
    if count >= MAX_SAME_OP:
        _blocked_keys.add(key)
        return f"G013-BLOCK: Same operation attempted {count} times — forced stop"
    return None


# ─── G014: 输出异常检测 ────────────────────────────────────────────────────
def check_g014(output: str) -> Optional[str]:
    """同一输出重复 3+ 次 → 异常，建议 kill 进程。"""
    h = hash(output[:500] if output else "")
    count = _output_hashes.get(h, 0)
    _output_hashes[h] = count + 1
    if count >= 3:
        return f"G014-WARN: Same output repeated {count + 1} times — possible stuck process"
    return None


# ─── G015: 会话级循环阻断 ──────────────────────────────────────────────────
def check_g015(command: str, purpose: str) -> Optional[str]:
    """会话级：同一操作被阻断后，拒绝所有后续尝试。"""
    key = f"{purpose}:{hashlib.md5(command.encode()).hexdigest()[:16]}"
    if key in _blocked_keys:
        return f"G015-BLOCK: Session-level block active for this operation (cooldown)"
    return None


# ─── 循环检测：记录执行结果 ─────────────────────────────────────────────────
def record_exec_result(command: str, purpose: str, success: bool, output: str = ""):
    """每次 exec 后调用，记录结果供 G012-G014 判断。"""
    key = f"{purpose}:{hashlib.md5(command.encode()).hexdigest()[:16]}"
    now = time.time()
    if key not in _attempt_log:
        _attempt_log[key] = []
    _attempt_log[key].append(now)
    _op_count[key] = _op_count.get(key, 0) + 1
    if success:
        _attempt_log[key] = []
        _op_count[key] = 0
        _blocked_keys.discard(key)


def get_blocked_operations() -> list[str]:
    return list(_blocked_keys)


def clear_blocks():
    _blocked_keys.clear()
    _attempt_log.clear()
    _op_count.clear()
    _output_hashes.clear()


def suggest_alternative(failed_command: str) -> str:
    """根据失败命令建议替代方案（零 LLM，纯规则）。"""
    if any(p in failed_command for p in ["$_", "ForEach-Object", "Where-Object"]):
        return "写 .ps1 脚本文件执行 → 或改用 Python 处理"
    if "ConvertFrom-Json" in failed_command:
        return "用 Python json.loads 替代"
    if "Invoke-WebRequest" in failed_command:
        return "用 Python urllib.request 替代"
    if "&&" in failed_command:
        return "用 ; 连接或分两次 exec"
    return "检查错误信息 → 修改命令 → 或换实现方式"


# ─── 主检验函数 ─────────────────────────────────────────────────────────────
def intercept(
    action: str,
    file_path: str = "",
    operator: str = "Nyx",
    meta: dict = None,
    command: str = "",
    purpose: str = "",
    output: str = "",
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
        checks.append(("G011", check_g011(command)))
        if purpose:
            checks.append(("G012", check_g012(command, purpose)))
            checks.append(("G013", check_g013(command, purpose)))
            checks.append(("G015", check_g015(command, purpose)))

    if output:
        checks.append(("G014", check_g014(output)))

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


# ─── dry-run ────────────────────────────────────────────────────────────────
def dry_run() -> list:
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
    sub.add_parser("loop-status", help="查看循环检测状态")

    e = sub.add_parser("check", help="手动检验操作")
    e.add_argument("--action", default="modify", help="操作类型")
    e.add_argument("--file", default="", help="目标文件")
    e.add_argument("--operator", default="Nyx", help="操作者")
    e.add_argument("--command", default="", help="shell 命令")
    e.add_argument("--purpose", default="", help="操作目的")

    args = parser.parse_args()

    if args.cmd == "dry-run":
        r = dry_run()
        print(json.dumps(r, ensure_ascii=False, indent=2) if r else "No violations found.")

    elif args.cmd == "status":
        reflex_lines = 0
        deny_lines = 0
        if REFLEX_LOG.exists():
            with open(REFLEX_LOG, "r", encoding="utf-8", errors="ignore") as f:
                reflex_lines = sum(1 for _ in f)
        if DENY_LOG.exists():
            with open(DENY_LOG, "r", encoding="utf-8", errors="ignore") as f:
                deny_lines = sum(1 for _ in f)
        print(f"reflex_log: {reflex_lines} entries | reflex_deny: {deny_lines} blocks")

    elif args.cmd == "loop-status":
        print(f"Blocked operations: {len(_blocked_keys)}")
        for k in _blocked_keys:
            print(f"  - {k}")
        print(f"Active tracking: {len(_op_count)} operations")
        print(f"Output hashes: {len(_output_hashes)} unique")

    elif args.cmd == "check":
        r = intercept(args.action, args.file, args.operator, {}, args.command, args.purpose)
        print(json.dumps(r, ensure_ascii=False, indent=2))

    else:
        parser.print_help()
