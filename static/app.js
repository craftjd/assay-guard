(function () {
  "use strict";

  var FLASH_KEYS = [
    "already", "loaded", "rows", "batches", "blanks", "stopped", "done", "total",
    "deleted", "saved", "reprocessed", "replaced", "set_aside", "absent", "error", "config_loaded",
    "note", "swapped", "into", "voided", "removed", "same_run", "new_run", "changes", "kept", "tested", "affected"
  ];

  function each(selector, fn, root) {
    Array.prototype.forEach.call((root || document).querySelectorAll(selector), fn);
  }

  function interactive(target) {
    return target.closest("a, button, input, select, textarea, summary, label, form");
  }

  // Flash: dismiss, and drop the message keys so a refresh does not repeat it.
  function initFlash() {
    var url = new URL(window.location.href);
    var changed = false;
    FLASH_KEYS.forEach(function (key) {
      if (url.searchParams.has(key)) {
        url.searchParams.delete(key);
        changed = true;
      }
    });
    if (changed) window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    each("[data-dismiss]", function (button) {
      button.addEventListener("click", function () {
        var box = button.closest(".flash");
        if (box) box.remove();
      });
    });
  }

  // Two-step confirm for writes that archive or delete. First click arms.
  function initArm() {
    var armed = null;

    function disarm() {
      if (!armed) return;
      window.clearTimeout(armed.timer);
      armed.button.classList.remove("armed");
      armed.button.textContent = armed.text;
      armed = null;
    }

    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-arm]");
      if (armed && armed.button === button) return;
      disarm();
      if (!button) return;
      event.preventDefault();
      armed = { button: button, text: button.textContent, timer: window.setTimeout(disarm, 4000) };
      button.classList.add("armed");
      button.textContent = button.getAttribute("data-arm");
    }, true);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") disarm();
    });
  }

  // One submit per form. A second click cannot post the same write twice.
  function initSubmitLock() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (form.getAttribute("data-busy") === "1") {
        event.preventDefault();
        return;
      }
      form.setAttribute("data-busy", "1");
      var button = event.submitter || form.querySelector("[type=submit]");
      if (!button) return;
      window.requestAnimationFrame(function () {
        button.classList.add("busy");
        var tile = button.closest(".tile");
        if (tile) tile.classList.add("busy");
        var label = button.getAttribute("data-busy-label");
        if (label) button.textContent = label;
      });
    });
    window.addEventListener("pageshow", function () {
      each("form[data-busy]", function (form) { form.removeAttribute("data-busy"); });
      each(".busy", function (node) { node.classList.remove("busy"); });
    });
  }

  // Rows with data-href open on click or Enter, and arrow keys move between them.
  function initRowLinks() {
    each("tr[data-href]", function (row) {
      row.tabIndex = 0;
      row.addEventListener("click", function (event) {
        if (interactive(event.target)) return;
        if (event.metaKey || event.ctrlKey) {
          window.open(row.getAttribute("data-href"), "_blank");
          return;
        }
        window.location.href = row.getAttribute("data-href");
      });
      row.addEventListener("keydown", function (event) {
        if (event.target !== row) return;
        if (event.key === "Enter") {
          window.location.href = row.getAttribute("data-href");
        } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          var rows = Array.prototype.slice.call(document.querySelectorAll("tr[data-href]"));
          var next = rows[rows.indexOf(row) + (event.key === "ArrowDown" ? 1 : -1)];
          if (next) next.focus();
        }
      });
    });
  }

  // Preview: classify again in memory and show the result. Stores nothing.
  function initDry() {
    document.addEventListener("click", function (event) {
      var close = event.target.closest("[data-dry-close]");
      var toggle = event.target.closest("[data-dry-panel]");
      if (close || toggle) {
        var panel = document.getElementById("dry-panel");
        var opener = document.querySelector("[data-dry-panel]");
        if (close || !panel.hidden) {
          panel.hidden = true;
          panel.innerHTML = "";
          opener.setAttribute("aria-expanded", "false");
          opener.textContent = "Preview";
          return;
        }
        opener.classList.add("busy");
        fetch("/process/import/" + opener.getAttribute("data-dry-panel") + "/dry?view=panel")
          .then(function (response) { return response.text(); })
          .then(function (html) {
            panel.innerHTML = html;
            panel.hidden = false;
            initPayloads(panel);
            opener.setAttribute("aria-expanded", "true");
            opener.textContent = "Hide preview";
          })
          .finally(function () { opener.classList.remove("busy"); });
        return;
      }
      var button = event.target.closest("[data-dry]");
      if (!button) return;
      event.preventDefault();
      var row = button.closest("tr");
      var next = row.nextElementSibling;
      if (next && next.classList.contains("expand")) {
        next.remove();
        button.setAttribute("aria-expanded", "false");
        button.textContent = "Preview";
        return;
      }
      button.classList.add("busy");
      fetch("/process/import/" + button.getAttribute("data-dry") + "/dry")
        .then(function (response) { return response.text(); })
        .then(function (html) {
          var holder = document.createElement("tbody");
          holder.innerHTML = html;
          var expand = holder.firstElementChild;
          expand.firstElementChild.colSpan = row.children.length;
          row.after(expand);
          initPayloads(expand);
          button.setAttribute("aria-expanded", "true");
          button.textContent = "Hide preview";
        })
        .finally(function () { button.classList.remove("busy"); });
    });
  }

  // Outcome filters and text search over one table.
  function initFilters() {
    var selectors = [];
    each("[data-filter-table], input[data-search]", function (node) {
      var selector = node.getAttribute("data-filter-table") || node.getAttribute("data-search");
      if (selectors.indexOf(selector) === -1) selectors.push(selector);
    });
    selectors.forEach(function (selector) {
      var table = document.querySelector(selector);
      if (!table) return;
      var scope = document.querySelector("[data-filter-table='" + selector + "']");
      var rows = Array.prototype.slice.call(table.tBodies[0].rows);
      var buttons = scope ? scope.querySelectorAll("button[data-outcome]") : [];
      var search = document.querySelector("[data-search='" + selector + "']");
      var shown = document.querySelector("[data-shown='" + selector + "']");

      function apply() {
        var on = {};
        Array.prototype.forEach.call(buttons, function (button) {
          if (!button.disabled && button.getAttribute("aria-pressed") === "true") {
            on[button.getAttribute("data-outcome")] = true;
          }
        });
        var needle = search ? search.value.trim().toLowerCase() : "";
        var count = 0;
        rows.forEach(function (row) {
          var visible = (!buttons.length || on[row.getAttribute("data-outcome")]) &&
            (!needle || row.textContent.toLowerCase().indexOf(needle) !== -1);
          row.hidden = !visible;
          if (visible) count += 1;
        });
        if (shown) {
          var totalText = rows.length.toLocaleString();
          if (!needle && count === rows.length) {
            shown.textContent = (count ? "1–" + count : "0") + " of " + totalText;
          } else {
            shown.textContent = count + " of " + totalText + (needle ? " matching" : "");
          }
        }
      }

      Array.prototype.forEach.call(buttons, function (button) {
        button.addEventListener("click", function (event) {
          if (event.altKey || event.shiftKey) {
            Array.prototype.forEach.call(buttons, function (other) {
              other.setAttribute("aria-pressed", String(other === button));
            });
          } else {
            var pressed = button.getAttribute("aria-pressed") === "true";
            button.setAttribute("aria-pressed", String(!pressed));
          }
          apply();
        });
      });
      if (search) {
        search.addEventListener("input", apply);
        if (search.form) {
          search.form.addEventListener("submit", function (event) {
            event.preventDefault();
            apply();
          });
        }
      }
      apply();
    });
  }

  // Import row search: filter server-side across the file, 25 rows per page.
  function initLiveSearch() {
    each("input[data-live-search]", function (input) {
      var timer;
      function go() {
        var form = input.form;
        if (!form) return;
        var action = form.getAttribute("action") || window.location.pathname;
        var params = new URLSearchParams();
        var needle = input.value.trim();
        if (needle) params.set("q", needle);
        Array.prototype.forEach.call(form.querySelectorAll("input[name]"), function (field) {
          if (field === input) return;
          if (field.name && field.value) params.set(field.name, field.value);
        });
        params.set("page", "1");
        var hash = (action.split("#")[1] || "rows");
        var path = action.split("#")[0];
        window.location = path + "?" + params.toString() + "#" + hash;
      }
      input.addEventListener("input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(go, 300);
      });
      if (input.form) {
        input.form.addEventListener("submit", function (event) {
          event.preventDefault();
          window.clearTimeout(timer);
          go();
        });
      }
    });
  }

  // "/" focuses the page search. Escape clears it.
  function initKeys() {
    document.addEventListener("keydown", function (event) {
      var search = document.querySelector("input[data-search], input[data-focus-search]");
      if (!search) return;
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (event.key === "/" && !typing) {
        event.preventDefault();
        search.focus();
        search.select();
      } else if (event.key === "Escape" && document.activeElement === search) {
        search.value = "";
        if (search.hasAttribute("data-search")) {
          search.dispatchEvent(new Event("input"));
          search.blur();
          return;
        }
        if (search.hasAttribute("data-live-search")) {
          search.dispatchEvent(new Event("input"));
          search.blur();
          return;
        }
        if (search.form) search.form.submit();
        else search.blur();
      }
    });
  }

  // Replace opens an upload row under the import.
  function initReplace() {
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-replace]");
      if (!button) return;
      event.preventDefault();
      var id = button.getAttribute("data-replace");
      var row = document.getElementById("replace-" + id);
      if (!row) return;
      row.hidden = !row.hidden;
      each("[data-replace='" + id + "']", function (other) {
        other.setAttribute("aria-expanded", String(!row.hidden));
      });
      if (!row.hidden) row.querySelector("input[type=file]").focus();
    });
  }

  // SQL checks: the query of the hovered or focused check shows in the panel below.
  function initSql() {
    var view = document.querySelector("[data-sql-view]");
    if (!view) return;
    var title = document.querySelector("[data-sql-title]");
    var rows = Array.prototype.slice.call(document.querySelectorAll("tr[data-sql]"));
    function show(row) {
      rows.forEach(function (other) { other.classList.toggle("sql-on", other === row); });
      view.textContent = row.getAttribute("data-sql");
      if (title) title.textContent = row.getAttribute("data-sql-name");
    }
    rows.forEach(function (row) {
      row.addEventListener("mouseenter", function () { show(row); });
      row.addEventListener("focus", function () { show(row); });
    });
    var first = rows.filter(function (row) { return row.hasAttribute("data-failing"); })[0] || rows[0];
    if (first) show(first);
  }

  // Earlier loads of the same file stay folded under the latest row.
  // In-page links scroll without changing the hash, which holds the open tab.
  function initFolds() {
    document.addEventListener("click", function (event) {
      var jump = event.target.closest("a[data-scroll]");
      if (jump) {
        event.preventDefault();
        var target = document.querySelector(jump.getAttribute("href"));
        if (target) target.scrollIntoView({ block: "start" });
        return;
      }
      var button = event.target.closest("[data-fold]");
      if (!button) return;
      var open = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(open));
      each("tr[data-fold-of='" + button.getAttribute("data-fold") + "']", function (row) {
        row.hidden = !open;
      });
      var label = button.querySelector("[data-fold-label]");
      if (label) label.textContent = open ? "Hide" : label.getAttribute("data-fold-label");
    });
  }

  // Payload dialog: main and file side by side, changed fields marked.
  function initPayloads(root) {
    var dialog = document.getElementById("payload-dialog");
    if (!dialog) return;
    each("[data-payload-main]", function (button) {
      if (button.getAttribute("data-bound")) return;
      button.setAttribute("data-bound", "1");
      button.addEventListener("click", function () {
        var main = JSON.parse(button.getAttribute("data-payload-main") || "{}");
        var file = button.getAttribute("data-payload-file");
        file = file ? JSON.parse(file) : null;
        var failing = JSON.parse(button.getAttribute("data-fail-fields") || "[]");
        dialog.querySelector("[data-dialog-title]").textContent = button.getAttribute("data-key");
        var head = "<tr><th>Field</th><th>Main</th>" + (file ? "<th>This file</th>" : "") + "</tr>";
        var names = Object.keys(main).concat(file ? Object.keys(file) : []).concat(failing)
          .filter(function (name, index, list) { return list.indexOf(name) === index; });
        var body = names.map(function (name) {
            var left = main[name] == null ? "" : String(main[name]);
            var right = file && file[name] != null ? String(file[name]) : "";
            var changed = file && left !== right;
            var bad = failing.indexOf(name) !== -1;
            var css = bad ? " class='bad'" : changed ? " class='changed'" : "";
            return "<tr" + css + "><td class='key'>" + escape(name) +
              (bad ? " <span class='tag err'>Fails</span>" : "") +
              "</td><td class='mono'>" + (left ? escape(left) : bad ? "<span class='dim'>blank</span>" : "") + "</td>" +
              (file ? "<td class='mono" + (changed ? " to" : "") + "'>" + escape(right) + "</td>" : "") + "</tr>";
          }).join("");
        dialog.querySelector("[data-dialog-body]").innerHTML =
          "<div class='table-wrap'><table class='table'><thead>" + head + "</thead><tbody>" + body + "</tbody></table></div>";
        dialog.showModal();
      });
    }, root);
    if (!dialog.getAttribute("data-bound")) {
      dialog.setAttribute("data-bound", "1");
      dialog.addEventListener("click", function (event) {
        if (event.target === dialog || event.target.closest("[data-dialog-close]")) dialog.close();
      });
    }
  }

  // Tabs. The open tab is kept in the hash so a refresh or link keeps it.
  function escape(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch];
    });
  }

  initFlash();
  initArm();
  initSubmitLock();
  initRowLinks();
  initDry();
  initFilters();
  initLiveSearch();
  initKeys();
  initFolds();
  initReplace();
  initSql();
  initPayloads(document);
})();
