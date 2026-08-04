# ANIMA SOMA — 自治层

**Self-Organizing Memory Automaton** — 智能体的自主神经记忆系统。

核心理念（老板定义）：
> SOMA 的运行机制类似碳基身体各系统，不需要调用大脑就可以自己运行。
> 这是智能体的自持关键所在。

## 八大子系统

| 子系统 | 功能 | 调度频率 |
|--------|------|----------|
| `pain_bus.py` | 疼痛总线·异常唤醒 | 事件驱动 |
| `autonomic_master.py` | 统一调度器 | 持续运行 |
| `heartd.py` | 心跳守护·多级探测 | 每 5min |
| `respiratory.py` | 呼吸·NAS 同步 | 每 1min |
| `thermo.py` | 体温·水位监控 | 每 60min |
| `digest.py` | 消化·文件生命周期 | 每天 04:00 |
| `reflex.py` | 反射·硬规则拦截 | 事件驱动 |
| `immune_cleaner.py` | 免疫·自动修复 | 按需触发 |

## 设计原则

- **零 LLM 依赖**：所有判定基于硬规则（哈希、阈值、正则、时间差）
- **静默运行**：正常状态零输出、零通知
- **异常唤醒**：`pain_bus` 是唯一向上通信通道
- **多稳态模式**：normal / standby / combat / hibernate / disaster

## 使用

```bash
# 启动自治层
python autonomic_master.py run

# 查看状态
python autonomic_master.py status

# 切换模式
python autonomic_master.py mode standby
```

## 文档

- `soma_design_windows_v1.0.md` — Windows 端落地方案
- `pain_log.jsonl` — 疼痛事件审计日志

## 状态

v1.0 — 全部 8 个子系统实现完成，生产就绪。
