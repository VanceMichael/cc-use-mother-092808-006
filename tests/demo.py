"""端到端可运行演示：构建指数族 -> 原始调仓 -> 事后更正 -> 重述审批 -> 复现报告。

运行：
    python3 -m tests.demo

全部主体为虚构，不连接外部系统。
"""

from __future__ import annotations

import json
from decimal import Decimal

from src.bond_index import Store
from tests.scenario import (REBALANCE_DATE, apply_october_correction,
                            build_world, principals)


def _decimal_friendly(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(type(obj))


def main() -> None:
    store = Store()
    p = principals()
    svc = build_world(store)

    # 1) 原调仓日（V1 方法、当时数据水位）
    run_id = svc.compute_run(p["provider"], REBALANCE_DATE)
    svc.publish_run(p["provider"], run_id)
    original = svc.get_run(run_id)
    o_core = original["indexes"]["IDX-CORE"]
    print("=" * 72)
    print(f"原始调仓 {REBALANCE_DATE}（方法 V{original['method_version_no']}）")
    print(f"  核心成分数：{len(o_core['constituents'])}")
    print(f"  核心碳强度：{o_core['carbon_intensity']}")
    print(f"  基准碳强度：{o_core['benchmark_carbon_intensity']}")
    print(f"  相对基准降幅：{Decimal(o_core['benchmark_reduction_pct']) * 100:.2f}%  (>19%)")

    # 2) 事后：排放更正 + 撤回 + 评级下调 + 方法 V2
    apply_october_correction(store)

    # 3) 维护者提议、另一角色批准
    rid = svc.propose_restatement(
        p["method"], REBALANCE_DATE, "MV-V2",
        "绿源电力2025排放由300更正为420；方法V2评级窗5→3天、权重改发行量加权")
    restated_id = svc.approve_restatement(p["approver"], rid)
    restated = svc.get_run(restated_id)
    r_core = restated["indexes"]["IDX-CORE"]
    print("=" * 72)
    print(f"重述后 {REBALANCE_DATE}（方法 V{restated['method_version_no']}，另一角色批准）")
    print(f"  核心成分数：{len(r_core['constituents'])}（剔除 B0801 评级窗口缩短）")
    print(f"  核心碳强度：{r_core['carbon_intensity']}")
    print(f"  相对基准降幅：{Decimal(r_core['benchmark_reduction_pct']) * 100:.2f}%  (原 >19% 无法重现)")

    # 4) 对外复现报告：不只给最新数字
    report = svc.reproduction_report(p["investor"], REBALANCE_DATE)
    print("=" * 72)
    print("对外复现报告（机构投资者视角）")
    print(f"  原始 run：{report['original']['run_id']}")
    print(f"  原始水位哈希：{report['original']['watermark_hash']}")
    print(f"  当前应采用：{report['current_run_id']}")
    print(f"  重述次数：{len(report['restatements'])}")
    for rs in report["restatements"]:
        print(f"    - [{rs['status']}] {rs['reason']}")
        print(f"        提议：{rs['proposed_by_role']}  批准：{rs['approved_by_role']}")
        imp = rs["impact"]
        print(f"        核心降幅 {imp['original_benchmark_reduction_pct']} -> "
              f"{imp['restated_benchmark_reduction_pct']}")
        print(f"        成分剔除：{imp['constituent_diff']['removed']}")
    print("=" * 72)
    print("原始成分/权重/碳指标完整保留在报告 original 节，可独立复现。")


if __name__ == "__main__":
    main()
