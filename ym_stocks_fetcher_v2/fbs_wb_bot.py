"""
fbs_wb_bot.py — Telegram-часть модуля 7.1 (FBS Wildberries), этапы 1–2.

Независим от Яндекс-части (fbs_bot_handlers.py): свои команды, свой
префикс callback, ни одного импорта из YM-модулей. Подключается в
tg_supplies_bot.py под флагом WB_API_TOKEN:
    register_fbs_wb_handlers(app, is_allowed=_is_allowed)

Команды:
  /fbs_wb [склад] [auto|new] [стикер=58x40|40x30|zpl] [supply=WB-GI-…]
        — сводка по складу: всё новое + уже на сборке. Даты у WB нет.
          Склад — ID или часть названия; без него: FBS_WB_WAREHOUSE или
          единственный склад с заданиями, иначе кнопки выбора склада.
  /fbs_wb_check <id> [id…]   — почему задание (не) в листе.
  /fbs_wb_boxes <WB-GI-…> <K> — грузомест в поставке = K (больше, чем кнопок).

Кнопки (порядок — DESIGN_FBS_WB.md §4.2):
    [🏬 Склад — N]           → сводка по складу
    [📦 Собрать поставку (N)] → ЗАПИСЬ: задания → поставка, стикеры заданий
                               + лист сборки → чат и Drive → «сейчас N шт,
                               сколько грузомест?»
    [1] [2] … / [🖨 Перепечатать] → ЗАПИСЬ: грузомест = K, их стикеры
    [📋 Лист сборки]  [🔄 Обновить] — только чтение

Запись — только пользователям из TG_ALLOWED_USERS (пустой список = никому).
Блокирующая работа — в asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
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

# Двойное нажатие «Собрать» не должно создать две поставки: одна запись на склад/поставку.
_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


def _clip(text: str) -> str:
    return text if len(text) <= TG_MAX_LEN else text[:TG_MAX_LEN] + "\n…(обрезано)"


@dataclass
class WbArgs:
    warehouse: str = ""
    mode: str = ""             # auto | new
    sticker: str = ""          # 58x40 | 40x30 | zpl
    supply: str = ""           # WB-GI-…


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
                if not re.match(wb.SUPPLY_ID_RE, out.supply):
                    raise ValueError("Поставка — в виде WB-GI-123456")
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


def _default_mode() -> str:
    m = os.environ.get("FBS_WB_SUPPLY_MODE", "auto").strip().lower()
    return m if m in wb.SUPPLY_MODES else "auto"


def _default_sticker() -> str:
    s = os.environ.get("FBS_WB_STICKER", wb.DEFAULT_STICKER).strip().lower()
    return s if s in wb.STICKER_FORMATS else wb.DEFAULT_STICKER


def _write_allowed(user_id: int) -> bool:
    """Строгая проверка для действий, меняющих данные в WB (копия логики
    YM-модуля, без импорта): пустой или нечитаемый TG_ALLOWED_USERS = запрет."""
    raw = os.environ.get("TG_ALLOWED_USERS", "").strip()
    if not raw:
        return False
    try:
        allowed = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        return False
    return user_id in allowed


# callback_data ≤ 64 байт:
#   wh:<склад> · pdf:<склад> · take:<склад>:<auto|new|WB-GI-…>:<стикер>
#   box:<WB-GI-…>:<K>:<стикер>   (K=0 — не менять число, только перепечатать)
def _cb(*parts) -> str:
    return CB_PREFIX + ":".join(str(p) for p in parts)


def _choice_keyboard(exc: wb.WarehouseChoiceNeeded) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏬 {w.name[:30]} — {n}",
                                  callback_data=_cb("wh", w.id))]
            for w, n in exc.options[:20]]
    return InlineKeyboardMarkup(rows)


def _keyboard(plan: wb.WbPlan, args: WbArgs) -> InlineKeyboardMarkup:
    wid = plan.warehouse.id
    rows = []
    if plan.orders:
        take = len(plan.to_take)
        label = (f"📦 Собрать поставку ({take})" if take
                 else "📦 Документы поставки")
        rows.append([InlineKeyboardButton(label, callback_data=_cb(
            "take", wid, args.supply or args.mode or _default_mode(),
            args.sticker or _default_sticker()))])
        rows.append([InlineKeyboardButton("📋 Лист сборки (PDF)",
                                          callback_data=_cb("pdf", wid))])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=_cb("wh", wid))])
    return InlineKeyboardMarkup(rows)


def _box_keyboard(supply_id: str, units: int, existing: int,
                  fmt: str) -> InlineKeyboardMarkup:
    n = min(wb.BOX_BUTTONS, wb.max_boxes(units))
    nums = [InlineKeyboardButton(("✓" if k == existing else "") + str(k),
                                 callback_data=_cb("box", supply_id, k, fmt))
            for k in range(1, n + 1)]
    rows = [nums[i:i + 3] for i in range(0, len(nums), 3)]
    if existing:
        rows.insert(0, [InlineKeyboardButton(
            f"🖨 Перепечатать стикеры мест ({existing})",
            callback_data=_cb("box", supply_id, 0, fmt))])
    return InlineKeyboardMarkup(rows)


def _err_text(e: Exception) -> str:
    if isinstance(e, KeyError):
        return f"❌ Не задана переменная окружения {e}"
    if isinstance(e, (ValueError, wb.SupplyClosed)):
        return f"⚠️ {html.escape(str(e))}"
    hint = ""
    if isinstance(e, wb.WBApiError) and e.status_code in (401, 403):
        hint = ("\nПроверь токен WB_API_TOKEN: категория «Маркетплейс», "
                "права на запись, не истёк ли срок.")
    return f"❌ Ошибка WB: <code>{html.escape(str(e))[:800]}</code>{hint}"


async def _load(ref: str | None) -> tuple[wb.WbPlan | None, wb.WarehouseChoiceNeeded | None]:
    client = wb.make_client()
    try:
        return await asyncio.to_thread(wb.load_plan, client, ref), None
    except wb.WarehouseChoiceNeeded as e:
        return None, e


async def _send_files(bot, chat_id: int, files: list[tuple[Path, str]]) -> None:
    for path, caption in files:
        with path.open("rb") as f:
            await bot.send_document(chat_id, f, filename=path.name, caption=caption)


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────

def register_fbs_wb_handlers(app: Application,
                             is_allowed: Callable[[int], bool]) -> None:

    async def _show(edit, ref: str | None, args: WbArgs | None = None):
        plan, choice = await _load(ref)
        if choice is not None:
            await edit(_clip(wb.format_choice(choice)), parse_mode="HTML",
                       reply_markup=_choice_keyboard(choice))
        else:
            await edit(_clip(wb.format_summary(plan)), parse_mode="HTML",
                       reply_markup=_keyboard(plan, args or WbArgs()))

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
            await _show(msg.edit_text, args.warehouse or None, args)
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

    async def cmd_fbs_wb_boxes(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        args = context.args or []
        if len(args) != 2 or not args[1].isdigit():
            await update.message.reply_text(
                "Использование: /fbs_wb_boxes WB-GI-123456 8")
            return
        if not _write_allowed(update.effective_user.id):
            await update.message.reply_text(
                "⛔ Грузоместа меняются в WB — только для TG_ALLOWED_USERS.")
            return
        msg = await update.message.reply_text("⏳ Грузоместа…")
        await _do_boxes(context.bot, update.effective_chat.id, msg.edit_text,
                        args[0].upper(), int(args[1]), _default_sticker(),
                        update.effective_user)

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not is_allowed(query.from_user.id):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return
        action, *parts = query.data[len(CB_PREFIX):].split(":")
        if action in ("take", "box") and not _write_allowed(query.from_user.id):
            await query.answer(
                "⛔ Это меняет данные в WB — доступно только пользователям из "
                "TG_ALLOWED_USERS (сейчас список пуст или вас в нём нет).",
                show_alert=True)
            return
        await query.answer()
        try:
            if action == "wh":
                await query.edit_message_text("⏳ Загружаю задания WB…")
                await _show(query.edit_message_text, parts[0])
            elif action == "pdf":
                await _send_pdf(query, context, parts[0])
            elif action == "take":
                await _take(query, context, *parts)
            elif action == "box":
                sid, k, fmt = parts
                await _do_boxes(context.bot, query.message.chat_id,
                                query.edit_message_text, sid,
                                int(k) or None, fmt, query.from_user)
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
            await _send_files(context.bot, chat_id, [(path, (
                f"📋 Лист сборки FBS WB «{plan.warehouse.name}» — "
                f"{len(plan.orders)} зад."))])

    async def _take(query, context, wh: str, target: str, fmt: str):
        chat_id = query.message.chat_id
        mode, supply = (("auto", target) if target.startswith("WB-GI-")
                        else (target, ""))
        lock = _lock(f"wh:{wh}")
        if lock.locked():
            await context.bot.send_message(chat_id, "⏳ Уже собираю поставку по этому складу…")
            return
        async with lock:
            status = await context.bot.send_message(
                chat_id, "⏳ Собираю поставку: задания → поставка, стикеры, лист…")
            client = wb.make_client()
            with tempfile.TemporaryDirectory(prefix="fbs_wb_") as tmp:
                res, plan, docs = await asyncio.to_thread(
                    wb.collect_supplies, client, wh, mode, supply, fmt, Path(tmp))
                user = query.from_user
                logging.info("FBS WB take wh=%s mode=%s supply=%s by %s (%s): "
                             "created=%s added=%s failed=%s", wh, mode, supply,
                             user.username, user.id, res.created,
                             {k: len(v) for k, v in res.added.items()}, res.failed)
                files, paths = [], []
                for d in docs:
                    if d.stickers_path:
                        files.append((d.stickers_path,
                                      f"🏷 Стикеры заданий {d.supply_id} — {d.units} шт "
                                      f"(в порядке листа)"))
                    files.append((d.picking_path, f"📋 Лист сборки {d.supply_id}"))
                    paths += [p for p in (d.stickers_path, d.picking_path) if p]
                await _send_files(context.bot, chat_id, files)
                text = wb.format_take(res, plan, docs)
                if paths:
                    await status.edit_text(_clip(text + "\n\n⏳ Выкладываю на Google Drive…"),
                                           parse_mode="HTML")
                    url, err = await asyncio.to_thread(wb.upload_to_drive, paths)
                    text += (f'\n📁 <a href="{url}">Папка WB на Google Drive</a>' if url
                             else f"\n⚠️ Drive: {html.escape(err)[:300]}")
                await status.edit_text(_clip(text), parse_mode="HTML",
                                       disable_web_page_preview=True)

            # Вопрос про грузоместа — отдельным сообщением на каждую поставку.
            for d in docs:
                if not d.pp_allowed:
                    await context.bot.send_message(
                        chat_id, wb.format_box_prompt(d, 0), parse_mode="HTML")
                    continue
                existing = len(await asyncio.to_thread(client.list_trbx, d.supply_id))
                await context.bot.send_message(
                    chat_id, wb.format_box_prompt(d, existing), parse_mode="HTML",
                    reply_markup=_box_keyboard(d.supply_id, d.units, existing, fmt))
        try:
            await query.edit_message_text(_clip(wb.format_summary(plan)),
                                          parse_mode="HTML",
                                          reply_markup=_keyboard(plan, WbArgs()))
        except Exception:
            logging.debug("summary refresh skipped", exc_info=True)

    async def _do_boxes(bot, chat_id: int, edit, sid: str, k: int | None,
                        fmt: str, user):
        lock = _lock(f"supply:{sid}")
        if lock.locked():
            await bot.send_message(chat_id, f"⏳ Уже меняю грузоместа {sid}…")
            return
        async with lock:
            try:
                await edit(f"⏳ {sid}: грузоместа и стикеры…")
                with tempfile.TemporaryDirectory(prefix="fbs_wb_") as tmp:
                    res = await asyncio.to_thread(
                        wb.set_boxes, wb.make_client(), sid, k, Path(tmp), fmt)
                    logging.info("FBS WB boxes %s k=%s by %s (%s): total=%d "
                                 "created=%s deleted=%s", sid, k, user.username,
                                 user.id, len(res.trbx_ids), res.created, res.deleted)
                    text = wb.format_boxes(res)
                    if res.stickers_path:
                        await _send_files(bot, chat_id, [(
                            res.stickers_path,
                            f"📦 Стикеры грузомест {sid} — {len(res.trbx_ids)} шт")])
                        url, err = await asyncio.to_thread(
                            wb.upload_to_drive, [res.stickers_path])
                        text += (f'\n📁 <a href="{url}">Папка WB на Google Drive</a>'
                                 if url else f"\n⚠️ Drive: {html.escape(err)[:300]}")
                await edit(_clip(text), parse_mode="HTML",
                           disable_web_page_preview=True,
                           reply_markup=_box_keyboard(sid, res.units,
                                                      len(res.trbx_ids), fmt))
            except Exception as e:
                logging.exception("fbs_wb boxes %s failed", sid)
                await edit(_err_text(e), parse_mode="HTML")

    app.add_handler(CommandHandler("fbs_wb", cmd_fbs_wb))
    app.add_handler(CommandHandler("fbs_wb_check", cmd_fbs_wb_check))
    app.add_handler(CommandHandler("fbs_wb_boxes", cmd_fbs_wb_boxes))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^fbswb:"))
