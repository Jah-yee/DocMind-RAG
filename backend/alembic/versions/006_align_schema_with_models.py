"""Align schema built by migrations 001-005 with the ORM models (issue #150).

Revision ID: 006_align_schema_with_models
Revises: 005_add_missing_tables

用同一份代码建库，「alembic upgrade head」与「Base.metadata.create_all」
产出的 schema 曾有 28 列不一致（含聊天开箱即坏的 1364 错误）。
本迁移把迁移路径对齐到模型声明（模型是唯一事实源），分四类处理：

1. 迁移缺 created_at 默认值（6 列）：模型声明 server_default=func.now()，
   ORM 插入时会省略该列指望数据库给默认值 → 补 DEFAULT。
2. 迁移里多出的 server_default（10 列）：模型只有 Python 侧 default=func.now()，
   不声明 DB 默认值 → 去掉（ORM 插入路径始终自带值，见报告逐列判断）。
3. 可空性不一致（10 列）：对齐到模型；收紧 NOT NULL 前先填充存量 NULL。
4. 枚举口径不一致（2 列）：SQLAlchemy 的 Enum() 默认按枚举**成员名**入库，
   模型/ create_all 两侧都是大写名，而迁移 004 建了小写值、005 建了 varchar(10)。
   agent_tasks.status 与 knowledge_processing_jobs.status 需要带数据转换。

危险点（实测确认）：MySQL 把值写入枚举、或 ALTER 成枚举时，若存量值不在新集合内：
  - 严格模式（MySQL 8 默认）：报 1265，但错误信息不点名具体值；
  - 非严格模式：静默截断成空串，只给一条 warning。
因此本迁移在动任何列之前先对存量值做**显式校验**，遇到无法识别的取值直接
抛 RuntimeError 中止，绝不静默截断；转换完成后再做一次兜底断言。

downgrade 还原 005 的形态：agent_tasks.status 数据转回小写枚举；
knowledge_processing_jobs.status 还原 varchar(10)（数据保持大写成员名，
小写会让 ORM 按成员名读取时报 LookupError）。
"""

import sqlalchemy as sa

from alembic import op

revision = "006_align_schema_with_models"
down_revision = "005_add_missing_tables"
branch_labels = None
depends_on = None

# ── 类1：迁移缺 DB 默认值，补 server_default=func.now()（模型声明如此）──────
_ADD_NOW_DEFAULT: list[tuple[str, str, sa.types.TypeEngine]] = [
    ("chat_messages", "created_at", sa.DateTime(timezone=True)),
    ("chat_sessions", "created_at", sa.DateTime(timezone=True)),
    ("document_chunks", "created_at", sa.DateTime(timezone=True)),
    ("document_tags", "created_at", sa.DateTime(timezone=True)),
    ("documents", "created_at", sa.DateTime(timezone=True)),
    ("tags", "created_at", sa.DateTime(timezone=True)),
]

# ── 类2：迁移里有、模型没声明的 server_default，去掉 ────────────────────────
_DROP_SERVER_DEFAULT: list[tuple[str, str, sa.types.TypeEngine]] = [
    ("node_definitions", "created_at", sa.DateTime()),
    ("notifications", "created_at", sa.DateTime()),
    ("permissions", "created_at", sa.DateTime()),
    ("roles", "created_at", sa.DateTime()),
    ("system_manuals", "created_at", sa.DateTime()),
    ("user_activity_logs", "created_at", sa.DateTime()),
    ("user_login_sessions", "created_at", sa.DateTime()),
    ("user_login_sessions", "last_seen_at", sa.DateTime()),
    ("workflow_executions", "created_at", sa.DateTime()),
    ("workflows", "created_at", sa.DateTime()),
]

# ── 类3：nullable 对齐到模型（YES→NO 之外，前三列还要去掉 004 的残留默认值）──
# fill 是收紧前对存量 NULL 的填充表达式：
#   - agent_tasks 三列用模型 Python 侧 default（0）；created_at 无法回溯，用当前时间；
#   - updated_at 用该行的 created_at（插入时间，也是模型 default=func.now() 的语义）；
#   - category / color 用模型 default 字面量。
# 第 5 个元素是目标 server_default："now" = 保留/设置为 func.now()，
# "drop" = 去掉 004 里的残留默认值，None = 两侧都没有。
# 注意：MySQL 的 MODIFY/CHANGE 会整体重写列定义，未显式传 default 就等于删掉
# 现有默认值——所以目标状态有默认值的列必须显式传 _expr_now()（agent_tasks.created_at）。
_TIGHTEN_NULLABLE: list[tuple[str, str, sa.types.TypeEngine, str, str | None]] = [
    ("agent_tasks", "completed_steps", sa.Integer(), "0", "drop"),
    ("agent_tasks", "progress", sa.Float(), "0", "drop"),
    ("agent_tasks", "retry_count", sa.Integer(), "0", "drop"),
    ("agent_tasks", "created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP", "now"),
    ("permissions", "updated_at", sa.DateTime(), "COALESCE(created_at, CURRENT_TIMESTAMP)", None),
    ("prompt_templates", "category", sa.String(50), "'general'", None),
    ("roles", "updated_at", sa.DateTime(), "COALESCE(created_at, CURRENT_TIMESTAMP)", None),
    ("system_manuals", "updated_at", sa.DateTime(), "COALESCE(created_at, CURRENT_TIMESTAMP)", None),
    ("tags", "color", sa.String(7), "'#007bff'", None),
    ("workflows", "updated_at", sa.DateTime(), "COALESCE(created_at, CURRENT_TIMESTAMP)", None),
]

# 004 给这三列的 server_default（downgrade 时按原样恢复；progress 原声明是 "0.0"）
_RESIDUAL_DEFAULTS = {
    ("agent_tasks", "completed_steps"): "0",
    ("agent_tasks", "progress"): "0.0",
    ("agent_tasks", "retry_count"): "0",
}

# ── 类4：枚举 ───────────────────────────────────────────────────────────────
# SQLAlchemy Enum 默认以成员名入库；模型即目标，两侧统一为大写成员名。
_TASK_STATUS_NAMES = (
    "PENDING", "RUNNING", "WAITING_TOOL", "WAITING_HUMAN",
    "FAILED", "COMPLETED", "ABANDONED",
)
_JOB_STATUS_NAMES = ("QUEUED", "PROCESSING", "SUCCESS", "FAILED")

# 中间形态用宽松的 varchar：把枚举约束先摘掉，再显式校验+归一大小写，
# 最后改回目标枚举。这样既不依赖 collation 的大小写折叠，也不会在
# 数据不合规时被 MySQL 静默截断（校验在 Python 侧，先于 MODIFY）。
_STATUS_COLUMNS = [
    # (table, column, existing_type, canonical_names, enum_type_name)
    ("agent_tasks", "status", sa.Enum(*_TASK_STATUS_NAMES, name="taskstatus"), _TASK_STATUS_NAMES, "taskstatus"),
    ("knowledge_processing_jobs", "status", sa.String(10), _JOB_STATUS_NAMES, "knowledgejobstatus"),
]


def _distinct_values(table: str, column: str) -> list[str | None]:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT DISTINCT {column} FROM {table}"))  # noqa: S608
    return [row[0] for row in rows]


def _validate_values(table: str, column: str, canonical_upper: tuple[str, ...]) -> list[str | None]:
    """校验存量值（大小写不敏感）都在允许集合内；否则显式失败。返回 DISTINCT 值列表。"""
    values = _distinct_values(table, column)
    bad = sorted({v for v in values if v is None or v.upper() not in canonical_upper})
    if bad:
        raise RuntimeError(
            f"006: {table}.{column} 存量数据含无法识别的取值 {bad!r}；"
            f"允许的取值为 {sorted(canonical_upper)!r}（大小写不敏感）。"
            "迁移已中止——不做静默截断，请先人工核对这批数据后再升级。"
        )
    return values


def _canonicalize_case(table: str, column: str, values: list[str | None], to_upper: bool) -> None:
    """把存量值逐组改写成目标大小写形式（每组一条 UPDATE，目标行精确匹配）。"""
    bind = op.get_bind()
    for value in values:
        if value is None:
            continue
        target = value.upper() if to_upper else value.lower()
        if value != target:
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = :target WHERE {column} = :current"),  # noqa: S608
                {"target": target, "current": value},
            )


def _expr_now() -> sa.sql.expression.TextClause | sa.sql.functions.Function:
    """DEFAULT 表达式：MySQL 侧必须渲染成表达式形式 DEFAULT (now())。

    同一个 ``sa.func.now()`` 在两条路径上的落库形态实测不同：
      - create_all（CREATE TABLE）→ SQLAlchemy 生成 ``DEFAULT (now())``，
        information_schema.COLUMN_DEFAULT='now()' + EXTRA='DEFAULT_GENERATED'；
      - alembic 的 ALTER（CHANGE/MODIFY）→ 经 format_server_default 生成
        ``DEFAULT now()``，MySQL 把它当传统 CURRENT_TIMESTAMP 形式，
        COLUMN_DEFAULT='CURRENT_TIMESTAMP'。
    带括号的文本可以精确复现 create_all 的形态（语义一致且字节级一致，
    避免在比对工具里再引入归一化）。非 MySQL 方言保持 sa.func.now()
    （SQLite 会渲染成 CURRENT_TIMESTAMP，与 create_all 一致）。
    """
    if op.get_bind().dialect.name in ("mysql", "mariadb"):
        return sa.text("(now())")
    return sa.func.now()


def _alter(table: str, column: str, existing_type: sa.types.TypeEngine, **kwargs) -> None:
    """单列/单表的结构调整；batch 对 SQLite 自动退化为重建表，对 MySQL 是普通 ALTER。"""
    with op.batch_alter_table(table) as batch:
        batch.alter_column(column, existing_type=existing_type, **kwargs)


def _apply_alters(alters: list[tuple[str, str, sa.types.TypeEngine, dict]]) -> None:
    """按表分组执行，SQLite 下每表只重建一次。"""
    by_table: dict[str, list[tuple[str, sa.types.TypeEngine, dict]]] = {}
    for table, column, etype, kwargs in alters:
        by_table.setdefault(table, []).append((column, etype, kwargs))
    for table, items in by_table.items():
        with op.batch_alter_table(table) as batch:
            for column, etype, kwargs in items:
                batch.alter_column(column, existing_type=etype, **kwargs)


def _convert_to_enum(table, column, existing_type, canonical, enum_name):
    """把 varchar/枚举列收敛成大写成员名的枚举：宽列 → 校验+归一 → 改枚举 → 兜底断言。"""
    with op.batch_alter_table(table) as batch:
        batch.alter_column(
            column, existing_type=existing_type,
            type_=sa.String(100), existing_nullable=False, nullable=False,
        )
    values = _validate_values(table, column, canonical)
    _canonicalize_case(table, column, values, to_upper=True)
    _alter(
        table, column, sa.String(100),
        type_=sa.Enum(*canonical, name=enum_name), nullable=False,
    )
    # 兜底：万一仍发生截断（理论上已被上面的校验排除），让迁移显式失败而不是留空串。
    _validate_values(table, column, canonical)


def upgrade() -> None:
    # ── 0) 预检（先于一切结构变更）：枚举列存量值无法识别时直接中止 ──────────
    for table, column, _existing, canonical, _name in _STATUS_COLUMNS:
        _validate_values(table, column, canonical)

    # ── 1) 类3 前置：收紧 NOT NULL 之前，先把存量 NULL 填充掉 ────────────────
    bind = op.get_bind()
    for table, column, _etype, fill, _default in _TIGHTEN_NULLABLE:
        bind.execute(sa.text(  # noqa: S608
            f"UPDATE {table} SET {column} = {fill} WHERE {column} IS NULL"
        ))

    # ── 2) 结构对齐（类1 补默认值 / 类2 去默认值 / 类3 收紧可空性）──────────
    now = _expr_now()
    alters: list[tuple[str, str, sa.types.TypeEngine, dict]] = []
    for table, column, etype in _ADD_NOW_DEFAULT:
        alters.append((table, column, etype, {"server_default": now, "nullable": False}))
    for table, column, etype in _DROP_SERVER_DEFAULT:
        alters.append((table, column, etype, {"server_default": False, "nullable": False}))
    for table, column, etype, _fill, default in _TIGHTEN_NULLABLE:
        kwargs: dict = {"nullable": False}
        if default == "now":
            # agent_tasks.created_at：保留 server_default=func.now()（MySQL 的
            # CHANGE/MODIFY 不显式给 default 就等于把现有默认值删掉）。
            kwargs["server_default"] = now
        elif default == "drop":
            # completed_steps/progress/retry_count：004 里带 server_default='0'，
            # 模型只有 Python 侧 default → 去掉 DB 默认值（一次 CHANGE 同时改两者）。
            kwargs["server_default"] = False
        alters.append((table, column, etype, kwargs))
    _apply_alters(alters)

    # ── 3) 类4：枚举关口统一 + 数据转换 ──────────────────────────────────────
    for table, column, existing_type, canonical, enum_name in _STATUS_COLUMNS:
        _convert_to_enum(table, column, existing_type, canonical, enum_name)


def downgrade() -> None:
    # ── 3') 枚举回退：agent_tasks.status 数据转回小写；
    #        knowledge_processing_jobs.status 还原 varchar(10)（数据保持大写）。──
    values = _validate_values("agent_tasks", "status", _TASK_STATUS_NAMES)
    _canonicalize_case("agent_tasks", "status", values, to_upper=False)
    _alter(
        "agent_tasks", "status",
        sa.Enum(*_TASK_STATUS_NAMES, name="taskstatus"),
        type_=sa.Enum(
            "pending", "running", "waiting_tool", "waiting_human",
            "failed", "completed", "abandoned", name="taskstatus",
        ),
        nullable=False,
    )
    # varchar(10) 容纳全部成员名（最长 'PROCESSING'=10），无截断风险；
    # 数据不做小写化——模型按成员名读取，小写会造成 LookupError。
    _alter(
        "knowledge_processing_jobs", "status",
        sa.Enum(*_JOB_STATUS_NAMES, name="knowledgejobstatus"),
        type_=sa.String(10), nullable=False,
    )

    # ── 2') 结构回退：类3 恢复 nullable 与 004 的残留默认值；类2 补回默认值；
    #        类1 去掉默认值。────────────────────────────────────────────────
    alters: list[tuple[str, str, sa.types.TypeEngine, dict]] = []
    now = _expr_now()
    for table, column, etype, _fill, default in _TIGHTEN_NULLABLE:
        kwargs: dict = {"nullable": True}
        if default == "now":
            kwargs["server_default"] = now
        elif default == "drop":
            # 与 004 的声明一致（Integer 用 "0"、Float 用 "0.0"，字符串字面量）。
            kwargs["server_default"] = _RESIDUAL_DEFAULTS[(table, column)]
        alters.append((table, column, etype, kwargs))
    for table, column, etype in _DROP_SERVER_DEFAULT:
        alters.append((table, column, etype, {"server_default": now, "nullable": False}))
    for table, column, etype in _ADD_NOW_DEFAULT:
        alters.append((table, column, etype, {"server_default": False, "nullable": False}))
    _apply_alters(alters)
