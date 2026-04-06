#!/usr/bin/env python3
"""
gif_to_frames.py — Convert a GIF to ASCII frames you can paste into pet_animations.py

Install deps:  pip install Pillow

Usage:
    python gif_to_frames.py cookie.gif
    python gif_to_frames.py cookie.gif --width 20
    python gif_to_frames.py cookie.gif --width 20 --every 3   # sample every 3rd frame
    python gif_to_frames.py cookie.gif --preview              # play in terminal first
    python gif_to_frames.py cookie.gif --braille --width 48 --preview
        # braille packs 2x4 pixels per character cell — half the width, same detail as 2x

Braille tip: --width 48 in braille ≈ --width 96 in ASCII, fits a normal terminal.
"""

import sys
import time
import argparse
from PIL import Image

# ── ASCII mode ────────────────────────────────────────────────────────────────
# Characters from dark → light (works on dark terminal backgrounds)
CHARS = ["@", "#", "S", "%", "?", "*", "+", ";", ":", ",", ".", " "]

def pixel_to_char(brightness: int) -> str:
    idx = int(brightness / 255 * (len(CHARS) - 1))
    return CHARS[idx]

def frame_to_ascii(frame: Image.Image, width: int) -> list[str]:
    # Each terminal character is ~2x taller than wide, so halve the height
    aspect = frame.height / frame.width
    height = int(width * aspect * 0.45)
    height = max(height, 1)

    img = frame.convert("L").resize((width, height), Image.LANCZOS)
    lines = []
    for y in range(height):
        row = "".join(pixel_to_char(img.getpixel((x, y))) for x in range(width))
        lines.append(row)
    return lines

# ── Braille mode ──────────────────────────────────────────────────────────────
# Each braille character is a 2-wide × 4-tall pixel grid.
# Dot layout and their bit positions in the Unicode braille block (U+2800):
#   col0  col1
#    1(0)  4(3)   ← row 0
#    2(1)  5(4)   ← row 1
#    3(2)  6(5)   ← row 2
#    7(6)  8(7)   ← row 3
BRAILLE_BASE = 0x2800
DOT_BITS = [
    [0, 3],   # row 0: left=bit0, right=bit3
    [1, 4],   # row 1: left=bit1, right=bit4
    [2, 5],   # row 2: left=bit2, right=bit5
    [6, 7],   # row 3: left=bit6, right=bit7
]

def frame_to_braille(frame: Image.Image, width: int, threshold: int = 128) -> list[str]:
    """
    Convert frame to braille art.
    width = number of braille characters per line (each covers 2 pixel columns).
    The pixel grid is  (width*2) × (char_rows*4).
    Threshold: pixels darker than this value are filled dots (good for dark-bg terminals).
    """
    px_w = width * 2
    aspect = frame.height / frame.width
    # 4 pixel rows per braille row, chars are ~2x taller than wide
    char_rows = max(1, int(px_w * aspect * 0.45 / 4))
    px_h = char_rows * 4

    img = frame.convert("L").resize((px_w, px_h), Image.LANCZOS)
    pixels = img.load()

    lines = []
    for cy in range(char_rows):
        row = ""
        for cx in range(width):
            code = BRAILLE_BASE
            for dr, (bl, br) in enumerate(DOT_BITS):
                py = cy * 4 + dr
                # left dot
                if pixels[cx * 2, py] < threshold:
                    code |= (1 << bl)
                # right dot
                if cx * 2 + 1 < px_w and pixels[cx * 2 + 1, py] < threshold:
                    code |= (1 << br)
            row += chr(code)
        lines.append(row)
    return lines

# ── Frame extraction ──────────────────────────────────────────────────────────

def extract_frames(path: str, width: int, every: int, braille: bool) -> list[list[str]]:
    gif = Image.open(path)
    frames = []
    i = 0
    converter = frame_to_braille if braille else frame_to_ascii
    try:
        while True:
            gif.seek(i)
            if i % every == 0:
                frames.append(converter(gif.copy(), width))
            i += 1
    except EOFError:
        pass
    return frames

def preview(frames: list[list[str]], fps: float = 10):
    """Play the ASCII animation in the terminal."""
    import os
    delay = 1 / fps
    height = len(frames[0])
    UP = "\033[A"
    CLEAR = "\033[2K\r"

    print("\n" * height)
    try:
        while True:
            for frame in frames:
                for line in frame:
                    print(line)
                time.sleep(delay)
                # erase
                for _ in range(height):
                    sys.stdout.write(UP + CLEAR)
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n\nDone previewing.")

def print_python(frames: list[list[str]], name: str):
    """Print frames formatted as a Python list to paste into your script."""
    print(f"\n# ── Paste this into your PETS / MOODS dict ──────────────────────────")
    print(f'"{name}": {{')
    print(f'    "msg": ["...", "...", "...", "..."],')
    print(f'    "frames": [')
    for i, frame in enumerate(frames):
        print(f'        # Frame {i}')
        print(f'        [')
        for line in frame:
            # Escape backslashes so the string is valid Python
            escaped = line.replace("\\", "\\\\")
            print(f'            "{escaped}",')
        print(f'        ],')
    print(f'    ],')
    print(f'}},')
    print()

def main():
    parser = argparse.ArgumentParser(
        description="Convert GIF to ASCII (or braille) frames",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python gif_to_frames.py cookie.gif --width 48 --preview
  python gif_to_frames.py cookie.gif --braille --width 48 --preview
  python gif_to_frames.py cookie.gif --braille --width 48 --every 3 --name cookie_monster
        """
    )
    parser.add_argument("gif",                                   help="Path to GIF file")
    parser.add_argument("--width",   type=int,   default=48,     help="Width in chars (default 48)")
    parser.add_argument("--every",   type=int,   default=1,      help="Sample every Nth GIF frame (default 1 = all)")
    parser.add_argument("--name",    type=str,   default="my_animation", help="Variable name in Python output")
    parser.add_argument("--preview", action="store_true",        help="Play in terminal instead of printing frames")
    parser.add_argument("--fps",     type=float, default=10,     help="Preview playback FPS (default 10)")
    parser.add_argument("--braille", action="store_true",        help="Use braille chars — 2x detail at same terminal width")
    parser.add_argument("--threshold", type=int, default=128,    help="Braille dot threshold 0-255 (default 128, lower=more dots)")
    args = parser.parse_args()

    mode = "braille" if args.braille else "ASCII"
    print(f"Loading {args.gif}... (mode: {mode}, width: {args.width})")
    frames = extract_frames(args.gif, args.width, args.every, args.braille)
    print(f"Extracted {len(frames)} frames → {len(frames[0])} lines × {args.width} chars each")

    if args.preview:
        print("Previewing... Ctrl+C to stop\n")
        preview(frames, fps=args.fps)
    else:
        print_python(frames, args.name)

if __name__ == "__main__":
    main()