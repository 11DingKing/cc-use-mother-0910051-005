from typing import List, Optional, Dict
from datetime import datetime
from pydantic import BaseModel, Field

from .models import CreditRecordStatus, OrderType, OrderStatus, CarryoverStatus


class EnterpriseBase(BaseModel):
    name: str = Field(..., max_length=100, description="企业名称")
    short_name: Optional[str] = Field(None, max_length=50, description="企业简称")
    credit_code: Optional[str] = Field(None, max_length=50, description="统一社会信用代码")
    address: Optional[str] = Field(None, max_length=200, description="企业地址")
    contact_person: Optional[str] = Field(None, max_length=50, description="联系人")
    contact_phone: Optional[str] = Field(None, max_length=50, description="联系电话")


class EnterpriseCreate(EnterpriseBase):
    pass


class EnterpriseUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    short_name: Optional[str] = Field(None, max_length=50)
    credit_code: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=200)
    contact_person: Optional[str] = Field(None, max_length=50)
    contact_phone: Optional[str] = Field(None, max_length=50)


class Enterprise(EnterpriseBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EnterpriseWithStats(Enterprise):
    model_config = {"protected_namespaces": (), "from_attributes": True}

    model_count: int = 0
    total_annual_output: int = 0
    total_credit: float = 0.0


class VehicleModelBase(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int = Field(..., description="所属企业ID")
    model_name: str = Field(..., max_length=100, description="车型名称")
    model_code: str = Field(..., max_length=50, description="车型代码")
    curb_weight: float = Field(..., gt=0, description="整备质量(kg)")
    power_consumption: float = Field(..., gt=0, description="百公里电耗(kWh/100km)")
    range: float = Field(..., gt=0, description="续航里程(km)")
    annual_output: int = Field(..., ge=0, description="年产量(辆)")
    production_year: int = Field(..., description="生产年份")


class VehicleModelCreate(VehicleModelBase):
    pass


class VehicleModelUpdate(BaseModel):
    enterprise_id: Optional[int] = None
    model_name: Optional[str] = Field(None, max_length=100)
    model_code: Optional[str] = Field(None, max_length=50)
    curb_weight: Optional[float] = Field(None, gt=0)
    power_consumption: Optional[float] = Field(None, gt=0)
    range: Optional[float] = Field(None, gt=0)
    annual_output: Optional[int] = Field(None, ge=0)
    production_year: Optional[int] = None
    is_suspected_weight_manipulation: Optional[bool] = None


class VehicleModel(VehicleModelBase):
    id: int
    is_suspected_weight_manipulation: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VehicleModelWithEnterprise(VehicleModel):
    enterprise: Enterprise
    power_consumption_limit: Optional[float] = None
    unit_credit: Optional[float] = None

    class Config:
        from_attributes = True


class CreditRecordBase(BaseModel):
    vehicle_model_id: int
    year: int
    power_consumption_limit: float
    actual_power_consumption: float
    unit_credit: float
    total_credit: float
    annual_output: int


class CreditRecordCreate(CreditRecordBase):
    pass


class CreditRecord(CreditRecordBase):
    id: int
    status: CreditRecordStatus
    calculated_at: Optional[datetime] = None
    publicized_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CreditRecordWithDetail(CreditRecord):
    vehicle_model: VehicleModel
    enterprise: Optional[Enterprise] = None

    class Config:
        from_attributes = True


class CreditRecordStatusUpdate(BaseModel):
    status: CreditRecordStatus


class CreditRecordUpdate(BaseModel):
    power_consumption_limit: Optional[float] = None
    actual_power_consumption: Optional[float] = None
    unit_credit: Optional[float] = None
    total_credit: Optional[float] = None
    annual_output: Optional[int] = None


class CreditTransactionBase(BaseModel):
    from_enterprise_id: int
    to_enterprise_id: int
    credit_amount: float
    unit_price: Optional[float] = None
    total_amount: Optional[float] = None
    remark: Optional[str] = None


class CreditTransactionCreate(CreditTransactionBase):
    pass


class CreditTransaction(CreditTransactionBase):
    id: int
    transaction_no: str
    transaction_date: datetime
    status: str
    seal_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class CreditTransactionWithDetail(CreditTransaction):
    from_enterprise: Enterprise
    to_enterprise: Enterprise

    class Config:
        from_attributes = True


class CalculationResult(BaseModel):
    model_config = {"protected_namespaces": ()}

    vehicle_model_id: int
    model_name: str
    curb_weight: float
    power_consumption_limit: float
    actual_power_consumption: float
    unit_credit: float
    annual_output: int
    total_credit: float
    is_compliant: bool


class EnterpriseCreditSummaryResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    total_positive_credit: float
    total_negative_credit: float
    net_credit: float
    required_credit: float
    credit_gap: float
    credit_surplus: float
    compliance_rate: float
    average_power_consumption: float
    weighted_power_consumption: float
    model_count: int
    compliant_model_count: int


class MatchResultResponse(BaseModel):
    from_enterprise_id: int
    from_enterprise_name: str
    to_enterprise_id: int
    to_enterprise_name: str
    credit_amount: float
    unit_price: float
    total_amount: float


class MatchAndExecuteResponse(BaseModel):
    success: bool
    message: str
    transactions: List[CreditTransactionWithDetail] = []
    remaining_gap: float = 0.0
    remaining_surplus: float = 0.0


class WeightSuggestionResponse(BaseModel):
    current_weight: float
    current_limit: float
    is_suspicious: bool
    suggestions: List[dict]


class EnterpriseStatsResponse(BaseModel):
    enterprise_id: int
    enterprise_name: str
    model_count: int
    total_output: int
    average_power_consumption: float
    weighted_power_consumption: float
    average_power_consumption_limit: float
    compliance_rate: float
    total_positive_credit: float
    total_negative_credit: float
    net_credit: float


class SuspiciousModelResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    model_name: str
    model_code: str
    enterprise_name: str
    curb_weight: float
    power_consumption: float
    range: float
    power_consumption_limit: float
    annual_output: int
    weight_analysis: dict


class CreditOrderBase(BaseModel):
    enterprise_id: int
    year: int
    order_type: OrderType
    unit_price: float = Field(..., gt=0, description="报价单价(元/分)")
    total_amount: float = Field(..., gt=0, description="挂单总积分数量")
    remark: Optional[str] = Field(None, max_length=500)
    expires_at: Optional[datetime] = None


class CreditOrderCreate(CreditOrderBase):
    pass


class CreditOrderUpdate(BaseModel):
    unit_price: Optional[float] = Field(None, gt=0)
    total_amount: Optional[float] = Field(None, gt=0)
    status: Optional[OrderStatus] = None
    remark: Optional[str] = None


class CreditOrder(CreditOrderBase):
    id: int
    order_no: str
    filled_amount: float
    remaining_amount: float
    status: OrderStatus
    seal_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CreditOrderWithDetail(CreditOrder):
    enterprise: Enterprise
    sell_transactions: List["CreditTransaction"] = []
    buy_transactions: List["CreditTransaction"] = []

    class Config:
        from_attributes = True


class CreditOrderMatchRequest(BaseModel):
    buy_order_id: int
    sell_order_id: int
    credit_amount: float


class MatchWithOrdersResponse(BaseModel):
    success: bool
    message: str
    transactions: List[CreditTransactionWithDetail] = []
    matched_orders: List[dict] = []
    remaining_gap: float = 0.0
    remaining_surplus: float = 0.0


class PriceHistoryBase(BaseModel):
    year: int
    trade_date: datetime
    unit_price: float
    credit_amount: float
    total_amount: float
    from_enterprise_id: Optional[int] = None
    to_enterprise_id: Optional[int] = None
    transaction_id: Optional[int] = None


class PriceHistoryCreate(PriceHistoryBase):
    pass


class PriceHistory(PriceHistoryBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class PriceTrendResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    year: int
    avg_price: float
    min_price: float
    max_price: float
    total_volume: float
    total_value: float
    trade_count: int
    price_by_date: List[dict] = []


class CreditCarryoverBase(BaseModel):
    enterprise_id: int
    from_year: int
    to_year: int
    original_amount: float
    carryover_ratio: float
    carryover_amount: float
    remark: Optional[str] = None


class CreditCarryoverCreate(CreditCarryoverBase):
    pass


class CreditCarryoverUpdate(BaseModel):
    status: Optional[CarryoverStatus] = None
    remark: Optional[str] = None


class CreditCarryover(CreditCarryoverBase):
    id: int
    carryover_no: str
    used_amount: float
    remaining_amount: float
    status: CarryoverStatus
    seal_id: Optional[int] = None
    created_at: datetime
    approved_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CreditCarryoverWithDetail(CreditCarryover):
    enterprise: Enterprise

    class Config:
        from_attributes = True


class AnnualCreditSummaryBase(BaseModel):
    enterprise_id: int
    year: int
    total_positive_credit: float = 0.0
    total_negative_credit: float = 0.0
    net_credit: float = 0.0
    carryover_in: float = 0.0
    carryover_out: float = 0.0
    bought_credit: float = 0.0
    sold_credit: float = 0.0
    final_net_credit: float = 0.0
    credit_gap: float = 0.0
    credit_surplus: float = 0.0
    is_compliant: bool = True
    seal_id: Optional[int] = None
    input_version_id: Optional[int] = None


class AnnualCreditSummaryCreate(AnnualCreditSummaryBase):
    pass


class AnnualCreditSummary(AnnualCreditSummaryBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AnnualCreditSummaryWithDetail(AnnualCreditSummary):
    enterprise: Enterprise
    carryovers: List[CreditCarryover] = []
    transactions: List[CreditTransactionWithDetail] = []

    class Config:
        from_attributes = True


class EnterpriseMultiYearSummaryResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    years: List[int] = []
    annual_summaries: List[dict] = []
    total_carryover_in: float = 0.0
    total_carryover_out: float = 0.0
    total_bought: float = 0.0
    total_sold: float = 0.0


class CreditPredictionRequest(BaseModel):
    enterprise_id: Optional[int] = None
    target_year: int
    output_growth_rate: Optional[float] = Field(0.05, description="产量年增长率")
    pc_improvement_rate: Optional[float] = Field(0.02, description="电耗年改善率")


class ModelPrediction(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_name: str
    model_code: str
    curb_weight: float
    current_power_consumption: float
    predicted_power_consumption: float
    power_consumption_limit: float
    predicted_output: int
    predicted_unit_credit: float
    predicted_total_credit: float


class CreditPredictionResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    target_year: int
    historical_years: List[int] = []
    historical_credits: List[dict] = []
    predicted_total_positive: float
    predicted_total_negative: float
    predicted_net_credit: float
    predicted_compliance_rate: float
    model_predictions: List[ModelPrediction] = []
    prediction_method: str
    assumptions: dict


class MarketOverviewResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    year: int
    total_sell_orders: int
    total_buy_orders: int
    total_sell_volume: float
    total_buy_volume: float
    avg_sell_price: float
    avg_buy_price: float
    min_sell_price: float
    max_sell_price: float
    min_buy_price: float
    max_buy_price: float
    pending_sell_volume: float
    pending_buy_volume: float
    matched_count: int
    matched_volume: float
    matched_value: float


class CarryoverSummaryResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    enterprise_id: int
    enterprise_name: str
    from_year: int
    to_year: int
    original_surplus: float
    carryover_ratio: float
    carryover_amount: float
    used_amount: float
    remaining_amount: float
    status: str


CreditOrderWithDetail.model_rebuild()


# ---------------------------------------------------------------------------
# 核算输入版本、封账、重开重算
# ---------------------------------------------------------------------------

class InputVersionItemSubmit(BaseModel):
    model_code: str = Field(..., max_length=50, description="车型代码（须为本企业该年度车型）")
    curb_weight: Optional[float] = Field(None, gt=0, description="补交整备质量(kg)")
    power_consumption: Optional[float] = Field(None, gt=0, description="补交百公里电耗")
    range_km: Optional[float] = Field(None, gt=0, alias="range", description="补交续航里程(km)")
    annual_output: Optional[int] = Field(None, ge=0, description="补交年产量(辆)")
    evidence_no: Optional[str] = Field(None, max_length=100, description="凭证编号")

    model_config = {"populate_by_name": True}


class InputVersionSubmit(BaseModel):
    enterprise_id: int
    year: int
    submitter: str = Field(..., max_length=100, description="提交人/企业经办人")
    evidence_doc_no: Optional[str] = Field(None, max_length=100, description="本次补交凭证批次号")
    remark: Optional[str] = None
    items: Optional[List[InputVersionItemSubmit]] = Field(
        None, description="变动车型覆盖项；未列出车型以主数据计入，版本始终完整"
    )


class InputVersionItemOut(BaseModel):
    id: int
    vehicle_model_id: int
    model_code: str
    model_name: str
    curb_weight: float
    power_consumption: float
    range_km: float
    annual_output: int
    evidence_no: Optional[str] = None

    class Config:
        from_attributes = True


class LimitStandardSnapshotOut(BaseModel):
    id: int
    tiers_json: str
    credit_multiplier: float
    rule_version: str

    class Config:
        from_attributes = True


class InputVersionOut(BaseModel):
    id: int
    version_no: str
    enterprise_id: int
    year: int
    version_seq: int
    source: str
    submitter: str
    evidence_doc_no: Optional[str] = None
    remark: Optional[str] = None
    content_hash: str
    is_sealed: bool
    created_at: datetime
    items: List[InputVersionItemOut] = []
    limit_snapshot: Optional[LimitStandardSnapshotOut] = None

    class Config:
        from_attributes = True


class SealResultItemOut(BaseModel):
    id: int
    enterprise_id: int
    vehicle_model_id: int
    input_version_id: int
    year: int
    power_consumption_limit: float
    actual_power_consumption: float
    unit_credit: float
    annual_output: int
    total_credit: float

    class Config:
        from_attributes = True


class SealEnterpriseSummaryOut(BaseModel):
    id: int
    enterprise_id: int
    input_version_id: int
    year: int
    total_positive_credit: float
    total_negative_credit: float
    net_credit: float
    credit_gap: float
    credit_surplus: float
    model_count: int

    class Config:
        from_attributes = True


class SealCreate(BaseModel):
    year: int
    confirmed_by: str = Field(..., max_length=100, description="封账确认监管人员")
    remark: Optional[str] = None


class SealOut(BaseModel):
    id: int
    seal_no: str
    year: int
    seal_seq: int
    status: str
    input_version_id: int
    confirmed_by: str
    sealed_at: datetime
    remark: Optional[str] = None
    superseded_by_seal_id: Optional[int] = None
    reopen_decision_id: Optional[int] = None
    result_items: List[SealResultItemOut] = []
    enterprise_summaries: List[SealEnterpriseSummaryOut] = []

    class Config:
        from_attributes = True


class ReopenCreate(BaseModel):
    decision_no: str = Field(..., max_length=64, description="重开决定单号（幂等键，重复执行不再生成新版本）")
    year: Optional[int] = Field(None, description="封账年度；不传则取当前生效封账")
    base_seal_id: Optional[int] = Field(None, description="被重开的原封账ID")
    reason: str = Field(..., min_length=1, description="重开理由")
    requested_by: str = Field(..., max_length=100, description="申请人")
    approved_by: str = Field(..., max_length=100, description="批准人（监管权限）")
    authority_role: str = Field(..., max_length=100, description="批准权限/角色")
    enterprise_ids: List[int] = Field(..., min_length=1, description="受影响企业")
    input_version_id: Optional[int] = Field(None, description="统一指定重算依据的输入版本")
    version_by_enterprise: Optional[Dict[int, int]] = Field(
        None, description="按企业分别指定输入版本，优先级高于 input_version_id"
    )


class ReopenAffectedEnterpriseOut(BaseModel):
    id: int
    enterprise_id: int
    recalc_status: str
    fail_reason: Optional[str] = None

    class Config:
        from_attributes = True


class RecalcDifferenceItemOut(BaseModel):
    id: int
    enterprise_id: int
    vehicle_model_id: Optional[int] = None
    item_name: str
    old_value: float
    new_value: float
    delta: float

    class Config:
        from_attributes = True


class ReopenOut(BaseModel):
    id: int
    decision_no: str
    year: int
    base_seal_id: int
    reason: str
    requested_by: str
    approved_by: str
    authority_role: str
    input_version_id: int
    status: str
    executed_at: Optional[datetime] = None
    new_seal_id: Optional[int] = None
    created_at: datetime
    affected_enterprises: List[ReopenAffectedEnterpriseOut] = []
    differences: List[RecalcDifferenceItemOut] = []

    class Config:
        from_attributes = True
