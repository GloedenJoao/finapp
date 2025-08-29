# app/models.py
from datetime import datetime, date
from typing import Optional
from sqlmodel import SQLModel, Field


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


class Transaction(SQLModel, table=True):
    _tablename_ = "transaction"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)

    amount: float
    tx_date: date = Field(index=True)  # <- seu main.py usa tx_date

    group_id: Optional[int] = Field(default=None, index=True)
    account_id: Optional[int] = Field(default=None, index=True)

    category_id: int = Field(index=True)
    description: Optional[str] = Field(default=None)