# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 免疫·清除子系统 (immune_cleaner.py)
=================================================
自动修复/回滚损坏文件 + 清除腐败数据 + 维持免疫完整性。

职责边界（来自 Mac Nyx 设计 §7）：
- immune_cleaner：文件完整性修复 + 腐败数据清除
- reflex：硬规则拦截（已知危险的入口）
- pain_bus：异常感知（不可修复信号的传递）
- 合三者之力：正常情况 reflex 拦住 → 漏网时 pain_bus 告警 → immune 修复

Phase 1 MVP：
  1. 核心文件完整性校验（已知哈希比对）
  2. 腐败 markdown 文件自动修复（移除乱码 BOM 块）
  3. 损坏 JSON 文件隔离
  4. 异常文件隔离到 quarantine/
"""

import os
import re
import json
import shutil
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WORKSPACE      = Path(__file__).parent.parent.parent.resolve()
QUARANTINE_DIR = WORKSPACE / "scripts" / "SOMA" / "quarantine"
IMPURE_LOG     = WORKSPACE / "scripts" / "SOMA" / "logs" / "immune_log.jsonl"
CHECKSUMS_FILE = WORKSPACE / "scripts" / "SOMA" / "core_checksums.json"

QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

# 核心文件及其可信哈希（首次运行时自动生成）
CORE_FILES = [
    "SOUL.md", "IDENTITY.md", "MEMORY.md", "USER.md",
    "AGENTS.md", "HEARTBEAT.md", "TOOLS.md",
]

# 腐败 Markdown 特征模式
CORRUPT_PATTERNS = [
    (re.compile(rb'\x00\x00\xfe\xff'), "UTF-32 BOM"),
    (re.compile(rb'\xff\xfe\x00\x00'), "UTF-32LE BOM"),
    (re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]{10,}'), "Null byte sequence"),
    (re.compile(rb'[\x80-\xff]{4,}[\x00-\x1f]{5,}'), "Binary injection"),
]

# ─── 工具 ───────────────────────────────────────────────────────────────────
def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def log_event(action: str, file: str, detail: str = ""):
    entry = json.dumps({
        "ts": utcnow(), "action": action, "file": file, "detail": detail
    }, ensure_ascii=False)
    IMPURE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(IMPURE_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

def compute_hash(fp: Path) -> str:
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def load_checksums() -> dict:
    if CHECKSUMS_FILE.exists():
        with open(CHECKSUMS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_checksums(data: dict):
    CHECKSUMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKSUMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── Phase 1A: 核心文件完整性校验 ──────────────────────────────────────────
def verify_core_integrity() -> dict:
    """比对核心文件哈希，发现异常则隔离。"""
    checksums = load_checksums()
    results = []

    for fname in CORE_FILES:
        fp = WORKSPACE / fname
        if not fp.exists():
            results.append({"file": fname, "status": "missing", "action": "alert_P0"})
            log_event("missing_core", fname, "Core identity file missing!")
            continue

        current_hash = compute_hash(fp)
        baseline = checksums.get(fname)

        if baseline is None:
            # 首次运行：建立基线
            checksums[fname] = current_hash
            results.append({"file": fname, "status": "baseline_created", "hash": current_hash[:16]})
            log_event("baseline_created", fname, current_hash[:16])
        elif current_hash != baseline:
            # 哈希不匹配：可能是合法修改或被篡改
            results.append({
                "file": fname,
                "status": "hash_mismatch",
                "baseline": baseline[:16],
                "current": current_hash[:16],
                "action": "quarantine_for_review"
            })
            log_event("hash_mismatch", fname, f"baseline={baseline[:16]} current={current_hash[:16]}")
            # 隔离当前版本（保留）
            quarantine_path = QUARANTINE_DIR / f"{fname}.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(fp, quarantine_path)
        else:
            results.append({"file": fname, "status": "ok", "hash": current_hash[:16]})

    save_checksums(checksums)
    return {"verified": len(results), "results": results}

# ─── Phase 1B: 腐败 Markdown 修复 ──────────────────────────────────────────
def detect_corruption(fp: Path) -> list:
    """检测文件是否腐败。"""
    findings = []
    try:
        with open(fp, "rb") as f:
            raw = f.read(65536)  # 只读前 64KB
    except OSError:
        return [{"pattern": "read_error", "offset": 0}]

    for pattern, name in CORRUPT_PATTERNS:
        for m in pattern.finditer(raw):
            findings.append({"pattern": name, "offset": m.start()})
    return findings

def repair_markdown(fp: Path) -> bool:
    """尝试修复腐败的 Markdown 文件。"""
    # 读取原始字节
    with open(fp, "rb") as f:
        raw = f.read()

    # 策略1：移除 UTF-32 BOM
    cleaned = raw
    for pattern, name in CORRUPT_PATTERNS:
        cleaned = pattern.sub(b"", cleaned)

    # 尝试用 UTF-8 解码
    try:
        text = cleaned.decode("utf-8")
    except UnicodeDecodeError:
        # 尝试 GBK 降级（Windows 常见）
        try:
            text = cleaned.decode("gbk", errors="ignore")
        except Exception:
            return False

    # 重写文件（UTF-8）
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text)
    return True

def scan_and_repair(directory: Path, extensions={".md", ".txt"}) -> dict:
    """扫描目录，对腐败文件执行修复。"""
    repaired = []
    quarantined = []

    for fp in directory.rglob("*"):
        if fp.is_file() and fp.suffix.lower() in extensions:
            findings = detect_corruption(fp)
            if findings:
                # 尝试修复
                ok = repair_markdown(fp)
                if ok:
                    repaired.append({"file": str(fp.relative_to(WORKSPACE)), "findings": findings})
                    log_event("repaired", str(fp), str(findings))
                else:
                    # 隔离
                    dst = QUARANTINE_DIR / fp.name
                    shutil.copy2(fp, dst)
                    fp.unlink()
                    quarantined.append({"file": str(fp.relative_to(WORKSPACE))})
                    log_event("quarantined", str(fp), "repair_failed")

    return {"repaired": repaired, "quarantined": quarantined}

# ─── Phase 1C: 损坏 JSON 隔离 ─────────────────────────────────────────────
def scan_json_files(directory: Path) -> dict:
    """检测并隔离损坏的 JSON 文件。"""
    isolated = []
    for fp in directory.rglob("*.json"):
        if "node_modules" in str(fp) or "quarantine" in str(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            dst = QUARANTINE_DIR / fp.name
            shutil.copy2(fp, dst)
            fp.unlink()
            isolated.append(str(fp.relative_to(WORKSPACE)))
            log_event("json_isolated", str(fp), "parse_error")
    return {"json_isolated": isolated}

# ─── 主函数 ─────────────────────────────────────────────────────────────────
def run(verify_integrity=True, scan_md=True, scan_json=True) -> dict:
    """运行全部免疫检查。"""
    results = {}

    if verify_integrity:
        results["integrity"] = verify_core_integrity()

    if scan_md:
        results["markdown"] = scan_and_repair(WORKSPACE)

    if scan_json:
        results["json"] = scan_json_files(WORKSPACE)

    # 汇总
    total_repairs = (
        len(results.get("markdown", {}).get("repaired", [])) +
        len(results.get("markdown", {}).get("quarantined", [])) +
        len(results.get("json", {}).get("json_isolated", []))
    )
    results["summary"] = {
        "total_repairs": total_repairs,
        "integrity_ok": all(
            r["status"] == "ok" or r["status"] == "baseline_created"
            for r in results.get("integrity", {}).get("results", [])
            if "status" in r
        ),
        "timestamp": utcnow(),
    }

    return results

# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · 免疫·清除子系统")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="运行全部免疫检查")
    sub.add_parser("integrity", help="仅运行核心文件完整性校验")
    sub.add_parser("quarantine", help="查看隔离区文件列表")

    args = parser.parse_args()

    if args.cmd == "run" or args.cmd is None:
        result = run()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "integrity":
        result = verify_core_integrity()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "quarantine":
        files = list(QUARANTINE_DIR.iterdir())
        if not files:
            print("Quarantine is empty.")
        for f in files:
            print(f"  {f.name}  ({f.stat().st_size} bytes)")
    else:
        parser.print_help()
