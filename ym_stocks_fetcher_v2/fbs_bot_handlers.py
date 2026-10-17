"""
fbs_bot_handlers.py — Telegram-часть модуля 7 (FBS-отгрузка на сегодня).

Отдельно от FBY-команд tg_supplies_bot.py; подключается одной строкой:
    register_fbs_handlers(app, is_allowed=_is_allowed)

Команда:
  /fbs — что сегодня отгружать по FBS (сводка по SKU) + кнопки:
    [📄 Документы]     → лист сборки + ярлыки PDF в чат и на Google Drive
    [✅ Собрано (N)]   → подтверждение → STARTED → READY_TO_SHIP в Маркете
    [🔄 Обновить]      → перечитать заказы

Вся работа с API/PDF блокирующая (requests, reportlab) — уводим её в
asyncio.to_thread, чтобы не вешать event loop бота.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

import fbs_shipment as fbs

CB_PREFIX = "fbs:"
READY_IDS_KEY = "fbs_ready_ids"
TG_MAX_LEN = 4000


def _clip(text: str) -> str:
    return text if len(text) <= TG_MAX_LEN else text[:TG_MAX_LEN] + "\n…(обрезано)"


def _keyboard(plan: fbs.FbsPlan) -> InlineKeyboardMarkup | None:
    if not plan.orders:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Обновить", callback_data="fbs:refresh")]])
    rows = [[InlineKeyboardButton("📄 Документы (ярлыки + лист сборки)",
                                  callback_data="fbs:docs")]]
    n = len(plan.started_orders())
    if n:
        rows.append([InlineKeyboardButton(f"✅ Собрано ({n})",
                                          callback_data="fbs:ready")])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="fbs:refresh")])
    return InlineKeyboardMarkup(rows)


async def _load_plan() -> tuple[fbs.YMFbsClient, fbs.FbsPlan]:
    client = fbs.make_client()
    plan = await asyncio.to_thread(fbs.load_plan, client)
    return client, plan


def _write_allowed(user_id: int) -> bool:
    """Строгая проверка для действий, меняющих статусы в Маркете.

    В отличие от _is_allowed бота (пустой/битый TG_ALLOWED_USERS → пускает
    всех), здесь пустой или нечитаемый список = запрет.
    """
    raw = os.environ.get("TG_ALLOWED_USERS", "").strip()
    if not raw:
        return False
    try:
        allowed = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        return False
    return user_id in allowed


def _err_text(e: Exception) -> str:
    if isinstance(e, KeyError):
        return f"❌ Не задана переменная окружения {e}"
    hint = ""
    if isinstance(e, fbs.YMApiError) and e.status_code in (401, 403):
        hint = ("\nПроверь права API-ключа: нужен доступ к заказам "
                "(inventory-and-order-processing).")
    return f"❌ Ошибка: <code>{html.escape(str(e))[:800]}</code>{hint}"


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────

def register_fbs_handlers(app: Application,
                          is_allowed: Callable[[int], bool]) -> None:

    async def cmd_fbs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        msg = await update.message.reply_text("⏳ Загружаю FBS-заказы на сегодня…")
        try:
            _, plan = await _load_plan()
            await msg.edit_text(_clip(fbs.format_summary(plan)),
                                parse_mode="HTML", reply_markup=_keyboard(plan))
        except Exception as e:
            logging.exception("cmd_fbs failed")
            await msg.edit_text(_err_text(e), parse_mode="HTML")

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not is_allowed(query.from_user.id):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return
        action = query.data[len(CB_PREFIX):]
        if action in ("ready", "ready_yes") and not _write_allowed(query.from_user.id):
            await query.answer(
                "⛔ «Собрано» меняет статусы в Маркете — доступно только "
                "пользователям из TG_ALLOWED_USERS (сейчас список пуст "
                "или вас в нём нет).", show_alert=True)
            return
        await query.answer()
        try:
            if action == "refresh":
                await _refresh(query)
            elif action == "docs":
                await _send_docs(query, context)
            elif action == "ready":
                await _ask_ready(query, context)
            elif action == "ready_yes":
                await _do_ready(query, context)
            elif action == "ready_no":
                context.user_data.pop(READY_IDS_KEY, None)
                await _refresh(query)
        except Exception as e:
            logging.exception("fbs callback %s failed", action)
            await query.message.reply_text(_err_text(e), parse_mode="HTML")

    async def _refresh(query):
        await query.edit_message_text("⏳ Обновляю…")
        _, plan = await _load_plan()
        await query.edit_message_text(_clip(fbs.format_summary(plan)),
                                      parse_mode="HTML",
                                      reply_markup=_keyboard(plan))

    async def _send_docs(query, context):
        chat_id = query.message.chat_id
        status = await context.bot.send_message(
            chat_id, "⏳ Формирую документы (ярлыки Маркет генерирует до пары минут)…")
        client, plan = await _load_plan()
        if not plan.orders:
            await status.edit_text("✅ Отгружать сегодня нечего — документов нет.")
            return

        with tempfile.TemporaryDirectory(prefix="fbs_") as tmp:
            docs = await asyncio.to_thread(
                fbs.generate_documents, plan, client, Path(tmp))
            d = plan.ship_date.strftime("%d.%m.%Y")

            with docs.picking_path.open("rb") as f:
                await context.bot.send_document(
                    chat_id, f, filename=docs.picking_path.name,
                    caption=f"📋 Лист сборки FBS {d} — {len(plan.orders)} заказ., "
                            f"{plan.total_qty} шт")
            if docs.labels_path:
                with docs.labels_path.open("rb") as f:
                    await context.bot.send_document(
                        chat_id, f, filename=docs.labels_path.name,
                        caption=f"🏷 Ярлыки FBS {d} (в порядке листа сборки)")

            await status.edit_text("⏳ Выкладываю на Google Drive…")
            await asyncio.to_thread(fbs.upload_to_drive, docs, plan.ship_date)

        lines = ["✅ <b>Документы готовы</b>"]
        if docs.labels_error:
            lines.append(f"⚠️ Ярлыки не получены: "
                         f"<code>{html.escape(docs.labels_error)[:500]}</code>")
        if docs.drive_url:
            lines.append(f'📁 <a href="{docs.drive_url}">Папка на Google Drive</a>')
        else:
            lines.append(f"⚠️ Drive: {html.escape(docs.drive_error)[:500]}")
        await status.edit_text("\n".join(lines), parse_mode="HTML",
                               disable_web_page_preview=True)

    async def _ask_ready(query, context):
        await query.edit_message_text("⏳ Проверяю, какие заказы не собраны…")
        _, plan = await _load_plan()
        started = plan.started_orders()
        if not started:
            await query.edit_message_text(
                _clip(fbs.format_summary(plan) +
                      "\n\n✅ Все заказы уже в «Готов к отгрузке»."),
                parse_mode="HTML", reply_markup=_keyboard(plan))
            return
        context.user_data[READY_IDS_KEY] = [o.order_id for o in started]
        qty = sum(o.total_qty for o in started)
        text = (f"❓ Перевести <b>{len(started)}</b> заказ(ов), {qty} шт, в статус "
                f"<b>«Готов к отгрузке»</b> в Яндекс.Маркете?\n\n"
                f"Делай это только когда всё упаковано и ярлыки наклеены. "
                f"Отменить из бота нельзя.")
        skipped = [o.order_id for o in plan.orders
                   if o.substatus == "STARTED" and o.needs_cis]
        if skipped:
            text += (f"\n\n🏷 Без КИЗ, будут пропущены: "
                     f"{', '.join(map(str, skipped[:10]))}")
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton(
                                              "✅ Да, перевести",
                                              callback_data="fbs:ready_yes"),
                                          InlineKeyboardButton(
                                              "✖️ Отмена",
                                              callback_data="fbs:ready_no"),
                                      ]]))

    async def _do_ready(query, context):
        confirmed = set(context.user_data.pop(READY_IDS_KEY, []))
        if not confirmed:
            await query.edit_message_text(
                "⚠️ Подтверждение устарело (бот перезапускался?). "
                "Нажми «Собрано» ещё раз.")
            return
        await query.edit_message_text("⏳ Меняю статусы в Маркете…")
        client, plan = await _load_plan()
        skipped_cis = [o.order_id for o in plan.orders
                       if o.substatus == "STARTED" and o.needs_cis]
        # Переводим только то, что подтвердил человек И что всё ещё STARTED.
        plan.orders = [o for o in plan.orders if o.order_id in confirmed]
        res = await asyncio.to_thread(fbs.mark_ready_to_ship, client, plan)
        res.skipped_cis = skipped_cis

        user = query.from_user.username or query.from_user.first_name or ""
        logging.info("FBS READY_TO_SHIP by %s (%s): ok=%s failed=%s",
                     user, query.from_user.id, res.updated, res.failed)
        lines = [f"✅ Переведено в «Готов к отгрузке»: <b>{len(res.updated)}</b>"]
        if res.failed:
            lines.append(f"❌ Не удалось: {len(res.failed)}")
            for oid, err in res.failed[:10]:
                lines.append(f"• {oid}: <code>{html.escape(err)[:200]}</code>")
        if res.skipped_cis:
            lines.append(f"🏷 Пропущены (нужен КИЗ): "
                         f"{', '.join(map(str, res.skipped_cis[:10]))}")
        _, fresh = await _load_plan()
        await query.edit_message_text(
            _clip("\n".join(lines) + "\n\n" + fbs.format_summary(fresh)),
            parse_mode="HTML", reply_markup=_keyboard(fresh))

    app.add_handler(CommandHandler("fbs", cmd_fbs))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^fbs:"))
