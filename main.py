from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from contextlib import contextmanager
from datetime import datetime
import asyncio
import os
import sqlite3
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _pick_db_path() -> str:
    """Choose a folder the server can write to.

    1. EXPENSES_DB_PATH, if you set it (for example a persistent disk)
    2. The folder next to this file (works on your own PC)
    3. The system temp folder (works when the deployed code folder is read-only)
    """
    env_path = os.environ.get("EXPENSES_DB_PATH")
    if env_path:
        return env_path
    for folder in (BASE_DIR, tempfile.gettempdir()):
        probe = os.path.join(folder, ".write_test")
        try:
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return os.path.join(folder, "expenses.db")
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), "expenses.db")


DB_PATH = _pick_db_path()
CATEGORIES_PATH = os.path.join(BASE_DIR, "categories.json")

mcp = FastMCP("ExpenseTracker")


# --------------------------------------------------------------------------
# Database helpers (plain sqlite3, run in worker threads so they never block)
# --------------------------------------------------------------------------

@contextmanager
def db():
    """One short-lived connection per call: commit on success, always close."""
    conn = sqlite3.connect(DB_PATH, timeout=30)  # wait up to 30s if another user is writing
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        try:
            c.execute("PRAGMA journal_mode=WAL")  # readers and a writer can work together
        except sqlite3.DatabaseError:
            pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT '',
                entry_type TEXT NOT NULL DEFAULT 'expense',
                user_id TEXT NOT NULL DEFAULT 'anonymous'
            )
        """)
        # Upgrade older databases that were created without these columns
        cols = [row[1] for row in c.execute("PRAGMA table_info(expenses)")]
        if "entry_type" not in cols:
            c.execute("ALTER TABLE expenses ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'expense'")
        if "user_id" not in cols:
            c.execute("ALTER TABLE expenses ADD COLUMN user_id TEXT NOT NULL DEFAULT 'anonymous'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)")


init_db()


# --------------------------------------------------------------------------
# Who is calling?
# --------------------------------------------------------------------------

def _current_user() -> tuple[str, str]:
    """Return (user_id, source). Each signed-in user gets their own data.

    With no sign-in (for example on your own PC) everyone is 'anonymous'.
    """
    try:
        token = get_access_token()
    except Exception:
        token = None
    if token is None:
        return "anonymous", "no sign-in"
    claims = getattr(token, "claims", None) or {}
    for key in ("sub", "email", "preferred_username", "login"):
        value = claims.get(key)
        if value:
            return str(value), f"token claim '{key}'"
    return "anonymous", "signed in, but no user claim found"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _check_date(value: str) -> str:
    """Make sure the date is YYYY-MM-DD, because BETWEEN compares dates as text."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date '{value}'. Use the format YYYY-MM-DD.")
    return value


def _check_amount(value: float) -> float:
    if value <= 0:
        raise ValueError("amount must be greater than 0.")
    return value


# --------------------------------------------------------------------------
# Blocking work (each function opens its own connection inside one thread)
# --------------------------------------------------------------------------

def _add_entry(user_id, entry_type, date, amount, category, subcategory, note):
    with db() as c:
        cur = c.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note, entry_type, user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (date, amount, category, subcategory, note, entry_type, user_id),
        )
        return {"status": "ok", "id": cur.lastrowid, "type": entry_type}


def _edit_entry(user_id, expense_id, updates):
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    params = list(updates.values()) + [expense_id, user_id]
    with db() as c:
        cur = c.execute(
            f"UPDATE expenses SET {set_clause} WHERE id = ? AND user_id = ?", params
        )
        if cur.rowcount == 0:
            raise ValueError(f"No entry found with id {expense_id}.")
        return {"status": "ok", "id": expense_id, "updated_fields": list(updates)}


def _delete_entry(user_id, expense_id):
    with db() as c:
        cur = c.execute(
            "DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id)
        )
        if cur.rowcount == 0:
            raise ValueError(f"No entry found with id {expense_id}.")
        return {"status": "ok", "deleted_id": expense_id}


def _list_entries(user_id, start_date, end_date):
    with db() as c:
        cur = c.execute(
            """
            SELECT id, date, amount, category, subcategory, note, entry_type
            FROM expenses
            WHERE user_id = ? AND date BETWEEN ? AND ?
            ORDER BY date ASC, id ASC
            """,
            (user_id, start_date, end_date),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _summarize(user_id, start_date, end_date, category):
    query = """
        SELECT
            category,
            SUM(CASE WHEN entry_type = 'expense' THEN amount ELSE 0 END) AS total_expense,
            SUM(CASE WHEN entry_type = 'credit' THEN amount ELSE 0 END) AS total_credit,
            SUM(CASE WHEN entry_type = 'credit' THEN amount ELSE -amount END) AS net
        FROM expenses
        WHERE user_id = ? AND date BETWEEN ? AND ?
    """
    params = [user_id, start_date, end_date]
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " GROUP BY category ORDER BY category ASC"
    with db() as c:
        cur = c.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# MCP tools (async: the event loop stays free while the database works)
# --------------------------------------------------------------------------

@mcp.tool()
async def whoami() -> dict:
    '''Show which user id the server sees for you. Your entries are stored under this id.'''
    user_id, source = _current_user()
    return {"user_id": user_id, "source": source}


@mcp.tool()
async def add_expense(date: str, amount: float, category: str,
                      subcategory: str = "", note: str = "") -> dict:
    '''Add a new expense (money spent). Date format: YYYY-MM-DD.'''
    _check_date(date)
    _check_amount(amount)
    user_id, _ = _current_user()
    return await asyncio.to_thread(
        _add_entry, user_id, "expense", date, amount, category, subcategory, note
    )


@mcp.tool()
async def add_credit(date: str, amount: float, category: str = "Income",
                     subcategory: str = "", note: str = "") -> dict:
    '''Add a credit (money received, such as salary, a refund or a gift). Date format: YYYY-MM-DD.'''
    _check_date(date)
    _check_amount(amount)
    user_id, _ = _current_user()
    return await asyncio.to_thread(
        _add_entry, user_id, "credit", date, amount, category, subcategory, note
    )


@mcp.tool()
async def edit_expense(expense_id: int, date: str | None = None,
                       amount: float | None = None, category: str | None = None,
                       subcategory: str | None = None, note: str | None = None) -> dict:
    '''Edit one of your entries (expense or credit) by id. Only the fields you pass are changed.'''
    updates = {}
    if date is not None:
        updates["date"] = _check_date(date)
    if amount is not None:
        updates["amount"] = _check_amount(amount)
    if category is not None:
        updates["category"] = category
    if subcategory is not None:
        updates["subcategory"] = subcategory
    if note is not None:
        updates["note"] = note
    if not updates:
        raise ValueError("Nothing to update. Pass at least one field to change.")
    user_id, _ = _current_user()
    return await asyncio.to_thread(_edit_entry, user_id, expense_id, updates)


@mcp.tool()
async def delete_expense(expense_id: int) -> dict:
    '''Permanently delete one of your entries (expense or credit) by id. This cannot be undone.'''
    user_id, _ = _current_user()
    return await asyncio.to_thread(_delete_entry, user_id, expense_id)


@mcp.tool()
async def list_expenses(start_date: str, end_date: str) -> list[dict]:
    '''List your expenses and credits within an inclusive date range (YYYY-MM-DD).'''
    _check_date(start_date)
    _check_date(end_date)
    user_id, _ = _current_user()
    return await asyncio.to_thread(_list_entries, user_id, start_date, end_date)


@mcp.tool()
async def summarize(start_date: str, end_date: str,
                    category: str | None = None) -> list[dict]:
    '''Summarize your entries by category within an inclusive date range: total expenses, total credits and net (credits minus expenses).'''
    _check_date(start_date)
    _check_date(end_date)
    user_id, _ = _current_user()
    return await asyncio.to_thread(_summarize, user_id, start_date, end_date, category)


@mcp.resource("expense://categories", mime_type="application/json")
async def categories():
    # Read fresh each time so you can edit the file without restarting
    def _read():
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return await asyncio.to_thread(_read)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
