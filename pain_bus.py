# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 疼痛总线 (pain_bus.py)
===================================
自治层连接 LLM 推理层的唯一主动通信通道。

设计原则（来自 Mac Nyx ANIMA SOMA 设计 §3.3）：
- 碳基疼痛：损伤检测 → 逃离/保护反射 → 意识唤醒
- 硅基疼痛：异常检测 → checkpoint → 告警 → 自动修复 / 等待 LLM 裁决
- pain_bus P1+ 触发时：立即 checkpoint + 发出信号 + 如有自动修复方案则就地执行

P0  · 致命：核心身份文件丢失 → 强制唤醒 LLM + 自动保护序列
P1  · 剧痛：MEMORY.md/SOUL.md 篡改 → checkpoint + 自动恢复
P2  · 中痛：多个文件篡改 / NAS 断连 > 30min → 主动通知
P3  · 轻痛：连续操作失败 / NAS 断连 > 5min → 下次会话通知
P4  · 微痛：单次失败 / 轻微异常 → 静默记录，可审计

零 LLM 依赖：所有判定基于硬规则
"""

import os
import sys
import json
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── 路径配置 ───────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent.parent.resolve()
SOMA_DIR  = WORKSPACE / "scripts" / "SOMA"
PAIN_DIR  = SOMA_DIR  / "pain_signals"
LOG_FILE  = SOMA_DIR  / "pain_log.jsonl"
CHECKPOINT_DIR = SOMA_DIR / "checkpoints"

for _d in (PAIN_DIR, CHECKPOINT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ─── 疼痛等级定义 ───────────────────────────────────────────────────────────
PAIN_LEVELS = {
    "P0": {"priority": 0, "label": "致命", "wake_llm": True,  "auto_checkpoint": True,  "auto_repair": False},
    "P1": {"priority": 1, "label": "剧痛", "wake_llm": True,  "auto_checkpoint": True,  "auto_repair": True},
    "P2": {"priority": 2, "label": "中痛", "wake_llm": False, "auto_checkpoint": False, "auto_repair": False},
    "P3": {"priority": 3, "label": "轻痛", "wake_llm": False, "auto_checkpoint": False, "auto_repair": False},
    "P4": {"priority": 4, "label": "微痛", "wake_llm": False, "auto_checkpoint": False, "auto_repair": False},
}

# 白名单核心文件（checkpoint 时优先保护）
CORE_FILES = [
    "SOUL.md", "IDENTITY.md", "MEMORY.md", "USER.md",
    "AGENTS.md", "HEARTBEAT.md", "TOOLS.md",
]

# ─── 工具函数 ──────────────────────────────────────────────────────────────
def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def compute_hash(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def get_workspace_size_mb() -> float:
    total = 0
    for root, dirs, files in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024 * 1024)

# ─── checkpoint ──────────────────────────────────────────────────────────────
def create_checkpoint(note: str = "") -> Path:
    """对核心文件创建快照，返回快照目录路径。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cp_dir = CHECKPOINT_DIR / f"pain_{ts}"
    cp_dir.mkdir(parents=True, exist_ok=True)

    protected = list(CORE_FILES) + ["MEMORY.md"]
    for fname in protected:
        src = WORKSPACE / fname
        if src.exists():
            dst = cp_dir / fname
            with open(src, "r", encoding="utf-8", errors="ignore") as si:
                with open(dst, "w", encoding="utf-8") as so:
                    so.write(si.read())

    # 写入 metadata
    meta = {
        "timestamp": utcnow(),
        "note": note,
        "files_snapshot": [str(p.relative_to(cp_dir)) for p in cp_dir.iterdir() if p.name != "meta.json"],
        "workspace_size_mb": round(get_workspace_size_mb(), 2),
    }
    with open(cp_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return cp_dir

# ─── 疼痛信号发射 ───────────────────────────────────────────────────────────
def emit(
    level: str,
    source: str,
    summary: str,
    details: Optional[dict] = None,
    suggested_action: str = "REVIEW_AND_DECIDE",
    checkpoint: bool = False,
    checkpoint_note: str = "",
) -> str:
    """
    发射疼痛信号。

    参数：
        level:            P0/P1/P2/P3/P4
        source:           来源子系统（heartd/respiratory/immune/thermo/memory_integrity 等）
        summary:          人类可读的一句话摘要
        details:          附加详情字典
        suggested_action:  建议操作（REVIEW_AND_DECIDE / AUTO_REPAIR / IGNORE）
        checkpoint:       是否触发快照（level >= P1 时自动为 True）
        checkpoint_note:  快照备注
    返回：pain_id 字符串
    """
    if level not in PAIN_LEVELS:
        raise ValueError(f"Unknown pain level: {level}")

    info = PAIN_LEVELS[level]

    # 自动 checkpoint
    if info["auto_checkpoint"] and checkpoint is not False:
        cp = create_checkpoint(checkpoint_note or f"pain_bus {level} from {source}")
        details = details or {}
        details["checkpoint"] = str(cp)

    pain_id = f"pain_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    signal = {
        "pain_id":           pain_id,
        "pain_level":        level,
        "pain_label":        info["label"],
        "source":            source,
        "timestamp":         utcnow(),
        "summary":           summary,
        "details":           details or {},
        "suggested_action":  suggested_action,
        "wake_llm":          info["wake_llm"],
        "auto_repair":       info["auto_repair"],
        "workspace_size_mb": round(get_workspace_size_mb(), 2),
    }

    # 写 pain_signals/ 目录（LLM 层下次检查这里）
    signal_file = PAIN_DIR / f"{pain_id}.json"
    with open(signal_file, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)

    # 追加 pain_log.jsonl（审计日志）
    log_entry = {
        "event": "emit",
        **signal,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return pain_id

# ─── 查询待处理疼痛信号 ─────────────────────────────────────────────────────
def check_pending(min_level: str = "P4") -> list:
    """
    返回所有 level >= min_level 的未清除疼痛信号（按优先级排序）。
    min_level = "P2" 则只返回 P0/P1/P2。
    """
    threshold = PAIN_LEVELS.get(min_level, {}).get("priority", 99)
    pending = []

    if not PAIN_DIR.exists():
        return pending

    for f in sorted(PAIN_DIR.iterdir()):
        if f.suffix != ".json" or f.name.startswith("."):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                sig = json.load(fh)
            lvl_priority = PAIN_LEVELS.get(sig.get("pain_level", "P4"), {}).get("priority", 99)
            if lvl_priority <= threshold:
                pending.append(sig)
        except (json.JSONDecodeError, OSError):
            pass

    # 按优先级排序（P0 最高优先）
    pending.sort(key=lambda s: PAIN_LEVELS.get(s.get("pain_level", "P4"), {}).get("priority", 99))
    return pending

# ─── 清除疼痛信号 ───────────────────────────────────────────────────────────
def clear(pain_id: str, reason: str = "") -> bool:
    """清除指定的疼痛信号文件。"""
    signal_file = PAIN_DIR / f"{pain_id}.json"
    if not signal_file.exists():
        return False

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "event": "clear",
            "pain_id": pain_id,
            "reason": reason,
            "timestamp": utcnow(),
        }, ensure_ascii=False) + "\n")

    signal_file.unlink()
    return True

# ─── 状态摘要 ───────────────────────────────────────────────────────────────
def status() -> dict:
    """返回 pain_bus 当前状态（用于 autonomic_master 调用）。"""
    pending = check_pending("P4")
    worst = pending[0] if pending else None

    # 计算 checkpoints 数量
    cp_count = len(list(CHECKPOINT_DIR.glob("pain_*"))) if CHECKPOINT_DIR.exists() else 0

    return {
        "status": "running",
        "pending_count": len(pending),
        "worst_level": worst["pain_level"] if worst else None,
        "worst_summary": worst["summary"] if worst else None,
        "checkpoint_count": cp_count,
        "log_lines": sum(1 for _ in open(LOG_FILE, "r", encoding="utf-8", errors="ignore").readlines()) if LOG_FILE.exists() else 0,
    }

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · 疼痛总线")
    sub = parser.add_subparsers(dest="cmd")

    # emit
    e = sub.add_parser("emit", help="发射疼痛信号")
    e.add_argument("level", help="P0/P1/P2/P3/P4")
    e.add_argument("source", help="来源子系统")
    e.add_argument("summary", help="摘要")
    e.add_argument("--checkpoint", action="store_true", help="触发快照")
    e.add_argument("--detail", default="{}", help="详情 JSON 字符串")
    e.add_argument("--action", default="REVIEW_AND_DECIDE", help="建议操作")
    e.add_argument("--note", default="", help="快照备注")

    # pending
    sub.add_parser("pending", help="查看待处理疼痛信号")

    # clear
    c = sub.add_parser("clear", help="清除疼痛信号")
    c.add_argument("pain_id", help="疼痛信号 ID")
    c.add_argument("--reason", default="", help="清除原因")

    # status
    sub.add_parser("status", help="疼痛总线状态")

    args = parser.parse_args()

    if args.cmd == "emit":
        import json as _json
        details = _json.loads(args.detail) if args.detail != "{}" else None
        pid = emit(args.level, args.source, args.summary,
                   details=details, suggested_action=args.action,
                   checkpoint=args.checkpoint, checkpoint_note=args.note)
        print(f"Pain emitted: {pid}")

    elif args.cmd == "pending":
        pending = check_pending("P4")
        if not pending:
            print("No pending pain signals.")
        for s in pending:
            print(f"  [{s['pain_level']}] {s['timestamp']} {s['source']}: {s['summary']}")

    elif args.cmd == "clear":
        ok = clear(args.pain_id, args.reason)
        print(f"{'Cleared' if ok else 'Not found'}: {args.pain_id}")

    elif args.cmd == "status":
        import pprint
        pprint.pprint(status())

    else:
        parser.print_help()
