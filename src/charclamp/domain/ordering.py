"""顶部窑剪影行的排列规则。

排列只表达窑的左右顺序，不改变窑态、窑号等任何窑自身属性。
未保存过排列（或保存数据与现有窑集合不一致）时回退到窑号序；
保存后新增的窑追加到末尾，保证每个窑恰好出现一次。
"""

from __future__ import annotations

from charclamp.domain.models import Clamp


def fallback_ordering(clamps: list[Clamp]) -> list[int]:
    return [c.id for c in sorted(clamps, key=lambda c: (c.code, c.id))]


def resolve_ordering(clamps: list[Clamp], saved: list[int] | None) -> list[int]:
    """把保存的排列与现有窑集合对齐，返回现有窑 id 的完整去重顺序。"""
    by_id = {c.id: c for c in clamps}
    ordered: list[int] = []
    seen: set[int] = set()
    if saved:
        for clamp_id in saved:
            if clamp_id in by_id and clamp_id not in seen:
                ordered.append(clamp_id)
                seen.add(clamp_id)
    # 保存后新增（或保存数据里缺失）的窑，按窑号序追加到末尾
    for clamp_id in sorted((by_id.keys() - seen), key=lambda cid: (by_id[cid].code, cid)):
        ordered.append(clamp_id)
    return ordered


def order_clamps(clamps: list[Clamp], saved: list[int] | None) -> list[Clamp]:
    by_id = {c.id: c for c in clamps}
    return [by_id[cid] for cid in resolve_ordering(clamps, saved) if cid in by_id]
