// birdseye UI helpers. No inline handlers: the CSP forbids them.
(function () {
  "use strict";

  // --- confirm before destructive submits -------------------------------------
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-confirm]");
    if (btn && !window.confirm(btn.getAttribute("data-confirm"))) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, true);

  // --- pick lists: filter, "selected only", counter ---------------------------
  function refreshPicklist(list) {
    var filter = list.querySelector("[data-filter]");
    var onlySel = list.querySelector("[data-only-selected]");
    var q = filter ? filter.value.trim().toLowerCase() : "";
    var picks = list.querySelectorAll(".pick");
    var selected = 0;
    picks.forEach(function (pick) {
      var box = pick.querySelector("input[type=checkbox]");
      if (box.checked) selected++;
      var textHit = !q || (pick.getAttribute("data-text") || "").indexOf(q) !== -1;
      var selHit = !onlySel || !onlySel.checked || box.checked;
      pick.hidden = !(textHit && selHit);
      pick.classList.toggle("checked", box.checked);
    });
    var count = list.querySelector("[data-count]");
    if (count) count.textContent = selected ? selected + " selected" : "";
  }

  function initPicklists(root) {
    root.querySelectorAll("[data-picklist]").forEach(refreshPicklist);
  }

  document.addEventListener("input", function (ev) {
    var list = ev.target.closest("[data-picklist]");
    if (list) refreshPicklist(list);
  });
  document.addEventListener("change", function (ev) {
    var list = ev.target.closest("[data-picklist]");
    if (list) refreshPicklist(list);
  });
  // Filter inputs inside a form must not submit it on Enter.
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && ev.target.matches("[data-filter]")) ev.preventDefault();
  });

  // --- policy editor: add / remove rules --------------------------------------
  document.addEventListener("click", function (ev) {
    var add = ev.target.closest("[data-add-rule]");
    if (add) {
      var rules = document.getElementById("rules");
      var next = 0;
      rules.querySelectorAll("input[name=rule_idx]").forEach(function (i) {
        next = Math.max(next, parseInt(i.value, 10) + 1);
      });
      htmx.ajax("GET", "/policies/rule-row?idx=" + next, { target: "#rules", swap: "beforeend" });
      return;
    }
    var remove = ev.target.closest("[data-remove-rule]");
    if (remove) {
      var all = document.querySelectorAll("[data-rule]");
      if (all.length <= 1) {
        window.alert("A policy needs at least one rule.");
        return;
      }
      var form = remove.closest("form");
      remove.closest("[data-rule]").remove();
      if (form) form.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });

  // --- lazy <details> content ---------------------------------------------------
  document.addEventListener("toggle", function (ev) {
    var d = ev.target;
    if (d.matches && d.matches("details[data-lazy]") && d.open) {
      var inner = d.querySelector("[hx-trigger]");
      if (inner) htmx.trigger(inner, "lazy-open");
    }
  }, true);

  // --- matrix: highlight row + column under the pointer --------------------------
  var lastCol = null;
  document.addEventListener("mouseover", function (ev) {
    var td = ev.target.closest("table.matrix td, table.matrix th.col");
    var table = ev.target.closest("table.matrix");
    if (!table) return;
    var col = td ? td.getAttribute("data-col") : null;
    if (col === lastCol) return;
    table.querySelectorAll(".hl").forEach(function (el) { el.classList.remove("hl"); });
    lastCol = col;
    if (!col) return;
    table.querySelectorAll('[data-col="' + CSS.escape(col) + '"]').forEach(function (el) {
      el.classList.add("hl");
    });
  });

  // --- mark the selected cell --------------------------------------------------------
  document.addEventListener("click", function (ev) {
    var cell = ev.target.closest("table.matrix button.cell");
    if (!cell) return;
    document.querySelectorAll("table.matrix button.cell.sel").forEach(function (b) {
      b.classList.remove("sel");
    });
    cell.classList.add("sel");
  });

  document.addEventListener("DOMContentLoaded", function () { initPicklists(document); });
  document.addEventListener("htmx:afterSwap", function (ev) { initPicklists(ev.detail.elt); });
})();
