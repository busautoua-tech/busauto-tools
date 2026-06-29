#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BusAuto: Odoo → Маркетплейс Епіцентр
======================================
Генерує XML-фід товарів і публікує як публічний файл на busauto.ua.
URL для Єпіцентру: https://busauto.ua/web/content/{id}/epicentr_feed.xml

Фільтр товарів: є фото + є в наявності (qty_available > 0)

Категорії: читаються з public_categ_ids (вкладка «Продажі» → «Публічні категорії»).
Епіцентр робить авто-маппінг категорій при імпорті — ручне заповнення кодів не потрібне.

РЕЖИМИ:
  python odoo_epicentr_export.py           → повний фід (всі товари)
  python odoo_epicentr_export.py --update  → тільки ціна+наявність (для щоденного авто-оновлення)
"""

import xmlrpc.client
import json
import os
import sys
import re
import base64
import logging
import configparser
from datetime import datetime
from pathlib import Path
from xml.dom.minidom import Document

# ── Мапи країн: Назва в Odoo (українська) → {code: ISO3, name: українська} ──
# Єпіцентр вимагає: <country_of_origin code="deu">Німеččина</country_of_origin>
EPICENTR_COUNTRY = {
    "Австрія":          {"code": "aut", "name": "Австрія"},
    "Австралія":        {"code": "aus", "name": "Австралія"},
    "Бельгія":          {"code": "bel", "name": "Бельгія"},
    "Болгарія":         {"code": "bgr", "name": "Болгарія"},
    "Бразилія":         {"code": "bra", "name": "Бразилія"},
    "Великобританія":   {"code": "gbr", "name": "Великобританія"},
    "Індія":            {"code": "ind", "name": "Індія"},
    "Іспанія":          {"code": "esp", "name": "Іспанія"},
    "Італія":           {"code": "ita", "name": "Італія"},
    "Канада":           {"code": "can", "name": "Канада"},
    "Китай":            {"code": "chn", "name": "Китай"},
    "Нідерланди":       {"code": "nld", "name": "Нідерланди"},
    "Німеччина":        {"code": "deu", "name": "Німеççина"},
    "Польща":           {"code": "pol", "name": "Польща"},
    "Португалія":       {"code": "prt", "name": "Португалія"},
    "Румунія":          {"code": "rou", "name": "Румунія"},
    "США":              {"code": "usa", "name": "США"},
    "Словаччина":       {"code": "svk", "name": "Словаччина"},
    "Словенія":         {"code": "svn", "name": "Словенія"},
    "Туреччина":        {"code": "tur", "name": "Туреччина"},
    "Угорщина":         {"code": "hun", "name": "Угорщина"},
    "Україна":          {"code": "ukr", "name": "Україна"},
    "Франція":          {"code": "fra", "name": "Франція"},
    "Чехія":            {"code": "cze", "name": "Чехія"},
    "Чеська Республіка":{"code": "cze", "name": "Чехія"},
    "Швейцарія":        {"code": "che", "name": "Швейцарія"},
    "Швеція":           {"code": "swe", "name": "Швеція"},
    "Японія":           {"code": "jpn", "name": "Японія"},
    "Республіка Корея": {"code": "kor", "name": "Республіка Корея"},
    "Корея":            {"code": "kor", "name": "Республіка Корея"},
    "Тайвань":          {"code": "twn", "name": "Тайвань"},
    "Мексика":          {"code": "mex", "name": "Мексика"},
}

# ─────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent

# ── Завантажуємо map брендів (створюється epicentr_brand_lookup_v3.py) ──
_brand_map_path = BASE_DIR / "epicentr_brand_map.json"
BRAND_MAP = {}
if _brand_map_path.exists() and _brand_map_path.stat().st_size > 0:
    with open(_brand_map_path, encoding="utf-8") as _f:
        BRAND_MAP = json.load(_f)
ODOO_CONFIG     = BASE_DIR / "odoo_config.txt"
EPICENTR_CFG    = BASE_DIR / "epicentr_config.json"
OUTPUT_XML      = BASE_DIR / ("epicentr_test_feed.xml" if "--test" in sys.argv else "epicentr_feed.xml")
ODOO_ATT_ID     = BASE_DIR / ("odoo_test_attachment_id.txt" if "--test" in sys.argv else "odoo_attachment_id.txt")
LOG_FILE        = BASE_DIR / "epicentr_export_log.txt"

# ── Єпіцентр: код категорії "Запчастини для авто" ──────────
# Щоб знайти код: відкрий в Єпіцентрі будь-який товар → категорія → F12 →
# Network → шукай запит до api.epicentrm.com.ua/api/v2/categories → поле "code"
EPICENTR_CATEGORY_CODE = "7160"   # Запчастини для авто
EPICENTR_CATEGORY_NAME = "Запчастини для авто"

# Режим --update: тільки ціна + наявність (менший фід, швидший)
UPDATE_ONLY    = "--update" in sys.argv
# Режим --test: перші 20 товарів, окремий файл, XMLRPC upload (без FTP)
TEST_MODE      = "--test" in sys.argv
TEST_LIMIT     = 20

# ═══════════════════════════════════════════════════════════
#  PRICING ENGINE (аналог push_to_merchant_api.py)
# ═══════════════════════════════════════════════════════════
# Глобальна таблиця надбавок (правило 881)
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
LBS_MIN_MARGIN = 100.0  # мінімальний абсолютний прибуток UAH

# Категорійні правила: categ_id → % надбавки на ціну постачальника
CATEG_MARKUP = {6: 50.0, 7: 100.0, 9: 200.0}
# Категорія 13 → 35% на standard_price
CATEG_STD_PRICE_MARKUP = {13: 35.0}


def _apply_tiered_markup(price_uah, markup_table, min_margin=100.0):
    if price_uah <= 0:
        return 0.0
    pct = markup_table[-1][2] if markup_table else 10.0
    for from_p, to_p, tier_pct in markup_table:
        if from_p <= price_uah < to_p:
            pct = tier_pct
            break
    return round(max(price_uah * (1 + pct / 100.0), price_uah + min_margin), 2)


def _compute_retail_price(supplier_price_uah, categ_id=None, categ_ancestors=None,
                           brand_markup_table=None):
    """Розраховуємо роздрібну ціну. Пріоритет: категорія → бренд → глобальна таблиця."""
    if supplier_price_uah <= 0:
        return 0.0
    for cid in ([categ_id] if categ_id else []) + (categ_ancestors or []):
        if cid in CATEG_MARKUP:
            retail = supplier_price_uah * (1 + CATEG_MARKUP[cid] / 100.0)
            return round(max(retail, supplier_price_uah + LBS_MIN_MARGIN), 2)
    if brand_markup_table:
        return _apply_tiered_markup(supplier_price_uah, brand_markup_table, LBS_MIN_MARGIN)
    return _apply_tiered_markup(supplier_price_uah, MARKUP_TABLE, LBS_MIN_MARGIN)


def load_currency_rates(models, db, uid, pw):
    rates = {"UAH": 1.0}
    try:
        currencies = models.execute_kw(db, uid, pw, "res.currency", "search_read",
            [[["active", "=", True]]], {"fields": ["name", "rate"]})
        for c in currencies:
            if c.get("name") and c.get("rate"):
                rates[c["name"]] = float(c["rate"])
        log.info(f"  Курси: USD={rates.get('USD', 0):.2f}  EUR={rates.get('EUR', 0):.2f}")
    except Exception as e:
        log.warning(f"  ⚠ Курси валют: {e} — використовуємо USD=44.45")
        rates.update({"USD": 44.45, "EUR": 47.0})
    return rates


def load_categ_ancestors(models, db, uid, pw):
    try:
        cats = models.execute_kw(db, uid, pw, "product.category", "search_read",
            [[]], {"fields": ["id", "parent_id"], "limit": 2000})
        parent_map = {c["id"]: (c["parent_id"][0] if c.get("parent_id") else None) for c in cats}
        ancestors = {}
        for cid in parent_map:
            chain, cur = [], cid
            while cur is not None:
                chain.append(cur)
                cur = parent_map.get(cur)
            ancestors[cid] = chain
        log.info(f"  Категорій: {len(ancestors)}")
        return ancestors
    except Exception as e:
        log.warning(f"  ⚠ Категорії: {e}")
        return {}


def load_brand_markup_rules(models, db, uid, pw):
    rules = []
    try:
        items = models.execute_kw(db, uid, pw, "product.pricelist.item", "search_read",
            [[["pricelist_id", "=", 1], ["applied_on", "=", "3_1_brand"],
              ["is_suppler_price", "=", True]]],
            {"fields": ["brand_ids", "add_supp_markup_ids", "lbs_min_margin"]})
        for item in items:
            tier_ids = item.get("add_supp_markup_ids", [])
            if not tier_ids:
                continue
            tiers_raw = models.execute_kw(db, uid, pw, "lbs.pricelist.supp.markup", "read",
                [tier_ids], {})
            markup_table = sorted([(t["from_price"], t["to_price"], t["percent"]) for t in tiers_raw])
            rules.append((set(item.get("brand_ids", [])), markup_table,
                          float(item.get("lbs_min_margin") or 100.0)))
        log.info(f"  Brand rules: {len(rules)}, брендів: {sum(len(r[0]) for r in rules)}")
    except Exception as e:
        log.warning(f"  ⚠ Brand markup rules: {e}")
    return rules


def load_supplier_prices(models, db, uid, pw, tmpl_ids, currency_rates):
    """Повертає {tmpl_id: min_price_uah} — найдешевший постачальник."""
    price_map = {}
    chunk_size = 2000
    for i in range(0, len(tmpl_ids), chunk_size):
        chunk = tmpl_ids[i:i + chunk_size]
        try:
            records = models.execute_kw(db, uid, pw, "product.supplierinfo", "search_read",
                [[["product_tmpl_id", "in", chunk], ["price", ">", 0]]],
                {"fields": ["product_tmpl_id", "price", "currency_id"], "limit": 50000})
            for rec in records:
                tmpl_id = rec.get("product_tmpl_id")
                if not tmpl_id:
                    continue
                tmpl_id = tmpl_id[0] if isinstance(tmpl_id, (list, tuple)) else tmpl_id
                cur_name = (rec["currency_id"][1] if isinstance(rec.get("currency_id"),
                            (list, tuple)) else "UAH")
                price_uah = float(rec.get("price") or 0) * currency_rates.get(cur_name, 1.0)
                if price_uah > 0:
                    if tmpl_id not in price_map or price_uah < price_map[tmpl_id]:
                        price_map[tmpl_id] = price_uah
        except Exception as e:
            log.warning(f"  ⚠ supplierinfo пакет {i // chunk_size + 1}: {e}")
    log.info(f"  Ціни постачальників: {len(price_map)} шаблонів")
    return price_map


def compute_product_price(p, supp_price_map, currency_rates, categ_ancestors, brand_rules):
    """
    Розраховує роздрібну ціну товару.
    Пріоритет: ціна постачальника → apply markup.
    Fallback: list_price (якщо нема постачальника).
    """
    tmpl_id = p["id"]
    categ_id = p.get("categ_id")
    if isinstance(categ_id, (list, tuple)):
        categ_id = categ_id[0]

    # Категорія 13 → 35% на standard_price
    ancestors = categ_ancestors.get(categ_id, [categ_id] if categ_id else [])
    for cid in ancestors:
        if cid in CATEG_STD_PRICE_MARKUP:
            std = float(p.get("standard_price") or 0)
            if std > 0:
                return round(std * (1 + CATEG_STD_PRICE_MARKUP[cid] / 100.0), 2)

    # Бренд
    brand_id = p.get("product_brand_id")
    if isinstance(brand_id, (list, tuple)):
        brand_id = brand_id[0]
    brand_markup = None
    for brand_ids_set, markup_table, min_margin in brand_rules:
        if brand_id and brand_id in brand_ids_set:
            brand_markup = markup_table
            break

    # Ціна постачальника
    supp_price = supp_price_map.get(tmpl_id)
    if supp_price and supp_price > 0:
        return _compute_retail_price(supp_price, categ_id, ancestors, brand_markup)

    # Fallback: list_price
    return round(float(p.get("list_price") or 0), 2)

# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  1. КОНФІГУРАЦІЯ
# ═══════════════════════════════════════════════════════════
def load_odoo_cfg():
    cfg = configparser.ConfigParser()
    cfg.read(ODOO_CONFIG, encoding="utf-8")
    return {
        "url":      cfg["odoo"]["url"].rstrip("/"),
        "db":       cfg["odoo"]["database"],
        "username": cfg["odoo"]["username"],
        "password": cfg["odoo"]["api_key"],
    }

def load_epicentr_cfg():
    if not EPICENTR_CFG.exists():
        # Дефолт якщо файл не існує
        return {"gdrive_filename": "epicentr_feed.xml", "gdrive_folder_id": "",
                "max_products": 5000, "batch_size": 50}
    with open(EPICENTR_CFG, encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════
#  2. ODOO: ПІДКЛЮЧЕННЯ
# ═══════════════════════════════════════════════════════════
def connect_odoo(cfg):
    log.info(f"Odoo: підключення до {cfg['url']}")
    common = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/common", allow_none=True)
    uid    = common.authenticate(cfg["db"], cfg["username"], cfg["password"], {})
    if not uid:
        raise RuntimeError(f"❌ Odoo auth failed. Перевірте odoo_config.txt")
    log.info(f"✅ Odoo: uid={uid}")
    models = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/object", allow_none=True)
    return models, cfg["db"], uid, cfg["password"]


# ═══════════════════════════════════════════════════════════
#  3. ODOO: ЗАВАНТАЖЕННЯ ПУБЛІЧНИХ КАТЕГОРІЙ
# ═══════════════════════════════════════════════════════════
def load_public_categories(models, db, uid, pw):
    """
    Повертає dict {id: {'name': ..., 'full_name': ...}}
    Читає product.public.category (публічні категорії сайту).
    """
    log.info("Завантаження публічних категорій...")
    cats = models.execute_kw(db, uid, pw,
        "product.public.category", "search_read", [[]],
        {"fields": ["id", "name", "parent_id"]}
    )
    result = {}
    for c in cats:
        result[c["id"]] = {
            "name":      c.get("name", "Автозапчастини"),
            "full_name": c.get("name", "Автозапчастини"),
        }
    log.info(f"  Категорій: {len(result)}")
    return result


# ═══════════════════════════════════════════════════════════
#  4. ODOO: ЗАВАНТАЖЕННЯ ТОВАРІВ
# ═══════════════════════════════════════════════════════════
def get_products(models, db, uid, pw, cfg):
    """
    Вибірка: sale_ok + active + складські/витратні + qty>0 + є фото.
    Мікро-пакети по batch_size — обхід MemoryError у lbs_busauto.
    """
    domain = [
        ("sale_ok",       "=",  True),
        ("active",        "=",  True),
        ("type",          "in", ["product", "consu"]),
        ("qty_available", ">",  0),
        ("image_1920",    "!=", False),
    ]

    if UPDATE_ONLY:
        # У режимі оновлення: поля для прайсингу + наявність
        fields = ["id", "default_code", "list_price", "qty_available",
                  "categ_id", "standard_price", "product_brand_id"]
    else:
        fields = [
            "id", "name", "default_code", "list_price",
            "description_sale", "public_categ_ids",
            "barcode", "weight", "country_of_origin",
            "product_brand_id", "categ_id", "standard_price",
        ]

    # Перевіряємо чи існує поле product_brand_id у цій версії Odoo
    try:
        models.execute_kw(db, uid, pw,
            "product.template", "fields_get", [["product_brand_id"]], {"attributes": ["string"]}
        )
        brand_field_exists = True
        log.info("  Поле product_brand_id знайдено ✅")
    except Exception:
        brand_field_exists = False
        fields = [f for f in fields if f != "product_brand_id"]
        log.info("  Поле product_brand_id не знайдено, бренд буде пропущено")

    batch_size = cfg.get("batch_size", 50)

    # Без ліміту — беремо всі товари з фото та в наявності
    ids = models.execute_kw(db, uid, pw,
        "product.template", "search", [domain], {}
    )
    if TEST_MODE:
        ids = ids[:TEST_LIMIT]
        log.info(f"🧪 ТЕСТ-РЕЖИМ: обмежено до {TEST_LIMIT} товарів")
    log.info(f"Товарів з фото та в наявності: {len(ids)}. Завантажую пакетами {batch_size}...")

    products = []
    for i in range(0, len(ids), batch_size):
        batch = models.execute_kw(db, uid, pw,
            "product.template", "read",
            [ids[i:i + batch_size]], {"fields": fields}
        )
        products.extend(batch)
        done = min(i + batch_size, len(ids))
        if done % 500 == 0 or done == len(ids):
            log.info(f"  {done}/{len(ids)}...")

    log.info(f"✅ Завантажено: {len(products)} товарів")

    # Дедуплікація за парою БРЕНД+АРТИКУЛ — справжній унікальний ключ для авtozапчастин
    seen = set()
    unique = []
    dupes = 0
    no_article = 0
    for p in products:
        article = str(p.get("default_code") or "").strip()
        brand_raw = p.get("product_brand_id") if brand_field_exists else False
        brand = ""
        if brand_raw and isinstance(brand_raw, (list, tuple)):
            brand = str(brand_raw[1]).strip()
        elif brand_raw and isinstance(brand_raw, str):
            brand = brand_raw.strip()

        if not article:
            no_article += 1
            article = str(p["id"])  # fallback на Odoo ID

        key = f"{brand}_{article}".upper()
        if key in seen:
            dupes += 1
        else:
            seen.add(key)
            # Зберігаємо бренд у продукті для генерації XML
            p["_brand"] = brand
            p["_article"] = article
            unique.append(p)

    if no_article:
        log.info(f"  Товарів без артикула (використано Odoo ID): {no_article}")
    if dupes:
        log.info(f"  Дублікати бренд+артикул видалено: {dupes} шт. Унікальних: {len(unique)}")

    return unique


# ═══════════════════════════════════════════════════════════
#  5. ГЕНЕРАЦІЯ XML (точний формат Єпіцентру)
# ═══════════════════════════════════════════════════════════
def generate_xml(products, odoo_url, pub_cats):
    doc     = Document()
    catalog = doc.createElement("yml_catalog")
    catalog.setAttribute("date", datetime.now().strftime("%Y-%m-%d %H:%M"))
    doc.appendChild(catalog)

    # Єпіцентр: <offers> напряму в <yml_catalog>, без <shop>
    offers_el = doc.createElement("offers")
    catalog.appendChild(offers_el)

    ok = fail = 0
    for p in products:
        try:
            if UPDATE_ONLY:
                _offer_update_only(doc, offers_el, p)
            else:
                _offer_full(doc, offers_el, p, odoo_url, pub_cats)
            ok += 1
        except Exception as e:
            log.warning(f"  Пропущено id={p.get('id')}: {e}")
            fail += 1

    log.info(f"XML: ок={ok}, пропущено={fail}")
    raw = doc.toprettyxml(indent="  ", encoding="UTF-8").decode("UTF-8")
    # Виправляємо зайві пробіли/переноси в текстових вузлах (баг toprettyxml)
    raw = re.sub(r'>\s+([^<\s][^<]*?)\s*(</)', r'>\1\2', raw)
    lines = raw.split("\n")
    lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
    return "\n".join(lines)


def _make_offer_id(p):
    """
    Унікальний ID для Єпіцентру = тільки артикул (без бренду), макс 64 символи.
    Єпіцентр вимагає: буквено-цифрове значення без розділових знаків.
    """
    article = re.sub(r'[^A-Za-z0-9А-ЯҐЄІЇа-яґєії]', '', p.get("_article", str(p["id"])))
    return article[:64] or str(p["id"])


def _offer_update_only(doc, offers_el, p):
    """Мінімальний offer: offer_id, ціна, наявність — для щоденного авто-оновлення."""
    offer_id = _make_offer_id(p)
    in_stock = float(p.get("qty_available", 0)) > 0
    offer = doc.createElement("offer")
    offer.setAttribute("id",        offer_id)
    offer.setAttribute("available", "true" if in_stock else "false")
    _el(doc, offer, "price", str(p.get("_retail_price", round(float(p.get("list_price", 0)), 2))))
    offers_el.appendChild(offer)


def _offer_full(doc, offers_el, p, odoo_url, pub_cats):
    """Повний offer — точний формат шаблону Єпіцентру."""
    offer_id = _make_offer_id(p)
    in_stock = float(p.get("qty_available", 0)) > 0
    price    = str(p.get("_retail_price", round(float(p.get("list_price", 0)), 2)))

    # Пропускаємо товари без ціни
    if float(price) <= 0:
        return

    offer = doc.createElement("offer")
    offer.setAttribute("id",        offer_id)
    offer.setAttribute("available", "true" if in_stock else "false")

    # ── Ціна ──────────────────────────────────────────────
    _el(doc, offer, "price", price)

    # ── Категорія (єдина для всіх авtozапчастин) ──────────
    cat_code = EPICENTR_CATEGORY_CODE
    cat_name = EPICENTR_CATEGORY_NAME

    cat_el = doc.createElement("category")
    cat_el.setAttribute("code", cat_code)
    cat_el.appendChild(doc.createTextNode(cat_name))
    offer.appendChild(cat_el)

    # ── Набір атрибутів ───────────────────────────────────
    aset = doc.createElement("attribute_set")
    aset.setAttribute("code", cat_code)
    aset.appendChild(doc.createTextNode(cat_name))
    offer.appendChild(aset)

    # ── Назва (ua + ru) ───────────────────────────────────
    name = (p.get("name") or "")[:150].strip()
    for lang in ("ua", "ru"):
        n = doc.createElement("name")
        n.setAttribute("lang", lang)
        n.appendChild(doc.createTextNode(name))
        offer.appendChild(n)

    # ── Фото ──────────────────────────────────────────────
    _el(doc, offer, "picture",
        f"{odoo_url}/web/image/product.template/{p['id']}/image_1920")

    # ── Опис (ua + ru) — HTML-entities як у шаблоні ───────
    raw_desc = str(p.get("description_sale") or name).strip()
    if not raw_desc.startswith("<"):
        raw_desc = f"<p>{raw_desc}</p>"
    for lang in ("ua", "ru"):
        d = doc.createElement("description")
        d.setAttribute("lang", lang)
        d.appendChild(doc.createTextNode(raw_desc))   # → автоматично html-entities
        offer.appendChild(d)

    # ── Бренд: <vendor code="..."> ────────────────────────
    brand = p.get("_brand", "").strip()
    if brand:
        v = doc.createElement("vendor")
        brand_info = BRAND_MAP.get(brand, {})
        brand_code = brand_info.get("code", "")
        if brand_code:
            v.setAttribute("code", brand_code)
        v.appendChild(doc.createTextNode(brand))
        offer.appendChild(v)

    # ── Країна: <country_of_origin code="deu"> ───────────
    co = p.get("country_of_origin")
    if co:
        co_name = (co[1] if isinstance(co, (list, tuple)) else str(co)).strip()
        epicentr_co = EPICENTR_COUNTRY.get(co_name)
        if epicentr_co:
            co_el = doc.createElement("country_of_origin")
            co_el.setAttribute("code", epicentr_co["code"])
            co_el.appendChild(doc.createTextNode(epicentr_co["name"]))
            offer.appendChild(co_el)
        elif co_name:
            # Якщо країна не в словнику — відправляємо без коду (краще ніж нічого)
            co_el = doc.createElement("country_of_origin")
            co_el.appendChild(doc.createTextNode(co_name))
            offer.appendChild(co_el)

    # ── Міра виміру ───────────────────────────────────────
    _param(doc, offer, "Міра виміру", "measure", valuecode="measure_pcs", text="шт.")

    # ── Мінімальна кратність ──────────────────────────────
    _param(doc, offer, "Мінімальна кратність товару", "ratio", text="1")

    # ── Вага (Odoo: кг → г) ──────────────────────────────
    try:
        wf_g = round(float(p.get("weight") or 0) * 1000)
    except (ValueError, TypeError):
        wf_g = 0
    _param(doc, offer, "Вага", "weight", text=str(wf_g if wf_g > 0 else 100))

    # ── Розміри (немає в Odoo → дефолт 100 мм) ───────────
    _param(doc, offer, "Ширина",  "width",  text="100")
    _param(doc, offer, "Висота",  "height", text="100")
    _param(doc, offer, "Глибина", "length", text="100")

    # ── Обмін та повернення ───────────────────────────────
    _param(doc, offer, "Обмін та повернення", "14195",
           valuecode="0e28a481b4511ac498d0061d0f702bd5", text="14 днів з дня покупки")

    # ── Штрих-код ─────────────────────────────────────────
    if p.get("barcode"):
        _param(doc, offer, "Штрих код", "barcodes", text=str(p["barcode"]))

    offers_el.appendChild(offer)


def _el(doc, parent, tag, text):
    el = doc.createElement(tag)
    el.appendChild(doc.createTextNode(text))
    parent.appendChild(el)

def _param(doc, parent, name, paramcode, valuecode=None, text=""):
    p = doc.createElement("param")
    p.setAttribute("name", name)
    p.setAttribute("paramcode", paramcode)
    if valuecode:
        p.setAttribute("valuecode", valuecode)
    p.appendChild(doc.createTextNode(str(text)))
    parent.appendChild(p)


# ═══════════════════════════════════════════════════════════
#  6. ЗБЕРЕЖЕННЯ XML
# ═══════════════════════════════════════════════════════════
def save_xml(xml_str):
    with open(OUTPUT_XML, "w", encoding="UTF-8") as f:
        f.write(xml_str)
    kb = os.path.getsize(OUTPUT_XML) // 1024
    log.info(f"✅ XML збережено: {OUTPUT_XML.name} ({kb} КБ)")


# ═══════════════════════════════════════════════════════════
#  7. ЗАВАНТАЖЕННЯ XML
# ═══════════════════════════════════════════════════════════
def _load_ftp_cfg():
    """Читає FTP секцію з odoo_config.txt. Повертає dict або None."""
    cfg = configparser.ConfigParser()
    cfg.read(ODOO_CONFIG, encoding="utf-8")
    if "ftp" not in cfg:
        return None
    s = cfg["ftp"]
    host     = s.get("host", "").strip()
    username = s.get("username", "").strip()
    password = s.get("password", "").strip()
    # Не заповнено — пропускаємо FTP
    if not host or username in ("", "ВАШ_FTP_ЛОГІН") or not password or password == "ВАШ_FTP_ПАРОЛЬ":
        return None
    return {
        "host":          host,
        "port":          int(s.get("port", 21)),
        "username":      username,
        "password":      password,
        "remote_dir":    s.get("remote_dir", "/www").strip(),
        "epicentr_file": s.get("epicentr_file", "epicentr_feed.xml").strip(),
        "epicentr_url":  s.get("epicentr_url",  "https://busauto.ua/epicentr_feed.xml").strip(),
    }


def _upload_via_ftp():
    """Завантажує epicentr_feed.xml через FTP. Повертає URL або None."""
    import ftplib
    ftp_cfg = _load_ftp_cfg()
    if not ftp_cfg:
        return None

    try:
        log.info(f"FTP: підключення до {ftp_cfg['host']}:{ftp_cfg['port']}...")
        ftp = ftplib.FTP()
        ftp.connect(ftp_cfg["host"], ftp_cfg["port"], timeout=60)
        ftp.login(ftp_cfg["username"], ftp_cfg["password"])
        ftp.set_pasv(True)

        remote_dir = ftp_cfg["remote_dir"]
        if remote_dir:
            ftp.cwd(remote_dir)

        remote_name = ftp_cfg["epicentr_file"]
        mb = os.path.getsize(OUTPUT_XML) // (1024 * 1024)
        log.info(f"FTP: завантаження {remote_name} ({mb} МБ)...")

        with open(OUTPUT_XML, "rb") as f:
            ftp.storbinary(f"STOR {remote_name}", f, blocksize=65536)

        ftp.quit()
        log.info("✅ FTP: файл завантажено успішно")

        url = ftp_cfg["epicentr_url"]
        log.info(f"🔗 URL для Єпіцентру: {url}")
        (BASE_DIR / "epicentr_feed_url.txt").write_text(
            f"URL для Єпіцентру:\n{url}\n\nОновлено: {datetime.now()}\n",
            encoding="utf-8"
        )
        return url

    except Exception as e:
        log.error(f"❌ FTP помилка: {e}")
        return None


def upload_to_odoo(models, db, uid, pw, odoo_url):
    """Завантажує XML фід на сервер.

    Стратегія:
      1. FTP — якщо заповнено [ftp] в odoo_config.txt (рекомендовано для файлів > 100МБ)
      2. Odoo XMLRPC base64 — fallback (дає 413 якщо файл > ~150МБ через nginx ліміт)

    Для великих файлів (> 100МБ) обов'язково використовуйте FTP:
      Заповніть username і password в секції [ftp] файлу odoo_config.txt
    """
    # ── 1. Спроба FTP (тільки для повного фіду) ─────────────
    if not TEST_MODE:
        ftp_url = _upload_via_ftp()
        if ftp_url:
            return ftp_url

    if not TEST_MODE and not _load_ftp_cfg():
        log.warning(
            "⚠️  FTP не налаштовано. Файл %d МБ → XMLRPC base64 → може дати 413.\n"
            "    Заповніть username/password в секції [ftp] файлу odoo_config.txt",
            os.path.getsize(OUTPUT_XML) // (1024 * 1024)
        )

    # ── 2. Fallback: Odoo XMLRPC base64 ──────────────────────
    log.info("XMLRPC upload...")
    with open(OUTPUT_XML, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    att_id = None
    if ODOO_ATT_ID.exists():
        try:
            att_id = int(ODOO_ATT_ID.read_text().strip())
            exists = models.execute_kw(db, uid, pw,
                "ir.attachment", "search", [[["id", "=", att_id]]])
            if not exists:
                att_id = None
        except Exception:
            att_id = None

    if att_id:
        models.execute_kw(db, uid, pw,
            "ir.attachment", "write",
            [[att_id], {"datas": b64_data}])
        log.info(f"✅ Odoo attachment оновлено (id={att_id})")
    else:
        att_id = models.execute_kw(db, uid, pw,
            "ir.attachment", "create", [{
                "name": "epicentr_feed.xml",
                "datas": b64_data,
                "type": "binary",
                "public": True,
                "mimetype": "application/xml",
            }])
        ODOO_ATT_ID.write_text(str(att_id))
        log.info(f"✅ Odoo attachment створено (id={att_id})")

    url = f"{odoo_url}/web/content/{att_id}/epicentr_feed.xml"
    log.info(f"🔗 URL для Єпіцентру: {url}")
    (BASE_DIR / "epicentr_feed_url.txt").write_text(
        f"URL для Єпіцентру:\n{url}\n\nОновлено: {datetime.now()}\n",
        encoding="utf-8"
    )
    return url


# ═══════════════════════════════════════════════════════════
#  8. MAIN
# ═══════════════════════════════════════════════════════════
def main():
    if TEST_MODE:
        mode = f"🧪 ТЕСТ ({TEST_LIMIT} товарів)"
    elif UPDATE_ONLY:
        mode = "ОНОВЛЕННЯ (ціна+наявність)"
    else:
        mode = "ПОВНИЙ ФІД"
    log.info("=" * 60)
    log.info(f"  BusAuto → Епіцентр | Режим: {mode}")
    log.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    odoo_cfg     = load_odoo_cfg()
    epicentr_cfg = load_epicentr_cfg()

    models, db, uid, pw = connect_odoo(odoo_cfg)

    # Категорії потрібні тільки для повного фіду
    pub_cats = load_public_categories(models, db, uid, pw) if not UPDATE_ONLY else {}

    # ── Прайсинг: ті самі дані що в Google Merchant фіді ──
    log.info("Завантаження даних для прайсингу...")
    currency_rates  = load_currency_rates(models, db, uid, pw)
    categ_ancestors = load_categ_ancestors(models, db, uid, pw)
    brand_rules     = load_brand_markup_rules(models, db, uid, pw)

    products = get_products(models, db, uid, pw, epicentr_cfg)
    if not products:
        log.warning("⚠️  Товарів не знайдено (нема фото або нема в наявності).")
        return

    # Ціни постачальників для всіх шаблонів
    tmpl_ids    = [p["id"] for p in products]
    supp_prices = load_supplier_prices(models, db, uid, pw, tmpl_ids, currency_rates)

    # Розраховуємо роздрібну ціну для кожного товару
    log.info("Розрахунок роздрібних цін...")
    zero_price = 0
    for p in products:
        price = compute_product_price(p, supp_prices, currency_rates, categ_ancestors, brand_rules)
        p["_retail_price"] = str(price)
        if price <= 0:
            zero_price += 1
    log.info(f"  Розраховано: {len(products)}, без ціни: {zero_price}")

    xml_str = generate_xml(products, odoo_cfg["url"], pub_cats)
    save_xml(xml_str)

    try:
        url = upload_to_odoo(models, db, uid, pw, odoo_cfg["url"])
    except Exception as e:
        log.warning(f"Upload пропущено: {e}")
        url = "—"

    log.info("=" * 60)
    log.info(f"✅ Готово! Товарів: {xml_str.count('<offer ')} | URL: {url}")
    log.info("=" * 60)

    if TEST_MODE:
        # Підраховуємо скільки товарів мають код бренду
        vendor_with_code = xml_str.count(' code="') - xml_str.count('<country_of_origin code=')
        vendor_total     = xml_str.count('<vendor')
        print("\n" + "=" * 60)
        print(f"  🧪 ТЕСТ ЗАВЕРШЕНО")
        print(f"  Товарів у XML:         {xml_str.count('<offer ')}")
        print(f"  <vendor> з кодом:      {vendor_with_code}/{vendor_total}")
        print(f"  <country_of_origin>:   {xml_str.count('<country_of_origin')}")
        print(f"  URL для перевірки:")
        print(f"  {url}")
        print("=" * 60)
        print("\nВідкрий URL вище у браузері — перевір XML вручну.")
        print(f"Або переглянь файл: {OUTPUT_XML}")


if __name__ == "__main__":
    main()
