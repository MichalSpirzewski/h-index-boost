// Multi-select export toolbar. Downloads work without this file (the buttons are
// plain submits with formaction); this only adds the counter, select-all and copy.
(function () {
  function wire(form) {
    var rows = form.querySelectorAll(".js-row-check");
    if (!rows.length) return;
    var selectAll = form.querySelector(".js-select-all");
    var counter = form.querySelector(".js-selected-count");
    var buttons = form.querySelectorAll(".js-export-btn");
    var copyBtn = form.querySelector(".js-copy-bibtex");

    function checked() {
      return Array.prototype.filter.call(rows, function (r) { return r.checked; });
    }

    function sync() {
      var n = checked().length;
      if (counter) {
        counter.textContent = n === 0 ? "none selected" : n + " selected";
      }
      Array.prototype.forEach.call(buttons, function (b) { b.disabled = n === 0; });
      if (selectAll) {
        selectAll.checked = n === rows.length;
        selectAll.indeterminate = n > 0 && n < rows.length;
      }
    }

    Array.prototype.forEach.call(rows, function (row) {
      row.addEventListener("change", sync);
    });

    if (selectAll) {
      selectAll.addEventListener("change", function () {
        var on = selectAll.checked;
        Array.prototype.forEach.call(rows, function (r) { r.checked = on; });
        sync();
      });
    }

    if (copyBtn) {
      var defaultLabel = copyBtn.textContent;
      var flash = function (label) {
        copyBtn.textContent = label;
        setTimeout(function () { copyBtn.textContent = defaultLabel; }, 1500);
      };

      var copyText = function (text) {
        if (navigator.clipboard && window.isSecureContext) {
          return navigator.clipboard.writeText(text);
        }
        // http:// on the LAN has no clipboard API, so fall back to a hidden textarea.
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try {
          document.execCommand("copy");
          return Promise.resolve();
        } finally {
          document.body.removeChild(ta);
        }
      };

      copyBtn.addEventListener("click", function () {
        fetch("/export/bibtex", { method: "POST", body: new FormData(form) })
          .then(function (r) {
            if (!r.ok) throw new Error(r.status);
            return r.text();
          })
          .then(copyText)
          .then(function () { flash("Copied!"); })
          .catch(function () { flash("Copy failed"); });
      });
    }

    sync();
  }

  document.querySelectorAll("form.js-export-form").forEach(wire);
})();
