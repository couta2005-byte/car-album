// static/like.js
// 全ページ共通：いいねを /api/like/{post_id} でトグルして即反映
// クリック遷移（記事onclick）より先に止めるため capture=true で拾う

(function () {
  function updateButtons(postId, liked, likes) {
    const buttons = document.querySelectorAll(`.js-like[data-post-id="${postId}"]`);
    buttons.forEach((btn) => {
      btn.dataset.liked = liked ? "1" : "0";
      btn.classList.toggle("active", !!liked);
      btn.setAttribute("aria-pressed", liked ? "true" : "false");

      const iconEl = btn.querySelector(".like-icon");
      if (iconEl) iconEl.textContent = liked ? "❤️" : "🤍";

      const countEl = btn.querySelector(".like-count");
      if (countEl) countEl.textContent = String(likes);
    });
  }

  async function toggleLike(btn) {
    const postId = btn.dataset.postId;
    if (!postId) return;

    if (btn.dataset.loading === "1") return;
    btn.dataset.loading = "1";
    btn.disabled = true;
    btn.classList.add("is-loading");

    try {
      const res = await fetch(`/api/like/${postId}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" }
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

      updateButtons(postId, data.liked, data.likes);
    } catch (e) {
      alert("通信エラー");
    } finally {
      btn.dataset.loading = "0";
      btn.disabled = false;
      btn.classList.remove("is-loading");
    }
  }

  // クリック（最優先で止める）
  document.addEventListener(
    "click",
    (e) => {
      const btn = e.target.closest(".js-like");
      if (!btn) return;

      // 記事カードのonclick遷移を絶対に止める
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation?.();

      toggleLike(btn);
    },
    true // capture
  );

  // 万が一 form が残ってても止める（保険）
  document.addEventListener(
    "submit",
    (e) => {
      const form = e.target.closest(".like-form");
      if (!form) return;

      const btn = form.querySelector(".js-like");
      if (!btn) return;

      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation?.();

      toggleLike(btn);
    },
    true
  );
})();
