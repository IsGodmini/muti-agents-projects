from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from app.models.schemas import (
    ItineraryDay,
    ItineraryEvent,
    PlanRequest,
    ProductType,
    Quote,
    QuoteItem,
)
from app.services.pdf_report import build_pdf_report
from app.services.renderer import render_markdown_report


def _request() -> PlanRequest:
    return PlanRequest(
        title="杭州两天亲子研学之旅",
        product_type=ProductType.FAMILY,
        destination="杭州",
        days=2,
        nights=1,
        group_size=4,
        budget_per_person=1800,
        target_audience="亲子家庭",
        themes=["自然教育"],
    )


def _itinerary() -> list[ItineraryDay]:
    return [
        ItineraryDay(
            day=day,
            theme=f"自然探索第 {day} 天",
            events=[
                ItineraryEvent(
                    start_time="09:00",
                    end_time="11:30",
                    title=f"西湖研学活动 {day}",
                    resource_id=f"res-{day}",
                    category="activity",
                    description="观察湖区生态并完成自然笔记。",
                ),
                ItineraryEvent(
                    start_time="11:30",
                    end_time="12:30",
                    title="午餐",
                    category="dining",
                    description="团队午餐。",
                    cost_per_person=50,
                ),
            ],
        )
        for day in (1, 2)
    ]


def _quote() -> Quote:
    return Quote(
        items=[QuoteItem(category="交通", description="团队大巴", amount=1200)],
        total_cost=1200,
        cost_per_person=300,
        sale_price_per_person=353,
        expected_revenue=1412,
        expected_profit=212,
        margin_rate=0.15,
    )


def _make_image(path: Path, color: tuple[int, int, int]) -> str:
    Image.new("RGB", (640, 480), color).save(path)
    return str(path)


def test_vector_pdf_is_concise_and_includes_cover_and_day_images(tmp_path: Path) -> None:
    cover = _make_image(tmp_path / "cover.png", (30, 80, 110))
    day1 = _make_image(tmp_path / "day1.png", (80, 130, 90))
    day2 = _make_image(tmp_path / "day2.png", (150, 110, 70))
    output = tmp_path / "report.pdf"

    build_pdf_report(
        request=_request(),
        itinerary=_itinerary(),
        poster_path=cover,
        day_image_paths=[[day1], [day2]],
        output_path=str(output),
        quote=_quote(),
        weather_forecast=[{
            "date": "2026-08-01",
            "text_day": "晴",
            "temp_min": 26,
            "temp_max": 34,
            "provider": "amap",
        }],
    )

    reader = PdfReader(output)
    assert len(reader.pages) == 3
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "杭州两天亲子研学之旅" in extracted
    assert "DAY 1" in extracted
    assert "DAY 2" in extracted
    assert "报价与出行提醒" in extracted

    for page in reader.pages[:2]:
        resources = page["/Resources"]
        assert "/Font" in resources
        xobjects = resources.get("/XObject", {})
        images = [
            item.get_object()
            for item in xobjects.values()
            if item.get_object().get("/Subtype") == "/Image"
        ]
        assert images


def test_markdown_is_detailed_text_only_and_explains_costs(tmp_path: Path) -> None:
    day1 = str(tmp_path / "day1.png")
    day2 = str(tmp_path / "day2.png")

    markdown = render_markdown_report(
        request=_request(),
        itinerary=_itinerary(),
        day_image_paths=[[day1], [day2]],
    )

    assert "![" not in markdown
    assert day1 not in markdown
    assert day2 not in markdown
    assert "## 二、费用口径说明" in markdown
    assert "## 四、详细分日行程" in markdown
    assert "¥50/人（估算）" in markdown
