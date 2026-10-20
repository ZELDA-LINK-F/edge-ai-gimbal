#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YOLO 目标检测模块（常驻快路径）

后端：
    - TensorRT engine（.engine）—— 默认优先，板子上实测 detect_frame ≈ 20ms
    - PyTorch（.pt）—— 找不到 engine 时回退，≈ 30ms

注意：本仓库现有的 lab1/yolov8n_fp16.engine 是用 trtexec 从 ONNX 转的，
      没有嵌入元数据，类别名会变成 class0..class79。本模块会自动映射回 COCO 80 类，
      否则 follow.py 里 d["class"] == "person" 永远匹配不到（跟随会静默失效）。

用法：
    python3 detect.py image 图片.jpg        # 检测一张图
    python3 detect.py camera                # 摄像头实时检测
    python3 detect.py camera --frames 100   # 跑 100 帧测 FPS
    python3 detect.py bench                 # 纯性能测试
    python3 detect.py image x.jpg --model yolov8n.pt   # 强制用 PyTorch

导入：
    from detect import load, detect, detect_frame
"""

import os
import sys
import time

DEFAULT_CONF = 0.4
DEFAULT_IOU = 0.5

_HERE = os.path.dirname(os.path.abspath(__file__))

# engine 优先顺序（项目根目录那个 yolov8n.engine 已损坏，加载会 LLVM OOM，不要放前面）
ENGINE_CANDIDATES = (
    os.path.join(_HERE, "lab1", "yolov8n_fp16.engine"),
    os.path.join(_HERE, "yolov8n_fp16.engine"),
    os.path.join(_HERE, "yolov8n.engine"),
)

# 环境变量 GIMBAL_MODEL 可覆盖，例如 GIMBAL_MODEL=yolov8n.pt python3 follow.py
DEFAULT_MODEL = os.environ.get("GIMBAL_MODEL") or ""

# COCO 80 类（顺序即模型输出索引）
COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]

# COCO 80 类里常用的中文名（够用就行）
CN = {
    "person": "人", "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "bus": "公交车", "truck": "卡车", "bottle": "瓶子", "cup": "杯子",
    "bowl": "碗", "chair": "椅子", "couch": "沙发", "bed": "床",
    "dining table": "桌子", "tv": "电视", "laptop": "笔记本电脑",
    "mouse": "鼠标", "keyboard": "键盘", "cell phone": "手机",
    "book": "书", "clock": "时钟", "scissors": "剪刀", "teddy bear": "玩偶",
    "cat": "猫", "dog": "狗", "bird": "鸟", "banana": "香蕉",
    "apple": "苹果", "orange": "橙子", "pizza": "披萨", "cake": "蛋糕",
    "backpack": "背包", "handbag": "手提包", "umbrella": "雨伞",
    "potted plant": "盆栽", "refrigerator": "冰箱", "microwave": "微波炉",
    "toilet": "马桶", "sink": "水槽", "remote": "遥控器", "toothbrush": "牙刷",
}


def cn(name):
    return CN.get(name, name)


def resolve_model(path=None):
    """决定用哪个权重：显式参数 > 环境变量 > 自动找 engine > 回退 .pt"""
    if path:
        return path
    if DEFAULT_MODEL:
        return DEFAULT_MODEL
    for candidate in ENGINE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return "yolov8n.pt"


def load(model_path=None, backend=None):
    """加载模型：engine 优先走自定义 TRT runner（~10ms），失败回退 ultralytics；
    .pt 直接走 ultralytics（PyTorch，~30ms）。

    backend: custom（默认）| ultralytics | pt，也可用环境变量 GIMBAL_BACKEND 指定。
    """
    path = resolve_model(model_path)
    backend = backend or os.environ.get("GIMBAL_BACKEND", "custom")
    if path.endswith(".engine") and not os.path.exists(path):
        print("[detect] 找不到 " + path + "，回退到 yolov8n.pt")
        path = "yolov8n.pt"

    if path.endswith(".engine") and backend != "ultralytics":
        try:
            from trt_runner import TrtRunner
            model = TrtRunner(path, names=COCO80)
            print("[detect] 后端: TensorRT(自定义 runner)  权重: " + path)
            return model
        except Exception as exc:
            print("[detect] 自定义 runner 不可用（" + str(exc)[:120] + "），回退 ultralytics")

    from ultralytics import YOLO
    if path.endswith(".pt") and not os.path.exists(path):
        print("[detect] 本地没有 " + path + "，尝试下载...")
    model = YOLO(path)
    if path.endswith(".engine"):
        print("[detect] 后端: TensorRT(ultralytics)  权重: " + path)
        names = getattr(model, "names", {}) or {}
        if names and str(names.get(0, "")).startswith("class"):
            print("[detect] engine 无元数据，类别名将按 COCO80 映射")
    else:
        print("[detect] 后端: PyTorch  权重: " + path)
    return model


def class_name(names, idx):
    """把 class0..class79 这种无元数据的名字映射回 COCO 80 类。"""
    try:
        name = names[idx] if isinstance(names, dict) else names[int(idx)]
    except Exception:
        name = str(idx)
    if isinstance(name, str) and name.startswith("class") and name[5:].isdigit():
        n = int(name[5:])
        if 0 <= n < len(COCO80):
            return COCO80[n]
    return name


def detect_frame(model, frame, conf=DEFAULT_CONF, iou=DEFAULT_IOU, device=0):
    """对一帧做检测。返回 (结果列表, 耗时秒)

    结果列表每项: {"class": 类别, "cn": 中文, "conf": 置信度,
                   "box": (x1,y1,x2,y2)}
    """
    # 自定义 TRT runner 的接口是 infer()，ultralytics 的是 predict()
    if hasattr(model, "infer") and not hasattr(model, "predict"):
        dets, dt = model.infer(frame, conf=conf, iou=iou)
        for d in dets:
            d["cn"] = cn(d["class"])
        return dets, dt

    t0 = time.time()
    results = model.predict(frame, conf=conf, iou=iou, verbose=False, device=device)
    dt = time.time() - t0

    out = []
    r = results[0]
    names = r.names
    if r.boxes is not None:
        for b in r.boxes:
            cls_id = int(b.cls[0])
            name = class_name(names, cls_id)
            out.append({
                "class": name,
                "cn": cn(name),
                "conf": round(float(b.conf[0]), 3),
                "box": tuple(round(float(v), 1) for v in b.xyxy[0]),
            })
    return out, dt


def detect_image(model, path, conf=DEFAULT_CONF, iou=DEFAULT_IOU):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    dets, dt = detect_frame(model, im, conf, iou)
    return dets, dt, im.size


def _print_dets(dets):
    if not dets:
        print("  （没有检测到目标）")
        return
    for d in sorted(dets, key=lambda x: -x["conf"]):
        x1, y1, x2, y2 = d["box"]
        print("  " + d["cn"].ljust(8) + " " + str(d["conf"]).ljust(6) +
              " 位置(" + str(int(x1)) + "," + str(int(y1)) + ")-(" +
              str(int(x2)) + "," + str(int(y2)) + ")")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]
    opts = {}
    pos = []
    i = 1
    while i < len(args):
        if args[i].startswith("--"):
            k = args[i][2:]
            opts[k] = args[i + 1] if i + 1 < len(args) else "1"
            i += 2
        else:
            pos.append(args[i]); i += 1

    model_path = opts.get("model") or resolve_model()
    print("[detect] 加载模型: " + model_path)
    t0 = time.time()
    m = load(model_path)
    print("[detect] 加载耗时: " + str(round(time.time() - t0, 2)) + " 秒")

    if cmd == "image":
        if not pos:
            print("用法: python3 detect.py image 图片.jpg")
            return
        dets, dt, size = detect_image(m, pos[0])
        print("")
        print("图片: " + pos[0] + "  尺寸 " + str(size))
        print("检测耗时: " + str(round(dt * 1000, 1)) + " ms")
        print("检测到 " + str(len(dets)) + " 个目标:")
        _print_dets(dets)

    elif cmd == "camera":
        import camera
        n = int(opts.get("frames", 200))
        print("[detect] 摄像头实时检测，跑 " + str(n) + " 帧...")
        times = []
        got = 0
        t_start = time.time()
        for frame in camera.frames(width=int(opts.get("width", 1280)),
                                   height=int(opts.get("height", 720))):
            dets, dt = detect_frame(m, frame)
            times.append(dt)
            got += 1
            if got % 20 == 0:
                avg = sum(times[-20:]) / 20
                print("  第 " + str(got) + " 帧  平均 " + str(round(avg * 1000, 1)) +
                      " ms/帧  →  " + str(round(1 / avg, 1)) + " FPS  |  当前 " +
                      str(len(dets)) + " 个目标")
            if got >= n:
                break
        total = time.time() - t_start
        if times:
            avg = sum(times) / len(times)
            print("")
            print("  共 " + str(got) + " 帧，总耗时 " + str(round(total, 1)) + " 秒")
            print("  纯推理平均: " + str(round(avg * 1000, 1)) + " ms/帧  →  " +
                  str(round(1 / avg, 1)) + " FPS")
            print("  端到端（含取帧）: " + str(round(got / total, 1)) + " FPS")

    elif cmd == "bench":
        import numpy as np
        print("[detect] 性能测试（合成图）...")
        for hw in [(640, 640), (1280, 720)]:
            frame = np.zeros((hw[1], hw[0], 3), dtype=np.uint8)
            detect_frame(m, frame)  # 预热
            ts = []
            for _ in range(20):
                _, dt = detect_frame(m, frame)
                ts.append(dt)
            avg = sum(ts) / len(ts)
            print("  " + str(hw[0]) + "x" + str(hw[1]) + ": " +
                  str(round(avg * 1000, 1)) + " ms  →  " + str(round(1 / avg, 1)) + " FPS")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
