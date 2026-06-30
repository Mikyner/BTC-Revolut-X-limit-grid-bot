"""
Web Dashboard pro BTC/EUR Limit Order Grid Bot
===============================================
Port 5060. Klíčová nová sekce: živé limit ordery na burze.
"""

import logging
import time

from flask import Flask, render_template, jsonify, request

import config
import bot.database as db
from bot.grid_engine import build_grid
from bot.revolutx_client import RevolutXClient
import bot.runner as runner
import bot.telegram_notify as telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("web")

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
revx = RevolutXClient()


def _enrich_grid_levels(grid_levels, open_buys, open_sells):
    """Přidá stav (buy_open/sell_open/empty) a last_trade_ts ke každému grid levelu."""
    open_buy_prices = {round(o["grid_price"], 2) for o in open_buys}

    # SELL ordery: mapuj přes linked_buy_order_id → BUY grid_price
    sell_linked_buy_prices = set()
    if open_sells:
        try:
            buy_ids = [o["linked_buy_order_id"] for o in open_sells if o.get("linked_buy_order_id")]
            if buy_ids:
                with db.get_conn() as conn:
                    placeholders = ",".join("?" * len(buy_ids))
                    rows = conn.execute(
                        f"SELECT grid_price FROM exchange_orders WHERE id IN ({placeholders})",
                        buy_ids
                    ).fetchall()
                    sell_linked_buy_prices = {round(r["grid_price"], 2) for r in rows}
        except Exception:
            pass

    try:
        with db.get_conn() as conn:
            trade_rows = conn.execute("""
                SELECT ROUND(buy_price, 0) as p, MAX(timestamp) as last_ts
                FROM trades GROUP BY ROUND(buy_price, 0)
            """).fetchall()
        last_trade_by_price = {float(r["p"]): r["last_ts"] for r in trade_rows}
    except Exception:
        last_trade_by_price = {}

    result = []
    for lvl in grid_levels:
        p = round(lvl["price"], 2)
        p0 = round(lvl["price"], 0)
        if p in open_buy_prices:
            status = "buy_open"
        elif p in sell_linked_buy_prices:
            status = "sell_open"
        else:
            status = "empty"
        result.append({
            **dict(lvl),
            "status": status,
            "last_trade_ts": last_trade_by_price.get(p0),
        })
    return result


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.errorhandler(500)
def handle_error(e):
    logger.error(f"Exception on {request.path}: {e}", exc_info=True)
    return jsonify({"error": str(e)}), 500


@app.route("/api/state")
def api_state():
    try:
        status = db.get_bot_status()
        wallet = db.get_wallet()
        grid_levels = db.get_grid_levels()
        settings = db.get_all_settings()
        # Normalizace compounding_enabled na lowercase pro JS
        if "compounding_enabled" in settings:
            settings["compounding_enabled"] = "true" if str(settings["compounding_enabled"]).lower() in ("true", "1") else "false"
        trade_count = db.get_trade_count()
        realized_profit = db.get_realized_profit()
        open_buys = db.get_open_buy_orders()
        open_sells = db.get_open_sell_orders()
        recent_trades = db.get_recent_trades(limit=30)
        avg_profit = db.get_avg_profit_per_trade()
        win_rate_data = db.get_win_rate()

        # Časové metriky: průměrná doba mezi nákupy a průměrná doba držení pozice
        avg_time_between_buys = None
        avg_holding_hours = None
        try:
            with db.get_conn() as conn:
                # Doba mezi po sobě jdoucími nákupy (timestamp v trades = čas SELL, takže použijeme buy_order_id->created_at)
                rows = conn.execute("""
                    SELECT eo.created_at FROM exchange_orders eo
                    WHERE eo.side='buy' AND eo.status='filled'
                    ORDER BY eo.created_at ASC
                """).fetchall()
                if len(rows) >= 2:
                    timestamps = [r["created_at"] for r in rows]
                    diffs = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
                    avg_time_between_buys = sum(diffs) / len(diffs) / 3600  # v hodinách

                # Doba držení pozice = filled_at(sell) - filled_at(buy), přes linked_buy_order_id
                hold_rows = conn.execute("""
                    SELECT s.filled_at as sell_ts, b.filled_at as buy_ts
                    FROM exchange_orders s
                    JOIN exchange_orders b ON s.linked_buy_order_id = b.id
                    WHERE s.side='sell' AND s.status='filled' AND b.filled_at IS NOT NULL
                """).fetchall()
                if hold_rows:
                    holdings = [r["sell_ts"] - r["buy_ts"] for r in hold_rows if r["sell_ts"] and r["buy_ts"]]
                    if holdings:
                        avg_holding_hours = sum(holdings) / len(holdings) / 3600
        except Exception as e:
            logger.warning(f"Nepodařilo se spočítat časové metriky: {e}")
        trades_24h = db.get_trade_count_since(time.time() - 86400)
        best_trade = db.get_best_trade()
        first_trade_ts = db.get_first_trade_timestamp()
        first_snapshot_ts = db.get_first_snapshot_timestamp()
        profit_7d = db.get_realized_profit_since(time.time() - 7 * 86400)
        profit_30d = db.get_realized_profit_since(time.time() - 30 * 86400)
        profit_90d = db.get_realized_profit_since(time.time() - 90 * 86400)

        try:
            current_price = status.get("last_price")
            if not current_price:
                current_price = revx.get_current_price()
        except Exception as e:
            current_price = None
            logger.error(f"Nepodařilo se získat cenu: {e}")

        # Hodnota portfolia (volné EUR + rezervace v BUY orderech + BTC)
        locked_eur = sum((o.get("size_eur") or 0) for o in db.get_open_buy_orders())
        total_value = wallet["eur_balance"] + locked_eur + wallet["btc_balance"] * (current_price or 0)
        adjustments = db.get_total_cash_adjustments()
        starting_value = config.PAPER_STARTING_BALANCE_EUR + adjustments
        pnl_percent = ((total_value - starting_value) / starting_value * 100) if starting_value else 0

        # Nerealizovaný P&L — BTC v otevřených SELL orderech vs co bylo zaplaceno
        unrealized = 0.0
        if current_price:
            for order in open_sells:
                btc = order.get("size_btc") or 0
                # Najdi linked buy order pro eur_spent
                if order.get("linked_buy_order_id") and btc > 0:
                    try:
                        with db.get_conn() as conn:
                            row = conn.execute(
                                "SELECT fill_eur, size_eur FROM exchange_orders WHERE id=?",
                                (order["linked_buy_order_id"],)
                            ).fetchone()
                            spent = (row["fill_eur"] or row["size_eur"] or 0) if row else 0
                        unrealized += btc * current_price - spent
                    except Exception:
                        pass

        return jsonify({
            "bot_running": bool(status.get("running", False)),
            "dry_run": config.DRY_RUN,
            "current_price": current_price,
            "total_value_eur": round(total_value, 2),
            "pnl_percent": round(pnl_percent, 2),
            "starting_value_eur": round(starting_value, 2),
            "realized_profit_eur": round(realized_profit, 2),
            "unrealized_pnl_eur": round(unrealized, 2),
            "eur_balance": round(wallet["eur_balance"], 2),
            "btc_balance": wallet["btc_balance"],
            "trade_count": trade_count,
            "trades_24h": trades_24h,
            "open_buy_orders": len(open_buys),
            "open_sell_orders": len(open_sells),
            "open_buy_orders_list": [dict(o) for o in open_buys],
            "open_sell_orders_list": [dict(o) for o in open_sells],
            "grid_levels": _enrich_grid_levels(grid_levels, open_buys, open_sells),
            "settings": settings,
            "recent_trades": [dict(t) for t in recent_trades],
            "avg_profit_per_sell": avg_profit,
            "win_rate": win_rate_data,
            "avg_time_between_buys_hours": avg_time_between_buys,
            "avg_holding_hours": avg_holding_hours,
            "best_trade": dict(best_trade) if best_trade else None,
            "first_trade_ts": first_trade_ts,
            "first_snapshot_ts": first_snapshot_ts,
            "profit_7d": round(profit_7d, 4),
            "profit_30d": round(profit_30d, 4),
            "profit_90d": round(profit_90d, 4),
            "profit_alltime": round(realized_profit, 4),
            "config_defaults": {
                "grid_levels": config.GRID_LEVELS,
                "grid_range_percent": config.GRID_RANGE_PERCENT,
                "grid_bias_percent": config.GRID_BIAS_PERCENT,
                "order_size_eur": config.ORDER_SIZE_EUR,
            },
        })
    except Exception as e:
        logger.exception(f"Chyba v api_state: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/equity")
def api_equity():
    range_param = request.args.get("range", "1m")
    now = time.time()
    since_map = {"1d": now - 86400, "1w": now - 7*86400, "1m": now - 30*86400,
                 "3m": now - 90*86400}
    since = since_map.get(range_param)
    snapshots = db.get_equity_history(since_ts=since)
    return jsonify([{
        "timestamp": s["timestamp"],
        "total_value_eur": s["total_value_eur"],
    } for s in snapshots])


@app.route("/api/cnb_rate")
def api_cnb_rate():
    """Proxy pro ČNB kurz EUR/CZK — obchází CORS omezení prohlížeče."""
    try:
        import urllib.request
        url = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
        with urllib.request.urlopen(url, timeout=5) as resp:
            text = resp.read().decode("utf-8")
        for line in text.splitlines():
            if "|EUR|" in line:
                rate_str = line.strip().split("|")[-1].replace(",", ".")
                rate = float(rate_str)
                return jsonify({"rate": rate, "source": "cnb"})
        return jsonify({"rate": 24.24, "source": "fallback"})
    except Exception as e:
        logger.warning(f"ČNB rate fetch selhal: {e}")
        return jsonify({"rate": 24.24, "source": "fallback"})


@app.route("/api/bot/start", methods=["POST"])
def api_start():
    db.set_bot_running(True)
    return jsonify({"ok": True})


@app.route("/api/bot/pause", methods=["POST"])
def api_pause():
    db.set_bot_running(False, paused_reason="Ručně pozastaveno z dashboardu")
    return jsonify({"ok": True})


@app.route("/api/grid/recenter", methods=["POST"])
def api_recenter():
    try:
        current_price = revx.get_current_price()
        settings = {
            "grid_levels": db.get_setting_int("grid_levels", config.GRID_LEVELS),
            "grid_range_percent": db.get_setting_float("grid_range_percent", config.GRID_RANGE_PERCENT),
            "grid_bias_percent": db.get_setting_float("grid_bias_percent", config.GRID_BIAS_PERCENT),
            "compounding_enabled": db.get_setting("compounding_enabled", "false") == "true",
            "compounding_percent": db.get_setting_float("compounding_percent", 3.0),
            "max_position_eur": db.get_setting_float("max_position_eur", 0.0),
        }
        runner.do_recenter(current_price, settings, triggered_by="manual")
        db.set_bot_running(True)
        return jsonify({"ok": True, "new_center": current_price})
    except Exception as e:
        logger.error(f"Re-center selhal: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json()
    ALLOWED = {
        "order_size_eur": float,
        "grid_levels": int,
        "grid_range_percent": float,
        "grid_bias_percent": float,
        "max_price_deviation_percent": float,
        "compounding_enabled": str,
        "compounding_percent": float,
        "max_position_eur": float,
    }
    saved = {}
    errors = {}
    for key, value in data.items():
        if key not in ALLOWED:
            continue
        try:
            typed = ALLOWED[key](value)
            # Normalizace bool hodnot na lowercase string pro konzistenci
            if key == "compounding_enabled":
                typed = "true" if str(typed).lower() in ("true", "1", "yes") else "false"
            db.set_setting(key, typed)
            saved[key] = typed
        except (ValueError, TypeError) as e:
            errors[key] = str(e)
    logger.info(f"Settings aktualizovány: {saved}")
    return jsonify({"saved": saved, "errors": errors})


@app.route("/api/orders/cancel_buys", methods=["POST"])
def api_cancel_buys():
    open_buys = db.get_open_buy_orders()
    cancelled = 0
    for order in open_buys:
        try:
            if not config.DRY_RUN:
                revx.cancel_order(order["venue_order_id"])
            db.mark_order_cancelled(order["venue_order_id"])
            # Vrať rezervaci zpět do volných EUR
            w = db.get_wallet()
            db.update_wallet(w["eur_balance"] + (order.get("size_eur") or 0), w["btc_balance"])
            cancelled += 1
        except Exception as e:
            logger.warning(f"Nepodařilo se zrušit order {order['venue_order_id']}: {e}")
    logger.info(f"Cancel buys: zrušeno {cancelled} orderů")
    return jsonify({"ok": True, "cancelled": cancelled})


@app.route("/api/cash_adjustment", methods=["POST"])
def api_cash_adjustment():
    data = request.get_json()
    amount = float(data.get("amount_eur", 0))
    note = data.get("note", "")
    new_balance = db.record_cash_adjustment(amount, note)
    telegram.notify_cash_adjustment(amount, note, new_balance)
    return jsonify({"ok": True, "new_eur_balance": round(new_balance, 2)})


if __name__ == "__main__":
    db.init_db()
    db.init_wallet()
    runner.start_background_thread()
    app.run(host="0.0.0.0", port=config.FLASK_PORT, debug=False)
