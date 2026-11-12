import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis

from crawlnet.frontier import Frontier
from crawlnet.spec import SpiderSpec
from crawlnet.storage import Storage

SHOP = "https://shop.test"

SPEC_YAML = """
name: shop
allowed_domains: [shop.test]
start: [{url: "https://shop.test/", page: list}]
settings: {delay: 0, max_retries: 2}
pages:
  list:
    follow:
      - {css: "a.item::attr(href)", page: item}
      - {css: "a.next::attr(href)", page: list}
  item:
    item:
      key: sku
      required: [sku, title]
      fields:
        sku:   {css: ".sku::text"}
        title: {css: "h1::text"}
        price: {css: ".price::text", regex: "[\\\\d ]+", type: int}
        tags:  {css: ".tag::text", many: true}
"""


def page(body: str) -> httpx.Response:
    return httpx.Response(200, html=f"<html><body>{body}</body></html>")


def item_html(sku: str, title: str, price: str = "1 990 ₽") -> str:
    return f'<h1>{title}</h1><span class="sku">{sku}</span><b class="price">{price}</b><i class="tag">a</i><i class="tag">b</i>'


@pytest.fixture
def spec() -> SpiderSpec:
    return SpiderSpec.from_yaml(SPEC_YAML)


@pytest.fixture
async def redis():
    r = FakeAsyncRedis()
    yield r
    await r.aclose()


@pytest.fixture
def frontier(redis) -> Frontier:
    return Frontier(redis, prefix="t", inflight_timeout=5)


@pytest.fixture
async def storage(tmp_path):
    s = Storage(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def shop():
    """Маленький магазин: 2 страницы каталога, 4 товара, битая ссылка, внешняя ссылка, 503 и 404."""
    with respx.mock(base_url=SHOP, assert_all_called=False) as mock:
        mock.get("/robots.txt").respond(200, text="User-agent: *\nDisallow: /private/\n")
        mock.get("/", params={"page": "2"}).mock(return_value=page(
            '<a class="item" href="/p/3">3</a><a class="item" href="/p/404">404</a><a class="item" href="/p/bad">bad</a>'
        ))
        mock.get("/").mock(return_value=page(
            '<a class="item" href="/p/1">1</a><a class="item" href="/p/2?utm_source=x">2</a>'
            '<a class="item" href="/p/2">2 dup</a><a class="item" href="https://evil.test/p/9">ext</a>'
            '<a class="item" href="/private/secret">no</a><a class="next" href="/?page=2">next</a>'
        ))
        mock.get("/p/1").mock(return_value=page(item_html("A-1", "Чайник")))
        mock.get("/p/2").mock(return_value=page(item_html("A-2", "Кружка", "350 ₽")))
        # первый запрос — 503, второй — успех: проверяем ретраи
        mock.get("/p/3").mock(side_effect=[httpx.Response(503, headers={"Retry-After": "0"}), page(item_html("A-3", "Ложка"))])
        mock.get("/p/404").respond(404)
        mock.get("/p/bad").mock(return_value=page("<h1>без артикула</h1>"))
        yield mock
