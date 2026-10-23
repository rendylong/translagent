#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自绘客户端图标:浅底 + 近黑波形 + 红点,纯 numpy 像素绘制,手写 PNG(无 PIL)。

用法:
  make_icon.py <out.png> <size> [A|B|C]     渲染单张
  make_icon.py --icns <outdir> [variant]    渲染 iconset 全尺寸并合成 AppIcon.icns

变体:A=灰底黑波形红点(默认) B=白底黑波形红点 C=灰底、最高一条波形为红。
"""
import os
import struct
import sys
import zlib

import numpy as np

BG = {'A': (244, 245, 246), 'B': (255, 255, 255), 'C': (244, 245, 246)}
INK = (30, 33, 38)
RED = (179, 38, 30)

# 品牌波形(工作台侧栏同款):viewBox 22 内五条 bar 的 x/w/h
WAVE = [(0.0, 2.6, 6.0), (4.8, 2.6, 12.4), (9.7, 2.6, 19.0), (14.6, 2.6, 10.0), (19.4, 2.6, 6.8)]
WAVE_RX = 1.3


def rounded_mask(x, y, x0, y0, x1, y1, r):
    """圆角矩形覆盖矩阵:x/y 为像素中心坐标网格,形状 (h,w)。"""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w, half_h = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    dx = np.maximum(np.abs(x - cx) - half_w, 0)
    dy = np.maximum(np.abs(y - cy) - half_h, 0)
    return dx * dx + dy * dy <= r * r


def circle_mask(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size, variant='A'):
    """返回 RGBA uint8 (size,size,4)。SS 倍超采样后盒式降采样抗锯齿。"""
    ss = 4 if size <= 512 else 2
    n = size * ss
    step = 1.0 / ss
    # 像素中心坐标(单位 = 输出像素),直接以 n 网格生成再按块平均
    c = (np.arange(n) + 0.5) * step
    xx, yy = np.meshgrid(c, c)

    radius = 0.225 * size
    squircle = rounded_mask(xx, yy, 0, 0, size, size, radius)

    # 波形条占图标中央 58%
    k = 0.58 * size / 22.0
    ox = (size - 22 * k) / 2
    oy = (size - 22 * k) / 2
    bars = []
    for i, (bx, bw, bh) in enumerate(WAVE):
        x0, y0 = ox + bx * k, oy + (22 - bh) / 2 * k
        bars.append((rounded_mask(xx, yy, x0, y0, x0 + bw * k, y0 + bh * k, WAVE_RX * k),
                     RED if variant == 'C' and i == 2 else INK))

    red_dot = None
    if variant != 'C':
        dr = 0.082 * size
        red_dot = (circle_mask(xx, yy, 0.775 * size, 0.792 * size, dr), RED)

    bg = np.array(BG[variant], dtype=np.float64)
    layers = bars + ([red_dot] if red_dot else [])
    rgb = np.empty((n, n, 3), dtype=np.float64)
    rgb[:] = bg
    for mask, col in layers:
        rgb[mask] = np.array(col, dtype=np.float64)

    # 按 ss×ss 块平均(SS 网格本就按输出像素对齐)
    a = squircle.reshape(size, ss, size, ss).mean(axis=(1, 3))
    rgb = rgb.reshape(size, ss, size, ss, 3).mean(axis=(1, 3))
    out = np.empty((size, size, 4), dtype=np.uint8)
    out[..., :3] = np.clip(rgb + 0.5, 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(a * 255 + 0.5, 0, 255).astype(np.uint8)
    return out


def write_png(path, rgba):
    h, w = rgba.shape[:2]
    raw = b''.join(b'\x00' + rgba[y].tobytes() for y in range(h))

    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw, 9))
           + chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)


def build_icns(outdir, variant):
    iconset = os.path.join(outdir, 'AppIcon.iconset')
    os.makedirs(iconset, exist_ok=True)
    sizes = [(16, ''), (16, '@2x'), (32, ''), (32, '@2x'), (128, ''), (128, '@2x'),
             (256, ''), (256, '@2x'), (512, ''), (512, '@2x')]
    for base, suffix in sizes:
        px = base * 2 if suffix else base
        name = 'icon_%dx%d%s.png' % (base, base, suffix)
        write_png(os.path.join(iconset, name), render(px, variant))
        print('rendered', name)
    import subprocess
    subprocess.run(['iconutil', '-c', 'icns', iconset, '-o',
                    os.path.join(outdir, 'AppIcon.icns')], check=True)
    print('AppIcon.icns written to', outdir)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    if args[0] == '--icns':
        build_icns(args[1], args[2] if len(args) > 2 else 'A')
        return
    out, size = args[0], int(args[1])
    variant = args[2] if len(args) > 2 else 'A'
    write_png(out, render(size, variant))
    print('rendered', out, size, variant)


if __name__ == '__main__':
    main()
