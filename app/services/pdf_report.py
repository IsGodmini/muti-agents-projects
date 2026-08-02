"""Vector-first PDF travel brochure generator.

Text, tables, lines, panels, headers, and footers are emitted as native PDF
operators through ReportLab. Generated travel artwork remains raster content
embedded at its original resolution, which is the expected PDF representation
for PNG/JPEG imagery.
"""
from __future__ import annotations

import logging
from math import ceil
from pathlib import Path
from typing import Any

from PIL import Image
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from app.models.schemas import ItineraryDay, PlanRequest, Quote

logger = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
MARGIN = 34
HEADER_HEIGHT = 62
FOOTER_HEIGHT = 28

NAVY = HexColor("#183A5E")
NAVY_LIGHT = HexColor("#38668F")
GOLD = HexColor("#D4AF37")
PAPER = HexColor("#F4F6F8")
INK = HexColor("#2D343E")
GRAY = HexColor("#7A828C")
LIGHT_LINE = HexColor("#DEE2E8")
ROW_ALT = HexColor("#ECF0F4")

FONT_SEARCH_PATHS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
)


def _register_font() -> str:
    name = "TripOpsCJK"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    for path in FONT_SEARCH_PATHS:
        if not Path(path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:
            logger.debug("Unable to register PDF font %s", path, exc_info=True)
    fallback = "STSong-Light"
    if fallback not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(fallback))
    return fallback


def _wrap_text(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Wrap mixed CJK/Latin text by actual PDF glyph width."""
    lines: list[str] = []
    for raw in str(text).splitlines() or [""]:
        current = ""
        for char in raw:
            candidate = current + char
            if current and pdfmetrics.stringWidth(candidate, font, size) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return lines


def _fit_text(text: str, font: str, size: float, max_width: float) -> str:
    if pdfmetrics.stringWidth(text, font, size) <= max_width:
        return text
    ellipsis = "..."
    value = text
    while value and pdfmetrics.stringWidth(value + ellipsis, font, size) > max_width:
        value = value[:-1]
    return value + ellipsis


def _draw_cover_image(
    canvas: Canvas,
    path: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> bool:
    """Crop an embedded image with a PDF clipping path, without rasterizing the page."""
    source = Path(path)
    if not source.exists():
        return False
    with Image.open(source) as image:
        image_width, image_height = image.size
    scale = max(width / image_width, height / image_height)
    draw_width = image_width * scale
    draw_height = image_height * scale
    draw_x = x + (width - draw_width) / 2
    draw_y = y + (height - draw_height) / 2

    canvas.saveState()
    clip = canvas.beginPath()
    clip.rect(x, y, width, height)
    canvas.clipPath(clip, stroke=0, fill=0)
    canvas.drawImage(
        ImageReader(str(source)),
        draw_x,
        draw_y,
        draw_width,
        draw_height,
        preserveAspectRatio=True,
        mask="auto",
    )
    canvas.restoreState()
    return True


def _draw_footer(canvas: Canvas, font: str, page_number: int, total_pages: int) -> None:
    canvas.setStrokeColor(LIGHT_LINE)
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, FOOTER_HEIGHT + 7, PAGE_WIDTH - MARGIN, FOOTER_HEIGHT + 7)
    canvas.setFont(font, 7.5)
    canvas.setFillColor(GRAY)
    canvas.drawString(MARGIN, 17, "* 部分数据为 AI 估算，实际以官方/供应商渠道为准 *")
    canvas.drawRightString(PAGE_WIDTH - MARGIN, 17, f"{page_number} / {total_pages}")


def _draw_header(canvas: Canvas, font: str, title: str, subtitle: str = "") -> None:
    canvas.setFillColor(GOLD)
    canvas.rect(0, PAGE_HEIGHT - 6, PAGE_WIDTH, 6, stroke=0, fill=1)
    canvas.setFillColor(NAVY)
    canvas.rect(0, PAGE_HEIGHT - HEADER_HEIGHT, PAGE_WIDTH, HEADER_HEIGHT - 6, stroke=0, fill=1)
    canvas.setFillColor(white)
    canvas.setFont(font, 23)
    canvas.drawString(MARGIN, PAGE_HEIGHT - 43, title)
    if subtitle:
        canvas.setFillColor(HexColor("#CDD8E4"))
        canvas.setFont(font, 10)
        canvas.drawRightString(PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 41, subtitle)


def _draw_cover(canvas: Canvas, font: str, request: PlanRequest, poster_path: str | None) -> None:
    canvas.setFillColor(NAVY)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
    image_width = PAGE_WIDTH * 0.61
    if not poster_path or not _draw_cover_image(
        canvas, poster_path, 0, 0, image_width, PAGE_HEIGHT
    ):
        canvas.setFillColor(NAVY_LIGHT)
        canvas.rect(0, 0, image_width, PAGE_HEIGHT, stroke=0, fill=1)

    panel_x = image_width
    canvas.setFillColor(white)
    canvas.rect(panel_x, 0, PAGE_WIDTH - panel_x, PAGE_HEIGHT, stroke=0, fill=1)
    x = panel_x + 34
    content_width = PAGE_WIDTH - x - 30
    y = PAGE_HEIGHT - 78

    canvas.setFillColor(GOLD)
    canvas.setFont(font, 12)
    canvas.drawString(x, y, "TRIP PLANNER")
    y -= 35
    canvas.setFillColor(INK)
    canvas.setFont(font, 24)
    for line in _wrap_text(request.title, font, 24, content_width)[:4]:
        canvas.drawString(x, y, line)
        y -= 31
    y -= 8

    items: list[tuple[str, str]] = [
        ("目的地", request.destination),
        ("周期", f"{request.days} 天 {request.nights} 晚"),
        ("人数", f"{request.group_size} 人"),
        ("客群", request.target_audience),
        ("主题", " / ".join(request.themes) or "综合"),
    ]
    departure_text = (
        request.departure_date.strftime("%Y-%m-%d")
        if request.departure_date
        else request.departure_time_note
    )
    if departure_text:
        items.insert(1, ("出发时间", departure_text))
    for label, value in items:
        canvas.setFillColor(GOLD)
        canvas.setFont(font, 9)
        canvas.drawString(x, y, label)
        y -= 16
        canvas.setFillColor(INK)
        canvas.setFont(font, 12)
        canvas.drawString(x, y, _fit_text(value, font, 12, content_width))
        y -= 29

    canvas.setStrokeColor(LIGHT_LINE)
    canvas.line(x, 54, PAGE_WIDTH - 30, 54)
    canvas.setFillColor(GRAY)
    canvas.setFont(font, 8)
    canvas.drawString(x, 36, "TripOps AI - 智能旅行策划")


def _draw_image_grid(
    canvas: Canvas, paths: list[str], x: float, y: float, width: float, height: float
) -> None:
    available = [path for path in paths if path and Path(path).exists()]
    if not available:
        canvas.setFillColor(NAVY_LIGHT)
        canvas.roundRect(x, y, width, height, 10, stroke=0, fill=1)
        canvas.setFillColor(white)
        canvas.setFont(_register_font(), 12)
        canvas.drawCentredString(x + width / 2, y + height / 2, "暂无图片")
        return
    if len(available) == 1:
        _draw_cover_image(canvas, available[0], x, y, width, height)
        return
    columns = min(3, len(available))
    cell_width = width / columns
    for index, path in enumerate(available[:columns]):
        cell_x = x + index * cell_width
        _draw_cover_image(canvas, path, cell_x, y, cell_width, height)
        if index:
            canvas.setStrokeColor(white)
            canvas.setLineWidth(2)
            canvas.line(cell_x, y, cell_x, y + height)


def _draw_day(
    canvas: Canvas,
    font: str,
    day: ItineraryDay,
    images: list[str],
    day_date: str,
) -> None:
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
    subtitle = f"{day_date} | {day.theme}" if day_date else day.theme
    _draw_header(canvas, font, f"DAY {day.day}", subtitle)

    image_y = 276
    image_height = PAGE_HEIGHT - HEADER_HEIGHT - image_y - 12
    _draw_image_grid(canvas, images, MARGIN, image_y, PAGE_WIDTH - 2 * MARGIN, image_height)

    panel_y = FOOTER_HEIGHT + 18
    panel_height = image_y - panel_y - 12
    canvas.setFillColor(white)
    canvas.setStrokeColor(LIGHT_LINE)
    canvas.setLineWidth(0.7)
    canvas.roundRect(
        MARGIN, panel_y, PAGE_WIDTH - 2 * MARGIN, panel_height, 10, stroke=1, fill=1
    )

    events = day.events
    if not events:
        canvas.setFillColor(GRAY)
        canvas.setFont(font, 12)
        canvas.drawString(MARGIN + 20, panel_y + panel_height - 30, "当日暂无行程")
        return
    inner_height = panel_height - 26
    row_height = inner_height / len(events)
    title_size = 10 if row_height >= 22 else 8.5
    time_size = 8.5 if row_height >= 20 else 7.5
    description_size = 7.5
    top = panel_y + panel_height - 14
    cost_width = 72
    time_width = 92

    for index, event in enumerate(events):
        row_top = top - index * row_height
        row_bottom = row_top - row_height
        if index:
            canvas.setStrokeColor(LIGHT_LINE)
            canvas.setLineWidth(0.35)
            canvas.line(MARGIN + 16, row_top, PAGE_WIDTH - MARGIN - 16, row_top)
        dot_y = row_bottom + row_height / 2
        canvas.setFillColor(GOLD)
        canvas.circle(MARGIN + 22, dot_y, 2.8, stroke=0, fill=1)
        canvas.setFont(font, time_size)
        canvas.drawString(
            MARGIN + 31,
            dot_y - time_size / 3,
            f"{event.start_time} - {event.end_time}",
        )

        title_x = MARGIN + 31 + time_width
        title_width = PAGE_WIDTH - MARGIN - cost_width - title_x
        canvas.setFillColor(INK)
        canvas.setFont(font, title_size)
        title_y = dot_y - title_size / 3
        if row_height >= 34 and event.description and event.category not in ("logistics", "break"):
            title_y = dot_y + 3
        canvas.drawString(
            title_x, title_y, _fit_text(event.title, font, title_size, title_width)
        )
        if row_height >= 34 and event.description and event.category not in ("logistics", "break"):
            canvas.setFillColor(GRAY)
            canvas.setFont(font, description_size)
            description = _fit_text(
                event.description, font, description_size, title_width
            )
            canvas.drawString(title_x, dot_y - 10, description)

        canvas.setFillColor(GRAY)
        canvas.setFont(font, 8)
        cost = f"CNY {event.cost_per_person}" if event.cost_per_person else "-"
        canvas.drawRightString(PAGE_WIDTH - MARGIN - 20, dot_y - 3, cost)


def _draw_summary(
    canvas: Canvas,
    font: str,
    request: PlanRequest,
    quote: Quote | None,
    weather_forecast: list[dict[str, Any]] | None,
) -> None:
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
    _draw_header(canvas, font, "方案总结")
    x = MARGIN
    top = PAGE_HEIGHT - HEADER_HEIGHT - 24

    canvas.setFillColor(NAVY)
    canvas.setFont(font, 16)
    canvas.drawString(x, top, "预算明细（估算）")
    table_top = top - 24
    table_width = PAGE_WIDTH - 2 * MARGIN
    row_height = 27
    canvas.setFillColor(NAVY)
    canvas.rect(x, table_top - row_height, table_width, row_height, stroke=0, fill=1)
    canvas.setFillColor(white)
    canvas.setFont(font, 9)
    canvas.drawString(x + 12, table_top - 18, "类别")
    canvas.drawString(x + 220, table_top - 18, "说明")
    canvas.drawRightString(x + table_width - 12, table_top - 18, "金额")

    cursor = table_top - row_height
    items = quote.items if quote else []
    for index, item in enumerate(items):
        cursor -= row_height
        canvas.setFillColor(white if index % 2 == 0 else ROW_ALT)
        canvas.rect(x, cursor, table_width, row_height, stroke=0, fill=1)
        canvas.setFillColor(INK)
        canvas.setFont(font, 9)
        canvas.drawString(x + 12, cursor + 9, item.category)
        canvas.drawString(
            x + 220,
            cursor + 9,
            _fit_text(item.description, font, 9, table_width - 360),
        )
        canvas.drawRightString(x + table_width - 12, cursor + 9, f"CNY {item.amount:,}")

    cursor -= row_height
    canvas.setFillColor(GOLD)
    canvas.rect(x, cursor, table_width, row_height, stroke=0, fill=1)
    canvas.setFillColor(INK)
    canvas.setFont(font, 10)
    canvas.drawString(x + 12, cursor + 8, "合计")
    total = quote.total_cost if quote else 0
    canvas.drawRightString(x + table_width - 12, cursor + 8, f"CNY {total:,}")

    if quote:
        cursor -= 30
        canvas.setFont(font, 10)
        canvas.drawString(
            x,
            cursor + 8,
            f"人均成本 CNY {quote.cost_per_person:,}  |  "
            f"人均售价 CNY {quote.sale_price_per_person:,}  |  "
            f"毛利率 {quote.margin_rate:.1%}",
        )

    cursor -= 30
    canvas.setFillColor(NAVY)
    canvas.setFont(font, 16)
    canvas.drawString(x, cursor, "数据来源说明")
    cursor -= 22
    weather_providers = {
        str(day.get("provider", "qweather")) for day in (weather_forecast or [])
    }
    provider_labels = {"qweather": "和风天气", "amap": "高德天气"}
    weather_source = " + ".join(
        provider_labels.get(provider, provider) for provider in sorted(weather_providers)
    ) or "未获取"
    notes = [
        "景点/攻略：Tavily 实时网页搜索 + 高德 POI（真实检索）",
        f"天气：{weather_source}官方预报",
        "景点票价/建议时长/开放时间：估算数据，需官方确认",
        "景点间交通时间：高德实测 + 模型估算兜底",
        "成本明细：模型市场估价，以供应商报价为准",
        "行程编排与活动描述：AI 生成，仅供参考",
        "路线优化/约束校验/计价：确定性计算",
    ]
    canvas.setFillColor(GRAY)
    canvas.setFont(font, 9)
    for note in notes:
        canvas.drawString(x + 8, cursor, f"- {note}")
        cursor -= 20

    if weather_forecast:
        box_x = PAGE_WIDTH - MARGIN - 235
        box_y = FOOTER_HEIGHT + 28
        box_width = 235
        box_height = min(112, 30 + len(weather_forecast) * 20)
        canvas.setFillColor(white)
        canvas.setStrokeColor(LIGHT_LINE)
        canvas.roundRect(box_x, box_y, box_width, box_height, 9, stroke=1, fill=1)
        canvas.setFillColor(NAVY)
        canvas.setFont(font, 10)
        canvas.drawString(box_x + 12, box_y + box_height - 20, "行程天气")
        weather_y = box_y + box_height - 40
        canvas.setFont(font, 8)
        for day in weather_forecast[:4]:
            text = (
                f"{day.get('date', '?')}  {day.get('text_day', '未知')}  "
                f"{day.get('temp_min', '?')} - {day.get('temp_max', '?')} C"
            )
            canvas.drawString(box_x + 12, weather_y, text)
            weather_y -= 18


def _core_event_titles(day: ItineraryDay) -> list[str]:
    excluded = {
        "transport", "transportation", "logistics", "transfer", "pickup", "break", "rest",
        "交通", "集合", "休息", "休整",
    }
    titles = [event.title for event in day.events if event.category.strip().lower() not in excluded]
    if not titles:
        titles = [event.title for event in day.events]
    return titles[:3]


def _draw_brief_itinerary_page(
    canvas: Canvas,
    font: str,
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    quote: Quote | None,
) -> None:
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
    _draw_header(canvas, font, request.title, "精简行程版")

    x = MARGIN
    content_width = PAGE_WIDTH - 2 * MARGIN
    top = PAGE_HEIGHT - HEADER_HEIGHT - 20
    canvas.setFillColor(INK)
    canvas.setFont(font, 11)
    intro = (
        f"{request.destination}  |  {request.days} 天 {request.nights} 晚  |  "
        f"{request.group_size} 人  |  {request.target_audience}"
    )
    canvas.drawString(x, top, _fit_text(intro, font, 11, content_width))

    card_y = top - 70
    gap = 10
    card_width = (content_width - gap * 3) / 4
    cards = [
        (
            "出发时间",
            request.departure_date.strftime("%Y-%m-%d")
            if request.departure_date
            else request.departure_time_note or "待定",
        ),
        ("行程节奏", {"intense": "紧凑", "moderate": "适中", "relaxed": "舒缓"}.get(str(request.pace), str(request.pace))),
        ("人均预算", f"CNY {request.budget_per_person:,}"),
        ("建议售价", f"CNY {quote.sale_price_per_person:,}" if quote else "待报价"),
    ]
    for index, (label, value) in enumerate(cards):
        card_x = x + index * (card_width + gap)
        canvas.setFillColor(white)
        canvas.setStrokeColor(LIGHT_LINE)
        canvas.roundRect(card_x, card_y, card_width, 52, 7, stroke=1, fill=1)
        canvas.setFillColor(GRAY)
        canvas.setFont(font, 8)
        canvas.drawString(card_x + 12, card_y + 34, label)
        canvas.setFillColor(NAVY)
        canvas.setFont(font, 12)
        canvas.drawString(card_x + 12, card_y + 14, _fit_text(value, font, 12, card_width - 24))

    heading_y = card_y - 27
    canvas.setFillColor(NAVY)
    canvas.setFont(font, 15)
    canvas.drawString(x, heading_y, "分日行程概览")

    rows = itinerary[: request.days]
    table_top = heading_y - 14
    available_height = table_top - FOOTER_HEIGHT - 26
    row_height = min(58, available_height / max(len(rows), 1))
    for index, day in enumerate(rows):
        row_top = table_top - index * row_height
        row_bottom = row_top - row_height + 3
        canvas.setFillColor(white if index % 2 == 0 else ROW_ALT)
        canvas.roundRect(x, row_bottom, content_width, row_height - 4, 6, stroke=0, fill=1)

        badge_width = 72
        canvas.setFillColor(NAVY if index % 2 == 0 else NAVY_LIGHT)
        canvas.roundRect(x, row_bottom, badge_width, row_height - 4, 6, stroke=0, fill=1)
        canvas.setFillColor(white)
        canvas.setFont(font, 12)
        canvas.drawCentredString(x + badge_width / 2, row_bottom + row_height / 2 - 6, f"DAY {day.day}")

        text_x = x + badge_width + 14
        text_width = content_width - badge_width - 118
        canvas.setFillColor(INK)
        canvas.setFont(font, 10.5)
        canvas.drawString(text_x, row_bottom + row_height - 21, _fit_text(day.theme, font, 10.5, text_width))
        canvas.setFillColor(GRAY)
        canvas.setFont(font, 8.5)
        summary = "  /  ".join(_core_event_titles(day)) or "自由安排"
        canvas.drawString(text_x, row_bottom + 14, _fit_text(summary, font, 8.5, text_width))

        time_text = ""
        if day.events:
            time_text = f"{day.events[0].start_time}-{day.events[-1].end_time}"
        canvas.setFillColor(NAVY_LIGHT)
        canvas.setFont(font, 8.5)
        canvas.drawRightString(x + content_width - 14, row_bottom + row_height / 2 - 4, time_text)


def _draw_brief_details_page(
    canvas: Canvas,
    font: str,
    request: PlanRequest,
    quote: Quote | None,
    weather_forecast: list[dict[str, Any]] | None,
) -> None:
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
    _draw_header(canvas, font, "报价与出行提醒", request.destination)

    x = MARGIN
    top = PAGE_HEIGHT - HEADER_HEIGHT - 22
    content_width = PAGE_WIDTH - 2 * MARGIN

    if quote:
        cards = [
            ("团队总成本", f"CNY {quote.total_cost:,}"),
            ("人均成本", f"CNY {quote.cost_per_person:,}"),
            ("建议人均售价", f"CNY {quote.sale_price_per_person:,}"),
            ("预计毛利率", f"{quote.margin_rate:.1%}"),
        ]
        card_gap = 10
        card_width = (content_width - card_gap * 3) / 4
        for index, (label, value) in enumerate(cards):
            card_x = x + index * (card_width + card_gap)
            canvas.setFillColor(white)
            canvas.setStrokeColor(LIGHT_LINE)
            canvas.roundRect(card_x, top - 55, card_width, 55, 7, stroke=1, fill=1)
            canvas.setFillColor(GRAY)
            canvas.setFont(font, 8)
            canvas.drawString(card_x + 11, top - 19, label)
            canvas.setFillColor(NAVY)
            canvas.setFont(font, 13)
            canvas.drawString(card_x + 11, top - 41, value)

    column_top = top - 84
    left_width = content_width * 0.56
    right_x = x + left_width + 22
    right_width = content_width - left_width - 22

    canvas.setFillColor(NAVY)
    canvas.setFont(font, 14)
    canvas.drawString(x, column_top, "团队成本构成")
    row_y = column_top - 25
    if quote:
        for index, item in enumerate(quote.items[:6]):
            canvas.setFillColor(white if index % 2 == 0 else ROW_ALT)
            canvas.rect(x, row_y - 16, left_width, 24, stroke=0, fill=1)
            canvas.setFillColor(INK)
            canvas.setFont(font, 9)
            canvas.drawString(x + 10, row_y - 8, _fit_text(item.category, font, 9, left_width - 130))
            canvas.drawRightString(x + left_width - 10, row_y - 8, f"CNY {item.amount:,}")
            row_y -= 24
    else:
        canvas.setFillColor(GRAY)
        canvas.setFont(font, 9)
        canvas.drawString(x, row_y, "当前无可用报价。")
        row_y -= 24

    canvas.setFillColor(NAVY)
    canvas.setFont(font, 14)
    canvas.drawString(right_x, column_top, "行程天气")
    weather_y = column_top - 25
    if weather_forecast:
        for index, day in enumerate(weather_forecast[:7]):
            canvas.setFillColor(white if index % 2 == 0 else ROW_ALT)
            canvas.rect(right_x, weather_y - 16, right_width, 24, stroke=0, fill=1)
            canvas.setFillColor(INK)
            canvas.setFont(font, 8.5)
            weather_text = (
                f"{day.get('date', '?')}  {day.get('text_day', '未知')}  "
                f"{day.get('temp_min', '?')}-{day.get('temp_max', '?')} C"
            )
            canvas.drawString(right_x + 10, weather_y - 8, _fit_text(weather_text, font, 8.5, right_width - 20))
            weather_y -= 24
    else:
        canvas.setFillColor(GRAY)
        canvas.setFont(font, 9)
        canvas.drawString(right_x, weather_y, "未获取天气，请出发前核对。")

    note_top = min(row_y, weather_y) - 22
    canvas.setFillColor(NAVY)
    canvas.setFont(font, 14)
    canvas.drawString(x, note_top, "出行前必读")
    notes = [
        "票价、开放时间和预约政策请在出发前以官方渠道为准。",
        "节点价格是人均参考值；整体付款以团队报价和最终合同为准。",
        "交通、住宿、餐标、保险和取消条款应在锁价前逐项确认。",
        "高温或降雨时优先减少户外暴露，并保留室内备选方案。",
    ]
    canvas.setFillColor(GRAY)
    canvas.setFont(font, 9)
    cursor = note_top - 24
    for index, note in enumerate(notes, start=1):
        canvas.drawString(x + 6, cursor, f"{index}. {note}")
        cursor -= 20

    canvas.setFillColor(HexColor("#FFF8E1"))
    canvas.setStrokeColor(GOLD)
    canvas.roundRect(x, FOOTER_HEIGHT + 24, content_width, 35, 6, stroke=1, fill=1)
    canvas.setFillColor(INK)
    canvas.setFont(font, 8.5)
    canvas.drawString(
        x + 12,
        FOOTER_HEIGHT + 37,
        "本 PDF 为客户沟通精简版；详细节点、费用语义、资源来源与执行清单请查看同目录 Markdown 报告。",
    )


def _draw_illustrated_itinerary_page(
    canvas: Canvas,
    font: str,
    days: list[ItineraryDay],
    day_images: list[list[str]],
) -> None:
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
    first_day = days[0].day if days else 1
    last_day = days[-1].day if days else first_day
    _draw_header(canvas, font, "分日行程概览", f"DAY {first_day} - DAY {last_day}")

    gap = 14
    content_top = PAGE_HEIGHT - HEADER_HEIGHT - 14
    content_bottom = FOOTER_HEIGHT + 22
    card_width = (PAGE_WIDTH - 2 * MARGIN - gap) / 2
    card_height = (content_top - content_bottom - gap) / 2
    image_height = card_height * 0.58

    for index, day in enumerate(days[:4]):
        column = index % 2
        row = index // 2
        card_x = MARGIN + column * (card_width + gap)
        card_y = content_top - (row + 1) * card_height - row * gap

        canvas.setFillColor(white)
        canvas.setStrokeColor(LIGHT_LINE)
        canvas.roundRect(card_x, card_y, card_width, card_height, 8, stroke=1, fill=1)

        image_y = card_y + card_height - image_height
        available = [path for path in day_images[index] if path and Path(path).is_file()]
        if available:
            _draw_cover_image(canvas, available[0], card_x, image_y, card_width, image_height)
        else:
            canvas.setFillColor(NAVY_LIGHT)
            canvas.rect(card_x, image_y, card_width, image_height, stroke=0, fill=1)
            canvas.setFillColor(white)
            canvas.setFont(font, 10)
            canvas.drawCentredString(
                card_x + card_width / 2,
                image_y + image_height / 2,
                "行程图片生成中",
            )

        text_x = card_x + 13
        text_width = card_width - 26
        title_y = image_y - 22
        canvas.setFillColor(NAVY)
        canvas.setFont(font, 11.5)
        canvas.drawString(
            text_x,
            title_y,
            _fit_text(f"DAY {day.day}  {day.theme}", font, 11.5, text_width),
        )
        summary = "  /  ".join(_core_event_titles(day)) or "自由安排"
        canvas.setFillColor(GRAY)
        canvas.setFont(font, 8.2)
        canvas.drawString(
            text_x,
            title_y - 21,
            _fit_text(summary, font, 8.2, text_width),
        )
        time_text = (
            f"{day.events[0].start_time}-{day.events[-1].end_time}"
            if day.events
            else "待安排"
        )
        canvas.setFillColor(NAVY_LIGHT)
        canvas.setFont(font, 8.2)
        canvas.drawString(text_x, card_y + 10, time_text)


def build_pdf_report(
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    poster_path: str | None,
    day_image_paths: list[list[str] | str],
    output_path: str,
    quote: Quote | None = None,
    weather_forecast: list[dict[str, Any]] | None = None,
) -> str:
    """生成带图精简 PDF：封面、分日图卡与报价/天气摘要。"""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()
    canvas = Canvas(
        str(destination),
        pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
        pageCompression=1,
    )
    canvas.setTitle(request.title)
    canvas.setAuthor("TripOps AI")
    canvas.setSubject("精简旅行行程与报价摘要")

    itinerary_page_count = max(1, ceil(len(itinerary) / 4))
    total_pages = itinerary_page_count + 2
    _draw_cover(canvas, font, request, poster_path)
    _draw_footer(canvas, font, 1, total_pages)
    canvas.showPage()

    for page_index in range(itinerary_page_count):
        start = page_index * 4
        page_days = itinerary[start : start + 4]
        page_images: list[list[str]] = []
        for original_index in range(start, start + len(page_days)):
            raw = day_image_paths[original_index] if original_index < len(day_image_paths) else []
            page_images.append(raw if isinstance(raw, list) else ([raw] if raw else []))
        _draw_illustrated_itinerary_page(canvas, font, page_days, page_images)
        _draw_footer(canvas, font, page_index + 2, total_pages)
        canvas.showPage()

    _draw_brief_details_page(canvas, font, request, quote, weather_forecast)
    _draw_footer(canvas, font, total_pages, total_pages)
    canvas.save()
    logger.info("Vector PDF report -> %s (%d pages)", destination, total_pages)
    return str(destination)
