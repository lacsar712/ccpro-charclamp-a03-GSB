from __future__ import annotations

from datetime import date, datetime
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Clamp, IgnitionPermit, User
from charclamp.domain.rules import (
    RuleError,
    assert_can_open_permit,
    assert_can_revoke_permit,
    assert_can_set_clamp_status,
    can_mark_clamp_drawn,
    consume_permit_for_first_shift,
    validate_permit_no,
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
    return bool(getattr(request.user, "role", None) == "admin")


async def _open_permits(db) -> list[IgnitionPermit]:
    return list(
        (
            await db.execute(
                select(IgnitionPermit)
                .options(selectinload(IgnitionPermit.clamp))
                .where(IgnitionPermit.revoked_at.is_(None))
            )
        )
        .scalars()
        .all()
    )


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
        open_permits = await _open_permits(db)
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    open_permits_by_clamp = {p.clamp_id: p for p in open_permits}
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
        "open_permits_by_clamp": open_permits_by_clamp,
        "open_permit_count": len(open_permits),
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
            open_permit_clamp_ids = {p.clamp_id for p in await _open_permits(db)}
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "open_permit_clamp_ids": open_permit_clamp_ids,
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
                .options(
                    selectinload(Clamp.shifts),
                    selectinload(Clamp.site),
                    selectinload(Clamp.permits),
                )
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            open_permit = next((p for p in clamp.permits if p.revoked_at is None), None)
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
        started_raw = data.get("started_at") or ""
        started_at = datetime.fromisoformat(started_raw) if started_raw else datetime.utcnow()
        peak_raw = (data.get("peak_temp_c") or "").strip()
        peak = float(peak_raw) if peak_raw else None
        clamp_id = int(data["clamp_id"])
        async with SessionLocal() as db:
            # 锁窑行：并发登记首笔班次时在此串行，避免重复核销/重复开窑
            clamp = (
                await db.execute(
                    select(Clamp).where(Clamp.id == clamp_id).with_for_update()
                )
            ).scalar_one_or_none()
            if not clamp:
                _set_flash(request, "炭窑不存在", "error")
                return Redirect("/")
            shift = BurnShift(
                clamp_id=clamp_id,
                started_at=started_at,
                peak_temp_c=peak,
                charcoal_grade=(data.get("charcoal_grade") or "B").strip(),
                notes=(data.get("notes") or "").strip(),
            )
            db.add(shift)
            try:
                consumed: IgnitionPermit | None = None
                if clamp.status == Clamp.STATUS_STACKED:
                    # 已码窑首笔班次：必须存在未核销帖，
                    # 并在同一事务内核销、入班次、改窑态为焖烧中。
                    permits = list(
                        (
                            await db.execute(
                                select(IgnitionPermit)
                                .where(IgnitionPermit.clamp_id == clamp_id)
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )
                    consumed = consume_permit_for_first_shift(
                        clamp, permits, datetime.utcnow()
                    )
                    clamp.status = Clamp.STATUS_BURNING
                # 焖烧中（或已出炭）窑追加班次：免持新帖，出炭仍走峰值门槛
                await db.commit()
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
                return Redirect(f"/?clamp_id={clamp_id}")
        if consumed is not None:
            _set_flash(
                request,
                f"许可帖 {consumed.permit_no} 已随首笔班次核销，窑 {clamp.code} 转为焖烧中",
                "ok",
            )
        else:
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


class PermitController(Controller):
    path = "/permits"
    tags = ["permits"]

    @get("", media_type=MediaType.HTML)
    async def permit_page(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        only_open = request.query_params.get("status", "open") != "all"
        async with SessionLocal() as db:
            query = (
                select(IgnitionPermit)
                .options(
                    selectinload(IgnitionPermit.clamp).selectinload(Clamp.site),
                    selectinload(IgnitionPermit.duty_admin),
                )
                .order_by(
                    IgnitionPermit.revoked_at.is_(None).desc(),
                    IgnitionPermit.opened_on.desc(),
                    IgnitionPermit.id.desc(),
                )
            )
            if only_open:
                query = query.where(IgnitionPermit.revoked_at.is_(None))
            permits = list((await db.execute(query)).scalars().all())
            open_count = len(await _open_permits(db))
            stacked_clamps = list(
                (
                    await db.execute(
                        select(Clamp)
                        .where(Clamp.status == Clamp.STATUS_STACKED)
                        .order_by(Clamp.code)
                    )
                )
                .scalars()
                .all()
            )
            admins = list(
                (
                    await db.execute(
                        select(User).where(User.role == "admin").order_by(User.username)
                    )
                )
                .scalars()
                .all()
            )
            open_permit_clamp_ids = {
                p.clamp_id
                for p in await _open_permits(db)
            }
        return Template(
            template_name="permits.html",
            context={
                "permits": permits,
                "only_open": only_open,
                "open_count": open_count,
                "open_permit_count": open_count,
                "stacked_clamps": stacked_clamps,
                "open_permit_clamp_ids": open_permit_clamp_ids,
                "admins": admins,
                "today": date.today().isoformat(),
                "is_admin": _is_admin(request),
                "user": request.user,
                "flash": flash,
                "flash_cat": flash_cat,
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
            _set_flash(request, "仅值班管理员可开点火许可帖", "error")
            return Redirect("/permits")
        try:
            clamp_id = int(data["clamp_id"])
        except (KeyError, TypeError, ValueError):
            _set_flash(request, "请选择炭窑", "error")
            return Redirect("/permits")
        try:
            permit_no = validate_permit_no(data.get("permit_no") or "")
        except RuleError as exc:
            _set_flash(request, str(exc), "error")
            return Redirect("/permits")
        opened_raw = (data.get("opened_on") or "").strip()
        try:
            opened_on = date.fromisoformat(opened_raw) if opened_raw else date.today()
        except ValueError:
            _set_flash(request, "开帖日格式无效", "error")
            return Redirect("/permits")
        admin_id = _parse_optional_int(data.get("duty_admin_id")) or request.user.id

        async with SessionLocal() as db:
            # 锁窑行：两名管理员并发给同一已码窑开帖时在此串行，
            # 只有一个事务能落下未核销帖，另一个被规则/唯一索引挡下。
            clamp = (
                await db.execute(
                    select(Clamp).where(Clamp.id == clamp_id).with_for_update()
                )
            ).scalar_one_or_none()
            permits = (
                list(
                    (
                        await db.execute(
                            select(IgnitionPermit).where(IgnitionPermit.clamp_id == clamp_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if clamp
                else []
            )
            try:
                assert_can_open_permit(clamp, permits)
                permit = IgnitionPermit(
                    clamp_id=clamp_id,
                    opened_on=opened_on,
                    permit_no=permit_no,
                    duty_admin_id=admin_id,
                )
                db.add(permit)
                await db.commit()
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
                return Redirect("/permits")
            except IntegrityError:
                # 编号唯一 / 每窑仅一张未核销帖：并发下由数据库约束兜底
                await db.rollback()
                _set_flash(request, "开帖被挡下：许可编号重复，或该窑已有未核销帖", "error")
                return Redirect("/permits")
        _set_flash(request, f"点火许可帖 {permit_no} 已开（{clamp.code}，待点火）", "ok")
        return Redirect("/permits")

    @post("/{permit_id:int}/revoke")
    async def revoke_permit(
        self,
        request: Request,
        permit_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request):
            _set_flash(request, "仅管理员可核销点火许可帖", "error")
            return Redirect("/permits")
        async with SessionLocal() as db:
            try:
                permit = (
                    await db.execute(
                        select(IgnitionPermit)
                        .where(IgnitionPermit.id == permit_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                assert_can_revoke_permit(permit)
                permit.revoked_at = datetime.utcnow()
                await db.commit()
            except RuleError as exc:
                await db.rollback()
                _set_flash(request, str(exc), "error")
                return Redirect("/permits")
        _set_flash(request, f"许可帖 {permit.permit_no} 已核销", "ok")
        return Redirect("/permits")
