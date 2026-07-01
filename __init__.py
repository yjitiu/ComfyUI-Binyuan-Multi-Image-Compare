"""Multi Image Compare — 一个支持 N 张图片对比的 ComfyUI 自定义节点。

与 rgthree_comfy 的 Image Comparer（仅 A/B 两张）不同，本节点支持动态增减
图片输入数量，并在前端用滑块 / 网格两种模式对比任意张图片。
"""

from .nodes import MultiImageCompareNode

NODE_CLASS_MAPPINGS = {
    "binyuanMultiImageCompare": MultiImageCompareNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "binyuanMultiImageCompare": "🖼️ binyuan · Multi Image Compare (N)",
}

# 让 ComfyUI 加载前端扩展（js/ 目录下的脚本）
WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
