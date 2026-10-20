#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云台跟随：检测画面中的人，控制舵机转向（低延迟版）

相对旧版的改动
---------------
1. 控制：SMOOTH=0.25 → 时间常数 TAU=30ms 的一阶滤波（按 dt 自适应）
2. 控制：MOVE_MS 200 → 50，并加每帧最大步长保护（保护摄像头 FPC 排线）
3. 控制：新增 DEADBAND 死区，抑制检测框抖动
4. 控制：新增前馈（目标速度预测），抵消滤波滞后
5. 相机：30fps → 60fps，max-buffers=1，sync=false，曝光上限 8ms
6. 视觉：不再每帧 cv2.rotate(180)，改为 offset 取负（数学等价，省 1.3ms/帧）
7. 后端：detect.load() 默认走 TensorRT（自定义 runner，检测 ≈10ms）

硬件: Jetson CSI 摄像头 + STM32 + LX-16A 舵机

安全限位:
    水平舵机限制在 400~600（±24°），保护摄像头 FPC 排线

用法:
    python3 follow.py                        # 跟随模式
    python3 follow.py --no-servo             # 只跑视觉+控制，不动舵机
    python3 follow.py --profile              # 每 200 帧打印各段耗时 p50/p95
    python3 follow.py --seconds 10 --profile
    python3 follow.py --no-flip              # 摄像头装正时用
    python3 follow.py --model yolov8n.pt     # 强制 PyTorch
    GIMBAL_BACKEND=ultralytics python3 follow.py   # 用 ultralytics 的 TRT 路径
"""
import sys, time, os, math

sys.path.insert(0, "/home/nvidia/project")

import cv2
from detect import load, detect_frame
import servo

# ---------------- 配置 ----------------
SENSOR_W, SENSOR_H = 1280, 720   # 传感器出图（模式 4：720p60）
FRAME_W, FRAME_H = 640, 360       # nvvidconv 在 GPU 上缩放后的取帧尺寸
W, H = FRAME_W, FRAME_H           # 下面用这个做水平映射（模型实际看到的也是 640×360）
FPS = 60
PREHEAT = 60
CONF = 0.4

UPSIDE_DOWN = True        # 摄像头装反了，画面需要 180°（用 offset 取负实现，不再逐帧旋转）

PAN_ID = 1
PAN_MIN, PAN_MAX = 400, 600
PAN_CENTER = 500

TAU = 0.030               # 一阶滤波时间常数（秒）
MOVE_MS = 50              # 舵机运动时间参数（原 200）
DEADBAND = 4              # 死区（编码器单位，4 ≈ 0.96°）
MAX_STEP = 12.0           # 每帧最大步长（编码器单位），保护机械结构
LEAD = TAU                # 前馈提前量（秒）
FEEDFORWARD = True

EXPOSURE_MAX_NS = 8000000  # 曝光上限 8ms（60fps 下 AE 不能拉满一帧）

PROFILE_EVERY = 200


class Prof:
    """分段打点：只看 p50/p95，控制环怕的是抖动不是均值。"""

    def __init__(self, every=PROFILE_EVERY):
        self.every = every
        self.data = {}
        self.n = 0

    def add(self, key, ms):
        self.data.setdefault(key, []).append(ms)

    def tick(self):
        self.n += 1
        if self.n % self.every:
            return
        parts = []
        for key in ("grab", "infer", "ctrl", "loop", "bright"):
            v = self.data.get(key)
            if not v:
                continue
            v = sorted(v)
            n = len(v)
            parts.append("%s p50=%.1f p95=%.1f" % (key, v[n // 2], v[min(n - 1, int(n * 0.95))]))
            self.data[key] = []
        print("[prof] " + " | ".join(parts), flush=True)


def open_camera():
    if not os.path.exists("/dev/video0"):
        raise RuntimeError("找不到 /dev/video0（摄像头没接好）")
    pipeline = (
        "nvarguscamerasrc sensor-id=0 aeantibanding=2"
        " exposuretimerange=\"1000 %d\" gainrange=\"1 16\""
        " ! video/x-raw(memory:NVMM),width=%d,height=%d,framerate=%d/1,format=NV12"
        " ! nvvidconv ! video/x-raw(memory:NVMM),width=%d,height=%d,format=NV12"
        " ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert"
        " ! video/x-raw,format=BGR ! appsink drop=1 max-buffers=1 sync=false"
        % (EXPOSURE_MAX_NS, SENSOR_W, SENSOR_H, FPS, FRAME_W, FRAME_H)
    )
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        raise RuntimeError("打不开摄像头管线")
    return cap


class Follower:
    def __init__(self, use_servo=True, flip=True, model_path=None, profile=False):
        self.flip = flip
        self.prof = Prof() if profile else None
        print("[1/3] 打开摄像头 (%dx%d@%d)..." % (W, H, FPS))
        self.cap = open_camera()
        print("[2/3] 预热摄像头（自动曝光收敛）...")
        for _ in range(PREHEAT):
            self.cap.read()
        print("[3/3] 加载模型（TensorRT 优先，找不到回退 PyTorch）...")
        self.model = load(model_path)
        self.bus = None
        if use_servo:
            self.bus = servo.ServoBus()
            print("     舵机串口:", self.bus.port)
            self.bus.center(800)
            time.sleep(1.5)
        self.pan = float(PAN_CENTER)
        self.target_prev = float(PAN_CENTER)
        self.v = 0.0
        self.t_prev = time.perf_counter()
        self.last_drain = time.time()
        self.frames = 0
        self.hits = 0
        print("准备完成！（画面翻转: %s，前馈: %s）\n" % (
            "开" if flip else "关", "开" if FEEDFORWARD else "关"))

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
        t0 = time.perf_counter()
        dt = max(t0 - self.t_prev, 1e-3)
        self.t_prev = t0

        ok, frame = self.cap.read()
        t1 = time.perf_counter()
        if not ok:
            return None

        dets, _ = detect_frame(self.model, frame, conf=CONF)
        t2 = time.perf_counter()
        person = self.biggest_person(dets)
        self.frames += 1

        if person:
            self.hits += 1
            x1, y1, x2, y2 = person["box"]
            cx = (x1 + x2) / 2.0
            fw = frame.shape[1]
            offset = max(-1.0, min(1.0, (cx - fw / 2.0) / (fw / 2.0)))
            if self.flip:
                offset = -offset
            target = PAN_CENTER + offset * (PAN_MAX - PAN_MIN) / 2.0
            # 前馈：目标速度估计（低通必须有，否则框抖动会被放大）
            raw_v = (target - self.target_prev) / dt
            self.v = 0.75 * self.v + 0.25 * raw_v
            self.target_prev = target
        else:
            target = float(PAN_CENTER)
            self.v = 0.0
            self.target_prev = target

        # 死区：误差小于 1° 就当没误差（有了它才敢把 TAU 调到 30ms）
        if abs(target - self.pan) < DEADBAND:
            target = self.pan

        if FEEDFORWARD and person:
            lead = target + self.v * LEAD
            lead = max(PAN_MIN, min(PAN_MAX, lead))   # 必须限幅，否则目标消失时会冲出去
        else:
            lead = target

        alpha = 1.0 - math.exp(-dt / TAU)
        want = self.pan + (lead - self.pan) * alpha

        # 每帧最大步长：大跳变时不猛甩，保护 FPC 排线
        self.pan += max(-MAX_STEP, min(MAX_STEP, want - self.pan))
        self.send_move(self.pan)

        t3 = time.perf_counter()
        if self.prof:
            self.prof.add("grab", (t1 - t0) * 1000)
            self.prof.add("infer", (t2 - t1) * 1000)
            self.prof.add("ctrl", (t3 - t2) * 1000)
            self.prof.add("loop", (t3 - t0) * 1000)
            self.prof.add("bright", float(frame.mean()))
            self.prof.tick()

        if verbose:
            if person:
                x1, y1, x2, y2 = person["box"]
                cx = (x1 + x2) / 2.0
                fw = frame.shape[1]
                side = "左" if cx < fw / 2 - 0.15 * fw else ("右" if cx > fw / 2 + 0.15 * fw else "中")
                print("  人 中心x=%4.0f(%s) 目标=%3.0f 舵机=%3.0f (dt=%4.1fms)"
                      % (cx, side, target, self.pan, dt * 1000))
            else:
                print("  没人 → 回中 舵机=%3.0f (dt=%4.1fms)" % (self.pan, dt * 1000))
            return {"person": bool(person), "pan": self.pan}
        return {"person": bool(person), "pan": self.pan}

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
    profile = "--profile" in args
    max_frames = None
    max_seconds = None
    model_path = None
    for i, a in enumerate(args):
        if a == "--frames" and i + 1 < len(args):
            max_frames = int(args[i + 1])
        if a == "--seconds" and i + 1 < len(args):
            max_seconds = int(args[i + 1])
        if a == "--model" and i + 1 < len(args):
            model_path = args[i + 1]

    f = Follower(use_servo=use_servo, flip=flip, model_path=model_path, profile=profile)
    f.run(max_frames=max_frames, max_seconds=max_seconds)


if __name__ == "__main__":
    main()
