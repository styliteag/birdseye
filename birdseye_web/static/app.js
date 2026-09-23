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

  // --- cell popover ---------------------------------------------------------------------
  var pop = null, anchor = null;

  function closePopover() {
    if (!pop) return;
    pop.hidden = true;
    if (anchor) anchor.classList.remove("pop-anchor");
    anchor = null;
  }

  function placePopover(td) {
    var r = td.getBoundingClientRect();
    var w = pop.offsetWidth, h = pop.offsetHeight, m = 8;
    var left = r.right + m;
    if (left + w > window.innerWidth - m) left = Math.max(m, r.left - w - m);
    var top = Math.min(Math.max(m, r.top - 20), window.innerHeight - h - m);
    pop.style.left = left + "px";
    pop.style.top = Math.max(m, top) + "px";
  }

  function openCell(td) {
    var tr = td.closest("tr");
    var rowTh = tr && tr.querySelector("th.row");
    var col = td.getAttribute("data-col");
    if (!rowTh || !col) return;
    var form = document.getElementById("matrix-filter");
    var q = new URLSearchParams(form ? new FormData(form) : undefined);
    q.set("row", rowTh.getAttribute("data-row"));
    q.set("col", col);
    pop = document.getElementById("popover");
    if (anchor) anchor.classList.remove("pop-anchor");
    anchor = td;
    td.classList.add("pop-anchor");
    htmx.ajax("GET", "/matrix/cell?" + q.toString(), { target: "#popover-body", swap: "innerHTML" })
      .then(function () {
        pop.hidden = false;
        placePopover(td);
      });
  }

  document.addEventListener("click", function (ev) {
    if (ev.target.closest("[data-pop-close]")) { closePopover(); return; }
    var cancel = ev.target.closest("[data-quick-cancel]");
    if (cancel) { cancel.closest(".quick-result").innerHTML = ""; return; }
    var td = ev.target.closest("table.matrix td");
    if (td) {
      var editable = td.closest("table.matrix[data-editable='1']");
      if (td.classList.contains("hit") || editable) { openCell(td); return; }
    }
    if (pop && !pop.hidden && !ev.target.closest("#popover")) closePopover();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") closePopover();
  });
  // Popover grows when a preview loads: keep it inside the viewport.
  document.addEventListener("htmx:afterSettle", function (ev) {
    if (pop && !pop.hidden && anchor && ev.target.closest && ev.target.closest("#popover")) {
      placePopover(anchor);
    }
  });
  document.addEventListener("scroll", function (ev) {
    if (ev.target.closest && ev.target.closest("#popover")) return;
    closePopover();
  }, true);

  document.addEventListener("DOMContentLoaded", function () { initPicklists(document); });
  document.addEventListener("htmx:afterSwap", function (ev) { initPicklists(ev.detail.elt); });
})();
