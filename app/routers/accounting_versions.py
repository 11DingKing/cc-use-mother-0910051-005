from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, sealing
from ..database import get_db

router = APIRouter(tags=["accounting-version-seal"])


# ---------------------------------------------------------------------------
# 核算输入版本
# ---------------------------------------------------------------------------

@router.post(
    "/input-versions/",
    response_model=schemas.InputVersionOut,
    summary="企业提交（含补交凭证）形成完整输入版本",
)
def submit_input_version(
    payload: schemas.InputVersionSubmit, db: Session = Depends(get_db)
):
    items = None
    if payload.items is not None:
        items = []
        for item in payload.items:
            raw = item.model_dump(exclude_none=True)
            if "range_km" in raw:
                raw["range_km"] = raw.pop("range_km")
            items.append(raw)
    try:
        version, created = sealing.submit_input_version(
            db,
            enterprise_id=payload.enterprise_id,
            year=payload.year,
            submitter=payload.submitter,
            items=items,
            evidence_doc_no=payload.evidence_doc_no,
            remark=payload.remark,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not created:
        # 内容与最近版本完全一致，不产生新版本
        pass
    return version


@router.get("/input-versions/", response_model=List[schemas.InputVersionOut])
def list_input_versions(
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return sealing.list_input_versions(db, enterprise_id=enterprise_id, year=year)


@router.get("/input-versions/{version_id}", response_model=schemas.InputVersionOut)
def get_input_version(version_id: int, db: Session = Depends(get_db)):
    version = sealing.get_input_version(db, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="输入版本不存在")
    return version


@router.get("/input-versions/{version_id}/diff", summary="比较两个完整输入版本")
def diff_input_versions(
    version_id: int, target_version_id: int, db: Session = Depends(get_db)
):
    try:
        return sealing.diff_input_versions(db, version_id, target_version_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------------------------------------------------------------------------
# 封账
# ---------------------------------------------------------------------------

@router.post("/seals/", response_model=schemas.SealOut, summary="监管确认后冻结当期核算")
def seal_year(payload: schemas.SealCreate, db: Session = Depends(get_db)):
    try:
        return sealing.seal_year(
            db, year=payload.year, confirmed_by=payload.confirmed_by, remark=payload.remark
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/seals/active", response_model=Optional[schemas.SealOut])
def get_active_seal(year: int, db: Session = Depends(get_db)):
    return sealing.get_active_seal(db, year)


@router.get("/seals/{seal_id}", response_model=schemas.SealOut)
def get_seal(seal_id: int, db: Session = Depends(get_db)):
    seal = sealing.get_seal(db, seal_id)
    if not seal:
        raise HTTPException(status_code=404, detail="封账不存在")
    return seal


@router.get("/seals/", response_model=List[schemas.SealOut])
def list_seals(year: Optional[int] = None, db: Session = Depends(get_db)):
    from .. import models
    query = db.query(models.AccountingSeal)
    if year:
        query = query.filter(models.AccountingSeal.year == year)
    return query.order_by(
        models.AccountingSeal.year, models.AccountingSeal.seal_seq
    ).all()


# ---------------------------------------------------------------------------
# 重开决定与重算
# ---------------------------------------------------------------------------

@router.post(
    "/reopen-decisions/",
    response_model=schemas.ReopenOut,
    summary="凭重开决定基于指定版本重算（同决定单号幂等）",
)
def create_reopen_decision(
    payload: schemas.ReopenCreate, db: Session = Depends(get_db)
):
    try:
        decision, _created = sealing.reopen_and_recalculate(
            db,
            decision_no=payload.decision_no,
            reason=payload.reason,
            requested_by=payload.requested_by,
            approved_by=payload.approved_by,
            authority_role=payload.authority_role,
            enterprise_ids=payload.enterprise_ids,
            year=payload.year,
            base_seal_id=payload.base_seal_id,
            version_by_enterprise=payload.version_by_enterprise,
            input_version_id=payload.input_version_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return decision


@router.get("/reopen-decisions/{decision_id}", response_model=schemas.ReopenOut)
def get_reopen_decision(decision_id: int, db: Session = Depends(get_db)):
    decision = sealing.get_reopen_decision(db, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="重开决定不存在")
    return decision


@router.get("/reopen-decisions/", response_model=List[schemas.ReopenOut])
def list_reopen_decisions(year: Optional[int] = None, db: Session = Depends(get_db)):
    return sealing.list_reopen_decisions(db, year=year)
