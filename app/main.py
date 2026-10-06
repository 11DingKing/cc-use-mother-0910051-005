from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from .config import settings
from .database import Base, engine
from .routers import enterprises, vehicle_models, credit_records, credit_transactions, statistics
from .routers import credit_market, credit_carryover, credit_prediction, accounting

Base.metadata.create_all(bind=engine)


def _ensure_schema_upgrades():
    """为已存在的 SQLite 数据库补充新增列（create_all 不会修改已有表）"""
    new_columns = {
        "annual_credit_summaries": [("closure_id", "INTEGER")],
        "credit_carryovers": [("source_closure_id", "INTEGER")],
    }
    with engine.begin() as conn:
        for table, columns in new_columns.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for col_name, col_type in columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))


_ensure_schema_upgrades()

app = FastAPI(
    title=settings.APP_NAME,
    description="工信部双积分核算与电耗限值管理系统 - 用于管理新能源汽车企业双积分核算、电耗限值标准、积分交易撮合等业务。新增功能：积分交易市场（挂单交易、价格走势）、跨年度结转、积分预测。",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(enterprises.router, prefix=settings.API_V1_PREFIX)
app.include_router(vehicle_models.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_records.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_transactions.router, prefix=settings.API_V1_PREFIX)
app.include_router(statistics.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_market.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_carryover.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_prediction.router, prefix=settings.API_V1_PREFIX)
app.include_router(accounting.router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["root"])
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.VERSION,
        "message": "欢迎使用双积分核算与电耗限值管理系统",
        "docs": "/docs",
        "api_prefix": settings.API_V1_PREFIX
    }


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "healthy"}
