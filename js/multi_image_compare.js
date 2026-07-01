// Multi Image Compare —— 前端扩展（addDOMWidget 版，内核托管定位）
//
// 为什么用 addDOMWidget 而不是手算坐标：ComfyUI 新前端（comfyui-frontend-package
// 1.45.x / v5.5）会把 DOM widget 的定位、缩放、HiDPI、画布平移全部交给内核托管
// （Canvas Mode 走 .dom-widget-container，Vue DOM Mode 走 WidgetDOM.vue）。手写
// draw() 里用 convertOffsetToCanvas + rect 比例换算的方案已经连续坏过两次
// （2026-06：getTransform 失效；2026-06-29：computeSize 抢占分支导致不拉伸 / 定位
// 偏移到节点外）。改用 node.addDOMWidget 后，容器由内核定位，永远在节点内，
// 跨内核更新不会再「跑到节点外」。
//
// 关键点（核对 comfyui-frontend-package 源码 + lora-manager DOMWidget 开发指南）：
// - _arrangeWidgets：if(widget.computeSize){固定高度} else if(widget.computeLayoutSize){拉伸}。
//   addDOMWidget 返回的 DOMWidgetImpl 只提供 computeLayoutSize（读 getMinHeight/
//   getMaxHeight），所以会走「拉伸」分支，填满节点主体，不再留白 / 溢出。
// - widget.serialize = false：纯展示型 widget，不写进 widgets_values，避免污染
//   工作流导致宽高错乱 / 按键失效（见记忆 comfyui-lora-ui-cross-version）。
// - 内部尺寸变化用 ResizeObserver 触发 relayout，不依赖 draw()。
// - stage 上加 data-capture-wheel="true"：Vue DOM Mode 下滚轮默认会被转发给画布
//   做缩放，加这个属性后我们的滚轮缩放才生效。
//
// 功能：
// 1. 动态端口：始终保持最后一个 image_* 输入为空槽（待接入）。
// 2. 多帧：batch 每一帧都进入对比序列。
// 3. 对比：Slider（拖分割线对比 A/B）/ Grid（并排）。
// 4. 滚轮缩放 + 拖动平移（zoom>1 时）。
// 5. 显示源切换：逐帧 / 每端口合成图。
// 6. 角标显示工作流名称（图上像素文字由后端烘焙）。

import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const NODE_NAME = "binyuanMultiImageCompare";
const PREFIX = "image_";
const MAX_INPUTS = 16;

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// --------------------------------------------------------------------------- #
// 端口管理
// --------------------------------------------------------------------------- #
function getImageInputs(node) {
    return node.inputs
        .map((inp, i) => ({ inp, i }))
        .filter(({ inp }) => inp && typeof inp.name === "string" && inp.name.startsWith(PREFIX));
}

function isConnected(inp) {
    return !!inp && inp.link != null;
}

function renumberInputs(node) {
    let n = 0;
    for (const { inp } of getImageInputs(node)) {
        n += 1;
        inp.name = PREFIX + n;
    }
}

function refreshPorts(node) {
    if (node._mic_port_updating) return;
    node._mic_port_updating = true;
    try {
        renumberInputs(node);
        let changed = true;
        while (changed) {
            changed = false;
            const list = getImageInputs(node);
            for (let k = 0; k < list.length - 1; k++) {
                if (!isConnected(list[k].inp)) {
                    node.removeInput(list[k].i);
                    changed = true;
                    break;
                }
            }
        }
        renumberInputs(node);
        const list = getImageInputs(node);
        if (list.length === 0) {
            node.addInput(PREFIX + "1", "IMAGE");
        } else if (list.length < MAX_INPUTS && isConnected(list[list.length - 1].inp)) {
            node.addInput(PREFIX + (list.length + 1), "IMAGE");
        }
        if (typeof node.setDirtyCanvas === "function") node.setDirtyCanvas(true, true);
    } finally {
        node._mic_port_updating = false;
    }
}

function addImageInput(node) {
    const count = getImageInputs(node).length;
    if (count >= MAX_INPUTS) return;
    node.addInput(PREFIX + (count + 1), "IMAGE");
    if (typeof node.setDirtyCanvas === "function") node.setDirtyCanvas(true, true);
}

// --------------------------------------------------------------------------- #
// 工具
// --------------------------------------------------------------------------- #
function viewUrl(info) {
    return (
        api.apiURL("/view?filename=" + encodeURIComponent(info.filename)) +
        "&subfolder=" + encodeURIComponent(info.subfolder || "") +
        "&type=" + encodeURIComponent(info.type || "temp") +
        "&t=" + Math.floor(Math.random() * 1e9)
    );
}

function makeLabeler(images) {
    const maxFrame = {};
    for (const info of images) {
        maxFrame[info.input] = Math.max(maxFrame[info.input] || 0, info.frame || 0);
    }
    return (info) => {
        const multi = (maxFrame[info.input] || 0) > 0;
        return "#" + info.input + (multi ? "·f" + ((info.frame || 0) + 1) : "");
    };
}

// --------------------------------------------------------------------------- #
// DOM widget（addDOMWidget：定位 / 缩放全部由内核托管）
// --------------------------------------------------------------------------- #
function makeWidget(node) {
    // 容器：交给内核定位，自身只负责填满（width/height 100%）。
    const container = document.createElement("div");
    container.className = "binyuan-mic";
    Object.assign(container.style, {
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#1b1b1b",
        color: "#ddd",
        fontFamily: "sans-serif",
        fontSize: "13px",
        border: "1px solid #000",
        boxSizing: "border-box",
        overflow: "hidden",
    });

    // 顶部工具条
    const bar = document.createElement("div");
    Object.assign(bar.style, {
        display: "flex",
        alignItems: "center",
        gap: "0.3em",
        padding: "0.2em 0.4em",
        height: "2.4em",
        background: "#111",
        flex: "0 0 auto",
        boxSizing: "border-box",
    });
    const titleEl = document.createElement("span");
    titleEl.textContent = "Multi Compare";
    Object.assign(titleEl.style, { flex: "1 1 auto", overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" });
    bar.appendChild(titleEl);

    const btnStyle = {
        height: "2em",
        minWidth: "2.4em",
        padding: "0 0.5em",
        background: "#2c2c2c",
        color: "#ddd",
        border: "1px solid #444",
        borderRadius: "3px",
        cursor: "pointer",
        fontSize: "1em",
        lineHeight: "2em",
        boxSizing: "border-box",
    };
    const mkBtn = (label) => {
        const b = document.createElement("button");
        b.textContent = label;
        Object.assign(b.style, btnStyle);
        return b;
    };
    const flipBtn = mkBtn("↔");
    const modeBtn = mkBtn("Slider");
    Object.assign(modeBtn.style, { flex: "1 1 auto", minWidth: "5em" });
    const resetBtn = mkBtn("⟲");

    // A / B 选择器：固定对比哪两张图
    const selStyle = {
        height: "2em",
        minWidth: "3em",
        padding: "0 0.2em",
        background: "#2c2c2c",
        color: "#ddd",
        border: "1px solid #444",
        borderRadius: "3px",
        fontSize: "1em",
        boxSizing: "border-box",
        cursor: "pointer",
    };
    const selectA = document.createElement("select");
    const selectB = document.createElement("select");
    Object.assign(selectA.style, selStyle);
    Object.assign(selectB.style, selStyle);
    const vsLabel = document.createElement("span");
    vsLabel.textContent = "vs";
    Object.assign(vsLabel.style, { padding: "0 0.2em", color: "#9aa" });

    bar.appendChild(flipBtn);
    bar.appendChild(selectA);
    bar.appendChild(vsLabel);
    bar.appendChild(selectB);
    bar.appendChild(modeBtn);
    bar.appendChild(resetBtn);
    container.appendChild(bar);

    // 图片舞台
    const stage = document.createElement("div");
    stage.setAttribute("data-capture-wheel", "true"); // Vue DOM Mode 下保留滚轮缩放
    Object.assign(stage.style, {
        position: "relative",
        flex: "1 1 auto",
        minHeight: "0",
        background: "#000",
        overflow: "hidden",
        userSelect: "none",
        touchAction: "none",
    });
    container.appendChild(stage);

    const placeholder = document.createElement("div");
    Object.assign(placeholder.style, {
        position: "absolute",
        inset: "0",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#777",
        pointerEvents: "none",
    });
    placeholder.textContent = "连接 image 输入并运行工作流";
    stage.appendChild(placeholder);

    // 注册成内核托管的 DOM widget。定位 / 缩放 / HiDPI / 平移全部由内核处理，
    // 这里只声明尺寸需求（拉伸填满节点主体）。
    const widget = node.addDOMWidget(
        "multi_image_compare_widget",
        "multi_image_compare_widget",
        container,
        {
            hideOnZoom: false, // 缩小画布时仍显示对比框
            canvasOnly: true,  // 只在画布节点上显示，不进属性面板
            getMinHeight: () => 360,
            getMaxHeight: () => 4000,
            onResize: () => relayout(),
        }
    );
    widget.serialize = false; // 纯展示型，不进 widgets_values
    widget.parent = node;

    // ---- 内部交互状态 ----
    widget.mode = "slider";      // "slider" | "grid"
    widget.flip = false;
    widget.handle = 0.5;
    widget.aIdx = 0;
    widget.bIdx = 1;
    widget.zoom = 1;
    widget.panX = 0;
    widget.panY = 0;
    widget._viewport = null;
    widget._applySlider = null;
    widget._sliderUpdateHandle = null;

    // ---- 重建舞台内容 ----
    function relayout() {
        widget._applySlider = null;
        widget._sliderUpdateHandle = null;
        const imgs = (node.mic_loaded || []).filter(Boolean);
        const infos = node.mic_images || [];
        stage.innerHTML = "";
        stage.appendChild(placeholder);
        placeholder.style.display = imgs.length ? "none" : "flex";
        if (imgs.length === 0) {
            placeholder.textContent = "连接 image 输入并运行工作流";
        }
        titleEl.textContent = infos.length
            ? `Multi Compare · ${infos.length} frame${infos.length > 1 ? "s" : ""}`
            : "Multi Compare";
        populateSelects();
        const showSel = widget.mode === "slider" && imgs.length > 0;
        selectA.style.display = selectB.style.display = vsLabel.style.display = showSel ? "" : "none";
        if (imgs.length === 0) return;

        const labeler = node.mic_labeler || ((info) => "#" + (info.input || 1));
        const applyFlip = (el) => (el.style.transform = widget.flip ? "scaleX(-1)" : "none");

        // viewport 承载缩放/平移
        const viewport = document.createElement("div");
        Object.assign(viewport.style, {
            position: "absolute",
            inset: "0",
            transformOrigin: "0 0",
            pointerEvents: "none",
        });
        stage.appendChild(viewport);
        widget._viewport = viewport;

        if (widget.mode === "grid") {
            buildGrid(viewport, imgs, infos, labeler, applyFlip, widget, node);
        } else {
            buildSlider(stage, viewport, imgs, infos, labeler, applyFlip, widget, node);
        }
        applyTransform();
    }
    widget.relayout = relayout;

    function applyTransform() {
        if (widget._viewport) {
            widget._viewport.style.transform = `translate(${widget.panX}px, ${widget.panY}px) scale(${widget.zoom})`;
        }
        if (widget._applySlider) widget._applySlider();
    }
    widget._applyTransform = applyTransform;

    widget.resetView = () => {
        widget.zoom = 1;
        widget.panX = 0;
        widget.panY = 0;
        applyTransform();
    };

    // ---- 容器尺寸变化时重排（内核重新定位后会触发）----
    let ro = null;
    if (typeof ResizeObserver !== "undefined") {
        ro = new ResizeObserver(() => relayout());
        ro.observe(container);
    }

    widget.onRemoved = () => {
        try { ro?.disconnect(); } catch (_e) {}
    };

    // ---- 滚轮缩放（stage 只挂一次）----
    stage.addEventListener("wheel", (e) => {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.12 : 0.89;
        const newZoom = clamp(widget.zoom * factor, 0.2, 10);
        const rect = stage.getBoundingClientRect();
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        const k = newZoom / widget.zoom;
        widget.panX = cx - (cx - widget.panX) * k;
        widget.panY = cy - (cy - widget.panY) * k;
        widget.zoom = newZoom;
        if (widget.zoom <= 1.001) { widget.zoom = 1; widget.panX = 0; widget.panY = 0; }
        applyTransform();
    }, { passive: false });

    stage.addEventListener("dblclick", () => widget.resetView());

    // ---- 指针拖动（stage 只挂一次）：zoom>1 平移，否则 slider 分割 ----
    let dragging = false;
    let mode = "split";
    let lastX = 0, lastY = 0;
    const onMove = (e) => {
        if (!dragging) return;
        e.preventDefault();
        if (mode === "pan") {
            widget.panX += e.clientX - lastX;
            widget.panY += e.clientY - lastY;
            lastX = e.clientX;
            lastY = e.clientY;
            applyTransform();
        } else if (widget._sliderUpdateHandle) {
            widget._sliderUpdateHandle(e);
        }
    };
    const onUp = () => {
        dragging = false;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
    };
    stage.addEventListener("pointerdown", (e) => {
        // 中键留给画布平移，不拦截
        if (e.button === 1) return;
        dragging = true;
        e.preventDefault();
        e.stopPropagation();
        if (widget.zoom > 1.001) {
            mode = "pan";
            lastX = e.clientX;
            lastY = e.clientY;
        } else {
            mode = "split";
            widget._sliderUpdateHandle?.(e);
        }
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
    });

    // ---- 按钮事件 ----
    flipBtn.addEventListener("click", () => {
        widget.flip = !widget.flip;
        flipBtn.style.background = widget.flip ? "#2a5a2a" : "#2c2c2c";
        relayout();
    });
    modeBtn.addEventListener("click", () => {
        widget.mode = widget.mode === "grid" ? "slider" : "grid";
        modeBtn.textContent = widget.mode === "grid" ? "Grid" : "Slider";
        widget.resetView();
        relayout();
    });
    resetBtn.addEventListener("click", () => widget.resetView());

    // A/B 下拉框
    const nImages = () => (node.mic_loaded || []).filter(Boolean).length;
    const populateSelects = () => {
        const n = nImages();
        const fill = (sel, val) => {
            sel.innerHTML = "";
            for (let i = 0; i < n; i++) {
                const o = document.createElement("option");
                o.value = i;
                o.textContent = "#" + (i + 1);
                sel.appendChild(o);
            }
            if (n > 0) sel.value = String(val);
        };
        if (n > 0) {
            if (widget.aIdx >= n) widget.aIdx = 0;
            if (widget.bIdx >= n || widget.bIdx === widget.aIdx) widget.bIdx = (widget.aIdx + 1) % n;
            fill(selectA, widget.aIdx);
            fill(selectB, widget.bIdx);
        }
    };
    widget.populateSelects = populateSelects;
    selectA.addEventListener("change", () => {
        widget.aIdx = +selectA.value;
        if (widget.bIdx === widget.aIdx) widget.bIdx = (widget.aIdx + 1) % Math.max(1, nImages());
        populateSelects();
        relayout();
    });
    selectB.addEventListener("change", () => {
        widget.bIdx = +selectB.value;
        if (widget.aIdx === widget.bIdx) widget.aIdx = (widget.bIdx + 1) % Math.max(1, nImages());
        populateSelects();
        relayout();
    });

    return widget;
}

// ---- Grid 构建（flex-wrap 紧凑排列，保持宽高比、无大间隔）----
function buildGrid(viewport, imgs, infos, labeler, applyFlip, w, node) {
    const sw = stageWidth(viewport);
    const sh = stageHeight(viewport);
    const ar = (im) => (im.naturalWidth || 1) / (im.naturalHeight || 1);
    const rowsFor = (rowH) => {
        let rows = 1, w = 0;
        for (const im of imgs) {
            const iw = ar(im) * rowH + 2;
            if (w + iw > sw && w > 0) { rows++; w = 0; }
            w += iw;
        }
        return rows;
    };
    let lo = 20, hi = sh, rowH = 40;
    while (lo <= hi) {
        const mid = (lo + hi) / 2;
        if (rowsFor(mid) * mid <= sh) { rowH = mid; lo = mid + 1; }
        else hi = mid - 1;
    }
    const wrap = document.createElement("div");
    Object.assign(wrap.style, {
        position: "absolute", inset: "0", display: "flex",
        flexWrap: "wrap", alignContent: "flex-start", gap: "2px",
    });
    let row = null, rowW = 0;
    imgs.forEach((im, i) => {
        const iw = ar(im) * rowH;
        if (rowW + iw > sw && row) { row = null; rowW = 0; }
        if (!row) {
            row = document.createElement("div");
            Object.assign(row.style, { display: "flex", gap: "2px", height: `${rowH}px` });
            wrap.appendChild(row);
        }
        const cell = document.createElement("div");
        Object.assign(cell.style, { position: "relative", height: `${rowH}px`, overflow: "hidden", background: "#000" });
        const iimg = document.createElement("img");
        iimg.src = im.src;
        Object.assign(iimg.style, { height: "100%", width: `${iw}px`, objectFit: "cover", display: "block" });
        applyFlip(iimg);
        cell.appendChild(iimg);
        cell.appendChild(mkTag(tagText(infos[i], labeler, i, node), "left"));
        row.appendChild(cell);
        rowW += iw + 2;
    });
    viewport.appendChild(wrap);
}

// ---- Slider 构建 ----
function buildSlider(stage, viewport, imgs, infos, labeler, applyFlip, w, node) {
    const N = imgs.length;
    const sw0 = stageWidth(viewport);

    let a = clamp(w.aIdx, 0, N - 1);
    let b = clamp(w.bIdx, 0, N - 1);
    if (b === a) b = (a + 1) % Math.max(1, N);
    let splitPx = w.handle * sw0;

    const leftLayer = document.createElement("div");
    Object.assign(leftLayer.style, {
        position: "absolute", top: "0", bottom: "0", left: "0",
        width: `${splitPx}px`, overflow: "hidden",
    });
    const rightLayer = document.createElement("div");
    Object.assign(rightLayer.style, {
        position: "absolute", top: "0", bottom: "0",
        left: `${splitPx}px`, right: "0", overflow: "hidden",
    });

    const mkImg = (idx) => {
        const im = document.createElement("img");
        if (imgs[idx]) im.src = imgs[idx].src;
        Object.assign(im.style, {
            position: "absolute", top: "0", left: "0",
            width: `${sw0}px`, height: "100%",
            objectFit: "contain", display: "block",
        });
        applyFlip(im);
        return im;
    };
    const imgA = mkImg(a);
    const imgB = mkImg(b);
    imgB.style.left = `${-splitPx}px`;
    leftLayer.appendChild(imgA);
    rightLayer.appendChild(imgB);
    viewport.appendChild(leftLayer);
    viewport.appendChild(rightLayer);

    // 分割线 / 角标放在 stage 层（不随 viewport 缩放厚度）
    const divider = document.createElement("div");
    Object.assign(divider.style, {
        position: "absolute", top: "0", bottom: "0",
        left: `${splitPx}px`, width: "2px", background: "#fff",
        marginLeft: "-1px", pointerEvents: "none",
    });
    const knob = document.createElement("div");
    knob.textContent = "↔";
    Object.assign(knob.style, {
        position: "absolute", top: "50%", left: "50%",
        transform: "translate(-50%,-50%)", width: "16px", height: "16px",
        background: "#fff", color: "#333", borderRadius: "50%",
        fontSize: "11px", lineHeight: "16px", textAlign: "center",
    });
    divider.appendChild(knob);
    const tagL = mkTag(infos[a] ? tagText(infos[a], labeler, a, node) : "#" + (a + 1), "left");
    const tagR = mkTag(infos[b] ? tagText(infos[b], labeler, b, node) : "#" + (b + 1), "right");
    stage.appendChild(divider);
    stage.appendChild(tagL);
    stage.appendChild(tagR);

    const apply = () => {
        const sw = stageWidth(viewport);
        const sp = w.handle * sw;
        leftLayer.style.width = `${sp}px`;
        rightLayer.style.left = `${sp}px`;
        imgB.style.left = `${-sp}px`;
        imgB.style.width = `${sw}px`;
        imgA.style.width = `${sw}px`;
        const screenSp = sp * w.zoom + w.panX;
        divider.style.left = `${screenSp}px`;
    };

    const updateHandle = (e) => {
        const rect = stage.getBoundingClientRect();
        if (rect.width <= 0) return;
        const x = e.clientX - rect.left;
        w.handle = clamp((x - w.panX) / w.zoom / rect.width, 0, 1);
        apply();
    };

    w._applySlider = apply;
    w._sliderUpdateHandle = updateHandle;
    apply();
}

function stageWidth(vp) {
    return (vp && vp.clientWidth) || 400;
}
function stageHeight(vp) {
    return (vp && vp.clientHeight) || 300;
}

function tagText(info, labeler, i, node) {
    if (!info) return "#" + (i + 1);
    const base = labeler(info);
    if (node && node.mic_print_label) {
        const pl = node.mic_labels && node.mic_labels[info.input - 1];
        if (pl) return pl + " · " + base;
        if (node.mic_label) return node.mic_label + " · " + base;
    }
    return base;
}

function mkTag(text, align) {
    const tag = document.createElement("span");
    tag.textContent = text;
    Object.assign(tag.style, {
        position: "absolute",
        top: "2px",
        background: "rgba(0,0,0,0.55)",
        color: "#fff",
        padding: "1px 5px",
        fontSize: "11px",
        borderRadius: "2px",
        pointerEvents: "none",
    });
    tag.style[align] = "2px";
    return tag;
}

// --------------------------------------------------------------------------- #
// 扩展注册
// --------------------------------------------------------------------------- #
app.registerExtension({
    name: "Comfy.MultiImageCompare",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const getExtraMenuOptions = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function (_canvas, options) {
            getExtraMenuOptions?.apply(this, arguments);
            const count = getImageInputs(this).length;
            options.push(null, {
                content: `➕ 添加图片输入 (${count}/${MAX_INPUTS})`,
                disabled: count >= MAX_INPUTS,
                callback: () => addImageInput(this),
            });
            return options;
        };

        // 动态端口
        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (type, index, connected, link_info) {
            const r = onConnectionsChange?.apply?.(this, arguments);
            try {
                const stack = new Error().stack || "";
                if (
                    stack.includes("loadGraphData") ||
                    stack.includes("pasteFromClipboard") ||
                    stack.includes("configure") ||
                    stack.includes("Subgraph")
                ) {
                    return r;
                }
            } catch (_e) {}
            if (type !== 1 || !link_info) return r;
            const input = this.inputs[index];
            if (!input || !input.name.startsWith(PREFIX)) return r;
            refreshPorts(this);
            return r;
        };

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            this.mic_loaded = [];
            this.mic_images = [];
            this.mic_label = "";
            this.mic_labels = [];
            this.mic_print_label = true;

            if (getImageInputs(this).length === 0) {
                this.addInput(PREFIX + "1", "IMAGE");
            }

            // 把 label 多行文本框压小（默认太高占地方），把空间让给对比框。
            // 多行 STRING 是内核 DOM widget，靠 getMinHeight/getMaxHeight + CSS 变量控高。
            const labelW = (this.widgets || []).find((w) => w && w.name === "label");
            if (labelW) {
                const minH = 48, maxH = 72;
                labelW.options = labelW.options || {};
                labelW.options.getMinHeight = () => minH;
                labelW.options.getMaxHeight = () => maxH;
                const applyCss = () => {
                    if (!labelW.element) return;
                    labelW.element.style.setProperty("--comfy-widget-min-height", minH + "px");
                    labelW.element.style.setProperty("--comfy-widget-max-height", maxH + "px");
                    // 顺手约束内部 textarea，防止它自带 rows 撑高
                    const ta = labelW.element.querySelector("textarea");
                    if (ta) {
                        ta.style.minHeight = "0";
                        ta.style.maxHeight = maxH + "px";
                        ta.style.height = minH + "px";
                    }
                };
                applyCss();
                // element 可能尚未挂载，延后一帧再应用一次
                setTimeout(applyCss, 0);
            }

            const widget = makeWidget(this);
            this.mic_widget = widget;

            // 节点默认更高，给对比框留足空间（label 已压小，多余空间全归对比框）。
            this.setSize([this.size[0] > 420 ? this.size[0] : 460, Math.max(600, (typeof this.computeSize === "function" ? this.computeSize()[1] : 600) + 60)]);
            if (typeof this.setDirtyCanvas === "function") this.setDirtyCanvas(true, true);
        };

        // 加载后补一个空槽
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (data) {
            const r = onConfigure?.apply(this, arguments);
            try {
                renumberInputs(this);
                const list = getImageInputs(this);
                if (list.length === 0) {
                    this.addInput(PREFIX + "1", "IMAGE");
                } else if (list.length < MAX_INPUTS && isConnected(list[list.length - 1].inp)) {
                    this.addInput(PREFIX + (list.length + 1), "IMAGE");
                }
            } catch (_e) {}
            if (typeof this.setDirtyCanvas === "function") this.setDirtyCanvas(true, true);
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (data) {
            onExecuted?.apply(this, arguments);
            // 清掉 ComfyUI 自动预览的 imgs，避免 widget 下方多出一排小缩略图
            this.imgs = null;
            if (!data) return;
            this.mic_images = data.images || [];
            this.mic_label = Array.isArray(data.label) ? (data.label[0] || "") : (data.label || "");
            this.mic_labels = Array.isArray(data.labels) ? data.labels : [];
            this.mic_print_label = Array.isArray(data.print_label) ? !!data.print_label[0] : !!data.print_label;
            this.mic_labeler = makeLabeler(this.mic_images);
            this.mic_loaded = new Array(this.mic_images.length).fill(null);
            if (this.mic_widget) {
                this.mic_widget.handle = 0.5;
                this.mic_widget.resetView();
            }

            const loadList = (infos, store) => {
                infos.forEach((info, i) => {
                    const img = new Image();
                    img.onload = () => {
                        store[i] = img;
                        this.mic_widget?.relayout?.();
                        if (typeof this.setDirtyCanvas === "function") this.setDirtyCanvas(true, true);
                    };
                    img.onerror = () => {
                        store[i] = null;
                        if (typeof this.setDirtyCanvas === "function") this.setDirtyCanvas(true, true);
                    };
                    img.src = viewUrl(info);
                });
            };

            if (this.mic_images.length === 0) {
                this.mic_widget?.relayout?.();
                if (typeof this.setDirtyCanvas === "function") this.setDirtyCanvas(true, true);
                return;
            }
            loadList(this.mic_images, this.mic_loaded);
        };

        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            onRemoved?.apply(this, arguments);
            this.mic_widget?.onRemoved?.();
            if (this.mic_widget && this.widgets) {
                const idx = this.widgets.indexOf(this.mic_widget);
                if (idx >= 0) this.widgets.splice(idx, 1);
            }
        };
    },
});
