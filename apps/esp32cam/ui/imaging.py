"""画像を Tkinter に表示するための共通処理。"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image


def frame_to_image(frame: np.ndarray) -> Image.Image:
    """OpenCV の BGR フレームを Pillow の画像に変換する。"""
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def fit_image(
    image: Image.Image,
    width: int,
    height: int,
    resample: int = Image.LANCZOS,
) -> Image.Image:
    """縦横比を保ったまま、指定した表示領域に収まるよう拡大縮小する。

    領域サイズがまだ確定していない（1 以下の）場合は元の画像をそのまま返す。
    多数の映像を並べて表示する場合は resample に Image.BILINEAR を渡すと軽い。
    """
    if width <= 1 or height <= 1:
        return image

    scale = min(width / image.width, height / image.height)
    size = (
        max(int(image.width * scale), 1),
        max(int(image.height * scale), 1),
    )
    return image.resize(size, resample)
