/* ==========================================================================
   ClareDEX Admin Console — app.js
   로그인 게이트 · 라우터 · 대시보드 · 회원 · 콘텐츠 · 수집기 · 블로그 초안 · 보안
   ========================================================================== */
(function () {
  "use strict";

  /* ------------------------------------------------------------- 기본 유틸 */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const BASE = location.pathname.replace(/admin(\/index\.html)?\/?$/i, "");
  const API = (path) => BASE + "api/" + path;

  const PROVIDER_LABEL = { email: "이메일", phone: "휴대폰", kakao: "카카오", google: "구글" , naver: "네이버" };
  const AUDIT_LABEL = {
    login: "로그인", login_fail: "로그인 실패", logout: "로그아웃",
    password_change: "비밀번호 변경", password_change_fail: "변경 실패",
    post_status: "글 상태 변경", post_delete: "글 삭제", collect_start: "수집 실행"
  };
  const SOURCE_LABEL = {
    fed: "연준 보도자료 RSS", sec: "SEC EDGAR", dart: "OpenDART 공시",
    ecos: "한국은행 ECOS", market: "시장 지표 API"
  };
  const SOURCE_DESC = {
    fed: "federalreserve.gov", sec: "data.sec.gov", dart: "opendart.fss.or.kr",
    ecos: "ecos.bok.or.kr", market: "업비트 · OKX · F&G · 환율"
  };
  const VIEW_META = {
    overview: { title: "대시보드", eyebrow: "Overview" },
    members: { title: "회원 관리", eyebrow: "Members" },
    posts: { title: "콘텐츠 관리", eyebrow: "Content" },
    system: { title: "수집기 · 시스템", eyebrow: "Collector" },
    blog: { title: "네이버 블로그 초안", eyebrow: "Blog desk" },
    security: { title: "보안 · 설정", eyebrow: "Security" }
  };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const num = (n) => (n == null || isNaN(n) ? "—" : Number(n).toLocaleString("ko-KR"));
  const pad = (n) => String(n).padStart(2, "0");

  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return esc(String(iso));
    const k = new Date(d.toLocaleString("en-US", { timeZone: "Asia/Seoul" }));
    return `${k.getFullYear()}.${pad(k.getMonth() + 1)}.${pad(k.getDate())} ${pad(k.getHours())}:${pad(k.getMinutes())}`;
  }
  function fmtDay(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return esc(String(iso));
    const k = new Date(d.toLocaleString("en-US", { timeZone: "Asia/Seoul" }));
    return `${k.getFullYear()}.${pad(k.getMonth() + 1)}.${pad(k.getDate())}`;
  }
  function rel(iso) {
    if (!iso) return "—";
    const then = new Date(iso).getTime();
    if (isNaN(then)) return esc(String(iso));
    const diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 60) return "방금 전";
    if (diff < 3600) return `${Math.floor(diff / 60)}분 전`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
    if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}일 전`;
    return fmtDay(iso);
  }

  class StaticMode extends Error {}
  class AuthError extends Error {}

  async function api(path, options) {
    let res;
    try {
      res = await fetch(API(path), Object.assign({ cache: "no-store", credentials: "same-origin" }, options || {}));
    } catch (e) {
      throw new StaticMode("network");
    }
    const ctype = res.headers.get("content-type") || "";
    if (!ctype.includes("json")) throw new StaticMode("not-json");
    let data = {};
    try { data = (await res.json()) || {}; } catch (e) { throw new StaticMode("bad-json"); }
    if (res.status === 401 && path !== "admin/login") {
      showLogin(data.error || "세션이 만료되었습니다. 다시 로그인해 주세요.");
      throw new AuthError(data.error || "unauthorized");
    }
    return { status: res.status, data };
  }

  const postJSON = (body) => ({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });

  function toast(message, kind) {
    const el = $("#toast");
    el.textContent = message;
    el.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = "toast"; }, 3000);
  }

  /* ------------------------------------------------------------- 상태 */
  const state = {
    view: "overview",
    overview: null,
    users: { limit: 50, offset: 0, q: "", provider: "all", status: "all", total: 0, items: [], loaded: false },
    posts: { limit: 25, offset: 0, q: "", category: "all", source: "", status: "all", total: 0, items: [], loaded: false },
    drafts: [],
    draft: null,
    draftTab: "preview",
    pollTimer: null,
    pollCount: 0,
    system: null
  };

  /* ------------------------------------------------------------- 로그인 */
  const loginView = $("#loginView");
  const appView = $("#appView");
  const sidebar = $("#sidebar");

  function showLogin(message) {
    appView.hidden = true;
    loginView.hidden = false;
    $("#loginForm").hidden = false;
    $("#staticNotice").hidden = true;
    $("#gateTitle").textContent = "운영자 로그인";
    $("#gateLead").textContent = "관리자 콘솔입니다. 운영자 비밀번호를 입력해 주세요.";
    $("#loginErr").textContent = message || "";
    const btn = $("#loginBtn");
    btn.disabled = false;
    btn.textContent = "로그인 →";
    $("#password").value = "";
    document.title = "운영자 로그인 · 클레어덱스";
    setTimeout(() => $("#password").focus(), 40);
  }

  function showStatic() {
    appView.hidden = true;
    loginView.hidden = false;
    $("#staticNotice").hidden = false;
    $("#loginForm").hidden = true;
    $("#gateTitle").textContent = "관리자 콘솔";
    $("#gateLead").textContent = "운영자 전용 화면입니다.";
    document.title = "정적 프리뷰 · 관리자 콘솔 · 클레어덱스";
  }

  $("#loginForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("#password");
    const password = input.value;
    if (!password) {
      $("#loginErr").textContent = "비밀번호를 입력해 주세요.";
      input.focus();
      return;
    }
    const btn = $("#loginBtn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 확인 중…';
    $("#loginErr").textContent = "";
    try {
      const { status, data } = await api("admin/login", postJSON({ password }));
      if (status === 200 && data.ok) {
        input.value = "";
        await enterApp();
        return;
      }
      showLogin(data.error || "비밀번호가 올바르지 않습니다.");
    } catch (e) {
      if (e instanceof StaticMode) { showStatic(); return; }
      showLogin("로그인 요청을 보내지 못했습니다. 잠시 후 다시 시도해 주세요.");
    }
  });

  async function logout() {
    try { await api("admin/logout", postJSON({})); } catch (e) { /* 화면은 어차피 게이트로 */ }
    state.overview = null;
    state.users.loaded = false;
    state.posts.loaded = false;
    state.drafts = [];
    state.draft = null;
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    showLogin("로그아웃되었습니다.");
  }
  $("#logoutBtn").addEventListener("click", logout);

  /* ------------------------------------------------------------- 라우터 */
  function go(name) {
    if (!VIEW_META[name]) name = "overview";
    state.view = name;
    $$(".view").forEach((v) => { v.hidden = v.id !== "view-" + name; });
    $$(".nav-item").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
    $("#pageTitle").textContent = VIEW_META[name].title;
    $("#pageEyebrow").textContent = VIEW_META[name].eyebrow;
    document.title = VIEW_META[name].title + " · 관리자 콘솔 · 클레어덱스";
    setSidebar(false);

    if (name === "overview") loadOverview();
    else if (name === "members") loadUsers();
    else if (name === "posts") loadPosts();
    else if (name === "system") loadSystem();
    else if (name === "blog") loadDrafts();
    else if (name === "security") loadSecurity();
  }

  window.addEventListener("hashchange", () => {
    if (appView.hidden) return;
    go(location.hash.replace(/^#/, ""));
  });

  /* 모바일 드로어: 스크림 + 닫기 버튼으로도 닫을 수 있게 */
  const sideScrim = $("#sideScrim");
  function setSidebar(open) {
    sidebar.classList.toggle("open", !!open);
    if (sideScrim) sideScrim.hidden = !open;
    const toggle = $("#menuToggle");
    if (toggle) toggle.setAttribute("aria-expanded", String(!!open));
  }
  $$(".nav-item").forEach((a) => a.addEventListener("click", () => setSidebar(false)));
  $("#menuToggle").addEventListener("click", (ev) => {
    ev.stopPropagation();
    setSidebar(!sidebar.classList.contains("open"));
  });
  if (sideScrim) sideScrim.addEventListener("click", () => setSidebar(false));
  const sideClose = $("#sideClose");
  if (sideClose) sideClose.addEventListener("click", () => setSidebar(false));
  document.addEventListener("click", (ev) => {
    if (sidebar.classList.contains("open") && !sidebar.contains(ev.target) && ev.target !== $("#menuToggle")) {
      setSidebar(false);
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && sidebar.classList.contains("open")) setSidebar(false);
  });

  function setClock() {
    const now = new Date();
    const k = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Seoul" }));
    $("#kstClock").textContent = `${pad(k.getHours())}:${pad(k.getMinutes())}:${pad(k.getSeconds())}`;
  }
  setInterval(setClock, 1000);
  setClock();

  $("#refreshBtn").addEventListener("click", () => {
    go(state.view);
    toast("최신 상태로 새로고침했습니다.");
  });

  /* ============================================================= 대시보드 */
  const ICON = {
    users: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="9" cy="8" r="3"/><path d="M3.8 19c.4-3 2.6-4.8 5.2-4.8s4.8 1.8 5.2 4.8"/><path d="M15.8 5.4a2.8 2.8 0 0 1 0 5.2M17 19c0-1.9-.5-3.4-1.5-4.5 2.3-.2 4.3 1.3 4.7 4.5"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M5 12h14"/></svg>',
    week: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 9.5h17M8 3.5v3M16 3.5v3"/></svg>',
    doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 3.5h8l4 4v13H6z"/><path d="M9 11h6M9 14.5h6M9 18h4"/></svg>',
    pen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20l1-4.5L16.5 4l3.5 3.5L8.5 19z"/><path d="M14.5 6l3.5 3.5"/></svg>',
    pulse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2.5 12.5h4l2.2-5 3.4 9 2.4-5.5h7"/></svg>'
  };

  function kpiCard(o) {
    return `<div class="kpi tone-${o.tone || "navy"}">
      <div class="kpi-label"><span>${esc(o.label)}</span>${o.icon || ""}</div>
      <div class="kpi-value${o.small ? " small" : ""}">${o.value}${o.unit ? `<small>${esc(o.unit)}</small>` : ""}</div>
      <div class="kpi-sub">${o.sub || ""}</div>
    </div>`;
  }

  async function loadOverview() {
    const grid = $("#kpiGrid");
    if (!state.overview) {
      grid.innerHTML = Array.from({ length: 6 }, () => '<div class="kpi"><div class="kpi-label">불러오는 중…</div><div class="kpi-value small">—</div></div>').join("");
    }
    let data;
    try {
      ({ data } = await api("admin/overview"));
    } catch (e) {
      if (e instanceof AuthError || e instanceof StaticMode) return;
      grid.innerHTML = '<div class="empty-state">현황을 불러오지 못했습니다. 새로고침해 주세요.</div>';
      return;
    }
    renderOverview(data);
  }

  function renderOverview(data) {
    state.overview = data;
    const k = data.kpis || {};
    const c = k.collector || {};

    $("#overviewStamp").textContent =
      `서버 시각 ${fmtDate(data.generated_at)} KST · 가입·수집 데이터를 한 화면에 정리했습니다.`;
    $("#navUsers").textContent = num(k.users_total);
    $("#navPosts").textContent = num(k.posts_total);
    $("#navCollector").className = "dot" + (c.running ? " on" : "");

    $("#kpiGrid").innerHTML = [
      kpiCard({ label: "총 회원수", value: num(k.users_total), unit: "명",
        sub: `최근 30일 <b>+${num(k.users_30d)}</b>`, tone: "navy", icon: ICON.users }),
      kpiCard({ label: "오늘 신규 가입", value: num(k.users_today), unit: "명",
        sub: `최근 7일 <b>+${num(k.users_7d)}</b>`, tone: "signal", icon: ICON.plus }),
      kpiCard({ label: "최근 7일 가입", value: num(k.users_7d), unit: "명",
        sub: "KST 자정 기준", tone: "gold", icon: ICON.week }),
      kpiCard({ label: "총 속보·공시", value: num(k.posts_total), unit: "건",
        sub: `공개 <b>${num(k.posts_published)}</b> · 숨김 ${num(k.posts_hidden)}`, tone: "navy", icon: ICON.doc }),
      kpiCard({ label: "블로그 초안", value: num(k.drafts_total), unit: "건",
        sub: `오늘 <b>+${num(k.drafts_today)}</b> · 글 ${num(k.posts_today)}건 수집`, tone: "gold", icon: ICON.pen }),
      kpiCard({
        label: "수집기 상태",
        value: c.running ? "실행 중" : (c.ok === true ? "정상" : (c.ok === false ? "실패" : "대기")),
        small: true,
        sub: c.running ? "백그라운드 실행 중"
          : (c.last_run_at ? `마지막 <b>${esc(rel(c.last_run_at))}</b>` : `최근 글 ${esc(rel(c.latest_post_at || k.latest_post_at))}`),
        tone: c.running ? "signal" : (c.ok === false ? "signal" : (c.ok === true ? "ok" : "warn")),
        icon: ICON.pulse
      })
    ].join("");

    renderChart(data.trend || []);
    renderDist($("#categoryDist"), (data.categories || []).map((x) => ({
      name: x.label, count: x.count, color: x.color
    })));
    renderDist($("#sourceDist"), (data.sources || []).map((x) => ({ name: x.name, count: x.count })));
    renderRecentUsers(data.recent_users || []);
    renderRecentPosts(data.recent_posts || []);

    const total = (data.trend || []).reduce((a, b) => a + (b.count || 0), 0);
    $("#chartMeta").textContent = `최근 14일 · 합계 ${num(total)}명`;
  }

  function renderChart(series) {
    const box = $("#signupChart");
    const n = series.length || 14;
    const w = 720, h = 240, padL = 34, padR = 12, padT = 14, padB = 28;
    const innerW = w - padL - padR, innerH = h - padT - padB;
    const max = Math.max(4, ...series.map((s) => s.count || 0));
    const bw = Math.min(30, (innerW / n) * 0.54);
    const parts = [];
    for (let i = 0; i <= 4; i++) {
      const y = padT + innerH - (innerH * i) / 4;
      parts.push(`<line class="grid-line" x1="${padL}" x2="${w - padR}" y1="${y}" y2="${y}"/>`);
      parts.push(`<text x="${padL - 7}" y="${y + 3}" text-anchor="end">${Math.round((max * i) / 4)}</text>`);
    }
    series.forEach((s, i) => {
      const x = padL + (innerW * (i + 0.5)) / n;
      const count = s.count || 0;
      const bh = count > 0 ? Math.max(5, (innerH * count) / max) : 2;
      const y = padT + innerH - bh;
      parts.push(`<rect class="bar${count ? "" : " zero"}" x="${x - bw / 2}" y="${y}" width="${bw}" height="${bh}" rx="3"><title>${esc(s.date)} · ${count}명</title></rect>`);
      if (n <= 14 || i % 2 === 0) {
        parts.push(`<text x="${x}" y="${h - 9}" text-anchor="middle">${esc(String(s.date).slice(5).replace("-", "/"))}</text>`);
      }
      if (count > 0) {
        parts.push(`<text x="${x}" y="${y - 5}" text-anchor="middle" style="fill:#4a5260">${count}</text>`);
      }
    });
    box.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="최근 14일 일별 신규 가입 막대 차트">${parts.join("")}</svg>`;
    const total = series.reduce((a, b) => a + (b.count || 0), 0);
    if (!total) {
      box.insertAdjacentHTML("beforeend",
        `<div class="chart-empty">최근 14일간 신규 가입이 없습니다. 회원가입 페이지(<b>/signup.html</b>)로 가입이 들어오면 이 차트에 일별로 쌓입니다.</div>`);
    }
  }

  function renderDist(box, items) {
    if (!items.length) {
      box.innerHTML = '<div class="empty-note">표시할 데이터가 없습니다.</div>';
      return;
    }
    const max = Math.max(1, ...items.map((i) => i.count || 0));
    box.innerHTML = items.slice(0, 8).map((it) => `
      <div class="dist-row">
        <span class="dist-name" title="${esc(it.name)}">${esc(it.name)}</span>
        <span class="dist-track"><span class="dist-fill" style="width:${Math.max(3, ((it.count || 0) / max) * 100)}%;background:${esc(it.color || "#1e3a5f")}"></span></span>
        <span class="dist-count">${num(it.count)}</span>
      </div>`).join("");
  }

  function renderRecentUsers(items) {
    const body = $("#recentUsers");
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="3"><div class="empty-state"><b>아직 가입한 회원이 없습니다</b><p>/signup.html 에서 가입하면 이곳에 즉시 표시됩니다.</p></div></td></tr>`;
      return;
    }
    body.innerHTML = items.map((u) => `
      <tr>
        <td><span class="cell-title" title="${esc(u.email || u.phone)}">${u.name ? esc(u.name) : (u.email ? esc(u.email) : esc(u.phone || "휴대폰 가입"))}</span>
            <span class="cell-sub">${u.name && (u.email || u.phone) ? esc(u.email || u.phone) + " · " : ""}${esc(PROVIDER_LABEL[u.provider] || u.provider || "")}${u.email && u.phone ? " · " + esc(u.phone) : ""}</span></td>
        <td><div class="tag-list">${(u.watchlist || []).slice(0, 3).map((t) => `<span class="tag">${esc(t)}</span>`).join("") || '<span class="cell-sub">—</span>'}${(u.watchlist || []).length > 3 ? `<span class="tag">+${u.watchlist.length - 3}</span>` : ""}</div></td>
        <td class="num">${esc(fmtDate(u.created_at))}</td>
      </tr>`).join("");
  }

  function renderRecentPosts(items) {
    const body = $("#recentPosts");
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="3"><div class="empty-state"><b>수집된 글이 없습니다</b><p>수집기 · 시스템에서 “지금 수집 실행”을 눌러 주세요.</p></div></td></tr>`;
      return;
    }
    body.innerHTML = items.map((p) => `
      <tr>
        <td><span class="cell-title" title="${esc(p.title)}">${esc(p.title)}</span>
            <span class="cell-sub">${esc(p.source_name)} · ${esc(p.source_grade)}</span></td>
        <td><span class="badge navy">${esc(p.category_label || p.category)}</span></td>
        <td class="num">${esc(fmtDate(p.verified_at))}</td>
      </tr>`).join("");
  }

  /* ============================================================= 회원 관리 */
  function renderProviderOptions(providers) {
    const sel = $("#userProvider");
    const current = sel.value;
    const opts = ['<option value="all">가입경로 전체</option>'];
    (providers || []).forEach((p) => {
      opts.push(`<option value="${esc(p.key)}">${esc(PROVIDER_LABEL[p.key] || p.key)} (${num(p.count)})</option>`);
    });
    sel.innerHTML = opts.join("");
    sel.value = current && Array.from(sel.options).some((o) => o.value === current) ? current : "all";
  }

  async function loadUsers() {
    const p = state.users;
    $("#userRows").innerHTML = '<tr><td colspan="9" class="loading">회원 목록을 불러오는 중…</td></tr>';
    const qs = new URLSearchParams({
      q: p.q, provider: p.provider, status: p.status,
      limit: String(p.limit), offset: String(p.offset)
    });
    let data;
    try { ({ data } = await api("admin/users?" + qs)); }
    catch (e) {
      if (e instanceof AuthError || e instanceof StaticMode) return;
      $("#userRows").innerHTML = `<tr><td colspan="9" class="empty-state">회원 목록을 불러오지 못했습니다.</td></tr>`;
      return;
    }
    p.total = data.total || 0;
    p.items = data.items || [];
    p.loaded = true;
    renderProviderOptions(data.providers);
    $("#userResult").textContent = `${num(p.total)}명`;
    $("#navUsers").textContent = num(p.total);

    const rows = $("#userRows");
    if (!p.items.length) {
      rows.innerHTML = `<tr><td colspan="9"><div class="empty-state">
        <b>${p.q || p.provider !== "all" || p.status !== "all" ? "조건에 맞는 회원이 없습니다" : "아직 가입한 회원이 없습니다"}</b>
        <p>${p.q || p.provider !== "all" || p.status !== "all" ? "검색어나 필터를 바꿔 보세요." : "회원가입 페이지(/signup.html)에서 가입하면 이 목록에 바로 저장됩니다."}</p>
      </div></td></tr>`;
    } else {
      rows.innerHTML = p.items.map((u) => `
        <tr>
          <td><span class="cell-title" title="${esc(u.email)}">${u.email ? esc(u.email) : '<span class="cell-sub">휴대폰 가입</span>'}</span></td>
          <td>${u.name ? esc(u.name) : '<span class="cell-sub">—</span>'}</td>
          <td>${u.phone ? `<span class="mono">${esc(u.phone)}</span>` : '<span class="cell-sub">—</span>'}</td>
          <td><div class="tag-list">
            ${u.email_verified ? '<span class="badge ok" title="이메일 인증"><span class="pip"></span>이메일</span>' : '<span class="badge muted" title="이메일 미인증"><span class="pip"></span>이메일</span>'}
            ${u.phone_verified ? '<span class="badge ok" title="휴대폰 인증"><span class="pip"></span>휴대폰</span>' : '<span class="badge muted" title="휴대폰 미인증"><span class="pip"></span>휴대폰</span>'}
          </div></td>
          <td>${u.marketing_agreed
            ? `<span class="badge ok"><span class="pip"></span>동의</span>${u.marketing_agreed_at ? `<div class="cell-sub">${esc(fmtDate(u.marketing_agreed_at))}</div>` : ""}`
            : '<span class="badge muted">미동의</span>'}</td>
          <td><div class="tag-list">${(u.watchlist || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("") || '<span class="cell-sub">미선택</span>'}</div></td>
          <td><span class="badge muted">${esc(PROVIDER_LABEL[u.provider] || u.provider || "이메일")}</span></td>
          <td class="num">${esc(fmtDate(u.created_at))}</td>
          <td><span class="badge ${u.status === "paused" ? "warn" : "ok"}"><span class="pip"></span>${u.status === "paused" ? "정지" : "활성"}</span></td>
        </tr>`).join("");
    }
    renderPager($("#userPager"), p, loadUsers);
  }

  function renderPager(box, p, reload) {
    const from = p.total === 0 ? 0 : p.offset + 1;
    const to = Math.min(p.offset + p.items.length, p.total);
    const prev = Math.max(0, p.offset - p.limit);
    const next = p.offset + p.limit;
    box.innerHTML = `
      <span>${num(from)}–${num(to)} / 전체 ${num(p.total)}</span>
      <span class="btns">
        <button class="btn btn-sm" type="button" data-page="prev" ${p.offset <= 0 ? "disabled" : ""}>← 이전</button>
        <button class="btn btn-sm" type="button" data-page="next" ${next >= p.total ? "disabled" : ""}>다음 →</button>
      </span>`;
    box.querySelectorAll("[data-page]").forEach((btn) => {
      btn.addEventListener("click", () => {
        p.offset = btn.dataset.page === "prev" ? prev : next;
        reload();
      });
    });
  }

  let userTimer = null;
  $("#userSearch").addEventListener("input", (ev) => {
    clearTimeout(userTimer);
    userTimer = setTimeout(() => {
      state.users.q = ev.target.value.trim();
      state.users.offset = 0;
      loadUsers();
    }, 280);
  });
  $("#userProvider").addEventListener("change", (ev) => {
    state.users.provider = ev.target.value;
    state.users.offset = 0;
    loadUsers();
  });
  $("#userStatus").addEventListener("change", (ev) => {
    state.users.status = ev.target.value;
    state.users.offset = 0;
    loadUsers();
  });

  function csvCell(v) {
    const s = String(v == null ? "" : v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  $("#exportCsv").addEventListener("click", async () => {
    const btn = $("#exportCsv");
    btn.disabled = true;
    btn.textContent = "내보내는 중…";
    try {
      const p = state.users;
      let offset = 0;
      const rows = [];
      while (offset < 10000) {
        const qs = new URLSearchParams({ q: p.q, provider: p.provider, status: p.status, limit: "500", offset: String(offset) });
        const { data } = await api("admin/users?" + qs);
        rows.push(...(data.items || []));
        if ((data.items || []).length < 500) break;
        offset += 500;
      }
      const lines = [["이메일", "이름", "연락처", "이메일 인증", "휴대폰 인증", "마케팅 활용 동의", "마케팅 동의 시각", "가입경로", "관심자산", "가입일", "상태"].join(",")];
      rows.forEach((u) => lines.push([
        u.email, u.name || "", u.phone || "",
        u.email_verified ? "인증" : "미인증", u.phone_verified ? "인증" : "미인증",
        u.marketing_agreed ? "동의" : "미동의", u.marketing_agreed_at || "",
        PROVIDER_LABEL[u.provider] || u.provider || "",
        (u.watchlist || []).join(" "), u.created_at, u.status === "paused" ? "정지" : "활성"
      ].map(csvCell).join(",")));
      const blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `claredex-members-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 3000);
      toast(`회원 ${rows.length}명을 CSV로 내보냈습니다.`, "ok");
    } catch (e) {
      if (!(e instanceof AuthError || e instanceof StaticMode)) toast("CSV 내보내기에 실패했습니다.", "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "CSV 내보내기";
    }
  });

  /* ============================================================= 콘텐츠 관리 */
  function renderPostFilterOptions(data) {
    const cat = $("#postCategory");
    const curCat = cat.value;
    const cats = (data.categories || []).map((c) => `<option value="${esc(c.key)}">${esc(c.label)} (${num(c.count)})</option>`);
    cat.innerHTML = '<option value="all">카테고리 전체</option>' + cats.join("");
    cat.value = Array.from(cat.options).some((o) => o.value === curCat) ? curCat : "all";

    const src = $("#postSource");
    const curSrc = src.value;
    src.innerHTML = '<option value="">출처 전체</option>' +
      (data.sources || []).map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
    src.value = Array.from(src.options).some((o) => o.value === curSrc) ? curSrc : "";
  }

  async function loadPosts() {
    const p = state.posts;
    $("#postRows").innerHTML = '<tr><td colspan="6" class="loading">글 목록을 불러오는 중…</td></tr>';
    const qs = new URLSearchParams({
      category: p.category, source: p.source, status: p.status, q: p.q,
      limit: String(p.limit), offset: String(p.offset)
    });
    let data;
    try { ({ data } = await api("admin/posts?" + qs)); }
    catch (e) {
      if (e instanceof AuthError || e instanceof StaticMode) return;
      $("#postRows").innerHTML = '<tr><td colspan="6" class="empty-state">글 목록을 불러오지 못했습니다.</td></tr>';
      return;
    }
    p.total = data.total || 0;
    p.items = data.items || [];
    p.loaded = true;
    renderPostFilterOptions(data);
    $("#postResult").textContent = `${num(p.total)}건`;
    const st = data.stats || {};
    $("#postStatsBadge").textContent = `전체 ${num(st.total)} · 공개 ${num(st.published)} · 숨김 ${num(st.hidden)}`;
    $("#navPosts").textContent = num(st.total);

    const rows = $("#postRows");
    if (!p.items.length) {
      rows.innerHTML = `<tr><td colspan="6"><div class="empty-state"><b>조건에 맞는 글이 없습니다</b><p>필터를 바꾸거나 수집기 · 시스템에서 수집을 실행해 보세요.</p></div></td></tr>`;
    } else {
      rows.innerHTML = p.items.map((post) => `
        <tr data-id="${post.id}">
          <td>
            <span class="cell-title" title="${esc(post.title)}">${esc(post.title)}</span>
            <span class="cell-sub">#${post.id} · ${esc((post.tags || []).slice(0, 3).map((t) => "#" + t).join(" "))}</span>
          </td>
          <td><span class="badge navy">${esc(post.category_label || post.category)}</span></td>
          <td><span class="cell-sub" style="font-size:11px;color:#5d6470">${esc(post.source_name)}</span><br><span class="cell-sub">${esc(post.source_grade)}</span></td>
          <td><span class="badge ${post.status === "hidden" ? "muted" : "ok"}"><span class="pip"></span>${post.status === "hidden" ? "숨김" : "공개"}</span></td>
          <td class="num">${esc(fmtDate(post.verified_at))}</td>
          <td style="text-align:right;white-space:nowrap">
            <button class="btn btn-sm" type="button" data-act="view" data-id="${post.id}">보기</button>
            <button class="btn btn-sm" type="button" data-act="toggle" data-id="${post.id}" data-status="${post.status === "hidden" ? "published" : "hidden"}">${post.status === "hidden" ? "공개" : "숨김"}</button>
            <button class="btn btn-sm btn-danger" type="button" data-act="delete" data-id="${post.id}">삭제</button>
          </td>
        </tr>`).join("");
    }
    renderPager($("#postPager"), p, loadPosts);
  }

  $("#postRows").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const post = state.posts.items.find((x) => x.id === id);
    if (!post) return;
    if (btn.dataset.act === "view") showPostModal(post);
    else if (btn.dataset.act === "toggle") togglePost(post, btn.dataset.status);
    else if (btn.dataset.act === "delete") confirmDeletePost(post);
  });

  function showPostModal(post) {
    const href = /^https?:\/\//i.test(post.source_url || "") ? post.source_url : "#";
    openModal({
      title: post.title,
      body: `
        <dl class="kv">
          <dt>카테고리</dt><dd>${esc(post.category_label || post.category)}</dd>
          <dt>출처</dt><dd>${esc(post.source_name)} · ${esc(post.source_grade)}</dd>
          <dt>확인 시각</dt><dd>${esc(fmtDate(post.verified_at))} KST</dd>
          <dt>상태</dt><dd>${post.status === "hidden" ? "숨김" : "공개"}</dd>
          <dt>태그</dt><dd>${(post.tags || []).map((t) => esc("#" + t)).join(" ") || "—"}</dd>
        </dl>
        <p style="font-weight:600;margin-bottom:6px">3줄 요약</p>
        <ul class="summary-list">${(post.summary_lines || []).map((l) => `<li>${esc(l)}</li>`).join("") || "<li>—</li>"}</ul>
        <p style="font-weight:600;margin:14px 0 6px">시장 영향</p>
        <p>${esc(post.market_impact || "—")}</p>`,
      actions: `
        <a class="btn btn-sm" href="${esc(href)}" target="_blank" rel="noopener noreferrer">원문 열기 ↗</a>
        <button class="btn btn-sm btn-primary" type="button" data-close="1">닫기</button>`
    });
  }

  async function togglePost(post, nextStatus) {
    try {
      const { status, data } = await api("admin/posts/status", postJSON({ id: post.id, status: nextStatus }));
      if (status !== 200 || !data.ok) {
        toast(data.error || "상태를 바꾸지 못했습니다.", "err");
        return;
      }
      toast(nextStatus === "hidden" ? "글을 숨김 처리했습니다. 메인·공개 API에서 제외됩니다." : "글을 다시 공개했습니다.", "ok");
      await loadPosts();
      if (state.overview) loadOverview();
    } catch (e) {
      if (!(e instanceof AuthError || e instanceof StaticMode)) toast("상태 변경에 실패했습니다.", "err");
    }
  }

  function confirmDeletePost(post) {
    openModal({
      title: "글을 삭제할까요?",
      body: `<p><b>${esc(post.title)}</b></p>
             <p>연결된 네이버 블로그 초안도 함께 삭제됩니다. 이 작업은 되돌릴 수 없습니다. 잠시 숨기려는 것이라면 <b>숨김</b>을 사용하세요.</p>`,
      actions: `<button class="btn btn-sm" type="button" data-close="1">취소</button>
                <button class="btn btn-sm btn-danger" type="button" id="confirmDelete">삭제</button>`
    });
    $("#confirmDelete").addEventListener("click", async () => {
      $("#confirmDelete").disabled = true;
      try {
        const { status, data } = await api("admin/posts/delete", postJSON({ id: post.id }));
        closeModal();
        if (status !== 200 || !data.ok) { toast(data.error || "삭제하지 못했습니다.", "err"); return; }
        toast("글을 삭제했습니다.", "ok");
        state.posts.offset = Math.max(0, state.posts.offset - (state.posts.items.length === 1 ? state.posts.limit : 0));
        await loadPosts();
        if (state.overview) loadOverview();
      } catch (e) {
        closeModal();
        if (!(e instanceof AuthError || e instanceof StaticMode)) toast("삭제에 실패했습니다.", "err");
      }
    });
  }

  let postTimer = null;
  $("#postSearch").addEventListener("input", (ev) => {
    clearTimeout(postTimer);
    postTimer = setTimeout(() => {
      state.posts.q = ev.target.value.trim();
      state.posts.offset = 0;
      loadPosts();
    }, 280);
  });
  ["postCategory", "postSource", "postStatus"].forEach((id) => {
    $("#" + id).addEventListener("change", (ev) => {
      const p = state.posts;
      if (id === "postCategory") p.category = ev.target.value;
      if (id === "postSource") p.source = ev.target.value;
      if (id === "postStatus") p.status = ev.target.value;
      p.offset = 0;
      loadPosts();
    });
  });

  /* ============================================================= 수집기 · 시스템 */
  function parseSourceStatus(value) {
    const v = String(value || "").trim();
    if (!v) return { kind: "muted", label: "기록 없음" };
    if (v.startsWith("ok")) {
      const m = v.match(/\((\d+)\)/);
      return { kind: "ok", label: m ? `정상 · ${m[1]}건` : "정상" };
    }
    if (v === "no-key") return { kind: "muted", label: "키 없음" };
    if (v.startsWith("fail")) return { kind: "signal", label: "실패" };
    return { kind: "muted", label: v.slice(0, 30) };
  }

  async function loadSystem() {
    let data;
    try { ({ data } = await api("admin/collector")); }
    catch (e) {
      if (e instanceof AuthError || e instanceof StaticMode) return;
      toast("수집기 상태를 불러오지 못했습니다.", "err");
      return;
    }
    let security = null;
    try { security = (await api("admin/security")).data; } catch (e) { /* 선택 정보 */ }
    state.system = data;
    state.security = security;
    renderSystem(data, security);
  }

  function renderSystem(d, security) {
    const last = d.last || {};
    const running = !!d.running;
    const badge = $("#collectorBadge");
    if (running) {
      badge.className = "badge signal";
      badge.innerHTML = '<span class="spinner" style="border-color:rgba(179,38,30,.3);border-top-color:#b3261e"></span> 실행 중';
    } else if (last.ok === true) {
      badge.className = "badge ok";
      badge.innerHTML = '<span class="pip"></span> 정상';
    } else if (last.ok === false) {
      badge.className = "badge signal";
      badge.innerHTML = '<span class="pip"></span> 실패';
    } else {
      badge.className = "badge muted";
      badge.textContent = "실행 기록 없음";
    }
    $("#collectorStamp").textContent = running
      ? `시작 ${fmtDate(d.job && d.job.started_at)}`
      : (last.last_run_at ? `마지막 실행 ${fmtDate(last.last_run_at)}` : "—");

    $("#collectStats").innerHTML = `
      <dl class="stat-box"><dt>마지막 실행</dt><dd>${last.last_run_at ? esc(rel(last.last_run_at)) : "—"}</dd></dl>
      <dl class="stat-box"><dt>결과</dt><dd>${running ? "실행 중" : (last.ok === true ? "성공" : (last.ok === false ? "실패" : "—"))}</dd></dl>
      <dl class="stat-box"><dt>신규 글 / 누적</dt><dd>${num(last.new_posts)} / ${num(last.total_posts)}</dd></dl>
      <dl class="stat-box"><dt>소요 시간</dt><dd>${last.duration_s != null ? esc(last.duration_s) + "초" : "—"}</dd></dl>`;

    const sourceStatus = last.source_status || {};
    const keys = Object.keys(SOURCE_LABEL);
    $("#sourceGrid").innerHTML = keys.map((key) => {
      const st = parseSourceStatus(sourceStatus[key]);
      return `<div class="source-row">
        <span><span class="name">${esc(SOURCE_LABEL[key])}</span><br><span class="desc">${esc(SOURCE_DESC[key])}</span></span>
        <span class="badge ${st.kind}"><span class="pip"></span>${esc(st.label)}</span>
      </div>`;
    }).join("");

    renderNaverPublish(d.naver || {}, last.naver || {});

    const log = last.log || [];
    $("#collectLog").innerHTML = log.length
      ? esc(log.join("\n")).replace(/\[(fail|!|실패)[^\]]*\]/g, '<span class="err">$&</span>')
      : esc(last.last_run_at ? "이번 실행에서 출력된 로그가 없습니다." : "아직 실행 기록이 없습니다. “지금 수집 실행”을 눌러 주세요.");
    $("#logStamp").textContent = last.last_run_at ? `${fmtDate(last.last_run_at)} · ${last.trigger || "-"} · ${last.duration_s || 0}초` : "—";

    const info = [
      ["서버 시각", fmtDate(d.generated_at) + " KST"],
      ["공개 사이트", security ? security.public_base_url : "—"],
      ["최근 글 수집", d.latest_post_at ? fmtDate(d.latest_post_at) + " KST" : "—"],
      ["최근 실행 트리거", last.trigger ? (last.trigger === "admin" ? "관리자 콘솔" : String(last.trigger)) : "—"],
      ["실행 단계", "collector.py → naver_drafts.py → naver_publish.py"],
      ["관리자 세션", security ? `${Math.round((security.session_ttl || 0) / 3600)}시간 · HttpOnly` : "—"],
      ["비밀번호 저장", security && security.auth ? (security.auth.storage === "file" ? "admin_auth.json 해시" : security.auth.storage === "env" ? "환경변수 초기값" : "개발용 기본값") : "—"],
      ["API 보호", "미로그인 401"]
    ];
    $("#systemInfo").innerHTML = info.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

    if (!running && state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      restoreCollectButton();
    }
  }

  const NAVER_STATUS = {
    published: { kind: "ok", label: "발행 완료" },
    nothing_pending: { kind: "muted", label: "새 글 없음" },
    no_session: { kind: "warn", label: "세션 없음" },
    login_expired: { kind: "signal", label: "세션 만료" },
    playwright_missing: { kind: "warn", label: "브라우저 미설치" },
    failed: { kind: "signal", label: "발행 실패" },
    skipped: { kind: "muted", label: "건너뜀" },
    dry_run: { kind: "muted", label: "모의 실행" }
  };

  function renderNaverPublish(stats, run) {
    const session = run.session || {};
    const st = NAVER_STATUS[run.status] || { kind: "muted", label: run.status || "기록 없음" };
    $("#naverStamp").textContent = run.finished_at ? `마지막 시도 ${fmtDate(run.finished_at)}` : "—";
    $("#naverStats").innerHTML = `
      <dl class="stat-box"><dt>누적 발행</dt><dd>${num(stats.published)}건</dd></dl>
      <dl class="stat-box"><dt>발행 대기 초안</dt><dd>${num(stats.pending)}건</dd></dl>
      <dl class="stat-box"><dt>마지막 상태</dt><dd><span class="badge ${st.kind}"><span class="pip"></span>${esc(st.label)}</span></dd></dl>
      <dl class="stat-box"><dt>대상 블로그</dt><dd>blog.naver.com/${esc((run.blog_id || stats.blog_id || "cryptobrothers"))}</dd></dl>`;

    const lines = [];
    if (run.status) {
      lines.push(`<div class="naver-line"><b>${esc(st.label)}</b> · ${esc(run.message || "")}</div>`);
    }
    if (run.items && run.items.length) {
      lines.push(`<div class="naver-line meta">` + run.items.map((it) => {
        const url = it.url ? ` <a href="${esc(it.url)}" target="_blank" rel="noopener">발행 글 보기 ↗</a>` : "";
        return `<div>· ${esc(it.title || "#" + it.draft_id)} <span class="cell-sub">${esc(it.status)}</span>${url}</div>`;
      }).join("") + `</div>`);
    }
    const sess = session.available
      ? `<div class="naver-line ok-line">세션 준비됨 · ${esc(session.path || "")}${session.age_days != null ? ` (${esc(session.age_days)}일 전 저장)` : ""}</div>`
      : `<div class="naver-line warn-line">네이버 로그인 세션 없음 · <code>data/naver_state.json</code> 을 올리면 발행이 시작됩니다.</div>`;
    $("#naverSession").innerHTML = lines.join("") + sess;
  }

  function restoreCollectButton() {
    const btn = $("#runCollect");
    btn.disabled = false;
    btn.textContent = "지금 수집 실행";
  }

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollCount = 0;
    state.pollTimer = setInterval(async () => {
      state.pollCount += 1;
      try {
        const { data } = await api("admin/collector");
        if (state.view === "system") renderSystem(data, state.security || null);
        if (!data.running) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          restoreCollectButton();
          const last = data.last || {};
          if (last.ok === true) toast(`수집 완료 · 신규 ${num(last.new_posts)}건 · 초안 ${num(last.drafts_created)}건`, "ok");
          else if (last.ok === false) toast("수집이 실패했습니다. 실행 로그를 확인해 주세요.", "err");
          if (state.view === "system") loadSystem();
          loadOverview();
        } else if (state.pollCount > 150) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          restoreCollectButton();
          toast("수집이 예상보다 오래 걸립니다. 잠시 후 상태를 확인해 주세요.");
        }
      } catch (e) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        restoreCollectButton();
      }
    }, 2000);
  }

  $("#runCollect").addEventListener("click", async () => {
    const btn = $("#runCollect");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 시작하는 중…';
    try {
      const { status, data } = await api("admin/collect", postJSON({ trigger: "admin" }));
      if (status === 202 && data.ok) {
        toast("수집을 시작했습니다. 완료되면 알려 드릴게요.");
        btn.innerHTML = '<span class="spinner"></span> 실행 중…';
        startPolling();
        return;
      }
      toast(data.error || "수집을 시작하지 못했습니다.", "err");
    } catch (e) {
      if (!(e instanceof AuthError || e instanceof StaticMode)) toast("수집 요청에 실패했습니다.", "err");
    }
    restoreCollectButton();
  });

  /* ============================================================= 블로그 초안 */
  const blog = { tab: "preview" };

  async function loadDrafts() {
    const box = $("#draftItems");
    box.innerHTML = '<div class="loading">초안을 불러오는 중…</div>';
    let data;
    try { ({ data } = await api("naver-drafts")); }
    catch (e) {
      if (e instanceof AuthError || e instanceof StaticMode) return;
      box.innerHTML = '<div class="loading" style="color:var(--signal)">초안을 불러오지 못했습니다.</div>';
      return;
    }
    state.drafts = data.drafts || [];
    $("#draftCountBadge").textContent = `총 ${num(state.drafts.length)}건`;
    if (!state.drafts.length) {
      box.innerHTML = '<div class="loading">아직 초안이 없습니다.<br>수집기 · 시스템에서 수집을 실행하세요.</div>';
      $("#draftTitle").textContent = "초안이 없습니다";
      return;
    }
    box.innerHTML = state.drafts.map((d, i) => `
      <button class="draft-item" type="button" data-idx="${i}" aria-current="${state.draft && state.draft.id === d.id}">
        <b>${esc(d.seo_title)}</b>
        <span>${esc(d.category)} · ${esc(fmtDate(d.created_at))}</span>
      </button>`).join("");
    $$(".draft-item", box).forEach((el) => {
      el.addEventListener("click", () => selectDraft(Number(el.dataset.idx)));
    });
    const keep = state.draft && state.drafts.find((d) => d.id === state.draft.id);
    selectDraft(keep ? state.drafts.indexOf(keep) : 0);
  }

  function selectDraft(idx) {
    const d = state.drafts[idx];
    if (!d) return;
    state.draft = d;
    $$(".draft-item").forEach((el) => el.setAttribute("aria-current", String(Number(el.dataset.idx) === idx)));
    $("#draftTitle").textContent = d.seo_title;
    $("#draftSourceMeta").innerHTML = `<span>${esc(d.source_name)} · ${esc(d.source_grade)}</span><span>확인 ${esc(fmtDate(d.verified_at))} KST</span>`;
    $("#draftKeywords").innerHTML = (d.keywords || []).map((k) => `<span>#${esc(k)}</span>`).join("");
    $("#previewPane").innerHTML = d.body_html_preview || d.body_html || "";
    $("#htmlPane").textContent = d.body_html || "";
    $("#textPane").textContent = d.body_text || "";
    const link = $("#imageOpen");
    link.href = d.image_url || "#";
    link.style.display = d.image_url ? "" : "none";
    setDraftTab(blog.tab);
  }

  function setDraftTab(next) {
    blog.tab = next;
    $$(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === next)));
    $("#previewPane").hidden = next !== "preview";
    $("#htmlPane").hidden = next !== "html";
    $("#textPane").hidden = next !== "text";
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => setDraftTab(t.dataset.tab)));

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    }
  }

  async function copyRich(html, text) {
    try {
      if (window.ClipboardItem && navigator.clipboard.write) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" })
        })]);
        return true;
      }
    } catch (e) { /* fall through */ }
    const holder = document.createElement("div");
    holder.innerHTML = html;
    holder.style.position = "fixed";
    holder.style.left = "-9999px";
    document.body.appendChild(holder);
    const range = document.createRange();
    range.selectNodeContents(holder);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    const ok = document.execCommand("copy");
    sel.removeAllRanges();
    holder.remove();
    return ok;
  }

  $$("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const d = state.draft;
      if (!d) { toast("먼저 초안을 선택해 주세요."); return; }
      const kind = btn.dataset.copy;
      let ok = false;
      if (kind === "title") ok = await copyText(d.seo_title);
      else if (kind === "rich") ok = await copyRich(d.body_html, d.body_text);
      else if (kind === "html") ok = await copyText(d.body_html);
      else if (kind === "tags") ok = await copyText((d.keywords || []).map((k) => "#" + k).join(" "));
      else if (kind === "image") ok = await copyText(d.image_url || "");
      if (ok) {
        btn.classList.add("copied");
        const old = btn.textContent;
        btn.textContent = "복사됨 ✓";
        setTimeout(() => { btn.classList.remove("copied"); btn.textContent = old; }, 1400);
      } else {
        toast("복사에 실패했습니다. HTML 탭에서 직접 선택해 주세요.", "err");
      }
    });
  });

  /* ============================================================= 보안 */
  async function loadSecurity() {
    let data;
    try { ({ data } = await api("admin/security")); }
    catch (e) {
      if (e instanceof AuthError || e instanceof StaticMode) return;
      toast("보안 정보를 불러오지 못했습니다.", "err");
      return;
    }
    renderSecurity(data);
  }

  function renderSecurity(data) {
    const auth = data.auth || {};
    const storage = auth.storage === "file" ? "해시 파일 (data/admin_auth.json)"
      : auth.storage === "env" ? "환경변수 초기값 (.env)"
      : "개발용 기본값 ⚠";
    const rows = [
      ["비밀번호", storage],
      ["마지막 변경", auth.updated_at ? fmtDate(auth.updated_at) : "변경 기록 없음"],
      ["해시 방식", `${auth.algorithm || "pbkdf2_sha256"} · ${num(auth.iterations)}회`],
      ["세션", "12시간 · HttpOnly · SameSite=Lax"],
      ["활성 세션", `${num(data.session_count)}개`],
      ["로그인 잠금", `${data.policy.login_max_fails}회 실패 / ${Math.round(data.policy.login_window / 60)}분`],
      ["API 보호", data.policy.api_auth],
      ["검색 노출", "noindex, nofollow"],
      ["서버 시각", fmtDate(data.server_time) + " KST"]
    ];
    $("#securityInfo").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("#securityStamp").textContent = auth.updated_at ? `변경 ${rel(auth.updated_at)}` : "초기 비밀번호 사용 중";
    const badge = $("#securityBadge");
    badge.className = "badge " + (auth.storage === "default" ? "warn" : "ok");
    badge.innerHTML = `<span class="pip"></span>${auth.storage === "default" ? "기본 비밀번호" : "보호됨"}`;

    const list = $("#auditList");
    if (!data.audit || !data.audit.length) {
      list.innerHTML = '<div class="empty-note">아직 감사 로그가 없습니다.</div>';
    } else {
      list.innerHTML = data.audit.map((a) => `
        <div class="audit-row">
          <span class="when">${esc(fmtDate(a.at))}</span>
          <span class="what">${esc(AUDIT_LABEL[a.action] || a.action)}</span>
          <span class="detail">${esc(a.detail || "")}${a.ip ? ` <span style="color:#aeb4bc">· ${esc(a.ip)}</span>` : ""}</span>
        </div>`).join("");
    }
  }

  $("#pwForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const cur = $("#curPw").value;
    const next = $("#newPw").value;
    const confirm = $("#confirmPw").value;
    const err = $("#pwErr");
    err.textContent = "";
    if (!cur || !next || !confirm) { err.textContent = "모든 항목을 입력해 주세요."; return; }
    if (next.length < 10) { err.textContent = "새 비밀번호는 10자 이상이어야 합니다."; return; }
    if (next !== confirm) { err.textContent = "새 비밀번호와 확인이 일치하지 않습니다."; return; }
    if (next === cur) { err.textContent = "현재 비밀번호와 다른 비밀번호를 입력해 주세요."; return; }

    const btn = $("#pwSubmit");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 변경 중…';
    try {
      const { status, data } = await api("admin/change-password", postJSON({
        current_password: cur, new_password: next, confirm_password: confirm
      }));
      if (status === 200 && data.ok) {
        toast("비밀번호를 변경했습니다. 서버를 재시작해도 유지됩니다.", "ok");
        $("#pwForm").reset();
        updateStrength("");
        await loadSecurity();
        return;
      }
      err.textContent = data.error || "비밀번호를 변경하지 못했습니다.";
    } catch (e) {
      if (!(e instanceof AuthError || e instanceof StaticMode)) err.textContent = "변경 요청에 실패했습니다. 잠시 후 다시 시도해 주세요.";
    } finally {
      btn.disabled = false;
      btn.textContent = "비밀번호 변경";
    }
  });

  function updateStrength(value) {
    const bar = $("#pwStrengthBar");
    const label = $("#pwStrengthLabel");
    if (!value) {
      bar.style.width = "0%";
      label.textContent = "영문·숫자·기호를 섞으면 더 안전합니다.";
      return;
    }
    let score = 0;
    if (value.length >= 10) score++;
    if (value.length >= 14) score++;
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score++;
    if (/[0-9]/.test(value)) score++;
    if (/[^A-Za-z0-9]/.test(value)) score++;
    const table = [
      { w: 20, c: "#b3261e", t: "너무 짧습니다" },
      { w: 40, c: "#b5731a", t: "조금 약합니다" },
      { w: 60, c: "#b5731a", t: "보통입니다" },
      { w: 80, c: "#15724f", t: "안전합니다" },
      { w: 100, c: "#15724f", t: "매우 안전합니다" }
    ];
    const step = table[Math.max(0, Math.min(4, score - 1))];
    bar.style.width = step.w + "%";
    bar.style.background = step.c;
    label.textContent = step.t + ` · ${value.length}자`;
  }
  $("#newPw").addEventListener("input", (ev) => updateStrength(ev.target.value));

  $$(".pw-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = $("#" + btn.dataset.target);
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "숨김" : "보기";
    });
  });

  /* ============================================================= 모달 */
  const modal = $("#modal");
  function openModal({ title, body, actions }) {
    $("#modalTitle").textContent = title;
    $("#modalBody").innerHTML = body || "";
    $("#modalActions").innerHTML = actions || '<button class="btn btn-sm btn-primary" type="button" data-close="1">닫기</button>';
    modal.hidden = false;
    modal.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeModal));
  }
  function closeModal() { modal.hidden = true; }
  $("#modalClose").addEventListener("click", closeModal);
  modal.addEventListener("click", (ev) => { if (ev.target === modal) closeModal(); });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !modal.hidden) closeModal();
  });

  /* ============================================================= 시작 */
  async function enterApp() {
    loginView.hidden = true;
    appView.hidden = false;
    try {
      const { data } = await api("admin/session");
      const sourceLabel = data.password_source === "file" ? "해시 파일" : data.password_source === "env" ? "환경변수 초기값" : "기본값";
      $("#sideAuth").textContent = `비밀번호 ${sourceLabel} · 세션 ${Math.round((data.expires_in || 43200) / 3600)}시간`;
    } catch (e) { /* 세션 표시는 부가 정보 */ }

    const name = location.hash.replace(/^#/, "");
    if (!VIEW_META[name]) {
      if (location.hash) history.replaceState(null, "", "#overview");
      go("overview");
    } else {
      go(name);
      if (name !== "overview") loadOverview();
    }
  }

  (async function init() {
    try {
      const { data } = await api("admin/session");
      if (data.authenticated) await enterApp();
      else showLogin("");
    } catch (e) {
      if (e instanceof StaticMode) { showStatic(); return; }
      showLogin("");
    }
  })();
})();
