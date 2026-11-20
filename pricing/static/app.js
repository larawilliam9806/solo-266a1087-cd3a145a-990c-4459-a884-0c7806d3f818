(function () {
  "use strict";

  const viewer = document.getElementById("viewer");
  if (!viewer) return;

  const jobId = viewer.dataset.jobId;
  const svg = document.getElementById("canvas");
  const gridLayer = document.getElementById("grid-lines");
  const boxLayer = document.getElementById("boxes");
  const hitLayer = document.getElementById("line-hits");
  const inspector = document.getElementById("inspector");
  const verticalInput = document.getElementById("vertical-input");
  const horizontalInput = document.getElementById("horizontal-input");
  const lineError = document.getElementById("line-error");
  const applyButton = document.getElementById("apply-lines");
  const SVG_NS = "http://www.w3.org/2000/svg";

  const STATUS_COLORS = {
    assigned_auto: "#2f6b48",
    assigned_manual: "#1f5f8b",
    pending: "#b3261e",
    stale: "#a35a00",
    excluded: "#7a7266",
    inactive: "#a89f8d",
  };
  const STATUS_LABELS = {
    assigned_auto: "自动归位",
    assigned_manual: "人工归位",
    pending: "待核",
    stale: "待重新确认",
    excluded: "已排除",
    inactive: "未采用",
  };
  const STATUS_ORDER = ["assigned_auto", "assigned_manual", "pending", "stale", "excluded", "inactive"];
  const COLUMN_LABELS = { name: "品名", spec: "规格", price: "批发价" };
  const SIDE_LABELS = { old: "旧版", new: "新版" };

  let state = null;                 // last applied server state
  let draft = { h: [], v: [] };     // lines currently drawn (may be unapplied)
  let selected = null;              // {side, id} of the clicked box
  let bounds = { minX: 0, minY: 0, maxX: 1, maxY: 1 };
  let dragging = null;

  // ---------------------------------------------------------------- helpers
  function el(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });
    return node;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function num(value) {
    return Math.round(Number(value) * 100) / 100;
  }

  function parseList(value) {
    const matches = String(value || "").match(/[-+]?(?:\d+\.?\d*|\.\d+)/g) || [];
    const values = [];
    matches.map(Number).filter(isFinite).forEach(function (number) {
      if (values.indexOf(number) === -1) values.push(number);
    });
    return values.sort(function (a, b) { return a - b; });
  }

  function request(url, options) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, options || {}))
      .then(function (response) {
        return response.text().then(function (text) {
          let data = {};
          try { data = JSON.parse(text); } catch (e) { data = {}; }
          if (!response.ok || data.ok === false) {
            throw new Error(
              [data.error || ("请求失败（" + response.status + "）")]
                .concat(data.details || []).join("\n")
            );
          }
          return data;
        });
      });
  }

  function fail(error) {
    window.alert(error.message || String(error));
  }

  function draftMatchesState() {
    return draft.h.join(",") === state.horizontal.join(",") &&
           draft.v.join(",") === state.vertical.join(",");
  }

  // ---------------------------------------------------------------- layout
  function computeBounds() {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    state.boxes.forEach(function (box) {
      minX = Math.min(minX, box.x1); minY = Math.min(minY, box.y1);
      maxX = Math.max(maxX, box.x2); maxY = Math.max(maxY, box.y2);
    });
    draft.h.forEach(function (y) { minY = Math.min(minY, y); maxY = Math.max(maxY, y); });
    draft.v.forEach(function (x) { minX = Math.min(minX, x); maxX = Math.max(maxX, x); });
    if (!isFinite(minX)) { minX = 0; minY = 0; maxX = 800; maxY = 1000; }
    const padX = Math.max((maxX - minX) * 0.05, 10);
    const padY = Math.max((maxY - minY) * 0.05, 10);
    return { minX: minX - padX, minY: minY - padY, maxX: maxX + padX, maxY: maxY + padY };
  }

  function render() {
    bounds = computeBounds();
    const width = Math.max(bounds.maxX - bounds.minX, 1);
    const height = Math.max(bounds.maxY - bounds.minY, 1);
    svg.setAttribute("viewBox", [bounds.minX, bounds.minY, width, height].join(" "));
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.style.aspectRatio = width + " / " + height;

    const bg = document.getElementById("page-bg");
    bg.setAttribute("x", bounds.minX);
    bg.setAttribute("y", bounds.minY);
    bg.setAttribute("width", width);
    bg.setAttribute("height", height);

    gridLayer.innerHTML = "";
    boxLayer.innerHTML = "";
    hitLayer.innerHTML = "";

    // Faint column shading from the drafted verticals.
    if (draft.v.length === 4 && draft.h.length >= 2) {
      draft.h.slice(0, -1).forEach(function (top, row) {
        draft.v.slice(0, -1).forEach(function (left, col) {
          if (col === 1) return;
          gridLayer.appendChild(el("rect", {
            x: left, y: top,
            width: draft.v[col + 1] - left,
            height: draft.h[row + 1] - top,
            fill: col === 0 ? "#f7efe3" : "#eef4ef",
            "fill-opacity": "0.55",
            "pointer-events": "none",
          }));
        });
      });
    }

    draft.h.forEach(function (y, index) {
      gridLayer.appendChild(makeLine("h", y, index));
    });
    draft.v.forEach(function (x, index) {
      gridLayer.appendChild(makeLine("v", x, index));
    });

    state.boxes.forEach(function (box) {
      const color = STATUS_COLORS[box.status] || "#999";
      const isSelected = selected && box.side === selected.side && box.box_id === selected.id;
      const group = el("g", {
        "data-box-id": box.box_id,
        "data-side": box.side,
        class: "box" + (isSelected ? " sel" : ""),
      });
      group.appendChild(el("rect", {
        x: box.x1, y: box.y1,
        width: Math.max(box.x2 - box.x1, 0.5),
        height: Math.max(box.y2 - box.y1, 0.5),
        rx: 1.5,
        fill: color,
        "fill-opacity": isSelected ? "0.34"
          : (box.status === "excluded" || box.status === "inactive" ? "0.10" : "0.18"),
        stroke: color,
        "stroke-width": isSelected ? 3 : 1.4,
        "stroke-dasharray": (box.status !== "assigned_auto" && box.status !== "assigned_manual"
          && box.status !== "pending") ? "4 2" : "none",
        class: "box-rect",
      }));
      if (box.text) {
        const room = Math.max(box.y2 - box.y1, 1);
        const label = el("text", {
          x: box.x1 + 2,
          y: box.y1 + Math.max(room * 0.72, 4),
          class: "box-label",
        });
        label.setAttribute("font-size", Math.max(Math.min(room * 0.6, 22), 3));
        label.textContent = box.text.length > 24 ? box.text.slice(0, 24) + "…" : box.text;
        group.appendChild(label);
      }
      group.addEventListener("click", function (event) {
        event.stopPropagation();
        selected = { side: box.side, id: box.box_id };
        render();
        renderInspector();
      });
      boxLayer.appendChild(group);
    });

    syncInputs();
    updateSummary();
  }

  function makeLine(orientation, value, index) {
    const isH = orientation === "h";
    const line = el("line", isH
      ? { x1: bounds.minX, y1: value, x2: bounds.maxX, y2: value, class: "grid-line h" }
      : { x1: value, y1: bounds.minY, x2: value, y2: bounds.maxY, class: "grid-line v" });
    const hit = el("line", {
      x1: line.getAttribute("x1"), y1: line.getAttribute("y1"),
      x2: line.getAttribute("x2"), y2: line.getAttribute("y2"),
      class: "grid-hit",
    });

    hit.addEventListener("pointerdown", function (event) {
      event.preventDefault();
      event.stopPropagation();
      const point = svgPoint(event);
      dragging = {
        orientation: orientation,
        index: index,
        start: isH ? point.y : point.x,
        original: isH ? draft.h.slice() : draft.v.slice(),
      };
      line.classList.add("dragging");
      hit.classList.add("dragging");
      try { svg.setPointerCapture(event.pointerId); } catch (e) { /* ignore */ }
    });

    hit.addEventListener("dblclick", function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (isH) draft.h.splice(index, 1);
      else draft.v.splice(index, 1);
      render();
    });

    // The visible line goes under the boxes; the invisible hit target sits
    // in a top layer so lines remain draggable where they cross a box.
    const group = el("g", {});
    group.appendChild(line);
    hitLayer.appendChild(hit);
    return group;
  }

  function svgPoint(event) {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(svg.getScreenCTM().inverse());
  }

  window.addEventListener("pointermove", function (event) {
    if (!dragging) return;
    const point = svgPoint(event);
    const delta = (dragging.orientation === "h" ? point.y : point.x) - dragging.start;
    const value = num(dragging.original[dragging.index] + delta);
    const list = dragging.original.slice();
    list[dragging.index] = value;
    list.sort(function (a, b) { return a - b; });
    dragging.index = list.indexOf(value);
    if (dragging.orientation === "h") draft.h = list;
    else draft.v = list;
    render();
  });

  window.addEventListener("pointerup", function () {
    if (!dragging) return;
    dragging = null;
    render();
  });

  function syncInputs() {
    if (document.activeElement !== verticalInput) verticalInput.value = draft.v.join(", ");
    if (document.activeElement !== horizontalInput) horizontalInput.value = draft.h.join(", ");
  }

  // ------------------------------------------------------------- status area
  function updateSummary() {
    const pill = document.getElementById("status-pill");
    if (!state.grid_valid) {
      pill.className = "pill bad";
      pill.textContent = "格线无效";
    } else if (state.pending) {
      pill.className = "pill warn";
      pill.textContent = "待核 " + state.pending + " 个框";
    } else if (state.unchosen_groups) {
      pill.className = "pill warn";
      pill.textContent = "待选版 " + state.unchosen_groups + " 组";
    } else {
      pill.className = "pill good";
      pill.textContent = "全部归位，可下载";
    }

    document.getElementById("status-counts").textContent = "· " +
      state.boxes.length + " 个文字框 " +
      STATUS_ORDER.filter(function (key) { return state.status_counts[key]; })
        .map(function (key) { return STATUS_LABELS[key] + " " + state.status_counts[key]; })
        .join("　");

    const invalid = document.getElementById("invalid-message");
    invalid.hidden = state.grid_valid;
    if (!state.grid_valid) {
      invalid.textContent = state.invalid + "。当前保留原有布局与归位结果。";
    }

    const download = document.getElementById("download-link");
    if (state.can_export) {
      download.className = "button primary";
      download.setAttribute("href", "/jobs/" + jobId + "/export.csv");
      download.removeAttribute("aria-disabled");
      download.removeAttribute("title");
    } else {
      download.className = "button disabled";
      download.removeAttribute("href");
      download.setAttribute("aria-disabled", "true");
      download.setAttribute("title",
        !state.grid_valid ? "格线无效，修正后才可下载"
        : state.pending ? "没有待核框后才可下载"
        : state.unchosen_groups ? "所有对照组选版后才可下载"
        : "没有可导出的内容");
    }

    document.getElementById("pending-notice").hidden = !state.pending;
    document.getElementById("pending-count").textContent = state.pending;
    document.getElementById("groups-notice").hidden = !state.unchosen_groups;
    document.getElementById("groups-count").textContent = state.unchosen_groups;

    const dirty = !draftMatchesState();
    applyButton.classList.toggle("primary", dirty);
    applyButton.textContent = dirty ? "应用格线 *" : "应用格线";
  }

  // ------------------------------------------------------------- inspector
  function selectedBox() {
    if (!selected) return null;
    return state.boxes.find(function (box) {
      return box.side === selected.side && box.box_id === selected.id;
    }) || null;
  }

  function renderInspector() {
    const box = selectedBox();
    if (!box) {
      inspector.innerHTML = '<p class="muted">点击一个文字框查看详情。</p>';
      return;
    }
    const color = STATUS_COLORS[box.status] || "#333";
    const parts = [
      "<h3>框 " + escapeHtml(box.box_ref || box.box_id) +
        ' <span class="side-tag">' + escapeHtml(SIDE_LABELS[box.side] || box.side) +
        "</span></h3>",
      '<dl class="kv">',
      "<dt>文字</dt><dd>" +
        (box.text ? escapeHtml(box.text) : '<span class="muted">（空）</span>') + "</dd>",
      "<dt>坐标</dt><dd>" +
        [box.x1, box.y1, box.x2, box.y2].map(function (value) { return num(value); })
          .join(", ") + "</dd>",
      "<dt>状态</dt><dd style=\"color:" + color + "\">" +
        escapeHtml(STATUS_LABELS[box.status] || box.status) + "</dd>",
    ];
    if (box.row !== null && box.row !== undefined) {
      parts.push("<dt>归位</dt><dd>第 " + (box.row + 1) + " 行 · " +
        escapeHtml(COLUMN_LABELS[box.column] || "") + "</dd>");
    }
    parts.push("</dl>");

    if (box.note) {
      parts.push('<p class="' + (box.status === "stale" ? "note stale" : "note") + '">' +
        escapeHtml(box.note) + "</p>");
    }

    if (box.status === "inactive") {
      if (box.group_key) {
        parts.push('<div class="ops"><button type="button" id="goto-group">查看所在对照组</button></div>');
      }
      inspector.innerHTML = parts.join("");
      const gotoGroup = document.getElementById("goto-group");
      if (gotoGroup) {
        gotoGroup.addEventListener("click", function () {
          const card = document.getElementById("group-" + box.group_key);
          if (card) {
            if (card.scrollIntoView) {
              card.scrollIntoView({ behavior: "smooth", block: "center" });
            }
            card.classList.add("flash-once");
            setTimeout(function () { card.classList.remove("flash-once"); }, 1600);
          }
        });
      }
      return;
    }

    const actionable = box.status === "pending" || box.status === "stale";
    if (actionable) {
      parts.push('<div class="ops">');
      if (box.candidates.length) {
        parts.push('<div class="op"><select id="target-cell">');
        box.candidates.forEach(function (candidate) {
          parts.push('<option value="' + escapeHtml(candidate.key) + '">第 ' +
            (candidate.row + 1) + " 行 · " +
            escapeHtml(COLUMN_LABELS[candidate.column] || "") +
            "（重叠 " + Math.round(candidate.ratio * 100) + "%）</option>");
        });
        parts.push("</select>");
        parts.push('<button type="button" class="primary" id="assign-cell">归入此格</button></div>');
        parts.push('<p class="muted" style="margin:0">只列出与该框有面积重叠的格子。</p>');
      } else {
        parts.push('<p class="muted" style="margin:0">没有任何格子与该框重叠，只能排除。</p>');
      }
      parts.push('<button type="button" class="danger" id="exclude-box">排除此框</button>');
      if (box.decision) {
        parts.push('<button type="button" id="clear-decision">撤销原人工决定</button>');
      }
      parts.push("</div>");
    } else if (box.decision) {
      parts.push('<div class="ops"><button type="button" id="clear-decision">撤销人工决定</button></div>');
    }

    inspector.innerHTML = parts.join("");

    const assign = document.getElementById("assign-cell");
    if (assign) {
      assign.addEventListener("click", function () {
        sendDecision({
          box_id: box.box_id,
          side: box.side,
          kind: "cell",
          target_cell: document.getElementById("target-cell").value,
        });
      });
    }
    const exclude = document.getElementById("exclude-box");
    if (exclude) {
      exclude.addEventListener("click", function () {
        sendDecision({ box_id: box.box_id, side: box.side, kind: "exclude" });
      });
    }
    const clear = document.getElementById("clear-decision");
    if (clear) {
      clear.addEventListener("click", function () {
        request("/api/jobs/" + jobId + "/decisions/" + box.box_id + "?side=" + box.side,
          { method: "DELETE" })
          .then(applyState).catch(fail);
      });
    }
  }

  function sendDecision(payload) {
    request("/api/jobs/" + jobId + "/decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(applyState).catch(fail);
  }

  // ------------------------------------------------------------- state sync
  function applyState(next) {
    state = next;
    draft = { h: next.horizontal.slice(), v: next.vertical.slice() };
    if (selected && !selectedBox()) selected = null;
    render();
    renderInspector();
    updatePreview();
    renderGroups();
  }

  function updatePreview() {
    const tbody = document.querySelector("#preview-table tbody");
    tbody.innerHTML = "";
    state.rows.forEach(function (row) {
      const tr = document.createElement("tr");
      [row.row, row.name, row.spec, row.price].forEach(function (value, index) {
        const td = document.createElement("td");
        if (index === 0) td.className = "muted";
        td.textContent = value;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    document.getElementById("preview-empty").hidden = state.rows.length > 0;
  }

  // --------------------------------------------------------- version groups
  function groupBadge(group) {
    if (!group.choice) return '<span class="pill warn">待选版</span>';
    const sideBoxes = group.choice === "old" ? group.old : group.new;
    if (!sideBoxes.length) return '<span class="pill muted-pill">已舍弃</span>';
    return '<span class="pill good">已用' +
      escapeHtml(SIDE_LABELS[group.choice]) + "</span>";
  }

  function boxLine(item) {
    return "<li><span class=\"g-ref\">" + escapeHtml(item.box_ref || item.box_id) +
      "</span> " + (item.text ? escapeHtml(item.text) : '<span class="muted">（空）</span>') +
      ' <span class="g-coords">(' +
      [item.x1, item.y1, item.x2, item.y2].map(function (value) { return num(value); })
        .join(", ") + ")</span></li>";
  }

  function renderGroups() {
    const container = document.getElementById("groups");
    if (!container) return;
    const groups = state.groups || [];
    const summary = document.getElementById("variant-summary");
    if (state.variant) {
      summary.textContent = "新版文件 " + state.variant.filename +
        " · 相同组 " + state.identical_groups + "（沿用原框）" +
        " · 差异组 " + groups.length +
        (state.unchosen_groups ? "，待选 " + state.unchosen_groups : "，已全部选完");
    } else {
      summary.textContent = "";
    }
    document.getElementById("groups-empty").hidden = groups.length > 0;
    if (state.variant && !groups.length) {
      document.getElementById("groups-empty").textContent = "新版与旧版没有差异组。";
    } else if (!state.variant) {
      document.getElementById("groups-empty").textContent = "尚未上传新版。";
    }
    container.innerHTML = "";

    groups.forEach(function (group, index) {
      const card = document.createElement("div");
      card.className = "group" + (group.choice ? "" : " unchosen");
      card.id = "group-" + group.key;

      const head = document.createElement("div");
      head.className = "group-head";
      head.innerHTML = "<strong>对照组 " + (index + 1) + "</strong>" + groupBadge(group);
      card.appendChild(head);

      const cols = document.createElement("div");
      cols.className = "group-cols";
      ["old", "new"].forEach(function (side) {
        const items = group[side];
        const col = document.createElement("div");
        col.className = "group-side" + (group.choice === side ? " chosen" : "");
        const list = items.length
          ? "<ul>" + items.map(boxLine).join("") + "</ul>"
          : '<p class="muted g-empty">（空）</p>';
        const hint = items.length ? "" :
          '<span class="muted">（选则舍弃整组）</span>';
        col.innerHTML = "<h4>" + escapeHtml(SIDE_LABELS[side]) + "</h4>" + list +
          '<label class="pick"><input type="radio" name="group-' + group.key +
          '" value="' + side + '"' + (group.choice === side ? " checked" : "") +
          "> 用" + escapeHtml(SIDE_LABELS[side]) + hint + "</label>";
        cols.appendChild(col);
      });
      card.appendChild(cols);
      container.appendChild(card);

      card.querySelectorAll('input[type="radio"]').forEach(function (radio) {
        radio.addEventListener("change", function () {
          if (!radio.checked) return;
          request("/api/jobs/" + jobId + "/groups/" + group.key, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ side: radio.value }),
          }).then(applyState).catch(fail);
        });
      });
    });
  }

  // ----------------------------------------------------------- line editing
  function applyLines() {
    lineError.hidden = true;
    const attempted = { h: draft.h.slice(), v: draft.v.slice() };
    request("/api/jobs/" + jobId + "/lines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ horizontal: attempted.h, vertical: attempted.v }),
    }).then(function (data) {
      applyState(data);
      lineError.hidden = true;
    }).catch(function (error) {
      // The server refused the lines, so nothing was saved: put the canvas
      // and inputs back to the still-saved layout (the rejected draft is
      // kept in the message so the user can see what was wrong).
      draft = { h: state.horizontal.slice(), v: state.vertical.slice() };
      lineError.hidden = false;
      lineError.textContent = error.message + "（已保留原布局和归位结果）";
      render();
    });
  }

  applyButton.addEventListener("click", applyLines);

  document.getElementById("suggest-lines").addEventListener("click", function () {
    request("/api/jobs/" + jobId + "/suggest-lines", { method: "POST" })
      .then(function (data) {
        draft = { h: data.horizontal, v: data.vertical };
        render();
      })
      .catch(fail);
  });

  document.getElementById("reset-lines").addEventListener("click", function () {
    draft = { h: state.horizontal.slice(), v: state.vertical.slice() };
    lineError.hidden = true;
    render();
  });

  document.getElementById("add-horizontal").addEventListener("click", function () {
    addLine("h");
  });
  document.getElementById("add-vertical").addEventListener("click", function () {
    addLine("v");
  });

  function addLine(orientation) {
    const isH = orientation === "h";
    const list = isH ? draft.h : draft.v;
    const low = isH ? bounds.minY : bounds.minX;
    const high = isH ? bounds.maxY : bounds.maxX;
    const edges = [low].concat(list.filter(function (value) {
      return value > low && value < high;
    }), [high]);
    let best = { gap: -1, mid: (low + high) / 2 };
    for (let i = 0; i < edges.length - 1; i += 1) {
      const gap = edges[i + 1] - edges[i];
      if (gap > best.gap) best = { gap: gap, mid: (edges[i] + edges[i + 1]) / 2 };
    }
    const next = list.concat([num(best.mid)]).sort(function (a, b) { return a - b; });
    if (isH) draft.h = next;
    else draft.v = next;
    render();
  }

  // Inputs are the source of truth for typed coordinates, so parse on blur
  // and on apply; keep the canvas in sync as the user types.
  [verticalInput, horizontalInput].forEach(function (input) {
    input.addEventListener("input", function () {
      if (input === verticalInput) draft.v = parseList(input.value);
      else draft.h = parseList(input.value);
      render();
      // render() rewrites the other input; keep the focused one untouched.
      if (input === verticalInput) verticalInput.value = input.value;
      else horizontalInput.value = input.value;
    });
  });

  svg.addEventListener("click", function () {
    if (selected !== null) {
      selected = null;
      render();
      renderInspector();
    }
  });

  // ------------------------------------------------------------ bootstrap
  const initial = document.getElementById("initial-state");
  try {
    state = JSON.parse(initial.textContent);
  } catch (e) {
    state = null;
  }
  if (state) {
    applyState(state);
  } else {
    request("/api/jobs/" + jobId).then(applyState).catch(fail);
  }
})();
