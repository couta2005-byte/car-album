document.addEventListener("DOMContentLoaded", async () => {
  console.log("JS起動");

  const makerSelect = document.getElementById("makerSelectAdd");
  if (!makerSelect) return;

  await loadMakers("makerSelectAdd");

  makerSelect.addEventListener("change", async function () {
    await loadCars("carSelectAdd", this.value);
  });
});

async function loadMakers(selectId) {
  const makerSelect = document.getElementById(selectId);
  makerSelect.innerHTML = "<option value=''>メーカー選択</option>";

  const res = await fetch("/api/makers?category=car");
  const makers = await res.json();

  console.log("makers:", makers);

  makers.forEach((m) => {
    const option = document.createElement("option");
    option.value = m.name;
    option.textContent = m.name;
    makerSelect.appendChild(option);
  });
}

async function loadCars(selectId, makerName) {
  const carSelect = document.getElementById(selectId);
  carSelect.innerHTML = "<option value=''>車種選択</option>";

  if (!makerName) return;

  const res = await fetch(`/api/cars/by-name/${encodeURIComponent(makerName)}`);
  const cars = await res.json();

  cars.forEach((c) => {
    const option = document.createElement("option");
    option.value = c.name;
    option.textContent = c.name;
    carSelect.appendChild(option);
  });
}