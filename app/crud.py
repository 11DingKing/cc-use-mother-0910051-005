import json
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from . import models, schemas
from .rules import (
    calculate_power_consumption_limit,
    calculate_unit_credit,
    calculate_total_credit,
    detect_weight_manipulation,
    EnterpriseCreditSummary,
    match_credit_transactions,
    match_orders_with_price,
    calculate_carryover_amount,
    calculate_chain_carryover_amount,
    validate_order_price,
    predict_next_year_credit,
    EnterpriseCreditSummaryV2,
    get_limit_standard_snapshot,
    compute_credit_results,
    diff_credit_results,
    diff_input_items,
    PRICE_FLOOR,
    PRICE_CEILING,
    DEFAULT_MARKET_PRICE,
    CARRYOVER_MAX_YEARS
)
from .models import (
    CreditRecordStatus,
    OrderType,
    OrderStatus,
    CarryoverStatus,
    InputVersionStatus,
    ClosureStatus,
    ReopenDecisionStatus
)

VALID_STATUS_TRANSITIONS = {
    CreditRecordStatus.CALCULATED: [CreditRecordStatus.PUBLICIZED],
    CreditRecordStatus.PUBLICIZED: [CreditRecordStatus.CONFIRMED],
    CreditRecordStatus.CONFIRMED: []
}


def validate_status_transition(
    current_status: CreditRecordStatus,
    target_status: CreditRecordStatus
) -> Tuple[bool, Optional[str]]:
    if current_status == target_status:
        return False, f"记录已经是 {target_status.value} 状态，无需重复操作"

    allowed_next = VALID_STATUS_TRANSITIONS.get(current_status, [])
    if target_status not in allowed_next:
        allowed_str = ", ".join([s.value for s in allowed_next]) if allowed_next else "无"
        return False, (
            f"不允许从 {current_status.value} 直接变更为 {target_status.value}。"
            f"合法的下一个状态: {allowed_str}"
        )

    return True, None


def get_enterprise(db: Session, enterprise_id: int) -> Optional[models.Enterprise]:
    return db.query(models.Enterprise).filter(models.Enterprise.id == enterprise_id).first()


def get_enterprise_by_name(db: Session, name: str) -> Optional[models.Enterprise]:
    return db.query(models.Enterprise).filter(models.Enterprise.name == name).first()


def get_enterprises(db: Session, skip: int = 0, limit: int = 100) -> List[models.Enterprise]:
    return db.query(models.Enterprise).offset(skip).limit(limit).all()


def create_enterprise(db: Session, enterprise: schemas.EnterpriseCreate) -> models.Enterprise:
    db_enterprise = models.Enterprise(**enterprise.model_dump())
    db.add(db_enterprise)
    db.commit()
    db.refresh(db_enterprise)
    return db_enterprise


def update_enterprise(
    db: Session, enterprise_id: int, enterprise_update: schemas.EnterpriseUpdate
) -> Optional[models.Enterprise]:
    db_enterprise = get_enterprise(db, enterprise_id)
    if not db_enterprise:
        return None
    update_data = enterprise_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_enterprise, key, value)
    db_enterprise.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_enterprise)
    return db_enterprise


def delete_enterprise(db: Session, enterprise_id: int) -> bool:
    db_enterprise = get_enterprise(db, enterprise_id)
    if not db_enterprise:
        return False
    db.delete(db_enterprise)
    db.commit()
    return True


def get_vehicle_model(db: Session, model_id: int) -> Optional[models.VehicleModel]:
    return db.query(models.VehicleModel).filter(models.VehicleModel.id == model_id).first()


def get_vehicle_model_by_code(db: Session, model_code: str) -> Optional[models.VehicleModel]:
    return db.query(models.VehicleModel).filter(models.VehicleModel.model_code == model_code).first()


def get_vehicle_models(
    db: Session, enterprise_id: Optional[int] = None, skip: int = 0, limit: int = 100
) -> List[models.VehicleModel]:
    query = db.query(models.VehicleModel)
    if enterprise_id:
        query = query.filter(models.VehicleModel.enterprise_id == enterprise_id)
    return query.offset(skip).limit(limit).all()


def create_vehicle_model(db: Session, model: schemas.VehicleModelCreate) -> models.VehicleModel:
    is_suspicious = detect_weight_manipulation(
        model.curb_weight,
        model.power_consumption,
        model.range
    )
    db_model = models.VehicleModel(
        **model.model_dump(),
        is_suspected_weight_manipulation=is_suspicious
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


def update_vehicle_model(
    db: Session, model_id: int, model_update: schemas.VehicleModelUpdate
) -> Optional[models.VehicleModel]:
    db_model = get_vehicle_model(db, model_id)
    if not db_model:
        return None
    update_data = model_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_model, key, value)

    if "curb_weight" in update_data or "power_consumption" in update_data or "range" in update_data:
        db_model.is_suspected_weight_manipulation = detect_weight_manipulation(
            db_model.curb_weight,
            db_model.power_consumption,
            db_model.range
        )

    db_model.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_model)
    return db_model


def delete_vehicle_model(db: Session, model_id: int) -> bool:
    db_model = get_vehicle_model(db, model_id)
    if not db_model:
        return False
    db.delete(db_model)
    db.commit()
    return True


def calculate_vehicle_credit(
    db: Session, model_id: int, year: Optional[int] = None
) -> Optional[schemas.CalculationResult]:
    db_model = get_vehicle_model(db, model_id)
    if not db_model:
        return None

    calc_year = year or db_model.production_year
    limit = calculate_power_consumption_limit(db_model.curb_weight)
    unit_credit = calculate_unit_credit(db_model.power_consumption, limit)
    total_credit = calculate_total_credit(unit_credit, db_model.annual_output)

    return schemas.CalculationResult(
        vehicle_model_id=db_model.id,
        model_name=db_model.model_name,
        curb_weight=db_model.curb_weight,
        power_consumption_limit=limit,
        actual_power_consumption=db_model.power_consumption,
        unit_credit=unit_credit,
        annual_output=db_model.annual_output,
        total_credit=total_credit,
        is_compliant=db_model.power_consumption <= limit
    )


def create_credit_record(db: Session, model_id: int, year: int) -> Optional[models.CreditRecord]:
    existing = db.query(models.CreditRecord).filter(
        models.CreditRecord.vehicle_model_id == model_id,
        models.CreditRecord.year == year
    ).first()
    if existing:
        return existing

    result = calculate_vehicle_credit(db, model_id, year)
    if not result:
        return None

    db_record = models.CreditRecord(
        vehicle_model_id=model_id,
        year=year,
        power_consumption_limit=result.power_consumption_limit,
        actual_power_consumption=result.actual_power_consumption,
        unit_credit=result.unit_credit,
        total_credit=result.total_credit,
        annual_output=result.annual_output,
        status=CreditRecordStatus.CALCULATED,
        calculated_at=datetime.utcnow()
    )
    db.add(db_record)
    db.commit()
    db.refresh(db_record)
    return db_record


def batch_calculate_credits(db: Session, year: int) -> List[models.CreditRecord]:
    models_list = db.query(models.VehicleModel).filter(
        models.VehicleModel.production_year == year
    ).all()
    records = []
    for model in models_list:
        record = create_credit_record(db, model.id, year)
        if record:
            records.append(record)
    return records


def get_credit_records(
    db: Session,
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[CreditRecordStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditRecord]:
    query = db.query(models.CreditRecord)
    if enterprise_id:
        query = query.join(models.VehicleModel).filter(
            models.VehicleModel.enterprise_id == enterprise_id
        )
    if year:
        query = query.filter(models.CreditRecord.year == year)
    if status:
        query = query.filter(models.CreditRecord.status == status)
    return query.offset(skip).limit(limit).all()


def update_credit_record_status(
    db: Session, record_id: int, status: CreditRecordStatus
) -> Optional[models.CreditRecord]:
    db_record = db.query(models.CreditRecord).filter(models.CreditRecord.id == record_id).first()
    if not db_record:
        return None

    is_valid, error_msg = validate_status_transition(db_record.status, status)
    if not is_valid:
        raise ValueError(error_msg)

    previous_status = db_record.status
    db_record.status = status
    now = datetime.utcnow()

    if status == CreditRecordStatus.PUBLICIZED:
        db_record.publicized_at = now
    elif status == CreditRecordStatus.CONFIRMED:
        db_record.confirmed_at = now

    db_record.updated_at = now
    db.commit()
    db.refresh(db_record)

    if status == CreditRecordStatus.CONFIRMED:
        vehicle_model = get_vehicle_model(db, db_record.vehicle_model_id)
        if vehicle_model:
            update_annual_summary_with_transactions(
                db, vehicle_model.enterprise_id, db_record.year
            )

    return db_record


def batch_update_credit_records_status(
    db: Session, year: int, status: CreditRecordStatus
) -> int:
    records = db.query(models.CreditRecord).filter(
        models.CreditRecord.year == year
    ).all()

    now = datetime.utcnow()
    count = 0
    updated_enterprise_ids = set()

    for record in records:
        is_valid, _ = validate_status_transition(record.status, status)
        if not is_valid:
            continue

        record.status = status
        if status == CreditRecordStatus.PUBLICIZED:
            record.publicized_at = now
        elif status == CreditRecordStatus.CONFIRMED:
            record.confirmed_at = now
            vehicle_model = get_vehicle_model(db, record.vehicle_model_id)
            if vehicle_model:
                updated_enterprise_ids.add(vehicle_model.enterprise_id)
        record.updated_at = now
        count += 1

    db.commit()

    for enterprise_id in updated_enterprise_ids:
        update_annual_summary_with_transactions(db, enterprise_id, year)

    return count


def calculate_enterprise_credit_summary(
    db: Session, enterprise_id: int, year: int
) -> Optional[EnterpriseCreditSummary]:
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        return None

    records = db.query(models.CreditRecord).join(models.VehicleModel).filter(
        models.VehicleModel.enterprise_id == enterprise_id,
        models.CreditRecord.year == year,
        models.CreditRecord.status == CreditRecordStatus.CONFIRMED
    ).all()

    if not records:
        return EnterpriseCreditSummary(
            enterprise_id=enterprise_id,
            enterprise_name=enterprise.name,
            total_positive_credit=0.0,
            total_negative_credit=0.0,
            net_credit=0.0,
            required_credit=0.0,
            credit_gap=0.0,
            credit_surplus=0.0,
            compliance_rate=100.0,
            average_power_consumption=0.0,
            weighted_power_consumption=0.0,
            model_count=0,
            compliant_model_count=0
        )

    total_positive = sum(r.total_credit for r in records if r.total_credit > 0)
    total_negative = sum(r.total_credit for r in records if r.total_credit < 0)
    net_credit = total_positive + total_negative

    total_output = sum(r.annual_output for r in records)
    total_pc_weighted = sum(r.actual_power_consumption * r.annual_output for r in records)
    avg_pc_weighted = round(total_pc_weighted / total_output, 2) if total_output > 0 else 0.0
    avg_pc_simple = round(sum(r.actual_power_consumption for r in records) / len(records), 2) if records else 0.0

    compliant_count = sum(1 for r in records if r.actual_power_consumption <= r.power_consumption_limit)
    compliance_rate = round((compliant_count / len(records)) * 100, 2) if records else 100.0

    required_credit = abs(total_negative) if total_negative < 0 else 0
    credit_gap = max(0, required_credit - total_positive)
    credit_surplus = max(0, total_positive - required_credit)

    return EnterpriseCreditSummary(
        enterprise_id=enterprise_id,
        enterprise_name=enterprise.name,
        total_positive_credit=round(total_positive, 2),
        total_negative_credit=round(total_negative, 2),
        net_credit=round(net_credit, 2),
        required_credit=round(required_credit, 2),
        credit_gap=round(credit_gap, 2),
        credit_surplus=round(credit_surplus, 2),
        compliance_rate=round(compliance_rate, 2),
        average_power_consumption=round(avg_pc_simple, 2),
        weighted_power_consumption=round(avg_pc_weighted, 2),
        model_count=len(records),
        compliant_model_count=compliant_count
    )


def calculate_all_enterprise_summaries(
    db: Session, year: int
) -> List[EnterpriseCreditSummary]:
    enterprises = get_enterprises(db)
    summaries = []
    for ent in enterprises:
        summary = calculate_enterprise_credit_summary(db, ent.id, year)
        if summary:
            summaries.append(summary)
    return summaries


def generate_transaction_no(db: Session, counter: Optional[int] = None) -> str:
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d%H%M%S")
    if counter is not None:
        return f"TXN{timestamp}{counter:04d}"

    max_id = db.query(func.max(models.CreditTransaction.id)).scalar() or 0
    session_new_count = sum(
        1 for obj in db.new
        if isinstance(obj, models.CreditTransaction) and obj.id is None
    )
    next_id = max_id + session_new_count + 1
    return f"TXN{timestamp}{next_id:04d}"


def create_credit_transaction(
    db: Session, transaction: schemas.CreditTransactionCreate
) -> models.CreditTransaction:
    txn_no = generate_transaction_no(db)
    unit_price = transaction.unit_price or 3000.0
    total_amount = transaction.total_amount or (transaction.credit_amount * unit_price)

    db_txn = models.CreditTransaction(
        **transaction.model_dump(exclude={"unit_price", "total_amount"}),
        transaction_no=txn_no,
        unit_price=unit_price,
        total_amount=total_amount
    )
    db.add(db_txn)
    db.commit()
    db.refresh(db_txn)
    return db_txn


def get_credit_transactions(
    db: Session,
    enterprise_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditTransaction]:
    query = db.query(models.CreditTransaction)
    if enterprise_id:
        query = query.filter(
            (models.CreditTransaction.from_enterprise_id == enterprise_id) |
            (models.CreditTransaction.to_enterprise_id == enterprise_id)
        )
    return query.order_by(models.CreditTransaction.transaction_date.desc()).offset(skip).limit(limit).all()


def match_and_execute_transactions(
    db: Session, year: int, unit_price: float = 3000.0
) -> Tuple[List[models.CreditTransaction], float, float]:
    summaries = calculate_all_enterprise_summaries(db, year)
    match_results = match_credit_transactions(summaries, unit_price)

    transactions = []
    for mr in match_results:
        txn = schemas.CreditTransactionCreate(
            from_enterprise_id=mr.from_enterprise_id,
            to_enterprise_id=mr.to_enterprise_id,
            credit_amount=round(mr.credit_amount, 2),
            unit_price=round(mr.unit_price, 2),
            total_amount=round(mr.total_amount, 2),
            remark=f"{year}年度双积分自动撮合交易"
        )
        db_txn = create_credit_transaction(db, txn)
        transactions.append(db_txn)
        update_annual_summary_after_transaction(db, db_txn, year)

    remaining_gap = sum(round(s.credit_gap, 2) for s in summaries if s.credit_gap > 0.01)
    remaining_surplus = sum(round(s.credit_surplus, 2) for s in summaries if s.credit_surplus > 0.01)

    return transactions, round(remaining_gap, 2), round(remaining_surplus, 2)


def get_suspicious_weight_models(db: Session) -> List[models.VehicleModel]:
    return db.query(models.VehicleModel).filter(
        models.VehicleModel.is_suspected_weight_manipulation == True
    ).all()


def get_enterprise_stats(db: Session, year: int) -> List[schemas.EnterpriseStatsResponse]:
    enterprises = get_enterprises(db)
    stats_list = []

    for ent in enterprises:
        models_list = get_vehicle_models(db, enterprise_id=ent.id)
        if not models_list:
            continue

        total_output = sum(m.annual_output for m in models_list)
        avg_pc = round(sum(m.power_consumption for m in models_list) / len(models_list), 2) if models_list else 0.0
        weighted_pc = round(sum(m.power_consumption * m.annual_output for m in models_list) / total_output, 2) if total_output > 0 else 0.0
        avg_limit = round(sum(calculate_power_consumption_limit(m.curb_weight) for m in models_list) / len(models_list), 2) if models_list else 0.0

        records = db.query(models.CreditRecord).join(models.VehicleModel).filter(
            models.VehicleModel.enterprise_id == ent.id,
            models.CreditRecord.year == year
        ).all()

        if records:
            compliant_count = sum(1 for r in records if r.actual_power_consumption <= r.power_consumption_limit)
            compliance_rate = round((compliant_count / len(records)) * 100, 2) if records else 100.0
            total_positive = sum(r.total_credit for r in records if r.total_credit > 0)
            total_negative = sum(r.total_credit for r in records if r.total_credit < 0)
        else:
            compliant_count = sum(1 for m in models_list if m.power_consumption <= calculate_power_consumption_limit(m.curb_weight))
            compliance_rate = round((compliant_count / len(models_list)) * 100, 2) if models_list else 100.0
            total_positive = 0.0
            total_negative = 0.0

        stats_list.append(schemas.EnterpriseStatsResponse(
            enterprise_id=ent.id,
            enterprise_name=ent.name,
            model_count=len(models_list),
            total_output=total_output,
            average_power_consumption=round(avg_pc, 2),
            weighted_power_consumption=round(weighted_pc, 2),
            average_power_consumption_limit=round(avg_limit, 2),
            compliance_rate=round(compliance_rate, 2),
            total_positive_credit=round(total_positive, 2),
            total_negative_credit=round(total_negative, 2),
            net_credit=round(total_positive + total_negative, 2)
        ))

    return stats_list


def generate_order_no(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    count = db.query(models.CreditOrder).filter(
        func.substr(models.CreditOrder.order_no, 3, 8) == today
    ).count() + 1
    return f"OR{today}{count:05d}"


def generate_carryover_no(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    count = db.query(models.CreditCarryover).filter(
        func.substr(models.CreditCarryover.carryover_no, 3, 8) == today
    ).count() + 1
    return f"CO{today}{count:05d}"


def create_credit_order(
    db: Session, order: schemas.CreditOrderCreate
) -> Optional[models.CreditOrder]:
    is_valid, error_msg = validate_order_price(order.unit_price)
    if not is_valid:
        raise ValueError(error_msg)

    enterprise = get_enterprise(db, order.enterprise_id)
    if not enterprise:
        raise ValueError("企业不存在")

    if order.order_type == OrderType.SELL:
        summary = calculate_enterprise_credit_summary_v2(
            db, order.enterprise_id, order.year
        )
        if summary and order.total_amount > summary.final_credit_surplus:
            raise ValueError("挂单数量超过可出售的积分钟余")

    order_no = generate_order_no(db)
    expires_at = order.expires_at or (datetime.now() + timedelta(days=90))

    db_order = models.CreditOrder(
        **order.model_dump(exclude={"expires_at"}),
        order_no=order_no,
        filled_amount=0.0,
        remaining_amount=order.total_amount,
        expires_at=expires_at
    )
    db.add(db_order)
    db.commit()
    db.refresh(db_order)
    return db_order


def get_credit_order(db: Session, order_id: int) -> Optional[models.CreditOrder]:
    return db.query(models.CreditOrder).filter(models.CreditOrder.id == order_id).first()


def get_credit_orders(
    db: Session,
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    order_type: Optional[OrderType] = None,
    status: Optional[OrderStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditOrder]:
    query = db.query(models.CreditOrder)
    if enterprise_id:
        query = query.filter(models.CreditOrder.enterprise_id == enterprise_id)
    if year:
        query = query.filter(models.CreditOrder.year == year)
    if order_type:
        query = query.filter(models.CreditOrder.order_type == order_type)
    if status:
        query = query.filter(models.CreditOrder.status == status)
    return query.order_by(models.CreditOrder.created_at.desc()).offset(skip).limit(limit).all()


def update_credit_order(
    db: Session, order_id: int, order_update: schemas.CreditOrderUpdate
) -> Optional[models.CreditOrder]:
    db_order = get_credit_order(db, order_id)
    if not db_order:
        return None

    if db_order.status not in [OrderStatus.PENDING, OrderStatus.PARTIAL]:
        raise ValueError("只有待成交或部分成交的订单可以修改")

    update_data = order_update.model_dump(exclude_unset=True)

    if "unit_price" in update_data:
        is_valid, error_msg = validate_order_price(update_data["unit_price"])
        if not is_valid:
            raise ValueError(error_msg)

    if "total_amount" in update_data:
        if update_data["total_amount"] < db_order.filled_amount:
            raise ValueError("挂单总量不能小于已成交数量")
        db_order.remaining_amount = round(
            update_data["total_amount"] - db_order.filled_amount, 2
        )

    for key, value in update_data.items():
        setattr(db_order, key, value)

    db_order.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_order)
    return db_order


def cancel_credit_order(db: Session, order_id: int) -> Optional[models.CreditOrder]:
    db_order = get_credit_order(db, order_id)
    if not db_order:
        return None

    if db_order.status not in [OrderStatus.PENDING, OrderStatus.PARTIAL]:
        raise ValueError("只有待成交或部分成交的订单可以取消")

    db_order.status = OrderStatus.CANCELLED
    db_order.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_order)
    return db_order


def match_orders_by_id(
    db: Session, match_request: schemas.CreditOrderMatchRequest
) -> Tuple[Optional[models.CreditTransaction], Optional[str]]:
    sell_order = get_credit_order(db, match_request.sell_order_id)
    buy_order = get_credit_order(db, match_request.buy_order_id)

    if not sell_order or not buy_order:
        return None, "订单不存在"

    if sell_order.order_type != OrderType.SELL:
        return None, "卖单类型错误"
    if buy_order.order_type != OrderType.BUY:
        return None, "买单类型错误"

    if sell_order.status not in [OrderStatus.PENDING, OrderStatus.PARTIAL]:
        return None, "卖单状态不可交易"
    if buy_order.status not in [OrderStatus.PENDING, OrderStatus.PARTIAL]:
        return None, "买单状态不可交易"

    if buy_order.unit_price < sell_order.unit_price:
        return None, f"买单价格({buy_order.unit_price})低于卖单价格({sell_order.unit_price})，无法成交"

    match_amount = min(
        match_request.credit_amount,
        sell_order.remaining_amount,
        buy_order.remaining_amount
    )
    match_amount = round(match_amount, 2)

    if match_amount <= 0.01:
        return None, "可成交数量不足"

    matched_price = round((sell_order.unit_price + buy_order.unit_price) / 2, 2)
    total_amount = round(match_amount * matched_price, 2)

    txn_create = schemas.CreditTransactionCreate(
        from_enterprise_id=sell_order.enterprise_id,
        to_enterprise_id=buy_order.enterprise_id,
        credit_amount=match_amount,
        unit_price=matched_price,
        total_amount=total_amount,
        remark=f"挂单交易：卖单{sell_order.order_no} → 买单{buy_order.order_no}"
    )

    db_txn = models.CreditTransaction(
        **txn_create.model_dump(),
        transaction_no=generate_transaction_no(db),
        sell_order_id=sell_order.id,
        buy_order_id=buy_order.id,
        status="completed"
    )
    db.add(db_txn)

    sell_order.filled_amount = round(sell_order.filled_amount + match_amount, 2)
    sell_order.remaining_amount = round(sell_order.remaining_amount - match_amount, 2)
    sell_order.status = OrderStatus.FILLED if sell_order.remaining_amount <= 0.01 else OrderStatus.PARTIAL
    sell_order.updated_at = datetime.utcnow()

    buy_order.filled_amount = round(buy_order.filled_amount + match_amount, 2)
    buy_order.remaining_amount = round(buy_order.remaining_amount - match_amount, 2)
    buy_order.status = OrderStatus.FILLED if buy_order.remaining_amount <= 0.01 else OrderStatus.PARTIAL
    buy_order.updated_at = datetime.utcnow()

    create_price_history(db, db_txn, sell_order.year)

    update_annual_summary_after_transaction(db, db_txn, sell_order.year)

    db.commit()
    db.refresh(db_txn)
    db.refresh(sell_order)
    db.refresh(buy_order)

    return db_txn, None


def match_all_pending_orders(
    db: Session, year: int
) -> Tuple[List[models.CreditTransaction], List[dict], float, float]:
    pending_sell_orders = get_credit_orders(
        db, year=year, order_type=OrderType.SELL, status=OrderStatus.PENDING
    )
    pending_partial_sell = get_credit_orders(
        db, year=year, order_type=OrderType.SELL, status=OrderStatus.PARTIAL
    )
    all_sell_orders = pending_sell_orders + pending_partial_sell

    pending_buy_orders = get_credit_orders(
        db, year=year, order_type=OrderType.BUY, status=OrderStatus.PENDING
    )
    pending_partial_buy = get_credit_orders(
        db, year=year, order_type=OrderType.BUY, status=OrderStatus.PARTIAL
    )
    all_buy_orders = pending_buy_orders + pending_partial_buy

    sell_order_dicts = []
    for o in all_sell_orders:
        enterprise = get_enterprise(db, o.enterprise_id)
        sell_order_dicts.append({
            "id": o.id,
            "enterprise_id": o.enterprise_id,
            "enterprise_name": enterprise.name if enterprise else "",
            "unit_price": o.unit_price,
            "remaining_amount": round(o.remaining_amount, 2),
            "filled_amount": round(o.filled_amount, 2)
        })

    buy_order_dicts = []
    for o in all_buy_orders:
        enterprise = get_enterprise(db, o.enterprise_id)
        buy_order_dicts.append({
            "id": o.id,
            "enterprise_id": o.enterprise_id,
            "enterprise_name": enterprise.name if enterprise else "",
            "unit_price": o.unit_price,
            "remaining_amount": round(o.remaining_amount, 2),
            "filled_amount": round(o.filled_amount, 2)
        })

    match_results = match_orders_with_price(sell_order_dicts, buy_order_dicts)

    transactions = []
    matched_order_info = []

    base_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    for i, mr in enumerate(match_results):
        credit_amount = round(mr.credit_amount, 2)
        matched_price = round(mr.matched_price, 2)
        total_amount = round(mr.total_amount, 2)

        txn_create = schemas.CreditTransactionCreate(
            from_enterprise_id=mr.sell_enterprise_id,
            to_enterprise_id=mr.buy_enterprise_id,
            credit_amount=credit_amount,
            unit_price=matched_price,
            total_amount=total_amount,
            remark=f"自动撮合：卖单ID{mr.sell_order_id} → 买单ID{mr.buy_order_id}"
        )

        db_txn = models.CreditTransaction(
            **txn_create.model_dump(),
            transaction_no=f"TXN{base_timestamp}{i+1:04d}",
            sell_order_id=mr.sell_order_id,
            buy_order_id=mr.buy_order_id,
            status="completed"
        )
        db.add(db_txn)
        transactions.append(db_txn)

        sell_order = get_credit_order(db, mr.sell_order_id)
        buy_order = get_credit_order(db, mr.buy_order_id)

        if sell_order:
            sell_order.filled_amount = round(sell_order.filled_amount + credit_amount, 2)
            sell_order.remaining_amount = round(sell_order.remaining_amount - credit_amount, 2)
            sell_order.status = OrderStatus.FILLED if sell_order.remaining_amount <= 0.01 else OrderStatus.PARTIAL
            sell_order.updated_at = datetime.utcnow()

        if buy_order:
            buy_order.filled_amount = round(buy_order.filled_amount + credit_amount, 2)
            buy_order.remaining_amount = round(buy_order.remaining_amount - credit_amount, 2)
            buy_order.status = OrderStatus.FILLED if buy_order.remaining_amount <= 0.01 else OrderStatus.PARTIAL
            buy_order.updated_at = datetime.utcnow()

        create_price_history(db, db_txn, year)
        update_annual_summary_after_transaction(db, db_txn, year)

        matched_order_info.append({
            "sell_order_id": mr.sell_order_id,
            "buy_order_id": mr.buy_order_id,
            "sell_enterprise": mr.sell_enterprise_name,
            "buy_enterprise": mr.buy_enterprise_name,
            "credit_amount": credit_amount,
            "matched_price": matched_price,
            "total_amount": total_amount
        })

    db.commit()

    remaining_sell = sum(round(o["remaining_amount"], 2) for o in sell_order_dicts if o["remaining_amount"] > 0.01)
    remaining_buy = sum(round(o["remaining_amount"], 2) for o in buy_order_dicts if o["remaining_amount"] > 0.01)

    return transactions, matched_order_info, round(remaining_buy, 2), round(remaining_sell, 2)


def create_price_history(
    db: Session, transaction: models.CreditTransaction, year: int
) -> models.PriceHistory:
    db_price = models.PriceHistory(
        year=year,
        trade_date=transaction.transaction_date,
        unit_price=transaction.unit_price or DEFAULT_MARKET_PRICE,
        credit_amount=transaction.credit_amount,
        total_amount=transaction.total_amount or (transaction.credit_amount * (transaction.unit_price or DEFAULT_MARKET_PRICE)),
        from_enterprise_id=transaction.from_enterprise_id,
        to_enterprise_id=transaction.to_enterprise_id,
        transaction_id=transaction.id
    )
    db.add(db_price)
    return db_price


def get_price_history(
    db: Session,
    year: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.PriceHistory]:
    query = db.query(models.PriceHistory)
    if year:
        query = query.filter(models.PriceHistory.year == year)
    if start_date:
        query = query.filter(models.PriceHistory.trade_date >= start_date)
    if end_date:
        query = query.filter(models.PriceHistory.trade_date <= end_date)
    return query.order_by(models.PriceHistory.trade_date.desc()).offset(skip).limit(limit).all()


def get_price_trend(db: Session, year: int) -> schemas.PriceTrendResponse:
    price_records = get_price_history(db, year=year, limit=10000)

    if not price_records:
        return schemas.PriceTrendResponse(
            year=year,
            avg_price=DEFAULT_MARKET_PRICE,
            min_price=PRICE_FLOOR,
            max_price=PRICE_CEILING,
            total_volume=0.0,
            total_value=0.0,
            trade_count=0,
            price_by_date=[]
        )

    prices = [p.unit_price for p in price_records]
    volumes = [p.credit_amount for p in price_records]
    values = [p.total_amount for p in price_records]

    daily_prices: Dict[str, List[float]] = {}
    for p in price_records:
        date_str = p.trade_date.strftime("%Y-%m-%d")
        if date_str not in daily_prices:
            daily_prices[date_str] = []
        daily_prices[date_str].append(p.unit_price)

    price_by_date = []
    for date_str in sorted(daily_prices.keys()):
        daily_avg = sum(daily_prices[date_str]) / len(daily_prices[date_str])
        price_by_date.append({
            "date": date_str,
            "avg_price": round(daily_avg, 2),
            "trade_count": len(daily_prices[date_str])
        })

    return schemas.PriceTrendResponse(
        year=year,
        avg_price=round(sum(prices) / len(prices), 2),
        min_price=round(min(prices), 2),
        max_price=round(max(prices), 2),
        total_volume=round(sum(volumes), 2),
        total_value=round(sum(values), 2),
        trade_count=len(price_records),
        price_by_date=price_by_date
    )


def get_market_overview(db: Session, year: int) -> schemas.MarketOverviewResponse:
    all_sell_orders = get_credit_orders(db, year=year, order_type=OrderType.SELL, limit=10000)
    all_buy_orders = get_credit_orders(db, year=year, order_type=OrderType.BUY, limit=10000)
    completed_txn = db.query(models.CreditTransaction).filter(
        models.CreditTransaction.status == "completed"
    ).all()

    pending_sell = [o for o in all_sell_orders if o.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]]
    pending_buy = [o for o in all_buy_orders if o.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]]

    sell_prices = [o.unit_price for o in all_sell_orders]
    buy_prices = [o.unit_price for o in all_buy_orders]

    return schemas.MarketOverviewResponse(
        year=year,
        total_sell_orders=len(all_sell_orders),
        total_buy_orders=len(all_buy_orders),
        total_sell_volume=round(sum(o.total_amount for o in all_sell_orders), 2),
        total_buy_volume=round(sum(o.total_amount for o in all_buy_orders), 2),
        avg_sell_price=round(sum(sell_prices) / len(sell_prices), 2) if sell_prices else 0.0,
        avg_buy_price=round(sum(buy_prices) / len(buy_prices), 2) if buy_prices else 0.0,
        min_sell_price=round(min(sell_prices), 2) if sell_prices else 0.0,
        max_sell_price=round(max(sell_prices), 2) if sell_prices else 0.0,
        min_buy_price=round(min(buy_prices), 2) if buy_prices else 0.0,
        max_buy_price=round(max(buy_prices), 2) if buy_prices else 0.0,
        pending_sell_volume=round(sum(o.remaining_amount for o in pending_sell), 2),
        pending_buy_volume=round(sum(o.remaining_amount for o in pending_buy), 2),
        matched_count=len(completed_txn),
        matched_volume=round(sum(t.credit_amount for t in completed_txn), 2),
        matched_value=round(sum(t.total_amount or 0 for t in completed_txn), 2)
    )


def create_credit_carryover(
    db: Session, carryover: schemas.CreditCarryoverCreate
) -> models.CreditCarryover:
    carryover_no = generate_carryover_no(db)

    db_carryover = models.CreditCarryover(
        **carryover.model_dump(),
        carryover_no=carryover_no,
        used_amount=0.0,
        remaining_amount=carryover.carryover_amount,
        approved_at=datetime.utcnow()
    )
    db.add(db_carryover)
    db.commit()
    db.refresh(db_carryover)
    return db_carryover


def get_credit_carryover(
    db: Session, carryover_id: int
) -> Optional[models.CreditCarryover]:
    return db.query(models.CreditCarryover).filter(
        models.CreditCarryover.id == carryover_id
    ).first()


def get_credit_carryovers(
    db: Session,
    enterprise_id: Optional[int] = None,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    status: Optional[CarryoverStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.CreditCarryover]:
    query = db.query(models.CreditCarryover)
    if enterprise_id:
        query = query.filter(models.CreditCarryover.enterprise_id == enterprise_id)
    if from_year:
        query = query.filter(models.CreditCarryover.from_year == from_year)
    if to_year:
        query = query.filter(models.CreditCarryover.to_year == to_year)
    if status:
        query = query.filter(models.CreditCarryover.status == status)
    return query.order_by(models.CreditCarryover.created_at.desc()).offset(skip).limit(limit).all()


def execute_yearly_carryover(
    db: Session, from_year: int, to_year: int
) -> List[models.CreditCarryover]:
    enterprises = get_enterprises(db)
    carryovers = []

    year_diff = to_year - from_year
    if year_diff > CARRYOVER_MAX_YEARS:
        return carryovers

    for enterprise in enterprises:
        try:
            current_year = from_year
            current_surplus = 0.0

            start_summary = get_or_create_annual_summary(db, enterprise.id, from_year)
            if not start_summary or start_summary.credit_surplus <= 0.01:
                continue

            current_surplus = start_summary.credit_surplus
            total_carryover_ratio = 1.0

            for step in range(year_diff):
                src_year = from_year + step
                tgt_year = src_year + 1

                src_summary = get_or_create_annual_summary(db, enterprise.id, src_year)
                tgt_summary = get_or_create_annual_summary(db, enterprise.id, tgt_year)

                if step == 0:
                    amount_to_carry = current_surplus
                else:
                    amount_to_carry = tgt_summary.credit_surplus if tgt_summary.credit_surplus > 0.01 else 0.0

                if amount_to_carry <= 0.01:
                    continue

                ratio, carryover_amount = calculate_carryover_amount(
                    amount_to_carry,
                    1
                )
                total_carryover_ratio *= ratio

                if carryover_amount <= 0.01:
                    continue

                carryover_create = schemas.CreditCarryoverCreate(
                    enterprise_id=enterprise.id,
                    from_year=src_year,
                    to_year=tgt_year,
                    original_amount=amount_to_carry,
                    carryover_ratio=ratio,
                    carryover_amount=carryover_amount,
                    remark=f"{src_year}年度结转至{tgt_year}年度，结转比例{int(ratio*100)}%"
                )

                db_carryover = create_credit_carryover(db, carryover_create)

                # 结转必须明确依据哪次封账：记录来源年度当前有效封账
                src_closure = get_active_closure(db, enterprise.id, src_year)
                if src_closure is not None:
                    db_carryover.source_closure_id = src_closure.id

                carryovers.append(db_carryover)

                src_summary.carryover_out = round(src_summary.carryover_out + carryover_amount, 2)
                src_summary.credit_surplus = round(src_summary.credit_surplus - carryover_amount, 2)
                src_summary.final_net_credit = round(src_summary.final_net_credit - carryover_amount, 2)
                src_summary.updated_at = datetime.utcnow()

                tgt_summary.carryover_in = round(tgt_summary.carryover_in + carryover_amount, 2)
                tgt_summary.final_net_credit = round(tgt_summary.final_net_credit + carryover_amount, 2)

                if tgt_summary.credit_gap > 0.01:
                    used_amount = min(carryover_amount, tgt_summary.credit_gap)
                    db_carryover.used_amount = used_amount
                    db_carryover.remaining_amount = round(carryover_amount - used_amount, 2)
                    tgt_summary.credit_gap = round(tgt_summary.credit_gap - used_amount, 2)

                    if tgt_summary.credit_gap <= 0.01:
                        tgt_summary.is_compliant = True
                        tgt_summary.credit_surplus = round(abs(tgt_summary.credit_gap), 2)
                        tgt_summary.credit_gap = 0.0
                else:
                    tgt_summary.credit_surplus = round(tgt_summary.credit_surplus + carryover_amount, 2)

                tgt_summary.updated_at = datetime.utcnow()

            db.commit()

        except Exception as e:
            db.rollback()
            continue

    return carryovers


def get_carryover_summary(
    db: Session, enterprise_id: int, year: int
) -> List[schemas.CarryoverSummaryResponse]:
    carryovers = get_credit_carryovers(
        db, enterprise_id=enterprise_id, to_year=year, status=CarryoverStatus.APPROVED
    )

    result = []
    for c in carryovers:
        enterprise = get_enterprise(db, c.enterprise_id)
        source_closure_no = None
        if c.source_closure_id is not None:
            closure = get_accounting_closure(db, c.source_closure_id)
            source_closure_no = closure.closure_no if closure else None
        result.append(schemas.CarryoverSummaryResponse(
            enterprise_id=c.enterprise_id,
            enterprise_name=enterprise.name if enterprise else "",
            from_year=c.from_year,
            to_year=c.to_year,
            original_surplus=c.original_amount,
            carryover_ratio=c.carryover_ratio,
            carryover_amount=c.carryover_amount,
            used_amount=c.used_amount,
            remaining_amount=c.remaining_amount,
            status=c.status.value,
            source_closure_id=c.source_closure_id,
            source_closure_no=source_closure_no
        ))

    return result


def get_or_create_annual_summary(
    db: Session, enterprise_id: int, year: int, commit: bool = True
) -> models.AnnualCreditSummary:
    existing = db.query(models.AnnualCreditSummary).filter(
        models.AnnualCreditSummary.enterprise_id == enterprise_id,
        models.AnnualCreditSummary.year == year
    ).first()

    if existing:
        return existing

    db_summary = models.AnnualCreditSummary(
        enterprise_id=enterprise_id,
        year=year
    )
    db.add(db_summary)
    if commit:
        db.commit()
        db.refresh(db_summary)
    else:
        db.flush()
    return db_summary


def update_annual_summary_with_transactions(
    db: Session, enterprise_id: int, year: int, commit: bool = True
) -> models.AnnualCreditSummary:
    """
    刷新企业年度汇总。若该企业当年存在有效封账，则当年核算口径
    （正/负/净积分）以封账冻结结果为准，并记录所采用的封账ID，
    保证下游年度汇总可以明确追溯采用了哪次封账。
    """
    summary = get_or_create_annual_summary(db, enterprise_id, year, commit=commit)

    closure = get_active_closure(db, enterprise_id, year)
    if closure is not None:
        closure_totals = json.loads(closure.result_snapshot)["totals"]
        summary.total_positive_credit = closure_totals["total_positive_credit"]
        summary.total_negative_credit = closure_totals["total_negative_credit"]
        summary.net_credit = closure_totals["net_credit"]
        summary.credit_gap = closure_totals["credit_gap"]
        summary.credit_surplus = closure_totals["credit_surplus"]
        summary.closure_id = closure.id
    else:
        base_summary = calculate_enterprise_credit_summary(db, enterprise_id, year)
        if base_summary:
            summary.total_positive_credit = base_summary.total_positive_credit
            summary.total_negative_credit = base_summary.total_negative_credit
            summary.net_credit = base_summary.net_credit
            summary.credit_gap = base_summary.credit_gap
            summary.credit_surplus = base_summary.credit_surplus
        summary.closure_id = None

    transactions = get_credit_transactions(db, enterprise_id=enterprise_id, limit=10000)
    bought = 0.0
    sold = 0.0
    for txn in transactions:
        txn_year = txn.transaction_date.year if txn.transaction_date else year
        if txn_year == year:
            if txn.to_enterprise_id == enterprise_id:
                bought += txn.credit_amount
            if txn.from_enterprise_id == enterprise_id:
                sold += txn.credit_amount

    summary.bought_credit = round(bought, 2)
    summary.sold_credit = round(sold, 2)

    carryovers_in = get_credit_carryovers(
        db, enterprise_id=enterprise_id, to_year=year, status=CarryoverStatus.APPROVED
    )
    total_carryover_in = sum(c.carryover_amount for c in carryovers_in)
    summary.carryover_in = round(total_carryover_in, 2)

    carryovers_out = get_credit_carryovers(
        db, enterprise_id=enterprise_id, from_year=year, status=CarryoverStatus.APPROVED
    )
    total_carryover_out = sum(c.carryover_amount for c in carryovers_out)
    summary.carryover_out = round(total_carryover_out, 2)

    available_credit = (
        summary.net_credit
        + summary.carryover_in
        + summary.bought_credit
        - summary.sold_credit
        - summary.carryover_out
    )
    summary.final_net_credit = round(available_credit, 2)

    if available_credit >= 0:
        summary.credit_gap = 0.0
        summary.credit_surplus = round(available_credit, 2)
        summary.is_compliant = True
    else:
        summary.credit_gap = round(abs(available_credit), 2)
        summary.credit_surplus = 0.0
        summary.is_compliant = False

    summary.updated_at = datetime.utcnow()
    if commit:
        db.commit()
        db.refresh(summary)
    else:
        db.flush()
    return summary


def calculate_enterprise_credit_summary_v2(
    db: Session, enterprise_id: int, year: int
) -> EnterpriseCreditSummaryV2:
    base_summary = calculate_enterprise_credit_summary(db, enterprise_id, year)

    annual_summary = update_annual_summary_with_transactions(db, enterprise_id, year)

    closure_no = None
    closure_totals = None
    if annual_summary.closure_id is not None:
        closure = get_accounting_closure(db, annual_summary.closure_id)
        if closure:
            closure_no = closure.closure_no
            closure_totals = json.loads(closure.result_snapshot)["totals"]

    # 存在有效封账时，当年核算口径以封账冻结结果为准（与监管公布一致）
    total_positive = closure_totals["total_positive_credit"] if closure_totals else base_summary.total_positive_credit
    total_negative = closure_totals["total_negative_credit"] if closure_totals else base_summary.total_negative_credit
    net_credit = closure_totals["net_credit"] if closure_totals else base_summary.net_credit
    required_credit = closure_totals["required_credit"] if closure_totals else base_summary.required_credit
    base_gap = closure_totals["credit_gap"] if closure_totals else base_summary.credit_gap
    base_surplus = closure_totals["credit_surplus"] if closure_totals else base_summary.credit_surplus

    v2_summary = EnterpriseCreditSummaryV2(
        enterprise_id=base_summary.enterprise_id,
        enterprise_name=base_summary.enterprise_name,
        total_positive_credit=total_positive,
        total_negative_credit=total_negative,
        net_credit=net_credit,
        required_credit=required_credit,
        credit_gap=base_gap,
        credit_surplus=base_surplus,
        compliance_rate=base_summary.compliance_rate,
        average_power_consumption=base_summary.average_power_consumption,
        weighted_power_consumption=base_summary.weighted_power_consumption,
        model_count=base_summary.model_count,
        compliant_model_count=base_summary.compliant_model_count,
        carryover_in=annual_summary.carryover_in,
        carryover_out=annual_summary.carryover_out,
        bought_credit=annual_summary.bought_credit,
        sold_credit=annual_summary.sold_credit,
        final_net_credit=annual_summary.final_net_credit,
        final_credit_gap=annual_summary.credit_gap,
        final_credit_surplus=annual_summary.credit_surplus,
        is_compliant=annual_summary.is_compliant,
        closure_id=annual_summary.closure_id,
        closure_no=closure_no
    )

    return v2_summary


def calculate_all_enterprise_summaries_v2(
    db: Session, year: int
) -> List[EnterpriseCreditSummaryV2]:
    enterprises = get_enterprises(db)
    summaries = []
    for ent in enterprises:
        summary = calculate_enterprise_credit_summary_v2(db, ent.id, year)
        if summary:
            summaries.append(summary)
    return summaries


def get_multi_year_summary(
    db: Session, enterprise_id: int, start_year: int, end_year: int
) -> schemas.EnterpriseMultiYearSummaryResponse:
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        raise ValueError("企业不存在")

    years = list(range(start_year, end_year + 1))
    annual_summaries = []
    total_carryover_in = 0.0
    total_carryover_out = 0.0
    total_bought = 0.0
    total_sold = 0.0

    for year in years:
        summary = update_annual_summary_with_transactions(db, enterprise_id, year)
        closure_no = None
        if summary.closure_id is not None:
            closure = get_accounting_closure(db, summary.closure_id)
            closure_no = closure.closure_no if closure else None
        annual_summaries.append({
            "year": year,
            "total_positive_credit": summary.total_positive_credit,
            "total_negative_credit": summary.total_negative_credit,
            "net_credit": summary.net_credit,
            "carryover_in": summary.carryover_in,
            "carryover_out": summary.carryover_out,
            "bought_credit": summary.bought_credit,
            "sold_credit": summary.sold_credit,
            "final_net_credit": summary.final_net_credit,
            "credit_gap": summary.credit_gap,
            "credit_surplus": summary.credit_surplus,
            "is_compliant": summary.is_compliant,
            "closure_id": summary.closure_id,
            "closure_no": closure_no
        })
        total_carryover_in += summary.carryover_in
        total_carryover_out += summary.carryover_out
        total_bought += summary.bought_credit
        total_sold += summary.sold_credit

    return schemas.EnterpriseMultiYearSummaryResponse(
        enterprise_id=enterprise_id,
        enterprise_name=enterprise.name,
        years=years,
        annual_summaries=annual_summaries,
        total_carryover_in=round(total_carryover_in, 2),
        total_carryover_out=round(total_carryover_out, 2),
        total_bought=round(total_bought, 2),
        total_sold=round(total_sold, 2)
    )


def predict_enterprise_credit(
    db: Session,
    enterprise_id: int,
    target_year: int,
    output_growth_rate: float = 0.05,
    pc_improvement_rate: float = 0.02
) -> schemas.CreditPredictionResponse:
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        raise ValueError("企业不存在")

    models_list = get_vehicle_models(db, enterprise_id=enterprise_id, limit=1000)
    if not models_list:
        return schemas.CreditPredictionResponse(
            enterprise_id=enterprise_id,
            enterprise_name=enterprise.name,
            target_year=target_year,
            predicted_total_positive=0.0,
            predicted_total_negative=0.0,
            predicted_net_credit=0.0,
            predicted_compliance_rate=0.0,
            prediction_method="无历史数据",
            assumptions={}
        )

    historical_data = []
    for m in models_list:
        historical_data.append({
            "model_code": m.model_code,
            "model_name": m.model_name,
            "power_consumption": m.power_consumption,
            "annual_output": m.annual_output,
            "curb_weight": m.curb_weight,
            "year": m.production_year
        })

    historical_years = sorted(list(set(m.production_year for m in models_list)))
    historical_credits = []
    for year in historical_years:
        summary = calculate_enterprise_credit_summary(db, enterprise_id, year)
        if summary:
            historical_credits.append({
                "year": year,
                "total_positive": summary.total_positive_credit,
                "total_negative": summary.total_negative_credit,
                "net_credit": summary.net_credit,
                "compliance_rate": summary.compliance_rate
            })

    prediction = predict_next_year_credit(
        historical_data, output_growth_rate, pc_improvement_rate
    )

    model_predictions = [
        schemas.ModelPrediction(**md) for md in prediction.model_details
    ]

    return schemas.CreditPredictionResponse(
        enterprise_id=enterprise_id,
        enterprise_name=enterprise.name,
        target_year=target_year,
        historical_years=historical_years,
        historical_credits=historical_credits,
        predicted_total_positive=prediction.total_positive,
        predicted_total_negative=prediction.total_negative,
        predicted_net_credit=prediction.net_credit,
        predicted_compliance_rate=prediction.compliance_rate,
        model_predictions=model_predictions,
        prediction_method="趋势外推法",
        assumptions={
            "output_growth_rate": output_growth_rate,
            "pc_improvement_rate": pc_improvement_rate,
            "description": "基于历史最新车型数据，按给定增长率和改善率预测下一年度积分"
        }
    )


def predict_all_enterprises_credit(
    db: Session,
    target_year: int,
    output_growth_rate: float = 0.05,
    pc_improvement_rate: float = 0.02
) -> List[schemas.CreditPredictionResponse]:
    enterprises = get_enterprises(db)
    predictions = []
    for ent in enterprises:
        pred = predict_enterprise_credit(
            db, ent.id, target_year, output_growth_rate, pc_improvement_rate
        )
        predictions.append(pred)
    return predictions


def update_annual_summary_after_transaction(
    db: Session, transaction: models.CreditTransaction, year: int
) -> None:
    update_annual_summary_with_transactions(db, transaction.from_enterprise_id, year)
    update_annual_summary_with_transactions(db, transaction.to_enterprise_id, year)


def update_credit_record(
    db: Session, record_id: int, record_update: schemas.CreditRecordUpdate
) -> Optional[models.CreditRecord]:
    db_record = db.query(models.CreditRecord).filter(models.CreditRecord.id == record_id).first()
    if not db_record:
        return None

    if db_record.status == CreditRecordStatus.CONFIRMED:
        raise ValueError("已确认的积分记录不允许修改，如需修改请先联系管理员")

    update_data = record_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_record, key, value)

    db_record.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_record)

    vehicle_model = get_vehicle_model(db, db_record.vehicle_model_id)
    if vehicle_model:
        update_annual_summary_with_transactions(
            db, vehicle_model.enterprise_id, db_record.year
        )

    return db_record


# ---------------------------------------------------------------------------
# 核算输入版本与封账
# ---------------------------------------------------------------------------

def _input_item_to_dict(item: models.AccountingInputItem) -> Dict:
    return {
        "model_code": item.model_code,
        "model_name": item.model_name,
        "curb_weight": item.curb_weight,
        "power_consumption": item.power_consumption,
        "range_km": item.range_km,
        "annual_output": item.annual_output,
    }


def submit_accounting_input_version(
    db: Session,
    enterprise_id: int,
    year: int,
    items: List[Dict],
    submitted_by: str,
    note: Optional[str] = None
) -> models.AccountingInputVersion:
    """
    企业提交核算输入（产量/能耗凭证），形成一份完整、可比较的输入版本。
    版本一旦创建不可修改；同一企业同一年度再次提交会生成递增的新版本，
    旧的未封账提交版本标记为已被取代，历史版本保留用于比较与追溯。
    """
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        raise ValueError("企业不存在")
    if not items:
        raise ValueError("提交的核算输入不能为空，每次提交必须包含完整的车型明细")

    model_codes = [item["model_code"] for item in items]
    if len(model_codes) != len(set(model_codes)):
        raise ValueError("同一次提交中车型代码不能重复")

    max_no = db.query(func.max(models.AccountingInputVersion.version_no)).filter(
        models.AccountingInputVersion.enterprise_id == enterprise_id,
        models.AccountingInputVersion.year == year
    ).scalar() or 0

    db_version = models.AccountingInputVersion(
        enterprise_id=enterprise_id,
        year=year,
        version_no=max_no + 1,
        status=InputVersionStatus.SUBMITTED,
        submitted_by=submitted_by,
        note=note
    )
    db.add(db_version)
    db.flush()

    for item in items:
        db.add(models.AccountingInputItem(version_id=db_version.id, **item))

    # 旧的未封账提交版本被本次提交取代（已封账采用的版本保留原状态）
    db.query(models.AccountingInputVersion).filter(
        models.AccountingInputVersion.enterprise_id == enterprise_id,
        models.AccountingInputVersion.year == year,
        models.AccountingInputVersion.id != db_version.id,
        models.AccountingInputVersion.status == InputVersionStatus.SUBMITTED
    ).update({"status": InputVersionStatus.SUPERSEDED}, synchronize_session=False)

    db.commit()
    db.refresh(db_version)
    return db_version


def get_accounting_input_version(
    db: Session, version_id: int
) -> Optional[models.AccountingInputVersion]:
    return db.query(models.AccountingInputVersion).filter(
        models.AccountingInputVersion.id == version_id
    ).first()


def get_accounting_input_versions(
    db: Session,
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[InputVersionStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.AccountingInputVersion]:
    query = db.query(models.AccountingInputVersion)
    if enterprise_id:
        query = query.filter(models.AccountingInputVersion.enterprise_id == enterprise_id)
    if year:
        query = query.filter(models.AccountingInputVersion.year == year)
    if status:
        query = query.filter(models.AccountingInputVersion.status == status)
    return query.order_by(
        models.AccountingInputVersion.enterprise_id,
        models.AccountingInputVersion.year,
        models.AccountingInputVersion.version_no
    ).offset(skip).limit(limit).all()


def get_latest_input_version(
    db: Session, enterprise_id: int, year: int
) -> Optional[models.AccountingInputVersion]:
    return db.query(models.AccountingInputVersion).filter(
        models.AccountingInputVersion.enterprise_id == enterprise_id,
        models.AccountingInputVersion.year == year
    ).order_by(models.AccountingInputVersion.version_no.desc()).first()


def compare_input_versions(
    db: Session, version_a_id: int, version_b_id: int
) -> Dict:
    """比较两个输入版本，给出车型明细的字段级差异"""
    version_a = get_accounting_input_version(db, version_a_id)
    version_b = get_accounting_input_version(db, version_b_id)
    if not version_a or not version_b:
        raise ValueError("输入版本不存在，无法比较")

    old_items = [_input_item_to_dict(i) for i in version_a.items]
    new_items = [_input_item_to_dict(i) for i in version_b.items]
    diff = diff_input_items(old_items, new_items)
    diff["version_a"] = {"id": version_a.id, "version_no": version_a.version_no,
                         "enterprise_id": version_a.enterprise_id, "year": version_a.year}
    diff["version_b"] = {"id": version_b.id, "version_no": version_b.version_no,
                         "enterprise_id": version_b.enterprise_id, "year": version_b.year}
    return diff


def generate_closure_no(db: Session, year: int) -> str:
    max_id = db.query(func.max(models.AccountingClosure.id)).scalar() or 0
    return f"CL{year}-{max_id + 1:05d}"


def get_active_closure(
    db: Session, enterprise_id: int, year: int
) -> Optional[models.AccountingClosure]:
    """企业当年当前有效（SEALED）的封账，下游结转/汇总/交易可用额以此为准"""
    return db.query(models.AccountingClosure).filter(
        models.AccountingClosure.enterprise_id == enterprise_id,
        models.AccountingClosure.year == year,
        models.AccountingClosure.status == ClosureStatus.SEALED
    ).order_by(models.AccountingClosure.id.desc()).first()


def get_accounting_closure(db: Session, closure_id: int) -> Optional[models.AccountingClosure]:
    return db.query(models.AccountingClosure).filter(
        models.AccountingClosure.id == closure_id
    ).first()


def get_accounting_closures(
    db: Session,
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[ClosureStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.AccountingClosure]:
    query = db.query(models.AccountingClosure)
    if enterprise_id:
        query = query.filter(models.AccountingClosure.enterprise_id == enterprise_id)
    if year:
        query = query.filter(models.AccountingClosure.year == year)
    if status:
        query = query.filter(models.AccountingClosure.status == status)
    return query.order_by(models.AccountingClosure.id).offset(skip).limit(limit).all()


def seal_accounting(
    db: Session,
    enterprise_id: int,
    year: int,
    input_version_id: int,
    sealed_by: str,
    remark: Optional[str] = None
) -> models.AccountingClosure:
    """
    监管确认封账：冻结当期核算输入版本、限值标准与计算结果。
    已存在有效封账时禁止直接再次封账，必须通过重开决定流程重算，
    防止已公布积分被悄悄改写。
    """
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        raise ValueError("企业不存在")

    version = get_accounting_input_version(db, input_version_id)
    if not version:
        raise ValueError("核算输入版本不存在")
    if version.enterprise_id != enterprise_id or version.year != year:
        raise ValueError("核算输入版本与企业或年度不匹配")

    existing = get_active_closure(db, enterprise_id, year)
    if existing:
        raise ValueError(
            f"该企业{year}年度已存在有效封账({existing.closure_no})，"
            f"如需依据补交凭证重算，请先登记重开决定"
        )

    standard = get_limit_standard_snapshot()
    items = [_input_item_to_dict(i) for i in version.items]
    result = compute_credit_results(items, standard)

    db_closure = models.AccountingClosure(
        closure_no=generate_closure_no(db, year),
        enterprise_id=enterprise_id,
        year=year,
        input_version_id=version.id,
        limit_standard_snapshot=json.dumps(standard, ensure_ascii=False),
        result_snapshot=json.dumps(result, ensure_ascii=False),
        diff_from_previous=None,
        previous_closure_id=None,
        reopen_decision_id=None,
        status=ClosureStatus.SEALED,
        sealed_by=sealed_by,
        sealed_at=datetime.utcnow(),
        remark=remark
    )
    db.add(db_closure)
    db.flush()

    version.status = InputVersionStatus.SEALED
    db.commit()
    db.refresh(db_closure)

    # 年度汇总立即切换到本次封账口径
    update_annual_summary_with_transactions(db, enterprise_id, year)

    return db_closure


def generate_decision_no(db: Session, year: int) -> str:
    max_id = db.query(func.max(models.ReopenDecision.id)).scalar() or 0
    return f"RD{year}-{max_id + 1:04d}"


def create_reopen_decision(
    db: Session,
    year: int,
    enterprise_ids: List[int],
    reason: str,
    approved_by: str,
    input_version_map: Optional[Dict[int, int]] = None,
    remark: Optional[str] = None
) -> models.ReopenDecision:
    """登记重开决定：记录理由、审批权限与受影响企业"""
    if not reason or not reason.strip():
        raise ValueError("重开必须记录理由")
    if not approved_by or not approved_by.strip():
        raise ValueError("重开必须记录审批人（权限）")
    if not enterprise_ids:
        raise ValueError("重开必须指定受影响企业")

    unique_ids = []
    for ent_id in enterprise_ids:
        if ent_id not in unique_ids:
            unique_ids.append(ent_id)
        if not get_enterprise(db, ent_id):
            raise ValueError(f"受影响企业不存在(ID:{ent_id})")

    # 指定重算版本(input_version_map)不在登记时校验：
    # 执行时逐企业验证，无效版本仅使该企业重算失败并保持其原封账结果
    version_map = input_version_map or {}

    db_decision = models.ReopenDecision(
        decision_no=generate_decision_no(db, year),
        year=year,
        enterprise_ids=json.dumps(unique_ids),
        input_version_map=json.dumps({str(k): v for k, v in version_map.items()}),
        reason=reason,
        approved_by=approved_by,
        status=ReopenDecisionStatus.PENDING,
        remark=remark
    )
    db.add(db_decision)
    db.commit()
    db.refresh(db_decision)
    return db_decision


def get_reopen_decision(db: Session, decision_id: int) -> Optional[models.ReopenDecision]:
    return db.query(models.ReopenDecision).filter(
        models.ReopenDecision.id == decision_id
    ).first()


def get_reopen_decisions(
    db: Session,
    year: Optional[int] = None,
    status: Optional[ReopenDecisionStatus] = None,
    skip: int = 0,
    limit: int = 100
) -> List[models.ReopenDecision]:
    query = db.query(models.ReopenDecision)
    if year:
        query = query.filter(models.ReopenDecision.year == year)
    if status:
        query = query.filter(models.ReopenDecision.status == status)
    return query.order_by(models.ReopenDecision.id).offset(skip).limit(limit).all()


def _recalculate_closure_for_enterprise(
    db: Session,
    decision: models.ReopenDecision,
    enterprise_id: int,
    version_map: Dict[int, int]
) -> Dict:
    """
    基于指定输入版本为单个企业重算并生成新封账。
    旧封账完整保留（置为 superseded），新封账记录与旧封账的各积分项差异，
    年度汇总切换到新封账。任何失败都会抛出异常，由调用方回滚保存点，
    保证该企业维持原封账结果。
    """
    enterprise = get_enterprise(db, enterprise_id)
    if not enterprise:
        raise ValueError(f"受影响企业不存在(ID:{enterprise_id})")

    old_closure = get_active_closure(db, enterprise_id, decision.year)
    if not old_closure:
        raise ValueError("该企业当年无有效封账，无法重开，保持原状")

    if enterprise_id in version_map:
        input_version = get_accounting_input_version(db, version_map[enterprise_id])
        if not input_version:
            raise ValueError(f"指定的核算输入版本不存在(ID:{version_map[enterprise_id]})")
        if input_version.enterprise_id != enterprise_id or input_version.year != decision.year:
            raise ValueError("指定的核算输入版本与企业或年度不匹配")
    else:
        input_version = get_latest_input_version(db, enterprise_id, decision.year)
        if not input_version:
            raise ValueError("该企业当年没有可用于重算的核算输入版本")

    standard = get_limit_standard_snapshot()
    items = [_input_item_to_dict(i) for i in input_version.items]
    new_result = compute_credit_results(items, standard)

    old_result = json.loads(old_closure.result_snapshot)
    old_standard = json.loads(old_closure.limit_standard_snapshot)
    standard_changed = old_standard != standard

    diff = diff_credit_results(old_result, new_result, standard_changed)
    diff["previous_closure_id"] = old_closure.id
    diff["previous_closure_no"] = old_closure.closure_no
    diff["input_version_id"] = input_version.id
    diff["input_version_no"] = input_version.version_no

    new_closure = models.AccountingClosure(
        closure_no=generate_closure_no(db, decision.year),
        enterprise_id=enterprise_id,
        year=decision.year,
        input_version_id=input_version.id,
        limit_standard_snapshot=json.dumps(standard, ensure_ascii=False),
        result_snapshot=json.dumps(new_result, ensure_ascii=False),
        diff_from_previous=json.dumps(diff, ensure_ascii=False),
        previous_closure_id=old_closure.id,
        reopen_decision_id=decision.id,
        status=ClosureStatus.SEALED,
        sealed_by=decision.approved_by,
        sealed_at=datetime.utcnow(),
        remark=f"依据重开决定 {decision.decision_no} 重算生成"
    )
    db.add(new_closure)
    db.flush()

    # 旧封账不覆盖、不删除，仅标记为已被取代，历史结论可回溯
    old_closure.status = ClosureStatus.SUPERSEDED
    input_version.status = InputVersionStatus.SEALED
    db.flush()

    # 年度汇总切换到新封账口径（commit=False，随保存点一起提交或回滚）
    update_annual_summary_with_transactions(db, enterprise_id, decision.year, commit=False)

    return {
        "enterprise_id": enterprise_id,
        "enterprise_name": enterprise.name,
        "new_closure_id": new_closure.id,
        "new_closure_no": new_closure.closure_no,
        "previous_closure_id": old_closure.id,
        "previous_closure_no": old_closure.closure_no,
        "input_version_id": input_version.id,
        "input_version_no": input_version.version_no,
        "limit_standard_changed": standard_changed,
        "totals_delta": diff["totals"]["delta"],
    }


def execute_reopen_decision(db: Session, decision_id: int) -> Optional[Dict]:
    """
    执行重开决定：对受影响企业逐一基于指定版本重算并生成新封账。

    - 幂等：同一决定重复执行直接返回首次执行结果，不再生成新版本；
    - 失败隔离：单个企业重算失败仅回滚该企业，保持其原封账结果，
      其余企业继续执行，失败原因记录在执行结果中。
    """
    decision = get_reopen_decision(db, decision_id)
    if not decision:
        return None

    if decision.status == ReopenDecisionStatus.EXECUTED:
        return json.loads(decision.execution_result)

    enterprise_ids = json.loads(decision.enterprise_ids)
    version_map = {int(k): v for k, v in json.loads(decision.input_version_map or "{}").items()}

    closures_created = []
    failures = []

    for enterprise_id in enterprise_ids:
        try:
            # 保存点：单企业失败仅回滚本企业的变更，保持其原封账结果
            with db.begin_nested():
                outcome = _recalculate_closure_for_enterprise(
                    db, decision, enterprise_id, version_map
                )
            closures_created.append(outcome)
        except Exception as exc:  # 保存点已回滚，其余企业不受影响
            failures.append({
                "enterprise_id": enterprise_id,
                "error": str(exc),
                "action": "保持原封账结果",
            })

    decision.status = ReopenDecisionStatus.EXECUTED
    decision.executed_at = datetime.utcnow()
    result = {
        "decision_id": decision.id,
        "decision_no": decision.decision_no,
        "year": decision.year,
        "success_count": len(closures_created),
        "failure_count": len(failures),
        "closures_created": closures_created,
        "failures": failures,
    }
    decision.execution_result = json.dumps(result, ensure_ascii=False)
    db.commit()
    return result

