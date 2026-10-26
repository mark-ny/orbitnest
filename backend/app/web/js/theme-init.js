// Runs before first paint (classic script, external because the CSP forbids inline script).
try {
  const t = localStorage.getItem("orbitnest-theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch { /* storage can be blocked */ }
