// ═══════════════════════════════════════════════════════
//  NSW FUEL OPTIMISER — app.js
//  Mirrors Flutter main.dart logic exactly, in vanilla JS
// ═══════════════════════════════════════════════════════

// Automatically use whatever IP/host the page was loaded from.
// Works on both localhost (Mac) and your phone on the same Wi-Fi.
const API_BASE = `${location.protocol}//${location.host}`;
// Read lazily so /config fetch has time to complete before Maps JS loads
function getGoogleApiKey() { return window.GOOGLE_MAPS_API_KEY || ""; }

// ─────────────────────────────────────────────────────────
// SETTINGS STORE  (mirrors SettingsStore in Dart)
// Uses localStorage instead of SharedPreferences
// ─────────────────────────────────────────────────────────
const Settings = {
  get avoidTolls()  { return localStorage.getItem("avoid_tolls") === "true"; },
  get wMoney()      { return parseFloat(localStorage.getItem("w_money")    ?? "0.5"); },
  get wTime()       { return parseFloat(localStorage.getItem("w_time")     ?? "0.5"); },
  get lPer100km()   { return parseFloat(localStorage.getItem("l_per_100km") ?? "8.0"); },
  get pickHistory() {
    try { return JSON.parse(localStorage.getItem("pick_history") ?? "[]"); }
    catch { return []; }
  },

  setAvoidTolls(v)  { localStorage.setItem("avoid_tolls", v ? "true" : "false"); },
  setLPer100km(v)   { localStorage.setItem("l_per_100km", String(v)); },

  recordPick(pick) {
    let history = this.pickHistory;
    history.push(pick);
    if (history.length > 20) history.shift();

    let moneySignal = 0, timeSignal = 0;
    for (const p of history) {
      if (p === "cheapest")     { moneySignal += 1.0; }
      else if (p === "fastest") { timeSignal  += 1.0; }
      else                      { moneySignal += 0.5; timeSignal += 0.5; }
    }

    const total = moneySignal + timeSignal;
    let wM = 0.5, wT = 0.5;
    if (total > 0) {
      const blend = 0.7;
      wM = blend * (moneySignal / total) + (1 - blend) * 0.5;
      wT = blend * (timeSignal  / total) + (1 - blend) * 0.5;
      const s = wM + wT;
      wM /= s; wT /= s;
    }

    localStorage.setItem("w_money",      String(wM));
    localStorage.setItem("w_time",       String(wT));
    localStorage.setItem("pick_history", JSON.stringify(history));
  },

  resetWeights() {
    localStorage.setItem("w_money",      "0.5");
    localStorage.setItem("w_time",       "0.5");
    localStorage.setItem("pick_history", "[]");
  },
};

// ─────────────────────────────────────────────────────────
// PAGE NAVIGATION
// ─────────────────────────────────────────────────────────
function showPage(pageId, clickedLink) {
  // Hide all pages
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.getElementById("page-" + pageId).classList.add("active");

  // Update sidebar active state
  document.querySelectorAll(".nav-link, .bnav-link").forEach(l => {
    l.classList.toggle("active", l.dataset.page === pageId);
  });

  // Initialise map on first visit (loads Maps JS lazily)
  if (pageId === "map" && !window._mapInitialised) {
    window._mapInitialised = true;
    loadGoogleMaps().then(() => initMap());
  }

  // Refresh settings display each time
  if (pageId === "settings") {
    renderSettings();
  }
}

// ─────────────────────────────────────────────────────────
// GOOGLE PLACES AUTOCOMPLETE  (proxied via backend — no Maps JS dependency)
// ─────────────────────────────────────────────────────────
let _autocompleteTimers = {};
let _userLat = -33.8688, _userLng = 151.2093; // Sydney CBD default; updated by GPS
let _myLocationCoords = null; // set when GPS fills "My Location" into origin field

function setupAutocomplete(inputId, listId) {
  const input = document.getElementById(inputId);
  const list  = document.getElementById(listId);

  input.addEventListener("input", () => {
    if (inputId === "origin-input" && input.value.trim() !== "My Location") {
      _myLocationCoords = null;
    }
    const val = input.value.trim();

    // Skip if looks like lat,lng
    if (/^-?\d+\.\d+,-?\d+\.\d+$/.test(val)) { list.classList.remove("open"); return; }
    if (val.length < 3) { list.classList.remove("open"); return; }

    clearTimeout(_autocompleteTimers[inputId]);
    _autocompleteTimers[inputId] = setTimeout(() => fetchSuggestions(val, list, input), 350);
  });

  input.addEventListener("blur", () => {
    setTimeout(() => list.classList.remove("open"), 180);
  });
}

async function fetchSuggestions(query, listEl, inputEl) {
  try {
    const params = new URLSearchParams({ q: query, lat: _userLat, lng: _userLng });
    const res = await fetch(`${API_BASE}/autocomplete?${params}`);
    if (!res.ok) return;
    const predictions = await res.json();

    listEl.innerHTML = "";
    if (!predictions.length) { listEl.classList.remove("open"); return; }

    predictions.forEach(pred => {
      const li = document.createElement("li");
      li.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z"/></svg>${pred.description}`;
      li.addEventListener("mousedown", () => {
        inputEl.value = pred.description;
        listEl.classList.remove("open");
      });
      listEl.appendChild(li);
    });
    listEl.classList.add("open");
  } catch (e) {
    console.warn("Autocomplete error:", e);
  }
}

// ─────────────────────────────────────────────────────────
// GEOLOCATION  (mirrors _getCurrentLatLng in Dart)
// ─────────────────────────────────────────────────────────
function getLocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("Geolocation is not supported by your browser."));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      pos => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      err => {
        if (err.code === err.PERMISSION_DENIED) {
          reject(new Error("Location permission denied. Please enable it in your browser settings."));
        } else if (err.code === err.POSITION_UNAVAILABLE) {
          reject(new Error("Location unavailable."));
        } else {
          reject(new Error("Could not get location: " + err.message));
        }
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );
  });
}

async function prefillLocation() {
  const btn   = document.getElementById("gps-btn");
  const input = document.getElementById("origin-input");
  btn.classList.add("loading");
  try {
    const loc = await getLocation();
    _userLat = loc.lat;
    _userLng = loc.lng;
    _myLocationCoords = `${loc.lat.toFixed(6)},${loc.lng.toFixed(6)}`;
    input.value = "My Location";
  } catch (e) {
    showFinderError(e.message);
  } finally {
    btn.classList.remove("loading");
  }
}

document.getElementById("gps-btn").addEventListener("click", prefillLocation);

// ─────────────────────────────────────────────────────────
// OPTIMISE  (mirrors _optimise in Dart)
// ─────────────────────────────────────────────────────────
async function runOptimise() {
  const originRaw = document.getElementById("origin-input").value.trim();
  const origin = (originRaw === "My Location" && _myLocationCoords) ? _myLocationCoords : originRaw;
  const dest   = document.getElementById("dest-input").value.trim();
  const litres = parseFloat(document.getElementById("litres-input").value);
  const fuel   = document.getElementById("fuel-type").value;

  hideFinderError();

  if (!origin || !dest) { showFinderError("Please enter origin and destination."); return; }
  if (!litres || litres <= 0) { showFinderError("Enter a valid litres amount."); return; }

  setFindLoading(true);
  document.getElementById("results-section").classList.add("hidden");

  try {
    const res = await fetch(`${API_BASE}/optimise`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origin,
        destination: dest,
        litres,
        fuelType: fuel,
        wMoney:   Settings.wMoney,
        wTime:    Settings.wTime,
        lPer100km: Settings.lPer100km,
      }),
    });

    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`Server error ${res.status}: ${txt}`);
    }

    const data = await res.json();
    renderResults(data);
    updateWeightBadge();

  } catch (e) {
    showFinderError(e.message + (e.message.includes("fetch") ? "\n\nIs the FastAPI server running on port 8000?" : ""));
  } finally {
    setFindLoading(false);
  }
}

function setFindLoading(on) {
  const btn     = document.getElementById("find-btn");
  const txt     = document.getElementById("find-btn-text");
  const spinner = document.getElementById("find-btn-spinner");
  btn.disabled = on;
  txt.classList.toggle("hidden", on);
  spinner.classList.toggle("hidden", !on);
}

function showFinderError(msg) {
  const el = document.getElementById("finder-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function hideFinderError() {
  document.getElementById("finder-error").classList.add("hidden");
}

// ─────────────────────────────────────────────────────────
// RENDER RESULTS
// ─────────────────────────────────────────────────────────
function renderResults(data) {
  // Baseline banner
  document.getElementById("baseline-text").textContent =
    `Your trip is ${data.baseline.km.toFixed(1)} km · ${data.baseline.minutes.toFixed(0)} min`;
  document.getElementById("baseline-sub").textContent =
    `We found ${data.candidateCount} fuel stations along your route`;

  // Cards
  const grid = document.getElementById("result-cards");
  grid.innerHTML = "";

  const cards = [
    { type: "cheapest", label: "Cheapest", emoji: "💰", color: "#00C896", station: data.cheapest },
    { type: "fastest",  label: "Fastest",  emoji: "⚡", color: "#FFB800", station: data.fastest  },
    { type: "balanced", label: "Balanced", emoji: "⚖️", color: "#3B82F6", station: data.balanced, recommended: true },
  ];

  cards.forEach(c => grid.appendChild(makeResultCard(c, data.weights)));

  document.getElementById("results-section").classList.remove("hidden");
  document.getElementById("results-section").scrollIntoView({ behavior: "smooth", block: "start" });
}

function makeResultCard({ type, label, emoji, color, station, recommended }, weights) {
  const card = document.createElement("div");
  card.className = "rec-card" + (recommended ? " recommended" : "");
  card.style.setProperty("--accent", color);

  const stationLine = station.brand
    ? `${station.brand} · ${station.name}`
    : station.name;

  card.innerHTML = `
    <div class="rec-card-header">
      <div class="rec-emoji-box" style="background:${color}1a">${emoji}</div>
      <div class="rec-title-group">
        <div class="rec-title-row">
          <span class="rec-label">${label}</span>
          ${recommended ? `<span class="rec-badge" style="background:${color}">PICK</span>` : ""}
        </div>
        <div class="rec-station">${stationLine}</div>
      </div>
      <div class="rec-price-group">
        <div class="rec-price" style="color:${color}">$${station.price.toFixed(3)}</div>
        <div class="rec-price-unit">/litre</div>
      </div>
    </div>
    <div class="rec-divider"></div>
    <div class="rec-stats">
      <div class="stat-chip" style="background:rgba(0,200,150,0.06)">
        <span class="stat-chip-label">Total cost</span>
        <span class="stat-chip-value">$${station.moneyCost.toFixed(2)}</span>
      </div>
      <div class="stat-chip" style="background:rgba(255,184,0,0.06)">
        <span class="stat-chip-label">Detour</span>
        <span class="stat-chip-value">${station.detourMin.toFixed(0)} min</span>
      </div>
      <div class="stat-chip" style="background:rgba(59,130,246,0.06)">
        <span class="stat-chip-label">Extra km</span>
        <span class="stat-chip-value">${station.detourKm.toFixed(1)} km</span>
      </div>
    </div>
    <div class="picked-confirm hidden" id="confirm-${type}" style="background:${color}1a;color:${color}">
      ✓ Preference saved! Opening navigation…
    </div>
  `;

  card.addEventListener("click", () => onCardPick(type, station, card, color));
  return card;
}

async function onCardPick(type, station, cardEl, color) {
  // Record preference
  Settings.recordPick(type);
  updateWeightBadge();
  renderSettingsPrefs();

  // Show confirm
  const confirm = cardEl.querySelector(`#confirm-${type}`);
  if (confirm) confirm.classList.remove("hidden");
  cardEl.style.borderColor = color;
  cardEl.style.borderWidth = "2px";

  // Short delay then open nav modal
  await sleep(400);
  openNavModal(station);

  // Hide confirm after 2s
  await sleep(1600);
  if (confirm) confirm.classList.add("hidden");
  cardEl.style.borderColor = "";
  cardEl.style.borderWidth = "";
}

// ─────────────────────────────────────────────────────────
// WEIGHT BADGE
// ─────────────────────────────────────────────────────────
function updateWeightBadge() {
  // Weight badge was removed from the UI — nothing to update.
}

// ─────────────────────────────────────────────────────────
// NAVIGATION MODAL  (mirrors launchNavigation in Dart)
// ─────────────────────────────────────────────────────────
function openNavModal(station) {
  const lat     = station.lat;
  const lng     = station.lng;
  const name    = station.brand || station.name;
  const address = station.address || "";

  document.getElementById("modal-station-name").textContent =
    address ? `${name}, ${address}` : name;
  document.getElementById("nav-waze").href   = `waze://?ll=${lat},${lng}&navigate=yes`;
  document.getElementById("nav-google").href = `https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}`;
  document.getElementById("nav-apple").href  = `maps://?daddr=${lat},${lng}&dirflg=d`;

  document.getElementById("nav-modal-backdrop").classList.remove("hidden");
}

function closeNavModal() {
  document.getElementById("nav-modal-backdrop").classList.add("hidden");
}

// ─────────────────────────────────────────────────────────
// MAP PAGE
// ─────────────────────────────────────────────────────────
let _map          = null;
let _mapMarkers   = [];
let _mapStations  = [];
let _mapUserMarker = null;

function initMap() {
  const defaultPos = { lat: -33.8688, lng: 151.2093 }; // Sydney CBD fallback

  _map = new google.maps.Map(document.getElementById("map-container"), {
    center: defaultPos,
    zoom: 13,
    disableDefaultUI: false,
    zoomControl: true,
    streetViewControl: false,
    mapTypeControl: false,
    fullscreenControl: false,
    styles: [
      { featureType: "poi", elementType: "labels", stylers: [{ visibility: "off" }] },
    ],
  });

  // Try to centre on user
  getLocation().then(loc => {
    _map.setCenter({ lat: loc.lat, lng: loc.lng });
    _mapUserMarker = new google.maps.Marker({
      position: { lat: loc.lat, lng: loc.lng },
      map: _map,
      icon: {
        path: google.maps.SymbolPath.CIRCLE,
        scale: 8,
        fillColor: "#00C896",
        fillOpacity: 1,
        strokeColor: "#fff",
        strokeWeight: 2,
      },
      title: "You are here",
      zIndex: 999,
    });
    fetchMapStations();
  }).catch(() => {
    fetchMapStations();
  });
}

async function fetchMapStations() {
  const radiusKm = document.getElementById("map-radius").value;
  const fuel     = document.getElementById("map-fuel-type").value;

  document.getElementById("map-loading").classList.remove("hidden");
  document.getElementById("map-error").classList.add("hidden");

  // Get centre from user marker or map centre
  let centre = _map ? _map.getCenter() : null;
  const lat  = centre ? centre.lat() : -33.8688;
  const lng  = centre ? centre.lng() : 151.2093;

  try {
    const res = await fetch(
      `${API_BASE}/stations?lat=${lat}&lng=${lng}&radius_km=${radiusKm}&fuel_type=${fuel}`
    );
    if (!res.ok) throw new Error(`Error ${res.status}: ${await res.text()}`);
    const stations = await res.json();

    _mapStations = stations;
    renderMapMarkers(stations);

    document.getElementById("map-station-count").textContent =
      `${stations.length} station${stations.length !== 1 ? "s" : ""}`;

  } catch (e) {
    const errEl = document.getElementById("map-error");
    errEl.textContent = e.message;
    errEl.classList.remove("hidden");
  } finally {
    document.getElementById("map-loading").classList.add("hidden");
  }
}

function renderMapMarkers(stations) {
  // Clear existing custom overlay markers
  _mapMarkers.forEach(m => m.setMap(null));
  _mapMarkers = [];

  if (!stations.length) return;

  const prices = stations.map(s => s.price);
  const minP   = Math.min(...prices);
  const maxP   = Math.max(...prices);

  stations.forEach(station => {
    const t = maxP > minP ? (station.price - minP) / (maxP - minP) : 0.5;
    const borderColor = interpolateColor(t);
    const brandColor  = getBrandColor(station.brand);
    const initial     = (station.brand || station.name || "?")[0].toUpperCase();
    const brandKey    = station.brand_key || "";
    const brandBg     = brandKey ? "#fff" : brandColor;
    const brandInner  = brandKey
      ? `<img src="/assets/icons/brands/${brandKey}.png" class="map-marker-logo"
             data-fallback-color="${brandColor}" data-fallback-initial="${initial}"
             onerror="onBrandLogoError(this)" alt="${initial}">`
      : initial;

    // Build custom HTML marker
    const div = document.createElement("div");
    div.className = "map-marker";
    div.innerHTML = `
      <div class="map-marker-bubble" style="border-color:${borderColor}">
        <div class="map-marker-brand" style="background:${brandBg}">${brandInner}</div>
        <div class="map-marker-price">$${station.price.toFixed(3)}</div>
      </div>
      <div class="map-marker-tail" style="border-top-color:${borderColor}"></div>
    `;

    const overlay = new google.maps.OverlayView();
    overlay.onAdd = function () {
      this.getPanes().overlayMouseTarget.appendChild(div);
    };
    overlay.draw = function () {
      const proj = this.getProjection();
      const pos  = proj.fromLatLngToDivPixel(
        new google.maps.LatLng(station.lat, station.lng)
      );
      if (pos) {
        div.style.position = "absolute";
        div.style.left = (pos.x - 55) + "px";  // ~half marker width
        div.style.top  = (pos.y - 50) + "px";  // above the point
      }
    };
    overlay.onRemove = function () {
      if (div.parentNode) div.parentNode.removeChild(div);
    };

    // Click opens nav modal
    div.addEventListener("click", () => {
      openNavModal({
        lat: station.lat,
        lng: station.lng,
        brand: station.brand,
        name: station.name,
        address: station.address,
      });
    });

    overlay.setMap(_map);
    _mapMarkers.push(overlay);
  });
}

function interpolateColor(t) {
  // green (#22C55E) → amber (#F59E0B) → red (#EF4444)
  if (t < 0.5) {
    return lerpColor([34,197,94], [245,158,11], t * 2);
  } else {
    return lerpColor([245,158,11], [239,68,68], (t - 0.5) * 2);
  }
}

function lerpColor(a, b, t) {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl= Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r},${g},${bl})`;
}

const BRAND_COLORS = {
  "bp":            "#009900",
  "shell":         "#DD1D21",
  "ampol":         "#E31837",
  "caltex":        "#E31837",
  "7-eleven":      "#1A7B30",
  "costco":        "#0033A0",
  "liberty":       "#003087",
  "united":        "#E31837",
  "coles express": "#ED1C24",
  "woolworths":    "#007A33",
  "metro petroleum":"#156D84",
  "budget petrol": "#156D84",
  "ultra petroleum":"#FF6A2B",
};

function getBrandColor(brand) {
  return BRAND_COLORS[(brand || "").toLowerCase()] ?? "#4B5563";
}

function onBrandLogoError(img) {
  img.style.display = "none";
  img.parentElement.style.background = img.dataset.fallbackColor;
  img.parentElement.textContent = img.dataset.fallbackInitial;
}

function mapFuelChanged() { fetchMapStations(); }

function mapRadiusInput(val) {
  document.getElementById("map-radius-label").textContent = `${Math.round(val)} km`;
}

// ─────────────────────────────────────────────────────────
// SETTINGS PAGE
// ─────────────────────────────────────────────────────────
function renderSettings() {
  // Tolls toggle
  document.getElementById("tolls-toggle").checked = Settings.avoidTolls;

  // Consumption
  const v = Settings.lPer100km;
  document.getElementById("consumption-input").value = v.toFixed(1);
  document.querySelectorAll(".chip").forEach(c => {
    c.classList.toggle("active", parseFloat(c.dataset.val) === v);
  });

  renderSettingsPrefs();
}

function renderSettingsPrefs() {
  const history = Settings.pickHistory;
  const detail  = document.getElementById("prefs-detail");
  const desc    = document.getElementById("prefs-desc");

  if (history.length === 0) {
    detail.classList.add("hidden");
    desc.textContent = "Start picking recommendations on the Find Fuel tab and we'll learn your preferences.";
    return;
  }

  desc.textContent = `Based on ${history.length} pick${history.length !== 1 ? "s" : ""}.`;
  detail.classList.remove("hidden");

  const wM = Settings.wMoney;
  const wT = Settings.wTime;

  document.getElementById("pref-cost-bar").style.width = `${(wM * 100).toFixed(0)}%`;
  document.getElementById("pref-time-bar").style.width = `${(wT * 100).toFixed(0)}%`;
  document.getElementById("pref-cost-pct").textContent = `${(wM * 100).toFixed(0)}%`;
  document.getElementById("pref-time-pct").textContent = `${(wT * 100).toFixed(0)}%`;

  // History chips
  const chipsEl = document.getElementById("history-chips");
  chipsEl.innerHTML = "";
  [...history].reverse().slice(0, 10).forEach(p => {
    const chip = document.createElement("span");
    chip.className = "history-chip";
    const color = p === "cheapest" ? "#00C896" : p === "fastest" ? "#FFB800" : "#3B82F6";
    chip.style.background = color + "1a";
    chip.style.color       = color;
    chip.style.border      = `1px solid ${color}4d`;
    chip.textContent = p;
    chipsEl.appendChild(chip);
  });
}

function saveTolls() {
  Settings.setAvoidTolls(document.getElementById("tolls-toggle").checked);
}

function saveConsumption() {
  const raw = document.getElementById("consumption-input").value.trim();
  const v   = parseFloat(raw);
  const err = document.getElementById("consumption-error");

  if (isNaN(v) || v <= 0 || v > 30) {
    err.textContent = "Enter a value between 1 and 30";
    err.classList.remove("hidden");
    return;
  }

  err.classList.add("hidden");
  Settings.setLPer100km(v);

  // Update chip highlight
  document.querySelectorAll(".chip").forEach(c => {
    c.classList.toggle("active", parseFloat(c.dataset.val) === v);
  });

  showToast(`Fuel consumption set to ${v.toFixed(1)} L/100km`);
}

function setConsumptionChip(chipEl) {
  const v = parseFloat(chipEl.dataset.val);
  document.getElementById("consumption-input").value = v.toFixed(1);
  saveConsumption();
}

function resetPreferences() {
  Settings.resetWeights();
  updateWeightBadge();
  renderSettingsPrefs();
  showToast("Preferences reset to 50/50");
}

// ─────────────────────────────────────────────────────────
// TOAST NOTIFICATION  (mirrors SnackBar in Flutter)
// ─────────────────────────────────────────────────────────
let _toastTimer = null;

function showToast(msg) {
  let toast = document.getElementById("app-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "app-toast";
    toast.style.cssText = `
      position:fixed; bottom:calc(80px + env(safe-area-inset-bottom, 0px)); left:50%; transform:translateX(-50%);
      background:#00C896; color:#fff; padding:12px 20px;
      border-radius:12px; font-family:'DM Sans',sans-serif;
      font-size:14px; font-weight:600; box-shadow:0 4px 16px rgba(0,0,0,0.15);
      z-index:2000; opacity:0; transition:opacity 0.2s; white-space:nowrap;
      pointer-events:none; max-width:calc(100vw - 40px); text-align:center;
    `;
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.opacity = "1";
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { toast.style.opacity = "0"; }, 2500);
}

// ─────────────────────────────────────────────────────────
// GOOGLE MAPS SCRIPT LOADER
// ─────────────────────────────────────────────────────────
function loadGoogleMaps() {
  return new Promise((resolve) => {
    if (window.google && window.google.maps) { resolve(); return; }

    window._googleMapsReady = resolve;
    const script = document.createElement("script");
    script.src = `https://maps.googleapis.com/maps/api/js?key=${getGoogleApiKey()}&libraries=places&callback=_googleMapsReady`;
    script.async = true;
    document.head.appendChild(script);
  });
}

// ─────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ─────────────────────────────────────────────────────────
// INIT
// ─────────────────────────────────────────────────────────
async function init() {
  // Autocomplete uses the backend proxy — no Maps JS needed here.
  setupAutocomplete("origin-input", "origin-suggestions");
  setupAutocomplete("dest-input",   "dest-suggestions");

  // Pre-fill location in origin field (also updates _userLat/_userLng for autocomplete bias)
  prefillLocation();

  // Render initial weight badge if history exists
  updateWeightBadge();

  // Render settings
  renderSettings();

  // Keyboard shortcut: Enter triggers search
  document.addEventListener("keydown", e => {
    if (e.key === "Enter" && document.getElementById("page-finder").classList.contains("active")) {
      if (document.activeElement.tagName !== "BUTTON") {
        runOptimise();
      }
    }
  });

  // Maps JS is only needed for the map page — load it lazily on first visit there.
}

init();