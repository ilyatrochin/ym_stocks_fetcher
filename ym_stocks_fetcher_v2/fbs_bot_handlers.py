"""
fbs_bot_handlers.py — Telegram-часть модуля 7 (FBS-отгрузка на сегодня).

Отдельно от FBY-команд tg_supplies_bot.py; подключается одной строкой:
    register_fbs_handlers(app, is_allowed=_is_allowed)

Команда:
  /fbs [дата] — что отгружать по FBS (сводка по SKU) + кнопки.
                Дата: сегодня (по умолчанию), «завтра», ДД.ММ, ДД.ММ.ГГГГ,
                ГГГГ-ММ-ДД. Кнопки помнят дату (она в callback_data).
  /fbs_check <номер> [номер…] [дата] — почему заказ не попал в лист:
                статус, подстатус, дата отгрузки.
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
from datetime import date, datetime, timedelta
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


def parse_ship_date(arg: str | None, today: date) -> date:
    """'' / 'сегодня' / 'завтра' / 'послезавтра' / ДД.ММ / ДД.ММ.ГГГГ / ГГГГ-ММ-ДД."""
    s = (arg or "").strip().lower()
    words = {"": 0, "сегодня": 0, "today": 0, "завтра": 1, "tomorrow": 1,
             "послезавтра": 2, "вчера": -1}
    if s in words:
        return today + timedelta(days=words[s])
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # ДД.ММ разбираем руками: strptime без года в новых Python запрещён
    try:
        dd, mm = (int(x) for x in s.split("."))
        d = date(today.year, mm, dd)
    except ValueError:
        raise ValueError(
            f"Не понял дату «{arg}». Примеры: /fbs завтра, /fbs 20.10, "
            f"/fbs 2026-10-20") from None
    # 03.01 в конце декабря — это уже следующий год
    if d < today - timedelta(days=180):
        d = d.replace(year=today.year + 1)
    return d


def _cb(action: str, ship_date: date) -> str:
    return f"{CB_PREFIX}{action}:{ship_date.isoformat()}"


def _keyboard(plan: fbs.FbsPlan) -> InlineKeyboardMarkup | None:
    d = plan.ship_date
    if not plan.orders:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Обновить", callback_data=_cb("refresh", d))]])
    rows = [[InlineKeyboardButton("📄 Документы (ярлыки + лист сборки)",
                                  callback_data=_cb("docs", d))]]
    n = len(plan.started_orders())
    if n:
        rows.append([InlineKeyboardButton(f"✅ Собрано ({n})",
                                          callback_data=_cb("ready", d))])
    rows.append([InlineKeyboardButton("🔄 Обновить",
                                      callback_data=_cb("refresh", d))])
    return InlineKeyboardMarkup(rows)


async def _load_plan(ship_date: date) -> tuple[fbs.YMFbsClient, fbs.FbsPlan]:
    client = fbs.make_client()
    plan = await asyncio.to_thread(fbs.load_plan, client, ship_date)
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
        try:
            ship_date = parse_ship_date(" ".join(context.args or []),
                                        fbs.today_local())
        except ValueError as e:
            await update.message.reply_text(f"❓ {e}")
            return
        msg = await update.message.reply_text(
            f"⏳ Загружаю FBS-заказы на {ship_date.strftime('%d.%m.%Y')}…")
        try:
            _, plan = await _load_plan(ship_date)
            await msg.edit_text(_clip(fbs.format_summary(plan)),
                                parse_mode="HTML", reply_markup=_keyboard(plan))
        except Exception as e:
            logging.exception("cmd_fbs failed")
            await msg.edit_text(_err_text(e), parse_mode="HTML")

    async def cmd_fbs_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        args = [a.strip(",") for a in (context.args or [])]
        ids = [int(a) for a in args if a.isdigit() and len(a) > 6]
        rest = [a for a in args if not (a.isdigit() and len(a) > 6)]
        if not ids:
            await update.message.reply_text(
                "Использование: /fbs_check 123456789 987654321 [дата]")
            return
        try:
            ship_date = parse_ship_date(" ".join(rest), fbs.today_local())
        except ValueError as e:
            await update.message.reply_text(f"❓ {e}")
            return
        try:
            client = fbs.make_client()
            found = await asyncio.to_thread(client.get_orders, ids)
        except Exception as e:
            logging.exception("cmd_fbs_check failed")
            await update.message.reply_text(_err_text(e), parse_mode="HTML")
            return
        lines = [f"🔎 <b>Проверка для листа на {ship_date:%d.%m.%Y}</b>"]
        seen = set()
        for raw in found:
            o = fbs.parse_order(raw)
            seen.add(o.order_id)
            ship = (date.fromisoformat(o.shipment_date).strftime("%d.%m")
                    if o.shipment_date else "—")
            lines.append(
                f"• <b>{o.order_id}</b>: {html.escape(str(raw.get('programType') or '?'))}, "
                f"{html.escape(str(raw.get('status') or '?'))}/"
                f"{html.escape(o.substatus or '—')}, отгрузка {ship}\n"
                f"  → {html.escape(fbs.exclusion_reason(raw, ship_date))}")
        for oid in ids:
            if oid not in seen:
                lines.append(f"• <b>{oid}</b>: не найден (другой кабинет?)")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not is_allowed(query.from_user.id):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return
        # fbs:<action>:<YYYY-MM-DD>; без даты (кнопки до обновления) — сегодня
        action, _, d = query.data[len(CB_PREFIX):].partition(":")
        try:
            ship_date = date.fromisoformat(d) if d else fbs.today_local()
        except ValueError:
            ship_date = fbs.today_local()
        if action in ("ready", "ready_yes") and not _write_allowed(query.from_user.id):
            await query.answer(
                "⛔ «Собрано» меняет статусы в Маркете — доступно только "
                "пользователям из TG_ALLOWED_USERS (сейчас список пуст "
                "или вас в нём нет).", show_alert=True)
            return
        await query.answer()
        try:
            if action == "refresh":
                await _refresh(query, ship_date)
            elif action == "docs":
                await _send_docs(query, context, ship_date)
            elif action == "ready":
                await _ask_ready(query, context, ship_date)
            elif action == "ready_yes":
                await _do_ready(query, context, ship_date)
            elif action == "ready_no":
                context.user_data.pop(READY_IDS_KEY, None)
                await _refresh(query, ship_date)
        except Exception as e:
            logging.exception("fbs callback %s failed", action)
            await query.message.reply_text(_err_text(e), parse_mode="HTML")

    async def _refresh(query, ship_date):
        await query.edit_message_text("⏳ Обновляю…")
        _, plan = await _load_plan(ship_date)
        await query.edit_message_text(_clip(fbs.format_summary(plan)),
                                      parse_mode="HTML",
                                      reply_markup=_keyboard(plan))

    async def _send_docs(query, context, ship_date):
        chat_id = query.message.chat_id
        status = await context.bot.send_message(
            chat_id, "⏳ Формирую документы (ярлыки Маркет генерирует до пары минут)…")
        client, plan = await _load_plan(ship_date)
        if not plan.orders:
            await status.edit_text(
                f"✅ На {ship_date.strftime('%d.%m.%Y')} отгружать нечего — "
                f"документов нет.")
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

    async def _ask_ready(query, context, ship_date):
        await query.edit_message_text("⏳ Проверяю, какие заказы не собраны…")
        _, plan = await _load_plan(ship_date)
        started = plan.started_orders()
        if not started:
            await query.edit_message_text(
                _clip(fbs.format_summary(plan) +
                      "\n\n✅ Все заказы уже в «Готов к отгрузке»."),
                parse_mode="HTML", reply_markup=_keyboard(plan))
            return
        context.user_data[READY_IDS_KEY] = [o.order_id for o in started]
        qty = sum(o.total_qty for o in started)
        text = (f"❓ Отгрузка {ship_date.strftime('%d.%m.%Y')}: перевести "
                f"<b>{len(started)}</b> заказ(ов), {qty} шт, в статус "
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
                                              callback_data=_cb("ready_yes", ship_date)),
                                          InlineKeyboardButton(
                                              "✖️ Отмена",
                                              callback_data=_cb("ready_no", ship_date)),
                                      ]]))

    async def _do_ready(query, context, ship_date):
        confirmed = set(context.user_data.pop(READY_IDS_KEY, []))
        if not confirmed:
            await query.edit_message_text(
                "⚠️ Подтверждение устарело (бот перезапускался?). "
                "Нажми «Собрано» ещё раз.")
            return
        await query.edit_message_text("⏳ Меняю статусы в Маркете…")
        client, plan = await _load_plan(ship_date)
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
        _, fresh = await _load_plan(ship_date)
        await query.edit_message_text(
            _clip("\n".join(lines) + "\n\n" + fbs.format_summary(fresh)),
            parse_mode="HTML", reply_markup=_keyboard(fresh))

    app.add_handler(CommandHandler("fbs", cmd_fbs))
    app.add_handler(CommandHandler("fbs_check", cmd_fbs_check))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^fbs:"))
