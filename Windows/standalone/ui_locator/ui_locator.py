#!/usr/bin/env python3
"""
Standalone UI locator:
- Reads a screenshot or captures the live screen
- Detects probable buttons and input boxes
- Extracts visible text labels using OCR
- Moves the mouse to a selected element center (by name or selection)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Sequence, Tuple

import cv2
import numpy as np
import pyautogui
import pytesseract


@dataclass
class UIElement:
    element_type: str
    name: str
    bbox: Tuple[int, int, int, int]  # x, y, w, h
    center: Tuple[int, int]
    score: float


def _load_image(image_path: Path) -> np.ndarray:
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"Could not read screenshot: {image_path}")
    return image


def _read_ocr_words(image: np.ndarray) -> List[dict]:
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    ocr = pytesseract.image_to_data(
        rgb,
        output_type=pytesseract.Output.DICT,
        config="--oem 3 --psm 6",
    )
    words: List[dict] = []
    for i, text in enumerate(ocr["text"]):
        clean = text.strip()
        if not clean:
            continue
        if int(ocr["conf"][i]) < 20:
            continue
        words.append(
            {
                "text": clean,
                "x": int(ocr["left"][i]),
                "y": int(ocr["top"][i]),
                "w": int(ocr["width"][i]),
                "h": int(ocr["height"][i]),
            }
        )
    return words


def _is_inside(box: Tuple[int, int, int, int], word: dict, margin: int = 6) -> bool:
    x, y, w, h = box
    return (
        word["x"] >= x - margin
        and word["y"] >= y - margin
        and word["x"] + word["w"] <= x + w + margin
        and word["y"] + word["h"] <= y + h + margin
    )


def _name_for_box(box: Tuple[int, int, int, int], words: Sequence[dict]) -> str:
    inside = [w for w in words if _is_inside(box, w)]
    if not inside:
        x, y, w, h = box
        box_mid_y = y + (h // 2)
        near_line: List[dict] = []
        for word in words:
            word_mid_y = word["y"] + (word["h"] // 2)
            if abs(word_mid_y - box_mid_y) > max(18, h // 2):
                continue
            if word["x"] + word["w"] < x - 10 or word["x"] > x + w + 10:
                continue
            near_line.append(word)
        inside = near_line
    if not inside:
        return ""
    inside.sort(key=lambda item: (item["y"], item["x"]))
    text = " ".join(w["text"] for w in inside)
    return re.sub(r"\s+", " ", text).strip()


def _classify_box(box: Tuple[int, int, int, int], text: str) -> Tuple[str, float]:
    _, _, w, h = box
    ratio = w / max(h, 1)
    lowered = text.lower()

    # Input-like geometry and common field names.
    if ratio >= 3.6 and h <= 80:
        return "input", 0.78
    if any(
        token in lowered
        for token in (
            "email",
            "username",
            "password",
            "search",
            "phone",
            "name",
            "address",
            "message",
        )
    ):
        return "input", 0.72

    # Button-like geometry and action words.
    if 1.4 <= ratio <= 8.0 and 18 <= h <= 110:
        return "button", 0.68
    if any(
        token in lowered
        for token in (
            "submit",
            "save",
            "cancel",
            "login",
            "sign in",
            "sign up",
            "continue",
            "next",
            "back",
            "start",
            "ok",
            "send",
        )
    ):
        return "button", 0.75

    return "unknown", 0.4


def detect_elements_bgr(image: np.ndarray) -> List[UIElement]:
    """Run detection on a BGR image (OpenCV), e.g. from a live screen capture."""
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Expected a BGR image with shape (H, W, 3)")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    words = _read_ocr_words(image)

    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 60, 170)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: List[UIElement] = []
    unnamed_counts = {"button": 0, "input": 0}
    img_h, img_w = gray.shape
    min_area = max(900, int((img_h * img_w) * 0.00025))
    max_area = int((img_h * img_w) * 0.25)

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h

        if area < min_area or area > max_area:
            continue
        if w < 40 or h < 16:
            continue

        box = (x, y, w, h)
        name = _name_for_box(box, words)
        element_type, score = _classify_box(box, name)

        if element_type == "unknown":
            continue

        center = (x + w // 2, y + h // 2)
        display_name = name
        if not display_name:
            unnamed_counts[element_type] += 1
            display_name = f"{element_type.title()} {unnamed_counts[element_type]}"

        candidates.append(
            UIElement(
                element_type=element_type,
                name=display_name,
                bbox=box,
                center=center,
                score=score,
            )
        )

    # Add OCR-only fallback action labels that may not have visible borders.
    action_words = {
        "submit",
        "save",
        "cancel",
        "login",
        "continue",
        "next",
        "back",
        "send",
        "ok",
    }
    for word in words:
        clean = word["text"].strip().lower()
        if clean not in action_words:
            continue
        box = (word["x"], word["y"], word["w"], word["h"])
        center = (word["x"] + word["w"] // 2, word["y"] + word["h"] // 2)
        candidates.append(
            UIElement(
                element_type="button",
                name=word["text"],
                bbox=box,
                center=center,
                score=0.55,
            )
        )

    # Deduplicate overlapping boxes (keep higher score).
    final: List[UIElement] = []
    for element in sorted(candidates, key=lambda item: item.score, reverse=True):
        ex, ey, ew, eh = element.bbox
        keep = True
        for existing in final:
            xx, yy, ww, hh = existing.bbox
            inter_w = max(0, min(ex + ew, xx + ww) - max(ex, xx))
            inter_h = max(0, min(ey + eh, yy + hh) - max(ey, yy))
            inter = inter_w * inter_h
            union = (ew * eh) + (ww * hh) - inter
            iou = inter / union if union else 0.0
            if iou > 0.35:
                keep = False
                break
        if keep:
            final.append(element)

    return sorted(final, key=lambda item: (item.bbox[1], item.bbox[0]))


def detect_elements(image_path: Path) -> List[UIElement]:
    return detect_elements_bgr(_load_image(image_path))


def capture_screen_bgr() -> np.ndarray:
    """Grab the current screen as BGR (matches OpenCV / detect_elements_bgr)."""
    pil = pyautogui.screenshot()
    rgb = np.array(pil.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def go_to_target_on_live_screen(
    target: str,
    *,
    dry_run: bool = False,
    duration: float = 0.2,
    screen_width: int = 0,
    screen_height: int = 0,
) -> dict:
    """
    Capture the live screen, detect UI elements, pick best match for ``target``,
    move the mouse to that element center (with scaling if capture size != logical screen).
    """
    image = capture_screen_bgr()
    img_h, img_w = image.shape[:2]
    elements = detect_elements_bgr(image)
    chosen = _choose_target(elements, target)

    if screen_width and screen_height:
        target_size = (screen_width, screen_height)
    else:
        size = pyautogui.size()
        target_size = (int(size.width), int(size.height))

    move_x, move_y = _scale_center(chosen.center, (img_w, img_h), target_size)
    if not dry_run:
        pyautogui.moveTo(move_x, move_y, duration=duration)

    return {
        "selected": asdict(chosen),
        "cursor_position": {"x": move_x, "y": move_y},
        "image_size": {"width": img_w, "height": img_h},
        "target_screen_size": {"width": target_size[0], "height": target_size[1]},
    }


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _choose_target(elements: Sequence[UIElement], query: str) -> UIElement:
    query_n = _normalize(query)
    if not query_n:
        raise ValueError("Target query is empty after normalization.")

    best: UIElement | None = None
    best_score = -1.0

    for element in elements:
        name_n = _normalize(element.name)
        sim = SequenceMatcher(None, query_n, name_n).ratio()
        type_bonus = 0.06 if query_n in name_n else 0.0
        score = sim + type_bonus + (0.05 * element.score)
        if score > best_score:
            best = element
            best_score = score

    if best is None or best_score < 0.32:
        raise ValueError(f"No strong match found for target: '{query}'")
    return best


def _scale_center(
    center: Tuple[int, int],
    image_size: Tuple[int, int],
    target_size: Tuple[int, int],
) -> Tuple[int, int]:
    cx, cy = center
    img_w, img_h = image_size
    target_w, target_h = target_size

    scale_x = target_w / max(img_w, 1)
    scale_y = target_h / max(img_h, 1)
    return int(round(cx * scale_x)), int(round(cy * scale_y))


def command_list(args: argparse.Namespace) -> int:
    screenshot = Path(args.screenshot).expanduser().resolve()
    elements = detect_elements(screenshot)
    payload = [asdict(item) for item in elements]
    print(json.dumps(payload, indent=2))
    return 0


def command_move(args: argparse.Namespace) -> int:
    screenshot = Path(args.screenshot).expanduser().resolve()
    image = _load_image(screenshot)
    img_h, img_w = image.shape[:2]
    elements = detect_elements(screenshot)
    target = _choose_target(elements, args.target)

    if args.screen_width and args.screen_height:
        target_size = (args.screen_width, args.screen_height)
    else:
        size = pyautogui.size()
        target_size = (int(size.width), int(size.height))

    move_x, move_y = _scale_center(target.center, (img_w, img_h), target_size)
    if not args.dry_run:
        pyautogui.moveTo(move_x, move_y, duration=args.duration)

    print(
        json.dumps(
            {
                "selected": asdict(target),
                "cursor_position": {"x": move_x, "y": move_y},
                "image_size": {"width": img_w, "height": img_h},
                "target_screen_size": {"width": target_size[0], "height": target_size[1]},
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect UI buttons/inputs from a screenshot and move cursor to them."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List detected UI elements in JSON.")
    list_parser.add_argument("--screenshot", required=True, help="Path to screenshot image.")
    list_parser.set_defaults(func=command_list)

    move_parser = sub.add_parser("move", help="Move cursor to the best-matching UI element.")
    move_parser.add_argument("--screenshot", required=True, help="Path to screenshot image.")
    move_parser.add_argument("--target", required=True, help="Element label to find.")
    move_parser.add_argument("--duration", type=float, default=0.15, help="Mouse move duration.")
    move_parser.add_argument("--screen-width", type=int, default=0, help="Target screen width.")
    move_parser.add_argument("--screen-height", type=int, default=0, help="Target screen height.")
    move_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only compute and print target point without moving cursor.",
    )
    move_parser.set_defaults(func=command_move)

    live_parser = sub.add_parser(
        "live",
        help="Capture the current screen, detect UI, move cursor to best name match.",
    )
    live_parser.add_argument("--target", required=True, help="Button or field label to find.")
    live_parser.add_argument("--duration", type=float, default=0.2, help="Mouse move duration.")
    live_parser.add_argument("--screen-width", type=int, default=0, help="Logical screen width.")
    live_parser.add_argument("--screen-height", type=int, default=0, help="Logical screen height.")
    live_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only compute and print target point without moving cursor.",
    )
    live_parser.set_defaults(func=command_live)

    return parser


def command_live(args: argparse.Namespace) -> int:
    result = go_to_target_on_live_screen(
        args.target,
        dry_run=args.dry_run,
        duration=args.duration,
        screen_width=args.screen_width or 0,
        screen_height=args.screen_height or 0,
    )
    print(json.dumps(result, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
