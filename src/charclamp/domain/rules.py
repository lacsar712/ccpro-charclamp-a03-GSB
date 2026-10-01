"""炭窑焖烧志业务规则。"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from charclamp.domain.models import BurnShift, Clamp, IgnitionPermit, utcnow

MIN_PEAK_TEMP_FOR_DRAWN = 400.0

# 点火许可编号：全坞唯一，4 到 8 位数字。
PERMIT_CODE_PATTERN = re.compile(r"^[0-9]{4,8}$")


class RuleError(ValueError):
    """业务规则校验失败。"""


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    return max(clamp.shifts, key=lambda s: s.started_at)


def can_mark_clamp_drawn(clamp: Clamp) -> tuple[bool, str]:
    """
    炭窑转为「已出炭」(drawn) 的前提：
    最近一条焖烧班次的峰值温度已记录，且 >= 400℃。
    """
    latest = latest_shift_for_clamp(clamp)
    if latest is None:
        return False, "该窑尚无焖烧班次，不能标记为已出炭"
    if latest.peak_temp_c is None:
        return False, "最近班次尚未记录峰值温度，不能标记为已出炭"
    if latest.peak_temp_c < MIN_PEAK_TEMP_FOR_DRAWN:
        return (
            False,
            f"最近班次峰值温度 {latest.peak_temp_c}℃ 低于 {MIN_PEAK_TEMP_FOR_DRAWN:.0f}℃，不能标记为已出炭",
        )
    return True, ""


def assert_can_set_clamp_status(clamp: Clamp, new_status: str) -> None:
    allowed = {Clamp.STATUS_STACKED, Clamp.STATUS_BURNING, Clamp.STATUS_DRAWN}
    if new_status not in allowed:
        raise RuleError(f"无效状态：{new_status}")
    if new_status == Clamp.STATUS_DRAWN:
        ok, msg = can_mark_clamp_drawn(clamp)
        if not ok:
            raise RuleError(msg)


# —— 点火许可帖 ——


def normalize_permit_code(raw: str) -> str:
    code = (raw or "").strip()
    if not PERMIT_CODE_PATTERN.fullmatch(code):
        raise RuleError("许可编号须为 4 到 8 位数字")
    return code


def assert_can_open_permit(clamp: Clamp, has_open_permit: bool) -> None:
    """仅已码窑可开帖；未核销期间同一窑不得再开第二张。"""
    if clamp.status != Clamp.STATUS_STACKED:
        raise RuleError(f"窑 {clamp.code} 当前不是已码窑，不能开点火许可帖")
    if has_open_permit:
        raise RuleError(f"窑 {clamp.code} 已持有未核销点火许可帖，不得重复开帖")


async def find_open_permit(
    db: AsyncSession, clamp_id: int, lock: bool = False
) -> IgnitionPermit | None:
    stmt = select(IgnitionPermit).where(
        IgnitionPermit.clamp_id == clamp_id,
        IgnitionPermit.consumed_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def register_burn_shift(
    db: AsyncSession,
    *,
    clamp_id: int,
    started_at: datetime,
    peak_temp_c: float | None,
    charcoal_grade: str,
    notes: str,
) -> tuple[BurnShift, Clamp]:
    """
    登记焖烧班次（单一事务，由调用方 commit）：
      - 先以 SELECT ... FOR UPDATE 锁定窑行，串行化同窑并发首班；
      - 已码窑必须存在未核销许可帖，先在本事务写核销时刻；
      - 班次入库，随后窑态改焖烧中；
      - 已是焖烧中的窑追加班次不必持新帖。
    任一步失败由调用方回滚，保证「核销与班次同生死」。
    """
    clamp = (
        await db.execute(select(Clamp).where(Clamp.id == clamp_id).with_for_update())
    ).scalar_one_or_none()
    if clamp is None:
        raise RuleError("炭窑不存在")

    permit: IgnitionPermit | None = None
    if clamp.status == Clamp.STATUS_STACKED:
        permit = await find_open_permit(db, clamp_id, lock=True)
        if permit is None:
            raise RuleError(
                f"窑 {clamp.code} 为已码窑且无未核销点火许可帖，不能登记首笔班次"
            )

    shift = BurnShift(
        clamp_id=clamp_id,
        started_at=started_at,
        peak_temp_c=peak_temp_c,
        charcoal_grade=charcoal_grade,
        notes=notes,
    )
    db.add(shift)

    if permit is not None:
        # 核销时刻与班次在同一事务写入：先核销，再翻窑态。
        permit.consumed_at = utcnow()
        clamp.status = Clamp.STATUS_BURNING

    await db.flush()
    return shift, clamp


async def consume_open_permit(db: AsyncSession, permit_id: int) -> IgnitionPermit:
    """管理员手动核销一张帖（与登记班次共用的核销语义）。"""
    permit = (
        await db.execute(
            select(IgnitionPermit).where(IgnitionPermit.id == permit_id).with_for_update()
        )
    ).scalar_one_or_none()
    if permit is None:
        raise RuleError("点火许可帖不存在")
    if permit.consumed_at is not None:
        raise RuleError("该点火许可帖已核销，请勿重复操作")
    permit.consumed_at = utcnow()
    await db.flush()
    return permit
