export function attach(input, output) {
  const update = () => { output.textContent = `${input.value.length} / 1000`; };
  input.addEventListener("input", update);
  update();
  return () => input.removeEventListener("input", update);
}
