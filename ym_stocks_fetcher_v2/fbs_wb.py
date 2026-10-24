"""
fbs_wb.py — модуль 7.1: FBS-сборка Wildberries (этапы 1–3).

Полностью независим от Яндекс-модуля (fbs_shipment.py): ни одного импорта
в обе стороны, свои модели, свой HTTP-клиент, свой лист сборки. Проект —
DESIGN_FBS_WB.md.

Что делает (этап 1):
1. GET /api/v3/warehouses — склады продавца (выбор «где собирать»).
2. GET /api/v3/orders/new — все новые сборочные задания (даты у WB нет:
   собирается всё, что есть на складе).
3. GET /api/v3/supplies + /api/marketplace/v3/supplies/{id}/order-ids —
   открытые поставки и задания в них («уже на сборке»); детали заданий —
   GET /api/v3/orders, статусы — POST /api/v3/orders/status (отменённые
   помечаются).
4. План по одному складу: группы (cargoType, crossBorderType, isB2b) —
   столько поставок понадобится на этапе 2; лист сборки PDF (reportlab).

Этап 2 (ЗАПИСЬ в WB, только явно: кнопки бота или --take / --boxes):
5. take_into_supplies() — «Собрать поставку»: для каждой группы открытая
   поставка (auto) или новая (POST /api/v3/supplies), задания добавляются
   PATCH /api/marketplace/v3/supplies/{id}/orders (по 100) → статус confirm.
6. Документы поставки: стикеры заданий (POST /api/v3/orders/stickers,
   PNG 58×40 → PDF в порядке листа) + лист сборки → Google Drive
   (GDRIVE_FBS_WB_FOLDER_ID/YYYY-MM-DD/, своя папка, не YM).
7. set_boxes() — грузоместа: довести число до K (POST/DELETE …/trbx) и
   стикеры мест (POST …/trbx/stickers). K спрашивается у человека.

Этап 3 (ЗАПИСЬ в WB, только явно: кнопка «Сдать в ПВЗ» с подтверждением
или --deliver … --yes):
8. Пункт отгрузки — ПВЗ из FBS_WB_SHIPPING_POINT (ID или адрес, напр.
   «Москва, проспект Лихачёва, 22»): ищется среди
   GET /api/marketplace/v3/fbs/supplies/{id}/shipping-points (officeType=pp,
   shippingDt=сегодня) по адресу/названию.
9. deliver_supply(): проверки (открыта, ПВЗ, грузоместа, маркировка) →
   PATCH …/fbs/supplies/shipping-method (selfShipping, сегодня, ПВЗ) →
   PATCH /api/v3/supplies/{id}/deliver → QR поставки (GET …/barcode).

Запуск:
  python fbs_wb.py --list-warehouses
  python fbs_wb.py --warehouse офис --verbose        # сводка + PDF в ./fbs_wb_out
  python fbs_wb.py --explain 123456,654321           # почему задание не в листе
  python fbs_wb.py --fixture wb_orders.json --warehouse 1   # без API (тест вёрстки)
  python fbs_wb.py --warehouse офис --take [--mode new] [--no-drive]   # ЗАПИСЬ
  python fbs_wb.py --boxes WB-GI-123456:2                              # ЗАПИСЬ
  python fbs_wb.py --points WB-GI-123456 "Лихачёва 22"   # найти ПВЗ (чтение)
  python fbs_wb.py --deliver WB-GI-123456 [--point ID]   # проверка, без записи
  python fbs_wb.py --deliver WB-GI-123456 --yes          # ЗАПИСЬ: сдать в ПВЗ
  python fbs_wb.py --qr WB-GI-123456                     # QR сданной поставки
  WB_SANDBOX=1 python fbs_wb.py …                    # песочница WB
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
API_BASE = "https://marketplace-api.wildberries.ru"
SANDBOX_API_BASE = "https://marketplace-api-sandbox.wildberries.ru"
WAREHOUSES_ENDPOINT = "/api/v3/warehouses"
ORDERS_NEW_ENDPOINT = "/api/v3/orders/new"
ORDERS_ENDPOINT = "/api/v3/orders"
ORDERS_STATUS_ENDPOINT = "/api/v3/orders/status"
SUPPLIES_ENDPOINT = "/api/v3/supplies"
SUPPLY_ORDER_IDS_ENDPOINT = "/api/marketplace/v3/supplies/{supply_id}/order-ids"
ORDERS_META_ENDPOINT = "/api/marketplace/v3/orders/meta"
SUPPLY_ENDPOINT = "/api/v3/supplies/{supply_id}"
SUPPLY_ADD_ORDERS_ENDPOINT = "/api/marketplace/v3/supplies/{supply_id}/orders"
STICKERS_ENDPOINT = "/api/v3/orders/stickers"
TRBX_ENDPOINT = "/api/v3/supplies/{supply_id}/trbx"
TRBX_STICKERS_ENDPOINT = "/api/v3/supplies/{supply_id}/trbx/stickers"
SHIPPING_POINTS_ENDPOINT = "/api/marketplace/v3/fbs/supplies/{supply_id}/shipping-points"
SHIPPING_METHOD_ENDPOINT = "/api/marketplace/v3/fbs/supplies/shipping-method"
DELIVER_ENDPOINT = "/api/v3/supplies/{supply_id}/deliver"
SUPPLY_BARCODE_ENDPOINT = "/api/v3/supplies/{supply_id}/barcode"

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

HTTP_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
MIN_INTERVAL = 0.25           # лимит WB: 300 запросов/мин, интервал 200 мс
PAGE_LIMIT = 1000             # максимум limit для /orders и /supplies
STATUS_BATCH = 1000           # максимум заданий в /orders/status
META_BATCH = 100              # максимум заданий в /orders/meta
ADD_BATCH = 100               # максимум заданий в добавлении к поставке
STICKERS_BATCH = 100          # максимум стикеров за запрос
ADD_SINGLE_FALLBACK = 20      # при 409 на пачку — по одному, не больше N (4XX = 10 запросов)
SUPPLY_NAME_PREFIX = "FBS WB"  # «FBS WB <складID> ДД.ММ ЧЧ:ММ» — по нему узнаём свои пустые поставки
SUPPLY_ID_RE = r"^WB-GI-\d+$"
BOX_BUTTONS = 6               # кнопок выбора числа грузомест
POINT_BUTTONS = 6             # кнопок выбора ПВЗ, если адрес совпал с несколькими
DEFAULT_CITY = "Москва"       # город ПВЗ, если не задан в адресе / FBS_WB_CITY
SHIPPING_TYPE = "selfShipping"  # везём сами (DESIGN_FBS_WB.md §4.1)
ORDERS_LOOKBACK_DAYS = 30     # глубина поиска деталей заданий из открытых поставок
ORDERS_MAX_LOOKBACK_DAYS = 89 # /orders с 06.08.2026 отдаёт только 3 месяца
ORDERS_WINDOW_DAYS = 29       # /orders: «максимум 30 календарных дней одним запросом»

# Статусы проверки маркировки, с которыми поставку МОЖНО передать в доставку.
# Всё остальное (required, pending, *Invalid*, *NotFound* …) — блокирует.
META_OK = {"filled", "optional", "deadlineExceeded"}
META_OK_SUFFIXES = ("MaySell", "SoldB2B", "Introduced")

DEFAULT_TZ = "Europe/Moscow"
SUPPLY_MODES = ("auto", "new")
STICKER_FORMATS = ("58x40", "40x30", "zpl")
DEFAULT_STICKER = "58x40"

CARGO_NAMES = {1: "МГТ", 2: "СГТ", 3: "КГТ+"}
CANCELLED_SUPPLIER = {"cancel", "cancel_carrier"}
CANCELLED_WB = {"canceled", "canceled_by_client", "declined_by_client",
                "defect", "canceled_by_carrier"}

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


def now_local() -> datetime:
    return datetime.now(ZoneInfo(os.environ.get("FBS_TZ", DEFAULT_TZ)))


# ----------------------------------------------------------------------
# Структуры
# ----------------------------------------------------------------------
@dataclass
class WbWarehouse:
    id: int
    name: str
    office_id: int = 0
    cargo_type: int = 0


@dataclass
class WbOrder:
    order_id: int
    warehouse_id: int
    article: str               # артикул продавца
    nm_id: int                 # артикул WB
    barcode: str
    cargo_type: int
    cross_border: int
    is_b2b: bool
    pp_allowed: bool = True    # isPickupPointShipmentAllowed: можно ли сдать в ПВЗ
    # Маркировка, которую ещё нужно закрепить/исправить («sgtin: required»).
    # Для новых — из requiredMeta, затем уточняется по /orders/meta.
    required_meta: list[str] = field(default_factory=list)
    meta_checked: bool = False
    comment: str = ""
    created_at: str = ""
    supply_id: str = ""        # пусто — ещё не в поставке (status new)
    supplier_status: str = "new"
    wb_status: str = ""
    sticker: str = ""          # «partA partB» — подпись стикера, после получения

    @property
    def group_key(self) -> tuple:
        """Признаки, которые WB фиксирует у поставки по первому заданию,
        + можно ли сдать в ПВЗ: по умолчанию отгружаем в ПВЗ, а задания
        «не для ПВЗ» нужно везти отдельной поставкой на СЦ/склад."""
        return (self.warehouse_id, self.cargo_type, self.cross_border,
                self.is_b2b, self.pp_allowed)

    @property
    def cancelled(self) -> bool:
        return (self.supplier_status in CANCELLED_SUPPLIER
                or self.wb_status in CANCELLED_WB)

    @property
    def needs_meta(self) -> bool:
        return bool(self.required_meta)

    @property
    def flags(self) -> list[str]:
        out = []
        if self.cancelled:
            out.append("ОТМЕНЁН")
        if self.supply_id:
            out.append(f"в поставке {self.supply_id}")
        if self.needs_meta:
            out.append("нужна маркировка: " + ", ".join(self.required_meta))
        elif not self.meta_checked and self.supply_id:
            out.append("маркировка не проверена")
        if self.cargo_type not in (0, 1):
            out.append(CARGO_NAMES.get(self.cargo_type, f"cargo {self.cargo_type}"))
        if self.cross_border:
            out.append("трансграничный")
        if self.is_b2b:
            out.append("B2B")
        if not self.pp_allowed:
            out.append("не для ПВЗ")
        return out


@dataclass
class WbSupply:
    supply_id: str
    name: str
    created_at: str
    cargo_type: int = 0
    cross_border: int = 0
    is_b2b: bool = False
    pp_allowed: bool = True
    order_ids: list[int] = field(default_factory=list)
    warehouse_id: int = 0      # у поставки в API нет склада — берём по её заданиям

    @property
    def group_key(self) -> tuple:
        """Как WbOrder.group_key — с какими заданиями совместима поставка."""
        return (self.warehouse_id, self.cargo_type, self.cross_border,
                self.is_b2b, self.pp_allowed)


@dataclass
class WbOverview:
    """Всё, что сейчас «в работе» у продавца, по всем складам."""
    warehouses: list[WbWarehouse]
    new_orders: list[WbOrder]
    open_supplies: list[WbSupply]
    in_assembly: list[WbOrder]          # задания из открытых поставок
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[int, int]:
        """warehouse_id → сколько заданий собирать (новые + на сборке)."""
        cnt: dict[int, int] = defaultdict(int)
        for o in self.new_orders + self.in_assembly:
            if not o.cancelled:
                cnt[o.warehouse_id] += 1
        return dict(cnt)

    def warehouse(self, wh_id: int) -> WbWarehouse:
        for w in self.warehouses:
            if w.id == wh_id:
                return w
        return WbWarehouse(id=wh_id, name=f"склад {wh_id}")


@dataclass
class WbPlan:
    warehouse: WbWarehouse
    new_orders: list[WbOrder] = field(default_factory=list)
    in_assembly: list[WbOrder] = field(default_factory=list)
    supplies: list[WbSupply] = field(default_factory=list)
    other_warehouses: list[tuple[WbWarehouse, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=now_local)
    # Открытые пустые поставки, созданные ботом для этого склада (по имени) —
    # переиспользуем, чтобы повторный «Собрать» после сбоя не плодил пустые.
    empty_supplies: list[WbSupply] = field(default_factory=list)

    @property
    def orders(self) -> list[WbOrder]:
        """Что собирать физически: на сборке (без отменённых) + новые.
        Порядок = порядок листа сборки (и стикеров на этапе 2)."""
        active = [o for o in self.in_assembly + self.new_orders if not o.cancelled]
        return sorted(active, key=lambda o: (o.supply_id == "", o.supply_id,
                                             o.article, o.order_id))

    @property
    def to_take(self) -> list[WbOrder]:
        """Новые задания, которые ещё нужно положить в поставку."""
        return [o for o in self.new_orders if not o.cancelled]

    @property
    def cancelled(self) -> list[WbOrder]:
        return [o for o in self.in_assembly + self.new_orders if o.cancelled]

    @property
    def total_qty(self) -> int:
        # Одно сборочное задание WB = одна единица товара.
        return len(self.orders)

    def groups(self) -> dict[tuple, list[WbOrder]]:
        """Новые задания по ключу поставки — сколько поставок нужно на этапе 2."""
        g: dict[tuple, list[WbOrder]] = defaultdict(list)
        for o in self.to_take:
            g[o.group_key].append(o)
        return dict(g)

    def for_supply(self, supply_id: str) -> WbPlan:
        """Подплан одной поставки — для её листа сборки и стикеров."""
        sub = WbPlan(warehouse=self.warehouse, generated_at=self.generated_at)
        sub.in_assembly = [o for o in self.in_assembly if o.supply_id == supply_id]
        sub.supplies = [s for s in self.supplies if s.supply_id == supply_id]
        return sub

    def sku_summary(self) -> list[tuple[str, int, str, int]]:
        """[(article, nm_id, barcode, qty)], по артикулу продавца."""
        qty: dict[str, int] = defaultdict(int)
        nm: dict[str, int] = {}
        bc: dict[str, str] = {}
        for o in self.orders:
            qty[o.article] += 1
            nm.setdefault(o.article, o.nm_id)
            bc.setdefault(o.article, o.barcode)
        return [(a, nm[a], bc[a], qty[a]) for a in sorted(qty)]


# ----------------------------------------------------------------------
# WB API клиент
# ----------------------------------------------------------------------
class WBApiError(RuntimeError):
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        try:
            self.body = json.loads(text) if text else {}
        except ValueError:
            self.body = {}
        if not isinstance(self.body, dict):
            self.body = {}
        self.code = str(self.body.get("code") or "")   # напр. SupplyShippingRequired
        super().__init__(f"HTTP {status_code}: {text[:500]}")


class WBFbsClient:
    def __init__(self, token: str, base_url: str = API_BASE):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
        })
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self._last_call + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        last_err = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                resp = self.session.request(method, url,
                                            timeout=HTTP_TIMEOUT, **kwargs)
            except requests.RequestException as e:
                last_err = e
                wait = RETRY_BACKOFF ** attempt
                logging.warning("Request failed: %s, retry in %.1fs", e, wait)
                time.sleep(wait)
                continue
            if resp.status_code in (200, 201, 204):
                return resp.json() if resp.content else {}
            if resp.status_code == 429:
                # WB подсказывает, сколько ждать; иначе — экспонента.
                try:
                    wait = float(resp.headers.get("X-Ratelimit-Retry", ""))
                except ValueError:
                    wait = RETRY_BACKOFF ** attempt * 5
                logging.warning("Rate limit (429), waiting %.1fs", wait)
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF ** attempt
                logging.warning("Server error %d, retry in %.1fs",
                                resp.status_code, wait)
                time.sleep(wait)
                continue
            # 4XX не ретраим: каждый стоит 10 запросов из лимита.
            raise WBApiError(resp.status_code, resp.text)
        raise RuntimeError(f"Max retries exceeded: {last_err}")

    # --- справочники --------------------------------------------------
    def get_warehouses(self) -> list[dict]:
        data = self._request("GET", WAREHOUSES_ENDPOINT)
        return data if isinstance(data, list) else data.get("warehouses", [])

    # --- задания ------------------------------------------------------
    def get_new_orders(self) -> list[dict]:
        return self._request("GET", ORDERS_NEW_ENDPOINT).get("orders") or []

    def iter_orders(self, date_from: datetime, date_to: datetime) -> Iterator[dict]:
        """Задания, созданные в [date_from, date_to]; период ≤ 30 дней —
        иначе WB молча режет его (свежие задания не приходят)."""
        nxt = 0
        while True:
            data = self._request("GET", ORDERS_ENDPOINT, params={
                "limit": PAGE_LIMIT, "next": nxt,
                "dateFrom": int(date_from.timestamp()),
                "dateTo": int(date_to.timestamp())})
            orders = data.get("orders") or []
            yield from orders
            nxt = data.get("next") or 0
            if len(orders) < PAGE_LIMIT or not nxt:
                break

    def get_statuses(self, order_ids: list[int]) -> dict[int, tuple[str, str]]:
        """order_id → (supplierStatus, wbStatus)."""
        out: dict[int, tuple[str, str]] = {}
        for i in range(0, len(order_ids), STATUS_BATCH):
            data = self._request("POST", ORDERS_STATUS_ENDPOINT,
                                 json={"orders": order_ids[i:i + STATUS_BATCH]})
            for r in data.get("orders") or []:
                out[int(r["id"])] = (str(r.get("supplierStatus") or ""),
                                     str(r.get("wbStatus") or ""))
        return out

    def get_meta(self, order_ids: list[int]) -> dict[int, list[dict]]:
        """order_id → metaDetails [{key, value, decision}]."""
        out: dict[int, list[dict]] = {}
        for i in range(0, len(order_ids), META_BATCH):
            data = self._request("POST", ORDERS_META_ENDPOINT,
                                 json={"orders": order_ids[i:i + META_BATCH]})
            for r in data.get("orders") or []:
                out[int(r["id"])] = list(r.get("metaDetails") or [])
        return out

    # --- поставки -----------------------------------------------------
    def iter_supplies(self) -> Iterator[dict]:
        """Все поставки продавца. Фильтра по done у API нет — фильтруем сами."""
        nxt = 0
        while True:
            data = self._request("GET", SUPPLIES_ENDPOINT,
                                 params={"limit": PAGE_LIMIT, "next": nxt})
            supplies = data.get("supplies") or []
            yield from supplies
            nxt = data.get("next") or 0
            if len(supplies) < PAGE_LIMIT or not nxt:
                break

    def get_supply_order_ids(self, supply_id: str) -> list[int]:
        data = self._request(
            "GET", SUPPLY_ORDER_IDS_ENDPOINT.format(supply_id=supply_id))
        return [int(x) for x in data.get("orderIds") or []]

    def get_supply(self, supply_id: str) -> dict:
        return self._request("GET", SUPPLY_ENDPOINT.format(supply_id=supply_id))

    # --- запись (этап 2) ---------------------------------------------
    def create_supply(self, name: str) -> str:
        data = self._request("POST", SUPPLIES_ENDPOINT, json={"name": name[:128]})
        return str(data["id"])

    def add_orders(self, supply_id: str, order_ids: list[int]) -> None:
        """До ADD_BATCH заданий за раз; 204. Задания → confirm."""
        self._request("PATCH",
                      SUPPLY_ADD_ORDERS_ENDPOINT.format(supply_id=supply_id),
                      json={"orders": order_ids})

    def get_stickers(self, order_ids: list[int], fmt: str) -> dict[int, dict]:
        """order_id → {partA, partB, barcode, file}. fmt: 58x40 | 40x30 | zpl."""
        if fmt == "zpl":
            params = {"type": "zplh", "width": 58, "height": 40}
        else:
            w, h = (int(x) for x in fmt.split("x"))
            params = {"type": "png", "width": w, "height": h}
        out: dict[int, dict] = {}
        for i in range(0, len(order_ids), STICKERS_BATCH):
            data = self._request("POST", STICKERS_ENDPOINT, params=params,
                                 json={"orders": order_ids[i:i + STICKERS_BATCH]})
            for s in data.get("stickers") or []:
                out[int(s["orderId"])] = s
        return out

    def list_trbx(self, supply_id: str) -> list[str]:
        data = self._request("GET", TRBX_ENDPOINT.format(supply_id=supply_id))
        return [str(t["id"]) for t in data.get("trbxes") or []]

    def add_trbx(self, supply_id: str, amount: int) -> list[str]:
        data = self._request("POST", TRBX_ENDPOINT.format(supply_id=supply_id),
                             json={"amount": amount})
        return [str(x) for x in data.get("trbxIds") or []]

    def delete_trbx(self, supply_id: str, trbx_ids: list[str]) -> None:
        self._request("DELETE", TRBX_ENDPOINT.format(supply_id=supply_id),
                      json={"trbxIds": trbx_ids})

    def get_trbx_stickers(self, supply_id: str, trbx_ids: list[str],
                          fmt: str) -> list[dict]:
        """[{barcode, file}] в порядке trbx_ids (насколько отдаёт API)."""
        data = self._request(
            "POST", TRBX_STICKERS_ENDPOINT.format(supply_id=supply_id),
            params={"type": "zplh" if fmt == "zpl" else "png"},
            json={"trbxIds": trbx_ids})
        return list(data.get("stickers") or [])

    # --- сдача в ПВЗ (этап 3) -----------------------------------------
    def get_shipping_points(self, supply_id: str, city: str, office_type: str,
                            shipping_dt: str) -> list[dict]:
        """Пункты отгрузки поставки. В поставке должно быть ≥1 задание
        (иначе 409 SupplyHasZeroOrders). shippingDt шлём всегда: для МГТ/КГТ+
        обязателен, и тогда available — про эту дату."""
        data = self._request(
            "GET", SHIPPING_POINTS_ENDPOINT.format(supply_id=supply_id),
            params={"city": city, "officeType": office_type,
                    "shippingDt": shipping_dt})
        return list(data.get("shippingPoints") or [])

    def set_shipping_method(self, supply_id: str, point_id: int,
                            shipping_dt: str, shipping_type: str) -> dict:
        """HTTP 200 даже при ошибке: результат — по каждой поставке в results[]."""
        data = self._request("PATCH", SHIPPING_METHOD_ENDPOINT, json={"data": [{
            "supplyId": supply_id, "shippingPointId": int(point_id),
            "shippingDt": shipping_dt, "shippingType": shipping_type}]})
        for r in data.get("results") or []:
            if r.get("supplyId") == supply_id:
                return r
        return {}

    def deliver(self, supply_id: str) -> None:
        """Закрывает поставку, задания → complete. Необратимо."""
        self._request("PATCH", DELIVER_ENDPOINT.format(supply_id=supply_id))

    def get_supply_barcode(self, supply_id: str, fmt: str) -> dict:
        """QR поставки {barcode, file}; только для переданной в доставку."""
        return self._request(
            "GET", SUPPLY_BARCODE_ENDPOINT.format(supply_id=supply_id),
            params={"type": "zplh" if fmt == "zpl" else "png"})


# ----------------------------------------------------------------------
# Парсинг
# ----------------------------------------------------------------------
def parse_warehouse(raw: dict) -> WbWarehouse:
    return WbWarehouse(
        id=int(raw.get("id") or 0),
        name=str(raw.get("name") or f"склад {raw.get('id')}"),
        office_id=int(raw.get("officeId") or 0),
        cargo_type=int(raw.get("cargoType") or 0),
    )


def parse_order(raw: dict) -> WbOrder:
    skus = raw.get("skus") or []
    return WbOrder(
        order_id=int(raw["id"]),
        warehouse_id=int(raw.get("warehouseId") or 0),
        article=str(raw.get("article") or "?"),
        nm_id=int(raw.get("nmId") or 0),
        barcode=str(skus[0]) if skus else "",
        cargo_type=int(raw.get("cargoType") or 0),
        cross_border=int(raw.get("crossBorderType") or 0),
        is_b2b=bool((raw.get("options") or {}).get("isB2B")),
        pp_allowed=raw.get("isPickupPointShipmentAllowed") is not False,
        required_meta=[str(m) for m in raw.get("requiredMeta") or []],
        comment=str(raw.get("comment") or ""),
        created_at=str(raw.get("createdAt") or ""),
        supply_id=str(raw.get("supplyId") or ""),
    )


def parse_supply(raw: dict) -> WbSupply:
    return WbSupply(
        supply_id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        created_at=str(raw.get("createdAt") or ""),
        cargo_type=int(raw.get("cargoType") or 0),
        cross_border=int(raw.get("crossBorderType") or 0),
        is_b2b=bool(raw.get("isB2b")),
        pp_allowed=raw.get("isPickupPointShipmentAllowed") is not False,
    )


def meta_blockers(details: list[dict]) -> list[str]:
    """Маркировка, из-за которой поставку нельзя передать в доставку."""
    out = []
    for d in details:
        decision = str(d.get("decision") or "")
        if decision in META_OK or decision.endswith(META_OK_SUFFIXES):
            continue
        out.append(f"{d.get('key') or '?'}: {decision or '?'}")
    return out


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ----------------------------------------------------------------------
# Загрузка и план
# ----------------------------------------------------------------------
def iter_orders_since(client: WBFbsClient, date_from: datetime,
                      until: datetime | None = None) -> Iterator[dict]:
    """/orders окнами по ORDERS_WINDOW_DAYS — от сейчас назад до date_from
    (свежие — первыми: вызывающий может остановиться, найдя всё нужное)."""
    end = until or datetime.now(timezone.utc) + timedelta(minutes=5)
    while end > date_from:
        start = max(date_from, end - timedelta(days=ORDERS_WINDOW_DAYS))
        yield from client.iter_orders(start, end)
        end = start


def load_overview(client: WBFbsClient,
                  known: dict[int, WbOrder] | None = None) -> WbOverview:
    """known — задания, детали которых уже есть (только что взятые в поставку):
    подстраховка, если /orders их ещё не отдаёт."""
    new_orders = [parse_order(o) for o in client.get_new_orders()]
    warnings: list[str] = []
    try:
        warehouses = [parse_warehouse(w) for w in client.get_warehouses()]
    except Exception as e:
        # Справочник складов — для имён; без него работаем по warehouseId.
        logging.warning("Warehouses lookup failed: %s", e)
        warnings.append(f"ℹ️ Список складов не получен ({e}) — склады по ID")
        warehouses = []

    open_supplies = []
    for raw in client.iter_supplies():
        if raw.get("done"):
            continue
        s = parse_supply(raw)
        s.order_ids = client.get_supply_order_ids(s.supply_id)
        open_supplies.append(s)

    in_assembly: list[WbOrder] = []
    wanted = {oid: s for s in open_supplies for oid in s.order_ids}
    if wanted:
        # Детали заданий «на сборке» есть только в /orders (по периоду).
        oldest = [t for s in open_supplies if s.order_ids
                  for t in [_parse_ts(s.created_at)] if t]
        now = datetime.now(timezone.utc)
        date_from = min(oldest + [now - timedelta(days=ORDERS_LOOKBACK_DAYS)])
        date_from = max(date_from - timedelta(days=1),
                        now - timedelta(days=ORDERS_MAX_LOOKBACK_DAYS))
        found: dict[int, WbOrder] = {}
        for raw in iter_orders_since(client, date_from):
            oid = int(raw.get("id") or 0)
            if oid in wanted:
                found[oid] = parse_order(raw)
                if len(found) == len(wanted):
                    break
        for oid in set(wanted) - set(found):
            if known and oid in known:
                k = known[oid]
                found[oid] = replace(k)
        for oid, o in found.items():
            sup = wanted[oid]
            o.supply_id = sup.supply_id
            # В /orders нет isPickupPointShipmentAllowed и options.isB2B —
            # их WB фиксирует у поставки, берём оттуда.
            o.pp_allowed, o.is_b2b = sup.pp_allowed, sup.is_b2b
            in_assembly.append(o)
        missing = set(wanted) - set(found)
        if missing:
            sids = sorted({wanted[oid].supply_id for oid in missing})
            warnings.append(
                f"ℹ️ {len(missing)} задани(й) из открытых поставок "
                f"{', '.join(sids[:5])} не найдены в /orders — их нет в листе: "
                f"{', '.join(map(str, sorted(missing)[:10]))}")

    # Статусы — и для новых: покупатель мог отменить заказ после /orders/new.
    statuses = client.get_statuses([o.order_id for o in new_orders + in_assembly])
    for o in new_orders + in_assembly:
        o.supplier_status, o.wb_status = statuses.get(
            o.order_id, (o.supplier_status, o.wb_status))

    # Маркировка: в /orders (на сборке) нет requiredMeta — проверяем по /orders/meta.
    to_check = [o for o in in_assembly + new_orders
                if not o.cancelled and (o.supply_id or o.required_meta)]
    if to_check:
        try:
            meta = client.get_meta([o.order_id for o in to_check])
            for o in to_check:
                if o.order_id in meta:
                    o.required_meta = meta_blockers(meta[o.order_id])
                    o.meta_checked = True
        except Exception as e:
            logging.warning("Meta lookup failed: %s", e)
            warnings.append(f"ℹ️ Маркировка заданий на сборке не проверена: {e}")

    by_order = {o.order_id: o.warehouse_id for o in in_assembly}
    for s in open_supplies:
        whs = {by_order[oid] for oid in s.order_ids if oid in by_order}
        if len(whs) == 1:
            s.warehouse_id = whs.pop()

    known = {w.id for w in warehouses}
    unknown = {o.warehouse_id for o in new_orders + in_assembly} - known
    if unknown and warehouses:
        warnings.append(f"ℹ️ Задания на складах вне списка /warehouses: "
                        f"{', '.join(map(str, sorted(unknown)))}")
    # Без справочника (или вне его) — склад по ID, чтобы выбор всё равно работал.
    warehouses += [WbWarehouse(id=w, name=f"склад {w}") for w in sorted(unknown)]
    logging.info("WB overview: %d warehouses, %d new, %d open supplies, %d in assembly",
                 len(warehouses), len(new_orders), len(open_supplies), len(in_assembly))
    return WbOverview(warehouses, new_orders, open_supplies, in_assembly, warnings)


class WarehouseChoiceNeeded(Exception):
    """Склад не задан и однозначно не определяется — нужен выбор."""

    def __init__(self, options: list[tuple[WbWarehouse, int]], message: str):
        self.options = options
        super().__init__(message)


def resolve_warehouse(ov: WbOverview, ref: str | None) -> WbWarehouse:
    """ref — ID или часть названия. Без ref: FBS_WB_WAREHOUSE, иначе
    единственный склад с заданиями, иначе WarehouseChoiceNeeded."""
    counts = ov.counts()
    ref = (ref or "").strip() or os.environ.get("FBS_WB_WAREHOUSE", "").strip()
    if ref:
        if ref.isdigit():
            wh_id = int(ref)
            if any(w.id == wh_id for w in ov.warehouses) or wh_id in counts:
                return ov.warehouse(wh_id)
        matches = [w for w in ov.warehouses if ref.casefold() in w.name.casefold()]
        if len(matches) == 1:
            return matches[0]
        options = [(w, counts.get(w.id, 0)) for w in (matches or ov.warehouses)]
        raise WarehouseChoiceNeeded(
            options,
            f"Склад «{ref}» " + ("неоднозначен" if matches else "не найден")
            + " — выбери из списка")

    busy = [ov.warehouse(wid) for wid, n in counts.items() if n]
    if len(busy) == 1:
        return busy[0]
    if not busy and len(ov.warehouses) == 1:
        return ov.warehouses[0]
    options = sorted(((ov.warehouse(wid), n) for wid, n in counts.items() if n),
                     key=lambda x: -x[1]) or [(w, 0) for w in ov.warehouses]
    raise WarehouseChoiceNeeded(
        options,
        "Новых заданий нет ни на одном складе" if not busy
        else "Задания есть на нескольких складах — выбери, где собирать")


def build_plan(ov: WbOverview, warehouse: WbWarehouse) -> WbPlan:
    plan = WbPlan(warehouse=warehouse)
    wid = warehouse.id
    plan.new_orders = [o for o in ov.new_orders if o.warehouse_id == wid]
    plan.in_assembly = [o for o in ov.in_assembly if o.warehouse_id == wid]
    plan.supplies = [s for s in ov.open_supplies if s.warehouse_id == wid]
    plan.other_warehouses = sorted(
        ((ov.warehouse(w), n) for w, n in ov.counts().items() if w != wid and n),
        key=lambda x: -x[1])
    plan.warnings = list(ov.warnings)

    groups = plan.groups()
    if len(groups) > 1:
        parts = [f"{CARGO_NAMES.get(k[1], k[1])}"
                 + (", трансгран." if k[2] else "") + (", B2B" if k[3] else "")
                 + ("" if k[4] else ", не для ПВЗ")
                 + f" — {len(v)}" for k, v in groups.items()]
        plan.warnings.append(
            f"📦 Новые задания разных типов — на этапе сборки будет "
            f"{len(groups)} поставки: {'; '.join(parts)}")
    no_pp = [o.order_id for o in plan.orders if not o.pp_allowed]
    if no_pp:
        plan.warnings.append(
            f"🏬 Нельзя сдать в ПВЗ (нужна поставка на СЦ/склад WB): "
            f"{', '.join(map(str, no_pp[:10]))}{'…' if len(no_pp) > 10 else ''}")
    meta = [o.order_id for o in plan.orders if o.needs_meta]
    if meta:
        plan.warnings.append(
            f"🏷 Нужна маркировка (без неё поставку не передать в доставку): "
            f"{', '.join(map(str, meta[:10]))}{'…' if len(meta) > 10 else ''}")
    if plan.cancelled:
        plan.warnings.append(
            f"❌ Отменены — НЕ собирать (если уже в поставке — убрать вручную): "
            f"{', '.join(str(o.order_id) for o in plan.cancelled[:10])}")
    own_prefix = f"{SUPPLY_NAME_PREFIX} {wid} "
    plan.empty_supplies = [s for s in ov.open_supplies
                           if not s.order_ids and s.name.startswith(own_prefix)]
    empty = [s.supply_id for s in ov.open_supplies
             if not s.order_ids and not s.name.startswith(own_prefix)]
    if empty:
        plan.warnings.append(f"ℹ️ Открытые пустые поставки: {', '.join(empty[:5])}")
    return plan


def load_plan(client: WBFbsClient, warehouse_ref: str | None) -> WbPlan:
    ov = load_overview(client)
    return build_plan(ov, resolve_warehouse(ov, warehouse_ref))


def explain_orders(client: WBFbsClient, ids: list[int]) -> list[str]:
    """Почему задание (не) в листе: статус, склад, поставка. Строки без HTML."""
    ov = load_overview(client)
    statuses = client.get_statuses(ids)
    new_by_id = {o.order_id: o for o in ov.new_orders}
    asm_by_id = {o.order_id: o for o in ov.in_assembly}
    lines = []
    for oid in ids:
        st, wb = statuses.get(oid, ("", ""))
        if not st and not wb:
            lines.append(f"{oid}: не найден (другой кабинет или токен?)")
            continue
        head = f"{oid}: supplierStatus={st or '—'} wbStatus={wb or '—'}"
        if st in CANCELLED_SUPPLIER or wb in CANCELLED_WB:
            why = "отменён — не собирать"
        elif oid in new_by_id:
            o = new_by_id[oid]
            why = f"новое, в листе склада «{ov.warehouse(o.warehouse_id).name}»"
        elif oid in asm_by_id:
            o = asm_by_id[oid]
            why = (f"на сборке в открытой поставке {o.supply_id}, склад "
                   f"«{ov.warehouse(o.warehouse_id).name}» — в листе")
        elif st == "confirm":
            why = "на сборке, но открытая поставка не найдена"
        elif st == "complete":
            why = "уже передано в доставку"
        else:
            why = f"не в работе (статус {st})"
        lines.append(f"{head} → {why}")
    return lines


# ----------------------------------------------------------------------
# Лист сборки (PDF) — своя копия вёрстки, без импорта из fbs_shipment
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
            pdfmetrics.registerFont(TTFont("WbSans", regular))
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont("WbSans-Bold", bold))
                return "WbSans", "WbSans-Bold"
            return "WbSans", "WbSans"
    raise RuntimeError(
        "Не найден TTF-шрифт с кириллицей. Установи `apt install "
        "fonts-dejavu-core` или задай FBS_FONT_PATH.")


def build_picking_pdf(plan: WbPlan, path: Path) -> Path:
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

    orders = plan.orders
    title = f"Лист сборки FBS WB — {esc(plan.warehouse.name)}"
    if len(plan.supplies) == 1 and not plan.new_orders:
        title += f" — {esc(plan.supplies[0].supply_id)}"
    story = [Paragraph(title, st_title)]
    story.append(Paragraph(
        f'Заданий: <font name="{font_bold}">{len(orders)}</font> · '
        f'Новых: {len(plan.to_take)} · На сборке: {len(orders) - len(plan.to_take)} · '
        f'Сформирован: {plan.generated_at.strftime("%d.%m.%Y %H:%M")}', st_txt))
    for s in plan.supplies:
        story.append(Paragraph(
            f"Поставка {esc(s.supply_id)} «{esc(s.name)}» · заданий {len(s.order_ids)}",
            st_txt))
    story.append(Spacer(1, 5 * mm))

    # --- 1. Подбор по артикулам ----------------------------------------
    story.append(Paragraph("1. Подбор по товарам", st_h))
    rows = [["✓", "№", "Артикул продавца", "Артикул WB", "Баркод", "Кол-во"]]
    for i, (article, nm_id, barcode, qty) in enumerate(plan.sku_summary(), 1):
        rows.append(["☐", str(i), Paragraph(esc(article), st_cell_b),
                     str(nm_id or ""), barcode, str(qty)])
    rows.append(["", "", Paragraph("Итого", st_cell_b), "", "", str(len(orders))])
    t = Table(rows, colWidths=[8 * mm, 8 * mm, 72 * mm, 32 * mm, 40 * mm, 20 * mm],
              repeatRows=1)
    t.setStyle(TableStyle(base_style + [
        ("ALIGN", (0, 0), (1, -1), "CENTER"),
        ("ALIGN", (5, 0), (5, -1), "CENTER"),
        ("FONTSIZE", (5, 1), (5, -1), 11),
        ("FONTNAME", (5, 1), (5, -1), font_bold),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f4f4f4")),
    ]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))

    # --- 2. По заданиям -------------------------------------------------
    story.append(Paragraph("2. Упаковка по заданиям (1 задание = 1 товар = 1 стикер)", st_h))
    rows = [["✓", "№", "Задание", "Артикул продавца", "Артикул WB", "Отметки"]]
    marks = []
    for i, o in enumerate(orders, 1):
        notes = [f for f in o.flags if not f.startswith("в поставке")]
        if o.comment:
            notes.append(f"комментарий: {o.comment}")
        order_cell = str(o.order_id) + (
            f"<br/><font size=7>стикер {esc(o.sticker)}</font>" if o.sticker else "")
        rows.append(["☐", str(i), Paragraph(order_cell, st_cell_b),
                     Paragraph(esc(o.article), st_cell), str(o.nm_id or ""),
                     Paragraph(esc("; ".join(notes)), st_cell)])
        if o.needs_meta or o.comment:
            marks.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fff1e0")))
    t = Table(rows, colWidths=[8 * mm, 8 * mm, 26 * mm, 52 * mm, 24 * mm, 62 * mm],
              repeatRows=1)
    t.setStyle(TableStyle(base_style + marks + [
        ("ALIGN", (0, 0), (1, -1), "CENTER"),
    ]))
    story.append(t)

    if plan.cancelled:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(
            "Отменены, НЕ собирать: "
            + ", ".join(str(o.order_id) for o in plan.cancelled), st_txt))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.drawRightString(
            A4[0] - 12 * mm, 8 * mm,
            f"FBS WB {plan.warehouse.name} · "
            f"{plan.generated_at.strftime('%d.%m.%Y %H:%M')} · стр. {doc.page}")
        canvas.restoreState()

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=14 * mm,
                            title=f"FBS WB picking {plan.warehouse.name}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


def picking_filename(plan: WbPlan) -> str:
    return (f"fbs_wb_picking_{plan.warehouse.id}_"
            f"{plan.generated_at.strftime('%Y-%m-%d_%H%M')}.pdf")


# ----------------------------------------------------------------------
# Этап 2: поставка, стикеры, грузоместа (ЗАПИСЬ в WB)
# ----------------------------------------------------------------------
class SupplyClosed(RuntimeError):
    pass


@dataclass
class TakeResult:
    created: list[str] = field(default_factory=list)
    added: dict[str, list[int]] = field(default_factory=dict)
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def added_total(self) -> int:
        return sum(len(v) for v in self.added.values())


@dataclass
class SupplyDocs:
    supply_id: str
    name: str
    units: int                 # активные задания в поставке = единицы товара
    picking_path: Path
    stickers_path: Path | None = None
    stickers_error: str = ""
    missing_stickers: list[int] = field(default_factory=list)
    pp_allowed: bool = True    # False — сдавать на СЦ/склад, грузоместа не нужны


@dataclass
class BoxResult:
    supply_id: str
    units: int
    trbx_ids: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    stickers_path: Path | None = None
    stickers_error: str = ""


def supply_name(warehouse: WbWarehouse) -> str:
    return f"{SUPPLY_NAME_PREFIX} {warehouse.id} {now_local():%d.%m %H:%M}"


def max_boxes(units: int) -> int:
    """WB: грузомест не больше половины заданий (10 → 5). Для 1 задания —
    одно место (в спецификации не сказано; проверено не было)."""
    return max(1, units // 2)


def check_open(client: WBFbsClient, supply_id: str) -> dict:
    if not re.match(SUPPLY_ID_RE, supply_id or ""):
        raise ValueError(f"Неверный номер поставки «{supply_id}» (нужен WB-GI-…)")
    raw = client.get_supply(supply_id)
    if raw.get("done"):
        raise SupplyClosed(f"Поставка {supply_id} уже закрыта (передана в доставку)")
    return raw


def _add_to_supply(client: WBFbsClient, supply_id: str, ids: list[int],
                   res: TakeResult) -> None:
    added = res.added.setdefault(supply_id, [])
    for i in range(0, len(ids), ADD_BATCH):
        batch = ids[i:i + ADD_BATCH]
        for attempt in range(2):
            try:
                client.add_orders(supply_id, batch)
                added.extend(batch)
                break
            except WBApiError as e:
                if attempt == 0 and "SupplyIsProcessing" in str(e):
                    time.sleep(3)
                    continue
                if e.status_code not in (400, 409) or len(batch) == 1:
                    res.failed.extend((oid, str(e)) for oid in batch)
                    break
                # Пачка отклонена целиком — ищем виноватых по одному (дорого:
                # каждый 4XX = 10 запросов лимита), остальное — с общей ошибкой.
                logging.warning("Batch add to %s failed (%s), retry one by one",
                                supply_id, e)
                for j, oid in enumerate(batch):
                    if j >= ADD_SINGLE_FALLBACK:
                        res.failed.append((oid, str(e)))
                        continue
                    try:
                        client.add_orders(supply_id, [oid])
                        added.append(oid)
                    except WBApiError as e1:
                        res.failed.append((oid, str(e1)))
                break


def take_into_supplies(client: WBFbsClient, plan: WbPlan, mode: str = "auto",
                       supply_id: str = "") -> TakeResult:
    """«Собрать поставку»: все новые задания склада → поставки (по группам).

    auto — открытая поставка с теми же признаками, иначе своя пустая
    («FBS WB <склад> …»), иначе новая; new — всегда новая; supply_id — всё
    в указанную (несовместимые WB отклонит, они попадут в failed)."""
    res = TakeResult()
    groups = plan.groups()
    if not groups:
        return res
    if supply_id:
        check_open(client, supply_id)
    own_empty = [s for s in plan.empty_supplies]
    for key, orders in groups.items():
        ids = [o.order_id for o in orders]
        if supply_id:
            target = supply_id
        else:
            target = ""
            if mode == "auto":
                match = next((s for s in plan.supplies if s.group_key == key), None)
                if match:
                    target = match.supply_id
                elif own_empty:
                    target = own_empty.pop(0).supply_id
            if not target:
                target = client.create_supply(supply_name(plan.warehouse))
                res.created.append(target)
                logging.info("Created supply %s for group %s", target, key)
        _add_to_supply(client, target, ids, res)
    return res


def _decode_sticker(file: str) -> bytes:
    """PNG приходит base64, ZPL — иногда текстом (^XA…)."""
    s = (file or "").strip()
    if s.startswith("^XA"):
        return s.encode()
    return base64.b64decode(s)


def images_pdf(images: list[bytes], fmt: str, path: Path) -> Path:
    """PNG-стикеры → PDF, одна страница = один стикер нужного размера."""
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    w, h = (int(x) for x in (fmt if fmt != "zpl" else DEFAULT_STICKER).split("x"))
    size = (w * mm, h * mm)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=size)
    for img in images:
        c.drawImage(ImageReader(io.BytesIO(img)), 0, 0, width=size[0],
                    height=size[1], preserveAspectRatio=True, anchor="c")
        c.showPage()
    c.save()
    return path


def _write_stickers(files: list[str], fmt: str, path_stem: Path) -> Path:
    blobs = [_decode_sticker(f) for f in files]
    if fmt == "zpl":
        path = path_stem.with_suffix(".zpl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\n".join(blobs))
        return path
    return images_pdf(blobs, fmt, path_stem.with_suffix(".pdf"))


def make_supply_docs(client: WBFbsClient, plan: WbPlan, supply_id: str,
                     out_dir: Path, fmt: str = DEFAULT_STICKER) -> SupplyDocs:
    """Стикеры заданий (в порядке листа) + лист сборки одной поставки."""
    sub = plan.for_supply(supply_id)
    orders = sub.orders
    name = sub.supplies[0].name if sub.supplies else ""
    docs = SupplyDocs(supply_id, name, len(orders), Path(),
                      pp_allowed=sub.supplies[0].pp_allowed if sub.supplies else True)
    if orders:
        try:
            got = client.get_stickers([o.order_id for o in orders], fmt)
            files = []
            for o in orders:
                s = got.get(o.order_id)
                if not s:
                    docs.missing_stickers.append(o.order_id)
                    continue
                o.sticker = f"{s.get('partA', '')} {s.get('partB', '')}".strip()
                files.append(s.get("file") or "")
            if files:
                docs.stickers_path = _write_stickers(
                    files, fmt, out_dir / f"stickers_{supply_id}")
        except Exception as e:
            logging.exception("Stickers for %s failed", supply_id)
            docs.stickers_error = str(e)
    # Лист — после стикеров, чтобы в нём были подписи стикеров.
    docs.picking_path = build_picking_pdf(sub, out_dir / f"picking_{supply_id}.pdf")
    return docs


def collect_supplies(client: WBFbsClient, warehouse_ref: str | None,
                     mode: str, supply_id: str, fmt: str,
                     out_dir: Path) -> tuple[TakeResult, WbPlan, list[SupplyDocs]]:
    """Полный шаг 1: взять задания в поставки, перечитать, сделать документы
    по каждой открытой поставке склада."""
    plan = load_plan(client, warehouse_ref)
    res = take_into_supplies(client, plan, mode, supply_id)
    fresh = build_plan(load_overview(client, {o.order_id: o for o in plan.orders}),
                       plan.warehouse)
    docs = [make_supply_docs(client, fresh, s.supply_id, out_dir, fmt)
            for s in fresh.supplies if fresh.for_supply(s.supply_id).orders]
    return res, fresh, docs


def set_boxes(client: WBFbsClient, supply_id: str, k: int | None,
              out_dir: Path, fmt: str = DEFAULT_STICKER) -> BoxResult:
    """Довести число грузомест поставки до k (None — не менять, только
    перепечатать) и получить их стикеры."""
    raw = check_open(client, supply_id)
    if raw.get("isPickupPointShipmentAllowed") is False:
        raise ValueError(f"Поставку {supply_id} нельзя сдать в ПВЗ — грузоместа "
                         f"нужны только для ПВЗ, сдавайте её на СЦ/склад WB")
    units = len(client.get_supply_order_ids(supply_id))
    res = BoxResult(supply_id, units)
    if k is not None and not 1 <= k <= max_boxes(units):
        raise ValueError(f"Грузомест можно от 1 до {max_boxes(units)} "
                         f"(в поставке {units} шт)")
    existing = client.list_trbx(supply_id)
    if k is not None and k > len(existing):
        try:
            res.created = client.add_trbx(supply_id, k - len(existing))
        except WBApiError as e:
            if 400 <= e.status_code < 500 and e.status_code not in (401, 403, 429):
                # Спецификация не говорит, нужны ли до грузомест параметры
                # отгрузки (ПВЗ) — если WB откажет, объясняем, а не падаем молча.
                raise ValueError(
                    f"WB не дал создать грузоместа ({e}). Возможно, сначала "
                    f"нужно указать ПВЗ/параметры отгрузки поставки в кабинете WB "
                    f"или число мест больше допустимого.") from e
            raise
    elif k is not None and k < len(existing):
        res.deleted = existing[k:]
        client.delete_trbx(supply_id, res.deleted)
    res.trbx_ids = client.list_trbx(supply_id)
    if not res.trbx_ids:
        return res
    try:
        stickers = client.get_trbx_stickers(supply_id, res.trbx_ids, fmt)
        res.stickers_path = _write_stickers(
            [s.get("file") or "" for s in stickers], fmt,
            out_dir / f"boxes_{supply_id}")
    except Exception as e:
        logging.exception("Box stickers for %s failed", supply_id)
        res.stickers_error = str(e)
    return res


# ----------------------------------------------------------------------
# Этап 3: сдача в ПВЗ — пункт отгрузки, shipping-method, deliver, QR (ЗАПИСЬ)
# ----------------------------------------------------------------------
@dataclass
class WbShippingPoint:
    id: int
    name: str
    address: str
    city: str = ""
    office_type: str = ""
    available: bool = True

    @property
    def label(self) -> str:
        return self.address or self.name


def parse_point(raw: dict) -> WbShippingPoint:
    return WbShippingPoint(
        id=int(raw["id"]), name=str(raw.get("name") or ""),
        address=str(raw.get("address") or ""), city=str(raw.get("city") or ""),
        office_type=str(raw.get("officeType") or ""),
        available=raw.get("available") is not False)


# Типы улиц и служебные слова адреса: «проспект/пр-кт/просп.», «г», «д.» и т.п.
# WB и человек пишут их по-разному — сравниваем без них.
ADDR_STOP = {
    "г", "гор", "город", "д", "дом", "ул", "улица", "пр", "кт", "т", "просп",
    "проспект", "пркт", "ш", "шоссе", "пер", "переулок", "б", "р", "бул",
    "бульвар", "наб", "набережная", "пл", "площадь", "проезд", "туп", "тупик",
    "мкр", "микрорайон", "россия", "рф", "обл", "область", "пвз", "wb",
    "wildberries", "вайлдберриз",
}


CITY_WORDS = {"г", "гор", "город", "россия", "рф", "обл", "область"}


def addr_tokens(text: str) -> list[str]:
    """«г Москва, Проспект Лихачёва 22к1» → ['москва', 'лихачева', '22к1']."""
    low = (text or "").lower().replace("ё", "е")
    return [t for t in re.findall(r"\d+[а-яa-z]*\d*|[а-яa-z]+", low)
            if t not in ADDR_STOP]


def _word_match(w: str, tokens: list[str]) -> bool:
    # Окончания у WB и у человека расходятся («Лихачёва»/«Лихачева»/«Лихачёв»).
    return any(t == w or (len(w) >= 6 and len(t) >= 6 and t[:6] == w[:6])
               for t in tokens)


def match_points(points: list[WbShippingPoint], query: str,
                 city: str = "") -> tuple[list[WbShippingPoint], list[WbShippingPoint]]:
    """(точные, похожие). Точный — все слова запроса и номер дома целиком
    («22» ≠ «122», «22» ≠ «22к1»). Похожий — та же улица, другой дом/корпус."""
    city_t = set(addr_tokens(city))
    q = [t for t in dict.fromkeys(addr_tokens(query)) if t not in city_t]
    words = [t for t in q if not t[0].isdigit()]
    houses = [t for t in q if t[0].isdigit()]
    if not words and not houses:
        return [], []
    exact, near = [], []
    for p in points:
        tokens = addr_tokens(f"{p.name} {p.address}")
        if not all(_word_match(w, tokens) for w in words):
            continue
        if all(h in tokens for h in houses):
            exact.append(p)
        elif words:
            near.append(p)
    # Сначала дома, начинающиеся с нужного номера (22к1 при поиске 22).
    near.sort(key=lambda p: not any(
        t.startswith(h) for h in houses for t in addr_tokens(p.address)))
    return exact, near


class PointChoiceNeeded(Exception):
    """Адрес ПВЗ совпал с несколькими пунктами (или ни с одним точно)."""

    def __init__(self, options: list[WbShippingPoint], message: str):
        super().__init__(message)
        self.options = options


def point_setting() -> tuple[int | None, str, str]:
    """FBS_WB_SHIPPING_POINT → (ID или None, город, адрес-запрос).

    «123456» — ID; иначе адрес: «Москва, проспект Лихачёва, 22». Город —
    FBS_WB_CITY, иначе первая часть адреса до запятой (если без цифр),
    иначе Москва."""
    raw = os.environ.get("FBS_WB_SHIPPING_POINT", "").strip()
    city = os.environ.get("FBS_WB_CITY", "").strip()
    if raw.isdigit():
        return int(raw), city or DEFAULT_CITY, ""
    return None, city or city_from_address(raw) or DEFAULT_CITY, raw


def city_from_address(raw: str) -> str:
    """«Москва, проспект …» → «Москва»; «проспект Лихачёва, 22» → ""."""
    city = ""
    first = raw.split(",")[0].strip()
    first_words = set(re.findall(r"[а-яa-z]+", first.lower().replace("ё", "е")))
    is_street = bool(first_words & (ADDR_STOP - CITY_WORDS))
    if "," in raw and first and not is_street and not re.search(r"\d", first):
        city = re.sub(r"^(г\.?|город)\s+", "", first, flags=re.I)
    return city


def today_iso() -> str:
    return now_local().date().isoformat()


def find_points(client: WBFbsClient, supply_id: str, query: str,
                city: str = "", shipping_dt: str = "") -> tuple[list, list, list]:
    """(все ПВЗ города, точные, похожие) — для --points и выбора в боте."""
    if not city:
        _, city, _ = point_setting()
    raw = client.get_shipping_points(supply_id, city, "pp",
                                     shipping_dt or today_iso())
    points = [parse_point(r) for r in raw]
    if not query:
        return points, [], []
    exact, near = match_points(points, query, city)
    return points, exact, near


def resolve_point(client: WBFbsClient, supply_id: str,
                  shipping_dt: str) -> WbShippingPoint:
    """ПВЗ из FBS_WB_SHIPPING_POINT. Один точный и доступный — он; иначе
    PointChoiceNeeded / ValueError. Другой ПВЗ молча не выбираем."""
    pid, city, query = point_setting()
    if pid is None and not query:
        raise ValueError(
            "Не задан ПВЗ: добавьте в .env FBS_WB_SHIPPING_POINT=«Москва, "
            "проспект Лихачёва, 22» (адрес) или ID пункта")
    points, exact, near = find_points(client, supply_id, query, city, shipping_dt)
    if pid is not None:
        exact = [p for p in points if p.id == pid]
        if not exact:
            raise ValueError(f"ПВЗ с ID {pid} не найден среди пунктов отгрузки "
                             f"поставки {supply_id} ({city}, {shipping_dt})")
    if len(exact) > 1:
        raise PointChoiceNeeded(exact, f"Адрес «{query}» совпал с {len(exact)} ПВЗ — выберите")
    if not exact:
        if near:
            raise PointChoiceNeeded(
                near, f"ПВЗ «{query}» точно не найден ({city}). Похожие:")
        raise ValueError(f"ПВЗ «{query}» не найден среди {len(points)} ПВЗ "
                         f"({city}), куда WB принимает эту поставку. Проверьте "
                         f"адрес: python fbs_wb.py --points {supply_id} \"улица дом\"")
    p = exact[0]
    if not p.available:
        raise ValueError(f"ПВЗ «{p.label}» перегружен на {shipping_dt} — WB не "
                         f"принимает туда поставки в эту дату. Выберите другой ПВЗ "
                         f"(FBS_WB_SHIPPING_POINT) или сдайте в другой день.")
    return p


@dataclass
class DeliverCheck:
    """Всё, что показываем перед «Да, сдать» (ничего не пишет)."""
    supply_id: str
    name: str
    units: int
    boxes: int
    shipping_dt: str
    cancelled: list[int] = field(default_factory=list)
    point: WbShippingPoint | None = None
    choice: PointChoiceNeeded | None = None


@dataclass
class DeliverResult:
    supply_id: str
    point_id: int
    shipping_dt: str
    qr_path: Path | None = None
    qr_error: str = ""


DELIVER_ERRORS = {
    "SupplyShippingRequired": "WB не видит параметров отгрузки (ПВЗ, дата, способ)",
    "FulfillmentSupplyBoxesRequired": "в поставке нет грузомест — добавьте их",
    "SupplyHasZeroOrders": "в поставке нет заданий",
    "MetaValidationFail": "маркировка заданий не прошла проверку",
}
SHIPPING_METHOD_ERRORS = {
    "IncorrectRequestBody": "WB отклонил параметры (ПВЗ не найден / неверная дата)",
    "FulfillmentRequired": "в этом пункте нет услуги «Фулфилмент в СЦ»",
    "NotFound": "поставка не найдена",
    "SupplyAlreadyScanned": "поставка уже отсканирована в пункте отгрузки",
    "InvalidShippingDt": "WB не принимает эту дату отгрузки",
}


def _deliver_precheck(client: WBFbsClient, supply_id: str) -> DeliverCheck:
    raw = check_open(client, supply_id)
    if raw.get("isPickupPointShipmentAllowed") is False:
        raise ValueError(f"Поставку {supply_id} нельзя сдать в ПВЗ — только на "
                         f"СЦ/склад WB (пока в кабинете WB)")
    ids = client.get_supply_order_ids(supply_id)
    if not ids:
        raise ValueError(f"В поставке {supply_id} нет заданий")
    boxes = len(client.list_trbx(supply_id))
    if not boxes:
        raise ValueError(f"У поставки {supply_id} нет грузомест — сначала "
                         f"выберите их число (/fbs_wb_boxes {supply_id} K)")
    blockers = {oid: b for oid, d in client.get_meta(ids).items()
                if (b := meta_blockers(d))}
    if blockers:
        lines = [f"{oid}: {', '.join(b)}" for oid, b in list(blockers.items())[:10]]
        raise ValueError(f"Нельзя сдать {supply_id}: не указана или не прошла "
                         f"проверку маркировка у {len(blockers)} зад.\n"
                         + "\n".join(lines))
    st = client.get_statuses(ids)
    cancelled = [oid for oid, (sup, wbs) in st.items()
                 if sup in CANCELLED_SUPPLIER or wbs in CANCELLED_WB]
    return DeliverCheck(supply_id, str(raw.get("name") or ""), len(ids), boxes,
                        today_iso(), cancelled)


def check_delivery(client: WBFbsClient, supply_id: str) -> DeliverCheck:
    """Шаг «подтверждение»: проверки + ПВЗ. Ничего не пишет в WB."""
    chk = _deliver_precheck(client, supply_id)
    try:
        chk.point = resolve_point(client, supply_id, chk.shipping_dt)
    except PointChoiceNeeded as e:
        chk.choice = e
    return chk


class ConfirmationStale(ValueError):
    pass


def deliver_supply(client: WBFbsClient, supply_id: str, point_id: int,
                   out_dir: Path, fmt: str = DEFAULT_STICKER,
                   expect: tuple[int, int, str] | None = None) -> DeliverResult:
    """ЗАПИСЬ, необратимо: параметры отгрузки (сами, сегодня, ПВЗ point_id) →
    передать в доставку → QR. point_id — тот, что человек видел в подтверждении.

    expect = (заданий, мест, дата) из подтверждения: если поставка с тех пор
    изменилась (кто-то нажал «Собрать», поменял места) или наступил другой
    день — не сдаём, просим подтвердить заново."""
    chk = _deliver_precheck(client, supply_id)
    if expect is not None and (chk.units, chk.boxes, chk.shipping_dt) != tuple(expect):
        units, boxes, dt = expect
        raise ConfirmationStale(
            f"Поставка {supply_id} изменилась после подтверждения (было {units} зад., "
            f"{boxes} мест, {dt}; сейчас {chk.units} зад., {chk.boxes} мест, "
            f"{chk.shipping_dt}). Не сдана — нажмите «🚛 Сдать в ПВЗ» ещё раз.")
    res = DeliverResult(supply_id, point_id, chk.shipping_dt)
    r = client.set_shipping_method(supply_id, point_id, chk.shipping_dt,
                                   os.environ.get("FBS_WB_SHIPPING_TYPE",
                                                  SHIPPING_TYPE).strip())
    if not r.get("success"):
        err = r.get("error") or {}
        detail = str(err.get("detail") or "нет ответа по поставке")
        raise ValueError(f"WB не принял параметры отгрузки {supply_id}: "
                         f"{SHIPPING_METHOD_ERRORS.get(detail, detail)} "
                         f"({err.get('code', '?')} {detail}). Поставка НЕ сдана.")
    try:
        client.deliver(supply_id)
    except WBApiError as e:
        if e.code not in DELIVER_ERRORS:
            raise
        msg = f"WB не передал {supply_id} в доставку: {DELIVER_ERRORS[e.code]}"
        for o in ((e.body.get("data") or {}).get("orders") or [])[:10]:
            msg += f"\n{o.get('id')}: " + ", ".join(
                f"{m.get('key')}: {m.get('decision')}"
                for m in o.get("metaDetails") or [])
        raise ValueError(msg + ". Поставка НЕ сдана — исправьте и повторите "
                               "«🚛 Сдать в ПВЗ».") from e
    logging.info("Supply %s delivered to point %s on %s", supply_id, point_id,
                 chk.shipping_dt)
    try:
        res.qr_path = fetch_qr(client, supply_id, out_dir, fmt)
    except Exception as e:
        logging.exception("QR for %s failed", supply_id)
        res.qr_error = str(e)
    return res


def fetch_qr(client: WBFbsClient, supply_id: str, out_dir: Path,
             fmt: str = DEFAULT_STICKER) -> Path:
    """QR-код поставки (580×400) → PDF 58×40 / .zpl. Только после deliver."""
    if not re.match(SUPPLY_ID_RE, supply_id or ""):
        raise ValueError(f"Неверный номер поставки «{supply_id}» (нужен WB-GI-…)")
    data = client.get_supply_barcode(supply_id, fmt)
    return _write_stickers([data.get("file") or ""], fmt, out_dir / f"qr_{supply_id}")


# ----------------------------------------------------------------------
# Google Drive (общий диск) — своя копия, своя папка GDRIVE_FBS_WB_FOLDER_ID
# ----------------------------------------------------------------------
class DriveUploader:
    """Минимальный клиент Drive v3 поверх requests.

    Сервисный аккаунт не имеет своей квоты — файлы можно класть только
    на ОБЩИЙ диск (Shared Drive), куда SA добавлен «Менеджером контента».
    """

    def __init__(self, creds_path: str):
        from oauth2client.service_account import ServiceAccountCredentials
        self.creds = ServiceAccountCredentials.from_json_keyfile_name(
            creds_path, ["https://www.googleapis.com/auth/drive"])
        self.session = requests.Session()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.creds.get_access_token().access_token}"}

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
            json={"name": name, "mimeType": DRIVE_FOLDER_MIME, "parents": [parent_id]},
            timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["id"]

    def upload(self, parent_id: str, path: Path) -> str:
        """Загружает файл; если с таким именем уже есть — заменяет содержимое."""
        mime = "application/pdf" if path.suffix == ".pdf" else "text/plain"
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
            boundary = "fbs-wb-upload-boundary"
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


def upload_to_drive(paths: list[Path]) -> tuple[str, str]:
    """→ (url папки дня, ошибка). Не бросает."""
    root = os.environ.get("GDRIVE_FBS_WB_FOLDER_ID", "").strip()
    if not root:
        return "", "GDRIVE_FBS_WB_FOLDER_ID не задан — на Drive не выкладывали"
    try:
        up = DriveUploader(os.environ.get("GOOGLE_CREDS",
                                          "/opt/openclaw/secrets/gcp-sa.json"))
        folder = up.ensure_folder(root, now_local().date().isoformat())
        for p in paths:
            up.upload(folder, p)
        return f"https://drive.google.com/drive/folders/{folder}", ""
    except Exception as e:
        logging.exception("Drive upload failed")
        msg = str(e)
        if "storageQuotaExceeded" in msg or "403" in msg:
            msg += (" — папка должна быть на ОБЩЕМ диске, SA добавлен "
                    "«Менеджером контента»")
        return "", msg


# ----------------------------------------------------------------------
# Текст для Telegram
# ----------------------------------------------------------------------
def format_summary(plan: WbPlan, max_skus: int = 25) -> str:
    import html as _html
    wh = _html.escape(plan.warehouse.name)
    orders = plan.orders
    lines = [f"🚚 <b>FBS WB · склад «{wh}»</b>"]
    if not orders:
        lines += ["", "✅ Собирать нечего — новых заданий нет"]
    else:
        lines.append(f"Заданий: <b>{len(orders)}</b> (1 задание = 1 шт) · "
                     f"новых: <b>{len(plan.to_take)}</b> · "
                     f"на сборке: {len(orders) - len(plan.to_take)}")
        for s in plan.supplies:
            lines.append(f"📋 Поставка {_html.escape(s.supply_id)} "
                         f"«{_html.escape(s.name)}» — {len(s.order_ids)} зад.")
        lines.append("")
        summary = plan.sku_summary()
        for article, _, _, qty in summary[:max_skus]:
            lines.append(f"• <code>{_html.escape(article)}</code> × <b>{qty}</b>")
        if len(summary) > max_skus:
            lines.append(f"…и ещё {len(summary) - max_skus} артикулов (в листе сборки)")
    if plan.warnings:
        lines.append("")
        lines += [_html.escape(w) for w in plan.warnings]
    if plan.other_warehouses:
        lines += ["", "🏬 На других складах:"]
        for w, n in plan.other_warehouses:
            lines.append(f"• {_html.escape(w.name)} — {n} → <code>/fbs_wb {w.id}</code>")
    return "\n".join(lines)


def format_choice(exc: WarehouseChoiceNeeded) -> str:
    import html as _html
    lines = [f"🏬 <b>FBS WB</b>: {_html.escape(str(exc))}"]
    for w, n in exc.options:
        lines.append(f"• {_html.escape(w.name)} (ID {w.id}) — {n} зад.")
    return "\n".join(lines)


def format_take(res: TakeResult, plan: WbPlan, docs: list[SupplyDocs]) -> str:
    """Итог «Собрать поставку» (без вопроса про грузоместа — он отдельно)."""
    import html as _html
    wh = _html.escape(plan.warehouse.name)
    lines = [f"📦 <b>FBS WB · склад «{wh}»</b>"]
    if res.created:
        lines.append(f"Создана поставка: {', '.join(res.created)}")
    lines.append(f"Добавлено заданий: <b>{res.added_total}</b>")
    if res.failed:
        lines.append(f"❌ Не добавлены: {len(res.failed)}")
        for oid, err in res.failed[:10]:
            lines.append(f"• {oid}: <code>{_html.escape(err)[:200]}</code>")
    for d in docs:
        if d.stickers_error:
            lines.append(f"⚠️ {d.supply_id}: стикеры не получены — "
                         f"<code>{_html.escape(d.stickers_error)[:300]}</code>")
        elif d.missing_stickers:
            lines.append(f"⚠️ {d.supply_id}: нет стикеров для "
                         f"{', '.join(map(str, d.missing_stickers[:10]))}")
    if not docs:
        lines.append("Открытых поставок с заданиями нет.")
    warn = [w for w in plan.warnings if w.startswith(("🏷", "❌", "🏬"))]
    if warn:
        lines += [""] + [_html.escape(w) for w in warn]
    return "\n".join(lines)


def format_box_prompt(d: SupplyDocs, existing: int) -> str:
    import html as _html
    name = f" «{_html.escape(d.name)}»" if d.name else ""
    lines = [f"✅ Поставка <b>{d.supply_id}</b>{name}: сейчас <b>{d.units} шт</b> "
             f"({d.units} зад.)"]
    if not d.pp_allowed:
        lines.append("🏬 Не для ПВЗ — сдавать на СЦ/склад WB, грузоместа не нужны.")
    elif existing:
        lines.append(f"Грузомест уже: <b>{existing}</b>. Перепечатать или изменить число?")
    else:
        lines.append(f"Сколько грузомест? (от 1 до {max_boxes(d.units)})")
    return "\n".join(lines)


def format_boxes(res: BoxResult) -> str:
    import html as _html
    lines = [f"📦 <b>{res.supply_id}</b>: {res.units} шт, грузомест "
             f"<b>{len(res.trbx_ids)}</b>"]
    if res.created:
        lines.append(f"Создано: {len(res.created)}")
    if res.deleted:
        lines.append(f"Удалено: {len(res.deleted)} — их старые стикеры НЕ клеить")
    if res.stickers_error:
        lines.append(f"⚠️ Стикеры мест не получены: "
                     f"<code>{_html.escape(res.stickers_error)[:300]}</code>")
    if res.trbx_ids:
        lines.append("Дальше — наклеить стикеры мест и «🚛 Сдать в ПВЗ».")
    return "\n".join(lines)


def format_point(p: WbShippingPoint) -> str:
    import html as _html
    mark = "" if p.available else " ⛔ перегружен на эту дату"
    name = f" «{_html.escape(p.name)}»" if p.name and p.name != p.address else ""
    return f"{_html.escape(p.address or p.name)}{name} (ID {p.id}){mark}"


def format_deliver_check(chk: DeliverCheck) -> str:
    """Подтверждение перед необратимой сдачей."""
    import html as _html
    name = f" «{_html.escape(chk.name)}»" if chk.name else ""
    lines = [f"🚛 <b>Сдать в ПВЗ поставку {chk.supply_id}</b>{name}?",
             f"Заданий: <b>{chk.units}</b> · грузомест: <b>{chk.boxes}</b> · "
             f"дата: {datetime.fromisoformat(chk.shipping_dt):%d.%m.%Y} · везём сами"]
    if chk.cancelled:
        lines.append(f"⚠️ Отменены покупателем/WB: {', '.join(map(str, chk.cancelled[:10]))} "
                     f"— их не везти")
    if chk.point:
        lines.append(f"📍 ПВЗ: {format_point(chk.point)}")
    elif chk.choice:
        lines.append(f"📍 {_html.escape(str(chk.choice))}")
        for p in chk.choice.options[:POINT_BUTTONS]:
            lines.append(f"• {format_point(p)}")
    lines += ["", "❗ После передачи в доставку поставку нельзя изменить: "
                  "задания перейдут в «в доставке», добавить новые будет нельзя."]
    return "\n".join(lines)


def format_delivered(res: DeliverResult) -> str:
    import html as _html
    lines = [f"✅ <b>{res.supply_id}</b> передана в доставку "
             f"({datetime.fromisoformat(res.shipping_dt):%d.%m}, ПВЗ ID {res.point_id})."]
    if res.qr_path:
        lines.append("QR поставки — в файле: покажите его в ПВЗ при сдаче.")
    else:
        lines.append(f"⚠️ QR не получен: <code>{_html.escape(res.qr_error)[:300]}</code>"
                     f" — повторите «🖨 QR поставки» или /fbs_wb_qr {res.supply_id}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def make_client() -> WBFbsClient:
    base = SANDBOX_API_BASE if os.environ.get("WB_SANDBOX") == "1" else API_BASE
    return WBFbsClient(os.environ["WB_API_TOKEN"], base)


def overview_from_fixture(path: Path) -> WbOverview:
    """JSON: {"warehouses": [...], "orders": [...новые...],
    "supplies": [{...поставка..., "orderIds": [...], "orders": [...детали...]}]}
    У любого задания можно указать supplierStatus/wbStatus и metaDetails
    (как в ответах /orders/status и /orders/meta)."""
    raw = json.loads(path.read_text("utf-8"))
    if isinstance(raw, list):
        raw = {"orders": raw}

    def with_extras(o_raw: dict, default_status: str) -> WbOrder:
        o = parse_order(o_raw)
        o.supplier_status = str(o_raw.get("supplierStatus") or default_status)
        o.wb_status = str(o_raw.get("wbStatus") or "waiting")
        if "metaDetails" in o_raw:
            o.required_meta = meta_blockers(o_raw["metaDetails"])
            o.meta_checked = True
        return o

    new_orders = [with_extras(o, "new") for o in raw.get("orders", [])]
    warehouses = [parse_warehouse(w) for w in raw.get("warehouses", [])]
    if not warehouses:
        warehouses = [WbWarehouse(id=w, name=f"склад {w}")
                      for w in sorted({o.warehouse_id for o in new_orders})]
    supplies, in_assembly = [], []
    for s_raw in raw.get("supplies", []):
        s = parse_supply(s_raw)
        s.order_ids = [int(x) for x in s_raw.get("orderIds", [])]
        for o_raw in s_raw.get("orders", []):
            o = with_extras(o_raw, "confirm")
            o.supply_id = s.supply_id
            in_assembly.append(o)
            s.warehouse_id = o.warehouse_id
        supplies.append(s)
    return WbOverview(warehouses, new_orders, supplies, in_assembly)


def main():
    p = argparse.ArgumentParser(
        description="FBS WB: сводка, лист сборки; --take/--boxes — запись в WB")
    p.add_argument("--warehouse", "-w", help="ID склада или часть названия")
    p.add_argument("--take", action="store_true",
                   help="ЗАПИСЬ: собрать новые задания склада в поставку + документы")
    p.add_argument("--mode", choices=SUPPLY_MODES,
                   default=os.environ.get("FBS_WB_SUPPLY_MODE", "auto"))
    p.add_argument("--supply", default="", help="С --take: всё в эту поставку WB-GI-…")
    p.add_argument("--boxes", metavar="WB-GI-…:K",
                   help="ЗАПИСЬ: довести число грузомест поставки до K (K=0 — "
                        "не менять, только стикеры)")
    p.add_argument("--points", nargs="+", metavar=("WB-GI-…", "АДРЕС"),
                   help="Найти ПВЗ для поставки: все ПВЗ города или по адресу "
                        "(только чтение; в поставке нужно ≥1 задание)")
    p.add_argument("--deliver", metavar="WB-GI-…",
                   help="Сдать поставку в ПВЗ: без --yes только проверка")
    p.add_argument("--point", type=int, help="С --deliver: ID ПВЗ вместо "
                                             "FBS_WB_SHIPPING_POINT")
    p.add_argument("--yes", action="store_true",
                   help="С --deliver: ЗАПИСЬ, передать в доставку (необратимо)")
    p.add_argument("--qr", metavar="WB-GI-…", help="QR сданной поставки")
    p.add_argument("--sticker", choices=STICKER_FORMATS,
                   default=os.environ.get("FBS_WB_STICKER", DEFAULT_STICKER))
    p.add_argument("--no-drive", action="store_true",
                   help="Не выкладывать документы на Google Drive")
    p.add_argument("--out", default="fbs_wb_out", help="Куда сохранить PDF")
    p.add_argument("--list-warehouses", action="store_true",
                   help="Показать склады и число заданий на них")
    p.add_argument("--explain", metavar="ID,ID",
                   help="Разобрать задания: статусы, склад, поставка")
    p.add_argument("--fixture", help="JSON вместо API (тест вёрстки)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        if args.explain:
            ids = [int(x) for x in args.explain.replace(" ", "").split(",") if x]
            print("\n".join(explain_orders(make_client(), ids)))
            sys.exit(0)

        out = Path(args.out)
        if args.points:
            sid = args.points[0].upper()
            query = " ".join(args.points[1:])
            _, city, _ = point_setting()
            city = os.environ.get("FBS_WB_CITY", "").strip() \
                or city_from_address(query) or city
            points, exact, near = find_points(make_client(), sid, query, city)
            shown = (exact or near) if query else points
            print(f"ПВЗ ({city}, {today_iso()}): всего {len(points)}"
                  + (f", точных совпадений {len(exact)}, похожих {len(near)}"
                     if query else ""))
            for pt in shown[:50]:
                print(f"{pt.id}\t{'да ' if pt.available else 'НЕТ'}\t"
                      f"{pt.address}\t{pt.name}")
            sys.exit(0 if shown else 2)

        if args.qr:
            path = fetch_qr(make_client(), args.qr.strip().upper(), out, args.sticker)
            print(f"QR: {path}")
            if not args.no_drive:
                url, err = upload_to_drive([path])
                logging.info("Drive: %s", url or err)
            sys.exit(0)

        if args.deliver:
            client, sid = make_client(), args.deliver.strip().upper()
            if args.point:
                chk = _deliver_precheck(client, sid)
                point_id = args.point
            else:
                chk = check_delivery(client, sid)
                point_id = chk.point.id if chk.point else None
            print(re.sub(r"</?[a-z]+>", "", format_deliver_check(chk)))
            if point_id is None:
                print("Задайте ПВЗ точнее (FBS_WB_SHIPPING_POINT) или --point ID")
                sys.exit(2)
            if not args.yes:
                print(f"Проверка пройдена. Сдать: --deliver {sid} --point {point_id} --yes")
                sys.exit(0)
            res = deliver_supply(client, sid, point_id, out, args.sticker)
            print(re.sub(r"</?[a-z]+>", "", format_delivered(res)))
            if res.qr_path and not args.no_drive:
                url, err = upload_to_drive([res.qr_path])
                logging.info("Drive: %s", url or err)
            sys.exit(1 if res.qr_error else 0)

        if args.boxes:
            sid, _, k = args.boxes.partition(":")
            res = set_boxes(make_client(), sid.strip().upper(),
                            int(k) or None if k else None, out, args.sticker)
            print(format_boxes(res))
            if res.stickers_path:
                logging.info("Box stickers: %s", res.stickers_path)
                if not args.no_drive:
                    url, err = upload_to_drive([res.stickers_path])
                    logging.info("Drive: %s", url or err)
            sys.exit(1 if res.stickers_error else 0)

        if args.take:
            res, plan, docs = collect_supplies(
                make_client(), args.warehouse, args.mode, args.supply.upper(),
                args.sticker, out)
            print(format_take(res, plan, docs))
            paths = []
            for d in docs:
                paths += [p for p in (d.picking_path, d.stickers_path) if p]
                print(f"{d.supply_id}: {d.units} шт → грузоместа: "
                      f"--boxes {d.supply_id}:K (K ≤ {max_boxes(d.units)})")
            logging.info("Files: %s", ", ".join(map(str, paths)))
            if paths and not args.no_drive:
                url, err = upload_to_drive(paths)
                logging.info("Drive: %s", url or err)
            sys.exit(1 if res.failed or any(d.stickers_error for d in docs) else 0)

        ov = (overview_from_fixture(Path(args.fixture)) if args.fixture
              else load_overview(make_client()))

        if args.list_warehouses:
            counts = ov.counts()
            for w in ov.warehouses:
                print(f"{w.id}\t{w.name}\t{counts.get(w.id, 0)} зад.")
            sys.exit(0)

        try:
            wh = resolve_warehouse(ov, args.warehouse)
        except WarehouseChoiceNeeded as e:
            print(f"{e}. Укажи --warehouse:")
            for w, n in e.options:
                print(f"  {w.id}\t{w.name}\t{n} зад.")
            sys.exit(2)

        plan = build_plan(ov, wh)
        print(format_summary(plan))
        if plan.orders:
            pdf = build_picking_pdf(plan, out / picking_filename(plan))
            logging.info("Picking list: %s", pdf)
    except KeyError as e:
        logging.error("Missing env var: %s", e)
        sys.exit(1)
    except (ValueError, SupplyClosed, WarehouseChoiceNeeded, PointChoiceNeeded) as e:
        logging.error("%s", e)
        sys.exit(2)
    except Exception:
        logging.exception("FBS WB failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
