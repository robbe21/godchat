// De klok voor geplande taken: onder elk project in de chatlijst één rustige rij per geplande taak (klok.py, klok_lader.py).
// Eén IIFE zonder globale namen; leest /api/klok elke 25 s en na een eigen klik. index.html tekent de chatlijst
// (#boomLijst) opnieuw wanneer ze verandert; een MutationObserver haakt de rijen er daarna weer in, zonder dat
// index.html iets van de klok weet. De gewone taakchat-rij van een ronde (<onder>--r-<id>) verbergt dit bestand:
// de klokrij neemt haar plaats in, en een tik op de klokrij opent die chat.
(() => {
  "use strict";
  const ELKE = 25000;
  const DAG = ["", "ma", "di", "wo", "do", "vr", "za", "zo"];
  const MAAND = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"];
  const TEKEN = { klaar: "✓", "klaar (niet zelf afgesloten)": "✓", "klaar (vervangen)": "✓", overgeslagen: "–",
    geweigerd: "✕", fout: "!", verloren: "!", loopt: "…", "loopt lang": "…", "wacht op de vorige ronde": "…" };

  let data = null;        // het laatste antwoord van /api/klok
  let bezig = false;
  const melding = {};     // id → tekst na een klik (een weigering), een tijdje zichtbaar
  let obs = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const kort = (s, n) => { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length <= n ? s : s.slice(0, n - 1) + "…"; };

  // --- tijd: in de tijdzone van het rooster (tz uit /api/klok), ook als de telefoon elders is -------------------
  function maakFmt(tz) {
    const o = { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", weekday: "short" };
    try { return new Intl.DateTimeFormat("nl-BE", { ...o, timeZone: tz }); } catch (e) { return new Intl.DateTimeFormat("nl-BE", o); }
  }
  let fmtTz = "Europe/Brussels";
  let fmt = maakFmt(fmtTz);
  function delen(d) {
    const p = {};
    for (const x of fmt.formatToParts(d)) p[x.type] = x.value;
    const dag = new Date(Date.UTC(+p.year, +p.month - 1, +p.day));
    return { y: +p.year, m: +p.month, d: +p.day, uur: `${p.hour}:${p.minute}`, wd: dag.getUTCDay() || 7, dagnr: dag.getTime() / 86400000 };
  }
  function wanneer(iso, vooruit) {   // "vandaag 09:42", "morgen 09:42", "gisteren 08:04", "di 09:42", "di 1 dec 09:42"
    const t = new Date(iso || "");
    if (isNaN(t)) return "";
    const a = delen(t), n = delen(new Date());
    const v = a.dagnr - n.dagnr;
    if (v === 0) return `vandaag ${a.uur}`;
    if (v === 1) return `morgen ${a.uur}`;
    if (v === -1) return `gisteren ${a.uur}`;
    if ((vooruit && v > 1 && v < 7) || (!vooruit && v < -1 && v > -7)) return `${DAG[a.wd]} ${a.uur}`;
    return `${DAG[a.wd]} ${a.d} ${MAAND[a.m - 1]} ${a.uur}`;
  }
  function rooster(t) {   // "ma 08:00", "werkdagen 09:42", "elke dag 07:00, 19:00"
    const d = Array.isArray(t.dagen) ? [...t.dagen].sort() : [];
    const s = d.join(",");
    const dagen = s === "1,2,3,4,5" ? "werkdagen" : s === "1,2,3,4,5,6,7" ? "elke dag" : s === "6,7" ? "weekend"
      : d.map((x) => DAG[x] || "?").join(", ");
    return `${dagen} ${(Array.isArray(t.uren) ? t.uren : []).join(", ")}`.trim();
  }

  // --- stijl ---------------------------------------------------------------------------------------------------
  const STIJL = `
.boom li.klok-rij { display: block; cursor: default; padding: 8px 10px; }
.boom li.klok-rij.klok-kan { cursor: pointer; }
.boom li.klok-verborgen { display: none !important; }
.klok-r1 { display: flex; align-items: center; gap: 8px; min-width: 0; }
.klok-ico { flex: none; width: 14px; height: 14px; color: var(--zacht); }
.klok-rij.klok-aan .klok-ico { color: var(--accent); }
.klok-naam { font-size: 14px; font-weight: 500; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.klok-wanneer { margin-left: auto; flex: none; font-size: 12px; color: var(--flauw); white-space: nowrap; }
.klok-r2, .klok-r3, .klok-r4 { font-size: 12.5px; line-height: 1.4; color: var(--zacht); margin: 2px 0 0 22px; min-width: 0; }
.klok-r3 { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.klok-r3 .klok-st { font-weight: 600; }
.klok-st.klok-goed { color: var(--accent); }
.klok-st.klok-mis { color: var(--fout); }
.klok-st.klok-bezig { color: var(--werkt); }
.klok-st.klok-stil { color: var(--flauw); }
.klok-r4 { color: var(--wacht); }
.klok-r4.klok-fout { color: var(--fout); }
.klok-knoppen { display: flex; align-items: center; gap: 10px; margin: 6px 0 0 22px; }
.klok-schak { position: relative; flex: none; width: 40px; height: 24px; border-radius: 999px; border: 1px solid var(--lijn);
  background: var(--bg); padding: 0; cursor: pointer; transition: background var(--snel, 160ms) ease, border-color var(--snel, 160ms) ease; }
.klok-schak::after { content: ""; position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%;
  background: var(--vlak); box-shadow: 0 1px 2px rgba(0,0,0,.25); transition: transform var(--snel, 160ms) var(--uit, ease); }
.klok-schak[aria-checked="true"] { background: var(--accent); border-color: var(--accent); }
.klok-schak[aria-checked="true"]::after { transform: translateX(16px); }
.klok-schak:disabled { opacity: .45; cursor: not-allowed; }
.klok-schak:focus-visible, .klok-nu:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.klok-lbl { font-size: 12.5px; color: var(--zacht); }
.klok-nu { margin-left: auto; font: inherit; font-size: 12.5px; color: var(--inkt); background: transparent; border: 1px solid var(--lijn);
  border-radius: 999px; padding: 5px 12px; min-height: 30px; cursor: pointer; }
.klok-nu:hover { border-color: var(--zacht); }
.klok-nu:disabled { opacity: .45; cursor: not-allowed; }
.klok-algemeen { margin: 4px 10px 8px; font-size: 12.5px; color: var(--fout); }
@media (prefers-reduced-motion: reduce) { .klok-schak, .klok-schak::after { transition: none; } }
.klok-pagina { flex: none; margin: 8px 16px 0; padding: 10px 12px; border: 1px solid var(--lijn); border-radius: 10px;
  background: var(--vlak); font-size: 13px; line-height: 1.45; color: var(--zacht); max-height: 42vh; overflow-y: auto; }
@media (max-width: 959px) { .klok-pagina { max-height: none; margin: 8px 12px 0; } }
.kp-r1 { color: var(--inkt); }
.kp-voorstel { margin-top: 6px; color: var(--wacht); }
.kp-archief { margin-top: 6px; color: var(--inkt); }
.kp-link { font: inherit; padding: 0; border: 0; background: none; color: var(--accent); text-decoration: underline; cursor: pointer; min-height: 0; }
.klok-pagina details { margin-top: 8px; }
.klok-pagina summary { cursor: pointer; color: var(--inkt); font-weight: 500; padding: 2px 0; }
.kp-opdracht pre { white-space: pre-wrap; word-break: break-word; font: 12.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  max-height: 30vh; overflow: auto; margin: 6px 0 0; color: var(--inkt); }
.kp-rondes ul { list-style: none; margin: 6px 0 0; padding: 0; }
.kp-ronde { padding: 6px 8px; border-radius: 8px; }
.kp-ronde.kp-kan { cursor: pointer; }
.kp-ronde.kp-kan:hover { background: var(--bg); }
.kp-ronde.kp-kan:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.kp-ronde.kp-actief { background: var(--bg); box-shadow: inset 2px 0 0 var(--accent); }
.kp-wanneer { color: var(--inkt); }
.kp-ronde .klok-st { font-weight: 600; }
.kp-regel { margin-top: 2px; }
.kp-tip, .kp-leeg { margin-top: 8px; color: var(--flauw); }
body.klok-archief #gesprek .invoer { display: none; }
body.klok-zonder-chat #model, body.klok-zonder-chat #effort { display: none !important; }
`;
  function stijl() {
    if (document.getElementById("klok-stijl")) return;
    const s = document.createElement("style");
    s.id = "klok-stijl";
    s.textContent = STIJL;
    document.head.appendChild(s);
  }
  const ICO = `<svg class="klok-ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.3" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 4.6V8l2.3 1.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`;

  function soortVan(status) {
    if (/^klaar/.test(status)) return "klok-goed";
    if (status === "fout" || status === "verloren" || status === "geweigerd") return "klok-mis";
    if (/^loopt|^wacht/.test(status)) return "klok-bezig";
    return "klok-stil";
  }

  function rijHtml(t, kan) {
    const l = t.laatste;
    let r3 = "";
    if (l && l.status) {
      const moment = wanneer(l.einde || l.gestart || l.gezien || l.slot, false);
      r3 = `<div class="klok-r3"><span class="klok-st ${soortVan(l.status)}">${esc(TEKEN[l.status] || "·")} ${esc(l.status)}</span>`
        + `${moment ? ` · ${esc(moment)}` : ""}${l.regel ? ` — ${esc(kort(l.regel, 140))}` : ""}</div>`;
    }
    const volgende = t.aan ? (t.volgende ? `volgende: ${wanneer(t.volgende, true)}` : "geen volgend moment") : "uit";
    const extra = [];
    if (t.fout) extra.push(`<div class="klok-r4 klok-fout">${esc(t.fout)}</div>`);
    else if (t.desktop && t.desktop.aan) extra.push(`<div class="klok-r4">${/niet te lezen/.test(t.desktop.uitleg || "")
      ? esc(t.desktop.uitleg) : "staat nog aan in de desktop-app: zet ze daar eerst uit"}</div>`);
    if (melding[t.id]) extra.push(`<div class="klok-r4 klok-fout">${esc(melding[t.id].tekst)}</div>`);
    const kanAan = t.aan || (!t.fout && !(t.desktop && t.desktop.aan));
    return `<li class="kind taak klok-rij${t.onder === "god" ? " ondergod" : ""}${t.aan ? " klok-aan" : ""}${kan ? " klok-kan" : ""}" data-klok="${esc(t.id)}"${kan ? ` tabindex="0" role="button" aria-label="${esc(t.naam)}: open de pagina met alle rondes"` : ""}>
      <div class="klok-r1">${ICO}<span class="klok-naam">${esc(t.naam)}</span><span class="klok-wanneer">${esc(rooster(t))}</span></div>
      <div class="klok-r2">${esc(volgende)}</div>
      ${r3}${extra.join("")}
      <div class="klok-knoppen">
        <button type="button" class="klok-schak" role="switch" aria-checked="${t.aan ? "true" : "false"}" aria-label="${esc(t.naam)} aan of uit"${kanAan ? "" : " disabled"} data-klok-zet="${esc(t.id)}"></button>
        <span class="klok-lbl">${t.aan ? "aan" : "uit"}</span>
        ${t.aan && !t.fout && t.soort !== "motor" ? `<button type="button" class="klok-nu" data-klok-nu="${esc(t.id)}">Nu draaien</button>` : ""}
      </div>
    </li>`;
  }

  // --- inhaken in de chatlijst -----------------------------------------------------------------------------------
  function teken() {
    const ul = document.getElementById("boomLijst");
    if (!ul) return;
    if (obs) obs.disconnect();
    try {
      ul.querySelectorAll("li.klok-rij, li.klok-algemeen").forEach((x) => x.remove());
      ul.querySelectorAll("li.klok-verborgen").forEach((x) => x.classList.remove("klok-verborgen"));
      if (!data) return;
      const taken = Array.isArray(data.taken) ? data.taken : [];
      const sleutels = new Set(taken.map((t) => t.chat).filter(Boolean));
      // alleen de open ronde in de gewone lijst verbergen; een gesloten ronde staat onder "Afgesloten" (li.los) en mag
      // daar blijven: daar staat ze niet naast haar klokrij
      ul.querySelectorAll("li[data-chat]:not(.los)").forEach((li) => {
        if (sleutels.has(li.dataset.chat)) li.classList.add("klok-verborgen");
      });
      const perOuder = new Map();
      for (const t of taken) { if (!perOuder.has(t.onder)) perOuder.set(t.onder, []); perOuder.get(t.onder).push(t); }
      for (const [onder, lijst] of perOuder) {
        const ouder = [...ul.querySelectorAll("li[data-chat]:not(.los)")].find((li) => li.dataset.chat === onder);
        if (!ouder) continue;   // onbekend project: de fout staat in /api/klok; er is geen plek om ze te tonen
        let na = ouder;   // na de taakchats van dit project
        while (na.nextElementSibling && na.nextElementSibling.classList.contains("taak")) na = na.nextElementSibling;
        na.insertAdjacentHTML("afterend", lijst.map((t) => rijHtml(t, !!t.chat && t.soort !== "motor")).join(""));
      }
      if (data.fout) {
        const li = document.createElement("li");
        li.className = "klok-algemeen";
        li.textContent = "Klok: " + data.fout;
        ul.appendChild(li);
      }
    } finally {
      if (obs) obs.observe(ul, { childList: true });
    }
  }

  function zoek(id) { return data && Array.isArray(data.taken) ? data.taken.find((t) => t.id === id) : null; }

  function meld(id, tekst) {
    melding[id] = { tekst, tot: Date.now() + 12000 };
    setTimeout(() => { if (melding[id] && melding[id].tot <= Date.now()) { delete melding[id]; teken(); } }, 12500);
    teken();
  }

  async function post(pad, body, id) {
    try {
      const r = await fetch(pad, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      let d = null;
      try { d = await r.json(); } catch (e) { /* geen JSON */ }
      if (!r.ok || !d || d.ok === false) meld(id, (d && d.fout) || `mislukt (${r.status})`);
      else delete melding[id];
    } catch (e) {
      meld(id, "geen verbinding met de app");
    }
    await haal(true);
  }

  function klik(e) {
    const zet = e.target.closest("[data-klok-zet]");
    const nu = e.target.closest("[data-klok-nu]");
    const rij = e.target.closest("li.klok-rij");
    if (!rij) return;
    e.stopPropagation();
    if (zet) {
      const t = zoek(zet.dataset.klokZet);
      if (t && !zet.disabled) { zet.disabled = true; post("/api/klok/zet", { id: t.id, aan: !t.aan }, t.id); }
      return;
    }
    if (nu) {
      const t = zoek(nu.dataset.klokNu);
      if (t && !nu.disabled && window.confirm("Nu draaien? Dit neemt de plaats in van de ronde van vandaag.")) {
        nu.disabled = true;
        post("/api/klok/nu", { id: t.id }, t.id);
      }
      return;
    }
    const t = zoek(rij.dataset.klok);
    if (t && t.chat && rij.classList.contains("klok-kan") && typeof window.kies === "function") window.kies(t.chat, true);
  }

  // --- de pagina van een automatisering ----------------------------------------------------------------------
  // Een tik op de rij opent de chat van de taak (<onder>--r-<id>), ook als er nog geen ronde liep. Boven het gesprek
  // zet dit bestand een blok met het rooster, de opdracht en alle rondes; een oude ronde opent als <sleutel>@<stempel>
  // (alleen lezen, de server haalt ze uit data/archief). Typen gaat via /api/zeg: de motor stuurt het naar de chat van
  // de taak (klok.py → praat). index.html weet hier niets van; alles hangt aan #beurten en de kop.
  let detail = null, detailId = null, detailT = 0, detailBezig = false;
  let openOpdracht = false, openRondes = true;
  const gekozenNu = () => (typeof gekozen === "string" ? gekozen : null);   // de chat die index.html toont
  function taakVan(id) {
    if (!id || !data || !Array.isArray(data.taken)) return null;
    const sl = id.split("@")[0];
    return data.taken.find((t) => t.chat === sl && t.soort !== "motor") || null;
  }
  function stempelTekst(st) {   // "2026-11-09-0800" → "9 nov 08:00"
    const m = /^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})/.exec(st || "");
    return m ? `${+m[3]} ${MAAND[+m[2] - 1]} ${m[4]}:${m[5]}` : st;
  }
  async function haalDetail(id, meteen) {
    if (detailBezig || (!meteen && detailId === id && Date.now() - detailT < 20000)) return;
    detailBezig = true;
    try {
      const r = await fetch(`/api/klok/taak?id=${encodeURIComponent(id)}`, { cache: "no-store" });
      if (r.ok) { detail = await r.json(); detailId = id; detailT = Date.now(); }
    } catch (e) { /* laten staan */ }
    finally { detailBezig = false; }
    pagina(false);
  }
  function paginaHtml(t, g, d) {
    const st = g.includes("@") ? g.split("@")[1] : null;
    const bekijkt = st || "huidig";
    const r = [];
    const volgende = t.aan ? (t.volgende ? `volgende: ${wanneer(t.volgende, true)}` : "aan") : "staat uit";
    r.push(`<div class="kp-r1">${esc(rooster(t))} · ${esc(volgende)} · ${esc(t.model || "model standaard")}${t.effort ? ", " + esc(t.effort) : ""}</div>`);
    if (d && d.voorstel) r.push(`<div class="kp-voorstel">Een aanpassing wacht op je tik: ${esc(d.voorstel.wat || "")}</div>`);
    if (st) r.push(`<div class="kp-archief">Je leest de ronde van ${esc(stempelTekst(st))}. <button type="button" class="kp-link" data-kp-open="huidig">Naar de nieuwste</button>, daar kun je typen.</div>`);
    if (d && d.opdracht) r.push(`<details class="kp-opdracht" data-kp="opdracht"${openOpdracht ? " open" : ""}><summary>Opdracht</summary><pre>${esc(d.opdracht)}</pre></details>`);
    const rondes = d && Array.isArray(d.rondes) ? d.rondes : [];
    const li = (x) => {
      const kan = !!x.gesprek;
      const actief = kan && x.gesprek === bekijkt;
      const extra = (x.ingehaald ? " · ingehaald" : "") + (x.handmatig ? " · Nu draaien" : "");
      return `<li class="kp-ronde${kan ? " kp-kan" : ""}${actief ? " kp-actief" : ""}"${kan ? ` data-kp-open="${esc(x.gesprek)}" tabindex="0" role="button"` : ""}>`
        + `<span class="kp-wanneer">${esc(wanneer(x.gestart || x.slot, false) || "?")}</span> · `
        + `<span class="klok-st ${soortVan(x.status || "")}">${esc(TEKEN[x.status] || "·")} ${esc(x.status || "")}</span>${esc(extra)}`
        + `${x.regel ? `<div class="kp-regel">${esc(kort(x.regel, 220))}</div>` : ""}</li>`;
    };
    r.push(`<details class="kp-rondes" data-kp="rondes"${openRondes ? " open" : ""}><summary>Rondes${d ? ` (${rondes.length})` : ""}</summary>`
      + (rondes.length ? `<ul>${rondes.map(li).join("")}</ul>` : `<p class="kp-leeg">${d ? "Nog geen ronde gelopen." : "laden…"}</p>`)
      + `</details>`);
    if (!st) r.push(`<div class="kp-tip">Typ hieronder om iets te vragen of de automatisering aan te passen. Een aanpassing gaat pas door na je tik.</div>`);
    return r.join("");
  }
  function kopZetten(t, g) {
    const st = g.includes("@") ? g.split("@")[1] : null;
    const echt = typeof stand !== "undefined" && stand && Array.isArray(stand.chats) && stand.chats.some((c) => c.id === g);
    const ouder = typeof chat === "function" ? chat(t.onder).naam : t.onder;
    const naam = document.getElementById("gesprekNaam");
    const wil = `${ouder} › ${t.naam}`;
    if (naam && naam.textContent !== wil) naam.textContent = wil;
    // zonder chat (nog geen ronde, of een oude ronde) kent de motor geen model of effort voor deze sleutel
    if (document.body.classList.contains("klok-zonder-chat") === echt) document.body.classList.toggle("klok-zonder-chat", !echt);
    if (echt) return;
    const ts = document.getElementById("gesprekToestand");
    const tekst = st ? "oudere ronde" : t.aan ? "gepland" : "uit";
    const klasse = "toestand " + (st ? "klaar" : t.aan ? "slaapt" : "uit");
    if (ts && ts.textContent !== tekst) ts.textContent = tekst;
    if (ts && ts.className !== klasse) ts.className = klasse;
    const knop = document.getElementById("taakStatus");
    if (knop && !knop.hidden) knop.hidden = true;
    if (st) return;   // oudere ronde: geen invoer
    // Nog geen chat voor deze taak: index.html kent haar niet in "Aan" en zou de sleutel als naam tonen
    // (een option toevoegen aan een select zonder keuze kiest de eerste, de God-chat: daarom eerst de keuze lezen)
    const doel = document.getElementById("doel");
    if (doel && ![...doel.options].some((o) => o.value === g)) {
      const leeg = !doel.value;
      const o = document.createElement("option");
      o.value = g; o.textContent = "  › " + t.naam;
      doel.appendChild(o);
      if (leeg) doel.value = g;
    }
    const tx = document.getElementById("tekst");
    const wilP = `Schrijf aan ${t.naam}…`;
    if (tx && (!doel || doel.value === g) && tx.placeholder !== wilP) tx.placeholder = wilP;
  }
  function pagina(ophalen = true) {
    const g = gekozenNu();
    const t = taakVan(g);
    const doos = document.getElementById("beurten");
    let el = document.getElementById("klokPagina");
    const archief = !!(t && g.includes("@"));
    if (document.body.classList.contains("klok-archief") !== archief) document.body.classList.toggle("klok-archief", archief);
    if (!t || !doos) { if (el) el.remove(); document.body.classList.remove("klok-zonder-chat"); return; }
    if (ophalen) haalDetail(t.id, detailId !== t.id);
    if (!el) {
      el = document.createElement("div");
      el.id = "klokPagina";
      el.className = "klok-pagina";
      doos.parentNode.insertBefore(el, doos);
      el.addEventListener("click", paginaKlik);
      el.addEventListener("keydown", (e) => {
        if ((e.key === "Enter" || e.key === " ") && e.target.closest && e.target.closest("li[data-kp-open]") === e.target) { e.preventDefault(); paginaKlik(e); }
      });
      el.addEventListener("toggle", (e) => {
        const k = e.target && e.target.dataset ? e.target.dataset.kp : null;
        if (k === "opdracht") openOpdracht = e.target.open;
        if (k === "rondes") openRondes = e.target.open;
      }, true);
    }
    const html = paginaHtml(t, g, detailId === t.id ? detail : null);
    if (el._html !== html) { el.innerHTML = html; el._html = html; }
    kopZetten(t, g);
  }
  function paginaKlik(e) {
    const knop = e.target.closest && e.target.closest("[data-kp-open]");
    const t = taakVan(gekozenNu());
    if (!knop || !t || typeof window.kies !== "function") return;
    const v = knop.dataset.kpOpen;
    window.kies(v === "huidig" ? t.chat : `${t.chat}@${v}`, true);
  }

  // --- ophalen -------------------------------------------------------------------------------------------------
  async function haal(nuMeteen) {
    if ((document.hidden && !nuMeteen) || bezig) return;
    bezig = true;
    try {
      const r = await fetch("/api/klok", { cache: "no-store" });
      if (!r.ok) return;   // bv. 404 vóór de herstart: niets tonen, niets breken
      const d = await r.json();
      if (d && typeof d === "object") {
        const oud = JSON.stringify(data);
        data = d;
        if (d && d.tz && d.tz !== fmtTz) { fmtTz = d.tz; fmt = maakFmt(fmtTz); }
        if (JSON.stringify(d) !== oud) teken();
        pagina();
      }
    } catch (e) { /* geen verbinding: laten staan */ }
    finally { bezig = false; }
  }

  function begin() {
    stijl();
    const ul = document.getElementById("boomLijst");
    if (!ul) return;
    obs = new MutationObserver((lijst) => {   // index.html tekende de lijst opnieuw: de klokrijen zijn weg, opnieuw inhaken
      if (lijst.some((m) => [...m.addedNodes].some((n) => n.nodeType === 1 && !n.classList.contains("klok-rij")))) {
        teken();
        const g = gekozenNu(), t = taakVan(g);   // tekenBoom bouwde ook "Aan" opnieuw
        if (t) kopZetten(t, g);
      }
    });
    obs.observe(ul, { childList: true });
    ul.addEventListener("click", klik, true);   // vóór de onclick van index.html, die op elke <li> staat
    ul.addEventListener("keydown", (e) => {
      const rij = e.target.closest && e.target.closest("li.klok-rij.klok-kan");
      if (rij && e.target === rij && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); klik({ target: rij, stopPropagation() {} }); }
    });
    const beurten = document.getElementById("beurten");   // index.html tekende het gesprek (een andere chat of iets nieuws)
    if (beurten) new MutationObserver(() => pagina()).observe(beurten, { childList: true });
    const kop = document.querySelector("#gesprek .bovenaan");   // tekenKop zette de naam terug
    if (kop) new MutationObserver(() => { const g = gekozenNu(), t = taakVan(g); if (t) kopZetten(t, g); })
      .observe(kop, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class"] });
    haal(true);
    setInterval(haal, ELKE);
    setInterval(() => { if (data) teken(); }, 60000);   // "vandaag/morgen" schuift mee met de klok
    document.addEventListener("visibilitychange", () => { if (!document.hidden) haal(); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", begin); else begin();
})();
