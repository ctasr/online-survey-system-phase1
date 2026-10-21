"""Build an index image from the real browser captures."""

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
folder = ROOT / "output" / "第一阶段_实际运行流程"
steps = json.loads((folder / "步骤清单.json").read_text(encoding="utf-8"))
columns, cell_w, cell_h = 4, 423, 340
rows = (len(steps) + columns - 1) // columns
canvas = Image.new("RGB", (columns * cell_w + 72, rows * cell_h + 190), "#f4f2f8")
draw = ImageDraw.Draw(canvas)
bold = r"C:\Windows\Fonts\msyhbd.ttc"
regular = r"C:\Windows\Fonts\msyh.ttc"
draw.text((37, 26), "在线问卷系统 · 实际运行流程", font=ImageFont.truetype(bold, 38), fill="#30285f")
draw.text((39, 87), "按编号查看；每一步的完整页面截图都在同一文件夹。", font=ImageFont.truetype(regular, 20), fill="#666179")

for position, step in enumerate(steps):
    x = 36 + (position % columns) * cell_w
    y = 150 + (position // columns) * cell_h
    draw.rounded_rectangle((x, y, x + 405, y + 316), radius=15, fill="white", outline="#e0dbea", width=2)
    with Image.open(folder / step["file"]) as original:
        view = original.convert("RGB").crop((0, 0, min(original.width, 1360), min(original.height, 900)))
        view = ImageOps.fit(view, (378, 251), method=Image.Resampling.LANCZOS)
        canvas.paste(view, (x + 14, y + 13))
    draw.text((x + 16, y + 275), f'{step["number"]:02d}  {step["name"]}', font=ImageFont.truetype(bold, 19), fill="#30285f")

path = folder / "00_步骤总览.png"
canvas.save(path, optimize=True)
print(path)
