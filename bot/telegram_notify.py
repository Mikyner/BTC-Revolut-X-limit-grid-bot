"""
Telegram notifikace
======================
Posílá zprávy o důležitých událostech bota na Telegram.

Použití Telegram Bot API: https://core.telegram.org/bots/api#sendmessage
Žádná závislost navíc není potřeba - používá se obyčejný HTTP POST přes `requests`.

Pokud TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID nejsou nastavené, všechny funkce
v tomhle modulu tiše nedělají nic (bot funguje dál normálně, jen bez notifikací).
"""

import logging

import requests

import config

logger = logging.getLogger("telegram")


def _is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _send(text: str):
    """Pošle zprávu na Telegram. Chyby se jen zalogují, nikdy nepřeruší běh bota -
    notifikace jsou vedlejší efekt, ne kritická část obchodní logiky."""
    if not _is_configured():
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not resp.ok:
            logger.warning(f"Telegram notifikace selhala ({resp.status_code}): {resp.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"Telegram notifikace selhala: {e}")


def _fmt_eur(value: float) -> str:
    return f"{value:,.2f} €".replace(",", " ")


# ---------------------------------------------------------------------
# Konkrétní typy notifikací
# ---------------------------------------------------------------------

def _fmt_czk(value: float, rate: float = 24.27) -> str:
    """Přibližný přepočet EUR → CZK. Rate se aktualizuje denně přes ČNB API v dashboardu."""
    return f"{value * rate:,.1f} Kč".replace(",", " ")


def notify_trade(side: str, price: float, btc_amount: float, profit_eur: float = None,
                 dry_run: bool = True, slippage_eur: float = None):
    if not config.TELEGRAM_NOTIFY_TRADES:
        return
    mode_tag = "📝 PAPER" if dry_run else "💰 LIVE"
    price_czk = _fmt_czk(price)

    if side == "buy":
        text = (
            f"{mode_tag} 🔵🛒 <b>NÁKUP</b>\n"
            f"{btc_amount:.6f} BTC\n"
            f"Cena: {_fmt_eur(price)} ({price_czk})"
        )
    else:
        profit_str = f"{'+' if (profit_eur or 0) >= 0 else ''}{_fmt_eur(profit_eur)}" if profit_eur is not None else "—"
        profit_czk_str = f" ({_fmt_czk(profit_eur)})" if profit_eur is not None else ""
        emoji = "🟢💵" if (profit_eur or 0) >= 0 else "🔴💵"
        text = (
            f"{mode_tag} {emoji} <b>PRODEJ</b>\n"
            f"{btc_amount:.6f} BTC\n"
            f"Cena: {_fmt_eur(price)} ({price_czk})\n"
            f"Zisk: {profit_str}{profit_czk_str}"
        )
    _send(text)


def notify_bot_paused(reason: str):
    if not config.TELEGRAM_NOTIFY_PAUSE:
        return
    _send(f"⏸ <b>Bot pozastaven</b>\n{reason}")


def notify_pause_reminder(
    paused_since_minutes: int,
    current_price: float,
    open_positions: list,
    total_unrealized_eur: float,
):
    """
    Připomínka, že bot je pořád pozastavený - posílá se opakovaně
    (TELEGRAM_PAUSE_REMINDER_MINUTES), dokud uživatel grid neresetuje nebo
    pozice ručně nevyřeší. Obsahuje konkrétní doporučení podle stavu pozic,
    ne jen holé "pořád čekám".
    """
    if not config.TELEGRAM_NOTIFY_PAUSE:
        return

    hours = paused_since_minutes / 60

    if not open_positions:
        # žádné otevřené pozice - není co řešit, jen mřížka čeká na re-center
        text = (
            f"⏸ <b>Bot je pozastavený už {hours:.1f} h</b>\n"
            f"Aktuální cena: {_fmt_eur(current_price)}\n"
            f"Žádné otevřené pozice - klidně klikni na <b>Re-center gridu</b> v "
            f"dashboardu, jakmile budeš chtít pokračovat."
        )
        _send(text)
        return

    worst = min(open_positions, key=lambda p: p["pnl_percent"])
    avg_pnl = sum(p["pnl_percent"] for p in open_positions) / len(open_positions)

    if avg_pnl <= -5:
        recommendation = (
            "📉 Pozice jsou v průměru dost ve ztrátě. Pokud věříš, že trend bude "
            "pokračovat, zvaž ruční prodej nebo počkej na stop-loss (pokud ho má "
            "pozice nastavený). Re-center gridu otevřené pozice nezavře, jen "
            "přidá nové nákupní úrovně kolem aktuální ceny."
        )
    elif avg_pnl >= 0:
        recommendation = (
            "📈 Pozice jsou v plusu nebo na nule. Mřížka je jen mimo svůj rozsah - "
            "klidně zvaž <b>Re-center gridu</b>. Staré pozice se nezruší, dál "
            "čekají na svůj původní cíl prodeje bez ohledu na novou mřížku."
        )
    else:
        recommendation = (
            "Pozice jsou mírně ve ztrátě. Žádná akce není nutná - jen měj na "
            "paměti, že stop-loss (pokud ho pozice má) zasáhne sám, pokud "
            "ztráta poroste."
        )

    text = (
        f"⏸ <b>Bot je pozastavený už {hours:.1f} h</b>\n"
        f"Aktuální cena: {_fmt_eur(current_price)}\n"
        f"Otevřených pozic: {len(open_positions)}, nerealizovaný P&amp;L: "
        f"{'+' if total_unrealized_eur >= 0 else ''}{_fmt_eur(total_unrealized_eur)}\n"
        f"Nejhorší pozice: {worst['pnl_percent']:+.1f}%\n\n"
        f"{recommendation}"
    )
    _send(text)


def notify_stop_loss(buy_price: float, current_price: float, pnl_percent: float, profit_eur: float):
    if not config.TELEGRAM_NOTIFY_STOP_LOSS:
        return
    _send(
        f"🛑 <b>STOP-LOSS spuštěn</b>\n"
        f"Pozice nakoupena @ {_fmt_eur(buy_price)}\n"
        f"Prodáno @ {_fmt_eur(current_price)} ({pnl_percent:+.1f}%)\n"
        f"Realizovaná ztráta: {_fmt_eur(profit_eur)}"
    )


def notify_low_capital(eur_balance: float, needed_eur: float):
    """Upozornění, že bot nemá dost EUR na další nákup - nákup byl přeskočen.
    Posílá se max. jednou za 30 minut (cooldown řeší execution.py), ne při
    každém cyklu, dokud kapitálu chybí."""
    _send(
        f"⚠️ <b>Nedostatek kapitálu</b>\n"
        f"Bot chtěl nakoupit za {_fmt_eur(needed_eur)}, ale má jen {_fmt_eur(eur_balance)}.\n"
        f"Nákup byl přeskočen. Pošli kapitál na účet a přidej ho přes "
        f"<b>Vklad/výběr</b> v dashboardu, nebo nech bota čekat na uvolnění "
        f"kapitálu z budoucích prodejů."
    )


def notify_daily_summary(
    total_value_eur: float,
    pnl_percent: float,
    realized_profit_eur: float,
    unrealized_pnl_eur: float,
    trade_count_today: int,
    open_positions: int,
):
    if not config.TELEGRAM_NOTIFY_DAILY_SUMMARY:
        return
    sign = "+" if pnl_percent >= 0 else ""
    text = (
        f"📊 <b>Denní souhrn</b>\n\n"
        f"Hodnota portfolia: {_fmt_eur(total_value_eur)} ({sign}{pnl_percent:.2f}% od startu)\n"
        f"Realizovaný zisk: {_fmt_eur(realized_profit_eur)}\n"
        f"Nerealizovaný P&amp;L: {_fmt_eur(unrealized_pnl_eur)} ({open_positions} otevřených pozic)\n"
        f"Obchodů za posledních 24h: {trade_count_today}"
    )
    _send(text)


def notify_bot_started():
    """Volitelné uvítací hlášení při startu kontejneru - užitečné pro ověření, že notifikace fungují."""
    if not _is_configured():
        return
    mode = "PAPER TRADING" if config.DRY_RUN else "⚠️ LIVE OBCHODOVÁNÍ"
    _send(f"🤖 <b>Bot spuštěn</b>\nRežim: {mode}\nPár: {config.DISPLAY_PAIR}")


def notify_cash_adjustment(amount_eur: float, note: str, new_balance_eur: float):
    """Notifikace o ručním vkladu/výběru kapitálu."""
    kind = "💵 Vklad" if amount_eur > 0 else "💸 Výběr"
    text = (
        f"{kind} kapitálu\n"
        f"Částka: {_fmt_eur(abs(amount_eur))}\n"
        f"Nový EUR zůstatek bota: {_fmt_eur(new_balance_eur)}"
    )
    if note:
        text += f"\nPoznámka: {note}"
    _send(text)


def notify_panic_sell(sold_count: int, failed_count: int, total_profit_eur: float):
    """Notifikace po dokončení Panic Sell - vždy se pošle, bez ohledu na
    jiné notifikační přepínače, protože jde o kritickou bezpečnostní akci."""
    sign = "+" if total_profit_eur >= 0 else ""
    text = (
        f"🚨 <b>PANIC SELL dokončen</b>\n"
        f"Prodáno pozic: {sold_count}\n"
    )
    if failed_count > 0:
        text += f"⚠️ Nepodařilo se prodat: {failed_count} (zkontroluj log a dashboard)\n"
    text += f"Celkový realizovaný výsledek: {sign}{_fmt_eur(total_profit_eur)}"
    _send(text)
