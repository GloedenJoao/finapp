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

from .db import engine, init_db
from .models import User, Category, Transaction, Group

import json


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


def require_user(request: Request) -> int:
    uid = get_current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Login required")
    return uid


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
        g = s.exec(select(Group).where(Group.user_id == uid, Group.name == "Conta Corrente")).first()
        if g:
            return g
        g = Group(user_id=uid, name="Conta Corrente")
        s.add(g); s.commit(); s.refresh(g)
        return g


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


# -----------------------------
# Home (dashboard)
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    gf: Optional[TList[str]] = Query(default=None),  # filtro por grupos
):
    uid = get_current_user_id(request)

    if not uid:
        return render(
            "home.html",
            title="FinApp",
            user_id=None,
            month=None,
            groups=None,
            totals=None,
            day_rows=None,
            start_val=None,
            end_val=None,
            gf=[],
            gf_label=None,
            group_options=[],
            default_group_id=None,
        )

    # Garante grupo padrão e carrega opções
    default_group = ensure_default_group(uid)
    groups_orm = get_user_groups(uid) or [default_group]
    group_options = [{"id": g.id, "name": g.name} for g in groups_orm]
    name_by_gid = {g["id"]: g["name"] for g in group_options}

    # Resumo do mês (sem futuro)
    first_day, next_month_first, month_label = month_bounds()
    groups_list, totals = monthly_summary_by_group(uid)

    # Período (mês atual por padrão)
    default_start = first_day
    default_end = next_month_first - timedelta(days=1)
    start_date = parse_date_yyyy_mm_dd(start) or default_start
    end_date = parse_date_yyyy_mm_dd(end) or default_end
    if end_date < start_date:
        end_date = start_date

    full_day_rows = daily_series_by_group(uid, start_date, end_date)

    # Normaliza gf para lista
    gf_list: TList[str] = gf or []
    gf_set = set(gf_list)

    # Aplica filtro
    if not gf_list or "all" in gf_set:
        filtered = full_day_rows
        gf_label = "Todos os grupos"
    elif "total" in gf_set and len(gf_set) == 1:
        filtered = []
        running = 0.0
        for d in full_day_rows:
            inc = sum(r["in"] for r in d["rows"])
            out = sum(r["out"] for r in d["rows"])
            net = inc - out
            running += net
            filtered.append({
                "day": d["day"],
                "rows": [{
                    "group": "Total",
                    "in": inc, "out": out, "net": net, "balance": running
                }]
            })
        gf_label = "Total"
    else:
        # Apenas grupos selecionados por id
        selected_ids = set()
        for v in gf_list:
            try:
                selected_ids.add(int(v))
            except ValueError:
                pass
        selected_names = {name_by_gid[i] for i in selected_ids if i in name_by_gid}

        if not selected_names:
            filtered = full_day_rows
            gf_label = "Todos os grupos"
        else:
            out_rows: TList[Dict] = []
            for d in full_day_rows:
                rows = [r for r in d["rows"] if r["group"] in selected_names]
                existing_names = {r["group"] for r in rows}
                for name in selected_names - existing_names:
                    prev_balance = 0.0
                    if out_rows:
                        for prev in reversed(out_rows):
                            prev_row = next((rr for rr in prev["rows"] if rr["group"] == name), None)
                            if prev_row:
                                prev_balance = prev_row["balance"]
                                break
                    rows.append({"group": name, "in": 0.0, "out": 0.0, "net": 0.0, "balance": prev_balance})
                rows.sort(key=lambda x: x["group"].lower())
                out_rows.append({"day": d["day"], "rows": rows})
            gf_label = ", ".join(sorted(selected_names))
            filtered = out_rows

    # Mantém apenas dias com movimento
    display_rows = _prune_empty_days(filtered)

    return render(
        "home.html",
        title="FinApp",
        user_id=uid,
        month=month_label,
        groups=groups_list,
        totals=totals,
        day_rows=display_rows,
        start_val=start_date.isoformat(),
        end_val=end_date.isoformat(),
        gf=gf_list,
        gf_label=gf_label,
        group_options=group_options,
        default_group_id=default_group.id,
    )


# -----------------------------
# Auth
# -----------------------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    uid = get_current_user_id(request)
    if uid:
        return RedirectResponse("/", status_code=302)
    return render("login.html", title="Sign in")


@app.post("/login")
def login_post(email: str = Form(), password: str = Form()):
    with Session(engine) as s:
        user = s.exec(select(User).where(User.email == email)).first()
        if not user or not PWD.verify(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = make_token(user.id)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, secure=False)
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
def groups_new(request: Request, name: str = Form(...)):
    uid = require_user(request)
    with Session(engine) as s:
        g = Group(name=name, user_id=uid)
        s.add(g); s.commit()
    return RedirectResponse("/groups", status_code=303)


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
@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request):
    uid = require_user(request)
    ensure_default_group(uid)

    # Garante que existam as categorias Entrada/Saída e pega seus IDs
    cat_in, cat_out, categories = get_or_create_in_out_categories(uid)

    with Session(engine) as s:
        groups = get_user_groups(uid)
        txs = s.exec(
            select(Transaction)
            .where(Transaction.user_id == uid)
            .order_by(Transaction.tx_date.desc(), Transaction.id.desc())
        ).all()
        group_map = {g.id: g.name for g in groups}

    return render(
        "transactions.html",
        title="Transactions",
        user_id=uid,
        groups=groups,
        categories=categories,              # pode manter se quiser, mas não vamos depender disso
        txs=txs,
        group_map=group_map,
        today=today_date().isoformat(),
        cat_in_id=cat_in.id,                # >>> ADICIONADO
        cat_out_id=cat_out.id,              # >>> ADICIONADO
    )




@app.post("/transactions/new")
def create_transaction(
    request: Request,
    amount: float = Form(),
    date_str: str = Form(alias="date"),
    group_id: Optional[int] = Form(default=None),
    category_id: Optional[int] = Form(default=None),
    description: str = Form(default="")
):
    uid = require_user(request)

    # data
    try:
        d = date.fromisoformat(date_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date")

    default_group = ensure_default_group(uid)

    # garante tipos e usa Entrada por padrão se vier vazio
    cat_in, cat_out, _ = get_or_create_in_out_categories(uid)
    if category_id is None:
        category_id = cat_in.id

    with Session(engine) as s:
        # valida tipo
        cat = s.exec(select(Category).where(Category.id == category_id, Category.user_id == uid)).first()
        if not cat:
            raise HTTPException(status_code=400, detail="Tipo inválido")

        # valida grupo (ou usa padrão)
        if group_id is not None:
            g = s.exec(select(Group).where(Group.id == group_id, Group.user_id == uid)).first()
            if not g:
                raise HTTPException(status_code=400, detail="Grupo inválido")
        else:
            group_id = default_group.id

        tx = Transaction(
            user_id=uid,
            amount=amount,
            tx_date=d,
            group_id=group_id,
            category_id=category_id,
            description=description,
            account_id=None,
        )
        s.add(tx); s.commit()

    return RedirectResponse("/transactions", status_code=302)



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