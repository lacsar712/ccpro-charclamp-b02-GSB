from __future__ import annotations

from datetime import datetime
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Response, Template
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BoardOrder, BurnShift, Clamp, User, utcnow
from charclamp.domain.rules import (
    RuleError,
    assert_can_set_clamp_status,
    can_mark_clamp_drawn,
    parse_clamp_order,
)
from charclamp.infra.db import SessionLocal
from charclamp.infra.security import verify_password

STATUS_LABELS = {
    Clamp.STATUS_STACKED: "已码窑",
    Clamp.STATUS_BURNING: "焖烧中",
    Clamp.STATUS_DRAWN: "已出炭",
}


def _set_flash(request: Request, message: str, category: str = "ok") -> None:
    data = dict(request.session or {})
    data["flash"] = message
    data["flash_cat"] = category
    request.set_session(data)


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    data = dict(request.session or {})
    message = data.pop("flash", None)
    category = data.pop("flash_cat", None)
    if message is not None or category is not None:
        request.set_session(data)
    return message, category


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _order_clamps(clamps: list[Clamp], board_order: BoardOrder | None) -> list[Clamp]:
    """按 BoardOrder 单行重排窑剪影；未登记/异常 id 回退并按窑号补尾。"""
    by_id = {c.id: c for c in clamps}
    if board_order is None or not board_order.clamp_ids:
        return sorted(clamps, key=lambda c: c.code)
    ordered: list[Clamp] = []
    seen: set[int] = set()
    for token in board_order.clamp_ids.split(","):
        token = token.strip()
        if token.isdigit():
            cid = int(token)
            clamp = by_id.get(cid)
            if clamp is not None and cid not in seen:
                seen.add(cid)
                ordered.append(clamp)
    ordered.extend(sorted((c for c in clamps if c.id not in seen), key=lambda c: c.code))
    return ordered


async def _load_timeline_context(
    clamp_id: int | None = None,
    order_edit: bool = False,
) -> dict[str, Any]:
    async with SessionLocal() as db:
        clamps = list(
            (
                await db.execute(
                    select(Clamp)
                    .options(selectinload(Clamp.site), selectinload(Clamp.shifts))
                    .order_by(Clamp.code)
                )
            )
            .scalars()
            .all()
        )
        board_order = (
            await db.execute(
                select(BoardOrder).where(BoardOrder.id == BoardOrder.SINGLETON_ID)
            )
        ).scalar_one_or_none()
        clamps = _order_clamps(clamps, board_order)
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
        "board_version": board_order.version if board_order else 0,
        "order_edit": order_edit,
    }


def _board_template(
    ctx: dict[str, Any],
    user: User,
    notice: str | None = None,
    notice_cat: str = "ok",
    status_code: int = 200,
) -> Template:
    return Template(
        template_name="partials/board.html",
        context={
            **ctx,
            "user": user,
            "board_notice": notice,
            "board_notice_cat": notice_cat,
        },
        status_code=status_code,
    )


def _is_admin(user: User | None) -> bool:
    return bool(user and user.role == "admin")


class AuthController(Controller):
    path = ""
    tags = ["auth"]

    @get("/login", media_type=MediaType.HTML)
    async def login_page(self, request: Request) -> Template:
        flash, flash_cat = _pop_flash(request)
        return Template(
            template_name="login.html",
            context={"flash": flash, "flash_cat": flash_cat},
        )

    @post("/login")
    async def login(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user or not verify_password(password, user.password_hash):
                request.set_session({"flash": "用户名或密码错误", "flash_cat": "error"})
                return Redirect("/login")
            request.set_session({"user_id": user.id})
        return Redirect("/")

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.clear_session()
        return Redirect("/login")


class TimelineController(Controller):
    path = ""
    tags = ["timeline"]

    @get("/", media_type=MediaType.HTML)
    async def timeline(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="timeline.html",
            context={
                **ctx,
                "user": request.user,
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @get("/timeline/partial", media_type=MediaType.HTML)
    async def timeline_partial(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return _board_template(ctx, request.user)

    @get("/board/order/edit", media_type=MediaType.HTML)
    async def board_order_edit(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request.user):
            return Redirect("/")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id, order_edit=True)
        return _board_template(ctx, request.user)

    @post("/board/order")
    async def board_order_save(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Response | Redirect:
        # 服务端硬权限：操作工只有读权限，任何改序提交一律拒绝（不走重定向掩盖）。
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request.user):
            # HTMX 对 4xx 不做内容替换：操作工界面没有入口，直连接口也只得到 403。
            return Response(
                "仅管理员可调整窑剪影排列",
                status_code=403,
                media_type=MediaType.TEXT,
            )

        clamp_id = _parse_optional_int(data.get("clamp_id") or None)
        expected_version = _parse_optional_int(data.get("version"))
        raw_order = data.get("order")

        async with SessionLocal() as db:
            valid_ids = set((await db.execute(select(Clamp.id))).scalars().all())
            try:
                order = parse_clamp_order(raw_order, valid_ids)
            except RuleError as exc:
                ctx = await _load_timeline_context(clamp_id, order_edit=True)
                return _board_template(ctx, request.user, str(exc), "error", 422)

            if expected_version is None:
                ctx = await _load_timeline_context(clamp_id, order_edit=True)
                return _board_template(ctx, request.user, "缺少排列版本号，未保存", "error", 422)

            new_value = ",".join(str(cid) for cid in order)
            # 单条条件更新 = 比较并设置 + 行锁：并发两名管理员只有一人 rowcount=1。
            result = await db.execute(
                update(BoardOrder)
                .where(
                    BoardOrder.id == BoardOrder.SINGLETON_ID,
                    BoardOrder.version == expected_version,
                )
                .values(
                    clamp_ids=new_value,
                    version=BoardOrder.version + 1,
                    updated_at=utcnow(),
                )
            )
            await db.commit()

        if result.rowcount == 1:
            ctx = await _load_timeline_context(clamp_id)
            return _board_template(ctx, request.user, "窑剪影排列已保存", "ok")

        # rowcount=0 可能是并发冲突，也可能是单行尚未建立；查一次以区分。
        current = (
            await db.execute(
                select(BoardOrder).where(BoardOrder.id == BoardOrder.SINGLETON_ID)
            )
        ).scalar_one_or_none()
        if current is None:
            # 极端环境（建表未播种）：幂等补建单行，仍不允许覆盖任何并发写入。
            try:
                db.add(
                    BoardOrder(
                        id=BoardOrder.SINGLETON_ID,
                        clamp_ids=new_value,
                        version=1,
                    )
                )
                await db.commit()
            except IntegrityError:
                # 另一管理员已抢先补建：本版不入库。
                await db.rollback()
                ctx = await _load_timeline_context(clamp_id, order_edit=False)
                return _board_template(
                    ctx,
                    request.user,
                    "排列已被其他管理员更新，当前显示最新顺序，如需调整请重新编辑",
                    "error",
                    409,
                )
            ctx = await _load_timeline_context(clamp_id)
            return _board_template(ctx, request.user, "窑剪影排列已保存", "ok")

        # 版本已被另一名管理员推进：本次不落库，整页/局部统一回显胜者版本。
        ctx = await _load_timeline_context(clamp_id, order_edit=False)
        return _board_template(
            ctx,
            request.user,
            "排列已被其他管理员更新，当前显示最新顺序，如需调整请重新编辑",
            "error",
            409,
        )

    @get("/drawer/shift-new", media_type=MediaType.HTML)
    async def drawer_shift_new(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        async with SessionLocal() as db:
            clamps = list(
                (
                    await db.execute(
                        select(Clamp).options(selectinload(Clamp.site)).order_by(Clamp.code)
                    )
                )
                .scalars()
                .all()
            )
            board_order = (
                await db.execute(
                    select(BoardOrder).where(BoardOrder.id == BoardOrder.SINGLETON_ID)
                )
            ).scalar_one_or_none()
            clamps = _order_clamps(clamps, board_order)
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "user": request.user,
            },
        )

    @get("/drawer/clamp/{clamp_id:int}", media_type=MediaType.HTML)
    async def drawer_clamp(self, request: Request, clamp_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts), selectinload(Clamp.site))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
        can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "user": request.user,
            },
        )


class ShiftController(Controller):
    path = "/shifts"
    tags = ["shifts"]

    @post("/new")
    async def create_shift(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        started_raw = data.get("started_at") or ""
        started_at = datetime.fromisoformat(started_raw) if started_raw else datetime.utcnow()
        peak_raw = (data.get("peak_temp_c") or "").strip()
        peak = float(peak_raw) if peak_raw else None
        clamp_id = int(data["clamp_id"])
        async with SessionLocal() as db:
            shift = BurnShift(
                clamp_id=clamp_id,
                started_at=started_at,
                peak_temp_c=peak,
                charcoal_grade=(data.get("charcoal_grade") or "B").strip(),
                notes=(data.get("notes") or "").strip(),
            )
            db.add(shift)
            clamp = (
                await db.execute(select(Clamp).where(Clamp.id == clamp_id))
            ).scalar_one_or_none()
            if clamp and clamp.status == Clamp.STATUS_STACKED:
                clamp.status = Clamp.STATUS_BURNING
            await db.commit()
        _set_flash(request, "焖烧班次已登记", "ok")
        return Redirect(f"/?clamp_id={clamp_id}")


class ClampController(Controller):
    path = "/clamps"
    tags = ["clamps"]

    @post("/{clamp_id:int}/status")
    async def set_status(
        self,
        request: Request,
        clamp_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        new_status = (data.get("status") or "").strip()
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            try:
                assert_can_set_clamp_status(clamp, new_status)
                clamp.status = new_status
                await db.commit()
                _set_flash(request, f"窑 {clamp.code} 状态已更新", "ok")
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")
