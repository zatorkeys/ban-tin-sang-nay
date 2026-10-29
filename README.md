# Sáng Nay: bản tin buổi sáng do AI đọc báo thay bạn

Mỗi sáng trước 7h, trang web tự chọn 15 tin VnExpress được độc giả bình luận sôi nổi nhất
trong 24 giờ qua, tóm tắt từng tin và cho biết dư luận đang nghĩ gì.

```
6:15 mỗi sáng (GitHub Actions)
  → crawler.py      RSS + trang bài + API bình luận, chỉ lấy bài trong 24 giờ
  → run_daily.py    lưu dữ liệu thô vào data/articles/YYYY-MM-DD.jsonl.gz (kho cho RAG sau này)
  → ai_summary.py   chọn 15 tin theo bình luận/giờ, Gemini tóm tắt tin + dư luận + tổng quan
                    → digests/YYYY-MM-DD.json
  → build_site.py   Jinja2 sinh HTML tĩnh vào site/
  → GitHub Pages
```

## Cấu trúc

| File | Việc |
|---|---|
| `crawler.py` | Thu thập bài 24 giờ qua của 11 chuyên mục. Đọc RSS trước (có sẵn giờ đăng, khỏi tải bài cũ); chuyên mục nào RSS không phủ đủ 24 giờ thì xem thêm trang danh sách. |
| `ai_summary.py` | Chọn tin, gọi Gemini, ghi `digests/<ngày>.json`. |
| `run_daily.py` | Chạy cả chuỗi, lưu dữ liệu thô. |
| `build_site.py` + `templates/` + `static/` | Trang chủ, từng ngày, lưu trữ, giới thiệu. |
| `.github/workflows/daily.yml` | Lịch chạy hằng ngày + đăng lên GitHub Pages. |

## Chạy trên máy

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

set GEMINI_API_KEY=...             # PowerShell: $env:GEMINI_API_KEY="..."
python run_daily.py                # crawl (~15-25 phút) + AI
python build_site.py               # mở site/index.html
```

Hữu ích khi chỉnh sửa:

```bash
python crawler.py --categories suc-khoe --out-dir .     # crawl thử 1 chuyên mục ra CSV
python run_daily.py --from-data                          # chạy lại AI trên dữ liệu đã crawl hôm nay
python ai_summary.py articles.csv comments.csv --now "2026-10-29 07:00"
```

Đổi model mà không sửa code: đặt biến môi trường `GEMINI_MODEL`.

## Đưa lên GitHub

1. Tạo repo mới trên GitHub, đẩy thư mục này lên nhánh `main`.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   `GEMINI_API_KEY` = key tạo ở Google AI Studio.
3. **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Actions → Bản tin hằng ngày → Run workflow** để chạy lần đầu. Từ hôm sau sẽ tự chạy lúc 6:15.

Trang web sẽ ở `https://<tên-tài-khoản>.github.io/<tên-repo>/`.

Nút **Run workflow** có 2 tùy chọn: chỉ chạy lại AI trên dữ liệu đã crawl, hoặc chỉ dựng lại web.
Sửa `templates/`, `static/` rồi push thì web tự dựng lại, không crawl.

## Ghi chú

- **Giờ chạy**: lịch GitHub thường trễ 5-20 phút, có hôm hơn. Workflow đặt 6:15 để kịp 7:00.
- **Dung lượng**: dữ liệu thô khoảng vài trăm KB/ngày (nén gzip), commit thẳng vào repo dùng được
  vài năm. Khi lớn dần có thể chuyển sang Hugging Face Datasets hoặc Releases.
- **Lỗi**: nếu crawl không được bài nào hoặc thiếu API key, workflow báo lỗi (GitHub gửi email)
  và trang web giữ nguyên bản tin cũ. Bài AI không tóm tắt được vẫn hiển thị, dùng mô tả gốc.
- **Không bao giờ** dán API key vào code hay notebook; luôn đọc từ biến môi trường / Secrets.
