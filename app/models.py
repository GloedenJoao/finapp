from typing import Optional
from sqlmodel import SQLModel, Field
from datetime import date
from sqlalchemy import Column
from sqlalchemy.types import Date as SA_Date

# ---------- Usuário ----------
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str

# ---------- Conta (LEGADO) ----------
class Account(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    type: Optional[str] = None
    currency: Optional[str] = None

# ---------- Categoria ----------
class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    kind: str  # "in" ou "out"

# ---------- Grupo ----------
class Group(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str = Field(index=True)

# ---------- Transação ----------
class Transaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    amount: float

    # atributo Python tx_date; coluna no banco continua "date"
    tx_date: date = Field(sa_column=Column("date", SA_Date, index=True))

    group_id: Optional[int] = Field(default=None, index=True)  # <- NOVO
    account_id: Optional[int] = Field(default=None, index=True)  # legado
    category_id: int = Field(index=True)
    description: Optional[str] = None