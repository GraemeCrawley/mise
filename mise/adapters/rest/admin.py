"""Admin endpoints. Phase 0 surface — xlsx import, canonical-review,
sync-run history. Internal-only (no auth in Phase 0; access model
deferred to Phase 1).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from mise.db import get_session
from mise.models import Household, SyncRun
from mise.services.xlsx_importer import LLMVerifyMode, import_xlsx

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/sync-runs")
def list_sync_runs(db: Session = Depends(get_session)) -> list[SyncRun]:
    """Recent imports, newest first."""
    return list(db.exec(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(20)))


class ImportXlsxResponse(BaseModel):
    sync_run_id: int
    rows_total: int
    rows_new: int
    rows_changed: int
    rows_unchanged: int
    rows_failed: int
    needs_review: list[str]
    dry_run: bool


@router.post("/import-xlsx", response_model=ImportXlsxResponse)
def import_xlsx_endpoint(
    path: str,
    dry_run: bool = False,
    llm_verify: LLMVerifyMode = "off",
    db: Session = Depends(get_session),
) -> ImportXlsxResponse:
    """One-shot bootstrap of the legacy recipe library from a path-on-disk
    xlsx file. The xlsx is expected to be mounted at /legacy inside the
    container per docker-compose.yml.

    `dry_run=true` runs the entire pipeline (parser, resolver, UPSERT
    staging) inside a SAVEPOINT that ROLLBACKs at the end. Counts are
    persisted on SyncRun even on dry-run.

    `llm_verify=off|first-seen|every|audit-only` — wired through but
    only `off` is functional in this commit; the LLM verify layer
    lands in the followup.
    """
    try:
        result = import_xlsx(db, path, dry_run=dry_run, llm_verify=llm_verify)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return ImportXlsxResponse(**result.__dict__)


class PatchHouseholdIn(BaseModel):
    """Single-tenant Phase 0/1 — there's exactly one household.
    All fields optional; PATCH semantics."""
    default_servings_per_meal: int | None = None


@router.patch("/household")
def patch_household(
    body: PatchHouseholdIn, db: Session = Depends(get_session),
) -> dict:
    """Update household settings. Plan page uses this to adjust the
    'servings per meal' value that drives placement +/- step size."""
    h = db.exec(select(Household)).first()
    if h is None:
        raise HTTPException(status_code=404, detail="household not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "default_servings_per_meal" and (value is None or value < 1):
            raise HTTPException(
                status_code=400,
                detail="default_servings_per_meal must be >= 1",
            )
        setattr(h, field, value)
    db.add(h)
    db.commit()
    db.refresh(h)
    return {
        "id": h.id,
        "default_servings_per_meal": h.default_servings_per_meal,
        "meals": h.meals,
    }
