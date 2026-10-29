// Lọc thẻ tin theo chuyên mục ngay trên trang, không tải lại.
// Chuyên mục đang chọn ghi vào địa chỉ (#c=thoi-su) để gửi link cho người khác.
(function () {
  var buttons = document.querySelectorAll(".filters button");
  if (!buttons.length) return;
  var cards = document.querySelectorAll("#tin-chinh .card");   // khung Tâm sự luôn hiện
  var emptyNote = document.querySelector(".empty-filter");

  function apply(slug) {
    var shown = 0;
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.filter === slug));
    });
    cards.forEach(function (c) {
      var match = !slug || c.dataset.category === slug;
      c.hidden = !match;
      if (match) shown++;
    });
    if (emptyNote) emptyNote.hidden = shown > 0;
  }

  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      apply(b.dataset.filter);
      history.replaceState(null, "", b.dataset.filter ? "#c=" + b.dataset.filter : location.pathname);
    });
  });

  var m = location.hash.match(/^#c=([\w-]+)$/);
  if (m && document.querySelector('.filters button[data-filter="' + m[1] + '"]')) apply(m[1]);
})();
