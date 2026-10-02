"""炭窑焖烧志业务规则。"""

from __future__ import annotations

import re

from charclamp.domain.models import BurnShift, Clamp

MIN_PEAK_TEMP_FOR_DRAWN = 400.0

_ORDER_TOKEN_RE = re.compile(r"^\d+$")


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


def parse_clamp_order(raw: str | None, valid_ids: set[int]) -> list[int]:
    """解析顶部窑剪影排列提交值。

    要求：非空、全部为整数 id、无重复，且与现存窑 id 集合完全一致
    （既不能少排、漏排，也不能夹带不存在的窑）。排列只决定展示顺序，
    不允许借此改动任何窑态或窑号。
    """
    if raw is None or not raw.strip():
        raise RuleError("排列内容为空，未保存")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    order: list[int] = []
    for tok in tokens:
        if not _ORDER_TOKEN_RE.match(tok):
            raise RuleError("排列中存在非法窑编号，未保存")
        cid = int(tok)
        if cid in order:
            raise RuleError("排列中存在重复窑，未保存")
        if cid not in valid_ids:
            raise RuleError("排列中包含不存在的窑，未保存")
        order.append(cid)
    if set(order) != valid_ids:
        raise RuleError("排列必须且只能包含全部现有窑，未保存")
    return order
