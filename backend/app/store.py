"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

批次水位数据记录（BATCH_WATERMARK_TABLE）是跨模块共享的一张表：
- 指标采集模块在批量暂停采集器时整组写入一条批次水位；
- 水位线模块（另一个业务模块）按版本号同步写入同一条记录；
- 告警模块只消费当前生效（已提交、未关闭）的批次水位。
所有跨模块读写都走同一把锁，并以版本号做乐观并发控制，竞态请求只允许一项生效。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS

#: 批次水位数据记录表：跨模块共享，按「批次编号」唯一
BATCH_WATERMARK_TABLE = "batch_watermark"
#: 生效中的批次水位状态；已关闭的批次不再约束采集器与告警规则
BATCH_WATERMARK_ACTIVE_STATUSES = ("处理中", "已暂停", "部分失败")


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 批次水位表初始为空，由业务链路按需写入，沿用既有表的内存实现
        self._tables.setdefault(BATCH_WATERMARK_TABLE, [])
        # 保护所有写操作的进程内锁：批次并发暂停、跨模块同步写入都靠它串行化
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """对外暴露同一把锁，业务层的复合校验+写入需要在锁内原子完成。"""
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        """按业务编号等唯一字段定位记录，供跨模块按采集器编号/批次编号关联。"""
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def next_id(self, module: str) -> int:
        """在锁内分配下一个主键，避免并发写入产生重复 id。"""
        rows = self.rows(module)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        with self._lock:
            for name in self.module_names():
                rows = self.rows(name)
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
