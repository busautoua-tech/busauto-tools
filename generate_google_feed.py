# -*- coding: utf-8 -*-
"""
generate_google_feed.py — Генерація XML фіду для Google Merchant Center
Джерело: oDoo 17 (busauto.ua) → Google Merchant Center 163800443

Що генерує:
  - google_feed.xml — товарний фід для завантаження в Merchant Center

Фільтри товарів:
  - active = True, sale_ok = True, is_published = True
  - qty_available > 0  (лише товари в наявності)
  - є зображення (image_1920)
  - є артикул (default_code)
  - list_price > 0

Обов'язкові поля Google Shopping:
  g:id, g:title, g:description, g:link, g:image_link,
  g:price, g:availability, g:condition, g:brand, g:mpn,
  g:google_product_category

Використання:
  python generate_google_feed.py

Потім завантажити google_feed.xml в Google Merchant Center:
  https://merchants.google.com → Продукти → Фіди → Додати фід → Завантажити файл
"""

import configparser
import xmlrpc.client as xc
import xml.etree.ElementTree as ET
from xml.dom import minidom
import ftplib
import re
import os
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Налаштування ────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "odoo_config.txt")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "google_feed.xml")
LOG_FILE    = os.path.join(SCRIPT_DIR, "google_feed.log")

SHOP_URL    = "https://busauto.ua"   # URL сайту без слешу в кінці
ODOO_CHUNK  = 200                    # розмір пакету (менший — стабільніший)
MAX_TITLE   = 150                    # Google: max 150 символів
MAX_DESC    = 5000                   # Google: max 5000 символів

# Google product category для автозапчастин
# 5765 = Vehicles & Parts > Vehicle Parts & Accessories
GOOGLE_PRODUCT_CATEGORY = "5765"


# ── Утиліти ─────────────────────────────────────────────────────────

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
    if "odoo" not in cfg:
        raise KeyError("Секція [odoo] не знайдена в odoo_config.txt")
    s = cfg["odoo"]
    return (s["url"].strip().rstrip("/"),
            s["database"].strip(),
            s["username"].strip(),
            s["api_key"].strip()), cfg


def read_ftp_config(cfg):
    """Читаємо FTP налаштування (опційно — якщо секція [ftp] заповнена)"""
    if "ftp" not in cfg:
        return None
    f = cfg["ftp"]
    user = f.get("username", "").strip()
    pwd  = f.get("password", "").strip()
    # Якщо логін/пароль не заповнені — пропускаємо FTP
    if not user or user.startswith("ВАШ_") or not pwd or pwd.startswith("ВАШ_"):
        return None
    return {
        "host":        f.get("host", "").strip(),
        "port":        int(f.get("port", "21").strip()),
        "username":    user,
        "password":    pwd,
        "remote_dir":  f.get("remote_dir", "/www").strip(),
        "remote_file": f.get("remote_file", "google_feed.xml").strip(),
    }


def upload_via_ftp(local_file, ftp_cfg):
    """Завантажуємо XML на сервер через FTP"""
    host  = ftp_cfg["host"]
    port  = ftp_cfg["port"]
    user  = ftp_cfg["username"]
    rdir  = ftp_cfg["remote_dir"]
    rfile = ftp_cfg["remote_file"]

    log(f"FTP підключення до {host}:{port} ...")
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(user, ftp_cfg["password"])
        ftp.set_pasv(True)
        log(f"FTP авторизація успішна")

        # Переходимо в потрібну папку
        ftp.cwd(rdir)
        log(f"FTP: перейшли в папку {rdir}")

        # Завантажуємо файл
        with open(local_file, "rb") as f:
            ftp.storbinary(f"STOR {rfile}", f)

        ftp.quit()
        log(f"✅ Файл завантажено: {rdir}/{rfile}")
        log(f"   URL фіду: https://busauto.ua/{rfile}")
        return True

    except ftplib.all_errors as e:
        log(f"❌ FTP помилка: {e}")
        log("   Перевірте: хост, логін, пароль, шлях remote_dir в odoo_config.txt")
        return False


# ── Читаємо конфіг (стара функція замінена вище, залишаємо сумісність)
def _read_odoo_only(cfg):
    s = cfg["odoo"]
    return (s["url"].strip().rstrip("/"),
            s["database"].strip(),
            s["username"].strip(),
            s["api_key"].strip())


def odoo_connect(url, db, user, apikey):
    uid = xc.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, user, apikey, {})
    if not uid:
        log("ПОМИЛКА авторизації Odoo! Перевірте api_key та логін.")
        raise SystemExit(1)
    models = xc.ServerProxy(f"{url}/xmlrpc/2/object")
    log(f"Avторизація успішна, uid={uid}")
    return uid, models


def clean_html(text, max_len=None):
    """Видаляємо HTML-теги, нормалізуємо пробіли, обрізаємо до max_len"""
    if not text:
        return ""
    t = re.sub(r'<[^>]+>', ' ', str(text))
    t = t.replace('&nbsp;', ' ').replace('&amp;', '&') \
         .replace('&lt;', '<').replace('&gt;', '>') \
         .replace('&quot;', '"')
    t = re.sub(r'\s+', ' ', t).strip()
    if max_len and len(t) > max_len:
        t = t[:max_len - 3] + "..."
    return t


def get_brand(product):
    b = product.get("product_brand_id")
    if isinstance(b, (list, tuple)) and len(b) > 1:
        return str(b[1]).strip()
    return ""


def build_title(product):
    """Назва: 'БРЕНД Назва товару' якщо бренд не повторюється"""
    name  = clean_html(product.get("name") or "")
    brand = get_brand(product)
    if brand and brand.lower() not in name.lower():
        title = f"{brand} {name}"
    else:
        title = name
    return clean_html(title, MAX_TITLE)


def build_description(product):
    """Опис: беремо description_sale, якщо порожній — формуємо з назви"""
    desc = clean_html(product.get("description_sale") or "", MAX_DESC)
    if len(desc) < 20:
        name  = clean_html(product.get("name") or "")
        brand = get_brand(product)
        desc  = (f"Автозапчастина {brand} {name}. "
                 "В наявності на складі. Доставка по всій Україні 1-2 дні. "
                 "Оригінальні деталі та якісні аналоги. "
                 "Консультація фахівців безкоштовно.")
    return desc


def get_product_url(product):
    """URL сторінки товару на busauto.ua"""
    wu = (product.get("website_url") or "").strip()
    if wu and wu not in ("/", ""):
        return SHOP_URL + wu
    # Fallback: пошук по артикулу
    code = (product.get("default_code") or "").replace(" ", "+")
    return f"{SHOP_URL}/shop?search={code}"


def get_image_url(product_id):
    """Пряме посилання на зображення через oDoo web/image endpoint"""
    return f"{SHOP_URL}/web/image/product.template/{product_id}/image_1920"


# ── Завантаження товарів з Odoo ──────────────────────────────────────

def load_products(models, db, uid, apikey):
    domain = [
        ["active",        "=", True],
        ["sale_ok",       "=", True],
        ["is_published",  "=", True],
        ["qty_available", ">", 0],
        ["image_1920",    "!=", False],
        ["list_price",    ">", 0],
        ["default_code",  "!=", False],
        ["default_code",  "!=", ""],
    ]
    fields = [
        "id", "name", "default_code", "list_price",
        "product_brand_id", "categ_id",
        "description_sale",
        "website_url",
        "qty_available",
    ]

    products = []
    offset   = 0
    page     = 1

    log("Завантажуємо товари з Odoo...")
    while True:
        chunk = models.execute_kw(db, uid, apikey,
            "product.template", "search_read", [domain],
            {"fields": fields,
             "limit": ODOO_CHUNK,
             "offset": offset,
             "context": {"lang": "uk_UA"}})
        if not chunk:
            break
        products.extend(chunk)
        log(f"  Пакет {page} — {len(chunk)} товарів (загалом: {len(products)})")
        if len(chunk) < ODOO_CHUNK:
            break
        offset += ODOO_CHUNK
        page   += 1
        time.sleep(0.1)

    log(f"Товарів завантажено з Odoo: {len(products)}")
    return products


# ── Генерація XML ────────────────────────────────────────────────────

def generate_feed(products):
    """Будуємо RSS-фід у форматі Google Merchant Center"""

    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:g": "http://base.google.com/ns/1.0",
    })
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text       = "BusAuto — автозапчастини для іномарок"
    ET.SubElement(channel, "link").text        = SHOP_URL
    ET.SubElement(channel, "description").text = (
        "Каталог автозапчастин BusAuto. Оригінал та аналоги. Доставка по Україні.")

    ok_count   = 0
    skip_count = 0

    for p in products:
        pid  = p.get("id")
        code = (p.get("default_code") or "").strip()
        price = p.get("list_price") or 0.0
        qty   = p.get("qty_available") or 0

        # Пропускаємо товари без артикула або ціни
        if not code or price <= 0:
            skip_count += 1
            continue

        title       = build_title(p)
        description = build_description(p)
        link        = get_product_url(p)
        image_url   = get_image_url(pid)
        brand       = get_brand(p) or "BusAuto"
        avail       = "in_stock" if qty > 0 else "out_of_stock"
        price_str   = f"{price:.2f} UAH"

        item = ET.SubElement(channel, "item")

        def g(tag, text):
            """Додаємо елемент з простором імен g:"""
            el = ET.SubElement(item, f"g:{tag}")
            el.text = str(text) if text is not None else ""

        # Обов'язкові поля
        g("id",          code)
        ET.SubElement(item, "title").text       = title
        ET.SubElement(item, "description").text = description
        ET.SubElement(item, "link").text        = link
        g("image_link",  image_url)
        g("price",       price_str)
        g("availability", avail)
        g("condition",   "new")
        g("brand",       brand)
        g("mpn",         code)   # для запчастин артикул = MPN

        # Рекомендовані поля
        g("google_product_category", GOOGLE_PRODUCT_CATEGORY)

        categ = p.get("categ_id")
        if isinstance(categ, (list, tuple)) and len(categ) > 1:
            g("product_type", str(categ[1]))

        ok_count += 1

    log(f"Записів у фіді: {ok_count}  |  пропущено (немає ціни/артикула): {skip_count}")
    return rss


def save_xml(rss_element, output_path):
    """Зберігаємо XML з форматуванням"""
    raw  = ET.tostring(rss_element, encoding="unicode")
    dom  = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}')
    pretty = dom.toprettyxml(indent="  ", encoding="UTF-8")
    with open(output_path, "wb") as f:
        f.write(pretty)
    kb = os.path.getsize(output_path) / 1024
    log(f"Файл збережено: {output_path}  ({kb:.0f} KB)")


# ── Головна функція ──────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("  Google Merchant Center Feed — BusAuto (163800443)")
    log(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    # 1. Конфіг
    try:
        (url, db, user, apikey), cfg = read_config()
        log(f"Підключення: {url}  DB={db}  User={user}")
    except Exception as e:
        log(f"ПОМИЛКА читання {CONFIG_FILE}: {e}")
        raise SystemExit(1)

    # Читаємо FTP конфіг (опційно)
    ftp_cfg = read_ftp_config(cfg)
    if ftp_cfg:
        log(f"FTP налаштований: {ftp_cfg['host']}  →  {ftp_cfg['remote_dir']}/{ftp_cfg['remote_file']}")
    else:
        log("FTP не налаштований — файл збережеться лише локально")

    # 2. Підключення до Odoo
    uid, models = odoo_connect(url, db, user, apikey)

    # 3. Завантаження товарів
    products = load_products(models, db, uid, apikey)
    if not products:
        log("Товарів не знайдено! Перевірте фільтри або права доступу.")
        raise SystemExit(1)

    # 4. Генерація XML
    log("Будуємо XML фід...")
    rss = generate_feed(products)

    # 5. Збереження локально
    save_xml(rss, OUTPUT_FILE)

    # 6. Завантаження на сервер через FTP (якщо налаштований)
    if ftp_cfg:
        log("")
        log("Завантажуємо фід на сервер...")
        ok = upload_via_ftp(OUTPUT_FILE, ftp_cfg)
        if ok:
            feed_url = f"https://busauto.ua/{ftp_cfg['remote_file']}"
            log("")
            log("✅  ФІД ЗАВАНТАЖЕНО НА СЕРВЕР!")
            log("")
            log(f"   URL фіду: {feed_url}")
            log("")
            log("   В Google Merchant Center (один раз):")
            log("   Налаштування → Джерела даних → + Додати джерело даних")
            log("   → Метод: Заплановане отримання (Scheduled fetch)")
            log(f"   → URL: {feed_url}")
            log("   → Частота: Щодня")
            log("")
            log("   Після цього Google сам оновлюватиме фід щодня!")
        else:
            log("")
            log("⚠️  FTP не спрацював — завантажте google_feed.xml вручну.")
            log("   Перевірте дані в секції [ftp] файлу odoo_config.txt")
    else:
        log("")
        log("✅  ФІД ГОТОВИЙ (локально)!")
        log("")
        log("   Файл: google_feed.xml (в папці скрипту)")
        log("")
        log("   Щоб налаштувати автоматичне оновлення через URL:")
        log("   1. Заповніть секцію [ftp] в odoo_config.txt")
        log("   2. Запустіть скрипт знову")
        log("")
        log("   АБО завантажте файл вручну в Merchant Center:")
        log("   Налаштування → Джерела даних → + Додати → Завантажити файл")


if __name__ == "__main__":
    main()
