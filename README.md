# 气候债券指数编制与重述

编制气候债券指数族：登记发行人与债券主数据、评级及期限事件、气候数据来源、
排放口径、合规筛选、权重方法、指数族依赖与调仓日历；每次计算固定数据水位与
方法版本；支持批次幂等入账、可恢复调仓任务、需独立批准的回溯重述与可复现报告。

## 解决的问题

发行人更新一项排放数据后，核心指数、六只子指数和历史披露若分别按不同方法版本
重算，投资机构无法复现原调仓日 **超过 19%** 的碳强度降幅。本服务通过以下机制保证
可复现：

- 每次发布钉死「数据水位 × 方法版本」，原始发布冻结、不被最新数据覆盖；
- 核心与子指数共享合格性判断，权重在各自范围内用精确分数独立守恒；
- 到期、停牌、评级变化、数据撤回按参数化生效窗口进入调仓；
- 批次重送内容一致不重复入账，内容不同阻断相关发布；
- 方法维护者提议的回溯重述必须由另一角色批准；
- 复现结果列明某日成分、权重、碳指标、基准差异及后来重述原因，而非只返回最新数字。

合成示例数据下，核心指数基准 WACI 275 → 调仓日 211.25，降幅 **23.18%**（>19%），
见 `test_core_reduction_exceeds_19_percent_and_is_exactly_reproducible`。

## 目录

- `src/cbi/`：编制服务包
  - `model.py` 领域模型与登记校验
  - `store.py` 原子状态存储与只增任务日志
  - `access.py` 角色与受限数据访问控制
  - `windows.py` 生效窗口规则
  - `engine.py` 水位、合格性、权重守恒与碳指标计算
  - `service.py` 服务门面（登记/批次/调仓/重述/复现）
- `docs/service-architecture.md`：详细设计与角色矩阵
- `docs/domain-rules.md`：领域边界
- `contracts/`、`fixtures/`：既有领域资料（上下文读取器保持可用）
- `src/news_context_006.py`：领域资料读取与校验
- `tests/builders.py`：不含真实身份信息的合成测试世界
- `tests/test_service.py`：端到端测试（29 项）

## 快速示例

```python
from src.cbi import IndexService
from tests.builders import build_family_world, propose_v1, CALC, INVESTOR

svc = IndexService("./data")
build_family_world(svc)   # 8 发行人、核心 + 六只子指数、两年披露
propose_v1(svc)           # 方法维护者提议、审批人激活 v1

run = svc.run_rebalance(CALC, "2026-09-01")
print(run["snapshot"]["indexes"]["CB-CORE"]["reduction_vs_baseline_pct"])  # 23.1818

# 投资者复现：冻结数字 + 后来重述原因，不随新数据变化
report = svc.reproduce(INVESTOR, rebalance_date="2026-09-01")
```

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src tests
```

两条命令只读写仓库内文件与临时目录，不需要连接外部业务系统。
