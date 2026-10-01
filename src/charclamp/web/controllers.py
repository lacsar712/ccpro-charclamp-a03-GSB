from __future__ import annotations

from datetime import date, datetime
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, IgnitionPermit, User
from charclamp.domain.rules import (
    RuleError,
    assert_can_open_permit,
    assert_can_set_clamp_status,
    can_mark_clamp_drawn,
    consume_open_permit,
    find_open_permit,
    normalize_permit_code,
    register_burn_shift,
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


def _is_admin(request: Request) -> bool:
    return bool(request.user) and getattr(request.user, "role", None) == "admin"


async def _open_permit_counts(db) -> dict[int, int]:
    """每座窑的未核销帖数（窑剪影「待点火」标记与专页对账用同一数据源）。"""
    rows = (
        await db.execute(
            select(IgnitionPermit.clamp_id, func.count(IgnitionPermit.id))
            .where(IgnitionPermit.consumed_at.is_(None))
            .group_by(IgnitionPermit.clamp_id)
        )
    ).all()
    return {clamp_id: count for clamp_id, count in rows}


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
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
        open_counts = await _open_permit_counts(db)
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    open_permit_total = sum(open_counts.values())
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
        "open_permit_counts": open_counts,
        "open_permit_total": open_permit_total,
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
            clamps = list((await db.execute(select(Clamp).order_by(Clamp.code))).scalars().all())
            open_counts = await _open_permit_counts(db)
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "open_permit_counts": open_counts,
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
            open_permit = await find_open_permit(db, clamp_id)
        can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "open_permit": open_permit,
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
        clamp_id = _parse_optional_int(data.get("clamp_id"))
        if clamp_id is None:
            _set_flash(request, "未选择炭窑，班次未登记", "error")
            return Redirect("/")
        started_raw = data.get("started_at") or ""
        started_at = datetime.fromisoformat(started_raw) if started_raw else datetime.utcnow()
        peak_raw = (data.get("peak_temp_c") or "").strip()
        try:
            peak = float(peak_raw) if peak_raw else None
        except ValueError:
            peak = None
        async with SessionLocal() as db:
            try:
                # 核销许可、班次入库、窑态转焖烧中——全部在同一事务内提交或一起回滚。
                _, clamp = await register_burn_shift(
                    db,
                    clamp_id=clamp_id,
                    started_at=started_at,
                    peak_temp_c=peak,
                    charcoal_grade=(data.get("charcoal_grade") or "B").strip(),
                    notes=(data.get("notes") or "").strip(),
                )
                await db.commit()
                _set_flash(
                    request,
                    f"焖烧班次已登记，窑 {clamp.code} 已进入焖烧中"
                    if clamp.status == Clamp.STATUS_BURNING
                    else "焖烧班次已登记",
                    "ok",
                )
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
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
                await db.rollback()
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")


class PermitController(Controller):
    path = "/permits"
    tags = ["permits"]

    @get("", media_type=MediaType.HTML)
    async def permit_page(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        open_only = request.query_params.get("open") == "1"
        async with SessionLocal() as db:
            query = (
                select(IgnitionPermit)
                .options(
                    selectinload(IgnitionPermit.clamp).selectinload(Clamp.site),
                    selectinload(IgnitionPermit.duty_admin),
                )
                .order_by(
                    IgnitionPermit.consumed_at.is_(None).desc(),
                    IgnitionPermit.opened_on.desc(),
                    IgnitionPermit.id.desc(),
                )
            )
            if open_only:
                query = query.where(IgnitionPermit.consumed_at.is_(None))
            permits = list((await db.execute(query)).scalars().all())
            clamps = list((await db.execute(select(Clamp).order_by(Clamp.code))).scalars().all())
            admins = list(
                (
                    await db.execute(
                        select(User).where(User.role == "admin").order_by(User.username)
                    )
                )
                .scalars()
                .all()
            )
            open_counts = await _open_permit_counts(db)
        return Template(
            template_name="permits.html",
            context={
                "permits": permits,
                "clamps": clamps,
                "admins": admins,
                "open_only": open_only,
                "open_permit_counts": open_counts,
                "open_permit_total": sum(open_counts.values()),
                "status_labels": STATUS_LABELS,
                "user": request.user,
                "is_admin": _is_admin(request),
                "flash": flash,
                "flash_cat": flash_cat,
                "today": date.today().isoformat(),
            },
        )

    @post("/new")
    async def create_permit(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request):
            # 不抛 PermissionDenied（避免被带去登录页），保留登录态就地提示。
            _set_flash(request, "仅值班管理员可开点火许可帖", "error")
            return Redirect("/permits?open=1")
        clamp_id = _parse_optional_int(data.get("clamp_id"))
        if clamp_id is None:
            _set_flash(request, "请选择炭窑", "error")
            return Redirect("/permits?open=1")
        try:
            permit_code = normalize_permit_code(data.get("permit_code") or "")
        except RuleError as exc:
            _set_flash(request, str(exc), "error")
            return Redirect("/permits?open=1")
        opened_raw = (data.get("opened_on") or "").strip()
        try:
            opened_on = date.fromisoformat(opened_raw) if opened_raw else date.today()
        except ValueError:
            opened_on = date.today()
        duty_admin_id = _parse_optional_int(data.get("duty_admin_id")) or request.user.id

        async with SessionLocal() as db:
            try:
                # 锁窑行 + 锁该窑未核销帖，先做应用层判定；数据库部分唯一索引兜底并发。
                clamp = (
                    await db.execute(
                        select(Clamp).where(Clamp.id == clamp_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if clamp is None:
                    _set_flash(request, "炭窑不存在", "error")
                    return Redirect("/permits?open=1")
                existing = await find_open_permit(db, clamp_id, lock=True)
                assert_can_open_permit(clamp, existing is not None)
                admin = (
                    await db.execute(select(User).where(User.id == int(duty_admin_id)))
                ).scalar_one_or_none()
                if admin is None or admin.role != "admin":
                    raise RuleError("值班管理员无效")
                permit = IgnitionPermit(
                    clamp_id=clamp_id,
                    opened_on=opened_on,
                    permit_code=permit_code,
                    duty_admin_id=admin.id,
                )
                db.add(permit)
                await db.commit()
                _set_flash(
                    request,
                    f"点火许可帖 {permit_code} 已开（窑 {clamp.code} 待点火）",
                    "ok",
                )
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
            except IntegrityError:
                # 两名管理员抢开同窑、或编号撞车：只许落下一张，本事务整体回滚。
                await db.rollback()
                _set_flash(
                    request,
                    "开帖未成功：该窑已有未核销许可帖，或许可编号已被使用",
                    "error",
                )
        return Redirect("/permits?open=1")

    @post("/{permit_id:int}/consume")
    async def consume_permit(self, request: Request, permit_id: int) -> Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request):
            _set_flash(request, "仅值班管理员可核销点火许可帖", "error")
            return Redirect("/permits?open=1")
        async with SessionLocal() as db:
            try:
                permit = await consume_open_permit(db, permit_id)
                await db.commit()
                _set_flash(request, f"许可帖 {permit.permit_code} 已核销", "ok")
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
            except IntegrityError:
                await db.rollback()
                _set_flash(request, "核销失败，请重试", "error")
        return Redirect("/permits?open=1")
