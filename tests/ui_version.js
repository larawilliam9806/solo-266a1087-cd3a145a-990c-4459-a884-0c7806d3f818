// Headless check of the new-version comparison workflow: upload a re-OCR'd
// CSV into the same job, pick a side per difference group, watch the preview
// and the export gate react.  Requires a live server.
// Usage: node tests/ui_version.js <baseUrl>
const { JSDOM } = require("jsdom");

const base = process.argv[2] || "http://127.0.0.1:5057";

const OLD_CSV = `框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,跨格,90,10,110,40
7,出界,400,400,450,430
`;

// Same page re-recognised: box 6 reads differently, box 7 is gone, one extra
// box appears far outside the table.
const NEW_CSV = `框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,跨格准,90,10,110,40
8,新品,500,500,540,520
`;

function assert(condition, message) {
  if (!condition) throw new Error("FAIL: " + message);
  console.log("ok - " + message);
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function state(jobId) {
  return fetch(`${base}/api/jobs/${jobId}`).then((r) => r.json());
}

(async () => {
  // Upload the original page and pin a known grid.
  const form = new FormData();
  form.append("file", new Blob([OLD_CSV], { type: "text/csv" }), "page.csv");
  const up = await fetch(`${base}/upload`, { method: "POST", body: form, redirect: "manual" });
  const jobId = up.headers.get("location").split("/").pop();
  await fetch(`${base}/api/jobs/${jobId}/lines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ horizontal: [0, 100, 200], vertical: [0, 100, 200, 300] }),
  });

  // Attach the re-OCR'd version to the same job.
  const vform = new FormData();
  vform.append("file", new Blob([NEW_CSV], { type: "text/csv" }), "page-v2.csv");
  const vup = await fetch(`${base}/jobs/${jobId}/version`, {
    method: "POST", body: vform, redirect: "manual",
  });
  assert(vup.status === 302, "new version upload redirects back to the job");

  const before = await state(jobId);
  assert(before.variant && before.variant.filename === "page-v2.csv",
    "variant recorded on the job");
  assert(before.identical_groups === 5, "five identical groups kept on the old boxes");
  assert(before.groups.length === 3 && before.unchosen_groups === 3,
    "three difference groups await a side choice");
  assert(before.can_export === false, "export locked while groups are unchosen");

  // Open the page and drive the group cards in the DOM.
  const page = await fetch(`${base}/jobs/${jobId}`).then((r) => r.text());
  const dom = new JSDOM(page, {
    url: `${base}/jobs/${jobId}`,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.fetch = (url, options) => fetch(new URL(url, base), options);
  window.alert = (message) => { throw new Error("UI alert: " + message); };
  await new Promise((resolve) => window.addEventListener("load", resolve));
  await wait(300);
  const doc = window.document;

  const cards = doc.querySelectorAll("#groups .group");
  assert(cards.length === 3, "three group cards render side by side");
  assert(doc.querySelectorAll("#groups .group.unchosen").length === 3,
    "all three cards start unchosen");
  assert(!doc.getElementById("groups-notice").hidden,
    "groups notice warns about unchosen groups");
  assert(doc.getElementById("status-pill").textContent.includes("待选版"),
    "status pill reports groups awaiting a side choice");
  assert(!doc.getElementById("download-link").getAttribute("href"),
    "download stays disabled while groups are unchosen");

  const keys = before.groups.map((group) => group.key);
  const [straddle, oldOnly, newOnly] = keys;

  function pick(key, side) {
    const radio = doc.querySelector(`#group-${key} input[value="${side}"]`);
    assert(radio, `radio for ${side} exists on group ${key.slice(0, 6)}`);
    radio.checked = true;
    radio.dispatchEvent(new window.Event("change", { bubbles: true }));
    return wait(400);
  }

  // Take the new reading of the straddling box.
  await pick(straddle, "new");
  let now = await state(jobId);
  assert(now.unchosen_groups === 2, "first choice recorded");
  const new6 = now.boxes.find((b) => b.side === "new" && b.box_ref === "6");
  const old6 = now.boxes.find((b) => b.side === "old" && b.box_ref === "6");
  assert(new6.status === "pending", "chosen new box becomes pending (straddles)");
  assert(old6.status === "inactive", "old box of the group drops out");
  assert(doc.querySelector(`#group-${straddle} input[value="new"]`).checked,
    "radio reflects the stored choice after re-render");

  // Change the choice: switching sides is always allowed.
  await pick(straddle, "old");
  now = await state(jobId);
  assert(now.boxes.find((b) => b.side === "old" && b.box_ref === "6").status === "pending",
    "switching back reactivates the old box");
  await pick(straddle, "new");

  // Discard both singleton groups by choosing their empty sides.
  await pick(oldOnly, "new");
  await pick(newOnly, "old");
  now = await state(jobId);
  assert(now.unchosen_groups === 0, "all groups chosen");
  assert(now.boxes.find((b) => b.side === "old" && b.box_ref === "7").status === "inactive",
    "old-only box discarded via the empty side");
  assert(now.boxes.find((b) => b.side === "new" && b.box_ref === "8").status === "inactive",
    "new-only box discarded via the empty side");
  assert(doc.getElementById("groups-notice").hidden, "groups notice clears");
  assert(now.can_export === false, "export still locked: the new straddler is pending");

  // Resolve the chosen side's pending box through the API (the page polls no
  // state; the export gate is driven by the response of every action).
  const pendingNew = now.boxes.find((b) => b.side === "new" && b.status === "pending");
  const target = pendingNew.candidates[0].key;
  const decided = await fetch(`${base}/api/jobs/${jobId}/decisions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      box_id: pendingNew.box_id, side: "new", kind: "cell", target_cell: target,
    }),
  }).then((r) => r.json());
  assert(decided.can_export === true, "export unlocked once groups and boxes are done");

  const csv = await fetch(`${base}/jobs/${jobId}/export.csv`).then((r) => r.text());
  assert(csv.includes("跨格准"), "exported CSV uses the chosen new-side text");
  assert(!csv.includes("出界") && !csv.includes("新品"),
    "discarded groups stay out of the export");

  console.log("done");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
