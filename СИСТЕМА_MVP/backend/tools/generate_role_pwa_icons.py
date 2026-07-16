from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


CANVAS = 1024
SCALE = 4
INK = "#F7FAFC"
GRAPHITE = "#101820"
COPPER = "#D17A3A"
SHADOW = "#0B1117"

ROLE_ICONS = {
    "driver": ("#147D7E", "Самосвал"),
    "excavator": ("#D58B14", "Экскаватор"),
    "mining-master": ("#2366A8", "Горный мастер"),
    "deputy-mining-manager": ("#2E7D52", "Заместитель начальника участка"),
    "dispatcher": ("#B33A4C", "Диспетчер"),
    "oup": ("#A64778", "ОУП"),
    "mechanic": ("#C65C2E", "Механик"),
    "management": ("#5058A4", "Руководство"),
    "admin": ("#53616F", "Администратор"),
}


def s(value: int | float) -> int:
    return round(value * SCALE)


def box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple(s(value) for value in values)


def points(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(s(x), s(y)) for x, y in values]


def line(draw: ImageDraw.ImageDraw, values, *, fill=INK, width=42, joint="curve"):
    draw.line(points(values), fill=fill, width=s(width), joint=joint)


def rounded(draw: ImageDraw.ImageDraw, values, *, radius=30, fill=None, outline=None, width=1):
    draw.rounded_rectangle(
        box(values),
        radius=s(radius),
        fill=fill,
        outline=outline,
        width=s(width),
    )


def draw_frame(draw: ImageDraw.ImageDraw, role_color: str) -> None:
    draw.rectangle(box((0, 0, CANVAS, CANVAS)), fill=role_color)
    draw.polygon(points([(0, 0), (430, 0), (0, 430)]), fill=SHADOW)
    line(draw, [(100, 120), (282, 120)], fill=COPPER, width=32)
    line(draw, [(100, 174), (212, 174)], fill=COPPER, width=20)


def draw_driver(draw: ImageDraw.ImageDraw) -> None:
    draw.polygon(points([(255, 378), (535, 378), (594, 554), (310, 554)]), fill=INK)
    rounded(draw, (570, 422, 760, 563), radius=26, fill=INK)
    draw.polygon(points([(608, 444), (696, 444), (735, 520), (608, 520)]), fill=GRAPHITE)
    rounded(draw, (278, 548, 764, 617), radius=23, fill=INK)
    for center in ((382, 642), (666, 642)):
        x, y = center
        draw.ellipse(box((x - 70, y - 70, x + 70, y + 70)), fill=SHADOW, outline=INK, width=s(28))
        draw.ellipse(box((x - 20, y - 20, x + 20, y + 20)), fill=COPPER)


def draw_excavator(draw: ImageDraw.ImageDraw) -> None:
    rounded(draw, (266, 650, 690, 730), radius=39, fill=INK)
    rounded(draw, (315, 610, 650, 665), radius=25, fill=SHADOW, outline=INK, width=24)
    rounded(draw, (334, 446, 520, 616), radius=28, fill=INK)
    draw.polygon(points([(372, 476), (482, 476), (482, 548), (372, 548)]), fill=GRAPHITE)
    line(draw, [(495, 492), (620, 328), (735, 389)], fill=INK, width=55)
    line(draw, [(625, 333), (732, 493)], fill=INK, width=46)
    draw.polygon(points([(686, 474), (790, 438), (754, 566), (674, 548)]), fill=INK)
    draw.polygon(points([(718, 490), (767, 474), (748, 528), (696, 528)]), fill=COPPER)


def draw_mining_master(draw: ImageDraw.ImageDraw) -> None:
    draw.pieslice(box((294, 302, 730, 720)), start=180, end=360, fill=INK)
    rounded(draw, (270, 493, 754, 568), radius=30, fill=INK)
    rounded(draw, (472, 312, 550, 503), radius=22, fill=COPPER)
    line(draw, [(335, 646), (440, 566), (512, 624), (624, 522), (718, 646)], fill=INK, width=34)
    draw.polygon(points([(614, 569), (663, 616), (751, 516), (781, 547), (666, 681), (584, 603)]), fill=COPPER)


def draw_deputy(draw: ImageDraw.ImageDraw) -> None:
    rounded(draw, (284, 290, 740, 732), radius=48, fill=INK)
    rounded(draw, (328, 352, 696, 686), radius=25, fill=GRAPHITE)
    for x in (374, 512, 650):
        line(draw, [(x, 374), (x, 662)], fill=INK, width=18)
    for y in (442, 526, 610):
        line(draw, [(350, y), (674, y)], fill=INK, width=18)
    draw.polygon(points([(355, 628), (422, 561), (485, 609), (593, 484), (625, 514), (490, 676), (423, 625), (383, 665)]), fill=COPPER)


def draw_dispatcher(draw: ImageDraw.ImageDraw) -> None:
    nodes = [(512, 506), (335, 355), (690, 345), (318, 660), (706, 660)]
    for node in nodes[1:]:
        line(draw, [nodes[0], node], fill=INK, width=28)
    draw.ellipse(box((406, 400, 618, 612)), fill=SHADOW, outline=INK, width=s(36))
    draw.ellipse(box((468, 462, 556, 550)), fill=COPPER)
    for x, y in nodes[1:]:
        draw.ellipse(box((x - 55, y - 55, x + 55, y + 55)), fill=INK)
        draw.ellipse(box((x - 20, y - 20, x + 20, y + 20)), fill=GRAPHITE)
    draw.arc(box((344, 338, 680, 674)), start=205, end=335, fill=COPPER, width=s(24))


def draw_oup(draw: ImageDraw.ImageDraw) -> None:
    for x, y, radius in ((390, 402, 80), (584, 402, 80), (487, 330, 88)):
        draw.ellipse(box((x - radius, y - radius, x + radius, y + radius)), fill=INK)
    rounded(draw, (294, 500, 680, 716), radius=98, fill=INK)
    rounded(draw, (448, 476, 758, 720), radius=38, fill=GRAPHITE, outline=INK, width=28)
    rounded(draw, (500, 530, 706, 574), radius=18, fill=COPPER)
    for y in (622, 674):
        rounded(draw, (500, y, 682, y + 24), radius=12, fill=INK)


def gear_points(cx: int, cy: int, outer: int, inner: int, teeth: int = 10):
    import math

    values = []
    for index in range(teeth * 2):
        radius = outer if index % 2 == 0 else inner
        angle = -math.pi / 2 + (math.pi * index / teeth)
        values.append((cx + round(math.cos(angle) * radius), cy + round(math.sin(angle) * radius)))
    return values


def draw_mechanic(draw: ImageDraw.ImageDraw) -> None:
    draw.polygon(points(gear_points(520, 500, 218, 174, 12)), fill=INK)
    draw.ellipse(box((395, 375, 645, 625)), fill=GRAPHITE)
    draw.ellipse(box((462, 442, 578, 558)), fill=COPPER)
    line(draw, [(306, 700), (474, 532)], fill=COPPER, width=58)
    draw.polygon(points([(270, 746), (302, 640), (360, 698), (328, 804)]), fill=COPPER)
    draw.polygon(points([(660, 266), (754, 302), (690, 354), (760, 422), (708, 474), (588, 354)]), fill=COPPER)


def draw_management(draw: ImageDraw.ImageDraw) -> None:
    rounded(draw, (284, 594, 390, 716), radius=18, fill=INK)
    rounded(draw, (438, 486, 544, 716), radius=18, fill=INK)
    rounded(draw, (592, 370, 698, 716), radius=18, fill=INK)
    line(draw, [(292, 520), (456, 416), (548, 456), (716, 292)], fill=COPPER, width=38)
    draw.polygon(points([(666, 288), (758, 260), (730, 352)]), fill=COPPER)
    line(draw, [(270, 736), (748, 736)], fill=INK, width=28)


def draw_admin(draw: ImageDraw.ImageDraw) -> None:
    shield = [(512, 270), (724, 346), (690, 604), (512, 752), (334, 604), (300, 346)]
    draw.polygon(points(shield), fill=INK)
    inner = [(512, 332), (660, 384), (635, 568), (512, 670), (389, 568), (364, 384)]
    draw.polygon(points(inner), fill=GRAPHITE)
    draw.ellipse(box((452, 420, 572, 540)), fill=COPPER)
    rounded(draw, (486, 500, 538, 610), radius=22, fill=COPPER)


DRAWERS = {
    "driver": draw_driver,
    "excavator": draw_excavator,
    "mining-master": draw_mining_master,
    "deputy-mining-manager": draw_deputy,
    "dispatcher": draw_dispatcher,
    "oup": draw_oup,
    "mechanic": draw_mechanic,
    "management": draw_management,
    "admin": draw_admin,
}


def render_icon(role: str) -> Image.Image:
    role_color, _ = ROLE_ICONS[role]
    image = Image.new("RGB", (s(CANVAS), s(CANVAS)), GRAPHITE)
    draw = ImageDraw.Draw(image)
    draw_frame(draw, role_color)
    DRAWERS[role](draw)
    return image.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)


def save_role_icons(output_dir: Path) -> dict[str, Image.Image]:
    output_dir.mkdir(parents=True, exist_ok=True)
    masters = {}
    for role in ROLE_ICONS:
        master = render_icon(role)
        masters[role] = master
        for size in (180, 192, 512):
            target = master.resize((size, size), Image.Resampling.LANCZOS)
            target.save(output_dir / f"{role}-{size}.png", optimize=True)
        master.resize((512, 512), Image.Resampling.LANCZOS).save(
            output_dir / f"{role}-maskable-512.png",
            optimize=True,
        )
    return masters


def save_preview(masters: dict[str, Image.Image], preview_path: Path) -> None:
    tile = 320
    gap = 28
    preview = Image.new("RGB", (gap * 4 + tile * 3, gap * 4 + tile * 3), "#E8EDF0")
    for index, role in enumerate(ROLE_ICONS):
        row, column = divmod(index, 3)
        icon = masters[role].resize((tile, tile), Image.Resampling.LANCZOS)
        preview.paste(icon, (gap + column * (tile + gap), gap + row * (tile + gap)))
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(preview_path, optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Copper Resources role PWA icon family.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "static" / "img" / "pwa",
    )
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    masters = save_role_icons(args.output_dir)
    if args.preview:
        save_preview(masters, args.preview)


if __name__ == "__main__":
    main()
