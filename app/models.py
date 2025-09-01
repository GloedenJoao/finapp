# app/models.py
from datetime import datetime, date
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint


class User(SQLModel, table=True):
    _tablename_ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True)  # opcional: unique via migração/constraint
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class Group(SQLModel, table=True):
    _tablename_ = "group"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str


class Category(SQLModel, table=True):
    _tablename_ = "category"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    # valores esperados: "in" ou "out"
    kind: str = Field(description="in or out")


# --- NOVO: saldo diário por grupo (ex.: Conta Corrente) ---
class DayBalance(SQLModel, table=True):
    __tablename__ = "day_balance"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", "day", name="uq_day_balance_user_group_day"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    group_id: int = Field(index=True)
    day: date = Field(index=True)
    balance: float  # em moeda (R$)
    source: str = Field(default="itau")  # opcional: origem do saldo

# --- ALTERAÇÃO: transação com UID de deduplicação ---
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

    # NOVO: UID de deduplicação (ex.: hash "itau|2025-08-31|DESCR| -19,89")
    tx_uid: Optional[str] = Field(default=None, index=True)

    # constraint: (user_id, tx_uid) único quando tx_uid for preenchido
    __table_args__ = (
        UniqueConstraint("user_id", "tx_uid", name="uq_transaction_user_uid"),
    )