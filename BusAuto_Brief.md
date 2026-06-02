# BusAuto — контекстний brief

**Як використовувати:** прикріпіть/посилайтесь на цей файл на початку кожного нового чату з Claude. Це дозволяє пропустити "вступ" і одразу йти в справу.

---

## Бізнес-профіль

- **Сфера:** продаж автозапчастин до іномарок
- **Власник:** Олексій (busauto.ua@gmail.com)
- **Мета власника:** **вийти з оперативки** і контролювати бізнес зі сторони
- **Команда:** 3 продавці, бухгалтер, водій, касир
- **Юридична структура:** 3 ФОПи (типова українська схема)
  - ФОП Онищенко Олена Валеріївна — основний (41% виручки за рік)
  - ФОП Грохольський Олександр Вікторович — 37%
  - ФОП Онищенко Крістіна Олегівна — 21%

---

## Канали продажів

- **busauto.ua** — нативний oDoo eCommerce. Меньшість заказів через корзину; більшість — менеджер створює після дзвінка. ⚠️ **Реклама ще не запущена** — очікує доробки кошика (UX/конверсія). Після готовності — запуск Google Ads окремим акаунтом. Слідкувати за прогресом розробника.
- **busauto.kh.ua** — на платформі **Prom**. Заказы приходять в oDoo з `source_id = "prom.ua"` (плутанина в назві — насправді це наш сайт)
- **automobil.in.ua** — на **Horoshop**. Source = "Automobil"
- **Маркетплейси:** Rozetka, Avto.pro — Александр заводить вручну, ставить source сам. Епіцентр - Максим заводить вручну, ставить source сам
- **Офлайн магазин** — фіксується в oDoo через звичайні SO (правило: якщо carrier = «Самовивіз» → source = "Магазин")

---

## Технічний стек

- **oDoo 17** з українською локалізацією (НП(С)БО, 355 рахунків плану)
  - URL: https://busauto.ua, БД: `busauto`
  - Кастомний модуль `lbs_busauto` (баг — зачасту падає `_compute_display_name` з MemoryError → обходимо мікро-пакетами 50)
  - Розробник є зовнішній — пишемо йому таски через документ, не безпосередньо
- **Horoshop** (sync через `sync_odoo_to_horoshop.py` — наш кастомний скрипт)
- **Prom API** (через oDoo)
- **Google Ads** — два активних аккаунти + один у перспективі:
  - 4884912382 → busauto.kh.ua (ROAS **5.06x**, 30д до 2026-05-27, основний донор конверсій)
  - 5691399829 → automobil.in.ua (ROAS **2.68x**, потребує оптимізації)
  - 🔜 busauto.ua — реклама НЕ запущена, очікує доробки кошика
  - **Менеджерський акаунт:** "Керуючий API" ID **2836486392** (283-648-6392) — обидва акаунти під ним
  - **OAuth:** busauto@gmail.com (власник MCC), проект `busauto-merchant` (ID: 294564314697), статус **Production** ✅ (токен безстроковий, 2026-05-28)
  - **Дані тягнуться через:** `gads_export.py` → прямий Google Ads API (не Supermetrics!)
- **Supermetrics** — ~~DEMO trial закінчився 2026-05-15~~ → **замінено прямим API**
- **PrivatBank API** — підключено для авто-узгодження банковських виписок
- **GitHub Actions** — налаштовано для daily Telegram-дайджестів (репозиторій приватний)

---

## Що вже автоматизовано

### Дашборди (HTML, на компʼютері + GitHub Actions)

- `busauto_owner_dashboard.html` — оперативний дашборд: виручка, заказы, source, ROAS, повторні клієнти, Prom UTM, ABC товарів
- `busauto_warehouse_dashboard.html` — склад Шевченко: ABC по 1858 SKU, days_left з incoming, dead stock, підвислі pickings
- `busauto_financial_dashboard.html` — P&L по 12 місяцях, структура витрат, грошова позиція, дебіторка/кредиторка, ФОПи
- `busauto_gads_dashboard.html` — Google Ads 30д: KPI-картки, порівняння акаунтів, графіки витрат/ROAS/кліків. Дані: `dashboard_data/gads_summary.json` (генерує `gads_export.py`)

### Telegram-дайджести

- **Daily** (щодня 09:00 Київ) — оперативні KPI + 3 HTML-дашборди (`digest.yml`)
  - Виручка / замовлення / AOV vs середнє за 7 днів
  - YoY, маржа, повторні клієнти
  - ROAS Google Ads **окремо по кожному акаунту** (busauto.kh.ua / automobil.in.ua)
  - Витрати Google Ads сьогодні + аномалія (±% до середнього)
  - Розбивка замовлень по каналах: Prom / Horoshop / Rozetka / Avto.pro / busauto.ua / Магазин
  - Алерти: скасування, A-клас склад, підвислі pickings, dead stock
  - **Google Ads дані:** тягнуться через `gads_export.py` (прямий API, крок в digest.yml)
- **Weekly Finance** (пн 09:00) — підсумки тижня
- **Monthly Finance** (1-го о 10:00) — повний P&L закритого місяця
- **Cashflow** (за запитом, --mode cashflow) — рух грошей
- **Accountant Daily** (пн-пт 10:00, опційно) — дебіторка, pickings, узгодження

> ✅ **Коміт e605009 (2026-05-24):** всі дайджест-файли додано в репо.
> ✅ **Коміт e5233e7 (2026-05-28):** Google Ads API підключено, обидва акаунти працюють, дашборд оновлено, GitHub Secrets додано.
> ✅ **Коміти c33cc56 + b965f8f (2026-06-02):** виправлено падіння всіх дайджестів (детальніше в розділі "GitHub Actions — відомі проблеми").

### Скрипти-помічники

- `odoo_export.py` — основний експорт операційних даних
- `finance_export.py` — фінансовий експорт (12 міс P&L + cashflow)
- `gads_export.py` — Google Ads API: тягне 30д даних по обох акаунтах → `dashboard_data/gads_summary.json`
- `check_product.py <артикул>` — діагностика конкретного товару (incoming, PO, moves)
- `check_account_balance.py <код>` — діагностика балансу рахунку (для пошуку помилок обліку)
- `check_attribution_fields.py` — розвідка полів oDoo
- `shafer_photo_import.py` — завантаження доп. фото товарів SHAFER з market.shafer.ua в Odoo (детальніше нижче)

---

## Shafer Photo Import

**Скрипт:** `shafer_photo_import.py` | **Запуск:** `ЗАПУСТИТИ_SHAFER_ФОТО.bat`

### Алгоритм (2 кроки на товар)
1. **GET** `https://market.shafer.ua/?w=base64({"s":"ARTICLE"})` → парсимо HTML, шукаємо `data-template="ARTICLE_({index}).jpg"` → витягуємо `data-prod=NNNNN` (унікальний ID товару на сайті Shafer)
2. **POST** `https://market.shafer.ua/ajax/` з `action=showGallery, id=NNNNN, market=0` (multipart/form-data) → повертає `{"gallery": {"photoId": "/images/fullimage/NNNNN_photoId_ARTICLE(N).jpg", ...}}`

### Логіка збереження в Odoo
- **Перше фото** з галереї → замінює `image_1920` (головне фото `product.template`)
- **Решта фото** → зберігаються як `product.image` (додаткові)
- Старі доп. фото очищаються перед записом (немає дублів)
- Файли що не є JPEG/PNG/GIF/WebP — пропускаються

### Важлива технічна інформація
- **Авторизація:** cookies з браузера → `shafer_cookies.json` (простий dict `{name: value}`, НЕ Playwright формат)
  - Потрібні куки: `PHPSESSID`, `uid`, `shaferOnlyNew`
  - Session check: GET `/` і перевіряємо наявність `o-user-cart` в HTML (не через `/ajax/`)
- **`/ajax/` повертає HTML** для більшості запитів — це нормально! Тільки `showGallery` повертає JSON
- **Фото розміщені на:** `https://globalavto.com.ua/images/fullimage/` (головні фото запчастин) і `https://market.shafer.ua/img/review/technotes/` (технічні рисунки)
- **Brand ID Shafer в Odoo:** 771
- **Прогрес:** `shafer_processed.txt` — список оброблених `product.template.id`
- **Режими:** `[T]` тест одного артикулу, `[R]` скинути прогрес, `[Enter]` повний запуск
- **Куки expire:** якщо каже "куки протерміновані" → відкрити Chrome → market.shafer.ua → F12 → Console → `document.cookie` → скопіювати PHPSESSID/uid/shaferOnlyNew → оновити `shafer_cookies.json`

---

## Google Merchant Center

- **Merchant ID:** 163800443
- **Скрипт:** `push_to_merchant_api.py` — завантаження товарів через Content API v2.1
- **Ключ:** `google_merchant_key.json` — НЕ комітимо (в `.gitignore`). GitHub Secret: `GOOGLE_MERCHANT_KEY_JSON`
- **Прогрес:** `merchant_api_progress.json` — НЕ комітимо (локальний кеш)

### Розклад синхронізації (GitHub Actions — `merchant_feed.yml`)
- **Щодня 10:00 Київ** — `--mode daily --days 2` → тільки змінені товари (~5 хв)
- **Щопонеділка 08:00 Київ** — `--mode full`, 3 паралельних воркери (offset 0/40k/80k) → всі ~120к товарів (~40 хв)

### Стан фіду (станом на 2026-05-24)
- **Ukrainian (uk):** ~100к товарів ✅
- **Russian (ru):** ~257 товарів (новий фід, вирівняється після найближчого full sync в понеділок)

### Логіка ціноутворення (реалізована в скрипті)
Пріоритети прайс-листа "Розниця" (ID=1):
1. `0_product_variant` — фіксована ціна на конкретний варіант
2. `1_product` — фіксована ціна на шаблон
3. `2_product_category` — categ 6=50%, categ 7=100%, categ 9=200% від ціни постачальника; categ 13=35% від standard_price
4. `3_1_brand` — брендові правила:
   - **SHAFER** (brand_id=1062): тири 0-2000=70/60/50%, 2000-4000=35%, 4000+=30%
   - **81 бренд** (incl. RAISO, AUTOTECHTEILE): тири 0-250=70%, 250-500=60%, 500-1000=50%, 1000-2000=40%, 2000-3000=30%, 3000+=25/20/15%
5. `3_global` — глобальне правило: тири 60/50/40/30/25/20/15/10%
- **Курс USD:** 44.45 UAH/USD (з `res.currency.rate`, оновлюється в Odoo)
- **Мінімальна маржа:** +100 UAH від ціни постачальника
- **Двомовність:** `DUAL_LANGUAGE = True` — кожен товар відправляється двічі (uk + ru)

### Верифіковані приклади
- `SH141.10K` (SHAFER): avg постачальників 2061-2556-2999 UAH → retail **3,427.61 UAH** ✓
- `RL-311786-KIT` (RAISO): 36.43 USD × 44.45 × 1.40 = **2,267.04 UAH** (сайт: 2,267.03) ✓

### Наступні кроки Merchant Center
- 🟡 Дочекатись full sync в понеділок — Russian фід вирівняється до ~100к товарів
- 🟡 Перевірити в Merchant Center статус товарів (чи немає disapproved)
- 🟢 Після стабілізації — запустити Google Shopping рекламу

---

## Поточні відомі проблеми

### Бухгалтерія
- **311008 (Грохольский) -238 323,88 ₴** — накопичена різниця від незведення виписок. Виправляється коригувальною проводкою + PrivatBank API далі підтримуватиме.
- **311006 (Карта ПРИВАТБАНК) -29 333,93 ₴** — законсервований рахунок з 2024 року, потребує депрекації
- **Дзебан О.О. дебіторка 617 тис ₴** — реальний борг + накопичені курсові різниці (USD-контрагент). Звести вручну.
- **Старі PO 2022-2023 років** — в системі є висячі замовлення з імпорту попередньої версії. Не впливають на incoming_qty, але засмічують Purchase-звіт.

### Атрибуція
- **54% заказів без `source_id`** до впровадження автоматизації — змішане: ручні менеджерські + busauto.ua API без розмітки
- Розробнику дані задачі (`Developer_Tasks.md`):
  - carrier_id = Самовивіз → source = Магазин (в роботі)
  - busauto.ua API: ставити source = busauto.ua при імпорті
  - Inheriting acquisition source на partner (Variant A)
  - Computed `is_repeat_customer`

### Команди продажів
- Розділені на subteam (вирішено не дробити, бо менеджери розподіляються гнучко)
- AVTOPRO (маркетплейси) і Web-Магазин (сайти) — кожен має свого основного користувача

---

## Ключові фінансові показники (12 міс, станом на 2026-05)

- **Виручка:** 17.98 млн ₴
- **COGS:** 12.00 млн (66.8%)
- **Валова маржа:** 5.98 млн (33.2%)
- **OpEx:** 3.86 млн (21.5%) — Адмін 10.4% + Збут 10.6%
- **EBIT:** 2.11 млн (**11.8% маржа**)
- **Грошова позиція:** ~1.96 млн ₴ (з урахуванням 3 негативних рахунків)
- **Дебіторка:** 1.06 млн / **Кредиторка:** 1.27 млн / **Розрив:** -207 тис

---

## Файлова структура

```
C:\Users\busau\Desktop\automobil-SEO-tools\
├─ BusAuto_Brief.md                      ← цей файл
├─ odoo_config.txt                       ← НЕ комітимо (API-ключ)
├─ telegram_config.txt                   ← НЕ комітимо
│
├─ odoo_export.py                        ← основний експорт
├─ finance_export.py                     ← фінансовий експорт
├─ build_dashboard.py                    ← збирає 3 HTML-дашборди
│
├─ daily_digest.py                       ← денний Telegram
├─ finance_digest.py                     ← фінансовий (weekly/monthly/cashflow/accountant)
│
├─ check_product.py                      ← діагностика товару
├─ check_account_balance.py              ← діагностика рахунку
│
├─ run_full_refresh.bat                  ← повний refresh локально
├─ run_odoo_export.bat                   ← тільки експорт
│
├─ gads_export.py                        ← Google Ads API → gads_summary.json
├─ busauto_gads_dashboard.html           ← Google Ads 30д дашборд (Chart.js)
├─ mcp-google-ads/
│   ├─ google-ads.yaml                   ← OAuth credentials (НЕ комітимо!)
│   └─ .venv/                            ← Python venv з google-ads lib
├─ get_token.bat                         ← перегенерація OAuth токену (busauto@gmail.com)
├─ dashboard_data/                       ← згенеровані JSON (не в git)
├─ github_workflow_files/                ← YAML для GitHub Actions
├─ goodrem_photos/, mata_photos/         ← фото для Horoshop sync
└─ .gitignore
```

---

## Аналіз юзабіліті та SEO (травень 2026)

### Що вже зроблено на сайті (сесія "Аналіз і покращення")
- ✅ Title головної: "Автозапчастини для іномарок — BusAuto | Доставка по всій Україні"
- ✅ Банер: реальний контент з марками авто та перевагами
- ✅ Фото: реальні запчастини замість шаблонного зображення
- ✅ Лічильники: 20 000 артикулів / 50+ марок / 1-2 дні / 20 років
- ✅ Футер: реальний опис магазину (замість lorem ipsum)
- ✅ Шапка: телефон +38 099 492 72 42, Харків, Пн-Пт 9-18, Сб 9-15
- ✅ Меню: Головна / Магазин / Доставка і оплата / Зв'яжіться
- ✅ Сторінка доставки — створена
- ✅ SEO Title+Description: 4 сторінки + 9 категорій
- ✅ Самовивіз 0 грн — виправлено
- ✅ Google Business Profile — вже існує! Рейтинг 4.7⭐, 26 відгуків (був знайдений, не треба створювати)
- ✅ Google Search Console — вже підключена (верифікація через DNS у Імена.ua)
- ✅ Google Analytics — вже підключений (G-05S0L457L5)
- 🔧 Локалізація (en): "Get notified when back in stock", "Save for later", "Shopping Cart" → розробнику
- 🔧 Артикул товару на картці товару — розробнику
- 🔧 Захист від продажу товарів з нульовою ціною — увімкнути: oDoo → Веб-сайт → Магазин
- 📄 Повний звіт: `BusAuto_Usability_Report.docx`

### Ключові технічні відкриття (oDoo 17)
- `ir.translation` не існує — переклади зберігаються безпосередньо в полях через `update_field_translations`
- XML-RPC заблокований ззовні (403) — працює тільки з локальної машини
- JSON-RPC через браузер (`/web/dataset/call_kw`) — завжди працює
- Категорії магазину не мають окремих URL — працюють як фільтри (важливо для SEO)

### SEO — статус і дані з Search Console
- 📄 Повний SEO аудит: `BusAuto_SEO_Audit.docx`
- ✅ Органіка вже є: **313 кліків/місяць** з Google
- ✅ 97 728 товарів з фото проіндексовано
- ⚠️ 774 000 сторінок НЕ проіндексовано — причина: сторінки фільтрів генерують дублі URL
  - 467k — "проскановано але не проіндексовано"
  - 279k — проблема canonical тегів
- ⚠️ Sitemap порожній — причина: `ir.translation` замінено в oDoo 17
- 🔧 **Головний gap**: немає landing-сторінок за марками авто (`/shop/toyota/` тощо) → розробнику
- 🔧 Заблокувати сторінки фільтрів у `robots.txt` → зменшить 774k неіндексованих
- 🔧 Виправити 279k canonical дублів → розробнику
- 🔧 Schema.org Product на картках товарів → розробнику
- 🔧 Open Graph теги, BreadcrumbList Schema, LocalBusiness Schema → розробнику

### Переклад товарів (скрипт готовий)
- З 97 728 товарів з фото тільки ~1-2% мають рос. символи (Ё/Ъ/Ы/Э) в українському полі
- Скрипт `ЗАПУСТИТИ_ПЕРЕКЛАД.bat` — готовий, ще не запущений на всіх товарах
- Claude Haiku перекладає автозапчастини зі збереженням артикулів

### Пріоритетні залишки
- 🔴 Запустити `ЗАПУСТИТИ_ПЕРЕКЛАД.bat` на всіх товарах
- 🔴 Додати фото в Google My Business + відповідати на відгуки
- 🟡 Виправити canonical дублі (279k сторінок)
- 🟡 Заблокувати фільтри в robots.txt
- 🟡 Перевірити sitemap.xml
- 🟢 SEO-описи для топ-100 товарів
- 🟢 Аналіз Search Console — які запити приводять клієнтів
- 🟢 Корпоративна пошта через Zoho ($1.25/міс)

---

## GitHub Actions — відомі проблеми і виправлення

### Падіння дайджестів (2026-06-02) — ВИРІШЕНО
**Причина:** `finance_export.py` і `odoo_export.py` читали `account.move.line` посторінково по 500 записів. Після місячного закриття травня кількість записів зросла → скрипти перевищували таймаути (15-25 хв).

**Виправлення (коміт b965f8f):**
- `fetch_lines_by_account()` замінено на `read_group` — Odoo агрегує суми на сервері, ~50-100x швидше
- `socket.setdefaulttimeout(300)` в обох скриптах — запобігає вічному зависанню
- Таймаути в workflow: `odoo_export` 15→25 хв, `finance_export` зменшено до 20 хв (тепер швидкий)

### accountant_daily.yml — ВИПРАВЛЕНО
**Причина:** workflow використовував `${{ secrets.ODOO_URL }}`, `ODOO_DATABASE`, `ODOO_USERNAME` — ці secrets не існують. Тепер хардкодить URL/DB/user як всі інші workflows.

### merchant_feed.yml — ОНОВЛЕНО
Додано Telegram-повідомлення після кожного daily sync (✅/❌ + останні рядки логу).

### Розклад workflows
| Workflow | Час (Київ) | Скрипти |
|---|---|---|
| Daily BusAuto Digest | щодня 09:00 | odoo_export → finance_export → daily_digest → dashboards |
| Weekly Finance | пн 09:00 | odoo_export → finance_export → finance_digest --mode weekly |
| Monthly Finance | 1-го 10:00 | finance_export → finance_digest --mode monthly → dashboards |
| Accountant Daily | пн-пт 10:00 | odoo_export → finance_export → finance_digest --mode accountant |
| Merchant Feed daily | щодня 10:00 | push_to_merchant_api --mode daily --days 2 |
| Merchant Feed full | пн 08:00 | push_to_merchant_api --mode full (3 паралельних воркери) |

---

## Зроблено в паралельних чатах

- **Compliance план**: `BusAuto_Compliance_Growth_Plan.docx` — матриця ризиків (9 позицій), план виправлення бухгалтерії (311008, 311006), ФОП-ліміти, Держпраця
- **Єпіцентр**: 82 343 товари вивантажено в XML-фід. Google Drive авто-оновлення — в процесі налаштування
- **Google Ads аналіз** (квіт–трав 2026): busauto.kh.ua ROAS 4.72 / CPA 94 ₴ (добре), automobil.in.ua ROAS 2.13 / CPA 206 ₴ (потребує оптимізації). Загальний бюджет ~103 тис ₴/міс. Проблема: дублювання PMax SHAFER між акаунтами — одну треба поставити на паузу
- **busauto.ua — підготовка до реклами**: SEO-оптимізація зроблена (title, description, банер, фото, лічильники, футер). Блокер для запуску Google Ads — кошик сайту (UX не доопрацьований). Після готовності кошика — запуск окремої Google Ads кампанії на busauto.ua
- **Google Ads API (2026-05-28):** Supermetrics замінено прямим Google Ads API. `gads_export.py` — новий скрипт. OAuth налаштовано через проект `busauto-merchant`. Акаунт busauto.kh.ua прив'язано до MCC "Керуючий API" (2836486392). GitHub Secrets додано (5 шт: GADS_DEVELOPER_TOKEN, GADS_CLIENT_ID, GADS_CLIENT_SECRET, GADS_REFRESH_TOKEN, GADS_LOGIN_CUSTOMER_ID). Свіжі ROAS: busauto.kh.ua 5.06x / automobil.in.ua 2.68x (30д, квіт–трав 2026).

---

## Як вести розмову зі мною (Claude)

- **На початку нового чату** — посилайтесь на цей файл або скопіюйте його зміст: «Контекст у `BusAuto_Brief.md`»
- **Тема** — зразу скажіть, чого стосується розмова: «Бухгалтерія / Команда / Маркетинг / Склад / Дашборди»
- **Якщо щось вирішили** — попросіть оновити цей brief

Я звертатимусь до файлу і не буду перепитувати про базові речі.
