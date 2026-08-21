// Safety guard loaded after app.js. Any data edit invalidates a previous
// verification. Choosing a status explicitly remains a separate user action.
const resultBody = document.querySelector("#result-body");

resultBody?.addEventListener("input", (event) => {
  const input = event.target.closest?.(".cell-input");
  if (!input) return;
  const row = rowById(input.dataset.id);
  if (row) row.status = "edited";
}, {capture: true});

resultBody?.addEventListener("change", (event) => {
  const select = event.target.closest?.(".cell-select");
  if (!select || select.dataset.key === "status") return;
  const row = rowById(select.dataset.id);
  if (row) row.status = "edited";
}, {capture: true});
