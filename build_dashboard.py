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

GADS_OUTPUT = os.path.join(SCRIPT_DIR, "busauto_gads_dashboard.html")

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

# Google Ads dashboard — оновлюємо const D у файлі
if os.path.exists(GADS) and os.path.exists(GADS_OUTPUT):
    try:
        with open(GADS, "r", encoding="utf-8") as f:
            g = json.load(f)

        rows = g.get("daily_by_account", [])
        ACC = {"busauto.kh.ua": "bk", "automobil.in.ua": "au", "busauto.ua": "bu"}

        # Збираємо унікальні дати
        dates = sorted(set(r["date"] for r in rows))

        # Денні дані по акаунтах
        def daily(acc_key, field):
            by_date = {r["date"]: r for r in rows if ACC.get(r["account"]) == acc_key}
            return [round(by_date.get(d, {}).get(field, 0), 2) for d in dates]

        def acc_totals(acc_key):
            acc_rows = [r for r in rows if ACC.get(r["account"]) == acc_key]
            cost  = round(sum(r.get("cost", 0) for r in acc_rows))
            cv    = round(sum(r.get("conv_value", 0) for r in acc_rows))
            clicks = sum(r.get("clicks", 0) for r in acc_rows)
            impr  = sum(r.get("impressions", 0) for r in acc_rows)
            conv  = round(sum(r.get("conversions", 0) for r in acc_rows))
            roas  = round(cv / cost, 2) if cost else 0
            ctr   = round(clicks / impr * 100, 2) if impr else 0
            cpc   = round(cost / clicks, 2) if clicks else 0
            return {"cost": cost, "cv": cv, "clicks": clicks,
                    "impr": impr, "conv": conv, "roas": roas, "ctr": ctr, "cpc": cpc}

        bk = acc_totals("bk")
        au = acc_totals("au")
        bu = acc_totals("bu")
        total_cost = bk["cost"] + au["cost"] + bu["cost"]
        total_cv   = bk["cv"] + au["cv"] + bu["cv"]
        D = {
            "dates":     dates,
            "bk_cost":   daily("bk", "cost"),
            "au_cost":   daily("au", "cost"),
            "bu_cost":   daily("bu", "cost"),
            "bk_clicks": daily("bk", "clicks"),
            "au_clicks": daily("au", "clicks"),
            "bu_clicks": daily("bu", "clicks"),
            "bk":    bk,
            "au":    au,
            "bu":    bu,
            "total": {"cost": total_cost, "cv": total_cv,
                      "clicks": bk["clicks"] + au["clicks"] + bu["clicks"],
                      "conv": bk["conv"] + au["conv"] + bu["conv"],
                      "roas": round(total_cv / total_cost, 2) if total_cost else 0},
            "generated": g.get("generated_at", ""),
            "period": f"{dates[0]} — {dates[-1]}" if dates else "",
        }

        # Рахуємо ROAS по днях через conv_value/cost
        for key, acc_key in [("bk_roas", "bk"), ("au_roas", "au"), ("bu_roas", "bu")]:
            by_date = {r["date"]: r for r in rows if ACC.get(r["account"]) == acc_key}
            D[key] = [
                round(by_date[d]["conv_value"] / by_date[d]["cost"], 2)
                if by_date.get(d) and by_date[d].get("cost") else 0
                for d in dates
            ]

        d_json = json.dumps(D, ensure_ascii=False, separators=(",", ":"))

        with open(GADS_OUTPUT, "r", encoding="utf-8") as f:
            gads_html = f.read()

        # Замінюємо рядок "const D = {...};"
        import re
        gads_html = re.sub(r'const D = \{.*?\};', f'const D = {d_json};', gads_html, count=1)

        with open(GADS_OUTPUT, "w", encoding="utf-8") as f:
            f.write(gads_html)

        size_kb = os.path.getsize(GADS_OUTPUT) / 1024
        print(f"\n[OK] Google Ads dashboard: {GADS_OUTPUT} ({size_kb:.1f} KB)")
        print(f"     Період: {D['period']}  |  ROAS: {D['total']['roas']}x  |  Витрати: {D['total']['cost']:,} ₴")
    except Exception as e:
        print(f"\n[!] Google Ads dashboard помилка: {e}")
elif not os.path.exists(GADS):
    print(f"\n[!] gads_summary.json не знайдено — gads_export.py не запускався або впав")
elif not os.path.exists(GADS_OUTPUT):
    print(f"\n[!] busauto_gads_dashboard.html не знайдено в репо")
