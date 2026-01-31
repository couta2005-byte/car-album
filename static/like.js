(() => {
  "use strict";

  async function postJson(url) {
    const res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Accept": "application/json" },
    });
    let data = null;
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, data };
  }

  function setLikeBtnState(btn, liked, likes) {
    btn.classList.toggle("active", !!liked);
    btn.dataset.liked = liked ? "1" : "0";
    btn.setAttribute("aria-pressed", liked ? "true" : "false");

    const icon = btn.querySelector(".like-icon");
    const count = btn.querySelector(".like-count");
    if (icon) icon.textContent = liked ? "❤️" : "🤍";
    if (count && typeof likes !== "undefined") count.textContent = String(likes);
  }

  async function handlePostLike(btn) {
    if (btn.classList.contains("is-loading")) return;
    const postId = btn.dataset.postId;
    if (!postId) return;

    btn.classList.add("is-loading");
    try {
      const r = await postJson(`/api/like/${postId}`);
      if (!r.ok || !r.data || r.data.ok !== true) {
        if (r.data && r.data.error === "login_required") {
          location.href = "/login";
          return;
        }
        return;
      }
      setLikeBtnState(btn, r.data.liked, r.data.likes);
    } finally {
      btn.classList.remove("is-loading");
    }
  }

  async function handleCommentLike(btn) {
    if (btn.classList.contains("is-loading")) return;
    const commentId = btn.dataset.commentId;
    if (!commentId) return;

    btn.classList.add("is-loading");
    try {
      const r = await postJson(`/api/comment_like/${commentId}`);
      if (!r.ok || !r.data || r.data.ok !== true) {
        if (r.data && r.data.error === "login_required") {
          location.href = "/login";
          return;
        }
        return;
      }
      setLikeBtnState(btn, r.data.liked, r.data.likes);
    } finally {
      btn.classList.remove("is-loading");
    }
  }

  document.addEventListener("click", (e) => {
    const postBtn = e.target.closest(".js-like");
    if (postBtn) {
      e.preventDefault();
      e.stopPropagation();
      handlePostLike(postBtn);
      return;
    }

    const commentBtn = e.target.closest(".js-comment-like");
    if (commentBtn) {
      e.preventDefault();
      e.stopPropagation();
      handleCommentLike(commentBtn);
      return;
    }
  });
})();
