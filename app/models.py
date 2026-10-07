import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean, Text
from sqlalchemy.orm import relationship

from .database import Base


class CreditRecordStatus(str, enum.Enum):
    CALCULATED = "calculated"
    PUBLICIZED = "publicized"
    CONFIRMED = "confirmed"


class OrderType(str, enum.Enum):
    SELL = "sell"
    BUY = "buy"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"


class CarryoverStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class InputVersionSource(str, enum.Enum):
    ENTERPRISE_SUBMISSION = "enterprise_submission"
    SEAL_SNAPSHOT = "seal_snapshot"


class SealStatus(str, enum.Enum):
    SEALED = "sealed"
    REOPENED = "reopened"
    RESEALED = "resealed"


class ReopenStatus(str, enum.Enum):
    APPROVED = "approved"
    EXECUTED = "executed"


class Enterprise(Base):
    __tablename__ = "enterprises"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    short_name = Column(String(50), unique=True, index=True)
    credit_code = Column(String(50), unique=True, index=True)
    address = Column(String(200))
    contact_person = Column(String(50))
    contact_phone = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicle_models = relationship("VehicleModel", back_populates="enterprise")
    credit_transactions_from = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.from_enterprise_id",
        back_populates="from_enterprise"
    )
    credit_transactions_to = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.to_enterprise_id",
        back_populates="to_enterprise"
    )
    credit_orders = relationship(
        "CreditOrder",
        foreign_keys="CreditOrder.enterprise_id",
        back_populates="enterprise"
    )
    credit_carryovers_from = relationship(
        "CreditCarryover",
        foreign_keys="CreditCarryover.enterprise_id",
        back_populates="enterprise"
    )


class VehicleModel(Base):
    __tablename__ = "vehicle_models"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    model_name = Column(String(100), nullable=False, index=True)
    model_code = Column(String(50), unique=True, nullable=False, index=True)
    curb_weight = Column(Float, nullable=False, comment="整备质量(kg)")
    power_consumption = Column(Float, nullable=False, comment="百公里电耗(kWh/100km)")
    range = Column(Float, nullable=False, comment="续航里程(km)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")
    production_year = Column(Integer, nullable=False, comment="生产年份")
    is_suspected_weight_manipulation = Column(Boolean, default=False, comment="是否疑似堆重量放宽限值")
    based_on_input_version_id = Column(
        Integer, ForeignKey("input_versions.id", use_alter=True),
        nullable=True, comment="当前车型数据所依据的最新核算输入版本"
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enterprise = relationship("Enterprise", back_populates="vehicle_models")
    credit_records = relationship("CreditRecord", back_populates="vehicle_model")


class CreditRecord(Base):
    __tablename__ = "credit_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="核算年份")
    power_consumption_limit = Column(Float, nullable=False, comment="电耗限值(kWh/100km)")
    actual_power_consumption = Column(Float, nullable=False, comment="实际电耗(kWh/100km)")
    unit_credit = Column(Float, nullable=False, comment="单车积分(分/辆)")
    total_credit = Column(Float, nullable=False, comment="总积分(分)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")
    status = Column(Enum(CreditRecordStatus), default=CreditRecordStatus.CALCULATED, nullable=False)
    seal_id = Column(Integer, ForeignKey("accounting_seals.id", use_alter=True), nullable=True, comment="冻结该结果的封账ID")
    input_version_id = Column(Integer, ForeignKey("input_versions.id", use_alter=True), nullable=True, comment="结果所依据的输入版本")
    calculated_at = Column(DateTime, default=datetime.utcnow)
    publicized_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicle_model = relationship("VehicleModel", back_populates="credit_records")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id = Column(Integer, primary_key=True, index=True)
    transaction_no = Column(String(50), unique=True, nullable=False, index=True)
    from_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    to_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    sell_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="卖单ID")
    buy_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="买单ID")
    credit_amount = Column(Float, nullable=False, comment="交易积分数量")
    unit_price = Column(Float, comment="交易单价(元/分)")
    total_amount = Column(Float, comment="交易总额(元)")
    transaction_date = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="completed", comment="交易状态")
    seal_id = Column(Integer, ForeignKey("accounting_seals.id", use_alter=True), nullable=True, comment="交易依据的封账ID")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    from_enterprise = relationship(
        "Enterprise",
        foreign_keys=[from_enterprise_id],
        back_populates="credit_transactions_from"
    )
    to_enterprise = relationship(
        "Enterprise",
        foreign_keys=[to_enterprise_id],
        back_populates="credit_transactions_to"
    )
    sell_order = relationship(
        "CreditOrder",
        foreign_keys=[sell_order_id],
        back_populates="sell_transactions"
    )
    buy_order = relationship(
        "CreditOrder",
        foreign_keys=[buy_order_id],
        back_populates="buy_transactions"
    )


class CreditOrder(Base):
    __tablename__ = "credit_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="核算年度")
    order_type = Column(Enum(OrderType), nullable=False, comment="订单类型：sell/buy")
    unit_price = Column(Float, nullable=False, comment="报价单价(元/分)")
    total_amount = Column(Float, nullable=False, comment="挂单总积分数量")
    filled_amount = Column(Float, default=0.0, comment="已成交积分数量")
    remaining_amount = Column(Float, nullable=False, comment="剩余积分数量")
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False, comment="订单状态")
    seal_id = Column(Integer, ForeignKey("accounting_seals.id", use_alter=True), nullable=True, comment="可交易额度依据的封账ID")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, comment="过期时间")

    enterprise = relationship("Enterprise", back_populates="credit_orders")
    sell_transactions = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.sell_order_id",
        back_populates="sell_order"
    )
    buy_transactions = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.buy_order_id",
        back_populates="buy_order"
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, nullable=False, comment="年度")
    trade_date = Column(DateTime, default=datetime.utcnow, comment="交易日期")
    unit_price = Column(Float, nullable=False, comment="成交单价(元/分)")
    credit_amount = Column(Float, nullable=False, comment="成交积分数量")
    total_amount = Column(Float, nullable=False, comment="成交总额(元)")
    from_enterprise_id = Column(Integer, ForeignKey("enterprises.id"))
    to_enterprise_id = Column(Integer, ForeignKey("enterprises.id"))
    transaction_id = Column(Integer, ForeignKey("credit_transactions.id"))
    created_at = Column(DateTime, default=datetime.utcnow)


class CreditCarryover(Base):
    __tablename__ = "credit_carryovers"

    id = Column(Integer, primary_key=True, index=True)
    carryover_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    from_year = Column(Integer, nullable=False, comment="结转来源年度")
    to_year = Column(Integer, nullable=False, comment="结转目标年度")
    original_amount = Column(Float, nullable=False, comment="原始正积分结余")
    carryover_ratio = Column(Float, nullable=False, comment="结转比例")
    carryover_amount = Column(Float, nullable=False, comment="实际结转积分数量")
    used_amount = Column(Float, default=0.0, comment="已使用结转积分数量")
    remaining_amount = Column(Float, nullable=False, comment="剩余结转积分数量")
    status = Column(Enum(CarryoverStatus), default=CarryoverStatus.APPROVED, nullable=False, comment="结转状态")
    seal_id = Column(Integer, ForeignKey("accounting_seals.id", use_alter=True), nullable=True, comment="结转依据的封账ID")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime)

    enterprise = relationship("Enterprise", back_populates="credit_carryovers_from")


class AnnualCreditSummary(Base):
    __tablename__ = "annual_credit_summaries"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="年度")
    total_positive_credit = Column(Float, default=0.0, comment="当年正积分")
    total_negative_credit = Column(Float, default=0.0, comment="当年负积分")
    net_credit = Column(Float, default=0.0, comment="当年净积分")
    carryover_in = Column(Float, default=0.0, comment="上年结转积分")
    carryover_out = Column(Float, default=0.0, comment="结转下年积分")
    bought_credit = Column(Float, default=0.0, comment="买入积分")
    sold_credit = Column(Float, default=0.0, comment="卖出积分")
    final_net_credit = Column(Float, default=0.0, comment="最终净积分")
    credit_gap = Column(Float, default=0.0, comment="最终积分缺口")
    credit_surplus = Column(Float, default=0.0, comment="最终积分钟余")
    is_compliant = Column(Boolean, default=True, comment="是否达标")
    seal_id = Column(Integer, ForeignKey("accounting_seals.id", use_alter=True), nullable=True, index=True, comment="本汇总内容所采用的封账ID")
    input_version_id = Column(Integer, ForeignKey("input_versions.id", use_alter=True), nullable=True, comment="本汇总所采用的输入版本")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True},
    )


class InputVersion(Base):
    """核算输入版本：企业每次提交形成一个完整、不可变、可比较的输入版本"""
    __tablename__ = "input_versions"

    id = Column(Integer, primary_key=True, index=True)
    version_no = Column(String(64), unique=True, nullable=False, index=True, comment="版本编号，如IV-2025-E001-0003")
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True, comment="核算年度")
    version_seq = Column(Integer, nullable=False, comment="企业年度内提交序号，从1开始")
    source = Column(Enum(InputVersionSource), default=InputVersionSource.ENTERPRISE_SUBMISSION, nullable=False)
    submitter = Column(String(100), nullable=False, comment="提交人/企业经办人")
    evidence_doc_no = Column(String(100), comment="补交凭证编号（产量/能耗凭证）")
    remark = Column(Text)
    content_hash = Column(String(64), nullable=False, comment="版本内容SHA256，同内容重复提交不产生新版本")
    is_sealed = Column(Boolean, default=False, nullable=False, comment="是否已被封账冻结")
    created_at = Column(DateTime, default=datetime.utcnow)

    enterprise = relationship("Enterprise")
    items = relationship(
        "InputVersionItem", back_populates="input_version",
        cascade="all, delete-orphan", order_by="InputVersionItem.id"
    )
    limit_snapshot = relationship(
        "LimitStandardSnapshot", back_populates="input_version",
        cascade="all, delete-orphan", uselist=False
    )


class InputVersionItem(Base):
    """输入版本明细：提交时点每个车型的产量、能耗、整备质量等输入快照"""
    __tablename__ = "input_version_items"

    id = Column(Integer, primary_key=True, index=True)
    input_version_id = Column(Integer, ForeignKey("input_versions.id"), nullable=False, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False, index=True)
    model_code = Column(String(50), nullable=False, comment="车型代码快照")
    model_name = Column(String(100), nullable=False, comment="车型名称快照")
    curb_weight = Column(Float, nullable=False, comment="整备质量(kg)")
    power_consumption = Column(Float, nullable=False, comment="百公里电耗(kWh/100km)")
    range_km = Column(Float, nullable=False, comment="续航里程(km)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")
    evidence_no = Column(String(100), comment="该车型凭证编号")

    input_version = relationship("InputVersion", back_populates="items")
    vehicle_model = relationship("VehicleModel")


class LimitStandardSnapshot(Base):
    """限值标准快照：提交/封账时点生效的电耗限值档位与积分倍率"""
    __tablename__ = "limit_standard_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    input_version_id = Column(Integer, ForeignKey("input_versions.id"), unique=True, nullable=False, index=True)
    tiers_json = Column(Text, nullable=False, comment="限值档位JSON快照")
    credit_multiplier = Column(Float, nullable=False, comment="积分倍率快照")
    rule_version = Column(String(50), nullable=False, comment="限值标准版本标识")
    created_at = Column(DateTime, default=datetime.utcnow)

    input_version = relationship("InputVersion", back_populates="limit_snapshot")


class AccountingSeal(Base):
    """封账：监管确认后冻结当期输入版本、限值标准与计算结果"""
    __tablename__ = "accounting_seals"

    id = Column(Integer, primary_key=True, index=True)
    seal_no = Column(String(64), unique=True, nullable=False, index=True, comment="封账编号")
    year = Column(Integer, nullable=False, index=True, comment="封账年度")
    seal_seq = Column(Integer, nullable=False, comment="年度内封账序号，首次为1，重开重封为2…")
    status = Column(Enum(SealStatus), default=SealStatus.SEALED, nullable=False)
    input_version_id = Column(Integer, ForeignKey("input_versions.id"), nullable=False, comment="封账采用的企业输入版本（多企业时记录基准，逐企业见汇总）")
    confirmed_by = Column(String(100), nullable=False, comment="封账确认监管人员")
    sealed_at = Column(DateTime, default=datetime.utcnow, comment="封账时间")
    remark = Column(Text)
    superseded_by_seal_id = Column(Integer, ForeignKey("accounting_seals.id"), nullable=True, comment="重开后由哪次新封账替代")
    reopen_decision_id = Column(Integer, ForeignKey("reopen_decisions.id", use_alter=True), nullable=True, comment="若为重开封账，对应重开决定")
    created_at = Column(DateTime, default=datetime.utcnow)

    input_version = relationship("InputVersion")
    result_items = relationship(
        "SealResultItem", back_populates="seal", cascade="all, delete-orphan"
    )
    enterprise_summaries = relationship(
        "SealEnterpriseSummary", back_populates="seal", cascade="all, delete-orphan"
    )


class SealResultItem(Base):
    """封账冻结的逐车型计算结果（限值、单车积分、总积分）"""
    __tablename__ = "seal_result_items"

    id = Column(Integer, primary_key=True, index=True)
    seal_id = Column(Integer, ForeignKey("accounting_seals.id"), nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    input_version_id = Column(Integer, ForeignKey("input_versions.id"), nullable=False, comment="该车型结果采用的输入版本")
    year = Column(Integer, nullable=False)
    power_consumption_limit = Column(Float, nullable=False)
    actual_power_consumption = Column(Float, nullable=False)
    unit_credit = Column(Float, nullable=False)
    annual_output = Column(Integer, nullable=False)
    total_credit = Column(Float, nullable=False)

    seal = relationship("AccountingSeal", back_populates="result_items")


class SealEnterpriseSummary(Base):
    """封账冻结的企业级积分汇总，并记录该企业采用的具体输入版本"""
    __tablename__ = "seal_enterprise_summaries"

    id = Column(Integer, primary_key=True, index=True)
    seal_id = Column(Integer, ForeignKey("accounting_seals.id"), nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    input_version_id = Column(Integer, ForeignKey("input_versions.id"), nullable=False, comment="该企业封账采用的输入版本")
    year = Column(Integer, nullable=False)
    total_positive_credit = Column(Float, default=0.0)
    total_negative_credit = Column(Float, default=0.0)
    net_credit = Column(Float, default=0.0)
    credit_gap = Column(Float, default=0.0)
    credit_surplus = Column(Float, default=0.0)
    model_count = Column(Integer, default=0)

    seal = relationship("AccountingSeal", back_populates="enterprise_summaries")


class ReopenDecision(Base):
    """重开决定：记录理由、权限、受影响企业；同一决定幂等，重复执行不再生成新版本"""
    __tablename__ = "reopen_decisions"

    id = Column(Integer, primary_key=True, index=True)
    decision_no = Column(String(64), unique=True, nullable=False, index=True, comment="重开决定单号（幂等键）")
    year = Column(Integer, nullable=False, index=True)
    base_seal_id = Column(Integer, ForeignKey("accounting_seals.id"), nullable=False, comment="被重开的原封账")
    reason = Column(Text, nullable=False, comment="重开理由")
    requested_by = Column(String(100), nullable=False, comment="申请人")
    approved_by = Column(String(100), nullable=False, comment="批准人（监管权限）")
    authority_role = Column(String(100), nullable=False, comment="批准权限/角色")
    input_version_id = Column(Integer, ForeignKey("input_versions.id"), nullable=False, comment="重算指定基于的输入版本")
    status = Column(Enum(ReopenStatus), default=ReopenStatus.APPROVED, nullable=False)
    executed_at = Column(DateTime, comment="执行重算时间")
    new_seal_id = Column(Integer, ForeignKey("accounting_seals.id"), nullable=True, comment="重算后生成的新封账")
    created_at = Column(DateTime, default=datetime.utcnow)

    affected_enterprises = relationship(
        "ReopenAffectedEnterprise", back_populates="decision", cascade="all, delete-orphan"
    )
    differences = relationship(
        "RecalcDifferenceItem", back_populates="decision", cascade="all, delete-orphan"
    )


class ReopenAffectedEnterprise(Base):
    """重开决定覆盖的受影响企业清单"""
    __tablename__ = "reopen_affected_enterprises"

    id = Column(Integer, primary_key=True, index=True)
    decision_id = Column(Integer, ForeignKey("reopen_decisions.id"), nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    recalc_status = Column(String(20), default="pending", comment="pending/success/failed")
    fail_reason = Column(Text, comment="重算失败原因；失败时保留原封账结果")

    decision = relationship("ReopenDecision", back_populates="affected_enterprises")


class RecalcDifferenceItem(Base):
    """重算差异：各积分项新值/旧值/差额，逐车型与企业汇总两层"""
    __tablename__ = "recalc_difference_items"

    id = Column(Integer, primary_key=True, index=True)
    decision_id = Column(Integer, ForeignKey("reopen_decisions.id"), nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=True, comment="为空表示企业汇总层差异")
    item_name = Column(String(50), nullable=False, comment="积分项：unit_credit/total_credit/net_credit等")
    old_value = Column(Float, default=0.0, comment="原封账值")
    new_value = Column(Float, default=0.0, comment="重算新封账值")
    delta = Column(Float, default=0.0, comment="差额 new-old")

    decision = relationship("ReopenDecision", back_populates="differences")
