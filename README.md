# 气候债券指数编制与重述

编制气候债券指数族，管理数据水位、方法版本、调仓和重述。

本仓库在原有领域资料基础上，提供一个**无外部依赖**（仅 Python 3.11+ 标准库）
的债券指数编制服务：登记主数据与方法、固定水位与方法版本计算、
指数族共享合格性且各自权重守恒、批次幂等入账、重述 maker-checker、
调仓故障恢复，以及可复现历史的对外报告。

## 业务问题

发行人更新一项排放数据后，核心指数、六只子指数与历史披露若按不同方法版本
各自重算，投资机构将无法复现原调仓日超过 19% 的碳强度降幅。本服务通过
"每次计算固定数据水位 + 方法版本、原始 run 永久留痕、重述须另一角色批准、
复现报告并列原始与重述"来解决该问题。

## 代码结构

```
src/bond_index/
  models.py          枚举与主数据模型、默认生效窗口
  errors.py          业务异常
  store.py           JSON 原子写入状态仓库（Decimal 安全往返）
  access_control.py  角色权限、受限数据存在性闸门
  registry.py        发行人/债券/来源/口径/筛选/权重/指数族/方法版本/日历登记
  ingest.py          行情/披露/事件/基准批次：幂等、异文隔离、生效窗口
  engine.py          水位、事件状态推导、共享合格性、权重守恒、碳指标
  service.py         运行、发布闸门、重述审批、调仓恢复、复现报告
tests/
  scenario.py        端到端虚构场景（复现 19% 降幅叙事）
  demo.py            可运行演示：python3 -m tests.demo
  test_*.py          55 个单元/场景测试
```

## 快速开始

```python
from src.bond_index import BondIndexService, Store, Principal, Role

svc = BondIndexService(Store("index_state.json"))
provider = Principal("u1", Role.INDEX_PROVIDER)

run_id = svc.compute_run(provider, "2026-09-01")          # 固定水位+方法版本
svc.publish_run(provider, run_id)                          # 通过发布闸门后发布
report = svc.reproduction_report(
    Principal("u2", Role.INVESTOR), "2026-09-01")          # 投资者取复现报告
# report 含 original（成分/权重/碳指标/基准差异/水位哈希）+ restatements（原因与审批链）
```

端到端演示（原始降幅 20.56% → 排放更正与方法 V2 重述后 17.25%，原始结果仍可复现）：

```bash
python3 -m tests.demo
```

## 关键规则

- **一核六子**：子指数与核心共享筛选与口径，合格性判断族内共享，
  但权重在各自成分范围内独立归一、严格守恒（和为 1）。
- **水位固定**：原始 run 知识截止日 = 调仓日；重述 run 纳入事后更正数据；
  水位清单哈希入快照，不可变。
- **生效窗口**：到期 0 / 停牌复牌 1 / 评级 5 / 撤回 0 天（方法版本可覆盖），
  生效日在计算时按 run 的方法版本解析。
- **批次**：同编号同内容重送不重复入账；内容不同即隔离并阻止相关发布。
- **重述**：维护者提议、必须由另一角色批准（禁止自批、同角色互批），
  批准才生成 restated run 并记录影响。
- **受限数据**：无权者无法从任何查询确认受限气候数据是否存在。
- **故障恢复**：调仓任务按检查点落盘，崩溃后重建实例即从断点续跑，
  计算与发布均幂等。

详见 `docs/domain-rules.md`。

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src tests
```

两条命令只读写仓库内文件，不需要连接外部业务系统。
