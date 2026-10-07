"""核算输入版本、封账、重开重算机制的集成测试。"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import crud, schemas, sealing
from app.models import (
    AccountingSeal,
    SealStatus,
    InputVersion,
    InputVersionSource,
)
from app.rules import CREDIT_MULTIPLIER


TEST_DATABASE_URL = "sqlite:///:memory:"
YEAR = 2025


@pytest.fixture(scope="function")
def db():
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _make_enterprise(db, name, code):
    return crud.create_enterprise(
        db, schemas.EnterpriseCreate(name=name, short_name=name, credit_code=code)
    )


def _make_model(db, ent_id, code, weight, pc, output, range_km=400.0):
    return crud.create_vehicle_model(
        db,
        schemas.VehicleModelCreate(
            enterprise_id=ent_id,
            model_name=code,
            model_code=code,
            curb_weight=weight,
            power_consumption=pc,
            range=range_km,
            annual_output=output,
            production_year=YEAR,
        ),
    )


# M1: 限值10.5, 电耗8.0 → 单车积分 (10.5-8)/10.5*2.5 = 0.5952
M1_UNIT = round((10.5 - 8.0) / 10.5 * CREDIT_MULTIPLIER, 4)
# M2: 限值13.5, 电耗15.0 → 单车积分 -0.2778
M2_UNIT = round((13.5 - 15.0) / 13.5 * CREDIT_MULTIPLIER, 4)


@pytest.fixture(scope="function")
def two_enterprises(db):
    ent_a = _make_enterprise(db, "车企A", "EA")
    ent_b = _make_enterprise(db, "车企B", "EB")
    m1 = _make_model(db, ent_a.id, "M1", 1000.0, 8.0, 1000)
    m2 = _make_model(db, ent_b.id, "M2", 1500.0, 15.0, 800, range_km=300.0)
    return ent_a, ent_b, m1, m2


class TestInputVersion:
    def test_submit_forms_complete_version_with_limit_snapshot(self, db, two_enterprises):
        ent_a, _, m1, _ = two_enterprises

        version, created = sealing.submit_input_version(
            db, ent_a.id, YEAR, submitter="张三"
        )
        assert created is True
        assert version.version_seq == 1
        assert len(version.items) == 1
        item = version.items[0]
        assert item.annual_output == 1000
        assert item.power_consumption == 8.0
        # 限值标准随版本冻结
        snapshot = version.limit_snapshot
        assert snapshot.credit_multiplier == CREDIT_MULTIPLIER
        assert "10.5" in snapshot.tiers_json
        assert len(version.content_hash) == 64

    def test_duplicate_submission_does_not_create_new_version(self, db, two_enterprises):
        ent_a, _, _, _ = two_enterprises
        v1, created1 = sealing.submit_input_version(db, ent_a.id, YEAR, "张三")
        v2, created2 = sealing.submit_input_version(db, ent_a.id, YEAR, "张三")
        assert created1 is True
        assert created2 is False
        assert v1.id == v2.id

    def test_supplementary_evidence_creates_comparable_new_version(self, db, two_enterprises):
        ent_a, _, m1, _ = two_enterprises
        v1, _ = sealing.submit_input_version(db, ent_a.id, YEAR, "张三")

        # 企业补交产量凭证：产量1000→1200，只给变动车型，版本仍须完整
        v2, created = sealing.submit_input_version(
            db,
            ent_a.id,
            YEAR,
            submitter="张三",
            evidence_doc_no="EVD-2025-009",
            items=[{"model_code": "M1", "annual_output": 1200, "evidence_no": "OUT-1200"}],
        )
        assert created is True
        assert v2.version_seq == 2
        assert v2.content_hash != v1.content_hash
        assert v2.items[0].annual_output == 1200
        assert v2.items[0].evidence_no == "OUT-1200"
        # 车型主数据未被提交动作改写
        db.refresh(m1)
        assert m1.annual_output == 1000

        comparison = sealing.diff_input_versions(db, v1.id, v2.id)
        changed = [c for c in comparison["changes"] if c["model_code"] == "M1"][0]
        assert changed["fields"]["annual_output"]["old"] == 1000
        assert changed["fields"]["annual_output"]["new"] == 1200

        # 重复提交相同补交内容不再产生新版本
        v3, created3 = sealing.submit_input_version(
            db,
            ent_a.id,
            YEAR,
            "张三",
            items=[{"model_code": "M1", "annual_output": 1200, "evidence_no": "OUT-1200"}],
        )
        assert created3 is False
        assert v3.id == v2.id


class TestSealing:
    def test_seal_freezes_versions_standards_and_results(self, db, two_enterprises):
        ent_a, ent_b, _, _ = two_enterprises
        sealing.submit_input_version(db, ent_a.id, YEAR, "张三")
        sealing.submit_input_version(db, ent_b.id, YEAR, "李四")

        seal = sealing.seal_year(db, YEAR, confirmed_by="监管员王五")
        assert seal.seal_no == f"SEAL{YEAR}001"
        assert seal.status == SealStatus.SEALED

        items = {(i.enterprise_id, i.vehicle_model_id): i for i in seal.result_items}
        a_item = [i for i in seal.result_items if i.enterprise_id == ent_a.id][0]
        b_item = [i for i in seal.result_items if i.enterprise_id == ent_b.id][0]
        assert a_item.unit_credit == M1_UNIT
        assert a_item.total_credit == round(M1_UNIT * 1000, 2)
        assert b_item.total_credit == round(M2_UNIT * 800, 2)

        summaries = {s.enterprise_id: s for s in seal.enterprise_summaries}
        assert summaries[ent_a.id].net_credit == round(M1_UNIT * 1000, 2)
        assert summaries[ent_b.id].net_credit == round(M2_UNIT * 800, 2)
        assert summaries[ent_b.id].credit_gap == round(abs(M2_UNIT * 800), 2)

        # 被封账采用的输入版本被标记冻结
        for vid in {s.input_version_id for s in seal.enterprise_summaries}:
            assert db.get(InputVersion, vid).is_sealed is True

        # 年度汇总明确记录采用哪次封账/哪个版本
        sa = crud.get_or_create_annual_summary(db, ent_a.id, YEAR)
        assert sa.seal_id == seal.id
        assert sa.input_version_id == summaries[ent_a.id].input_version_id

    def test_seal_without_submission_auto_snapshots(self, db, two_enterprises):
        ent_a, _, _, _ = two_enterprises
        seal = sealing.seal_year(db, YEAR, confirmed_by="监管员王五")
        version = sealing.get_latest_input_version(db, ent_a.id, YEAR)
        assert version is not None
        assert version.source == InputVersionSource.SEAL_SNAPSHOT
        assert seal.enterprise_summaries[0].input_version_id == version.id

    def test_sealed_period_rejects_direct_rewrite_and_reseal(self, db, two_enterprises):
        ent_a, ent_b, m1, m2 = two_enterprises
        sealing.seal_year(db, YEAR, confirmed_by="监管员王五")

        with pytest.raises(ValueError):
            crud.update_vehicle_model(
                db, m1.id, schemas.VehicleModelUpdate(annual_output=9999)
            )
        with pytest.raises(ValueError):
            crud.create_vehicle_model(
                db,
                schemas.VehicleModelCreate(
                    enterprise_id=ent_a.id, model_name="X", model_code="X1",
                    curb_weight=1000.0, power_consumption=8.0, range=400.0,
                    annual_output=10, production_year=YEAR,
                ),
            )
        with pytest.raises(ValueError):
            crud.create_credit_record(db, m1.id, YEAR)
        with pytest.raises(ValueError):
            sealing.seal_year(db, YEAR, confirmed_by="监管员王五")

    def test_new_submission_after_seal_is_archived_not_applied(self, db, two_enterprises):
        ent_a, ent_b, m1, _ = two_enterprises
        seal1 = sealing.seal_year(db, YEAR, confirmed_by="监管员王五")

        v2, created = sealing.submit_input_version(
            db, ent_a.id, YEAR, "张三",
            items=[{"model_code": "M1", "annual_output": 1200}],
        )
        assert created is True
        # 已公布封账结果不因新提交而变化
        a_summary = [s for s in seal1.enterprise_summaries if s.enterprise_id == ent_a.id][0]
        assert a_summary.net_credit == round(M1_UNIT * 1000, 2)
        # 当前生效封账仍是第一次
        assert sealing.get_active_seal(db, YEAR).id == seal1.id


class TestReopenRecalculate:
    def _sealed_with_supplement(self, db, two_enterprises, new_output=1200):
        ent_a, ent_b, m1, m2 = two_enterprises
        seal1 = sealing.seal_year(db, YEAR, confirmed_by="监管员王五")
        v2, _ = sealing.submit_input_version(
            db, ent_a.id, YEAR, "张三",
            evidence_doc_no="EVD-1",
            items=[{"model_code": "M1", "annual_output": new_output}],
        )
        return ent_a, ent_b, m1, m2, seal1, v2

    def test_reopen_recalculates_on_designated_version_with_diffs(self, db, two_enterprises):
        ent_a, ent_b, m1, m2, seal1, v2 = self._sealed_with_supplement(db, two_enterprises)

        decision, created = sealing.reopen_and_recalculate(
            db,
            decision_no="RO-2025-001",
            reason="企业补交产量凭证，经稽核属实",
            requested_by="企业经办人张三",
            approved_by="监管局长赵六",
            authority_role="省级工信主管部门-审批人",
            enterprise_ids=[ent_a.id],
            base_seal_id=seal1.id,
            input_version_id=v2.id,
        )
        assert created is True
        assert decision.new_seal_id != seal1.id

        seal2 = sealing.get_seal(db, decision.new_seal_id)
        assert seal2.seal_seq == 2
        assert seal2.reopen_decision_id == decision.id

        # 旧封账保留可查，状态为重开，未被悄悄覆盖
        db.refresh(seal1)
        assert seal1.status == SealStatus.REOPENED
        assert seal1.superseded_by_seal_id == seal2.id
        old_a = [s for s in seal1.enterprise_summaries if s.enterprise_id == ent_a.id][0]
        assert old_a.net_credit == round(M1_UNIT * 1000, 2)

        # 新封账采用指定版本重算
        new_a = [s for s in seal2.enterprise_summaries if s.enterprise_id == ent_a.id][0]
        assert new_a.input_version_id == v2.id
        assert new_a.net_credit == round(M1_UNIT * 1200, 2)
        new_b = [s for s in seal2.enterprise_summaries if s.enterprise_id == ent_b.id][0]
        assert new_b.net_credit == round(M2_UNIT * 800, 2)  # 未受影响企业保持

        # 差异：车型层（产量+200、总积分+119.04）与企业汇总层（净积分+119.04）
        model_rows = {
            d.item_name: d for d in decision.differences
            if d.vehicle_model_id == m1.id
        }
        assert model_rows["annual_output"].old_value == 1000
        assert model_rows["annual_output"].new_value == 1200
        assert model_rows["annual_output"].delta == 200
        expected_delta = round(M1_UNIT * 1200 - M1_UNIT * 1000, 4)
        assert model_rows["total_credit"].delta == expected_delta

        summary_rows = {
            d.item_name: d for d in decision.differences if d.vehicle_model_id is None
        }
        assert summary_rows["net_credit"].delta == expected_delta
        assert summary_rows["total_positive_credit"].delta == expected_delta
        assert summary_rows["total_negative_credit"].delta == 0.0

        # 年度汇总明确改采用新封账
        sa = crud.get_or_create_annual_summary(db, ent_a.id, YEAR)
        assert sa.seal_id == seal2.id
        assert sa.input_version_id == v2.id
        assert sa.net_credit == round(M1_UNIT * 1200, 2)

        # 生效封账切换
        assert sealing.get_active_seal(db, YEAR).id == seal2.id
        # 全流程封账链完整可追溯
        assert db.query(AccountingSeal).filter(AccountingSeal.year == YEAR).count() == 2

    def test_reopen_requires_reason_authority_and_enterprises(self, db, two_enterprises):
        ent_a, _, _, _, seal1, v2 = self._sealed_with_supplement(db, two_enterprises)
        with pytest.raises(ValueError):
            sealing.reopen_and_recalculate(
                db, decision_no="RO-X", reason="", requested_by="x",
                approved_by="y", authority_role="role",
                enterprise_ids=[ent_a.id], base_seal_id=seal1.id, input_version_id=v2.id,
            )
        with pytest.raises(ValueError):
            sealing.reopen_and_recalculate(
                db, decision_no="RO-X", reason="合理理由", requested_by="x",
                approved_by="", authority_role="",
                enterprise_ids=[ent_a.id], base_seal_id=seal1.id, input_version_id=v2.id,
            )
        with pytest.raises(ValueError):
            sealing.reopen_and_recalculate(
                db, decision_no="RO-X", reason="合理理由", requested_by="x",
                approved_by="y", authority_role="role",
                enterprise_ids=[], base_seal_id=seal1.id, input_version_id=v2.id,
            )

    def test_same_decision_is_idempotent(self, db, two_enterprises):
        ent_a, _, _, _, seal1, v2 = self._sealed_with_supplement(db, two_enterprises)
        kwargs = dict(
            reason="补交凭证", requested_by="张三", approved_by="赵六",
            authority_role="审批人", enterprise_ids=[ent_a.id],
            base_seal_id=seal1.id, input_version_id=v2.id,
        )
        d1, created1 = sealing.reopen_and_recalculate(db, decision_no="RO-2025-002", **kwargs)
        seal_count_after_first = db.query(AccountingSeal).count()
        version_count_after_first = db.query(InputVersion).count()

        d2, created2 = sealing.reopen_and_recalculate(db, decision_no="RO-2025-002", **kwargs)
        assert created1 is True
        assert created2 is False
        assert d1.id == d2.id
        assert d2.new_seal_id == d1.new_seal_id
        assert db.query(AccountingSeal).count() == seal_count_after_first
        assert db.query(InputVersion).count() == version_count_after_first

    def test_failed_enterprise_keeps_original_sealed_result(self, db, two_enterprises):
        ent_a, ent_b, _, _, seal1, v2 = self._sealed_with_supplement(db, two_enterprises)
        # 先让 A 的重开成功，形成 seal2
        sealing.reopen_and_recalculate(
            db, decision_no="RO-2025-003", reason="补交产量", requested_by="张三",
            approved_by="赵六", authority_role="审批人",
            enterprise_ids=[ent_a.id], base_seal_id=seal1.id, input_version_id=v2.id,
        )
        seal2 = sealing.get_active_seal(db, YEAR)

        # 再重开，B 被指定了 A 的版本（不属于 B）→ 重算失败，必须保留 B 的原封账结果
        decision, created = sealing.reopen_and_recalculate(
            db, decision_no="RO-2025-004", reason="稽核B企业", requested_by="钱七",
            approved_by="赵六", authority_role="审批人",
            enterprise_ids=[ent_b.id], base_seal_id=seal2.id, input_version_id=v2.id,
        )
        assert created is True
        affected = {a.enterprise_id: a for a in decision.affected_enterprises}
        assert affected[ent_b.id].recalc_status == "failed"
        assert affected[ent_b.id].fail_reason
        assert decision.differences == []

        seal3 = sealing.get_seal(db, decision.new_seal_id)
        b3 = [s for s in seal3.enterprise_summaries if s.enterprise_id == ent_b.id][0]
        assert b3.net_credit == round(M2_UNIT * 800, 2)  # 原结果保留
        assert b3.input_version_id == [
            s for s in seal2.enterprise_summaries if s.enterprise_id == ent_b.id
        ][0].input_version_id

        # B 的年度汇总指向新封账但数值不变
        sb = crud.get_or_create_annual_summary(db, ent_b.id, YEAR)
        assert sb.seal_id == seal3.id
        assert sb.net_credit == round(M2_UNIT * 800, 2)
        # A 未列入受影响企业，结果同样保留
        a3 = [s for s in seal3.enterprise_summaries if s.enterprise_id == ent_a.id][0]
        assert a3.net_credit == round(M1_UNIT * 1200, 2)

    def test_cannot_reopen_already_reopened_seal(self, db, two_enterprises):
        ent_a, _, _, _, seal1, v2 = self._sealed_with_supplement(db, two_enterprises)
        sealing.reopen_and_recalculate(
            db, decision_no="RO-OK", reason="理由", requested_by="张三",
            approved_by="赵六", authority_role="审批人",
            enterprise_ids=[ent_a.id], base_seal_id=seal1.id, input_version_id=v2.id,
        )
        with pytest.raises(ValueError):
            sealing.reopen_and_recalculate(
                db, decision_no="RO-DUP-SEAL", reason="理由", requested_by="张三",
                approved_by="赵六", authority_role="审批人",
                enterprise_ids=[ent_a.id], base_seal_id=seal1.id, input_version_id=v2.id,
            )


class TestDownstreamSealReferences:
    def test_orders_carryovers_record_seal_basis(self, db, two_enterprises):
        ent_a, _, _, _ = two_enterprises
        seal = sealing.seal_year(db, YEAR, confirmed_by="监管员王五")

        order = crud.create_credit_order(
            db,
            schemas.CreditOrderCreate(
                enterprise_id=ent_a.id, year=YEAR, order_type="sell",
                unit_price=3000.0, total_amount=10.0,
            ),
        )
        assert order.seal_id == seal.id

        carryover = crud.create_credit_carryover(
            db,
            schemas.CreditCarryoverCreate(
                enterprise_id=ent_a.id, from_year=YEAR, to_year=YEAR + 1,
                original_amount=100.0, carryover_ratio=0.8, carryover_amount=80.0,
            ),
        )
        assert carryover.seal_id == seal.id

    def test_sellable_amount_uses_frozen_seal_summary(self, db, two_enterprises):
        ent_a, _, _, _, seal1, v2 = TestReopenRecalculate()._sealed_with_supplement(
            db, two_enterprises, new_output=1200
        )
        # 封账后、重开前：可售额度按封账净积分（1000 辆口径）
        summary_before = crud.calculate_enterprise_credit_summary_v2(db, ent_a.id, YEAR)
        assert summary_before.final_credit_surplus == round(M1_UNIT * 1000, 2)

        sealing.reopen_and_recalculate(
            db, decision_no="RO-DS-1", reason="补交", requested_by="张三",
            approved_by="赵六", authority_role="审批人",
            enterprise_ids=[ent_a.id], base_seal_id=seal1.id, input_version_id=v2.id,
        )
        # 重开重封后：可售额度改按新封账（1200 辆口径），且明确来自新封账
        summary_after = crud.calculate_enterprise_credit_summary_v2(db, ent_a.id, YEAR)
        assert summary_after.final_credit_surplus == round(M1_UNIT * 1200, 2)
        row = crud.get_or_create_annual_summary(db, ent_a.id, YEAR)
        assert row.seal_id == sealing.get_active_seal(db, YEAR).id
