"""Giao diện hỏi đáp cho rag.py (Gradio).

    python app.py          # rồi mở http://127.0.0.1:7860

Cần GEMINI_API_KEY và chỉ mục đã tạo bằng `python rag.py index`.
Trên Hugging Face Spaces, file này là điểm chạy mặc định; key đặt trong Settings → Secrets.
"""
import os
import re
import sys

import gradio as gr

import rag
from crawler import CATEGORIES

EXAMPLES = [
    "Tuần trước độc giả nói gì về giá vàng?",
    "Hôm qua có tin gì đáng chú ý về giao thông?",
    "Độc giả khuyên gì trong các bài Tâm sự gần đây?",
    "Diễn biến giá xăng từ đầu tháng 11",
]


def _scope(plan, filtered):
    """Dòng nhỏ cho người dùng biết hệ thống đã hiểu câu hỏi thế nào."""
    parts = [f"Đã tìm: “{plan.search_query}”"]
    if not filtered:
        parts.append("không có bài trong phạm vi được hỏi, đã tìm trên toàn bộ kho")
    else:
        if plan.date_from or plan.date_to:
            fmt = lambda d: f"{d[8:]}/{d[5:7]}" if d else "…"
            parts.append(f"{fmt(plan.date_from)} – {fmt(plan.date_to)}")
        parts += [CATEGORIES.get(c, c) for c in plan.categories]
    if plan.wants_opinion:
        parts.append("ưu tiên bình luận độc giả")
    return " · ".join(parts)


def answer(question):
    question = (question or "").strip()
    if not question:
        return "", ""
    try:
        r = rag.ask(question)
    except RuntimeError as e:
        return f"**{e}**", ""
    urls = {s["n"]: s["url"] for s in r["sources"]}
    # [2] trong câu trả lời -> liên kết tới bài gốc
    text = re.sub(r"\[(\d+)\]", lambda m: f"[[{m[1]}]]({urls[int(m[1])]})" if int(m[1]) in urls else m[0],
                  r["answer"])
    sources = "\n".join(f"{s['n']}. [{s['title']}]({s['url']}) · {s['publish'][8:10]}/{s['publish'][5:7]}"
                        for s in r["sources"])
    return f"*{_scope(r['plan'], r['filtered'])}*\n\n{text}", (f"**Nguồn**\n\n{sources}" if sources else "")


with gr.Blocks(title="Sáng Nay · Hỏi đáp") as demo:
    n_docs = f"{rag.get_collection().count():,}".replace(",", ".")      # 7.856 kiểu Việt
    gr.Markdown("# Sáng Nay · Hỏi đáp tin tức\n"
                f"Hỏi về các bài VnExpress đã thu thập ({n_docs} đoạn văn và bình luận). "
                "Câu trả lời chỉ dựa trên các bài tìm được, kèm số nguồn để bạn đọc bài gốc.")
    question = gr.Textbox(label="Câu hỏi", placeholder="Ví dụ: Tuần trước độc giả nói gì về giá vàng?",
                          lines=1, autofocus=True)
    ask_btn = gr.Button("Hỏi", variant="primary")
    gr.Examples(EXAMPLES, inputs=question, label="Câu hỏi mẫu")
    out = gr.Markdown()
    src = gr.Markdown()

    gr.on([ask_btn.click, question.submit], answer, inputs=question, outputs=[out, src], api_name="answer")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Thiếu biến môi trường GEMINI_API_KEY")
    if rag.get_collection().count() == 0:
        sys.exit(rag.EMPTY_INDEX)
    demo.launch()
