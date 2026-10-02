from __future__ import annotations

from datetime import datetime
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import PermissionDeniedException
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, ClampLayout, Site, User, utcnow
from charclamp.domain.ordering import fallback_ordering, order_clamps
from charclamp.domain.rules import RuleError, assert_can_set_clamp_status, can_mark_clamp_drawn
from charclamp.infra.db import SessionLocal
from charclamp.infra.security import verify_password

ADMIN_ROLE = "admin"

STATUS_LABELS = {
    Clamp.STATUS_STACKED: "已码窑",
    Clamp.STATUS_BURNING: "焖烧中",
    Clamp.STATUS_DRAWN: "已出炭",
}


def _is_admin(request: Request) -> bool:
    return bool(request.user) and getattr(request.user, "role", None) == ADMIN_ROLE


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


async def _load_layout(db, site_id: int) -> ClampLayout | None:
    return (
        await db.execute(select(ClampLayout).where(ClampLayout.site_id == site_id))
    ).scalar_one_or_none()


async def _load_timeline_context(clamp_id: int | None = None) -> dict[str, Any]:
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
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
        saved_ordering: list[int] | None = None
        layout_version: int | None = None
        if clamps:
            layout = await _load_layout(db, clamps[0].site_id)
            if layout is not None:
                saved_ordering = layout.ordering
                layout_version = layout.version
        # 整页与 HTMX 局部共用同一排序源：保存的排列优先，否则窑号序。
        clamps = order_clamps(clamps, saved_ordering)
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
        "layout_version": layout_version,
    }


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
        return Template(
            template_name="partials/board.html",
            context={
                **ctx,
                "user": request.user,
            },
        )

    @get("/drawer/shift-new", media_type=MediaType.HTML)
    async def drawer_shift_new(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        async with SessionLocal() as db:
            clamps = list(
                (await db.execute(select(Clamp).order_by(Clamp.code))).scalars().all()
            )
            saved = None
            if clamps:
                layout = await _load_layout(db, clamps[0].site_id)
                if layout is not None:
                    saved = layout.ordering
            clamps = order_clamps(clamps, saved)
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "user": request.user,
            },
        )

    @get("/drawer/layout", media_type=MediaType.HTML)
    async def drawer_layout(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request):
            # 操作工只许看不许改：排列抽屉不对其开放。
            raise PermissionDeniedException()
        async with SessionLocal() as db:
            site = (await db.execute(select(Site).order_by(Site.id).limit(1))).scalar_one_or_none()
            clamps = list(
                (
                    await db.execute(
                        select(Clamp)
                        .options(selectinload(Clamp.site))
                        .order_by(Clamp.code)
                    )
                )
                .scalars()
                .all()
            )
            layout = await _load_layout(db, site.id) if site else None
            saved = layout.ordering if layout is not None else None
            version = layout.version if layout is not None else 0
            clamps = order_clamps(clamps, saved)
            current_ids = [c.id for c in clamps]
        return Template(
            template_name="partials/drawer_layout.html",
            context={
                "clamps": clamps,
                "layout_version": version,
                "current_ids": current_ids,
                "fallback_ids": fallback_ordering(clamps),
                "status_labels": STATUS_LABELS,
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


class LayoutController(Controller):
    path = "/layout"
    tags = ["layout"]

    @post("/order")
    async def save_order(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request):
            # 操作工只许看不许改：保存接口服务端强制拒绝，绕过前端也无用。
            _set_flash(request, "仅管理员可保存窑剪影顺序", "error")
            return Redirect("/")

        raw = str(data.get("ordering") or "")
        try:
            submitted = [int(part) for part in raw.split(",") if part.strip()]
        except (TypeError, ValueError):
            submitted = []
        try:
            expected_version = int(data.get("version") or 0)
        except (TypeError, ValueError):
            expected_version = 0

        async with SessionLocal() as db:
            site = (
                await db.execute(select(Site).order_by(Site.id).limit(1))
            ).scalar_one_or_none()
            if site is None:
                _set_flash(request, "场地尚未初始化，无法保存排列", "error")
                return Redirect("/")

            # 行锁串行化并发保存；版本号构成乐观锁，两者只允许一版入库。
            layout = (
                await db.execute(
                    select(ClampLayout)
                    .where(ClampLayout.site_id == site.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            current_version = layout.version if layout is not None else 0

            if expected_version != current_version:
                _set_flash(
                    request,
                    "排列刚被另一位管理员保存过，您的版本已过期；页面已显示最新顺序，请按它重新调整后再保存。",
                    "error",
                )
                return Redirect("/")

            valid_ids = {
                cid
                for (cid,) in (
                    await db.execute(select(Clamp.id).where(Clamp.site_id == site.id))
                ).all()
            }
            # 排列只是 id 的重排：必须与当前窑集合一一对应，杜绝借保存增删窑或改窑。
            if (
                not submitted
                or len(submitted) != len(valid_ids)
                or len(set(submitted)) != len(submitted)
                or set(submitted) != valid_ids
            ):
                _set_flash(request, "提交的窑排列与当前窑清单不一致，请刷新后重新排列", "error")
                return Redirect("/")

            try:
                if layout is None:
                    db.add(
                        ClampLayout(
                            site_id=site.id,
                            ordering=submitted,
                            version=1,
                            updated_at=utcnow(),
                            updated_by=request.user.username,
                        )
                    )
                    await db.flush()
                else:
                    layout.ordering = submitted
                    layout.version = current_version + 1
                    layout.updated_at = utcnow()
                    layout.updated_by = request.user.username
                await db.commit()
            except IntegrityError:
                # 两位管理员同时首存：唯一约束只放行一条，另一条在此失败。
                await db.rollback()
                _set_flash(
                    request,
                    "排列刚被另一位管理员抢先保存，页面已显示最新顺序，请按它重新调整。",
                    "error",
                )
                return Redirect("/")

        _set_flash(request, "窑剪影顺序已保存", "ok")
        return Redirect("/")
