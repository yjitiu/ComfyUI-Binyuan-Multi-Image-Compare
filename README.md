# 🖼️ ComfyUI Binyuan · Multi Image Compare (N)

> A ComfyUI custom node for comparing **N images** (and multi-frame batches) side
> by side — slider A/B mode **and** grid mode, with optional label baking and a
> composite grid export. Unlike rgthree's Image Comparer (A/B only), this one
> supports any number of inputs.

适配 ComfyUI v0.26 / 新前端（comfyui-frontend-package 1.45.x）。

---

## 中文说明

### 这是什么
节点 `🖼️ binyuan · Multi Image Compare (N)`，分类 `image`。
支持 **N 张图片 + 多帧 batch** 对比：节点内用滑块拖分割线对比任意两张，或网格并排查看所有图；可把自定义文字烧到每张图左上角；最终把所有端口所有帧横向拼成一张大图导出。

### 怎么用
1. 放到 `ComfyUI/custom_nodes/binyuan_multi_image_compare/`，重启 ComfyUI。
2. 右键 → `image` → `🖼️ binyuan · Multi Image Compare (N)`。
3. 把若干 `IMAGE`（如 VAEDecode 输出）依次连到 `image_1`、`image_2`… 连满一个端口会自动加下一个空槽（最多 16 个）。也可右键节点 → "➕ 添加图片输入"。
4. 在 `label` 多行文本里每行写一个端口的标签（第1行→image_1，第2行→image_2…）。
5. 运行后：节点内可 Slider / Grid 对比；把 `grid` 输出连到 `SaveImage` 存成一张大图。

### 参数
| 参数 | 说明 |
|---|---|
| label | 多行文本，每行对应一个输入端口，烧到该端口图片左上角 |
| print_label | 是否把 label 烘焙到图片上（默认 true） |
| label_size | 字号 8–200（默认 32，支持中文微软雅黑） |

### 输出
- `grid` (IMAGE)：所有端口所有帧横向拼成的一张大图。

### 节点内对比操作
- 顶部工具栏：`Slider`/`Grid` 切换；Slider 模式下用 `A vs B` 下拉选要对比的两张；`↔` 翻转；`⟲` 重置缩放。
- 图片区：滚轮缩放（0.2x–10x）、拖动平移、双击重置；Slider 模式下拖动=移动分割线。

### 典型场景
- **对比多个模型**：3 个 VAEDecode 分别连 image_1/2/3，label 填模型名，运行后导出带名字的合成图。
- **多帧对比**：某端口接 batch（视频帧/多步采样），所有帧都进对比序列和合成大图。

### 常见问题
| 现象 | 解决 |
|---|---|
| 图片区不在节点内/乱跑 | Ctrl+F5 硬刷新浏览器（JS 没更新） |
| 文字没印上 | 检查 print_label=true 且 label 填了对应行 |
| 改参数没生效 | 后端改动需重启 ComfyUI（不只刷新浏览器） |
| 字体异常 | 系统缺微软雅黑，会回退默认字体 |
<img width="1502" height="638" alt="屏幕截图 2026-07-01 120127" src="https://github.com/user-attachments/assets/300b6f5c-1e08-49ec-a335-258e22d93f6c" />
<img width="736" height="719" alt="屏幕截图 2026-07-01 115316" src="https://github.com/user-attachments/assets/21bc7a21-10c6-4201-8dd9-dd7f09c4c879" />

---

## English

### What it is
Node `🖼️ binyuan · Multi Image Compare (N)` under category `image`.
Compare **N images and multi-frame batches**: in-node slider A/B compare **or**
grid view of all images; bake per-port text labels onto images; export a single
composite grid image of every frame from every port.

### How to use
1. Drop into `ComfyUI/custom_nodes/binyuan_multi_image_compare/`, restart ComfyUI.
2. Right-click → `image` → add the node.
3. Connect `IMAGE` sources (e.g. VAEDecode) to `image_1`, `image_2`… A new empty
   slot appears automatically when one is filled (up to 16). Right-click node →
   "➕ 添加图片输入" to add manually.
4. Write one label per line in `label` (line 1 → image_1, line 2 → image_2…).
5. Run: compare with Slider/Grid inside the node; connect `grid` to `SaveImage`
   to save the composite.

### Params
`label` (multiline, per-port text), `print_label` (bool, default true),
`label_size` (8–200, default 32, CJK font supported).

### Output
`grid` (IMAGE): one wide composite of all frames from all ports.

### In-node controls
Toolbar: `Slider`/`Grid` toggle; `A vs B` dropdown to pick two for slider mode;
`↔` flip; `⟲` reset zoom. Canvas: wheel to zoom (0.2x–10x), drag to pan,
double-click to reset; in Slider mode drag = move the split line.

### Files
- `nodes.py` — backend: inputs/outputs, label baking, grid composition
- `js/multi_image_compare.js` — frontend: compare UI, zoom, DOM overlay positioning
- `__init__.py` — registration (`WEB_DIRECTORY = "./js"`)
- `使用说明.txt` — original Chinese manual
- `README.md` / `LICENSE` (MIT)

## Install
- ComfyUI Manager: search `Binyuan Multi Image Compare`.
- Manual: `git clone https://github.com/yjitiu/ComfyUI-Binyuan-Multi-Image-Compare.git binyuan_multi_image_compare`
