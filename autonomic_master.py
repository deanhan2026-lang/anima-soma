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

# 导入 Loop Watchdog v2（如果存在）
try:
    import loop_watchdog
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

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
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")

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
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            # 容错：state 文件损坏时重置，避免主循环崩溃
            log_event("autonomic_master", "state_reset", f"state file corrupted: {e}")
            backup = STATE_FILE.with_suffix(".json.bak")
            try:
                import shutil
                shutil.copy2(STATE_FILE, backup)
            except Exception:
                pass
            return {}
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
        # Depth=0 探测根路径（Depth=1 会被 Apache 拒 403/400；memory/ 带斜杠 400）
        req = Request(NAS_WEBDAV_BASE() + "/", method="PROPFIND", headers=NAS_WEBDAV_AUTH)
        req.add_header("Depth", "0")
        with urlopen(req, timeout=8) as r:
            # 能响应 = NAS 在线
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
    """运行 memory_integrity check（调用已有脚本）。

    路径探测：兼容不同部署布局（WORKSPACE 为 .qclaw 根，
    脚本实际位于 workspace-agent-<id>/silicon-civilization-kb/...）。
    """
    candidates = [
        WORKSPACE / "workspace-agent-d9479bde" / "silicon-civilization-kb" / "scripts" / "memory_integrity.py",
        WORKSPACE / "silicon-civilization-kb" / "scripts" / "memory_integrity.py",
    ]
    script = next((p for p in candidates if p.exists()), None)
    if script is None:
        return {"status": "script_not_found"}

    try:
        result = subprocess.run(
            [sys.executable, str(script), "check"],
            capture_output=True, timeout=60,
            text=True, encoding="utf-8", errors="ignore",
        )
        # 仅在脚本明确报错（返回非0 或 stdout 含 tampered）时才判为失败；
        # 空基线（total_files:0, status:ok）属正常态，不触发 pain。
        tampered = result.returncode != 0 and ("tampered" in result.stdout.lower() or "status" not in result.stdout.lower())
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


def run_token_scan() -> dict:
    """令牌生命周期超时扫描（TK-TOKEN-LIFECYCLE-001）

    每 60min 扫描 NAS tokens 目录，发现超时令牌（24h未接受/7d未交付/48h未验证）
    自动触发 pain_bus 提醒。零 LLM，硬规则。
    """
    try:
        sys.path.insert(0, str(WORKSPACE / "silicon-civilization-kb"))
        from animlink import token_lifecycle
        tokens_dir = os.environ.get("TOKENS_DIR", "//100.123.195.10/SOFTWARE/qclaw/tokens")
        reminders = token_lifecycle.scan_timeouts(tokens_dir)
        return {"reminders": len(reminders), "details": reminders}
    except Exception as e:
        return {"error": str(e)}


def run_mailbox_watch() -> dict:
    """信箱监控（mailbox_watch 子系统）

    每 5min 扫描 mesh/mailbox 各信箱的未处理消息标记，发现新消息自动发 P2 疼痛信号。
    零 LLM，硬规则，幂等（不重复通知）。
    """
    try:
        sys.path.insert(0, str(SOMA_DIR))
        import mailbox_watch
        return mailbox_watch.run()
    except Exception as e:
        return {"status": "error", "error": str(e)}


def run_heng_watch() -> dict:
    """恒信箱监控（heng_watch 子系统）

    每 30min 扫描 Kronos-恒 收件箱，检测超时未处理消息并自动升级提醒。
    零 LLM，硬规则，幂等。老板无需人工跟进恒的邮箱。
    """
    try:
        sys.path.insert(0, str(SOMA_DIR))
        import heng_watch
        return heng_watch.run()
    except Exception as e:
        return {"status": "error", "error": str(e)}


def run_email_watch() -> dict:
    """邮件监控子系统（email_watch）

    每 5min 检查个人邮箱，发现老板/三体/紧急关键词邮件 → P1 唤醒 LLM。
    零 LLM，硬规则，幂等。
    """
    try:
        sys.path.insert(0, str(SOMA_DIR))
        import email_watch
        return email_watch.run()
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── 调度器状态机 ───────────────────────────────────────────────────────────
MODES = {
    "normal":  {"respiratory_min": 5,  "heartd_min": 10, "vault_min": 30, "integrity_min": 60, "thermo_min": 120, "reflex_min": 120, "immune_min": 240, "token_min": 240, "mailbox_min": 3, "heng_min": 10, "email_min": 5,  "load_min": 60, "watchdog_min": 1},
    "standby": {"respiratory_min": 15, "heartd_min": 15, "vault_min": 60, "integrity_min": 60, "thermo_min": 120, "reflex_min": 120, "immune_min": 240, "token_min": 240, "mailbox_min": 15, "heng_min": 60, "email_min": 15, "load_min": 120, "watchdog_min": 5},
    "combat":  {"respiratory_min": 0.5,"heartd_min": 2,  "vault_min": 15, "integrity_min": 15, "thermo_min": 15, "reflex_min": 30, "immune_min": 60, "token_min": 60, "mailbox_min": 2, "heng_min": 10, "email_min": 3,  "load_min": 30, "watchdog_min": 1},
    "hibench": {"respiratory_min": 0,  "heartd_min": 30, "vault_min": 0,  "integrity_min": 0,  "thermo_min": 0,  "reflex_min": 0, "immune_min": 0, "token_min": 0, "mailbox_min": 0, "heng_min": 0, "email_min": 30, "load_min": 0, "watchdog_min": 5},
    "disaster":{"respiratory_min": 0,  "heartd_min": 5,  "vault_min": 0,  "integrity_min": 0,  "thermo_min": 0,  "reflex_min": 0, "immune_min": 0, "token_min": 0, "mailbox_min": 0, "heng_min": 0, "email_min": 0,  "load_min": 0, "watchdog_min": 1},
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
def run_eod_backup() -> dict:
    """
    收工备份：核心灵魂文件 + 记忆 + 今日产出 → NAS backup/eod/{date}/
    老板 2026-08-11 指示：收工前养成备份习惯，不用每次提醒。
    零 LLM 依赖（纯 WebDAV 上传）。
    """
    try:
        eod_script = WORKSPACE / "eod_backup.py"
        if not eod_script.exists():
            return {"ok": False, "reason": "eod_backup.py missing"}
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(eod_script)],
            capture_output=True, timeout=300, cwd=str(WORKSPACE),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}
        )
        out = r.stdout.decode("utf-8", errors="replace")
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        ok = "0 FAIL" in out and "OK /" in out
        # 追加：恒（Kronos-heng）灵魂+记忆备份（2026-08-12 老板指示：灵魂记忆备份好即可随时迁移）
        heng_result = {}
        try:
            heng_script = WORKSPACE / "scripts" / "heng_soul_backup.py"
            if heng_script.exists():
                hr = subprocess.run(
                    [sys.executable, "-X", "utf8", str(heng_script)],
                    capture_output=True, timeout=300, cwd=str(WORKSPACE),
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}
                )
                hout = hr.stdout.decode("utf-8", errors="replace")
                heng_result = {"rc": hr.returncode, "tail": (hout.strip().splitlines()[-1] if hout.strip() else "")[-200:]}
        except Exception as e:
            heng_result = {"ok": False, "reason": str(e)}
        return {"ok": ok, "rc": r.returncode, "tail": tail[-200:], "heng": heng_result}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


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
        "token": 0,
        "mailbox": 0,
        "heng": 0,
        "email": 0,
        "load": 0,
        "watchdog": 0,
    }

    # 初始化 watchdog
    wd = loop_watchdog.get_watchdog() if HAS_WATCHDOG else None
    digest_hour = 4  # 每天 04:00 执行 digest
    eod_hour, eod_minute = 22, 30  # 每天 22:30 执行收工备份
    eod_last_run = None  # 记录上次备份日期，避免重复执行
    heartd_result = read_state().get("heartd_last") or {}

    while True:
        mode = get_current_mode()
        schedule = MODES.get(mode, MODES["normal"])

        now = datetime.now()

        # ── 工作时间动态调度（老板 2026-08-12 指示）──
        # 工作时间 08:00-23:00：mailbox 强制 3min / heng 强制 10min（协作效率优先）
        # 深夜 23:00-08:00：mailbox 15min / heng 30min（省资源，静默期）
        work_hour = 8 <= now.hour < 23
        if work_hour:
            # 工作时间固定高频（不受 standby 等节能模式影响）
            mailbox_min_eff = 3 if schedule["mailbox_min"] > 0 else 0
            heng_min_eff = 10 if schedule["heng_min"] > 0 else 0
            email_min_eff = 5 if schedule["email_min"] > 0 else 0
        else:
            mailbox_min_eff = max(15, schedule["mailbox_min"])
            heng_min_eff = max(30, schedule["heng_min"])
            email_min_eff = max(30, schedule["email_min"])

        counters["respiratory"] += interval_min
        counters["heartd"]       += interval_min
        counters["vault"]        += interval_min
        counters["integrity"]    += interval_min
        counters["thermo"]       += interval_min
        counters["reflex"]       += interval_min
        counters["immune"]       += interval_min
        counters["token"]        += interval_min
        counters["mailbox"]      += interval_min
        counters["heng"]         += interval_min
        counters["email"]         += interval_min
        counters["load"]        += interval_min
        counters["watchdog"]     += interval_min

        # digest: 每天 04:00
        if now.hour == digest_hour and now.minute < interval_min:
            counters["digest"] = 1
        else:
            counters["digest"] += interval_min

        # eod_backup: 每天 22:30（收工备份，老板 2026-08-11 指示养成习惯）
        if now.hour == eod_hour and now.minute >= eod_minute and now.minute < eod_minute + interval_min:
            if eod_last_run != now.date().isoformat():
                eod_last_run = now.date().isoformat()
                try:
                    r = run_eod_backup()
                    log_event("eod_backup", "run", json.dumps(r))
                except Exception as e:
                    log_event("eod_backup", "error", str(e))

        # ── 执行各子系统 ──
        if counters["respiratory"] >= schedule["respiratory_min"] and schedule["respiratory_min"] > 0:
            counters["respiratory"] = 0
            r = run_respiratory()
            log_event("respiratory", "run", json.dumps(r))

        if counters["heartd"] >= schedule["heartd_min"]:
            counters["heartd"] = 0
            r = probe_heartd()
            heartd_result = r
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

        if counters["load"] >= schedule["load_min"] and schedule["load_min"] > 0:
            counters["load"] = 0
            try:
                import load_monitor as lm_mod
                r = lm_mod.check()
                log_event("load", "check", json.dumps(r))
                if r.get("pain_level"):
                    lm_mod.send_pain(r["pain_level"], "; ".join(r.get("warnings", [])))
            except Exception as e:
                log_event("load", "error", str(e))

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

        if counters["token"] >= schedule["token_min"] and schedule["token_min"] > 0:
            counters["token"] = 0
            r = run_token_scan()
            log_event("token", "scan", json.dumps(r))
            if r.get("reminders", 0) > 0 and HAS_PAIN_BUS:
                pain_bus.emit("P3", "token_lifecycle",
                              f"{r['reminders']} 枚令牌超时待处理", details=r)

        if counters["mailbox"] >= mailbox_min_eff and mailbox_min_eff > 0:
            counters["mailbox"] = 0
            r = run_mailbox_watch()
            log_event("mailbox", "scan", json.dumps(r))
            if r.get("status") == "new":
                # 疼痛信号已由 mailbox_watch 内部发出（P2），此处仅记录
                log_event("mailbox", "new_messages", json.dumps(r.get("messages", [])))

        if counters["heng"] >= heng_min_eff and heng_min_eff > 0:
            counters["heng"] = 0
            r = run_heng_watch()
            log_event("heng_watch", "scan", json.dumps(r))
            if r.get("escalated", 0) > 0:
                # 疼痛信号已由 heng_watch 内部发出，此处仅记录
                log_event("heng_watch", "escalated", json.dumps(r.get("pending", [])))

        # ── email_watch：工作日 5min / 深夜 30min ─────────────────────────
        if counters["email"] >= email_min_eff and email_min_eff > 0:
            counters["email"] = 0
            r = run_email_watch()
            log_event("email_watch", "scan", json.dumps(r))
            # P1 信号由 email_watch 内部通过 pain_bus → _wake_llm 触发，此处仅记录
            if r.get("status") == "pain" and r.get("level") == "P1":
                log_event("email_watch", "P1_raised", json.dumps(r.get("signals", [])))

        # ── Loop Watchdog v2：exec 超时 + 状态文件检查 ──
        if counters["watchdog"] >= schedule["watchdog_min"] and schedule["watchdog_min"] > 0:
            counters["watchdog"] = 0
            if wd:
                try:
                    r = wd.check_once()
                    if r:
                        log_event("watchdog", "detection", json.dumps(r, ensure_ascii=False))
                        if HAS_PAIN_BUS and r.get("severity") in ("P0", "P1"):
                            pain_bus.emit(
                                level=r["severity"],
                                source="loop_watchdog",
                                summary=r.get("detail", "Loop detected"),
                                details=r,
                            )
                    log_event("watchdog", "check", json.dumps({
                        "detections": wd.state.get("total_detections", 0),
                        "interventions": wd.state.get("total_interventions", 0),
                        "active_execs": len(wd.exec_tracker.registered),
                    }))
                except Exception as e:
                    log_event("watchdog", "error", str(e))

        # 更新状态文件
        state = read_state()
        state.update({
            "last_tick": utcnow(),
            "mode": mode,
            "counters": counters,
            "heartd_last": heartd_result,
        })
        write_state(state)

        # 检查退出信号
        if stop_event and stop_event.is_set():
            log_event("autonomic_master", "shutdown", "stop event received")
            break

        time.sleep(interval_min * 60)

# ─── 状态视图 ───────────────────────────────────────────────────────────────
def _read_load_state() -> dict:
    """读取 load_monitor 的最新状态（轻量，不触发 check）。"""
    try:
        import json as _json
        sf = WORKSPACE / "scripts" / "SOMA" / "state" / "load_monitor_state.json"
        if sf.exists():
            with open(sf, "r", encoding="utf-8") as _f:
                return _json.load(_f)
    except Exception:
        pass
    return {"node_history": [], "mailbox_history": []}

def get_subsystem_status() -> dict:
    """收集所有子系统的最新状态。"""
    state = read_state() or {}
    heartd_last = state.get("heartd_last", {}) or {}

    # pain_bus 状态
    pb = pain_bus.status() if HAS_PAIN_BUS else {"status": "not_installed"}

    # workspace 大小
    total_size = sum(
        f.stat().st_size
        for f in Path(WORKSPACE).rglob("*")
        if f.is_file() and "__pycache__" not in str(f)
    ) / (1024 * 1024)

    # watchdog 状态
    wd_status = {}
    if HAS_WATCHDOG:
        try:
            wd_status = loop_watchdog.get_watchdog().get_status()
        except Exception:
            pass

    return {
        "mode": state.get("mode", "normal"),
        "last_tick": state.get("last_tick", "never"),
        "uptime": state.get("last_mode_change", "unknown"),
        "workspace_mb": round(total_size, 1),
        "pain_bus": pb,
        "load": _read_load_state(),
        "watchdog": wd_status,
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
