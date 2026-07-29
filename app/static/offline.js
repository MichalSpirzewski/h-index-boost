// Behaviour for the exported offline page (see app/site_export.py).
//
// The page is opened straight off the filesystem, so there is no server to sort,
// filter or build downloads: everything here works on the rows already in the DOM
// without requiring a server or any supporting files.
(function () {
  var tables = Array.prototype.slice.call(document.querySelectorAll(".js-offline-table"));
  var filterBar = document.getElementById("js-filter-bar");
  var filterLabel = document.getElementById("js-filter-label");
  // One filter at a time (author / topic / journal), plus the free-text box.
  var state = { text: "", type: null, value: null };

  // --- helpers ---------------------------------------------------------------

  function rowPairs(container) {
    // Each article row is followed by its (hidden) detail row; they move together.
    return Array.prototype.map.call(
      container.querySelectorAll("tr.article-row"),
      function (row) { return { row: row, detail: row.nextElementSibling }; }
    );
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    var area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.focus();
    area.select();
    try {
      return document.execCommand("copy")
        ? Promise.resolve()
        : Promise.reject(new Error("copy failed"));
    } catch (error) {
      return Promise.reject(error);
    } finally {
      document.body.removeChild(area);
    }
  }

  // --- filtering -------------------------------------------------------------

  function matches(row) {
    if (state.text && row.getAttribute("data-search").indexOf(state.text) === -1) {
      return false;
    }
    if (state.type === "author") {
      return row.getAttribute("data-authors").indexOf(" " + state.value + " ") !== -1;
    }
    if (state.type === "topic") {
      return row.getAttribute("data-topics").indexOf(" " + state.value + " ") !== -1;
    }
    if (state.type === "journal") {
      return row.getAttribute("data-journal") === state.value;
    }
    return true;
  }

  function applyFilter() {
    tables.forEach(function (container) {
      var visible = 0;
      rowPairs(container).forEach(function (pair) {
        var show = matches(pair.row);
        pair.row.hidden = !show;
        if (!show) {
          pair.detail.hidden = true;
          // Hosted share pages add the normal selection toolbar. A filtered-out
          // paper should not remain invisibly selected for a later download.
          var check = pair.row.querySelector(".js-row-check");
          if (check && check.checked) {
            check.checked = false;
            check.dispatchEvent(new Event("change", { bubbles: true }));
          }
        } else {
          visible++;
        }
      });
      var empty = container.querySelector(".no-matches");
      if (empty) empty.hidden = visible !== 0;
      var section = container.closest("section");
      var badge = section && section.querySelector(".js-section-count");
      if (badge) badge.textContent = visible;
    });
  }

  function setFilter(type, value, label) {
    if (state.type === type && state.value === value) {  // clicking it again clears it
      type = null;
      value = null;
    }
    state.type = type;
    state.value = value;
    if (filterBar) {
      filterBar.hidden = !type;
      if (type) filterLabel.textContent = "Showing only: " + label;
    }
    applyFilter();
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target.closest) return;

    var author = target.closest(".js-author-filter");
    if (author) {
      event.preventDefault();
      setFilter("author", author.getAttribute("data-author"), author.textContent.trim());
      return;
    }
    var topic = target.closest(".js-topic-filter");
    if (topic) {
      event.preventDefault();
      var name = topic.firstChild ? topic.firstChild.textContent.trim() : "";
      setFilter("topic", topic.getAttribute("data-topic"), name);
      return;
    }
    var journal = target.closest(".js-journal-filter");
    if (journal) {
      event.preventDefault();
      var title = journal.getAttribute("data-journal");
      setFilter("journal", title, title);
      return;
    }
    if (target.closest("#js-filter-clear")) {
      event.preventDefault();
      setFilter(null, null, "");
      return;
    }
    var toggle = target.closest(".js-detail-toggle");
    if (toggle) {
      event.preventDefault();
      var detail = toggle.closest("tr").nextElementSibling;
      detail.hidden = !detail.hidden;
    }
  });

  var search = document.getElementById("js-search");
  if (search) {
    search.addEventListener("input", function () {
      state.text = search.value.trim().toLowerCase();
      applyFilter();
    });
  }

  // --- sorting ---------------------------------------------------------------

  // Same defaults as the server-rendered dashboard: dates newest-first, text A→Z.
  function defaultOrder(key) {
    return key === "published" || key === "online" ? "desc" : "asc";
  }

  function sortTable(table, key) {
    var order = table.getAttribute("data-sort") === key
      ? (table.getAttribute("data-order") === "asc" ? "desc" : "asc")
      : defaultOrder(key);
    var direction = order === "asc" ? 1 : -1;
    var body = table.tBodies[0];
    var pairs = Array.prototype.map.call(
      body.querySelectorAll("tr.article-row"),
      function (row) { return { row: row, detail: row.nextElementSibling }; }
    );
    // Array.prototype.sort is stable, so equal keys keep the order they came in
    // with — which is the date-added tiebreak the server applied.
    pairs.sort(function (a, b) {
      var left = a.row.getAttribute("data-sort-" + key) || "";
      var right = b.row.getAttribute("data-sort-" + key) || "";
      if (left === right) return 0;
      return (left < right ? -1 : 1) * direction;
    });
    pairs.forEach(function (pair) {
      body.appendChild(pair.row);
      body.appendChild(pair.detail);
    });
    table.setAttribute("data-sort", key);
    table.setAttribute("data-order", order);
    Array.prototype.forEach.call(table.querySelectorAll("th.sortable"), function (th) {
      var active = th.getAttribute("data-sort-key") === key;
      th.classList.toggle("active", active);
      th.querySelector(".arrow").textContent = active ? (order === "asc" ? "▲" : "▼") : "";
    });
  }

  document.querySelectorAll("th.sortable a").forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault();
      var th = link.closest("th");
      sortTable(th.closest("table"), th.getAttribute("data-sort-key"));
    });
  });

  // --- hosted share link -----------------------------------------------------

  var shareInput = document.getElementById("js-share-url");
  var shareButton = document.getElementById("js-copy-share");
  var shareStatus = document.getElementById("js-share-status");
  if (shareInput && shareButton) {
    shareButton.addEventListener("click", function () {
      copyText(shareInput.value)
        .then(function () {
          if (shareStatus) shareStatus.textContent = "Link copied.";
        })
        .catch(function () {
          shareInput.focus();
          shareInput.select();
          if (shareStatus) shareStatus.textContent = "Copy failed — select the link above.";
        });
    });
  }

})();
