"""M8 入口：重算月需量、查询发布行、需量页。

需量是独立于电度账的第二条账：不进区间、不进月度分摊，单独成表、
单独导出，页面上把观察覆盖、采用的最高值、兜底与追补部分一次讲清。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...web import Ctx, get_ctx, render
from ..m6_batches import resume as resume_mod
from . import service

router = APIRouter(tags=["M8 月需量"])

COVERAGE_LABELS = {"full": "完整", "partial": "残缺", "missing": "缺证"}
PUBLISH_LABELS = {"final": "最终发布", "provisional": "临时发布·待追补"}


@router.post("/api/demand/recalc")
def api_recalc(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch_id = (payload or {}).get("batch_id")
    batch = resume_mod.require_batch(
        ctx.store, batch_id or (ctx.current_batch() or {}).get("batch_id", ""))
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    body = service.payload(result)
    return body


@router.get("/api/demand")
def api_demand(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        return {"batch_id": None, "rows": [], "stats": None}
    result = service.run(ctx.settings, ctx.store, batch["batch_id"])
    return service.payload(result)


@router.get("/demand")
def page_demand(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    rows: list[dict] = []
    stats: dict | None = None
    if batch is not None:
        result = service.run(ctx.settings, ctx.store, batch["batch_id"])
        rows = result["rows"]
        stats = result["stats"]
    return render(request, "demand.html", page="demand", page_title="月需量",
                  batch=batch, demand_rows=rows, demand_stats=stats,
                  coverage_labels=COVERAGE_LABELS, publish_labels=PUBLISH_LABELS)
