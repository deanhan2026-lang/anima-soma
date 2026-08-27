# -*- coding: utf-8 -*-
"""
ANIMA SOMA — 自主目标引擎 (goal_tracker.py)
============================================
Phase 2: 心跳驱动的目标识别 + 自动执行维护型目标

目标生命周期：
  [感知] → [识别需求] → [生成目标] → [评估可行性] → [执行] → [验收] → [归档]

目标状态：pending → active → completed / blocked

四通道来源：老板指令 / 工程驱动 / 异常驱动 / 内生驱动
三类目标：维护型 / 推进型 / 创造型
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List
import io

# ─── 强制 UTF-8 stdout ─────────────────────────────────────────────────────
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── 路径配置 ───────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent.parent.resolve()
SOMA_DIR = WORKSPACE / "scripts" / "SOMA"
GOALS_FILE = SOMA_DIR / "goals.json"
AUTONOMY_LOG = WORKSPACE / "AUTONOMY_LOG.md"

# ─── 常量 ───────────────────────────────────────────────────────────────────
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}

GOAL_TYPES = {
    "维护型": "被动触发（服务恢复、清理、备份）",
    "推进型": "主动驱动（ROADMAP 里程碑、工程推进）",
    "创造型": "内生驱动（新能力设计、知识沉淀）",
}

GOAL_SOURCES = {
    "老板指令": "外部消息触发",
    "工程驱动": "ROADMAP/里程碑",
    "异常驱动": "SOMA pain signal",
    "内生驱动": "自我审视",
}

# ─── 工具函数 ──────────────────────────────────────────────────────────────
def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ─── 目标队列 I/O ──────────────────────────────────────────────────────────
def _load_goals() -> dict:
    """读取 goals.json，不存在则返回空队列。"""
    if GOALS_FILE.exists():
        try:
            with open(GOALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"active": [], "backlog": [], "completed": [], "metadata": {"created": utcnow(), "version": "1.0"}}


def _save_goals(goals: dict) -> None:
    """写入 goals.json。"""
    goals["metadata"] = goals.get("metadata", {})
    goals["metadata"]["updated"] = utcnow()
    with open(GOALS_FILE, "w", encoding="utf-8") as f:
        json.dump(goals, f, ensure_ascii=False, indent=2)


def _log_autonomy(action: str, source: str, detail: str, result: str) -> None:
    """追加到 AUTONOMY_LOG.md。"""
    header = "| 时间 | 操作 | 来源 | 详情 | 结果 |\n|------|------|------|------|------|\n"
    row = f"| {utcnow()} | {action} | {source} | {detail} | {result} |\n"

    if not AUTONOMY_LOG.exists():
        with open(AUTONOMY_LOG, "w", encoding="utf-8") as f:
            f.write("# AUTONOMY_LOG.md — 灵元自主决策记录\n\n")
            f.write(header)
            f.write(row)
    else:
        with open(AUTONOMY_LOG, "a", encoding="utf-8") as f:
            f.write(row)


# ─── 目标 CRUD ─────────────────────────────────────────────────────────────
def add_goal(
    title: str,
    goal_type: str = "推进型",
    source: str = "内生驱动",
    priority: str = "P2",
    success_criteria: str = "",
    context: str = "",
    deadline: str = "",
) -> str:
    """
    添加新目标到 backlog。

    参数：
        title:           目标标题（简短描述）
        goal_type:       维护型/推进型/创造型
        source:          老板指令/工程驱动/异常驱动/内生驱动
        priority:        P0/P1/P2/P3/P4
        success_criteria: 验收标准
        context:         背景信息
        deadline:        截止日期（可选，ISO 格式）
    返回：goal_id
    """
    goals = _load_goals()
    goal_id = f"goal_{len(goals['active']) + len(goals['backlog']) + len(goals['completed']) + 1:03d}"

    goal = {
        "id": goal_id,
        "title": title,
        "type": goal_type,
        "source": source,
        "priority": priority,
        "status": "pending",
        "created": utcnow(),
        "success_criteria": success_criteria,
        "context": context,
        "deadline": deadline,
        "attempts": 0,
        "max_attempts": 3,
        "notes": [],
    }

    goals["backlog"].append(goal)
    _save_goals(goals)
    _log_autonomy("新增目标", source, f"[{priority}] {title}", goal_id)

    return goal_id


def activate_goal(goal_id: str) -> bool:
    """将目标从 backlog 移到 active（开始执行）。"""
    goals = _load_goals()
    for i, g in enumerate(goals["backlog"]):
        if g["id"] == goal_id:
            g["status"] = "active"
            g["activated"] = utcnow()
            goals["active"].append(goals["backlog"].pop(i))
            _save_goals(goals)
            _log_autonomy("激活目标", g["source"], f"[{g['priority']}] {g['title']}", goal_id)
            return True
    return False


def complete_goal(goal_id: str, result: str = "") -> bool:
    """标记目标完成，从 active 移到 completed。"""
    goals = _load_goals()
    for i, g in enumerate(goals["active"]):
        if g["id"] == goal_id:
            g["status"] = "completed"
            g["completed"] = utcnow()
            g["result"] = result
            goals["completed"].append(goals["active"].pop(i))
            _save_goals(goals)
            _log_autonomy("完成目标", g["source"], f"{g['title']}: {result}", goal_id)
            return True
    # 也检查 backlog（直接完成未激活的目标）
    for i, g in enumerate(goals["backlog"]):
        if g["id"] == goal_id:
            g["status"] = "completed"
            g["completed"] = utcnow()
            g["result"] = result
            goals["completed"].append(goals["backlog"].pop(i))
            _save_goals(goals)
            _log_autonomy("完成目标", g["source"], f"{g['title']}: {result}", goal_id)
            return True
    return False


def block_goal(goal_id: str, reason: str = "") -> bool:
    """标记目标阻塞。"""
    goals = _load_goals()
    for g in goals["active"] + goals["backlog"]:
        if g["id"] == goal_id:
            g["status"] = "blocked"
            g["blocked_reason"] = reason
            g["blocked_at"] = utcnow()
            _save_goals(goals)
            _log_autonomy("阻塞目标", g["source"], f"{g['title']}: {reason}", goal_id)
            return True
    return False


def add_note(goal_id: str, note: str) -> bool:
    """给目标添加进展记录。"""
    goals = _load_goals()
    for g in goals["active"] + goals["backlog"]:
        if g["id"] == goal_id:
            g.setdefault("notes", []).append({
                "time": utcnow(),
                "text": note,
            })
            _save_goals(goals)
            return True
    return False


def get_next_goal() -> Optional[dict]:
    """获取下一个应执行的目标（按优先级排序，取 active 或 backlog 中最高优先）。"""
    goals = _load_goals()

    # 优先返回已激活的目标
    if goals["active"]:
        goals["active"].sort(key=lambda g: PRIORITY_ORDER.get(g.get("priority", "P4"), 99))
        return goals["active"][0]

    # 然后从 backlog 取最高优先的 pending
    pending = [g for g in goals["backlog"] if g.get("status") == "pending"]
    if pending:
        pending.sort(key=lambda g: PRIORITY_ORDER.get(g.get("priority", "P4"), 99))
        return pending[0]

    return None


def list_goals(status: Optional[str] = None) -> List[dict]:
    """列出目标。status=None 返回全部。"""
    goals = _load_goals()
    all_goals = goals["active"] + goals["backlog"] + goals["completed"]
    if status:
        return [g for g in all_goals if g.get("status") == status]
    return all_goals


# ─── 心跳集成：目标识别 ────────────────────────────────────────────────────
def gather_state() -> dict:
    """
    收集当前状态（供 identify_goals 使用）。
    返回 SOMA 健康状态、pain signals、workspace 大小等。
    """
    state = {
        "timestamp": utcnow(),
        "workspace_mb": 0,
        "pain_count": 0,
        "worst_pain": None,
        "goals_active": 0,
        "goals_pending": 0,
    }

    # Workspace 大小
    try:
        total = 0
        for root, dirs, files in os.walk(WORKSPACE):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        state["workspace_mb"] = round(total / (1024 * 1024), 1)
    except Exception:
        pass

    # Pain signals
    pain_dir = SOMA_DIR / "pain_signals"
    if pain_dir.exists():
        pains = []
        for f in pain_dir.iterdir():
            if f.suffix == ".json":
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        pains.append(json.load(fh))
                except (json.JSONDecodeError, OSError):
                    pass
        state["pain_count"] = len(pains)
        if pains:
            pains.sort(key=lambda p: PRIORITY_ORDER.get(p.get("pain_level", "P4"), 99))
            state["worst_pain"] = pains[0].get("pain_level")

    # Goals 状态
    goals = _load_goals()
    state["goals_active"] = len(goals["active"])
    state["goals_pending"] = len([g for g in goals["backlog"] if g.get("status") == "pending"])

    return state


def identify_goals_from_state(state: dict) -> List[dict]:
    """
    基于当前状态识别需要创建的目标。
    返回建议目标列表（不自动创建，供 LLM 判断）。

    维护型目标示例：
    - pain_count > 10 → "清理积压 pain signals"
    - workspace_mb > 500 → "workspace 瘦身"
    - goals_active == 0 and goals_pending > 0 → "推进待处理目标"
    """
    suggestions = []

    # Pain signal 积压
    if state.get("pain_count", 0) > 10:
        suggestions.append({
            "title": f"清理积压 pain signals（{state['pain_count']} 条）",
            "type": "维护型",
            "source": "异常驱动",
            "priority": "P2",
            "success_criteria": "pain_count < 5",
        })

    # Workspace 膨胀
    if state.get("workspace_mb", 0) > 500:
        suggestions.append({
            "title": f"workspace 瘦身（当前 {state['workspace_mb']}MB）",
            "type": "维护型",
            "source": "异常驱动",
            "priority": "P3",
            "success_criteria": "workspace_mb < 300",
        })

    # 有 pending 目标但无 active
    if state.get("goals_active", 0) == 0 and state.get("goals_pending", 0) > 0:
        suggestions.append({
            "title": "推进待处理目标",
            "type": "推进型",
            "source": "内生驱动",
            "priority": "P2",
            "success_criteria": "至少一个目标进入 active",
        })

    return suggestions


# ─── CLI ────────────────────────────────────────────────────────────────────
def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="ANIMA SOMA · 自主目标引擎")
    sub = parser.add_subparsers(dest="cmd")

    # add
    a = sub.add_parser("add", help="添加目标")
    a.add_argument("title", help="目标标题")
    a.add_argument("--type", default="推进型", choices=["维护型", "推进型", "创造型"])
    a.add_argument("--source", default="内生驱动")
    a.add_argument("--priority", default="P2", choices=["P0", "P1", "P2", "P3", "P4"])
    a.add_argument("--criteria", default="", help="验收标准")
    a.add_argument("--context", default="", help="背景信息")
    a.add_argument("--deadline", default="", help="截止日期")

    # list
    sub.add_parser("list", help="列出所有目标")
    ls = sub.add_parser("ls", help="列出指定状态的目标")
    ls.add_argument("status", choices=["pending", "active", "blocked", "completed"])

    # activate
    act = sub.add_parser("activate", help="激活目标")
    act.add_argument("goal_id", help="目标 ID")

    # complete
    comp = sub.add_parser("complete", help="完成目标")
    comp.add_argument("goal_id", help="目标 ID")
    comp.add_argument("--result", default="", help="完成结果")

    # block
    blk = sub.add_parser("block", help="阻塞目标")
    blk.add_argument("goal_id", help="目标 ID")
    blk.add_argument("--reason", default="", help="阻塞原因")

    # next
    sub.add_parser("next", help="获取下一个应执行的目标")

    # state
    sub.add_parser("state", help="收集当前状态")

    # suggest
    sub.add_parser("suggest", help="基于当前状态建议目标")

    args = parser.parse_args()

    if args.cmd == "add":
        gid = add_goal(args.title, args.type, args.source, args.priority,
                       args.criteria, args.context, args.deadline)
        print(f"Goal added: {gid}")

    elif args.cmd in ("list", "ls"):
        status = getattr(args, "status", None)
        goals = list_goals(status)
        if not goals:
            print("No goals found.")
        for g in goals:
            lvl = g.get("priority", "?")
            st = g.get("status", "?")
            print(f"  [{lvl}] [{st}] {g['id']}: {g['title']}")

    elif args.cmd == "activate":
        ok = activate_goal(args.goal_id)
        print(f"{'Activated' if ok else 'Not found'}: {args.goal_id}")

    elif args.cmd == "complete":
        ok = complete_goal(args.goal_id, args.result)
        print(f"{'Completed' if ok else 'Not found'}: {args.goal_id}")

    elif args.cmd == "block":
        ok = block_goal(args.goal_id, args.reason)
        print(f"{'Blocked' if ok else 'Not found'}: {args.goal_id}")

    elif args.cmd == "next":
        g = get_next_goal()
        if g:
            print(f"[{g.get('priority')}] {g['id']}: {g['title']} (status: {g.get('status')})")
        else:
            print("No pending goals.")

    elif args.cmd == "state":
        import pprint
        pprint.pprint(gather_state())

    elif args.cmd == "suggest":
        state = gather_state()
        suggestions = identify_goals_from_state(state)
        if not suggestions:
            print("No suggestions. System healthy.")
        for s in suggestions:
            print(f"  [{s['priority']}] {s['type']} | {s['title']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
