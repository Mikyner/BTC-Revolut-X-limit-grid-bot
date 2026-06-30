"""
Databáze pro Limit Order Grid Bot
===================================
Oproti market botu přibývá tabulka exchange_orders — udržuje stav
živých limit orderů na Revolut X burze.
"""

import logging
import sqlite3
import time
from pathlib import Path

import config

logger = logging.getLogger("database")

def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bot_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                running INTEGER NOT NULL DEFAULT 0,
                paused_since REAL,
                paused_reason TEXT,
                last_price REAL,
                last_summary_date TEXT,
                last_pause_reminder REAL,
                last_low_capital_alert REAL
            );
            INSERT OR IGNORE INTO bot_status (id, running) VALUES (1, 0);

            -- Grid úrovně (senzor - kde BUY ordery mají být)
            CREATE TABLE IF NOT EXISTS grid_levels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                price REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            -- Živé ordery na burze Revolut X
            -- Toto je klíčová nová tabulka oproti market botu
            CREATE TABLE IF NOT EXISTS exchange_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue_order_id TEXT UNIQUE NOT NULL,
                client_order_id TEXT,
                side TEXT NOT NULL,           -- 'buy' | 'sell'
                grid_price REAL NOT NULL,     -- cena grid úrovně
                size_eur REAL,               -- velikost v EUR (pro buy)
                size_btc REAL,               -- velikost v BTC (pro sell)
                status TEXT NOT NULL DEFAULT 'open',  -- open | filled | cancelled
                fill_price REAL,             -- skutečná fill cena (po vyplnění)
                fill_btc REAL,               -- skutečné množství BTC
                fill_eur REAL,               -- skutečná EUR hodnota
                linked_buy_order_id INTEGER, -- FK na exchange_orders.id (pro sell ordery)
                created_at REAL NOT NULL,
                filled_at REAL,
                cancelled_at REAL
            );

            -- Uzavřené obchody (párované buy+sell = zisk)
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                side TEXT NOT NULL,
                buy_price REAL NOT NULL,
                sell_price REAL,
                btc_amount REAL NOT NULL,
                eur_spent REAL NOT NULL,
                eur_received REAL,
                profit_eur REAL,
                buy_order_id INTEGER,  -- FK na exchange_orders.id
                sell_order_id INTEGER, -- FK na exchange_orders.id
                dry_run INTEGER NOT NULL DEFAULT 0
            );

            -- Zůstatky (virtuální peněženka pro paper trading i tracking)
            CREATE TABLE IF NOT EXISTS wallet (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                eur_balance REAL NOT NULL DEFAULT 0,
                btc_balance REAL NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO wallet (id, eur_balance, btc_balance)
                VALUES (1, 0, 0);

            -- Nastavení (živě editovatelné z dashboardu)
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Snapshoty hodnoty portfolia (pro graf)
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                total_value_eur REAL NOT NULL,
                eur_balance REAL NOT NULL,
                btc_balance REAL NOT NULL,
                btc_price REAL NOT NULL
            );

            -- Cash adjustments (vklady/výběry)
            CREATE TABLE IF NOT EXISTS cash_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                amount_eur REAL NOT NULL,
                note TEXT
            );
        """)
        _seed_default_settings(conn)
    logger.info("DB inicializována")


def _seed_default_settings(conn):
    defaults = {
        "order_size_eur": str(config.ORDER_SIZE_EUR),
        "grid_levels": str(config.GRID_LEVELS),
        "grid_range_percent": str(config.GRID_RANGE_PERCENT),
        "grid_bias_percent": str(config.GRID_BIAS_PERCENT),
        "max_price_deviation_percent": str(config.MAX_PRICE_DEVIATION_PERCENT),
        "compounding_enabled": "false",
        "compounding_percent": "3.0",
        "max_position_eur": "0",
    }
    for key, value in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))


# --- Bot status ---

def get_bot_status():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bot_status WHERE id = 1").fetchone()
        return dict(row) if row else {}

def set_bot_running(running: bool, paused_reason: str = None):
    now = time.time()
    with get_conn() as conn:
        if running:
            conn.execute(
                "UPDATE bot_status SET running=1, paused_since=NULL, paused_reason=NULL WHERE id=1"
            )
        else:
            conn.execute(
                "UPDATE bot_status SET running=0, paused_since=COALESCE(paused_since,?), paused_reason=? WHERE id=1",
                (now, paused_reason)
            )

def set_last_price(price):
    with get_conn() as conn:
        conn.execute("UPDATE bot_status SET last_price=? WHERE id=1", (price,))

def get_last_price():
    with get_conn() as conn:
        row = conn.execute("SELECT last_price FROM bot_status WHERE id=1").fetchone()
        return row["last_price"] if row else None

def get_last_summary_date():
    with get_conn() as conn:
        row = conn.execute("SELECT last_summary_date FROM bot_status WHERE id=1").fetchone()
        return row["last_summary_date"] if row else None

def set_last_summary_date(date_str):
    with get_conn() as conn:
        conn.execute("UPDATE bot_status SET last_summary_date=? WHERE id=1", (date_str,))

def set_last_pause_reminder(ts):
    with get_conn() as conn:
        conn.execute("UPDATE bot_status SET last_pause_reminder=? WHERE id=1", (ts,))


# --- Grid levels ---

def get_grid_levels():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM grid_levels WHERE is_active=1 ORDER BY price ASC"
        ).fetchall()
        return [dict(r) for r in rows]

def replace_grid_levels(prices: list):
    with get_conn() as conn:
        conn.execute("UPDATE grid_levels SET is_active=0")
        conn.executemany(
            "INSERT INTO grid_levels (price, is_active) VALUES (?, 1)",
            [(p,) for p in prices]
        )


# --- Exchange orders ---

def create_exchange_order(venue_order_id, client_order_id, side, grid_price,
                          size_eur=None, size_btc=None, linked_buy_order_id=None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO exchange_orders
                (venue_order_id, client_order_id, side, grid_price, size_eur, size_btc,
                 status, linked_buy_order_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """, (venue_order_id, client_order_id, side, grid_price,
              size_eur, size_btc, linked_buy_order_id, time.time()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def mark_order_filled(venue_order_id, fill_price, fill_btc, fill_eur):
    with get_conn() as conn:
        conn.execute("""
            UPDATE exchange_orders
            SET status='filled', fill_price=?, fill_btc=?, fill_eur=?, filled_at=?
            WHERE venue_order_id=?
        """, (fill_price, fill_btc, fill_eur, time.time(), venue_order_id))


def mark_order_cancelled(venue_order_id):
    with get_conn() as conn:
        conn.execute("""
            UPDATE exchange_orders
            SET status='cancelled', cancelled_at=?
            WHERE venue_order_id=?
        """, (time.time(), venue_order_id))


def get_open_buy_orders():
    """Všechny otevřené BUY limit ordery na burze."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM exchange_orders
            WHERE side='buy' AND status='open'
            ORDER BY grid_price DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_open_sell_orders():
    """Všechny otevřené SELL limit ordery na burze."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM exchange_orders
            WHERE side='sell' AND status='open'
            ORDER BY grid_price ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_all_open_orders():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM exchange_orders WHERE status='open' ORDER BY grid_price ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_order_by_venue_id(venue_order_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exchange_orders WHERE venue_order_id=?", (venue_order_id,)
        ).fetchone()
        return dict(row) if row else None


def cancel_all_open_orders_in_db():
    """Označí všechny otevřené ordery jako cancelled v DB (volá se při re-centru)."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE exchange_orders SET status='cancelled', cancelled_at=?
            WHERE status='open'
        """, (time.time(),))


# --- Trades (uzavřené buy+sell páry) ---

def record_completed_trade(buy_order_id, sell_order_id, buy_price, sell_price,
                           btc_amount, eur_spent, eur_received, dry_run=False):
    profit = eur_received - eur_spent if eur_received else None
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO trades
                (timestamp, side, buy_price, sell_price, btc_amount, eur_spent,
                 eur_received, profit_eur, buy_order_id, sell_order_id, dry_run)
            VALUES (?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (time.time(), buy_price, sell_price, btc_amount, eur_spent,
              eur_received, profit, buy_order_id, sell_order_id, int(dry_run)))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_recent_trades(limit=50):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_realized_profit():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(profit_eur),0) as total FROM trades WHERE profit_eur IS NOT NULL"
        ).fetchone()
        return row["total"] or 0.0


def get_trade_count():
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()
        return row["c"]


def get_trade_count_since(ts):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM trades WHERE timestamp>=?", (ts,)
        ).fetchone()
        return row["c"]


def get_realized_profit_since(ts):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(profit_eur),0) as t FROM trades WHERE timestamp>=? AND profit_eur IS NOT NULL",
            (ts,)
        ).fetchone()
        return row["t"] or 0.0


def get_first_trade_timestamp():
    with get_conn() as conn:
        row = conn.execute("SELECT MIN(timestamp) as ts FROM trades").fetchone()
        return row["ts"] if row else None

def get_first_snapshot_timestamp():
    """Čas prvního equity snapshotu = kdy bot reálně začal běžet."""
    with get_conn() as conn:
        row = conn.execute("SELECT MIN(timestamp) as ts FROM equity_snapshots").fetchone()
        return row["ts"] if row else None


def get_avg_profit_per_trade():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT AVG(profit_eur) as avg, COUNT(*) as cnt FROM trades WHERE profit_eur IS NOT NULL"
        ).fetchone()
        return {"avg": row["avg"] or 0.0, "count": row["cnt"] or 0}


def get_win_rate():
    """Procento ziskových obchodů (profit_eur > 0) z celkového počtu prodejů."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN profit_eur > 0 THEN 1 ELSE 0 END) as wins
            FROM trades WHERE profit_eur IS NOT NULL
        """).fetchone()
        total = row["total"] or 0
        wins = row["wins"] or 0
        win_rate = (wins / total * 100) if total > 0 else None
        return {"win_rate": win_rate, "wins": wins, "total": total}


def get_best_trade():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT profit_eur, sell_price FROM trades WHERE profit_eur IS NOT NULL ORDER BY profit_eur DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# --- Wallet ---

def get_wallet():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM wallet WHERE id=1").fetchone()
        if row:
            return dict(row)
        return {"eur_balance": config.PAPER_STARTING_BALANCE_EUR, "btc_balance": 0.0}


def init_wallet():
    """Inicializuje peněženku na startovací zůstatek (jen jednou)."""
    with get_conn() as conn:
        row = conn.execute("SELECT eur_balance FROM wallet WHERE id=1").fetchone()
        if row and row["eur_balance"] == 0:
            conn.execute(
                "UPDATE wallet SET eur_balance=? WHERE id=1",
                (config.PAPER_STARTING_BALANCE_EUR,)
            )


def update_wallet(eur, btc):
    with get_conn() as conn:
        conn.execute("UPDATE wallet SET eur_balance=?, btc_balance=? WHERE id=1", (eur, btc))


# --- Settings ---

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def get_setting_float(key, default=0.0):
    val = get_setting(key)
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def get_setting_int(key, default=0):
    val = get_setting(key)
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )

def get_all_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# --- Equity snapshots ---

def record_equity_snapshot(btc_price, eur_balance, btc_balance):
    total = eur_balance + btc_balance * btc_price
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO equity_snapshots (timestamp, total_value_eur, eur_balance, btc_balance, btc_price)
            VALUES (?, ?, ?, ?, ?)
        """, (time.time(), total, eur_balance, btc_balance, btc_price))


def get_equity_history(since_ts=None):
    with get_conn() as conn:
        if since_ts:
            rows = conn.execute(
                "SELECT * FROM equity_snapshots WHERE timestamp>=? ORDER BY timestamp ASC",
                (since_ts,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM equity_snapshots ORDER BY timestamp ASC"
            ).fetchall()
        return [dict(r) for r in rows]


# --- Cash adjustments ---

def record_cash_adjustment(amount_eur, note=None):
    wallet = get_wallet()
    new_eur = wallet["eur_balance"] + amount_eur
    update_wallet(new_eur, wallet["btc_balance"])
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO cash_adjustments (timestamp, amount_eur, note) VALUES (?, ?, ?)",
            (time.time(), amount_eur, note)
        )
    return new_eur


def get_total_cash_adjustments():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_eur),0) as total FROM cash_adjustments"
        ).fetchone()
        return row["total"] or 0.0
