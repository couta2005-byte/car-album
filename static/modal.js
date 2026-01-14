(function () {
  const modal = document.getElementById("imgModal");
  const modalImg = document.getElementById("modalImg");

  function openModal(src) {
    if (!src) return;
    modalImg.src = src;
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function closeModal() {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    modalImg.src = "";
    document.body.style.overflow = "";
  }

  // 画像クリックで拡大
  document.addEventListener("click", (e) => {
    const img = e.target.closest("img.post-image");
    if (img) {
      openModal(img.dataset.full || img.src);
      return;
    }

    // 閉じる（× or 背景）
    const closer = e.target.closest("[data-close='1']");
    if (closer) {
      closeModal();
      return;
    }
  });

  // Escで閉じる
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal.classList.contains("open")) {
      closeModal();
    }
  });
})();
