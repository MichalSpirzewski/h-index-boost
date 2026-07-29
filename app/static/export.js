// Multi-select export toolbar. Downloads and share creation work without this
// file via normal form submits; JavaScript adds counters, clipboard actions, and
// an in-place result when a share link is created.
(function () {
  function wire(form) {
    var rows = form.querySelectorAll(".js-row-check");
    if (!rows.length) return;
    var selectAll = form.querySelector(".js-select-all");
    var counter = form.querySelector(".js-selected-count");
    var buttons = form.querySelectorAll(".js-export-btn");
    var copyBtn = form.querySelector(".js-copy-bibtex");
    var createShareBtn = form.querySelector(".js-create-share");
    var shareResult = form.querySelector(".js-share-result");
    var shareUrl = form.querySelector(".js-share-url");
    var openShare = form.querySelector(".js-open-share");
    var copyShare = form.querySelector(".js-copy-share");
    var shareStatus = form.querySelector(".js-share-status");
    var sharing = false;

    function checked() {
      return Array.prototype.filter.call(rows, function (r) { return r.checked; });
    }

    function copyText(text) {
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
        return document.execCommand("copy")
          ? Promise.resolve()
          : Promise.reject(new Error("copy failed"));
      } catch (error) {
        return Promise.reject(error);
      } finally {
        document.body.removeChild(ta);
      }
    }

    function sync() {
      var n = checked().length;
      if (counter) {
        counter.textContent = n === 0 ? "none selected" : n + " selected";
      }
      Array.prototype.forEach.call(buttons, function (b) {
        b.disabled = n === 0 || (b === createShareBtn && sharing);
      });
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

    if (createShareBtn && shareResult && shareUrl && openShare) {
      var defaultShareLabel = createShareBtn.textContent;
      createShareBtn.addEventListener("click", function (event) {
        event.preventDefault();
        if (!checked().length || sharing) return;
        sharing = true;
        createShareBtn.textContent = "Creating…";
        if (shareStatus) shareStatus.textContent = "";
        sync();

        fetch(createShareBtn.formAction, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: new FormData(form)
        })
          .then(function (response) {
            if (!response.ok) throw new Error(response.status);
            return response.json();
          })
          .then(function (result) {
            shareUrl.value = result.url;
            openShare.href = result.url;
            shareResult.hidden = false;
            shareUrl.focus();
            shareUrl.select();
            if (shareStatus) shareStatus.textContent = "Link ready.";
          })
          .catch(function () {
            shareResult.hidden = false;
            if (shareStatus) shareStatus.textContent = "Could not create link.";
          })
          .then(function () {
            sharing = false;
            createShareBtn.textContent = defaultShareLabel;
            sync();
          });
      });
    }

    if (copyShare && shareUrl) {
      copyShare.addEventListener("click", function () {
        if (!shareUrl.value) return;
        copyText(shareUrl.value)
          .then(function () {
            if (shareStatus) shareStatus.textContent = "Link copied.";
          })
          .catch(function () {
            shareUrl.focus();
            shareUrl.select();
            if (shareStatus) shareStatus.textContent = "Copy failed — select the link.";
          });
      });
    }

    sync();
  }

  document.querySelectorAll("form.js-export-form").forEach(wire);
})();
