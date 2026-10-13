from fastmcp import FastMCP
from datetime import datetime
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "expenses.db")
CATEGORIES_PATH = os.path.join(BASE_DIR, "categories.json")

mcp = FastMCP("ExpenseTracker")


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT '',
                entry_type TEXT NOT NULL DEFAULT 'expense'
            )
        """)
        # Upgrade an older database that was created without entry_type
        cols = [row[1] for row in c.execute("PRAGMA table_info(expenses)")]
        if "entry_type" not in cols:
            c.execute(
                "ALTER TABLE expenses ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'expense'"
            )


init_db()


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


@mcp.tool()
def add_expense(date: str, amount: float, category: str,
                subcategory: str = "", note: str = "") -> dict:
    '''Add a new expense (money spent). Date format: YYYY-MM-DD.'''
    _check_date(date)
    _check_amount(amount)
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note, entry_type) "
            "VALUES (?,?,?,?,?,'expense')",
            (date, amount, category, subcategory, note),
        )
        return {"status": "ok", "id": cur.lastrowid, "type": "expense"}


@mcp.tool()
def add_credit(date: str, amount: float, category: str = "Income",
               subcategory: str = "", note: str = "") -> dict:
    '''Add a credit (money received, such as salary, a refund or a gift). Date format: YYYY-MM-DD.'''
    _check_date(date)
    _check_amount(amount)
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note, entry_type) "
            "VALUES (?,?,?,?,?,'credit')",
            (date, amount, category, subcategory, note),
        )
        return {"status": "ok", "id": cur.lastrowid, "type": "credit"}


@mcp.tool()
def edit_expense(expense_id: int, date: str | None = None,
                 amount: float | None = None, category: str | None = None,
                 subcategory: str | None = None, note: str | None = None) -> dict:
    '''Edit an existing entry (expense or credit) by id. Only the fields you pass are changed.'''
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

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    params = list(updates.values()) + [expense_id]

    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(f"UPDATE expenses SET {set_clause} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise ValueError(f"No entry found with id {expense_id}.")
        return {"status": "ok", "id": expense_id, "updated_fields": list(updates)}


@mcp.tool()
def delete_expense(expense_id: int) -> dict:
    '''Permanently delete an entry (expense or credit) by id. This cannot be undone.'''
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        if cur.rowcount == 0:
            raise ValueError(f"No entry found with id {expense_id}.")
        return {"status": "ok", "deleted_id": expense_id}


@mcp.tool()
def list_expenses(start_date: str, end_date: str) -> list[dict]:
    '''List expenses and credits within an inclusive date range (YYYY-MM-DD).'''
    _check_date(start_date)
    _check_date(end_date)
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            """
            SELECT id, date, amount, category, subcategory, note, entry_type
            FROM expenses
            WHERE date BETWEEN ? AND ?
            ORDER BY date ASC, id ASC
            """,
            (start_date, end_date),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@mcp.tool()
def summarize(start_date: str, end_date: str, category: str | None = None) -> list[dict]:
    '''Summarize by category within an inclusive date range: total expenses, total credits and net (credits minus expenses).'''
    _check_date(start_date)
    _check_date(end_date)
    with sqlite3.connect(DB_PATH) as c:
        query = """
            SELECT
                category,
                SUM(CASE WHEN entry_type = 'expense' THEN amount ELSE 0 END) AS total_expense,
                SUM(CASE WHEN entry_type = 'credit' THEN amount ELSE 0 END) AS total_credit,
                SUM(CASE WHEN entry_type = 'credit' THEN amount ELSE -amount END) AS net
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """
        params = [start_date, end_date]

        if category:
            query += " AND category = ?"
            params.append(category)

        query += " GROUP BY category ORDER BY category ASC"

        cur = c.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@mcp.resource("expense://categories", mime_type="application/json")
def categories():
    # Read fresh each time so you can edit the file without restarting
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))