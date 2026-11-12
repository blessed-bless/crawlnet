# crawlnet

Распределённый краулер на asyncio. Общая очередь лежит в Redis: воркеров можно запускать сколько угодно и на разных машинах, они сами делят работу. Что и как парсить, описывается в YAML без кода. Результаты сохраняются в PostgreSQL или SQLite, выгружаются в CSV и Excel. Есть веб-дашборд и Telegram-бот.

```
           ┌────────────┐   submit    ┌──────────────────────── Redis ───────────────────────┐
 CLI / бот │  дашборд   │ ──────────► │ frontier (ZSET по времени)   seen:{job} (SET, дедуп)  │
           └────────────┘             │ inflight (ZSET, дедлайны)    slot:{host} (вежливость) │
                                      │ job:{id} (счётчики)          dead:{job}, robots:{…}   │
                                      └───────────▲───────────────────────────▲───────────────┘
                                         pop/ack  │                           │  pop/ack
                                      ┌───────────┴──────┐          ┌─────────┴────────┐
                                      │ worker × N слотов│   ...    │ worker × N слотов│
                                      └───────────┬──────┘          └─────────┬────────┘
                                                  └──────── upsert ───────────┴──► PostgreSQL
```

## Что внутри

- **Общая очередь.** `ZSET`, где score — время, когда запрос можно брать. На этом же механизме работают отложенные ретраи, вежливые паузы и приоритеты: приоритетный запрос получает score чуть в прошлом.
- **At-least-once.** Взятый запрос попадает в `inflight` с дедлайном. Если воркер упал, reaper в любом живом воркере вернёт запрос в очередь. Повторный `ack` от медленного воркера не ломает счётчики.
- **Вежливость между машинами.** `SET slot:{host} NX PX delay` пускает к домену не больше одного запроса за интервал на весь кластер. Учитывается `Crawl-delay` из robots.txt.
- **robots.txt** кэшируется в Redis на час, его разбор хранится в памяти процесса. На 401/403 сайт считается закрытым, как требует RFC 9309.
- **Ретраи** на 408/425/429/5xx и сетевые ошибки: экспоненциальная задержка с джиттером, учитывается `Retry-After`. Запросы, у которых кончились попытки, уходят в `dead:{job}`.
- **Дедупликация.** URL сначала канонизируется: регистр, порт по умолчанию, сортировка query, без `utm_*` и `#фрагмента`. Затем его SHA-1 кладётся в SET задачи.
- **Upsert и история изменений.** Записи уникальны по `(spider, key)`. Если при повторном обходе данные поменялись, растёт `versions`. Так видно, например, у каких товаров изменилась цена.
- **Завершение задачи без координатора.** Счётчик `pending` растёт при постановке и уменьшается при `ack`. Потомки ставятся в очередь раньше, чем подтверждается родитель, поэтому ноль означает, что работа действительно закончена.

## Спайдер на YAML

```yaml
name: books
allowed_domains: [books.toscrape.com]
start: [{url: "https://books.toscrape.com/", page: catalog}]
settings: {delay: 0.2, max_pages: 1100, max_depth: 60}

pages:
  catalog:
    follow:
      - {css: "article.product_pod h3 a::attr(href)", page: book, priority: 1}
      - {css: "li.next a::attr(href)", page: catalog}
  book:
    item:
      key: upc                      # по нему upsert
      required: [title, upc]        # без них запись считается ошибкой
      fields:
        title:  {css: "div.product_main h1::text"}
        upc:    {xpath: "//th[text()='UPC']/following-sibling::td/text()"}
        price:  {css: "p.price_color::text", regex: "[\\d.]+", type: float}
        rating: {css: "p.star-rating::attr(class)", regex: "star-rating (\\w+)",
                 map: {One: 1, Two: 2, Three: 3, Four: 4, Five: 5}, type: int}
```

Опции поля: `css` или `xpath`, `regex` (берётся первая группа, если она есть), `type` (`str`, `int`, `float`, `bool`), `many`, `join`, `map`, `default`. Если на странице несколько записей, укажите `each: "<css блока>"`, и поля будут искаться внутри каждого блока (см. [spiders/quotes.yaml](spiders/quotes.yaml)).

## Запуск

Без инфраструктуры. Очередь живёт в памяти, данные пишутся в SQLite:

```bash
pip install -e ".[dev]"
crawlnet run quotes --export xlsx
```

Кластер в Docker: Redis, Postgres, 3 воркера и дашборд на http://localhost:8000. Дашборд закрыт паролем: задайте `CRAWLNET_DASHBOARD_PASSWORD` в `.env`, без него контейнер дашборда не стартует.

```bash
docker compose up -d --build
docker compose exec worker crawlnet submit books
docker compose up -d --scale worker=6          # добавить воркеров на лету
docker compose --profile bot up -d             # плюс Telegram-бот (нужен CRAWLNET_TG_TOKEN в .env)
```

Команды CLI:

| команда | что делает |
|---|---|
| `crawlnet submit <spider>` | поставить задачу в общую очередь |
| `crawlnet worker -c 16` | запустить воркер с 16 параллельными запросами |
| `crawlnet jobs` / `cancel <id>` | список задач и отмена |
| `crawlnet export <id> -f csv\|xlsx` | выгрузить результаты |
| `crawlnet validate <file.yaml>` | проверить спайдер |
| `crawlnet dashboard` / `bot` | веб-интерфейс и Telegram-бот |

Бот понимает команды `/spiders` (кнопки запуска), `/run books`, `/jobs`, `/status <id>`. В ответах есть кнопки «CSV», «Excel» и «Отменить». Когда задача заканчивается, бот присылает сообщение. Доступ только у пользователей из `CRAWLNET_TG_ALLOWED`; без этого списка бот не запустится.

## Тесты

```bash
pytest
```

Redis подменяется fakeredis, сайт — respx: фейковый магазин с дублями, внешними ссылками, закрытым robots-путём, ответами 503 и 404 и страницей без нужных полей. Тесты проверяют дедуп, лимиты, отложенные запросы, reaper и двойной ack, вежливость, отмену, полный обход несколькими воркерами, версии записей при повторном обходе, экспорт и API дашборда.

## Ограничения

- Только статический HTML. Для страниц, которые рендерятся на JS, нужен отдельный fetcher на Playwright.
- `seen:{job}` — обычный SET. Для сотен миллионов URL его стоит заменить на Bloom-фильтр (RedisBloom).
- `ZPOPMIN` и запись в `inflight` — две отдельные команды. Если воркер упадёт ровно между ними, запрос потеряется. Это закрывается Lua-скриптом.
