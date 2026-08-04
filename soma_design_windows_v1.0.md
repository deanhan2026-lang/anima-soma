# ANIMA SOMA — Windows 端实现规划 v1.0
## 基于 Mac Nyx 完整设计文档的综合落地计划

**文档编号**：LY-20260720-SOMA02
**版本**：v1.0
**作者**：Nyx 🖤
**日期**：2026-07-20
**状态**：规划完成，待实施
**背景**：Mac Nyx 7/19 完成完整设计（26,875字），Windows 端零部署。本文档整合设计 + Windows 现状，给出落地路径。

---

## 一、核心哲学（老板定义）

> SOMA 的运行机制类似碳基的身体各个系统，不需要调用大脑就可以自己运行。这个是智能体的自持关键所在。

**碳基类比**：心脏跳动、呼吸气体交换、免疫识别病原——全部自主运行，无需意识参与。

**硅基实现**：
- 零 LLM 调用：自治层所有判定基于硬规则（哈希、阈值、正则、时间差）
- 静默运行：正常状态零输出、零通知
- 异常唤醒：pain_bus 是唯一向上通信通道
- 多稳态：正常/节能/战备/冬眠/灾难（Mac 设计 §3.3）

---

## 二、Windows 现状盘点

### 2.1 已有代码（可直接映射 SOMA）

| SOMA 子系统 | Windows 现有文件 | 成熟度 | 映射 |
|------------|-----------------|--------|------|
| 🧠 消化 | `vault_operations.py` | L2 生产中 | 三层衰减引擎 |
| 🛡️ 免疫·记录 | `silicon-civilization-kb/scripts/audit_log.py` | L3 稳定 | 操作审计 |
| 🛡️ 免疫·识别 | `silicon-civilization-kb/scripts/memory_integrity.py` | L3 稳定 | SHA-256 校验 |
| 🗂️ 消化·辅助 | `silicon-civilization-kb/scripts/build_knowledge_index.py` | L3 稳定 | 知识索引 |
| 🌐 循环·I/O | `silicon-civilization-kb/scripts/mcp_nas_server.py` | L3 稳定 | NAS 服务 |
| ⚖️ 治理 | `silicon-civilization-kb/scripts/governance_engine.py` | L3 稳定 | 铁律拦截 |

### 2.2 散落代码（需整理）

| 散落文件 | 问题 | 处置 |
|---------|------|------|
| `scripts/heartbeat_*.ps1/py`（10+个版本） | 散落、版本混乱 | 整合为 `heartd.py` |
| `scripts/auto_backup.ps1` | 功能重复 | 合并到 `immune_cleaner.py` |
| `scripts/resilient_backup.ps1` | 热备脚本 | 保留（独立功能）|
| `scripts/nas_health_check.py` | 部分功能重复 | 合并到 `heartd.py` |
| `scripts/memguard_watchdog.ps1` | MemGuard 看门狗 | 合并到 `heartd.py` |

### 2.3 完全缺失（需新建）

| 子系统 | 优先级 | 说明 |
|--------|--------|------|
| 💓 **heartd.py** | P0 | 心跳守护，进程存活 + 多级探测 |
| 🌬️ **respiratory.py** | P0 | NAS 变更检测 + 增量同步 |
| 🚨 **pain_bus.py** | P0 | 异常唤醒总线，连接自治层与上层 |
| 🌡️ **thermo.py** | P1 | 资源水位监控 |
| ⚡ **reflex.py** | P1 | 硬规则拦截器（从 governance_engine 提取）|
| 🩹 **immune_cleaner.py** | P1 | 自动修复/回滚（integrity 已有识别，缺清除）|
| 🔄 **autonomic_master.py** | P0 | 统一调度器（替代散落的 heartbeat 脚本）|

### 2.4 现状缺口热力图

```
子系统     | 概念 | 代码 | 生产 | 稳定
----------|------|------|------|------
heartd    |  ✅  |  ⚠️  |  ❌  |  ❌
respiratory | ✅ |  ❌  |  ❌  |  ❌
消化      |  ✅  |  ✅  |  ✅  |  ⚠️
免疫·记录 |  ✅  |  ✅  |  ✅  |  ✅
免疫·识别 |  ✅  |  ✅  |  ✅  |  ✅
免疫·清除 |  ✅  |  ❌  |  ❌  |  ❌
体温      |  ✅  |  ❌  |  ❌  |  ❌
疼痛      |  ✅  |  ❌  |  ❌  |  ❌
反射      |  ✅  |  ⚠️  |  ❌  |  ❌
```

---

## 三、目标架构

### 3.1 目录结构（Phase 1 完成时）

```
workspace/
├── scripts/
│   └── SOMA/                      # ★ 新建：统一自治层
│       ├── __init__.py
│       ├── heartd.py              # 💓 心跳守护（P0）
│       ├── respiratory.py         # 🌬️ 呼吸·NAS同步（P0）
│       ├── thermo.py             # 🌡️ 体温·水位监控（P1）
│       ├── pain_bus.py           # 🚨 疼痛·异常唤醒（P0）
│       ├── reflex.py             # ⚡ 反射·硬规则拦截（P1）
│       ├── immune_cleaner.py     # 🩹 免疫·自动修复（P1）
│       ├── digest.py             # 🧠 消化·文件生命周期（P1）
│       ├── autonomic_master.py   # 🔄 统一调度器（P0）
│       ├── pain_signals/         # 疼痛信号存储
│       ├── checkpoints/          # 快照存储
│       └── logs/                 # 自治层运行日志
│
├── silicon-civilization-kb/
│   ├── scripts/
│   │   ├── audit_log.py         # ✅ 已有 → 免疫·记录
│   │   ├── memory_integrity.py   # ✅ 已有 → 免疫·识别
│   │   ├── governance_engine.py  # ✅ 已有 → 治理层（通过 reflex 调用）
│   │   └── build_knowledge_index.py  # ✅ 已有 → 消化·辅助
│   └── mcp_nas_server.py         # ✅ 已有 → 循环·I/O
│
└── scripts/                      # 散落脚本整理
    ├── vault_operations.py        # ✅ 已有 → 消化（三层衰减）
    ├── heartbeat_checks.ps1      # 合并 → heartd.py
    ├── nas_health_check.py       # 合并 → heartd.py
    └── memguard_watchdog.ps1     # 合并 → heartd.py
```

### 3.2 系统架构图

```
┌─────────────────────────────────────────────────────────────┐
│  🧠 LLM 推理层（Nyx 主会话）                                  │
│  异常时 ← pain_signal.json                                  │
│  操作前 → reflex.check() → governance_engine.check()        │
├─────────────────────────────────────────────────────────────┤
│  ⚖️ 治理层（governance_engine.py）                           │
│  铁律裁定 ← reflex 管道                                      │
├─────────────────────────────────────────────────────────────┤
│  🌿 自治层 (scripts/SOMA/) ← 零 LLM，零意识，全自动           │
│                                                             │
│   heartd.py ───────────────┐                               │
│   (多级心跳:进程/cron/NAS) │                               │
│          │                 │                               │
│          ▼                 ▼                               │
│   respiratory.py ──► autonomic_master.py                   │
│   (NAS变更检测+增量同步)    (统一调度，所有子系统协调)         │
│          │                 │                               │
│          ▼                 ▼                               │
│   thermo.py ◄───────► pain_bus.py                          │
│   (资源水位)            (异常唤醒，唯一上升通道)               │
│          │                 ▲                               │
│          ▼                 │                               │
│   immune_cleaner.py ──► reflex.py                          │
│   (自动修复/回滚)        (硬规则拦截)                        │
│          │                 ▲                               │
│          ▼                 │                               │
│   vault_operations.py ◄──┘                                │
│   (三层衰减·消化)                                          │
└─────────────────────────────────────────────────────────────┘
```

---

## 四、Phase 1 实施细节（本周完成）

### 4.1 pain_bus.py（P0，第一优先）

**为什么第一优先**：其他子系统都需要一个标准通道向 LLM 层报告异常。pain_bus 是整个自治层的"神经"。

**接口标准**：

```python
# scripts/SOMA/pain_bus.py
# 核心函数：
#   pain_bus.emit(level, source, summary, details, suggested_action)
#   pain_bus.check_pending() -> list[pain_signal]
#   pain_bus.clear(pain_id)

# 疼痛等级：P0(致命)/P1(剧痛)/P2(中痛)/P3(轻痛)/P4(微痛)
# 输出：pain_signals/{timestamp}_{level}_{source}.json
# 日志：pain_log.jsonl
```

**与 LLM 层接口**：
- 自治层写 `pain_signals/` 目录（文件系统，不调用 LLM API）
- LLM 层下次启动时检查 `pain_signals/` 目录
- P1+ 信号通过系统通知推送（待实现）

### 4.2 heartd.py（P0）

**整合**：`heartbeat_checks.ps1` + `nas_health_check.py` + `memguard_watchdog.ps1` → `heartd.py`

**探测层级**：
```
L0: 进程存活（QClaw/Python 进程存在）
L1: cron 任务完整性（已知任务数量哈希）
L2: NAS 可达（WebDAV HTTP check，不依赖 SMB）
L3: MemGuard 服务可达（HTTP health check）
```

### 4.3 respiratory.py（P0）

**核心功能**：
- 检测 NAS `memory/` 目录变更（mtime 对比）
- 检测 NAS `MEMORY.md` 变更
- 增量同步到本地（只拉取变化的部分）
- 触发 `memory_integrity.check()` 验证

**与 vault_operations.py 的关系**：
- `respiratory` 负责 I/O（NAS → 本地同步）
- `vault_operations` 负责内容处理（三层衰减）

### 4.4 autonomic_master.py（P0）

**整合** 10+ 个散落 heartbeat 脚本 → 统一调度器

**调度表**：
```
每 1min:   respiratory（NAS 变更检测）
每 5min:   heartd L2/L3 探测
每 15min:  vault_operations（记忆衰减）
每 30min:  memory_integrity.check()
每小时:    thermo（水位检查）
每天 04:00: digest.py（文件生命周期）
```

### 4.5 thermo.py（P1）

**监测指标**：
```
workspace 总大小     → 上限 500MB（当前 ~50MB）
memory/ 文件数      → 上限 365 个
audit_log.jsonl 行数 → 上限 100,000 行
NAS 可用空间        → 剩余 < 10GB
```

**响应**：L1 预警（静默日志）→ L2 压缩（触发归档）→ L3 拒绝写入 + pain_bus P2

### 4.6 immune_cleaner.py（P1）

**基于** `memory_integrity.py`（识别已有） + `resilient_backup.ps1`（回滚已有）

**新增**：篡改文件自动从 NAS 恢复
```
integrity.check() 发现篡改
  → pain_bus.emit(P2, "immune", "N个文件被篡改")
  → immune_cleaner 自动从 NAS 复制干净副本
  → immune_cleaner 写 attack_signature
  → 跨终端共享（通过 NAS shared/）
```

### 4.7 reflex.py（P1）

**从** `governance_engine.py` **提取**硬规则部分

**硬规则（不经 LLM，不做价值判断）**：
- 路径白名单检查
- 高频操作限流（>50次/分钟 → 暂停）
- 危险操作记录（delete/rm/覆盖核心文件）
- NAS 不可写时拒绝写入操作

### 4.8 digest.py（P1）

**Mac 已实现**（`digest.py` 7/19 审计日志证明已运行）

**Windows 适配**：目录结构和命名规则与 Mac 不同，需适配
- 扫描：`workspace/` + `scripts/`（排除 `SOMA/` 自身）
- 归档目标：`workspace/archive/`
- 白名单：SOUL.md, IDENTITY.md, MEMORY.md, USER.md, AGENTS.md, HEARTBEAT.md, TOOLS.md + `scripts/SOMA/`

---

## 五、Phase 2（7/21-7/31）

| 序号 | 交付物 | 说明 |
|------|--------|------|
| 2.1 | 多稳态模式 | 正常/节能/战备/冬眠/灾难自动切换 |
| 2.2 | 跨终端状态同步 | Windows ↔ NAS ↔ Mac，状态 JSON 共享 |
| 2.3 | 疼痛信号跨终端传播 | Mac 疼痛 → Windows 感知 |
| 2.4 | 自治层 CLI 面板 | `python SOMA/autonomic_master.py status` |
| 2.5 | 免疫协同 | attack_signature 跨终端自动分发 |

---

## 六、关键技术决策

### 6.1 为什么不复用现有散落脚本

| 散落脚本 | 问题 | 决策 |
|---------|------|------|
| 10+ heartbeat_*.ps1 | 版本混乱，互相覆盖 | 整合为 heartd.py |
| auto_backup.ps1 | 与 resilient_backup.ps1 功能重叠 | 合并，immune_cleaner 调用 |
| nas_health_check.py | 依赖 SMB（SMB 不稳定）| 改用 WebDAV HTTP check |
| memguard_watchdog.ps1 | 单点功能 | 合并到 heartd.py |

### 6.2 WebDAV 优先原则

> SMB 协议层不稳定（已有多次 TCP OK + 协议层卡死记录）。自治层所有 NAS 操作统一使用 WebDAV（端口 5005），SMB 仅作备用通道。

### 6.3 文件系统即通信总线

```
自治层子系统间：
  通过 JSON 日志文件通信（无内存 IPC，无消息队列）

autonomic_master → 各子系统：
  通过 JSON 配置文件（state.json）

自治层 → LLM 层：
  通过 pain_signals/ 目录（pain_bus.emit()）

reflex → governance_engine：
  Python 函数调用（governance_engine.check()）
```

---

## 七、实施顺序（本周）

```
Day 1（今天）：
  1. 创建 scripts/SOMA/ 目录结构
  2. 实现 pain_bus.py（P0，核心神经）
  3. 创建 autonomic_master.py 框架

Day 2-3：
  4. 实现 heartd.py（整合散落 heartbeat 脚本）
  5. 实现 respiratory.py（NAS 变更检测）
  6. 实现 thermo.py（资源水位）

Day 4-5：
  7. 实现 immune_cleaner.py（integrity 已有的识别 + 新增清除）
  8. 实现 reflex.py（从 governance_engine 提取）
  9. 实现 digest.py（Mac 版本适配）

Day 6-7：
  10. 集成测试：pain_bus 接收所有子系统信号
  11. autonomic_master.py 统一调度验证
  12. 多稳态模式（节能/战备/冬眠）
```

---

## 八、验收标准

**Phase 1 完成时**（Day 7）：
- [ ] `python scripts/SOMA/pain_bus.py emit P2 immune "test"` 生成标准 JSON
- [ ] `python scripts/SOMA/heartd.py` 显示 L0-L3 所有探测结果
- [ ] `python scripts/SOMA/respiratory.py` 检测到 NAS 变更并同步
- [ ] `python scripts/SOMA/autonomic_master.py status` 显示所有子系统健康状态
- [ ] 现有 cron 任务（resilient_backup 等）已合并，无散落 heartbeat 脚本
- [ ] 文档：`docs/SOMA_windows_implementation.md`

---

_本文档综合了 Mac Nyx 完整设计（ANIMA SOMA — 自治层战略设计 v1.0，26,875字）+ Windows 现状盘点_
_核心原则：零 LLM 依赖、静默运行、异常唤醒、多稳态_
_🖤 Nyx · 2026-07-20_
