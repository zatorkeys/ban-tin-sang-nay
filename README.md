# Sáng Nay: bản tin buổi sáng do AI đọc báo thay bạn

Mỗi tối khoảng 20:20, trang web tự chọn 15 tin VnExpress được độc giả bình luận sôi nổi nhất
trong 24 giờ qua, tóm tắt từng tin và cho biết dư luận đang nghĩ gì.

```
20:00 mỗi tối (GitHub Actions)
  → crawler.py      RSS + trang bài + API bình luận, chỉ lấy bài trong 24 giờ
  → run_daily.py    lưu dữ liệu thô vào data/articles/YYYY-MM-DD.jsonl.gz (kho cho RAG sau này)
  → ai_summary.py   chọn 15 tin + 2 bài Tâm sự theo bình luận/giờ, Gemini tóm tắt tin + dư luận + tổng quan
                    → digests/YYYY-MM-DD.json
  → build_site.py   Jinja2 sinh HTML tĩnh vào site/
  → GitHub Pages
```

## Cấu trúc

| File | Việc |
|---|---|
| `crawler.py` | Thu thập bài 24 giờ qua của 13 chuyên mục (danh sách trong `CATEGORIES`). Đọc RSS trước (có sẵn giờ đăng, khỏi tải bài cũ); chuyên mục nào RSS không phủ đủ 24 giờ thì xem thêm trang danh sách. |
| `ai_summary.py` | Chọn tin, gọi Gemini, ghi `digests/<ngày>.json`. Tâm sự (`TAM_SU`, `TAM_SU_N`) tách thành khung riêng, không tính vào 15 tin. |
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

## Hỏi đáp trên kho bài (RAG)

`rag.py` cho phép hỏi bằng tiếng Việt trên toàn bộ bài đã crawl, kể cả bình luận:

```bash
pip install -r requirements-rag.txt        # thêm ChromaDB, không cần cho bản tin hằng ngày
python rag.py index                        # lần đầu lâu (vài nghìn đoạn), các lần sau chỉ làm phần mới
python rag.py ask "Tuần trước độc giả nói gì về giá vàng?"
python app.py                              # giao diện web hỏi đáp: http://127.0.0.1:7860
```

- Mỗi bài được cắt thành vài đoạn nội dung (có tiêu đề ở đầu mỗi đoạn) cùng một document gom 20 bình luận
  nhiều thích nhất. Embedding bằng Gemini (`gemini-embedding-001`, 768 chiều) rồi lưu vào ChromaDB ở `rag_index/`.
- **Self-query**: trước khi tìm, Gemini đọc câu hỏi và trả về câu truy vấn cùng bộ lọc ngày / chuyên mục
  ("tuần trước", "bóng đá"...). Vì vậy câu hỏi có mốc thời gian không bị lẫn bài cũ chỉ vì giống chữ.
- Nội dung bài và bình luận được tìm riêng, nên câu trả lời tách được sự việc với ý kiến độc giả, có số nguồn [1], [2].

## Đưa lên GitHub

1. Tạo repo mới trên GitHub, đẩy thư mục này lên nhánh `main`.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   `GEMINI_API_KEY` = key tạo ở Google AI Studio.
3. **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Actions → Bản tin hằng ngày → Run workflow** để chạy lần đầu. Từ đó sẽ tự chạy lúc 20:00 mỗi tối.

Trang web sẽ ở `https://<tên-tài-khoản>.github.io/<tên-repo>/`.

Nút **Run workflow** có 2 tùy chọn: chỉ chạy lại AI trên dữ liệu đã crawl, hoặc chỉ dựng lại web.
Sửa `templates/`, `static/` rồi push thì web tự dựng lại, không crawl.

## Ghi chú

- **Giờ chạy**: lịch GitHub thường trễ 5-20 phút, có hôm hơn. Workflow đặt 20:00, bản tin lên web
  sau đó khoảng 20-30 phút. Đổi giờ: sửa dòng `cron` trong `.github/workflows/daily.yml` (giờ UTC = giờ VN - 7).
- **Dung lượng**: dữ liệu thô khoảng vài trăm KB/ngày (nén gzip), commit thẳng vào repo dùng được
  vài năm. Khi lớn dần có thể chuyển sang Hugging Face Datasets hoặc Releases.
- **Lỗi**: nếu crawl không được bài nào hoặc thiếu API key, workflow báo lỗi (GitHub gửi email)
  và trang web giữ nguyên bản tin cũ. Bài AI không tóm tắt được vẫn hiển thị, dùng mô tả gốc.
- **Không bao giờ** dán API key vào code hay notebook; luôn đọc từ biến môi trường / Secrets.
