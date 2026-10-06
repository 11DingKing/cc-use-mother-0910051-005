import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean, Text, UniqueConstraint
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


class InputVersionStatus(str, enum.Enum):
    SUBMITTED = "submitted"      # 已提交，等待监管确认
    SEALED = "sealed"            # 已被某次封账采用
    SUPERSEDED = "superseded"    # 已被同一企业同一年度更新的提交取代


class ClosureStatus(str, enum.Enum):
    SEALED = "sealed"            # 当前有效封账
    SUPERSEDED = "superseded"    # 已被重开后的新封账取代（历史保留，不可覆盖）


class ReopenDecisionStatus(str, enum.Enum):
    PENDING = "pending"          # 已登记，待执行
    EXECUTED = "executed"        # 已执行（重复执行不再生成新版本）


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
    source_closure_id = Column(Integer, ForeignKey("accounting_closures.id"), comment="结转依据的封账ID")
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
    closure_id = Column(Integer, ForeignKey("accounting_closures.id"), comment="本汇总采用的封账ID")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True},
    )


class AccountingInputVersion(Base):
    """核算输入版本：企业每次提交产量/能耗凭证形成的完整、可比较快照"""
    __tablename__ = "accounting_input_versions"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, comment="核算年度")
    version_no = Column(Integer, nullable=False, comment="版本号（同一企业同一年度内递增）")
    status = Column(Enum(InputVersionStatus), default=InputVersionStatus.SUBMITTED, nullable=False)
    submitted_by = Column(String(50), nullable=False, comment="提交人")
    note = Column(String(500), comment="提交说明（如：补交产量和能耗凭证）")
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("AccountingInputItem", back_populates="version",
                         cascade="all, delete-orphan", order_by="AccountingInputItem.id")

    __table_args__ = (
        UniqueConstraint("enterprise_id", "year", "version_no", name="uq_input_version_no"),
    )


class AccountingInputItem(Base):
    """核算输入版本明细：单车型产量与能耗凭证数据"""
    __tablename__ = "accounting_input_items"

    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(Integer, ForeignKey("accounting_input_versions.id"), nullable=False, index=True)
    model_code = Column(String(50), nullable=False, comment="车型代码")
    model_name = Column(String(100), nullable=False, comment="车型名称")
    curb_weight = Column(Float, nullable=False, comment="整备质量(kg)")
    power_consumption = Column(Float, nullable=False, comment="百公里电耗(kWh/100km)")
    range_km = Column(Float, nullable=False, comment="续航里程(km)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")

    version = relationship("AccountingInputVersion", back_populates="items")


class AccountingClosure(Base):
    """封账：监管确认后冻结当期核算输入、限值标准和计算结果。

    旧封账永不物理覆盖：重开重算产生新封账，旧封账状态置为 superseded 并完整保留，
    新封账通过 diff_from_previous 记录与上一封账的各积分项差异。
    """
    __tablename__ = "accounting_closures"

    id = Column(Integer, primary_key=True, index=True)
    closure_no = Column(String(50), unique=True, nullable=False, index=True, comment="封账编号")
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, comment="核算年度")
    input_version_id = Column(Integer, ForeignKey("accounting_input_versions.id"), nullable=False,
                              comment="本次封账采用的核算输入版本")
    limit_standard_snapshot = Column(Text, nullable=False,
                                     comment="冻结的限值标准快照(JSON)：档位表与积分系数")
    result_snapshot = Column(Text, nullable=False,
                             comment="冻结的计算结果快照(JSON)：各车型积分项与合计")
    diff_from_previous = Column(Text, comment="与上一封账的各积分项差异(JSON)，首次封账为空")
    previous_closure_id = Column(Integer, ForeignKey("accounting_closures.id"),
                                 comment="被取代的上一封账ID")
    reopen_decision_id = Column(Integer, ForeignKey("reopen_decisions.id"),
                                comment="产生本封账的重开决定ID（首次封账为空）")
    status = Column(Enum(ClosureStatus), default=ClosureStatus.SEALED, nullable=False)
    sealed_by = Column(String(50), nullable=False, comment="监管确认人")
    sealed_at = Column(DateTime, default=datetime.utcnow, comment="封账时间")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    input_version = relationship("AccountingInputVersion")


class ReopenDecision(Base):
    """重开决定：确需重开已封账年度时登记理由、权限与受影响企业。

    执行具有幂等性：同一决定重复执行不会生成新的封账版本，
    直接返回首次执行的结果。
    """
    __tablename__ = "reopen_decisions"

    id = Column(Integer, primary_key=True, index=True)
    decision_no = Column(String(50), unique=True, nullable=False, index=True, comment="决定编号（幂等键）")
    year = Column(Integer, nullable=False, comment="重开的核算年度")
    enterprise_ids = Column(Text, nullable=False, comment="受影响企业ID列表(JSON)")
    input_version_map = Column(Text, comment="各企业指定重算的输入版本 {企业ID: 版本ID}(JSON)，缺省用最新提交")
    reason = Column(String(500), nullable=False, comment="重开理由")
    approved_by = Column(String(50), nullable=False, comment="审批人（权限记录）")
    status = Column(Enum(ReopenDecisionStatus), default=ReopenDecisionStatus.PENDING, nullable=False)
    execution_result = Column(Text, comment="执行结果(JSON)：成功封账与失败企业明细")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    executed_at = Column(DateTime)
