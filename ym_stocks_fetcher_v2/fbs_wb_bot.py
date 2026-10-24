"""
fbs_wb_bot.py — Telegram-часть модуля 7.1 (FBS Wildberries), этап 1.

Независим от Яндекс-части (fbs_bot_handlers.py): свои команды, свой
префикс callback, ни одного импорта из YM-модулей. Подключается в
tg_supplies_bot.py под флагом WB_API_TOKEN:
    register_fbs_wb_handlers(app, is_allowed=_is_allowed)

Команды:
  /fbs_wb [склад] [auto|new] [стикер=58x40|40x30|zpl] [supply=WB-GI-…]
        — сводка по складу: всё новое + уже на сборке. Даты у WB нет.
          Склад — ID или часть названия; без него: FBS_WB_WAREHOUSE или
          единственный склад с заданиями, иначе кнопки выбора склада.
          Режим/стикер/supply разбираются уже сейчас, применяются с этапа 2.
  /fbs_wb_check <id> [id…] — почему задание (не) в листе.
    [🏬 Склад — N]   → сводка по этому складу
    [📋 Лист сборки] → PDF в чат (только чтение)
    [🔄 Обновить]

Записи в WB на этапе 1 нет. Блокирующая работа — в asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

import fbs_wb as wb

CB_PREFIX = "fbswb:"
TG_MAX_LEN = 4000
DATE_LIKE = re.compile(r"^(\d{1,2}\.\d{1,2}(\.\d{2,4})?|\d{4}-\d{2}-\d{2}|"
                       r"сегодня|завтра|послезавтра|вчера|today|tomorrow)$")


def _clip(text: str) -> str:
    return text if len(text) <= TG_MAX_LEN else text[:TG_MAX_LEN] + "\n…(обрезано)"


@dataclass
class WbArgs:
    warehouse: str = ""
    mode: str = ""             # auto | new — применяется с этапа 2
    sticker: str = ""          # 58x40 | 40x30 | zpl — с этапа 2
    supply: str = ""           # WB-GI-… — с этапа 2


def parse_args(args: list[str]) -> WbArgs:
    """key=value, затем режим, остальное — склад. Дата — ошибка."""
    out = WbArgs()
    rest = []
    for a in args:
        low = a.strip().lower()
        if not low:
            continue
        if DATE_LIKE.match(low):
            raise ValueError("У WB даты нет — собирается всё новое на складе. "
                             "Пример: /fbs_wb офис")
        key, eq, val = low.partition("=")
        if eq:
            if key in ("стикер", "sticker"):
                if val not in wb.STICKER_FORMATS:
                    raise ValueError(f"Стикер: {', '.join(wb.STICKER_FORMATS)}")
                out.sticker = val
            elif key in ("supply", "поставка"):
                out.supply = a.strip().partition("=")[2].upper()
            elif key in ("склад", "warehouse"):
                out.warehouse = a.strip().partition("=")[2]
            else:
                raise ValueError(f"Неизвестный параметр «{key}»")
        elif low in wb.SUPPLY_MODES:
            out.mode = low
        else:
            rest.append(a.strip())
    if rest:
        out.warehouse = " ".join(rest)
    return out


def _cb(action: str, wh_id: int) -> str:
    return f"{CB_PREFIX}{action}:{wh_id}"


def _choice_keyboard(exc: wb.WarehouseChoiceNeeded) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏬 {w.name[:30]} — {n}",
                                  callback_data=_cb("wh", w.id))]
            for w, n in exc.options[:20]]
    return InlineKeyboardMarkup(rows)


def _keyboard(plan: wb.WbPlan) -> InlineKeyboardMarkup:
    wid = plan.warehouse.id
    rows = []
    if plan.orders:
        rows.append([InlineKeyboardButton("📋 Лист сборки (PDF)",
                                          callback_data=_cb("pdf", wid))])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=_cb("wh", wid))])
    return InlineKeyboardMarkup(rows)


def _err_text(e: Exception) -> str:
    if isinstance(e, KeyError):
        return f"❌ Не задана переменная окружения {e}"
    hint = ""
    if isinstance(e, wb.WBApiError) and e.status_code in (401, 403):
        hint = ("\nПроверь токен WB_API_TOKEN: категория «Маркетплейс», "
                "не истёк ли срок.")
    return f"❌ Ошибка WB: <code>{html.escape(str(e))[:800]}</code>{hint}"


async def _load(ref: str | None) -> tuple[wb.WbPlan | None, wb.WarehouseChoiceNeeded | None]:
    client = wb.make_client()
    try:
        return await asyncio.to_thread(wb.load_plan, client, ref), None
    except wb.WarehouseChoiceNeeded as e:
        return None, e


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────

def register_fbs_wb_handlers(app: Application,
                             is_allowed: Callable[[int], bool]) -> None:

    async def _show(edit, ref: str | None):
        plan, choice = await _load(ref)
        if choice is not None:
            await edit(_clip(wb.format_choice(choice)), parse_mode="HTML",
                       reply_markup=_choice_keyboard(choice))
        else:
            await edit(_clip(wb.format_summary(plan)), parse_mode="HTML",
                       reply_markup=_keyboard(plan))

    async def cmd_fbs_wb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        try:
            args = parse_args(context.args or [])
        except ValueError as e:
            await update.message.reply_text(f"❓ {e}")
            return
        msg = await update.message.reply_text("⏳ Загружаю задания WB…")
        try:
            await _show(msg.edit_text, args.warehouse or None)
        except Exception as e:
            logging.exception("cmd_fbs_wb failed")
            await msg.edit_text(_err_text(e), parse_mode="HTML")

    async def cmd_fbs_wb_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        ids = [int(a.strip(",")) for a in (context.args or [])
               if a.strip(",").isdigit()]
        if not ids:
            await update.message.reply_text(
                "Использование: /fbs_wb_check 123456789 987654321")
            return
        try:
            lines = await asyncio.to_thread(wb.explain_orders, wb.make_client(), ids)
        except Exception as e:
            logging.exception("cmd_fbs_wb_check failed")
            await update.message.reply_text(_err_text(e), parse_mode="HTML")
            return
        await update.message.reply_text(
            "🔎 <b>Проверка заданий WB</b>\n" +
            "\n".join(f"• {html.escape(s)}" for s in lines), parse_mode="HTML")

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not is_allowed(query.from_user.id):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return
        # fbswb:<action>:<warehouseId>
        action, _, wh = query.data[len(CB_PREFIX):].partition(":")
        await query.answer()
        try:
            if action == "wh":
                await query.edit_message_text("⏳ Загружаю задания WB…")
                await _show(query.edit_message_text, wh)
            elif action == "pdf":
                await _send_pdf(query, context, wh)
        except Exception as e:
            logging.exception("fbs_wb callback %s failed", action)
            await query.message.reply_text(_err_text(e), parse_mode="HTML")

    async def _send_pdf(query, context, wh: str):
        chat_id = query.message.chat_id
        plan, choice = await _load(wh)
        if choice is not None or not plan.orders:
            await context.bot.send_message(chat_id, "✅ Собирать нечего — листа нет.")
            return
        with tempfile.TemporaryDirectory(prefix="fbs_wb_") as tmp:
            path = await asyncio.to_thread(
                wb.build_picking_pdf, plan, Path(tmp) / wb.picking_filename(plan))
            with path.open("rb") as f:
                await context.bot.send_document(
                    chat_id, f, filename=path.name,
                    caption=f"📋 Лист сборки FBS WB «{plan.warehouse.name}» — "
                            f"{len(plan.orders)} зад.")

    app.add_handler(CommandHandler("fbs_wb", cmd_fbs_wb))
    app.add_handler(CommandHandler("fbs_wb_check", cmd_fbs_wb_check))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^fbswb:"))
