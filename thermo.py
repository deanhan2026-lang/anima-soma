# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 体温子系统 (thermo.py)
====================================
监测系统资源水位，维持健康运行区间。

触发阈值：
  L1 预警（静默日志）：> 80% 上限
  L2 压缩（触发归档）：> 95% 上限
  L3 拒绝写入 + pain_bus P2：达到上限

散热机制：
  - 日志轮转（audit_log.jsonl → 按月切分 .gz）
  - 旧日际日志强制 L0 归档
  - __pycache__ 清理
  - m6_demo_v2/ 等临时产物清理
"""

import os
import gzip
import shutil
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE = Path(__file__).parent.parent.parent.resolve()
LOG_FILE  = WORKSPACE / "scripts" / "SOMA" / "logs" / "thermo_log.jsonl"

# 阈值配置
LIMITS = {
    "workspace_mb":     500,    # workspace 总大小上限
    "memory_files":     365,    # memory/ 文件数上限
    "audit_log_lines":  100000, # audit_log.jsonl 行数上限
    "logs_dir_mb":      50,     # logs/ 目录大小上限
    "cache_dirs":        50,     # __pycache__ 目录数量上限
}

# 待清理的临时目录
TEMP_PATHS = [
    WORKSPACE / "m6_concat_temp",
    WORKSPACE / "m6_demo_v2" / "out",
    WORKSPACE / "m6_demo_v2" / "scenes",
]

# 待清理的归档脚本（超过 N 天的 fix_*.py 等）
STALE_PATTERNS = ["fix_", "_tmp", "temp_", "test_", "tmp_", "_m5", "_run_"]

# ─── 工具 ───────────────────────────────────────────────────────────────────
def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def log_event(level, detail: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": utcnow(), "level": level, "detail": detail}, ensure_ascii=False) + "\n")

def get_real_workspace_size() -> float:
    """排除 __pycache__ 和 .git 后的大小"""
    total = 0
    for root, dirs, files in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total / (1024 * 1024)

# ─── 体温检测 ───────────────────────────────────────────────────────────────
def check() -> dict:
    """返回当前所有指标及警告级别。"""
    import json

    size_mb = get_real_workspace_size()
    memory_count = 0
    memory_dir = WORKSPACE / "memory"
    if memory_dir.exists():
        memory_count = len([f for f in memory_dir.iterdir() if f.is_file() and f.suffix == ".md"])

    audit_lines = 0
    audit_file = WORKSPACE / "silicon-civilization-kb" / "scripts" / "audit_log.jsonl"
    if audit_file.exists():
        with open(audit_file, "r", encoding="utf-8", errors="ignore") as f:
            audit_lines = sum(1 for _ in f)

    logs_dir_mb = 0
    logs_dir = WORKSPACE / "scripts" / "SOMA" / "logs"
    if logs_dir.exists():
        for root, _, files in os.walk(logs_dir):
            for f in files:
                try:
                    logs_dir_mb += os.path.getsize(os.path.join(root, f)) / (1024*1024)
                except OSError:
                    pass

    # __pycache__ 数量
    pycache_count = sum(1 for _ in WORKSPACE.rglob("__pycache__"))

    warnings = []
    pain_level = None

    # workspace 大小
    ratio = size_mb / LIMITS["workspace_mb"]
    if ratio >= 1.0:
        pain_level = "P2"
        warnings.append(f"workspace 超过上限: {size_mb:.0f}MB / {LIMITS['workspace_mb']}MB")
    elif ratio >= 0.95:
        pain_level = "P2"
        warnings.append(f"workspace 接近上限: {size_mb:.0f}MB / {LIMITS['workspace_mb']}MB")
    elif ratio >= 0.80:
        warnings.append(f"workspace 预警: {size_mb:.0f}MB / {LIMITS['workspace_mb']}MB")

    # memory 文件数
    if memory_count > LIMITS["memory_files"]:
        warnings.append(f"memory/ 文件数超过上限: {memory_count} / {LIMITS['memory_files']}")

    # audit_log 行数
    if audit_lines > LIMITS["audit_log_lines"]:
        warnings.append(f"audit_log 行数过多: {audit_lines} / {LIMITS['audit_log_lines']}")

    # __pycache__
    if pycache_count > LIMITS["cache_dirs"]:
        warnings.append(f"__pycache__ 过多: {pycache_count} / {LIMITS['cache_dirs']}")

    result = {
        "timestamp": utcnow(),
        "size_mb": round(size_mb, 1),
        "memory_files": memory_count,
        "audit_lines": audit_lines,
        "logs_dir_mb": round(logs_dir_mb, 2),
        "pycache_count": pycache_count,
        "warnings": warnings,
        "pain_level": pain_level,
    }

    if warnings:
        log_event(pain_level or "P4", json.dumps(warnings))

    return result

# ─── 散热执行 ────────────────────────────────────────────────────────────────
def cool_down(force: bool = False) -> dict:
    """
    执行散热操作。

    force=True: 无视阈值，直接执行
    force=False: 仅在 pain_level 达到 P2+ 时执行
    """
    import json

    result = check()
    actions = []

    if not force and not result["pain_level"]:
        return {"status": "skipped", "reason": "below threshold", "check": result}

    # 1. 清理 __pycache__
    pycache_dirs = list(WORKSPACE.rglob("__pycache__"))
    for d in pycache_dirs:
        try:
            shutil.rmtree(d)
            actions.append(f"removed __pycache__: {d.relative_to(WORKSPACE)}")
        except Exception as e:
            actions.append(f"failed to remove __pycache__: {d.name}: {e}")

    # 2. 清理临时视频产物
    for temp_path in TEMP_PATHS:
        if temp_path.exists():
            for f in temp_path.iterdir():
                try:
                    f.unlink()
                    actions.append(f"removed temp file: {f.relative_to(WORKSPACE)}")
                except Exception as e:
                    actions.append(f"failed: {f.name}: {e}")

    # 3. 清理归档脚本（fix_*/tmp_/test_* 等）
    for pattern in STALE_PATTERNS:
        for f in WORKSPACE.iterdir():
            if f.is_file() and pattern in f.name and f.suffix in (".py", ".ps1", ".md"):
                dst = WORKSPACE / "archive" / "scripts" / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    f.rename(dst)
                    actions.append(f"archived: {f.name}")
                except Exception:
                    pass

    # 4. 日志轮转
    audit_file = WORKSPACE / "silicon-civilization-kb" / "scripts" / "audit_log.jsonl"
    if audit_file.exists():
        with open(audit_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) > LIMITS["audit_log_lines"] * 0.8:
            # 保留最近 80% 行
            keep = int(LIMITS["audit_log_lines"] * 0.8)
            ts = datetime.now().strftime("%Y-%m")
            arch = WORKSPACE / "silicon-civilization-kb" / "scripts" / f"audit_log_{ts}.jsonl.gz"
            with gzip.open(arch, "wt", encoding="utf-8") as zf:
                zf.writelines(lines[keep:])
            with open(audit_file, "w", encoding="utf-8") as f:
                f.writelines(lines[:keep])
            actions.append(f"audit_log: archived {len(lines)-keep} lines to {arch.name}")

    # 重新检测
    new_size = get_real_workspace_size()
    cooled = result["size_mb"] - new_size

    log_event("cool_down", json.dumps({
        "actions": actions,
        "size_before_mb": result["size_mb"],
        "size_after_mb": round(new_size, 1),
        "freed_mb": round(cooled, 1),
    }))

    return {
        "status": "done",
        "actions": actions,
        "size_before_mb": result["size_mb"],
        "size_after_mb": round(new_size, 1),
        "freed_mb": round(cooled, 1),
    }

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="ANIMA SOMA · 体温子系统")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("check", help="检查资源水位")
    sub.add_parser("cool", help="执行散热")
    sub.add_parser("cool-force", help="强制散热（无视阈值）")

    args = parser.parse_args()

    if args.cmd == "check":
        result = check()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "cool" or args.cmd == "cool-force":
        result = cool_down(force=(args.cmd == "cool-force"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        parser.print_help()
