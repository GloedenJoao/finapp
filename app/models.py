# app/models.py
from datetime import datetime, date
from typing import Optional, List

from sqlmodel import SQLModel, Field, UniqueConstraint, Relationship
from sqlalchemy import and_
from sqlalchemy.orm import foreign  # marcar colunas "filhas" no primaryjoin


class User(SQLModel, table=True):
    __tablename__ = "user"
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class Group(SQLModel, table=True):
    __tablename__ = "group"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    # "in" (entradas) ou "out" (saídas)
    kind: str = Field(default="in", description="in or out")


class Category(SQLModel, table=True):
    __tablename__ = "category"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    kind: str = Field(description="in or out")


class DayBalance(SQLModel, table=True):
    __tablename__ = "day_balance"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    group_id: int = Field(index=True)
    day: date = Field(index=True)
    balance: float

    # Transações do MESMO (user_id, group_id, day) — somente leitura
    transactions: List["Transaction"] = Relationship(
        back_populates="day_balance",
        sa_relationship_kwargs=dict(
            primaryjoin=lambda: and_(
                foreign(Transaction.user_id) == DayBalance.user_id,
                foreign(Transaction.group_id) == DayBalance.group_id,
                foreign(Transaction.tx_date) == DayBalance.day,
            ),
            viewonly=True,  # <- passe via sa_relationship_kwargs
        ),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "group_id", "day", name="uq_day_balance_user_group_day"),
    )


class Transaction(SQLModel, table=True):
    __tablename__ = "transaction"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    tx_date: date = Field(index=True)
    group_id: Optional[int] = Field(default=None, index=True)
    category_id: Optional[int] = Field(default=None, index=True)
    amount: float
    description: Optional[str] = ""
    account_id: Optional[int] = None
    tx_uid: Optional[str] = Field(default=None, index=True)
    is_simulation: bool = Field(default=False, index=True)

    # DayBalance do MESMO (user_id, group_id, day) — somente leitura
    day_balance: Optional[DayBalance] = Relationship(
        back_populates="transactions",
        sa_relationship_kwargs=dict(
            primaryjoin=lambda: and_(
                foreign(Transaction.user_id) == DayBalance.user_id,
                foreign(Transaction.group_id) == DayBalance.group_id,
                foreign(Transaction.tx_date) == DayBalance.day,
            ),
            viewonly=True,  # <- passe via sa_relationship_kwargs
        ),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "tx_uid", name="uq_transaction_user_uid"),
    )
