// Client-side narrowing for the topic page's keyword picker. The library carries
// far more keywords than papers, so the list is long by design — this only hides
// chips, it never changes what is submitted.
(function () {
  var box = document.getElementById("js-keyword-filter");
  if (!box) return;
  var chips = Array.prototype.slice.call(document.querySelectorAll(".js-keyword-chip"));
  var empty = document.getElementById("js-keyword-empty");

  box.addEventListener("input", function () {
    var query = box.value.trim().toLowerCase();
    var visible = 0;
    chips.forEach(function (chip) {
      var show = !query || chip.getAttribute("data-name").indexOf(query) !== -1;
      chip.hidden = !show;
      if (show) visible++;
    });
    if (empty) empty.hidden = visible !== 0;
  });
})();
