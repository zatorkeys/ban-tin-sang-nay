"""Sinh trang web tĩnh từ digests/*.json (Jinja2) vào thư mục site/.

    python build_site.py            # rồi mở site/index.html

Các trang:
    index.html          bản tin mới nhất
    YYYY-MM-DD.html     bản tin từng ngày
    luu-tru.html        danh sách các ngày
    gioi-thieu.html     cách chọn tin, cách AI tóm tắt, hạn chế
    digests/*.json      dữ liệu gốc, để sau này làm API / RAG
"""
import glob
import json
import math
import os
import shutil
import sys
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from crawler import CATEGORIES

SITE_NAME = "Sáng Nay"
TAGLINE = "15 tin độc giả quan tâm nhất trong 24 giờ qua, kèm dư luận, đọc trong 5 phút"
DIGEST_DIR = "digests"
OUT_DIR = "site"
WORDS_PER_MINUTE = 200
WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


def date_label(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{WEEKDAYS[d.weekday()]}, {d:%d/%m/%Y}"


def fmt_number(x):
    """7.0 -> '7', 7.25 -> '7,3' (dấu phẩy thập phân kiểu Việt)."""
    return f"{x:.0f}" if x >= 10 or x == int(x) else f"{x:.1f}".replace(".", ",")


def fmt_time(dt_str):
    """'2026-10-29 21:30' -> '21:30 29/10'."""
    d = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return f"{d:%H:%M %d/%m}"


def prepare(digest):
    """Thêm các trường chỉ dùng để hiển thị."""
    texts = [digest.get("overview") or ""]
    counts = {}
    for it in digest["items"]:
        it["category_name"] = CATEGORIES.get(it.get("category"), it.get("category") or "Khác")
        texts += [it["title"], it.get("tom_tat") or it.get("description") or "", it.get("du_luan") or ""]
        counts[it.get("category")] = counts.get(it.get("category"), 0) + 1

    words = sum(len(t.split()) for t in texts)
    digest["read_minutes"] = max(1, math.ceil(words / WORDS_PER_MINUTE))
    digest["date_label"] = date_label(digest["date"])
    digest["updated"] = digest["generated_at"][-5:].lstrip("0")
    digest["page"] = f"{digest['date']}.html"
    # nút lọc: theo thứ tự chuyên mục quen thuộc, chỉ những chuyên mục có tin
    order = list(CATEGORIES)
    digest["filters"] = sorted(
        ({"slug": c, "name": CATEGORIES.get(c, c or "Khác"), "count": n} for c, n in counts.items()),
        key=lambda f: order.index(f["slug"]) if f["slug"] in order else len(order),
    )
    return digest


def load_digests():
    digests = []
    for path in sorted(glob.glob(os.path.join(DIGEST_DIR, "*.json")), reverse=True):
        with open(path, encoding="utf-8") as f:
            digests.append(prepare(json.load(f)))
    return digests      # mới nhất trước


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    env = Environment(loader=FileSystemLoader("templates"), autoescape=select_autoescape())
    env.filters["num"] = fmt_number
    env.filters["time"] = fmt_time
    env.globals.update(site_name=SITE_NAME, tagline=TAGLINE,
                       built_at=datetime.now().strftime("%Y-%m-%d %H:%M"))

    digests = load_digests()

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    shutil.copytree("static", os.path.join(OUT_DIR, "static"))
    if os.path.isdir(DIGEST_DIR):
        shutil.copytree(DIGEST_DIR, os.path.join(OUT_DIR, "digests"))
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()   # GitHub Pages phục vụ nguyên file

    def render(template, filename, **ctx):
        html = env.get_template(template).render(**ctx)
        with open(os.path.join(OUT_DIR, filename), "w", encoding="utf-8") as f:
            f.write(html)

    for i, d in enumerate(digests):
        newer = digests[i - 1] if i > 0 else None
        older = digests[i + 1] if i + 1 < len(digests) else None
        render("day.html", d["page"], digest=d, newer=newer, older=older, active="")
    render("day.html", "index.html", digest=digests[0] if digests else None,
           older=digests[1] if len(digests) > 1 else None, newer=None, active="home")
    render("archive.html", "luu-tru.html", digests=digests, active="archive")
    render("about.html", "gioi-thieu.html", categories=CATEGORIES,
           model=digests[0]["model"] if digests else None, active="about")

    print(f"Đã sinh {len(digests)} bản tin vào {OUT_DIR}/")


if __name__ == "__main__":
    main()
