"""
tg_supplies_bot.py — Telegram-бот для управления поставками YM.

Команды:
  /supplies — сводка поставок (в пути + запланированы + статус упаковки)

Inline-кнопки (только для запланированных заявок):
  [📦 #12345 — упаковать]   → помечает заявку как упакованную
  [↩️ #12345 — снять отметку] → снимает отметку упаковки

Данные:
  Читает лист «supplies_requests» (пишется fetch_ym_supplies.py каждое утро).
  Статус упаковки хранит в листе «supply_packing» (Google Sheets).

Безопасность:
  TG_ALLOWED_USERS — через запятую user_id, кто может использовать бота.
  Если переменная пуста — ограничений нет.

Запуск:
  python tg_supplies_bot.py

Systemd-сервис:
  [Unit]
  Description=YM Supplies Telegram Bot
  After=network.target

  [Service]
  User=openclaw
  WorkingDirectory=/opt/openclaw/skills/ym_stocks_fetcher
  EnvironmentFile=/opt/openclaw/secrets/.env
  ExecStart=/usr/bin/python3 tg_supplies_bot.py
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

from sheets_helpers import safe_append_rows, with_retry

# ──────────────────────────────────────────────────────────────────────────────
# Константы
# ──────────────────────────────────────────────────────────────────────────────

SUPPLY_REQUESTS_SHEET = "supplies_requests"
PACKING_SHEET = "supply_packing"
PACKING_HEADERS = ["request_id", "packed", "packed_at", "tg_user_id", "tg_username"]

# Заявки, к которым относится этап упаковки в офисе — ещё не отгружены.
# CREATED — создана, ждёт одобрения. ACCEPTED_BY_WAREHOUSE_SYSTEM —
# одобрена складской системой МП, ждёт нашей упаковки/отгрузки.
# Всё остальное активное (SHIPPED_TO_*, WAREHOUSE_HANDLING и т.п.) — уже
# уехало или обрабатывается складом МП → показываем без кнопок в «В пути».
PLANNED_STATUSES = {"CREATED", "ACCEPTED_BY_WAREHOUSE_SYSTEM"}


# ──────────────────────────────────────────────────────────────────────────────
# Google Sheets client
# ──────────────────────────────────────────────────────────────────────────────

class SheetsClient:
    def __init__(self, creds_path: str, spreadsheet_id: str):
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
        self.spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)

    def _get_ws(self, name: str):
        try:
            return self.spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            return None

    def read_supply_requests(self) -> dict[int, dict]:
        """Читает supplies_requests. Возвращает:
        {request_id: {request_id, marketplace_request_id, status, marketplace,
                      target_warehouse, requested_date,
                      items: [{sku, plan_qty, fact_qty, qty_in_transit}]}}

        Колонки листа (из fetch_ym_supplies.py):
          0  fetch_timestamp
          1  request_id
          2  marketplace_request_id
          3  status
          4  marketplace
          5  target_warehouse
          6  requested_date
          7  sku
          8  plan_qty
          9  fact_qty
         10  qty_in_transit
        """
        ws = self._get_ws(SUPPLY_REQUESTS_SHEET)
        if not ws:
            return {}

        all_rows = ws.get_all_values()
        header_idx = None
        for i, row in enumerate(all_rows):
            if row and row[0].strip().lower() == "fetch_timestamp":
                header_idx = i
                break
        if header_idx is None:
            return {}

        result: dict[int, dict] = {}
        for row in all_rows[header_idx + 1:]:
            if len(row) < 2 or not row[1].strip():
                continue
            try:
                req_id = int(row[1])
            except ValueError:
                continue

            if req_id not in result:
                result[req_id] = {
                    "request_id": req_id,
                    "marketplace_request_id": row[2].strip() if len(row) > 2 else "",
                    "status": row[3].strip() if len(row) > 3 else "",
                    "marketplace": row[4].strip() if len(row) > 4 else "YM",
                    "target_warehouse": row[5].strip() if len(row) > 5 else "",
                    "requested_date": row[6].strip() if len(row) > 6 else "",
                    "items": [],
                }

            sku = row[7].strip() if len(row) > 7 else ""
            if sku:
                try:
                    plan = int(row[8]) if len(row) > 8 and row[8] else 0
                    fact = int(row[9]) if len(row) > 9 and row[9] else 0
                    in_tr = int(row[10]) if len(row) > 10 and row[10] else 0
                except ValueError:
                    plan = fact = in_tr = 0
                result[req_id]["items"].append({
                    "sku": sku,
                    "plan_qty": plan,
                    "fact_qty": fact,
                    "qty_in_transit": in_tr,
                })

        return result

    def read_packing_status(self) -> dict[int, dict]:
        """Возвращает {request_id: {packed, packed_at, tg_user_id, tg_username}}."""
        ws = self._get_ws(PACKING_SHEET)
        if not ws:
            return {}

        all_rows = ws.get_all_values()
        result: dict[int, dict] = {}
        for row in all_rows[1:]:  # пропускаем заголовок
            if not row or not row[0].strip():
                continue
            try:
                req_id = int(row[0])
            except ValueError:
                continue
            result[req_id] = {
                "packed": (row[1].strip().lower() in ("да", "true", "1", "yes")
                           if len(row) > 1 else False),
                "packed_at": row[2].strip() if len(row) > 2 else "",
                "tg_user_id": row[3].strip() if len(row) > 3 else "",
                "tg_username": row[4].strip() if len(row) > 4 else "",
            }
        return result

    def set_packing_status(self, request_id: int, packed: bool,
                           tg_user_id: str, tg_username: str):
        """Обновляет или добавляет статус упаковки для заявки."""
        ws = self._get_ws(PACKING_SHEET)
        if not ws:
            ws = self.spreadsheet.add_worksheet(
                title=PACKING_SHEET, rows=500, cols=5)
            safe_append_rows(ws, [PACKING_HEADERS])

        packed_at = datetime.now().strftime("%Y-%m-%d %H:%M") if packed else ""
        new_row = [str(request_id), "Да" if packed else "Нет",
                   packed_at, tg_user_id, tg_username]

        all_rows = ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row and row[0].strip() == str(request_id):
                row_num = i + 1  # 1-indexed
                with_retry(ws.update, f"A{row_num}:E{row_num}", [new_row])
                logging.info("Updated packing status for request %d → packed=%s",
                             request_id, packed)
                return

        safe_append_rows(ws, [new_row])
        logging.info("Added packing status for request %d → packed=%s",
                     request_id, packed)


# ──────────────────────────────────────────────────────────────────────────────
# Формирование отчёта
# ──────────────────────────────────────────────────────────────────────────────

def _pluralize(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def _items_summary(items: list[dict], qty_field: str) -> str:
    parts = [f"{it['sku']}: {it[qty_field]} шт"
             for it in items if it.get(qty_field, 0) > 0]
    return ", ".join(parts) if parts else "—"


def build_report(
    requests_data: dict[int, dict],
    packing: dict[int, dict],
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Строит текст сводки и inline-клавиатуру.

    В пути (status ≠ CREATED) — только информационные строки, кнопок нет.
    Запланированы (status == CREATED) — кнопки «упаковать» / «снять отметку».
    """
    today = datetime.now().strftime("%Y-%m-%d")

    in_transit: list[tuple[int, dict]] = []
    planned: list[tuple[int, dict]] = []
    for req_id, req in requests_data.items():
        (planned if req["status"] in PLANNED_STATUSES else in_transit).append(
            (req_id, req))

    key_eta = lambda x: x[1]["requested_date"] or "9999-99-99"
    in_transit.sort(key=key_eta)
    planned.sort(key=key_eta)

    DIVIDER = "─" * 28

    lines = [f"📦 <b>Поставки YM на {today}</b>"]
    keyboard: list[list[InlineKeyboardButton]] = []

    # ── В пути ────────────────────────────────────────────────────────────
    if in_transit:
        n = len(in_transit)
        word = _pluralize(n, "заявка", "заявки", "заявок")
        lines.append("")
        lines.append(DIVIDER)
        lines.append(f"🚚 <b>В пути — {n} {word}</b>")
        lines.append(DIVIDER)
        for req_id, req in in_transit:
            total = sum(i["qty_in_transit"] for i in req["items"])
            items_str = _items_summary(req["items"], "qty_in_transit")
            eta = req["requested_date"] or "дата не указана"
            mp_id = req["marketplace_request_id"] or str(req_id)
            status_label = req["status"].replace("_", " ").title()
            lines.append(
                f"• <b>#{mp_id}</b> [{status_label}]\n"
                f"  📍 {req['target_warehouse']} (≈{eta})\n"
                f"  {items_str} — итого <b>{total} шт</b>"
            )

    # ── Запланированы ─────────────────────────────────────────────────────
    if planned:
        n = len(planned)
        word = _pluralize(n, "заявка", "заявки", "заявок")
        lines.append("")
        lines.append(DIVIDER)
        lines.append(f"📋 <b>Запланированы — {n} {word}</b>")
        lines.append(DIVIDER)
        for req_id, req in planned:
            total = sum(i["plan_qty"] for i in req["items"])
            items_str = _items_summary(req["items"], "plan_qty")
            eta = req["requested_date"] or "дата не указана"
            mp_id = req["marketplace_request_id"] or str(req_id)
            pack_info = packing.get(req_id, {})
            is_packed = pack_info.get("packed", False)

            if is_packed:
                packed_at = pack_info.get("packed_at", "")
                lines.append(
                    f"✅ <b>#{mp_id}</b> → {req['target_warehouse']} (≈{eta})\n"
                    f"  {items_str} — итого <b>{total} шт</b>\n"
                    f"  <i>упаковано {packed_at}</i>"
                )
                keyboard.append([InlineKeyboardButton(
                    f"↩️ #{mp_id} — снять отметку",
                    callback_data=f"unpack:{req_id}",
                )])
            else:
                lines.append(
                    f"🔲 <b>#{mp_id}</b> → {req['target_warehouse']} (≈{eta})\n"
                    f"  {items_str} — итого <b>{total} шт</b>\n"
                    f"  <i>не упаковано</i>"
                )
                keyboard.append([InlineKeyboardButton(
                    f"📦 #{mp_id} — упаковать",
                    callback_data=f"pack:{req_id}",
                )])

    if not in_transit and not planned:
        lines.append("")
        lines.append("✅ Нет активных поставок")

    text = "\n".join(lines).strip()
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return text, markup


# ──────────────────────────────────────────────────────────────────────────────
# Telegram handlers
# ──────────────────────────────────────────────────────────────────────────────

def _make_sheets_client() -> SheetsClient:
    return SheetsClient(
        creds_path=os.environ.get("GOOGLE_CREDS",
                                  "/opt/openclaw/secrets/gcp-sa.json"),
        spreadsheet_id=os.environ["GSHEET_ID"],
    )


def _is_allowed(user_id: int) -> bool:
    raw = os.environ.get("TG_ALLOWED_USERS", "").strip()
    if not raw:
        return True
    try:
        allowed = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        return True
    return user_id in allowed


async def cmd_supplies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    msg = await update.message.reply_text("⏳ Загружаю данные поставок...")
    try:
        sheets = _make_sheets_client()
        requests_data = sheets.read_supply_requests()
        packing = sheets.read_packing_status()

        if not requests_data:
            await msg.edit_text(
                "📦 Данных нет в <code>supplies_requests</code>.\n"
                "Запусти <code>fetch_ym_supplies.py</code> для обновления.",
                parse_mode="HTML",
            )
            return

        text, markup = build_report(requests_data, packing)
        await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logging.exception("cmd_supplies failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def callback_pack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _is_allowed(query.from_user.id):
        return

    try:
        action, req_id_str = query.data.split(":", 1)
        req_id = int(req_id_str)
        packed = (action == "pack")

        sheets = _make_sheets_client()
        sheets.set_packing_status(
            req_id, packed,
            tg_user_id=str(query.from_user.id),
            tg_username=query.from_user.username or query.from_user.first_name or "",
        )

        requests_data = sheets.read_supply_requests()
        packing = sheets.read_packing_status()
        text, markup = build_report(requests_data, packing)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logging.exception("callback_pack failed")
        await query.edit_message_text(f"❌ Ошибка при обновлении: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not bot_token:
        logging.error("TG_BOT_TOKEN не задан")
        sys.exit(1)
    if "GSHEET_ID" not in os.environ:
        logging.error("GSHEET_ID не задан")
        sys.exit(1)

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("supplies", cmd_supplies))
    app.add_handler(CallbackQueryHandler(callback_pack))

    logging.info("Бот запущен, polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
