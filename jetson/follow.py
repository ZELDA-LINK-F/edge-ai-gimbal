#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云台跟随：检测画面中的人，控制舵机转向
==========================================
硬件: Jetson CSI 摄像头 + STM32 + LX-16A 舵机

逻辑:
    摄像头拍帧 → 【180°翻转修正】→ YOLO 找"人"
      → 算人的水平位置 → 映射成舵机角度 → 发命令

安全限位:
    水平舵机限制在 400~600（±24°），保护摄像头 FPC 排线

用法:
    python3 follow.py               # 跟随模式（Ctrl+C 退出）
    python3 follow.py --frames 20   # 只跑 20 帧
    python3 follow.py --seconds 60  # 跑 60 秒
    python3 follow.py --no-flip     # 不翻转（摄像头装正时用）
    python3 follow.py --no-servo    # 只检测，不动舵机
"""
import sys, time, os

sys.path.insert(0, "/home/nvidia/project")

import cv2
from detect import load, detect_frame
import servo

# ---------------- 配置 ----------------
W, H = 1280, 720
FPS = 30
PREHEAT = 60
CONF = 0.4

UPSIDE_DOWN = True        # 摄像头装反了，画面需要 180° 翻转

PAN_ID = 1
PAN_MIN, PAN_MAX = 400, 600
PAN_CENTER = 500
SMOOTH = 0.25
MOVE_MS = 200


def open_camera():
    if not os.path.exists("/dev/video0"):
        raise RuntimeError("找不到 /dev/video0（摄像头没接好）")
    pipeline = (
        "nvarguscamerasrc sensor-id=0"
        " ! video/x-raw(memory:NVMM),width=%d,height=%d,framerate=%d/1,format=NV12"
        " ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert"
        " ! video/x-raw,format=BGR ! appsink drop=1 max-buffers=2"
        % (W, H, FPS)
    )
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        raise RuntimeError("打不开摄像头管线")
    return cap


class Follower:
    def __init__(self, use_servo=True, flip=True):
        self.flip = flip
        print("[1/3] 打开摄像头...")
        self.cap = open_camera()
        print("[2/3] 预热摄像头（自动曝光收敛）...")
        for _ in range(PREHEAT):
            self.cap.read()
        print("[3/3] 加载 YOLO 模型...")
        self.model = load()
        self.bus = None
        if use_servo:
            self.bus = servo.ServoBus()
            print("     舵机串口:", self.bus.port)
            self.bus.center(800)
            time.sleep(1.5)
        self.pan = PAN_CENTER
        self.last_drain = time.time()
        self.frames = 0
        self.hits = 0
        print("准备完成！（画面翻转: %s）\n" % ("开" if flip else "关"))

    def drain(self):
        now = time.time()
        if self.bus and now - self.last_drain > 2.0:
            try:
                self.bus.s.reset_input_buffer()
            except Exception:
                pass
            self.last_drain = now

    def send_move(self, pos, ms=MOVE_MS):
        if not self.bus:
            return
        p, clipped = self.bus.clamp(PAN_ID, pos)
        self.bus.s.write(("S%d,%d,%d\r\n" % (PAN_ID, p, ms)).encode())

    def biggest_person(self, dets):
        persons = [d for d in dets if d["class"] == "person"]
        if not persons:
            return None
        return max(persons, key=lambda d: (d["box"][2] - d["box"][0]) *
                                          (d["box"][3] - d["box"][1]))

    def step(self, verbose=False):
        ok, frame = self.cap.read()
        if not ok:
            return None
        if self.flip:
            frame = cv2.rotate(frame, cv2.ROTATE_180)

        dets, dt = detect_frame(self.model, frame, conf=CONF)
        person = self.biggest_person(dets)
        self.frames += 1

        if person:
            self.hits += 1
            x1, y1, x2, y2 = person["box"]
            cx = (x1 + x2) / 2.0
            offset = max(-1.0, min(1.0, (cx - W / 2.0) / (W / 2.0)))
            target = PAN_CENTER + offset * (PAN_MAX - PAN_MIN) / 2.0
            self.pan = self.pan + (target - self.pan) * SMOOTH
            self.send_move(self.pan)
            if verbose:
                side = "左" if offset < -0.15 else ("右" if offset > 0.15 else "中")
                print("  人 中心x=%4.0f 偏移=%+.2f(%s) 目标=%3.0f 舵机=%3.0f (%4.1fms)"
                      % (cx, offset, side, target, self.pan, dt * 1000))
            return {"person": True, "offset": offset, "pan": self.pan}
        else:
            self.pan = self.pan + (PAN_CENTER - self.pan) * 0.1
            self.send_move(self.pan, 400)
            if verbose:
                print("  没人 → 回中 舵机=%3.0f (%4.1fms)" % (self.pan, dt * 1000))
            return {"person": False, "pan": self.pan}

    def run(self, max_frames=None, max_seconds=None):
        t0 = time.time()
        try:
            while True:
                self.step(verbose=True)
                self.drain()
                if max_frames and self.frames >= max_frames:
                    break
                if max_seconds and time.time() - t0 >= max_seconds:
                    break
        except KeyboardInterrupt:
            print("\n[中断]")
        finally:
            self.close()
        print("\n共 %d 帧，检测到人 %d 帧 (%.0f%%)"
              % (self.frames, self.hits, 100.0 * self.hits / max(1, self.frames)))

    def close(self):
        try:
            self.cap.release()
        except Exception:
            pass
        if self.bus:
            try:
                self.bus.center(800)
                time.sleep(1.0)
                self.bus.close()
            except Exception:
                pass


def main():
    args = sys.argv[1:]
    use_servo = "--no-servo" not in args
    flip = "--no-flip" not in args
    max_frames = None
    max_seconds = None
    for i, a in enumerate(args):
        if a == "--frames" and i + 1 < len(args):
            max_frames = int(args[i + 1])
        if a == "--seconds" and i + 1 < len(args):
            max_seconds = int(args[i + 1])

    f = Follower(use_servo=use_servo, flip=flip)
    f.run(max_frames=max_frames, max_seconds=max_seconds)


if __name__ == "__main__":
    main()
