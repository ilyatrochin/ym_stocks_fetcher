"""
fetch_ym_orders.py — модуль 2: скачивание заказов с Яндекс.Маркет (FBY).

Что делает:
1. POST /v1/businesses/{businessId}/orders с фильтрами по датам.
2. Пагинация через nextPageToken (limit 50).
3. Раскладывает каждый заказ на позиции (один заказ → N строк по items).
4. Upsert в ym_orders_raw: удаляет старые строки за окно по date_created,
   пишет свежие.
5. Rebuild sales_daily за то же окно:
   - qty_sold_created — по date_created, статус не в (CANCELLED, UNPAID,
     RESERVED, PLACING), не fake.
   - qty_sold_delivered — по date_delivered, статус == DELIVERED, не fake.
6. Алерт в Telegram.

Что НЕ делает (отдельные расширения):
- Возвраты (qty_returned всегда 0). Модуль 2.1.
- Комиссии и удержания (отдельный отчётный API). Модуль 3.

Запуск: python fetch_ym_orders.py [--dry-run] [--days N] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
Cron:   30 5 * * *  /opt/openclaw/skills/ym_stocks_fetcher/fetch_ym_orders.py
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

from sheets_helpers import batch_delete_rows, safe_append_rows

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
API_BASE = "https://api.partner.market.yandex.ru"
ENDPOINT = "/v1/businesses/{business_id}/orders"

PAGE_LIMIT = 50           # максимум по API
RATE_SLEEP = 0.3
HTTP_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0

DEFAULT_WINDOW_DAYS = 30  # API не разрешает больше 30 дней за один запрос
MARKETPLACE = "YM"

# Статусы, исключаемые из qty_sold_created
EXCLUDED_STATUSES = {"CANCELLED", "UNPAID", "RESERVED", "PLACING"}
DELIVERED_STATUS = "DELIVERED"


# ----------------------------------------------------------------------
# Структуры
# ----------------------------------------------------------------------
@dataclass
class OrderItemRow:
    fetch_timestamp: str
    order_id: str
    sku: str
    date_created: str          # YYYY-MM-DD
    date_delivered: str        # YYYY-MM-DD или ""
    status: str
    substatus: str
    qty: int
    price_per_unit: float
    subsidy: float
    total_price: float
    fake: str                  # "Да"/"Нет"
    warehouse_id: int | str
    region: str
    cancel_reason: str
    request_id: str

    def as_list(self) -> list:
        return [self.fetch_timestamp, self.order_id, self.sku,
                self.date_created, self.date_delivered,
                self.status, self.substatus,
                self.qty, self.price_per_unit, self.subsidy, self.total_price,
                self.fake, self.warehouse_id, self.region,
                self.cancel_reason, self.request_id]


@dataclass
class SalesDailyRow:
    date: str
    sku: str
    marketplace: str
    qty_sold_created: int
    qty_sold_delivered: int
    qty_returned: int
    revenue_created: float
    revenue_delivered: float

    def as_list(self) -> list:
        return [self.date, self.sku, self.marketplace,
                self.qty_sold_created, self.qty_sold_delivered, self.qty_returned,
                self.revenue_created, self.revenue_delivered]


@dataclass
class FetchResult:
    success: bool
    orders_count: int = 0
    items_count: int = 0
    sales_rows: int = 0
    period_from: str = ""
    period_to: str = ""
    cancelled_pct: float = 0.0
    errors: list[str] = field(default_factory=list)
    duration_sec: float = 0.0


# ----------------------------------------------------------------------
# YM API клиент
# ----------------------------------------------------------------------
class YMOrdersClient:
    def __init__(self, api_key: str, business_id: int,
                 debug_dir: str | None = None):
        self.api_key = api_key
        self.business_id = business_id
        self.debug_dir = Path(debug_dir) if debug_dir else None
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            logging.info("DEBUG: raw JSON responses will be saved to %s",
                         self.debug_dir)
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": api_key,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, url: str, **kwargs) -> dict:
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 420:
                    wait = RETRY_BACKOFF ** attempt * 5
                    logging.warning("Rate limit, waiting %.1fs", wait)
                    time.sleep(wait)
                    continue
                if 500 <= resp.status_code < 600:
                    wait = RETRY_BACKOFF ** attempt
                    logging.warning("Server error %d, retry in %.1fs",
                                    resp.status_code, wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            except requests.RequestException as e:
                last_err = e
                wait = RETRY_BACKOFF ** attempt
                logging.warning("Request failed: %s, retry in %.1fs", e, wait)
                time.sleep(wait)
        raise RuntimeError(f"Max retries exceeded: {last_err}")

    def fetch_orders(self, from_date: date, to_date: date) -> Iterator[dict]:
        """Итератор по заказам со всех страниц за указанный период."""
        url = f"{API_BASE}{ENDPOINT.format(business_id=self.business_id)}"
        page_token = None
        page_num = 0
        first_order_logged = False

        while True:
            page_num += 1
            params = {"limit": PAGE_LIMIT}
            if page_token:
                params["page_token"] = page_token

            # Актуальная структура запроса (yandex.ru/dev/market/partner-api):
            # POST /v1/businesses/{businessId}/orders
            # body: dateFrom/dateTo в ISO формате YYYY-MM-DD.
            # ВАЖНО: старая структура {"dates": {"fromDate": "DD-MM-YYYY"}}
            # API молча игнорировал и применял дефолт "последние 30 дней" —
            # отсюда и казавшийся "30-дневный лимит".
            body = {
                "dateFrom": from_date.isoformat(),
                "dateTo": to_date.isoformat(),
                "fake": False,
                "hasCis": False,
                # programTypes больше не отдельное поле — фильтрация FBY
                # происходит через campaignId или просто через состав заказа.
                # Если нужно строго FBY — можно отфильтровать на стороне
                # парсера: order.delivery.deliveryPartnerType == "YANDEX_MARKET"
            }

            data = self._request("POST", url, params=params, json=body)

            # ── DEBUG: сохраняем сырой JSON страницы ────────────────────
            if self.debug_dir:
                fname = (f"page_{from_date.isoformat()}_{to_date.isoformat()}"
                         f"_p{page_num:03d}.json")
                fpath = self.debug_dir / fname
                fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
                logging.info("DEBUG: saved raw page → %s (%d bytes)",
                             fpath.name, fpath.stat().st_size)

            # Структура ответа: result.orders или orders
            result_obj = data.get("result") or data
            orders = result_obj.get("orders", [])

            # Защита: если за длинный период не нашлось вообще ничего —
            # либо период полностью в "пустой зоне" (что редко за месяц+),
            # либо API не понял параметры и применил молча какой-то дефолт.
            period_days = (to_date - from_date).days + 1
            if page_num == 1 and len(orders) == 0 and period_days > 7:
                logging.warning(
                    "API returned 0 orders for period %s..%s (%d days). "
                    "Check that request params are accepted by the API. "
                    "Запросите с --debug-json чтобы увидеть что прислал сервер.",
                    from_date.isoformat(), to_date.isoformat(), period_days)

            # ── DEBUG: показать структуру первого заказа в логе ─────────
            if self.debug_dir and not first_order_logged and orders:
                first = orders[0]
                logging.info("DEBUG: first order keys: %s",
                             sorted(first.keys()))
                logging.info("DEBUG: first order id-candidates: "
                             "id=%r, orderId=%r, number=%r",
                             first.get("id"), first.get("orderId"),
                             first.get("number"))
                items = first.get("items") or []
                logging.info("DEBUG: first order has %d items", len(items))
                if items:
                    it = items[0]
                    logging.info("DEBUG: first item keys: %s",
                                 sorted(it.keys()))
                    logging.info("DEBUG: first item id/sku/price-candidates: "
                                 "offerId=%r, shopSku=%r, marketSku=%r, "
                                 "price=%r, buyerPrice=%r, count=%r",
                                 it.get("offerId"), it.get("shopSku"),
                                 it.get("marketSku"),
                                 it.get("price"), it.get("buyerPrice"),
                                 it.get("count"))
                first_order_logged = True

            # ── DEBUG: диапазон дат в полученных заказах
            # (проверка что фильтр сервера сработал) ────────────────────
            if self.debug_dir and orders:
                dates_in_response = []
                for o in orders:
                    cd = o.get("creationDate", "")
                    if cd:
                        dates_in_response.append(cd[:10])
                if dates_in_response:
                    logging.info(
                        "DEBUG page %d: %d orders, "
                        "creationDate range %s..%s (requested %s..%s)",
                        page_num, len(orders),
                        min(dates_in_response), max(dates_in_response),
                        from_date.isoformat(), to_date.isoformat())

            for order in orders:
                yield order

            paging = result_obj.get("paging") or {}
            page_token = paging.get("nextPageToken")
            if not page_token:
                logging.info("Pagination done after %d pages", page_num)
                break
            time.sleep(RATE_SLEEP)


# ----------------------------------------------------------------------
# Парсер
# ----------------------------------------------------------------------
def _parse_date(value: str | None) -> str:
    """Возвращает YYYY-MM-DD из ISO или DD-MM-YYYY."""
    if not value:
        return ""
    # DD-MM-YYYY (без времени)
    if len(value) == 10 and value[2] == "-":
        try:
            return datetime.strptime(value, "%d-%m-%Y").date().isoformat()
        except ValueError:
            pass
    # DD-MM-YYYY HH:MM:SS
    if len(value) == 19 and value[2] == "-":
        try:
            return datetime.strptime(value, "%d-%m-%Y %H:%M:%S").date().isoformat()
        except ValueError:
            pass
    # ISO 8601
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        return ""


def parse_order(order: dict, fetch_ts: str, request_id: str) -> list[OrderItemRow]:
    """Один заказ → список строк-позиций.

    Источник полей — реальный JSON YM Partner API v1/businesses/{id}/orders
    (FBY, май 2026):
      order.orderId           — uint, ID заказа
      order.creationDate      — ISO datetime с TZ
      order.updateDate        — ISO, последний апдейт (для date_delivered)
      order.status / substatus
      order.fake              — bool
      order.delivery.warehouseId  — str/int, ID склада отгрузки
      order.items[].offerId   — SKU магазина (наш)
      order.items[].count     — qty
      order.items[].prices.payment.value  — что заплатил покупатель
      order.items[].prices.subsidy.value  — субсидия от маркета
      order.items[].prices.cashback.value — кэшбек

    region покупателя в этом endpoint НЕ возвращается. Поле оставлено
    пустым осознанно. Для прогноза остатков нужен только warehouseId.
    """
    rows: list[OrderItemRow] = []
    # ── ID заказа: orderId — основное поле, fallback на старое id/externalOrderId
    order_id = str(order.get("orderId")
                   or order.get("id")
                   or order.get("externalOrderId")
                   or "")
    if not order_id:
        # Без order_id строка бесполезна (нельзя дедупнуть, сослаться)
        logging.warning("Skipping order without orderId: keys=%s",
                        sorted(order.keys()))
        return []

    status = order.get("status", "")
    substatus = order.get("substatus", "") or ""
    cancel_reason = substatus if status == "CANCELLED" else ""

    date_created = _parse_date(order.get("creationDate")
                               or order.get("createdAt"))
    date_delivered = ""
    if status == DELIVERED_STATUS:
        # В свежем API дата доставки лежит в delivery.dates.realDeliveryDate
        delivery_dates = (order.get("delivery") or {}).get("dates") or {}
        date_delivered = (
            _parse_date(delivery_dates.get("realDeliveryDate"))
            or _parse_date(order.get("updateDate"))
            or _parse_date(order.get("statusUpdateDate"))
        )

    fake = "Да" if order.get("fake") else "Нет"

    delivery = order.get("delivery") or {}
    # region в FBY-endpoint API не отдаёт → оставляем пустым
    region = ""

    warehouse_id: int | str = ""
    if "warehouseId" in delivery:
        warehouse_id = str(delivery["warehouseId"])
    elif isinstance(delivery.get("warehouse"), dict):
        warehouse_id = str(delivery["warehouse"].get("id", ""))

    # ── Дедупликация items внутри одного заказа ──────────────────────
    # Если в одном заказе встречается тот же SKU несколько раз
    # (теоретически возможно, на практике в FBY редко), складываем count.
    # Это убирает класс дублей даже если API когда-нибудь вернёт N
    # одинаковых items вместо одного с count=N.
    sku_buckets: dict[str, dict] = {}
    for item in order.get("items", []):
        sku = item.get("offerId") or item.get("shopSku") or ""
        if not sku:
            continue
        qty = int(item.get("count", 0) or 0)
        if qty <= 0:
            continue

        # ── Цена: prices.payment.value (новая структура) ────────────
        prices_obj = item.get("prices") or {}
        payment_obj = prices_obj.get("payment") or {}
        subsidy_obj = prices_obj.get("subsidy") or {}

        # payment.value — что реально заплатил покупатель за единицу
        price = float(payment_obj.get("value", 0) or 0)
        # subsidy.value — компенсация от Я.Маркета (на весь item)
        subsidy = float(subsidy_obj.get("value", 0) or 0)

        # Fallback на старые поля, если структура когда-то вернётся
        if price == 0 and "price" in item:
            price = float(item.get("price", 0) or 0)
        if subsidy == 0 and item.get("subsidies"):
            subsidy = sum(float(s.get("amount", 0) or 0)
                          for s in item["subsidies"])

        bucket = sku_buckets.get(sku)
        if bucket is None:
            sku_buckets[sku] = {
                "qty": qty,
                "price": price,           # цена за единицу
                "subsidy": subsidy,
            }
        else:
            # Объединяем — берём среднюю цену взвешенно
            old_qty = bucket["qty"]
            old_price = bucket["price"]
            bucket["price"] = round(
                (old_price * old_qty + price * qty) / (old_qty + qty), 2)
            bucket["qty"] = old_qty + qty
            bucket["subsidy"] += subsidy

    for sku, b in sku_buckets.items():
        qty = b["qty"]
        price = b["price"]
        subsidy = b["subsidy"]
        total = round(qty * price - subsidy, 2)

        rows.append(OrderItemRow(
            fetch_timestamp=fetch_ts,
            order_id=order_id,
            sku=sku,
            date_created=date_created,
            date_delivered=date_delivered,
            status=status,
            substatus=substatus,
            qty=qty,
            price_per_unit=price,
            subsidy=subsidy,
            total_price=total,
            fake=fake,
            warehouse_id=warehouse_id,
            region=region,
            cancel_reason=cancel_reason,
            request_id=request_id,
        ))
    return rows


# ----------------------------------------------------------------------
# Агрегация
# ----------------------------------------------------------------------
def aggregate_sales(items: list[OrderItemRow],
                    from_date: date, to_date: date) -> list[SalesDailyRow]:
    by_created: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"qty": 0, "revenue": 0.0})
    by_delivered: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"qty": 0, "revenue": 0.0})

    for it in items:
        if it.fake == "Да" or not it.sku:
            continue

        if it.status not in EXCLUDED_STATUSES and it.date_created:
            k = (it.date_created, it.sku)
            by_created[k]["qty"] += it.qty
            by_created[k]["revenue"] += it.total_price

        if it.status == DELIVERED_STATUS and it.date_delivered:
            k = (it.date_delivered, it.sku)
            by_delivered[k]["qty"] += it.qty
            by_delivered[k]["revenue"] += it.total_price

    all_keys = set(by_created) | set(by_delivered)
    out: list[SalesDailyRow] = []
    for k in sorted(all_keys):
        d_str, sku = k
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        if d < from_date or d > to_date:
            continue
        c = by_created.get(k, {"qty": 0, "revenue": 0.0})
        dl = by_delivered.get(k, {"qty": 0, "revenue": 0.0})
        out.append(SalesDailyRow(
            date=d_str, sku=sku, marketplace=MARKETPLACE,
            qty_sold_created=c["qty"],
            qty_sold_delivered=dl["qty"],
            qty_returned=0,
            revenue_created=round(c["revenue"], 2),
            revenue_delivered=round(dl["revenue"], 2),
        ))
    return out


# ----------------------------------------------------------------------
# Google Sheets
# ----------------------------------------------------------------------
class SheetsClient:
    HEADER_MARKERS = {
        "ym_orders_raw": "fetch_timestamp",
        "sales_daily": "date",
    }

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
        self.spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)

    def _find_header_row(self, ws, sheet_name: str) -> int:
        marker = self.HEADER_MARKERS[sheet_name]
        all_rows = ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row and row[0].strip().lower() == marker:
                return i + 1
        raise RuntimeError(f"Header row not found in {sheet_name}")

    def upsert_orders_raw(self, items: list[OrderItemRow],
                          from_date: date, to_date: date):
        ws = self.spreadsheet.worksheet("ym_orders_raw")
        header_row = self._find_header_row(ws, "ym_orders_raw")
        all_rows = ws.get_all_values()
        from_s, to_s = from_date.isoformat(), to_date.isoformat()

        to_delete = []
        for i, row in enumerate(all_rows[header_row:], start=header_row + 1):
            # date_created — 4-я колонка (индекс 3)
            if len(row) < 4:
                continue
            d = row[3]
            if d and from_s <= d <= to_s:
                to_delete.append(i)

        # Батчевое удаление одним запросом (вместо построчного)
        if to_delete:
            batch_delete_rows(ws, to_delete)

        if items:
            written = safe_append_rows(ws, [r.as_list() for r in items])
            logging.info("Wrote %d order rows", written)

    def upsert_sales_daily(self, rows: list[SalesDailyRow],
                           from_date: date, to_date: date):
        ws = self.spreadsheet.worksheet("sales_daily")
        header_row = self._find_header_row(ws, "sales_daily")
        all_rows = ws.get_all_values()
        from_s, to_s = from_date.isoformat(), to_date.isoformat()

        to_delete = []
        for i, row in enumerate(all_rows[header_row:], start=header_row + 1):
            if len(row) < 3:
                continue
            if row[2] == MARKETPLACE and row[0] and from_s <= row[0] <= to_s:
                to_delete.append(i)

        if to_delete:
            batch_delete_rows(ws, to_delete)

        if rows:
            written = safe_append_rows(ws, [r.as_list() for r in rows])
            logging.info("Wrote %d sales_daily rows", written)


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def send_telegram(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id, "text": message, "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        logging.error("Telegram send failed: %s", e)


def format_result(result: FetchResult) -> str:
    if result.success:
        return (
            f"✅ <b>YM orders</b> {result.period_from}..{result.period_to}\n"
            f"Заказов: {result.orders_count}, позиций: {result.items_count}\n"
            f"Дней-SKU в sales_daily: {result.sales_rows}\n"
            f"Отмен: {result.cancelled_pct:.1f}%, время: {result.duration_sec:.1f}с"
        )
    errs = "\n".join(f"• {e}" for e in result.errors[:5])
    return f"❌ <b>YM orders fetch failed</b>\n{errs}"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def run(from_date: date, to_date: date, dry_run: bool = False,
        debug_dir: str | None = None) -> FetchResult:
    started = time.time()
    fetch_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    result = FetchResult(success=False,
                         period_from=from_date.isoformat(),
                         period_to=to_date.isoformat())

    try:
        api_key = os.environ["YM_API_KEY"]
        business_id = int(os.environ["YM_BUSINESS_ID"])
        sheet_id = os.environ["GSHEET_ID"]
        creds_path = os.environ.get("GOOGLE_CREDS",
                                    "/opt/openclaw/secrets/gcp-sa.json")

        client = YMOrdersClient(api_key, business_id, debug_dir=debug_dir)
        sheets = SheetsClient(creds_path, sheet_id) if not dry_run else None

        # API не разрешает диапазон > 30 дней — режем на куски
        all_items: list[OrderItemRow] = []
        orders_count = 0
        cancelled_count = 0
        seen_order_ids: set[str] = set()
        dup_skipped = 0

        chunk_to = to_date
        while chunk_to >= from_date:
            chunk_from = max(from_date, chunk_to - timedelta(days=29))
            logging.info("Fetching orders %s..%s", chunk_from, chunk_to)
            for order in client.fetch_orders(chunk_from, chunk_to):
                oid = str(order.get("orderId")
                          or order.get("id")
                          or order.get("externalOrderId") or "")
                if oid and oid in seen_order_ids:
                    # Тот же заказ уже попался (граница окон, повтор страницы)
                    dup_skipped += 1
                    continue
                if oid:
                    seen_order_ids.add(oid)

                orders_count += 1
                if order.get("status") == "CANCELLED":
                    cancelled_count += 1
                all_items.extend(parse_order(order, fetch_ts, request_id))
            chunk_to = chunk_from - timedelta(days=1)

        if dup_skipped:
            logging.info("Dedup: skipped %d duplicate orderId across pages/chunks",
                         dup_skipped)
        logging.info("Unique orders: %d, total order-item rows: %d",
                     orders_count, len(all_items))

        result.orders_count = orders_count
        result.items_count = len(all_items)
        if orders_count > 0:
            result.cancelled_pct = (cancelled_count / orders_count) * 100

        sales_rows = aggregate_sales(all_items, from_date, to_date)
        result.sales_rows = len(sales_rows)

        if sheets:
            sheets.upsert_orders_raw(all_items, from_date, to_date)
            sheets.upsert_sales_daily(sales_rows, from_date, to_date)
        else:
            logging.info("DRY RUN: %d raw items, %d sales rows",
                         len(all_items), len(sales_rows))
            for r in sales_rows[:10]:
                logging.info("  %s", r.as_list())

        result.success = True

    except KeyError as e:
        result.errors.append(f"Missing env var: {e}")
        logging.error("Config error: %s", e)
    except Exception as e:
        result.errors.append(str(e))
        logging.exception("Fetch failed")

    result.duration_sec = time.time() - started
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                   help="Сколько дней назад от сегодня (по умолчанию 30)")
    p.add_argument("--from", dest="from_date", default=None,
                   help="Дата начала YYYY-MM-DD (переопределяет --days)")
    p.add_argument("--to", dest="to_date", default=None,
                   help="Дата конца YYYY-MM-DD (по умолчанию сегодня)")
    p.add_argument("--debug-json", dest="debug_json", default=None,
                   metavar="DIR",
                   help="Сохранить сырой JSON каждой страницы в эту папку "
                        "(для диагностики структуры ответа API). "
                        "Также включает подробное логирование "
                        "ключей первого заказа.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    to_date = (datetime.strptime(args.to_date, "%Y-%m-%d").date()
               if args.to_date else date.today())
    if args.from_date:
        from_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    else:
        from_date = to_date - timedelta(days=args.days - 1)

    if from_date > to_date:
        logging.error("from_date > to_date")
        sys.exit(1)

    result = run(from_date, to_date, dry_run=args.dry_run,
                 debug_dir=args.debug_json)

    bot_token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_ADMIN_CHAT_ID")
    if not args.dry_run and bot_token and chat_id:
        send_telegram(bot_token, chat_id, format_result(result))

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
