"""Тестовые данные для проверки галереи/просмотрщика (локальная копия, C:\\cloud\\data)."""
import os
from PIL import Image, ImageDraw

BASE = r"C:\cloud\data\тест-галерея"
os.makedirs(BASE, exist_ok=True)

# 5 фотографий разных размеров/пропорций
specs = [("фото-1.jpg", (1600, 1200), (200, 60, 60)),
         ("фото-2.jpg", (900, 1600), (60, 140, 90)),
         ("фото-3.jpg", (1200, 1200), (60, 90, 180)),
         ("фото-4.jpg", (2400, 800), (170, 130, 40)),
         ("фото-5.jpg", (640, 480), (120, 60, 160))]
for name, size, color in specs:
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    d.ellipse((size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4), fill=(255, 255, 255))
    d.text((20, 20), name, fill=(255, 255, 255))
    im.save(os.path.join(BASE, name), quality=88)

# png с прозрачностью + gif
im = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
ImageDraw.Draw(im).ellipse((50, 50, 450, 450), fill=(255, 120, 0, 200))
im.save(os.path.join(BASE, "прозрачный.png"))

frames = [Image.new("P", (200, 200), i) for i in (1, 5, 9)]
frames[0].save(os.path.join(BASE, "анимация.gif"), save_all=True, append_images=frames[1:], duration=300, loop=0)

# текстовые файлы: utf-8 и cp1251 (проверка фолбэка кодировки)
with open(os.path.join(BASE, "заметки.txt"), "w", encoding="utf-8") as f:
    f.write("Проверка предпросмотра текста — utf-8.\nВторая строка.\n")
with open(os.path.join(BASE, "старое-cp1251.txt"), "wb") as f:
    f.write("Проверка cp1251: ёжик, «кавычки», тире — всё на месте.\n".encode("cp1251"))
with open(os.path.join(BASE, "данные.csv"), "w", encoding="utf-8") as f:
    f.write("артикул;цена\n001;100\n002;200\n")

# архив (предпросмотра нет — должен показать «скачать»)
with open(os.path.join(BASE, "архив.zip"), "wb") as f:
    f.write(b"PK\x03\x04" + b"\x00" * 100)

# минимальный валидный PDF (предпросмотр во встроенном просмотрщике)
pdf = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 62>>stream
BT /F1 18 Tf 30 120 Td (Test PDF preview) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
"""
with open(os.path.join(BASE, "документ.pdf"), "wb") as f:
    f.write(pdf)

for n in sorted(os.listdir(BASE)):
    print(n, os.path.getsize(os.path.join(BASE, n)))
