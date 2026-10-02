/* Registrazione del service worker di DomusChat. */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch((e) => {
      console.warn("[pwa] SW non registrato:", e.message);
    });
  });
}