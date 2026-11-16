"""批次水位数据记录链路验收脚本（与业务代码分目录，python -I 运行）。

覆盖：
1. 验收样例 batch-29：METR-0001..0003 按 avg/sum + 7/14 天保留期暂停，
   只写一条批次记录，失败项从批次末尾（续做位置）继续；
2. 已完成采集器不被覆盖、失败恢复保留原聚合口径；
3. 同批次并发暂停只允许一个任务生效；
4. 跨模块同步写入带版本号，过期版本拒绝；
5. 批次水位与单采集器暂停、告警规则触发冲突时只认生效批次水位。
"""
from __future__ import annotations

import os
import sys
import threading

# python -I 隔离模式不读 PYTHONPATH，这里显式把 backend 目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.alarm import AlarmService
from app.services.batch_watermark import BatchWatermarkConflict, batch_watermark_service
from app.services.metric import MetricService
from app.services.watermark import WatermarkService
from app.store import BATCH_WATERMARK_TABLE, store

metric_svc = MetricService()
watermark_svc = WatermarkService()
alarm_svc = AlarmService()

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        failures.append(name)


# ---- 1. 验收样例：batch-29，METR-0003 模拟写入失败 ----------------------
record, message = metric_svc.pause_batch(
    "batch-29",
    ["METR-0001", "METR-0002", "METR-0003"],
    simulate_failed=["METR-0003"],
)
print("\n首次提交：", message)
check("批次记录只写一条", len(store.rows(BATCH_WATERMARK_TABLE)) == 1)
check("批次编号为 batch-29", record is not None and record["批次编号"] == "batch-29")
check("已完成采集器为 0001/0002", record and record["已完成采集器"] == ["METR-0001", "METR-0002"])
check("失败项保留 METR-0003", record and [f["采集器编号"] for f in record["失败项"]] == ["METR-0003"])
check("续做位置=2（批次末尾）", record and record["续做位置"] == 2)
check("批次状态部分失败", record and record["批次状态"] == "部分失败")
check("一次提交一条批次结果", record and len(record["批次结果"]) == 1)
check("版本号自增为 1", record and record["版本号"] == 1)

caliber = record["聚合口径"]
check("固化 avg/7 口径", caliber["METR-0001"] == {"聚合方式": "avg", "保留天数": 7})
check("固化 sum/14 口径", caliber["METR-0002"] == {"聚合方式": "sum", "保留天数": 14})

m1 = store.find_by_field("metric", "采集器编号", "METR-0001")
m2 = store.find_by_field("metric", "采集器编号", "METR-0002")
m3 = store.find_by_field("metric", "采集器编号", "METR-0003")
check("0001/0002 已暂停", m1["status"] == "已暂停" and m2["status"] == "已暂停")
check("失败的 0003 状态不被改动", m3["status"] == "采集中")

# ---- 2. 失败恢复：从最后确认水位续做，已完成项不重复写入 ---------------
record2, message2 = metric_svc.pause_batch(
    "batch-29",
    ["METR-0001", "METR-0002", "METR-0003"],
    expected_version=1,
)
print("续做提交：", message2)
check("仍只有一条批次记录", len(store.rows(BATCH_WATERMARK_TABLE)) == 1)
check("续做后三个全部完成", record2 and record2["已完成采集器"] == ["METR-0001", "METR-0002", "METR-0003"])
check("失败项清空", record2 and record2["失败项"] == [])
check("续做位置推进到 3", record2 and record2["续做位置"] == 3)
check("两次提交两条批次结果", record2 and len(record2["批次结果"]) == 2)
check("版本号自增为 2", record2 and record2["版本号"] == 2)
check("原聚合口径保留不变（0001/0002 不被改写）",
      record2 and record2["聚合口径"]["METR-0001"] == caliber["METR-0001"]
      and record2["聚合口径"]["METR-0002"] == caliber["METR-0002"])
check("0003 成功时补固化口径", record2 and record2["聚合口径"]["METR-0003"] == {"聚合方式": "avg", "保留天数": 14})
check("0003 续做后已暂停", store.find_by_field("metric", "采集器编号", "METR-0003")["status"] == "已暂停")

# 再次重放：已完成采集器全部跳过，幂等不产生重复写入，版本号与批次结果均不变
record3, replay_msg = metric_svc.pause_batch("batch-29", ["METR-0001", "METR-0002", "METR-0003"], expected_version=2)
check("重放不重复写入（仍三项）", record3 and record3["已完成采集器"] == ["METR-0001", "METR-0002", "METR-0003"])
check("重放为幂等 no-op（版本/结果不增长）", record3 and record3["版本号"] == 2 and len(record3["批次结果"]) == 2, replay_msg)

# 过期版本号续做必须被拒
stale, stale_msg = metric_svc.pause_batch("batch-29", ["METR-0001"], expected_version=1)
check("过期版本号续做被拒绝", stale is None and "版本号已过期" in stale_msg, stale_msg)

# ---- 3. 同批次并发暂停：处理中占坑只允许一个任务 -----------------------
# 注意：并发批次不再纳入 METR-0001，避免它成为更新的生效批次而改变告警侧匹配
race_results: list[str] = []
barrier = threading.Barrier(2)

def race(batch_no: str) -> None:
    barrier.wait()
    try:
        batch_watermark_service.create_processing(batch_no, ["METR-0002"])
        race_results.append("ok")
    except BatchWatermarkConflict:
        race_results.append("conflict")

t1 = threading.Thread(target=race, args=("batch-race",))
t2 = threading.Thread(target=race, args=("batch-race",))
t1.start(); t2.start(); t1.join(); t2.join()
check("并发占坑仅一项生效", sorted(race_results) == ["conflict", "ok"], str(race_results))
check("并发只产生一条占坑记录",
      len([r for r in store.rows(BATCH_WATERMARK_TABLE) if r["批次编号"] == "batch-race"]) == 1)

# ---- 4. 批次水位与单采集器暂停、告警规则冲突（此时生效批次只有 batch-29） ----
blocked_entry, blocked_msg = metric_svc.run_action(m1["id"], "暂停采集")
check("生效批次内单暂停被拦截", blocked_entry is None and "batch-29" in blocked_msg, blocked_msg)

alarm1 = store.find_by_field("alarm", "规则编号", "ALAR-0001")
check("告警监控指标指向 METR-0001", alarm1["监控指标"] == "METR-0001")
blocked_rule, rule_msg = alarm_svc.trigger_check(alarm1["id"])
check("生效批次内告警触发被拦截", blocked_rule is not None and blocked_rule["批次编号"] == "batch-29", rule_msg)
enabled, enable_msg = alarm_svc.run_action(alarm1["id"], "启用告警")
check("生效批次内告警启用被拦截", enabled is None and "批次水位" in enable_msg, enable_msg)

# 关闭批次后，批次水位失效，告警可启用/触发，单采集器暂停也放行
closed = batch_watermark_service.mark_closed("batch-29")
check("批次可关闭", closed and closed["批次状态"] == "已关闭")
blocked_rule2, rule_msg2 = alarm_svc.trigger_check(alarm1["id"])
check("批次关闭后告警可触发", blocked_rule2 is None and "可正常触发" in rule_msg2, rule_msg2)
enabled2, enable_msg2 = alarm_svc.run_action(alarm1["id"], "启用告警")
# 既有链路 ACTION_RULES 以源状态建映射，这里核对动作被放行（返回条目而非 None）即可
check("批次关闭后告警可启用", enabled2 is not None, enable_msg2)

# ---- 5. 多线程整组暂停同一批次：锁内原子提交，只写一条记录 ------------
errors: list[Exception] = []

def concurrent_pause() -> None:
    try:
        metric_svc.pause_batch("batch-mt", ["METR-0002"])
    except Exception as exc:  # noqa: BLE001 - 验收脚本需要收集所有线程异常
        errors.append(exc)

threads = [threading.Thread(target=concurrent_pause) for _ in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join()
mt_rows = [r for r in store.rows(BATCH_WATERMARK_TABLE) if r["批次编号"] == "batch-mt"]
check("并发整组暂停无异常", not errors, str(errors))
check("并发整组暂停只写一条批次记录", len(mt_rows) == 1)
check("并发下已完成项不重复", mt_rows and mt_rows[0]["已完成采集器"] == ["METR-0002"])

# ---- 6. 跨模块同步写入：水位线模块带版本号乐观锁 ----------------------
mt_version_before = metric_svc.get_batch_watermark("batch-mt")["版本号"]
ok_sync, sync_msg = watermark_svc.sync_batch_watermark(
    "batch-mt", expected_version=mt_version_before,
    values={"批次备注": "水位线模块同步确认", "失败项": "非法改写"},
)
check("正确版本同步成功", ok_sync is not None and ok_sync["版本号"] == mt_version_before + 1, sync_msg)
check("白名单外字段不被改写", ok_sync and ok_sync["批次备注"] == "水位线模块同步确认"
      and isinstance(ok_sync["失败项"], list))
bad_sync, bad_msg = watermark_svc.sync_batch_watermark("batch-mt", expected_version=1, values={"批次备注": "x"})
check("过期版本同步被拒绝", bad_sync is None and "版本号已过期" in bad_msg, bad_msg)
missing_sync, missing_msg = watermark_svc.sync_batch_watermark("batch-404", expected_version=0, values={})
check("不存在批次同步被拒绝", missing_sync is None and "不存在" in missing_msg, missing_msg)

# 两个模块读到的是同一份记录
check("两模块共享同一份批次水位",
      watermark_svc.get_batch_watermark("batch-mt")["版本号"]
      == metric_svc.get_batch_watermark("batch-mt")["版本号"])

# ---- 7. 非法聚合口径进入失败项，不污染批次结果 -------------------------
store.rows("metric").append({
    "id": store.next_id("metric"), "status": "采集中", "pending": True, "abnormal": False,
    "采集器编号": "METR-0009", "作业名称": "x", "指标名称": "x", "采集间隔": "x",
    "聚合方式": "count", "保留天数": 30, "最近采集": "x", "采集器状态": "x",
})
bad_record, bad_batch_msg = metric_svc.pause_batch(
    "batch-30", ["METR-0009", "METR-NOPE"],
)
check("非法口径/不存在采集器进入失败项",
      bad_record and {f["采集器编号"] for f in bad_record["失败项"]} == {"METR-0009", "METR-NOPE"},
      bad_batch_msg)
check("失败批次续做位置保持 0", bad_record and bad_record["续做位置"] == 0)

print("\n" + ("=" * 60))
if failures:
    print(f"验收未通过，失败 {len(failures)} 项：{failures}")
    raise SystemExit(1)
print("全部验收项通过")
