/* Azeroth Watch — zero-dependency live frontend (SSE + read-only API) */
(() => {
  "use strict";

  const state = {
    data: null,
    view: "overview",
    vaultCat: "ALL",
    vaultSearch: "",
    rosterFilter: "all",
    slideId: "vault",
    slidePaused: false,
  };

  const $ = (id) => document.getElementById(id);

  function setHtmlIfChanged(el, html) {
    if (!el || el._lastHtml === html) return;
    el._lastHtml = html;
    el.innerHTML = html;
  }

  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const ROLE_LABELS = {
    tank: "Tank",
    healer: "Healer",
    mdps: "Melee",
    rdps: "Ranged",
  };

  const SHIFT_LABELS = {
    "core-wonky": "Core Wonky",
    "core-logan": "Core Logan",
    "prime-time": "Prime Time",
    "weekend-raiders": "Weekend Raiders",
    "afternoon-crew": "Afternoon Crew",
    "midday-crew": "Midday Crew",
    "early-birds": "Early Birds",
    "night-owls": "Night Owls",
  };

  const EVENT_ICONS = {
    level_up: "⭐",
    quest_done: "📜",
    craft: "⚒",
    craft_done: "⚒",
    skill_milestone: "✦",
    skill_up: "✦",
    boss_kill: "⚔",
    death: "💀",
    work_order: "📦",
  };

  function fmtUptime(secs) {
    if (!secs) return "--";
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }

  function switchView(name) {
    state.view = name;
    document.querySelectorAll(".tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    document.querySelectorAll(".view").forEach((v) => {
      v.classList.toggle("active", v.id === `view-${name}`);
    });
    if (state.data) render(state.data);
  }

  document.querySelectorAll(".tab").forEach((b) => {
    b.addEventListener("click", () => switchView(b.dataset.view));
  });
  document.querySelectorAll("[data-jump]").forEach((b) => {
    b.addEventListener("click", () => switchView(b.dataset.jump));
  });
  document.addEventListener("keydown", (e) => {
    if (document.activeElement && document.activeElement.tagName === "INPUT") return;
    if (e.key === "1") switchView("overview");
    else if (e.key === "2") switchView("vault");
    else if (e.key === "3") switchView("roster");
    else if (e.key === "4") switchView("brain");
  });

  $("vault-search").addEventListener("input", (e) => {
    state.vaultSearch = e.target.value.trim().toLowerCase();
    if (state.data) renderVaultView(state.data);
  });

  document.querySelectorAll("[data-rfilter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.rosterFilter = btn.dataset.rfilter;
      document.querySelectorAll("[data-rfilter]").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      if (state.data) renderRosterView(state.data);
    });
  });

  function renderSparkline(series) {
    const svg = $("tick-spark");
    if (!series || series.length === 0) {
      svg.innerHTML = `<line x1="0" y1="15" x2="88" y2="15" stroke="#2c3952" stroke-width="1.5"/>`;
      return;
    }
    const vals = series.map((p) => p.ms);
    const maxV = Math.max(25, ...vals);
    const w = 88;
    const h = 22;
    const step = vals.length > 1 ? w / (vals.length - 1) : 0;
    const pts = vals.map((v, i) => {
      const x = vals.length > 1 ? i * step : w / 2;
      const y = h - 3 - Math.min(1, v / maxV) * (h - 6);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const latest = vals[vals.length - 1];
    const stroke = latest > 50 ? "#e05c5c" : latest > 25 ? "#f2b84b" : "#3ad29f";
    svg.innerHTML = `<polyline fill="none" stroke="${stroke}" stroke-width="1.8" points="${pts.join(" ")}"/>`;
  }

  function renderHeader(d) {
    const v = d.vitals;
    const up = v.worldserver_up && d.realm.flag === 0 && d.db_ok !== false;
    const stale = d.db_ok === false || (typeof d.data_age_sec === "number" && d.data_age_sec > 30);
    const sp = $("status-pill");
    sp.className = `pill status-pill ${up && !stale ? "online" : "offline"}`;
    if (d.db_ok === false) {
      $("status-text").textContent = `DB STALE (${d.data_age_sec || 0}s)`;
    } else if (stale) {
      $("status-text").textContent = `STALE (${d.data_age_sec}s)`;
    } else {
      $("status-text").textContent = up ? `ONLINE · ${fmtUptime(v.worldserver_uptime_s)}` : "OFFLINE";
    }

    $("realm-sub").textContent = `${d.realm.name} · ${d.realm.address}:${d.realm.port} · ${d.population.total_online} Online`;
    $("clock-val").textContent = d.clock;

    const latestMs = d.ticks.latest;
    $("tick-val").textContent = latestMs !== null ? `${latestMs} ms` : "10 ms";
    renderSparkline(d.ticks.series);

    $("ws-vitals").textContent = `${(v.worldserver_rss_mib / 1024).toFixed(2)}G · ${v.worldserver_cpu_pct}%`;
    $("host-vitals").textContent = `${v.mem_used_gib}/${v.mem_total_gib}G · ${v.host_cpu_pct}%`;

    const hasErr = d.errors_bytes > 0 || d.slow_warnings > 0;
    $("err-pill").classList.toggle("has-err", hasErr);
    $("err-val").textContent = hasErr ? `${d.errors_bytes}B / ${d.slow_warnings}s` : "0";

    const aiPill = $("ai-pill");
    if (aiPill) {
      const ab = d.ai_brain || {};
      if (ab.redacted) {
        const activeCnt = Number(ab.active_plans_count || 0);
        aiPill.classList.toggle("has-err", Boolean(ab.stale_alarm));
        $("ai-val").textContent = `${activeCnt} active · 🔒`;
        aiPill.title = `Claude AI Brain (${activeCnt} active companion plans) · Click or press 4 and unlock Admin to view model & budget`;
      } else {
        const t = ab.today || {};
        const bal = ab.balance || {};
        const mShort = fmtModelShort(ab.model);
        const ok = t.ai_ok || 0;
        const err = t.ai_err || 0;
        const cap = ab.daily_cap || 500;
        const rem = typeof bal.remaining_usd === "number" ? `~$${bal.remaining_usd.toFixed(2)}` : `${ok}/${cap}`;
        aiPill.classList.toggle("has-err", err > 0);
        $("ai-val").textContent = `${mShort} · ${rem}`;
        aiPill.title = `Claude AI Brain (${ab.model || "none"} ${ab.effort || ""}) · Est. ${rem} left of $${(bal.credit_total_usd || 98).toFixed(2)} · Today: ${ok}/${cap} plans, ${err} errors · Click or press 4`;
      }
    }

    const evPill = $("event-pill");
    if (evPill) {
      const ev = d.weekly_event || "";
      evPill.style.display = ev ? "" : "none";
      if (ev) $("event-val").textContent = ev;
    }

    // Issue #59: Visible operator alert banner for DB outage, stale data, brain staleness, or recent failure alerts
    const alertBanner = $("operator-alert-banner");
    if (alertBanner) {
      const ab = d.ai_brain || {};
      const msgs = [];
      if (!d.db_ok) msgs.push("MySQL disconnected (db_ok=false)");
      if ((d.data_age_sec || 0) > 120) msgs.push(`Telemetry stale (${d.data_age_sec}s old)`);
      if (ab.stale_alarm) msgs.push("AI Brain stale alarm: no recent applied companion plan");
      const nowEpoch = Math.floor(Date.now() / 1000);
      const rawAlerts = Array.isArray(ab.failure_alerts) ? ab.failure_alerts : [];
      const recentRaw = rawAlerts.filter((a) => !a.epoch || nowEpoch - Number(a.epoch) <= 7200);
      if (recentRaw.length > 0) {
        const last = recentRaw[recentRaw.length - 1];
        msgs.push(`Alert (${last.source || "system"} @ ${last.ts || ""}): ${last.detail || "failure recorded"}`);
      } else if (ab.recent_alert_count > 0 && ab.latest_alert) {
        msgs.push(`Alert (${ab.latest_alert.source || "system"} @ ${ab.latest_alert.ts || ""}) — unlock admin for details`);
      }
      if (msgs.length > 0) {
        alertBanner.style.display = "flex";
        setHtmlIfChanged(
          alertBanner,
          `<span class="alert-badge">ALERT</span><span>${msgs.map((m) => esc(m)).join(" · ")}</span>`
        );
      } else {
        alertBanner.style.display = "none";
      }
    }

    const hb = $("humans-banner");
    const hbHtml =
      d.humans.length > 0
        ? d.humans
            .map(
              (h) =>
                `<a class="human-chip char-link" href="/character/${encodeURIComponent(h.name.toLowerCase())}" title="Open ${esc(h.name)}'s Character Sheet · XP: ${h.xp.toLocaleString()} / ${h.xp_max.toLocaleString()} (${h.xp_pct}%)"><img class="human-avatar" src="/static/icons/race_${h.race_id}_${h.gender}.png" alt=""/><span style="color:${h.class_color}">★ ${esc(h.name)}</span><span>Lv ${h.level} (${Math.round(h.xp_pct)}%) · ${esc(h.zone)}</span></a>`
            )
            .join("")
        : `<div class="human-chip idle">No Humans Online</div>`;
    setHtmlIfChanged(hb, hbHtml);
  }

  function renderRaidBanner(d) {
    const r = d.raid;
    const pop = d.population;
    const total = Math.max(1, pop.companions_total);
    const b = r.brackets;

    const erasHtml = r.stages
      .map((s, idx) => {
        const isCurrent = s.stage === r.stage;
        const cls = isCurrent ? "active-era" : s.unlocked ? "" : "locked";
        const arrow = idx < r.stages.length - 1 ? `<span class="era-arrow">›</span>` : "";
        return `
          <div class="era-node ${cls}">
            <div class="era-top">
              <span class="era-name">${esc(s.era)}</span>
              <span class="era-cap mono">Cap ${s.cap}</span>
            </div>
            <div class="era-boss">${isCurrent ? "Gate: " : ""}${esc(s.boss)}</div>
          </div>${arrow}`;
      })
      .join("");

    setHtmlIfChanged($("raid-banner"), `
      <div class="raid-row">
        <div class="era-track">${erasHtml}</div>
        <div class="raid-readiness">
          <div class="readiness-title">
            <span>Hearthguard Raid Roster (${pop.companions_total} Guildies)</span>
            <span class="mono">60+: ${b.ready} · 40+: ${b.high} · 20+: ${b.mid} · &lt;20: ${b.low}</span>
          </div>
          <div class="role-pills">
            <div class="role-pill tank"><span>TANKS</span><strong class="mono">${r.roles.tank}</strong></div>
            <div class="role-pill healer"><span>HEALERS</span><strong class="mono">${r.roles.healer}</strong></div>
            <div class="role-pill mdps"><span>MELEE</span><strong class="mono">${r.roles.mdps}</strong></div>
            <div class="role-pill rdps"><span>RANGED</span><strong class="mono">${r.roles.rdps}</strong></div>
          </div>
          <div class="bracket-bar" title="Level progression toward cap ${r.level_cap}">
            <div class="b-ready" style="width:${(100 * b.ready) / total}%"></div>
            <div class="b-high" style="width:${(100 * b.high) / total}%"></div>
            <div class="b-mid" style="width:${(100 * b.mid) / total}%"></div>
            <div class="b-low" style="width:${(100 * b.low) / total}%"></div>
          </div>
        </div>
        <div class="raid-stats-col">
          <div class="mini-kpi">
            <div class="k-val mono" style="color:var(--gold-bright)">${r.avg_core_wonky}</div>
            <div class="k-lbl">Core Wonky Avg</div>
          </div>
          <div class="mini-kpi">
            <div class="k-val mono" style="color:var(--green)">${r.avg_core_logan}</div>
            <div class="k-lbl">Core Logan Avg</div>
          </div>
          <div class="mini-kpi">
            <div class="k-val mono">${pop.companions_online} / ${pop.random_online}</div>
            <div class="k-lbl">Guild / World</div>
          </div>
        </div>
      </div>`);
  }

  // Persistent keyed companion card builder/updater — keeps <img> portraits mounted forever (zero flicker!)
  const botCardCache = new Map();

  function createBotCardEl(c) {
    const el = document.createElement("a");
    el.className = "bot-card char-link";
    el.href = `/character/${encodeURIComponent(c.name.toLowerCase())}`;
    el.dataset.bot = c.name;
    el.style.setProperty("--cls-color", c.class_color);
    el.innerHTML = `
      <div class="bot-head">
        <div class="avatar-wrap" title="${esc(c.race)} ${esc(c.class_name)}">
          <img class="avatar-img" src="/static/icons/race_${c.race_id}_${c.gender}.png" alt=""/>
          <img class="avatar-cls" src="/static/icons/class_${c.class_id}.png" alt=""/>
          <span class="avatar-dead-badge" style="display:none">💀</span>
        </div>
        <div class="bot-info">
          <div class="bot-top">
            <span class="bot-name">${esc(c.name)}</span>
            <span class="bag-warn mono" style="display:none"></span>
            <span class="bot-lv mono"></span>
          </div>
          <div class="bot-mid">
            <span class="bot-zone"></span>
            <span class="bot-arch">${esc(c.archetype || ROLE_LABELS[c.role])}</span>
          </div>
          <div class="bot-act">
            <span class="act-tag"></span><span class="act-detail"></span>
          </div>
        </div>
      </div>
      <div class="xp-row">
        <div class="xp-bar"><div class="xp-fill"></div></div>
        <span class="xp-pct mono"></span>
      </div>
      <div class="bot-brain" style="display:none"></div>
      <div class="bot-profs"></div>`;
    el._refs = {
      deadBadge: el.querySelector(".avatar-dead-badge"),
      bagWarn: el.querySelector(".bag-warn"),
      lv: el.querySelector(".bot-lv"),
      zone: el.querySelector(".bot-zone"),
      actRow: el.querySelector(".bot-act"),
      actTag: el.querySelector(".act-tag"),
      actDetail: el.querySelector(".act-detail"),
      xpRow: el.querySelector(".xp-row"),
      xpFill: el.querySelector(".xp-fill"),
      xpPct: el.querySelector(".xp-pct"),
      brain: el.querySelector(".bot-brain"),
      profs: el.querySelector(".bot-profs"),
    };
    return el;
  }

  function updateBotCardEl(el, c) {
    const r = el._refs;
    const act = c.dead
      ? { tag: "Dead", detail: "Corpse run / reviving" }
      : c.activity || { tag: "Active", detail: c.zone };

    el.classList.toggle("is-dead", !!c.dead);
    r.deadBadge.style.display = c.dead ? "" : "none";

    const bagFull = c.bag_total > 0 && c.bag_used >= c.bag_total;
    if (bagFull) {
      r.bagWarn.style.display = "";
      const txt = `BAGS ${c.bag_used}/${c.bag_total}`;
      if (r.bagWarn.textContent !== txt) r.bagWarn.textContent = txt;
    } else {
      r.bagWarn.style.display = "none";
    }

    const lvTxt = `Lv ${c.level}`;
    if (r.lv.textContent !== lvTxt) r.lv.textContent = lvTxt;
    if (r.zone.textContent !== c.zone) r.zone.textContent = c.zone;

    if (r.actTag.textContent !== act.tag) {
      r.actTag.textContent = act.tag;
      r.actTag.className = `act-tag ${act.tag}`;
    }
    if (r.actDetail.textContent !== act.detail) {
      r.actDetail.textContent = act.detail;
      r.actRow.title = act.detail;
    }

    const xpW = `${c.xp_pct}%`;
    if (r.xpFill.style.width !== xpW) r.xpFill.style.width = xpW;
    const xpPctTxt = `${Math.round(c.xp_pct)}%`;
    if (r.xpPct.textContent !== xpPctTxt) r.xpPct.textContent = xpPctTxt;
    const xpTip = c.xp_max > 0 ? `XP: ${c.xp.toLocaleString()} / ${c.xp_max.toLocaleString()} (${c.xp_pct}%)` : "Max Level";
    if (r.xpRow.title !== xpTip) r.xpRow.title = xpTip;

    if (c.ai_plan && c.ai_plan.active) {
      const label = `🧠 ${fmtGoalShort(c.ai_plan.primary_goal_id)}`;
      r.brain.style.display = "";
      if (r.brain.textContent !== label) r.brain.textContent = label;
      const tip = `${fmtModelShort(c.ai_plan.model || c.ai_plan.source)}: ${c.ai_plan.primary_desc} (${fmtReason(c.ai_plan.reason_code)})`;
      if (r.brain.title !== tip) r.brain.title = tip;
    } else {
      r.brain.style.display = "none";
    }

    const profsHtml = (c.primaries || [])
      .slice(0, 2)
      .map((p) => `<div class="prof-chip"><span>${esc(p.short)}</span><b class="mono">${p.value}/${p.max}</b></div>`)
      .join("");
    setHtmlIfChanged(r.profs, profsHtml);
    r.profs.style.display = profsHtml ? "flex" : "none";
  }

  function renderCompanionsInPlace(online) {
    const root = $("companions-groups");
    if (!online.length) {
      root._layoutKey = "";
      setHtmlIfChanged(root, `<div class="empty-note">Waiting for companions to log in...</div>`);
      return;
    }
    const groupOrder = ["core-wonky", "core-logan", "prime-time", "weekend-raiders", "afternoon-crew", "midday-crew", "early-birds", "night-owls"];
    const byGroup = {};
    online.forEach((c) => {
      (byGroup[c.shift_group] = byGroup[c.shift_group] || []).push(c);
    });
    const activeGroups = groupOrder.filter((g) => byGroup[g] && byGroup[g].length > 0);
    const layoutKey = activeGroups.map((g) => `${g}:${byGroup[g].map((c) => c.name).join(",")}`).join("|");

    if (root._layoutKey !== layoutKey) {
      root._layoutKey = layoutKey;
      root._lastHtml = null;
      const frag = document.createDocumentFragment();
      activeGroups.forEach((g) => {
        const list = byGroup[g];
        const block = document.createElement("div");
        block.className = "group-block";
        const lbl = document.createElement("div");
        lbl.className = "group-label";
        lbl.textContent = `${SHIFT_LABELS[g] || g} (${list.length})`;
        const grid = document.createElement("div");
        grid.className = "bot-cards";
        list.forEach((c) => {
          let card = botCardCache.get(c.name);
          if (!card) {
            card = createBotCardEl(c);
            botCardCache.set(c.name, card);
          }
          updateBotCardEl(card, c);
          grid.appendChild(card);
        });
        block.appendChild(lbl);
        block.appendChild(grid);
        frag.appendChild(block);
      });
      root.replaceChildren(frag);
    } else {
      online.forEach((c) => {
        const card = botCardCache.get(c.name);
        if (card) updateBotCardEl(card, c);
      });
    }
  }

  function getShowcaseSlides(d) {
    const slides = [{ id: "vault", kind: "vault", title: "Hearthguard Vault Mats", short: "Vault" }];
    (d.map_slides || []).forEach((ms) => {
      slides.push({
        ...ms,
        short: ms.kind === "world" ? "World" : ms.title.replace("Eastern Kingdoms", "EK").replace(" Mountains", "").replace(" Forest", ""),
      });
    });
    return slides;
  }

  // Persistent map DOM — NEVER recreates the <img> element while updating pins on the same map!
  let mapDom = null;
  function ensureMapDom(mapSlide) {
    if (mapDom) return mapDom;
    mapSlide.innerHTML = `
      <div class="map-frame">
        <img class="map-img" alt=""/>
        <div class="map-layer map-dots-layer"></div>
        <div class="map-layer map-pins-layer"></div>
        <span class="map-overlay-tag mono"></span>
      </div>`;
    mapDom = {
      img: mapSlide.querySelector(".map-img"),
      dotsLayer: mapSlide.querySelector(".map-dots-layer"),
      pinsLayer: mapSlide.querySelector(".map-pins-layer"),
      tag: mapSlide.querySelector(".map-overlay-tag"),
      curImg: "",
      curSlideId: "",
      pinNodes: new Map(),
    };
    return mapDom;
  }

  function renderShowcase(d) {
    const slides = getShowcaseSlides(d);
    let idx = slides.findIndex((s) => s.id === state.slideId);
    if (idx < 0) {
      idx = 0;
      state.slideId = slides[0].id;
    }
    const cur = slides[idx];

    const titleEl = $("showcase-title");
    const vaultSlide = $("vault-slide");
    const mapSlide = $("map-slide");

    if (cur.kind === "vault") {
      setHtmlIfChanged(titleEl, `Hearthguard Vault Mats <span class="badge mono">${d.bank.total_slots} / ${d.bank.max_slots} slots</span>`);
      vaultSlide.style.display = "flex";
      mapSlide.style.display = "none";

      setHtmlIfChanged(
        $("vault-cat-pills"),
        d.bank.categories
          .slice(0, 6)
          .map((c) => `<span class="vcat-pill">${esc(c.category)}<b class="mono">${c.total_qty}</b></span>`)
          .join("")
      );

      const allItems = [];
      d.bank.categories.forEach((cat) => {
        cat.items.forEach((it) => allItems.push({ ...it, cat: cat.category }));
      });
      allItems.sort((a, b) => b.qty - a.qty);
      setHtmlIfChanged(
        $("vault-top-items"),
        allItems
          .slice(0, 14)
          .map(
            (it) => `
            <div class="mat-row" title="${esc(it.cat)} · ${it.stacks} stack(s)">
              <span class="mat-name" style="color:${it.color}">${esc(it.name)}</span>
              <span class="mat-qty mono">×${it.qty}</span>
            </div>`
          )
          .join("")
      );
    } else {
      const kindTag = cur.kind === "world" ? "World" : cur.kind === "continent" ? "Continent" : "Zone";
      setHtmlIfChanged(titleEl, `${esc(cur.title)} <span class="badge mono">${esc(cur.subtitle)}</span>`);
      vaultSlide.style.display = "none";
      mapSlide.style.display = "flex";

      const dom = ensureMapDom(mapSlide);
      const slideChanged = dom.curSlideId !== cur.id;
      if (dom.curImg !== cur.img) {
        dom.curImg = cur.img;
        dom.img.src = cur.img;
        dom.img.alt = cur.title;
      }
      if (slideChanged) {
        dom.curSlideId = cur.id;
        dom.pinsLayer.replaceChildren();
        dom.pinNodes.clear();
      }

      const dotsHtml = (cur.dots || [])
        .map((pt) => `<span class="map-dot" style="left:${pt.u}%;top:${pt.v}%"></span>`)
        .join("");
      setHtmlIfChanged(dom.dotsLayer, dotsHtml);

      // Update named pins in-place so they smoothly glide instead of flashing!
      const showLabels = cur.kind === "zone" || (cur.pins || []).length <= 12;
      const seen = new Set();
      (cur.pins || []).forEach((p) => {
        seen.add(p.name);
        let node = dom.pinNodes.get(p.name);
        const cls = `map-pin ${p.human ? "is-human" : ""} ${p.dead ? "is-dead" : ""}`;
        const lblTxt = showLabels || p.human ? `${p.human ? "★ " : ""}${p.dead ? "💀 " : ""}${p.name}` : "";
        if (!node) {
          node = document.createElement("div");
          node.className = cls;
          node.style.left = `${p.u}%`;
          node.style.top = `${p.v}%`;
          node.style.setProperty("--pin-color", p.color);
          node.innerHTML = `<span class="pin-dot"></span><span class="pin-lbl"></span>`;
          dom.pinsLayer.appendChild(node);
          dom.pinNodes.set(p.name, node);
        } else {
          if (node.className !== cls) node.className = cls;
          node.style.left = `${p.u}%`;
          node.style.top = `${p.v}%`;
        }
        node.title = `${p.name} Lv ${p.level} · ${p.zone}`;
        const lblEl = node.lastElementChild;
        lblEl.style.display = lblTxt ? "" : "none";
        if (lblEl.textContent !== lblTxt) lblEl.textContent = lblTxt;
      });
      for (const [name, node] of dom.pinNodes.entries()) {
        if (!seen.has(name)) {
          node.remove();
          dom.pinNodes.delete(name);
        }
      }

      const liveNote = cur.realtime ? "LIVE 3s" : `${kindTag} · DB sync`;
      if (dom.tag.textContent !== liveNote) dom.tag.textContent = liveNote;
    }

    $("sc-pause").textContent = state.slidePaused ? "▶" : "⏸";
    $("sc-pause").classList.toggle("paused", state.slidePaused);

    const dotsBarHtml = slides
      .map(
        (s) =>
          `<button class="sc-pill ${s.id === state.slideId ? "active" : ""}" data-sid="${esc(s.id)}" title="${esc(s.title)}">${esc(s.short)}</button>`
      )
      .join("");
    const dotsEl = $("showcase-dots");
    if (dotsEl._lastHtml !== dotsBarHtml) {
      dotsEl._lastHtml = dotsBarHtml;
      dotsEl.innerHTML = dotsBarHtml;
      dotsEl.querySelectorAll("[data-sid]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.slideId = btn.dataset.sid;
          if (state.data) renderShowcase(state.data);
        });
      });
    }
  }

  function stepShowcase(delta) {
    if (!state.data) return;
    const slides = getShowcaseSlides(state.data);
    if (slides.length <= 1) return;
    let idx = slides.findIndex((s) => s.id === state.slideId);
    if (idx < 0) idx = 0;
    state.slideId = slides[(idx + delta + slides.length) % slides.length].id;
    renderShowcase(state.data);
  }

  $("sc-prev").addEventListener("click", () => stepShowcase(-1));
  $("sc-next").addEventListener("click", () => stepShowcase(1));
  $("sc-pause").addEventListener("click", () => {
    state.slidePaused = !state.slidePaused;
    if (state.data) renderShowcase(state.data);
  });
  setInterval(() => {
    if (state.slidePaused || state.view !== "overview" || !state.data) return;
    stepShowcase(1);
  }, 50000);

  function renderOverview(d) {
    renderRaidBanner(d);

    const online = d.companions.filter((c) => c.online);
    $("comp-online-badge").textContent = `${online.length} / ${d.companions.length}`;

    const activeShiftNames = d.shifts
      .filter((s) => s.active && !s.always)
      .map((s) => s.label)
      .join(" · ");
    setHtmlIfChanged($("active-shifts-meta"), `<span class="muted">Active Shifts: <b style="color:var(--green)">${esc(activeShiftNames || "Core Only")}</b></span>`);

    renderCompanionsInPlace(online);

    // Profession mini matrix (11 primaries + Cooking -> top 12 fit nicely in 6x2)
    $("prof-cap-label").textContent = `Cap ${d.raid.prof_cap}`;
    const primaryProfs = d.professions.filter((p) => p.kind !== "secondary").concat(
      d.professions.filter((p) => p.name === "Cooking")
    );
    setHtmlIfChanged(
      $("prof-mini-grid"),
      primaryProfs
        .slice(0, 12)
        .map((p) => {
          const top = p.leaders[0];
          const leaderTxt = top
            ? `<span style="color:${top.class_color}">${esc(top.name)}</span> (${p.count})`
            : "Unassigned";
          return `
            <div class="prof-tile">
              <div class="prof-tile-top">
                <span class="prof-tile-name">${esc(p.name)}</span>
                <span class="prof-tile-val mono">${p.max_value}</span>
              </div>
              <div class="bar-bg"><div class="bar-fill ${p.kind}" style="width:${p.pct}%"></div></div>
              <div class="prof-tile-leader">${leaderTxt}</div>
            </div>`;
        })
        .join("")
    );

    // World bots strip
    const wz = d.world_zones
      .map((z) => `<span class="zone-pill">${esc(z.zone)}<b class="mono">${z.count}</b></span>`)
      .join("");
    setHtmlIfChanged($("world-strip"), `<span class="world-label">World (${d.population.random_online})</span>${wz}`);

    // Right: Rotating Showcase (Vault Mats + World / Continent / Zone Live Maps)
    renderShowcase(d);

    // Right: Chronicle
    $("events-count-badge").textContent = String(d.events.length);
    setHtmlIfChanged(
      $("chronicle-list"),
      d.events.length > 0
        ? d.events
            .slice(0, 18)
            .map(
              (ev) => `
              <div class="ev-item">
                <span class="ev-time mono">${esc(ev.time_str)}</span>
                <span class="ev-ico">${EVENT_ICONS[ev.type] || "•"}</span>
                <div class="ev-text"><span class="ev-bot" style="color:${ev.class_color}">${esc(ev.bot)}</span>${esc(ev.summary)}</div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">Listening for live Guild Life events...</div>`
    );

    // Right: 24h shift timeline
    const nowPct = ((d.hour + d.minute / 60) / 24) * 100;
    $("shift-now-label").textContent = `${String(d.hour).padStart(2, "0")}:${String(d.minute).padStart(2, "0")} local`;
    setHtmlIfChanged(
      $("shift-timeline"),
      d.shifts
        .filter((s) => !s.always)
        .map((s) => {
          const left = (s.start / 24) * 100;
          const width = ((s.end - s.start) / 24) * 100;
          return `
            <div class="shift-row ${s.active ? "active" : ""}">
              <span class="shift-name">${esc(s.label)}</span>
              <div class="shift-track">
                <div class="shift-bar" style="left:${left}%;width:${width}%"></div>
                <div class="shift-now-line" style="left:${nowPct.toFixed(1)}%"></div>
              </div>
              <span class="shift-cnt mono">${s.online_count}/${s.total_count}</span>
            </div>`;
        })
        .join("")
    );
  }

  function renderVaultView(d) {
    $("vault-full-stats").textContent = `${d.bank.distinct_items} items · ${d.bank.total_units} units · ${d.bank.total_slots}/${d.bank.max_slots} slots`;
    $("prof-full-cap").textContent = `Target: ${d.raid.prof_cap}`;

    // Category filter chips
    const cats = ["ALL", ...d.bank.categories.map((c) => c.category)];
    $("vault-cat-filters").innerHTML = cats
      .map(
        (c) =>
          `<button class="chip ${state.vaultCat === c ? "active" : ""}" data-vcat="${esc(c)}">${esc(c === "ALL" ? "All" : c)}</button>`
      )
      .join("");
    document.querySelectorAll("[data-vcat]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.vaultCat = btn.dataset.vcat;
        renderVaultView(state.data);
      });
    });

    // Filtered vault sections
    const q = state.vaultSearch;
    const sections = d.bank.categories
      .filter((c) => state.vaultCat === "ALL" || c.category === state.vaultCat)
      .map((cat) => {
        const items = q ? cat.items.filter((i) => i.name.toLowerCase().includes(q)) : cat.items;
        if (!items.length) return "";
        return `
          <div class="vault-cat-section">
            <h3><span>${esc(cat.category)}</span><span class="mono muted">${items.length} items · ${items.reduce((a, b) => a + b.qty, 0)} units</span></h3>
            <div class="vault-cat-grid">
              ${items
                .map(
                  (it) => `
                  <div class="mat-row">
                    <span class="mat-name" style="color:${it.color}">${esc(it.name)}</span>
                    <span class="mat-qty mono">×${it.qty}</span>
                  </div>`
                )
                .join("")}
            </div>
          </div>`;
      })
      .join("");
    $("vault-full-list").innerHTML = sections || `<div class="empty-note">No matching materials in vault.</div>`;

    // Full profession matrix
    $("prof-full-list").innerHTML = d.professions
      .map((p) => {
        const leaders = p.leaders
          .map(
            (l) =>
              `<span><b style="color:${l.class_color}">${esc(l.name)}</b> <span class="mono">${l.value}/${l.max}</span></span>`
          )
          .join("");
        return `
          <div class="prof-full-row">
            <div class="prof-full-top">
              <span>${esc(p.name)} <span class="muted">(${p.count} crafters)</span></span>
              <span class="mono" style="color:var(--gold-bright)">${p.max_value} / ${p.cap}</span>
            </div>
            <div class="bar-bg"><div class="bar-fill ${p.kind}" style="width:${p.pct}%"></div></div>
            <div class="prof-leaders-row">${leaders || '<span class="muted">None trained yet</span>'}</div>
          </div>`;
      })
      .join("");

    // Recent ledger
    $("ledger-list").innerHTML = d.ledger
      .map((l) => {
        const t = (l.time || "").slice(11, 16);
        const detail = l.item_name
          ? `${esc(l.item_name)} <span class="mono muted">×${l.count}</span>`
          : `${esc(l.note)} <span class="mono muted">${l.count > 0 ? l.count + "c" : ""}</span>`;
        return `
          <div class="ledger-row">
            <span class="mono muted">${esc(t)}</span>
            <span style="color:${l.class_color};font-weight:700">${esc(l.bot)}</span>
            <span class="act-badge ${esc(l.action)}">${esc(l.action)}</span>
            <div>${detail}</div>
          </div>`;
      })
      .join("");
  }

  function renderRosterView(d) {
    const cap = d.raid.level_cap;
    const f = state.rosterFilter;
    const rows = d.companions.filter((c) => {
      if (f === "online") return c.online;
      if (f === "tank") return c.role === "tank";
      if (f === "healer") return c.role === "healer";
      if (f === "dps") return c.role === "mdps" || c.role === "rdps";
      return true;
    });

    $("roster-total-badge").textContent = `${rows.length} shown · ${d.population.companions_online} online`;

    $("roster-tbody").innerHTML = rows
      .map((c) => {
        const pct = Math.min(100, Math.round((c.level / cap) * 100));
        const profs = (c.primaries || [])
          .map((p) => `${esc(p.short)} <span class="mono">${p.value}</span>`)
          .join(" · ");
        const statusTxt = c.online
          ? `<span style="color:var(--green)">●</span> ${esc(c.zone)}`
          : `<span class="muted">Offline · ${esc(c.zone)}</span>`;
        return `
          <tr class="${c.online ? "" : "offline-row"}">
            <td><a class="char-inline-link" href="/character/${encodeURIComponent(c.name.toLowerCase())}"><b style="color:${c.class_color}">${esc(c.name)}</b></a> <span class="muted">${esc(c.race)} ${esc(c.class_short)}</span></td>
            <td class="muted">${esc(SHIFT_LABELS[c.shift_group] || c.shift_group)}</td>
            <td>${esc(ROLE_LABELS[c.role] || c.role)}</td>
            <td class="mono">
              <div style="display:flex;align-items:center;gap:6px">
                <span style="min-width:24px">${c.level}</span>
                <div class="bar-bg" style="flex:1;min-width:48px;margin:0"><div class="bar-fill" style="width:${pct}%"></div></div>
              </div>
            </td>
            <td>${profs || '<span class="muted">—</span>'}</td>
            <td class="muted">${esc(c.archetype || "—")}</td>
            <td>${statusTxt}</td>
            <td class="num mono muted">${c.hours}h</td>
          </tr>`;
      })
      .join("");

    // Phase II Shadow Opportunity Board & Raid Readiness (Card II-07/II-08)
    const p2El = $("p2-summary-list");
    if (p2El) {
      const p2s = d.ai_brain && d.ai_brain.phase2_shadow;
      const p2r = d.ai_brain && d.ai_brain.phase2_readiness;
      const badge = $("p2-mode-badge");
      if (badge) {
        badge.textContent = p2s && p2s.coop_enabled_live ? "COOP LIVE" : "SHADOW / OFF";
      }
      const blocks = [];
      if (p2s && p2s.shadow_dungeons) {
        const hr = p2s.headroom || {};
        blocks.push(`
          <div class="stack-item">
            <div style="display:flex;justify-content:space-between">
              <b>Pilot Dungeons (Shadow Match)</b>
              <span class="act-badge">${hr.admit_ok ? "Headroom OK" : "Paused"}</span>
            </div>
            ${p2s.shadow_dungeons
              .map(
                (dg) =>
                  `<div class="muted" style="font-size:11px;margin-top:2px"><b>${esc(dg.name)}</b> (Lv ${esc(dg.level_band)}): T:${dg.eligible_counts.tank} H:${dg.eligible_counts.healer} D:${dg.eligible_counts.damage} · ${dg.feasible_now ? '<span style="color:var(--green)">Feasible</span>' : "Waiting"}</div>`
              )
              .join("")}
          </div>`);
      }
      if (p2r) {
        const sc = p2r.summary_counts || {};
        blocks.push(`
          <div class="stack-item">
            <div style="display:flex;justify-content:space-between">
              <b>${esc(p2r.tier_name || "Raid Readiness")}</b>
              <span class="mono muted">${sc.ready_available || 0} ready · ${sc.unknown || 0} unk</span>
            </div>
            <div class="muted" style="font-size:11px;margin-top:2px">${(p2r.bottlenecks || []).slice(0, 3).map(esc).join(" · ") || "Roster feasible"}</div>
          </div>`);
      }
      p2El.innerHTML = blocks.join("") || `<div class="empty-note">Run <code>phase2-shadow-match</code> &amp; <code>phase2-readiness</code> to populate.</div>`;
    }

    // Rep goals
    $("rep-goals-list").innerHTML =
      d.raid.rep_goals.length > 0
        ? d.raid.rep_goals
            .map(
              (r) => `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between">
                  <span><b style="color:${r.class_color}">${esc(r.bot)}</b> → ${esc(r.faction)}</span>
                  <span class="mono" style="color:var(--gold-bright)">${esc(r.rank)} (${r.to_exalted_pct}%)</span>
                </div>
                <div class="bar-bg"><div class="bar-fill" style="width:${r.to_exalted_pct}%"></div></div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">No active reputation goals.</div>`;

    // Work orders
    $("work-orders-list").innerHTML =
      d.work_orders.length > 0
        ? d.work_orders
            .map(
              (w) => `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between">
                  <b>${esc(w.item)} ×${w.qty}</b>
                  <span class="act-badge">${esc(w.status)}</span>
                </div>
                <div class="muted">${esc(w.requester)}${w.crafter ? " ← " + esc(w.crafter) : ""}${w.story ? " · " + esc(w.story) : ""}</div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">No guild work orders yet — create one in-game with <code>.companion life order</code>.</div>`;

    // Relationships
    $("relationships-list").innerHTML =
      d.relationships.length > 0
        ? d.relationships
            .map(
              (r) => `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between">
                  <b>${esc(r.a)} &amp; ${esc(r.b)}</b>
                  <span class="mono" style="color:var(--green)">Affinity ${r.affinity}</span>
                </div>
                <div class="muted">Runs: ${r.runs} · Helps: ${r.helps}${r.last_event ? " · " + esc(r.last_event) : ""}</div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">Bonds grow as guildies dungeon, craft, and fulfill orders together.</div>`;
  }

  function fmtModelShort(m) {
    if (!m) return "Offline";
    if (m === "fallback" || m === "fallback_after_api") return "Fallback";
    return String(m)
      .replace(/^claude-/, "")
      .replace(/-/g, " ")
      .replace(/\b\w/g, (ch) => ch.toUpperCase());
  }

  function fmtReason(code) {
    if (!code) return "";
    const s = String(code).replace(/[_-]+/g, " ").trim();
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
  }

  function fmtGoalBadge(gid) {
    const g = String(gid || "");
    if (g.startsWith("guild_order:")) return { icon: "📦", label: "Guild Order", cls: "cat-order" };
    if (g.startsWith("craft:")) return { icon: "⚒", label: "Crafting", cls: "cat-craft" };
    if (g.startsWith("gather:")) return { icon: "⛏", label: "Gathering", cls: "cat-gather" };
    if (g.startsWith("fish:")) return { icon: "🎣", label: "Fishing", cls: "cat-fish" };
    if (g.startsWith("quest:")) return { icon: "📜", label: "Questing", cls: "cat-quest" };
    if (g.startsWith("dungeon:")) return { icon: "⚔", label: "Dungeon Prep", cls: "cat-dungeon" };
    if (g.startsWith("explore:")) return { icon: "🧭", label: "Exploring", cls: "cat-explore" };
    if (g.startsWith("train:")) return { icon: "🎓", label: "Training", cls: "cat-train" };
    if (g.startsWith("bank:")) return { icon: "🏦", label: "Guild Bank", cls: "cat-bank" };
    if (g.startsWith("repair:")) return { icon: "🔧", label: "Vendor & Repair", cls: "cat-repair" };
    if (g.startsWith("profession:")) return { icon: "✦", label: "Secondaries", cls: "cat-craft" };
    return { icon: "🧠", label: "Focus", cls: "cat-quest" };
  }

  function fmtGoalShort(gid) {
    const g = String(gid || "");
    const parts = g.split(":");
    const pretty = (s) => String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    if (parts[0] === "guild_order") return `Order: ${pretty(parts[2] || parts[1])}`;
    if (parts[0] === "craft") return `Craft ${pretty(parts[1])}`;
    if (parts[0] === "gather") return `Gather ${pretty(parts[1])}`;
    if (parts[0] === "fish") return "Fish (Guild Food)";
    if (parts[0] === "quest") return "Zone Quests";
    if (parts[0] === "dungeon") return "Dungeon Prep";
    if (parts[0] === "explore") return parts[1] && parts[1].startsWith("near_") ? `With ${pretty(parts[1].slice(5))}` : "Explore Frontier";
    if (parts[0] === "train") return "Trainer Visit";
    if (parts[0] === "bank") return "Guild Vault";
    if (parts[0] === "repair") return "Vendor & Repair";
    if (parts[0] === "profession") return "Cooking & First Aid";
    return pretty(g);
  }

  function fmtPriorityPills(topWeights) {
    const labelMap = {
      craft: "Crafting",
      gather: "Gathering",
      fish: "Fishing",
      quest_work: "Questing",
      quest_hub: "Questing",
      quest_turnin: "Questing",
      quest_grind: "Questing",
      train: "Training",
      bank: "Banking",
      repair: "Repair",
      sell: "Vendoring",
      travel: "Travel",
      hearth: "Hearth/Rest",
    };
    const merged = {};
    for (const w of topWeights || []) {
      const lbl = labelMap[w.k] || w.k;
      const pct = Math.round((w.v - 1.0) * 100);
      if (Math.abs(pct) < 8) continue;
      if (!(lbl in merged) || Math.abs(pct) > Math.abs(merged[lbl])) {
        merged[lbl] = pct;
      }
    }
    return Object.entries(merged)
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
      .slice(0, 4)
      .map(([lbl, pct]) => {
        const up = pct > 0;
        const arrows = Math.abs(pct) >= 30 ? (up ? "↑↑" : "↓↓") : (up ? "↑" : "↓");
        const sign = up ? "+" : "";
        return `<span class="w-chip ${up ? "w-up" : "w-down"}">${esc(lbl)} <b>${arrows} ${sign}${pct}%</b></span>`;
      })
      .join("");
  }

  function showBrainToast(msg, isErr = false) {
    const el = $("brain-toast");
    if (!el) return;
    el.textContent = msg;
    el.className = `brain-toast show ${isErr ? "err" : "ok"}`;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = "brain-toast"; }, 5000);
  }

  async function postBrainApi(url, payload, okMsg) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        showBrainToast(data.error || `Request failed (${res.status})`, true);
        return;
      }
      showBrainToast(okMsg);
      const stRes = await fetch("/api/state", { cache: "no-store" });
      if (stRes.ok) {
        state.data = await stRes.json();
        render(state.data);
      }
    } catch (e) {
      showBrainToast("Network error", true);
    }
  }

  let adminUnlocked = false;
  function updateAdminBtn() {
    const b = $("btn-admin-auth");
    if (!b) return;
    b.textContent = adminUnlocked ? "🔓 Admin Unlocked" : "🔒 Unlock Admin";
    b.classList.toggle("unlocked", adminUnlocked);
  }
  async function checkAdminAuth() {
    try {
      const r = await fetch("/api/brain/auth-status", { cache: "no-store" });
      if (r.ok) {
        const j = await r.json();
        adminUnlocked = Boolean(j.authenticated);
        updateAdminBtn();
      }
    } catch (_) {}
  }
  if ($("btn-admin-auth")) {
    $("btn-admin-auth").addEventListener("click", async () => {
      if (adminUnlocked) {
        await postBrainApi("/api/brain/logout", {}, "Admin controls locked");
        adminUnlocked = false;
        updateAdminBtn();
        connectStream();
        return;
      }
      const tok = window.prompt("Enter AZEROTH_WATCH_ADMIN_TOKEN (from hearthcore/.env):");
      if (!tok || !tok.trim()) return;
      const res = await fetch("/api/brain/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tok.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        adminUnlocked = true;
        updateAdminBtn();
        showBrainToast("Admin controls unlocked for this browser");
        // Immediately fetch full admin snapshot & reconnect SSE with the new session cookie!
        try {
          const stRes = await fetch("/api/state", { cache: "no-store" });
          if (stRes.ok) {
            state.data = await stRes.json();
            render(state.data);
          }
        } catch (_) {}
        connectStream();
      } else {
        showBrainToast(data.error || "Invalid admin token", true);
      }
    });
  }
  checkAdminAuth();

  function fmtProposalBadge(status, advisory) {
    const st = String(status || "").toLowerCase();
    if (advisory || st === "advisory") return `<span class="prop-badge prop-adv" title="Advisory soft weights only (no dedicated native executor)">Advisory</span>`;
    if (st === "completed") return `<span class="prop-badge prop-ok">✓ Completed</span>`;
    if (st === "executing") return `<span class="prop-badge prop-exec">▶ Executing</span>`;
    if (st === "accepted") return `<span class="prop-badge prop-acc">● Accepted</span>`;
    if (st === "rejected") return `<span class="prop-badge prop-rej">✕ Rejected</span>`;
    if (st === "interrupted") return `<span class="prop-badge prop-int">⏸ Interrupted</span>`;
    if (st === "expired") return `<span class="prop-badge prop-exp">Expired</span>`;
    return `<span class="prop-badge prop-prop">Proposed</span>`;
  }

  document.querySelectorAll("#model-switch .ms-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = btn.dataset.model;
      postBrainApi("/api/brain/model", { model: m }, `Switched companion planner to ${fmtModelShort(m)}`);
    });
  });
  if ($("btn-run-brain")) {
    $("btn-run-brain").addEventListener("click", () => {
      postBrainApi("/api/brain/run", { target: "brain" }, "Planning due online companions now...");
    });
  }
  if ($("btn-run-qm")) {
    $("btn-run-qm").addEventListener("click", () => {
      postBrainApi("/api/brain/run", { target: "quartermaster" }, "Running Sonnet 5.5 Guild Quartermaster...");
    });
  }
  if ($("btn-sync-credit")) {
    $("btn-sync-credit").addEventListener("click", () => {
      const cur = (state.data && state.data.ai_brain && state.data.ai_brain.balance && state.data.ai_brain.balance.remaining_usd) || 96.5;
      const val = window.prompt("Enter your current Anthropic credit balance (USD) to sync:", cur.toFixed(2));
      if (val === null || val.trim() === "") return;
      const num = parseFloat(val);
      if (Number.isNaN(num) || num < 0) {
        showBrainToast("Enter a valid dollar amount", true);
        return;
      }
      postBrainApi("/api/brain/credit", { remaining_usd: num }, `Synced balance to $${num.toFixed(2)}`);
    });
  }

  function renderBrainView(d) {
    const ab = d.ai_brain || {};
    const t = ab.today || {};
    const bal = ab.balance || {};
    const all = ab.all_time || {};
    const ok = t.ai_ok || 0;
    const err = t.ai_err || 0;
    const fb = t.fallback || 0;
    const apiTotal = ok + err;
    const passPct = apiTotal > 0 ? Math.round((100 * ok) / apiTotal) : 100;
    const cap = ab.daily_cap || 500;
    const capPct = Math.min(100, Math.round((100 * apiTotal) / Math.max(1, cap)));
    const avgS = ((t.avg_latency_ms || 0) / 1000).toFixed(2);
    const activeList = ab.active_list || [];
    const activeCnt = activeList.filter((x) => x.active).length;

    const credTot = Number(bal.credit_total_usd || 98.0);
    const credRem = Number(bal.remaining_usd ?? credTot);
    const credSpent = Number(bal.spent_total_usd || 0);
    const credPct = Math.min(100, Math.max(0, Math.round((100 * credRem) / Math.max(0.01, credTot))));
    const burn = Number(bal.daily_burn_usd || 0);
    const daysLeft = Number(bal.days_left || 0);
    const resetLbl = bal.reset_date || "2026-12-02";

    // Highlight active model toggle button
    document.querySelectorAll("#model-switch .ms-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.model === ab.model);
    });

    const statsRows = (ab.model_stats || [])
      .map(
        (ms) =>
          `<div class="ms-stat-row"><span><b>${esc(fmtModelShort(ms.model))}</b> <span class="muted">${ms.role === "Guild Quartermaster" ? "QM" : "Bots"}</span></span><span class="mono">${ms.calls}× · $${Number(ms.cost_usd || 0).toFixed(2)}</span></div>`
      )
      .join("");

    const kpiHtml = `
      <div class="brain-kpi kpi-balance">
        <div class="brain-kpi-label">API Credit Baseline vs Budget</div>
        <div class="brain-kpi-val mono" style="color:var(--green)">~$${credRem.toFixed(2)} <span class="muted">/ $${credTot.toFixed(0)}</span></div>
        <div class="bar-bg" style="margin-top:4px"><div class="bar-fill" style="width:${credPct}%;background:var(--green)"></div></div>
        <div class="brain-kpi-sub">~$${credSpent.toFixed(2)} est · Cap $${Number((ab.budget && ab.budget.monthly_cap_usd) || 85).toFixed(0)}/mo · resets ${esc(resetLbl)} (${daysLeft}d)</div>
      </div>
      <div class="brain-kpi">
        <div class="brain-kpi-label">Daily Safety Cap</div>
        <div class="brain-kpi-val mono">${apiTotal} <span class="muted">/ ${cap} plans</span></div>
        <div class="bar-bg" style="margin-top:4px"><div class="bar-fill" style="width:${capPct}%;background:#7a8cff"></div></div>
        <div class="brain-kpi-sub">${activeCnt} of ${d.population.companions_online} online bots guided</div>
      </div>
      <div class="brain-kpi">
        <div class="brain-kpi-label">Claude Reliability</div>
        <div class="brain-kpi-val mono" style="color:${err > 0 ? "var(--amber)" : "var(--green)"}">${passPct}% <span class="muted">valid</span></div>
        <div class="brain-kpi-sub">${ok} ok · ${err} err · ${fb} fallback · ${avgS}s avg</div>
      </div>
      <div class="brain-kpi">
        <div class="brain-kpi-label">Guild Progress (AI-Guided)</div>
        <div class="brain-kpi-val mono">+${all.levels || 0} <span class="muted">lv</span> · +${all.skillups || 0} <span class="muted">skills</span></div>
        <div class="brain-kpi-sub">Today: +${t.outcome_levels || 0} lv, +${t.outcome_skillups || 0} sk · ${all.orders || 0} orders filled</div>
      </div>
      <div class="brain-kpi">
        <div class="brain-kpi-label">Spend Pace</div>
        <div class="brain-kpi-val mono">~$${burn.toFixed(2)} <span class="muted">/ day</span></div>
        <div class="brain-kpi-sub">Today: ~$${Number(bal.spent_today_usd || 0).toFixed(2)} (Bots + Quartermaster)</div>
      </div>
      <div class="brain-kpi">
        <div class="brain-kpi-label">Usage Breakdown</div>
        <div class="ms-stats-box">${statsRows || '<span class="muted">No calls yet</span>'}</div>
      </div>`;
    setHtmlIfChanged($("brain-kpi-strip"), kpiHtml);

    // Claude Guild Quartermaster & 40-char 1-300 supply deficits
    const qmPanel = $("qm-panel");
    const qm = ab.quartermaster;
    if (qmPanel && qm) {
      qmPanel.style.display = "";
      $("qm-model-badge").textContent = `${fmtModelShort(qm.model)} · ${(qm.bank_requests || []).length} vault targets · hourly`;
      const focusHtml = (qm.focus_skills || [])
        .map((s) => `<span class="w-chip w-up">${esc(s)}</span>`)
        .join("");
      setHtmlIfChanged($("qm-focus-pills"), focusHtml);
      setHtmlIfChanged($("qm-summary"), `<span>${esc(qm.strategy_summary || "")}</span>`);
      const defs = (qm.supply && qm.supply.top_deficits) || [];
      const defHtml = defs
        .slice(0, 12)
        .map((m) => {
          const pct = Math.min(100, Math.round(m.active_pct || 0));
          return `
            <div class="qm-def-card" title="Full 1-300 guild total: ${m.full_300_need} (${m.driven_by_all})">
              <div class="qm-def-top">
                <b>${esc(m.name)}</b>
                <span class="mono" style="color:${m.active_deficit > 0 ? "var(--amber)" : "var(--green)"}">${m.vault_have} / ${m.active_need}</span>
              </div>
              <div class="bar-bg" style="margin:3px 0"><div class="bar-fill" style="width:${pct}%;background:${pct >= 80 ? "var(--green)" : "#d6b25e"}"></div></div>
              <div class="muted" style="font-size:10px;display:flex;justify-content:space-between;gap:4px">
                <span>${esc(m.driven_by_active || m.category)}</span>
                <span class="mono">1-300: ${m.full_300_need}</span>
              </div>
            </div>`;
        })
        .join("");
      setHtmlIfChanged($("qm-deficits-grid"), defHtml);
    } else if (qmPanel) {
      qmPanel.style.display = "none";
    }

    // Issue #65: AI vs Fallback A/B outcome & cost panel
    const abPanel = $("ab-panel");
    const abRep = ab.ab_report;
    if (abPanel && abRep && abRep.by_source && Object.keys(abRep.by_source).length > 0) {
      abPanel.style.display = "";
      const hoPct = Number(abRep.holdout_pct || 0);
      $("ab-holdout-badge").textContent = `Holdout ${hoPct}% · ${abRep.window_days || 14}d`;
      const rec = abRep.recommendation || {};
      $("ab-tuning-advice").textContent = rec.action || "";
      const abRows = Object.entries(abRep.by_source)
        .sort((a, b) => (b[1].sessions || 0) - (a[1].sessions || 0))
        .map(([src, m]) => {
          const pct = Math.round(Number(m.progress_rate || 0) * 100);
          return `
            <tr>
              <td><b>${esc(src)}</b></td>
              <td class="num mono">${Number(m.sessions || 0)}</td>
              <td class="num mono" style="color:${pct >= 50 ? "var(--green)" : "var(--amber)"}">${pct}%</td>
              <td class="num mono">${Number(m.stalled || 0)}</td>
              <td class="num mono">${Number(m.avg_skillups || 0).toFixed(2)}</td>
              <td class="num mono">$${Number(m.total_cost_usd || 0).toFixed(4)}</td>
              <td class="num mono">$${Number(m.cost_per_session_usd || 0).toFixed(5)}</td>
            </tr>`;
        })
        .join("");
      setHtmlIfChanged($("ab-tbody"), abRows);
    } else if (abPanel) {
      abPanel.style.display = "none";
    }

    const bySlug = {};
    for (const c of d.companions || []) bySlug[c.name.toLowerCase()] = c;
    const mem = ab.memory || {};
    const opAlerts = mem.operational_alerts || [];
    const resCount = Object.keys(mem.active_reservations || {}).length;

    $("brain-active-badge").textContent = `${activeCnt} active · ${resCount} reserved orders${opAlerts.length ? ` · ⚠ ${opAlerts.length} stall alert${opAlerts.length > 1 ? "s" : ""}` : ""}`;
    const emptyPlansMsg = ab.redacted
      ? `<tr><td colspan="5" class="empty-note">🔒 Admin session locked or expired — click <b>🔒 Unlock Admin</b> above to view companion plans &amp; Claude decisions.</td></tr>`
      : `<tr><td colspan="5" class="empty-note">No AI Brain session plans recorded yet.</td></tr>`;
    const plansHtml =
      activeList.length > 0
        ? activeList
            .map((p) => {
              const comp = bySlug[p.character_id] || {};
              const name = comp.name || p.character_id;
              const col = comp.class_color || "#e8ecf2";
              const cat = fmtGoalBadge(p.primary_goal_id);
              const wChips = fmtPriorityPills(p.top_weights);
              const secs = (p.secondary_goal_ids || [])
                .map((s) => `<span class="sec-chip">${esc(fmtGoalShort(s))}</span>`)
                .join("");
              const srcCls = p.source === "anthropic" ? "src-ai" : "src-fb";
              const remTxt = p.active ? `${p.rem_minutes}m left` : "expired";
              const outParts = [];
              if (p.outcome_skillups > 0) outParts.push(`+${p.outcome_skillups} skills`);
              if (p.outcome_levels > 0) outParts.push(`+${p.outcome_levels} lv`);
              if (p.outcome_quests > 0) outParts.push(`+${p.outcome_quests} q`);
              if (p.outcome_xp > 0 && !p.outcome_levels) outParts.push(`+${p.outcome_xp} xp`);
              if (p.outcome_orders > 0) outParts.push(`+${p.outcome_orders} order`);
              const outHtml = outParts.length
                ? `<div style="color:var(--green);font-size:10px;margin-top:2px">${outParts.join(" · ")}</div>`
                : "";
              const propBadge = fmtProposalBadge(p.proposal_status, p.advisory_only);
              const memBits = [];
              if (p.open_intentions && p.open_intentions.length) {
                memBits.push(`<span class="mem-chip mem-intent">🎯 ${esc(p.open_intentions[0])}</span>`);
              }
              if (p.fatigue_category && p.fatigue_streak >= 2) {
                memBits.push(`<span class="mem-chip mem-variety">↻ ${p.fatigue_streak}× ${esc(p.fatigue_category)}</span>`);
              }
              if (p.stalled_goal_id) {
                memBits.push(`<span class="mem-chip mem-stall" title="2+ verified sessions with 0 goal-specific progress on ${esc(p.stalled_goal_id)}">⚠ Stall caution</span>`);
              }
              const memHtml = memBits.length ? `<div class="chip-wrap" style="margin-top:3px">${memBits.join("")}</div>` : "";
              return `
                <tr class="${p.active ? "" : "offline-row"}">
                  <td>
                    <div><a class="char-inline-link" href="/character/${encodeURIComponent(p.character_id)}"><b style="color:${col}">${esc(name)}</b></a> ${comp.level ? `<span class="muted mono">Lv ${comp.level}</span>` : ""}</div>
                    <div class="muted" style="font-size:10px;margin-top:2px"><span class="act-badge ${srcCls}">${esc(fmtModelShort(p.model || p.source))}</span> ${comp.archetype ? esc(comp.archetype) : ""}</div>
                  </td>
                  <td>
                    <div class="goal-headline"><span class="goal-cat ${cat.cls}">${cat.icon} ${esc(cat.label)}</span> ${propBadge} <span>${esc(p.primary_desc)}</span></div>
                    <div class="muted" style="font-size:11px;margin-top:2px">Why: ${esc(fmtReason(p.reason_code))}${p.proposal_reason ? ` · Native: ${esc(fmtReason(p.proposal_reason))}` : ""}</div>
                    ${memHtml}
                  </td>
                  <td><div class="chip-wrap">${secs || '<span class="muted">—</span>'}</div></td>
                  <td><div class="chip-wrap">${wChips || '<span class="muted">Balanced</span>'}</div></td>
                  <td class="num mono">${remTxt}${outHtml}</td>
                </tr>`;
            })
            .join("")
        : emptyPlansMsg;
    setHtmlIfChanged($("brain-plans-tbody"), plansHtml);

    const hist = ab.history || [];
    $("brain-hist-badge").textContent = ab.redacted ? "locked" : `${hist.length} recent`;
    const emptyHistMsg = ab.redacted
      ? `<div class="empty-note">🔒 Admin session locked or expired — click <b>🔒 Unlock Admin</b> above.</div>`
      : `<div class="empty-note">No decisions logged yet.</div>`;
    const histHtml =
      hist.length > 0
        ? hist
            .map((h) => {
              const comp = bySlug[h.character_id] || {};
              const name = comp.name || h.character_id;
              const col = comp.class_color || "#e8ecf2";
              const srcCls = h.source === "anthropic" ? "src-ai" : "src-fb";
              const cat = fmtGoalBadge(h.primary_goal_id);
              const errLine = h.error_note
                ? `<div class="brain-err-note">⚠ ${esc(h.error_note)}</div>`
                : "";
              return `
                <div class="brain-hist-item">
                  <div class="brain-hist-top">
                    <span><span class="mono muted">${esc(h.time_str)}</span> · <b style="color:${col}">${esc(name)}</b></span>
                    <span class="act-badge ${srcCls}">${esc(fmtModelShort(h.model || h.source))}</span>
                  </div>
                  <div style="font-size:11px;color:#dce3ff">${cat.icon} ${esc(h.primary_desc || fmtGoalShort(h.primary_goal_id))}</div>
                  <div class="muted" style="font-size:10px">${esc(fmtReason(h.reason_code))} · ${(h.latency_ms / 1000).toFixed(1)}s · ~$${Number(h.cost_usd || 0).toFixed(4)}</div>
                  ${errLine}
                </div>`;
            })
            .join("")
        : emptyHistMsg;
    setHtmlIfChanged($("brain-history-list"), histHtml);
  }

  function render(d) {
    if (d && typeof d.view_mode === "string") {
      const nextAuth = d.view_mode === "admin";
      if (nextAuth !== adminUnlocked) {
        adminUnlocked = nextAuth;
        updateAdminBtn();
      }
    }
    renderHeader(d);
    if (state.view === "overview") renderOverview(d);
    else if (state.view === "vault") renderVaultView(d);
    else if (state.view === "roster") renderRosterView(d);
    else if (state.view === "brain") renderBrainView(d);
  }

  let sseSource = null;
  function connectStream() {
    if (sseSource) {
      try { sseSource.close(); } catch (_) {}
      sseSource = null;
    }
    const es = new EventSource("/api/stream");
    sseSource = es;
    es.addEventListener("state", (ev) => {
      try {
        state.data = JSON.parse(ev.data);
        $("sse-dot").classList.remove("stale");
        render(state.data);
      } catch (_) {}
    });
    es.onerror = () => {
      $("sse-dot").classList.add("stale");
    };
  }

  async function init() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      if (res.ok) {
        state.data = await res.json();
        render(state.data);
      }
    } catch (_) {}

    connectStream();
  }

  init();
})();
