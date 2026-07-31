"""PDF report generator: travel-brochure style.

Layout model (all pages share the same size, text never overlaid on photos):
- Cover: full-bleed poster with a side info panel.
- Day pages: alternating image/text panels (odd pages image-left,
  even pages image-right); each page may contain one or more photos.
- Summary page: budget table + data-provenance notes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.models.schemas import ItineraryDay, PlanRequest, Quote

logger = logging.getLogger(__name__)

PAGE_WIDTH = 896
PAGE_HEIGHT = 1152

MARGIN = 48
FOOTER_Y = 1068

NAVY = (24, 58, 92)
NAVY_LIGHT = (56, 102, 146)
GOLD = (212, 175, 55)
PAPER = (245, 246, 248)
WHITE = (255, 255, 255)
INK = (45, 52, 62)
GRAY = (122, 130, 140)
LIGHT_LINE = (222, 226, 232)

FONT_SEARCH_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_SEARCH_PATHS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except (OSError, ImportError):
                continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Wrap text by measured pixel width (handles CJK reliably)."""
    lines: list[str] = []
    for raw in text.split("\n"):
        current = ""
        for ch in raw:
            if draw.textlength(current + ch, font=font) <= max_width:
                current += ch
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    return lines


def _cover_image(path: str, box_w: int, box_h: int) -> Image.Image:
    """Resize-and-crop so the image completely fills the box (no letterboxing)."""
    img = Image.open(path).convert("RGB")
    ratio = max(box_w / img.width, box_h / img.height)
    new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
    img = img.resize(new_size, Image.Resampling.LANCZOS)
    left = (new_size[0] - box_w) // 2
    top = (new_size[1] - box_h) // 2
    return img.crop((left, top, left + box_w, top + box_h))


def build_pdf_report(
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    poster_path: str | None,
    day_image_paths: list[str | None],
    output_path: str,
    quote: Quote | None = None,
) -> str:
    """Assemble the final brochure PDF: cover + per-day pages + summary."""
    pages: list[Image.Image] = [build_cover(request, poster_path)]

    for index, day in enumerate(itinerary):
        raw = day_image_paths[index] if index < len(day_image_paths) else None
        images = raw if isinstance(raw, list) else ([raw] if raw else [])
        pages.append(build_day_page(day, images, (day.day - 1) % 3))

    pages.append(build_summary_page(request, quote))

    total = len(pages)
    for index, page in enumerate(pages, start=1):
        draw_footer(page, index, total)

    pages[0].save(
        output_path,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=150,
    )
    logger.info("PDF report → %s (%d pages)", output_path, total)
    return output_path


# ------------------------------------------------------------------
# Footer
# ------------------------------------------------------------------

def draw_footer(page: Image.Image, page_number: int, total_pages: int) -> None:
    draw = ImageDraw.Draw(page)
    small = _load_font(15)
    note = "* 部分数据为 AI 估算，实际以官方/供应商渠道为准 *"
    draw.text((MARGIN, FOOTER_Y + 2), note, font=small, fill=GRAY)
    pager = f"{page_number} / {total_pages}"
    w = draw.textlength(pager, font=small)
    draw.text((PAGE_WIDTH - MARGIN - w, FOOTER_Y + 2), pager, font=small, fill=GRAY)


def _draw_top_bar(draw: ImageDraw.ImageDraw, title: str, subtitle: str = "") -> None:
    draw.rectangle([0, 0, PAGE_WIDTH, 8], fill=GOLD)
    draw.rectangle([0, 8, PAGE_WIDTH, 96], fill=NAVY)
    title_font = _load_font(38)
    sub_font = _load_font(18)
    draw.text((MARGIN, 26), title, font=title_font, fill=WHITE)
    if subtitle:
        draw.text((MARGIN, 66), subtitle, font=sub_font, fill=(205, 216, 228))


# ------------------------------------------------------------------
# Cover page
# ------------------------------------------------------------------

def build_cover(request: PlanRequest, poster_path: str | None) -> Image.Image:
    page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), NAVY)
    if poster_path and Path(poster_path).exists():
        page.paste(_cover_image(poster_path, PAGE_WIDTH, PAGE_HEIGHT), (0, 0))

    # Right side info panel (translucent white)
    panel = Image.new("RGBA", (PAGE_WIDTH, PAGE_HEIGHT), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rectangle([540, 0, PAGE_WIDTH, PAGE_HEIGHT], fill=(255, 255, 255, 235))
    page = Image.alpha_composite(page.convert("RGBA"), panel).convert("RGB")

    draw = ImageDraw.Draw(page)
    title_font = _load_font(42)
    head_font = _load_font(20)
    body_font = _load_font(22)
    small_font = _load_font(16)

    x = 572
    y = 120
    draw.text((x, y), "TRIP PLANNER", font=head_font, fill=GOLD)
    y += 46

    for line in _wrap_text(draw, request.title, title_font, 300)[:4]:
        draw.text((x, y), line, font=title_font, fill=INK)
        y += 56
    y += 12

    for label, value in (
        ("目的地", request.destination),
        ("周期", f"{request.days} 天 {request.nights} 晚"),
        ("人数", f"{request.group_size} 人"),
        ("客群", request.target_audience),
        ("主题", " / ".join(request.themes) or "综合"),
    ):
        draw.text((x, y), f"{label}", font=head_font, fill=GOLD)
        y += 28
        draw.text((x, y), value, font=body_font, fill=INK)
        y += 44

    draw.line([(x, y), (x + 300, y)], fill=LIGHT_LINE, width=1)
    y += 22
    draw.text((x, y), "TripOps AI · 智能旅行策划", font=small_font, fill=GRAY)
    return page


# ------------------------------------------------------------------
# Day pages (alternating layout, multi-image support)
# ------------------------------------------------------------------

def build_day_page(day: ItineraryDay, images: list[str], layout: int) -> Image.Image:
    """Brochure day page.

    layout:
      0 = full-width image on top + text panel below
      1 = full-height image on the right + text panel on the left
      2 = full-height image on the left + text panel on the right
    """
    page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), PAPER)
    draw = ImageDraw.Draw(page)
    _draw_top_bar(draw, f"DAY {day.day}", day.theme)

    content_top = 132
    content_bottom = 1030
    gap = 24

    if layout == 0:
        # Full-width image on top; text panel height adapts to content
        text_h = _measure_text_height(draw, day, PAGE_WIDTH - 2 * MARGIN - 52)
        text_h = max(260, min(text_h, 540))
        img_box = (MARGIN, content_top, PAGE_WIDTH - MARGIN, content_bottom - text_h - gap)
        txt_box = (MARGIN, img_box[3] + gap, PAGE_WIDTH - MARGIN, content_bottom)
    else:
        img_w = int((PAGE_WIDTH - 2 * MARGIN) * 0.58)
        if layout == 1:  # image right, text left
            img_box = (PAGE_WIDTH - MARGIN - img_w, content_top, PAGE_WIDTH - MARGIN, content_bottom)
            txt_box = (MARGIN, content_top, img_box[0] - gap, content_bottom)
        else:  # image left, text right
            img_box = (MARGIN, content_top, MARGIN + img_w, content_bottom)
            txt_box = (img_box[2] + gap, content_top, PAGE_WIDTH - MARGIN, content_bottom)

    _paint_images_cover(page, images, img_box)

    draw.rounded_rectangle(txt_box, radius=18, fill=WHITE, outline=LIGHT_LINE, width=2)
    _paint_itinerary_text(draw, day, txt_box)
    return page


def _measure_text_height(draw: ImageDraw.ImageDraw, day: ItineraryDay, max_width: int) -> int:
    desc_font = _load_font(16)
    height = 36
    for event in day.events:
        height += 30
        if event.description and event.category not in ("logistics", "break"):
            lines = len(_wrap_text(draw, event.description, desc_font, max_width)) or 1
            height += min(lines, 2) * 22
        height += 14
    return height


def _paint_images_cover(page: Image.Image, images: list[str], box: tuple[int, int, int, int]) -> None:
    """Fill the whole box with photos (cover-cropped); multiple photos become a grid."""
    x1, y1, x2, y2 = box
    available = [p for p in images if p and Path(p).exists()]
    draw = ImageDraw.Draw(page)

    if not available:
        draw.rounded_rectangle(box, radius=14, fill=NAVY_LIGHT)
        draw.text((x1 + 20, y1 + 20), "暂无图片", font=_load_font(20), fill=WHITE)
        return

    if len(available) == 1:
        page.paste(_cover_image(available[0], x2 - x1, y2 - y1), (x1, y1))
        draw.rectangle([x1, y1, x2 - 1, y2 - 1], outline=WHITE, width=3)
        return

    cols = 2
    rows = (len(available) + cols - 1) // cols
    cell_w = (x2 - x1) // cols
    cell_h = (y2 - y1) // rows
    for index, path in enumerate(available[: cols * rows]):
        cx = x1 + (index % cols) * cell_w
        cy = y1 + (index // cols) * cell_h
        page.paste(_cover_image(path, cell_w, cell_h), (cx, cy))
        draw.rectangle([cx, cy, cx + cell_w - 1, cy + cell_h - 1], outline=WHITE, width=3)


def _paint_itinerary_text(draw: ImageDraw.ImageDraw, day: ItineraryDay, box: tuple[int, int, int, int]) -> None:
    x, y, x2, _ = box
    pad = 26
    event_font = _load_font(20)
    desc_font = _load_font(16)
    small_font = _load_font(16)

    cursor_y = y + pad
    right = x2 - pad
    for event in day.events:
        time_str = f"{event.start_time} - {event.end_time}"

        # timeline dot + line
        dot_x = x + pad + 4
        draw.ellipse([dot_x - 5, cursor_y + 8, dot_x + 5, cursor_y + 18], fill=GOLD)
        draw.line([(dot_x, cursor_y + 18), (dot_x, cursor_y + 40)], fill=LIGHT_LINE, width=2)

        # time
        draw.text((x + pad + 22, cursor_y), time_str, font=small_font, fill=GOLD)
        # title
        draw.text((x + pad + 110, cursor_y), event.title[:24], font=event_font, fill=INK)
        cursor_y += 30

        # description (up to 2 lines)
        if event.description and event.category not in ("logistics", "break"):
            for line in _wrap_text(draw, event.description, desc_font, right - (x + pad + 22))[:2]:
                draw.text((x + pad + 22, cursor_y), line, font=desc_font, fill=GRAY)
                cursor_y += 22
        cursor_y += 14

        if cursor_y > box[3] - 70:
            break

    # day subtotal pinned to the bottom of the text card
    subtotal = sum(e.cost_per_person for e in day.events)
    note = f"当日人均估算费用 ¥{subtotal:,}" if subtotal else "当日无额外费用"
    note_font = _load_font(16)
    draw.text((box[0] + 26, box[3] - 44), note, font=note_font, fill=GRAY)


# ------------------------------------------------------------------
# Summary page
# ------------------------------------------------------------------

def build_summary_page(request: PlanRequest, quote: Quote | None) -> Image.Image:
    page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), PAPER)
    draw = ImageDraw.Draw(page)
    _draw_top_bar(draw, "方案总结")

    x = MARGIN + 24
    y = 140
    head_font = _load_font(24)
    body_font = _load_font(20)
    small_font = _load_font(16)

    if quote and quote.items:
        draw.text((x, y), "预算明细（估算）", font=head_font, fill=NAVY)
        y += 44

        col_x = (MARGIN + 24, MARGIN + 300, PAGE_WIDTH - MARGIN - 200)
        table_y = y
        header_fill = NAVY
        # header row
        draw.rectangle([MARGIN + 24, table_y, PAGE_WIDTH - MARGIN - 24, table_y + 44], fill=header_fill)
        draw.text((col_x[0] + 16, table_y + 10), "类别", font=body_font, fill=WHITE)
        draw.text((col_x[1] + 16, table_y + 10), "说明", font=body_font, fill=WHITE)
        draw.text((PAGE_WIDTH - MARGIN - 24 - 130, table_y + 10), "金额", font=body_font, fill=WHITE)
        table_y += 44

        for index, item in enumerate(quote.items):
            row_fill = WHITE if index % 2 == 0 else (236, 240, 244)
            draw.rectangle([MARGIN + 24, table_y, PAGE_WIDTH - MARGIN - 24, table_y + 40], fill=row_fill)
            draw.text((col_x[0] + 16, table_y + 10), item.category, font=body_font, fill=INK)
            draw.text((col_x[1] + 16, table_y + 10), item.description[:16], font=small_font, fill=INK)
            amount = f"¥{item.amount:,}"
            aw = draw.textlength(amount, font=body_font)
            draw.text((PAGE_WIDTH - MARGIN - 24 - 24 - aw, table_y + 10), amount, font=body_font, fill=INK)
            table_y += 40

        # total row
        draw.rectangle([MARGIN + 24, table_y, PAGE_WIDTH - MARGIN - 24, table_y + 46], fill=GOLD)
        draw.text((col_x[0] + 16, table_y + 10), "合计", font=body_font, fill=INK)
        total = f"¥{quote.total_cost:,}"
        tw = draw.textlength(total, font=body_font)
        draw.text((PAGE_WIDTH - MARGIN - 24 - 24 - tw, table_y + 10), total, font=body_font, fill=INK)
        table_y += 60

        summary_lines = (
            f"人均成本: ¥{quote.cost_per_person:,}   ·   人均售价: ¥{quote.sale_price_per_person:,}"
            f"   ·   毛利率: {quote.margin_rate:.1%}"
        )
        draw.text((x, table_y), summary_lines, font=body_font, fill=INK)
        table_y += 56
    else:
        draw.text((x, y), "暂无报价数据", font=body_font, fill=GRAY)
        table_y = y + 60

    draw.text((x, table_y), "数据来源说明", font=head_font, fill=NAVY)
    table_y += 44
    notes = [
        "✅ 景点/攻略：Tavily 实时网页搜索 + 高德 POI（真实检索）",
        "✅ 天气：和风天气官方预报",
        "⚠️ 景点票价 / 建议时长 / 开放时间：LLM 估算，需官方确认",
        "🟡 景点间交通时间：高德实测 + LLM 估算兜底",
        "⚠️ 成本明细：LLM 市场估价，以供应商报价为准",
        "🟡 行程编排与活动描述：AI 生成，仅供参考",
        "✅ 路线优化 / 约束校验 / 计价：确定性计算",
    ]
    for note in notes:
        draw.text((x, table_y), note, font=small_font, fill=GRAY)
        table_y += 34

    return page
