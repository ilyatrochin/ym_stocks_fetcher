# Финальный деплой — v4

## Что починено

### `fetch_ym_orders.py`

JSON-выгрузка показала что поля API называются **по-другому**, чем
ожидал старый парсер. Все поля поправлены:

| Поле | Было | Стало |
|---|---|---|
| ID заказа | `order.id` | `order.orderId` |
| Цена | `item.price` | `item.prices.payment.value` |
| Субсидия | `item.subsidies[].amount` | `item.prices.subsidy.value` |
| Дата доставки | `delivery.deliveryDate` | `delivery.dates.realDeliveryDate` |
| Регион | `delivery.region.name` | **поля нет в FBY-API** — оставлено пустым |

### Дедупликация на трёх уровнях

1. **Внутри заказа** (если N одинаковых items с count=1 → один item с count=N).
2. **Между страницами/чанками** (set seen_order_ids).
3. **При чтении в forecast** (set seen_order_sku).

Это значит: даже если в листе уже накопился мусор, новый forecast его
проигнорирует. И при следующем backfill — лист перезальётся чисто.

### `forecast_stocks.py`

- `SAFETY_DAYS`: **21 → 10**
- Скорость теперь = `(created + delivered) / 2` — компромисс между
  быстрым сигналом спроса и реальными доставками без отмен.
- Строки с пустым `order_id` пропускаются (защита от legacy-данных).

## Порядок деплоя

```bash
# 1. Заменить файлы
cp fetch_ym_orders.py /opt/openclaw/skills/ym_stocks_fetcher/
cp forecast_stocks.py /opt/openclaw/skills/ym_stocks_fetcher/

# 2. Очистить лист ym_orders_raw в Google Sheets
#    Выделить все строки данных под заголовком → Delete rows
#    (Заголовок и intro-строки НЕ трогать)

# 3. Сделать чистый backfill за 30 дней
cd /opt/openclaw/skills/ym_stocks_fetcher
python fetch_ym_orders.py --from 2026-04-26 --to 2026-05-26 -v

#    В логе должно быть что-то типа:
#    Fetching orders 2026-04-26..2026-05-25
#    Pagination done after N pages
#    Unique orders: ~150-300, total order-item rows: ~150-300

# 4. Прогнать forecast
python forecast_stocks.py --dry-run -v 2>&1 | tail -30

#    Должно быть:
#    - Routing loaded: ~30 active pairs
#    - Loaded warehouse sales: ~200 created + ~150 delivered
#    - Routing filter: kept ~30, dropped ~150
#    - Built ~30 forecast rows

# 5. Если выглядит разумно — пускать без --dry-run
python forecast_stocks.py
```

## Что должно прийти в Telegram

Что-то такое:

```
🔴 Срочно (< 7 дней до OOS):
• ЧХ6 / МО Софьино: остаток 4, скорость 3.4/день → ~1 день. Рекомендую 30 шт.
• ЧХ2 / МО Софьино: остаток 8, скорость 2.0/день → ~4 дня. Рекомендую 12 шт.

🟡 Скоро (7-14 дней):
• ЧХ5 / МО Софьино: 13 шт, ~7 дней. Рекомендую 5.

🟢 В норме: ~25 позиций
```

**Числа примерные** — настоящие появятся когда зальётся 30 дней данных.

## Проверки после первого реального запуска

В `ym_orders_raw` каждая строка должна иметь:
- ✅ `order_id` (не пустой, длинное число)
- ✅ `price_per_unit > 0`
- ✅ `warehouse_id` (обычно `172` для Софьино)
- ⚠️ `region` будет **пустым** — это нормально, API не возвращает
- ⚠️ В колонке `subsidy` теперь субсидия на весь item, не на единицу

Если в `ym_orders_raw` снова появятся дубли по order_id — пришлите
выгрузку, посмотрим что ещё. Но судя по структуре JSON их быть не
должно.

## CLI шпаргалка

```bash
# Стандартный ежедневный запуск (последние 30 дней)
python fetch_ym_orders.py

# Backfill за конкретный период
python fetch_ym_orders.py --from 2026-04-01 --to 2026-05-26

# Проверка без записи в Sheets
python fetch_ym_orders.py --dry-run -v

# Сохранить сырой JSON для диагностики
python fetch_ym_orders.py --debug-json /tmp/ym_debug --dry-run

# Forecast — с фильтром по routing и per-warehouse скоростями
python forecast_stocks.py

# Forecast без routing (вернуться к старой логике)
python forecast_stocks.py --no-routing --no-wh-speeds
```
