# 边缘 AI 云台跟随系统

> 基于 **Jetson Orin Nano Super** + **STM32F103VET6** + **LX-16A 总线舵机**
> 实现「摄像头看到人 → 云台自动跟随」的完整边缘 AI 系统。

---

## 🎯 项目简介

这是一个**从零构建**的边缘 AI 运动控制系统，完整覆盖：

```
感知（摄像头）→ 推理（YOLO/TensorRT）→ 决策（偏移计算）
    → 通信（串口）→ 控制（STM32）→ 执行（总线舵机）→ 物理转动
```

**全部代码自研**，包括 STM32 裸机固件（无 HAL 库）、LX-16A 协议实现、云台跟随算法。

---

## 📊 实测性能

| 指标 | 数值 |
|---|---|
| 检测帧率 | **37 FPS**（27ms/帧，TensorRT FP16） |
| 检测准确率 | **100%**（3509/3509 帧） |
| 舵机定位精度 | **±1 / 1000**（约 ±0.24°） |
| 跟随响应 | 实时（每帧更新） |
| 端到端延迟 | < 50ms |

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────┐
│  Jetson Orin Nano Super (JetPack 6.2.1)                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ IMX219 CSI   │→ │ YOLOv8n      │→ │ 偏移计算      │  │
│  │ 摄像头        │  │ TensorRT FP16│  │ 角度映射      │  │
│  └──────────────┘  └──────────────┘  └──────┬───────┘  │
│                                              │           │
│                              servo.py (串口协议)         │
└──────────────────────────────────────────────┼──────────┘
                                               │ USB (CH340)
┌──────────────────────────────────────────────┼──────────┐
│  STM32F103VET6 (野火指南者)                   ↓          │
│  ┌──────────────────────────────────────────────────┐   │
│  │ 裸机固件：串口命令解析 → LX-16A 帧编码             │   │
│  │ USART1 (PC)  ←→  USART2 (舵机总线)                │   │
│  └──────────────────────────┬───────────────────────┘   │
└─────────────────────────────┼───────────────────────────┘
                              │ 半双工 UART 115200
                    ┌─────────┴─────────┐
                    │  LX-16A 舵机 ×2   │
                    │  ID1 水平  ID2 俯仰│
                    └───────────────────┘
```

---

## 🔧 硬件清单

| 部件 | 型号 | 说明 |
|---|---|---|
| 主控 | Jetson Orin Nano Super 8GB | JetPack 6.2.1 / L4T 36.4.7 |
| 摄像头 | IMX219 CSI | 1920×1080，15pin 转 22pin 排线 |
| 协处理器 | STM32F103VET6 | 野火「指南者」开发板 |
| 舵机 | LX-16A 总线舵机 ×2~3 | 7.4V，0~1000 位置（240°） |
| 电源 | 7.4V 锂电池 | 舵机独立供电 |
| 烧录 | USB 转串口（CH340） | 板载，一键 ISP |
| 连接 | 面包板 + 杜邦线 | 共地 + 分线 |

---

## 📁 目录结构

```
edge-ai-gimbal/
├── README.md
├── docs/
│   ├── 01-系统架构.md
│   ├── 02-硬件接线.md
│   ├── 03-STM32固件.md
│   ├── 04-LX16A协议.md
│   ├── 05-Jetson视觉.md
│   ├── 06-云台跟随.md
│   ├── 07-踩坑记录.md
│   ├── 08-性能地图.md
│   └── 09-延迟优化方案.md
├── stm32/                    # STM32 裸机固件
│   ├── main.c                # 主程序（命令解析 + 舵机协议）
│   ├── startup.c             # 启动文件 + 中断向量表
│   ├── linker.ld             # 链接脚本
│   ├── Makefile              # 编译脚本
│   └── flash.ps1             # FlyMcu 自动烧录（Windows）
└── jetson/                   # Jetson 端 Python
    ├── camera.py             # 摄像头模块（拍照/录像/取帧）
    ├── detect.py             # YOLO 检测
    ├── servo.py              # 舵机控制库（含安全限位）
    └── follow.py             # 云台跟随主程序
```

---

## 🚀 快速开始

### 1. 编译并烧录 STM32 固件

```powershell
cd stm32

# 编译（需要 arm-none-eabi-gcc）
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -nostdlib -ffreestanding -c main.c -o main.o
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -nostdlib -ffreestanding -c startup.c -o startup.o
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -T linker.ld -nostdlib -Wl,--gc-sections -o firmware.elf startup.o main.o
arm-none-eabi-objcopy -O ihex firmware.elf firmware.hex

# 自动烧录（需要 FlyMcu）
powershell -File flash.ps1 firmware.hex
```

### 2. Jetson 端部署

```bash
# 复制文件到 Jetson
scp jetson/*.py nvidia@192.168.55.1:/home/nvidia/project/

# 安装依赖（通常已自带）
pip3 install pyserial ultralytics opencv-python

# 扫描舵机
cd /home/nvidia/project
python3 servo.py scan

# 云台跟随
python3 follow.py --seconds 60
```

### 3. 安全限位（重要）

摄像头使用 FPC 硬排线，**反复弯折会断裂**。所以舵机默认限制在 ±24°：

```python
# jetson/servo.py
PAN_MIN, PAN_MAX = 400, 600     # ID1 水平，±24°
TILT_MIN, TILT_MAX = 400, 600   # ID2 俯仰
```

需要更大角度时调整这两个常量，但要先确认排线走线有余量。

---

## 🎮 使用示例

```python
from servo import ServoBus

bus = ServoBus()                  # 自动识别串口

bus.scan()                        # 扫描在线舵机 → [(1, 501), (2, 498)]
bus.move(1, 500, 1000)            # 舵机1 到中位，1 秒
bus.move(2, 450)                  # 舵机2
bus.read_pos(1)                   # 读位置 → 501
bus.center()                      # 两个回中位
bus.move(1, 300, bypass=True)     # 强制超出安全限位
```

```bash
python3 follow.py                # 跟随模式（Ctrl+C 退出）
python3 follow.py --frames 20    # 只跑 20 帧
python3 follow.py --no-servo     # 只检测不动舵机
python3 follow.py --no-flip      # 摄像头装正时用
```

---

## 🎯 关键设计

### 1. STM32 裸机固件（无库依赖）

- 手写启动文件、中断向量表、链接脚本
- 直接操作寄存器（RCC/GPIO/USART/SysTick）
- 编译产物仅 **4.5KB**

### 2. LX-16A 协议实现

- 帧格式：`55 55 ID Length Cmd Params Checksum`
- **关键**：`Length = ID + Cmd + 参数 + 校验和`，`Length + 3 = 整包长度`
- 支持：位置控制、读位置、改 ID、装载/卸载

### 3. 无电阻的舵机接线

利用 STM32 的特性实现**无上拉电阻**的总线连接：

- **PA2（TX）**：推挽输出发送 → 发完立刻切成高阻（避免和舵机应答抢总线）
- **PA3（RX）**：输入上拉（提供总线空闲电平）

### 4. 自动烧录

通过 Win32 API 自动化 FlyMcu GUI（Delphi 程序不暴露 UI Automation 控件）：

1. 修改 `FlyMcuConfig.ini` 预设参数
2. 启动程序并置顶窗口
3. 用**真实鼠标事件**点击「开始编程」
4. 检查 `ProgramLogs.txt` 判断结果

---

## 🐛 踩坑记录

详见 [docs/07-踩坑记录.md](docs/07-踩坑记录.md)，摘要：

| 坑 | 原因 | 教训 |
|---|---|---|
| 舵机完全不动 | 协议帧多加了 2 字节「速度」，Length 算错 | **协议文档必须读完再写代码** |
| 摄像头消失 | 系统更新重置了 `extlinux.conf` 的 `DEFAULT` | 板子出问题先查配置文件 |
| 舵机收不到信号 | PA2/PA3 插在面包板不同排 | 用「回环自测」验证线路 |
| FlyMcu 点不动 | 程序被其他窗口遮挡 | 先置顶再模拟鼠标点击 |
| 拍照全黑 | 自动曝光未收敛 | 抓 60~90 帧预热后取最后一帧 |

---

## 📈 后续优化方向

- [ ] **降低跟随延迟**（方案见 [docs/09-延迟优化方案.md](docs/09-延迟优化方案.md)）
- [ ] 把摄像头装到舵机上（真正的云台）
- [ ] 加俯仰舵机（上下看）
- [ ] VLM 按需识别（`qwen2.5vl:3b`，「这是什么？」）
- [ ] 语音控制（「转过来」「看右边」）
- [ ] 人脸跟随（比 YOLO 的 person 更精细）

---

## 📄 License

MIT
