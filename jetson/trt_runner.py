#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 TensorRT 推理（绕开 ultralytics 的 Python 前后处理）

为什么要有这个文件
------------------
ultralytics 走 engine 时，detect_frame 实测 ~20ms，而 engine 纯计算只有 ~4.5ms，
差的 15ms 全在 ultralytics 的 letterbox/NMS/Python 上。本文件把这条链路自己实现一遍，
目标：**pre + infer + post < 10ms**。

设计要点
--------
- 内存全部用 torch 分配并常驻（torch 的 CUDA 分配器），不依赖 pycuda（板子上没装）
- 输入输出地址在初始化时绑定一次，循环里不再创建任何 buffer
- 预处理：CPU 上 letterbox（cv2.resize + 灰边），一次转 float16 CHW
- 后处理：numpy 向量化解码 + cv2.dnn.NMSBoxes

用法
----
    from trt_runner import TrtRunner
    r = TrtRunner("lab1/yolov8n_fp16.engine", names=COCO80)
    dets, ms = r.infer(frame_bgr, conf=0.4, iou=0.5)
    # dets: [{"class": "person", "conf": 0.89, "box": (x1,y1,x2,y2)}, ...]
"""

import time

import cv2
import numpy as np
import torch

try:
    import tensorrt as trt
except ImportError as exc:  # pragma: no cover
    raise ImportError("需要 tensorrt（JetPack 自带）: " + str(exc))


class TrtRunner:
    def __init__(self, path, names=None, imgsz=640, verbose=False):
        self.path = path
        self.names = names
        self.imgsz = imgsz
        logger = trt.Logger(trt.Logger.INFO if verbose else trt.Logger.WARNING)
        with open(path, "rb") as f:
            blob = f.read()
        runtime = trt.Runtime(logger)
        self.engine = runtime.deserialize_cuda_engine(blob)
        if self.engine is None:
            raise RuntimeError("engine 反序列化失败: " + path)
        self.ctx = self.engine.create_execution_context()

        inputs, outputs = [], []
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                inputs.append(name)
            else:
                outputs.append(name)
        if len(inputs) != 1 or len(outputs) < 1:
            raise RuntimeError("只支持单输入单输出的 YOLO engine，实际: %s / %s" % (inputs, outputs))
        self.in_name = inputs[0]
        self.out_name = outputs[0]

        self.in_shape = tuple(int(d) for d in self.engine.get_tensor_shape(self.in_name))
        self.out_shape = tuple(int(d) for d in self.engine.get_tensor_shape(self.out_name))
        if self.in_shape[2] > 0:
            self.imgsz = self.in_shape[2]
        self.in_dtype = self._torch_dtype(self.engine.get_tensor_dtype(self.in_name))
        self.out_dtype = self._torch_dtype(self.engine.get_tensor_dtype(self.out_name))

        # 常驻显存（torch 分配，不用 pycuda）
        self.in_dev = torch.empty(self.in_shape, dtype=self.in_dtype, device="cuda")
        self.out_dev = torch.empty(self.out_shape, dtype=self.out_dtype, device="cuda")
        self.in_host = torch.empty(self.in_shape, dtype=self.in_dtype, pin_memory=True)
        self.ctx.set_tensor_address(self.in_name, self.in_dev.data_ptr())
        self.ctx.set_tensor_address(self.out_name, self.out_dev.data_ptr())
        self.stream = torch.cuda.Stream()
        self._canvas = None
        self._warmup()

    @staticmethod
    def _torch_dtype(dtype):
        return torch.float16 if trt.nptype(dtype) == np.float16 else torch.float32

    def _warmup(self):
        dummy = np.zeros((self.imgsz, self.imgsz, 3), np.uint8)
        for _ in range(3):
            self.infer(dummy)

    # ---------------- 预处理 ----------------
    def preprocess(self, frame):
        """letterbox 到 imgsz×imgsz → (1,3,H,W) float16 torch 张量

        性能坑（都实测过）：
          - numpy 对「负 stride 的转置视图」做 astype：25ms（逐元素 gather）
          - cv2.dnn.blobFromImage：6.7ms + 再 astype(float16) 6.3ms
          - torch 在 CPU 上做 permute + half + div：0.7ms  ← 用这个
        """
        h0, w0 = frame.shape[:2]
        scale = min(self.imgsz / h0, self.imgsz / w0)
        nw, nh = int(round(w0 * scale)), int(round(h0 * scale))
        if (nw, nh) != (w0, h0):
            resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        else:
            resized = frame
        padw, padh = (self.imgsz - nw) // 2, (self.imgsz - nh) // 2
        if self._canvas is None:
            self._canvas = np.full((self.imgsz, self.imgsz, 3), 114, np.uint8)
        self._canvas[padh:padh + nh, padw:padw + nw] = resized

        tensor = torch.from_numpy(self._canvas)                  # HWC uint8 BGR
        chw = tensor.permute(2, 0, 1).flip(0)                    # CHW + BGR->RGB
        chw = chw.to(torch.float16)
        chw = chw.div_(255.0).contiguous().unsqueeze(0)          # (1,3,H,W)
        return chw, scale, padw, padh

    # ---------------- 后处理 ----------------
    def postprocess(self, out, scale, padw, padh, shape, conf_thres, iou_thres):
        # out: (84, 8400)；前 4 行 cx,cy,w,h（640 空间），后 80 行是类别分数（YOLOv8 无 objectness）
        cls_scores = out[4:]
        scores = cls_scores.max(axis=0)
        keep = scores > conf_thres
        if not keep.any():
            return []
        boxes = out[:4, keep].T
        scores = scores[keep]
        cls_ids = cls_scores.argmax(axis=0)[keep]

        cx, cy, bw, bh = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        x1 = (cx - bw / 2.0 - padw) / scale
        y1 = (cy - bh / 2.0 - padh) / scale
        x2 = (cx + bw / 2.0 - padw) / scale
        y2 = (cy + bh / 2.0 - padh) / scale
        h, w = shape[:2]
        x1 = np.clip(x1, 0, w - 1); x2 = np.clip(x2, 0, w - 1)
        y1 = np.clip(y1, 0, h - 1); y2 = np.clip(y2, 0, h - 1)

        rects = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1).tolist()
        idx = cv2.dnn.NMSBoxes(rects, scores.tolist(), conf_thres, iou_thres)
        idx = np.array(idx).flatten().astype(int).tolist() if len(idx) else []

        dets = []
        for i in idx:
            cls_id = int(cls_ids[i])
            name = self.names[cls_id] if self.names and cls_id < len(self.names) else "class%d" % cls_id
            dets.append({
                "class": name,
                "conf": round(float(scores[i]), 3),
                "box": (round(float(x1[i]), 1), round(float(y1[i]), 1),
                        round(float(x2[i]), 1), round(float(y2[i]), 1)),
            })
        return dets

    # ---------------- 推理 ----------------
    def infer(self, frame, conf=0.4, iou=0.5):
        t0 = time.perf_counter()
        # 允许传 PIL.Image：转成 numpy 并 RGB -> BGR，与相机管线保持一致
        if not isinstance(frame, np.ndarray):
            frame = np.asarray(frame)
            if frame.ndim == 3 and frame.shape[2] == 3:
                frame = frame[:, :, ::-1]
            frame = np.ascontiguousarray(frame)
        chw, scale, padw, padh = self.preprocess(frame)
        self.in_host.copy_(chw)
        with torch.cuda.stream(self.stream):
            self.in_dev.copy_(self.in_host, non_blocking=True)
            ok = self.ctx.execute_async_v3(self.stream.cuda_stream)
        self.stream.synchronize()
        if ok is False:
            raise RuntimeError("TensorRT execute_async_v3 失败")
        out = self.out_dev[0].float().cpu().numpy()
        dets = self.postprocess(out, scale, padw, padh, frame.shape, conf, iou)
        return dets, time.perf_counter() - t0

    def close(self):
        del self.ctx, self.engine
