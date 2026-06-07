"""
fetch_ym_supplies.py — модуль 3: товары в пути (FBY supply-requests).

Что делает:
1. POST /v2/campaigns/{campaignId}/supply-requests — список заявок на поставку.
2. Для каждой АКТИВНОЙ заявки (статус не FINISHED/CANCELLED/INVALID/
   CANCELLATION_REQUESTED, тип SUPPLY) дёргает
   POST /v2/campaigns/{campaignId}/supply-requests/items — товары в заявке.
3. Агрегирует по (sku, marketplace=YM) → qty_in_transit и ближайшую
   ожидаемую дату приёмки.
4. Пишет ТЕКУЩИЙ СРЕЗ в лист `supplies_planned` (старые строки YM удаляются
   целиком, пишутся новые — без истории по дням).
5. Алерт в Telegram.

Что НЕ делает:
- Не хранит историю (для аналитики динамики поставок — это модуль будущего).
- Не разворачивает мультипоставки в дочерние, т.к. для прогноза важна
  суммарная цифра по SKU независимо от иерархии заявок. Если заявка-родитель
  и заявка-дочка одновременно активны и обе вернутся в API — это нормально
  суммируется (Яндекс не дублирует товары).

Запуск: python fetch_ym_supplies.py [--dry-run] [--verbose]
Cron:   45 5 * * *  /opt/openclaw/skills/ym_stocks_fetcher/fetch_ym_supplies.py

Запускается ПОСЛЕ fetch_ym_orders (05:30) и ДО fetch_ym_stocks (06:00).
forecast_stocks.py (06:30) затем читает supplies_planned.

Требования к API-Key:
- supplies-management:read-only (отдельный scope, см. DEPLOY.md шаг 2)
- ИЛИ all-methods:read-only / all-methods
"""

from __future__ import annotations
import argparse
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Iterator

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

from sheets_helpers import batch_delete_rows, safe_append_rows

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
API_BASE = "https://api.partner.market.yandex.ru"
REQUESTS_ENDPOINT = "/v2/campaigns/{campaign_id}/supply-requests"
ITEMS_ENDPOINT = "/v2/campaigns/{campaign_id}/supply-requests/items"

REQUESTS_PAGE_LIMIT = 100   # макс. для supply-requests
ITEMS_PAGE_LIMIT = 500      # макс. для items
RATE_SLEEP = 0.6
HTTP_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0

MARKETPLACE = "YM"

# Статусы, которые ИСКЛЮЧАЕМ из расчёта "товар едет".
# Решение (см. CLAUDE.md, раздел 4): берём всё АКТИВНОЕ, включая CREATED
# (создана, но ещё не одобрена) — т.к. это уже реальный план поставки.
EXCLUDED_STATUSES = {
    "FINISHED",                 # уже на складе — это остаток
    "CANCELLED",                # отменена
    "INVALID",                  # ошибка обработки
    "CANCELLATION_REQUESTED",   # запрошена отмена, в прогноз не включаем
}

# Берём только заявки типа SUPPLY (поставка). WITHDRAW/UTILIZATION — это
# вывоз/утилизация, для прогноза остатков они отрицательны и считаются
# отдельно (пока вне скоупа).
RELEVANT_TYPES = {"SUPPLY"}


# ----------------------------------------------------------------------
# Структуры
# ----------------------------------------------------------------------
@dataclass
class SupplyRequest:
    """Заголовок заявки на поставку."""
    request_id: int                 # внутренний API-id
    marketplace_request_id: str     # номер в кабинете (для человека)
    status: str
    subtype: str
    target_warehouse_name: str
    target_warehouse_id: int | str
    requested_date: str             # YYYY-MM-DD ожидаемая дата на складе
    transit_warehouse_name: str
    updated_at: str


@dataclass
class PlannedRow:
    """Агрегированная строка для supplies_planned."""
    fetch_timestamp: str
    sku: str
    marketplace: str
    qty_in_transit: int             # сумма (planCount - factCount) по активным
    qty_planned_total: int          # сумма planCount по активным (для справки)
    qty_already_accepted: int       # сумма factCount по активным
    nearest_arrival_date: str       # ближайшая requestedDate среди заявок
    requests_count: int             # сколько активных заявок содержат этот SKU
    request_ids: str                # "12345; 12346" — для отладки
    statuses: str                   # "ARRIVED_TO_SERVICE; CREATED"
    target_warehouses: str          # "Софьино; Томилино"

    def as_list(self) -> list:
        return [self.fetch_timestamp, self.sku, self.marketplace,
                self.qty_in_transit, self.qty_planned_total,
                self.qty_already_accepted,
                self.nearest_arrival_date, self.requests_count,
                self.request_ids, self.statuses, self.target_warehouses]


@dataclass
class RequestItemRow:
    """Строка для supplies_requests: одна заявка × один SKU."""
    fetch_timestamp: str
    request_id: int
    marketplace_request_id: str
    status: str
    marketplace: str
    target_warehouse: str
    requested_date: str
    sku: str
    plan_qty: int
    fact_qty: int
    qty_in_transit: int

    def as_list(self) -> list:
        return [
            self.fetch_timestamp, self.request_id,
            self.marketplace_request_id, self.status,
            self.marketplace, self.target_warehouse,
            self.requested_date, self.sku,
            self.plan_qty, self.fact_qty, self.qty_in_transit,
        ]


@dataclass
class FetchResult:
    success: bool = False
    total_requests_seen: int = 0
    active_requests: int = 0
    skus_in_transit: int = 0
    total_qty_in_transit: int = 0
    errors: list[str] = field(default_factory=list)
    duration_sec: float = 0.0


# ----------------------------------------------------------------------
# YM API клиент
# ----------------------------------------------------------------------
class YMSuppliesClient:
    def __init__(self, api_key: str, campaign_id: int):
        self.campaign_id = campaign_id
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": api_key,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, url: str, **kwargs) -> dict:
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url,
                                            timeout=HTTP_TIMEOUT, **kwargs)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 420:
                    wait = RETRY_BACKOFF ** attempt * 5
                    logging.warning("Rate limit (420), waiting %.1fs", wait)
                    time.sleep(wait)
                    continue
                if 500 <= resp.status_code < 600:
                    wait = RETRY_BACKOFF ** attempt
                    logging.warning("Server error %d, retry in %.1fs",
                                    resp.status_code, wait)
                    time.sleep(wait)
                    continue
                # 401/403/404 — не ретраим, чёткая ошибка
                raise RuntimeError(
                    f"HTTP {resp.status_code}: {resp.text[:500]}")
            except requests.RequestException as e:
                last_err = e
                wait = RETRY_BACKOFF ** attempt
                logging.warning("Request failed: %s, retry in %.1fs", e, wait)
                time.sleep(wait)
        raise RuntimeError(f"Max retries exceeded: {last_err}")

    def iter_requests(self) -> Iterator[dict]:
        """Итератор по всем заявкам (без фильтрации — фильтруем на нашей
        стороне). Пагинация через nextPageToken."""
        url = f"{API_BASE}{REQUESTS_ENDPOINT.format(campaign_id=self.campaign_id)}"
        page_token = None
        page_num = 0
        # Пустое тело = без фильтров. Если в будущем понадобится фильтровать
        # по дате — добавить requestDateFrom/requestDateTo.
        body: dict = {}
        while True:
            page_num += 1
            params = {"limit": REQUESTS_PAGE_LIMIT}
            if page_token:
                params["page_token"] = page_token
            data = self._request("POST", url, params=params, json=body)
            result_obj = data.get("result") or {}
            for req in result_obj.get("requests", []):
                yield req
            page_token = (result_obj.get("paging") or {}).get("nextPageToken")
            if not page_token:
                logging.info("Requests pagination done after %d pages", page_num)
                break
            time.sleep(RATE_SLEEP)

    def iter_items(self, request_id: int) -> Iterator[dict]:
        """Итератор по товарам в заявке. Пагинация."""
        url = f"{API_BASE}{ITEMS_ENDPOINT.format(campaign_id=self.campaign_id)}"
        page_token = None
        body = {"requestId": request_id}
        while True:
            params = {"limit": ITEMS_PAGE_LIMIT}
            if page_token:
                params["page_token"] = page_token
            data = self._request("POST", url, params=params, json=body)
            result_obj = data.get("result") or {}
            for item in result_obj.get("items", []):
                yield item
            page_token = (result_obj.get("paging") or {}).get("nextPageToken")
            if not page_token:
                break
            time.sleep(RATE_SLEEP)


# ----------------------------------------------------------------------
# Парсинг
# ----------------------------------------------------------------------
def parse_request(req: dict) -> SupplyRequest | None:
    """Из ответа API → SupplyRequest. Возвращает None для невалидных."""
    id_obj = req.get("id") or {}
    request_id = id_obj.get("id")
    if request_id is None:
        return None

    target = req.get("targetLocation") or {}
    transit = req.get("transitLocation") or {}
    target_addr = (target.get("address") or {})  # на будущее, пока не пишем
    _ = target_addr

    # requestedDate приходит как ISO datetime, нам нужна только дата
    raw_date = target.get("requestedDate") or ""
    try:
        arrival = (datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                   .date().isoformat()) if raw_date else ""
    except (ValueError, AttributeError):
        arrival = ""

    return SupplyRequest(
        request_id=int(request_id),
        marketplace_request_id=str(id_obj.get("marketplaceRequestId", "")),
        status=req.get("status", ""),
        subtype=req.get("subtype", ""),
        target_warehouse_name=target.get("name", "") or "",
        target_warehouse_id=target.get("serviceId", "") or "",
        requested_date=arrival,
        transit_warehouse_name=transit.get("name", "") or "",
        updated_at=req.get("updatedAt", "") or "",
    )


def is_active_supply(req: SupplyRequest) -> bool:
    """Фильтр: только активные заявки на поставку (не вывоз/утилизация,
    не отменённые, не завершённые)."""
    if req.status in EXCLUDED_STATUSES:
        return False
    return True  # тип SUPPLY уже отфильтрован отдельно — см. iter_active_supplies


# ----------------------------------------------------------------------
# Сбор и агрегация
# ----------------------------------------------------------------------
def iter_active_supplies(client: YMSuppliesClient) -> Iterator[SupplyRequest]:
    """Стримит активные заявки типа SUPPLY (применяет оба фильтра)."""
    for raw in client.iter_requests():
        # фильтр по типу — отбрасываем WITHDRAW/UTILIZATION
        if raw.get("type") not in RELEVANT_TYPES:
            continue
        parsed = parse_request(raw)
        if not parsed:
            continue
        if not is_active_supply(parsed):
            continue
        yield parsed


def aggregate(
    client: YMSuppliesClient, fetch_ts: str,
) -> tuple[list[PlannedRow], list[RequestItemRow], FetchResult]:
    """Собирает все активные заявки, тянет товары в каждой, агрегирует
    по SKU. Возвращает (planned_rows, request_item_rows, result)."""

    result = FetchResult()
    request_item_rows: list[RequestItemRow] = []
    # ключ: sku → накопленные данные
    agg: dict[str, dict] = defaultdict(lambda: {
        "qty_in_transit": 0,
        "qty_planned_total": 0,
        "qty_already_accepted": 0,
        "nearest_arrival": None,  # date | None
        "request_ids": [],
        "statuses": set(),
        "warehouses": set(),
    })

    total_seen = 0
    active = 0
    for req in iter_active_supplies(client):
        total_seen += 1  # это уже только SUPPLY, но т.к. iter_active_supplies
        # уже фильтрует — для total_requests_seen считаем total на уровне выше
        active += 1
        logging.info("Active request #%d (%s): status=%s, eta=%s, wh=%s",
                     req.request_id, req.marketplace_request_id,
                     req.status, req.requested_date or "?",
                     req.target_warehouse_name)

        for item in client.iter_items(req.request_id):
            sku = (item.get("offerId") or "").strip()
            if not sku:
                continue
            counters = item.get("counters") or {}
            plan = int(counters.get("planCount") or 0)
            fact = int(counters.get("factCount") or 0)
            # "В пути" = запланировано минус уже принято.
            # Если plan < fact (избыток на приёмке) — не вычитаем в минус.
            in_transit = max(plan - fact, 0)

            # Собираем детализацию по каждой заявке+SKU для бота
            request_item_rows.append(RequestItemRow(
                fetch_timestamp=fetch_ts,
                request_id=req.request_id,
                marketplace_request_id=req.marketplace_request_id,
                status=req.status,
                marketplace=MARKETPLACE,
                target_warehouse=req.target_warehouse_name,
                requested_date=req.requested_date,
                sku=sku,
                plan_qty=plan,
                fact_qty=fact,
                qty_in_transit=in_transit,
            ))

            a = agg[sku]
            a["qty_in_transit"] += in_transit
            a["qty_planned_total"] += plan
            a["qty_already_accepted"] += fact
            a["request_ids"].append(req.request_id)
            a["statuses"].add(req.status)
            if req.target_warehouse_name:
                a["warehouses"].add(req.target_warehouse_name)

            # Ближайшая дата прибытия — минимум среди заявок, в которых
            # этот SKU встретился. Заявки без даты пропускаем.
            if req.requested_date:
                try:
                    d = date.fromisoformat(req.requested_date)
                except ValueError:
                    d = None
                if d is not None:
                    if a["nearest_arrival"] is None or d < a["nearest_arrival"]:
                        a["nearest_arrival"] = d

    # Считаем total_requests_seen отдельно (без повторного запроса не получится,
    # т.к. итератор уже отработал; для метрики достаточно active).
    result.total_requests_seen = active  # на самом деле "активных SUPPLY"
    result.active_requests = active

    # Превращаем агрегат в строки
    rows: list[PlannedRow] = []
    total_qty = 0
    for sku, a in sorted(agg.items()):
        # Если в пути 0 (всё уже принято в этих заявках) — всё равно пишем
        # строку для прозрачности. Если хочется убрать — раскомментируй:
        # if a["qty_in_transit"] == 0: continue
        nearest = a["nearest_arrival"].isoformat() if a["nearest_arrival"] else ""
        total_qty += a["qty_in_transit"]
        rows.append(PlannedRow(
            fetch_timestamp=fetch_ts,
            sku=sku,
            marketplace=MARKETPLACE,
            qty_in_transit=a["qty_in_transit"],
            qty_planned_total=a["qty_planned_total"],
            qty_already_accepted=a["qty_already_accepted"],
            nearest_arrival_date=nearest,
            requests_count=len(a["request_ids"]),
            request_ids="; ".join(str(x) for x in a["request_ids"]),
            statuses="; ".join(sorted(a["statuses"])),
            target_warehouses="; ".join(sorted(a["warehouses"])),
        ))

    result.skus_in_transit = sum(1 for r in rows if r.qty_in_transit > 0)
    result.total_qty_in_transit = total_qty
    return rows, request_item_rows, result


# ----------------------------------------------------------------------
# Google Sheets I/O
# ----------------------------------------------------------------------
class SheetsClient:
    # Заголовок ищем по первой колонке — структура листа:
    # 1-3 строки intro + 1 заголовок + данные.
    HEADER_MARKER = "fetch_timestamp"
    SHEET_NAME = "supplies_planned"
    REQUESTS_SHEET = "supplies_requests"
    REQUESTS_HEADERS = [
        "fetch_timestamp", "request_id", "marketplace_request_id",
        "status", "marketplace", "target_warehouse", "requested_date",
        "sku", "plan_qty", "fact_qty", "qty_in_transit",
    ]

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            credentials_path, scope)
        self.spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)

    def _ensure_sheet(self):
        """Если листа нет — создаёт его с заголовками и одной intro-строкой.
        Это полезно при первом запуске на свежей таблице."""
        try:
            ws = self.spreadsheet.worksheet(self.SHEET_NAME)
            return ws
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Sheet '%s' not found — creating", self.SHEET_NAME)
            ws = self.spreadsheet.add_worksheet(
                title=self.SHEET_NAME, rows=200, cols=15)
            intro = [
                ["supplies_planned — товары в пути на склады FBY. "
                 "Полностью переписывается при каждом fetch_ym_supplies. "
                 "qty_in_transit = planCount - factCount по активным заявкам."],
                [],
                ["fetch_timestamp", "sku", "marketplace",
                 "qty_in_transit", "qty_planned_total", "qty_already_accepted",
                 "nearest_arrival_date", "requests_count",
                 "request_ids", "statuses", "target_warehouses"],
            ]
            safe_append_rows(ws, intro)
            return ws

    def _find_header_row(self, ws) -> int:
        all_rows = ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row and row[0].strip().lower() == self.HEADER_MARKER:
                return i + 1  # 1-indexed
        raise RuntimeError(
            f"Header row '{self.HEADER_MARKER}' not found in {self.SHEET_NAME}")

    def replace_all(self, rows: list[PlannedRow]):
        """ТЕКУЩИЙ СРЕЗ: удаляем все строки данных для marketplace=YM,
        пишем свежие. Старые YM-строки уходят полностью, история не
        копится (так договорились — см. CLAUDE.md, новый раздел)."""
        ws = self._ensure_sheet()
        header_row = self._find_header_row(ws)
        all_values = ws.get_all_values()

        to_delete: list[int] = []
        # колонка marketplace — 3-я (индекс 2)
        for i, row in enumerate(all_values[header_row:],
                                start=header_row + 1):
            if len(row) >= 3 and row[2] == MARKETPLACE:
                to_delete.append(i)

        if to_delete:
            batch_delete_rows(ws, to_delete)
            logging.info("Removed %d stale rows for %s",
                         len(to_delete), MARKETPLACE)

        if rows:
            written = safe_append_rows(ws, [r.as_list() for r in rows])
            logging.info("Wrote %d planned rows", written)

    def replace_requests(self, rows: list[RequestItemRow]):
        """Полностью переписывает supplies_requests текущим срезом.
        Создаёт лист при первом запуске."""
        try:
            ws = self.spreadsheet.worksheet(self.REQUESTS_SHEET)
        except gspread.exceptions.WorksheetNotFound:
            logging.info("Creating sheet '%s'", self.REQUESTS_SHEET)
            ws = self.spreadsheet.add_worksheet(
                title=self.REQUESTS_SHEET, rows=1000, cols=11)
            safe_append_rows(ws, [
                ["supplies_requests — детализация поставок (заявка × SKU). "
                 "Переписывается fetch_ym_supplies.py. "
                 "Читается tg_supplies_bot.py."],
                [],
                self.REQUESTS_HEADERS,
            ])

        # Найти строку заголовка
        all_values = ws.get_all_values()
        header_row_idx = None
        for i, row in enumerate(all_values):
            if row and row[0].strip().lower() == self.HEADER_MARKER:
                header_row_idx = i + 1  # 1-indexed
                break

        if header_row_idx is None:
            safe_append_rows(ws, [self.REQUESTS_HEADERS])
            all_values = ws.get_all_values()
            header_row_idx = len(all_values)

        # Удалить все строки данных после заголовка
        to_delete = list(range(header_row_idx + 1, len(all_values) + 1))
        if to_delete:
            batch_delete_rows(ws, to_delete)
            logging.info("Removed %d stale rows from %s",
                         len(to_delete), self.REQUESTS_SHEET)

        if rows:
            written = safe_append_rows(ws, [r.as_list() for r in rows])
            logging.info("Wrote %d request-item rows to %s",
                         written, self.REQUESTS_SHEET)


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def send_telegram(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logging.error("Telegram send failed: %s", e)


def format_message(result: FetchResult) -> str:
    if result.success:
        return (
            f"📦 <b>YM supplies (в пути)</b>\n"
            f"Активных заявок: {result.active_requests}\n"
            f"SKU в пути: {result.skus_in_transit}, "
            f"всего штук: {result.total_qty_in_transit}\n"
            f"Время: {result.duration_sec:.1f}с"
        )
    errs = "\n".join(f"• {e}" for e in result.errors[:5])
    return f"❌ <b>YM supplies failed</b>\n{errs}"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def run(dry_run: bool = False) -> FetchResult:
    started = time.time()
    fetch_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    request_id_local = f"req-{uuid.uuid4().hex[:8]}"
    logging.info("Run %s start (dry_run=%s)", request_id_local, dry_run)

    result = FetchResult()

    try:
        api_key = os.environ["YM_API_KEY"]
        campaign_id = int(os.environ["YM_CAMPAIGN_ID"])
        sheet_id = os.environ["GSHEET_ID"]
        creds_path = os.environ.get("GOOGLE_CREDS",
                                    "/opt/openclaw/secrets/gcp-sa.json")

        client = YMSuppliesClient(api_key, campaign_id)
        rows, request_rows, agg_result = aggregate(client, fetch_ts)
        # Сливаем метрики
        result.total_requests_seen = agg_result.total_requests_seen
        result.active_requests = agg_result.active_requests
        result.skus_in_transit = agg_result.skus_in_transit
        result.total_qty_in_transit = agg_result.total_qty_in_transit

        if dry_run:
            logging.info("DRY RUN: %d planned rows, %d request-item rows",
                         len(rows), len(request_rows))
            for r in rows[:10]:
                logging.info("  planned: %s", r.as_list())
            for r in request_rows[:10]:
                logging.info("  request: %s", r.as_list())
        else:
            sheets = SheetsClient(creds_path, sheet_id)
            sheets.replace_all(rows)
            sheets.replace_requests(request_rows)

        result.success = True

    except KeyError as e:
        result.errors.append(f"Missing env var: {e}")
        logging.error("Config error: %s", e)
    except Exception as e:
        result.errors.append(str(e))
        logging.exception("Supplies fetch failed")

    result.duration_sec = time.time() - started
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Не писать в Sheets, только логировать")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    result = run(dry_run=args.dry_run)

    bot_token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_ADMIN_CHAT_ID")
    if not args.dry_run and bot_token and chat_id:
        send_telegram(bot_token, chat_id, format_message(result))

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
