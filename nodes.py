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
import json
import re
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


def _graph_link(value, prompt):
    return (isinstance(value, list) and len(value) == 2
            and isinstance(value[1], int) and str(value[0]) in prompt)


def _config_value(value, prompt):
    if not _graph_link(value, prompt):
        return value
    node = prompt[str(value[0])]
    inputs = node.get("inputs", {})
    # Only resolve known literal providers; computed outputs are not widget values.
    if node.get("class_type") == "Seed (rgthree)" and value[1] == 0:
        return inputs.get("seed", "未知")
    if node.get("class_type") in ("PrimitiveNode", "PrimitiveInt", "PrimitiveFloat", "PrimitiveString"):
        result = inputs.get("value")
        if not _graph_link(result, prompt):
            return result
    return f"动态输入 #{value[0]}:{value[1]}（运行值未知）"


def _lora_config(text):
    try:
        data = json.loads(str(text))
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
    except (ValueError, TypeError):
        pass
    result = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        match = re.match(r"^(.+?)\s*[:|,]\s*(-?\d+(?:\.\d+)?)\s*$", line)
        result.append({"n": match[1].strip() if match else line,
                       "s": float(match[2]) if match else 1.0})
    return result


def _generation_info(prompt, link):
    lines, seen = [], set()

    def visit(edge):
        if not _graph_link(edge, prompt) or str(edge[0]) in seen:
            return
        node_id = str(edge[0])
        seen.add(node_id)
        node = prompt[node_id]
        kind, inputs = node.get("class_type", ""), node.get("inputs", {})
        val = lambda key: _config_value(inputs.get(key), prompt)
        if kind == "BinyuanUltimateSamplerEN":
            # 英文/双语版采样器：信息项与中文版一致，标签保持英文，不做任何中文化。
            lines.append(f"Sampler #{node_id} · committed config")
            inherit = ("Inherit upstream" in str(inputs.get("chaining_mode", ""))
                       and _graph_link(inputs.get("external_model"), prompt))
            if inherit:
                lines.append("Model: inherits upstream model, see below")
            else:
                key = "checkpoint" if "whole file" in str(inputs.get("load_mode", "")) else "diffusion_model"
                lines.append(f"Model: {val(key)}")
            for key, label in (("weight_precision", "Weight precision"), ("clip_1", "CLIP 1"),
                               ("clip_2", "CLIP 2"), ("vae", "VAE")):
                if val(key) not in (None, "None", ""):
                    lines.append(f"{label}: {val(key)}")
            raw = inputs.get("lora_json", "[]")
            if _graph_link(raw, prompt):
                lines.append("LoRA: " + str(val("lora_json")))
            else:
                entries = _lora_config(raw)
                active = 0
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("n") or entry.get("name")
                    if not name or name == "None":
                        continue
                    strength = entry.get("s", entry.get("strength", 1.0))
                    sm = entry.get("sm") if entry.get("sm") is not None else strength
                    sc = entry.get("sc") if entry.get("sc") is not None else strength
                    enabled = entry.get("e", True)
                    active += bool(enabled)
                    state = "enabled" if enabled else "disabled"
                    lines.extend([f"LoRA ({state}): {name}", f"  Model weight={sm} | CLIP weight={sc}"])
                if not active:
                    lines.append("Node LoRA: none enabled")
            if "lora_settings" in inputs:
                lines.append("Extra LoRA settings: " + str(val("lora_settings")))
            for keys, labels in ((("seed", "steps", "cfg"), ("seed", "Steps", "CFG")),
                                 (("sampler", "scheduler", "denoise"), ("Sampler", "Scheduler", "Denoise")),
                                 (("flux_guidance",), ("Flux guidance",))):
                lines.append(" | ".join(f"{label}={val(key)}" for key, label in zip(keys, labels) if key in inputs))
            for key, label in (("external_sigmas", "External sigmas"),
                               ("external_positive", "External positive conditioning"),
                               ("external_negative", "External negative conditioning")):
                if key in inputs:
                    lines.append(f"{label}: connected (overrides internal)")
            if inherit:
                visit(inputs["external_model"])
            for key, label in (("upstream_image_1", "Upstream image 1"),
                               ("upstream_image_2", "Upstream image 2"),
                               ("upstream_image_3", "Upstream image 3")):
                if key in inputs:
                    lines.append(f"{label} source:")
                    visit(inputs[key])
            return
        if kind == "BinyuanUltimateSampler":
            lines.append(f"采样器 #{node_id} · 提交配置")
            inherit = inputs.get("串联模式") == "继承上游模型" and _graph_link(inputs.get("外部模型"), prompt)
            if inherit:
                lines.append("模型: 继承外部模型，来源见下方")
            else:
                key = "Checkpoint" if inputs.get("加载模式") == "整包Checkpoint" else "扩散模型"
                lines.append(f"模型: {val(key)}")
            for key in ("权重精度", "CLIP_1", "CLIP_2", "VAE"):
                if val(key) not in (None, "None", ""):
                    lines.append(f"{key}: {val(key)}")
            raw = inputs.get("lora_json", "[]")
            if _graph_link(raw, prompt):
                lines.append("LoRA: " + str(val("lora_json")))
            else:
                entries = _lora_config(raw)
                active = 0
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("n") or entry.get("name")
                    if not name or name == "None":
                        continue
                    strength = entry.get("s", entry.get("strength", 1.0))
                    sm = entry.get("sm") if entry.get("sm") is not None else strength
                    sc = entry.get("sc") if entry.get("sc") is not None else strength
                    enabled = entry.get("e", True)
                    active += bool(enabled)
                    state = "配置" if enabled else "已禁用"
                    lines.extend([f"LoRA ({state}): {name}", f"  模型权重={sm} | CLIP权重={sc}"])
                if not active:
                    lines.append("本节点 LoRA: 无启用配置")
            if "LoRA设置" in inputs:
                lines.append("额外 LoRA设置: " + str(val("LoRA设置")))
            for keys in (("seed", "步数", "CFG"), ("采样算法", "调度器", "重绘强度"), ("Flux引导",)):
                lines.append(" | ".join(f"{key}={val(key)}" for key in keys if key in inputs))
            for key in ("外部Sigmas", "外部正面条件", "外部负面条件"):
                if key in inputs:
                    lines.append(f"{key}: 已接入（覆盖对应内部配置）")
            if inherit:
                visit(inputs["外部模型"])
            for key in ("上游图像_1", "上游图像_2", "上游图像_3"):
                if key in inputs:
                    lines.append(f"{key} 来源:")
                    visit(inputs[key])
            return
        fields = ("ckpt_name", "unet_name", "model_name", "lora_name", "strength_model", "strength_clip",
                  "seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler", "denoise", "vae_name")
        details = [f"{key}={val(key)}" for key in fields if key in inputs]
        if details:
            lines.append(f"{kind} #{node_id}")
            lines.extend(details)
        for value in inputs.values():
            if _graph_link(value, prompt):
                visit(value)

    visit(link)
    return [line for line in lines if line] or ["未识别生成参数（输入图像无可追溯配置）"]


def _draw_info_panel(pil, text, size):
    image = _pil_to_rgb(pil)
    width = image.width
    max_height = max(1, image.height // 3)
    draw = ImageDraw.Draw(image)
    # label_size is an upper bound for automatic information, not a forced size.
    for fitted_size in range(max(8, min(int(size), width // 24)), 7, -1):
        font = _load_font(fitted_size)
        pad = max(4, fitted_size // 3)
        lines = []
        for paragraph in text.splitlines():
            line = ""
            for char in paragraph:
                if line and draw.textlength(line + char, font=font) > max(1, width - pad * 2):
                    lines.append(line)
                    line = ""
                line += char
            lines.append(line)
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + max(2, fitted_size // 5)
        height = pad * 2 + line_height * len(lines)
        if height <= max_height:
            break
    footer = Image.new("RGB", (width, height), (20, 20, 20))
    draw = ImageDraw.Draw(footer)
    for index, line in enumerate(lines):
        draw.text((pad, pad + index * line_height), line, font=font, fill=(240, 240, 240))
    if height > max_height:
        footer = footer.resize((width, max_height), Image.LANCZOS)
    panel = Image.new("RGB", (width, image.height + footer.height), (20, 20, 20))
    panel.paste(image, (0, 0))
    panel.paste(footer, (0, image.height))
    return panel


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
            "optional": {
                "auto_info": ("BOOLEAN", {"default": True, "tooltip": "按每个图片输入追溯模型、LoRA权重和采样配置；关闭则恢复手动标签。"}),
                "include_manual_label": ("BOOLEAN", {"default": False, "tooltip": "把原有手写标签作为备注附加，避免旧标签与实际参数混淆。"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID", "prompt": "PROMPT"},
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
        auto_info = bool(_scalar(kwargs.get("auto_info", True)))
        include_manual = bool(_scalar(kwargs.get("include_manual_label", False)))
        prompt = kwargs.get("prompt") or {}
        own_inputs = prompt.get(str(_scalar(kwargs.get("unique_id", ""))), {}).get("inputs", {})

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

            this_label = port_label(_idx) if print_label else ""
            if print_label and auto_info:
                details = _generation_info(prompt, own_inputs.get(_key))
                this_label = "\n".join(details)
                if include_manual and port_label(_idx):
                    this_label = "备注: " + port_label(_idx) + "\n" + this_label
            port_labels_used.append(this_label)

            # 逐帧：可选烘焙【本端口】的标签
            for frame_no, pil in enumerate(pils):
                preview = pil
                if print_label and this_label:
                    if auto_info:
                        text = f"输入 {_idx} | 帧 {frame_no + 1}/{len(pils)} | {pil.width} × {pil.height}\n{this_label}"
                        pil = _draw_info_panel(pil, text, label_size)
                    else:
                        pil = _draw_label(pil, this_label, label_size)
                all_pils.append(pil)
                info = _save_temp(preview if auto_info else pil)
                info["input"] = input_no
                info["frame"] = frame_no
                images.append(info)

        # ---- grid 输出：所有端口所有帧横向并列合成一张大图（不另起一行）----
        if all_pils:
            if auto_info and print_label:
                height = max(p.height for p in all_pils)
                canvas = Image.new("RGB", (sum(p.width for p in all_pils), height), (20, 20, 20))
                x = 0
                for p in all_pils:
                    canvas.paste(p, (x, 0))
                    x += p.width
                grid = _pil_to_tensor(canvas)
            else:
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
