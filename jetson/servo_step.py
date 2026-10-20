#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""舵机阶跃响应测量（Phase 0.2）

为什么需要
----------
整个延迟模型的地基是「MOVE_MS 这个参数 = 舵机跟随的稳态滞后」。
这个数只能实测：发一个阶跃命令，看舵机用多久走到位。

注意
----
- servo.py 的 cmd() 固定等 0.4~0.6s，不能用来采样；这里直接写串口 + 快读。
- STM32 的 R 命令内部会等 50ms，所以采样分辨率 ~50ms，只能分辨 50ms 级差异。
  要更准就用手机 240fps 慢动作拍舵机臂，数帧数 × 4.17ms。
- 会真的动舵机（限位内，±25 编码 ≈ 6°）。

用法:
    python3 servo_step.py            # 对比 ms=50 与 ms=200
    python3 servo_step.py 40 250     # 自定义两步 ms
"""
import sys
import time

from servo import ServoBus

START = 500
TARGET = 550          # +50 编码 ≈ +12°，在 400~600 限位内


def raw_read(bus, sid, timeout=0.12):
    """直接读写串口，绕过 servo.cmd() 的 0.4s 等待。"""
    bus.s.reset_input_buffer()
    bus.s.write(("R%d\r\n" % sid).encode())
    t0 = time.time()
    buf = b""
    while time.time() - t0 < timeout:
        buf += bus.s.read(256)
    txt = buf.decode("utf-8", "replace")
    for line in txt.splitlines():
        if "字节" not in line:
            continue
        try:
            data = [int(h, 16) for h in line.split(":")[1].split()]
        except Exception:
            continue
        for i in range(len(data) - 7):
            if data[i:i + 3] == [0x55, 0x55, sid] and data[i + 3] == 0x05:
                v = data[i + 5] | (data[i + 6] << 8)
                return v - 65536 if v > 32000 else v
    return None


def main():
    steps = [int(x) for x in sys.argv[1:]] or [50, 200]
    bus = ServoBus()
    print("串口:", bus.port, flush=True)

    for ms in steps:
        bus.s.write(("S1,%d,1000\r\n" % START).encode())
        time.sleep(1.5)
        t0 = time.time()
        bus.s.write(("S1,%d,%d\r\n" % (TARGET, ms)).encode())
        samples = []
        while time.time() - t0 < 0.8:
            pos = raw_read(bus, 1)
            samples.append((round((time.time() - t0) * 1000), pos))
        arrive = next((t for t, pos in samples if pos is not None and abs(pos - TARGET) <= 2), None)
        print("ms=%-4d 到位=%s 采样=%s" % (ms, ("%dms" % arrive) if arrive is not None else "未到位", samples), flush=True)

    bus.close()


if __name__ == "__main__":
    main()
