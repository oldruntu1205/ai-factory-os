"""tool_call_audits：AI 员工工具调用审计台账

Revision ID: b7d41c9a2e13
Revises: 219285869b5f
Create Date: 2026-09-12

幂等说明：应用启动时 Base.metadata.create_all 会先建表，
因此 upgrade 里用 inspector 判断存在即跳过（SQLite/Postgres 同样适用），
避免出现"表已存在"导致迁移链卡住。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d41c9a2e13'
down_revision: Union[str, Sequence[str], None] = '219285869b5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "tool_call_audits"


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("agent_name", sa.String(length=100), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=True),
        sa.Column("actor", sa.String(length=30), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=True),
        sa.Column("session_id", sa.String(length=100), nullable=True),
        sa.Column("params_json", sa.Text(), nullable=True),
        sa.Column("params_hash", sa.String(length=64), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("result_excerpt", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=True),
    )
    op.create_index("ix_tool_call_audits_company_id", TABLE, ["company_id"])
    op.create_index("ix_tool_call_audits_agent_name", TABLE, ["agent_name"])
    op.create_index("ix_tool_call_audits_tool_name", TABLE, ["tool_name"])
    op.create_index("ix_tool_call_audits_session_id", TABLE, ["session_id"])
    op.create_index("ix_tool_call_audits_params_hash", TABLE, ["params_hash"])
    op.create_index("ix_tool_call_audits_created_at", TABLE, ["created_at"])


def downgrade() -> None:
    if _has_table(TABLE):
        op.drop_table(TABLE)
