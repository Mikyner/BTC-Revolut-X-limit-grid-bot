"""
Revolut X API klient pro Limit Order Grid Bot
===============================================
Oproti market botu přibývají:
- place_limit_order() - GTC limit order (maker, 0% fee)
- cancel_order()      - zrušení živého orderu
- get_active_orders() - seznam živých orderů na burze
"""

import base64
import json
import logging
import time
import uuid
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from nacl.signing import SigningKey

import config

logger = logging.getLogger("revolutx_client")


class RevolutXClient:
    BASE_URL = "https://revx.revolut.com/api/1.0"

    def __init__(self):
        self.api_key = config.REVOLUTX_API_KEY
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "btc-limit-grid-bot/1.0",
            "Content-Type": "application/json"
        })
        self._signing_key = self._load_signing_key()

    def _load_signing_key(self):
        key_path = config.REVOLUTX_PRIVATE_KEY_PATH
        if not key_path or not Path(key_path).exists():
            return None
        pem_data = Path(key_path).read_bytes()
        private_key_obj = serialization.load_pem_private_key(
            pem_data, password=None, backend=default_backend()
        )
        raw_private = private_key_obj.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return SigningKey(raw_private)

    def get_current_price(self, symbol=None):
        symbol = symbol or config.PAIR
        url = f"{self.BASE_URL}/public/order-book/{symbol}"
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()["data"]
        if not data["asks"] or not data["bids"]:
            raise RuntimeError(f"Order book pro {symbol} je prázdný")
        best_ask = float(data["asks"][-1]["p"])
        best_bid = float(data["bids"][0]["p"])
        return round((best_ask + best_bid) / 2, 2)

    def _sign(self, timestamp, method, path, query, body):
        message = f"{timestamp}{method}{path}{query}{body}".encode("utf-8")
        signed = self._signing_key.sign(message)
        return base64.b64encode(signed.signature).decode()

    def _authed_request(self, method, path, params=None, json_body=None):
        if not self.api_key or not self._signing_key:
            raise RuntimeError("Chybí API klíč nebo privátní klíč (DRY_RUN=false vyžaduje klíče).")

        full_path = f"/api/1.0{path}"
        query_string = "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
        body_str = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
        timestamp = str(int(time.time() * 1000))
        signature = self._sign(timestamp, method.upper(), full_path, query_string, body_str)

        headers = {
            "X-Revx-API-Key": self.api_key,
            "X-Revx-Timestamp": timestamp,
            "X-Revx-Signature": signature,
        }
        url = self.BASE_URL + path
        resp = self.session.request(
            method, url, params=params,
            data=body_str if json_body is not None else None,
            headers=headers, timeout=10
        )
        if not resp.ok:
            try:
                err = resp.json()
                raise RuntimeError(f"Revolut X API error {resp.status_code}: {err.get('message')}")
            except ValueError:
                resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def place_limit_order(self, symbol: str, side: str, price: float,
                          base_size: float = None, quote_size: float = None) -> dict:
        """
        Umístí GTC limit order — maker order, 0% fee na Revolut X.

        Pro BUY obvykle zadáváme quote_size (EUR kolik utratit).
        Pro SELL zadáváme base_size (BTC kolik prodat).

        GTC = Good Till Cancelled — order zůstane aktivní dokud:
        - se nevyplní (cena dosáhne limitu)
        - ho ručně nezrušíme přes cancel_order()
        """
        order_config = {"price": f"{price:.2f}"}
        if quote_size is not None:
            order_config["quote_size"] = f"{quote_size:.2f}"
        if base_size is not None:
            order_config["base_size"] = f"{base_size:.8f}"

        client_order_id = str(uuid.uuid4())
        body = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "order_configuration": {
                "limit": order_config
                # time_in_force není potřeba explicitně — GTC je default pro limit ordery
            },
        }
        result = self._authed_request("POST", "/orders", json_body=body)
        data = result["data"]
        logger.info(
            f"[LIMIT ORDER] {side.upper()} @ {price:.2f} EUR | "
            f"venue_id={data.get('venue_order_id')} | state={data.get('state')}"
        )
        return data

    def cancel_order(self, venue_order_id: str) -> bool:
        """Zruší živý order na burze. Vrátí True pokud úspěšně, False pokud nenalezen."""
        try:
            self._authed_request("DELETE", f"/orders/{venue_order_id}")
            logger.info(f"[CANCEL] Order {venue_order_id} zrušen")
            return True
        except RuntimeError as e:
            if "404" in str(e) or "not found" in str(e).lower():
                logger.warning(f"[CANCEL] Order {venue_order_id} nenalezen (možná již vyplněn)")
                return False
            raise

    def get_active_orders(self, symbol=None) -> list:
        """Stáhne seznam živých orderů z burzy."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        result = self._authed_request("GET", "/orders/active", params=params or None)
        if not result or "data" not in result:
            return []
        return result["data"]

    def get_order(self, venue_order_id: str) -> dict:
        """Stáhne detail konkrétního orderu."""
        result = self._authed_request("GET", f"/orders/{venue_order_id}")
        return result["data"]

    def cancel_all_orders(self, symbol=None) -> bool:
        """Zruší všechny aktivní ordery najednou."""
        try:
            params = {"symbol": symbol} if symbol else None
            self._authed_request("DELETE", "/orders", params=params)
            logger.info(f"[CANCEL ALL] Všechny aktivní ordery zrušeny")
            return True
        except Exception as e:
            logger.error(f"[CANCEL ALL] Chyba: {e}")
            return False
