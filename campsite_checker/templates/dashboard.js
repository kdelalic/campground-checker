document.addEventListener("DOMContentLoaded", () => {
  const lastUpdated = document.getElementById("last-updated");
  if (lastUpdated) {
    const ts = new Date(lastUpdated.getAttribute("data-timestamp"));
    lastUpdated.textContent = ts.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) + ' at ' + ts.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', hour12: true });
  }

  const monthSelector = document.getElementById("month-selector");
  const prevMonthBtn = document.getElementById("prev-month-btn");
  const nextMonthBtn = document.getElementById("next-month-btn");
  const clearBtn = document.getElementById("clear-date-filter");
  const allMonths = document.querySelectorAll(".calendar-month");
  const availCells = document.querySelectorAll(".calendar-available");
  const allCards = document.querySelectorAll(".card");

  function updateNavButtons() {
    if (!monthSelector) return;
    const idx = monthSelector.selectedIndex;
    if (prevMonthBtn) prevMonthBtn.disabled = idx <= 0;
    if (nextMonthBtn) nextMonthBtn.disabled = idx >= monthSelector.options.length - 1;
  }

  if (monthSelector) {
    monthSelector.addEventListener("change", (e) => {
      allMonths.forEach(m => m.style.display = "none");
      const selectedMonth = document.getElementById(e.target.value);
      if (selectedMonth) {
        selectedMonth.style.display = "block";
      }
      updateNavButtons();
    });
    updateNavButtons();
  }

  if (prevMonthBtn) {
    prevMonthBtn.addEventListener("click", () => {
      if (monthSelector.selectedIndex > 0) {
        monthSelector.selectedIndex--;
        monthSelector.dispatchEvent(new Event("change"));
      }
    });
  }

  if (nextMonthBtn) {
    nextMonthBtn.addEventListener("click", () => {
      if (monthSelector.selectedIndex < monthSelector.options.length - 1) {
        monthSelector.selectedIndex++;
        monthSelector.dispatchEvent(new Event("change"));
      }
    });
  }

  function filterByDate(dateStr) {
    allCards.forEach(card => {
      // Unavailable cards: show only when no date filter is active
      if (card.classList.contains("card-unavailable")) {
        card.style.display = dateStr ? "none" : "";
        const navItem = document.querySelector(`.quick-nav li[data-ref="${card.id}"]`);
        if (navItem) navItem.style.display = dateStr ? "none" : "";
        return;
      }

      const rows = card.querySelectorAll("tbody tr");
      let cardHasMatch = false;
      let visibleCount = 0;
      rows.forEach(row => {
        if (!dateStr || row.getAttribute("data-date") === dateStr) {
          row.style.display = "";
          cardHasMatch = true;
          visibleCount += parseInt(row.getAttribute("data-count") || "0", 10);
        } else {
          row.style.display = "none";
        }
      });
      const navItem = document.querySelector(`.quick-nav li[data-ref="${card.id}"]`);
      const siteCountSpan = card.querySelector(".site-count");
      const navCountSpan = navItem ? navItem.querySelector(".nav-count") : null;
      if (cardHasMatch) {
        card.style.display = "";
        if (navItem) navItem.style.display = "";
        if (siteCountSpan) siteCountSpan.textContent = `${visibleCount} open site(s)`;
        if (navCountSpan) navCountSpan.textContent = visibleCount;
      } else {
        card.style.display = "none";
        if (navItem) navItem.style.display = "none";
      }
    });

    availCells.forEach(cell => {
      if (dateStr && cell.getAttribute("data-date") === dateStr) {
        cell.classList.add("selected-date");
      } else {
        cell.classList.remove("selected-date");
      }
    });

    if (dateStr) {
      if(clearBtn) clearBtn.style.display = "inline-block";
    } else {
      if(clearBtn) clearBtn.style.display = "none";
    }
  }

  availCells.forEach(cell => {
    cell.addEventListener("click", () => {
      filterByDate(cell.getAttribute("data-date"));
    });
    cell.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        filterByDate(cell.getAttribute("data-date"));
      }
    });
  });

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      filterByDate(null);
    });
  }

  // The page is republished periodically; reload so an open tab does not go
  // stale forever. Background tabs wait until they are looked at again.
  // The interval comes from the rendered markup so this stays a plain, static
  // asset with nothing templated into it.
  const refreshSeconds = parseInt(document.body.getAttribute("data-refresh-seconds") || "0", 10);
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
