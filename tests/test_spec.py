import pytest
from parsel import Selector
from pydantic import ValidationError

from crawlnet.spec import FieldSpec, ItemSpec, SpiderSpec, extract_field, extract_items
from crawlnet.urls import canonicalize


@pytest.mark.parametrize(
    ("raw", "base", "expected"),
    [
        ("HTTPS://Example.COM:443/a?b=2&a=1#frag", None, "https://example.com/a?a=1&b=2"),
        ("/x?utm_source=tg&id=5", "https://ex.com/y/", "https://ex.com/x?id=5"),
        ("../c", "https://ex.com/a/b/", "https://ex.com/a/c"),
        ("http://ex.com:8080", None, "http://ex.com:8080/"),
        ("mailto:me@ex.com", None, None),
        ("javascript:void(0)", None, None),
    ],
)
def test_canonicalize(raw, base, expected):
    assert canonicalize(raw, base) == expected


SEL = Selector(text='<p class="p">Цена: 12 345,50 ₽</p><p class="r">star-rating Four</p><a>x</a><a>y</a>')


def test_field_regex_and_types():
    assert extract_field(SEL, FieldSpec(css=".p::text", regex=r"[\d\s,]+\d", type="float")) == 12345.5
    assert extract_field(SEL, FieldSpec(css=".r::text", regex=r"star-rating (\w+)", map={"Four": 4}, type="int")) == 4
    assert extract_field(SEL, FieldSpec(css="a::text", many=True)) == ["x", "y"]
    assert extract_field(SEL, FieldSpec(css="a::text", join="|")) == "x|y"
    assert extract_field(SEL, FieldSpec(css=".none::text", default="—")) == "—"


def test_each_and_required():
    sel = Selector(text='<div class="q"><b>1</b><i>a</i></div><div class="q"><i>b</i></div><div class="q"><b>3</b></div>')
    spec = ItemSpec(each="div.q", key="n", required=["n"], fields={"n": FieldSpec(css="b::text", type="int"), "t": FieldSpec(css="i::text")})
    items, bad = extract_items(sel, spec)
    assert items == [{"n": 1, "t": "a"}, {"n": 3, "t": None}]
    assert bad == 1


def test_spec_validation_errors(spec):
    raw = spec.model_dump()
    raw["start"][0]["page"] = "nope"
    with pytest.raises(ValidationError, match="неизвестные типы страниц"):
        SpiderSpec.model_validate(raw)
    with pytest.raises(ValidationError, match="ровно один"):
        FieldSpec(css="a", xpath="//a")
    with pytest.raises(ValidationError, match="нужен key"):
        ItemSpec(each="div", fields={"a": FieldSpec(css="a")})


def test_domain_allowed(spec):
    assert spec.domain_allowed("shop.test")
    assert spec.domain_allowed("img.shop.test")
    assert not spec.domain_allowed("evilshop.test")


def test_bundled_spiders_are_valid():
    from pathlib import Path

    for p in Path("spiders").glob("*.yaml"):
        SpiderSpec.from_file(p)
