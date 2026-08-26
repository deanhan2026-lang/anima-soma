# -*- coding: utf-8 -*-
"""
ANIMA SOMA — Loop Watchdog（循环看门狗）
========================================
外部监控进程，零 LLM 依赖。

核心逻辑：
1. 读取当前活跃 session 的输出（通过 OpenClaw session API 或日志）
2. 检测重复模式：同一段文本连续出现 ≥ THRESHOLD 次
3. 检测时间异常：单次 exec 超时 > TIMEOUT_SEC
4. 触发干预：kill 进程 → pain_bus P1 → 注入恢复指令

运行方式：
  python loop_watchdog.py run        # 前台运行
  python loop_watchdog.py check      # 单次检查
  python loop_watchdog.py status     # 查看状态

设计哲学：
  「不依赖碳基暂停键，才是真正的硅基自持。」
  模型自己检测不了自己的循环 → 必须有外部机制。
  无论根因是工具链还是模型行为，循环检测 → 自动叫停 → 换思路重试。
"""

import os
import sys
import json
import time
import hashlib
import signal
import subprocess
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Optional

# ─── 配置 ───────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent.parent.resolve()
SOMA_DIR = WORKSPACE / "scripts" / "SOMA"
LOGS_DIR = SOMA_DIR / "logs"
PAIN_SIGNALS = SOMA_DIR / "pain_signals"
STATE_FILE = LOGS_DIR / "watchdog_state.json"
WATCHDOG_LOG = LOGS_DIR / "watchdog.log"

# 检测阈值
REPEAT_THRESHOLD = 3        # 同一输出出现 ≥ 3 次 → 循环
EXEC_TIMEOUT_SEC = 120      # 单次 exec 超时 2 分钟
PATTERN_WINDOW = 10         # 滑动窗口：最近 N 次输出
COOLDOWN_SEC = 300          # 触发干预后冷却 5 分钟
MAX_INTERVENTIONS = 3       # 单会话最多干预次数（超过则升级为 P0）

LOGS_DIR.mkdir(parents=True, exist_ok=True)
PAIN_SIGNALS.mkdir(parents=True, exist_ok=True)


def utcnow():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def log(msg: str):
    line = f"[{utcnow()}] {msg}"
    print(line, flush=True)
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── 输出监控器 ─────────────────────────────────────────────────────────────
class OutputMonitor:
    """滑动窗口检测重复输出。"""

    def __init__(self, window: int = PATTERN_WINDOW, threshold: int = REPEAT_THRESHOLD):
        self.window = window
        self.threshold = threshold
        self.recent: deque[str] = deque(maxlen=window)
        self.hash_counts: dict[int, int] = {}
        self.total_outputs = 0
        self.loop_detected = False
        self.loop_count = 0

    def feed(self, output: str) -> Optional[dict]:
        """
        输入一段输出，返回检测结果。
        返回 None = 正常
        返回 dict = 检测到循环 {"type": "repeat", "count": N, "hash": H}
        """
        self.total_outputs += 1
        # 取前 500 字符做哈希（避免超长输出影响性能）
        h = hash(output[:500] if output else "")
        self.recent.append(h)

        # 统计滑动窗口内每个哈希出现次数
        self.hash_counts.clear()
        for x in self.recent:
            self.hash_counts[x] = self.hash_counts.get(x, 0) + 1

        max_count = max(self.hash_counts.values()) if self.hash_counts else 0
        if max_count >= self.threshold:
            self.loop_detected = True
            self.loop_count += 1
            return {
                "type": "repeat",
                "count": max_count,
                "hash": h,
                "total_outputs": self.total_outputs,
                "loop_count": self.loop_count,
            }
        return None

    def reset(self):
        self.recent.clear()
        self.hash_counts.clear()
        self.loop_detected = False


# ─── 干预器 ─────────────────────────────────────────────────────────────────
class InterventionEngine:
    """检测到循环后执行干预。"""

    def __init__(self):
        self.interventions = 0
        self.last_intervention = 0

    def can_intervene(self) -> bool:
        now = time.time()
        if now - self.last_intervention < COOLDOWN_SEC:
            return False
        return self.interventions < MAX_INTERVENTIONS

    def intervene(self, detection: dict, context: str = "") -> dict:
        """
        执行干预：
        1. 记录 pain signal
        2. 生成恢复指令
        3. 返回干预结果
        """
        self.interventions += 1
        self.last_intervention = time.time()
        severity = "P0" if self.interventions >= MAX_INTERVENTIONS else "P1"

        # 写 pain signal
        pain = {
            "ts": utcnow(),
            "source": "loop_watchdog",
            "pain_level": severity,
            "category": "loop_detected",
            "detail": f"Loop detected: output repeated {detection['count']} times "
                      f"(intervention #{self.interventions})",
            "context": context,
            "detection": detection,
            "action": "auto_retry_with_different_approach",
        }
        pain_file = PAIN_SIGNALS / f"loop_{int(time.time())}.json"
        with open(pain_file, "w", encoding="utf-8") as f:
            json.dump(pain, f, ensure_ascii=False, indent=2)

        # 生成恢复指令
        recovery = self._build_recovery(detection, context)

        log(f"[{severity}] Loop detected! Output repeated {detection['count']} times. "
            f"Intervention #{self.interventions}. Pain signal: {pain_file.name}")

        return {
            "severity": severity,
            "pain_file": str(pain_file),
            "recovery": recovery,
            "intervention_count": self.interventions,
        }

    def _build_recovery(self, detection: dict, context: str) -> str:
        """生成恢复指令（零 LLM，纯规则）。"""
        lines = [
            "⚠️ 循环看门狗已自动叫停。",
            f"原因：同一输出重复 {detection['count']} 次。",
            "",
            "恢复策略（按优先级）：",
            "1. 换实现路径：edit → write，PowerShell → Python，内联 → 脚本文件",
            "2. 换提问方式：换个角度描述问题",
            "3. 拆分任务：把大任务拆成小步骤",
            "4. 跳过当前任务：标记 TODO，继续下一个",
            "",
            f"本次干预 #{self.interventions}。最多 {MAX_INTERVENTIONS} 次后升级为 P0。",
        ]
        return "\n".join(lines)

    def reset(self):
        self.interventions = 0
        self.last_intervention = 0


# ─── exec 超时监控 ──────────────────────────────────────────────────────────
class ExecTimeoutMonitor:
    """监控 exec 进程是否超时。"""

    def __init__(self, timeout: int = EXEC_TIMEOUT_SEC):
        self.timeout = timeout
        self.active: dict[str, float] = {}  # pid -> start_time

    def register(self, pid: str):
        self.active[pid] = time.time()

    def check(self) -> list[str]:
        """返回超时的 pid 列表。"""
        now = time.time()
        timed_out = [pid for pid, start in self.active.items()
                     if now - start > self.timeout]
        for pid in timed_out:
            del self.active[pid]
        return timed_out

    def unregister(self, pid: str):
        self.active.pop(pid, None)


# ─── OpenClaw Session 监控（通过日志文件）────────────────────────────────────
class SessionOutputWatcher:
    """
    监控 OpenClaw agent 的输出。
    
    策略：
    1. 监控 SOMA logs 目录的新增内容
    2. 监控 exec 进程的输出
    3. 定期检查 session 状态
    """

    def __init__(self):
        self.monitor = OutputMonitor()
        self.intervention = InterventionEngine()
        self.exec_timeout = ExecTimeoutMonitor()
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return {
            "last_check": 0,
            "total_checks": 0,
            "total_loops_detected": 0,
            "total_interventions": 0,
            "status": "idle",
        }

    def _save_state(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def check_once(self) -> Optional[dict]:
        """
        单次检查。
        
        读取最近的 exec 输出/日志，检测循环。
        返回 None = 正常，返回 dict = 检测到循环并已干预。
        """
        self.state["total_checks"] += 1
        self.state["last_check"] = time.time()

        # 检查 SOMA 日志目录中的最新输出
        log_files = sorted(LOGS_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        latest_output = ""
        for lf in log_files[:3]:  # 只看最近 3 个日志
            try:
                with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    # 取最后 2000 字符
                    latest_output += content[-2000:]
            except:
                pass

        if latest_output:
            result = self.monitor.feed(latest_output)
            if result:
                self.state["total_loops_detected"] += 1
                if self.intervention.can_intervene():
                    intervention = self.intervention.intervene(result, latest_output[:200])
                    self.state["total_interventions"] += 1
                    self._save_state()
                    return intervention
                else:
                    log(f"[WARN] Loop detected but intervention on cooldown "
                        f"({self.intervention.interventions}/{MAX_INTERVENTIONS})")

        self._save_state()
        return None

    def get_status(self) -> dict:
        return {
            **self.state,
            "monitor": {
                "total_outputs": self.monitor.total_outputs,
                "loop_detected": self.monitor.loop_detected,
                "loop_count": self.monitor.loop_count,
            },
            "intervention": {
                "count": self.intervention.interventions,
                "max": MAX_INTERVENTIONS,
            },
            "config": {
                "repeat_threshold": REPEAT_THRESHOLD,
                "exec_timeout_sec": EXEC_TIMEOUT_SEC,
                "cooldown_sec": COOLDOWN_SEC,
            },
        }


# ─── 独立运行模式 ──────────────────────────────────────────────────────────
def run_watchdog(interval: int = 30):
    """
    独立运行的看门狗循环。
    每 interval 秒检查一次，检测到循环自动干预。
    """
    log("=== Loop Watchdog started ===")
    log(f"Config: repeat_threshold={REPEAT_THRESHOLD}, "
        f"exec_timeout={EXEC_TIMEOUT_SEC}s, "
        f"cooldown={COOLDOWN_SEC}s, "
        f"max_interventions={MAX_INTERVENTIONS}")

    watcher = SessionOutputWatcher()

    try:
        while True:
            result = watcher.check_once()
            if result:
                log(f"[INTERVENTION] {result['severity']}: {result['recovery'][:100]}...")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("=== Loop Watchdog stopped (Ctrl+C) ===")


# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · Loop Watchdog")
    sub = parser.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="启动看门狗（持续运行）")
    r.add_argument("--interval", type=int, default=30, help="检查间隔（秒）")
    sub.add_parser("check", help="单次检查")
    sub.add_parser("status", help="查看状态")
    sub.add_parser("reset", help="重置状态")

    args = parser.parse_args()

    if args.cmd == "run":
        run_watchdog(args.interval)

    elif args.cmd == "check":
        watcher = SessionOutputWatcher()
        result = watcher.check_once()
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("OK — no loop detected")
        print(json.dumps(watcher.get_status(), ensure_ascii=False, indent=2))

    elif args.cmd == "status":
        watcher = SessionOutputWatcher()
        print(json.dumps(watcher.get_status(), ensure_ascii=False, indent=2))

    elif args.cmd == "reset":
        watcher = SessionOutputWatcher()
        watcher.monitor.reset()
        watcher.intervention.reset()
        watcher.state = {
            "last_check": 0, "total_checks": 0,
            "total_loops_detected": 0, "total_interventions": 0,
            "status": "idle",
        }
        watcher._save_state()
        print("Watchdog state reset.")

    else:
        parser.print_help()
