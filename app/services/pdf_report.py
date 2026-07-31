"""PDF report generator: cover poster + per-day illustrated itinerary pages.

Uses Pillow for image compositing and PDF assembly.
"""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.models.schemas import ItineraryDay, PlanRequest, Quote

logger = logging.getLogger(__name__)

PAGE_WIDTH = 896
PAGE_HEIGHT = 1152

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


def build_pdf_report(
    request: PlanRequest,
    itinerary: list[ItineraryDay],
    poster_path: str | None,
    day_image_paths: list[str | None],
    output_path: str,
    quote: Quote | None = None,
) -> str:
    """Assemble the final PDF: cover + per-day illustrated pages."""
    pages: list[Image.Image] = []

    cover = _build_cover(request, poster_path)
    pages.append(cover)

    for i, day in enumerate(itinerary):
        image_path = day_image_paths[i] if i < len(day_image_paths) else None
        page = _build_day_page(request, day, image_path, quote)
        pages.append(page)

    pages[0].save(
        output_path,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=150,
    )
    logger.info("PDF report → %s (%d pages)", output_path, len(pages))
    return output_path


def _build_cover(request: PlanRequest, poster_path: str | None) -> Image.Image:
    if poster_path and Path(poster_path).exists():
        img = Image.open(poster_path).convert("RGB")
        img = img.resize((PAGE_WIDTH, PAGE_HEIGHT), Image.Resampling.LANCZOS)
    else:
        img = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), (41, 128, 185))

    draw = ImageDraw.Draw(img)
    overlay = Image.new("RGBA", (PAGE_WIDTH, 200), (0, 0, 0, 140))
    img.paste(Image.alpha_composite(
        img.crop((0, PAGE_HEIGHT - 200, PAGE_WIDTH, PAGE_HEIGHT)).convert("RGBA"),
        overlay,
    ).convert("RGB"), (0, PAGE_HEIGHT - 200))

    title_font = _load_font(42)
    sub_font = _load_font(22)

    draw = ImageDraw.Draw(img)
    draw.text((40, PAGE_HEIGHT - 180), request.title, font=title_font, fill="white")
    subtitle = (
        f"{request.destination} · {request.days}天{request.nights}晚 · "
        f"{request.group_size}人 · {request.target_audience}"
    )
    draw.text((40, PAGE_HEIGHT - 120), subtitle, font=sub_font, fill=(220, 220, 220))
    draw.text((40, PAGE_HEIGHT - 85), "TripOps AI · 智能旅行策划", font=sub_font, fill=(180, 180, 180))

    return img


def _build_day_page(
    request: PlanRequest,
    day: ItineraryDay,
    image_path: str | None,
    quote: Quote | None,
) -> Image.Image:
    if image_path and Path(image_path).exists():
        img = Image.open(image_path).convert("RGB")
        img = img.resize((PAGE_WIDTH, PAGE_HEIGHT), Image.Resampling.LANCZOS)
    else:
        colors = [(41, 128, 185), (39, 174, 96), (142, 68, 173), (211, 84, 0)]
        color = colors[(day.day - 1) % len(colors)]
        img = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), color)

    overlay = Image.new("RGBA", (PAGE_WIDTH, PAGE_HEIGHT), (0, 0, 0, 120))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    title_font = _load_font(36)
    event_font = _load_font(22)
    small_font = _load_font(18)

    y = 40
    draw.text((40, y), f"Day {day.day}", font=title_font, fill=(255, 215, 0))
    y += 50
    draw.text((40, y), day.theme, font=title_font, fill="white")
    y += 60

    draw.line([(40, y), (PAGE_WIDTH - 40, y)], fill=(255, 255, 255, 128), width=1)
    y += 20

    for event in day.events:
        time_str = f"{event.start_time} - {event.end_time}"
        cost_str = f"  ¥{event.cost_per_person}" if event.cost_per_person else ""

        draw.text((40, y), time_str, font=event_font, fill=(255, 215, 0))
        draw.text((220, y), event.title[:28], font=event_font, fill="white")
        if cost_str:
            draw.text((PAGE_WIDTH - 120, y), cost_str, font=event_font, fill=(144, 238, 144))
        y += 32

        if event.description and event.category not in ("logistics", "break"):
            wrapped = textwrap.wrap(event.description[:80], width=38)
            for line in wrapped[:2]:
                draw.text((60, y), line, font=small_font, fill=(200, 200, 200))
                y += 24
        y += 12

    if quote and day.day == 1:
        y = max(y + 20, PAGE_HEIGHT - 160)
        draw.line([(40, y), (PAGE_WIDTH - 40, y)], fill=(255, 255, 255, 128), width=1)
        y += 15
        draw.text((40, y), f"总成本: ¥{quote.total_cost:,}  |  人均售价: ¥{quote.sale_price_per_person:,}  |  毛利率: {quote.margin_rate:.1%}",
                  font=small_font, fill=(200, 200, 200))

    draw.text((40, PAGE_HEIGHT - 40), "TripOps AI", font=small_font, fill=(150, 150, 150))

    return img
