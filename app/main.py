# app/main.py
from datetime import datetime, timedelta, date
from typing import Optional, Dict, List, Tuple
from typing import List as TList

from fastapi import FastAPI, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy import func, text, case
from sqlmodel import Session, select

# imports no topo de app/main.py
from fastapi import UploadFile, File  # etc...
import pdfplumber
from decimal import Decimal
import io            # <<< ADICIONE/MOVA PARA CÁ
import re
# from pypdf import PdfReader  # se estiver usando o fallback

from .db import engine, init_db
from .models import User, Category, Transaction, Group, DayBalance

import json

from urllib.parse import quote
from starlette.requests import Request as StarletteRequest  # se já não tiver
from starlette.responses import RedirectResponse
from fastapi import HTTPException



# -----------------------------
# Config
# -----------------------------
SECRET = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
ALGO = "HS256"
TOKEN_MINUTES = 60

PWD = CryptContext(schemes=["bcrypt"], deprecated="auto")

# -----------------------------
# App & Templates
# -----------------------------
app = FastAPI()

TEMPLATE_DIR = "app/templates"
STATIC_DIR = "app/static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Environment(loader=FileSystemLoader(TEMPLATE_DIR))


def render(name: str, **ctx) -> HTMLResponse:
    tpl = templates.get_template(name)
    return HTMLResponse(tpl.render(**ctx))


# -----------------------------
# Helpers: Auth
# -----------------------------


class NotAuthenticated(Exception):
    """Levantada quando o usuário precisa estar logado e não está."""
    pass

def _build_next_param(request: StarletteRequest) -> str:
    path = request.url.path
    q = str(request.url.query or "")
    current = path + (f"?{q}" if q else "")
    return quote(current, safe="")

@app.exception_handler(NotAuthenticated)
def _handle_not_authenticated(request: StarletteRequest, exc: NotAuthenticated):
    next_param = _build_next_param(request)
    return RedirectResponse(url=f"/login?next={next_param}", status_code=302)

# Opcional: redireciona 401/403 que surgirem em outros pontos
@app.exception_handler(HTTPException)
def _handle_http_exc_redirect_login(request: StarletteRequest, exc: HTTPException):
    if exc.status_code in (401, 403):
        next_param = _build_next_param(request)
        return RedirectResponse(url=f"/login?next={next_param}", status_code=302)
    # deixe os demais erros seguirem
    raise exc


def make_token(user_id: int, minutes: int = TOKEN_MINUTES) -> str:
    payload = {"sub": str(user_id), "exp": datetime.utcnow() + timedelta(minutes=minutes)}
    return jwt.encode(payload, SECRET, algorithm=ALGO)


def get_current_user_id(request: Request) -> Optional[int]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        data = jwt.decode(token, SECRET, algorithms=[ALGO])
        return int(data["sub"])
    except JWTError:
        return None
    except Exception:
        return None


def require_user(request: StarletteRequest) -> int:
    token = request.cookies.get("access_token")
    if not token:
        raise NotAuthenticated()

    try:
        data = jwt.decode(token, SECRET, algorithms=[ALGO])
        uid = int(data.get("sub") or 0)
    except Exception:
        raise NotAuthenticated()

    if not uid:
        raise NotAuthenticated()

    return uid

import re
from typing import Optional

_APLICACAO_RE = re.compile(r'^\s*APLICACAO[:\-\s]+(.+)$', flags=re.IGNORECASE)

def resolve_group_from_description(s: Session, user_id: int, description: Optional[str]) -> Optional[int]:
    """
    Se description começar com 'APLICACAO', retorna o group_id correspondente ao
    nome extraído (criando o grupo se não existir). Caso contrário, None.
    """
    if not description:
        return None
    m = _APLICACAO_RE.match(description or "")
    if not m:
        return None

    raw = m.group(1).strip()
    if not raw:
        return None

    # Normaliza espaços internos
    group_name = re.sub(r'\s+', ' ', raw)

    # procura grupo do usuário com esse nome (case-sensitive por padrão; ajuste se quiser insensitive)
    g = s.exec(select(Group).where(Group.user_id == user_id, Group.name == group_name)).first()
    if g:
        return g.id

    # cria se não existir
    g = Group(user_id=user_id, name=group_name)
    s.add(g); s.commit(); s.refresh(g)
    return g.id

# --- Regras de transferência interna (APLICACAO/RESGATE) ---
import re
from typing import Optional, Tuple

_RE_APLIC = re.compile(r'^\s*APLICACAO[:\-\s]+(.+)$', re.IGNORECASE)
_RE_RESG  = re.compile(r'^\s*RESGATE[:\-\s]+(.+)$', re.IGNORECASE)

def _ensure_group_by_name(s: Session, uid: int, name: str) -> Group:
    """
    Retorna (ou cria) um grupo do usuário.
    A busca é case-insensitive: 'casa', 'Casa', 'CASA' => mesmo grupo.
    O nome salvo será o primeiro encontrado ou, se criar, o 'name' informado.
    """
    norm = name.strip().lower()
    g = s.exec(
        select(Group).where(
            Group.user_id == uid,
            func.lower(Group.name) == norm  # <<< case-insensitive
        )
    ).first()
    if g:
        return g

    g = Group(user_id=uid, name=name.strip())
    s.add(g)
    s.commit()
    s.refresh(g)
    return g


def _ensure_default_current_account(s: Session, uid: int) -> Group:
    # por padrão chamamos de "Conta Corrente"
    return _ensure_group_by_name(s, uid, "Conta Corrente")

def _basic_category_ids(s: Session, uid: int) -> Tuple[int, int]:
    """Retorna (cat_in_id, cat_out_id). Se não houver, cria genéricas."""
    cat_in  = s.exec(select(Category).where(Category.user_id == uid, Category.kind == "in")).first()
    cat_out = s.exec(select(Category).where(Category.user_id == uid, Category.kind == "out")).first()
    if not cat_in:
        cat_in = Category(user_id=uid, name="Geral (Entrada)", kind="in")
        s.add(cat_in); s.commit(); s.refresh(cat_in)
    if not cat_out:
        cat_out = Category(user_id=uid, name="Geral (Saída)",   kind="out")
        s.add(cat_out); s.commit(); s.refresh(cat_out)
    return cat_in.id, cat_out.id

def _parse_transfer_intent(desc: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Retorna (tipo, group_name) onde tipo ∈ {"aplicacao","resgate"} ou (None,None) se não bater.
    """
    if not desc:
        return None, None
    m = _RE_APLIC.match(desc)
    if m:
        return "aplicacao", re.sub(r'\s+', ' ', m.group(1)).strip()
    m = _RE_RESG.match(desc)
    if m:
        return "resgate", re.sub(r'\s+', ' ', m.group(1)).strip()
    return None, None

def _create_internal_transfer_pair(
    s: Session,
    uid: int,
    tx_date: date,
    amount: float,
    target_group_name: str,
    intent: str,  # "aplicacao" (CC -> Grupo) ou "resgate" (Grupo -> CC)
    is_simulation: bool,
    base_uid: Optional[str] = None,
):
    """
    Cria duas transações espelho:
      - aplicacao: CC (out, -|amt|)  & Grupo (in, +|amt|)
      - resgate:   Grupo (out, -|amt|) & CC (in, +|amt|)
    """
    # normaliza sinal
    val = abs(float(amount or 0.0))
    if val <= 0:
        return

    cc = _ensure_default_current_account(s, uid)  # "Conta Corrente"
    tgt = _ensure_group_by_name(s, uid, target_group_name)
    cat_in_id, cat_out_id = _basic_category_ids(s, uid)

    # tx_uids (se houver base_uid — p.ex., da importação — cria um espelho)
    uid_a = base_uid or None
    uid_b = (base_uid + "::mirror") if base_uid else None

    if intent == "aplicacao":
        # 1) CC: saída
        tx1 = Transaction(
            user_id=uid, tx_date=tx_date, group_id=cc.id, category_id=cat_out_id,
            amount=-val, description=f"APLICACAO -> {tgt.name}", tx_uid=uid_a, is_simulation=is_simulation
        )
        # 2) Grupo: entrada
        tx2 = Transaction(
            user_id=uid, tx_date=tx_date, group_id=tgt.id, category_id=cat_in_id,
            amount=+val, description=f"APLICACAO de CC", tx_uid=uid_b, is_simulation=is_simulation
        )
    elif intent == "resgate":
        # 1) Grupo: saída
        tx1 = Transaction(
            user_id=uid, tx_date=tx_date, group_id=tgt.id, category_id=cat_out_id,
            amount=-val, description=f"RESGATE para CC", tx_uid=uid_a, is_simulation=is_simulation
        )
        # 2) CC: entrada
        tx2 = Transaction(
            user_id=uid, tx_date=tx_date, group_id=cc.id, category_id=cat_in_id,
            amount=+val, description=f"RESGATE <- {tgt.name}", tx_uid=uid_b, is_simulation=is_simulation
        )
    else:
        return

    s.add(tx1); s.add(tx2)
    s.commit()


# -----------------------------
# Helpers: Datas
# -----------------------------
def today_date() -> date:
    return datetime.utcnow().date()


def parse_date_yyyy_mm_dd(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def month_bounds() -> Tuple[date, date, str]:
    today = today_date()
    first_day = today.replace(day=1)
    if first_day.month == 12:
        next_month_first = first_day.replace(year=first_day.year + 1, month=1)
    else:
        next_month_first = first_day.replace(month=first_day.month + 1)
    return first_day, next_month_first, first_day.strftime("%Y-%m")

def month_first(day: date) -> date:
    return day.replace(day=1)

def parse_month(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        y, m = s.split("-")
        return date(int(y), int(m), 1)
    except Exception:
        return None

def next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)

def iter_months(start_m: date, end_m: date):
    cur = month_first(start_m)
    end = month_first(end_m)
    while cur <= end:
        yield cur
        cur = next_month(cur)

def monthly_net_totals(uid: int, start_m: date, end_m: date):
    months = list(iter_months(start_m, end_m))
    labels = [m.strftime("%Y-%m") for m in months]

    groups = get_user_groups(uid)
    name_by_gid = {g.id: g.name for g in groups}

    start_date = months[0]
    end_exclusive = next_month(months[-1])

    with Session(engine) as s:
        stmt = (
            select(
                func.strftime("%Y-%m", Transaction.tx_date).label("ym"),
                Transaction.group_id,
                func.coalesce(
                    func.sum(
                        case(
                            (Category.kind == "in", Transaction.amount),
                            else_=-Transaction.amount
                        )
                    ), 0.0
                ).label("net")
            )
            .select_from(Transaction)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.user_id == uid,
                Transaction.tx_date >= start_date,
                Transaction.tx_date < end_exclusive,
                Category.kind.in_(("in", "out")),
            )
            .group_by("ym", Transaction.group_id)
            .order_by("ym")
        )
        rows = list(s.exec(stmt).all())

    index_by_label = {lab: i for i, lab in enumerate(labels)}
    by_group = {name_by_gid[gid]: [0.0]*len(labels) for gid in name_by_gid}
    total = [0.0]*len(labels)

    for ym, gid, net in rows:
        i = index_by_label.get(ym)
        if i is None:
            continue
        val = float(net or 0.0)
        total[i] += val
        if gid in name_by_gid:
            by_group[name_by_gid[gid]][i] += val

    return {"labels": labels, "total": total, "by_group": by_group}



# -----------------------------
# Tipos (Entrada/Saída)
# -----------------------------
def normalize_types(uid: int) -> None:
    """Garante que existam categorias 'Entrada' (in) e 'Saída' (out) e migra duplicadas."""
    with Session(engine) as s:
        cats = s.exec(select(Category).where(Category.user_id == uid)).all()
        changed = False
        for c in cats:
            name_low = (c.name or "").strip().lower()
            if name_low == "entrada":
                if c.kind != "in":
                    c.kind = "in"; changed = True
            elif name_low in ("saída", "saida"):
                if c.kind != "out":
                    c.kind = "out"; changed = True
            elif c.kind == "income":
                c.kind = "in"; changed = True
            elif c.kind == "expense":
                c.kind = "out"; changed = True
        if changed:
            s.commit()

        # garantir 1 de cada
        cats = s.exec(select(Category).where(Category.user_id == uid)).all()
        ins = [c for c in cats if c.kind == "in"]
        outs = [c for c in cats if c.kind == "out"]

        def keep_latest_and_migrate(dupes: List[Category]) -> List[Category]:
            if len(dupes) <= 1:
                return dupes
            dupes.sort(key=lambda c: c.id or 0, reverse=True)
            keeper = dupes[0]
            extras = dupes[1:]
            for extra in extras:
                txs = s.exec(select(Transaction).where(
                    Transaction.user_id == uid,
                    Transaction.category_id == extra.id
                )).all()
                for tx in txs:
                    tx.category_id = keeper.id
                s.commit()
                s.delete(extra); s.commit()
            return [keeper]

        ins = keep_latest_and_migrate(ins)
        outs = keep_latest_and_migrate(outs)

        need_commit = False
        if not ins:
            s.add(Category(user_id=uid, name="Entrada", kind="in")); need_commit = True
        if not outs:
            s.add(Category(user_id=uid, name="Saída", kind="out")); need_commit = True
        if need_commit:
            s.commit()

def get_or_create_in_out_categories(uid: int):
    """Retorna (cat_in, cat_out, categories_list). Cria se não existir."""
    with Session(engine) as s:
        # normaliza o que já existir
        cats = s.exec(select(Category).where(Category.user_id == uid)).all()
        changed = False
        for c in cats:
            name_low = (c.name or "").strip().lower()
            if name_low == "entrada" and c.kind != "in":
                c.kind = "in"; changed = True
            if name_low in ("saída", "saida") and c.kind != "out":
                c.kind = "out"; changed = True
        if changed:
            s.commit()

        # relê
        cats = s.exec(select(Category).where(Category.user_id == uid)).all()
        cat_in  = next((c for c in cats if c.kind == "in"), None)
        cat_out = next((c for c in cats if c.kind == "out"), None)

        # cria se faltar
        need_commit = False
        if cat_in is None:
            cat_in = Category(user_id=uid, name="Entrada", kind="in")
            s.add(cat_in); need_commit = True
        if cat_out is None:
            cat_out = Category(user_id=uid, name="Saída", kind="out")
            s.add(cat_out); need_commit = True
        if need_commit:
            s.commit()
            s.refresh(cat_in); s.refresh(cat_out)

        return cat_in, cat_out, [cat_in, cat_out]

# -----------------------------
# Grupos
# -----------------------------
def get_user_groups(uid: int) -> List[Group]:
    with Session(engine) as s:
        return s.exec(select(Group).where(Group.user_id == uid).order_by(Group.name.asc())).all()


def ensure_default_group(uid: int) -> Group:
    with Session(engine) as s:
        g = s.exec(
            select(Group).where(Group.user_id == uid, Group.name == "Conta Corrente")
        ).first()
        if g:
            return g
        # criar com kind explícito
        g = Group(user_id=uid, name="Conta Corrente", kind="in")
        s.add(g); s.commit(); s.refresh(g)
        return g


# -----------------------------
# Helpers: Saldo (DayBalance)
# -----------------------------
def latest_balance_for(uid: int, group_id: int) -> Optional[float]:
    """Retorna o saldo mais recente (DayBalance) para o grupo do usuário."""
    with Session(engine) as s:
        row = s.exec(
            select(DayBalance)
            .where(DayBalance.user_id == uid, DayBalance.group_id == group_id)
            .order_by(DayBalance.day.desc())
        ).first()
        return float(row.balance) if row else None

# -----------------------------
# Migrações em runtime (SQLite)
# -----------------------------
def _column_exists(session: Session, table: str, col: str) -> bool:
    rows = session.exec(text(f"PRAGMA table_info('{table}')")).all()
    names = {r[1] for r in rows}
    return col in names


def _pragma_table_info(session: Session, table: str):
    return session.exec(text(f"PRAGMA table_info('{table}')")).all()


def _rebuild_transaction_table_if_needed():
    """Se 'account_id' estiver NOT NULL, recria a tabela 'transaction' permitindo NULL."""
    with Session(engine) as s:
        cols = _pragma_table_info(s, "transaction")
        if not cols:
            return
        info = {r[1]: (r[2], r[3]) for r in cols}  # name -> (type, notnull)
        acc = info.get("account_id")
        if not acc:
            return
        _, notnull = acc
        if int(notnull) == 0:
            return  # já é NULLABLE

        s.exec(text("BEGIN TRANSACTION"))
        s.exec(text("""
            CREATE TABLE transaction_new (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                date DATE,
                group_id INTEGER,
                account_id INTEGER,
                category_id INTEGER NOT NULL,
                description TEXT,
                tx_date DATE
            )
        """))
        s.exec(text("""
            INSERT INTO transaction_new (id, user_id, amount, date, group_id, account_id, category_id, description, tx_date)
            SELECT id, user_id, amount, date, group_id, account_id, category_id, description, tx_date
            FROM "transaction"
        """))
        s.exec(text('DROP TABLE "transaction"'))
        s.exec(text('ALTER TABLE transaction_new RENAME TO "transaction"'))
        s.exec(text('CREATE INDEX IF NOT EXISTS ix_transaction_user_id ON "transaction"(user_id)'))
        s.exec(text('CREATE INDEX IF NOT EXISTS ix_transaction_date ON "transaction"(date)'))
        s.exec(text('CREATE INDEX IF NOT EXISTS ix_transaction_tx_date ON "transaction"(tx_date)'))
        s.exec(text('CREATE INDEX IF NOT EXISTS ix_transaction_group_id ON "transaction"(group_id)'))
        s.exec(text('CREATE INDEX IF NOT EXISTS ix_transaction_category_id ON "transaction"(category_id)'))
        s.exec(text("COMMIT"))


def _runtime_migrate_users():
    with Session(engine) as s:
        cols = s.exec(text("PRAGMA table_info('user')")).all()
        colnames = {r[1] for r in cols}
        if "created_at" not in colnames:
            s.exec(text("ALTER TABLE 'user' ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
            s.commit()
        else:
            s.exec(text("UPDATE 'user' SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
            s.commit()

def _ensure_tx_uid_column():
    """
    Garante a coluna transaction.tx_uid e o índice único (user_id, tx_uid) quando tx_uid não for NULL.
    Não altera linhas existentes; deixa tx_uid como NULL para lançamentos antigos.
    """
    with Session(engine) as s:
        cols = s.exec(text("PRAGMA table_info('transaction')")).all()
        names = {r[1] for r in cols}

        # 1) Cria a coluna se faltar
        if "tx_uid" not in names:
            s.exec(text("ALTER TABLE 'transaction' ADD COLUMN tx_uid TEXT"))
            s.commit()

        # 2) Índice (parcial) de unicidade, ignorando NULL (SQLite 3.8+)
        #    -> evita duplicar importações; permite lançamentos manuais sem UID (NULL)
        s.exec(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_transaction_user_uid
            ON "transaction"(user_id, tx_uid)
            WHERE tx_uid IS NOT NULL
        """))
        # Índice auxiliar para buscas diretas por tx_uid (opcional, mas útil)
        s.exec(text("""
            CREATE INDEX IF NOT EXISTS ix_transaction_tx_uid
            ON "transaction"(tx_uid)
        """))
        s.commit()



def _ensure_tx_date_column():
    """Garante que a coluna tx_date exista e copie valores da coluna antiga 'date' se houver."""
    with Session(engine) as s:
        cols = s.exec(text("PRAGMA table_info('transaction')")).all()
        names = {r[1] for r in cols}

        if "tx_date" not in names:
            s.exec(text("ALTER TABLE 'transaction' ADD COLUMN tx_date DATE"))
            if "date" in names:
                s.exec(text("""
                    UPDATE 'transaction'
                    SET tx_date = date
                    WHERE tx_date IS NULL AND date IS NOT NULL
                """))
            s.exec(text("CREATE INDEX IF NOT EXISTS ix_transaction_tx_date ON 'transaction'(tx_date)"))
            s.commit()
        else:
            if "date" in names:
                s.exec(text("""
                    UPDATE 'transaction'
                    SET tx_date = date
                    WHERE tx_date IS NULL AND date IS NOT NULL
                """))
                s.commit()


def _runtime_migrate_groups():
    # cria tabelas que faltam
    init_db()

    # adiciona coluna group_id se faltar
    with Session(engine) as s:
        if not _column_exists(s, "transaction", "group_id"):
            s.exec(text("ALTER TABLE 'transaction' ADD COLUMN group_id INTEGER"))
            s.commit()

    # Limpeza de transações órfãs (group_id nulo ou grupo inexistente) por usuário
    with Session(engine) as s:
        users = s.exec(select(User)).all()
        for u in users:
            # remove NULL/'' (algum legado)
            s.execute(
                text("""
                    DELETE FROM "transaction"
                     WHERE user_id = :uid
                       AND (group_id IS NULL OR group_id = '')
                """),
                {"uid": u.id},
            )
            s.commit()

            # remove group_id que não existe mais para este usuário
            s.execute(
                text("""
                    DELETE FROM "transaction"
                     WHERE user_id = :uid
                       AND group_id IS NOT NULL
                       AND group_id NOT IN (SELECT id FROM "group" WHERE user_id = :uid)
                """),
                {"uid": u.id},
            )
            s.commit()




# -----------------------------
# Resumos
# -----------------------------
def monthly_summary_by_group(uid: int) -> Tuple[List[Dict], Dict[str, float]]:
    """
    (groups_list, totals) para o MÊS ATUAL.
    - Ignora FUTURO dentro do mês (até hoje).
    """
    first_day, next_month_first, _ = month_bounds()
    end_for_summary = min(next_month_first, today_date() + timedelta(days=1))

    normalize_types(uid)
    groups = get_user_groups(uid)

    with Session(engine) as s:
        stmt = (
            select(
                Transaction.group_id,
                Category.kind,
                func.coalesce(func.sum(Transaction.amount), 0.0)
            )
            .select_from(Transaction)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.user_id == uid,
                Transaction.tx_date >= first_day,
                Transaction.tx_date < end_for_summary,
                Category.kind.in_(("in", "out")),
            )
            .group_by(Transaction.group_id, Category.kind)
        )
        rows = list(s.exec(stmt).all())

    totals = {"in": 0.0, "out": 0.0, "net": 0.0}
    for gid, kind, amount in rows:
        amt = float(amount or 0.0)
        if kind == "in": totals["in"] += amt
        else:            totals["out"] += amt
    totals["net"] = totals["in"] - totals["out"]

    name_by_gid = {g.id: g.name for g in groups}
    agg: Dict[int, Dict[str, float]] = {}
    for gid, kind, amount in rows:
        if gid is None:
            continue
        agg.setdefault(gid, {"in": 0.0, "out": 0.0})
        agg[gid][kind] += float(amount or 0.0)

    groups_list: List[Dict] = []
    for g in groups:
        vals = agg.get(g.id, {"in": 0.0, "out": 0.0})
        inc, out = vals["in"], vals["out"]
        groups_list.append({"name": g.name, "in": inc, "out": out, "net": inc - out})

    return groups_list, totals


def daily_series_by_group(uid: int, start: date, end_inclusive: date) -> List[Dict]:
    """
    Série diária:
      - day: "YYYY-MM-DD"
      - rows: [{group, in, out, net, balance}] (balance = acumulado até o dia)
    """
    normalize_types(uid)
    groups = get_user_groups(uid)
    end_exclusive = end_inclusive + timedelta(days=1)

    with Session(engine) as s:
        stmt = (
            select(
                Transaction.tx_date,
                Transaction.group_id,
                Category.kind,
                func.coalesce(func.sum(Transaction.amount), 0.0),
            )
            .select_from(Transaction)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.user_id == uid,
                Transaction.tx_date >= start,
                Transaction.tx_date < end_exclusive,
                Category.kind.in_(("in", "out")),
            )
            .group_by(Transaction.tx_date, Transaction.group_id, Category.kind)
            .order_by(Transaction.tx_date.asc())
        )
        rows = list(s.exec(stmt).all())

    # Preparar dias
    all_days: List[date] = []
    cur = start
    while cur <= end_inclusive:
        all_days.append(cur)
        cur += timedelta(days=1)

    if len(groups) == 0:
        per_day = {d: {"in": 0.0, "out": 0.0} for d in all_days}
        for d, gid, kind, amount in rows:
            per_day[d][kind] += float(amount or 0.0)

        result_rows: List[Dict] = []
        balance = 0.0
        for d in all_days:
            inc = per_day[d]["in"]
            out = per_day[d]["out"]
            net = inc - out
            balance += net
            result_rows.append({
                "day": d.isoformat(),
                "rows": [{
                    "group": "Total",
                    "in": inc, "out": out, "net": net, "balance": balance
                }],
            })
        return result_rows

    name_by_gid = {g.id: g.name for g in groups}
    gids = list(name_by_gid.keys())

    per = {(d, gid, k): 0.0 for d in all_days for gid in gids for k in ("in", "out")}
    for d, gid, kind, amount in rows:
        if gid is None:
            continue
        per[(d, gid, kind)] += float(amount or 0.0)

    result_rows: List[Dict] = []
    balance_by_gid = {gid: 0.0 for gid in gids}
    gid_order = sorted(gids, key=lambda x: name_by_gid[x].lower())

    for d in all_days:
        day_rows: List[Dict] = []
        for gid in gid_order:
            inc = per[(d, gid, "in")]
            out = per[(d, gid, "out")]
            net = inc - out
            balance_by_gid[gid] += net
            day_rows.append({
                "group": name_by_gid[gid],
                "in": inc, "out": out, "net": net, "balance": balance_by_gid[gid]
            })
        result_rows.append({"day": d.isoformat(), "rows": day_rows})
    return result_rows


def _prune_empty_days(day_rows: List[Dict]) -> List[Dict]:
    """Mantém apenas os dias em que há alguma movimentação (in/out > 0)."""
    pruned = []
    for d in day_rows:
        total_in = sum(float(r.get("in") or 0.0) for r in d["rows"])
        total_out = sum(float(r.get("out") or 0.0) for r in d["rows"])
        if abs(total_in) > 1e-9 or abs(total_out) > 1e-9:
            pruned.append(d)
    return pruned


# -----------------------------
# Startup
# -----------------------------
@app.on_event("startup")
def on_startup():
    _runtime_migrate_groups()
    _runtime_migrate_users()
    _ensure_tx_date_column()
    _ensure_tx_uid_column()

# -----------------------------
# Home (dashboard)
# -----------------------------
from datetime import date, datetime
from typing import Optional, Dict, Any, List
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi import Query
from sqlmodel import Session, select, func, case

# ... imports existentes do seu arquivo (engine, templates, require_user, ensure_default_group, etc.)
from .models import DayBalance, Transaction, Group, Category

from datetime import date
from typing import Optional, Dict, Any, List
from fastapi import Query, Request, HTTPException
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select, func, case

from .models import DayBalance, Transaction, Group, Category

@app.get("/", response_class=HTMLResponse)
def home(request: Request, day: Optional[str] = Query(default=None), include_simulations: bool = Query(default=False)):
    """
    Home diária consolidada a partir de joins entre DayBalance e Transaction,
    sem usar Relationship nem Jinja2Templates.
    Filtro por data (?day=YYYY-MM-DD) e opção ?include_simulations=1 para incluir simulações.
    """
    uid = require_user(request)

    # 1) Resolver a data
    if day:
        try:
            current_day = date.fromisoformat(day)
        except ValueError:
            raise HTTPException(status_code=400, detail="Parâmetro 'day' inválido. Use YYYY-MM-DD.")
    else:
        current_day = date.today()

    # 2) Garantir que o grupo padrão exista (ex.: "Conta Corrente")
    default_group = ensure_default_group(uid)

    rows: List[Dict[str, Any]] = []
    transactions_by_group: Dict[int, List[Transaction]] = {}

    with Session(engine) as s:
        # 3) Mapa de grupos do usuário
        groups = s.exec(select(Group).where(Group.user_id == uid)).all()
        group_map = {g.id: g.name for g in groups}

        # 4) DayBalance do dia (por grupo)
        balances = s.exec(
            select(DayBalance.group_id, DayBalance.balance)
            .where(DayBalance.user_id == uid)
            .where(DayBalance.day == current_day)
        ).all()
        balance_map: Dict[int, float] = {gid: float(bal) for gid, bal in balances}

        # 5) Agregações de transações do dia (por grupo), com join em Category
        tx_stmt = (
            select(
                Transaction.group_id.label("gid"),
                func.sum(case((Category.kind == "in", Transaction.amount), else_=0.0)).label("sum_in"),
                func.sum(case((Category.kind == "out", -Transaction.amount), else_=0.0)).label("sum_out"),
                func.sum(case((Category.kind in ("in","out"), Transaction.amount), else_=0.0)).label("net"),
                func.count(Transaction.id).label("qty"),
            )
            .join(Category, Category.id == Transaction.category_id)
            .where(Transaction.user_id == uid)
            .where(Transaction.tx_date == current_day)
            .group_by(Transaction.group_id)
        )
        if not include_simulations:
            tx_stmt = tx_stmt.where(Transaction.is_simulation == False)

        tx_aggs = s.exec(tx_stmt).all()
        tx_map: Dict[int, Dict[str, Any]] = {}
        for gid, sum_in, sum_out, net, qty in tx_aggs:
            tx_map[gid or 0] = {
                "sum_in": float(sum_in or 0.0),
                "sum_out": float(sum_out or 0.0),
                "net": float(net or 0.0),
                "qty": int(qty or 0),
            }

        # 6) Carregar as transações do dia (pra listar no final, agrupadas por grupo)
        list_stmt = (
            select(Transaction)
            .where(Transaction.user_id == uid, Transaction.tx_date == current_day)
            .order_by(Transaction.id.desc())
        )
        if not include_simulations:
            list_stmt = list_stmt.where(Transaction.is_simulation == False)

        for tx in s.exec(list_stmt).all():
            transactions_by_group.setdefault(tx.group_id or 0, []).append(tx)

        # 7) Montar linhas: união dos grupos que têm saldo no dia OU transações no dia
        group_ids = set(balance_map.keys()) | set(tx_map.keys())
        for gid in sorted(group_ids):
            name = group_map.get(gid, "-")
            bal = balance_map.get(gid)
            ag = tx_map.get(gid, {"sum_in": 0.0, "sum_out": 0.0, "net": 0.0, "qty": 0})
            rows.append({
                "group_id": gid,
                "group_name": name,
                "balance": bal,
                "sum_in": ag["sum_in"],
                "sum_out": ag["sum_out"],
                "net": ag["net"],
                "qty": ag["qty"],
            })

    # 8) Renderizar sem mudar sua forma de conectar (templates é um Environment)
    html = templates.get_template("home.html").render(
        request=request,  # se o seu base.html usa isso para url_for, etc.
        day=current_day,
        rows=rows,
        transactions_by_group=transactions_by_group,
        group_map=group_map,
        include_simulations=include_simulations,
    )
    return HTMLResponse(html)

# --- helpers de datas (reutilizamos o formato YYYY-MM-DD) ---
from typing import Optional
from datetime import date as _date

def _parse_date_yyyy_mm_dd(v: Optional[str]) -> Optional[_date]:
    if not v:
        return None
    try:
        return _date.fromisoformat(v)
    except Exception:
        return None


# --- consulta consolidada por período ---
from collections import defaultdict
from sqlmodel import Session, select
from sqlalchemy import func, case

def period_aggregate(uid: int, start_d: _date, end_d: _date, include_simulations: bool):
    """
    Retorna:
      - totals: {"sum_in": float, "sum_out": float, "net": float, "qty": int}
      - by_group: [{group_id, group_name, sum_in, sum_out, net, qty}]
      - by_day: [{"day": date, "sum_in":..., "sum_out":..., "net":..., "qty":..., "items":[Transaction,...], "day_balance": float|None}]
      - group_map: {id: nome}
    """
    with Session(engine) as s:
        # mapa de grupos
        groups = s.exec(select(Group).where(Group.user_id == uid)).all()
        name_by_gid = {g.id: g.name for g in groups}

        # base: transações do usuário no intervalo
        conds = [
            Transaction.user_id == uid,
            Transaction.tx_date >= start_d,
            Transaction.tx_date <= end_d,
        ]
        if not include_simulations:
            conds.append(Transaction.is_simulation == False)  # noqa: E712

        # agregação por grupo
        stmt_group = (
            select(
                Transaction.group_id.label("gid"),
                func.sum(
                    case(
                        (Category.kind == "in", Transaction.amount),
                        else_=0.0,
                    )
                ).label("sum_in"),
                func.sum(
                    case(
                        (Category.kind == "out", -Transaction.amount),
                        else_=0.0,
                    )
                ).label("sum_out"),
                func.count().label("qty"),
            )
            .select_from(Transaction)
            .join(Category, Category.id == Transaction.category_id)
            .where(*conds, Category.kind.in_(("in", "out")))
            .group_by(Transaction.group_id)
        )
        rows_group = list(s.exec(stmt_group).all())

        by_group = []
        totals = {"sum_in": 0.0, "sum_out": 0.0, "net": 0.0, "qty": 0}
        for gid, s_in, s_out, qty in rows_group:
            s_in = float(s_in or 0.0)
            s_out = float(s_out or 0.0)
            net = s_in - s_out
            by_group.append({
                "group_id": gid,
                "group_name": name_by_gid.get(gid, "-"),
                "sum_in": s_in, "sum_out": s_out, "net": net, "qty": int(qty or 0),
            })
            totals["sum_in"] += s_in
            totals["sum_out"] += s_out
            totals["net"] += net
            totals["qty"] += int(qty or 0)

        # itens por dia (deduplicados)
        stmt_items = (
            select(Transaction)
            .where(*conds)
            .order_by(Transaction.tx_date.desc(), Transaction.id.desc())
        )
        items = s.exec(stmt_items).all()

        items_by_day = defaultdict(dict)  # day -> {tx.id: tx}
        for tx in items:
            if not getattr(tx, "id", None):
                continue
            items_by_day[tx.tx_date][tx.id] = tx

        # Conta Corrente
        cc = s.exec(
            select(Group).where(Group.user_id == uid, func.lower(Group.name) == "conta corrente")
        ).first()
        cc_id = cc.id if cc else None

        all_days = sorted(items_by_day.keys())
        day_balance_map = {}
        if cc_id and all_days:
            rows_bal = s.exec(
                select(DayBalance.day, DayBalance.balance)
                .where(
                    DayBalance.user_id == uid,
                    DayBalance.group_id == cc_id,
                    DayBalance.day.in_(all_days),
                )
            ).all()
            for d, bal in rows_bal:
                day_balance_map[d] = float(bal or 0.0)

        by_day = []
        for d in sorted(items_by_day.keys(), reverse=True):
            day_items = list(items_by_day[d].values())
            s_in, s_out = 0.0, 0.0

            # soma in/out
            cat_ids = list({it.category_id for it in day_items if it.category_id})
            kind_by_cat = {}
            if cat_ids:
                qs = s.exec(
                    select(Category.id, Category.kind).where(
                        Category.user_id == uid, Category.id.in_(cat_ids)
                    )
                ).all()
                kind_by_cat = {cid: kind for cid, kind in qs}

            for it in day_items:
                kind = kind_by_cat.get(it.category_id)
                if kind == "in":
                    s_in += float(it.amount or 0.0)
                elif kind == "out":
                    s_out += float(-(it.amount or 0.0))

            by_day.append({
                "day": d,
                "sum_in": s_in,
                "sum_out": s_out,
                "net": s_in - s_out,
                "qty": len(day_items),
                "items": day_items,
                "day_balance": day_balance_map.get(d),
            })

        by_group.sort(key=lambda r: r["group_name"].lower() if r["group_name"] else "")
        return {"totals": totals, "by_group": by_group, "by_day": by_day, "group_map": name_by_gid}

# --- página Resumo por Período ---
from fastapi import Query
from fastapi.responses import HTMLResponse

from typing import Optional
from fastapi import Query

@app.get("/summary", response_class=HTMLResponse)
def summary_page(
    request: Request,
    start: Optional[str] = Query(default=None),  # antes: str | None
    end:   Optional[str] = Query(default=None),  # antes: str | None
    include_simulations: bool = Query(default=False)
):
    uid = require_user(request)  # mantém o fluxo de auth/redirect como no resto do app
    # ^ usa o mesmo handler/redirect para login que você já tem. :contentReference[oaicite:2]{index=2}

    today = _date.today()
    # defaults: últimos 7 dias
    default_end = today
    default_start = _date.fromordinal(today.toordinal() - 6)

    s_date = _parse_date_yyyy_mm_dd(start) or default_start
    e_date = _parse_date_yyyy_mm_dd(end)   or default_end
    if e_date < s_date:
        e_date = s_date

    data = period_aggregate(uid, s_date, e_date, include_simulations)

    html = templates.get_template("period.html").render(
        request=request,
        title="Resumo por Período",
        user_id=uid,
        start_val=s_date.isoformat(),
        end_val=e_date.isoformat(),
        include_simulations=include_simulations,
        totals=data["totals"],
        by_group=data["by_group"],
        by_day=data["by_day"],
        group_map=data["group_map"],  # << incluir isto
    )

    return HTMLResponse(html)



# -----------------------------
# Auth
# -----------------------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: StarletteRequest, next: str = Query(default="")):
    # usa seu Environment (templates) normalmente
    html = templates.get_template("login.html").render(
        request=request,
        next=next or "",
        error=""
    )
    return HTMLResponse(html)


def _is_safe_next(v: str) -> bool:
    return bool(v) and v.startswith("/")


@app.post("/login")
def login_post(
    request: StarletteRequest,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="")
):
    with Session(engine) as s:
        user = s.exec(select(User).where(User.email == email)).first()

    if not user or not PWD.verify(password, user.password_hash):
        html = templates.get_template("login.html").render(
            request=request,
            next=next or "",
            error="Usuário ou senha inválidos."
        )
        return HTMLResponse(html, status_code=401)

    token = make_token(user.id)
    # redireciona para 'next' (se for um caminho interno), senão para home
    target = next if _is_safe_next(next) else "/"
    resp = RedirectResponse(url=target, status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax")  # ajuste 'secure' se usar HTTPS
    return resp



@app.get("/signup", response_class=HTMLResponse)
def signup_get(request: Request):
    uid = get_current_user_id(request)
    if uid:
        return RedirectResponse("/", status_code=302)
    return render("signup.html", title="Create account")


@app.post("/signup")
def signup_post(email: str = Form(), password: str = Form()):
    with Session(engine) as s:
        exists = s.exec(select(User).where(User.email == email)).first()
        if exists:
            raise HTTPException(status_code=400, detail="Email already registered")
        user = User(
            email=email,
            password_hash=PWD.hash(password),
            created_at=datetime.utcnow(),
        )
        s.add(user); s.commit(); s.refresh(user)
        token = make_token(user.id)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, secure=False)
    return resp

# helpers itau
import hashlib
import re

def ensure_group(uid: int, name: str, kind: str = "in") -> Group:
    with Session(engine) as s:
        g = s.exec(
            select(Group).where(Group.user_id == uid, Group.name == name)
        ).first()
        if g:
            # se existir mas estiver sem kind, corrige
            if not g.kind:
                g.kind = kind or "in"
                s.add(g); s.commit(); s.refresh(g)
            return g
        g = Group(user_id=uid, name=name, kind=kind or "in")
        s.add(g); s.commit(); s.refresh(g)
        return g


def build_itau_uid(dt: date, desc: str, amount: float) -> str:
    # UID estável por origem: "itau|YYYY-MM-DD|DESC_NORMALIZADA|{amount:.2f}"
    key = f"itau|{dt.isoformat()}|{(desc or '').strip()}|{amount:.2f}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()

# --- helper: saldo por grupo no dia (posição) ---
from sqlalchemy import case

def balances_by_group_on(uid: int, on_date: date):
    """
    Retorna lista de dicts [{id, name, balance}] com o saldo acumulado por grupo
    até 'on_date' (inclusive). Saldo = Entrada - Saída.
    """
    groups = get_user_groups(uid)
    name_by_gid = {g.id: g.name for g in groups}
    if not groups:
        return []

    with Session(engine) as s:
        stmt = (
            select(
                Transaction.group_id,
                func.coalesce(
                    func.sum(
                        case(
                            (Category.kind == "in",  Transaction.amount),
                            else_=-Transaction.amount
                        )
                    ),
                    0.0
                ).label("balance")
            )
            .select_from(Transaction)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.user_id == uid,
                Transaction.tx_date <= on_date,
                Category.kind.in_(("in", "out")),
            )
            .group_by(Transaction.group_id)
        )
        rows = list(s.exec(stmt).all())

    # normaliza para incluir grupos sem movimento com saldo 0
    balance_by_gid = {gid: 0.0 for gid in name_by_gid.keys()}
    for gid, bal in rows:
        if gid is not None:
            balance_by_gid[gid] = float(bal or 0.0)

    result = [
        {"id": gid, "name": name_by_gid[gid], "balance": balance_by_gid[gid]}
        for gid in sorted(name_by_gid.keys(), key=lambda x: name_by_gid[x].lower())
    ]
    return result


# --- rota do dash ---
@app.get("/dash", response_class=HTMLResponse)
def dash_page(
    request: Request,
    on: Optional[str] = Query(default=None)  # 'YYYY-MM-DD'; default = hoje
):
    uid = require_user(request)

    # data filtrada
    on_date = parse_date_yyyy_mm_dd(on) or today_date()

    # garante ao menos o grupo padrão (não cria transação, só o grupo)
    ensure_default_group(uid)

    # dados
    data = balances_by_group_on(uid, on_date)
    labels = [d["name"] for d in data]
    values = [round(d["balance"], 2) for d in data]
    total  = round(sum(values), 2)

    return render(
    "dash.html",
    title="Dashboards",
    user_id=uid,
    on_val=on_date.isoformat(),
    labels_json=json.dumps(labels, ensure_ascii=False),  # >>> usa JSON válido p/ JS
    values_json=json.dumps(values),
    total=total,
)

@app.get("/api/dash/monthly")
def api_dash_monthly(request: Request, start: Optional[str] = None, end: Optional[str] = None):
    uid = require_user(request)

    today = today_date()
    this_m = month_first(today)
    default_end = this_m

    # últimos 6 meses
    m5 = this_m
    for _ in range(5):
        m5 = m5.replace(day=1)
        m5 = date(m5.year - 1, 12, 1) if m5.month == 1 else date(m5.year, m5.month - 1, 1)
    default_start = m5

    start_m = parse_month(start) or default_start
    end_m   = parse_month(end)   or default_end
    if end_m < start_m:
        end_m = start_m

    data = monthly_net_totals(uid, start_m, end_m)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})





@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# -----------------------------
# Grupos
# -----------------------------
@app.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request):
    uid = require_user(request)
    groups = get_user_groups(uid)
    return render("groups.html", title="Grupos", user_id=uid, groups=groups)


@app.post("/groups/new")
def groups_new(
    request: Request,
    name: str = Form(...),
    kind: str = Form(default="in")  # "in" ou "out"
):
    uid = require_user(request)
    if kind not in ("in", "out"):
        kind = "in"
    with Session(engine) as s:
        exists = s.exec(
            select(Group).where(Group.user_id == uid, Group.name == name)
        ).first()
        if exists:
            return RedirectResponse(url="/groups?err=exists", status_code=303)
        g = Group(user_id=uid, name=name, kind=kind)
        s.add(g); s.commit()
    return RedirectResponse(url="/groups", status_code=303)



@app.post("/groups/delete/{gid}")
def groups_delete(request: Request, gid: int):
    uid = require_user(request)
    with Session(engine) as s:
        g_del = s.get(Group, gid)
        if not g_del or g_del.user_id != uid:
            return RedirectResponse("/groups", status_code=303)

        # 1) Apaga todas as transações que pertencem a este grupo
        s.execute(
            text("""
                DELETE FROM "transaction"
                 WHERE user_id = :uid
                   AND group_id = :gid
            """),
            {"uid": uid, "gid": gid},
        )
        s.commit()

        # 2) Apaga o grupo
        s.delete(g_del)
        s.commit()

    return RedirectResponse("/groups", status_code=303)




@app.get("/api/groups")
def api_groups(request: Request):
    uid = require_user(request)
    groups = get_user_groups(uid)
    return JSONResponse(
        [{"id": g.id, "name": g.name} for g in groups],
        headers={"Cache-Control": "no-store"}
    )


# -----------------------------
# Transações
# -----------------------------
from collections import defaultdict
from sqlalchemy import text
from sqlmodel import select

@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request):
    uid = require_user(request)
    ensure_default_group(uid)

    cat_in, cat_out, categories = get_or_create_in_out_categories(uid)

    imp = int(request.query_params.get("imported", 0) or 0)
    skp = int(request.query_params.get("skipped", 0) or 0)
    bal = int(request.query_params.get("balances", 0) or 0)

    with Session(engine) as s:
        groups = get_user_groups(uid)
        txs = s.exec(
            select(Transaction)
            .where(Transaction.user_id == uid)
            .order_by(Transaction.tx_date.desc(), Transaction.id.desc())
        ).all()
        group_map = {g.id: g.name for g in groups}

    # agrupa por dia
    by_day = defaultdict(list)
    for tx in txs:
        d = getattr(tx, "tx_date", None) or getattr(tx, "date", None)
        by_day[d].append(tx)

    txs_grouped = []
    for d in sorted(by_day.keys(), reverse=True):
        txs_grouped.append(
            (d, sorted(by_day[d], key=lambda t: (t.tx_date, t.id), reverse=True))
        )
    # Depois de preparar 'txs_grouped'
    conta_corrente = ensure_group(uid, "Conta Corrente")
    days = [d for d, _ in txs_grouped if d is not None]
    day_balances = {}
    if days:
        with Session(engine) as s:
            rows = s.exec(
                select(DayBalance.day, DayBalance.balance).where(
                    DayBalance.user_id == uid,
                    DayBalance.group_id == conta_corrente.id,
                    DayBalance.day.in_(days)
                )
            ).all()
            for day, balance in rows:
                day_balances[day.isoformat()] = float(balance or 0.0)

    # >>> NOVO: buscar saldo do dia (DayBalance) para "Conta Corrente"
    conta_corrente = ensure_group(uid, "Conta Corrente")
    days = [d for d, _ in txs_grouped if d is not None]
    day_balances = {}
    if days:
        with Session(engine) as s:
            rows = s.exec(
                select(DayBalance.day, DayBalance.balance).where(
                    DayBalance.user_id == uid,
                    DayBalance.group_id == conta_corrente.id,
                    DayBalance.day.in_(days)
                )
            ).all()
            # usa chave string (ISO) para evitar problemas de comparação no template
            for day, balance in rows:
                day_balances[day.isoformat()] = float(balance or 0.0)

    return render(
        "transactions.html",
        title="Transactions",
        user_id=uid,
        groups=groups,
        categories=categories,
        txs_grouped=txs_grouped,
        group_map=group_map,
        today=today_date().isoformat(),
        cat_in_id=cat_in.id,
        cat_out_id=cat_out.id,
        imported=imp,
        skipped=skp,
        balances=bal,
        day_balances=day_balances,   # <<< passa para o template
        
    )

from typing import Optional
from datetime import date as _date
from fastapi import Request, Form
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

@app.post("/transactions/new")
def create_transaction(
    request: Request,
    tx_date: str = Form(...),           # "YYYY-MM-DD"
    amount: float = Form(...),          # pode vir negativo/positivo
    category_id: int = Form(...),       # id de Category existente (in/out)
    group_id: Optional[int] = Form(None),
    description: Optional[str] = Form(""),
):
    uid = require_user(request)

    # Parse da data
    try:
        d = _date.fromisoformat(tx_date)
    except Exception:
        d = _date.today()

    with Session(engine) as s:
        # valida categoria pertence ao usuário
        cat = s.exec(
            select(Category).where(Category.user_id == uid, Category.id == category_id)
        ).first()
        if not cat:
            raise HTTPException(status_code=400, detail="Tipo inválido")

        # garante "Conta Corrente" (default)
        conta_corrente = _ensure_default_current_account(s, uid)

        # valida grupo caso tenha vindo
        if group_id is not None:
            g = s.exec(
                select(Group).where(Group.user_id == uid, Group.id == group_id)
            ).first()
            if not g:
                raise HTTPException(status_code=400, detail="Grupo inválido")
        else:
            group_id = conta_corrente.id

        # ===== NOVA REGRA: transferência interna por descrição =====
        intent, tgt_name = _parse_transfer_intent(description)
        if intent in ("aplicacao", "resgate") and tgt_name:
            # Lançamento manual é SIMULAÇÃO
            _create_internal_transfer_pair(
                s=s,
                uid=uid,
                tx_date=d,
                amount=amount,
                target_group_name=tgt_name,
                intent=intent,            # "aplicacao" (CC->Grupo) | "resgate" (Grupo->CC)
                is_simulation=True,       # <<< manual = simulação
                base_uid=None,            # sem UID base aqui
            )
            return RedirectResponse("/transactions", status_code=303)

        # ===== Fluxo normal (sem regra APLICACAO/RESGATE) =====
        tx = Transaction(
            user_id=uid,
            tx_date=d,
            group_id=group_id,
            category_id=category_id,
            amount=amount,
            description=description or "",
            account_id=None,
            tx_uid=None,                 # manual não usa UID de dedupe
            is_simulation=True,          # <<< manual = simulação
        )
        s.add(tx)
        s.commit()

    return RedirectResponse("/transactions", status_code=303)


@app.post("/transactions/delete/{tx_id}")
def delete_transaction_post(request: Request, tx_id: int):
    uid = require_user(request)
    with Session(engine) as s:
        tx = s.exec(
            select(Transaction).where(
                Transaction.id == tx_id,
                Transaction.user_id == uid
            )
        ).first()
        if tx:
            s.delete(tx); s.commit()
    return RedirectResponse("/transactions", status_code=303)


@app.get("/transactions/delete/{tx_id}")
def delete_transaction_get(request: Request, tx_id: int):
    uid = require_user(request)
    with Session(engine) as s:
        tx = s.exec(
            select(Transaction).where(
                Transaction.id == tx_id,
                Transaction.user_id == uid
            )
        ).first()
        if tx:
            s.delete(tx); s.commit()
    return RedirectResponse("/transactions", status_code=303)

@app.post("/transactions/delete-all")
def delete_all_transactions(request: Request):
    """Remove all transactions belonging to the current user."""
    uid = require_user(request)
    with Session(engine) as s:
        # Fetch all transactions for this user
        user_txs = s.exec(
            select(Transaction).where(Transaction.user_id == uid)
        ).all()

        for tx in user_txs:
            s.delete(tx)
        s.commit()

    # Redirect back to the transactions page
    return RedirectResponse("/transactions", status_code=303)

# ================================
# IMPORTAÇÃO ITAÚ (PDF) — PYTHON 3.8
# ================================
from fastapi import UploadFile, File, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select
from decimal import Decimal
from typing import Optional, List
from datetime import date as _date, datetime as _dt
import hashlib
import pdfplumber
from pypdf import PdfReader
import io
import re

# --- Regex e parsers ---
_MONEY_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")  # "1.234,56" / "-19,89"
_LINE_DATE = re.compile(r"^(\d{2}/\d{2}/\d{4}|\d{2}/\d{2}/\d{2}|\d{2}/\d{2})\b")
_TIME_RE  = re.compile(r"\b([01]\d|2[0-3]):[0-5]\d\b")     # HH:MM (24h)

def parse_brl_money(s: str) -> float:
    s = s.strip().replace(".", "").replace(",", ".")
    return float(Decimal(s))

def _safe_parse_pt_date(s: str) -> _date:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return _dt.strptime(s, fmt).date()
        except Exception:
            pass
    try:
        d, m = s.split("/")
        return _date(today_date().year, int(m), int(d))
    except Exception:
        return today_date()

# --- UID helpers (com hora e legado) ---
def build_itau_uid(d: _date, time_str: Optional[str], desc: str, amount: float) -> str:
    t = (time_str or "00:00").strip()
    key = f"itau|{d.isoformat()}|{t}|{(desc or '').strip()}|{amount:.2f}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()

def build_itau_uid_legacy(d: _date, desc: str, amount: float) -> str:
    key = f"itau|{d.isoformat()}|{(desc or '').strip()}|{amount:.2f}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()

# Se seu projeto tem DayBalance, importe do seu models:
# from .models import DayBalance, Transaction, Group, Category
# E já existem: require_user, ensure_group, get_or_create_in_out_categories, today_date, engine
from typing import List, Optional, Tuple
from datetime import date as _date, datetime
from fastapi import UploadFile, File, HTTPException
from sqlmodel import Session, select
from pypdf import PdfReader
import io, re
import uuid

# --- utilitários rápidos para parser ---
_RE_LINE = re.compile(
    r'(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<desc>.+?)\s+(?P<amount>[-+]?\d{1,3}(?:\.\d{3})*,\d{2})\s*$'
)

def _brl_to_float(s: str) -> float:
    # "1.234,56" -> 1234.56
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def _parse_itau_pdf_to_rows(pdf_bytes: bytes) -> List[Tuple[_date, str, float]]:
    """
    Retorna lista de tuplas (data, descricao, valor_float).
    Valor positivo = crédito; negativo = débito (pelo próprio sinal).
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    rows: List[Tuple[_date, str, float]] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for raw in text.splitlines():
            m = _RE_LINE.search(raw)
            if not m:
                continue
            dt_s = m.group("date")
            desc = m.group("desc").strip()
            amt_s = m.group("amount")

            try:
                d = datetime.strptime(dt_s, "%d/%m/%Y").date()
            except Exception:
                continue

            val = _brl_to_float(amt_s)
            rows.append((d, desc, val))
    return rows

def _first_category_of_kind(s: Session, uid: int, kind: str) -> int:
    c = s.exec(select(Category).where(Category.user_id == uid, Category.kind == kind)).first()
    if c:
        return c.id
    # cria genérica se não houver
    c = Category(user_id=uid, name=f"Geral ({'Entrada' if kind=='in' else 'Saída'})", kind=kind)
    s.add(c); s.commit(); s.refresh(c)
    return c.id


@app.post("/transactions/import/itau-pdf")
def import_itau_pdf(request: Request, file: UploadFile = File(...)):
    uid = require_user(request)

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Envie um arquivo PDF.")

    pdf_bytes = file.file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="PDF vazio.")

    # 1) Parser -> linhas (data, descrição, valor)
    try:
        rows = _parse_itau_pdf_to_rows(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao ler PDF: {e}")

    if not rows:
        raise HTTPException(status_code=400, detail="Nenhuma linha reconhecida no PDF.")

    with Session(engine) as s:
        # 2) Garantias: Conta Corrente + categorias básicas
        conta_corrente = _ensure_default_current_account(s, uid)
        cat_in_id  = _first_category_of_kind(s, uid, "in")
        cat_out_id = _first_category_of_kind(s, uid, "out")

        # 3) SALVAR TRANSAÇÕES
        created = 0
        for d, desc, amount in rows:
            # UID base para dedupe (por linha do extrato)
            new_uid = f"itau:{uid}:{d.isoformat()}:{uuid.uuid4().hex}"

            # ===== NOVA REGRA: transferência interna por descrição =====
            intent, tgt_name = _parse_transfer_intent(desc)
            if intent in ("aplicacao", "resgate") and tgt_name:
                _create_internal_transfer_pair(
                    s=s,
                    uid=uid,
                    tx_date=d,
                    amount=amount,           # usa o valor "cru" do extrato
                    target_group_name=tgt_name,
                    intent=intent,           # "aplicacao" (CC->Grupo) | "resgate" (Grupo->CC)
                    is_simulation=False,     # <<< importado = oficial
                    base_uid=new_uid,        # cria par com ::mirror
                )
                created += 2
                continue

            # ===== Fluxo original (sem APLICACAO/RESGATE) =====
            # Decide categoria pela direção do valor (opcional; mantenha sua lógica se já tiver):
            cat_id = cat_in_id if amount >= 0 else cat_out_id

            tx = Transaction(
                user_id=uid,
                tx_date=d,
                group_id=conta_corrente.id,   # padrão: Conta Corrente
                category_id=cat_id,
                amount=amount,
                description=desc,
                account_id=None,
                tx_uid=new_uid,
                is_simulation=False,
            )
            s.add(tx)
            s.commit()
            created += 1

    return RedirectResponse("/transactions", status_code=303)

