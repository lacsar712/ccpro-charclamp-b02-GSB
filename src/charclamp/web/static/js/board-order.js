/* 顶部窑剪影排列编辑：HTML5 拖拽 + 左右箭头，仅管理员编辑态挂载。 */
(function () {
  "use strict";

  function draggableChips(root) {
    return Array.prototype.slice.call(
      root.querySelectorAll(".kiln-chip.is-draggable")
    );
  }

  function syncOrderField(row) {
    var field = document.getElementById("board-order-field");
    if (!field) return;
    field.value = draggableChips(row)
      .map(function (chip) {
        return chip.getAttribute("data-clamp-id");
      })
      .join(",");
  }

  function moveChip(chip, direction) {
    var row = chip.closest(".kiln-row.is-ordering");
    if (!row) return;
    var chips = draggableChips(row);
    var idx = chips.indexOf(chip);
    var target = idx + direction;
    if (target < 0 || target >= chips.length) return;
    if (direction < 0) {
      row.insertBefore(chip, chips[target]);
    } else {
      row.insertBefore(chip, chips[target].nextSibling);
    }
    syncOrderField(row);
  }

  function initOrderForm(root) {
    var form = root.querySelector(".board-order-form");
    if (!form || form.dataset.orderBound === "1") return;
    form.dataset.orderBound = "1";

    var row = form.querySelector(".kiln-row.is-ordering");
    var dragged = null;

    row.addEventListener("dragstart", function (evt) {
      var chip = evt.target.closest(".kiln-chip.is-draggable");
      if (!chip) return;
      dragged = chip;
      chip.classList.add("is-dragging");
      if (evt.dataTransfer) {
        evt.dataTransfer.effectAllowed = "move";
        evt.dataTransfer.setData("text/plain", chip.dataset.clampId);
      }
    });

    row.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("is-dragging");
      dragged = null;
    });

    row.addEventListener("dragover", function (evt) {
      if (!dragged) return;
      evt.preventDefault();
      var chip = evt.target.closest(".kiln-chip.is-draggable");
      if (!chip || chip === dragged) return;
      var rect = chip.getBoundingClientRect();
      var after = evt.clientX > rect.left + rect.width / 2;
      if (after) {
        row.insertBefore(dragged, chip.nextSibling);
      } else {
        row.insertBefore(dragged, chip);
      }
    });

    row.addEventListener("drop", function (evt) {
      evt.preventDefault();
      if (dragged) dragged.classList.remove("is-dragging");
      syncOrderField(row);
      dragged = null;
    });

    row.addEventListener("click", function (evt) {
      var btn = evt.target.closest(".order-step");
      if (!btn) return;
      var chip = btn.closest(".kiln-chip.is-draggable");
      if (!chip) return;
      moveChip(chip, btn.dataset.move === "left" ? -1 : 1);
    });

    // 提交前再同步一次，防止拖拽未触发 drop 收尾。
    form.addEventListener("htmx:configRequest", function () {
      syncOrderField(row);
    });
  }

  function init() {
    var board = document.getElementById("board");
    if (board) initOrderForm(board);
  }

  document.addEventListener("DOMContentLoaded", init);
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    if (evt.detail.target && evt.detail.target.id === "board") {
      initOrderForm(evt.detail.target);
    }
  });

  // 409（并发冲突）/ 422（校验失败）返回的是最新板面片段，照常换入 #board。
  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var xhr = evt.detail.xhr;
    if (!xhr) return;
    if (xhr.status === 409 || xhr.status === 422) {
      evt.detail.shouldSwap = true;
      evt.detail.isError = false;
    }
  });
})();
