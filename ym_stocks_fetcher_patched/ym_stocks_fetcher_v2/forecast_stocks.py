"""
forecast_stocks.py — расчёт прогноза 7/14/30 на основе данных в Google Sheets.

Что делает:
1. Читает свежий снапшот из stock_marketplace (последняя дата).
2. Читает sales_daily за последние 30 дней.
3. Считает скорости 7/14/30 дней для каждого (sku, warehouse).
4. Считает days_to_oos для каждой строки.
5. Sanity-check с turnover от Яндекса.
6. Пишет в forecast (только новые строки на сегодня).
7. Формирует и шлёт текстовую сводку в Telegram.

Запуск: python forecast_stocks.py [--dry-run] [--date YYYY-MM-DD]
Cron:   30 6 * * *  /opt/openclaw/skills/ym_stocks_fetcher/forecast_stocks.py

Запускается ПОСЛЕ fetch_ym_stocks.py (с интервалом в 30 минут).
"""

from __future__ import annotations
import argparse
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, datetime
from statistics import mean
from typing import Any

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

from sheets_helpers import safe_append_rows, sanitize_value


def _safe_int(value, default: int = 0) -> int:
    """Парсит int из str/float/None, при ошибке возвращает default.
    Отсекает inf/nan и экстремально большие значения."""
    if value is None or value == "":
        return default
    try:
        f = float(value)
        if math.isinf(f) or math.isnan(f) or abs(f) > 1e12:
            return default
        return int(f)
    except (ValueError, TypeError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    """Парсит float, отсекая inf/nan и экстремальные значения."""
    if value is None or value == "":
        return default
    try:
        f = float(value)
        if math.isinf(f) or math.isnan(f) or abs(f) > 1e12:
            return default
        return f
    except (ValueError, TypeError):
        return default

# Параметры прогноза
HORIZONS = [7, 14, 30]
SPEED_USED_HORIZON = 14    # какую скорость использовать в days_to_oos (компромисс шум/инерция)
SAFETY_DAYS = 21           # запас при расчёте рекомендованной поставки
ALERT_THRESHOLD_DAYS = 14  # ниже этого попадает в алерт

# Приоритеты
def priority(days_to_oos: float | None) -> str:
    if days_to_oos is None:
        return "🟢"
    if days_to_oos < 7:
        return "🔴"
    if days_to_oos < 14:
        return "🟡"
    return "🟢"


@dataclass
class SalesPoint:
    date: date
    sku: str
    marketplace: str
    qty_sold: int


@dataclass
class StockSnapshot:
    date: date
    sku: str
    marketplace: str
    warehouse_id: int
    warehouse_name: str
    qty_full: int
    turnover_grade: str
    turnover_days: float | str


@dataclass
class ForecastRow:
    forecast_date: str
    sku: str
    marketplace: str
    warehouse: str
    stock_mp: int
    ready_office: int
    qty_in_transit: int           # из supplies_planned (модуль 3)
    nearest_arrival: str          # YYYY-MM-DD или ""
    speed_7d: float
    speed_14d: float
    speed_30d: float
    speed_used: float
    days_to_oos: int | str
    priority: str
    ym_turnover_grade: str
    ym_turnover_days: float | str
    sanity_check: str
    recommended_qty: int
    sent_to_tg: str = "Нет"

    def as_list(self) -> list:
        return [self.forecast_date, self.sku, self.marketplace, self.warehouse,
                self.stock_mp, self.ready_office,
                self.qty_in_transit, self.nearest_arrival,
                self.speed_7d, self.speed_14d, self.speed_30d, self.speed_used,
                self.days_to_oos, self.priority,
                self.ym_turnover_grade, self.ym_turnover_days, self.sanity_check,
                self.recommended_qty, self.sent_to_tg]


# ----------------------------------------------------------------------
# Чтение данных из Sheets
# ----------------------------------------------------------------------
class SheetsReader:
    # Структура листов: 3-5 строк intro + 1 заголовок + данные
    # Чтобы не зависеть от точного числа интро-строк, ищем строку заголовка по первой колонке
    SHEET_HEADERS = {
        "stock_marketplace": "date",
        "sales_daily": "date",
        "stock_office": "sku",
        "supplies_planned": "fetch_timestamp",
        "warehouse_routing": "sku",
    }

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
        self.spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)

    def _find_data_rows(self, sheet_name: str) -> tuple[list[str], list[list[str]]]:
        """Возвращает (headers, data_rows). Пропускает intro-строки."""
        ws = self.spreadsheet.worksheet(sheet_name)
        all_rows = ws.get_all_values()
        marker = self.SHEET_HEADERS[sheet_name]
        header_idx = None
        for i, row in enumerate(all_rows):
            if row and row[0].strip().lower() == marker:
                header_idx = i
                break
        if header_idx is None:
            return [], []
        return all_rows[header_idx], all_rows[header_idx + 1:]

    # ────────────────────────────────────────────────────────────────
    # Routing: какие склады считать для какого SKU
    # ────────────────────────────────────────────────────────────────
    # Лист warehouse_routing — связки SKU × склад, которые нас интересуют.
    # Колонки: sku, warehouse, active, marketplace, comment
    # Фильтр в build_forecast:
    #   - Если SKU есть в routing → оставить ТОЛЬКО строки на разрешённых складах.
    #   - Если SKU нет в routing → пропустить все строки (fallback на все склады).
    # active != "Да" — строка routing игнорируется (как будто её нет).
    ROUTING_ACTIVE_VALUES = {"да", "yes", "y", "true", "1", "+"}

    def read_warehouse_routing(self, marketplace: str = "YM"
                                ) -> dict[str, set[str]] | None:
        """Возвращает {sku: {warehouse_name, ...}} активных связок,
        либо None если листа warehouse_routing нет.

        Это РАЗНЫЕ ситуации (важно для build_forecast):
        - None → фильтр ВЫКЛЮЧЕН (как было до апдейта, все строки идут).
        - {}   → фильтр ВКЛЮЧЁН и пуст: ни одной активной связки.
                 build_forecast в этом случае ничего не вернёт +
                 залогирует предупреждение. Это симптом, что забыли
                 заполнить лист — лучше явный пустой алерт, чем тихий
                 «всё нормально».
        - {sku: {...}} → нормальная работа: только перечисленные связки.

        Сравнение имени склада регистро- и пробело-нечувствительное —
        ключи в значениях нормализуем (strip + lower) одинаково и
        здесь, и в build_forecast.
        """
        try:
            headers, rows = self._find_data_rows("warehouse_routing")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning(
                "warehouse_routing sheet not found — фильтр SKU×склад "
                "ВЫКЛЮЧЕН, прогноз пойдёт по всем складам как раньше")
            return None

        routing: dict[str, set[str]] = {}
        total = 0
        for row in rows:
            # Минимум sku и warehouse
            if len(row) < 2 or not row[0].strip() or not row[1].strip():
                continue
            sku = row[0].strip()
            wh_name = row[1].strip().lower()
            # active — третья колонка
            active = row[2].strip().lower() if len(row) > 2 else "да"
            # marketplace — четвёртая колонка. Если задан и не наш — skip.
            row_mp = row[3].strip() if len(row) > 3 else ""
            if row_mp and row_mp != marketplace:
                continue
            if active not in self.ROUTING_ACTIVE_VALUES:
                continue
            routing.setdefault(sku, set()).add(wh_name)
            total += 1
        logging.info("warehouse_routing: %d active pairs for %d SKUs (%s)",
                     total, len(routing), marketplace)
        return routing

    def read_sales(self, since: date) -> list[SalesPoint]:
        headers, rows = self._find_data_rows("sales_daily")
        # Sales_daily колонки (модуль 2):
        # date, sku, marketplace, qty_sold_created, qty_sold_delivered, ...
        # Для прогноза используем qty_sold_created (4-я колонка, индекс 3).
        points = []
        for row in rows:
            if len(row) < 4 or not row[0]:
                continue
            try:
                d = datetime.strptime(row[0], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < since:
                continue
            qty = _safe_int(row[3])
            points.append(SalesPoint(d, row[1], row[2], qty))
        return points

    def read_latest_stocks(self, marketplace: str = "YM") -> list[StockSnapshot]:
        """Берёт только самую свежую дату снапшота. Фильтрация по
        routing (sku × warehouse) делается на следующем шаге, в
        build_forecast — здесь возвращаем все строки."""
        headers, rows = self._find_data_rows("stock_marketplace")
        latest_date = None
        parsed: list[StockSnapshot] = []
        for row in rows:
            if len(row) < 11 or not row[0]:
                continue
            try:
                d = datetime.strptime(row[0], "%Y-%m-%d").date()
            except ValueError:
                continue
            if row[2] != marketplace:
                continue
            if latest_date is None or d > latest_date:
                latest_date = d

            # Все числовые поля парсим через safe-функции, защищаясь от
            # inf/nan/мусорных строк.
            turnover_days = _safe_float(row[9]) if row[9] else ""
            wh_id = _safe_int(row[3])
            qty_full = _safe_int(row[5])

            parsed.append(StockSnapshot(d, row[1], row[2], wh_id, row[4],
                                        qty_full, row[8], turnover_days))
        return [p for p in parsed if p.date == latest_date]

    def read_office_ready(self) -> dict[tuple[str, str], int]:
        """Возвращает {(sku, marketplace): ready_to_ship_qty} из stock_office.

        На этапах 1-2 лист stock_office может отсутствовать (он появляется
        в модуле офисного учёта). В этом случае возвращаем пустой словарь —
        в прогнозе ready_office будет 0 для всех позиций.
        """
        try:
            headers, rows = self._find_data_rows("stock_office")
        except gspread.exceptions.WorksheetNotFound:
            logging.info("stock_office sheet not found (OK for stages 1-2), ready_office=0")
            return {}
        # Колонки: sku, name, checked_total, packed_wb, packed_ym, packed_ozon,
        #         shipped_wb, shipped_ym, shipped_ozon, free,
        #         ready_to_ship_wb, ready_to_ship_ym, ready_to_ship_ozon
        result: dict[tuple[str, str], int] = {}
        mp_to_col = {"WB": 10, "YM": 11, "Ozon": 12}
        for row in rows:
            if not row or not row[0]:
                continue
            for mp, col in mp_to_col.items():
                if col >= len(row):
                    continue
                try:
                    qty = int(float(row[col])) if row[col] else 0
                except ValueError:
                    qty = 0
                result[(row[0], mp)] = max(qty, 0)
        return result

    def read_supplies_in_transit(self) -> dict[tuple[str, str], dict]:
        """Возвращает {(sku, marketplace): {"qty": int, "eta": str}}
        из листа supplies_planned (модуль 3, fetch_ym_supplies).

        Если листа нет (не накатили модуль 3 / первый запуск) —
        возвращаем пустой словарь, qty_in_transit будет 0 для всех.

        В отличие от stock_office, лист пишется самим ботом — поэтому
        форматы строго наши (см. fetch_ym_supplies.PlannedRow):
        fetch_timestamp, sku, marketplace,
        qty_in_transit, qty_planned_total, qty_already_accepted,
        nearest_arrival_date, requests_count, request_ids, statuses,
        target_warehouses
        """
        try:
            headers, rows = self._find_data_rows("supplies_planned")
        except gspread.exceptions.WorksheetNotFound:
            logging.info(
                "supplies_planned sheet not found "
                "(модуль 3 не накатан?), qty_in_transit=0")
            return {}
        except RuntimeError as e:
            # _find_data_rows кидает RuntimeError если не нашёл заголовок —
            # например, лист есть, но он пустой (после первого add_worksheet
            # без записи). Тоже трактуем как "нет данных".
            logging.warning("supplies_planned read failed: %s; treating as empty", e)
            return {}

        result: dict[tuple[str, str], dict] = {}
        for row in rows:
            # Минимум 7 колонок до nearest_arrival_date
            if len(row) < 7 or not row[1]:
                continue
            sku = row[1].strip()
            mp = row[2].strip() if len(row) > 2 else ""
            if not sku or not mp:
                continue
            qty = _safe_int(row[3])  # qty_in_transit
            if qty <= 0:
                # Строки с 0 в пути пишутся для прозрачности, но в
                # прогнозе нет смысла их учитывать
                continue
            eta = row[6].strip() if len(row) > 6 else ""
            result[(sku, mp)] = {"qty": qty, "eta": eta}
        return result

    def write_forecast(self, rows: list[ForecastRow]):
        if not rows:
            return
        ws = self.spreadsheet.worksheet("forecast")
        safe_append_rows(ws, [r.as_list() for r in rows])


# ----------------------------------------------------------------------
# Расчёт
# ----------------------------------------------------------------------
def compute_speeds(
    sales: list[SalesPoint],
    today: date,
    sku: str,
    marketplace: str,
) -> dict[int, float]:
    """Возвращает {7: speed_7d, 14: speed_14d, 30: speed_30d}."""
    by_day: dict[date, int] = defaultdict(int)
    for p in sales:
        if p.sku == sku and p.marketplace == marketplace:
            by_day[p.date] += p.qty_sold

    speeds = {}
    for horizon in HORIZONS:
        start = today - timedelta(days=horizon - 1)
        vals = [by_day.get(today - timedelta(days=i), 0) for i in range(horizon)]
        speeds[horizon] = round(mean(vals), 2) if vals else 0.0
    return speeds


def sanity_check(days_to_oos: int | str, ym_grade: str, ym_days: float | str) -> str:
    if ym_grade == "NO_SALES":
        return "нет продаж"
    if isinstance(days_to_oos, str):
        return "—"
    # Расхождение с YM
    if days_to_oos < 14 and ym_grade == "LOW":
        return "⚠️ расхождение"
    if days_to_oos > 60 and ym_grade == "VERY_HIGH":
        return "⚠️ расхождение"
    return "ок"


def recommend_qty(stock_mp: int, ready_office: int, speed: float) -> int:
    """Рекомендация: сколько надо иметь, чтобы хватило на SAFETY_DAYS."""
    if speed <= 0:
        return 0
    target = speed * SAFETY_DAYS
    deficit = target - stock_mp - ready_office
    return max(int(round(deficit)), 0)


def build_forecast(
    snapshots: list[StockSnapshot],
    sales: list[SalesPoint],
    office_ready: dict[tuple[str, str], int],
    supplies_in_transit: dict[tuple[str, str], dict],
    today: date,
    routing: dict[str, set[str]] | None = None,
) -> list[ForecastRow]:
    """Считает прогноз для каждой строки stock_marketplace.

    Формула days_to_oos:
        days = (qty_full + ready_office + qty_in_transit) / speed_used

    Это плоский прогноз — он отвечает «хватит ли всего наличного и
    идущего, и на сколько дней». Это компромисс: он НЕ учитывает,
    что поставка приедет не сегодня. Точная временная модель
    («остаток упадёт до 0 через 8 дней, но 12-го приедет N штук»)
    будет в отдельном расширении прогноза.

    Поведение при отсутствии данных:
    - office_ready = 0, если stock_office нет;
    - supplies_in_transit = 0, если supplies_planned нет / 0 в пути.

    Фильтр по routing (warehouse_routing — это whitelist!):
    - Если routing задан и непуст:
        * SKU есть в routing → оставляем ТОЛЬКО снапшоты на разрешённых
          складах для этого SKU.
        * SKU нет в routing → ПОЛНОСТЬЮ игнорируем все его строки
          (как будто SKU нет ни в stock_marketplace, ни в supplies).
    - routing = None → фильтр выключен, идёт всё.
    - routing = {} → фильтр включён, но пуст → ничего не пройдёт
      (логируем предупреждение, потому что это похоже на пустой
      лист warehouse_routing — обычно симптом ошибки конфигурации).
    """
    rows: list[ForecastRow] = []
    today_str = today.isoformat()
    filtered_pairs = 0           # отброшенные по причине "склад не в routing"
    skipped_skus_total = 0       # уникальные SKU, отброшенные за "нет в routing"
    skipped_skus_set: set[str] = set()

    routing_enabled = routing is not None

    if routing_enabled and not routing:
        logging.warning(
            "routing is empty — все SKU будут отброшены. "
            "Если это не намеренно — проверь лист warehouse_routing")

    for snap in snapshots:
        if routing_enabled:
            allowed = routing.get(snap.sku)
            if allowed is None:
                # SKU не упоминается в routing → игнорируем целиком
                skipped_skus_set.add(snap.sku)
                continue
            # Сравнение имени склада регистро- и пробело-нечувствительное
            if snap.warehouse_name.strip().lower() not in allowed:
                filtered_pairs += 1
                continue

        speeds = compute_speeds(sales, today, snap.sku, snap.marketplace)
        speed_used = speeds[SPEED_USED_HORIZON]
        ready = office_ready.get((snap.sku, snap.marketplace), 0)

        transit_info = supplies_in_transit.get((snap.sku, snap.marketplace),
                                               {"qty": 0, "eta": ""})
        in_transit = transit_info["qty"]
        eta = transit_info["eta"]

        # Защита от inf: используем явный минимальный порог скорости, а не >0.
        # При speed_used < 0.01 (меньше 1 продажи в 100 дней) считаем "нет продаж".
        if speed_used >= 0.01:
            raw_days = (snap.qty_full + ready + in_transit) / speed_used
            # Дополнительная защита от переполнения
            days = int(round(raw_days)) if raw_days < 10000 else 9999
        else:
            days = "нет продаж"

        prio = priority(days if isinstance(days, int) else None)
        # При расчёте рекомендации к заказу — НЕ вычитаем in_transit:
        # это уже едет, но мы не управляем им. recommend_qty показывает
        # сколько НАМ нужно добрать сверх того что мы сами загружаем
        # (то что в офисе) и того что уже в МП.
        # in_transit вычитаем дополнительно, иначе будем заказывать дубль.
        rec = recommend_qty(snap.qty_full + in_transit, ready, speed_used)
        check = sanity_check(days, snap.turnover_grade, snap.turnover_days)
        will_alert = isinstance(days, int) and days < ALERT_THRESHOLD_DAYS

        rows.append(ForecastRow(
            forecast_date=today_str,
            sku=snap.sku,
            marketplace=snap.marketplace,
            warehouse=snap.warehouse_name,
            stock_mp=snap.qty_full,
            ready_office=ready,
            qty_in_transit=in_transit,
            nearest_arrival=eta,
            speed_7d=speeds[7],
            speed_14d=speeds[14],
            speed_30d=speeds[30],
            speed_used=speed_used,
            days_to_oos=days,
            priority=prio,
            ym_turnover_grade=snap.turnover_grade,
            ym_turnover_days=snap.turnover_days,
            sanity_check=check,
            recommended_qty=rec,
            sent_to_tg="Да" if will_alert else "Нет",
        ))
    if filtered_pairs or skipped_skus_set:
        logging.info(
            "warehouse_routing: filtered out %d (sku, warehouse) pairs "
            "from %d allowed SKUs; skipped %d SKUs entirely (not in routing)",
            filtered_pairs,
            len(routing) if routing_enabled and routing else 0,
            len(skipped_skus_set))
    return rows


# ----------------------------------------------------------------------
# Сводка для Telegram
# ----------------------------------------------------------------------
def _transit_hint(r: ForecastRow) -> str:
    """Хвост для строки про SKU: указывает, что уже едет, если есть."""
    if r.qty_in_transit <= 0:
        return ""
    if r.nearest_arrival:
        return f" 🚚 +{r.qty_in_transit} (≈{r.nearest_arrival})"
    return f" 🚚 +{r.qty_in_transit}"


def build_summary(rows: list[ForecastRow], today: date) -> str:
    critical = [r for r in rows if r.priority == "🔴"]
    warning = [r for r in rows if r.priority == "🟡"]
    sanity_issues = [r for r in rows if "⚠️" in r.sanity_check]
    incoming = [r for r in rows if r.qty_in_transit > 0]

    lines = [f"☀️ <b>Прогноз остатков YM на {today.isoformat()}</b>", ""]

    if critical:
        lines.append(f"🔴 <b>Срочно (&lt; 7 дней до OOS):</b>")
        for r in sorted(critical, key=lambda x: x.days_to_oos if isinstance(x.days_to_oos, int) else 999):
            lines.append(
                f"• {r.sku} / {r.warehouse}: остаток {r.stock_mp}, "
                f"скорость {r.speed_used:.1f}/день → ~{r.days_to_oos} дней. "
                f"Рекомендую {r.recommended_qty} шт.{_transit_hint(r)}"
            )
        lines.append("")

    if warning:
        lines.append(f"🟡 <b>Скоро (7-14 дней):</b>")
        for r in sorted(warning, key=lambda x: x.days_to_oos if isinstance(x.days_to_oos, int) else 999):
            lines.append(
                f"• {r.sku} / {r.warehouse}: {r.stock_mp} шт, "
                f"~{r.days_to_oos} дней. Рекомендую {r.recommended_qty}.{_transit_hint(r)}"
            )
        lines.append("")

    if sanity_issues:
        lines.append(f"⚠️ <b>Расхождения с оценкой Яндекса:</b>")
        for r in sanity_issues:
            lines.append(
                f"• {r.sku} / {r.warehouse}: наш прогноз {r.days_to_oos} дн, "
                f"YM={r.ym_turnover_grade} ({r.ym_turnover_days} дн)"
            )
        lines.append("")

    # Отдельная сводка по идущим поставкам — показываем даже там, где
    # прогноз 🟢, чтобы было видно "движение".
    # Дедуплицируем: одна строка supplies_planned может соответствовать
    # нескольким строкам forecast (одному SKU — несколько складов).
    # Берём по одной записи на SKU: qty_in_transit и eta у дублей
    # одинаковые (берутся из той же agg-записи), так что выбор любой.
    if incoming:
        by_sku: dict[str, ForecastRow] = {}
        for r in incoming:
            # max() оставит "первый встреченный" — детерминированно
            if r.sku not in by_sku:
                by_sku[r.sku] = r
        unique_incoming = list(by_sku.values())

        lines.append(f"🚚 <b>В пути на склады YM ({len(unique_incoming)} SKU):</b>")
        # Сортируем по nearest_arrival (пустые даты в конец)
        def _eta_key(x):
            return x.nearest_arrival or "9999-99-99"
        for r in sorted(unique_incoming, key=_eta_key)[:15]:
            eta = r.nearest_arrival or "дата не указана"
            lines.append(f"• {r.sku}: +{r.qty_in_transit} шт (≈{eta})")
        if len(unique_incoming) > 15:
            lines.append(f"  …и ещё {len(unique_incoming) - 15} SKU")
        lines.append("")

    in_norm = [r for r in rows if r.priority == "🟢"]
    if in_norm:
        lines.append(f"🟢 <b>В норме:</b> {len(in_norm)} позиций")

    return "\n".join(lines)


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


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def run(today: date | None = None, dry_run: bool = False):
    today = today or date.today()
    sheet_id = os.environ["GSHEET_ID"]
    creds_path = os.environ.get("GOOGLE_CREDS", "/opt/openclaw/secrets/gcp-sa.json")
    reader = SheetsReader(creds_path, sheet_id)

    snapshots = reader.read_latest_stocks(marketplace="YM")
    sales = reader.read_sales(since=today - timedelta(days=30))
    office = reader.read_office_ready()
    supplies = reader.read_supplies_in_transit()
    routing = reader.read_warehouse_routing(marketplace="YM")

    logging.info(
        "Loaded %d snapshots, %d sales points, %d office positions, "
        "%d SKUs in transit, %s",
        len(snapshots), len(sales), len(office), len(supplies),
        ("routing OFF (no warehouse_routing sheet)" if routing is None
         else f"{len(routing)} SKUs in routing"))

    rows = build_forecast(snapshots, sales, office, supplies, today,
                          routing=routing)
    logging.info("Built %d forecast rows", len(rows))

    if dry_run:
        for r in rows:
            logging.info("  %s", r.as_list())
    else:
        reader.write_forecast(rows)

    # Telegram
    bot_token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_ADMIN_CHAT_ID")
    summary = build_summary(rows, today)
    print(summary)
    if not dry_run and bot_token and chat_id:
        send_telegram(bot_token, chat_id, summary)

    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    target_date = (datetime.strptime(args.date, "%Y-%m-%d").date()
                   if args.date else date.today())
    run(today=target_date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
