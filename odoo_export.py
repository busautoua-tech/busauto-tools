# -*- coding: utf-8 -*-
"""
Експорт даних з Odoo для дашборду власника BusAuto.

Версія 2 - стійка до різних конфігурацій Odoo:
  - перевіряє, які поля існують в моделі (модуль sale_margin може бути
    не встановлений - тоді поля 'margin'/'purchase_price' відсутні)
  - всі помилки пишуться в odoo_export.log + друкуються на екран
  - traceback при падінні зберігається у файл, щоб не загубитись при
    закритті чорного вікна

Зберігає JSON у dashboard_data/.
"""

import configparser
import xmlrpc.client as xc
import json
import os
import sys
import socket
import traceback
import ast
from datetime import datetime, timedelta
from collections import defaultdict, Counter

# Таймаут на кожен XML-RPC запит — запобігає вічному зависанню
socket.setdefaulttimeout(300)   # 5 хв на один запит

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "odoo_config.txt")
OUTPUT_DIR  = os.path.join(SCRIPT_DIR, "dashboard_data")
LOG_FILE    = os.path.join(SCRIPT_DIR, "odoo_export.log")
DAYS_BACK   = 30
CHUNK_SIZE  = 500


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return (cfg["odoo"]["url"].strip().rstrip("/"),
            cfg["odoo"]["database"].strip(),
            cfg["odoo"]["username"].strip(),
            cfg["odoo"]["api_key"].strip())


def odoo_connect(url, db, user, apikey):
    common = xc.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, apikey, {})
    if not uid:
        log("[ERROR] Авторизация не удалась - проверьте api_key и логин")
        raise SystemExit(1)
    info = common.version()
    log(f"[OK] Подключено к Odoo {info.get('server_version', '?')}, uid={uid}")
    return uid, xc.ServerProxy(f"{url}/xmlrpc/2/object")


def get_available_fields(models, db, uid, apikey, model, wanted):
    """Повертає тільки ті поля з wanted, які реально існують в моделі."""
    try:
        all_fields = models.execute_kw(db, uid, apikey, model, "fields_get",
                                       [], {"attributes": ["string", "type"]})
    except Exception as e:
        log(f"[WARN] fields_get для {model} не вдалось: {e}")
        return wanted
    available = [f for f in wanted if f in all_fields]
    missing = [f for f in wanted if f not in all_fields]
    if missing:
        log(f"[INFO] {model}: відсутні поля {missing} - будуть пропущені")
    return available


def search_read(models, db, uid, apikey, model, domain, fields, limit=None):
    kwargs = {"fields": fields}
    if limit:
        kwargs["limit"] = limit
    return models.execute_kw(db, uid, apikey, model, "search_read",
                             [domain], kwargs)


def export_sales(models, db, uid, apikey, since_date):
    log(f"[1/4] Замовлення з {since_date}...")
    domain = [
        ["date_order", ">=", since_date],
        ["state", "in", ["sale", "done"]],
    ]
    wanted = ["id", "name", "date_order", "partner_id",
              "amount_total", "amount_untaxed", "state",
              "team_id", "user_id", "company_id",
              # UTM / Джерело - джерело продажу (сайт/канал)
              "source_id", "medium_id", "campaign_id",
              # Prom UTM marks - реальні UTM-мітки з busauto.kh.ua (Prom)
              "prom_utm_mark"]
    fields = get_available_fields(models, db, uid, apikey,
                                  "sale.order", wanted)
    orders = search_read(models, db, uid, apikey,
                         "sale.order", domain, fields)
    log(f"      знайдено {len(orders)} замовлень")

    if not orders:
        return [], []

    order_ids = [o["id"] for o in orders]
    log(f"[2/4] Рядки замовлень...")
    line_wanted = ["id", "order_id", "product_id", "name",
                   "product_uom_qty", "price_unit", "price_subtotal",
                   "price_total", "purchase_price", "margin"]
    line_fields = get_available_fields(models, db, uid, apikey,
                                       "sale.order.line", line_wanted)
    lines = []
    for i in range(0, len(order_ids), CHUNK_SIZE):
        chunk = order_ids[i:i + CHUNK_SIZE]
        batch = search_read(models, db, uid, apikey, "sale.order.line",
                            [["order_id", "in", chunk]], line_fields)
        lines.extend(batch)
    log(f"      завантажено {len(lines)} рядків")
    return orders, lines


def export_products(models, db, uid, apikey, product_ids):
    if not product_ids:
        return []
    log(f"[3/4] Товари ({len(product_ids)})...")
    wanted = ["id", "name", "default_code", "categ_id",
              "list_price", "standard_price", "qty_available",
              "barcode", "type"]
    fields = get_available_fields(models, db, uid, apikey,
                                  "product.product", wanted)
    products = []
    ids = list(product_ids)
    for i in range(0, len(ids), CHUNK_SIZE):
        chunk = ids[i:i + CHUNK_SIZE]
        batch = search_read(models, db, uid, apikey, "product.product",
                            [["id", "in", chunk]], fields)
        products.extend(batch)
    log(f"      завантажено {len(products)} товарів")
    return products


def export_partners(models, db, uid, apikey, partner_ids):
    """
    Витягуємо партнерів з полем source_id (acquisition source - first-touch).
    Також рахуємо total_orders по кожному, щоб обчислити is_repeat.
    """
    if not partner_ids:
        return []
    log(f"[4/6] Партнери ({len(partner_ids)})...")
    wanted = ["id", "name", "source_id", "sale_order_count",
              "create_date", "country_id", "city"]
    fields = get_available_fields(models, db, uid, apikey,
                                  "res.partner", wanted)
    partners = []
    ids = list(partner_ids)
    for i in range(0, len(ids), CHUNK_SIZE):
        chunk = ids[i:i + CHUNK_SIZE]
        batch = search_read(models, db, uid, apikey, "res.partner",
                            [["id", "in", chunk]], fields)
        partners.extend(batch)
    with_acq = sum(1 for p in partners if p.get("source_id"))
    log(f"      завантажено {len(partners)} партнерів, з них з acquisition: {with_acq}")
    return partners


def parse_prom_utm(raw):
    """
    Парсить значення поля prom_utm_mark.
    Формат: рядок-репрезентація Python dict, наприклад
      "{'medium': 'cpc', 'source': 'google_pmax', 'campaign': '...'}"
    Повертає (source, medium, campaign) або (None, None, None).
    """
    if not raw or not isinstance(raw, str):
        return (None, None, None)
    raw = raw.strip()
    if not raw:
        return (None, None, None)
    try:
        d = ast.literal_eval(raw)
        if not isinstance(d, dict):
            return (None, None, None)
        return (d.get("source") or None,
                d.get("medium") or None,
                d.get("campaign") or None)
    except Exception:
        return (None, None, None)


def find_warehouse(models, db, uid, apikey, name_pattern="шевч"):
    """
    Знаходить склад за частиною назви/коду (шевч або shev).
    Повертає (warehouse_id, name, lot_stock_id, lot_stock_name).
    lot_stock_id - це і є локація 'Запаси' всередині складу.
    """
    try:
        warehouses = models.execute_kw(db, uid, apikey, "stock.warehouse",
            "search_read", [[]], {"fields": ["id", "name", "code", "lot_stock_id"]})
        log(f"      доступних складів: {len(warehouses)}")
        for w in warehouses:
            n = (w.get("name") or "").lower()
            c = (w.get("code") or "").lower()
            ls = w.get("lot_stock_id")
            ls_name = ls[1] if ls and isinstance(ls, list) else None
            log(f"        - id={w['id']}  name='{w.get('name')}'  code='{w.get('code')}'  lot_stock={ls_name}")
            if (name_pattern.lower() in n or name_pattern.lower() in c
                or "shev" in c or "shev" in n):
                lot_id = ls[0] if ls else None
                return w["id"], w.get("name"), lot_id, ls_name
    except Exception as e:
        log(f"[WARN] не вдалось зчитати warehouses: {e}")
    return None, None, None, None


def export_location_stock(models, db, uid, apikey, location_id):
    """
    Швидкий метод через stock.quant з мікро-пакетами по 50 шт
    (обходить MemoryError від кастомного _compute_display_name).

    Кроки:
      1. Шукаємо всі stock.quant на локації (з дочірніми) з quantity > 0
      2. Читаємо мікро-пакетами по 50 - отримуємо {product_id: total_qty}
      3. Для знайдених товарів читаємо метадані пакетами по 500
    """
    if not location_id:
        return {}

    log(f"      крок 1/3: шукаю stock.quant на локації {location_id} (з дочірніми)...")
    domain = [("location_id", "child_of", location_id), ("quantity", ">", 0)]
    try:
        quant_ids = models.execute_kw(db, uid, apikey, "stock.quant",
            "search", [domain])
    except Exception as e:
        log(f"[WARN] search stock.quant впав: {e}")
        return {}
    log(f"      знайдено quant-записів: {len(quant_ids)}")
    if not quant_ids:
        return {}

    log(f"      крок 2/3: читаю quants мікро-пакетами по 50 (агрегую по product_id)...")
    product_qty = {}
    MICRO = 50
    fail_count = 0
    for i in range(0, len(quant_ids), MICRO):
        chunk = quant_ids[i:i + MICRO]
        try:
            quants = models.execute_kw(db, uid, apikey, "stock.quant",
                "read", [chunk, ["product_id", "quantity"]])
            for q in quants:
                pidf = q.get("product_id")
                pid = pidf[0] if isinstance(pidf, list) and pidf else pidf
                qty = q.get("quantity", 0) or 0
                if pid:
                    product_qty[pid] = product_qty.get(pid, 0) + qty
        except Exception as e:
            fail_count += 1
            if fail_count <= 3:
                log(f"[WARN] read quants i={i}: {type(e).__name__} (продовжую)")
        # Прогрес кожні 20 пакетів = 1000 quants
        if (i // MICRO + 1) % 20 == 0:
            log(f"      ...прочитано {min(i+MICRO, len(quant_ids))} / {len(quant_ids)}, "
                f"товарів {len(product_qty)}, помилок {fail_count}")
    log(f"      готово: {len(product_qty)} унікальних товарів, всього помилок: {fail_count}")

    if not product_qty:
        log(f"[WARN] жодного товару не вдалось прочитати - fallback не реалізовано")
        return {}

    log(f"      крок 3/4: читаю метадані + incoming/outgoing для {len(product_qty)} товарів...")
    qty_map = {}
    # qty_available, virtual_available, incoming_qty, outgoing_qty - обчислюються
    # для конкретної локації через context={'location': X}
    fields_to_read = ["name", "default_code", "list_price", "standard_price",
                      "categ_id", "qty_available", "virtual_available",
                      "incoming_qty", "outgoing_qty"]
    pids = list(product_qty.keys())
    for i in range(0, len(pids), CHUNK_SIZE):
        chunk = pids[i:i + CHUNK_SIZE]
        try:
            batch = models.execute_kw(db, uid, apikey, "product.product",
                "read", [chunk, fields_to_read],
                {"context": {"location": location_id}})
            for p in batch:
                pid = p["id"]
                cat = p.get("categ_id")
                # qty_available з context'а має бути таким же як з stock.quant.
                # Якщо є розбіжність - беремо з context (надійніше).
                qty_ctx = p.get("qty_available", 0) or 0
                qty_quant = product_qty[pid]
                qty_map[pid] = {
                    "qty_available": qty_ctx if qty_ctx > 0 else qty_quant,
                    "virtual_available": p.get("virtual_available", 0) or 0,
                    "incoming_qty": p.get("incoming_qty", 0) or 0,
                    "outgoing_qty": p.get("outgoing_qty", 0) or 0,
                    "name": p.get("name") or "",
                    "default_code": p.get("default_code") or "",
                    "list_price": p.get("list_price", 0) or 0,
                    "standard_price": p.get("standard_price", 0) or 0,
                    "category": cat[1] if cat and isinstance(cat, list) else "",
                }
        except Exception as e:
            log(f"[WARN] read products batch i={i} впав: {e}")
    log(f"      готово: метадані для {len(qty_map)} товарів")

    # Крок 4: найближча дата приходу для кожного товара
    log(f"      крок 4/4: шукаю найближчу дату приходу...")
    next_arrivals = _find_next_arrivals(models, db, uid, apikey, location_id, pids)
    for pid, arr in next_arrivals.items():
        if pid in qty_map:
            qty_map[pid]["next_arrival_date"] = arr["date"]
            qty_map[pid]["next_arrival_qty"] = arr["qty"]
    log(f"      готово: дати поставок для {len(next_arrivals)} товарів")
    return qty_map


def export_stuck_pickings(models, db, uid, apikey, warehouse_id, days_threshold=7):
    """
    Знаходить pickings, які висять у проміжному стані (assigned/waiting/confirmed/
    partially_available) довше ніж days_threshold днів. Це товари, фізично
    оприбутковані або відвантажені, але в oDoo не валідовані.
    """
    if not warehouse_id:
        return []
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.now() - _td(days=days_threshold)).strftime("%Y-%m-%d %H:%M:%S")
    log(f"      шукаю pickings застрягли > {days_threshold} днів...")
    domain = [
        ("state", "in", ["assigned", "waiting", "confirmed",
                         "partially_available"]),
        ("scheduled_date", "<", cutoff),
        ("picking_type_id.warehouse_id", "=", warehouse_id),
    ]
    fields = ["id", "name", "state", "scheduled_date", "picking_type_id",
              "partner_id", "origin", "move_ids"]
    try:
        pickings = models.execute_kw(db, uid, apikey, "stock.picking",
            "search_read", [domain], {"fields": fields, "limit": 200})
    except Exception as e:
        log(f"[WARN] stock.picking search впав: {e}")
        return []
    log(f"      знайдено застряглих pickings: {len(pickings)}")
    if not pickings:
        return []

    # Тип пікінгу (incoming/outgoing/internal)
    pt_ids = list({p["picking_type_id"][0] for p in pickings
                   if p.get("picking_type_id")})
    pt_map = {}
    if pt_ids:
        try:
            pts = models.execute_kw(db, uid, apikey, "stock.picking.type",
                "read", [pt_ids, ["id", "code", "name"]])
            pt_map = {pt["id"]: pt for pt in pts}
        except Exception as e:
            log(f"[WARN] stock.picking.type read впав: {e}")

    # Деталі рухів - мікро-пакетами по 50
    all_move_ids = []
    for p in pickings:
        for mid in (p.get("move_ids") or []):
            all_move_ids.append(mid)
    moves_by_picking = defaultdict(list)
    for i in range(0, len(all_move_ids), 50):
        chunk = all_move_ids[i:i + 50]
        try:
            moves = models.execute_kw(db, uid, apikey, "stock.move",
                "read", [chunk, ["picking_id", "product_id",
                                 "product_uom_qty", "state"]])
            for m in moves:
                pidf = m.get("picking_id")
                pid = pidf[0] if isinstance(pidf, list) and pidf else pidf
                if pid:
                    prodf = m.get("product_id")
                    moves_by_picking[pid].append({
                        "product_name": prodf[1] if isinstance(prodf, list) and len(prodf) > 1 else "?",
                        "qty": m.get("product_uom_qty", 0) or 0,
                    })
        except Exception:
            pass  # Продовжуємо, мікро-пакет не критичний

    today = _dt.now()
    result = []
    for p in pickings:
        sched = p.get("scheduled_date") or ""
        days_stuck = None
        if sched:
            try:
                sched_dt = _dt.fromisoformat(sched.replace(" ", "T")[:19])
                days_stuck = (today - sched_dt).days
            except Exception:
                pass
        pt = pt_map.get(p["picking_type_id"][0]) if p.get("picking_type_id") else None
        moves = moves_by_picking.get(p["id"], [])
        total_qty = sum(m["qty"] for m in moves)
        result.append({
            "id": p["id"],
            "name": p.get("name") or "",
            "state": p.get("state"),
            "scheduled_date": sched,
            "days_stuck": days_stuck,
            "type_code": (pt or {}).get("code", "?"),
            "type_name": (pt or {}).get("name", ""),
            "partner": (p.get("partner_id") or [None, ""])[1] if p.get("partner_id") else "",
            "origin": p.get("origin") or "",
            "total_qty": round(total_qty, 1),
            "products_count": len(moves),
            "products_top": moves[:3],
        })
    result.sort(key=lambda x: x["days_stuck"] or 0, reverse=True)
    return result


def _find_next_arrivals(models, db, uid, apikey, location_id, product_ids):
    """
    Найближча дата приходу для кожного товара з stock.move.
    Шукаємо moves з location_dest_id == наша локація і state IN [...]
    """
    if not product_ids:
        return {}
    domain = [
        ("location_dest_id", "child_of", location_id),
        ("state", "in", ["confirmed", "assigned", "partially_available", "waiting"]),
        ("product_id", "in", list(product_ids)),
    ]
    try:
        move_ids = models.execute_kw(db, uid, apikey, "stock.move",
            "search", [domain],
            {"order": "date asc"})
    except Exception as e:
        log(f"[WARN] search stock.move впав: {e}")
        return {}
    log(f"      знайдено stock.move записів: {len(move_ids)}")
    if not move_ids:
        return {}

    # Мікро-пакети для уникнення MemoryError
    arrivals = {}  # product_id -> {'date': str, 'qty': float}
    MICRO = 50
    fail_count = 0
    for i in range(0, len(move_ids), MICRO):
        chunk = move_ids[i:i + MICRO]
        try:
            moves = models.execute_kw(db, uid, apikey, "stock.move",
                "read", [chunk, ["product_id", "product_uom_qty",
                                 "date", "state"]])
            for m in moves:
                pidf = m.get("product_id")
                pid = pidf[0] if isinstance(pidf, list) and pidf else pidf
                if not pid:
                    continue
                date = m.get("date")
                qty = m.get("product_uom_qty", 0) or 0
                if pid not in arrivals or (date and date < arrivals[pid]["date"]):
                    arrivals[pid] = {"date": date, "qty": qty}
        except Exception as e:
            fail_count += 1
            if fail_count <= 2:
                log(f"[WARN] read stock.move i={i}: {type(e).__name__}")
    if fail_count:
        log(f"      [INFO] stock.move помилок: {fail_count}")
    return arrivals


def export_returns(models, db, uid, apikey, since_date):
    """
    Повернення / скасовані замовлення.
    Дві джерела даних:
      1. sale.order з state='cancel' - скасовані замовлення
      2. account.move з move_type='out_refund' - кредит-ноти (повернення коштів)
    """
    log(f"[5/5] Скасовані замовлення і повернення з {since_date}...")

    # 1. Скасовані SO
    cancelled = []
    try:
        wanted = ["id", "name", "date_order", "amount_total",
                  "source_id", "partner_id"]
        fields = get_available_fields(models, db, uid, apikey,
                                      "sale.order", wanted)
        cancelled = search_read(
            models, db, uid, apikey, "sale.order",
            [["date_order", ">=", since_date], ["state", "=", "cancel"]],
            fields)
        log(f"      скасованих SO: {len(cancelled)}")
    except Exception as e:
        log(f"[WARN] не вдалось прочитати скасовані SO: {e}")

    # 2. Кредит-ноти (повернення)
    refunds = []
    try:
        wanted = ["id", "name", "invoice_date", "amount_total", "amount_untaxed",
                  "partner_id", "state"]
        fields = get_available_fields(models, db, uid, apikey,
                                      "account.move", wanted)
        refunds = search_read(
            models, db, uid, apikey, "account.move",
            [["invoice_date", ">=", since_date],
             ["move_type", "=", "out_refund"],
             ["state", "in", ["posted", "draft"]]],
            fields)
        log(f"      кредит-нот: {len(refunds)}")
    except Exception as e:
        log(f"[WARN] не вдалось прочитати account.move: {e}")

    return {"cancelled": cancelled, "refunds": refunds}


def export_stock(models, db, uid, apikey):
    log(f"[4/4] Залишки...")
    wanted = ["product_id", "location_id", "quantity", "available_quantity"]
    fields = get_available_fields(models, db, uid, apikey,
                                  "stock.quant", wanted)
    try:
        quants = search_read(models, db, uid, apikey, "stock.quant",
                             [["quantity", ">", 0]], fields)
    except Exception as e:
        log(f"[WARN] stock.quant читати не вдалось: {e}")
        return []
    totals = defaultdict(lambda: {"qty": 0.0, "available": 0.0})
    for q in quants:
        pid = q["product_id"][0] if q.get("product_id") else None
        if pid is None:
            continue
        totals[pid]["qty"] += q.get("quantity", 0) or 0
        if "available_quantity" in q:
            totals[pid]["available"] += q.get("available_quantity", 0) or 0
    log(f"      зведено по {len(totals)} SKU")
    return [{"product_id": pid, **vals} for pid, vals in totals.items()]


def build_summary(orders, lines, products, returns_data=None, partners=None,
                  warehouse_qty=None, warehouse_name=None, stuck_pickings=None,
                  yoy_orders=None):
    prod_by_id = {p["id"]: p for p in products}
    returns_data = returns_data or {"cancelled": [], "refunds": []}
    partners = partners or []
    partner_by_id = {p["id"]: p for p in partners}
    warehouse_qty = warehouse_qty or {}
    stuck_pickings = stuck_pickings or []
    yoy_orders = yoy_orders or []

    def src_name_static(rec, key):
        v = rec.get(key)
        return v[1] if v and isinstance(v, list) and len(v) > 1 else None

    revenue = sum(o.get("amount_total", 0) for o in orders)
    untaxed = sum(o.get("amount_untaxed", 0) for o in orders)
    n_orders = len(orders)
    aov = (revenue / n_orders) if n_orders else 0

    has_margin = any("margin" in l for l in lines)
    has_purchase = any("purchase_price" in l for l in lines)

    if has_margin:
        margin_total = sum(l.get("margin") or 0 for l in lines)
        margin_pct = (margin_total / untaxed * 100) if untaxed else 0
        cogs = untaxed - margin_total
    elif has_purchase:
        cogs = sum((l.get("purchase_price") or 0) * (l.get("product_uom_qty") or 0)
                   for l in lines)
        margin_total = untaxed - cogs
        margin_pct = (margin_total / untaxed * 100) if untaxed else 0
    else:
        cogs = None
        margin_total = None
        margin_pct = None
        log("[INFO] Дані по марже відсутні (нема ні margin, ні purchase_price)")

    def src_name(rec, key):
        v = rec.get(key)
        return v[1] if v and isinstance(v, list) and len(v) > 1 else "(не вказано)"

    by_day = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    by_source = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    by_medium = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    by_day_source = defaultdict(lambda: defaultdict(
        lambda: {"orders": 0, "revenue": 0.0}))

    for o in orders:
        day = (o.get("date_order") or "")[:10]
        if not day:
            continue
        rev = o.get("amount_total", 0)
        by_day[day]["orders"] += 1
        by_day[day]["revenue"] += rev
        s = src_name(o, "source_id")
        m = src_name(o, "medium_id")
        by_source[s]["orders"] += 1
        by_source[s]["revenue"] += rev
        by_medium[m]["orders"] += 1
        by_medium[m]["revenue"] += rev
        by_day_source[day][s]["orders"] += 1
        by_day_source[day][s]["revenue"] += rev

    daily = [{"date": d, **v} for d, v in sorted(by_day.items())]
    daily_by_source = [
        {"date": d, "source": s, **v}
        for d, srcs in sorted(by_day_source.items())
        for s, v in srcs.items()
    ]
    sources = sorted(
        [{"source": k, **v} for k, v in by_source.items()],
        key=lambda x: x["revenue"], reverse=True
    )
    mediums = sorted(
        [{"medium": k, **v} for k, v in by_medium.items()],
        key=lambda x: x["revenue"], reverse=True
    )

    # ---------- Prom UTM analytics ----------
    # Розпарсюємо prom_utm_mark - реальні UTM-мітки з busauto.kh.ua (Prom)
    by_prom_source = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    by_prom_campaign = defaultdict(lambda: {"orders": 0, "revenue": 0.0,
                                            "source": "", "medium": ""})
    prom_orders_count = 0
    for o in orders:
        utm_src, utm_med, utm_camp = parse_prom_utm(o.get("prom_utm_mark"))
        if not (utm_src or utm_med or utm_camp):
            continue
        prom_orders_count += 1
        rev = o.get("amount_total", 0)
        key_src = utm_src or "(empty)"
        by_prom_source[key_src]["orders"] += 1
        by_prom_source[key_src]["revenue"] += rev
        if utm_camp:
            by_prom_campaign[utm_camp]["orders"] += 1
            by_prom_campaign[utm_camp]["revenue"] += rev
            by_prom_campaign[utm_camp]["source"] = utm_src or ""
            by_prom_campaign[utm_camp]["medium"] = utm_med or ""
    prom_sources = sorted(
        [{"source": k, **v} for k, v in by_prom_source.items()],
        key=lambda x: x["revenue"], reverse=True
    )
    prom_campaigns = sorted(
        [{"campaign": k, **v} for k, v in by_prom_campaign.items()],
        key=lambda x: x["revenue"], reverse=True
    )[:30]

    # ---------- Acquisition source analytics ----------
    # Канал залучення (first-touch) на рівні клієнта.
    # Атрибуція: виручка кожного заказу - у acquisition_source партнера, який цей заказ зробив.
    by_acq = defaultdict(lambda: {"orders": 0, "revenue": 0.0, "customers": set()})
    no_acq_count = 0
    for o in orders:
        partner_ref = o.get("partner_id")
        if not partner_ref:
            continue
        pid = partner_ref[0]
        partner = partner_by_id.get(pid)
        rev = o.get("amount_total", 0)
        acq = src_name_static(partner, "source_id") if partner else None
        if not acq:
            no_acq_count += 1
            acq = "(не вказано)"
        by_acq[acq]["orders"] += 1
        by_acq[acq]["revenue"] += rev
        by_acq[acq]["customers"].add(pid)
    acquisition_sources = sorted(
        [{"source": k, "orders": v["orders"], "revenue": v["revenue"],
          "customers": len(v["customers"])} for k, v in by_acq.items()],
        key=lambda x: x["revenue"], reverse=True
    )

    # ---------- Repeat customer analytics ----------
    # Рахуємо к-сть заказів кожного клієнта в цьому періоді.
    # is_repeat = клієнт має більше 1 заказу В ЦЬОМУ ПЕРІОДІ
    # АБО загальна історія заказів (sale_order_count) > заказів у цьому періоді
    period_orders_per_partner = defaultdict(int)
    for o in orders:
        if o.get("partner_id"):
            period_orders_per_partner[o["partner_id"][0]] += 1

    repeat_orders = 0; repeat_revenue = 0.0; repeat_customers = set()
    new_orders = 0; new_revenue = 0.0; new_customers = set()
    for o in orders:
        if not o.get("partner_id"):
            continue
        pid = o["partner_id"][0]
        partner = partner_by_id.get(pid, {})
        # Загальна к-сть заказів цього клієнта (включно з історією поза періодом)
        total_orders_partner = partner.get("sale_order_count", 0) or 0
        period_orders = period_orders_per_partner[pid]
        # is_repeat: якщо у клієнта є хоч один заказ ПОЗА цим періодом - він повторний
        # Або якщо в цьому періоді більше 1 - теж повторний (з 2-го заказу)
        is_repeat = (total_orders_partner > period_orders) or (period_orders > 1)
        rev = o.get("amount_total", 0)
        if is_repeat:
            repeat_orders += 1
            repeat_revenue += rev
            repeat_customers.add(pid)
        else:
            new_orders += 1
            new_revenue += rev
            new_customers.add(pid)
    cohort = {
        "new": {"orders": new_orders, "revenue": round(new_revenue, 2),
                "customers": len(new_customers)},
        "repeat": {"orders": repeat_orders, "revenue": round(repeat_revenue, 2),
                   "customers": len(repeat_customers)},
        "repeat_revenue_pct": round(repeat_revenue / (repeat_revenue + new_revenue) * 100, 2)
                              if (repeat_revenue + new_revenue) else 0,
    }

    by_prod = defaultdict(lambda: {"units": 0.0, "revenue": 0.0,
                                   "name": "", "category": "",
                                   "default_code": ""})
    for l in lines:
        if not l.get("product_id"):
            continue
        pid = l["product_id"][0]
        by_prod[pid]["units"] += l.get("product_uom_qty") or 0
        by_prod[pid]["revenue"] += l.get("price_subtotal") or 0
        prod = prod_by_id.get(pid)
        if prod:
            by_prod[pid]["name"] = prod.get("name") or ""
            by_prod[pid]["default_code"] = prod.get("default_code") or ""
            categ = prod.get("categ_id")
            by_prod[pid]["category"] = categ[1] if categ else ""

    # Всі продані товари - для ABC аналізу
    all_sold = sorted(
        [{"product_id": pid, **v} for pid, v in by_prod.items()],
        key=lambda x: x["revenue"], reverse=True
    )
    top_products = all_sold[:50]

    # ---------- ABC analysis ----------
    # A: cumulative до 80% виручки
    # B: 80-95%
    # C: 95-100%
    total_rev_abc = sum(p["revenue"] for p in all_sold)
    abc_products = []
    cum_rev = 0.0
    a_count = b_count = c_count = 0
    a_rev = b_rev = c_rev = 0.0
    for p in all_sold:
        cum_rev += p["revenue"]
        cum_pct = (cum_rev / total_rev_abc * 100) if total_rev_abc else 0
        rev_share = (p["revenue"] / total_rev_abc * 100) if total_rev_abc else 0
        if cum_pct <= 80:
            cls = "A"; a_count += 1; a_rev += p["revenue"]
        elif cum_pct <= 95:
            cls = "B"; b_count += 1; b_rev += p["revenue"]
        else:
            cls = "C"; c_count += 1; c_rev += p["revenue"]
        # Підтягуємо залишок з products
        prod = prod_by_id.get(p["product_id"], {})
        qty_avail = prod.get("qty_available", 0) or 0
        # turnover: units sold / qty_available, > значит швидко обертається
        turnover = (p["units"] / qty_avail) if qty_avail else None
        abc_products.append({
            **p,
            "abc_class": cls,
            "rev_share_pct": round(rev_share, 3),
            "cum_rev_pct": round(cum_pct, 2),
            "qty_available": round(qty_avail, 1),
            "turnover": round(turnover, 2) if turnover is not None else None,
        })
    abc_summary = {
        "A": {"sku_count": a_count, "revenue": round(a_rev, 2),
              "rev_pct": round(a_rev / total_rev_abc * 100, 1) if total_rev_abc else 0,
              "sku_pct": round(a_count / len(all_sold) * 100, 1) if all_sold else 0},
        "B": {"sku_count": b_count, "revenue": round(b_rev, 2),
              "rev_pct": round(b_rev / total_rev_abc * 100, 1) if total_rev_abc else 0,
              "sku_pct": round(b_count / len(all_sold) * 100, 1) if all_sold else 0},
        "C": {"sku_count": c_count, "revenue": round(c_rev, 2),
              "rev_pct": round(c_rev / total_rev_abc * 100, 1) if total_rev_abc else 0,
              "sku_pct": round(c_count / len(all_sold) * 100, 1) if all_sold else 0},
        "total_sku": len(all_sold),
        "total_revenue": round(total_rev_abc, 2),
    }

    # ---------- Dead stock detection ----------
    # Товари з залишком > 0 і БЕЗ продажів за період
    sold_ids = set(by_prod.keys())
    dead_stock = []
    dead_value = 0.0
    for p in products:
        pid = p["id"]
        if pid in sold_ids:
            continue
        qty = p.get("qty_available", 0) or 0
        if qty <= 0:
            continue
        cost = p.get("standard_price", 0) or 0
        list_price = p.get("list_price", 0) or 0
        value = qty * cost  # вартість заморожена в залишку
        dead_stock.append({
            "product_id": pid,
            "name": p.get("name") or "",
            "default_code": p.get("default_code") or "",
            "qty_available": round(qty, 1),
            "standard_price": round(cost, 2),
            "list_price": round(list_price, 2),
            "frozen_value": round(value, 2),
        })
        dead_value += value
    dead_stock.sort(key=lambda x: x["frozen_value"], reverse=True)
    dead_stock_top = dead_stock[:50]

    # ---------- ABC по складу Шевченко ----------
    # Тільки товари, які реально лежать на цьому складі (qty>0)
    # Вираховуємо ABC за виручкою з продажів цих SKU за період.
    warehouse_abc = None
    if warehouse_qty:
        in_stock_products = []
        for pid, qinfo in warehouse_qty.items():
            qty = qinfo.get("qty_available", 0)
            if qty <= 0:
                continue
            sold_data = by_prod.get(pid, {"units": 0, "revenue": 0,
                                         "name": "", "category": "",
                                         "default_code": ""})
            name = sold_data["name"] or qinfo.get("name", "")
            code = sold_data.get("default_code") or qinfo.get("default_code", "")
            cat = sold_data["category"] or qinfo.get("category", "")
            cost = qinfo.get("standard_price", 0) or 0
            list_price = qinfo.get("list_price", 0) or 0
            incoming = qinfo.get("incoming_qty", 0) or 0
            outgoing = qinfo.get("outgoing_qty", 0) or 0
            virtual = qinfo.get("virtual_available", qty + incoming - outgoing)
            daily_sold = sold_data["units"] / DAYS_BACK if sold_data["units"] else 0
            # days_left по фактичному залишку
            days_left = (qty / daily_sold) if daily_sold > 0 else None
            # days_left_virtual - по прогнозу (з урахуванням приходу/розходу)
            days_left_virt = (virtual / daily_sold) if daily_sold > 0 else None
            # Рекомендоване замовлення: ціль 30 днів запасу. Якщо прогнозний запас
            # вже >= 30 днів - нічого не замовляти. Інакше: добити до 30 днів.
            TARGET_DAYS = 30
            if daily_sold > 0 and days_left_virt is not None:
                if days_left_virt >= TARGET_DAYS:
                    suggested_order = 0
                else:
                    suggested_order = max(0, round((TARGET_DAYS - days_left_virt) * daily_sold))
            else:
                suggested_order = 0
            in_stock_products.append({
                "product_id": pid,
                "name": name,
                "default_code": code,
                "category": cat,
                "qty_available": round(qty, 1),
                "incoming_qty": round(incoming, 1),
                "outgoing_qty": round(outgoing, 1),
                "virtual_available": round(virtual, 1),
                "next_arrival_date": qinfo.get("next_arrival_date"),
                "next_arrival_qty": round(qinfo.get("next_arrival_qty", 0) or 0, 1),
                "units_sold": sold_data["units"],
                "revenue": round(sold_data["revenue"], 2),
                "standard_price": round(cost, 2),
                "list_price": round(list_price, 2),
                "stock_value": round(qty * cost, 2),
                "daily_sold": round(daily_sold, 3),
                "days_left": round(days_left, 1) if days_left is not None else None,
                "days_left_virtual": round(days_left_virt, 1) if days_left_virt is not None else None,
                "suggested_order_qty": int(suggested_order),
                "suggested_order_value": round(suggested_order * cost, 2),
            })
        # Sort by revenue and assign ABC
        in_stock_products.sort(key=lambda x: x["revenue"], reverse=True)
        wh_total_rev = sum(p["revenue"] for p in in_stock_products)
        cum_rev = 0.0
        wh_a_count = wh_b_count = wh_c_count = 0
        wh_a_rev = wh_b_rev = wh_c_rev = 0.0
        wh_a_value = wh_b_value = wh_c_value = 0.0
        for p in in_stock_products:
            cum_rev += p["revenue"]
            cum_pct = (cum_rev / wh_total_rev * 100) if wh_total_rev else 0
            rev_share = (p["revenue"] / wh_total_rev * 100) if wh_total_rev else 0
            if cum_pct <= 80:
                cls = "A"; wh_a_count += 1; wh_a_rev += p["revenue"]; wh_a_value += p["stock_value"]
            elif cum_pct <= 95:
                cls = "B"; wh_b_count += 1; wh_b_rev += p["revenue"]; wh_b_value += p["stock_value"]
            else:
                cls = "C"; wh_c_count += 1; wh_c_rev += p["revenue"]; wh_c_value += p["stock_value"]
            p["abc_class"] = cls
            p["rev_share_pct"] = round(rev_share, 3)
            p["cum_rev_pct"] = round(cum_pct, 2)

        # Алерти по цим товарам
        # 1) A-class з низьким запасом - використовуємо days_left_virtual (з урахуванням
        #    incoming): якщо поставка вже в дорозі, товар не в критичному стані.
        # 2) C-class з великим запасом - теж по virtual (бо incoming збільшує запас)
        # 3) Товари без жодного продажу за період
        no_sales_in_stock = [p for p in in_stock_products if p["units_sold"] == 0]
        no_sales_value = sum(p["stock_value"] for p in no_sales_in_stock)
        a_low_stock = [p for p in in_stock_products
                       if p["abc_class"] == "A"
                       and p["days_left_virtual"] is not None
                       and p["days_left_virtual"] < 14]
        c_overstock = [p for p in in_stock_products
                       if p["abc_class"] == "C"
                       and p["days_left_virtual"] is not None
                       and p["days_left_virtual"] > 90 and p["units_sold"] > 0]
        total_stock_value = sum(p["stock_value"] for p in in_stock_products)

        warehouse_abc = {
            "warehouse_name": warehouse_name or "Шевченко",
            "total_sku": len(in_stock_products),
            "total_revenue": round(wh_total_rev, 2),
            "total_stock_value": round(total_stock_value, 2),
            "A": {"sku_count": wh_a_count, "revenue": round(wh_a_rev, 2),
                  "rev_pct": round(wh_a_rev / wh_total_rev * 100, 1) if wh_total_rev else 0,
                  "sku_pct": round(wh_a_count / len(in_stock_products) * 100, 1) if in_stock_products else 0,
                  "stock_value": round(wh_a_value, 2)},
            "B": {"sku_count": wh_b_count, "revenue": round(wh_b_rev, 2),
                  "rev_pct": round(wh_b_rev / wh_total_rev * 100, 1) if wh_total_rev else 0,
                  "sku_pct": round(wh_b_count / len(in_stock_products) * 100, 1) if in_stock_products else 0,
                  "stock_value": round(wh_b_value, 2)},
            "C": {"sku_count": wh_c_count, "revenue": round(wh_c_rev, 2),
                  "rev_pct": round(wh_c_rev / wh_total_rev * 100, 1) if wh_total_rev else 0,
                  "sku_pct": round(wh_c_count / len(in_stock_products) * 100, 1) if in_stock_products else 0,
                  "stock_value": round(wh_c_value, 2)},
            "products": in_stock_products[:1000],  # топ-1000
            "alerts": {
                "a_low_stock": sorted(a_low_stock, key=lambda x: x["days_left"])[:30],
                "c_overstock": sorted(c_overstock, key=lambda x: x["stock_value"], reverse=True)[:30],
                "no_sales_count": len(no_sales_in_stock),
                "no_sales_value": round(no_sales_value, 2),
                "no_sales_top": sorted(no_sales_in_stock, key=lambda x: x["stock_value"], reverse=True)[:50],
                "stuck_pickings": stuck_pickings,
                "stuck_in_count": sum(1 for sp in stuck_pickings if sp.get("type_code") == "incoming"),
                "stuck_out_count": sum(1 for sp in stuck_pickings if sp.get("type_code") == "outgoing"),
            },
        }

    by_cat = defaultdict(lambda: {"revenue": 0.0, "units": 0.0})
    for p in by_prod.values():
        cat = p["category"] or "Без категорії"
        by_cat[cat]["revenue"] += p["revenue"]
        by_cat[cat]["units"] += p["units"]
    categories = sorted(
        [{"category": k, **v} for k, v in by_cat.items()],
        key=lambda x: x["revenue"], reverse=True
    )

    # Повернення / скасовані
    cancelled = returns_data.get("cancelled", [])
    refunds = returns_data.get("refunds", [])
    cancelled_count = len(cancelled)
    cancelled_amount = sum(c.get("amount_total", 0) for c in cancelled)
    refunds_count = len(refunds)
    refunds_amount = sum(r.get("amount_total", 0) for r in refunds)
    # % скасувань = скасовані / (підтверджені + скасовані)
    total_attempts = n_orders + cancelled_count
    cancel_rate = (cancelled_count / total_attempts * 100) if total_attempts else 0
    # % повернень = сума кредит-нот / виручка (якщо посилається на цей же період)
    refund_rate = (refunds_amount / revenue * 100) if revenue else 0

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period_days": DAYS_BACK,
        "kpi": {
            "revenue": round(revenue, 2),
            "untaxed_revenue": round(untaxed, 2),
            "orders": n_orders,
            "aov": round(aov, 2),
            "cogs": round(cogs, 2) if cogs is not None else None,
            "margin_total": round(margin_total, 2) if margin_total is not None else None,
            "margin_pct": round(margin_pct, 2) if margin_pct is not None else None,
            "cancelled_orders": cancelled_count,
            "cancelled_amount": round(cancelled_amount, 2),
            "cancel_rate_pct": round(cancel_rate, 2),
            "refunds_count": refunds_count,
            "refunds_amount": round(refunds_amount, 2),
            "refund_rate_pct": round(refund_rate, 2),
        },
        "daily": daily,
        "daily_by_source": daily_by_source,
        "sources": sources,
        "mediums": mediums,
        "top_products": top_products,
        "categories": categories,
        # Нові секції
        "prom_sources": prom_sources,
        "prom_campaigns": prom_campaigns,
        "prom_orders_count": prom_orders_count,
        "acquisition_sources": acquisition_sources,
        "acquisition_no_data_count": no_acq_count,
        "cohort": cohort,
        # ABC + dead stock
        "abc_summary": abc_summary,
        "abc_products": abc_products[:200],  # топ-200 для дашборду
        "dead_stock": dead_stock_top,
        "dead_stock_total_count": len(dead_stock),
        "dead_stock_total_value": round(dead_value, 2),
        # ABC по складу Шевченко (для warehouse-дашборду)
        "warehouse_abc": warehouse_abc,
        # YoY-порівняння - той же період, але рік тому
        "yoy_kpi": _compute_yoy_kpi(yoy_orders),
    }


def _compute_yoy_kpi(yoy_orders):
    if not yoy_orders:
        return None
    rev = sum(o.get("amount_total", 0) for o in yoy_orders)
    n = len(yoy_orders)
    by_day = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    for o in yoy_orders:
        d = (o.get("date_order") or "")[:10]
        if not d:
            continue
        by_day[d]["orders"] += 1
        by_day[d]["revenue"] += o.get("amount_total", 0)
    return {
        "revenue": round(rev, 2),
        "orders": n,
        "aov": round(rev / n, 2) if n else 0,
        "daily": [{"date": d, **v} for d, v in sorted(by_day.items())],
    }


def save_json(data, name):
    path = os.path.join(OUTPUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    size_kb = os.path.getsize(path) / 1024
    log(f"      saved: {name} ({size_kb:.1f} KB)")


def main():
    # Очистити лог
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    log("=" * 60)
    log(" Odoo export - старт")
    log("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    url, db, user, apikey = read_config()
    log(f"      url={url}  db={db}  user={user}")
    uid, models = odoo_connect(url, db, user, apikey)

    since_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")

    orders, lines = export_sales(models, db, uid, apikey, since_date)

    product_ids = {l["product_id"][0] for l in lines if l.get("product_id")}
    products = export_products(models, db, uid, apikey, product_ids)

    partner_ids = {o["partner_id"][0] for o in orders if o.get("partner_id")}
    partners = export_partners(models, db, uid, apikey, partner_ids)

    log(f"[5/7] Шукаю склад Шевченко...")
    shev_id, shev_name, lot_id, lot_name = find_warehouse(
        models, db, uid, apikey, "шевч")
    # YoY: те ж саме вікно днів, але рік тому
    log(f"[6/8] YoY-вибірка: ті ж {DAYS_BACK} днів, але 365 днів тому...")
    yoy_until = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    yoy_since = (datetime.now() - timedelta(days=365 + DAYS_BACK)).strftime("%Y-%m-%d")
    try:
        yoy_orders = models.execute_kw(db, uid, apikey, "sale.order",
            "search_read",
            [[["date_order", ">=", yoy_since],
              ["date_order", "<", yoy_until],
              ["state", "in", ["sale", "done"]]]],
            {"fields": ["id", "date_order", "amount_total"]})
        log(f"      знайдено: {len(yoy_orders)} замовлень торік")
    except Exception as e:
        log(f"[WARN] YoY-запит впав: {e}")
        yoy_orders = []

    stuck_pickings = []
    if shev_id and lot_id:
        log(f"      знайдено склад: '{shev_name}' (id={shev_id})")
        log(f"      локація запасів: '{lot_name}' (id={lot_id})")
        warehouse_qty = export_location_stock(
            models, db, uid, apikey, lot_id)
        stuck_pickings = export_stuck_pickings(
            models, db, uid, apikey, shev_id, days_threshold=7)
    else:
        log(f"      НЕ знайдено - ABC по складу буде пропущено")
        warehouse_qty = {}
        shev_name = None

    stock = export_stock(models, db, uid, apikey)
    returns = export_returns(models, db, uid, apikey, since_date)

    log("[SAVE] Зберігаємо...")
    save_json(orders, "orders.json")
    save_json(lines, "order_lines.json")
    save_json(products, "products.json")
    save_json(partners, "partners.json")
    save_json(stock, "stock.json")
    save_json(returns, "returns.json")

    summary = build_summary(orders, lines, products, returns, partners,
                            warehouse_qty=warehouse_qty,
                            warehouse_name=shev_name,
                            stuck_pickings=stuck_pickings,
                            yoy_orders=yoy_orders)
    save_json(summary, "summary.json")

    log("=" * 60)
    log(" Готово")
    log("=" * 60)
    k = summary["kpi"]
    log(f" Виручка за {DAYS_BACK} днів:  {k['revenue']:,.2f}")
    log(f" Замовлень:                    {k['orders']:,}")
    log(f" Середній чек:                 {k['aov']:,.2f}")
    if k["margin_pct"] is not None:
        log(f" Маржа:                        {k['margin_pct']:.2f}%")
    log(f" Скасовано замовлень:          {k['cancelled_orders']:,} ({k['cancel_rate_pct']:.2f}%)")
    log(f" Кредит-нот (повернень):       {k['refunds_count']:,} на {k['refunds_amount']:,.2f} ₴ ({k['refund_rate_pct']:.2f}%)")
    log(f" Заказів з Prom UTM:           {summary.get('prom_orders_count', 0):,}")
    log(f" Заказів без acquisition:      {summary.get('acquisition_no_data_count', 0):,}")
    cohort = summary.get('cohort', {})
    if cohort:
        log(f" Нові клієнти:                 {cohort['new']['customers']:,} ({cohort['new']['orders']:,} заказів)")
        log(f" Повторні клієнти:             {cohort['repeat']['customers']:,} ({cohort['repeat']['orders']:,} заказів)")
        log(f" % виручки від повторних:      {cohort['repeat_revenue_pct']:.2f}%")
    abc = summary.get('abc_summary', {})
    if abc:
        log(f" ABC-аналіз:")
        log(f"   A: {abc['A']['sku_count']:,} SKU ({abc['A']['sku_pct']}%) → {abc['A']['rev_pct']}% виручки")
        log(f"   B: {abc['B']['sku_count']:,} SKU ({abc['B']['sku_pct']}%) → {abc['B']['rev_pct']}% виручки")
        log(f"   C: {abc['C']['sku_count']:,} SKU ({abc['C']['sku_pct']}%) → {abc['C']['rev_pct']}% виручки")
    if summary.get('dead_stock_total_count'):
        log(f" Dead stock:                   {summary['dead_stock_total_count']:,} SKU на {summary['dead_stock_total_value']:,.0f} ₴")
    wh = summary.get('warehouse_abc')
    if wh:
        log(f" Склад {wh['warehouse_name']} (товари в наявності):")
        log(f"   SKU в наявності:            {wh['total_sku']:,}")
        log(f"   Вартість залишку:           {wh['total_stock_value']:,.0f} ₴")
        log(f"   A: {wh['A']['sku_count']:,} SKU → {wh['A']['rev_pct']}% виручки, залишок {wh['A']['stock_value']:,.0f} ₴")
        log(f"   B: {wh['B']['sku_count']:,} SKU → {wh['B']['rev_pct']}% виручки, залишок {wh['B']['stock_value']:,.0f} ₴")
        log(f"   C: {wh['C']['sku_count']:,} SKU → {wh['C']['rev_pct']}% виручки, залишок {wh['C']['stock_value']:,.0f} ₴")
        a = wh.get('alerts', {})
        log(f"   ⚠ A-class з низьким запасом (<14 днів): {len(a.get('a_low_stock', []))} SKU")
        log(f"   ⚠ C-class затоварені (>90 днів): {len(a.get('c_overstock', []))} SKU")
        log(f"   ⚠ Без жодного продажу:       {a.get('no_sales_count', 0)} SKU на {a.get('no_sales_value', 0):,.0f} ₴")
        sp_count = len(a.get('stuck_pickings', []))
        if sp_count:
            log(f"   🚨 Підвислі pickings (>7 днів): {sp_count} (IN: {a.get('stuck_in_count', 0)}, OUT: {a.get('stuck_out_count', 0)})")
    log(f" Дані: {OUTPUT_DIR}")
    log(f" Лог:  {LOG_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"\n[FATAL] {type(e).__name__}: {e}")
        log("\n--- TR