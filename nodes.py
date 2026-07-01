"""MultiImageCompare 节点的后端实现。

设计要点
--------
1. **动态 N 个图片输入**：``image_1`` ... ``image_N`` 由前端 JS 动态增减，
   不在 ``INPUT_TYPES`` 里固定声明。靠 ``VALIDATE_INPUTS`` 返回 ``True``
   让后端接受这些未声明输入，全部作为 kwargs 传给 ``compare``。
2. **batch 多帧参与对比**：每个连入的 IMAGE 张量 ``[B,H,W,C]`` 的每一帧都
   被保存成独立图片，按 (输入序号, 帧序号) 顺序进入对比序列。
3. **工作流名称烘焙**：可选把自定义 ``label`` 文字烧到每张图左上角，
   便于导出/截图时区分来源。
4. **每端口合成图**：可选把每个输入端口的若干帧拼成一张图，
   作为 ``per_port`` 输出（batch）与 ``ui.composites`` 返回前端。
5. **拼图输出**：把所有帧横向拼成一张图，作为 ``IMAGE`` 输出 ``grid``。
6. ``compare`` 返回 ``{"result": (grid, per_port), "ui": {...}}``，
   前端 ``onExecuted`` 拿到 images / composites 列表后加载并渲染对比 widget。
"""

import math
import os
import uuid

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

import folder_paths  # ComfyUI 自带


# --------------------------------------------------------------------------- #
# 张量 / 图片工具
# --------------------------------------------------------------------------- #
def _tensor_to_pils(tensor):
    """IMAGE 张量 [B,H,W,C] 或 [H,W,C]（float 0..1）→ 每帧一张 PIL 图。"""
    t = tensor
    if hasattr(t, "dim"):
        if t.dim() == 3:
            t = t.unsqueeze(0)
    else:
        return []
    t = t.detach().cpu()
    pils = []
    for frame in t:
        arr = (frame.numpy() * 255.0).clip(0, 255).astype(np.uint8)
        pils.append(Image.fromarray(arr))
    return pils


def _pil_to_rgb(pil: Image.Image) -> Image.Image:
    """RGBA / 灰度等统一成 RGB（透明背景贴白底）。"""
    if pil.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", pil.size, (255, 255, 255))
        bg.paste(pil, mask=pil.split()[-1])
        return bg
    if pil.mode != "RGB":
        return pil.convert("RGB")
    return pil


def _save_temp(img: Image.Image, prefix: str = "mic") -> dict:
    out_dir = folder_paths.get_temp_directory()
    name = f"{prefix}_{uuid.uuid4().hex[:10]}.png"
    img.save(os.path.join(out_dir, name))
    return {"filename": name, "subfolder": "", "type": "temp"}


def _compose_grid(pils):
    """把若干 PIL 图横向拼成一张 RGB 图（高度统一到首图高度，上限 1024）。"""
    if not pils:
        return None
    target_h = max(64, min(pils[0].height, 1024))
    row = []
    for p in pils:
        p = _pil_to_rgb(p)
        w = max(1, int(p.width * target_h / p.height))
        row.append(p.resize((w, target_h), Image.LANCZOS))
    total_w = sum(im.width for im in row)
    canvas = Image.new("RGB", (total_w, target_h), (20, 20, 20))
    x = 0
    for im in row:
        canvas.paste(im, (x, 0))
        x += im.width
    return canvas


def _pil_to_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.array(_pil_to_rgb(img)).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)  # [1,H,W,3]


def _stack_uniform(pils):
    """把若干不同尺寸的 PIL 图统一到相同 H×W（等比缩放到目标高，宽度居中黑底填充）
    后堆叠成 [N,H,W,3] 张量，用于 batch 输出。"""
    rgb = [_pil_to_rgb(p) for p in pils if p is not None]
    if not rgb:
        return torch.zeros(1, 8, 8, 3)
    target_h = max(64, min(rgb[0].height, 1024))
    resized = []
    for p in rgb:
        w = max(1, int(p.width * target_h / p.height))
        resized.append(p.resize((w, target_h), Image.LANCZOS))
    max_w = max(im.width for im in resized)
    tensors = []
    for im in resized:
        if im.width < max_w:
            canvas = Image.new("RGB", (max_w, target_h), (0, 0, 0))
            canvas.paste(im, ((max_w - im.width) // 2, 0))
            im = canvas
        tensors.append(_pil_to_tensor(im))
    return torch.cat(tensors, dim=0)


# --------------------------------------------------------------------------- #
# 标签烘焙
# --------------------------------------------------------------------------- #
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "arial.ttf",
]
_font_cache = {}


def _load_font(size: int):
    """加载一个支持中文的字体，找不到则回退 PIL 默认字体。按 size 缓存。"""
    size = max(8, int(size))
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in _FONT_CANDIDATES:
        try:
            if os.path.isfile(path):
                font = ImageFont.truetype(path, size)
                break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size)  # 新版 PIL 支持
        except Exception:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
    _font_cache[size] = font
    return font


def _draw_label(pil: Image.Image, text: str, size: int) -> Image.Image:
    """在图片顶部画半透明黑底条 + 白字，返回新 RGB 图。无字体则原样返回。"""
    if not text:
        return _pil_to_rgb(pil)
    img = _pil_to_rgb(pil).copy()
    font = _load_font(size)
    if font is None:
        return img
    draw = ImageDraw.Draw(img, "RGBA")
    # 文字块尺寸（缺 ascent/descent 时给余量）
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = size * max(1, len(text)), size
    pad = max(4, size // 6)
    bar_h = th + pad * 2
    bar_w = tw + pad * 2
    # 顶部半透明黑底
    draw.rectangle((0, 0, bar_w, bar_h), fill=(0, 0, 0, 170))
    draw.text((pad, pad), text, fill=(255, 255, 255, 255), font=font)
    return img


# --------------------------------------------------------------------------- #
# 每端口合成
# --------------------------------------------------------------------------- #
def _compose_port_grid(pils, label: str = "", size: int = 32):
    """把同一端口的若干帧拼成一张 RGB 图（自动行列），可选烘焙标签。"""
    pils = [_pil_to_rgb(p) for p in pils if p is not None]
    if not pils:
        return None
    n = len(pils)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    # 用首帧宽高比定 cell，整体目标高度受控
    ref = pils[0]
    cell_h = max(64, min(ref.height, 1024))
    cell_w = max(1, int(ref.width * cell_h / ref.height))
    gap = 4
    canvas_w = cols * cell_w + (cols + 1) * gap
    canvas_h = rows * cell_h + (rows + 1) * gap
    canvas = Image.new("RGB", (canvas_w, canvas_h), (20, 20, 20))
    for i, p in enumerate(pils):
        r, c = divmod(i, cols)
        cell = p.resize((cell_w, cell_h), Image.LANCZOS)
        x = gap + c * (cell_w + gap)
        y = gap + r * (cell_h + gap)
        canvas.paste(cell, (x, y))
    if label:
        canvas = _draw_label(canvas, label, size)
    return canvas


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
def _scalar(v):
    """防御性解包：个别内核会把 widget 标量值包成单元素 list。"""
    if isinstance(v, list) and len(v) == 1:
        return v[0]
    return v


class MultiImageCompareNode:
    """N 张 / 多帧图片对比节点，输出横向拼图 + 每端口合成图。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "label": ("STRING", {"multiline": True, "default": "", "tooltip": "打印到图片上的文字。每行对应一个输入端口：第1行→image_1，第2行→image_2… 端口数超出行数则该端口不打印。"}),
                "print_label": ("BOOLEAN", {"default": True, "tooltip": "是否把 label 文字烧到图片左上角"}),
                "label_size": ("INT", {"default": 32, "min": 8, "max": 200, "step": 1}),
            },
            "optional": {},
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("grid",)
    FUNCTION = "compare"
    CATEGORY = "image"
    OUTPUT_NODE = True

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        # 返回 True 后端就不会因为 image_* 未声明而报错。
        return True

    def compare(self, **kwargs):
        # ---- widget 输入（防御性解包）----
        label = _scalar(kwargs.get("label", "")) or ""
        label = str(label)
        print_label = bool(_scalar(kwargs.get("print_label", True)))
        label_size = int(_scalar(kwargs.get("label_size", 32)))

        # label 支持每端口不同：每行对应一个端口（第1行=端口1，第2行=端口2…）。
        # 端口数超出行数时该端口不打印；行数多出时忽略。
        label_lines = [ln for ln in label.split("\n")]
        port_label = lambda n: (label_lines[n - 1].strip() if (n - 1) < len(label_lines) else "")

        # ---- 收集所有 image_* 输入，按后缀数字排序 ----
        items = []
        for key, value in kwargs.items():
            if not key.startswith("image_") or value is None:
                continue
            if not (hasattr(value, "dim") and hasattr(value, "numpy")):
                continue
            try:
                idx = int(key.split("_", 1)[1])
            except (ValueError, IndexError):
                idx = 0
            items.append((idx, key, value))
        items.sort(key=lambda x: x[0])

        images = []            # 给前端逐帧对比 widget 用
        all_pils = []          # 所有端口所有帧（已烘焙标签）→ 合成一张大图
        port_labels_used = []  # 回传前端：每个端口实际用的标签

        for input_no, (_idx, _key, tensor) in enumerate(items, start=1):
            try:
                pils = _tensor_to_pils(tensor)
            except Exception as err:
                print(f"[MultiImageCompare] 读取第 {input_no} 个输入失败: {err}")
                continue

            this_label = port_label(input_no) if print_label else ""
            port_labels_used.append(this_label)

            # 逐帧：可选烘焙【本端口】的标签
            for frame_no, pil in enumerate(pils):
                if print_label and this_label:
                    pil = _draw_label(pil, this_label, label_size)
                all_pils.append(pil)
                info = _save_temp(pil)
                info["input"] = input_no
                info["frame"] = frame_no
                images.append(info)

        # ---- grid 输出：所有端口所有帧横向并列合成一张大图（不另起一行）----
        if all_pils:
            grid = _pil_to_tensor(_compose_grid(all_pils))
        else:
            grid = torch.zeros(1, 8, 8, 3)

        # 注意：ui 字典的 value 必须是「列表」。
        return {
            "result": (grid,),
            "ui": {
                "images": images,
                "label": [label],
                "print_label": [print_label],
                "labels": port_labels_used,
            },
        }
