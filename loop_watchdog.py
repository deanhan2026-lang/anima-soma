# -*- coding: utf-8 -*-
"""
ANIMA SOMA — Loop Watchdog v2（循环看门狗）
============================================
外部看门狗，零 LLM 依赖。监控 exec 进程的实际输出，不看日志文件。

核心逻辑：
1. 通过 process poll 读取 exec session 的实际输出（方案 A）
2. 检测重复模式：同一段文本在滑动窗口内出现 ≥ REPEAT_THRESHOLD 次
3. 检测工具调用序列：连续 N 次相同调用 → 循环（方案 B）
4. 触发干预：记录 pain signal → 生成恢复指令 → 写入恢复文件

运行方式：
  python loop_watchdog.py run        # 前台运行
  python loop_watchdog.py check      # 单次检查
  python loop_watchdog.py status     # 查看状态
  python loop_watchdog.py reset      # 重置状态

设计哲学：
  「不依赖碳基暂停键，才是真正的硅基自持。」
  模型自己检测不了自己的循环 → 必须有外部机制。

v2 vs v1：
  - v1 监控日志文件 → 误报（GBK 乱码触发检测）
  - v2 监控 exec session 的 process poll 输出（实际输出，不是日志）
  - v2 使用 SHA-256 前 128 bit（hex 32 字符）替代 Python hash()
  - v2 集成到 autonomic_master.py 作为子模块
"""

import os
import sys
import json
import time
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Optional, List, Dict, Any

# ─── 配置 ───────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent.parent.resolve()
SOMA_DIR = WORKSPACE / "scripts" / "SOMA"
LOGS_DIR = SOMA_DIR / "logs"
PAIN_SIGNALS = SOMA_DIR / "pain_signals"
STATE_FILE = LOGS_DIR / "watchdog_state.json"
WATCHDOG_LOG = LOGS_DIR / "watchdog.log"

# 检测阈值（来自设计方案）
REPEAT_THRESHOLD = 3        # 同一输出出现 ≥ 3 次 → 循环
SAME_TOOL_THRESHOLD = 5     # 同一工具调用 ≥ 5 次
EXEC_TIMEOUT_SEC = 60       # 单次 exec 默认超时
PATTERN_WINDOW = 10         # 滑动窗口：最近 N 次输出
COOLDOWN_SEC = 300          # 触发干预后冷却 5 分钟
MAX_INTERVENTIONS = 3       # 单会话最多干预次数（超过则升级为 P0）

LOGS_DIR.mkdir(parents=True, exist_ok=True)
PAIN_SIGNALS.mkdir(parents=True, exist_ok=True)


# ─── 工具函数 ───────────────────────────────────────────────────────────────
def utcnow() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def stable_hash(text: str) -> str:
    """
    稳定哈希：SHA-256 前 128 bit（32 字符 hex）。
    用 SHA-256 而非 Python hash()，因为 hash() 在不同进程间不一致。
    """
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]


def log(msg: str):
    line = f"[{utcnow()}] {msg}"
    print(line, flush=True)
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── 核心检测器 ─────────────────────────────────────────────────────────────
class OutputDetector:
    """
    滑动窗口检测重复输出。
    
    使用 SHA-256 哈希（跨进程稳定），滑动窗口内计数。
    窗口大小 = PATTERN_WINDOW，阈值 = REPEAT_THRESHOLD。
    """

    def __init__(self, window: int = PATTERN_WINDOW, threshold: int = REPEAT_THRESHOLD):
        self.window = window
        self.threshold = threshold
        self.recent_hashes: deque = deque(maxlen=window)
        self.total_fed = 0
        self.detections: List[dict] = []

    def feed(self, output: str) -> Optional[dict]:
        """
        输入一段输出，返回检测结果。
        返回 None = 正常
        返回 dict = 检测到循环
        """
        self.total_fed += 1
        # 取前 500 字符做哈希（避免超长输出影响性能）
        h = stable_hash(output[:500] if output else "")
        self.recent_hashes.append(h)

        # 统计滑动窗口内每个哈希出现次数
        counts: Dict[str, int] = {}
        for x in self.recent_hashes:
            counts[x] = counts.get(x, 0) + 1

        max_count = max(counts.values()) if counts else 0
        if max_count >= self.threshold:
            detection = {
                "type": "repeat",
                "count": max_count,
                "hash": h,
                "total_fed": self.total_fed,
                "detection_index": len(self.detections),
                "ts": utcnow(),
            }
            self.detections.append(detection)
            return detection
        return None

    def reset(self):
        self.recent_hashes.clear()
        self.detections.clear()
        self.total_fed = 0


class ToolCallDetector:
    """
    检测工具调用序列循环。
    
    记录每次工具调用 (tool_name, params_hash)。
    连续 SAME_TOOL_THRESHOLD 次相同调用 → 循环。
    """

    def __init__(self, threshold: int = SAME_TOOL_THRESHOLD):
        self.threshold = threshold
        self.call_history: deque = deque(maxlen=threshold * 2)
        self.total_calls = 0
        self.detections: List[dict] = []

    def record(self, tool_name: str, params_hash: str) -> Optional[dict]:
        """
        记录一次工具调用，返回检测结果。
        """
        self.total_calls += 1
        key = f"{tool_name}:{params_hash}"
        self.call_history.append(key)

        # 检查最近 threshold 次是否完全相同
        recent = list(self.call_history)[-self.threshold:]
        if len(recent) >= self.threshold and len(set(recent)) == 1:
            detection = {
                "type": "same_tool",
                "tool": tool_name,
                "count": self.threshold,
                "total_calls": self.total_calls,
                "detection_index": len(self.detections),
                "ts": utcnow(),
            }
            self.detections.append(detection)
            return detection
        return None

    def reset(self):
        self.call_history.clear()
        self.detections.clear()
        self.total_calls = 0


class ExecTimeoutTracker:
    """
    追踪 exec 进程超时。
    
    通过 process poll 获取活跃 session 列表，
    检查运行时间是否超过 EXEC_TIMEOUT_SEC。
    """

    def __init__(self, timeout_sec: int = EXEC_TIMEOUT_SEC):
        self.timeout_sec = timeout_sec
        self.registered: Dict[str, float] = {}  # session_id -> start_time

    def register(self, session_id: str):
        self.registered[session_id] = time.time()

    def unregister(self, session_id: str):
        self.registered.pop(session_id, None)

    def check_timeouts(self) -> List[str]:
        """返回超时的 session_id 列表。"""
        now = time.time()
        timed_out = [
            sid for sid, start in self.registered.items()
            if now - start > self.timeout_sec
        ]
        # 自动清理已超时的
        for sid in timed_out:
            del self.registered[sid]
        return timed_out

    def get_active(self) -> List[dict]:
        """返回当前活跃进程及其运行时间。"""
        now = time.time()
        return [
            {"session_id": sid, "elapsed_sec": round(now - start, 1)}
            for sid, start in self.registered.items()
        ]


# ─── 干预引擎 ──────────────────────────────────────────────────────────────
class InterventionEngine:
    """
    检测到循环后执行干预。
    
    干预动作：
    1. 写 pain signal 到 pain_signals/ 目录
    2. 生成恢复指令（零 LLM，纯规则）
    3. 写入恢复文件供下次心跳读取
    
    不做的事：
    - 不 kill agent session（OpenClaw 管理）
    - 不替模型做决策
    """

    def __init__(self):
        self.intervention_count = 0
        self.last_intervention_ts = 0.0
        self.history: List[dict] = []

    def can_intervene(self) -> bool:
        now = time.time()
        if now - self.last_intervention_ts < COOLDOWN_SEC:
            return False
        return self.intervention_count < MAX_INTERVENTIONS

    def intervene(self, detection: dict, context: str = "") -> dict:
        self.intervention_count += 1
        self.last_intervention_ts = time.time()
        severity = "P0" if self.intervention_count >= MAX_INTERVENTIONS else "P1"

        # 写 pain signal
        pain = {
            "ts": utcnow(),
            "source": "loop_watchdog",
            "pain_level": severity,
            "category": "loop_detected",
            "detail": self._build_detail(detection),
            "context": context[:500] if context else "",
            "detection": detection,
            "action": "auto_retry_with_different_approach",
            "intervention_number": self.intervention_count,
        }
        pain_file = PAIN_SIGNALS / f"loop_{int(time.time())}.json"
        pain_file.parent.mkdir(parents=True, exist_ok=True)
        with open(pain_file, "w", encoding="utf-8") as f:
            json.dump(pain, f, ensure_ascii=False, indent=2)

        # 生成恢复指令
        recovery = self._build_recovery(detection)

        log(f"[{severity}] Loop detected! {self._build_detail(detection)}. "
            f"Intervention #{self.intervention_count}. Pain: {pain_file.name}")

        result = {
            "severity": severity,
            "pain_file": str(pain_file),
            "recovery": recovery,
            "intervention_count": self.intervention_count,
            "detail": self._build_detail(detection),
        }
        self.history.append(result)
        return result

    def _build_detail(self, detection: dict) -> str:
        dtype = detection.get("type", "unknown")
        if dtype == "repeat":
            return f"Output repeated {detection['count']} times (hash={detection['hash'][:16]}...)"
        elif dtype == "same_tool":
            return f"Same tool '{detection['tool']}' called {detection['count']} times"
        elif dtype == "timeout":
            return f"Exec session '{detection.get('session_id')}' timed out after {detection.get('elapsed_sec')}s"
        return f"Unknown detection type: {dtype}"

    def _build_recovery(self, detection: dict) -> str:
        dtype = detection.get("type", "unknown")
        lines = [
            "⚠️ Loop Watchdog v2 已自动叫停。",
            f"检测类型: {dtype}",
            f"详情: {self._build_detail(detection)}",
            "",
            "恢复策略（按优先级）：",
        ]

        if dtype == "repeat":
            lines.extend([
                "1. 换实现路径：edit → write，PowerShell → Python，内联 → 脚本文件",
                "2. 检查是否陷入了相同的错误处理逻辑",
                "3. 拆分任务：把大任务拆成小步骤",
                "4. 跳过当前步骤，标记 TODO",
            ])
        elif dtype == "same_tool":
            lines.extend([
                "1. 该工具调用已反复失败，停止调用",
                "2. 换一个工具或方法实现相同目标",
                "3. 重新评估任务分解方式",
            ])
        elif dtype == "timeout":
            lines.extend([
                "1. 命令可能卡死，考虑用 timeout 参数限制",
                "2. 检查命令是否需要交互式输入",
                "3. 简化命令或拆分为多步",
            ])

        lines.extend([
            "",
            f"本次干预 #{self.intervention_count}/{MAX_INTERVENTIONS}。",
            f"冷却期: {COOLDOWN_SEC}s。",
        ])
        return "\n".join(lines)

    def reset(self):
        self.intervention_count = 0
        self.last_intervention_ts = 0.0
        self.history.clear()


# ─── 看门狗主类 ─────────────────────────────────────────────────────────────
class LoopWatchdog:
    """
    Loop Watchdog v2 主类。
    
    设计方案 A（exec session 输出监控）+ 方案 B（工具调用序列监控）。
    方案 C（SOMA 状态文件）作为 autonomic_master.py 中的额外检查。
    """

    def __init__(self):
        self.output_detector = OutputDetector()
        self.tool_detector = ToolCallDetector()
        self.exec_tracker = ExecTimeoutTracker()
        self.intervention = InterventionEngine()
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "last_check": 0,
            "total_checks": 0,
            "total_detections": 0,
            "total_interventions": 0,
            "status": "idle",
            "created_at": utcnow(),
        }

    def _save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ── 方案 A: exec session 输出监控 ────────────────────────────────────
    def feed_exec_output(self, session_id: str, output: str) -> Optional[dict]:
        """
        输入 exec session 的实际输出。
        调用方（autonomic_master）负责通过 process poll 获取输出。
        
        返回 None = 正常，返回 dict = 检测到循环并可能已干预。
        """
        detection = self.output_detector.feed(output)
        if detection is None:
            return None

        # 检测到重复输出 → 尝试干预
        self.state["total_detections"] += 1
        if self.intervention.can_intervene():
            result = self.intervention.intervene(detection, output[:300])
            self.state["total_interventions"] += 1
            self._save_state()
            return result
        else:
            log(f"[WARN] Output loop detected but intervention on cooldown "
                f"({self.intervention.intervention_count}/{MAX_INTERVENTIONS})")
            self._save_state()
            return {"severity": "WARN", "detection": detection, "intervention": "cooldown"}

    # ── 方案 B: 工具调用序列监控 ────────────────────────────────────────
    def record_tool_call(self, tool_name: str, params: Any) -> Optional[dict]:
        """
        记录一次工具调用。
        params 会被哈希为稳定的 fingerprint。
        
        返回 None = 正常，返回 dict = 检测到循环。
        """
        params_str = json.dumps(params, sort_keys=True, ensure_ascii=False) if params else ""
        params_hash = stable_hash(params_str)

        detection = self.tool_detector.record(tool_name, params_hash)
        if detection is None:
            return None

        self.state["total_detections"] += 1
        if self.intervention.can_intervene():
            result = self.intervention.intervene(detection, f"{tool_name}({params_str[:200]})")
            self.state["total_interventions"] += 1
            self._save_state()
            return result
        else:
            self._save_state()
            return {"severity": "WARN", "detection": detection, "intervention": "cooldown"}

    # ── exec 超时追踪 ───────────────────────────────────────────────────
    def register_exec(self, session_id: str):
        """注册一个 exec session 开始运行。"""
        self.exec_tracker.register(session_id)

    def unregister_exec(self, session_id: str):
        """exec session 完成，取消注册。"""
        self.exec_tracker.unregister(session_id)

    def check_exec_timeouts(self) -> List[dict]:
        """检查是否有 exec 超时，返回干预结果列表。"""
        timed_out = self.exec_tracker.check_timeouts()
        results = []
        for sid in timed_out:
            detection = {
                "type": "timeout",
                "session_id": sid,
                "elapsed_sec": self.exec_tracker.timeout_sec,
                "ts": utcnow(),
            }
            self.state["total_detections"] += 1
            if self.intervention.can_intervene():
                result = self.intervention.intervene(detection)
                self.state["total_interventions"] += 1
                results.append(result)
        if results:
            self._save_state()
        return results

    # ── 综合检查 ────────────────────────────────────────────────────────
    def check_once(self) -> Optional[dict]:
        """
        单次综合检查。
        
        方案 A：由调用方通过 feed_exec_output() 注入数据。
        方案 C（兜底）：检查 SOMA 状态文件异常。
        
        此方法执行方案 C 检查 + 超时检查。
        """
        self.state["total_checks"] += 1
        self.state["last_check"] = time.time()

        # 超时检查
        timeout_results = self.check_exec_timeouts()
        if timeout_results:
            self._save_state()
            return timeout_results[0]

        # 方案 C：检查 pain_signals 异常增长
        pain_files = list(PAIN_SIGNALS.glob("loop_*.json"))
        if len(pain_files) > 10:
            log(f"[WARN] {len(pain_files)} loop pain signals accumulated — "
                "possible unresolved loop")

        self.state["status"] = "healthy"
        self._save_state()
        return None

    # ── 状态查询 ────────────────────────────────────────────────────────
    def get_status(self) -> dict:
        return {
            "state": self.state,
            "output_detector": {
                "total_fed": self.output_detector.total_fed,
                "window": self.output_detector.window,
                "threshold": self.output_detector.threshold,
                "recent_count": len(self.output_detector.recent_hashes),
                "detections": len(self.output_detector.detections),
            },
            "tool_detector": {
                "total_calls": self.tool_detector.total_calls,
                "threshold": self.tool_detector.threshold,
                "history_count": len(self.tool_detector.call_history),
                "detections": len(self.tool_detector.detections),
            },
            "exec_tracker": {
                "timeout_sec": self.exec_tracker.timeout_sec,
                "active_count": len(self.exec_tracker.registered),
                "active": self.exec_tracker.get_active(),
            },
            "intervention": {
                "count": self.intervention.intervention_count,
                "max": MAX_INTERVENTIONS,
                "cooldown_sec": COOLDOWN_SEC,
                "history": self.intervention.history[-5:],  # 最近 5 次
            },
            "config": {
                "repeat_threshold": REPEAT_THRESHOLD,
                "same_tool_threshold": SAME_TOOL_THRESHOLD,
                "exec_timeout_sec": EXEC_TIMEOUT_SEC,
                "cooldown_sec": COOLDOWN_SEC,
                "max_interventions": MAX_INTERVENTIONS,
            },
        }

    def reset(self):
        """重置所有状态。"""
        self.output_detector.reset()
        self.tool_detector.reset()
        self.exec_tracker.registered.clear()
        self.intervention.reset()
        self.state = {
            "last_check": 0,
            "total_checks": 0,
            "total_detections": 0,
            "total_interventions": 0,
            "status": "idle",
            "created_at": utcnow(),
        }
        self._save_state()
        log("Watchdog state reset.")


# ─── 模块级单例（供 autonomic_master.py 直接 import）────────────────────────
_global_watchdog: Optional[LoopWatchdog] = None


def get_watchdog() -> LoopWatchdog:
    """获取全局 watchdog 单例。"""
    global _global_watchdog
    if _global_watchdog is None:
        _global_watchdog = LoopWatchdog()
    return _global_watchdog


# ─── 独立运行模式 ──────────────────────────────────────────────────────────
def run_watchdog_loop(interval: int = 30):
    """
    独立运行的看门狗循环。
    每 interval 秒检查一次。
    
    注意：独立运行时只能做方案 C（状态文件检查），
    方案 A/B 需要 autonomic_master.py 在心跳中调用。
    """
    log("=== Loop Watchdog v2 started (standalone) ===")
    log(f"Config: repeat={REPEAT_THRESHOLD}, tool={SAME_TOOL_THRESHOLD}, "
        f"timeout={EXEC_TIMEOUT_SEC}s, cooldown={COOLDOWN_SEC}s, "
        f"max_interventions={MAX_INTERVENTIONS}")

    wd = get_watchdog()

    try:
        while True:
            result = wd.check_once()
            if result:
                log(f"[INTERVENTION] {result.get('severity', '?')}: "
                    f"{result.get('detail', str(result)[:100])}")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("=== Loop Watchdog v2 stopped (Ctrl+C) ===")


# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · Loop Watchdog v2")
    sub = parser.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="启动看门狗（持续运行）")
    r.add_argument("--interval", type=int, default=30, help="检查间隔（秒）")

    sub.add_parser("check", help="单次检查（方案 C 兜底）")
    sub.add_parser("status", help="查看状态")
    sub.add_parser("reset", help="重置状态")

    # 用于测试：喂入一段输出
    feed = sub.add_parser("feed", help="喂入输出进行检测")
    feed.add_argument("text", help="输出文本")

    args = parser.parse_args()

    if args.cmd == "run":
        run_watchdog_loop(args.interval)

    elif args.cmd == "check":
        wd = get_watchdog()
        result = wd.check_once()
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("OK — no loop detected")
        print("---")
        print(json.dumps(wd.get_status(), ensure_ascii=False, indent=2))

    elif args.cmd == "status":
        wd = get_watchdog()
        print(json.dumps(wd.get_status(), ensure_ascii=False, indent=2))

    elif args.cmd == "reset":
        wd = get_watchdog()
        wd.reset()
        print("Watchdog v2 state reset.")

    elif args.cmd == "feed":
        wd = get_watchdog()
        result = wd.feed_exec_output("cli-test", args.text)
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"OK — fed '{args.text[:50]}...' (no loop)")

    else:
        parser.print_help()
