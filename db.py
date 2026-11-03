import sqlite3
from config import DB_PATH


def _run(sql, args=(), fetch=None):
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        c.commit()
        if fetch == "one":
            r = cur.fetchone()
            return dict(r) if r else None
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        if fetch == "count":
            return cur.rowcount
        return cur.lastrowid
    finally:
        c.close()


def init():
    _run("""CREATE TABLE IF NOT EXISTS workers(
        user_id INTEGER PRIMARY KEY,
        username TEXT, full_name TEXT,
        status TEXT DEFAULT 'pending',      -- pending / approved / rejected
        anketa TEXT, invite_link TEXT,
        orders INTEGER DEFAULT 0,
        total_orders INTEGER DEFAULT 0,
        total_paid INTEGER DEFAULT 0,
        nickname TEXT, timezone TEXT, about TEXT,   -- профиль
        goal INTEGER DEFAULT 0)                     -- цель накопления в заказах (0 = по умолчанию)""")
    # payouts.status: requested / queued / processing / paid / failed / rejected / cancelled
    # payouts.sent: сколько звёзд УЖЕ реально выдано (чтобы повтор не платил дважды)
    _run("""CREATE TABLE IF NOT EXISTS payouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, stars INTEGER,
        status TEXT DEFAULT 'requested',
        sent INTEGER DEFAULT 0,
        orders_count INTEGER DEFAULT 0,   -- сколько заказов «закрывает» эта выплата
        created_at TEXT, paid_at TEXT)""")
    _run("""CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        status TEXT DEFAULT 'pending',    -- pending / approved / rejected
        header_msg_id INTEGER,            -- сообщение с кнопками в группе отчётов
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TEXT, reviewed_by INTEGER)""")
    _run("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")
    for sql in (  # миграция для баз, созданных старыми версиями
        "ALTER TABLE payouts ADD COLUMN sent INTEGER DEFAULT 0",
        "ALTER TABLE payouts ADD COLUMN orders_count INTEGER DEFAULT 0",
        "ALTER TABLE workers ADD COLUMN nickname TEXT",
        "ALTER TABLE workers ADD COLUMN timezone TEXT",
        "ALTER TABLE workers ADD COLUMN about TEXT",
        "ALTER TABLE workers ADD COLUMN goal INTEGER DEFAULT 0",
        "ALTER TABLE payouts ADD COLUMN created_at TEXT",
        "ALTER TABLE payouts ADD COLUMN paid_at TEXT",
    ):
        try:
            _run(sql)
        except sqlite3.OperationalError:
            pass  # колонка уже есть


# ── работники ──
def get_worker(uid):
    return _run("SELECT * FROM workers WHERE user_id=?", (uid,), "one")


def upsert_application(uid, username, full_name, anketa):
    _run("""INSERT INTO workers(user_id, username, full_name, anketa, status)
            VALUES(?,?,?,?, 'pending')
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, full_name=excluded.full_name,
              anketa=excluded.anketa, status='pending'""",
         (uid, username, full_name, anketa))


def set_status(uid, status):
    _run("UPDATE workers SET status=? WHERE user_id=?", (status, uid))


def set_invite(uid, link):
    _run("UPDATE workers SET invite_link=? WHERE user_id=?", (link, uid))


def add_order(uid):
    _run("UPDATE workers SET orders=orders+1, total_orders=total_orders+1 WHERE user_id=?", (uid,))
    return get_worker(uid)["orders"]


def change_orders(uid, delta):
    _run("UPDATE workers SET orders=orders+? WHERE user_id=?", (delta, uid))


def add_paid(uid, stars):
    _run("UPDATE workers SET total_paid=total_paid+? WHERE user_id=?", (stars, uid))


# ── выплаты ──
def create_payout(uid, stars, orders_count=0):
    return _run("INSERT INTO payouts(user_id, stars, orders_count, created_at) VALUES(?,?,?,datetime('now'))",
                (uid, stars, orders_count))


def get_payout(pid):
    return _run("SELECT * FROM payouts WHERE id=?", (pid,), "one")


def list_payouts(status):
    return _run("SELECT * FROM payouts WHERE status=? ORDER BY id", (status,), "all")


def set_payout_status(pid, status):
    _run("UPDATE payouts SET status=? WHERE id=?", (status, pid))


def claim_payout(pid):
    """Атомарно «забирает» выплату: queued -> processing. True, если забрали мы."""
    return _run("UPDATE payouts SET status='processing' WHERE id=? AND status='queued'",
                (pid,), "count") > 0


def add_sent(pid, stars):
    _run("UPDATE payouts SET sent=sent+? WHERE id=?", (stars, pid))


# ── профиль и цель ──
PROFILE_FIELDS = ("nickname", "timezone", "about")


def update_profile(uid, field, value):
    if field not in PROFILE_FIELDS:      # защита: имя колонки подставляется в SQL
        raise ValueError(field)
    _run(f"UPDATE workers SET {field}=? WHERE user_id=?", (value, uid))


def set_goal(uid, goal):
    _run("UPDATE workers SET goal=? WHERE user_id=?", (goal, uid))


def mark_paid(pid):
    _run("UPDATE payouts SET status='paid', paid_at=datetime('now') WHERE id=?", (pid,))


# ── отчёты по заказам ──
def create_report(uid):
    return _run("INSERT INTO reports(user_id) VALUES(?)", (uid,))


def set_report_header(rid, msg_id):
    _run("UPDATE reports SET header_msg_id=? WHERE id=?", (msg_id, rid))


def get_report(rid):
    return _run("SELECT * FROM reports WHERE id=?", (rid,), "one")


def get_report_by_header(msg_id):
    return _run("SELECT * FROM reports WHERE header_msg_id=?", (msg_id,), "one")


def review_report(rid, status, admin_id):
    """Атомарно: меняет pending -> approved/rejected. True, если это сделали мы."""
    return _run("UPDATE reports SET status=?, reviewed_by=?, reviewed_at=datetime('now') "
                "WHERE id=? AND status='pending'", (status, admin_id, rid), "count") > 0


def count_reports(uid, status):
    return _run("SELECT COUNT(*) AS n FROM reports WHERE user_id=? AND status=?",
                (uid, status), "one")["n"]


# ── служебные значения (юзербот пишет сюда пульс и баланс) ──
def kv_set(key, value):
    _run("INSERT INTO kv(key, value) VALUES(?,?) "
         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def kv_get(key, default=None):
    r = _run("SELECT value FROM kv WHERE key=?", (key,), "one")
    return r["value"] if r else default


# ── статистика ──
def stats(offset_h):
    mod = f"{offset_h:+d} hours"

    def by_status(table, where="1=1", args=()):
        rows = _run(f"SELECT status, COUNT(*) AS n FROM {table} WHERE {where} GROUP BY status", args, "all")
        return {r["status"]: r["n"] for r in rows}

    paid = _run("SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS s FROM payouts WHERE status='paid'", (), "one")
    today = _run("SELECT COALESCE(SUM(stars),0) AS s FROM payouts "
                 "WHERE status='paid' AND date(paid_at, ?) = date('now', ?)", (mod, mod), "one")
    pend = _run("SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS s FROM payouts "
                "WHERE status IN ('requested','queued','processing')", (), "one")
    failed = _run("SELECT COUNT(*) AS n FROM payouts WHERE status='failed'", (), "one")
    top = _run("SELECT user_id, nickname, full_name, username, total_orders, total_paid FROM workers "
               "WHERE status='approved' ORDER BY total_orders DESC, total_paid DESC LIMIT 5", (), "all")
    return {
        "workers": by_status("workers"),
        "reports_today": by_status("reports", "date(created_at, ?) = date('now', ?)", (mod, mod)),
        "reports_week": by_status("reports", "date(created_at, ?) >= date('now', ?, '-6 days')", (mod, mod)),
        "reports_all": by_status("reports"),
        "paid_count": paid["n"], "paid_stars": paid["s"], "paid_today": today["s"],
        "pending_count": pend["n"], "pending_stars": pend["s"],
        "failed_count": failed["n"],
        "top": top,
    }
