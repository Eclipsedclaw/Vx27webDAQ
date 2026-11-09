// Before the first paint: the theme this browser chose (no flash of the other theme).
(function () {
  try {
    // what this browser kept under the old name (ALAB DAQ, until v0.0.18: alab.*) moves to vx27.*, once
    var old = [];                         // listed first: adding keys may reorder localStorage
    for (var i = 0; i < localStorage.length; i++) old.push(localStorage.key(i));
    for (var j = 0; j < old.length; j++) {
      var k = old[j];
      if (k && k.indexOf("alab.") === 0) {
        var n = "vx27." + k.slice(5);
        if (localStorage.getItem(n) === null) localStorage.setItem(n, localStorage.getItem(k));
        localStorage.removeItem(k);
      }
    }
    var t = localStorage.getItem("vx27.theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    var l = localStorage.getItem("vx27.lang");
    if (l === "zh") document.documentElement.setAttribute("lang", "zh-CN");
  } catch (e) { /* storage may be off: the system theme then */ }
})();
