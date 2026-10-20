#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄像头模块（Jetson IMX219 CSI）

功能：
    photo()   拍照（多帧预热，取最后一帧 —— 自动曝光需要时间收敛）
    video()   录像（H.264 MP4，硬件编码）
    frames()  连续取帧（生成器，给目标检测用）
    info()    设备信息

命令行：
    python3 camera.py info
    python3 camera.py photo out.jpg [--width 1920] [--height 1080]
    python3 camera.py video out.mp4 [--seconds 5]
    python3 camera.py watch [--frames 30]

导入：
    from camera import photo, video, frames, info
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

SENSOR_ID = 0
DEFAULT_W = 1920
DEFAULT_H = 1080
DEFAULT_FPS = 30
DEFAULT_STREAM_FPS = 60      # 连续取帧默认 60fps（IMX219 720p 支持 59.999fps）
EXPOSURE_MAX_NS = 8000000    # 流式取帧时曝光上限 8ms：60fps 下自动曝光不能拉满一帧
PREHEAT_FRAMES = 90          # 预热帧数：自动曝光/白平衡需要这么多帧才收敛

GST = "gst-launch-1.0"


def _run(cmd, timeout=120):
    """执行命令，返回 (returncode, stdout+stderr)"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        return -1, "命令超时"


def _check_device():
    """检查摄像头设备是否存在"""
    if not os.path.exists("/dev/video0"):
        raise RuntimeError(
            "找不到 /dev/video0 —— 摄像头没被识别。\n"
            "排查顺序：\n"
            "  1. 开机后运行 bash /home/nvidia/project/cam_check.sh\n"
            "  2. 检查 /boot/extlinux/extlinux.conf 的 DEFAULT 是不是 JetsonIO\n"
            "  3. 检查排线方向（开发板端金手指朝下）"
        )


# ---------------------------------------------------------------- 拍照
def photo(path, width=DEFAULT_W, height=DEFAULT_H, preheat=PREHEAT_FRAMES):
    """拍一张照片。

    关键点：抓 preheat 帧后只保留最后一帧。
    因为 nvarguscamerasrc 上电后的前几帧自动曝光还没收敛，
    直接抓第 1 帧会得到全黑/纯噪声图。
    """
    _check_device()
    tmpd = tempfile.mkdtemp(prefix="cam_")
    try:
        pipeline = (
            GST + " -e nvarguscamerasrc sensor-id=" + str(SENSOR_ID) +
            " num-buffers=" + str(preheat) +
            ' ! "video/x-raw(memory:NVMM),width=' + str(width) +
            ",height=" + str(height) + ',format=NV12"'
            " ! nvjpegenc ! multifilesink location=" + tmpd + "/f_%03d.jpg"
        )
        rc, out = _run(["bash", "-c", pipeline], timeout=120)
        shots = sorted(f for f in os.listdir(tmpd) if f.endswith(".jpg"))
        if not shots:
            raise RuntimeError("拍照失败，没有产出文件。\n" + out[-800:])
        shutil.copy(os.path.join(tmpd, shots[-1]), path)
        return path
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


# ---------------------------------------------------------------- 录像
def video(path, seconds=5, width=DEFAULT_W, height=DEFAULT_H, fps=DEFAULT_FPS,
          bitrate=8000000):
    """录一段 MP4（H.264 硬件编码）。"""
    _check_device()
    pipeline = (
        GST + " -e nvarguscamerasrc sensor-id=" + str(SENSOR_ID) +
        ' ! "video/x-raw(memory:NVMM),width=' + str(width) + ",height=" + str(height) +
        ",framerate=" + str(fps) + '/1,format=NV12"'
        " ! nvvidconv ! nvv4l2h264enc bitrate=" + str(bitrate) +
        " ! h264parse ! qtmux ! filesink location=" + path
    )
    rc, out = _run(["bash", "-c", pipeline], timeout=seconds + 30)
    # gst-launch 不会自己停，用 timeout 控制；检查文件是否生成
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError("录像失败。\n" + out[-800:])
    return path


# ---------------------------------------------------------------- 连续取帧
def frames(width=DEFAULT_W, height=DEFAULT_H, fps=DEFAULT_STREAM_FPS,
           exposure_max_ns=EXPOSURE_MAX_NS):
    """生成器：连续产出 BGR 帧（给目标检测用）。

    默认 60fps + 曝光上限 8ms + max-buffers=1：
    取帧等待从 33ms 降到 16.7ms，且拿到的永远是最新帧（不排队）。

    用法：
        for f in frames():
            ...  # f 是 numpy 数组
    """
    import cv2
    _check_device()
    pipeline = (
        "nvarguscamerasrc sensor-id=" + str(SENSOR_ID) + " aeantibanding=2"
        ' exposuretimerange="1000 ' + str(int(exposure_max_ns)) + '" gainrange="1 16"'
        ' ! video/x-raw(memory:NVMM),width=' + str(width) + ",height=" + str(height) +
        ",framerate=" + str(fps) + '/1,format=NV12"'
        " ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert"
        " ! video/x-raw,format=BGR ! appsink drop=1 max-buffers=1 sync=false"
    )
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        raise RuntimeError(
            "打不开摄像头取帧管线。可能是 OpenCV 没编译 GStreamer 支持。\n"
            "检查：python3 -c \"import cv2; print('GStreamer' in cv2.getBuildInformation())\""
        )
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            yield frame
    finally:
        cap.release()


# ---------------------------------------------------------------- 设备信息
def info():
    """打印摄像头设备信息。"""
    print("=" * 50)
    print(" 摄像头设备信息")
    print("=" * 50)
    print("设备节点:", "/dev/video0" if os.path.exists("/dev/video0") else "❌ 不存在")
    print("media 节点:", "/dev/media0" if os.path.exists("/dev/media0") else "不存在")
    print("")
    rc, out = _run(["v4l2-ctl", "--list-devices"])
    print(out.strip() if rc == 0 else "v4l2-ctl 不可用")
    print("")
    rc, out = _run(["bash", "-c",
                    "dmesg 2>/dev/null | grep -i imx219 | tail -4 || "
                    "sudo dmesg 2>/dev/null | grep -i imx219 | tail -4"])
    if out.strip():
        print("内核记录:")
        print(out.strip())


# ---------------------------------------------------------------- 命令行
def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]
    opts = {}
    i = 1
    positional = []
    while i < len(args):
        if args[i].startswith("--"):
            k = args[i][2:]
            v = args[i + 1] if i + 1 < len(args) else "1"
            opts[k] = v
            i += 2
        else:
            positional.append(args[i])
            i += 1

    if cmd == "info":
        info()

    elif cmd == "photo":
        out = positional[0] if positional else "/tmp/photo.jpg"
        t0 = time.time()
        photo(out,
              width=int(opts.get("width", DEFAULT_W)),
              height=int(opts.get("height", DEFAULT_H)))
        n = os.path.getsize(out)
        print("✅ 拍照完成: " + out + "  (" + str(round(n / 1024)) + " KB, " +
              str(round(time.time() - t0, 2)) + " 秒)")

    elif cmd == "video":
        out = positional[0] if positional else "/tmp/video.mp4"
        secs = int(opts.get("seconds", 5))
        print("录像 " + str(secs) + " 秒 → " + out)
        t0 = time.time()
        video(out, seconds=secs,
              width=int(opts.get("width", DEFAULT_W)),
              height=int(opts.get("height", DEFAULT_H)))
        n = os.path.getsize(out)
        print("✅ 录像完成: " + out + "  (" + str(round(n / 1024 / 1024, 2)) + " MB, " +
              str(round(time.time() - t0, 2)) + " 秒)")

    elif cmd == "watch":
        n = int(opts.get("frames", 30))
        print("连续取帧测试，共 " + str(n) + " 帧...")
        t0 = time.time()
        got = 0
        for f in frames(width=int(opts.get("width", 1280)),
                        height=int(opts.get("height", 720))):
            got += 1
            if got == 1:
                print("帧尺寸:", f.shape)
            if got >= n:
                break
        dt = time.time() - t0
        print("✅ 取到 " + str(got) + " 帧，耗时 " + str(round(dt, 2)) +
              " 秒 → " + str(round(got / dt, 1)) + " FPS")

    else:
        print("未知命令:", cmd)
        print(__doc__)


if __name__ == "__main__":
    main()
