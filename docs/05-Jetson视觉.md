# Jetson 视觉模块

## 摄像头（camera.py）

### 硬件

- **IMX219 CSI 摄像头**，通过 15pin 转 22pin FPC 排线接 Jetson CSI 口
- 设备节点：`/dev/video0`
- 驱动：`nvarguscamerasrc`（GStreamer）

### 三种取帧模式

```python
from camera import photo, video, frames

# 1. 拍照（90 帧预热，取最后一帧）
photo("out.jpg", width=1920, height=1080)

# 2. 录像（H.264 硬件编码）
video("out.mp4", seconds=5)

# 3. 连续取帧（生成器，给检测用）
for frame in frames(width=1280, height=720):
    ...  # frame 是 BGR numpy 数组
```

### ⚠️ 关键：预热帧

`nvarguscamerasrc` 上电后**自动曝光/白平衡需要几十帧才收敛**。直接抓第 1 帧会得到全黑或纯噪声图。

```python
PREHEAT_FRAMES = 90   # 至少 40 帧，90 帧更稳
```

### 排查：摄像头不识别

```bash
# 1. 检查设备节点
ls -l /dev/video0

# 2. 检查设备树 overlay
grep -A3 "OVERLAYS" /boot/extlinux/extlinux.conf

# 3. 关键检查：DEFAULT 必须是 JetsonIO
grep "^DEFAULT" /boot/extlinux/extlinux.conf
# 应该是: DEFAULT JetsonIO
# 如果被系统更新改回 primary，摄像头会消失！
```

---

## YOLO 检测（detect.py）

### 模型

- **yolov8n.pt**：6.25MB，3,157,200 参数，COCO 80 类
- 导出 TensorRT FP16 引擎后 8.82MB

### 性能

| 配置 | 耗时 | 说明 |
|---|---|---|
| FP16 权重 | 27.3ms | 基线 |
| TensorRT FP16 @640 | **19.6ms** | 1.39× 加速 |
| TensorRT FP16 @720 | 27.6ms | 预处理瓶颈，无明显提升 |

**结论**：TensorRT 对 640 输入有效，720 输入时瓶颈在预处理。

### API

```python
from detect import load, detect_frame

model = load("yolov8n.pt")           # 加载
dets, dt = detect_frame(model, frame, conf=0.4)

# dets 格式:
# [{"class": "person", "cn": "人", "conf": 0.87, "box": (x1,y1,x2,y2)}]
```

### 注意：TensorRT 引擎丢失类名

`trtexec` 导出的引擎**不包含类名元数据**，加载后类别名会变成 `class0`、`class5` 等。

逆映射（基于 ultralytics COCO 顺序）：
```
class0  = person
class2  = car
class5  = bus
...
```

---

## 视觉大模型（vlm_see.py）

### 模型选择

| 模型 | 大小 | 速度 | 质量 |
|---|---|---|---|
| qwen2.5vl:3b | 3.2GB | 10 秒/张 | ✅ 准确 |
| granite3.2-vision:2b | 2.4GB | 13 秒/张 | ❌ 描述错误 |
| moondream | 1.7GB | - | ❌ 崩溃 |
| qwen2.5vl:latest | 8.3GB | 177 秒/张 | 66% 卸载到 CPU |

**结论**：用 `qwen2.5vl:3b`。

### 关键限制

```python
# 图片必须缩到 ≤1024 宽
# 否则 2048 个视觉 token 会导致 Ollama 崩溃
img = img.resize((min(1024, w), ...))
```

### 常驻 + 按需架构

```
YOLO（常驻，37 FPS）  → 实时感知，驱动云台
VLM （按需，10 秒/张）→ 用户提问时才加载
```

注意：Ollama 默认保持模型 5 分钟（`keep_alive`）。VLM 加载后占 5.4GB 显存，**和摄像头同时使用会内存不足**，需要先 `ollama stop`。

---

## 语音识别（asr_cpu.py）

- 使用 CPU 模式（GPU 显存不够）
- 加载 3.6 秒，推理 3.4 秒
- 麦克风：USB 语音模块

```python
model = WhisperModel("tiny", device="cpu", compute_type="int8")
```

**注意**：`amixer` 设置 Mic 音量时，只能用 `cap`，**不要 `unmute`**（会同时打开播放开关，导致啸叫）。
