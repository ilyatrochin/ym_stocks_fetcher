"""
fbs_shipment.py — модуль 7: формирование FBS-отгрузки на сегодня.

Отдельный от прогноза остатков функционал (FBY), живёт в том же боте.

Что делает:
1. POST /v1/businesses/{businessId}/orders — FBS-заказы в PROCESSING
   (STARTED + READY_TO_SHIP) с датой отгрузки сегодня. Заказы с прошедшей
   датой отгрузки, которые всё ещё не отгружены, тоже попадают в план —
   с пометкой «просрочено» (их всё равно надо везти сегодня).
2. PUT /v2/campaigns/{campaignId}/first-mile/shipments — отгрузки на сегодня
   (справочно: номер, интервал, сверка списка заказов). Best-effort: если
   метод упал — документы всё равно формируются.
3. Ярлыки: POST /v2/reports/documents/labels/generate → GET /v2/reports/info
   → PDF (формат FBS_LABEL_FORMAT, по умолчанию 58×40 A9_HORIZONTALLY).
   Порядок ярлыков = порядок заказов в листе сборки.
4. Лист сборки: PDF генерируется локально (reportlab) — сводка по SKU
   с чекбоксами + разбивка по заказам.
5. Выкладывает оба PDF на общий диск Google Drive в подпапку YYYY-MM-DD
   (повторный запуск перезаписывает файлы, не плодит дубли).
6. mark_ready_to_ship() — перевод STARTED → READY_TO_SHIP (вызывается
   ТОЛЬКО из бота по кнопке «Собрано» с подтверждением). Нужны права
   ключа inventory-and-order-processing (на запись).

Запуск:
  python fbs_shipment.py --dry-run --verbose      # PDF локально, без Drive
  python fbs_shipment.py                          # PDF + выгрузка на Drive
  python fbs_shipment.py --date 2026-10-17
  python fbs_shipment.py --fixture orders.json --dry-run   # без API (тест вёрстки)

Статус в Маркете из CLI НЕ меняется никогда — только из бота.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
API_BASE = "https://api.partner.market.yandex.ru"
ORDERS_ENDPOINT = "/v1/businesses/{business_id}/orders"
SHIPMENTS_ENDPOINT = "/v2/campaigns/{campaign_id}/first-mile/shipments"
LABELS_ENDPOINT = "/v2/reports/documents/labels/generate"
REPORT_INFO_ENDPOINT = "/v2/reports/info/{report_id}"
STATUS_UPDATE_ENDPOINT = "/v2/campaigns/{campaign_id}/orders/status-update"

PAGE_LIMIT = 50
HTTP_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
RATE_SLEEP = 0.3

STATUS_BATCH = 30             # максимум заказов в одном status-update
LABELS_MAX_ORDERS = 1000      # максимум orderIds в генерации ярлыков
LABELS_POLL_TIMEOUT = 300     # сек, ожидание готовности PDF
LABELS_POLL_INTERVAL = 3

OVERDUE_LOOKBACK_DAYS = 7     # насколько назад искать неотгруженные просрочки

FBS_SUBSTATUSES = ["STARTED", "READY_TO_SHIP"]
DEFAULT_LABEL_FORMAT = "A9_HORIZONTALLY"   # 58×40, термопринтер
DEFAULT_TZ = "Europe/Moscow"

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

FONT_CANDIDATES = [
    # Linux (apt install fonts-dejavu-core)
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    # macOS — для локальной отладки
    ("/Library/Fonts/Arial Unicode.ttf", None),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None),
]


def today_local() -> date:
    return datetime.now(ZoneInfo(os.environ.get("FBS_TZ", DEFAULT_TZ))).date()


# ----------------------------------------------------------------------
# Структуры
# ----------------------------------------------------------------------
@dataclass
class FbsItem:
    offer_id: str
    name: str
    count: int
    needs_cis: bool = False    # маркированный товар без переданных КИЗ


@dataclass
class FbsOrder:
    order_id: int
    campaign_id: int
    substatus: str
    shipment_date: str         # YYYY-MM-DD или ""
    boxes: int
    items: list[FbsItem] = field(default_factory=list)
    overdue: bool = False

    @property
    def total_qty(self) -> int:
        return sum(i.count for i in self.items)

    @property
    def needs_cis(self) -> bool:
        return any(i.needs_cis for i in self.items)

    @property
    def sort_key(self) -> tuple:
        # Группируем похожие заказы рядом: сначала по набору SKU.
        return (tuple(sorted(i.offer_id for i in self.items)), self.order_id)


@dataclass
class FbsShipment:
    shipment_id: int
    campaign_id: int
    status: str
    interval: str
    order_ids: list[int]


@dataclass
class FbsPlan:
    ship_date: date
    orders: list[FbsOrder] = field(default_factory=list)
    shipments: list[FbsShipment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_qty(self) -> int:
        return sum(o.total_qty for o in self.orders)

    @property
    def total_boxes(self) -> int:
        return sum(o.boxes for o in self.orders)

    def sku_summary(self) -> list[tuple[str, str, int, int]]:
        """[(offer_id, name, qty, orders_count)], по offer_id."""
        qty: dict[str, int] = defaultdict(int)
        names: dict[str, str] = {}
        orders_cnt: dict[str, int] = defaultdict(int)
        for o in self.orders:
            seen = set()
            for it in o.items:
                qty[it.offer_id] += it.count
                names.setdefault(it.offer_id, it.name)
                if it.offer_id not in seen:
                    orders_cnt[it.offer_id] += 1
                    seen.add(it.offer_id)
        return [(sku, names[sku], qty[sku], orders_cnt[sku])
                for sku in sorted(qty)]

    def started_orders(self) -> list[FbsOrder]:
        """Заказы, которые можно перевести в READY_TO_SHIP."""
        return [o for o in self.orders
                if o.substatus == "STARTED" and not o.needs_cis]


@dataclass
class ReadyResult:
    updated: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    skipped_cis: list[int] = field(default_factory=list)


# ----------------------------------------------------------------------
# YM API клиент
# ----------------------------------------------------------------------
class YMApiError(RuntimeError):
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {text[:500]}")


class YMFbsClient:
    def __init__(self, api_key: str, business_id: int):
        self.business_id = business_id
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
                # 400/401/403/404 — не ретраим
                raise YMApiError(resp.status_code, resp.text)
            except requests.RequestException as e:
                last_err = e
                wait = RETRY_BACKOFF ** attempt
                logging.warning("Request failed: %s, retry in %.1fs", e, wait)
                time.sleep(wait)
        raise RuntimeError(f"Max retries exceeded: {last_err}")

    # --- заказы -------------------------------------------------------
    def iter_fbs_orders(self, ship_from: date, ship_to_excl: date) -> Iterator[dict]:
        url = f"{API_BASE}{ORDERS_ENDPOINT.format(business_id=self.business_id)}"
        body = {
            "programTypes": ["FBS"],
            "statuses": ["PROCESSING"],
            "substatuses": FBS_SUBSTATUSES,
            "fake": False,
            "dates": {
                "shipmentDateFrom": ship_from.isoformat(),
                "shipmentDateTo": ship_to_excl.isoformat(),   # не включительно
            },
        }
        page_token = None
        while True:
            params = {"limit": PAGE_LIMIT}
            if page_token:
                params["pageToken"] = page_token
            data = self._request("POST", url, params=params, json=body)
            # Ответ v1 без обёртки result, но на всякий случай поддержим оба.
            payload = data.get("result", data)
            for order in payload.get("orders", []):
                yield order
            page_token = (payload.get("paging") or {}).get("nextPageToken")
            if not page_token:
                break
            time.sleep(RATE_SLEEP)

    # --- отгрузки (best-effort) ---------------------------------------
    def search_shipments(self, campaign_id: int, day: date) -> list[dict]:
        url = f"{API_BASE}{SHIPMENTS_ENDPOINT.format(campaign_id=campaign_id)}"
        # В доке формат дат противоречив (описание DD-MM-YYYY, примеры ISO).
        # Пробуем ISO, на 400 — DD-MM-YYYY.
        formats = [day.isoformat(), day.strftime("%d-%m-%Y")]
        last_err: Exception | None = None
        for d in formats:
            body = {"dateFrom": d, "dateTo": d, "cancelledOrders": False}
            shipments: list[dict] = []
            page_token = None
            try:
                while True:
                    params = {"limit": 30}
                    if page_token:
                        params["pageToken"] = page_token
                    data = self._request("PUT", url, params=params, json=body)
                    result = data.get("result", {})
                    shipments.extend(result.get("shipments", []))
                    page_token = (result.get("paging") or {}).get("nextPageToken")
                    if not page_token:
                        return shipments
            except YMApiError as e:
                last_err = e
                if e.status_code != 400:
                    break
        raise last_err or RuntimeError("search_shipments failed")

    # --- ярлыки -------------------------------------------------------
    def generate_labels_pdf(self, order_ids: list[int], label_format: str) -> bytes:
        if len(order_ids) > LABELS_MAX_ORDERS:
            raise RuntimeError(
                f"Слишком много заказов для одного PDF: {len(order_ids)}")
        url = f"{API_BASE}{LABELS_ENDPOINT}"
        data = self._request(
            "POST", url, params={"format": label_format},
            json={"businessId": self.business_id,
                  "orderIds": order_ids,
                  "sortingType": "SORT_BY_GIVEN_ORDER"})
        report_id = data["result"]["reportId"]
        logging.info("Labels report %s requested (%d orders)",
                     report_id, len(order_ids))

        info_url = f"{API_BASE}{REPORT_INFO_ENDPOINT.format(report_id=report_id)}"
        deadline = time.time() + LABELS_POLL_TIMEOUT
        while time.time() < deadline:
            info = self._request("GET", info_url).get("result", {})
            status = info.get("status")
            if status == "DONE":
                if info.get("subStatus") == "RESOURCE_NOT_FOUND":
                    logging.warning("Labels: часть заказов не найдена")
                file_url = info.get("file")
                if not file_url:
                    raise RuntimeError(
                        f"Ярлыки готовы, но нет ссылки (subStatus="
                        f"{info.get('subStatus')})")
                resp = requests.get(file_url, timeout=120)
                resp.raise_for_status()
                return resp.content
            if status == "FAILED":
                raise RuntimeError(
                    f"Генерация ярлыков упала (subStatus={info.get('subStatus')})")
            time.sleep(LABELS_POLL_INTERVAL)
        raise RuntimeError(f"Ярлыки не готовы за {LABELS_POLL_TIMEOUT}с")

    # --- смена статуса ------------------------------------------------
    def set_ready_to_ship(self, campaign_id: int,
                          order_ids: list[int]) -> list[dict]:
        url = f"{API_BASE}{STATUS_UPDATE_ENDPOINT.format(campaign_id=campaign_id)}"
        body = {"orders": [{"id": oid, "status": "PROCESSING",
                            "substatus": "READY_TO_SHIP"} for oid in order_ids]}
        data = self._request("POST", url, json=body)
        return data.get("result", {}).get("orders", [])


# ----------------------------------------------------------------------
# Парсинг
# ----------------------------------------------------------------------
def _parse_date(value) -> str:
    """'YYYY-MM-DD' / 'DD-MM-YYYY' / ISO datetime → 'YYYY-MM-DD'."""
    if not value:
        return ""
    s = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_order(order: dict) -> FbsOrder:
    delivery = order.get("delivery") or {}
    shipment = delivery.get("shipment") or {}
    if not shipment and delivery.get("shipments"):
        shipment = delivery["shipments"][0] or {}

    items = []
    for it in order.get("items", []):
        required = it.get("requiredInstanceTypes") or []
        instances = it.get("instances") or []
        items.append(FbsItem(
            offer_id=str(it.get("offerId") or it.get("shopSku") or "?"),
            name=str(it.get("offerName") or ""),
            count=int(it.get("count") or 0),
            needs_cis=bool(required) and not instances,
        ))

    boxes = len(delivery.get("boxesLayout") or shipment.get("boxes") or []) or 1

    return FbsOrder(
        order_id=int(order.get("orderId") or order.get("id")),
        campaign_id=int(order.get("campaignId") or 0),
        substatus=str(order.get("substatus") or ""),
        shipment_date=_parse_date(shipment.get("shipmentDate")),
        boxes=boxes,
        items=items,
    )


def build_plan(raw_orders: list[dict], ship_date: date) -> FbsPlan:
    plan = FbsPlan(ship_date=ship_date)
    today_iso = ship_date.isoformat()
    overdue_ready = 0
    for raw in raw_orders:
        o = parse_order(raw)
        if o.shipment_date and o.shipment_date < today_iso:
            # READY_TO_SHIP с прошлой даты — уже собран и, скорее всего,
            # уехал/ждёт курьера; в сегодняшний лист не берём.
            if o.substatus != "STARTED":
                overdue_ready += 1
                continue
            o.overdue = True
        plan.orders.append(o)
    plan.orders.sort(key=lambda o: (not o.overdue, o.sort_key))

    overdue = [o for o in plan.orders if o.overdue]
    if overdue:
        plan.warnings.append(
            f"⚠️ Просрочено (дата отгрузки прошла, не собрано): "
            f"{len(overdue)} — включены в план")
    if overdue_ready:
        plan.warnings.append(
            f"ℹ️ Ещё {overdue_ready} заказ(ов) с прошлой датой уже в "
            f"READY_TO_SHIP — в план не включены")
    cis = [o.order_id for o in plan.orders if o.needs_cis]
    if cis:
        plan.warnings.append(
            f"🏷 Нужны коды маркировки (КИЗ), «Собрано» через бота пропустит: "
            f"{', '.join(map(str, cis[:10]))}{'…' if len(cis) > 10 else ''}")
    return plan


def load_plan(client: YMFbsClient, ship_date: date | None = None) -> FbsPlan:
    ship_date = ship_date or today_local()
    raw = list(client.iter_fbs_orders(
        ship_date - timedelta(days=OVERDUE_LOOKBACK_DAYS),
        ship_date + timedelta(days=1)))
    plan = build_plan(raw, ship_date)
    logging.info("FBS plan %s: %d orders, %d units",
                 ship_date, len(plan.orders), plan.total_qty)

    # Сверка с отгрузками — best-effort, ошибка не мешает документам.
    campaign_ids = sorted({o.campaign_id for o in plan.orders if o.campaign_id})
    plan_ids = {o.order_id for o in plan.orders}
    for cid in campaign_ids:
        try:
            for s in client.search_shipments(cid, ship_date):
                plan.shipments.append(FbsShipment(
                    shipment_id=int(s.get("id") or 0),
                    campaign_id=cid,
                    status=str(s.get("status") or ""),
                    interval=_fmt_interval(s.get("planIntervalFrom"),
                                           s.get("planIntervalTo")),
                    order_ids=[int(x) for x in s.get("orderIds") or []],
                ))
        except Exception as e:
            logging.warning("Shipments lookup failed for campaign %s: %s", cid, e)
            plan.warnings.append(f"ℹ️ Отгрузки кампании {cid} не получены: {e}")

    in_shipments = {oid for s in plan.shipments for oid in s.order_ids}
    missing = in_shipments - plan_ids
    if missing:
        plan.warnings.append(
            f"ℹ️ В отгрузке есть {len(missing)} заказ(ов) вне плана "
            f"(другой статус?): {', '.join(map(str, sorted(missing)[:10]))}")
    return plan


def _fmt_interval(frm, to) -> str:
    def hm(v):
        try:
            return datetime.fromisoformat(str(v)).strftime("%H:%M")
        except ValueError:
            return ""
    a, b = hm(frm), hm(to)
    return f"{a}–{b}" if a and b else ""


# ----------------------------------------------------------------------
# Смена статуса (только из бота)
# ----------------------------------------------------------------------
def mark_ready_to_ship(client: YMFbsClient, plan: FbsPlan) -> ReadyResult:
    res = ReadyResult(skipped_cis=[o.order_id for o in plan.orders
                                   if o.substatus == "STARTED" and o.needs_cis])
    by_campaign: dict[int, list[int]] = defaultdict(list)
    for o in plan.started_orders():
        by_campaign[o.campaign_id].append(o.order_id)

    for cid, ids in by_campaign.items():
        for i in range(0, len(ids), STATUS_BATCH):
            batch = ids[i:i + STATUS_BATCH]
            try:
                rows = client.set_ready_to_ship(cid, batch)
            except YMApiError as e:
                hint = (" — у API-ключа нет прав inventory-and-order-processing"
                        if e.status_code in (401, 403) else "")
                res.failed.extend((oid, f"{e}{hint}") for oid in batch)
                continue
            seen = set()
            for r in rows:
                oid = int(r.get("id") or 0)
                seen.add(oid)
                if r.get("updateStatus") == "OK":
                    res.updated.append(oid)
                else:
                    res.failed.append((oid, str(r.get("errorDetails") or "ERROR")))
            for oid in batch:
                if oid not in seen:
                    res.failed.append((oid, "нет в ответе API"))
            time.sleep(RATE_SLEEP)
    return res


# ----------------------------------------------------------------------
# Лист сборки (PDF)
# ----------------------------------------------------------------------
def _register_fonts() -> tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = list(FONT_CANDIDATES)
    env_font = os.environ.get("FBS_FONT_PATH")
    if env_font:
        candidates.insert(0, (env_font, os.environ.get("FBS_FONT_BOLD_PATH")))

    for regular, bold in candidates:
        if regular and os.path.exists(regular):
            pdfmetrics.registerFont(TTFont("FbsSans", regular))
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont("FbsSans-Bold", bold))
                return "FbsSans", "FbsSans-Bold"
            return "FbsSans", "FbsSans"
    # Без кириллического шрифта reportlab молча нарисует квадраты — лучше упасть.
    raise RuntimeError(
        "Не найден TTF-шрифт с кириллицей. Установи `apt install "
        "fonts-dejavu-core` или задай FBS_FONT_PATH.")


def build_picking_pdf(plan: FbsPlan, path: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    font, font_bold = _register_fonts()
    st_title = ParagraphStyle("t", fontName=font_bold, fontSize=15, leading=19)
    st_h = ParagraphStyle("h", fontName=font_bold, fontSize=11.5, leading=15,
                          spaceBefore=4, spaceAfter=4)
    st_txt = ParagraphStyle("p", fontName=font, fontSize=9, leading=12)
    st_cell = ParagraphStyle("c", fontName=font, fontSize=8.5, leading=10.5)
    st_cell_b = ParagraphStyle("cb", fontName=font_bold, fontSize=8.5, leading=10.5)

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    base_style = [
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTNAME", (0, 0), (-1, 0), font_bold),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]

    story = []
    story.append(Paragraph(
        f"Лист сборки FBS — {plan.ship_date.strftime('%d.%m.%Y')}", st_title))
    meta = (f"Заказов: <b>{len(plan.orders)}</b> · Единиц: "
            f"<b>{plan.total_qty}</b> · Грузомест: <b>{plan.total_boxes}</b> · "
            f"Сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    story.append(Paragraph(meta.replace("<b>", f'<font name="{font_bold}">')
                           .replace("</b>", "</font>"), st_txt))
    for s in plan.shipments:
        story.append(Paragraph(
            f"Отгрузка №{s.shipment_id} {s.interval} · {s.status} · "
            f"заказов {len(s.order_ids)}", st_txt))
    story.append(Spacer(1, 5 * mm))

    # --- 1. Сводка по SKU (что взять со склада) ----------------------
    story.append(Paragraph("1. Подбор по товарам", st_h))
    rows = [["✓", "№", "Артикул", "Наименование", "Кол-во", "Заказов"]]
    for i, (sku, name, qty, n_orders) in enumerate(plan.sku_summary(), 1):
        rows.append(["☐", str(i), Paragraph(esc(sku), st_cell_b),
                     Paragraph(esc(name), st_cell), str(qty), str(n_orders)])
    rows.append(["", "", "", Paragraph("Итого", st_cell_b),
                 str(plan.total_qty), str(len(plan.orders))])
    t = Table(rows, colWidths=[8 * mm, 8 * mm, 35 * mm, 98 * mm, 15 * mm, 16 * mm],
              repeatRows=1)
    t.setStyle(TableStyle(base_style + [
        ("ALIGN", (0, 0), (1, -1), "CENTER"),
        ("ALIGN", (4, 0), (5, -1), "CENTER"),
        ("FONTSIZE", (4, 1), (4, -1), 11),
        ("FONTNAME", (4, 1), (4, -1), font_bold),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f4f4f4")),
    ]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))

    # --- 2. По заказам (упаковка; порядок = порядок ярлыков) ---------
    story.append(Paragraph("2. Упаковка по заказам (в порядке ярлыков)", st_h))
    rows = [["✓", "№", "Заказ", "Артикул", "Наименование", "Кол-во", "Мест"]]
    spans = []
    marks = []
    r = 1
    for i, o in enumerate(plan.orders, 1):
        flags = []
        if o.overdue:
            flags.append(f"просрочен ({o.shipment_date})")
        if o.substatus == "READY_TO_SHIP":
            flags.append("уже собран")
        if o.needs_cis:
            flags.append("нужен КИЗ")
        order_cell = Paragraph(
            esc(str(o.order_id)) +
            (f"<br/><font size=7>{esc(', '.join(flags))}</font>" if flags else ""),
            st_cell_b)
        start = r
        for j, it in enumerate(o.items):
            rows.append([
                "☐" if j == 0 else "", str(i) if j == 0 else "",
                order_cell if j == 0 else "",
                Paragraph(esc(it.offer_id), st_cell),
                Paragraph(esc(it.name), st_cell),
                str(it.count), str(o.boxes) if j == 0 else "",
            ])
            r += 1
        if r - start > 1:
            for col in (0, 1, 2, 6):
                spans.append(("SPAN", (col, start), (col, r - 1)))
        if o.overdue or o.needs_cis:
            marks.append(("BACKGROUND", (0, start), (-1, r - 1),
                          colors.HexColor("#fff1e0")))
    t = Table(rows, colWidths=[8 * mm, 8 * mm, 30 * mm, 32 * mm, 86 * mm,
                               14 * mm, 12 * mm],
              repeatRows=1)
    t.setStyle(TableStyle(base_style + spans + marks + [
        ("ALIGN", (0, 0), (1, -1), "CENTER"),
        ("ALIGN", (5, 0), (6, -1), "CENTER"),
        ("FONTNAME", (5, 1), (5, -1), font_bold),
    ]))
    story.append(t)

    if plan.warnings:
        story.append(Spacer(1, 5 * mm))
        for w in plan.warnings:
            # emoji в DejaVu нет — убираем
            story.append(Paragraph(esc(w.lstrip("⚠️ℹ️🏷 ")), st_txt))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.drawRightString(
            A4[0] - 12 * mm, 8 * mm,
            f"FBS {plan.ship_date.isoformat()} · стр. {doc.page}")
        canvas.restoreState()

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=14 * mm,
                            title=f"FBS picking {plan.ship_date.isoformat()}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


# ----------------------------------------------------------------------
# Документы целиком
# ----------------------------------------------------------------------
@dataclass
class FbsDocs:
    picking_path: Path
    labels_path: Path | None = None
    labels_error: str = ""
    drive_url: str = ""
    drive_error: str = ""


def generate_documents(plan: FbsPlan, client: YMFbsClient | None,
                       out_dir: Path) -> FbsDocs:
    d = plan.ship_date.isoformat()
    docs = FbsDocs(picking_path=build_picking_pdf(
        plan, out_dir / f"fbs_picking_{d}.pdf"))
    if client is None:
        docs.labels_error = "ярлыки пропущены (нет клиента API)"
        return docs
    try:
        pdf = client.generate_labels_pdf(
            [o.order_id for o in plan.orders],
            os.environ.get("FBS_LABEL_FORMAT", DEFAULT_LABEL_FORMAT))
        docs.labels_path = out_dir / f"fbs_labels_{d}.pdf"
        docs.labels_path.write_bytes(pdf)
    except Exception as e:
        logging.exception("Labels generation failed")
        docs.labels_error = str(e)
    return docs


# ----------------------------------------------------------------------
# Google Drive (общий диск)
# ----------------------------------------------------------------------
class DriveUploader:
    """Минимальный клиент Drive v3 поверх requests.

    Сервисный аккаунт не имеет своей квоты — файлы можно класть только
    на ОБЩИЙ диск (Shared Drive), куда SA добавлен «Менеджером контента».
    Поэтому везде supportsAllDrives / includeItemsFromAllDrives.
    """

    def __init__(self, creds_path: str):
        from oauth2client.service_account import ServiceAccountCredentials
        self.creds = ServiceAccountCredentials.from_json_keyfile_name(
            creds_path, ["https://www.googleapis.com/auth/drive"])
        self.session = requests.Session()

    def _headers(self) -> dict:
        token = self.creds.get_access_token().access_token
        return {"Authorization": f"Bearer {token}"}

    def _find(self, parent_id: str, name: str, mime: str | None = None) -> str | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"'{parent_id}' in parents and name = '{safe}' and trashed = false"
        if mime:
            q += f" and mimeType = '{mime}'"
        resp = self.session.get(f"{DRIVE_API}/files", headers=self._headers(),
                                params={"q": q, "fields": "files(id)",
                                        "supportsAllDrives": "true",
                                        "includeItemsFromAllDrives": "true",
                                        "corpora": "allDrives"},
                                timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        files = resp.json().get("files", [])
        return files[0]["id"] if files else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        found = self._find(parent_id, name, DRIVE_FOLDER_MIME)
        if found:
            return found
        resp = self.session.post(
            f"{DRIVE_API}/files", headers=self._headers(),
            params={"supportsAllDrives": "true", "fields": "id"},
            json={"name": name, "mimeType": DRIVE_FOLDER_MIME,
                  "parents": [parent_id]},
            timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["id"]

    def upload(self, parent_id: str, path: Path,
               mime: str = "application/pdf") -> str:
        """Загружает файл; если с таким именем уже есть — заменяет содержимое."""
        content = path.read_bytes()
        existing = self._find(parent_id, path.name)
        if existing:
            resp = self.session.patch(
                f"{DRIVE_UPLOAD_API}/files/{existing}",
                headers={**self._headers(), "Content-Type": mime},
                params={"uploadType": "media", "supportsAllDrives": "true",
                        "fields": "id"},
                data=content, timeout=120)
        else:
            boundary = "fbs-upload-boundary"
            meta = json.dumps({"name": path.name, "parents": [parent_id]})
            body = (f"--{boundary}\r\nContent-Type: application/json; "
                    f"charset=UTF-8\r\n\r\n{meta}\r\n--{boundary}\r\n"
                    f"Content-Type: {mime}\r\n\r\n").encode() + content + \
                f"\r\n--{boundary}--".encode()
            resp = self.session.post(
                f"{DRIVE_UPLOAD_API}/files",
                headers={**self._headers(),
                         "Content-Type": f"multipart/related; boundary={boundary}"},
                params={"uploadType": "multipart", "supportsAllDrives": "true",
                        "fields": "id"},
                data=body, timeout=120)
        resp.raise_for_status()
        return resp.json()["id"]


def upload_to_drive(docs: FbsDocs, ship_date: date) -> None:
    """Заполняет docs.drive_url или docs.drive_error. Не бросает."""
    root = os.environ.get("GDRIVE_FBS_FOLDER_ID", "").strip()
    if not root:
        docs.drive_error = "GDRIVE_FBS_FOLDER_ID не задан — на Drive не выкладывали"
        return
    try:
        up = DriveUploader(os.environ.get("GOOGLE_CREDS",
                                          "/opt/openclaw/secrets/gcp-sa.json"))
        folder = up.ensure_folder(root, ship_date.isoformat())
        for p in (docs.picking_path, docs.labels_path):
            if p:
                up.upload(folder, p)
        docs.drive_url = f"https://drive.google.com/drive/folders/{folder}"
    except Exception as e:
        logging.exception("Drive upload failed")
        msg = str(e)
        if "storageQuotaExceeded" in msg or "403" in msg:
            msg += (" — папка должна быть на ОБЩЕМ диске, SA добавлен "
                    "«Менеджером контента»")
        docs.drive_error = msg


# ----------------------------------------------------------------------
# Текст для Telegram
# ----------------------------------------------------------------------
def format_summary(plan: FbsPlan, max_skus: int = 25) -> str:
    import html as _html
    d = plan.ship_date.strftime("%d.%m.%Y")
    if not plan.orders:
        lines = [f"🚚 <b>FBS на {d}</b>", "", "✅ Отгружать сегодня нечего"]
        lines += [""] + plan.warnings if plan.warnings else []
        return "\n".join(lines)

    started = sum(1 for o in plan.orders if o.substatus == "STARTED")
    lines = [
        f"🚚 <b>FBS на {d}</b>",
        f"Заказов: <b>{len(plan.orders)}</b> · единиц: <b>{plan.total_qty}</b> "
        f"· мест: <b>{plan.total_boxes}</b>",
        f"Не собрано: <b>{started}</b> · собрано: {len(plan.orders) - started}",
    ]
    for s in plan.shipments:
        lines.append(f"📋 Отгрузка №{s.shipment_id} {s.interval} "
                     f"({len(s.order_ids)} заказ.)")
    lines.append("")
    summary = plan.sku_summary()
    for sku, name, qty, _ in summary[:max_skus]:
        short = name if len(name) <= 40 else name[:39] + "…"
        lines.append(f"• <code>{_html.escape(sku)}</code> × <b>{qty}</b>"
                     + (f" — {_html.escape(short)}" if short else ""))
    if len(summary) > max_skus:
        lines.append(f"…и ещё {len(summary) - max_skus} SKU (в листе сборки)")
    if plan.warnings:
        lines.append("")
        lines += [_html.escape(w) for w in plan.warnings]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def make_client() -> YMFbsClient:
    return YMFbsClient(os.environ["YM_API_KEY"], int(os.environ["YM_BUSINESS_ID"]))


def main():
    p = argparse.ArgumentParser(description="FBS: лист сборки + ярлыки на сегодня")
    p.add_argument("--date", help="Дата отгрузки YYYY-MM-DD (по умолчанию сегодня)")
    p.add_argument("--out", default="fbs_out", help="Куда сохранить PDF")
    p.add_argument("--dry-run", action="store_true",
                   help="Не выкладывать на Google Drive")
    p.add_argument("--fixture", help="JSON со списком заказов вместо API "
                                     "(тест вёрстки; ярлыки пропускаются)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        ship_date = (date.fromisoformat(args.date) if args.date
                     else today_local())
        if args.fixture:
            raw = json.loads(Path(args.fixture).read_text("utf-8"))
            plan = build_plan(raw.get("orders", raw) if isinstance(raw, dict)
                              else raw, ship_date)
            client = None
        else:
            client = make_client()
            plan = load_plan(client, ship_date)

        print(format_summary(plan))
        if not plan.orders:
            sys.exit(0)

        docs = generate_documents(plan, client, Path(args.out))
        logging.info("Picking list: %s", docs.picking_path)
        if docs.labels_path:
            logging.info("Labels: %s", docs.labels_path)
        else:
            logging.warning("Labels: %s", docs.labels_error)

        if not args.dry_run:
            upload_to_drive(docs, ship_date)
            if docs.drive_url:
                logging.info("Drive: %s", docs.drive_url)
            else:
                logging.error("Drive: %s", docs.drive_error)
                sys.exit(1)
        if client is not None and not docs.labels_path:
            sys.exit(1)
    except KeyError as e:
        logging.error("Missing env var: %s", e)
        sys.exit(1)
    except Exception:
        logging.exception("FBS shipment failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
