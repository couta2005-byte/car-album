<script>
let currentCategory = "japan_car";

document.addEventListener("DOMContentLoaded", async () => {
  console.log("JS起動");

  const makerSelect = document.getElementById("makerSelectAdd");
  if (!makerSelect) return;

  await loadMakers(currentCategory);

  makerSelect.addEventListener("change", async function () {
    await loadCars("carSelectAdd", this.value);
  });
});

// 🔥 メーカー切り替え（車 / バイク）
async function loadMakers(category) {
  currentCategory = category;

  const makerSelect = document.getElementById("makerSelectAdd");
  const carSelect = document.getElementById("carSelectAdd");

  makerSelect.innerHTML = "<option value=''>メーカー選択</option>";
  carSelect.innerHTML = "<option value=''>車種選択</option>";

  const res = await fetch(`/api/makers?category=${category}`);
  const makers = await res.json();

  console.log("makers:", makers);

  makers.forEach((m) => {
    const option = document.createElement("option");
    option.value = m.id;   // 🔥 ここ重要
    option.textContent = m.name;
    makerSelect.appendChild(option);
  });
}

// 🔥 車種取得
async function loadCars(selectId, makerId) {
  const carSelect = document.getElementById(selectId);
  carSelect.innerHTML = "<option value=''>車種選択</option>";

  if (!makerId) return;

  const res = await fetch(`/api/cars/by-maker-id/${makerId}`);
  const cars = await res.json();

  cars.forEach((c) => {
    const option = document.createElement("option");
    option.value = c.name;
    option.textContent = c.name;
    carSelect.appendChild(option);
  });
}
</script>