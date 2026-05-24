# -*- coding: utf-8 -*-
"""
push_to_merchant_api.py — Завантаження товарів в Google Merchant Center через API
BusAuto → Merchant Center 163800443

Схема роботи:
  oDoo (120к товарів) → цей скрипт → Google Merchant Center API → Google Shopping

Переваги перед XML/FTP:
  - Не потрібен FTP і хостинг для файлу
  - Оновлення в реальному часі (не чекати поки Google "забере" файл)
  - Легко перевірити статус кожного товару

Перед запуском (один раз):
  1. console.cloud.google.com → Новий проект → увімкнути "Content API for Shopping"
  2. IAM → Сервісні акаунти → Створити → завантажити JSON ключ
  3. Зберегти JSON ключ як: google_merchant_key.json (в цій же папці)
  4. Merchant Center → Налаштування → Доступ до акаунту
     → Додати користувача → вставити email сервісного акаунту → Стандартний доступ

Детальна інструкція: ІНСТРУКЦІЯ_MERCHANT_API.txt (в цій же папці)

Запуск:
  python push_to_merchant_api.py
  або двічі клікнути: ЗАПУСТИТИ_MERCHANT_API.bat
"""

import configparser
import xmlrpc.client as xc
import json
import os
import sys
import time
import re
import argparse
from datetime import datetime, timedelta

# Google auth — встановлюється один раз: pip install google-auth requests
try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleRequest
    import requests as req_lib
except ImportError:
    print("=" * 60)
    print("ПОМИЛКА: Потрібно встановити бібліотеки.")
    print("Відкрийте командний рядок і виконайте:")
    print("  pip install google-auth requests")
    print("=" * 60)
    sys.exit(1)

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Налаштування ─────────────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE   = os.path.join(SCRIPT_DIR, "odoo_config.txt")
CREDS_FILE    = os.path.join(SCRIPT_DIR, "google_merchant_key.json")
LOG_FILE      = os.path.join(SCRIPT_DIR, "merchant_api.log")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "merchant_api_progress.json")

MERCHANT_ID   = "163800443"
SHOP_URL      = "https://busauto.ua"
# Для batch endpoint merchant ID НЕ входить в URL — він в тілі кожного запиту
API_BASE      = "https://shoppingcontent.googleapis.com/content/v2.1"
API_SCOPES    = ["https://www.googleapis.com/auth/content"]

ODOO_CHUNK    = 500    # скільки товарів брати з Odoo за раз (було 200, збільшено для швидкості)
API_BATCH     = 1000   # скільки товарів відправляти в Google за раз (max 1000)
PAUSE_SEC     = 0.2    # мінімальна пауза між пакетами

# Google product category: 5765 = Vehicle Parts & Accessories
GOOGLE_CATEGORY = "5765"

# Якщо True — завантажуємо і відправляємо товари на ДВОХ мовах: uk + ru
# Кожен товар → два записи в Merchant Center (contentLanguage: "uk" і "ru")
DUAL_LANGUAGE = True


# ── Логування ────────────────────────────────────────────────────────

def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Прогрес (щоб при повторному запуску не дублювати) ────────────────

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"uploaded": {}, "last_run": None, "total_uploaded": 0}


def save_progress(prog):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


# ── Конфіг ───────────────────────────────────────────────────────────

def read_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    s = cfg["odoo"]
    return (s["url"].strip().rstrip("/"),
            s["database"].strip(),
            s["username"].strip(),
            s["api_key"].strip())


# ── Google Auth ───────────────────────────────────────────────────────

def get_google_token():
    """Отримуємо OAuth токен через сервісний акаунт"""
    if not os.path.exists(CREDS_FILE):
        log(f"ПОМИЛКА: Файл ключа не знайдено: {CREDS_FILE}")
        log("Дивіться інструкцію: ІНСТРУКЦІЯ_MERCHANT_API.txt")
        raise SystemExit(1)

    creds = service_account.Credentials.from_service_account_file(
        CREDS_FILE, scopes=API_SCOPES)
    creds.refresh(GoogleRequest())
    log("Google API авторизація успішна")
    return creds


def api_headers(creds):
    """HTTP заголовки з актуальним токеном"""
    creds.refresh(GoogleRequest())
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json; charset=utf-8",
    }


# ── Odoo ─────────────────────────────────────────────────────────────

def odoo_connect(url, db, user, apikey):
    uid = xc.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, user, apikey, {})
    if not uid:
        log("ПОМИЛКА авторизації Odoo!")
        raise SystemExit(1)
    log(f"Odoo авторизація успішна (uid={uid})")
    return uid, xc.ServerProxy(f"{url}/xmlrpc/2/object")


# ── Таблиця надбавок для прайс-листа "Розниця" (правило 881, global) ──
# Формат: (від_UAH, до_UAH, відсоток_надбавки)
MARKUP_TABLE = [
    (0,      250,    60.0),
    (250,    500,    50.0),
    (500,    1000,   40.0),
    (1000,   2000,   30.0),
    (2000,   3000,   25.0),
    (3000,   10000,  20.0),
    (10000,  20000,  15.0),
    (20000,  100000, 10.0),
]
LBS_MIN_MARGIN = 100.0   # мінімальний абсолютний прибуток в UAH

# Категорійні правила: categ_id → % надбавки на ціну постачальника
# categ "50 %" id=6 | categ "100 %" id=7 | categ "200 %" id=9
CATEG_MARKUP = {
    6: 50.0,
    7: 100.0,
    9: 200.0,
}
# Категорії на standard_price (не ціну постачальника)
# categ "35 %" id=13 → 35% на standard_price
CATEG_STD_PRICE_MARKUP = {
    13: 35.0,
}


def _apply_tiered_markup(price_uah, markup_table, min_margin=100.0):
    """Застосовує таблицю тирів до ціни, повертає роздрібну ціну."""
    if price_uah <= 0:
        return 0.0
    pct = markup_table[-1][2] if markup_table else 10.0
    for from_p, to_p, tier_pct in markup_table:
        if from_p <= price_uah < to_p:
            pct = tier_pct
            break
    retail = price_uah * (1 + pct / 100.0)
    retail = max(retail, price_uah + min_margin)
    return round(retail, 2)


def apply_markup(supplier_price_uah, categ_id=None, categ_ancestors=None,
                 brand_markup_table=None):
    """
    Розраховуємо роздрібну ціну по ціні постачальника.
    Пріоритет правил:
      1. Категорійне правило (categ 6/7/9) — фіксований % на ціну постачальника
      2. Бренд-правило (brand_markup_table) — тири на ціну постачальника
      3. Глобальна таблиця надбавок (правило 881)
    Категорія 13 (35% на standard_price) обробляється окремо в load_products().
    """
    if supplier_price_uah <= 0:
        return 0.0

    # Пріоритет 1: Категорійне правило (supplier-based)
    for cid in ([categ_id] if categ_id else []) + (categ_ancestors or []):
        if cid in CATEG_MARKUP:
            markup_pct = CATEG_MARKUP[cid]
            retail = supplier_price_uah * (1 + markup_pct / 100.0)
            retail = max(retail, supplier_price_uah + LBS_MIN_MARGIN)
            return round(retail, 2)

    # Пріоритет 2: Бренд-правило
    if brand_markup_table:
        return _apply_tiered_markup(supplier_price_uah, brand_markup_table, LBS_MIN_MARGIN)

    # Пріоритет 3: Глобальна таблиця (правило 881)
    return _apply_tiered_markup(supplier_price_uah, MARKUP_TABLE, LBS_MIN_MARGIN)


def load_brand_markup_rules(models, db, uid, apikey):
    """
    Завантажуємо brand-правила прайс-листа (applied_on='3_1_brand', is_suppler_price=True).
    Повертає список: [(brand_id_set, markup_table, min_margin), ...]
    """
    rules = []
    try:
        items = models.execute_kw(db, uid, apikey,
            'product.pricelist.item', 'search_read',
            [[['pricelist_id',     '=', 1],
              ['applied_on',       '=', '3_1_brand'],
              ['is_suppler_price', '=', True]]],
            {'fields': ['brand_ids', 'add_supp_markup_ids', 'lbs_min_margin']})
        for item in items:
            tier_ids = item.get('add_supp_markup_ids', [])
            if not tier_ids:
                continue
            tiers_raw = models.execute_kw(db, uid, apikey,
                'lbs.pricelist.supp.markup', 'read', [tier_ids], {})
            markup_table = sorted(
                [(t['from_price'], t['to_price'], t['percent']) for t in tiers_raw])
            brand_ids  = set(item.get('brand_ids', []))
            min_margin = float(item.get('lbs_min_margin') or 100.0)
            rules.append((brand_ids, markup_table, min_margin))
        log(f"  Brand markup rules: {len(rules)} правила, "
            f"{sum(len(r[0]) for r in rules)} брендів")
    except Exception as e:
        log(f"  ⚠ Помилка завантаження brand rules: {e}")
    return rules


def load_categ_ancestors(models, db, uid, apikey):
    """
    Завантажуємо всі категорії і будуємо карту:
    {categ_id: [categ_id, parent_id, grandparent_id, ...]}
    Щоб перевіряти чи продукт належить до категорії 6/7/9 або її підкатегорії.
    """
    try:
        cats = models.execute_kw(db, uid, apikey,
            "product.category", "search_read",
            [[]],
            {"fields": ["id", "parent_id"], "limit": 2000})
        # Будуємо словник parent: {id: parent_id}
        parent_map = {}
        for c in cats:
            pid = c["parent_id"][0] if c.get("parent_id") else None
            parent_map[c["id"]] = pid

        # Для кожної категорії будуємо повний ланцюг предків
        ancestors = {}
        for cid in parent_map:
            chain = []
            cur = cid
            while cur is not None:
                chain.append(cur)
                cur = parent_map.get(cur)
            ancestors[cid] = chain

        log(f"  Завантажено {len(ancestors)} категорій товарів")
        return ancestors
    except Exception as e:
        log(f"  ⚠ Помилка завантаження категорій: {e}")
        return {}


def load_currency_rates(models, db, uid, apikey):
    """Завантажуємо курси валют з Odoo (кількість UAH за 1 одиницю валюти)"""
    rates = {"UAH": 1.0}
    try:
        currencies = models.execute_kw(db, uid, apikey,
            "res.currency", "search_read",
            [[["active", "=", True]]],
            {"fields": ["name", "rate"]})
        for c in currencies:
            if c.get("name") and c.get("rate"):
                rates[c["name"]] = float(c["rate"])
        log(f"  Курси валют: EUR={rates.get('EUR',0):.2f}  USD={rates.get('USD',0):.2f}  UAH=1.0")
    except Exception as e:
        log(f"  ⚠ Помилка читання курсів, використовуємо запасні: {e}")
        rates.update({"EUR": 52.0, "USD": 44.0})
    return rates


def load_warehouse_location_map(models, db, uid, apikey):
    """
    Завантажуємо stock.warehouse і будуємо карту:
      {location_id: partner_id}
    де location_id = lot_stock_id складу постачальника,
        partner_id = wh_owner (res.partner) — юридична особа постачальника.
    Повертає також список location_ids для фільтрації quants.
    """
    try:
        whs = models.execute_kw(db, uid, apikey,
            "stock.warehouse", "search_read",
            [[["wh_owner", "!=", False]]],
            {"fields": ["code", "lot_stock_id", "wh_owner"], "limit": 100})
        loc_to_partner = {}
        for wh in whs:
            if wh.get("lot_stock_id") and wh.get("wh_owner"):
                loc_id     = wh["lot_stock_id"][0]
                partner_id = wh["wh_owner"][0]
                loc_to_partner[loc_id] = partner_id
        log(f"  Складів постачальників: {len(whs)}, локацій: {len(loc_to_partner)}")
        return loc_to_partner
    except Exception as e:
        log(f"  ⚠ Помилка завантаження warehouse map: {e}")
        return {}


def load_variant_stock_partners(models, db, uid, apikey, variant_ids, loc_to_partner):
    """
    Завантажуємо stock.quant тільки для наших variant_ids і складських локацій.
    Повертає {variant_id: set(partner_ids_з_наявністю)}
    """
    if not variant_ids or not loc_to_partner:
        return {}

    loc_ids = list(loc_to_partner.keys())
    variant_partners = {}   # {variant_id: set(partner_ids)}
    total_quants = 0
    chunk_size = 1000       # ids per request

    for i in range(0, len(variant_ids), chunk_size):
        chunk = variant_ids[i:i + chunk_size]
        try:
            quants = models.execute_kw(db, uid, apikey,
                "stock.quant", "search_read",
                [[["product_id",   "in", chunk],
                  ["location_id",  "in", loc_ids],
                  ["quantity",     ">",  0]]],
                {"fields": ["product_id", "location_id"], "limit": 10000})
            total_quants += len(quants)
            for q in quants:
                vid = q["product_id"][0]
                loc_id = q["location_id"][0]
                partner_id = loc_to_partner.get(loc_id)
                if partner_id:
                    if vid not in variant_partners:
                        variant_partners[vid] = set()
                    variant_partners[vid].add(partner_id)
        except Exception as e:
            log(f"  ⚠ Помилка читання stock.quant пакет {i//chunk_size+1}: {e}")

    log(f"  Quant записів: {total_quants} → {len(variant_partners)} варіантів мають stock")
    return variant_partners


def load_supplier_prices_all(models, db, uid, apikey, tmpl_ids, currency_rates):
    """
    Завантажуємо ВСІ ціни постачальників для списку template IDs.
    Повертає {tmpl_id: {partner_id: price_uah}}
    де price_uah — найнижча ціна даного постачальника по цьому шаблону.
    """
    if not tmpl_ids:
        return {}

    price_map  = {}   # {tmpl_id: {partner_id: min_price_uah}}
    chunk_size = 2000
    total_loaded = 0

    for i in range(0, len(tmpl_ids), chunk_size):
        chunk_ids = tmpl_ids[i:i + chunk_size]
        try:
            records = models.execute_kw(db, uid, apikey,
                "product.supplierinfo", "search_read",
                [[["product_tmpl_id", "in", chunk_ids],
                  ["price",           ">",  0]]],
                {"fields": ["product_tmpl_id", "partner_id", "price", "currency_id"],
                 "limit": 50000})
            total_loaded += len(records)
            for rec in records:
                tmpl_id = rec.get("product_tmpl_id")
                if not tmpl_id:
                    continue
                tmpl_id    = tmpl_id[0]    if isinstance(tmpl_id,    (list, tuple)) else tmpl_id
                partner_id = rec["partner_id"][0] if rec.get("partner_id") else None
                if not partner_id:
                    continue
                cur_name = "UAH"
                if rec.get("currency_id"):
                    cur_name = rec["currency_id"][1] if isinstance(rec["currency_id"], (list, tuple)) else "UAH"
                price     = float(rec.get("price") or 0)
                rate      = currency_rates.get(cur_name, 1.0)
                price_uah = price * rate
                if price_uah > 0:
                    if tmpl_id not in price_map:
                        price_map[tmpl_id] = {}
                    # Зберігаємо мінімальну ціну для кожного постачальника
                    existing = price_map[tmpl_id].get(partner_id)
                    if existing is None or price_uah < existing:
                        price_map[tmpl_id][partner_id] = price_uah
        except Exception as e:
            log(f"  ⚠ Помилка читання supplierinfo пакет {i//chunk_size+1}: {e}")

    log(f"  Завантажено {total_loaded} записів supplierinfo → {len(price_map)} шаблонів")
    return price_map


def load_products(models, db, uid, apikey, mode="full", days_back=2):
    """
    mode='full'  — всі товари (перший запуск, ~2.5 год)
    mode='daily' — тільки змінені за останні days_back днів (~5-15 хв)
    """
    domain = [
        ["active",        "=", True],
        ["sale_ok",       "=", True],
        ["is_published",  "=", True],
        ["qty_available", ">", 0],
        ["image_1920",    "!=", False],
        ["default_code",  "!=", False],
        ["default_code",  "!=", ""],
    ]

    if mode == "daily":
        since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")
        domain.append(["write_date", ">=", since])
        log(f"Режим DAILY — тільки змінені після {since} UTC")
    else:
        log("Режим FULL — всі товари (перший/повний синк)")

    fields = [
        "id", "name", "default_code", "list_price", "standard_price",
        "product_variant_ids",
        "product_brand_id", "categ_id",
        "description_sale", "website_url",
        "qty_available",
    ]
    products  = []
    offset    = 0
    page      = 1
    chunk_size = ODOO_CHUNK   # починаємо з великого, при помилці зменшуємо
    log(f"Завантажуємо товари з Odoo (пакет: {chunk_size})...")

    while True:
        try:
            chunk = models.execute_kw(db, uid, apikey,
                "product.template", "search_read", [domain],
                {"fields": fields, "limit": chunk_size, "offset": offset,
                 "context": {"lang": "uk_UA"}})
        except Exception as e:
            if chunk_size > 100:
                chunk_size = chunk_size // 2
                log(f"  ⚠ Помилка читання, зменшуємо пакет до {chunk_size}: {e}")
                time.sleep(2)
                continue
            else:
                log(f"  ❌ Критична помилка Odoo: {e}")
                break

        if not chunk:
            break
        products.extend(chunk)
        log(f"  Пакет {page}: {len(chunk)} товарів (всього: {len(products)})")
        if len(chunk) < chunk_size:
            break
        offset += chunk_size
        page   += 1
        time.sleep(0.05)   # мінімальна пауза

    log(f"Завантажено з Odoo: {len(products)} товарів")

    if not products:
        return products

    tmpl_ids = [p["id"] for p in products]

    # ── Крок 0б: завантажуємо російські назви (якщо DUAL_LANGUAGE) ──────
    if DUAL_LANGUAGE:
        log("Завантажуємо назви товарів на ru_RU...")
        ru_names = {}
        ru_offset = 0
        ru_chunk = ODOO_CHUNK
        while True:
            try:
                chunk_ru = models.execute_kw(db, uid, apikey,
                    "product.template", "search_read", [domain],
                    {"fields": ["id", "name", "description_sale"],
                     "limit": ru_chunk, "offset": ru_offset,
                     "context": {"lang": "ru_RU"}})
            except Exception as e:
                log(f"  ⚠ Помилка завантаження ru_RU назв: {e}")
                break
            if not chunk_ru:
                break
            for item in chunk_ru:
                ru_names[item["id"]] = {
                    "name": item.get("name") or "",
                    "desc": item.get("description_sale") or "",
                }
            if len(chunk_ru) < ru_chunk:
                break
            ru_offset += ru_chunk
        log(f"  Завантажено ru_RU назв: {len(ru_names)}")
        # Додаємо russian-дані в кожен продукт
        for p in products:
            ru = ru_names.get(p["id"], {})
            p["name_ru"] = ru.get("name") or p.get("name") or ""
            p["desc_ru"]  = ru.get("desc") or p.get("description_sale") or ""

    # Збираємо всі variant IDs для запиту stock.quant
    all_variant_ids = []
    for p in products:
        all_variant_ids.extend(p.get("product_variant_ids") or [])

    # ── Крок 1: курси валют ──────────────────────────────────────────
    log("Завантажуємо курси валют...")
    currency_rates = load_currency_rates(models, db, uid, apikey)

    # ── Крок 2: склади постачальників → карта location → partner ────
    log("Завантажуємо карту складів постачальників...")
    loc_to_partner = load_warehouse_location_map(models, db, uid, apikey)

    # ── Крок 3: наявність по складах (variant → set of partners) ────
    log("Завантажуємо наявність товарів по складах постачальників...")
    variant_stock_partners = load_variant_stock_partners(
        models, db, uid, apikey, all_variant_ids, loc_to_partner)

    # ── Крок 4: всі ціни постачальників (tmpl → {partner: price}) ───
    log("Завантажуємо ціни постачальників...")
    supp_price_map = load_supplier_prices_all(models, db, uid, apikey, tmpl_ids, currency_rates)

    # ── Крок 5: ієрархія категорій (для categ правил) ────────────────
    log("Завантажуємо ієрархію категорій...")
    categ_ancestors_map = load_categ_ancestors(models, db, uid, apikey)

    # ── Крок 5б: brand markup rules ──────────────────────────────────
    log("Завантажуємо brand markup rules...")
    brand_markup_rules = load_brand_markup_rules(models, db, uid, apikey)

    # ── Крок 6: фіксовані ціни прайс-листа (пріоритет 1) ────────────
    RETAIL_PRICELIST_ID = 1
    log("Завантажуємо фіксовані ціни прайс-листа 'Розниця'...")
    variant_price_map = {}
    try:
        pl_offset = 0
        while True:
            items = models.execute_kw(db, uid, apikey,
                "product.pricelist.item", "search_read",
                [[["pricelist_id", "=", RETAIL_PRICELIST_ID],
                  ["applied_on",   "=", "0_product_variant"],
                  ["compute_price","=", "fixed"]]],
                {"fields": ["product_id", "fixed_price"],
                 "limit": 5000, "offset": pl_offset})
            if not items:
                break
            for item in items:
                if item.get("product_id") and item.get("fixed_price", 0) > 1:
                    variant_price_map[item["product_id"][0]] = item["fixed_price"]
            if len(items) < 5000:
                break
            pl_offset += 5000
        log(f"  Фіксованих цін: {len(variant_price_map)}")
    except Exception as e:
        log(f"  ⚠ Помилка читання фіксованих цін: {e}")

    # ── Крок 7: обчислюємо retail_price для кожного шаблону ─────────
    matched_fixed      = 0
    matched_stock_avg  = 0   # є постачальники з наявністю
    matched_all_avg    = 0   # fallback: немає stock-даних, середня від усіх
    no_price           = 0

    for p in products:
        tmpl_id  = p["id"]
        variants = p.get("product_variant_ids") or []
        retail   = 0.0

        # Пріоритет 1: фіксована ціна варіанта в прайс-листі
        for vid in variants:
            if vid in variant_price_map:
                retail = variant_price_map[vid]
                matched_fixed += 1
                break

        # Визначаємо бренд і categ один раз (потрібно для кількох пріоритетів)
        categ_raw = p.get("categ_id")
        categ_id  = categ_raw[0] if isinstance(categ_raw, (list, tuple)) else categ_raw
        ancestors = categ_ancestors_map.get(categ_id, [categ_id] if categ_id else [])

        brand_raw = p.get("product_brand_id")
        brand_id  = brand_raw[0] if isinstance(brand_raw, (list, tuple)) else brand_raw

        # Знаходимо brand markup table (якщо є)
        brand_markup_table = None
        for (bids, btable, bmin) in brand_markup_rules:
            if brand_id and brand_id in bids:
                brand_markup_table = btable
                break

        # Перевіряємо categ 13 (35% на standard_price, без supplier)
        uses_std_price_categ = any(
            cid in CATEG_STD_PRICE_MARKUP
            for cid in ([categ_id] if categ_id else []) + ancestors
        )

        # Пріоритет 2: середня роздрібна по постачальниках З НАЯВНІСТЮ
        if retail <= 1 and not uses_std_price_categ:
            all_prices = supp_price_map.get(tmpl_id, {})  # {partner_id: price_uah}

            # Визначаємо яких постачальників цього шаблону є на складі
            tmpl_stock_partners = set()
            for vid in variants:
                tmpl_stock_partners.update(variant_stock_partners.get(vid, set()))

            # Беремо тільки постачальників з наявністю
            prices_to_use = {pid: price for pid, price in all_prices.items()
                             if pid in tmpl_stock_partners}

            # Fallback: якщо дані про наявність недоступні — беремо всіх
            if not prices_to_use and all_prices:
                prices_to_use = all_prices
                matched_all_avg += 1
            elif prices_to_use:
                matched_stock_avg += 1

            if prices_to_use:
                # Роздрібна ціна для кожного постачальника → середня
                retail_prices = [
                    apply_markup(price_uah, categ_id=categ_id,
                                 categ_ancestors=ancestors,
                                 brand_markup_table=brand_markup_table)
                    for price_uah in prices_to_use.values()
                ]
                retail = round(sum(retail_prices) / len(retail_prices), 2)

        # Пріоритет 3: categ 13 або fallback — standard_price
        if retail <= 1:
            std_price = float(p.get("standard_price") or 0.0)
            if std_price > 0:
                # Categ 13: фіксований %
                std_markup = None
                for cid in ([categ_id] if categ_id else []) + ancestors:
                    if cid in CATEG_STD_PRICE_MARKUP:
                        std_markup = CATEG_STD_PRICE_MARKUP[cid]
                        break
                if std_markup is None:
                    # Fallback: brand tiers або global на standard_price
                    if brand_markup_table:
                        retail = _apply_tiered_markup(std_price, brand_markup_table, LBS_MIN_MARGIN)
                    else:
                        retail = _apply_tiered_markup(std_price, MARKUP_TABLE, LBS_MIN_MARGIN)
                else:
                    retail = round(max(std_price * (1 + std_markup / 100.0),
                                       std_price + LBS_MIN_MARGIN), 2)
            if retail <= 1:
                no_price += 1

        # Пріоритет 4: list_price (запасний для товарів без будь-яких цін)
        if retail <= 1:
            lp = p.get("list_price") or 0.0
            if lp > 1:
                retail = lp

        p["retail_price"] = retail

    log(f"  Цін: фіксовані={matched_fixed} | avg(з_наявністю)={matched_stock_avg}"
        f" | avg(fallback)={matched_all_avg} | без_ціни={no_price}")

    # Приклади для перевірки
    examples = [p for p in products if p.get("retail_price", 0) > 1][:5]
    for ex in examples:
        tmpl_id  = ex["id"]
        variants = ex.get("product_variant_ids") or []
        all_prices = supp_price_map.get(tmpl_id, {})
        tmpl_sp = set()
        for vid in variants:
            tmpl_sp.update(variant_stock_partners.get(vid, set()))
        used = {pid: pr for pid, pr in all_prices.items() if pid in tmpl_sp} or all_prices
        categ_raw = ex.get("categ_id")
        categ_id  = categ_raw[0] if isinstance(categ_raw, (list, tuple)) else categ_raw
        ancestors = categ_ancestors_map.get(categ_id, [])
        brand_raw = ex.get("product_brand_id")
        brand_id  = brand_raw[0] if isinstance(brand_raw, (list, tuple)) else brand_raw
        brand_name = brand_raw[1] if isinstance(brand_raw, (list, tuple)) and len(brand_raw) > 1 else ""

        used_categ = next((c for c in ([categ_id] + ancestors) if c in CATEG_MARKUP), None)
        used_std_categ = next((c for c in ([categ_id] + ancestors) if c in CATEG_STD_PRICE_MARKUP), None)
        has_brand_rule = any(brand_id and brand_id in r[0] for r in brand_markup_rules)

        if used_categ:
            rule_info = f"categ={used_categ}({CATEG_MARKUP[used_categ]}%)"
        elif used_std_categ:
            rule_info = f"categ_std={used_std_categ}({CATEG_STD_PRICE_MARKUP[used_std_categ]}%/std_price)"
        elif has_brand_rule:
            rule_info = f"brand={brand_name}"
        else:
            rule_info = "global"
        stock_flag = "stock" if tmpl_sp else "no_stock_fallback"
        log(f"  ✓ {ex.get('default_code')} → retail={ex.get('retail_price')}"
            f" n_supps={len(used)} [{rule_info}] [{stock_flag}]")
    no_ex = [p for p in products if p.get("retail_price", 0) <= 1][:3]
    for ex in no_ex:
        log(f"  ✗ {ex.get('default_code')} → БЕЗ ЦІНИ (list_price={ex.get('list_price')})")

    return products


# ── Підготовка товару для Google API ─────────────────────────────────

def clean_html(text, max_len=None):
    if not text:
        return ""
    t = re.sub(r'<[^>]+>', ' ', str(text))
    t = t.replace('&nbsp;', ' ').replace('&amp;', '&') \
         .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    t = re.sub(r'\s+', ' ', t).strip()
    if max_len and len(t) > max_len:
        t = t[:max_len - 3] + "..."
    return t


def get_brand(product):
    b = product.get("product_brand_id")
    if isinstance(b, (list, tuple)) and len(b) > 1:
        return str(b[1]).strip()
    return ""


def build_product_payload(p, lang="uk"):
    """
    Формуємо об'єкт товару у форматі Google Content API
    Документація: https://developers.google.com/shopping-content/reference/rest/v2.1/products

    lang: "uk" або "ru" — мова контенту для contentLanguage
      - "uk": використовує p["name"] та p["description_sale"]
      - "ru": використовує p["name_ru"] та p["desc_ru"] (завантажені з ru_RU context)
    """
    pid   = p.get("id")
    code  = (p.get("default_code") or "").strip()
    # retail_price — ціна з прайс-листа "Розниця", list_price — запасний варіант
    price = p.get("retail_price") or p.get("list_price") or 0.0
    qty   = p.get("qty_available") or 0
    brand = get_brand(p) or "BusAuto"

    # Обираємо назву і опис залежно від мови
    if lang == "ru":
        raw_name = p.get("name_ru") or p.get("name") or ""
        raw_desc = p.get("desc_ru") or p.get("description_sale") or ""
        fallback_desc = (f"Автозапчасть {brand} {clean_html(raw_name)}. "
                         "В наличии. Доставка по всей Украине 1-2 дня.")
    else:
        raw_name = p.get("name") or ""
        raw_desc = p.get("description_sale") or ""
        fallback_desc = (f"Автозапчастина {brand} {clean_html(raw_name)}. "
                         "В наявності. Доставка по всій Україні 1-2 дні.")

    name  = clean_html(raw_name)
    if brand and brand.lower() not in name.lower():
        title = f"{brand} {name}"
    else:
        title = name
    title = clean_html(title, 150)

    desc = clean_html(raw_desc, 5000)
    if len(desc) < 20:
        desc = fallback_desc

    # URL сторінки товару
    wu = (p.get("website_url") or "").strip()
    if wu and wu not in ("/", ""):
        link = SHOP_URL + wu
    else:
        link = f"{SHOP_URL}/shop?search={code.replace(' ', '+')}"

    # URL зображення через oDoo endpoint
    image_url = f"{SHOP_URL}/web/image/product.template/{pid}/image_1920"

    product_payload = {
        "offerId":         code,       # унікальний ID товару (однаковий для uk і ru)
        "title":           title,
        "description":     desc,
        "link":            link,
        "imageLink":       image_url,
        "contentLanguage": lang,       # "uk" або "ru"
        "targetCountry":   "UA",       # Україна
        "channel":         "online",
        "availability":    "in_stock" if qty > 0 else "out_of_stock",
        "condition":       "new",
        "brand":           brand,
        "mpn":             code,       # для запчастин артикул = MPN
        "price": {
            "value":    f"{price:.2f}",
            "currency": "UAH",
        },
        "googleProductCategory": GOOGLE_CATEGORY,
    }

    # Категорія товару з Odoo
    categ = p.get("categ_id")
    if isinstance(categ, (list, tuple)) and len(categ) > 1:
        product_payload["productTypes"] = [str(categ[1])]

    return product_payload


# ── Відправка в Google API ────────────────────────────────────────────

def send_batch(creds, batch_entries):
    """
    Відправляємо пакет товарів через customBatch endpoint
    Повертає (ok_count, err_count, errors_list)
    """
    payload = {"entries": batch_entries}
    url     = f"{API_BASE}/products/batch"

    resp = req_lib.post(
        url,
        headers=api_headers(creds),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=120,
    )

    if resp.status_code == 401:
        log("  Токен прострочений, оновлюємо...")
        creds.refresh(GoogleRequest())
        resp = req_lib.post(
            url,
            headers=api_headers(creds),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=120,
        )

    if resp.status_code not in (200, 207):
        log(f"  HTTP помилка: {resp.status_code} — {resp.text[:300]}")
        return 0, len(batch_entries), [resp.text[:200]]

    result   = resp.json()
    ok_count  = 0
    err_count = 0
    errors    = []

    for entry in result.get("entries", []):
        errs = entry.get("errors", {}).get("errors", [])
        if errs:
            err_count += 1
            # Логуємо тільки перші 3 помилки щоб не засмічувати лог
            if len(errors) < 3:
                offer = entry.get("product", {}).get("offerId", "?")
                errors.append(f"{offer}: {errs[0].get('message','?')}")
        else:
            ok_count += 1

    return ok_count, err_count, errors


# ── Головна функція ───────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="BusAuto → Google Merchant Center")
    parser.add_argument(
        "--mode",
        choices=["full", "daily"],
        default="daily",
        help="full = всі товари, daily = тільки змінені за N днів"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Кількість днів для режиму daily (default: 2)"
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Пропустити перші N товарів (для паралельних workers)"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Обробити максимум N товарів, 0 = всі (для паралельних workers)"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    log("=" * 60)
    log("  Google Merchant Center API — BusAuto (163800443)")
    log(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"  Режим: {args.mode.upper()}")
    log("=" * 60)

    # 1. Конфіг і Google авторизація
    try:
        url, db, user, apikey = read_config()
        log(f"Odoo: {url}  DB={db}")
    except Exception as e:
        log(f"ПОМИЛКА конфігу: {e}")
        raise SystemExit(1)

    creds = get_google_token()

    # 2. Odoo
    uid, models = odoo_connect(url, db, user, apikey)
    products    = load_products(models, db, uid, apikey, mode=args.mode, days_back=args.days)
    if not products:
        log("Товарів не знайдено!")
        raise SystemExit(1)

    # Slice для паралельних workers (--offset / --count)
    if args.offset > 0 or args.count > 0:
        start = args.offset
        end   = (args.offset + args.count) if args.count > 0 else len(products)
        products = products[start:end]
        log(f"Worker slice: [{start}:{end}] — {len(products)} товарів")

    # 3. Прогрес
    progress    = load_progress()
    already_done = set(progress.get("uploaded", {}).keys())
    log(f"Вже завантажено раніше: {len(already_done)} товарів")

    # 4. Відправка пакетами
    total_ok  = 0
    total_err = 0
    batch_num = 0
    batch     = []

    # Мови для відправки: завжди uk, + ru якщо DUAL_LANGUAGE
    langs_to_send = ["uk", "ru"] if DUAL_LANGUAGE else ["uk"]

    for p in products:
        code = (p.get("default_code") or "").strip()
        # Пропускаємо якщо немає артикулу або ціна <= 1 (заглушка в Одо)
        actual_price = p.get("retail_price") or p.get("list_price") or 0
        if not code or actual_price <= 1:
            continue

        for lang in langs_to_send:
            try:
                payload = build_product_payload(p, lang=lang)
            except Exception as e:
                log(f"  Помилка підготовки {code} [{lang}]: {e}")
                continue

            entry_id = len(batch) + 1
            batch.append({
                "batchId":    entry_id,
                "merchantId": MERCHANT_ID,
                "method":     "insert",
                "product":    payload,
            })

        if len(batch) >= API_BATCH:
            batch_num += 1
            log(f"Відправляємо пакет #{batch_num} ({len(batch)} товарів)...")
            ok, err, errs = send_batch(creds, batch)
            total_ok  += ok
            total_err += err
            log(f"  OK: {ok}  Помилок: {err}")
            for e_msg in errs:
                log(f"  ⚠ {e_msg}")

            # Зберігаємо прогрес (ключ: offerId:lang)
            for entry in batch:
                oid  = entry["product"]["offerId"]
                clng = entry["product"].get("contentLanguage", "uk")
                progress["uploaded"][f"{oid}:{clng}"] = datetime.now().strftime("%Y-%m-%d")
            progress["last_run"]      = datetime.now().strftime("%Y-%m-%d %H:%M")
            progress["total_uploaded"] = len(progress["uploaded"])
            save_progress(progress)

            batch = []
            time.sleep(PAUSE_SEC)

    # Відправляємо залишок
    if batch:
        batch_num += 1
        log(f"Відправляємо останній пакет #{batch_num} ({len(batch)} товарів)...")
        ok, err, errs = send_batch(creds, batch)
        total_ok  += ok
        total_err += err
        log(f"  OK: {ok}  Помилок: {err}")
        for e_msg in errs:
            log(f"  ⚠ {e_msg}")
        for entry in batch:
            oid  = entry["product"]["offerId"]
            clng = entry["product"].get("contentLanguage", "uk")
            progress["uploaded"][f"{oid}:{clng}"] = datetime.now().strftime("%Y-%m-%d")
        progress["last_run"]      = datetime.now().strftime("%Y-%m-%d %H:%M")
        progress["total_uploaded"] = len(progress["uploaded"])
        save_progress(progress)

    # 5. Підсумок
    log("")
    log("=" * 60)
    log(f"✅  ЗАВАНТАЖЕННЯ ЗАВЕРШЕНО!")
    log(f"   Успішно: {total_ok} записів ({', '.join(langs_to_send)} мови)")
    log(f"   Помилок: {total_err} записів")
    log(f"   Пакетів відправлено: {batch_num}")
    log(f"   Мови: {', '.join(langs_to_send)}")
    log("")
    log("   Merchant Center → Товари й магазин → Товари")
    log("   Товари з'являються протягом ~30 хвилин")
    log("=" * 60)


if __name__ == "__main__":
    main()
