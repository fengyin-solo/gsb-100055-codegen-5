"""地球物理业务规则：物探测线单向流转工作流。

约束（对应业务口径）：
- 状态只能沿 待施测→施测中→已采集→数据处理→已归档 单向、逐站推进，跳级与回退一律拦下；
- 每次推进分配全局修订号，结果同时落到勘探区采集台账、成果图清单、待处理列表，三处同号；
- 撤回已归档测线不删历史，而是开新修订版本（版本号 +1、回到待施测），
  历史测线长度与数据质量按原始上报口径原样留存；
- 同一测线的并发提交用行锁 + 期望修订号（CAS）双重核对，只允许一个落地；
- 客户端带操作序号，重复序号直接回放已落地结果（断点续跑），同一条测线不会被推进两次；
- 老测线缺失/非法状态的，按施测先后迁移补齐。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "geophysics"
REQUIRED_FIELDS = ["测线编号", "勘探区", "物探方法"]
OPTIONAL_FIELDS = ["测线长度", "点距", "施测日期", "数据质量"]

# 单向流程：动作与“恰好相邻的下一站”一一对应，缺任何一站都算跳级
STATUS_ORDER = ["待施测", "施测中", "已采集", "数据处理", "已归档"]
FORWARD_ACTIONS: dict[str, str] = {
    "开始施测": "施测中",
    "完成采集": "已采集",
    "提交处理": "数据处理",
    "归档": "已归档",
}
# 撤回不是回退：只允许从终点站发起，落地为新修订版本
REVISE_ACTION = "撤回归档"
# 待处理列表里每个状态对应的下一步动作
NEXT_ACTIONS = {
    "待施测": "开始施测",
    "施测中": "完成采集",
    "已采集": "提交处理",
    "数据处理": "归档",
}
MAP_PROGRESS = {
    "待施测": "待出图",
    "施测中": "待出图",
    "已采集": "待出图",
    "数据处理": "成图中",
    "已归档": "已出图",
}

LEDGER_TABLE = "geophysics_ledger"
MAPS_TABLE = "geophysics_maps"
PENDING_TABLE = "geophysics_pending"
REVISIONS_TABLE = "geophysics_revisions"
OPS_TABLE = "geophysics_ops"
META_TABLE = "geophysics_meta"

# 同一测线串行化；修订号自增与三处同步再用一把全局锁保证“同号同批”
_line_locks: defaultdict[Any, threading.Lock] = defaultdict(threading.Lock)
_revision_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class GeophysicsService:
    # ---------- 列表/明细 ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        area: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("测线编号", ""))]
        if area:
            rows = [row for row in rows if area in str(row.get("勘探区", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ---------- 登记 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        line_no = str(values.get("测线编号")).strip()
        if any(str(row.get("测线编号", "")).strip() == line_no for row in rows):
            return None, [f"测线编号 {line_no} 已存在"]
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["revision"] = 0   # 登记站在修订号 0，尚未发生推进
        entry["version"] = 1
        entry["last_seq"] = 0
        rows.append(entry)
        self._upsert_pending(entry)
        return entry, []

    # ---------- 核心入口：带操作序号的提交 ----------
    def submit(
        self,
        entry_id: int,
        action: str,
        *,
        seq: int | None = None,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """返回 (当前/回放快照, 说明, 是否修订号冲突)。

        seq：客户端操作序号，从 1 起单调递增；重复序号回放断点结果。
        expected_revision：提交时该测线应处的修订号，不一致即并发冲突。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"物探测线 {entry_id} 不存在", False
        line_no = str(entry.get("测线编号", ""))
        with _line_locks[entry_id]:
            last_seq = int(entry.get("last_seq", 0))
            # 断点续跑：同一操作序号已落地过，原结果原样回放，不再推进第二次
            if seq is not None and seq <= last_seq:
                replay = self._find_op(entry_id, seq)
                if replay is not None:
                    return deepcopy(replay["result"]), f"操作序号 {seq} 已落地，按断点回放结果（{replay.get('message', '')}）", False
                return None, f"操作序号 {seq} 已越过断点 {last_seq}，结果无法回放，请核对后重新提交", False
            if seq is not None and seq != last_seq + 1:
                return None, f"操作序号 {seq} 与断点 {last_seq + 1} 不连续，被打断的提交请从断点序号接着走", False
            # 乐观锁：进入临界区后再核对修订号，拦住并发里的后来者
            if expected_revision is not None and int(expected_revision) != int(entry.get("revision", 0)):
                return None, (
                    f"测线 {line_no} 已被其他提交推进到修订号 {entry.get('revision')}，"
                    f"本次基于修订号 {expected_revision} 的提交已拒绝，请刷新后重试"
                ), True
            if action == REVISE_ACTION:
                return self._apply_withdraw(entry, seq)
            if action not in FORWARD_ACTIONS:
                return None, f"动作「{action}」不属于物探测线单向流程可执行范围", False
            return self._apply_forward(entry, action, seq)

    def _apply_forward(
        self, entry: dict[str, Any], action: str, seq: int | None
    ) -> tuple[dict[str, Any], str, bool]:
        line_no = str(entry.get("测线编号", ""))
        current = str(entry.get("status"))
        current_idx = STATUS_ORDER.index(current) if current in STATUS_ORDER else -1
        target = FORWARD_ACTIONS[action]
        target_idx = STATUS_ORDER.index(target)
        # 老测线理论上在启动时已迁移；这里再兜一道底
        if current_idx < 0:
            return None, f"测线 {line_no} 状态缺失，请先执行老测线状态迁移", False
        # 倒序回退拦下
        if target_idx <= current_idx:
            return None, (
                f"测线 {line_no} 当前为「{current}」，动作「{action}」属于倒序回退，单向流程不允许回退"
            ), False
        # 跳级提交拦下：只接受相邻一站
        if target_idx != current_idx + 1:
            return None, (
                f"测线 {line_no} 当前为「{current}」，不能直接{action}到「{target}」，"
                f"请先完成「{NEXT_ACTIONS[current]}」"
            ), False

        with _revision_lock:
            revision = self._next_revision()
            snapshot = deepcopy(entry)
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["revision"] = revision
            if seq is not None:
                entry["last_seq"] = seq
            message = f"测线 {line_no} 已{action}（{current}→{target}），修订号 {revision}"
            self._sync_outlets(entry, action, current, target, revision, note="推进")
            self._record_op(entry, seq, action, message)
        return deepcopy(entry), message, False

    def _apply_withdraw(
        self, entry: dict[str, Any], seq: int | None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        line_no = str(entry.get("测线编号", ""))
        current = str(entry.get("status"))
        if current != STATUS_ORDER[-1]:
            return None, (
                f"测线 {line_no} 当前为「{current}」，只有「{STATUS_ORDER[-1]}」的测线允许撤回；"
                "流程中的测线不能倒序回退"
            ), False

        with _revision_lock:
            revision = self._next_revision()
            old_version = int(entry.get("version", 1))
            # 历史版本先留档：长度与数据质量按原始上报口径，原样冻结
            archived = deepcopy(entry)
            self._append_revision(
                archived,
                revision=revision,
                version=old_version,
                action=REVISE_ACTION,
                from_status=STATUS_ORDER[-1],
                to_status=STATUS_ORDER[-1],
                note="撤回留档（原始上报口径）",
            )
            # 当前记录开新修订版本，回到流程起点重走
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["revision"] = revision
            entry["version"] = old_version + 1
            entry["施测日期"] = None
            if seq is not None:
                entry["last_seq"] = seq
            message = (
                f"测线 {line_no} 已撤回归档，按新版本 V{entry['version']} 从「{STATUS_ORDER[0]}」重走，"
                f"修订号 {revision}；历史长度与数据质量按原始口径留存"
            )
            self._sync_outlets(
                entry, REVISE_ACTION, STATUS_ORDER[-1], STATUS_ORDER[0], revision, note="撤回新版本"
            )
            self._record_op(entry, seq, REVISE_ACTION, message)
        return deepcopy(entry), message, False

    # ---------- 三处同步：同一修订号同批摆放 ----------
    def _sync_outlets(
        self,
        entry: dict[str, Any],
        action: str,
        from_status: str,
        to_status: str,
        revision: int,
        *,
        note: str,
    ) -> None:
        line_no = str(entry.get("测线编号", ""))
        version = int(entry.get("version", 1))

        # 1) 勘探区采集台账：流水追加，每次推进一行，修订号即台账序号
        self._table(LEDGER_TABLE).append({
            "revision": revision,
            "测线编号": line_no,
            "version": version,
            "勘探区": entry.get("勘探区"),
            "物探方法": entry.get("物探方法"),
            "动作": action,
            "前态": from_status,
            "后态": to_status,
            "测线长度": entry.get("测线长度"),
            "数据质量": entry.get("数据质量"),
            "备注": note,
            "时间": _now(),
        })

        # 2) 成果图清单：按测线 upsert 当前版本，挂同一修订号
        maps = self._table(MAPS_TABLE)
        maps[:] = [row for row in maps if not self._same_line_version(row, line_no, version)]
        maps.append({
            "revision": revision,
            "测线编号": line_no,
            "version": version,
            "勘探区": entry.get("勘探区"),
            "物探方法": entry.get("物探方法"),
            "成图状态": MAP_PROGRESS.get(to_status, "待出图"),
            "测线状态": to_status,
            "更新时间": _now(),
        })

        # 3) 待处理列表：在流程中 upsert（带下一步动作），到终点站撤下；撤回重走重新挂回
        pending = self._table(PENDING_TABLE)
        pending[:] = [row for row in pending if not self._same_line_version(row, line_no, version)]
        if to_status in NEXT_ACTIONS:
            pending.append({
                "revision": revision,
                "测线编号": line_no,
                "version": version,
                "勘探区": entry.get("勘探区"),
                "当前状态": to_status,
                "下一步动作": NEXT_ACTIONS[to_status],
                "挂起时间": _now(),
            })

        self._append_revision(
            entry,
            revision=revision,
            version=version,
            action=action,
            from_status=from_status,
            to_status=to_status,
            note=note,
        )

    def _append_revision(
        self,
        entry: dict[str, Any],
        *,
        revision: int,
        version: int,
        action: str,
        from_status: str,
        to_status: str,
        note: str,
    ) -> None:
        # 长度/数据质量取当时记录值：撤回留档时即原始上报口径，不做任何改写
        self._table(REVISIONS_TABLE).append({
            "revision": revision,
            "测线编号": entry.get("测线编号"),
            "version": version,
            "动作": action,
            "前态": from_status,
            "后态": to_status,
            "测线长度": entry.get("测线长度"),
            "数据质量": entry.get("数据质量"),
            "施测日期": entry.get("施测日期"),
            "备注": note,
            "时间": _now(),
        })

    # ---------- 操作序号留痕（断点续跑） ----------
    def _record_op(self, entry: dict[str, Any], seq: int | None, action: str, message: str) -> None:
        if seq is None:
            return
        self._table(OPS_TABLE).append({
            "entry_id": entry.get("id"),
            "测线编号": entry.get("测线编号"),
            "seq": seq,
            "action": action,
            "revision": entry.get("revision"),
            "message": message,
            "result": deepcopy(entry),
            "时间": _now(),
        })

    def _find_op(self, entry_id: int, seq: int) -> dict[str, Any] | None:
        for op in self._table(OPS_TABLE):
            if op.get("entry_id") == entry_id and int(op.get("seq", -1)) == seq:
                return op
        return None

    # ---------- 三处同步的只读视图 ----------
    def outlet(self, name: str, keyword: str | None = None) -> list[dict[str, Any]]:
        rows = self._table(name)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("测线编号", ""))]
        return rows

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUS_ORDER}
        for row in store.rows(MODULE):
            status = str(row.get("status", ""))
            if status in counts:
                counts[status] += 1
        return counts

    # ---------- 启动引导：修订号初始化 + 老测线迁移 ----------
    def bootstrap(self) -> dict[str, int]:
        """幂等：给现有测线补修订号/版本号，把缺失状态的老测线按施测先后补齐。"""
        migrated = 0
        for row in store.rows(MODULE):
            row.setdefault("revision", 0)
            row.setdefault("version", 1)
            row.setdefault("last_seq", 0)
        migrated = self.migrate_legacy_statuses()
        # 待处理列表与当前状态对齐（幂等重建），三处口径一致
        self._table(PENDING_TABLE).clear()
        for row in store.rows(MODULE):
            if str(row.get("status")) in NEXT_ACTIONS:
                self._upsert_pending(row)
        return {"migrated": migrated}

    def migrate_legacy_statuses(self) -> int:
        """老测线缺失/非法状态：按施测先后（施测日期、再按 id）循环补齐标准状态。"""
        rows = store.rows(MODULE)
        legacy = [row for row in rows if str(row.get("status", "")) not in STATUS_ORDER]
        if not legacy:
            return 0
        legacy.sort(key=lambda row: (str(row.get("施测日期") or "9999-99-99"), int(row.get("id", 0))))
        # 接续已合规老测线的站位，按施测先后沿单向序列摆放
        cursor = -1
        for row in legacy:
            cursor = (cursor + 1) % len(STATUS_ORDER)
            new_status = STATUS_ORDER[cursor]
            row["status"] = new_status
            row["pending"] = new_status != STATUS_ORDER[-1]
            row["revision"] = 0
            row["version"] = int(row.get("version", 1))
            row["last_seq"] = int(row.get("last_seq", 0))
        return len(legacy)

    # ---------- 内部小工具 ----------
    def _table(self, name: str) -> list[dict[str, Any]]:
        return store.rows(name)

    @staticmethod
    def _same_line_version(row: dict[str, Any], line_no: str, version: int) -> bool:
        return str(row.get("测线编号", "")) == line_no and int(row.get("version", 1)) == version

    def _upsert_pending(self, entry: dict[str, Any]) -> None:
        status = str(entry.get("status"))
        if status not in NEXT_ACTIONS:
            return
        pending = self._table(PENDING_TABLE)
        line_no = str(entry.get("测线编号", ""))
        version = int(entry.get("version", 1))
        pending[:] = [row for row in pending if not self._same_line_version(row, line_no, version)]
        pending.append({
            "revision": int(entry.get("revision", 0)),
            "测线编号": line_no,
            "version": version,
            "勘探区": entry.get("勘探区"),
            "当前状态": status,
            "下一步动作": NEXT_ACTIONS[status],
            "挂起时间": _now(),
        })

    def _next_revision(self) -> int:
        meta = self._table(META_TABLE)
        record = next((row for row in meta if row.get("key") == "revision"), None)
        if record is None:
            record = {"key": "revision", "value": 0}
            meta.append(record)
        record["value"] = int(record["value"]) + 1
        return int(record["value"])
