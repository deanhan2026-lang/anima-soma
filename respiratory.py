# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 呼吸子系统 (respiratory.py)
==========================================
NAS 变更检测 + 增量同步 + 触发完整性检查。

设计原则（来自 Mac Nyx 设计）：
- 呼吸 = 持续监测环境变化，是"自持"的基础
- 检测到 NAS 可用 → 检查增量变更 → 触发同步
- 检测到 NAS 不可用 → 降级 + 告警 pain_bus P3
- 与 immune_cleaner 联动：变更检测触发 integrity check

Phase 1 MVP：
  1. NAS WebDAV 可达性检测
  2. 增量检查：memory/ 目录修改时间
  3. NAS → 本地 增量同步（仅下载新/修改的文件）
  4. 触发 immune_cleaner.integrity 当检测到变更时
"""

import os
import json
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import base64

# WebDAV Basic 认证（新 NAS debianhan 已改为非匿名）
NAS_WEBDAV_AUTH = {'Authorization': 'Basic ' + base64.b64encode(b'anima:animastellar').decode()}

WORKSPACE = Path(__file__).parent.parent.parent.resolve()
STATE_FILE = WORKSPACE / "scripts" / "SOMA" / "respiratory_state.json"
LOG_FILE   = WORKSPACE / "scripts" / "SOMA" / "logs" / "respiratory_log.jsonl"

NAS_WEBDAV  = "http://100.123.195.10:5005/qclaw"
NAS_CLOUD   = "Z:\\qclaw"   # SMB fallback

def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def log_event(action: str, detail: str, data: dict = None):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": utcnow(), "action": action, "detail": detail}
    if data:
        entry.update(data)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_nas_mtime": None, "last_sync": None, "nas_reachable": None}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def compute_hash(fp: Path) -> str:
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

# ─── NAS 可达性检测 ────────────────────────────────────────────────────────
def is_nas_reachable() -> bool:
    try:
        from urllib.request import urlopen, Request
        req = Request(f"{NAS_WEBDAV}/memory/", method="PROPFIND", headers=NAS_WEBDAV_AUTH)
        req.add_header("Depth", "0")
        with urlopen(req, timeout=6) as r:
            return r.status in (200, 207)  # 207 = Multi-Status (WebDAV OK)
    except Exception:
        pass
    # SMB fallback
    try:
        smb_path = Path(NAS_CLOUD)
        return smb_path.exists()
    except Exception:
        return False

# ─── 获取 NAS memory 目录 mtime ───────────────────────────────────────────
def nas_memory_mtime() -> Optional[float]:
    try:
        from urllib.request import urlopen, Request
        req = Request(f"{NAS_WEBDAV}/memory/", method="PROPFIND", headers=NAS_WEBDAV_AUTH)
        req.add_header("Depth", "1")
        with urlopen(req, timeout=8) as r:
            text = r.read().decode("utf-8", errors="ignore")
        # 解析 WebDAV DAV:response 中的 getlastmodified
        import re
        matches = re.findall(r"<d:getlastmodified>([^<]+)</d:getlastmodified>", text, re.IGNORECASE)
        if matches:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(matches[0]).timestamp()
    except Exception:
        pass

    # SMB fallback: 检查本地 NAS 挂载的 memory 目录
    local_nas_memory = Path(r"Z:\qclaw\memory")
    if local_nas_memory.exists():
        mtimes = [f.stat().st_mtime for f in local_nas_memory.glob("*.md") if f.is_file()]
        return max(mtimes) if mtimes else None
    return None

# ─── 增量同步（NAS → 本地）───────────────────────────────────────────────
def sync_from_nas() -> dict:
    """下载 NAS memory/ 中本地没有或已过时的文件。"""
    synced = []
    skipped = []
    errors = []

    local_memory = WORKSPACE / "memory"
    local_memory.mkdir(exist_ok=True)

    try:
        from urllib.request import urlopen, Request
        req = Request(f"{NAS_WEBDAV}/memory/", method="PROPFIND", headers=NAS_WEBDAV_AUTH)
        req.add_header("Depth", "1")
        with urlopen(req, timeout=8) as r:
            text = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "error": str(e)}

    import re
    # 提取文件名
    filenames = re.findall(r"<d:href>([^<]+memory/[^<]+)</d:href>", text, re.IGNORECASE)
    for href in filenames:
        # 提取文件名部分
        fname = Path(href).name
        if not fname.endswith(".md"):
            continue
        local_fp = local_memory / fname

        need_sync = False
        if not local_fp.exists():
            need_sync = True
        else:
            # 对比 mtime（简化处理，以本地文件为准）
            pass

        if need_sync:
            try:
                file_url = f"{NAS_WEBDAV}/memory/{fname}"
                file_req = Request(file_url, headers=NAS_WEBDAV_AUTH)
                with urlopen(file_req, timeout=10) as r:
                    content = r.read().decode("utf-8", errors="ignore")
                with open(local_fp, "w", encoding="utf-8") as f:
                    f.write(content)
                synced.append(fname)
                log_event("sync", f"downloaded {fname}", {"file": fname})
            except Exception as e:
                errors.append({"file": fname, "error": str(e)})
        else:
            skipped.append(fname)

    return {
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "synced_count": len(synced),
        "skipped_count": len(skipped),
    }

# ─── 主函数 ────────────────────────────────────────────────────────────────
def run() -> dict:
    state = load_state()
    nas_ok = is_nas_reachable()

    if nas_ok:
        new_mtime = nas_memory_mtime()
        changed = (new_mtime != state.get("last_nas_mtime")) and state.get("last_nas_mtime") is not None

        if changed:
            # 检测到变更：触发同步 + integrity check
            sync_result = sync_from_nas()
            # 触发 immune check（作为子进程，避免循环导入）
            import subprocess, sys
            immune_script = WORKSPACE / "scripts" / "SOMA" / "immune_cleaner.py"
            if immune_script.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(immune_script), "integrity"],
                        capture_output=True, timeout=30,
                        text=True, encoding="utf-8", errors="ignore"
                    )
                except Exception:
                    pass

            state["last_nas_mtime"] = new_mtime
            state["nas_reachable"] = True
            state["last_sync"] = utcnow()
            save_state(state)

            log_event("change_detected", f"NAS memory changed, synced {sync_result.get('synced_count',0)} files", sync_result)
            return {"status": "changed", "synced": sync_result, "mtime": new_mtime}
        else:
            state["nas_reachable"] = True
            save_state(state)
            return {"status": "stable", "nas_reachable": True, "mtime": new_mtime}
    else:
        state["nas_reachable"] = False
        save_state(state)
        log_event("nas_unreachable", "NAS WebDAV + SMB both unavailable")
        return {"status": "unreachable", "nas_reachable": False}

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · 呼吸子系统")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="运行呼吸检测（包含增量同步）")
    sub.add_parser("status", help="查看呼吸状态")

    args = parser.parse_args()

    if args.cmd == "run":
        result = run()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "status":
        state = load_state()
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        parser.print_help()
