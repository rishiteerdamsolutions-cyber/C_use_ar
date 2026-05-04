// Compatibility: some pages expect site.js
// Load the original script bundle.
try {
  var s = document.createElement("script");
  s.src = "./script.js";
  s.defer = true;
  document.head.appendChild(s);
} catch (_) {}

