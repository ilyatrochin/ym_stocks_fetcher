"""
fbs_wb.py — модуль 7.1: FBS-сборка Wildberries (этап 1 — только чтение).

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

Записи в WB на этапе 1 нет. Поставки, стикеры, передача в доставку — этапы 2–3.

Запуск:
  python fbs_wb.py --list-warehouses
  python fbs_wb.py --warehouse офис --verbose        # сводка + PDF в ./fbs_wb_out
  python fbs_wb.py --explain 123456,654321           # почему задание не в листе
  python fbs_wb.py --fixture wb_orders.json --warehouse 1   # без API (тест вёрстки)
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

HTTP_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0
MIN_INTERVAL = 0.25           # лимит WB: 300 запросов/мин, интервал 200 мс
PAGE_LIMIT = 1000             # максимум limit для /orders и /supplies
STATUS_BATCH = 1000           # максимум заданий в /orders/status
META_BATCH = 100              # максимум заданий в /orders/meta
ORDERS_LOOKBACK_DAYS = 30     # глубина поиска деталей заданий из открытых поставок
ORDERS_MAX_LOOKBACK_DAYS = 89 # /orders с 06.08.2026 отдаёт только 3 месяца

# Статусы проверки маркировки, с которыми поставку МОЖНО передать в доставку.
# Всё остальное (required, pending, *Invalid*, *NotFound* …) — блокирует.
META_OK = {"filled", "optional", "deadlineExceeded"}
META_OK_SUFFIXES = ("MaySell", "SoldB2B", "Introduced")

DEFAULT_TZ = "Europe/Moscow"
SUPPLY_MODES = ("auto", "new")
STICKER_FORMATS = ("58x40", "40x30", "zpl")

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

    def iter_orders(self, date_from: datetime) -> Iterator[dict]:
        nxt = 0
        while True:
            data = self._request("GET", ORDERS_ENDPOINT, params={
                "limit": PAGE_LIMIT, "next": nxt,
                "dateFrom": int(date_from.timestamp())})
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
def load_overview(client: WBFbsClient) -> WbOverview:
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
        for raw in client.iter_orders(date_from):
            oid = int(raw.get("id") or 0)
            if oid in wanted:
                found[oid] = parse_order(raw)
        for oid, o in found.items():
            o.supply_id = wanted[oid].supply_id
            in_assembly.append(o)
        missing = set(wanted) - set(found)
        if missing:
            warnings.append(
                f"ℹ️ {len(missing)} задани(й) из открытых поставок не найдены "
                f"в /orders (старше {ORDERS_MAX_LOOKBACK_DAYS} дн.?): "
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
    empty = [s.supply_id for s in ov.open_supplies if not s.order_ids]
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
    story = [Paragraph(f"Лист сборки FBS WB — {esc(plan.warehouse.name)}", st_title)]
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
        rows.append(["☐", str(i), Paragraph(str(o.order_id), st_cell_b),
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
        description="FBS WB: сводка и лист сборки по складу (этап 1, только чтение)")
    p.add_argument("--warehouse", "-w", help="ID склада или часть названия")
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
            pdf = build_picking_pdf(plan, Path(args.out) / picking_filename(plan))
            logging.info("Picking list: %s", pdf)
    except KeyError as e:
        logging.error("Missing env var: %s", e)
        sys.exit(1)
    except Exception:
        logging.exception("FBS WB failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
