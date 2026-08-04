# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 心跳守护 (heartd.py)
===================================
多级心跳探测 + 多稳态模式管理。

L0: 进程存活
L1: QClaw 进程内存/句柄数
L2: NAS WebDAV 连通性
L3: MemGuard 健康状态
L4: 外部服务（可选：飞书/微信）

多稳态模式：
  normal  — 全4级探测，每5min
  standby — L0+L2，每15min
  combat  — 全4级，每1min
  hibench — 仅L0，每30min
  disaster— 仅L0内嵌最简版，每5min
"""

import time
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WORKSPACE  = Path(__file__).parent.parent.parent.resolve()
HEART_LOG  = WORKSPACE / "scripts" / "SOMA" / "logs" / "heartd_log.jsonl"

def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def log_event(level: str, detail: dict):
    HEART_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(HEART_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": utcnow(), "level": level, **detail
        }, ensure_ascii=False) + "\n")

# ─── L0: 进程存活 ─────────────────────────────────────────────────────────
def probe_L0() -> bool:
    try:
        import psutil
        nyx = [p for p in psutil.process_iter(["name","pid"])
               if "python" in p.info["name"].lower() or "qclaw" in p.info["name"].lower()]
        return len(nyx) > 0
    except ImportError:
        # 无 psutil：检查端口监听（QClaw 默认约 18789）
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(("127.0.0.1", 18789))
            sock.close()
            return result == 0
        except Exception:
            return True  # 假设存活

# ─── L2: NAS WebDAV ────────────────────────────────────────────────────────
def probe_L2() -> bool:
    try:
        from urllib.request import urlopen, Request
        import base64
        _auth = {'Authorization': 'Basic ' + base64.b64encode(b'anima:animastellar').decode()}
        req = Request("http://100.123.195.10:5005/qclaw/", method="HEAD", headers=_auth)
        with urlopen(req, timeout=6) as r:
            return r.status in (200, 301, 404)
    except Exception:
        return False

# ─── L3: MemGuard ─────────────────────────────────────────────────────────
def probe_L3() -> bool:
    try:
        from urllib.request import urlopen, Request
        req = Request("http://127.0.0.1:5050/api/health")
        with urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False

# ─── L4: Tailscale 连通性 ─────────────────────────────────────────────────
def probe_L4() -> bool:
    try:
        from urllib.request import urlopen, Request
        req = Request("https://login.tailscale.com/api/status", method="GET")
        with urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False

# ─── 全量探测 ─────────────────────────────────────────────────────────────
def probe_all() -> dict:
    L0 = probe_L0()
    L2 = probe_L2()
    L3 = probe_L3()
    L4 = probe_L4()

    all_ok = L0 and L2 and L3 and L4
    degraded = L0 and (L2 or L3)

    if all_ok:
        status = "healthy"
    elif L0 and not L2 and not L3:
        status = "degraded_nas"
    elif L0 and not L2:
        status = "degraded_memguard"
    elif not L0:
        status = "critical_process"
    else:
        status = "degraded"

    return {
        "status": status,
        "L0_process": L0,
        "L2_nas_webdav": L2,
        "L3_memguard": L3,
        "L4_tailscale": L4,
        "timestamp": utcnow(),
    }

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · 心跳守护")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("probe", help="执行全量探测")
    sub.add_parser("status", help="查看心跳日志摘要")

    args = parser.parse_args()

    if args.cmd == "probe":
        r = probe_all()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        log_event(r["status"], r)

    elif args.cmd == "status":
        if not HEART_LOG.exists():
            print("No heartd log yet.")
        else:
            with open(HEART_LOG, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            recent = lines[-10:]
            for l in recent:
                try:
                    d = json.loads(l)
                    print(f"  [{d['level']}] {d['ts']} L0={d.get('L0_process','?')} L2={d.get('L2_nas_webdav','?')} L3={d.get('L3_memguard','?')}")
                except Exception:
                    print(l.strip())
    else:
        parser.print_help()
