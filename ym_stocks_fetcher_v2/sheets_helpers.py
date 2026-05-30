"""
sheets_helpers.py — общие утилиты для всех YM-модулей.

Решает две главные проблемы Google Sheets API:
1. Квота 60 write/min/user. При построчном delete_rows() мы её мгновенно
   выжигаем. Здесь — батчевое удаление через batchUpdate одним запросом.
2. Float-значения inf/nan ломают JSON-сериализацию. Здесь — sanitize_value()
   на каждую ячейку.

Плюс автоматический retry на 429 (если квота всё-таки упёрлась).
"""

from __future__ import annotations
import logging
import math
import time
from typing import Any, Iterable

import gspread
from gspread.exceptions import APIError


# ----------------------------------------------------------------------
# Санитизация значений
# ----------------------------------------------------------------------
def sanitize_value(v: Any) -> Any:
    """Приводит значение к JSON-сериализуемому виду.

    - float inf/nan → ""
    - очень большие числа → "" (Sheets всё равно их не покажет нормально)
    - None → ""
    - всё остальное возвращает как есть
    """
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isinf(v) or math.isnan(v):
            return ""
        # Защита от безумных значений (>1e15 - переполнение даже для float64
        # в JSON многих парсеров)
        if abs(v) > 1e15:
            logging.warning("Clipping extreme float value: %s", v)
            return ""
        return v
    return v


def sanitize_row(row: list) -> list:
    """Применяет sanitize_value к каждой ячейке."""
    return [sanitize_value(c) for c in row]


def sanitize_rows(rows: Iterable[list]) -> list[list]:
    return [sanitize_row(r) for r in rows]


# ----------------------------------------------------------------------
# Retry на 429 / 503
# ----------------------------------------------------------------------
def with_retry(func, *args, max_attempts: int = 6, base_wait: float = 2.0,
               **kwargs):
    """Вызывает func(*args, **kwargs) с экспоненциальным retry на 429/503.

    Sheets API: квота 60 write/min/user → 429.
    Восстановление 60 секунд, делаем wait 5/10/20/40/60 секунд.
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            code = getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
            # gspread.APIError имеет .response с http статусом
            status = code or _extract_status(e)
            if status in (429, 503):
                wait = min(base_wait ** (attempt + 1), 60.0)
                logging.warning("Sheets API %d, retry %d/%d in %.1fs",
                                status, attempt + 1, max_attempts, wait)
                time.sleep(wait)
                last_err = e
                continue
            raise
    raise RuntimeError(f"Sheets API max retries exceeded: {last_err}")


def _extract_status(e: Exception) -> int | None:
    """Достаёт HTTP статус из gspread APIError разными способами."""
    if hasattr(e, 'response') and e.response is not None:
        return getattr(e.response, 'status_code', None)
    msg = str(e)
    if msg.startswith("APIError: ["):
        try:
            return int(msg.split("[")[1].split("]")[0])
        except (IndexError, ValueError):
            pass
    return None


# ----------------------------------------------------------------------
# Батчевое удаление строк
# ----------------------------------------------------------------------
def collapse_to_ranges(row_indices: list[int]) -> list[tuple[int, int]]:
    """[3, 4, 5, 8, 9, 12] → [(3, 6), (8, 10), (12, 13)]

    Возвращает полуоткрытые диапазоны [start, end) в нотации Google API
    (0-indexed). На входе индексы 1-indexed (как в gspread), на выходе
    переведено в 0-indexed.
    """
    if not row_indices:
        return []
    sorted_idx = sorted(set(row_indices))
    ranges: list[tuple[int, int]] = []
    start = sorted_idx[0]
    prev = start
    for i in sorted_idx[1:]:
        if i == prev + 1:
            prev = i
        else:
            # закрываем текущий range
            ranges.append((start - 1, prev))  # 0-indexed start, 0-indexed end (exclusive)
            start = i
            prev = i
    ranges.append((start - 1, prev))
    return ranges


def batch_delete_rows(worksheet, row_indices: list[int]) -> int:
    """Удаляет указанные строки (1-indexed) ОДНИМ запросом batchUpdate.

    Группирует смежные строки в диапазоны и удаляет их сверху вниз
    в обратном порядке (чтобы индексы не сдвигались).

    Возвращает количество удалённых строк.
    """
    if not row_indices:
        return 0

    ranges = collapse_to_ranges(row_indices)
    # Удаляем СНИЗУ ВВЕРХ, чтобы верхние индексы не сдвигались при удалении
    ranges.sort(key=lambda r: r[0], reverse=True)

    sheet_id = worksheet._properties['sheetId']
    requests_body = []
    for start_0, end_0 in ranges:
        requests_body.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start_0,
                    "endIndex": end_0,
                }
            }
        })

    body = {"requests": requests_body}
    with_retry(worksheet.spreadsheet.batch_update, body)

    total = sum(end - start for start, end in ranges)
    logging.info("Batch-deleted %d rows in %d range(s)", total, len(ranges))
    return total


# ----------------------------------------------------------------------
# Батчевая запись (одним запросом) с санитизацией и retry
# ----------------------------------------------------------------------
def safe_append_rows(worksheet, rows: Iterable[list],
                     value_input_option: str = "USER_ENTERED") -> int:
    """Санитизирует значения и пишет одним запросом с retry."""
    sanitized = sanitize_rows(rows)
    if not sanitized:
        return 0

    try:
        with_retry(worksheet.append_rows, sanitized,
                   value_input_option=value_input_option)
        return len(sanitized)
    except Exception as e:
        # Если упало — попробуем найти "плохую" строку
        logging.error("append_rows failed: %s", e)
        # Логируем для отладки первые несколько строк
        for i, row in enumerate(sanitized[:5]):
            logging.error("  Row %d: %s", i, row)
        raise
