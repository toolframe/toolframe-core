export function mountNotes() {
const instances = new WeakMap();
async function initialize(root = document) {
  for (const input of root.querySelectorAll("[data-note-input]")) {
    if (instances.has(input)) continue;
    const output = input.parentElement.querySelector("[data-count]");
    if (!output) continue;
    instances.set(input, null);
    const { attach } = await import("../model/count.js");
    if (input.isConnected) instances.set(input, attach(input, output));
  }
}
document.addEventListener("htmx:beforeCleanupElement", event => {
  const root = event.detail.elt;
  for (const input of [root, ...root.querySelectorAll("[data-note-input]")]) {
    instances.get(input)?.();
    instances.delete(input);
  }
});
document.addEventListener("htmx:afterSwap", event => initialize(event.detail.elt));
document.addEventListener("htmx:beforeSwap", event => {
  const xhr = event.detail.xhr;
  const revision = xhr.getResponseHeader("X-Web-Revision");
  const current = document.querySelector('meta[name="tf-revision"]')?.content;
  if (revision && current && revision !== current) {
    event.detail.shouldSwap = false;
    document.querySelector("#notice").textContent = "This page changed. Reload before continuing.";
    return;
  }
  if (xhr.status === 422 || xhr.status === 409) {
    if (xhr.getResponseHeader("Content-Type")?.includes("text/html")) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    } else {
      document.querySelector("#notice").textContent = "The page changed. Reload and check your notes before retrying.";
    }
  }
  if (xhr.status === 401) {
    event.detail.shouldSwap = false;
    const link = document.createElement("a");
    link.href = "/_toolframe/auth/login?return_to=" + encodeURIComponent(location.pathname + location.search);
    link.textContent = "Your session ended. Sign in to continue.";
    document.querySelector("#notice").replaceChildren(link);
  }
});
initialize();
}
