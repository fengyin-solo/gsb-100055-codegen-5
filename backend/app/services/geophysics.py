"""地球物理业务规则：单向流程、修订号台账、撤回改版、并发闸与老测线迁移都收在这里。

状态链固定为：待施测 → 施测中 → 已采集 → 数据处理 → 已归档。
每次推进都带一个全库单调递增的「修订号」，并把同一修订号原子同步到三处台账：
勘探区采集台账、成果图清单、待处理列表。撤回已归档测线不改老版本，而是开一个
新修订版本从待施测重走。同一测线的并发推进用操作序号做闸：同序号续跑、旧序号
冲突、跳序号拒绝，保证一条测线不会被推进两次。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import date
from typing import Any

from app.store import store

MODULE = "geophysics"
EVENT_TABLE = "geophysics_events"
LEDGER_ACQ = "geophysics_ledger_acq"
LEDGER_MAPS = "geophysics_ledger_maps"
LEDGER_PENDING = "geophysics_ledger_pending"

REQUIRED_FIELDS = ["测线编号", "勘探区", "物探方法"]
OPTIONAL_FIELDS = ["测线长度", "点距", "施测日期", "数据质量"]
STATUS_ORDER = ["待施测", "施测中", "已采集", "数据处理", "已归档"]
ARCHIVED = STATUS_ORDER[-1]

# 正向动作与目标状态一一对应，只允许走到「下一格」
FORWARD_ACTIONS = {
    "开始施测": "施测中",
    "完成采集": "已采集",
    "提交处理": "数据处理",
    "归档": "已归档",
}
# 撤回已归档改版时，新修订版本需要重新上报的字段（历史版本里按原始口径留存）
RESURVEY_CLEAR_FIELDS = ["测线长度", "数据质量", "施测日期"]
MAP_READY_STATUSES = {"已采集", "数据处理", "已归档"}


class ConcurrentUpdate(Exception):
    """同一测线并发核对失败：操作序号已过期或修订号已变。"""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class _LineGate:
    """单条测线的串行闸：锁、操作序号水位与被打断提交的续跑快照。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_seq: int = 0
        self.cached: dict[int, dict[str, Any]] = {}


class GeophysicsService:
    def __init__(self) -> None:
        self._gates_lock = threading.Lock()
        self._gates: dict[str, _LineGate] = {}
        self._rev_lock = threading.Lock()
        self.migrate_legacy_rows()
        self._baseline_existing_rows()

    # ---------- 基础读取 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("测线编号", ""))]
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
        line_no = str(values["测线编号"]).strip()
        if any(str(row.get("测线编号", "")).strip() == line_no for row in rows):
            return None, [f"测线编号 {line_no} 已存在，同一测线不能重复登记"]
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: str(values.get(field) or "").strip() for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            entry[field] = str(values.get(field) or "").strip()
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["revision"] = 1
        entry["history"] = []
        rows.append(entry)
        self._append_event(
            entry,
            action="登记测线",
            from_status=None,
            to_status=STATUS_ORDER[0],
            kind="create",
        )
        self._sync_ledgers(entry)
        return entry, []

    # ---------- 单向推进 ----------

    def run_action(
        self, entry_id: int, action: str, seq: int | None = None, client_revision: int | None = None
    ) -> tuple[dict[str, Any] | None, str, bool, int | None]:
        """推进一条测线。

        返回 (记录, 说明, 是否断点续跑, 建议HTTP状态码)。业务拦截给 200+ok=False，
        并发核对失败抛 ConcurrentUpdate（409）。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"物探测线 {entry_id} 不存在", False, 404
        line_no = str(entry.get("测线编号", ""))
        gate = self._gate_for(line_no)

        with gate.lock:
            self._expect_seq(gate, seq, entry, client_revision)
            resumed = False

            if seq is not None and seq in gate.cached:
                cached = gate.cached[seq]
                if cached.get("action") != action:
                    raise ConcurrentUpdate(
                        f"操作序号 {seq} 已用于动作「{cached.get('action')}」，不能再提交「{action}」"
                    )
                resumed = True
                return cached["entry"], f"物探测线已{action}（操作序号 {seq} 断点续跑）", resumed, None

            current = str(entry.get("status") or "")
            if action not in FORWARD_ACTIONS:
                return None, f"动作「{action}」不属于地球物理可执行范围", resumed, None
            target = FORWARD_ACTIONS[action]
            if current not in STATUS_ORDER:
                return None, f"当前状态「{current}」不在允许的状态序列里，请先补齐老测线状态", resumed, None
            cur_idx, target_idx = STATUS_ORDER.index(current), STATUS_ORDER.index(target)
            if target_idx <= cur_idx:
                return None, f"流程单向推进，{current} 不能回退到 {target}", resumed, None
            if target_idx != cur_idx + 1:
                return None, (
                    f"禁止跳级提交：{current} 只能推进到 {STATUS_ORDER[cur_idx + 1]}，"
                    f"不能直接到 {target}"
                ), resumed, None

            entry["status"] = target
            entry["pending"] = target != ARCHIVED
            entry["abnormal"] = False
            self._append_event(entry, action=action, from_status=current, to_status=target, kind="advance")
            self._sync_ledgers(entry)

            if seq is not None:
                gate.cached[seq] = {"action": action, "entry": deepcopy(entry)}
            return entry, f"物探测线已{action}，修订号 {entry['revision']}", resumed, None

    # ---------- 撤回已归档：开新修订版本 ----------

    def withdraw_archived(
        self, entry_id: int, seq: int | None = None, client_revision: int | None = None
    ) -> tuple[dict[str, Any] | None, str, bool, int | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"物探测线 {entry_id} 不存在", False, 404
        line_no = str(entry.get("测线编号", ""))
        gate = self._gate_for(line_no)

        with gate.lock:
            self._expect_seq(gate, seq, entry, client_revision)
            resumed = False

            if seq is not None and seq in gate.cached:
                cached = gate.cached[seq]
                if cached.get("action") != "撤回归档":
                    raise ConcurrentUpdate(
                        f"操作序号 {seq} 已用于动作「{cached.get('action')}」，不能再提交撤回"
                    )
                resumed = True
                return cached["entry"], cached["message"], resumed, None

            current = str(entry.get("status") or "")
            if current != ARCHIVED:
                return None, f"只有{ARCHIVED}测线允许撤回改版，当前为{current}", resumed, None

            # 历史版本原样封存：长度、质量等按原始上报口径留存，不就地改写
            snapshot = deepcopy(entry)
            history = entry.setdefault("history", [])
            history.append({k: snapshot.get(k) for k in snapshot if k != "history"})

            old_revision = int(entry.get("revision", 1))
            entry["revision"] = old_revision + 1
            for field in RESURVEY_CLEAR_FIELDS:
                entry[field] = ""
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False

            self._append_event(
                entry,
                action="撤回归档",
                from_status=ARCHIVED,
                to_status=STATUS_ORDER[0],
                kind="withdraw",
                note=f"历史修订 R{old_revision} 已封存",
            )
            self._sync_ledgers(entry)

            message = f"已归档测线撤回为新修订版本 R{entry['revision']}，历史 R{old_revision} 按原始口径留存"
            if seq is not None:
                gate.cached[seq] = {"action": "撤回归档", "entry": deepcopy(entry), "message": message}
            return entry, message, resumed, None

    # ---------- 三处台账（同一修订号摆放） ----------

    def ledgers(self) -> dict[str, Any]:
        acq = sorted(
            store.rows(LEDGER_ACQ),
            key=lambda row: (str(row.get("勘探区", "")), str(row.get("测线编号", "")), int(row.get("revision", 0))),
        )
        maps = sorted(
            store.rows(LEDGER_MAPS),
            key=lambda row: (str(row.get("测线编号", "")), int(row.get("revision", 0))),
        )
        pending = sorted(
            store.rows(LEDGER_PENDING),
            key=lambda row: (int(row.get("revision", 0)), str(row.get("测线编号", ""))),
        )
        revisions = sorted({int(row["revision"]) for row in store.rows(EVENT_TABLE)})
        return {
            "revision_head": revisions[-1] if revisions else 0,
            "acquisition_ledger": acq,
            "map_list": maps,
            "pending_list": pending,
            "events": sorted(
                store.rows(EVENT_TABLE), key=lambda row: int(row.get("revision", 0))
            ),
        }

    # ---------- 老测线缺失状态：按施测先后迁移补齐 ----------

    def migrate_legacy_rows(self) -> dict[str, int]:
        rows = store.rows(MODULE)
        legacy = [row for row in rows if row.get("status") not in STATUS_ORDER]
        if not legacy:
            return {"migrated": 0}

        legacy.sort(key=lambda row: (self._date_key(row.get("施测日期")), int(row.get("id", 0))))
        migrated: list[dict[str, Any]] = []
        for index, row in enumerate(legacy):
            target = STATUS_ORDER[index % len(STATUS_ORDER)]
            row.setdefault("history", [])
            row.setdefault("revision", 1)
            row["status"] = target
            row["pending"] = target != ARCHIVED
            row["abnormal"] = False
            self._append_event(
                row,
                action="老测线迁移",
                from_status=None,
                to_status=target,
                kind="migrate",
                note="缺失状态按施测先后顺序补齐",
            )
            self._sync_ledgers(row)
            migrated.append(row)
        return {"migrated": len(migrated)}

    def _baseline_existing_rows(self) -> None:
        """启动时给已有测线补登记基线：修订号/历史版本字段与三处台账一次对齐。"""
        existing = {str(row.get("测线编号", "")) for row in store.rows(EVENT_TABLE)}
        for row in store.rows(MODULE):
            line_no = str(row.get("测线编号", ""))
            if line_no in existing:
                row.setdefault("revision", 1)
                row.setdefault("history", [])
                continue
            row.setdefault("revision", 1)
            row.setdefault("history", [])
            self._append_event(
                row,
                action="台账建账",
                from_status=None,
                to_status=str(row.get("status", "")),
                kind="baseline",
            )
            self._sync_ledgers(row)

    # ---------- 内部实现 ----------

    def _gate_for(self, line_no: str) -> _LineGate:
        with self._gates_lock:
            gate = self._gates.get(line_no)
            if gate is None:
                gate = _LineGate()
                self._gates[line_no] = gate
            return gate

    def seq_head(self, line_no: str) -> int:
        """某条测线已受理的最大操作序号，供客户端断线/冲突后对齐续跑。"""
        with self._gates_lock:
            gate = self._gates.get(line_no)
            return gate.last_seq if gate is not None else 0

    def _expect_seq(
        self, gate: _LineGate, seq: int | None, entry: dict[str, Any], client_revision: int | None
    ) -> None:
        if client_revision is not None and int(client_revision) != int(entry.get("revision", 1)):
            raise ConcurrentUpdate(
                f"修订号已过期：本地 R{client_revision}，当前 R{entry.get('revision')}，请刷新后重试"
            )
        if seq is None:
            return
        if seq <= gate.last_seq and seq not in gate.cached:
            raise ConcurrentUpdate(f"操作序号 {seq} 已过期，该测线已被序号 {gate.last_seq} 推进，请刷新后重试")
        if seq > gate.last_seq + 1:
            raise ConcurrentUpdate(f"操作序号 {seq} 存在断号：期望 {gate.last_seq + 1}，禁止跳号提交")
        gate.last_seq = seq

    def _next_revision(self) -> int:
        with self._rev_lock:
            events = store.rows(EVENT_TABLE)
            return max((int(row.get("revision", 0)) for row in events), default=0) + 1

    def _append_event(
        self,
        entry: dict[str, Any],
        *,
        action: str,
        from_status: str | None,
        to_status: str,
        kind: str,
        note: str = "",
    ) -> dict[str, Any]:
        event = {
            "revision": self._next_revision(),
            "测线编号": entry.get("测线编号", ""),
            "勘探区": entry.get("勘探区", ""),
            "action": action,
            "kind": kind,
            "from_status": from_status,
            "to_status": to_status,
            "revision_of_line": int(entry.get("revision", 1)),
            "note": note,
            "occurred_on": date.today().isoformat(),
        }
        store.rows(EVENT_TABLE).append(event)
        return event

    def _sync_ledgers(self, entry: dict[str, Any]) -> None:
        """把当前修订版本同步到三处台账；三处修订号取自同一事件水位。"""
        revision = max(
            (int(row.get("revision", 0)) for row in store.rows(EVENT_TABLE)), default=0
        )
        line_no = str(entry.get("测线编号", ""))
        line_rev = int(entry.get("revision", 1))
        status = str(entry.get("status", ""))
        common = {
            "revision": revision,
            "line_revision": line_rev,
            "测线编号": line_no,
            "勘探区": entry.get("勘探区", ""),
            "物探方法": entry.get("物探方法", ""),
            "status": status,
        }

        # 勘探区采集台账：每个修订版本始终保留一行
        acq_row = {
            **common,
            "测线长度": entry.get("测线长度", ""),
            "点距": entry.get("点距", ""),
            "施测日期": entry.get("施测日期", ""),
            "数据质量": entry.get("数据质量", ""),
        }
        self._upsert(LEDGER_ACQ, acq_row, line_no, line_rev)

        # 成果图清单：自已采集起挂出，历史版本不摘牌（撤回改版不抹历史）
        if status in MAP_READY_STATUSES:
            map_row = {
                **common,
                "成果图状态": "可成图" if status != "已采集" else "待成图",
            }
            self._upsert(LEDGER_MAPS, map_row, line_no, line_rev)

        # 待处理列表：非已归档才挂；撤回后新版本重新挂回
        pending_rows = store.rows(LEDGER_PENDING)
        pending_rows[:] = [
            row
            for row in pending_rows
            if not (str(row.get("测线编号", "")) == line_no and int(row.get("line_revision", 0)) == line_rev)
        ]
        if status != ARCHIVED:
            cur_idx = STATUS_ORDER.index(status) if status in STATUS_ORDER else 0
            next_action = (
                list(FORWARD_ACTIONS)[cur_idx] if cur_idx < len(FORWARD_ACTIONS) else ""
            )
            pending_rows.append({**common, "next_action": next_action})

    def _upsert(self, table: str, row: dict[str, Any], line_no: str, line_rev: int) -> None:
        rows = store.rows(table)
        for index, existing in enumerate(rows):
            if str(existing.get("测线编号", "")) == line_no and int(existing.get("line_revision", 0)) == line_rev:
                rows[index] = row
                return
        rows.append(row)

    @staticmethod
    def _date_key(value: Any) -> tuple[int, ...]:
        text = str(value or "").strip()
        try:
            return tuple(int(part) for part in text.split("-"))
        except ValueError:
            # 没有可识别施测日期的排到最前，保持按施测先后迁移的稳定口径
            return (0,)


# 进程级单例：导入即完成老测线迁移，锁与序号水位也集中在这里
service = GeophysicsService()
