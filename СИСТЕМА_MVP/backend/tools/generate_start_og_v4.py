"""Build the review candidate for the /start Open Graph card.

The composition is rendered at 2400x1260 and downsampled to 1200x630.
All typography and brand/application marks come from deterministic local assets.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageEnhance, ImageFilter, ImageFont


BASE_DIR = Path(__file__).resolve().parent.parent
WORKTREE = BASE_DIR.parent.parent
SOURCE_DIR = BASE_DIR / "static" / "img" / "share" / "sources"
OUT_DIR = WORKTREE / "output" / "start-og-v4-review"

SCALE = 2
WIDTH, HEIGHT = 1200 * SCALE, 630 * SCALE
COPPER = (197, 138, 82)
WARM_WHITE = (247, 242, 233)
MUTED_WHITE = (218, 215, 207)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size * SCALE)


def cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    ratio = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize((round(image.width * ratio), round(image.height * ratio)), Image.Resampling.LANCZOS)
    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))


def gradient_overlay(size: tuple[int, int]) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    px = layer.load()
    for y in range(size[1]):
        yn = y / max(1, size[1] - 1)
        bottom = max(0.0, (yn - 0.48) / 0.52) ** 1.45
        for x in range(size[0]):
            xn = x / max(1, size[0] - 1)
            left = max(0.0, (0.64 - xn) / 0.64) ** 1.55
            alpha = int(208 * bottom + 112 * left * (1.0 - bottom * 0.35))
            px[x, y] = (2, 7, 9, min(226, alpha))
    return layer


def extract_logo() -> Image.Image:
    logo = Image.new("RGBA", (1120, 390), (0, 0, 0, 0))
    draw = ImageDraw.Draw(logo)
    # Reproduce the verified transparent vector geometry from kopper-mark-v1.svg
    # directly at high resolution, avoiding any raster background or halo.
    ox, oy, unit = 8, 18, 5.3
    pt = lambda x, y: (round(ox + x * unit), round(oy + y * unit))
    draw.polygon([pt(7, 5), pt(58, 32), pt(7, 59)], fill=(32, 39, 38, 255))
    draw.polygon([pt(7, 5), pt(27, 22), pt(19, 32)], fill=(50, 59, 57, 255))
    draw.polygon([pt(7, 59), pt(19, 32), pt(27, 42)], fill=(17, 22, 21, 255))
    draw.polygon([pt(27, 22), pt(58, 32), pt(27, 42), pt(19, 32)], fill=(72, 80, 78, 255))
    draw.line([pt(7, 5), pt(27, 22), pt(58, 32), pt(27, 42), pt(7, 59), pt(7, 5)], fill=(215, 138, 85, 255), width=6, joint="curve")
    draw.text((82, 119), "КР", font=ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 86), fill=(218, 145, 90, 255), stroke_width=3, stroke_fill=(20, 23, 22, 255))
    name_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 112)
    sub_font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 28)
    name_x = 310
    for y, text in ((55, "КОППЕР"), (160, "РИСОРСЕЗ")):
        draw.text((name_x + 3, y + 4), text, font=name_font, fill=(0, 0, 0, 145), stroke_width=2)
        draw.text(
            (name_x, y), text, font=name_font, fill=(83, 76, 68, 255),
            stroke_width=2, stroke_fill=(190, 129, 75, 240),
        )
    draw.text((315, 296), "ГОРНОТРАНСПОРТНАЯ КОМПАНИЯ", font=sub_font, fill=(71, 67, 62, 255))
    return logo


def rounded_icon(path: Path, size: int) -> Image.Image:
    icon = Image.open(path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=round(size * 0.22), fill=255)
    icon.putalpha(mask)
    return icon


def shadowed_paste(canvas: Image.Image, asset: Image.Image, pos: tuple[int, int], blur: int, offset: int, opacity: int) -> None:
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_asset = Image.new("RGBA", asset.size, (0, 0, 0, 0))
    shadow_asset.putalpha(asset.getchannel("A").point(lambda value: value * opacity // 255))
    shadow.paste(shadow_asset, (pos[0] + offset, pos[1] + offset), shadow_asset)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))
    canvas.alpha_composite(asset, pos)


def build() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    background_path = SOURCE_DIR / "og-start-v4-quarry.png"
    background = cover(Image.open(background_path).convert("RGB"), (WIDTH, HEIGHT))
    background = ImageEnhance.Contrast(background).enhance(1.06)
    background = ImageEnhance.Brightness(background).enhance(1.08)
    canvas = background.convert("RGBA")
    canvas.alpha_composite(gradient_overlay(canvas.size))

    logo = extract_logo()
    logo.thumbnail((370 * SCALE, 185 * SCALE), Image.Resampling.LANCZOS)
    shadowed_paste(canvas, logo, (68 * SCALE, 48 * SCALE), 5 * SCALE, 3 * SCALE, 150)

    draw = ImageDraw.Draw(canvas)
    title_y = 432 * SCALE
    draw.rounded_rectangle((88 * SCALE, 404 * SCALE, 174 * SCALE, 410 * SCALE), radius=3 * SCALE, fill=COPPER + (255,))
    draw.text((88 * SCALE, 418 * SCALE), "РАБОЧИЕ ПРИЛОЖЕНИЯ", font=font(58, bold=True), fill=WARM_WHITE)
    draw.text((88 * SCALE, 500 * SCALE), "Вход по номеру телефона", font=font(29), fill=MUTED_WHITE)

    icon_size = 96 * SCALE
    icon_y = 470 * SCALE
    icon_paths = [
        BASE_DIR / "static" / "img" / "pwa" / "driver-512.png",
        BASE_DIR / "static" / "img" / "pwa" / "excavator-512.png",
    ]
    for index, path in enumerate(icon_paths):
        icon = rounded_icon(path, icon_size)
        x = (920 + index * 112) * SCALE
        shadowed_paste(canvas, icon, (x, icon_y), 9 * SCALE, 5 * SCALE, 125)

    final = canvas.convert("RGB").resize((1200, 630), Image.Resampling.LANCZOS)
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    final_path = OUT_DIR / "og-start-v4.jpg"
    final.save(final_path, "JPEG", quality=91, optimize=True, progressive=True, icc_profile=srgb)
    final.resize((600, 315), Image.Resampling.LANCZOS).save(
        OUT_DIR / "og-start-v4-600x315.jpg", "JPEG", quality=91, optimize=True, progressive=True, icc_profile=srgb
    )

    logo_preview = Image.new("RGBA", (900, 360), (23, 26, 27, 255))
    large_logo = extract_logo()
    large_logo.thumbnail((780, 270), Image.Resampling.LANCZOS)
    logo_preview.alpha_composite(large_logo, ((900 - large_logo.width) // 2, (360 - large_logo.height) // 2))
    logo_preview.save(OUT_DIR / "logo-used-transparent-preview.png", "PNG", optimize=True)
    extract_logo().save(OUT_DIR / "kopper-logo-transparent.png", "PNG", optimize=True)

    # Neutral messenger-card mockup: only the part controlled by the website is
    # represented, without imitating a specific messenger chrome.
    mock = Image.new("RGB", (820, 650), (30, 32, 35))
    preview = final.resize((690, 362), Image.Resampling.LANCZOS)
    mock.paste(preview, (65, 50))
    md = ImageDraw.Draw(mock)
    mock_regular = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 20)
    mock_bold = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 30)
    mock_site = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 19)
    md.text((65, 438), "Коппер Рисорсез", font=mock_site, fill=(188, 190, 193))
    md.text((65, 478), "Вход в рабочие приложения", font=mock_bold, fill=(248, 248, 248))
    md.text((65, 530), "Введите номер телефона — система покажет нужное приложение.", font=mock_regular, fill=(202, 204, 208))
    mock.save(OUT_DIR / "og-start-v4-card-preview-690.jpg", "JPEG", quality=91, optimize=True, icc_profile=srgb)


if __name__ == "__main__":
    build()
