import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from .. import crud, schemas
from ..database import get_db
from ..models import InputVersionStatus, ClosureStatus, ReopenDecisionStatus

router = APIRouter(prefix="/accounting", tags=["accounting-sealing"])


def _closure_to_detail(closure) -> dict:
    return {
        "id": closure.id,
        "closure_no": closure.closure_no,
        "enterprise_id": closure.enterprise_id,
        "year": closure.year,
        "input_version_id": closure.input_version_id,
        "status": closure.status,
        "sealed_by": closure.sealed_by,
        "sealed_at": closure.sealed_at,
        "previous_closure_id": closure.previous_closure_id,
        "reopen_decision_id": closure.reopen_decision_id,
        "remark": closure.remark,
        "created_at": closure.created_at,
        "limit_standard_snapshot": json.loads(closure.limit_standard_snapshot),
        "result_snapshot": json.loads(closure.result_snapshot),
        "diff_from_previous": json.loads(closure.diff_from_previous)
        if closure.diff_from_previous else None,
    }


def _decision_to_dict(decision, with_result: bool = False) -> dict:
    data = {
        "id": decision.id,
        "decision_no": decision.decision_no,
        "year": decision.year,
        "enterprise_ids": json.loads(decision.enterprise_ids),
        "input_version_map": json.loads(decision.input_version_map or "{}"),
        "reason": decision.reason,
        "approved_by": decision.approved_by,
        "status": decision.status,
        "remark": decision.remark,
        "created_at": decision.created_at,
        "executed_at": decision.executed_at,
    }
    if with_result:
        data["execution_result"] = (
            json.loads(decision.execution_result) if decision.execution_result else None
        )
    return data


@router.post("/input-versions", response_model=schemas.AccountingInputVersion)
def submit_input_version(
    payload: schemas.AccountingInputVersionCreate,
    db: Session = Depends(get_db)
):
    """企业提交核算输入（产量/能耗凭证），形成完整可比较的输入版本"""
    try:
        return crud.submit_accounting_input_version(
            db,
            enterprise_id=payload.enterprise_id,
            year=payload.year,
            items=[item.model_dump() for item in payload.items],
            submitted_by=payload.submitted_by,
            note=payload.note
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/input-versions", response_model=List[schemas.AccountingInputVersion])
def read_input_versions(
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[InputVersionStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    return crud.get_accounting_input_versions(
        db, enterprise_id=enterprise_id, year=year, status=status, skip=skip, limit=limit
    )


@router.get("/input-versions/compare", response_model=schemas.InputVersionCompareResponse)
def compare_input_versions(
    version_a: int = Query(..., description="基准版本ID"),
    version_b: int = Query(..., description="对比版本ID"),
    db: Session = Depends(get_db)
):
    """比较两个输入版本，输出车型明细的字段级差异"""
    try:
        return crud.compare_input_versions(db, version_a_id=version_a, version_b_id=version_b)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/input-versions/{version_id}", response_model=schemas.AccountingInputVersion)
def read_input_version(version_id: int, db: Session = Depends(get_db)):
    version = crud.get_accounting_input_version(db, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="核算输入版本不存在")
    return version


@router.post("/closures/seal", response_model=schemas.AccountingClosureDetail)
def seal_accounting(
    payload: schemas.AccountingClosureSealRequest,
    db: Session = Depends(get_db)
):
    """监管确认封账：冻结当期核算输入、限值标准与计算结果"""
    try:
        closure = crud.seal_accounting(
            db,
            enterprise_id=payload.enterprise_id,
            year=payload.year,
            input_version_id=payload.input_version_id,
            sealed_by=payload.sealed_by,
            remark=payload.remark
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _closure_to_detail(closure)


@router.get("/closures", response_model=List[schemas.AccountingClosure])
def read_closures(
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[ClosureStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    return crud.get_accounting_closures(
        db, enterprise_id=enterprise_id, year=year, status=status, skip=skip, limit=limit
    )


@router.get("/closures/active/{enterprise_id}/{year}", response_model=schemas.AccountingClosureDetail)
def read_active_closure(enterprise_id: int, year: int, db: Session = Depends(get_db)):
    """查询企业当年当前有效封账（下游结转/汇总/交易可用额所采用的封账）"""
    closure = crud.get_active_closure(db, enterprise_id, year)
    if not closure:
        raise HTTPException(status_code=404, detail="该企业当年没有有效封账")
    return _closure_to_detail(closure)


@router.get("/closures/{closure_id}", response_model=schemas.AccountingClosureDetail)
def read_closure(closure_id: int, db: Session = Depends(get_db)):
    closure = crud.get_accounting_closure(db, closure_id)
    if not closure:
        raise HTTPException(status_code=404, detail="封账记录不存在")
    return _closure_to_detail(closure)


@router.post("/reopen-decisions", response_model=schemas.ReopenDecisionDetail)
def create_reopen_decision(
    payload: schemas.ReopenDecisionCreate,
    db: Session = Depends(get_db)
):
    """登记重开决定：记录理由、审批权限与受影响企业"""
    try:
        decision = crud.create_reopen_decision(
            db,
            year=payload.year,
            enterprise_ids=payload.enterprise_ids,
            reason=payload.reason,
            approved_by=payload.approved_by,
            input_version_map=payload.input_version_map,
            remark=payload.remark
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _decision_to_dict(decision, with_result=True)


@router.get("/reopen-decisions", response_model=List[schemas.ReopenDecisionDetail])
def read_reopen_decisions(
    year: Optional[int] = None,
    status: Optional[ReopenDecisionStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    decisions = crud.get_reopen_decisions(db, year=year, status=status, skip=skip, limit=limit)
    return [_decision_to_dict(d, with_result=True) for d in decisions]


@router.get("/reopen-decisions/{decision_id}", response_model=schemas.ReopenDecisionDetail)
def read_reopen_decision(decision_id: int, db: Session = Depends(get_db)):
    decision = crud.get_reopen_decision(db, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="重开决定不存在")
    return _decision_to_dict(decision, with_result=True)


@router.post("/reopen-decisions/{decision_id}/execute", response_model=schemas.ReopenExecutionResponse)
def execute_reopen_decision(decision_id: int, db: Session = Depends(get_db)):
    """
    执行重开决定：基于指定输入版本为受影响企业重算并生成新封账，
    给出各积分项差异。重复执行同一决定不会生成新版本。
    """
    decision = crud.get_reopen_decision(db, decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail="重开决定不存在")

    already_executed = decision.status == ReopenDecisionStatus.EXECUTED
    result = crud.execute_reopen_decision(db, decision_id)

    result["message"] = (
        "该重开决定已执行过，返回首次执行结果，未生成新版本"
        if already_executed else
        f"重开执行完成：成功 {result['success_count']} 家，失败 {result['failure_count']} 家"
    )
    return result
