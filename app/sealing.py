"""
核算输入版本、封账与重开重算机制。

设计要点：
- 企业每次提交形成不可变的完整输入版本（InputVersion + 明细 + 限值标准快照），
  内容哈希相同的重复提交不再产生新版本。
- 监管确认后封账（AccountingSeal），冻结当期采用的输入版本、限值标准、逐车型
  结果与企业汇总；封账后直接改车型数据/积分记录会被拒绝。
- 重开必须凭重开决定（ReopenDecision），记录理由、权限、受影响企业，并按决定
  单号幂等：重复执行同一决定不再生成新版本或新封账。
- 批量重算逐企业隔离异常：单个企业失败时在新封账中保留其原封账结果。
- 每次重算产出逐积分项差异（RecalcDifferenceItem），旧封账保留为 reopened，
  不会被悄悄覆盖。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from . import models
from .models import (
    InputVersion,
    InputVersionItem,
    InputVersionSource,
    LimitStandardSnapshot,
    AccountingSeal,
    SealResultItem,
    SealEnterpriseSummary,
    ReopenDecision,
    ReopenAffectedEnterprise,
    RecalcDifferenceItem,
    ReopenStatus,
    SealStatus,
)
from .rules import (
    limit_standard_json,
    serialize_limit_standard,
    CREDIT_MULTIPLIER,
)


# ---------------------------------------------------------------------------
# 基础查询
# ---------------------------------------------------------------------------

def get_active_seal(db: Session, year: int) -> Optional[AccountingSeal]:
    """返回某年度当前生效的封账（未被重开替代的那次）。"""
    return (
        db.query(AccountingSeal)
        .filter(
            AccountingSeal.year == year,
            AccountingSeal.status != SealStatus.REOPENED,
            AccountingSeal.superseded_by_seal_id.is_(None),
        )
        .order_by(AccountingSeal.seal_seq.desc())
        .first()
    )


def get_seal(db: Session, seal_id: int) -> Optional[AccountingSeal]:
    return db.query(AccountingSeal).filter(AccountingSeal.id == seal_id).first()


def is_year_sealed(db: Session, year: int) -> bool:
    return get_active_seal(db, year) is not None


def get_latest_input_version(
    db: Session, enterprise_id: int, year: int
) -> Optional[InputVersion]:
    return (
        db.query(InputVersion)
        .filter(InputVersion.enterprise_id == enterprise_id, InputVersion.year == year)
        .order_by(InputVersion.version_seq.desc())
        .first()
    )


def get_input_version(db: Session, version_id: int) -> Optional[InputVersion]:
    return db.query(InputVersion).filter(InputVersion.id == version_id).first()


def list_input_versions(
    db: Session, enterprise_id: Optional[int] = None, year: Optional[int] = None
) -> List[InputVersion]:
    q = db.query(InputVersion)
    if enterprise_id:
        q = q.filter(InputVersion.enterprise_id == enterprise_id)
    if year:
        q = q.filter(InputVersion.year == year)
    return q.order_by(InputVersion.year, InputVersion.enterprise_id, InputVersion.version_seq).all()


def get_reopen_decision(db: Session, decision_id: int) -> Optional[ReopenDecision]:
    return db.query(ReopenDecision).filter(ReopenDecision.id == decision_id).first()


def get_reopen_decision_by_no(db: Session, decision_no: str) -> Optional[ReopenDecision]:
    return db.query(ReopenDecision).filter(ReopenDecision.decision_no == decision_no).first()


def list_reopen_decisions(db: Session, year: Optional[int] = None) -> List[ReopenDecision]:
    q = db.query(ReopenDecision)
    if year:
        q = q.filter(ReopenDecision.year == year)
    return q.order_by(ReopenDecision.id.desc()).all()


# ---------------------------------------------------------------------------
# 输入版本提交
# ---------------------------------------------------------------------------

def _snapshot_models(db: Session, enterprise_id: int, year: int) -> List[dict]:
    vehicle_models = (
        db.query(models.VehicleModel)
        .filter(
            models.VehicleModel.enterprise_id == enterprise_id,
            models.VehicleModel.production_year == year,
        )
        .order_by(models.VehicleModel.id)
        .all()
    )
    return [
        {
            "vehicle_model_id": m.id,
            "model_code": m.model_code,
            "model_name": m.model_name,
            "curb_weight": m.curb_weight,
            "power_consumption": m.power_consumption,
            "range_km": m.range,
            "annual_output": m.annual_output,
            "evidence_no": None,
        }
        for m in vehicle_models
    ]


def _normalize_submitted_items(
    db: Session, enterprise_id: int, year: int, submitted: Optional[List[dict]]
) -> List[dict]:
    """
    形成完整输入明细：以企业该年度全部车型主数据为底，逐车型合并提交覆盖项。
    补交凭证只给出变动车型，未提及车型仍以主数据计入，保证每个版本完整可比较。
    """
    base_items = _snapshot_models(db, enterprise_id, year)
    if not submitted:
        return base_items

    by_code = {item["model_code"]: item for item in base_items}
    for raw in submitted:
        code = raw["model_code"]
        if code not in by_code:
            raise ValueError(f"车型 {code} 不属于该企业或不属于 {year} 年度，不能计入提交版本")
        target = by_code[code]
        for field in ("curb_weight", "power_consumption", "range_km", "annual_output", "evidence_no"):
            if raw.get(field) is not None:
                target[field] = raw[field]
    return list(by_code.values())


def _content_hash(items: List[dict]) -> str:
    payload = {
        "limit_standard": json.loads(limit_standard_json()),
        "items": sorted(
            [
                {
                    "model_code": i["model_code"],
                    "curb_weight": i["curb_weight"],
                    "power_consumption": i["power_consumption"],
                    "range_km": i["range_km"],
                    "annual_output": i["annual_output"],
                    "evidence_no": i.get("evidence_no"),
                }
                for i in items
            ],
            key=lambda x: x["model_code"],
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def submit_input_version(
    db: Session,
    enterprise_id: int,
    year: int,
    submitter: str,
    items: Optional[List[dict]] = None,
    evidence_doc_no: Optional[str] = None,
    remark: Optional[str] = None,
) -> Tuple[InputVersion, bool]:
    """
    企业提交（含补交凭证）形成完整输入版本。

    返回 (版本, 是否新建)。与最近一次提交内容完全一致（含限值标准）时不产生新版本。
    即使年度已封账也允许提交：新版本只作存档，只有经重开决定重算后才生效。
    """
    enterprise = db.query(models.Enterprise).filter(models.Enterprise.id == enterprise_id).first()
    if not enterprise:
        raise ValueError("企业不存在")

    normalized = _normalize_submitted_items(db, enterprise_id, year, items)
    if not normalized:
        raise ValueError(f"企业 {year} 年度没有任何车型输入，无法形成输入版本")

    content_hash = _content_hash(normalized)
    latest = get_latest_input_version(db, enterprise_id, year)
    if latest and latest.content_hash == content_hash:
        return latest, False

    seq = (latest.version_seq + 1) if latest else 1
    version = InputVersion(
        version_no=f"IV-{year}-E{enterprise_id:04d}-{seq:03d}",
        enterprise_id=enterprise_id,
        year=year,
        version_seq=seq,
        source=InputVersionSource.ENTERPRISE_SUBMISSION,
        submitter=submitter,
        evidence_doc_no=evidence_doc_no,
        remark=remark,
        content_hash=content_hash,
        is_sealed=False,
    )
    db.add(version)
    db.flush()

    for it in normalized:
        db.add(InputVersionItem(
            input_version_id=version.id,
            vehicle_model_id=it["vehicle_model_id"],
            model_code=it["model_code"],
            model_name=it["model_name"],
            curb_weight=it["curb_weight"],
            power_consumption=it["power_consumption"],
            range_km=it["range_km"],
            annual_output=it["annual_output"],
            evidence_no=it.get("evidence_no"),
        ))

    db.add(LimitStandardSnapshot(
        input_version_id=version.id,
        tiers_json=limit_standard_json(),
        credit_multiplier=CREDIT_MULTIPLIER,
        rule_version=serialize_limit_standard()["rule_version"],
    ))

    db.commit()
    db.refresh(version)
    return version, True


def diff_input_versions(
    db: Session, base_version_id: int, target_version_id: int
) -> dict:
    """逐车型、逐字段比较两个完整输入版本。"""
    base = get_input_version(db, base_version_id)
    target = get_input_version(db, target_version_id)
    if not base or not target:
        raise ValueError("输入版本不存在")

    def _map(v: InputVersion) -> Dict[str, dict]:
        return {i.model_code: {
            "model_name": i.model_name,
            "curb_weight": i.curb_weight,
            "power_consumption": i.power_consumption,
            "range_km": i.range_km,
            "annual_output": i.annual_output,
            "evidence_no": i.evidence_no,
        } for i in v.items}

    base_map, target_map = _map(base), _map(target)
    field_labels = {
        "curb_weight": "整备质量",
        "power_consumption": "百公里电耗",
        "range_km": "续航里程",
        "annual_output": "年产量",
        "evidence_no": "凭证编号",
    }
    changes = []
    for code in sorted(set(base_map) | set(target_map)):
        b, t = base_map.get(code), target_map.get(code)
        if b is None:
            changes.append({"model_code": code, "change_type": "added", "fields": t})
            continue
        if t is None:
            changes.append({"model_code": code, "change_type": "removed", "fields": b})
            continue
        field_changes = {
            key: {"old": b[key], "new": t[key], "label": field_labels[key]}
            for key in field_labels
            if b[key] != t[key]
        }
        if field_changes:
            changes.append({
                "model_code": code,
                "change_type": "modified",
                "fields": field_changes,
            })

    standard_changed = (
        base.limit_snapshot.tiers_json != target.limit_snapshot.tiers_json
        or base.limit_snapshot.credit_multiplier != target.limit_snapshot.credit_multiplier
    )
    return {
        "base_version_id": base.id,
        "base_version_no": base.version_no,
        "target_version_id": target.id,
        "target_version_no": target.version_no,
        "limit_standard_changed": standard_changed,
        "base_rule_version": base.limit_snapshot.rule_version,
        "target_rule_version": target.limit_snapshot.rule_version,
        "changes": changes,
    }


# ---------------------------------------------------------------------------
# 基于版本快照重算（纯计算，不读车型主数据，保证可复现）
# ---------------------------------------------------------------------------

def _limit_from_snapshot(curb_weight: float, tiers: List[dict]) -> float:
    for tier in tiers:
        max_weight = tier["max_weight"]
        if max_weight is None:
            if curb_weight >= tier["min_weight"]:
                return tier["limit"]
        elif tier["min_weight"] <= curb_weight <= max_weight:
            return tier["limit"]
    return tiers[-1]["limit"]


def _unit_credit(actual: float, limit: float, multiplier: float) -> float:
    if limit <= 0 or actual < 0:
        return 0.0
    if actual == 0:
        return round(multiplier, 4)
    return round((limit - actual) / limit * multiplier, 4)


def compute_results_from_version(version: InputVersion) -> dict:
    """
    依据版本自身冻结的明细与限值标准快照计算逐车型结果和企业汇总。
    返回 {"items": [...], "summary": {...}}。
    """
    tiers = json.loads(version.limit_snapshot.tiers_json)["tiers"]
    multiplier = version.limit_snapshot.credit_multiplier

    items = []
    total_positive = 0.0
    total_negative = 0.0
    for it in version.items:
        limit = _limit_from_snapshot(it.curb_weight, tiers)
        unit = _unit_credit(it.power_consumption, limit, multiplier)
        total = round(unit * it.annual_output, 2) if it.annual_output > 0 else 0.0
        items.append({
            "vehicle_model_id": it.vehicle_model_id,
            "year": version.year,
            "power_consumption_limit": limit,
            "actual_power_consumption": it.power_consumption,
            "unit_credit": unit,
            "annual_output": it.annual_output,
            "total_credit": total,
        })
        if total > 0:
            total_positive += total
        elif total < 0:
            total_negative += total

    net = round(total_positive + total_negative, 2)
    required = abs(total_negative) if total_negative < 0 else 0.0
    summary = {
        "total_positive_credit": round(total_positive, 2),
        "total_negative_credit": round(total_negative, 2),
        "net_credit": net,
        "credit_gap": round(max(0, required - total_positive), 2),
        "credit_surplus": round(max(0, total_positive - required), 2),
        "model_count": len(items),
    }
    return {"items": items, "summary": summary}


# ---------------------------------------------------------------------------
# 年度汇总（封账后冻结核算口径，交易/结转衍生字段照常滚动）
# ---------------------------------------------------------------------------

def _get_or_create_summary(db: Session, enterprise_id: int, year: int) -> models.AnnualCreditSummary:
    summary = (
        db.query(models.AnnualCreditSummary)
        .filter(
            models.AnnualCreditSummary.enterprise_id == enterprise_id,
            models.AnnualCreditSummary.year == year,
        )
        .first()
    )
    if summary:
        return summary
    summary = models.AnnualCreditSummary(enterprise_id=enterprise_id, year=year)
    db.add(summary)
    db.flush()
    return summary


def _refresh_derived_summary_fields(db: Session, summary: models.AnnualCreditSummary) -> None:
    """依据冻结的核算净积分，重算交易买卖、结转与最终可用口径。"""
    year = summary.year
    enterprise_id = summary.enterprise_id

    bought = sold = 0.0
    txns = db.query(models.CreditTransaction).all()
    for txn in txns:
        txn_year = txn.transaction_date.year if txn.transaction_date else year
        if txn_year != year:
            continue
        if txn.to_enterprise_id == enterprise_id:
            bought += txn.credit_amount
        if txn.from_enterprise_id == enterprise_id:
            sold += txn.credit_amount

    carry_in = (
        db.query(models.CreditCarryover)
        .filter(
            models.CreditCarryover.enterprise_id == enterprise_id,
            models.CreditCarryover.to_year == year,
            models.CreditCarryover.status == models.CarryoverStatus.APPROVED,
        )
        .all()
    )
    carry_out = (
        db.query(models.CreditCarryover)
        .filter(
            models.CreditCarryover.enterprise_id == enterprise_id,
            models.CreditCarryover.from_year == year,
            models.CreditCarryover.status == models.CarryoverStatus.APPROVED,
        )
        .all()
    )
    summary.bought_credit = round(bought, 2)
    summary.sold_credit = round(sold, 2)
    summary.carryover_in = round(sum(c.carryover_amount for c in carry_in), 2)
    summary.carryover_out = round(sum(c.carryover_amount for c in carry_out), 2)

    available = round(
        summary.net_credit
        + summary.carryover_in
        + summary.bought_credit
        - summary.sold_credit
        - summary.carryover_out,
        2,
    )
    summary.final_net_credit = available
    if available >= 0:
        summary.credit_gap = 0.0
        summary.credit_surplus = available
        summary.is_compliant = True
    else:
        summary.credit_gap = abs(available)
        summary.credit_surplus = 0.0
        summary.is_compliant = False
    summary.updated_at = datetime.utcnow()


def _ensure_snapshot_version(
    db: Session, enterprise_id: int, year: int
) -> Optional[InputVersion]:
    """封账时对未主动提交版本的企业，以当前车型数据生成封账快照版本。"""
    existing = get_latest_input_version(db, enterprise_id, year)
    if existing:
        return existing

    items = _snapshot_models(db, enterprise_id, year)
    if not items:
        return None

    content_hash = _content_hash(items)
    version = InputVersion(
        version_no=f"IV-{year}-E{enterprise_id:04d}-S01",
        enterprise_id=enterprise_id,
        year=year,
        version_seq=0,
        source=InputVersionSource.SEAL_SNAPSHOT,
        submitter="system-seal",
        evidence_doc_no=None,
        remark="封账时系统自动生成的输入快照",
        content_hash=content_hash,
        is_sealed=False,
    )
    db.add(version)
    db.flush()
    for it in items:
        db.add(InputVersionItem(
            input_version_id=version.id,
            vehicle_model_id=it["vehicle_model_id"],
            model_code=it["model_code"],
            model_name=it["model_name"],
            curb_weight=it["curb_weight"],
            power_consumption=it["power_consumption"],
            range_km=it["range_km"],
            annual_output=it["annual_output"],
            evidence_no=it.get("evidence_no"),
        ))
    db.add(LimitStandardSnapshot(
        input_version_id=version.id,
        tiers_json=limit_standard_json(),
        credit_multiplier=CREDIT_MULTIPLIER,
        rule_version=serialize_limit_standard()["rule_version"],
    ))
    db.flush()
    return version


# ---------------------------------------------------------------------------
# 封账
# ---------------------------------------------------------------------------

def seal_year(
    db: Session, year: int, confirmed_by: str, remark: Optional[str] = None
) -> AccountingSeal:
    """
    监管确认封账：冻结各企业最新输入版本、限值标准、逐车型结果与企业汇总。
    已存在生效封账时拒绝（必须先走重开流程）。
    """
    if get_active_seal(db, year):
        raise ValueError(f"{year} 年度已封账；如需调整须凭重开决定重算，不能直接重新封账")

    enterprises = (
        db.query(models.Enterprise)
        .join(models.VehicleModel, models.VehicleModel.enterprise_id == models.Enterprise.id)
        .filter(models.VehicleModel.production_year == year)
        .distinct()
        .all()
    )
    submitted_ents = {
        row[0]
        for row in db.query(InputVersion.enterprise_id)
        .filter(InputVersion.year == year)
        .distinct()
        .all()
    }
    all_ent_ids = sorted({e.id for e in enterprises} | submitted_ents)
    if not all_ent_ids:
        raise ValueError(f"{year} 年度没有任何可封账的企业输入")

    seq = (
        db.query(models.AccountingSeal)
        .filter(models.AccountingSeal.year == year)
        .count()
    ) + 1

    chosen_versions: Dict[int, InputVersion] = {}
    for ent_id in all_ent_ids:
        version = _ensure_snapshot_version(db, ent_id, year)
        if version is None:
            continue
        chosen_versions[ent_id] = version

    if not chosen_versions:
        raise ValueError(f"{year} 年度没有任何可封账的企业输入")

    base_version = sorted(chosen_versions.values(), key=lambda v: v.id)[0]
    seal = AccountingSeal(
        seal_no=f"SEAL{year}{seq:03d}",
        year=year,
        seal_seq=seq,
        status=SealStatus.SEALED,
        input_version_id=base_version.id,
        confirmed_by=confirmed_by,
        sealed_at=datetime.utcnow(),
        remark=remark,
    )
    db.add(seal)
    db.flush()

    now = datetime.utcnow()
    for ent_id, version in chosen_versions.items():
        computed = compute_results_from_version(version)
        for item in computed["items"]:
            db.add(SealResultItem(seal_id=seal.id, enterprise_id=ent_id, input_version_id=version.id, **item))
        s = computed["summary"]
        db.add(SealEnterpriseSummary(
            seal_id=seal.id,
            enterprise_id=ent_id,
            input_version_id=version.id,
            year=year,
            total_positive_credit=s["total_positive_credit"],
            total_negative_credit=s["total_negative_credit"],
            net_credit=s["net_credit"],
            credit_gap=s["credit_gap"],
            credit_surplus=s["credit_surplus"],
            model_count=s["model_count"],
        ))
        version.is_sealed = True

        summary = _get_or_create_summary(db, ent_id, year)
        summary.total_positive_credit = s["total_positive_credit"]
        summary.total_negative_credit = s["total_negative_credit"]
        summary.net_credit = s["net_credit"]
        summary.seal_id = seal.id
        summary.input_version_id = version.id
        _refresh_derived_summary_fields(db, summary)

        # 既有积分记录随封账冻结、置为确认
        records = (
            db.query(models.CreditRecord)
            .join(models.VehicleModel)
            .filter(
                models.VehicleModel.enterprise_id == ent_id,
                models.CreditRecord.year == year,
            )
            .all()
        )
        for record in records:
            record.seal_id = seal.id
            record.input_version_id = version.id
            record.status = models.CreditRecordStatus.CONFIRMED
            record.confirmed_at = now
            record.updated_at = now

    db.commit()
    db.refresh(seal)
    return seal


# ---------------------------------------------------------------------------
# 重开与批量重算
# ---------------------------------------------------------------------------

DIFF_MODEL_FIELDS = [
    ("power_consumption_limit", "电耗限值"),
    ("actual_power_consumption", "实际电耗"),
    ("annual_output", "年产量"),
    ("unit_credit", "单车积分"),
    ("total_credit", "车型总积分"),
]

DIFF_SUMMARY_FIELDS = [
    ("total_positive_credit", "当年正积分"),
    ("total_negative_credit", "当年负积分"),
    ("net_credit", "当年净积分"),
    ("credit_gap", "积分缺口"),
    ("credit_surplus", "积分钟余"),
]


def _build_diff_rows(
    decision_id: int,
    enterprise_id: int,
    old_items: List[SealResultItem],
    new_items: List[dict],
    old_summary: SealEnterpriseSummary,
    new_summary: dict,
) -> List[RecalcDifferenceItem]:
    rows: List[RecalcDifferenceItem] = []
    old_by_model = {i.vehicle_model_id: i for i in old_items}
    new_by_model = {i["vehicle_model_id"]: i for i in new_items}

    for model_id in sorted(set(old_by_model) | set(new_by_model)):
        o = old_by_model.get(model_id)
        n = new_by_model.get(model_id)
        for field, _label in DIFF_MODEL_FIELDS:
            old_val = getattr(o, field) if o else 0.0
            new_val = n[field] if n else 0.0
            rows.append(RecalcDifferenceItem(
                decision_id=decision_id,
                enterprise_id=enterprise_id,
                vehicle_model_id=model_id,
                item_name=field,
                old_value=round(float(old_val), 4),
                new_value=round(float(new_val), 4),
                delta=round(float(new_val) - float(old_val), 4),
            ))

    for field, _label in DIFF_SUMMARY_FIELDS:
        old_val = getattr(old_summary, field) if old_summary else 0.0
        new_val = new_summary[field]
        rows.append(RecalcDifferenceItem(
            decision_id=decision_id,
            enterprise_id=enterprise_id,
            vehicle_model_id=None,
            item_name=field,
            old_value=round(float(old_val), 4),
            new_value=round(float(new_val), 4),
            delta=round(float(new_val) - float(old_val), 4),
        ))
    return rows


def reopen_and_recalculate(
    db: Session,
    *,
    decision_no: str,
    reason: str,
    requested_by: str,
    approved_by: str,
    authority_role: str,
    enterprise_ids: List[int],
    year: Optional[int] = None,
    base_seal_id: Optional[int] = None,
    version_by_enterprise: Optional[Dict[int, int]] = None,
    input_version_id: Optional[int] = None,
) -> Tuple[ReopenDecision, bool]:
    """
    凭重开决定基于指定输入版本重算，并生成新封账与逐项差异。

    - 同一 decision_no 重复执行直接返回原决定，不再生成新版本/新封账（幂等）。
    - 单个受影响企业重算失败：新封账中复制其原封账结果，年度汇总仍指向原封账。
    - 未列入受影响清单的企业，新封账中原样保留其结果。
    """
    existing = get_reopen_decision_by_no(db, decision_no)
    if existing:
        return existing, False

    if not reason or not reason.strip():
        raise ValueError("必须填写重开理由")
    if not approved_by or not authority_role:
        raise ValueError("必须记录批准人与批准权限")
    if not enterprise_ids:
        raise ValueError("必须指定受影响企业")

    base = get_seal(db, base_seal_id) if base_seal_id else get_active_seal(db, year)
    if not base:
        raise ValueError("未找到生效封账，无法重开")
    if base.status == SealStatus.REOPENED:
        raise ValueError("该封账已被重开，不能重复重开，请基于当前生效封账发起")
    year = base.year

    version_by_enterprise = version_by_enterprise or {}
    designated: Dict[int, InputVersion] = {}
    affected_meta: Dict[int, dict] = {}
    for ent_id in enterprise_ids:
        vid = version_by_enterprise.get(ent_id, input_version_id)
        fail_reason = None
        version = None
        if vid is None:
            fail_reason = "未指定重算所依据的输入版本"
        else:
            version = get_input_version(db, vid)
            if not version:
                fail_reason = f"指定的输入版本(id={vid})不存在"
            elif version.enterprise_id != ent_id or version.year != year:
                fail_reason = f"输入版本(id={vid})不属于该企业 {year} 年度，无法据此重算"
        if fail_reason:
            affected_meta[ent_id] = {"status": "failed", "reason": fail_reason, "version": None}
        else:
            affected_meta[ent_id] = {"status": "pending", "reason": None, "version": version}
            designated[ent_id] = version

    if designated:
        decision_version_id = next(iter(designated.values())).id
    else:
        decision_version_id = base.input_version_id

    decision = ReopenDecision(
        decision_no=decision_no,
        year=year,
        base_seal_id=base.id,
        reason=reason,
        requested_by=requested_by,
        approved_by=approved_by,
        authority_role=authority_role,
        input_version_id=input_version_id or decision_version_id,
        status=ReopenStatus.APPROVED,
    )
    db.add(decision)
    db.flush()

    for ent_id in enterprise_ids:
        meta = affected_meta[ent_id]
        db.add(ReopenAffectedEnterprise(
            decision_id=decision.id,
            enterprise_id=ent_id,
            recalc_status=meta["status"],
            fail_reason=meta["reason"],
        ))

    new_seq = base.seal_seq + 1
    new_seal = AccountingSeal(
        seal_no=f"SEAL{year}{new_seq:03d}",
        year=year,
        seal_seq=new_seq,
        status=SealStatus.SEALED,
        input_version_id=base.input_version_id,
        confirmed_by=approved_by,
        sealed_at=datetime.utcnow(),
        remark=f"依据重开决定 {decision_no} 重算后重新封账",
        reopen_decision_id=decision.id,
    )
    db.add(new_seal)
    db.flush()

    old_items_by_ent: Dict[int, List[SealResultItem]] = {}
    for item in base.result_items:
        old_items_by_ent.setdefault(item.enterprise_id, []).append(item)
    old_summary_by_ent = {s.enterprise_id: s for s in base.enterprise_summaries}

    all_ent_ids = sorted(old_summary_by_ent.keys())
    for ent_id in all_ent_ids:
        old_items = old_items_by_ent.get(ent_id, [])
        old_summary = old_summary_by_ent[ent_id]

        new_version: Optional[InputVersion] = designated.get(ent_id)
        if ent_id in affected_meta and affected_meta[ent_id]["status"] == "failed":
            # 失败企业：原样保留原封账结果与原版本引用
            for oi in old_items:
                db.add(SealResultItem(
                    seal_id=new_seal.id,
                    enterprise_id=ent_id,
                    input_version_id=oi.input_version_id,
                    year=oi.year,
                    power_consumption_limit=oi.power_consumption_limit,
                    actual_power_consumption=oi.actual_power_consumption,
                    unit_credit=oi.unit_credit,
                    annual_output=oi.annual_output,
                    total_credit=oi.total_credit,
                    vehicle_model_id=oi.vehicle_model_id,
                ))
            db.add(SealEnterpriseSummary(
                seal_id=new_seal.id,
                enterprise_id=ent_id,
                input_version_id=old_summary.input_version_id,
                year=year,
                total_positive_credit=old_summary.total_positive_credit,
                total_negative_credit=old_summary.total_negative_credit,
                net_credit=old_summary.net_credit,
                credit_gap=old_summary.credit_gap,
                credit_surplus=old_summary.credit_surplus,
                model_count=old_summary.model_count,
            ))
            continue

        if new_version is None:
            # 未受影响企业：沿用原版本与原结果
            new_version = get_input_version(db, old_summary.input_version_id)
            computed_items = [{
                "vehicle_model_id": oi.vehicle_model_id,
                "year": oi.year,
                "power_consumption_limit": oi.power_consumption_limit,
                "actual_power_consumption": oi.actual_power_consumption,
                "unit_credit": oi.unit_credit,
                "annual_output": oi.annual_output,
                "total_credit": oi.total_credit,
            } for oi in old_items]
            computed_summary = {
                "total_positive_credit": old_summary.total_positive_credit,
                "total_negative_credit": old_summary.total_negative_credit,
                "net_credit": old_summary.net_credit,
                "credit_gap": old_summary.credit_gap,
                "credit_surplus": old_summary.credit_surplus,
                "model_count": old_summary.model_count,
            }
        else:
            try:
                computed = compute_results_from_version(new_version)
                computed_items = computed["items"]
                computed_summary = computed["summary"]
                affected_meta[ent_id]["status"] = "success"
            except Exception as exc:  # 单企业失败隔离
                affected_meta[ent_id]["status"] = "failed"
                affected_meta[ent_id]["reason"] = f"重算异常：{exc}"
                affected_row = next(
                    a for a in decision.affected_enterprises if a.enterprise_id == ent_id
                )
                affected_row.recalc_status = "failed"
                affected_row.fail_reason = affected_meta[ent_id]["reason"]
                for oi in old_items:
                    db.add(SealResultItem(
                        seal_id=new_seal.id,
                        enterprise_id=ent_id,
                        input_version_id=oi.input_version_id,
                        year=oi.year,
                        power_consumption_limit=oi.power_consumption_limit,
                        actual_power_consumption=oi.actual_power_consumption,
                        unit_credit=oi.unit_credit,
                        annual_output=oi.annual_output,
                        total_credit=oi.total_credit,
                        vehicle_model_id=oi.vehicle_model_id,
                    ))
                db.add(SealEnterpriseSummary(
                    seal_id=new_seal.id,
                    enterprise_id=ent_id,
                    input_version_id=old_summary.input_version_id,
                    year=year,
                    total_positive_credit=old_summary.total_positive_credit,
                    total_negative_credit=old_summary.total_negative_credit,
                    net_credit=old_summary.net_credit,
                    credit_gap=old_summary.credit_gap,
                    credit_surplus=old_summary.credit_surplus,
                    model_count=old_summary.model_count,
                ))
                continue

        for item in computed_items:
            db.add(SealResultItem(
                seal_id=new_seal.id, enterprise_id=ent_id,
                input_version_id=new_version.id, **item,
            ))
        db.add(SealEnterpriseSummary(
            seal_id=new_seal.id,
            enterprise_id=ent_id,
            input_version_id=new_version.id,
            year=year,
            total_positive_credit=computed_summary["total_positive_credit"],
            total_negative_credit=computed_summary["total_negative_credit"],
            net_credit=computed_summary["net_credit"],
            credit_gap=computed_summary["credit_gap"],
            credit_surplus=computed_summary["credit_surplus"],
            model_count=computed_summary["model_count"],
        ))

        if ent_id in affected_meta and affected_meta[ent_id]["status"] == "success":
            for row in _build_diff_rows(
                decision.id, ent_id, old_items, computed_items, old_summary, computed_summary
            ):
                db.add(row)

            summary = _get_or_create_summary(db, ent_id, year)
            summary.total_positive_credit = computed_summary["total_positive_credit"]
            summary.total_negative_credit = computed_summary["total_negative_credit"]
            summary.net_credit = computed_summary["net_credit"]
            summary.seal_id = new_seal.id
            summary.input_version_id = new_version.id
            _refresh_derived_summary_fields(db, summary)

            # 当前积分记录同步为重算结果；旧结论已完整冻结在原封账与差异表中。
            computed_by_model = {i["vehicle_model_id"]: i for i in computed_items}
            records = (
                db.query(models.CreditRecord)
                .join(models.VehicleModel)
                .filter(
                    models.VehicleModel.enterprise_id == ent_id,
                    models.CreditRecord.year == year,
                )
                .all()
            )
            for record in records:
                new_item = computed_by_model.get(record.vehicle_model_id)
                if new_item:
                    record.power_consumption_limit = new_item["power_consumption_limit"]
                    record.actual_power_consumption = new_item["actual_power_consumption"]
                    record.unit_credit = new_item["unit_credit"]
                    record.annual_output = new_item["annual_output"]
                    record.total_credit = new_item["total_credit"]
                record.seal_id = new_seal.id
                record.input_version_id = new_version.id
                record.updated_at = datetime.utcnow()

            affected_row = next(
                a for a in decision.affected_enterprises if a.enterprise_id == ent_id
            )
            affected_row.recalc_status = "success"

    # 未重算（失败或不在受影响清单）的企业：结果值原样保留，但年度汇总与积分记录
    # 必须明确指向当前生效的新封账，避免下游引用已作废的旧封账。
    db.flush()
    success_ents = {
        a.enterprise_id for a in decision.affected_enterprises if a.recalc_status == "success"
    }
    preserved_summary_by_ent = {s.enterprise_id: s for s in new_seal.enterprise_summaries}
    for ent_id in all_ent_ids:
        if ent_id in success_ents:
            continue
        new_ent_summary = preserved_summary_by_ent.get(ent_id)
        if not new_ent_summary:
            continue
        summary = _get_or_create_summary(db, ent_id, year)
        summary.total_positive_credit = new_ent_summary.total_positive_credit
        summary.total_negative_credit = new_ent_summary.total_negative_credit
        summary.net_credit = new_ent_summary.net_credit
        summary.seal_id = new_seal.id
        summary.input_version_id = new_ent_summary.input_version_id
        _refresh_derived_summary_fields(db, summary)

        records = (
            db.query(models.CreditRecord)
            .join(models.VehicleModel)
            .filter(
                models.VehicleModel.enterprise_id == ent_id,
                models.CreditRecord.year == year,
            )
            .all()
        )
        for record in records:
            record.seal_id = new_seal.id
            record.input_version_id = new_ent_summary.input_version_id
            record.updated_at = datetime.utcnow()

    base.status = SealStatus.REOPENED
    base.superseded_by_seal_id = new_seal.id
    decision.status = ReopenStatus.EXECUTED
    decision.executed_at = datetime.utcnow()
    decision.new_seal_id = new_seal.id

    db.commit()
    db.refresh(decision)
    return decision, True
