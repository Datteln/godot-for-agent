# Alembic 数据库迁移指南

## 目标

在修改数据库结构时，保证变更可追踪、可回滚、可重复执行。

## 基本流程

```bash
# 1) 生成迁移文件（根据模型差异）
alembic revision --autogenerate -m "add user_status"

# 2) 人工审查迁移脚本（必须）
# 检查字段类型、索引、默认值、非空约束、数据迁移语句

# 3) 执行升级
alembic upgrade head

# 4) 回滚验证（本地或测试环境）
alembic downgrade -1
```

## 编写规范

- 每次结构变更对应一个独立 revision，命名语义清晰。
- 复杂数据迁移应拆分为“结构迁移 + 数据迁移”两个步骤。
- 对不可逆操作（如删列、删表）在注释中说明风险与回滚策略。
- 避免在业务代码里隐式修改表结构。

## 升级/回滚模板

```python
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260320_add_user_status"
down_revision = "20260319_prev"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 users 表新增 status 字段并设置默认值。"""
    op.add_column(
        "users",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )


def downgrade() -> None:
    """回滚 users.status 字段。"""
    op.drop_column("users", "status")
```

## 常见检查清单

- 迁移脚本是否能在空库和存量库都成功执行。
- 新增非空字段是否提供默认值或分阶段上线方案。
- 索引与唯一约束是否与查询路径一致。
- 是否验证过 downgrade 至少一步可执行。

## 与代码评审联动

- PR 涉及模型变更时，必须包含对应 Alembic revision。
- 若未提供迁移脚本，需在评审中明确阻断上线。
