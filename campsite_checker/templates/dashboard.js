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
  const emptyResults = document.getElementById("empty-results");
  const allMonths = Array.from(document.querySelectorAll(".calendar-month"));
  const availButtons = Array.from(document.querySelectorAll(".calendar-available button"));
  const allCards = Array.from(document.querySelectorAll(".card"));
  const navItems = Array.from(document.querySelectorAll(".quick-nav li"));
  const defaultStatHtml = statLine ? statLine.innerHTML : "";
  const defaultSnapshotText = Object.fromEntries(
    Object.entries(snapshotFields).map(([name, element]) => [
      name,
      element ? element.textContent : "",
    ]),
  );
  const allowedStatuses = new Set(["actionable", "available", "failed", "all"]);
  const urlParams = new URLSearchParams(window.location.search);
  let activeDate = null;

  function plural(value, singular, pluralForm) {
    return `${value} ${value === 1 ? singular : pluralForm}`;
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
    const month = currentMonthValue();
    if (activeDate) params.set("date", activeDate);
    else params.delete("date");
    if (query) params.set("q", query);
    else params.delete("q");
    if (status !== "actionable") params.set("status", status);
    else params.delete("status");
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

  function applyFilters({ sync = true } = {}) {
    const query = searchInput ? searchInput.value.trim().toLowerCase() : "";
    const status = statusFilter ? statusFilter.value : "actionable";
    let visibleCards = 0;
    let visibleOpenings = 0;
    let visibleEmptyCards = 0;

    allCards.forEach((card) => {
      const cardState = card.getAttribute("data-state") || "";
      const nameMatches = (card.getAttribute("data-name") || "").includes(query);
      const stateMatches = statusMatches(cardState, status, query);
      let dateMatches = !activeDate;
      let dateOpenings = 0;

      card.querySelectorAll("tbody tr").forEach((row) => {
        const rowMatches = !activeDate || row.getAttribute("data-date") === activeDate;
        row.hidden = !rowMatches;
        if (rowMatches) {
          dateMatches = true;
          dateOpenings += parseInt(row.getAttribute("data-count") || "0", 10);
        }
      });
      if (activeDate && cardState !== "available") dateMatches = false;

      const visible = nameMatches && stateMatches && dateMatches;
      // Empty cards stay rendered inside their closed disclosure in the
      // default view so "Show checked campgrounds" remains useful.
      const visibleInsideDisclosure = (
        cardState === "empty"
        && status === "actionable"
        && !query
        && !activeDate
      );
      card.hidden = !(visible || visibleInsideDisclosure);

      const availabilityBadge = card.querySelector(".site-count-availability");
      if (availabilityBadge) {
        if (!availabilityBadge.dataset.defaultText) {
          availabilityBadge.dataset.defaultText = availabilityBadge.textContent;
        }
        availabilityBadge.textContent = activeDate && visible
          ? `${plural(dateOpenings, "opening", "openings")} on this date`
          : availabilityBadge.dataset.defaultText;
      }

      const navItem = navItems.find((item) => item.getAttribute("data-ref") === card.id);
      if (navItem) {
        const navVisible = visible && !(cardState === "empty" && status === "actionable" && !query);
        navItem.hidden = !navVisible;
        const navCount = navItem.querySelector(".nav-count");
        if (navCount) {
          if (!navCount.dataset.defaultText) navCount.dataset.defaultText = navCount.textContent;
          if (activeDate && visible && cardState === "available") {
            navCount.textContent = dateOpenings;
          } else {
            navCount.textContent = navCount.dataset.defaultText;
          }
        }
      }

      if (visible) {
        visibleCards += 1;
        visibleOpenings += activeDate
          ? dateOpenings
          : parseInt(card.getAttribute("data-openings") || "0", 10);
        if (cardState === "empty") visibleEmptyCards += 1;
      }
    });

    availButtons.forEach((button) => {
      const selected = button.getAttribute("data-date") === activeDate;
      button.classList.toggle("selected-date", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
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
        primaryDetail: `open on ${selectedDateLabel}`,
        secondaryLabel: "Open sites",
        secondaryValue: String(visibleOpenings),
        secondaryDetail: "across the selected date",
      });
    } else if (query || status !== "actionable") {
      updateSnapshot({
        primaryLabel: "Campgrounds",
        primaryValue: String(visibleCards),
        primarySuffix: "",
        primaryDetail: "match the current filters",
        secondaryLabel: "Open sites",
        secondaryValue: String(visibleOpenings),
        secondaryDetail: "across visible results",
      });
    } else {
      updateSnapshot();
    }
    if (statLine) {
      if (activeDate) {
        statLine.innerHTML = `<strong>${visibleCards}</strong> ${visibleCards === 1 ? "campground" : "campgrounds"} · <strong>${visibleOpenings}</strong> ${visibleOpenings === 1 ? "opening" : "openings"} on ${selectedDateLabel}`;
      } else if (query || status !== "actionable") {
        statLine.innerHTML = `<strong>${visibleCards}</strong> ${visibleCards === 1 ? "campground" : "campgrounds"} shown · <strong>${visibleOpenings}</strong> ${visibleOpenings === 1 ? "opening" : "openings"}`;
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
  if (emptyResults) {
    emptyResults.addEventListener("toggle", () => {
      const label = emptyResults.querySelector(".empty-toggle-label");
      if (label) label.textContent = emptyResults.open ? "Hide" : "Show";
    });
  }
  if (refreshNow) refreshNow.addEventListener("click", () => window.location.reload());

  updateFreshness();
  setInterval(updateFreshness, 60 * 1000);
  const storedDate = urlParams.get("date");
  const storedDateExists = availButtons.some(
    (button) => button.getAttribute("data-date") === storedDate,
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
