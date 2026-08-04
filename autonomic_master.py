# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 自治层统一调度器 (autonomic_master.py)
===================================================
替代所有散落的 heartbeat_*.ps1 / heartbeat_*.py 脚本，
成为 Windows Nyx 自治层的唯一入口。

调度策略（完全基于时间，零 LLM）：
  每 1min:   respiratory（NAS 变更检测）
  每 5min:   heartd（多级心跳探测）
  每 15min:  vault_operations（记忆衰减）
  每 30min:  memory_integrity（完整性校验）
  每 60min:  thermo（水位监控）
  每天 04:00: digest（文件生命周期清理）

多稳态模式：
  normal（默认） — 全频调度
  standby（节能） — 仅 heartd，每 15min
  combat（战备） — 全频 + respiratory 切换到每 30s
  hibench（冬眠）— 仅 heartd，每 30min
  disaster（灾难）— 仅 heartd 内嵌最简版

使用方法：
  python autonomic_master.py run           # 持续运行
  python autonomic_master.py status        # 查看所有子系统状态
  python autonomic_master.py mode <name>   # 切换稳态模式
  python autonomic_master.py health        # 快速健康报告
"""

import os
import sys
import json
import time
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WORKSPACE  = Path(__file__).parent.parent.parent.resolve()
SOMA_DIR   = WORKSPACE / "scripts" / "SOMA"
STATE_FILE = SOMA_DIR / "autonomic_state.json"
LOG_FILE   = SOMA_DIR / "autonomic_log.jsonl"

# 导入 pain_bus（如果存在）
try:
    sys.path.insert(0, str(SOMA_DIR))
    import pain_bus
    HAS_PAIN_BUS = True
except ImportError:
    HAS_PAIN_BUS = False

# SOMA_DIR 已加入 sys.path，所有子系统可直接 import
_REFLEX_ERR = _IMMUNE_ERR = None
try:
    import reflex as reflex_mod
    HAS_REFLEX = True
except ImportError as e:
    HAS_REFLEX = False
    _REFLEX_ERR = str(e)

try:
    import immune_cleaner as immune_mod
    HAS_IMMUNE = True
except ImportError as e:
    HAS_IMMUNE = False
    _IMMUNE_ERR = str(e)

# ─── 路径适配（Windows）────────────────────────────────────────────────────
def NAS_WEBDAV_BASE() -> str:
    return "http://100.123.195.10:5005/qclaw"

def NAS_WEBDAV(path: str) -> str:
    return f"{NAS_WEBDAV_BASE()}/{path.lstrip('/')}"

# WebDAV Basic 认证（新 NAS debianhan 已改为非匿名）
import base64
NAS_WEBDAV_AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'anima:animastellar').decode()}

# ─── 工具 ───────────────────────────────────────────────────────────────────
def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def log_event(subsystem: str, event: str, detail: str = ""):
    entry = json.dumps({
        "ts": utcnow(),
        "subsystem": subsystem,
        "event": event,
        "detail": detail,
    }, ensure_ascii=False)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

def write_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def read_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# ─── 心跳 L0-L3 探测 ───────────────────────────────────────────────────────
def probe_heartd() -> dict:
    """多级心跳探测。"""
    from urllib.request import urlopen, Request
    from urllib.error import URLError

    results = {
        "L0_process": True,
        "L1_cron": True,
        "L2_nas_webdav": False,
        "L3_memguard": False,
    }

    # L0: 进程存活
    try:
        import psutil
        nyx_found = any("python" in p.name().lower() or "qclaw" in p.name().lower()
                       for p in psutil.process_iter(["name"]))
        results["L0_process"] = nyx_found
    except ImportError:
        # psutil 不可用，简单检查
        results["L0_process"] = True  # 如果能运行脚本，说明进程活着

    # L2: NAS WebDAV 可达
    try:
        req = Request(NAS_WEBDAV(""), method="HEAD", headers=NAS_WEBDAV_AUTH)
        with urlopen(req, timeout=5) as r:
            results["L2_nas_webdav"] = r.status in (200, 301, 404)
    except (URLError, TimeoutError):
        results["L2_nas_webdav"] = False

    # L3: MemGuard 服务
    try:
        req = Request("http://127.0.0.1:5050/api/health")
        with urlopen(req, timeout=3) as r:
            results["L3_memguard"] = r.status == 200
    except Exception:
        results["L3_memguard"] = False

    return results

# ─── 呼吸子系统 ────────────────────────────────────────────────────────────
def run_respiratory() -> dict:
    """检测 NAS 变更 → 增量同步 → 触发 integrity check。"""
    from urllib.request import urlopen, Request
    from urllib.error import URLError

    try:
        # 检查 NAS memory 目录
        req = Request(NAS_WEBDAV("memory/"), method="PROPFIND", headers=NAS_WEBDAV_AUTH)
        req.add_header("Depth", "1")
        with urlopen(req, timeout=8) as r:
            # 简单判断：能响应 = NAS 在线
            return {"status": "ok", "nas_reachable": True}
    except Exception:
        pass

    # NAS 不可达时，记录 P3 疼痛
    if HAS_PAIN_BUS:
        pain_bus.emit(
            level="P3",
            source="respiratory",
            summary="NAS WebDAV 不可达，呼吸子系统暂停",
            details={"nas_url": NAS_WEBDAV_BASE()},
        )
    return {"status": "degraded", "nas_reachable": False}

# ─── 体温子系统 ────────────────────────────────────────────────────────────
def run_thermo() -> dict:
    """检查资源水位。"""
    import shutil

    warnings = []

    # workspace 大小
    total_size = sum(
        f.stat().st_size
        for f in Path(WORKSPACE).rglob("*")
        if f.is_file() and "__pycache__" not in str(f)
    ) / (1024 * 1024)

    # 磁盘可用空间
    try:
        drive = str(WORKSPACE.drive or "C:")
        free_gb = shutil.disk_usage(drive).free / (1024**3)
    except Exception:
        free_gb = 999

    if total_size > 500:
        warnings.append(f"workspace过大: {total_size:.1f}MB（上限500MB）")
    if free_gb < 10:
        warnings.append(f"磁盘剩余空间不足: {free_gb:.1f}GB")

    level = None
    if warnings:
        level = "P2" if total_size > 480 or free_gb < 5 else "P3"
        if HAS_PAIN_BUS:
            pain_bus.emit(
                level=level,
                source="thermo",
                summary="资源水位异常",
                details={"workspace_mb": round(total_size, 1), "free_gb": round(free_gb, 1), "warnings": warnings},
            )

    return {
        "workspace_mb": round(total_size, 1),
        "free_gb": round(free_gb, 1),
        "warnings": warnings,
        "pain_level": level,
    }

# ─── 记忆完整性 ─────────────────────────────────────────────────────────────
def run_integrity() -> dict:
    """运行 memory_integrity check（调用已有脚本）。"""
    script = WORKSPACE / "silicon-civilization-kb" / "scripts" / "memory_integrity.py"
    if not script.exists():
        return {"status": "script_not_found"}

    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, timeout=60,
            text=True, encoding="utf-8", errors="ignore",
        )
        tampered = "tampered" in result.stdout.lower() or result.returncode != 0
        if tampered and HAS_PAIN_BUS:
            pain_bus.emit(
                level="P1",
                source="memory_integrity",
                summary="记忆完整性检查失败",
                details={"stdout": result.stdout[:500], "stderr": result.stderr[:500]},
                checkpoint=True,
            )
        return {"status": "ok", "tampered": tampered}
    except Exception as e:
        return {"status": "error", "error": str(e)}

# ─── 消化子系统 ─────────────────────────────────────────────────────────────
def run_vault() -> dict:
    """运行记忆衰减（调用 vault_operations.py）。"""
    script = WORKSPACE / "scripts" / "vault_operations.py"
    if not script.exists():
        return {"status": "script_not_found"}

    try:
        result = subprocess.run(
            [sys.executable, str(script), "decay"],
            capture_output=True, timeout=60,
            text=True, encoding="utf-8", errors="ignore",
        )
        return {"status": "ok", "output": result.stdout[:200]}
    except Exception as e:
        return {"status": "error", "error": str(e)}

# ─── 消化·文件生命周期（digest）────────────────────────────────────────────
def run_digest(dry_run: bool = True) -> dict:
    """文件生命周期管理（Mac digest.py 的 Windows 版本）。"""
    # 白名单
    WHITELIST = {
        "SOUL.md", "IDENTITY.md", "MEMORY.md", "USER.md",
        "AGENTS.md", "HEARTBEAT.md", "TOOLS.md", "MEMORY_NAS_AUTHORITATIVE.md",
    }

    ARCHIVE = WORKSPACE / "archive"
    ARCHIVE.mkdir(exist_ok=True)

    # 扫描 workspace 根目录
    migrated = []
    for f in WORKSPACE.iterdir():
        if f.is_file() and f.name not in WHITELIST:
            # tmp/disposable 分类
            if any(x in f.name.lower() for x in ["tmp", "stale", "test_", "temp_", "fix_", "_tmp", "_m5"]):
                dst = ARCHIVE / "scripts" / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    f.rename(dst)
                    migrated.append(f"→ scripts/{f.name}")
                except Exception:
                    pass

    return {"migrated": migrated, "dry_run": dry_run}

# ─── 调度器状态机 ───────────────────────────────────────────────────────────
MODES = {
    "normal":  {"respiratory_min": 1,  "heartd_min": 5,  "vault_min": 15, "integrity_min": 30, "thermo_min": 60, "reflex_min": 60, "immune_min": 120},
    "standby": {"respiratory_min": 15, "heartd_min": 15, "vault_min": 60, "integrity_min": 60, "thermo_min": 120, "reflex_min": 120, "immune_min": 240},
    "combat":  {"respiratory_min": 0.5,"heartd_min": 2,  "vault_min": 15, "integrity_min": 15, "thermo_min": 15, "reflex_min": 30, "immune_min": 60},
    "hibench": {"respiratory_min": 0,  "heartd_min": 30, "vault_min": 0,  "integrity_min": 0,  "thermo_min": 0,  "reflex_min": 0, "immune_min": 0},
    "disaster":{"respiratory_min": 0,  "heartd_min": 5,  "vault_min": 0,  "integrity_min": 0,  "thermo_min": 0,  "reflex_min": 0, "immune_min": 0},
}

def get_current_mode() -> str:
    return read_state().get("mode", "normal")

def set_mode(mode: str) -> str:
    if mode not in MODES:
        return f"Unknown mode: {mode}"
    state = read_state()
    state["mode"] = mode
    state["last_mode_change"] = utcnow()
    write_state(state)
    log_event("autonomic_master", "mode_change", mode)
    return f"Mode set to: {mode}"

# ─── 调度主循环 ─────────────────────────────────────────────────────────────
def run_loop(interval_min: int = 1, stop_event=None):
    """
    主调度循环。
    interval_min: 每次轮询间隔（分钟）
    stop_event: threading.Event，设为 True 时优雅退出
    """
    import threading

    counters = {
        "respiratory": 0,
        "heartd": 0,
        "vault": 0,
        "integrity": 0,
        "thermo": 0,
        "reflex": 0,
        "immune": 0,
        "digest": 0,
    }
    digest_hour = 4  # 每天 04:00 执行 digest

    while True:
        mode = get_current_mode()
        schedule = MODES.get(mode, MODES["normal"])

        now = datetime.now()
        counters["respiratory"] += interval_min
        counters["heartd"]       += interval_min
        counters["vault"]        += interval_min
        counters["integrity"]    += interval_min
        counters["thermo"]       += interval_min
        counters["reflex"]       += interval_min
        counters["immune"]       += interval_min

        # digest: 每天 04:00
        if now.hour == digest_hour and now.minute < interval_min:
            counters["digest"] = 1
        else:
            counters["digest"] += interval_min

        # ── 执行各子系统 ──
        if counters["respiratory"] >= schedule["respiratory_min"] and schedule["respiratory_min"] > 0:
            counters["respiratory"] = 0
            r = run_respiratory()
            log_event("respiratory", "run", json.dumps(r))

        if counters["heartd"] >= schedule["heartd_min"]:
            counters["heartd"] = 0
            r = probe_heartd()
            log_event("heartd", "probe", json.dumps(r))
            # 如果 L2/L3 全挂，发疼痛
            if not r.get("L2_nas_webdav") and not r.get("L3_memguard") and HAS_PAIN_BUS:
                pain_bus.emit("P3", "heartd", "NAS WebDAV + MemGuard 均不可达", details=r)

        if counters["integrity"] >= schedule["integrity_min"] and schedule["integrity_min"] > 0:
            counters["integrity"] = 0
            r = run_integrity()
            log_event("integrity", "check", json.dumps(r))

        if counters["vault"] >= schedule["vault_min"] and schedule["vault_min"] > 0:
            counters["vault"] = 0
            r = run_vault()
            log_event("vault", "decay", json.dumps(r))

        if counters["thermo"] >= schedule["thermo_min"] and schedule["thermo_min"] > 0:
            counters["thermo"] = 0
            r = run_thermo()
            log_event("thermo", "check", json.dumps(r))

        if counters["reflex"] >= schedule["reflex_min"] and schedule["reflex_min"] > 0:
            counters["reflex"] = 0
            if HAS_REFLEX:
                try:
                    r = reflex_mod.dry_run()
                    violations = sum(1 for item in r for v in [item] if v.get(list(v.keys())[0], {}).get('blocks'))
                    log_event("reflex", "check", {"violations": violations})
                    if violations > 0 and HAS_PAIN_BUS:
                        pain_bus.emit("P1", "reflex", f"{violations} hard-rule violation(s) detected")
                except Exception as e:
                    log_event("reflex", "error", str(e))

        if counters["immune"] >= schedule["immune_min"] and schedule["immune_min"] > 0:
            counters["immune"] = 0
            if HAS_IMMUNE:
                try:
                    r = immune_mod.run(verify_integrity=True, scan_md=False, scan_json=False)
                    integrity_ok = r.get("summary", {}).get("integrity_ok", True)
                    repairs = r.get("summary", {}).get("total_repairs", 0)
                    log_event("immune", "run", {"repairs": repairs, "integrity_ok": integrity_ok})
                    if not integrity_ok and HAS_PAIN_BUS:
                        pain_bus.emit("P1", "immune", "Core integrity check failed", checkpoint=True)
                except Exception as e:
                    log_event("immune", "error", str(e))

        if counters["digest"] >= 1440 and schedule["respiratory_min"] > 0:  # ~每天
            counters["digest"] = 0
            r = run_digest()
            log_event("digest", "run", json.dumps(r))

        # 更新状态文件
        state = read_state()
        state.update({
            "last_tick": utcnow(),
            "mode": mode,
            "counters": counters,
            "heartd_last": r if counters["heartd"] == 0 else state.get("heartd_last"),
        })
        write_state(state)

        # 检查退出信号
        if stop_event and stop_event.is_set():
            log_event("autonomic_master", "shutdown", "stop event received")
            break

        time.sleep(interval_min * 60)

# ─── 状态视图 ───────────────────────────────────────────────────────────────
def get_subsystem_status() -> dict:
    """收集所有子系统的最新状态。"""
    state = read_state()
    heartd_last = state.get("heartd_last", {})

    # pain_bus 状态
    pb = pain_bus.status() if HAS_PAIN_BUS else {"status": "not_installed"}

    # workspace 大小
    total_size = sum(
        f.stat().st_size
        for f in Path(WORKSPACE).rglob("*")
        if f.is_file() and "__pycache__" not in str(f)
    ) / (1024 * 1024)

    return {
        "mode": state.get("mode", "normal"),
        "last_tick": state.get("last_tick", "never"),
        "uptime": state.get("last_mode_change", "unknown"),
        "workspace_mb": round(total_size, 1),
        "pain_bus": pb,
        "heartd": {
            "L0_process": heartd_last.get("L0_process", "?"),
            "L2_nas_webdav": heartd_last.get("L2_nas_webdav", "?"),
            "L3_memguard": heartd_last.get("L3_memguard", "?"),
        },
        "counters": state.get("counters", {}),
    }

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · 自治层统一调度器")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="启动调度主循环")
    sub.add_parser("status", help="查看所有子系统状态")
    sub.add_parser("health", help="快速健康检查")

    m = sub.add_parser("mode", help="切换稳态模式")
    m.add_argument("name", choices=list(MODES.keys()), help="模式名称")

    sub.add_parser("probe", help="手动执行 heartd 探测")

    args = parser.parse_args()

    if args.cmd == "run":
        print(f"Starting autonomic master... mode={get_current_mode()}")
        try:
            run_loop()
        except KeyboardInterrupt:
            print("Autonomic master stopped.")

    elif args.cmd == "status":
        import pprint
        pprint.pprint(get_subsystem_status())

    elif args.cmd == "health":
        s = get_subsystem_status()
        issues = []
        if not s["heartd"].get("L2_nas_webdav"):
            issues.append("[WARN] NAS WebDAV unavailable")
        if not s["heartd"].get("L3_memguard"):
            issues.append("[WARN] MemGuard service offline")
        pending = s['pain_bus'].get('pending_count', 0)
        if pending > 0:
            issues.append(f"[PAIN] {pending} pending pain signal(s)")
        if not issues:
            print("[OK] All subsystems healthy")
        else:
            for i in issues:
                print(i)
        print(f"    Mode: {s['mode']} | Workspace: {s['workspace_mb']}MB | Last tick: {s['last_tick']}")

    elif args.cmd == "mode":
        print(set_mode(args.name))

    elif args.cmd == "probe":
        import pprint
        pprint.pprint(probe_heartd())

    else:
        parser.print_help()
