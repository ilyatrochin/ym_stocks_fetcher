"""
fetch_ym_stocks.py — модуль OpenClaw для скачивания остатков с Яндекс.Маркет (FBY).

Что делает:
1. POST /v2/campaigns/{campaignId}/offers/stocks с withTurnover=true
2. Пагинация через nextPageToken
3. Парсит ответ: по складам → по товарам → по типам остатков
4. Пишет в Google Sheets:
   - ym_stocks_raw — сырые данные (все типы остатков)
   - stock_marketplace — агрегированный снапшот (qty_full = FIT, физический запас)
5. Отправляет алерт в Telegram, если что-то критично или произошёл сбой

Запуск: python fetch_ym_stocks.py [--dry-run] [--date YYYY-MM-DD]
Cron:   0 6 * * *  /opt/openclaw/skills/ym_stocks_fetcher/fetch_ym_stocks.py

Зависимости: requests, gspread, oauth2client, python-dotenv
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any, Iterator

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

from sheets_helpers import batch_delete_rows, safe_append_rows

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
API_BASE = "https://api.partner.market.yandex.ru"
ENDPOINT = "/v2/campaigns/{campaign_id}/offers/stocks"
WAREHOUSES_ENDPOINT = "/v2/warehouses"

PAGE_LIMIT = 200       # максимум по API
RATE_SLEEP = 0.6       # пауза между страницами, сек (для безопасности)
HTTP_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0    # экспоненциальная пауза при 5xx / 420

# Маркер для последующих модулей
MARKETPLACE = "YM"

# ----------------------------------------------------------------------
# Структуры
# ----------------------------------------------------------------------
@dataclass
class StockRow:
    """Строка для ym_stocks_raw (полный набор полей)."""
    fetch_timestamp: str
    date: str
    sku: str
    warehouse_id: int
    qty_available: int = 0
    qty_fit: int = 0
    qty_freeze: int = 0
    qty_defect: int = 0
    qty_expired: int = 0
    qty_quarantine: int = 0
    qty_utilization: int = 0
    turnover_grade: str = ""
    turnover_days: float | str = ""
    mp_updated_at: str = ""
    request_id: str = ""

    def as_list(self) -> list:
        return [self.fetch_timestamp, self.date, self.sku, self.warehouse_id,
                self.qty_available, self.qty_fit, self.qty_freeze, self.qty_defect,
                self.qty_expired, self.qty_quarantine, self.qty_utilization,
                self.turnover_grade, self.turnover_days, self.mp_updated_at,
                self.request_id]


@dataclass
class SnapshotRow:
    """Строка для stock_marketplace (агрегированный снапшот)."""
    date: str
    sku: str
    marketplace: str
    warehouse_id: int
    warehouse_name: str
    qty_full: int
    qty_to_client: int = 0
    qty_from_client: int = 0
    turnover_grade: str = ""
    turnover_days: float | str = ""
    mp_updated_at: str = ""

    def as_list(self) -> list:
        return [self.date, self.sku, self.marketplace, self.warehouse_id,
                self.warehouse_name, self.qty_full, self.qty_to_client,
                self.qty_from_client, self.turnover_grade, self.turnover_days,
                self.mp_updated_at]


@dataclass
class FetchResult:
    """Результат всего fetch для отправки в Telegram."""
    success: bool
    total_skus: int = 0
    total_warehouses: int = 0
    total_rows: int = 0
    new_warehouses: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_sec: float = 0.0


# ----------------------------------------------------------------------
# YM API клиент
# ----------------------------------------------------------------------
class YMClient:
    def __init__(self, api_key: str, campaign_id: int):
        self.api_key = api_key
        self.campaign_id = campaign_id
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": api_key,
            "Content-Type": "application/json",
        })

    def _request_with_retries(self, method: str, url: str, **kwargs) -> dict:
        """Повторы на 420 (rate limit) и 5xx с экспоненциальным backoff."""
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code == 420:
                    wait = RETRY_BACKOFF ** attempt * 5
                    logging.warning("Rate limit (420), waiting %.1fs", wait)
                    time.sleep(wait)
                    continue
                if 500 <= resp.status_code < 600:
                    wait = RETRY_BACKOFF ** attempt
                    logging.warning("Server error %d, retry in %.1fs", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                # 4xx (не 420) — не ретраим
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            except requests.RequestException as e:
                last_err = e
                wait = RETRY_BACKOFF ** attempt
                logging.warning("Request failed: %s, retry in %.1fs", e, wait)
                time.sleep(wait)
        raise RuntimeError(f"Max retries exceeded: {last_err}")

    def fetch_warehouses(self) -> list[dict]:
        """GET /v2/warehouses — список складов Яндекса."""
        url = f"{API_BASE}{WAREHOUSES_ENDPOINT}"
        data = self._request_with_retries("GET", url)
        # Структура: result.warehouses[]
        return data.get("result", {}).get("warehouses", [])

    def fetch_stocks_pages(self) -> Iterator[dict]:
        """Итератор по страницам остатков. Каждый yield — это сырой ответ."""
        url = f"{API_BASE}{ENDPOINT.format(campaign_id=self.campaign_id)}"
        page_token = None
        page_num = 0
        while True:
            page_num += 1
            params = {"limit": PAGE_LIMIT}
            if page_token:
                params["page_token"] = page_token
            body = {"withTurnover": True}
            data = self._request_with_retries("POST", url, params=params, json=body)
            yield data
            page_token = data.get("result", {}).get("paging", {}).get("nextPageToken")
            if not page_token:
                logging.info("Pagination complete after %d pages", page_num)
                break
            time.sleep(RATE_SLEEP)


# ----------------------------------------------------------------------
# Парсер ответа
# ----------------------------------------------------------------------
STOCK_TYPE_FIELDS = {
    "AVAILABLE":    "qty_available",
    "FIT":          "qty_fit",
    "FREEZE":       "qty_freeze",
    "DEFECT":       "qty_defect",
    "EXPIRED":      "qty_expired",
    "QUARANTINE":   "qty_quarantine",
    "UTILIZATION":  "qty_utilization",
}


def parse_warehouses_response(response: dict) -> Iterator[tuple[int, list[dict]]]:
    """Извлекает warehouses из одного ответа API."""
    warehouses = response.get("result", {}).get("warehouses", [])
    for wh in warehouses:
        wh_id = wh.get("warehouseId")
        offers = wh.get("offers", [])
        if wh_id is None:
            continue
        yield wh_id, offers


def parse_offer(
    offer: dict,
    warehouse_id: int,
    fetch_ts: str,
    snapshot_date: str,
    request_id: str,
) -> StockRow:
    """Преобразует один offer из ответа API в StockRow."""
    row = StockRow(
        fetch_timestamp=fetch_ts,
        date=snapshot_date,
        sku=offer.get("offerId", ""),
        warehouse_id=warehouse_id,
        mp_updated_at=offer.get("updatedAt", ""),
        request_id=request_id,
    )

    # Остатки по типам
    for stock in offer.get("stocks", []):
        stype = stock.get("type")
        count = stock.get("count", 0)
        field_name = STOCK_TYPE_FIELDS.get(stype)
        if field_name:
            setattr(row, field_name, count)

    # Оборачиваемость
    turnover = offer.get("turnoverSummary") or {}
    row.turnover_grade = turnover.get("turnover", "")
    days = turnover.get("turnoverDays")
    row.turnover_days = days if days is not None else ""

    return row


# ----------------------------------------------------------------------
# Google Sheets I/O
# ----------------------------------------------------------------------
class SheetsClient:
    def __init__(self, credentials_path: str, spreadsheet_id: str):
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
        client = gspread.authorize(creds)
        self.spreadsheet = client.open_by_key(spreadsheet_id)

    def get_warehouse_names(self) -> dict[int, str]:
        """Загружает справочник mp_warehouses → {warehouse_id: name} только для YM."""
        ws = self.spreadsheet.worksheet("mp_warehouses")
        rows = ws.get_all_values()
        # Пропускаем интро + заголовок (5 строк intro + 1 заголовок = 6 верхних строк)
        names = {}
        for row in rows[6:]:
            if len(row) < 5:
                continue
            try:
                wh_id = int(row[0])
            except (ValueError, IndexError):
                continue
            if len(row) >= 2 and row[1] == "YM":
                names[wh_id] = row[2] if len(row) > 2 else ""
        return names

    def upsert_warehouses(self, ym_warehouses: list[dict]) -> list[int]:
        """Добавляет новые склады YM в mp_warehouses, возвращает список новых ID."""
        ws = self.spreadsheet.worksheet("mp_warehouses")
        existing = self.get_warehouse_names()
        new_ids = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        rows_to_append = []
        for wh in ym_warehouses:
            wh_id = wh.get("id") or wh.get("warehouseId")
            if wh_id is None:
                continue
            wh_id = int(wh_id)
            name = wh.get("name", "")
            if wh_id not in existing:
                new_ids.append(wh_id)
                rows_to_append.append([wh_id, "YM", name, "", "Да", now, "авто-добавлен ботом"])

        if rows_to_append:
            safe_append_rows(ws, rows_to_append)
            logging.info("Added %d new warehouses to mp_warehouses", len(rows_to_append))

        return new_ids

    def append_raw_rows(self, rows: list[StockRow]):
        if not rows:
            return
        ws = self.spreadsheet.worksheet("ym_stocks_raw")
        safe_append_rows(ws, [r.as_list() for r in rows])

    def upsert_snapshot_rows(self, rows: list[SnapshotRow], snapshot_date: str):
        """Идемпотентность: удаляем существующие строки на эту дату для YM,
        потом пишем новые. Удаление — одним batch-запросом."""
        ws = self.spreadsheet.worksheet("stock_marketplace")
        all_values = ws.get_all_values()
        # Структура: 3 строки intro + 1 заголовок + данные
        header_row_idx = 4  # 1-indexed → строка 5 это первый data row
        rows_to_delete = []
        for i, row in enumerate(all_values[header_row_idx:], start=header_row_idx + 1):
            if len(row) >= 3 and row[0] == snapshot_date and row[2] == MARKETPLACE:
                rows_to_delete.append(i)

        # Батчевое удаление одним запросом (вместо построчного)
        if rows_to_delete:
            batch_delete_rows(ws, rows_to_delete)
            logging.info("Stale snapshot rows for %s/%s: %d",
                         snapshot_date, MARKETPLACE, len(rows_to_delete))

        if rows:
            written = safe_append_rows(ws, [r.as_list() for r in rows])
            logging.info("Wrote %d snapshot rows", written)


# ----------------------------------------------------------------------
# Telegram уведомления
# ----------------------------------------------------------------------
def send_telegram(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        logging.warning("Telegram credentials missing, skip notification")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        logging.error("Failed to send Telegram: %s", e)


def format_result_message(result: FetchResult, snapshot_date: str) -> str:
    if result.success:
        msg = (
            f"✅ <b>YM stocks fetched</b> {snapshot_date}\n"
            f"SKU: {result.total_skus}, складов: {result.total_warehouses}\n"
            f"Записей: {result.total_rows}, время: {result.duration_sec:.1f}с"
        )
        if result.new_warehouses:
            msg += f"\n🆕 Новые склады: {', '.join(map(str, result.new_warehouses))}"
        return msg
    else:
        errs = "\n".join(f"• {e}" for e in result.errors[:5])
        return f"❌ <b>YM fetch failed</b> {snapshot_date}\n{errs}"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def run(snapshot_date: str | None = None, dry_run: bool = False) -> FetchResult:
    started = time.time()
    snapshot_date = snapshot_date or date.today().isoformat()
    fetch_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    request_id = f"req-{uuid.uuid4().hex[:8]}"

    result = FetchResult(success=False)

    try:
        api_key = os.environ["YM_API_KEY"]
        campaign_id = int(os.environ["YM_CAMPAIGN_ID"])
        sheet_id = os.environ["GSHEET_ID"]
        creds_path = os.environ.get("GOOGLE_CREDS", "/opt/openclaw/secrets/gcp-sa.json")

        client = YMClient(api_key, campaign_id)
        sheets = SheetsClient(creds_path, sheet_id) if not dry_run else None

        # 1. Обновляем справочник складов (только новые добавляются)
        new_wh = []
        if sheets:
            try:
                ym_wh = client.fetch_warehouses()
                new_wh = sheets.upsert_warehouses(ym_wh)
                result.new_warehouses = new_wh
            except Exception as e:
                logging.warning("Could not refresh warehouses: %s", e)

        # 2. Тянем все страницы остатков
        wh_names = sheets.get_warehouse_names() if sheets else {}
        all_raw_rows: list[StockRow] = []
        all_snapshot_rows: list[SnapshotRow] = []
        seen_skus = set()
        seen_warehouses = set()

        for page in client.fetch_stocks_pages():
            for wh_id, offers in parse_warehouses_response(page):
                seen_warehouses.add(wh_id)
                wh_name = wh_names.get(wh_id, f"Склад {wh_id}")
                for offer in offers:
                    raw = parse_offer(offer, wh_id, fetch_ts, snapshot_date, request_id)
                    if not raw.sku:
                        continue
                    seen_skus.add(raw.sku)
                    all_raw_rows.append(raw)
                    snap = SnapshotRow(
                        date=snapshot_date,
                        sku=raw.sku,
                        marketplace=MARKETPLACE,
                        warehouse_id=wh_id,
                        warehouse_name=wh_name,
                        # FIT = "годный" = AVAILABLE + FREEZE.
                        # Берём именно FIT, а не AVAILABLE: для прогноза
                        # "на сколько дней хватит" важен физический запас
                        # на складе (включая зарезервированный под уже
                        # размещённые заказы — он всё равно скоро уйдёт
                        # покупателю или вернётся в AVAILABLE при отмене).
                        # AVAILABLE сам по себе обманчив: товар может быть
                        # весь "зарезервирован" под заказы и AVAILABLE=0,
                        # но физически на складе лежит — Яндекс при этом
                        # справедливо показывает turnover_days=100+.
                        # Подробности см. CHANGES.md, Patch 2.
                        qty_full=raw.qty_fit,
                        turnover_grade=raw.turnover_grade,
                        turnover_days=raw.turnover_days,
                        mp_updated_at=raw.mp_updated_at,
                    )
                    all_snapshot_rows.append(snap)

        # 3. Пишем в Sheets
        if sheets:
            sheets.append_raw_rows(all_raw_rows)
            sheets.upsert_snapshot_rows(all_snapshot_rows, snapshot_date)
        else:
            logging.info("DRY RUN: would write %d raw + %d snapshot rows",
                         len(all_raw_rows), len(all_snapshot_rows))
            for row in all_snapshot_rows[:5]:
                logging.info("  %s", row.as_list())

        result.success = True
        result.total_skus = len(seen_skus)
        result.total_warehouses = len(seen_warehouses)
        result.total_rows = len(all_snapshot_rows)

    except KeyError as e:
        result.errors.append(f"Missing env var: {e}")
        logging.error("Configuration error: %s", e)
    except Exception as e:
        result.errors.append(str(e))
        logging.exception("Fetch failed")

    result.duration_sec = time.time() - started
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать в Sheets, только логировать")
    parser.add_argument("--date", default=None,
                        help="Дата снапшота YYYY-MM-DD (по умолчанию сегодня)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    result = run(snapshot_date=args.date, dry_run=args.dry_run)

    bot_token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_ADMIN_CHAT_ID")
    if not args.dry_run and (bot_token and chat_id):
        send_telegram(bot_token, chat_id,
                      format_result_message(result, args.date or date.today().isoformat()))

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
