"""
Limit Order Grid Bot Runner (PRO VERSION)
"""

import logging
import threading
import time

import config
import bot.database as db
from bot.grid_engine import build_grid, is_price_outside_grid
from bot.revolutx_client import RevolutXClient
import bot.telegram_notify as telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("runner")

revx = RevolutXClient()
_stop_event = threading.Event()
settings_cache = {}


def _current_settings():
    return {
        "order_size_eur": db.get_setting_float("order_size_eur", config.ORDER_SIZE_EUR),
        "grid_levels": db.get_setting_int("grid_levels", config.GRID_LEVELS),
        "grid_range_percent": db.get_setting_float("grid_range_percent", config.GRID_RANGE_PERCENT),
        "grid_bias_percent": db.get_setting_float("grid_bias_percent", config.GRID_BIAS_PERCENT),
        "max_price_deviation_percent": db.get_setting_float(
            "max_price_deviation_percent", config.MAX_PRICE_DEVIATION_PERCENT
        ),
        "compounding_enabled": db.get_setting("compounding_enabled", "false") == "true",
        "compounding_percent": db.get_setting_float("compounding_percent", 3.0),
        "max_position_eur": db.get_setting_float("max_position_eur", 0.0),
    }


def _order_size(settings, current_price):
    if not settings["compounding_enabled"]:
        return settings["order_size_eur"]
    wallet = db.get_wallet()
    total = wallet["eur_balance"] + wallet["btc_balance"] * current_price
    size = total * (settings["compounding_percent"] / 100)
    if settings["max_position_eur"] > 0:
        size = min(size, settings["max_position_eur"])
    return round(size, 2)


def sync_filled_orders(settings):
    if config.DRY_RUN:
        return

    try:
        active_on_exchange = revx.get_active_orders(symbol=config.PAIR)
        active_venue_ids = {o.get("venue_order_id") or o.get("id") for o in active_on_exchange}
        db_open_orders = db.get_all_open_orders()

        for db_order in db_open_orders:
            vid = db_order["venue_order_id"]
            if vid not in active_venue_ids:
                try:
                    order_detail = revx.get_order(vid)
                    state = order_detail.get("state") or order_detail.get("status", "")
                    filled_qty = float(order_detail.get("filled_quantity") or 0)

                    if state == "filled" or (state in ("cancelled", "rejected") and filled_qty > 0):
                        _process_filled_order(db_order, order_detail, settings)
                    elif state in ("cancelled", "rejected", "replaced"):
                        db.mark_order_cancelled(vid)
                        logger.info(f"Order {vid} označen jako {state} (čisté storno bez plnění)")
                except Exception as e:
                    logger.warning(f"Nepodařilo se zjistit stav orderu {vid}: {e}")

    except Exception as e:
        logger.error(f"Chyba při synchronizaci orderů: {e}")


def _process_filled_order(db_order, order_detail, settings):
    vid = db_order["venue_order_id"]
    fill_price = float(order_detail.get("average_fill_price") or db_order["grid_price"])
    fill_btc = float(order_detail.get("filled_quantity") or 0)
    fill_eur = float(order_detail.get("filled_amount") or 0)

    if fill_eur <= 0 and fill_btc > 0:
        fill_eur = fill_btc * fill_price

    db.mark_order_filled(vid, fill_price, fill_btc, fill_eur)
    wallet = db.get_wallet()

    if db_order["side"] == "buy":
        new_eur = wallet["eur_balance"] - fill_eur
        new_btc = wallet["btc_balance"] + fill_btc
        db.update_wallet(new_eur, new_btc)

        grid_levels_count = settings["grid_levels"]
        grid_range_pct = settings["grid_range_percent"]
        half_range = db_order["grid_price"] * (grid_range_pct / 100) / 2
        grid_step = (2 * half_range) / (grid_levels_count - 1)
        target_sell = round(db_order["grid_price"] + grid_step, 2)

        logger.info(
            f"BUY vyplněn @ {fill_price:.2f} EUR (grid {db_order['grid_price']:.2f}), "
            f"BTC {fill_btc:.8f} | SELL target: {target_sell:.2f} EUR"
        )
        _place_sell_order(db_order, fill_btc, target_sell)
        telegram.notify_trade("buy", fill_price, fill_btc, dry_run=False,
                              slippage_eur=fill_price - db_order["grid_price"])

    elif db_order["side"] == "sell":
        buy_order = None
        if db_order.get("linked_buy_order_id"):
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM exchange_orders WHERE id=?",
                    (db_order["linked_buy_order_id"],)
                ).fetchone()
                buy_order = dict(row) if row else None

        eur_received = fill_eur if fill_eur > 0 else fill_btc * fill_price
        new_eur = wallet["eur_balance"] + eur_received
        new_btc = wallet["btc_balance"] - fill_btc
        db.update_wallet(new_eur, new_btc)

        eur_spent = buy_order["fill_eur"] if buy_order and buy_order.get("fill_eur") else db_order.get("size_eur", 0)
        profit = eur_received - eur_spent if eur_spent else None
        buy_price = buy_order["fill_price"] if buy_order else db_order["grid_price"]

        db.record_completed_trade(
            buy_order_id=db_order.get("linked_buy_order_id"),
            sell_order_id=db_order["id"],
            buy_price=buy_price,
            sell_price=fill_price,
            btc_amount=fill_btc,
            eur_spent=eur_spent,
            eur_received=eur_received,
            dry_run=False,
        )
        logger.info(
            f"SELL vyplněn @ {fill_price:.2f} EUR (target {db_order['grid_price']:.2f}), "
            f"zisk {profit:+.4f} EUR"
        )
        telegram.notify_trade("sell", fill_price, fill_btc, profit_eur=profit,
                              dry_run=False, slippage_eur=fill_price - db_order["grid_price"])


def _place_sell_order(buy_db_order, btc_amount, target_price):
    if config.DRY_RUN:
        return
    try:
        result = revx.place_limit_order(
            symbol=config.PAIR,
            side="sell",
            price=target_price,
            base_size=btc_amount,
        )
        venue_id = result.get("venue_order_id")
        client_id = result.get("client_order_id")
        db.create_exchange_order(
            venue_order_id=venue_id,
            client_order_id=client_id,
            side="sell",
            grid_price=target_price,
            size_btc=btc_amount,
            linked_buy_order_id=buy_db_order["id"],
        )
        logger.info(f"SELL limit order umístěn @ {target_price:.2f} EUR | {venue_id}")
    except Exception as e:
        logger.error(f"Nepodařilo se umístit SELL order @ {target_price:.2f}: {e}")


def ensure_buy_orders_placed(current_price, settings):
    """
    Sliding window / capital recycling (PRO VERSION).
    - Respektuje SELL pozice (double-buy fix)
    - Detekuje reálný nedostatek financí na burze (422 fix)
    - Konzervativní recyklace s bufferem
    """
    grid_levels = db.get_grid_levels()
    levels_below = sorted(
        [l for l in grid_levels if l["price"] < current_price],
        key=lambda l: l["price"],
        reverse=True,
    )
    if not levels_below:
        return

    size_eur = _order_size(settings, current_price)
    if size_eur <= 0:
        return

    wallet = db.get_wallet()
    open_buys = db.get_open_buy_orders()
    covered_prices = {round(o["grid_price"], 2) for o in open_buys}

    # Double-buy fix: zahrnout grid ceny kde už čeká SELL order
    open_sells = db.get_open_sell_orders()
    if open_sells:
        buy_ids = [o["linked_buy_order_id"] for o in open_sells if o.get("linked_buy_order_id")]
        if buy_ids:
            with db.get_conn() as conn:
                placeholders = ",".join("?" * len(buy_ids))
                rows = conn.execute(
                    f"SELECT grid_price FROM exchange_orders WHERE id IN ({placeholders})",
                    buy_ids
                ).fetchall()
                for r in rows:
                    covered_prices.add(round(r["grid_price"], 2))

    locked_eur = sum(o.get("size_eur") or 0 for o in open_buys)
    free_eur = wallet["eur_balance"] - locked_eur

    total_budget = wallet["eur_balance"]
    max_orders = int(total_budget / size_eur)
    target_levels = {round(l["price"], 2) for l in levels_below[:max_orders]}
    buffer = max(5, int(max_orders * 0.2))
    extended_levels = {round(l["price"], 2) for l in levels_below[:max_orders + buffer]}

    placed = 0
    cancelled = 0

    # Krok 1: recyklace (od nejnižších pater)
    for order in sorted(open_buys, key=lambda o: o["grid_price"]):
        op = round(order["grid_price"], 2)
        is_outside_extended = op not in extended_levels
        is_in_buffer_but_need_cash = (op not in target_levels) and (free_eur < size_eur)

        if is_outside_extended or is_in_buffer_but_need_cash:
            vid = order["venue_order_id"]
            reclaimed = order.get("size_eur") or 0
            if config.DRY_RUN:
                db.mark_order_cancelled(vid)
            else:
                try:
                    revx.cancel_order(vid)
                except Exception as e:
                    logger.warning(f"[SW] Nepodařilo se zrušit {vid}: {e}")
                    continue
            covered_prices.discard(op)
            free_eur += reclaimed
            cancelled += 1
            logger.info(f"[SW] Recyklace: zrušen BUY @ {op:.2f} (uvolněno {reclaimed:.2f} EUR)")

    # Krok 2: umísti nové ordery
    for level in levels_below[:max_orders]:
        price = round(level["price"], 2)
        if price in covered_prices:
            continue
        if free_eur < size_eur:
            break

        if config.DRY_RUN:
            venue_id = f"paper-buy-{price:.2f}-{int(time.time())}"
            db.create_exchange_order(
                venue_order_id=venue_id,
                client_order_id=venue_id,
                side="buy",
                grid_price=price,
                size_eur=size_eur,
            )
            covered_prices.add(price)
            free_eur -= size_eur
            placed += 1
        else:
            try:
                result = revx.place_limit_order(
                    symbol=config.PAIR,
                    side="buy",
                    price=price,
                    quote_size=size_eur,
                )
                venue_id = result.get("venue_order_id")
                client_id = result.get("client_order_id")
                db.create_exchange_order(
                    venue_order_id=venue_id,
                    client_order_id=client_id,
                    side="buy",
                    grid_price=price,
                    size_eur=size_eur,
                )
                covered_prices.add(price)
                free_eur -= size_eur
                placed += 1
                logger.info(f"BUY limit order umístěn @ {price:.2f} EUR")
            except Exception as e:
                logger.error(f"Nepodařilo se umístit BUY order @ {price:.2f}: {e}")
                if "Insufficient balance" in str(e) or "422" in str(e):
                    logger.warning("[SW] Nedostatek financí na burze — synchronizuji peněženku")
                    current_open = db.get_open_buy_orders()
                    real_locked = sum(o.get("size_eur") or 0 for o in current_open)
                    db.update_wallet(real_locked, wallet["btc_balance"])
                    break

    if placed > 0 or cancelled > 0:
        logger.info(f"Sliding window: umístěno {placed} BUY orderů, recyklováno {cancelled} nízkých")


def check_paper_sells(current_price, last_price):
    if not config.DRY_RUN:
        return

    open_sells = db.get_open_sell_orders()
    for order in open_sells:
        target = order["grid_price"]
        if current_price < target:
            continue

        btc_amount = order.get("size_btc") or 0
        if btc_amount <= 0:
            continue

        buy_order = None
        if order.get("linked_buy_order_id"):
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM exchange_orders WHERE id=?",
                    (order["linked_buy_order_id"],)
                ).fetchone()
                buy_order = dict(row) if row else None

        eur_received = btc_amount * target
        eur_spent = (buy_order.get("size_eur") or buy_order.get("fill_eur") or 0) if buy_order else eur_received
        profit = eur_received - eur_spent

        db.mark_order_filled(order["venue_order_id"], target, btc_amount, eur_received)
        wallet = db.get_wallet()
        db.update_wallet(wallet["eur_balance"] + eur_received, wallet["btc_balance"] - btc_amount)

        buy_price = buy_order["grid_price"] if buy_order else target
        db.record_completed_trade(
            buy_order_id=order.get("linked_buy_order_id"),
            sell_order_id=order["id"],
            buy_price=buy_price,
            sell_price=target,
            btc_amount=btc_amount,
            eur_spent=eur_spent,
            eur_received=eur_received,
            dry_run=True,
        )
        logger.info(f"[PAPER] SELL {btc_amount:.8f} BTC @ {target:.2f} EUR | zisk {profit:+.4f} EUR")
        telegram.notify_trade("sell", target, btc_amount, profit_eur=profit, dry_run=True,
                              slippage_eur=0.0)

    if last_price is None:
        return

    open_buys = db.get_open_buy_orders()
    for order in open_buys:
        buy_price = order["grid_price"]
        if not (last_price > buy_price >= current_price):
            continue

        size_eur = order.get("size_eur") or 0
        if size_eur <= 0:
            continue
        btc_amount = size_eur / buy_price

        db.mark_order_filled(order["venue_order_id"], buy_price, btc_amount, size_eur)
        wallet = db.get_wallet()
        db.update_wallet(wallet["eur_balance"] - size_eur, wallet["btc_balance"] + btc_amount)

        grid_levels_count = settings_cache.get("grid_levels", config.GRID_LEVELS)
        grid_range_pct = settings_cache.get("grid_range_percent", config.GRID_RANGE_PERCENT)
        half_range = buy_price * (grid_range_pct / 100) / 2
        grid_step = (2 * half_range) / (grid_levels_count - 1)
        target_sell = round(buy_price + grid_step, 2)

        sell_venue_id = f"paper-sell-{buy_price:.2f}-{int(time.time()*1000)}"
        db.create_exchange_order(
            venue_order_id=sell_venue_id,
            client_order_id=sell_venue_id,
            side="sell",
            grid_price=target_sell,
            size_btc=btc_amount,
            linked_buy_order_id=order["id"],
        )
        logger.info(f"[PAPER] BUY vyplněn {btc_amount:.8f} BTC @ {buy_price:.2f} | SELL target @ {target_sell:.2f}")
        telegram.notify_trade("buy", buy_price, btc_amount, dry_run=True, slippage_eur=0.0)


def do_recenter(current_price, settings, triggered_by="manual"):
    logger.info(f"Re-center mřížky @ {current_price:.2f} EUR (důvod: {triggered_by})")

    if not config.DRY_RUN:
        open_buys = db.get_open_buy_orders()
        for order in open_buys:
            try:
                revx.cancel_order(order["venue_order_id"])
            except Exception as e:
                logger.warning(f"Nepodařilo se zrušit order {order['venue_order_id']}: {e}")

    with db.get_conn() as conn:
        conn.execute("""
            UPDATE exchange_orders SET status='cancelled', cancelled_at=?
            WHERE status='open' AND side='buy'
        """, (time.time(),))

    new_prices = build_grid(
        current_price,
        settings["grid_levels"],
        settings["grid_range_percent"],
        settings["grid_bias_percent"],
    )
    db.replace_grid_levels(new_prices)
    db.set_last_price(None)

    logger.info(f"Grid re-centrován: {len(new_prices)} úrovní, {new_prices[0]:.0f}–{new_prices[-1]:.0f} EUR")
    telegram.notify_bot_paused(f"Grid re-centrován @ {current_price:.2f} EUR")


def run_cycle():
    global settings_cache
    settings = _current_settings()
    settings_cache = settings
    current_price = revx.get_current_price()

    last_price = db.get_last_price()
    db.set_last_price(current_price)

    if not db.get_grid_levels():
        prices = build_grid(
            current_price,
            settings["grid_levels"],
            settings["grid_range_percent"],
            settings["grid_bias_percent"],
        )
        db.replace_grid_levels(prices)
        logger.info(f"Grid inicializován @ {current_price:.2f} EUR | {len(prices)} úrovní")

    sync_filled_orders(settings)

    if config.DRY_RUN:
        check_paper_sells(current_price, last_price)

    grid_levels = db.get_grid_levels()
    if grid_levels:
        prices = [l["price"] for l in grid_levels]
        grid_min, grid_max = min(prices), max(prices)
        max_dev = settings["max_price_deviation_percent"]
        dev_low = (grid_min - current_price) / grid_min * 100
        dev_high = (current_price - grid_max) / grid_max * 100

        if dev_low > max_dev or dev_high > max_dev:
            msg = f"Cena {current_price:.2f} EUR mimo grid ({grid_min:.0f}–{grid_max:.0f} EUR)."
            logger.warning(msg)
            db.set_bot_running(False, paused_reason=msg)
            telegram.notify_bot_paused(msg)
            db.record_equity_snapshot(current_price, **_wallet_snapshot())
            return current_price

    ensure_buy_orders_placed(current_price, settings)
    db.record_equity_snapshot(current_price, **_wallet_snapshot())
    return current_price


def _wallet_snapshot():
    w = db.get_wallet()
    return {"eur_balance": w["eur_balance"], "btc_balance": w["btc_balance"]}


def check_daily_summary():
    if not config.TELEGRAM_NOTIFY_DAILY_SUMMARY:
        return
    now = time.localtime()
    today_str = time.strftime("%Y-%m-%d", now)
    current_hm = time.strftime("%H:%M", now)
    if current_hm < config.TELEGRAM_DAILY_SUMMARY_TIME:
        return
    if db.get_last_summary_date() == today_str:
        return
    try:
        wallet = db.get_wallet()
        price = revx.get_current_price()
        total = wallet["eur_balance"] + wallet["btc_balance"] * price
        adjustments = db.get_total_cash_adjustments()
        start = config.PAPER_STARTING_BALANCE_EUR + adjustments
        pnl_pct = ((total - start) / start * 100) if start else 0
        realized = db.get_realized_profit()
        trades_today = db.get_trade_count_since(time.time() - 86400)
        open_sells = db.get_open_sell_orders()
        telegram.notify_daily_summary(
            total_value_eur=total,
            pnl_percent=pnl_pct,
            realized_profit_eur=realized,
            unrealized_pnl_eur=0.0,
            trade_count_today=trades_today,
            open_positions=len(open_sells),
        )
        db.set_last_summary_date(today_str)
        logger.info("Denní souhrn odeslán")
    except Exception as e:
        logger.error(f"Nepodařilo se odeslat denní souhrn: {e}")


def main_loop():
    settings = _current_settings()
    logger.info(
        f"Spouštím LIMIT ORDER bota | pár={config.DISPLAY_PAIR} | DRY_RUN={config.DRY_RUN} | "
        f"grid_levels={settings['grid_levels']} | range={settings['grid_range_percent']}% | "
        f"fee=0% (maker limit ordery)"
    )
    db.init_db()
    db.init_wallet()
    db.set_bot_running(True)
    telegram.notify_bot_started()

    while not _stop_event.is_set():
        try:
            status = db.get_bot_status()
            if status["running"]:
                price = run_cycle()
                logger.info(f"Cyklus OK | cena={price:.2f} EUR")
            else:
                try:
                    price = revx.get_current_price()
                    last_price_paused = db.get_last_price()
                    db.set_last_price(price)
                    sync_filled_orders(settings)
                    if config.DRY_RUN:
                        check_paper_sells(price, last_price_paused)
                    db.record_equity_snapshot(price, **_wallet_snapshot())
                except Exception as e:
                    logger.error(f"Chyba v pozastaveném stavu: {e}")
                logger.info("Bot pozastaven, sell ordery stále sleduji...")

            check_daily_summary()
        except Exception as e:
            logger.exception(f"Chyba v hlavní smyčce: {e}")

        _stop_event.wait(config.POLL_INTERVAL_SECONDS)


def start_background_thread():
    thread = threading.Thread(target=main_loop, daemon=True)
    thread.start()
    return thread


def stop():
    _stop_event.set()
