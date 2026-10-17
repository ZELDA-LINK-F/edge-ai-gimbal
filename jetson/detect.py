#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YOLO 目标检测模块（常驻快路径）

设计原则：这是项目里【常驻】的那一半 —— 负责实时感知，驱动云台。
          VLM 是【按需】的另一半 —— 只在用户提问时加载。

用法：
    python3 detect.py image 图片.jpg        # 检测一张图
    python3 detect.py camera                # 摄像头实时检测
    python3 detect.py camera --frames 100   # 跑 100 帧测 FPS
    python3 detect.py bench                 # 纯性能测试

导入：
    from detect import load, detect, detect_frame
"""

import os
import sys
import time

DEFAULT_MODEL = "yolov8n.pt"
DEFAULT_CONF = 0.4
DEFAULT_IOU = 0.5

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


def load(model_path=DEFAULT_MODEL):
    """加载模型。第一次调用会从网上下载权重（约 6MB）。"""
    from ultralytics import YOLO
    if not os.path.exists(model_path):
        print("[detect] 本地没有 " + model_path + "，尝试下载...")
    return YOLO(model_path)


def detect_frame(model, frame, conf=DEFAULT_CONF, iou=DEFAULT_IOU, device=0):
    """对一帧做检测。返回 (结果列表, 耗时秒)

    结果列表每项: {"class": 类别, "cn": 中文, "conf": 置信度,
                   "box": (x1,y1,x2,y2)}
    """
    t0 = time.time()
    results = model.predict(frame, conf=conf, iou=iou, verbose=False, device=device)
    dt = time.time() - t0

    out = []
    r = results[0]
    names = r.names
    if r.boxes is not None:
        for b in r.boxes:
            cls_id = int(b.cls[0])
            name = names[cls_id]
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

    model_path = opts.get("model", DEFAULT_MODEL)
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
