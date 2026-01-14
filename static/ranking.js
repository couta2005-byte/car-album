async function loadRanking() {
  const select = document.getElementById("ranking-period");
  const period = select.value;

  const res = await fetch(`/ranking?period=${period}`);
  const data = await res.json();

  const list = document.getElementById("ranking-list");
  list.innerHTML = "";

  data.forEach((item, index) => {
    const li = document.createElement("li");
    li.className = "ranking-item";
    if (index === 0) li.classList.add("rank-1");

    li.innerHTML = `
      <span class="rank-no">${index + 1}</span>
      <span class="rank-name">${item.username} / ${item.car_name}</span>
      <span class="rank-like">❤️ ${item.likes}</span>
    `;

    list.appendChild(li);
  });
}

document.addEventListener("DOMContentLoaded", loadRanking);
