from __future__ import annotations

from datetime import timedelta

from charclamp.domain.models import BoardOrder, BurnShift, Clamp, Site, User, utcnow
from charclamp.infra.db import SyncSessionLocal
from charclamp.infra.security import hash_password


def _ensure_board_order(session) -> None:
    """每次启动幂等校准顶栏排列单行：缺则按窑号建默认序，新增窑补到末尾。"""
    clamps = list(session.query(Clamp).order_by(Clamp.code).all())
    row = session.get(BoardOrder, BoardOrder.SINGLETON_ID)
    valid_ids = {c.id for c in clamps}
    if row is None:
        if clamps:
            session.add(
                BoardOrder(
                    id=BoardOrder.SINGLETON_ID,
                    clamp_ids=",".join(str(c.id) for c in clamps),
                    version=0,
                )
            )
        return
    known = [cid for cid in (row.clamp_ids.split(",") if row.clamp_ids else []) if cid.isdigit() and int(cid) in valid_ids]
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in known:
        cid = int(raw)
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    ordered.extend(c.id for c in clamps if c.id not in seen)
    new_value = ",".join(str(cid) for cid in ordered)
    if new_value != row.clamp_ids:
        row.clamp_ids = new_value
        row.version += 1
        row.updated_at = utcnow()


def seed_demo() -> None:
    with SyncSessionLocal() as session:
        admin = session.query(User).filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin", role="admin", password_hash=hash_password("123456"))
            session.add(admin)
        else:
            admin.password_hash = hash_password("123456")
            admin.role = "admin"

        worker = session.query(User).filter_by(username="worker").first()
        if not worker:
            worker = User(username="worker", role="worker", password_hash=hash_password("123456"))
            session.add(worker)
        else:
            worker.password_hash = hash_password("123456")
            worker.role = "worker"

        if session.query(Site).first():
            _ensure_board_order(session)
            session.commit()
            return

        site = Site(name="乌石岗焖烧坞", location="河谷台地北侧", notes="青冈为主，夜班闷窑")
        session.add(site)
        session.flush()

        c1 = Clamp(site=site, code="坞东-甲", status=Clamp.STATUS_BURNING, wood_species="青冈")
        c2 = Clamp(site=site, code="坞东-乙", status=Clamp.STATUS_STACKED, wood_species="松木")
        c3 = Clamp(site=site, code="河沿-丙", status=Clamp.STATUS_DRAWN, wood_species="栎木")
        session.add_all([c1, c2, c3])
        session.flush()

        now = utcnow()
        session.add_all(
            [
                BurnShift(
                    clamp=c1,
                    started_at=now - timedelta(hours=10),
                    peak_temp_c=455.0,
                    charcoal_grade="A",
                    notes="峰值已过，可出炭",
                ),
                BurnShift(
                    clamp=c2,
                    started_at=now - timedelta(hours=3),
                    peak_temp_c=None,
                    charcoal_grade="B",
                    notes="刚点火，未测峰值",
                ),
                BurnShift(
                    clamp=c3,
                    started_at=now - timedelta(days=2),
                    peak_temp_c=520.0,
                    charcoal_grade="A+",
                    notes="已出炭班次",
                ),
            ]
        )
        session.flush()
        session.add(
            BoardOrder(
                id=BoardOrder.SINGLETON_ID,
                clamp_ids=",".join(str(cid) for cid in (c1.id, c2.id, c3.id)),
                version=0,
            )
        )
        session.commit()
