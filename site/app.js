// Spain Opportunity Atlas: loads data/index.json and one data/<category>.json at a time.
// Each area: c code, n name, x lon, y lat, p residents, m market €/yr, b businesses (est.),
// s score, lo/hi score range, f flag ("thin" | "missing" | "").
"use strict";

const DATA = "data/";
const STYLE = "https://tiles.openfreemap.org/styles/positron";
const PENINSULA = [[-9.6, 35.8], [4.6, 43.9]]; // mainland, Balearics, Ceuta and Melilla
const CANARIAS = [[-18.3, 27.5], [-13.3, 29.5]];
const $ = (id) => document.getElementById(id);
const fmtInt = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 });
const fmtEur = (v) => (v == null ? "n/a" : "€" + fmtInt.format(v));
const fmtScore = (v) => (v == null ? "n/a" : v.toFixed(2));
const params = new URLSearchParams(location.search);

let map;
let areas = [];
let meta = null;

function setStatus(text, isError = false) {
  const el = $("status");
  el.textContent = text;
  el.classList.toggle("error", isError);
}

function colour(a) {
  if (a.f === "thin") return "#ffffff";
  if (a.s == null) return "#bcccdc";
  if (a.s < 0.85) return "#829ab1";
  if (a.s <= 1.15) return "#f0b429";
  return "#c2410c";
}

function geojson(items) {
  return {
    type: "FeatureCollection",
    features: items.map((a) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [a.x, a.y] },
      properties: { code: a.c, colour: colour(a), r: Math.max(3, Math.min(22, Math.sqrt(a.p) / 22)), thin: a.f === "thin" ? 1 : 0 },
    })),
  };
}

function ranked(items) {
  return items
    .filter((a) => a.lo != null && a.f !== "thin" && a.f !== "missing")
    .sort((a, b) => b.lo - a.lo || b.s - a.s);
}

function showCard(a) {
  const card = $("card");
  const flag =
    a.f === "thin"
      ? "The map looks thin here (few mapped pharmacies and bars per resident), so supply is likely understated and the score overstated."
      : a.f === "missing"
      ? "Some inputs were unavailable; no score."
      : "";
  card.innerHTML = `
    <h2></h2>
    <dl>
      <dt>Residents</dt><dd>${fmtInt.format(a.p)}</dd>
      <dt>Market</dt><dd>${fmtEur(a.m)} a year</dd>
      <dt>Businesses</dt><dd>${a.b == null ? "n/a" : fmtInt.format(a.b)} (estimated)</dd>
      <dt>Score</dt><dd>${fmtScore(a.s)} (${fmtScore(a.lo)} to ${fmtScore(a.hi)})</dd>
    </dl>
    <p class="flag"></p>
    <button type="button" id="close-card">Close</button>`;
  card.querySelector("h2").textContent = a.n; // names are data: set as text, never HTML
  card.querySelector(".flag").textContent = flag;
  card.hidden = false;
  $("close-card").onclick = () => (card.hidden = true);
}

function renderList(items) {
  const ol = $("top");
  ol.replaceChildren();
  for (const a of ranked(items).slice(0, 20)) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = `${a.n}: score ${fmtScore(a.s)} (${fmtScore(a.lo)}–${fmtScore(a.hi)}), ${fmtInt.format(a.p)} residents`;
    b.onclick = () => {
      toggleList(false);
      map.flyTo({ center: [a.x, a.y], zoom: 10 });
      showCard(a);
    };
    li.append(b);
    ol.append(li);
  }
}

function renderTable(items) {
  const tbody = $("table").querySelector("tbody");
  tbody.replaceChildren();
  for (const a of ranked(items)) {
    const tr = document.createElement("tr");
    for (const v of [a.n, fmtInt.format(a.p), fmtEur(a.m), a.b == null ? "n/a" : fmtInt.format(a.b), `${fmtScore(a.s)} (${fmtScore(a.lo)}–${fmtScore(a.hi)})`]) {
      const td = document.createElement("td");
      td.textContent = v;
      tr.append(td);
    }
    tbody.append(tr);
  }
}

function toggleList(force) {
  const list = $("list");
  const show = force ?? list.hidden;
  list.hidden = !show;
  $("view-toggle").setAttribute("aria-pressed", String(show));
  $("view-toggle").textContent = show ? "Map" : "List";
}

async function loadCategory(key) {
  setStatus("Loading…");
  try {
    const r = await fetch(`${DATA}${key}.json`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    areas = await r.json();
  } catch (e) {
    setStatus(`Data unavailable (${e.message}). Last good release: ${meta ? meta.built_at.slice(0, 10) : "unknown"}.`, true);
    return;
  }
  const source = map.getSource("areas");
  if (source) source.setData(geojson(areas));
  renderList(areas);
  renderTable(areas);
  $("card").hidden = true;
  const label = meta.categories.find((c) => c.key === key)?.label ?? key;
  setStatus(
    `${label}: ${fmtInt.format(ranked(areas).length)} municipalities scored. Method ${meta.method}, data of ${meta.built_at.slice(0, 10)}. ` +
      "Prediction registered, not yet validated against INE's business openings."
  );
  const url = new URL(location.href);
  url.searchParams.set("category", key);
  history.replaceState(null, "", url);
}

async function main() {
  try {
    const r = await fetch(`${DATA}index.json`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    meta = await r.json();
  } catch (e) {
    setStatus(`Atlas data unavailable (${e.message}).`, true);
    return;
  }
  const select = $("category");
  for (const c of meta.categories) {
    const o = document.createElement("option");
    o.value = c.key;
    o.textContent = c.label;
    select.append(o);
  }
  const wanted = params.get("category");
  select.value = meta.categories.some((c) => c.key === wanted) ? wanted : meta.categories.some((c) => c.key === "restaurant") ? "restaurant" : meta.categories[0].key;
  select.onchange = () => loadCategory(select.value);
  $("view-toggle").onclick = () => toggleList();

  map = new maplibregl.Map({
    container: "map",
    style: STYLE,
    bounds: PENINSULA,
    fitBoundsOptions: { padding: 16 },
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
  $("canarias").onclick = () => map.fitBounds(CANARIAS, { padding: 16 });
  $("peninsula").onclick = () => map.fitBounds(PENINSULA, { padding: 16 });
  map.on("load", async () => {
    map.addSource("areas", { type: "geojson", data: geojson([]) });
    map.addLayer({
      id: "areas",
      type: "circle",
      source: "areas",
      paint: {
        // circles grow with zoom: small at country scale, full size at city scale
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, ["*", ["get", "r"], 0.3], 8, ["get", "r"], 12, ["*", ["get", "r"], 1.8]],
        "circle-color": ["get", "colour"],
        "circle-opacity": 0.85,
        "circle-stroke-width": ["case", ["==", ["get", "thin"], 1], 2, 0.5],
        "circle-stroke-color": "#52606d",
      },
    });
    map.on("click", "areas", (e) => {
      const code = e.features[0].properties.code;
      const a = areas.find((x) => x.c === code);
      if (a) showCard(a);
    });
    map.on("mouseenter", "areas", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "areas", () => (map.getCanvas().style.cursor = ""));
    await loadCategory(select.value);
    document.body.dataset.ready = "1";
  });
}

main();
