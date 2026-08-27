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
import time
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import io

# ─── 强制 UTF-8 stdout（避免 GBK 控制台下打印 emoji/中文崩溃）──────────────
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── 路径配置 ───────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent.parent.resolve()
SOMA_DIR  = WORKSPACE / "scripts" / "SOMA"
PAIN_DIR  = SOMA_DIR  / "pain_signals"
LOG_FILE  = SOMA_DIR  / "pain_log.jsonl"
CHECKPOINT_DIR = SOMA_DIR / "checkpoints"
STATE_FILE = SOMA_DIR / "pain_state.json"

for _d in (PAIN_DIR, CHECKPOINT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ─── SOMA→LLM 唤醒桥（TK-SOMA-WAKE-001）─────────────────────────────────────
# ⚠️ 端口动态探测：gateway 每次升级端口会变（58213→58243 等），从 openclaw.json 读真实端口
import json as _json, os as _os, re as _re

def _detect_gateway_port() -> int:
    """从 openclaw.json 读取 gateway 端口（失败回退 58243）。"""
    candidates = [
        _os.path.expanduser("~/.qclaw/openclaw.json"),
        _os.path.expanduser("~/.openclaw/openclaw.json"),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = _json.load(f)
            port = cfg.get("port") or (cfg.get("server") or {}).get("port")
            if port:
                return int(port)
        except Exception:
            continue
    return 58243  # 默认回退

HOOK_PORT = _detect_gateway_port()
HOOK_URL = f"http://127.0.0.1:{HOOK_PORT}/hooks/wake"
HOOK_TOKEN = _os.environ.get("SOMA_HOOK_TOKEN", "soma-wake-20260812-9f4e2c7a")
WAKE_COOLDOWN_SECONDS = 300  # 5 分钟防风暴：P1 在冷却期内降级 next-heartbeat

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
    """真实 UTC 时间（ISO8601，Z 后缀；符合时区统一规范，与 time.time() 一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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

# ─── SOMA→LLM 唤醒桥（TK-SOMA-WAKE-001）─────────────────────────────────────
def _load_state() -> dict:
    """读取 pain_bus 状态（last_wake_ts 等）。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _wake_llm(text: str, mode: str = "now") -> bool:
    """唤醒 LLM 主会话（POST gateway hooks/wake）。

    零依赖（urllib 标准库），3 秒超时，异常静默捕获不崩 pain_bus。
    返回是否成功；失败写 pain_log.jsonl。
    """
    import urllib.request
    body = json.dumps({"text": text, "mode": mode}).encode("utf-8")
    req = urllib.request.Request(
        HOOK_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {HOOK_TOKEN}",
                 "Content-Type": "application/json"},
    )
    ok = False
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            ok = 200 <= resp.status < 300
    except Exception as exc:  # 异常静默捕获：不崩 pain_bus
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "event": "wake_llm_error", "error": str(exc),
                    "mode": mode, "timestamp": utcnow(),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return ok


# ─── 去重配置 ──────────────────────────────────────────────────────────────
DEDUP_WINDOW_SECONDS = 3600  # 1 小时内同源同摘要不重复发射

def _is_duplicate(source: str, summary: str) -> Optional[str]:
    """检查是否已有同源同摘要的近期信号。返回已有 pain_id 或 None。"""
    if not PAIN_DIR.exists():
        return None
    now = time.time()
    for f in PAIN_DIR.iterdir():
        if f.suffix != '.json' or f.name.startswith('.'):
            continue
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                sig = json.load(fh)
            sig_source = sig.get('source', '')
            sig_summary = sig.get('summary', '') or sig.get('detail', '')
            sig_ts = sig.get('timestamp') or sig.get('ts') or ''
            if sig_source != source:
                continue
            # 摘要完全匹配或包含匹配
            if summary not in sig_summary and sig_summary not in summary:
                continue
            # 检查时间窗口
            if sig_ts:
                try:
                    ts_clean = sig_ts.replace('Z', '+00:00')
                    sig_time = datetime.fromisoformat(ts_clean).timestamp()
                    if now - sig_time < DEDUP_WINDOW_SECONDS:
                        return sig.get('pain_id', f.stem)
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, OSError):
            pass
    return None


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
    # 去重：同源同摘要在窗口期内不重复发射
    existing_id = _is_duplicate(source, summary)
    if existing_id:
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'event': 'emit_dedup', 'existing_id': existing_id,
                    'source': source, 'summary': summary, 'timestamp': utcnow(),
                }, ensure_ascii=False) + '\n')
        except OSError:
            pass
        return existing_id

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

    # ★ SOMA→LLM 唤醒桥（TK-SOMA-WAKE-001）：P0/P1 触发主动唤醒，大脑响应
    if info["wake_llm"]:
        state = _load_state()
        mode = "now"
        last_ts = state.get("last_wake_ts")
        if level == "P1" and last_ts:
            try:
                if time.time() - datetime.fromisoformat(last_ts).timestamp() < WAKE_COOLDOWN_SECONDS:
                    mode = "next-heartbeat"  # 防风暴：5 分钟内已唤醒，P1 降级
            except (ValueError, TypeError):
                pass
        wake_ok = _wake_llm(summary, mode=mode)
        state["last_wake_ts"] = utcnow()
        _save_state(state)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "event": "wake_llm", "ok": wake_ok, "mode": mode,
                    "pain_id": pain_id, "level": level, "timestamp": utcnow(),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

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
            # 归一化兼容字段：部分子系统写 "level"/"ts" 而非 "pain_level"/"timestamp"
            if "pain_level" not in sig and "level" in sig:
                sig["pain_level"] = sig["level"]
            if "timestamp" not in sig and "ts" in sig:
                sig["timestamp"] = sig["ts"]
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
        "worst_level": (worst.get("pain_level") or worst.get("level")) if worst else None,
        "worst_summary": (worst.get("summary") or worst.get("detail") or "") if worst else None,
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
            lvl = s.get('pain_level') or s.get('level') or 'P4'
            ts = s.get('timestamp') or s.get('ts') or '?'
            src = s.get('source') or s.get('subsystem') or '?'
            print(f"  [{lvl}] {ts} {src}: {s.get('summary', '')}")

    elif args.cmd == "clear":
        ok = clear(args.pain_id, args.reason)
        print(f"{'Cleared' if ok else 'Not found'}: {args.pain_id}")

    elif args.cmd == "status":
        import pprint
        pprint.pprint(status())

    else:
        parser.print_help()
