document.addEventListener("DOMContentLoaded", () => {
  const lastUpdated = document.getElementById("last-updated");
  const relativeAge = document.getElementById("relative-age");
  const staleWarning = document.getElementById("stale-warning");
  const freshnessCard = document.querySelector(".freshness-card");
  const refreshNow = document.getElementById("refresh-now");
  const statLine = document.getElementById("stat-line");
  const snapshotFields = {
    primaryLabel: document.getElementById("summary-primary-label"),
    primaryValue: document.getElementById("summary-primary-value"),
    primarySuffix: document.getElementById("summary-primary-suffix"),
    primaryDetail: document.getElementById("summary-primary-detail"),
    secondaryLabel: document.getElementById("summary-secondary-label"),
    secondaryValue: document.getElementById("summary-secondary-value"),
    secondaryDetail: document.getElementById("summary-secondary-detail"),
  };
  const resultCount = document.getElementById("result-count");
  const noFilterResults = document.getElementById("no-filter-results");
  const monthSelector = document.getElementById("month-selector");
  const prevMonthBtn = document.getElementById("prev-month-btn");
  const nextMonthBtn = document.getElementById("next-month-btn");
  const clearBtn = document.getElementById("clear-date-filter");
  const dateFilter = document.getElementById("date-filter");
  const dateFilterLabel = document.getElementById("date-filter-label");
  const searchInput = document.getElementById("campground-search");
  const statusFilter = document.getElementById("status-filter");
  const stayFilter = document.getElementById("stay-filter");
  const emptyResults = document.getElementById("empty-results");
  const emptyResultCount = document.getElementById("empty-result-count");
  const emptyResultUnit = document.getElementById("empty-result-unit");
  const mapElement = document.getElementById("campground-map");
  const mapResultCount = document.getElementById("map-result-count");
  const mapEmpty = document.getElementById("map-empty");
  const allMonths = Array.from(document.querySelectorAll(".calendar-month"));
  const availButtons = Array.from(document.querySelectorAll(".calendar-available button"));
  const allCards = Array.from(document.querySelectorAll(".card"));
  const navItems = Array.from(document.querySelectorAll(".quick-nav li"));
  const mapEntries = Array.from(document.querySelectorAll("#map-marker-data li")).map(
    (element) => ({
      cardId: element.getAttribute("data-card-id") || "",
      latitude: parseFloat(element.getAttribute("data-latitude") || ""),
      longitude: parseFloat(element.getAttribute("data-longitude") || ""),
      name: element.getAttribute("data-name") || "",
      state: element.getAttribute("data-state") || "empty",
      openings: parseInt(element.getAttribute("data-openings") || "0", 10),
      availability: element.getAttribute("data-availability") || "",
    }),
  ).filter((entry) => Number.isFinite(entry.latitude) && Number.isFinite(entry.longitude));
  const defaultStatHtml = statLine ? statLine.innerHTML : "";
  const defaultSnapshotText = Object.fromEntries(
    Object.entries(snapshotFields).map(([name, element]) => [
      name,
      element ? element.textContent : "",
    ]),
  );
  const allowedStatuses = new Set(["actionable", "available", "failed", "all"]);
  // The base style and these overrides form the campground map's editable
  // visual theme. Keep this list focused on geographic context rather than
  // availability state, which belongs to the markers below.
  const mapStylePaint = {
    background: { "background-color": "#eff0d6" },
    park: {
      "fill-color": "#bdd67f",
      "fill-opacity": 0.72,
      "fill-outline-color": "#7da44e",
    },
    park_outline: { "line-color": "#7da44e", "line-opacity": 0.72 },
    landuse_residential: { "fill-color": "#e8e8ce", "fill-opacity": 0.58 },
    landcover_wood: { "fill-color": "#5e9c53", "fill-opacity": 0.34 },
    landcover_grass: { "fill-color": "#a8ce6a", "fill-opacity": 0.48 },
    landcover_ice: { "fill-color": "#e9f2e5" },
    landuse_pitch: { "fill-color": "#92c85c" },
    water: { "fill-color": "#87bec0" },
    landcover_sand: { "fill-color": "#edd48e" },
    building: { "fill-color": "#d7cfaa", "fill-outline-color": "#b9ad84" },
    road_minor: { "line-color": "#fff9e8" },
    road_secondary_tertiary: { "line-color": "#f4d78d" },
    road_trunk_primary: { "line-color": "#efa14f" },
    road_motorway: { "line-color": "#e45934" },
  };
  const hiddenMapStyleLayers = new Set([
    "waterway_tunnel",
    "waterway_river",
    "waterway_other",
    "boundary_2",
    "boundary_3",
    "boundary_disputed",
  ]);
  const urlParams = new URLSearchParams(window.location.search);
  let activeDate = null;
  let campgroundMap = null;
  let mapMarkers = [];
  let visibleMapEntries = [];
  let mapResizeFrame = null;

  function plural(value, singular, pluralForm) {
    return `${value} ${value === 1 ? singular : pluralForm}`;
  }

  function parseNightCounts(value) {
    const counts = new Map();
    (value || "").split(",").forEach((part) => {
      const [nights, count] = part.split(":");
      if (nights && count) counts.set(nights, parseInt(count, 10));
    });
    return counts;
  }

  function selectedStay() {
    return stayFilter ? stayFilter.value : "any";
  }

  function stayLabel(stay) {
    if (stay === "any") return "";
    return `${stay}-night ${stay === "1" ? "stay" : "stays"}`;
  }

  function updateSnapshot(values = {}) {
    Object.entries(snapshotFields).forEach(([name, element]) => {
      if (element) element.textContent = values[name] ?? defaultSnapshotText[name];
    });
  }

  function formatExactDate(timestamp) {
    return timestamp.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    }) + " at " + timestamp.toLocaleTimeString(undefined, {
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatAge(seconds) {
    if (seconds < 45) return "just now";
    if (seconds < 3600) {
      const minutes = Math.floor(seconds / 60);
      return `${plural(minutes, "minute", "minutes")} ago`;
    }
    if (seconds < 86400) {
      const hours = Math.floor(seconds / 3600);
      return `${plural(hours, "hour", "hours")} ago`;
    }
    const days = Math.floor(seconds / 86400);
    return `${plural(days, "day", "days")} ago`;
  }

  function updateFreshness() {
    if (!lastUpdated) return;
    const timestamp = new Date(lastUpdated.getAttribute("data-timestamp"));
    if (Number.isNaN(timestamp.getTime())) return;
    lastUpdated.textContent = formatExactDate(timestamp);
    const ageSeconds = Math.max(0, Math.floor((Date.now() - timestamp.getTime()) / 1000));
    if (relativeAge) relativeAge.textContent = formatAge(ageSeconds);
    const staleAfter = parseInt(
      document.body.getAttribute("data-stale-after-seconds") || "0",
      10,
    );
    const isStale = staleAfter > 0 && ageSeconds >= staleAfter;
    if (staleWarning) staleWarning.hidden = !isStale;
    if (freshnessCard) freshnessCard.classList.toggle("is-stale", isStale);
  }

  function currentMonthValue() {
    if (!monthSelector || monthSelector.selectedIndex < 0) return null;
    return monthSelector.options[monthSelector.selectedIndex].getAttribute("data-month");
  }

  function syncUrl() {
    const params = new URLSearchParams(window.location.search);
    const query = searchInput ? searchInput.value.trim() : "";
    const status = statusFilter ? statusFilter.value : "actionable";
    const stay = selectedStay();
    const month = currentMonthValue();
    if (activeDate) params.set("date", activeDate);
    else params.delete("date");
    if (query) params.set("q", query);
    else params.delete("q");
    if (status !== "actionable") params.set("status", status);
    else params.delete("status");
    if (stay !== "any") params.set("nights", stay);
    else params.delete("nights");
    if (month) params.set("month", month);
    else params.delete("month");
    const nextUrl = `${window.location.pathname}${params.toString() ? `?${params}` : ""}${window.location.hash}`;
    try {
      window.history.replaceState(null, "", nextUrl);
    } catch (_error) {
      // Some local file viewers disallow History API updates. Filtering still
      // works; only state persistence is unavailable in that environment.
    }
  }

  function updateNavButtons() {
    if (!monthSelector) return;
    const index = monthSelector.selectedIndex;
    if (prevMonthBtn) prevMonthBtn.disabled = index <= 0;
    if (nextMonthBtn) nextMonthBtn.disabled = index >= monthSelector.options.length - 1;
  }

  function showSelectedMonth(sync = true) {
    if (!monthSelector) return;
    allMonths.forEach((month) => {
      month.hidden = month.id !== monthSelector.value;
    });
    updateNavButtons();
    if (sync) syncUrl();
  }

  function selectMonth(yearMonth, sync = true) {
    if (!monthSelector || !yearMonth) return false;
    const option = Array.from(monthSelector.options).find(
      (candidate) => candidate.getAttribute("data-month") === yearMonth,
    );
    if (!option) return false;
    monthSelector.value = option.value;
    showSelectedMonth(sync);
    return true;
  }

  function statusMatches(cardState, status, query) {
    if (status === "all") return true;
    if (status === "available") return cardState === "available";
    if (status === "failed") return cardState === "failed";
    // A name search should still find a confirmed-empty campground. Without
    // a search, the default view stays focused on actionable results.
    return cardState !== "empty" || Boolean(query);
  }

  function mapEntryStatus(entry) {
    if (entry.state === "available") {
      const card = document.getElementById(entry.cardId);
      const badge = card ? card.querySelector(".site-count-availability") : null;
      return badge ? badge.textContent : entry.availability;
    }
    if (entry.state === "failed") return "Failed or stale scan";
    return "No availability";
  }

  function popupContent(entries) {
    const list = document.createElement("ul");
    list.className = "map-popup-list";
    entries.forEach((entry) => {
      const item = document.createElement("li");
      item.className = "map-popup-item";
      const link = document.createElement("a");
      link.className = "map-popup-link";
      link.href = `#${entry.cardId}`;
      link.textContent = entry.name;
      const status = document.createElement("span");
      status.className = "map-popup-status";
      status.textContent = mapEntryStatus(entry);
      item.append(link, status);
      list.append(item);
    });
    return list;
  }

  function groupMarkerState(entries) {
    if (entries.some((entry) => entry.state === "available")) return "available";
    if (entries.some((entry) => entry.state === "failed")) return "failed";
    return "empty";
  }

  function fitMapToEntries(entries) {
    if (!campgroundMap || entries.length === 0) return;
    const points = entries.map((entry) => [entry.longitude, entry.latitude]);
    if (points.length === 1) {
      campgroundMap.jumpTo({ center: points[0], zoom: 13 });
      return;
    }
    const bounds = points.reduce(
      (currentBounds, point) => currentBounds.extend(point),
      new window.maplibregl.LngLatBounds(points[0], points[0]),
    );
    campgroundMap.fitBounds(bounds, {
      duration: 0,
      padding: 42,
      maxZoom: 14,
    });
  }

  function syncMapMarkers() {
    if (!campgroundMap || !window.maplibregl) return;
    visibleMapEntries = mapEntries.filter((entry) => {
      const card = document.getElementById(entry.cardId);
      return card && card.dataset.mapVisible === "true";
    });

    mapMarkers.forEach((marker) => marker.remove());
    mapMarkers = [];
    const groups = new Map();
    visibleMapEntries.forEach((entry) => {
      // Provider coordinates occasionally repeat for multiple campground
      // facilities. Keep every campground accessible in one shared popup
      // instead of stacking indistinguishable markers.
      const key = `${entry.latitude.toFixed(5)},${entry.longitude.toFixed(5)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entry);
    });

    groups.forEach((entries) => {
      const state = groupMarkerState(entries);
      const count = entries.length > 1 ? String(entries.length) : "";
      const title = entries.length === 1
        ? `${entries[0].name}: ${mapEntryStatus(entries[0])}`
        : `${entries.length} campgrounds at this location`;
      const markerElement = document.createElement("button");
      markerElement.className = "map-marker-container";
      markerElement.type = "button";
      markerElement.title = title;
      markerElement.setAttribute("aria-label", title);
      const markerDot = document.createElement("span");
      markerDot.className = `map-marker-dot map-marker-${state}`;
      markerDot.textContent = count;
      markerElement.append(markerDot);

      const popup = new window.maplibregl.Popup({
        closeButton: true,
        closeOnClick: true,
        maxWidth: "310px",
        offset: 18,
      }).setDOMContent(popupContent(entries));
      const marker = new window.maplibregl.Marker({
        element: markerElement,
        anchor: "center",
      });
      marker
        .setLngLat([entries[0].longitude, entries[0].latitude])
        .setPopup(popup)
        .addTo(campgroundMap);
      mapMarkers.push(marker);
    });

    if (mapResultCount) {
      mapResultCount.textContent = `${plural(
        visibleMapEntries.length,
        "campground",
        "campgrounds",
      )} mapped`;
    }
    if (mapEmpty) mapEmpty.hidden = visibleMapEntries.length > 0;

    window.requestAnimationFrame(() => {
      campgroundMap.resize();
      fitMapToEntries(visibleMapEntries);
    });
  }

  function initializeMap() {
    if (!mapElement || !window.maplibregl || mapEntries.length === 0) return;
    campgroundMap = new window.maplibregl.Map({
      container: mapElement,
      style: mapElement.getAttribute("data-style-url"),
      center: [0, 0],
      zoom: 2,
      minZoom: 2,
      maxZoom: 18,
      cooperativeGestures: true,
      attributionControl: false,
    });
    campgroundMap.addControl(
      new window.maplibregl.NavigationControl({ showCompass: false }),
      "top-left",
    );
    campgroundMap.addControl(
      new window.maplibregl.AttributionControl({ compact: true }),
      "bottom-right",
    );
    campgroundMap.on("style.load", () => {
      Object.entries(mapStylePaint).forEach(([layerId, properties]) => {
        if (!campgroundMap.getLayer(layerId)) return;
        Object.entries(properties).forEach(([property, value]) => {
          campgroundMap.setPaintProperty(layerId, property, value);
        });
      });
      hiddenMapStyleLayers.forEach((layerId) => {
        if (campgroundMap.getLayer(layerId)) {
          campgroundMap.setLayoutProperty(layerId, "visibility", "none");
        }
      });
    });
    fitMapToEntries(mapEntries);

    if (window.ResizeObserver) {
      const observer = new ResizeObserver(() => {
        if (mapResizeFrame !== null) window.cancelAnimationFrame(mapResizeFrame);
        mapResizeFrame = window.requestAnimationFrame(() => {
          campgroundMap.resize();
          fitMapToEntries(visibleMapEntries);
          mapResizeFrame = null;
        });
      });
      observer.observe(mapElement);
    }
  }

  function applyFilters({ sync = true } = {}) {
    const query = searchInput ? searchInput.value.trim().toLowerCase() : "";
    const status = statusFilter ? statusFilter.value : "actionable";
    const stay = selectedStay();
    const hasStayFilter = stay !== "any";
    let visibleCards = 0;
    let visibleOpenings = 0;
    let visibleEmptyCards = 0;

    allCards.forEach((card) => {
      const cardState = card.getAttribute("data-state") || "";
      const nameMatches = (card.getAttribute("data-name") || "").includes(query);
      const stateMatches = statusMatches(cardState, status, query);
      const searchedNights = (card.getAttribute("data-search-nights") || "").split(",");
      const searchedStayMatches = !hasStayFilter || searchedNights.includes(stay);
      let resultMatches = !activeDate && !hasStayFilter;
      let dateOpenings = 0;

      card.querySelectorAll("tbody tr").forEach((row) => {
        const dateMatches = !activeDate || row.getAttribute("data-date") === activeDate;
        const rowNights = (row.getAttribute("data-nights") || "").split(",");
        const stayMatches = !hasStayFilter || rowNights.includes(stay);
        const rowMatches = dateMatches && stayMatches;
        row.hidden = !rowMatches;
        row.querySelectorAll(".stay-option, .available-badge[data-nights]").forEach((option) => {
          option.hidden = hasStayFilter && option.getAttribute("data-nights") !== stay;
        });
        if (rowMatches) {
          resultMatches = true;
          const nightCounts = parseNightCounts(row.getAttribute("data-night-counts"));
          dateOpenings += hasStayFilter
            ? nightCounts.get(stay) || 0
            : parseInt(row.getAttribute("data-count") || "0", 10);
        }
      });
      if (!card.querySelector("tbody tr")) {
        resultMatches = !activeDate && searchedStayMatches;
      }
      if (activeDate && cardState !== "available") resultMatches = false;

      const visible = nameMatches && stateMatches && resultMatches;
      // Empty cards stay rendered inside their closed disclosure in the
      // default view so "Show checked campgrounds" remains useful.
      const visibleInsideDisclosure = (
        cardState === "empty"
        && status === "actionable"
        && !query
        && !activeDate
        && searchedStayMatches
      );
      card.hidden = !(visible || visibleInsideDisclosure);
      // Confirmed-empty cards remain part of the default collapsed results,
      // so keep their campground markers visible until a date, name, or
      // explicit status filter excludes them.
      card.dataset.mapVisible = visible || visibleInsideDisclosure ? "true" : "false";

      const availabilityBadge = card.querySelector(".site-count-availability");
      if (availabilityBadge) {
        if (!availabilityBadge.dataset.defaultText) {
          availabilityBadge.dataset.defaultText = availabilityBadge.textContent;
        }
        const filtered = activeDate || hasStayFilter;
        const context = [
          activeDate ? "on this date" : "",
          hasStayFilter ? `for ${stayLabel(stay)}` : "",
        ].filter(Boolean).join(" ");
        availabilityBadge.textContent = filtered && visible
          ? `${plural(dateOpenings, "opening", "openings")} ${context}`
          : availabilityBadge.dataset.defaultText;
      }

      const navItem = navItems.find((item) => item.getAttribute("data-ref") === card.id);
      if (navItem) {
        const navVisible = visible && !(cardState === "empty" && status === "actionable" && !query);
        navItem.hidden = !navVisible;
        const navCount = navItem.querySelector(".nav-count");
        if (navCount) {
          if (!navCount.dataset.defaultText) navCount.dataset.defaultText = navCount.textContent;
          if ((activeDate || hasStayFilter) && visible && cardState === "available") {
            navCount.textContent = dateOpenings;
          } else {
            navCount.textContent = navCount.dataset.defaultText;
          }
        }
      }

      if (visible) {
        visibleCards += 1;
        visibleOpenings += activeDate || hasStayFilter
          ? dateOpenings
          : parseInt(card.getAttribute("data-openings") || "0", 10);
      }
      if ((visible || visibleInsideDisclosure) && cardState === "empty") visibleEmptyCards += 1;
    });

    availButtons.forEach((button) => {
      const nightCounts = parseNightCounts(button.getAttribute("data-night-counts"));
      const count = hasStayFilter
        ? nightCounts.get(stay) || 0
        : parseInt(button.getAttribute("data-count") || "0", 10);
      const countElement = button.querySelector(".calendar-count");
      if (countElement) countElement.textContent = String(count);
      button.disabled = hasStayFilter && count === 0;
      const selected = button.getAttribute("data-date") === activeDate;
      button.classList.toggle("selected-date", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.setAttribute(
        "aria-label",
        `${button.getAttribute("data-label")} — ${plural(count, "site", "sites")} available${hasStayFilter ? ` for ${stayLabel(stay)}` : ""}`,
      );
    });

    const selectedButton = availButtons.find(
      (button) => button.getAttribute("data-date") === activeDate,
    );
    const selectedDateLabel = selectedButton
      ? selectedButton.getAttribute("data-label") || activeDate
      : activeDate;
    if (dateFilterLabel) {
      dateFilterLabel.textContent = selectedDateLabel || "";
    }
    if (dateFilter) dateFilter.hidden = !activeDate;

    if (emptyResults) {
      const showEmptySummary = (
        !activeDate
        && (
          status === "actionable"
          || status === "all"
          || (Boolean(query) && visibleEmptyCards > 0)
        )
      );
      emptyResults.hidden = !showEmptySummary;
      if (status === "all" || query) emptyResults.open = visibleEmptyCards > 0;
    }
    if (emptyResultCount) emptyResultCount.textContent = String(visibleEmptyCards);
    if (emptyResultUnit) {
      emptyResultUnit.textContent = visibleEmptyCards === 1 ? "campground" : "campgrounds";
    }

    syncMapMarkers();
    if (resultCount) resultCount.textContent = `${plural(visibleCards, "campground", "campgrounds")} shown`;
    if (noFilterResults) {
      const emptySummaryVisible = emptyResults && !emptyResults.hidden;
      noFilterResults.hidden = visibleCards > 0 || emptySummaryVisible;
    }
    if (activeDate) {
      updateSnapshot({
        primaryLabel: "Campgrounds",
        primaryValue: String(visibleCards),
        primarySuffix: "",
        primaryDetail: `open on ${selectedDateLabel}${hasStayFilter ? ` for ${stayLabel(stay)}` : ""}`,
        secondaryLabel: "Open sites",
        secondaryValue: String(visibleOpenings),
        secondaryDetail: hasStayFilter ? `matching ${stayLabel(stay)}` : "across the selected date",
      });
    } else if (query || status !== "actionable" || hasStayFilter) {
      updateSnapshot({
        primaryLabel: "Campgrounds",
        primaryValue: String(visibleCards),
        primarySuffix: "",
        primaryDetail: hasStayFilter ? `offer ${stayLabel(stay)}` : "match the current filters",
        secondaryLabel: "Open sites",
        secondaryValue: String(visibleOpenings),
        secondaryDetail: hasStayFilter ? `matching ${stayLabel(stay)}` : "across visible results",
      });
    } else {
      updateSnapshot();
    }
    if (statLine) {
      if (activeDate) {
        statLine.innerHTML = `<strong>${visibleCards}</strong> ${visibleCards === 1 ? "campground" : "campgrounds"} · <strong>${visibleOpenings}</strong> ${visibleOpenings === 1 ? "opening" : "openings"} on ${selectedDateLabel}${hasStayFilter ? ` for ${stayLabel(stay)}` : ""}`;
      } else if (query || status !== "actionable" || hasStayFilter) {
        statLine.innerHTML = `<strong>${visibleCards}</strong> ${visibleCards === 1 ? "campground" : "campgrounds"} shown · <strong>${visibleOpenings}</strong> ${visibleOpenings === 1 ? "opening" : "openings"}${hasStayFilter ? ` matching ${stayLabel(stay)}` : ""}`;
      } else {
        statLine.innerHTML = defaultStatHtml;
      }
    }
    if (sync) syncUrl();
  }

  function filterByDate(dateString, sync = true) {
    activeDate = dateString || null;
    if (activeDate) selectMonth(activeDate.slice(0, 7), false);
    applyFilters({ sync });
  }

  if (monthSelector) {
    monthSelector.addEventListener("change", () => showSelectedMonth());
    const storedMonth = urlParams.get("month");
    if (!selectMonth(storedMonth, false)) showSelectedMonth(false);
  }
  if (prevMonthBtn) {
    prevMonthBtn.addEventListener("click", () => {
      if (monthSelector && monthSelector.selectedIndex > 0) {
        monthSelector.selectedIndex -= 1;
        showSelectedMonth();
      }
    });
  }
  if (nextMonthBtn) {
    nextMonthBtn.addEventListener("click", () => {
      if (monthSelector && monthSelector.selectedIndex < monthSelector.options.length - 1) {
        monthSelector.selectedIndex += 1;
        showSelectedMonth();
      }
    });
  }
  availButtons.forEach((button) => {
    button.addEventListener("click", () => filterByDate(button.getAttribute("data-date")));
  });
  if (clearBtn) clearBtn.addEventListener("click", () => filterByDate(null));
  if (searchInput) {
    searchInput.value = urlParams.get("q") || "";
    searchInput.addEventListener("input", () => applyFilters());
  }
  if (statusFilter) {
    const storedStatus = urlParams.get("status");
    statusFilter.value = allowedStatuses.has(storedStatus) ? storedStatus : "actionable";
    statusFilter.addEventListener("change", () => applyFilters());
  }
  if (stayFilter) {
    const storedStay = urlParams.get("nights");
    const validStay = Array.from(stayFilter.options).some(
      (option) => option.value === storedStay,
    );
    stayFilter.value = validStay ? storedStay : "any";
    stayFilter.addEventListener("change", () => {
      if (activeDate) {
        const selectedButton = availButtons.find(
          (button) => button.getAttribute("data-date") === activeDate,
        );
        const selectedCount = selectedButton
          ? parseNightCounts(selectedButton.getAttribute("data-night-counts")).get(selectedStay()) || 0
          : 0;
        if (selectedStay() !== "any" && selectedCount === 0) activeDate = null;
      }
      applyFilters();
    });
  }
  if (emptyResults) {
    emptyResults.addEventListener("toggle", () => {
      const label = emptyResults.querySelector(".empty-toggle-label");
      if (label) label.textContent = emptyResults.open ? "Hide" : "Show";
    });
  }
  if (refreshNow) refreshNow.addEventListener("click", () => window.location.reload());

  initializeMap();
  updateFreshness();
  setInterval(updateFreshness, 60 * 1000);
  const storedDate = urlParams.get("date");
  const storedDateExists = availButtons.some(
    (button) => {
      if (button.getAttribute("data-date") !== storedDate) return false;
      const stay = selectedStay();
      return stay === "any"
        || (parseNightCounts(button.getAttribute("data-night-counts")).get(stay) || 0) > 0;
    },
  );
  filterByDate(storedDateExists ? storedDate : null, false);

  // The page is republished periodically; reload so an open tab does not go
  // stale forever. Background tabs wait until they are looked at again. URL
  // parameters restore the selected month, date, search, and status afterward.
  const refreshSeconds = parseInt(
    document.body.getAttribute("data-refresh-seconds") || "0",
    10,
  );
  if (refreshSeconds > 0) {
    let refreshDue = false;
    setInterval(() => {
      if (document.visibilityState === "visible") {
        window.location.reload();
      } else {
        refreshDue = true;
      }
    }, refreshSeconds * 1000);
    document.addEventListener("visibilitychange", () => {
      if (refreshDue && document.visibilityState === "visible") {
        window.location.reload();
      }
    });
  }
});
