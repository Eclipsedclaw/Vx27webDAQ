// Before the first paint: the theme this browser chose (no flash of the other theme).
(function () {
  try {
    var t = localStorage.getItem("alab.theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    var l = localStorage.getItem("alab.lang");
    if (l === "zh") document.documentElement.setAttribute("lang", "zh-CN");
  } catch (e) { /* storage may be off: the system theme then */ }
})();
