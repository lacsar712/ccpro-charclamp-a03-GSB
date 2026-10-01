"""炭窑焖烧志业务规则。"""

from __future__ import annotations

import re
from datetime import datetime

from charclamp.domain.models import BurnShift, Clamp, IgnitionPermit

MIN_PEAK_TEMP_FOR_DRAWN = 400.0

# 许可编号：4 到 8 位数字
PERMIT_NO_PATTERN = re.compile(r"^\d{4,8}$")


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


def validate_permit_no(permit_no: str) -> str:
    """许可编号必须为 4 到 8 位数字，全坞唯一由数据库约束兜底。"""
    permit_no = (permit_no or "").strip()
    if not PERMIT_NO_PATTERN.match(permit_no):
        raise RuleError("许可编号须为 4 到 8 位数字")
    return permit_no


def find_open_permit(permits: list[IgnitionPermit]) -> IgnitionPermit | None:
    """返回该窑当前未核销的点火许可帖。"""
    for permit in permits:
        if permit.revoked_at is None:
            return permit
    return None


def assert_can_open_permit(
    clamp: Clamp, permits: list[IgnitionPermit] | None = None
) -> None:
    """仅已码窑可开帖；未核销期间同一窑不得再开第二张。"""
    if clamp is None:
        raise RuleError("炭窑不存在")
    if clamp.status != Clamp.STATUS_STACKED:
        raise RuleError("仅已码窑可开点火许可帖")
    if permits is None:
        permits = list(clamp.permits)
    if find_open_permit(permits) is not None:
        raise RuleError("该窑已有未核销点火许可帖，核销前不得再开")


def assert_can_revoke_permit(permit: IgnitionPermit | None) -> None:
    if permit is None:
        raise RuleError("点火许可帖不存在")
    if permit.revoked_at is not None:
        raise RuleError("该帖已核销，无需重复核销")


def consume_permit_for_first_shift(
    clamp: Clamp,
    permits: list[IgnitionPermit],
    now: datetime,
) -> IgnitionPermit:
    """
    已码窑写入第一笔班次时的许可校验：
    必须存在未核销帖，并由调用方在同一事务内写入核销时刻。
    焖烧中窑追加班次无需调用本函数。
    """
    permit = find_open_permit(list(permits))
    if permit is None:
        raise RuleError("该已码窑尚无未核销点火许可帖，不能登记首笔班次")
    permit.revoked_at = now
    return permit
