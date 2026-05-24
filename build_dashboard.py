# -*- coding: utf-8 -*-
"""
Збірка HTML-дашборду з dashboard_data/summary.json.
Запускається після odoo_export.py - дашборд оновлюється з реальними даними.
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY = os.path.join(SCRIPT_DIR, "dashboard_data", "summary.json")
GADS    = os.path.join(SCRIPT_DIR, "dashboard_data", "gads_summary.json")
FIN     = os.path.join(SCRIPT_DIR, "dashboard_data", "finance_summary.json")
OUTPUT  = os.path.join(SCRIPT_DIR, "busauto_owner_dashboard.html")
TEMPLATE = os.path.join(SCRIPT_DIR, "dashboard_template.html")
WH_OUTPUT   = os.path.join(SCRIPT_DIR, "busauto_warehouse_dashboard.html")
WH_TEMPLATE = os.path.join(SCRIPT_DIR, "warehouse_dashboard_template.html")
FIN_OUTPUT   = os.path.join(SCRIPT_DIR, "busauto_financial_dashboard.html")
FIN_TEMPLATE = os.path.join(SCRIPT_DIR, "financial_dashboard_template.html")

with open(SUMMARY, "r", encoding="utf-8") as f:
    data = json.load(f)

gads = None
if os.path.exists(GADS):
    with open(GADS, "r", encoding="utf-8") as f:
        gads = json.load(f)

# Зменшуємо JSON: округлення до 2 знаків
def trim(obj):
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, list):
        return [trim(x) for x in obj]
    if isinstance(obj, dict):
        return {k: trim(v) for k, v in obj.items()}
    return obj

payload = json.dumps(trim(data), ensure_ascii=False, separators=(",", ":"))
gads_payload = json.dumps(trim(gads), ensure_ascii=False, separators=(",", ":")) if gads else "null"

with open(TEMPLATE, "r", encoding="utf-8") as f:
    html = f.read()

html = html.replace("/*__SUMMARY__*/null", payload)
html = html.replace("/*__GADS__*/null", gads_payload)

with open(OUTPUT, "w", encoding="utf-8") as f:
    f.write(html)

size_kb = os.path.getsize(OUTPUT) / 1024
print(f"[OK] Owner dashboard:    {OUTPUT} ({size_kb:.1f} KB)")
print(f"     Замовлень:          {data['kpi']['orders']:,}")
print(f"     Виручка:            {data['kpi']['revenue']:,.0f} ₴")
print(f"     Джерел:             {len(data['sources'])}")

# Warehouse dashboard
if os.path.exists(WH_TEMPLATE):
    with open(WH_TEMPLATE, "r", encoding="utf-8") as f:
        wh_html = f.read()
    wh_html = wh_html.replace("/*__SUMMARY__*/null", payload)
    with open(WH_OUTPUT, "w", encoding="utf-8") as f:
        f.write(wh_html)
    size_kb = os.path.getsize(WH_OUTPUT) / 1024
    print(f"\n[OK] Warehouse dashboard: {WH_OUTPUT} ({size_kb:.1f} KB)")
    wh = data.get("warehouse_abc")
    if wh:
        print(f"     Склад:              {wh['warehouse_name']}")
        print(f"     SKU в наявності:    {wh['total_sku']:,}")
        print(f"     Вартість залишку:   {wh['total_stock_value']:,.0f} ₴")
        print(f"     A/B/C:              {wh['A']['sku_count']}/{wh['B']['sku_count']}/{wh['C']['sku_count']}")
    else:
        print(f"     [!] warehouse_abc відсутній - перевірте, що склад знайдено")
else:
    print(f"\n[!] Warehouse template не знайдено: {WH_TEMPLATE}")

# Financial dashboard
if os.path.exists(FIN_TEMPLATE) and os.path.exists(FIN):
    with open(FIN, "r", encoding="utf-8") as f:
        fin_data = json.load(f)
    fin_payload = json.dumps(trim(fin_data), ensure_ascii=False, separators=(",", ":"))
    with open(FIN_TEMPLATE, "r", encoding="utf-8") as f:
        fin_html = f.read()
    fin_html = fin_html.replace("/*__FINANCE__*/null", fin_payload)
    with open(FIN_OUTPUT, "w", encoding="utf-8") as f:
        f.write(fin_html)
    size_kb = os.path.getsize(FIN_OUTPUT) / 1024
    print(f"\n[OK] Financial dashboard: {FIN_OUTPUT} ({size_kb:.1f} KB)")
    tot = fin_data.get("total_period", {})
    if tot:
        print(f"     Виручка (12 міс):   {tot.get('revenue', 0):,.0f} ₴")
        print(f"     EBIT:               {tot.get('ebit', 0):,.0f} ₴ "
              f"({tot.get('ebit', 0)/tot.get('revenue', 1)*100:.1f}%)")
        print(f"     Грошова позиція:    {fin_data.get('total_cash', 0):,.0f} ₴")
elif not os.path.exists(FIN):
    print(f"\n[!] finance_summary.json не знайдено - спершу запустіть finance_export.py")
else:
    print(f"\n[!] Financial template не знайдено: {FIN_TEMPLATE}")
