// static/like.js
// 全ページ共通：.js-like を押したら /api/like/{id} でトグルして即反映
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".js-like");
  if (!btn) return;

  // 親の onclick 遷移/フォームsubmit を止める（最重要）
  e.preventDefault();
  e.stopPropagation();

  const postId = btn.dataset.postId;
  if (!postId) return;

  // 連打防止
  if (btn.dataset.loading === "1") return;
  btn.dataset.loading = "1";
  btn.disabled = true;

  try {
    const res = await fetch(`/api/like/${postId}`, {
      method: "POST",
      credentials: "same-origin",
    });

    if (res.status === 401) {
      alert("ログインしてね");
      return;
    }
    if (!res.ok) {
      alert("いいね失敗（APIエラー）");
      return;
    }

    const data = await res.json();
    if (!data.ok) {
      alert(data.error || "いいね失敗");
      return;
    }

    // count
    const countEl = btn.querySelector(".like-count");
    if (countEl) countEl.textContent = String(data.likes);

    // state
    btn.dataset.liked = data.liked ? "1" : "0";
    btn.classList.toggle("active", !!data.liked);
    btn.setAttribute("aria-pressed", data.liked ? "true" : "false");

    // icon
    const iconEl = btn.querySelector(".like-icon");
    if (iconEl) iconEl.textContent = data.liked ? "❤️" : "🤍";

  } catch (err) {
    alert("通信エラー");
  } finally {
    btn.dataset.loading = "0";
    btn.disabled = false;
  }
}, true); // capture=true が大事（記事onclickより先に止める）
