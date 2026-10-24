# 证据目录说明（本地约定）

本目录存两类东西：

1. **入库的门禁证据**（可审计、需长期保留）
   - `P0-gate.txt`：`scripts/gate_p0.py` 的输出（命令、耗时、退出码）
   - `L1-P0-run1.log`：L1（Codex）产出契约包的原始会话日志
2. **本地运行时产物**（不入库，已在根 `.gitignore` 忽略）
   - `stdout-<service>.log`：各运行单元的标准输出（含内部错误栈）
   - `*.jsonl`：P0 运行验收的轨迹证据
   - `state/*.jsonl`：命令/回执/任务的追加式日志（Mock 跨进程存储）
   - `registry-*.json`：故障注入用的登记表变体

命令必须从**仓库根**运行，例如：

```powershell
backend\.venv\Scripts\python.exe -m pytest tests\contracts -q
backend\.venv\Scripts\python.exe -m pytest tests\p0_runtime -q
```
