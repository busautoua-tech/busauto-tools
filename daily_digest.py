# -*- coding: utf-8 -*-
"""
Щоденний Telegram-дайджест власника BusAuto.

Читає dashboard_data/summary.json + gads_summary.json, формує коротке
повідомлення з ключовими метриками і алертами, відправляє в Telegram.

Запуск:
  python daily_digest.py

Налаштування - в файлі telegram_config.txt (приклад див. telegram_config_EXAMPLE.txt).
"""
import json
import os
import sys
import configparser
import urllib.request
from datetime import datetime, timedelta, timezone


def now_kyiv():
    """Поточний час Києва незалежно від часового поясу сервера."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        # Fallback - UTC+3 (літо); зимою буде на годину вперед, не критично
        return datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=3)))

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_FILE = os.path.join(SCRIPT_DIR, "dashboard_data", "summary.json")
GADS_FILE    = os.path.join(SCRIPT_DIR, "dashboard_data", "gads_summary.json")
TG_CONFIG    = os.path.join(SCRIPT_DIR, "telegram_config.txt")


def fmt_uah(v):
    if v is None:
        return "-"
    if abs(v) >= 1e6:
        return f"{v/1e6:.2f} млн ₴"
    if abs(v) >= 1e3:
        return f"{v/1e3:.1f} тис ₴"
    return f"{v:.0f} ₴"


def fmt_pct(v, sign=True):
    if v is None:
        return "-"
    s = "+" if sign and v >= 0 else ""
    return f"{s}{v:.0f}%"


def read_tg_config():
    if not os.path.exists(TG_CONFIG):
        print(f"[ERROR] Не знайдено {TG_CONFIG}")
        print("Створіть копію з telegram_config_EXAMPLE.txt і заповніть.")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(TG_CONFIG, encoding="utf-8")
    return (cfg["telegram"]["bot_token"].strip(),
            cfg["telegram"]["chat_id"].strip())


def send_telegram(token, chat_id, text, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    resp = urllib.request.urlopen(req, timeout=20)
    result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API: {result}")
    return result


def build_digest():
    if not os.path.exists(SUMMARY_FILE):
        return "❌ Немає файла summary.json - спочатку запустіть odoo_export.py"
    with open(SUMMARY_FILE, "r", encoding="utf-8") as f:
        s = json.load(f)

    daily = s.get("daily") or []
    if not daily:
        return "❌ Немає денних даних в summary.json"

    # Останній день з даними
    last = daily[-1]
    last_date = last["date"]
    last_rev = last.get("revenue", 0) or 0
    last_orders = last.get("orders", 0) or 0
    last_aov = last_rev / last_orders if last_orders else 0

    # Середнє за період (виключаючи останній день)
    other = daily[:-1]
    avg_rev = (sum(d.get("revenue", 0) for d in other) / len(other)) if other else 0
    avg_orders = (sum(d.get("orders", 0) for d in other) / len(other)) if other else 0

    rev_vs_avg = ((last_rev / avg_rev) - 1) * 100 if avg_rev else None
    ord_vs_avg = ((last_orders / avg_orders) - 1) * 100 if avg_orders else None

    lines = [
        f"📊 *BusAuto · {last_date}*",
        "",
        f"💰 Виручка: *{fmt_uah(last_rev)}* "
        f"({fmt_pct(rev_vs_avg)} до серед.)",
        f"📦 Замовлень: *{last_orders}* "
        f"({fmt_pct(ord_vs_avg)})",
        f"🛒 Середній чек: *{fmt_uah(last_aov)}*",
    ]

    kpi = s.get("kpi", {}) or {}

    # YoY якщо є
    yoy = s.get("yoy_kpi")
    if yoy and yoy.get("revenue"):
        cur_rev_period = kpi.get("revenue", 0)
        yoy_change = (cur_rev_period / yoy["revenue"] - 1) * 100 if yoy["revenue"] else None
        emoji = "📈" if yoy_change and yoy_change >= 0 else "📉"
        lines.append(f"{emoji} До минулого року: *{fmt_pct(yoy_change)}* "
                     f"(торік {fmt_uah(yoy['revenue'])})")

    if kpi.get("margin_pct") is not None:
        lines.append(f"💹 Маржа за період: *{kpi['margin_pct']:.1f}%*")

    cohort = s.get("cohort", {}) or {}
    if cohort.get("repeat_revenue_pct") is not None:
        lines.append(f"🔄 Повторні клієнти: "
                     f"*{cohort['repeat_revenue_pct']:.0f}%* виручки")

    # ROAS Google Ads
    if os.path.exists(GADS_FILE):
        try:
            with open(GADS_FILE, "r", encoding="utf-8") as f:
                g = json.load(f)
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            recent = [r for r in g.get("daily_by_account", [])
                      if r.get("date", "") >= cutoff]
            cost = sum(r.get("cost", 0) or 0 for r in recent)
            cv = sum(r.get("conv_value", 0) or 0 for r in recent)
            roas = cv / cost if cost else 0
            emoji = "✅" if roas >= 4 else "⚠️" if roas >= 2 else "🔴"
            lines.append(f"🎯 ROAS Google (7д): *{roas:.2f}x* {emoji}")
        except Exception as e:
            print(f"[WARN] gads_summary: {e}")

    # ALERTS
    alerts = []

    # Скасування
    if kpi.get("cancel_rate_pct", 0) > 20:
        alerts.append(f"🚨 Високий % скасувань: *{kpi['cancel_rate_pct']:.1f}%*")

    # Warehouse
    wh = s.get("warehouse_abc")
    if wh:
        a = wh.get("alerts", {}) or {}
        a_low = a.get("a_low_stock", []) or []
        if a_low:
            order_total = sum(p.get("suggested_order_value", 0) or 0
                              for p in wh.get("products", [])
                              if (p.get("suggested_order_qty") or 0) > 0)
            alerts.append(f"⚠ A-class закінчуються: *{len(a_low)} SKU*, "
                          f"закупка ~{fmt_uah(order_total)}")
        stuck = a.get("stuck_pickings", []) or []
        if stuck:
            alerts.append(f"🚨 Підвислих pickings: *{len(stuck)}* "
                          f"(валідуйте в oDoo)")
        ns_value = a.get("no_sales_value", 0) or 0
        if ns_value > 50000:
            ns_count = a.get("no_sales_count", 0)
            alerts.append(f"💀 Заморожено в неходових ({ns_count} SKU): "
                          f"{fmt_uah(ns_value)}")

    if alerts:
        lines.append("")
        lines.append("⚡️ *Що потребує уваги:*")
        for a in alerts:
            lines.append(f"  • {a}")
    else:
        lines.append("")
        lines.append("✅ Все в нормі")

    lines.append("")
    lines.append(f"_Оновлено: {now_kyiv().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)


def main():
    digest = build_digest()
    print("=" * 50)
    print("Сформований дайджест:")
    print("=" * 50)
    print(digest)
    print("=" * 50)
    token, chat_id = read_tg_config()
    print(f"Відправляю в chat_id={chat_id}...")
    try:
        send_telegram(token, chat_id, digest)
        print("[OK] Дайджест надіслано в Telegram")
    except Exception as e:
        print(f"[ERROR] Не вдалось надіслати: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
