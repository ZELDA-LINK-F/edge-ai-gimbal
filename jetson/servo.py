#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LX-16A 舵机控制（通过 STM32 转接）
====================================
硬件: Jetson --USB--> STM32F103VET6 --UART2--> LX-16A 总线舵机

安全限位（重要！）
------------------
摄像头用的是 FPC 硬排线，反复弯折会断。所以舵机默认限制在
安全的旋转范围内（±约 48°），需要时用 bypass=True 临时放开。

    PAN_MIN, PAN_MAX   : ID 1 水平舵机
    TILT_MIN, TILT_MAX : ID 2 俯仰舵机

用法:
    python3 servo.py scan              # 扫描舵机
    python3 servo.py move 1 500        # 舵机1 到中位
    python3 servo.py move 1 500 1000   # 带时间
    python3 servo.py limit             # 显示安全限位
    python3 servo.py test              # 完整测试（限位内）
    python3 servo.py center            # 两个舵机回中位
"""
import sys, time, glob

try:
    import serial
except ImportError:
    print("需要 pyserial: sudo pip3 install pyserial")
    sys.exit(1)

BAUD = 115200

# ============================================================
#  安全限位（保护摄像头 FPC 排线）
#  范围 0~1000，500 = 中位
#  ±48° 对应 500±100，排线几乎不受力
# ============================================================
PAN_MIN,  PAN_MAX  = 400, 600     # ID 1  水平
TILT_MIN, TILT_MAX = 400, 600     # ID 2  俯仰

LIMITS = {1: (PAN_MIN, PAN_MAX), 2: (TILT_MIN, TILT_MAX)}


def candidates():
    out = []
    for pat in ['/dev/ttyCH341USB*', '/dev/ttyUSB*', '/dev/ttyACM*']:
        out += sorted(glob.glob(pat))
    return out


def probe(port):
    try:
        s = serial.Serial(port, BAUD, timeout=0.5)
        s.dtr = False
        s.rts = False
        time.sleep(0.35)
        s.reset_input_buffer()
        s.write(b"PING\r\n")
        time.sleep(0.6)
        data = s.read(512)
        s.close()
        return b"PONG" in data
    except Exception:
        return False


def find_port():
    for p in candidates():
        if probe(p):
            return p
    return None


class ServoBus:
    def __init__(self, port=None, baud=BAUD):
        self.port = port or find_port()
        if not self.port:
            raise RuntimeError("找不到 STM32 串口（检查 USB 线和固件）")
        self.s = serial.Serial(self.port, baud, timeout=0.5)
        self.s.dtr = False
        self.s.rts = False
        time.sleep(0.5)
        self.s.reset_input_buffer()

    # ---------- 底层 ----------
    def cmd(self, text, wait=0.4):
        self.s.reset_input_buffer()
        self.s.write((text + "\r\n").encode())
        time.sleep(wait)
        out = b""
        t0 = time.time()
        while time.time() - t0 < 0.3:
            d = self.s.read(512)
            if d:
                out += d
        return [l for l in out.decode("utf-8", "replace").splitlines()
                if l.strip() and "HELLO" not in l]

    def ping(self):
        return "PONG" in " ".join(self.cmd("PING"))

    # ---------- 安全限位 ----------
    def clamp(self, sid, pos, bypass=False):
        """把位置限制在安全范围内，返回 (实际位置, 是否被限制)"""
        pos = max(0, min(1000, int(pos)))
        if bypass:
            return pos, False
        lo, hi = LIMITS.get(sid, (0, 1000))
        if pos < lo:
            return lo, True
        if pos > hi:
            return hi, True
        return pos, False

    # ---------- 运动 ----------
    def move(self, sid, pos, ms=1000, bypass=False):
        """舵机运动（默认受安全限位保护）"""
        p, clipped = self.clamp(sid, pos, bypass)
        if clipped:
            print("  [限位] ID%d: %d -> %d (保护排线)" % (sid, pos, p))
        ms = max(0, min(30000, int(ms)))
        return self.cmd("S%d,%d,%d" % (sid, p, ms))

    def center(self, ms=1000):
        """两个舵机回中位"""
        self.move(1, 500, ms)
        self.move(2, 500, ms)

    # ---------- 读取 ----------
    def read_pos(self, sid):
        """读位置：严格校验 帧头 + ID + 指令 + 校验和"""
        lines = self.cmd("R%d" % sid, wait=0.6)
        for line in lines:
            if "收" not in line or "字节" not in line:
                continue
            parts = line.split(":")
            if len(parts) < 2:
                continue
            try:
                data = [int(h, 16) for h in parts[1].split()]
            except ValueError:
                continue
            # 应答帧: 55 55 ID 05 1C pos_lo pos_hi CS
            for i in range(len(data) - 7):
                if (data[i] == 0x55 and data[i+1] == 0x55
                        and data[i+2] == sid and data[i+3] == 0x05
                        and data[i+4] == 0x1C):
                    body = data[i+2:i+7]
                    cs = (~sum(body)) & 0xFF
                    if cs == data[i+7]:
                        v = data[i+5] | (data[i+6] << 8)
                        # 0xFFFE/0xFFFF 是顶到限位时的负过冲
                        return v - 65536 if v > 32000 else v
        return None

    def scan(self, maxid=10):
        found = []
        for i in range(1, maxid + 1):
            p = self.read_pos(i)
            if p is not None:
                found.append((i, p))
        return found

    def close(self):
        self.s.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]
    bus = ServoBus()
    print("[串口] " + bus.port)

    if not bus.ping():
        print("❌ STM32 没响应")
        return
    print("[通信] PONG ✅")

    if action == "scan":
        print("[扫描] 查找舵机...")
        for sid, pos in bus.scan():
            print("  ID=%d  位置=%d" % (sid, pos))

    elif action == "move":
        sid = int(sys.argv[2])
        pos = int(sys.argv[3])
        ms = int(sys.argv[4]) if len(sys.argv) > 4 else 1000
        print(bus.move(sid, pos, ms))

    elif action == "center":
        print("回中位...")
        bus.center()
        time.sleep(2)
        print("  ID1 =", bus.read_pos(1))
        print("  ID2 =", bus.read_pos(2))

    elif action == "limit":
        print("")
        print("=== 安全限位（保护摄像头排线）===")
        for sid, (lo, hi) in sorted(LIMITS.items()):
            print("  ID%d: %d ~ %d  (中位 500，范围 ±%d)" % (sid, lo, hi, (hi - lo) // 2))
        print("")
        print("  中位 500 对应 0°，1000 对应 240°")
        print("  当前限制 ±%d 约等于 ±%d°" % ((PAN_MAX - PAN_MIN) // 2,
                                            int((PAN_MAX - PAN_MIN) // 2 * 0.24)))

    elif action == "test":
        print("")
        for sid in (1, 2):
            print("=== 舵机 %d（限位内）===" % sid)
            lo, hi = LIMITS[sid]
            for p in [lo, hi, 500]:
                bus.move(sid, p, 1000)
                time.sleep(2.0)
                print("  目标 %4d -> 实际 %s" % (p, bus.read_pos(sid)))
        print("")
        print("=== 云台联动 ===")
        for a, b in [(450, 550), (550, 450), (500, 500)]:
            bus.move(1, a, 1200)
            bus.move(2, b, 1200)
            time.sleep(1.6)
            print("  云台 (%d, %d)  实际 (%s, %s)" % (a, b, bus.read_pos(1), bus.read_pos(2)))

    elif action == "ping":
        print("OK")

    bus.close()


if __name__ == "__main__":
    main()
