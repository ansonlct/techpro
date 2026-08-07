(() => {
  "use strict";

  const state = {
    articles: [],
    filtered: [],
    status: null,
    news: null,
    visible: 50,
    installPrompt: null,
    activeView: "inbox",
    loading: false,
    approved: new Set(),
    availableCategories: [],
    selectedCategories: new Set(),
    categorySelectionInitialized: false,
  };

  const els = {};
  const categoryColors = {
    "網騙": "#0f9d58",
    "電騙": "#1a73e8",
    "網安": "#9334e6",
    "網罪": "#00acc1",
    "平台罪案": "#f29900",
    "AI安全": "#d93025",
    "其他": "#80868b",
  };

  const $ = (id) => document.getElementById(id);
  const cacheBust = () => `v=${Date.now()}`;
  const storageGet = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const storageSet = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
  const safeUrl = (url) => {
    const value = typeof url === "string" ? url.trim() : "";
    if (!value) return "";
    try {
      // Require a genuinely absolute publisher URL. Using location.href as a
      // base turns an empty string into this GitHub Pages homepage.
      const parsed = new URL(value);
      return ["http:", "https:"].includes(parsed.protocol) && parsed.hostname ? parsed.href : "";
    } catch {
      return "";
    }
  };
  const readableUrlForExport = (url) => {
    let value = safeUrl(url);
    if (!value) return "";
    // URL.href percent-encodes non-ASCII characters. Decode URI-safe sequences
    // (including a second pass for accidentally double-encoded Chinese text)
    // while preserving reserved delimiters such as ?, &, # and /.
    for (let pass = 0; pass < 2; pass += 1) {
      try {
        const decoded = decodeURI(value);
        if (decoded === value) break;
        value = decoded;
      } catch {
        break;
      }
    }
    return value.normalize("NFC");
  };
  const formatNumber = (number) => new Intl.NumberFormat("zh-HK").format(number || 0);
  const parseDate = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const formatDate = (value) => {
    const date = value instanceof Date ? value : parseDate(value);
    if (!date) return "時間不詳";
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Hong_Kong",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(date).reduce((result, part) => {
      if (part.type !== "literal") result[part.type] = part.value;
      return result;
    }, {});
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  };
  const formatClock = (value) => {
    const date = value instanceof Date ? value : parseDate(value);
    if (!date) return "—";
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Hong_Kong",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(date).reduce((result, part) => {
      if (part.type !== "literal") result[part.type] = part.value;
      return result;
    }, {});
    return `${parts.hour}:${parts.minute}`;
  };
  const relativeAge = (value) => {
    const date = parseDate(value);
    if (!date) return "時間不詳";
    const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    if (minutes < 60) return `${minutes}分鐘前`;
    return `${Math.floor(minutes / 60)}小時前`;
  };
  const formatArticleTime = (value) => `${formatDate(value)} (${relativeAge(value)})`;
  const articleTimeText = (value) => matchMedia("(max-width: 720px)").matches
    ? `(${relativeAge(value)})`
    : formatArticleTime(value);
  const relativeTime = relativeAge;

  function collectElements() {
    [
      "menuButton", "sidebar", "sidebarOverlay", "installButton", "themeButton", "searchInput",
      "clearSearchButton", "filterToggleButton", "filterPanel", "freshnessBadge", "lastUpdated",
      "nextUpdate", "nextScheduleState", "sidebarNewsCount", "systemStatusDot",
      "totalCount", "sourceCount", "categoryCount", "feedHealth", "feedHealthText", "categoryFilter",
      "sourceFilter", "timeFilter", "sortFilter", "resetButton", "resultCount", "activeFilterLabel",
      "errorBanner", "newsList", "loadMoreButton", "categoryBars", "sourceBars", "dataVersion",
      "scheduleState", "healthyFeeds", "failedFeeds", "urlResolution", "urlResolutionText", "statusToggle", "statusDetails", "footerVersion",
      "articleTemplate", "refreshDataButton", "toast", "inboxView", "riskView", "keywordsView",
      "systemView", "approveAllCheckbox", "downloadApprovedButton", "approvedCount", "approvedToolbarCount",
      "keywordCount", "keywordSummary", "keywordList", "editKeywordsLink", "keywordRepoHint",
      "monitoredSourceSummary", "monitoredSourceList",
    ].forEach((id) => { els[id] = $(id); });
  }

  function initTheme() {
    const saved = storageGet("risk-monitor-theme");
    document.documentElement.dataset.theme = saved === "dark" ? "dark" : "light";
    els.themeButton.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      document.querySelector('meta[name="theme-color"]')?.setAttribute("content", next === "dark" ? "#171b20" : "#f8fafd");
      storageSet("risk-monitor-theme", next);
    });
  }


  function setActiveView(view) {
    const target = ["inbox", "risk", "keywords", "system"].includes(view) ? view : "inbox";
    state.activeView = target;
    document.querySelectorAll(".view-panel").forEach((panel) => {
      const active = panel.dataset.view === target;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
    document.querySelectorAll(".sidebar-nav [data-view-target]").forEach((button) => {
      const active = button.dataset.viewTarget === target;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    closeMobileSidebar();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function toggleSidebar() {
    if (matchMedia("(max-width: 980px)").matches) {
      const open = document.body.classList.toggle("sidebar-open");
      els.menuButton.setAttribute("aria-expanded", String(open));
      els.sidebarOverlay.setAttribute("aria-hidden", String(!open));
    } else {
      const collapsed = document.body.classList.toggle("sidebar-collapsed");
      els.menuButton.setAttribute("aria-expanded", String(!collapsed));
    }
  }

  function closeMobileSidebar() {
    document.body.classList.remove("sidebar-open");
    els.menuButton.setAttribute("aria-expanded", "false");
    els.sidebarOverlay.setAttribute("aria-hidden", "true");
  }

  function toggleFilters(force) {
    const shouldOpen = typeof force === "boolean" ? force : els.filterPanel.classList.contains("hidden");
    els.filterPanel.classList.toggle("hidden", !shouldOpen);
    els.filterToggleButton.classList.toggle("active", shouldOpen);
    els.filterToggleButton.setAttribute("aria-expanded", String(shouldOpen));
  }

  async function fetchJson(path) {
    const response = await fetch(`${path}?${cacheBust()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  async function loadData({ manual = false, silent = false } = {}) {
    if (state.loading) return false;
    state.loading = true;
    const previousGeneratedAt = state.news?.generated_at || "";
    els.refreshDataButton.disabled = true;
    els.refreshDataButton.classList.add("refreshing");
    try {
      const [news, status] = await Promise.all([
        fetchJson("./data/news.json"),
        fetchJson("./data/status.json"),
      ]);
      state.news = news;
      state.articles = Array.isArray(news.articles) ? news.articles : [];
      state.status = status;
      const validArticleIds = new Set(state.articles.map(articleKey));
      state.approved = new Set([...state.approved].filter((id) => validArticleIds.has(id)));
      updateSummary(news, status);
      populateFilters();
      renderDistributionBars(els.categoryBars, news.summary?.categories || {}, "category");
      renderDistributionBars(els.sourceBars, news.summary?.sources || {}, "source", 12);
      applyFilters();
      renderStatusDetails();
      renderKeywords(status);
      els.newsList.setAttribute("aria-busy", "false");
      els.errorBanner.classList.add("hidden");
      const changed = Boolean(previousGeneratedAt && news.generated_at !== previousGeneratedAt);
      if (manual) showToast(changed ? `已載入最新資料：${formatNumber(news.total)} 篇新聞` : "已重新載入，目前顯示的是最新已發布資料");
      if (changed && !manual) showToast("新聞資料已完成更新");
      return changed;
    } catch (error) {
      console.error(error);
      els.newsList.setAttribute("aria-busy", "false");
      if (!silent && !state.articles.length) els.newsList.replaceChildren(emptyState("暫時無法讀取新聞資料。請稍後重新整理。"));
      els.errorBanner.textContent = `資料載入失敗：${error.message}`;
      els.errorBanner.classList.remove("hidden");
      els.freshnessBadge.textContent = "資料讀取失敗";
      els.freshnessBadge.className = "status-pill error";
      if (manual) showToast("暫時無法更新，請稍後再試", true);
      return false;
    } finally {
      state.loading = false;
      els.refreshDataButton.disabled = false;
      els.refreshDataButton.classList.remove("refreshing");
    }
  }

  function updateSummary(news, status) {
    const sources = Object.keys(news.summary?.sources || {});
    const categories = Object.keys(news.summary?.categories || {});
    els.totalCount.textContent = formatNumber(news.total);
    els.sidebarNewsCount.textContent = formatNumber(news.total);
    els.sourceCount.textContent = formatNumber(sources.length);
    els.categoryCount.textContent = formatNumber(categories.length);
    els.lastUpdated.textContent = `最後更新：${formatDate(news.generated_at)}（${relativeTime(news.generated_at)}）`;
    els.dataVersion.textContent = news.version || "—";
    els.footerVersion.textContent = news.version || "VERSION 1";

    const generatedAt = parseDate(news.generated_at);
    const ageMinutes = generatedAt ? (Date.now() - generatedAt.getTime()) / 60000 : Infinity;
    if (ageMinutes <= 90) {
      els.freshnessBadge.textContent = "資料正常";
      els.freshnessBadge.className = "status-pill fresh";
    } else if (ageMinutes <= 240) {
      els.freshnessBadge.textContent = "更新稍有延遲";
      els.freshnessBadge.className = "status-pill stale";
    } else {
      els.freshnessBadge.textContent = "資料可能已過期";
      els.freshnessBadge.className = "status-pill error";
    }

    const feedSummary = status.feed_summary || {};
    const healthy = feedSummary.healthy || 0;
    const total = feedSummary.total || 0;
    const failed = feedSummary.failed || 0;
    const percent = total ? Math.round((healthy / total) * 100) : 0;
    els.feedHealth.textContent = total ? `${percent}%` : "—";
    els.feedHealthText.textContent = total ? `${healthy} / ${total} 個來源正常` : "未有來源狀態";
    els.healthyFeeds.textContent = String(healthy);
    els.failedFeeds.textContent = String(failed);
    els.scheduleState.textContent = failed === 0 ? "正常" : failed < Math.max(2, total * .2) ? "部分失敗" : "需檢查";
    els.systemStatusDot.className = `nav-status-dot ${failed === 0 ? "ok" : failed < Math.max(2, total * .2) ? "warning" : "error"}`;

    const resolution = status.collection?.url_resolution || {};
    const attempted = Number(resolution.attempted || 0);
    const resolved = Number(resolution.resolved || 0);
    const unresolved = Number(resolution.unresolved || 0);
    const rejected = Number(resolution.rejected_source_mismatch || 0) + Number(resolution.rejected_title_mismatch || 0);
    els.urlResolution.textContent = attempted ? `${resolved}/${attempted}` : "—";
    els.urlResolutionText.textContent = attempted
      ? (unresolved
          ? `${unresolved} 條保留 Google News 網址${rejected ? `；已攔截 ${rejected} 條疑似錯配` : ""}`
          : "全部已驗證為相符的報章原文網址")
      : "本輪沒有待解析網址";
    updateScheduleDisplay();
  }

  function scheduleMinutes() {
    const values = state.status?.deployment?.schedule_minutes;
    return Array.isArray(values) && values.length
      ? values.map(Number).filter(Number.isFinite).sort((a, b) => a - b)
      : [0, 15, 30, 45];
  }

  function nextScheduledDate(now = new Date()) {
    const offsetMs = 8 * 60 * 60 * 1000;
    const hk = new Date(now.getTime() + offsetMs);
    const year = hk.getUTCFullYear();
    const month = hk.getUTCMonth();
    const day = hk.getUTCDate();
    const hour = hk.getUTCHours();
    const candidates = [];
    scheduleMinutes().forEach((minute) => candidates.push(new Date(Date.UTC(year, month, day, hour, minute, 0) - offsetMs)));
    scheduleMinutes().forEach((minute) => candidates.push(new Date(Date.UTC(year, month, day, hour + 1, minute, 0) - offsetMs)));
    return candidates.find((candidate) => candidate.getTime() > now.getTime() + 5000) || candidates[candidates.length - 1];
  }

  function updateScheduleDisplay() {
    if (!els.nextUpdate) return;
    const next = nextScheduledDate();
    const difference = Math.max(0, Math.round((next.getTime() - Date.now()) / 1000));
    const minutes = Math.floor(difference / 60);
    const seconds = difference % 60;
    const countdown = minutes > 0 ? `${minutes} 分 ${String(seconds).padStart(2, "0")} 秒後` : `${seconds} 秒後`;
    els.nextUpdate.textContent = `下一次更新：${formatClock(next)}（${countdown}）`;
    els.nextScheduleState.textContent = `${formatClock(next)}・${countdown}`;
  }

  function setOptions(select, values, label) {
    const current = select.value;
    select.replaceChildren(new Option(label, ""));
    values.forEach(([name, count]) => select.add(new Option(`${name}（${count}）`, name)));
    if ([...select.options].some((option) => option.value === current)) select.value = current;
  }

  function defaultCategorySelection(categories = state.availableCategories) {
    return new Set(categories.filter((name) => name !== "其他"));
  }

  function renderCategoryFilters(categoryEntries) {
    const names = categoryEntries.map(([name]) => name);
    const oldAvailable = new Set(state.availableCategories);
    const previous = new Set(state.selectedCategories);
    if (!state.categorySelectionInitialized) {
      state.selectedCategories = defaultCategorySelection(names);
      state.categorySelectionInitialized = true;
    } else {
      state.selectedCategories = new Set(names.filter((name) => previous.has(name)));
      names.forEach((name) => {
        if (!oldAvailable.has(name) && name !== "其他") state.selectedCategories.add(name);
      });
    }
    state.availableCategories = names;

    const fragment = document.createDocumentFragment();
    categoryEntries.forEach(([name, count]) => {
      const label = document.createElement("label");
      label.className = "category-option";
      label.title = `${name}：${count} 篇`;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = name;
      input.checked = state.selectedCategories.has(name);
      input.setAttribute("aria-label", `${input.checked ? "取消顯示" : "顯示"}${name}分類`);
      const box = document.createElement("span");
      box.className = "category-checkmark";
      box.setAttribute("aria-hidden", "true");
      const text = document.createElement("b");
      text.textContent = name;
      const number = document.createElement("small");
      number.textContent = String(count);
      label.append(input, box, text, number);
      fragment.append(label);
    });
    els.categoryFilter.replaceChildren(fragment);
  }

  function populateFilters() {
    const categoryCounts = new Map();
    const sourceCounts = new Map();
    state.articles.forEach((article) => {
      const category = article.category || "其他";
      const source = article.source || "來源不詳";
      categoryCounts.set(category, (categoryCounts.get(category) || 0) + 1);
      sourceCounts.set(source, (sourceCounts.get(source) || 0) + 1);
    });
    const categoryEntries = [...categoryCounts].sort((a, b) => {
      if (a[0] === "其他") return 1;
      if (b[0] === "其他") return -1;
      return b[1] - a[1];
    });
    renderCategoryFilters(categoryEntries);
    setOptions(els.sourceFilter, [...sourceCounts].sort((a, b) => b[1] - a[1]), "全部來源");
  }

  function categoryFilterSummary() {
    const selected = state.availableCategories.filter((name) => state.selectedCategories.has(name));
    if (selected.length === state.availableCategories.length) return "";
    if (!selected.length) return "未選分類";
    if (state.availableCategories.includes("其他")
      && selected.length === state.availableCategories.length - 1
      && !state.selectedCategories.has("其他")) return "只顯示數碼風險相關";
    return selected.length <= 3 ? selected.join("、") : `分類 ${selected.length}/${state.availableCategories.length}`;
  }

  function activeFilterText() {
    const labels = [];
    const categorySummary = categoryFilterSummary();
    if (categorySummary) labels.push(categorySummary);
    if (els.sourceFilter.value) labels.push(els.sourceFilter.value);
    if (els.timeFilter.value !== "24") labels.push(`最近 ${els.timeFilter.value} 小時`);
    if (els.searchInput.value.trim()) labels.push(`「${els.searchInput.value.trim()}」`);
    return labels.length ? labels.join("・") : "";
  }

  function applyFilters() {
    const query = els.searchInput.value.trim().toLocaleLowerCase("zh-Hant");
    const selectedCategories = state.selectedCategories;
    const source = els.sourceFilter.value;
    const hours = Number(els.timeFilter.value || 24);
    const cutoff = Date.now() - hours * 3600000;
    state.filtered = state.articles.filter((article) => {
      const haystack = `${article.title} ${article.description} ${article.source} ${article.category}`.toLocaleLowerCase("zh-Hant");
      const time = parseDate(article.published_at)?.getTime() || 0;
      return (!query || haystack.includes(query))
        && selectedCategories.has(article.category || "其他")
        && (!source || article.source === source)
        && time >= cutoff;
    });
    const sort = els.sortFilter.value;
    state.filtered.sort((a, b) => sort === "oldest"
      ? new Date(a.published_at) - new Date(b.published_at)
      : sort === "source"
        ? a.source.localeCompare(b.source, "zh-Hant") || new Date(b.published_at) - new Date(a.published_at)
        : new Date(b.published_at) - new Date(a.published_at));
    state.visible = 50;
    els.activeFilterLabel.textContent = activeFilterText();
    els.clearSearchButton.classList.toggle("hidden", !els.searchInput.value.trim());
    renderArticles();
  }

  function renderArticles() {
    const fragment = document.createDocumentFragment();
    state.filtered.slice(0, state.visible).forEach((article) => fragment.appendChild(articleNode(article)));
    els.newsList.replaceChildren(fragment.childNodes.length ? fragment : emptyState("沒有符合目前篩選條件的新聞。"));
    els.resultCount.textContent = `${Math.min(state.visible, state.filtered.length)} / ${state.filtered.length}`;
    els.loadMoreButton.classList.toggle("hidden", state.visible >= state.filtered.length);
    updateApprovalUi();
  }

  function articleKey(article) {
    return String(article.id || article.url || `${article.title}|${article.published_at}`);
  }

  function visibleArticles() {
    return state.filtered.slice(0, state.visible);
  }

  function updateApprovalUi() {
    const current = visibleArticles();
    const selectedCurrent = current.filter((article) => state.approved.has(articleKey(article))).length;
    const allCurrentSelected = current.length > 0 && selectedCurrent === current.length;
    els.approveAllCheckbox.checked = allCurrentSelected;
    els.approveAllCheckbox.indeterminate = selectedCurrent > 0 && !allCurrentSelected;
    els.approveAllCheckbox.disabled = current.length === 0;
    const count = state.approved.size;
    els.approvedCount.textContent = String(count);
    els.approvedToolbarCount.textContent = `已核准 ${count}`;
    els.downloadApprovedButton.disabled = count === 0;
    els.downloadApprovedButton.title = count ? `下載 ${count} 篇已核准文章的 TXT 清單` : "請先核准至少一篇文章";
  }

  function setArticleApproved(articleId, approved) {
    if (approved) state.approved.add(articleId);
    else state.approved.delete(articleId);
    const row = els.newsList.querySelector(`[data-article-id="${CSS.escape(articleId)}"]`);
    row?.classList.toggle("approved", approved);
    updateApprovalUi();
  }

  function toggleApproveVisible(approved) {
    visibleArticles().forEach((article) => {
      const id = articleKey(article);
      if (approved) state.approved.add(id);
      else state.approved.delete(id);
    });
    renderArticles();
    showToast(approved ? `已核准目前顯示的 ${visibleArticles().length} 篇文章` : "已取消目前顯示文章的核准");
  }

  function downloadApprovedTxt() {
    const approvedArticles = state.articles
      .filter((article) => state.approved.has(articleKey(article)))
      .sort((a, b) => new Date(b.published_at) - new Date(a.published_at));
    const blocks = approvedArticles.map((article) => {
      const title = String(article.title || "無標題").replace(/[\r\n]+/g, " ").trim();
      const url = readableUrlForExport(article.url);
      return url ? `${title}\n${url}` : "";
    }).filter(Boolean);
    if (!blocks.length) {
      showToast("請先核准至少一篇有有效網址的文章", true);
      return;
    }
    const text = `\uFEFF${blocks.join("\n\n\n")}\n`;
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `approved_news_${formatDate(new Date()).replace(/[-: ]/g, "")}.txt`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    showToast(`已下載 ${blocks.length} 篇核准文章`);
  }

  function refreshVisibleArticleTimes() {
    els.newsList.querySelectorAll("time[data-published-at]").forEach((time) => {
      const value = time.dataset.publishedAt;
      time.textContent = articleTimeText(value);
      time.title = formatArticleTime(value);
    });
  }

  function articleNode(article) {
    const node = els.articleTemplate.content.firstElementChild.cloneNode(true);
    const id = articleKey(article);
    node.dataset.articleId = id;
    const approved = state.approved.has(id);
    node.classList.toggle("approved", approved);
    const approveCheckbox = node.querySelector(".article-approve");
    approveCheckbox.checked = approved;
    approveCheckbox.dataset.articleId = id;
    approveCheckbox.setAttribute("aria-label", `核准文章：${article.title}`);
    const color = categoryColors[article.category] || categoryColors["其他"];
    node.style.setProperty("--tag-color", color);
    const source = node.querySelector(".source-name");
    source.textContent = article.source || "來源不詳";
    source.title = article.source || "來源不詳";
    const href = safeUrl(article.url);
    const titleLink = node.querySelector("h2 a");
    titleLink.textContent = article.title;
    titleLink.title = article.title;
    if (href) titleLink.href = href;
    const time = node.querySelector("time");
    time.dateTime = article.published_at;
    time.dataset.publishedAt = article.published_at;
    time.textContent = articleTimeText(article.published_at);
    time.title = formatArticleTime(article.published_at);
    if (!href) {
      titleLink.removeAttribute("href");
      titleLink.removeAttribute("target");
      titleLink.removeAttribute("rel");
      titleLink.setAttribute("aria-disabled", "true");
      titleLink.classList.add("link-unavailable");
      titleLink.title = `${article.title}（原文連結尚待確認）`;
    }
    return node;
  }

  function emptyState(text) {
    const div = document.createElement("div");
    div.className = "empty-state";
    div.textContent = text;
    return div;
  }

  function renderDistributionBars(container, summary, type, limit = 99) {
    const entries = Object.entries(summary).sort((a, b) => b[1] - a[1]).slice(0, limit);
    const max = Math.max(...entries.map(([, value]) => value), 1);
    const fragment = document.createDocumentFragment();
    entries.forEach(([name, count], index) => {
      const row = document.createElement("div");
      row.className = "distribution-row";
      row.tabIndex = 0;
      row.role = "button";
      row.dataset.filterType = type;
      row.dataset.filterValue = name;
      const color = type === "category"
        ? (categoryColors[name] || categoryColors["其他"])
        : `hsl(${210 + (index * 19) % 110} 65% 48%)`;
      row.style.setProperty("--bar-color", color);
      const label = document.createElement("div");
      label.className = "distribution-label";
      const text = document.createElement("span");
      text.textContent = name;
      const value = document.createElement("strong");
      value.textContent = count;
      label.append(text, value);
      const track = document.createElement("div");
      track.className = "distribution-track";
      const fill = document.createElement("div");
      fill.className = "distribution-fill";
      fill.style.width = `${Math.max(4, (count / max) * 100)}%`;
      track.append(fill);
      row.append(label, track);
      fragment.append(row);
    });
    container.replaceChildren(fragment.childNodes.length ? fragment : emptyState("暫無分佈資料"));
  }

  function applyDistributionFilter(row) {
    const type = row.dataset.filterType;
    const value = row.dataset.filterValue || "";
    if (type === "category") {
      state.selectedCategories = new Set([value]);
      els.categoryFilter.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
        checkbox.checked = checkbox.value === value;
      });
    }
    if (type === "source") els.sourceFilter.value = value;
    toggleFilters(true);
    setActiveView("inbox");
    applyFilters();
  }

  function renderKeywords(status) {
    const keywords = Array.isArray(status?.custom_keywords) ? status.custom_keywords : [];
    const monitoredSources = Array.isArray(status?.target_sources) ? status.target_sources : [];
    els.keywordCount.textContent = String(keywords.length);
    els.keywordSummary.textContent = `${keywords.length} 組搜尋條件`;
    els.monitoredSourceSummary.textContent = `${monitoredSources.length} 間報章／新聞來源`;

    const sourceFragment = document.createDocumentFragment();
    monitoredSources.forEach((source) => {
      const chip = document.createElement("span");
      chip.className = "keyword-chip source-chip";
      chip.textContent = typeof source === "string" ? source : (source?.name || "");
      if (chip.textContent) sourceFragment.append(chip);
    });
    els.monitoredSourceList.replaceChildren(
      sourceFragment.childNodes.length ? sourceFragment : emptyState("暫時沒有設定監察報章。")
    );

    const fragment = document.createDocumentFragment();
    keywords.forEach((keyword) => {
      const chip = document.createElement("span");
      chip.className = "keyword-chip";
      chip.textContent = keyword;
      fragment.append(chip);
    });
    els.keywordList.replaceChildren(fragment.childNodes.length ? fragment : emptyState("keywords.txt 暫時沒有啟用的監察關鍵字。"));

    const repository = String(status?.deployment?.repository || "");
    const branch = String(status?.deployment?.default_branch || "main");
    if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
      els.editKeywordsLink.href = `https://github.com/${repository}/edit/${encodeURIComponent(branch)}/keywords.txt`;
      els.editKeywordsLink.classList.remove("hidden");
      els.keywordRepoHint.classList.add("hidden");
    } else {
      els.editKeywordsLink.removeAttribute("href");
      els.editKeywordsLink.classList.add("hidden");
      els.keywordRepoHint.classList.remove("hidden");
    }
  }

  function renderStatusDetails() {
    const feeds = state.status?.feeds || [];
    const fragment = document.createDocumentFragment();
    feeds.forEach((feed) => {
      const row = document.createElement("div");
      row.className = `feed-status${feed.ok ? " ok" : ""}`;
      const dot = document.createElement("i");
      const text = document.createElement("span");
      text.textContent = feed.ok
        ? `${feed.name}・${feed.last_item_count || 0} 項`
        : `${feed.name}・${feed.error || "失敗"}`;
      text.title = text.textContent;
      row.append(dot, text);
      fragment.append(row);
    });
    els.statusDetails.replaceChildren(fragment.childNodes.length ? fragment : emptyState("暫無來源狀態"));
  }


  let toastTimer;
  function showToast(message, isError = false, duration = 3600) {
    window.clearTimeout(toastTimer);
    els.toast.textContent = message;
    els.toast.classList.toggle("error", isError);
    els.toast.classList.add("visible");
    toastTimer = window.setTimeout(() => els.toast.classList.remove("visible"), duration);
  }

  function resetFilters() {
    els.searchInput.value = "";
    state.selectedCategories = defaultCategorySelection();
    els.categoryFilter.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
      checkbox.checked = state.selectedCategories.has(checkbox.value);
    });
    els.sourceFilter.value = "";
    els.timeFilter.value = "24";
    els.sortFilter.value = "newest";
    applyFilters();
    showToast("已清除所有搜尋及篩選");
  }

  function bindEvents() {
    let searchTimer;
    const debouncedSearch = () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        setActiveView("inbox");
        applyFilters();
      }, 120);
    };

    els.searchInput.addEventListener("input", debouncedSearch);
    els.clearSearchButton.addEventListener("click", () => {
      els.searchInput.value = "";
      els.searchInput.focus();
      applyFilters();
    });
    els.filterToggleButton.addEventListener("click", () => toggleFilters());
    els.categoryFilter.addEventListener("change", (event) => {
      const checkbox = event.target.closest('input[type="checkbox"]');
      if (!checkbox) return;
      if (checkbox.checked) state.selectedCategories.add(checkbox.value);
      else state.selectedCategories.delete(checkbox.value);
      checkbox.setAttribute("aria-label", `${checkbox.checked ? "取消顯示" : "顯示"}${checkbox.value}分類`);
      setActiveView("inbox");
      applyFilters();
    });
    [els.sourceFilter, els.timeFilter, els.sortFilter]
      .forEach((element) => element.addEventListener("change", () => {
        setActiveView("inbox");
        applyFilters();
      }));
    els.resetButton.addEventListener("click", resetFilters);
    els.loadMoreButton.addEventListener("click", () => {
      state.visible += 50;
      renderArticles();
    });
    els.statusToggle.addEventListener("click", () => {
      const hidden = els.statusDetails.classList.toggle("hidden");
      els.statusToggle.textContent = hidden ? "顯示全部來源" : "收起來源狀態";
    });
    els.refreshDataButton.addEventListener("click", () => loadData({ manual: true }));
    els.approveAllCheckbox.addEventListener("change", () => toggleApproveVisible(els.approveAllCheckbox.checked));
    els.downloadApprovedButton.addEventListener("click", downloadApprovedTxt);
    els.newsList.addEventListener("change", (event) => {
      const checkbox = event.target.closest(".article-approve");
      if (!checkbox) return;
      setArticleApproved(checkbox.dataset.articleId, checkbox.checked);
    });
    els.menuButton.addEventListener("click", toggleSidebar);
    els.sidebarOverlay.addEventListener("click", closeMobileSidebar);

    document.querySelectorAll("[data-view-target]").forEach((button) => {
      button.addEventListener("click", () => setActiveView(button.dataset.viewTarget));
    });
    document.querySelectorAll(".distribution-bars").forEach((container) => {
      container.addEventListener("click", (event) => {
        const row = event.target.closest(".distribution-row");
        if (row) applyDistributionFilter(row);
      });
      container.addEventListener("keydown", (event) => {
        const row = event.target.closest(".distribution-row");
        if (row && ["Enter", " "].includes(event.key)) {
          event.preventDefault();
          applyDistributionFilter(row);
        }
      });
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
        event.preventDefault();
        els.searchInput.focus();
      }
      if (event.key === "Escape") {
        closeMobileSidebar();
        if (!els.filterPanel.classList.contains("hidden")) toggleFilters(false);
      }
    });
    window.addEventListener("resize", () => {
      if (!matchMedia("(max-width: 980px)").matches) closeMobileSidebar();
      refreshVisibleArticleTimes();
    });
    window.setInterval(updateScheduleDisplay, 1000);
    window.setInterval(refreshVisibleArticleTimes, 60000);
  }

  function initPwa() {
    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      state.installPrompt = event;
      els.installButton.classList.remove("hidden");
    });
    els.installButton.addEventListener("click", async () => {
      if (!state.installPrompt) return;
      state.installPrompt.prompt();
      await state.installPrompt.userChoice;
      state.installPrompt = null;
      els.installButton.classList.add("hidden");
    });
    if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
      navigator.serviceWorker.register("./service-worker.js").catch(console.warn);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    collectElements();
    initTheme();
    bindEvents();
    initPwa();
    setActiveView("inbox");
    updateScheduleDisplay();
    loadData();
  });
})();
