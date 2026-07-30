"""Generate deterministic Parsezen brand derivatives from the official masters.

The supplied light and dark masters contain presentation backgrounds. This
script removes those backgrounds without redrawing the logo, crops transparent
margins and resizes the exact remaining pixels with a high-quality filter.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from statistics import median

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BRANDING_ROOT = ROOT / "assets" / "branding"
MASTER_PATH = BRANDING_ROOT / "masters" / "parsezen-light-master.png"
DARK_REFERENCE_PATH = BRANDING_ROOT / "masters" / "parsezen-dark-reference.png"
GENERATED_ROOT = BRANDING_ROOT / "generated"
ALPHA_THRESHOLD = 8
BACKGROUND_TRANSPARENT_DELTA = 10
BACKGROUND_OPAQUE_DELTA = 72
LOGO_HEIGHTS = {
    "parsezen-logo-light.png": 96,
    "parsezen-logo-light@2x.png": 192,
}
DARK_LOGO_HEIGHTS = {
    "parsezen-logo-dark.png": 96,
    "parsezen-logo-dark@2x.png": 192,
}
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _clean_visible_image(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    visible = alpha.point(lambda value: 255 if value >= ALPHA_THRESHOLD else 0)
    bounding_box = visible.getbbox()
    if bounding_box is None:
        raise ValueError("The Parsezen master does not contain visible pixels.")

    cropped = rgba.crop(bounding_box)
    cleaned_alpha = cropped.getchannel("A").point(
        lambda value: value if value >= ALPHA_THRESHOLD else 0
    )
    cropped.putalpha(cleaned_alpha)
    return cropped


def _remove_baked_background(image: Image.Image) -> Image.Image:
    """Recover alpha from a light or dark presentation background.

    The background of both official source images is virtually uniform along
    each scanline. Sampling both outer edges preserves the supplied gradients
    and antialiasing while excluding the surrounding canvas. The foreground
    color is unblended from that sampled background so the derivatives remain
    clean on any application surface.
    """

    source = image.convert("RGB")
    width, height = source.size
    edge_width = max(8, min(48, width // 24))
    output = Image.new("RGBA", source.size, (0, 0, 0, 0))
    source_pixels = source.load()
    output_pixels = output.load()

    for y in range(height):
        edge_pixels = [
            source_pixels[x, y] for x in (*range(edge_width), *range(width - edge_width, width))
        ]
        background = tuple(
            round(median(pixel[channel] for pixel in edge_pixels)) for channel in range(3)
        )
        for x in range(width):
            pixel = source_pixels[x, y]
            delta = max(abs(pixel[channel] - background[channel]) for channel in range(3))
            if delta <= BACKGROUND_TRANSPARENT_DELTA:
                continue
            if delta >= BACKGROUND_OPAQUE_DELTA:
                alpha = 255
            else:
                alpha = round(
                    255
                    * (delta - BACKGROUND_TRANSPARENT_DELTA)
                    / (BACKGROUND_OPAQUE_DELTA - BACKGROUND_TRANSPARENT_DELTA)
                )
            opacity = alpha / 255
            foreground = tuple(
                max(
                    0,
                    min(
                        255,
                        round((pixel[channel] - background[channel] * (1 - opacity)) / opacity),
                    ),
                )
                for channel in range(3)
            )
            output_pixels[x, y] = (*foreground, alpha)
    return output


def _resize_to_height(image: Image.Image, height: int) -> Image.Image:
    width = max(1, round(image.width * height / image.height))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _extract_symbol(logo: Image.Image) -> Image.Image:
    """Extract the exact symbol using the transparent gap before the wordmark."""

    alpha = logo.getchannel("A")
    occupied_columns = []
    for x in range(logo.width):
        column = alpha.crop((x, 0, x + 1, logo.height))
        occupied_columns.append(column.getbbox() is not None)

    gaps: list[tuple[int, int]] = []
    start: int | None = None
    for index, occupied in enumerate((*occupied_columns, True)):
        if not occupied and start is None:
            start = index
        elif occupied and start is not None:
            gaps.append((start, index))
            start = None

    candidates = [
        (start, end) for start, end in gaps if start > logo.width * 0.1 and end < logo.width * 0.55
    ]
    if not candidates:
        raise ValueError("The separator between the symbol and wordmark was not found.")
    separator_start, _separator_end = max(candidates, key=lambda gap: gap[1] - gap[0])
    return logo.crop((0, 0, separator_start, logo.height))


def _square_symbol(symbol: Image.Image, size: int = 1024) -> Image.Image:
    margin = round(size * 0.1)
    available = size - margin * 2
    scale = min(available / symbol.width, available / symbol.height)
    resized = symbol.resize(
        (max(1, round(symbol.width * scale)), max(1, round(symbol.height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(
        resized,
        ((size - resized.width) // 2, (size - resized.height) // 2),
    )
    return canvas


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate() -> None:
    GENERATED_ROOT.mkdir(parents=True, exist_ok=True)
    logo = _clean_visible_image(_remove_baked_background(Image.open(MASTER_PATH)))
    dark_logo = _clean_visible_image(_remove_baked_background(Image.open(DARK_REFERENCE_PATH)))
    symbol = _square_symbol(_extract_symbol(logo))

    generated_paths: list[Path] = []
    for filename, height in LOGO_HEIGHTS.items():
        output_path = GENERATED_ROOT / filename
        _resize_to_height(logo, height).save(output_path, optimize=True)
        generated_paths.append(output_path)
    for filename, height in DARK_LOGO_HEIGHTS.items():
        output_path = GENERATED_ROOT / filename
        _resize_to_height(dark_logo, height).save(output_path, optimize=True)
        generated_paths.append(output_path)

    readme_path = GENERATED_ROOT / "parsezen-readme.png"
    readme_logo = logo.resize(
        (480, max(1, round(logo.height * 480 / logo.width))),
        Image.Resampling.LANCZOS,
    )
    readme_logo.save(readme_path, optimize=True)
    generated_paths.append(readme_path)

    symbol_path = GENERATED_ROOT / "parsezen-symbol-1024.png"
    symbol.save(symbol_path, optimize=True)
    generated_paths.append(symbol_path)

    icon_path = GENERATED_ROOT / "parsezen-app-icon.ico"
    symbol.save(icon_path, sizes=[(size, size) for size in ICON_SIZES])
    generated_paths.append(icon_path)

    manifest_path = GENERATED_ROOT / "manifest.json"
    manifest = {
        "source": str(MASTER_PATH.relative_to(ROOT)).replace("\\", "/"),
        "source_sha256": _sha256(MASTER_PATH),
        "dark_reference": str(DARK_REFERENCE_PATH.relative_to(ROOT)).replace("\\", "/"),
        "dark_reference_sha256": _sha256(DARK_REFERENCE_PATH),
        "alpha_threshold": ALPHA_THRESHOLD,
        "background_extraction": {
            "transparent_delta": BACKGROUND_TRANSPARENT_DELTA,
            "opaque_delta": BACKGROUND_OPAQUE_DELTA,
            "sampling": "scanline-edge-median",
        },
        "outputs": {
            path.name: {
                "sha256": _sha256(path),
                "size": list(Image.open(path).size),
            }
            for path in generated_paths
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    generate()
