"""Картинки для превью ссылок в мессенджерах.

Мессенджер показывает ссылку красиво только если у страницы есть Open Graph-теги
и широкая картинка. Без картинки MAX и Telegram рисуют мелкий значок и скребут
описание прямо из текста страницы — получается «Телефон +7 Введите номер...».

Картинки собираются один раз и лежат в static/img/share. Шрифты берутся
системные: файлы уже готовые, на сервере шрифты не нужны.

Запуск:  python tools/generate_share_cards.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE_DIR = Path(__file__).resolve().parent.parent
ICON_DIR = BASE_DIR / 'static' / 'img' / 'pwa'
OUT_DIR = BASE_DIR / 'static' / 'img' / 'share'

WIDTH, HEIGHT = 1200, 630
BACKGROUND = (2, 8, 11)
COMPANY = 'КОППЕР РЕСОРСЕЗ'

FONT_CANDIDATES_BOLD = [
    Path('C:/Windows/Fonts/segoeuib.ttf'),
    Path('C:/Windows/Fonts/arialbd.ttf'),
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
]
FONT_CANDIDATES_REGULAR = [
    Path('C:/Windows/Fonts/segoeui.ttf'),
    Path('C:/Windows/Fonts/arial.ttf'),
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
]

CARDS = [
    {
        'slug': 'driver',
        'icon': 'driver-512.png',
        'title': 'Водитель самосвала',
        'accent': (140, 255, 46),
    },
    {
        'slug': 'excavator',
        'icon': 'excavator-512.png',
        'title': 'Машинист экскаватора',
        'accent': (255, 210, 0),
    },
    {
        'slug': 'mining-master',
        'icon': 'mining-master-512.png',
        'title': 'Горный мастер',
        'accent': (74, 163, 255),
    },
    {
        'slug': 'apps',
        'icon': 'admin-512.png',
        'title': 'Рабочие приложения',
        'accent': (47, 191, 113),
    },
]

LEAD = 'Установите приложение на телефон — вход будет уже в нём.'

# Общий вход: роль ещё неизвестна, поэтому вместо одного значка показываем три —
# сразу видно, что ссылка годится любому.
START_CARD = {
    'slug': 'start',
    'icons': ['driver-512.png', 'excavator-512.png', 'mining-master-512.png'],
    'title': 'Рабочее приложение',
    'lead': 'Введите номер телефона — система подскажет, какое приложение вам нужно.',
    'accent': (47, 191, 113),
}


def pick_font(candidates: list[Path], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    raise SystemExit(
        'Не найден шрифт с кириллицей. Проверьте пути в FONT_CANDIDATES_*.'
    )


def glow_layer(accent: tuple[int, int, int]) -> Image.Image:
    """Мягкое свечение цветом роли — то же, что на экране входа."""
    layer = Image.new('RGB', (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(layer)
    draw.ellipse(
        (-180, -360, WIDTH + 180, 300),
        fill=tuple(int(channel * 0.20) for channel in accent),
    )
    return layer.filter(ImageFilter.GaussianBlur(170))


def rounded(icon: Image.Image, radius_ratio: float = 0.22) -> Image.Image:
    """Значки лежат квадратными файлами, а на экране телефона они скруглённые."""
    mask = Image.new('L', icon.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, icon.size[0] - 1, icon.size[1] - 1),
        radius=int(icon.size[0] * radius_ratio),
        fill=255,
    )
    out = icon.copy()
    out.putalpha(mask)
    return out


def fit_font(draw: ImageDraw.ImageDraw, text: str, limit: int, start: int) -> ImageFont.FreeTypeFont:
    """Самое длинное название роли не влезало и уезжало за край картинки."""
    size = start
    while size > 34:
        font = pick_font(FONT_CANDIDATES_BOLD, size)
        if draw.textlength(text, font=font) <= limit:
            return font
        size -= 2
    return pick_font(FONT_CANDIDATES_BOLD, 34)


def build_card(card: dict) -> Path:
    accent = card['accent']
    image = glow_layer(accent)
    draw = ImageDraw.Draw(image)

    icon_path = ICON_DIR / card['icon']
    if not icon_path.exists():
        raise SystemExit(f'Нет значка {icon_path}')
    icon = rounded(Image.open(icon_path).convert('RGBA').resize((236, 236), Image.LANCZOS))
    icon_x, icon_y = 92, (HEIGHT - 236) // 2
    image.paste(icon, (icon_x, icon_y), icon)

    text_x = icon_x + 236 + 68
    limit = WIDTH - text_x - 92
    company_font = pick_font(FONT_CANDIDATES_BOLD, 30)
    title_font = fit_font(draw, card['title'], limit, 76)
    lead_font = pick_font(FONT_CANDIDATES_REGULAR, 34)

    draw.text(
        (text_x, 196),
        COMPANY,
        font=company_font,
        fill=(126, 146, 154),
    )
    draw.text((text_x, 246), card['title'], font=title_font, fill=(242, 248, 250))

    # Подпись переносим руками: длинная строка не должна убегать за край.
    words = LEAD.split(' ')
    line, lines = '', []
    for word in words:
        probe = (line + ' ' + word).strip()
        if draw.textlength(probe, font=lead_font) > limit and line:
            lines.append(line)
            line = word
        else:
            line = probe
    if line:
        lines.append(line)
    for index, text in enumerate(lines[:2]):
        draw.text((text_x, 356 + index * 46), text, font=lead_font, fill=(159, 178, 186))

    draw.rectangle((text_x, 470, text_x + 132, 478), fill=accent)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{card['slug']}-share.png"
    image.save(out_path, 'PNG', optimize=True)
    return out_path


def build_start_card(card: dict) -> Path:
    """Раскладка сверху вниз: марка, заголовок, значки, подпись.

    Значки ставим ниже заголовка фиксированно — при вычислении «по центру»
    заголовок налезал на них.
    """
    accent = card['accent']
    image = glow_layer(accent)
    draw = ImageDraw.Draw(image)

    text_x = 92
    limit = WIDTH - text_x - 92
    draw.text((text_x, 96), COMPANY, font=pick_font(FONT_CANDIDATES_BOLD, 30), fill=(126, 146, 154))
    draw.text((text_x, 140), card['title'], font=fit_font(draw, card['title'], limit, 72), fill=(242, 248, 250))

    size = 176
    gap = 26
    x, y = text_x, 268
    for name in card['icons']:
        icon_path = ICON_DIR / name
        if not icon_path.exists():
            raise SystemExit(f'Нет значка {icon_path}')
        icon = rounded(Image.open(icon_path).convert('RGBA').resize((size, size), Image.LANCZOS))
        image.paste(icon, (x, y), icon)
        x += size + gap

    lead_font = pick_font(FONT_CANDIDATES_REGULAR, 32)
    words = card['lead'].split(' ')
    line, lines = '', []
    for word in words:
        probe = (line + ' ' + word).strip()
        if draw.textlength(probe, font=lead_font) > limit and line:
            lines.append(line)
            line = word
        else:
            line = probe
    if line:
        lines.append(line)
    for index, text in enumerate(lines[:2]):
        draw.text((text_x, 486 + index * 44), text, font=lead_font, fill=(159, 178, 186))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{card['slug']}-share.png"
    image.save(out_path, 'PNG', optimize=True)
    return out_path


def main() -> int:
    for card in CARDS:
        path = build_card(card)
        print(f'готово: {path.relative_to(BASE_DIR)}  ({path.stat().st_size // 1024} КБ)')
    path = build_start_card(START_CARD)
    print(f'готово: {path.relative_to(BASE_DIR)}  ({path.stat().st_size // 1024} КБ)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
