/* Azeroth Watch — Dedicated Character Sheet & Companion Direction Page */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const slugFromPath = () => {
    const parts = window.location.pathname.split("/").filter(Boolean);
    return parts.length >= 2 ? decodeURIComponent(parts[1]).toLowerCase() : "";
  };

  const state = {
    slug: slugFromPath(),
    data: null,
    lifeTab: "events",
    busy: false,
    dirtyDirective: false,
    slidingKey: null,
  };

  function showToast(msg, isErr = false) {
    const el = $("char-toast");
    if (!el) return;
    el.textContent = msg;
    el.className = `brain-toast show ${isErr ? "err" : "ok"}`;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = "brain-toast"; }, 5000);
  }

  function fmtGold(m) {
    if (!m) return "0c";
    const parts = [];
    if (m.gold > 0) parts.push(`<span class="gold-g">${m.gold.toLocaleString()}g</span>`);
    if (m.silver > 0 || m.gold > 0) parts.push(`<span class="gold-s">${m.silver}s</span>`);
    parts.push(`<span class="gold-c">${m.copper}c</span>`);
    return parts.join(" ");
  }

  function fmtReason(code) {
    if (!code) return "";
    const s = String(code).replace(/[_-]+/g, " ").trim();
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
  }

  function fmtRelTime(epochSec) {
    if (!epochSec) return "";
    const diff = Math.max(0, Math.floor(Date.now() / 1000) - Number(epochSec));
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  async function apiPost(action, payload, okMsg) {
    if (state.busy) return null;
    state.busy = true;
    try {
      const res = await fetch(`/api/character/${encodeURIComponent(state.slug)}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        showToast(data.error || `Action failed (${res.status})`, true);
        return null;
      }
      showToast(okMsg || "Updated!");
      await loadSheet(true);
      return data;
    } catch (_) {
      showToast("Network error", true);
      return null;
    } finally {
      state.busy = false;
    }
  }

  function renderHero(d) {
    const c = d.character;
    document.title = `${c.name} — Azeroth Watch`;
    $("char-crumb").textContent = `${c.name} · Lv ${c.level} ${c.race} ${c.class_name}`;

    const prof = c.profile || {};
    const sched = c.schedule || {};
    const statusBadge = c.dead
      ? `<span class="char-status dead">💀 Dead</span>`
      : c.online
        ? `<span class="char-status online">● Online</span>`
        : `<span class="char-status offline">○ Offline</span>`;
    const typeBadge = c.is_companion
      ? `<span class="badge mono">${esc(sched.shift_group || "companion")}${sched.always_online ? " · Always On" : ""}</span>`
      : `<span class="badge mono human-badge">★ Human Player</span>`;
    const pausedBadge = d.ai_brain && d.ai_brain.paused
      ? `<span class="badge mono paused-badge">⏸ AI Paused</span>`
      : "";
    const bioHtml = prof.bio
      ? `<div class="char-bio">${esc(prof.bio)}${prof.flaw ? ` <span class="muted">· Flaw: ${esc(prof.flaw)}</span>` : ""}</div>`
      : "";
    const tags = [prof.primary_spec, prof.archetype, prof.culture].filter(Boolean);
    const tagsHtml = tags.length
      ? `<div class="char-tags">${tags.map((t) => `<span class="sec-chip">${esc(t)}</span>`).join("")}</div>`
      : "";

    $("char-hero").innerHTML = `
      <div class="char-hero-inner" style="--cls-color:${esc(c.class_color)}">
        <div class="char-portrait-lg">
          <img class="portrait-race" src="/static/icons/race_${c.race_id}_${c.gender}.png" alt=""/>
          <img class="portrait-cls" src="/static/icons/class_${c.class_id}.png" alt=""/>
        </div>
        <div class="char-hero-main">
          <div class="char-hero-title">
            <h1 style="color:${esc(c.class_color)}">${esc(c.name)}</h1>
            <span class="char-lvl mono">Lv ${c.level}</span>
            ${statusBadge}
            ${typeBadge}
            ${pausedBadge}
          </div>
          <div class="char-hero-sub">
            <span>${esc(c.race)} ${esc(c.class_name)}</span>
            <span>·</span>
            <span>📍 ${esc(c.zone)}</span>
            <span>·</span>
            <span title="Hearthstone Bind Location">🏠 ${esc(c.homebind && c.homebind.zone ? c.homebind.zone : "Unbound")}</span>
            <span>·</span>
            <span class="mono">${fmtGold(c.money)}</span>
            <span>·</span>
            <span class="mono muted">${c.played_hours}h played</span>
          </div>
          <div class="char-xp-wrap" title="XP: ${c.xp.toLocaleString()} / ${c.xp_max.toLocaleString()} (${c.xp_pct}%)">
            <div class="xp-bar lg"><div class="xp-fill" style="width:${c.xp_pct}%"></div></div>
            <span class="mono muted">${c.xp_max > 0 ? `${c.xp.toLocaleString()} / ${c.xp_max.toLocaleString()} XP (${c.xp_pct}%)` : "Max Level"}</span>
          </div>
          ${tagsHtml}
          ${bioHtml}
        </div>
      </div>`;
  }

  function renderSlotHtml(s) {
    if (!s.item) {
      return `
        <div class="gear-slot empty">
          <div class="slot-lbl">${esc(s.label)}</div>
          <div class="slot-name muted">Empty</div>
        </div>`;
    }
    const it = s.item;
    const meta = [];
    if (it.item_level > 0) meta.push(`iLvl ${it.item_level}`);
    if (it.armor > 0) meta.push(`${it.armor} Armor`);
    if (it.enchanted) meta.push(`<span class="ench-tag">✦ Enchanted</span>`);
    return `
      <div class="gear-slot equipped" style="--q-color:${esc(it.color)}" title="${esc(it.name)} (${esc(it.quality_name)}, Entry #${it.item_entry})">
        <div class="slot-top">
          <span class="slot-lbl">${esc(s.label)}</span>
          <span class="slot-meta mono">${meta.join(" · ")}</span>
        </div>
        <div class="slot-name" style="color:${esc(it.color)}">${esc(it.name)}</div>
      </div>`;
  }

  function renderPaperdoll(d) {
    const slots = d.gear || [];
    const equippedCnt = slots.filter((s) => !!s.item).length;
    $("gear-ilvl-badge").textContent = `Avg iLvl ${d.character.avg_ilvl || "--"}`;
    $("gear-count-meta").textContent = `${equippedCnt} / ${slots.length} equipped`;

    const left = slots.filter((s) => s.side === "left");
    const right = slots.filter((s) => s.side === "right");
    const weapons = slots.filter((s) => s.side === "weapon");

    $("paperdoll-grid").innerHTML = `
      <div class="pd-columns">
        <div class="pd-col">${left.map(renderSlotHtml).join("")}</div>
        <div class="pd-col">${right.map(renderSlotHtml).join("")}</div>
      </div>
      <div class="pd-weapons">${weapons.map(renderSlotHtml).join("")}</div>`;
  }

  function renderStats(d) {
    const st = d.stats || {};
    const c = d.character;
    $("stats-power-meta").textContent = `HP ${Number(st.max_health || 0).toLocaleString()}${st.max_mana > 0 ? ` · ${c.power_type} ${Number(st.max_mana).toLocaleString()}` : ""}`;

    const items = [
      { k: "Max Health", v: Number(st.max_health || 0).toLocaleString(), hi: "var(--green)" },
      { k: c.power_type || "Mana", v: Number(st.max_mana || 0).toLocaleString(), hi: "var(--blue)" },
      { k: "Armor", v: Number(st.armor || 0).toLocaleString() },
      { k: "Strength", v: st.strength ?? 0 },
      { k: "Agility", v: st.agility ?? 0 },
      { k: "Stamina", v: st.stamina ?? 0 },
      { k: "Intellect", v: st.intellect ?? 0 },
      { k: "Spirit", v: st.spirit ?? 0 },
      { k: "Attack Power", v: st.attack_power ?? 0 },
      { k: "Ranged AP", v: st.ranged_attack_power ?? 0 },
      { k: "Spell Power", v: st.spell_power ?? 0 },
      { k: "Melee Crit", v: `${st.crit_pct ?? 0}%` },
      { k: "Spell Crit", v: `${st.spell_crit_pct ?? 0}%` },
      { k: "Dodge", v: `${st.dodge_pct ?? 0}%` },
      { k: "Parry", v: `${st.parry_pct ?? 0}%` },
      { k: "Block", v: `${st.block_pct ?? 0}%` },
    ];
    $("stats-grid").innerHTML = items
      .map(
        (x) => `
        <div class="stat-tile">
          <div class="stat-lbl">${esc(x.k)}</div>
          <div class="stat-val mono"${x.hi ? ` style="color:${x.hi}"` : ""}>${esc(x.v)}</div>
        </div>`
      )
      .join("");
  }

  function renderProfessions(d) {
    const profs = d.professions || [];
    $("profs-count-meta").textContent = `${profs.length} tracked`;
    $("char-profs-list").innerHTML = profs.length
      ? profs
          .map(
            (p) => `
            <div class="stack-item">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">
                <span><b>${esc(p.name)}</b> <span class="muted">${p.kind === "primary" ? "Primary" : "Secondary"}</span></span>
                <span class="mono">${p.value} / ${p.max}${p.at_trained_cap ? ` <span class="trainer-cap-tag">Visit Trainer!</span>` : ""}</span>
              </div>
              <div class="bar-bg"><div class="bar-fill ${p.kind === "primary" ? "craft" : "secondary"}" style="width:${p.pct}%"></div></div>
            </div>`
          )
          .join("")
      : `<div class="empty-note">No professions trained yet.</div>`;
  }

  function renderQuests(d) {
    const q = d.quests || { active: [], completed_count: 0 };
    $("quests-done-badge").textContent = `${q.completed_count || 0} completed`;
    $("quests-active-meta").textContent = `${(q.active || []).length} in log`;
    $("char-quests-list").innerHTML = (q.active || []).length
      ? q.active
          .map(
            (it) => `
            <div class="stack-item quest-row">
              <span class="mono muted">${it.level > 0 ? `[${it.level}]` : "[—]"}</span>
              <span class="quest-title">${esc(it.title)}</span>
              <span class="act-badge ${it.status === "complete" ? "src-ai" : ""}">${it.status === "complete" ? "Ready" : "Active"}</span>
            </div>`
          )
          .join("")
      : `<div class="empty-note">Quest log is empty.</div>`;
  }

  function renderReps(d) {
    const reps = d.reputations || [];
    $("char-reps-grid").innerHTML = reps.length
      ? reps
          .map(
            (r) => `
            <div class="stack-item rep-tile ${r.is_goal ? "rep-goal" : ""}">
              <div style="display:flex;justify-content:space-between;gap:6px">
                <span>${r.is_goal ? "★ " : ""}<b>${esc(r.name)}</b></span>
                <span class="mono" style="color:var(--gold-bright)">${esc(r.rank)} (${r.current}/${r.max})</span>
              </div>
              <div class="bar-bg"><div class="bar-fill" style="width:${r.tier_pct}%"></div></div>
            </div>`
          )
          .join("")
      : `<div class="empty-note">No visible faction reputations yet.</div>`;
  }

  function renderControls(d) {
    const col = $("char-control-col");
    const layout = $("char-layout");
    if (!d.character.is_companion) {
      col.style.display = "none";
      layout.classList.add("human-layout");
      return;
    }
    col.style.display = "";
    layout.classList.remove("human-layout");

    const canCtl = Boolean(d.can_control);
    const ab = d.ai_brain || {};
    const paused = Boolean(ab.paused);
    const meta = d.controls_meta || {};

    $("ctrl-locked-note").style.display = canCtl ? "none" : "";
    $("ctrl-body").classList.toggle("ctrl-disabled", !canCtl);
    $("btn-char-admin").style.display = canCtl ? "none" : "";
    $("ctrl-mode-badge").textContent = paused ? "PAUSED" : canCtl ? "LIVE CONTROLS" : "READ-ONLY";

    const pBtn = $("btn-pause-ai");
    pBtn.textContent = paused ? "▶ Resume AI" : "⏸ Pause AI";
    pBtn.classList.toggle("paused-btn", paused);
    pBtn.disabled = !canCtl;

    // 1. Focus Presets
    const presets = meta.presets || [];
    const pGrid = $("preset-grid");
    if (!pGrid._rendered) {
      pGrid._rendered = true;
      pGrid.innerHTML = presets
        .map(
          (p) => `
          <button class="preset-btn" data-preset="${esc(p.id)}" title="${esc(p.summary)}">
            <span class="preset-ico">${p.icon}</span>
            <span class="preset-lbl">${esc(p.label)}</span>
          </button>`
        )
        .join("");
      pGrid.querySelectorAll("[data-preset]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const hrs = parseInt($("preset-hours").value || "2", 10);
          const pid = btn.dataset.preset;
          apiPost("preset", { preset: pid, hours: hrs }, `Applied "${btn.querySelector(".preset-lbl").textContent}" for ${hrs}h`);
        });
      });
    }
    pGrid.querySelectorAll("button").forEach((b) => { b.disabled = !canCtl; });

    const ov = ab.override || {};
    $("override-status-line").textContent = paused
      ? "AI & preset weight overrides are currently paused for this companion."
      : ov.active
        ? `Active override: ${ov.source || "ai"} (${ov.rem_minutes}m remaining)`
        : "No active temporary override (using baseline/pinned weights).";

    // 2. Claude Directive
    const dir = ab.operator_directive;
    const dInput = $("directive-input");
    if (!state.dirtyDirective) {
      dInput.value = dir && dir.note ? dir.note : "";
    }
    dInput.disabled = !canCtl;
    $("btn-save-directive").disabled = !canCtl;
    $("btn-replan-now").disabled = !canCtl;
    $("btn-clear-directive").disabled = !canCtl;
    if (dir && dir.active) {
      $("directive-status-badge").textContent = dir.consumed_at > 0
        ? `Applied by Claude (${fmtRelTime(dir.consumed_at)})`
        : `Pending next Claude plan (set ${fmtRelTime(dir.updated_at)})`;
    } else {
      $("directive-status-badge").textContent = "No active note";
    }

    // 3. Manual Weight Sliders + Pin/Unpin
    const wList = $("weights-list");
    if (!state.slidingKey) {
      const groups = ["Questing", "Professions", "Town & Upkeep"];
      const byGroup = {};
      (d.weights || []).forEach((w) => {
        (byGroup[w.group] = byGroup[w.group] || []).push(w);
      });
      wList.innerHTML = groups
        .filter((g) => (byGroup[g] || []).length > 0)
        .map(
          (g) => `
          <div class="w-group">
            <div class="w-group-title">${esc(g)}</div>
            ${byGroup[g]
              .map(
                (w) => `
                <div class="weight-row ${w.pinned ? "is-pinned" : ""}" data-wkey="${esc(w.key)}">
                  <div class="w-meta">
                    <span class="w-label">${esc(w.label)}</span>
                    <span class="w-src mono ${w.pinned ? "src-manual" : ""}">${esc(w.effective_source)}</span>
                  </div>
                  <input type="range" class="w-slider" min="0.50" max="1.50" step="0.05" value="${Number(w.effective).toFixed(2)}" ${canCtl ? "" : "disabled"} />
                  <span class="w-val mono">${Number(w.effective).toFixed(2)}×</span>
                  <button class="pin-btn ${w.pinned ? "pinned" : ""}" ${canCtl ? "" : "disabled"} title="${w.pinned ? "Unpin manual lock (return to Claude/persona control)" : "Pin this weight as manual (Claude cannot override)"}">${w.pinned ? "📌 Pinned" : "Pin"}</button>
                </div>`
              )
              .join("")}
          </div>`
        )
        .join("");

      wList.querySelectorAll(".weight-row").forEach((row) => {
        const key = row.dataset.wkey;
        const slider = row.querySelector(".w-slider");
        const valEl = row.querySelector(".w-val");
        const pinBtn = row.querySelector(".pin-btn");
        slider.addEventListener("input", () => {
          state.slidingKey = key;
          valEl.textContent = `${parseFloat(slider.value).toFixed(2)}×`;
        });
        slider.addEventListener("change", async () => {
          state.slidingKey = null;
          const v = parseFloat(slider.value);
          await apiPost("weight", { key, value: v, pinned: true }, `Pinned ${key} to ${v.toFixed(2)}×`);
        });
        pinBtn.addEventListener("click", async () => {
          const isPinned = pinBtn.classList.contains("pinned");
          const v = parseFloat(slider.value);
          await apiPost(
            "weight",
            { key, value: v, pinned: !isPinned },
            !isPinned ? `Pinned ${key} at ${v.toFixed(2)}×` : `Unpinned ${key} (restored to AI/persona control)`
          );
        });
      });
    }

    // 4. Work Order Catalog
    const woSel = $("wo-item-select");
    const cat = meta.work_order_catalog || [];
    if (!woSel._populated && cat.length > 0) {
      woSel._populated = true;
      woSel.innerHTML = cat
        .map((it) => `<option value="${it.item_entry}" data-qty="${it.default_qty}">${esc(it.name)} (${esc(it.skill_name)} ${it.min_skill}+)</option>`)
        .join("");
      woSel.addEventListener("change", () => {
        const opt = woSel.selectedOptions[0];
        if (opt && opt.dataset.qty) $("wo-qty").value = opt.dataset.qty;
      });
    }
    $("wo-char-name").textContent = d.character.name;
    woSel.disabled = !canCtl;
    $("wo-qty").disabled = !canCtl;
    $("wo-assign-here").disabled = !canCtl;
    $("wo-story").disabled = !canCtl;
    $("btn-create-wo").disabled = !canCtl;

    // 5. Shift Schedule
    const sgSel = $("sched-group-select");
    const shifts = meta.shift_groups || [];
    if (!sgSel._populated && shifts.length > 0) {
      sgSel._populated = true;
      sgSel.innerHTML = shifts
        .map((s) => `<option value="${esc(s.id)}">${esc(s.label)} (${s.start_hour}:00–${s.end_hour}:00)</option>`)
        .join("");
    }
    if (d.character.schedule && !sgSel._dirty && !$("sched-always-online")._dirty) {
      sgSel.value = d.character.schedule.shift_group || "prime-time";
      $("sched-always-online").checked = Boolean(d.character.schedule.always_online);
    }
    sgSel.disabled = !canCtl;
    $("sched-always-online").disabled = !canCtl;
    $("btn-save-sched").disabled = !canCtl;
  }

  function renderBrain(d) {
    const ab = d.ai_brain;
    if (!ab) return;
    if (ab.redacted) {
      $("brain-state-badge").textContent = "LOCKED";
      $("char-brain-body").innerHTML = `<div class="empty-note">🔒 Unlock controls to view Claude session history and notes.</div>`;
      return;
    }
    const cur = ab.current_plan;
    $("brain-state-badge").textContent = cur && cur.active ? `${cur.rem_minutes}m left` : "Idle / Due";

    const memChips = [];
    (ab.open_intentions || []).forEach((it) => memChips.push(`<span class="mem-chip mem-intent">🎯 ${esc(it)}</span>`));
    if (ab.fatigue_category && ab.fatigue_streak >= 2) {
      memChips.push(`<span class="mem-chip mem-variety">↻ ${ab.fatigue_streak}× ${esc(ab.fatigue_category)}</span>`);
    }
    if (ab.stalled_goal_id) {
      memChips.push(`<span class="mem-chip mem-stall">⚠ Stalled: ${esc(ab.stalled_goal_id)}</span>`);
    }

    const curHtml = cur
      ? `
        <div class="cur-plan-box">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
            <b>${esc(cur.primary_desc || cur.primary_goal_id)}</b>
            <span class="act-badge ${cur.source === "anthropic" ? "src-ai" : "src-fb"}">${esc(cur.model || cur.source)}</span>
          </div>
          <div class="muted" style="margin-top:4px">
            Why: ${esc(fmtReason(cur.reason_code))} · Status: ${esc(cur.proposal_status || "proposed")}${cur.proposal_reason ? ` (${esc(fmtReason(cur.proposal_reason))})` : ""}
          </div>
          ${memChips.length ? `<div class="chip-wrap" style="margin-top:6px">${memChips.join("")}</div>` : ""}
        </div>`
      : `<div class="empty-note">No active AI Brain plan recorded yet.</div>`;

    const hist = (ab.recent_plans || []).slice(1, 6);
    const histHtml = hist.length
      ? `<div class="plan-hist-list">${hist
          .map((p) => {
            const out = [];
            if (p.outcome_levels > 0) out.push(`+${p.outcome_levels} lv`);
            if (p.outcome_skillups > 0) out.push(`+${p.outcome_skillups} sk`);
            if (p.outcome_quests > 0) out.push(`+${p.outcome_quests} q`);
            if (p.outcome_orders > 0) out.push(`+${p.outcome_orders} ord`);
            return `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between;gap:6px">
                  <span>${esc(p.primary_desc || p.primary_goal_id)}</span>
                  <span class="mono muted">${esc((p.planned_iso || "").slice(5, 16).replace("T", " "))}</span>
                </div>
                <div class="muted" style="font-size:11px">${esc(fmtReason(p.reason_code))}${out.length ? ` · <span style="color:var(--green)">${out.join(" · ")}</span>` : ""}</div>
              </div>`;
          })
          .join("")}</div>`
      : "";

    $("char-brain-body").innerHTML = curHtml + histHtml;
  }

  function renderLife(d) {
    const body = $("char-life-body");
    const tab = state.lifeTab;
    if (tab === "events") {
      const evs = d.recent_events || [];
      body.innerHTML = evs.length
        ? evs
            .map(
              (ev) => `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between;gap:6px">
                  <span><b>${esc(ev.type)}</b> · ${esc(ev.summary)}</span>
                  <span class="mono muted">${esc(ev.time_str)}</span>
                </div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">No recent guild events recorded.</div>`;
    } else if (tab === "bonds") {
      const bonds = d.social_bonds || [];
      body.innerHTML = bonds.length
        ? bonds
            .map(
              (b) => `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between;gap:6px">
                  <a class="char-inline-link" href="/character/${encodeURIComponent(b.partner_slug)}"><b style="color:${esc(b.partner_color)}">${esc(b.partner)}</b></a>
                  <span class="mono" style="color:var(--green)">Affinity ${b.affinity}</span>
                </div>
                <div class="muted">Runs: ${b.runs} · Helps: ${b.helps}${b.last_event ? ` · ${esc(b.last_event)}` : ""}</div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">No guild relationships recorded yet.</div>`;
    } else if (tab === "orders") {
      const orders = d.work_orders || [];
      body.innerHTML = orders.length
        ? orders
            .map(
              (w) => `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between;gap:6px">
                  <b>${esc(w.item_name)} ×${w.quantity}</b>
                  <span class="act-badge ${w.assigned_here ? "src-ai" : ""}">${esc(w.status)}</span>
                </div>
                <div class="muted">Req: ${esc(w.requester)}${w.crafter ? ` · Crafter: ${esc(w.crafter)}` : ""}${w.story ? ` · ${esc(w.story)}` : ""}</div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">No active or recent work orders.</div>`;
    } else if (tab === "ledger") {
      const ld = d.recent_ledger || [];
      body.innerHTML = ld.length
        ? ld
            .map(
              (l) => `
              <div class="stack-item">
                <div style="display:flex;justify-content:space-between;gap:6px">
                  <span><span class="act-badge ${esc(l.action)}">${esc(l.action)}</span> ${esc(l.item_name || l.note || "action")} <span class="mono muted">${l.count > 0 ? `×${l.count}` : ""}</span></span>
                  <span class="mono muted">${esc((l.time || "").slice(5, 16))}</span>
                </div>
              </div>`
            )
            .join("")
        : `<div class="empty-note">No recent bank or craft ledger entries.</div>`;
    }
  }

  function renderAll(d) {
    $("char-loading").style.display = "none";
    $("char-error").style.display = "none";
    $("char-main").style.display = "";
    renderHero(d);
    renderPaperdoll(d);
    renderStats(d);
    renderProfessions(d);
    renderQuests(d);
    renderReps(d);
    renderControls(d);
    renderBrain(d);
    renderLife(d);
  }

  async function loadSheet(silent = false) {
    if (!state.slug) {
      $("char-loading").style.display = "none";
      $("char-error").style.display = "";
      $("char-error").textContent = "Missing character name in URL.";
      return;
    }
    try {
      const res = await fetch(`/api/character/${encodeURIComponent(state.slug)}`, { cache: "no-store" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        if (!silent) {
          $("char-loading").style.display = "none";
          $("char-error").style.display = "";
          $("char-error").textContent = err.error || `Character "${state.slug}" not found (${res.status}).`;
        }
        return;
      }
      state.data = await res.json();
      renderAll(state.data);
    } catch (_) {
      if (!silent) {
        $("char-loading").style.display = "none";
        $("char-error").style.display = "";
        $("char-error").textContent = "Could not connect to Azeroth Watch server.";
      }
    }
  }

  // Event bindings
  $("btn-char-refresh").addEventListener("click", () => loadSheet(false));

  $("btn-char-admin").addEventListener("click", async () => {
    const tok = window.prompt("Enter AZEROTH_WATCH_ADMIN_TOKEN (from hearthcore/.env):");
    if (!tok || !tok.trim()) return;
    const res = await fetch("/api/brain/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: tok.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      showToast("Controls unlocked for this browser");
      await loadSheet(true);
    } else {
      showToast(data.error || "Invalid admin token", true);
    }
  });

  $("btn-pause-ai").addEventListener("click", () => {
    const currentlyPaused = Boolean(state.data && state.data.ai_brain && state.data.ai_brain.paused);
    apiPost("pause", { paused: !currentlyPaused }, !currentlyPaused ? "Paused AI overrides for this companion" : "Resumed AI overrides");
  });

  $("directive-input").addEventListener("input", () => { state.dirtyDirective = true; });
  $("btn-save-directive").addEventListener("click", async () => {
    const note = $("directive-input").value.trim();
    const res = await apiPost("directive", { note, replan: false }, note ? "Saved note for Claude's next plan" : "Cleared directive note");
    if (res) state.dirtyDirective = false;
  });
  $("btn-replan-now").addEventListener("click", async () => {
    const note = $("directive-input").value.trim();
    const res = await apiPost("directive", { note, replan: true }, "Saved note & triggered Claude replan now!");
    if (res) state.dirtyDirective = false;
  });
  $("btn-clear-directive").addEventListener("click", async () => {
    const res = await apiPost("directive", { note: "", replan: false }, "Cleared directive note");
    if (res) {
      $("directive-input").value = "";
      state.dirtyDirective = false;
    }
  });

  $("btn-create-wo").addEventListener("click", async () => {
    const entry = parseInt($("wo-item-select").value || "0", 10);
    const qty = parseInt($("wo-qty").value || "5", 10);
    const story = $("wo-story").value.trim();
    const assign = $("wo-assign-here").checked;
    if (!entry) {
      showToast("Select an item first", true);
      return;
    }
    const res = await apiPost("work-order", { item_entry: entry, quantity: qty, story, assign_to_character: assign }, "Created guild work order!");
    if (res) $("wo-story").value = "";
  });

  $("sched-group-select").addEventListener("change", () => { $("sched-group-select")._dirty = true; });
  $("sched-always-online").addEventListener("change", () => { $("sched-always-online")._dirty = true; });
  $("btn-save-sched").addEventListener("click", async () => {
    const sg = $("sched-group-select").value;
    const ao = $("sched-always-online").checked;
    const res = await apiPost("schedule", { shift_group: sg, always_online: ao }, `Updated shift schedule to ${sg}`);
    if (res) {
      $("sched-group-select")._dirty = false;
      $("sched-always-online")._dirty = false;
    }
  });

  $("life-tabs").querySelectorAll("[data-ltab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.lifeTab = btn.dataset.ltab;
      $("life-tabs").querySelectorAll("[data-ltab]").forEach((b) => b.classList.toggle("active", b === btn));
      if (state.data) renderLife(state.data);
    });
  });

  loadSheet(false);
  setInterval(() => {
    if (!state.dirtyDirective && !state.slidingKey) {
      loadSheet(true);
    }
  }, 15000);
})();
