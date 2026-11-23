// Klanten in één blik: het blok "Klanten" op het dashboard en de kaart "Werktijd per klant".
// Eén IIFE zonder globale namen; leest /api/klanten (klanten_lader.py) elke 30 s. Wat de chats meldden is het nieuws,
// de rest zijn feiten van de app. Een mislukte aanvraag laat staan wat er stond; staat er niets, dan blijft het blok leeg.
(() => {
  "use strict";
  const DAGEN = ["zo", "ma", "di", "wo", "do", "vr", "za"];
  const MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"];
  const BRONNAAM = { stand: "stand", vragen: "vragen", verloop: "verloop", routering: "routering", blik: "meldingen",
    whatsapp: "whatsapp", log: "log" };
  const ELKE = 30000;

  let getekend = null;   // vergelijkingssleutel van wat er nu staat
  let open = null;       // id van de uitgeklapte klant
  let bezig = false;

  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const lijst = (x) => (Array.isArray(x) ? x : []);
  const getal = (x) => typeof x === "number" && isFinite(x);

  // --- tijd en getallen ----------------------------------------------------------------------------------------
  const tweecijfers = (n) => String(n).padStart(2, "0");
  const dagSleutel = (d) => `${d.getFullYear()}-${tweecijfers(d.getMonth() + 1)}-${tweecijfers(d.getDate())}`;
  function datum(s) {   // "2026-11-21" (of een volledige ISO-tijd) → Date om middernacht, lokale tijd
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }
  function kortDag(d) { return `${DAGEN[d.getDay()]} ${d.getDate()} ${MAANDEN[d.getMonth()]}`; }
  function verschilDagen(d) {   // vandaag = 0, gisteren = -1
    const nu = new Date(); nu.setHours(0, 0, 0, 0);
    return Math.round((d - nu) / 86400000);
  }
  function dagTekst(s, metGisteren = true) {
    const d = datum(s);
    if (!d) return "";
    const v = verschilDagen(d);
    if (v === 0) return "vandaag";
    if (v === -1 && metGisteren) return "gisteren";
    return kortDag(d);
  }
  function ouderdom(ts) { const t = Date.parse(ts || ""); return isNaN(t) ? Infinity : Date.now() - t; }
  function moment(ts) {   // laatste contact: vandaag het uur, deze week "vr 19:49", anders "do 12 nov"
    const t = new Date(ts || "");
    if (isNaN(t)) return "";
    const uur = `${tweecijfers(t.getHours())}:${tweecijfers(t.getMinutes())}`;
    const d = new Date(t); d.setHours(0, 0, 0, 0);
    const v = verschilDagen(d);
    if (v === 0) return uur;
    if (v >= -6) return `${DAGEN[t.getDay()]} ${uur}`;
    return kortDag(t);
  }
  function duur(s) {   // "52 min", "1 u 12 min"; onder een minuut null
    if (!getal(s) || s < 60) return null;
    const min = Math.round(s / 60);
    if (min < 60) return `${min} min`;
    const u = Math.floor(min / 60), r = min % 60;
    return r ? `${u} u ${r} min` : `${u} u`;
  }
  function compact(n) {   // 1.234 → 1,2k · 255.497 → 255k · 1.200.000 → 1,2M
    if (!getal(n)) return "";
    const a = Math.abs(n), komma = (x) => String(x).replace(".", ",");
    if (a < 1000) return komma(Math.round(n * 10) / 10);
    const k = n / 1000;
    if (Math.abs(k) < 10) return komma(Math.round(k * 10) / 10) + "k";
    if (Math.round(Math.abs(k)) < 1000) return Math.round(k) + "k";
    const m = n / 1e6;
    return (Math.abs(m) < 10 ? komma(Math.round(m * 10) / 10) : Math.round(m)) + "M";
  }
  const voluit = (n) => (getal(n) ? n.toLocaleString("nl-BE") : "");
  function metEenheid(tekst, eenheid) {
    if (!eenheid) return tekst;
    return /[$€£]$/.test(eenheid) ? eenheid + tekst : `${tekst} ${eenheid}`;
  }

  // --- stijl ---------------------------------------------------------------------------------------------------
  const STIJL = `
#klanten:empty { display: none; }
.kl-lijst { background: var(--vlak); border: 1px solid var(--lijn); border-radius: 12px; overflow: hidden; }
.kl-rij + .kl-rij { border-top: 1px solid var(--lijn); }
.kl-kop { display: grid; grid-template-columns: 8px minmax(0, 1fr); column-gap: 10px; padding: 12px 14px; cursor: pointer;
  outline: none; -webkit-tap-highlight-color: transparent; }
.kl-kop:focus-visible { box-shadow: inset 0 0 0 2px var(--accent); border-radius: 10px; }
.kl-rij.kl-open { background: var(--bg); }
.kl-r1 { grid-column: 1 / -1; display: grid; grid-template-columns: 8px minmax(0, 1fr); column-gap: 10px; align-items: start; }
.kl-r1 > .kl-stip { margin-top: 7px; }
.kl-stip { width: 8px; height: 8px; border-radius: 50%; }
.kl-stip.kl-werkt { background: var(--werkt); animation: kl-puls 1.4s ease-in-out infinite; }
.kl-stip.kl-wacht { background: var(--wacht); }
@keyframes kl-puls { 50% { opacity: .35; } }
@keyframes kl-komt { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: none; } }
.kl-uit.kl-komt { animation: kl-komt var(--gewoon, 260ms) var(--uit, cubic-bezier(.2,.8,.2,1)) both; }
@media (prefers-reduced-motion: reduce) { .kl-uit.kl-komt { animation: none; } }
@media (prefers-reduced-motion: reduce) { .kl-stip.kl-werkt { animation: none; } }
.kl-kopregel { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 12px; row-gap: 4px; min-width: 0; }
.kl-naam { font-size: 15px; font-weight: 600; min-width: 0; overflow-wrap: anywhere; margin-right: auto; }
.kl-lijf { grid-column: 2; min-width: 0; }
.kl-lijf > * { margin-top: 3px; }
.kl-lijf > :first-child { margin-top: 4px; }
.kl-nieuws { font-size: 14px; line-height: 1.4; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 3;
  -webkit-box-orient: vertical; overflow: hidden; }
.kl-nieuws.kl-vers { font-weight: 500; }
.kl-nieuws.kl-oud { color: var(--zacht); }
.kl-nieuws .kl-ndag { font-size: 12.5px; font-weight: 400; color: var(--flauw); white-space: nowrap; }
.kl-klein { font-size: 12.5px; line-height: 1.4; color: var(--zacht); overflow-wrap: anywhere; }
.kl-klein.kl-flauw, .kl-flauw { color: var(--flauw); }
.kl-klein.kl-dagregel { margin-top: 1px; }
.kl-klein.kl-luistert { color: var(--wacht); }
.kl-chips { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px !important; }
.kl-chips .kl-lbl { font-size: 12px; color: var(--flauw); }
.kl-chip { font: inherit; font-size: 12.5px; line-height: 1.3; color: var(--inkt); background: transparent; border: 1px solid var(--lijn);
  border-radius: 999px; padding: 3px 10px; max-width: 100%; min-width: 0; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.kl-chip:hover { border-color: var(--zacht); }
.kl-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.kl-chip > span { overflow: hidden; text-overflow: ellipsis; }
.kl-chip .kl-stip { width: 6px; height: 6px; flex: none; }
.kl-chip.kl-vraag { background: var(--wacht-zacht); border-color: transparent; color: var(--wacht); }
.kl-meter { display: inline-flex; flex-direction: column; align-items: flex-start; gap: 4px; font-size: 12.5px; color: var(--zacht);
  font-variant-numeric: tabular-nums; max-width: 100%; }
.kl-meter .kl-mrij { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
.kl-meter b { color: var(--inkt); font-weight: 600; }
.kl-meter .kl-mdag { color: var(--flauw); }
.kl-meter .kl-mrij > .kl-mdag { margin-left: -3px; }
.kl-vakjes { display: inline-flex; gap: 3px; }
.kl-vakjes i { width: 8px; height: 8px; border-radius: 2px; background: var(--lijn); }
.kl-vakjes i.kl-vol { background: var(--accent); }
.kl-balk { display: inline-block; width: 64px; height: 6px; border-radius: 3px; background: var(--accent-zacht); overflow: hidden; }
.kl-balk i { display: block; height: 100%; min-width: 3px; background: var(--accent); border-radius: 3px; }
.kl-uit { padding: 0 14px 14px 32px; font-size: 13.5px; }
.kl-uit[hidden] { display: none; }
.kl-blok { margin-top: 12px; min-width: 0; }
.kl-blok:first-child { margin-top: 2px; }
.kl-blok h5 { margin: 0 0 3px; font-size: 12px; font-weight: 500; color: var(--flauw); }
.kl-blok p { margin: 0 0 2px; overflow-wrap: anywhere; }
.kl-blok .kl-dag { color: var(--flauw); font-size: 12.5px; white-space: nowrap; }
.kl-lijn { display: flex; align-items: center; gap: 8px; margin: 4px 0 2px; font-size: 12px; color: var(--zacht); font-variant-numeric: tabular-nums; }
.kl-lijn svg { width: 120px; height: 28px; flex: none; overflow: visible; }
.kl-lijn polyline { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.kl-open-chat { margin-top: 14px; font-size: 13px; padding: 5px 12px; }
.kl-voet { font-size: 12.5px; color: var(--zacht); padding: 10px 4px 0; overflow-wrap: anywhere; display: flex; flex-wrap: wrap;
  align-items: baseline; gap: 0 14px; }
.kl-voet .kl-lbl { color: var(--flauw); }
.kl-voet .kl-link { font: inherit; color: inherit; background: none; border: none; padding: 0; cursor: pointer; text-decoration: underline;
  text-decoration-color: var(--lijn); text-underline-offset: 3px; }
.kl-voet .kl-link:hover { color: var(--accent); }
.kl-voet .kl-stip { display: inline-block; width: 6px; height: 6px; margin-right: 5px; vertical-align: 1px; }
.kl-niet { font-size: 12px; color: var(--flauw); padding: 6px 4px 0; }
.perchat .n[data-kl-chat]:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; border-radius: 3px; }
@media (pointer: coarse), (max-width: 599px) {   /* tikdoelen voor een vinger */
  .kl-chip { min-height: 36px; padding: 6px 12px; }
  .kl-voet .kl-link { display: inline-block; padding: 10px 2px; }
  .kl-open-chat { min-height: 40px; }
  .perchat .n[data-kl-chat] { padding: 8px 0; }
}
/* één regel per klant: de rest na een tik */
.kl-kop { padding: 11px 14px; }
.kl-r1 { align-items: center; }
.kl-r1 > .kl-stip { margin-top: 0; }
.kl-kopregel { flex-wrap: nowrap; align-items: center; }
.kl-naam { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kl-rechts { flex: none; font-size: 12.5px; color: var(--flauw); font-variant-numeric: tabular-nums; white-space: nowrap; }
.kl-rechts.kl-vraagt { color: var(--wacht); background: var(--wacht-zacht); border-radius: 999px; padding: 2px 9px; font-weight: 600; }
.kl-pijl { flex: none; color: var(--flauw); font-size: 12px; transition: transform var(--snel, 160ms) var(--uit, ease-out); }
.kl-open .kl-pijl { transform: rotate(90deg); }
.kl-lijf > :first-child { margin-top: 2px; }
.kl-lijf .kl-klein { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kl-klein.kl-bijjou { color: var(--wacht); }
.kl-uit .kl-blok p.kl-eerst { font-weight: 500; }
@media (prefers-reduced-motion: reduce) { .kl-pijl { transition: none; } }
@media (min-width: 600px) {
  .kl-kopregel { flex-wrap: nowrap; }
  .kl-naam { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .kl-meter { flex: none; align-items: flex-end; }
}
@media (min-width: 960px) {
  .dash .kl-lijst { background: var(--bg); border-color: transparent; }
  .dash .kl-rij + .kl-rij { border-top-color: var(--vlak); }
  .dash .kl-rij.kl-open { background: color-mix(in srgb, var(--bg) 55%, var(--vlak)); }
  .dash .kl-chip { border-color: var(--lijn); }
}
`;
  function stijl() {
    if (el("kl-stijl")) return;
    const s = document.createElement("style");
    s.id = "kl-stijl";
    s.textContent = STIJL;
    document.head.appendChild(s);
  }

  // --- de meter ------------------------------------------------------------------------------------------------
  function meterHtml(m) {
    if (!m || typeof m !== "object") return "";
    const dt = dagTekst(m.dag);
    const dag = dt && dt !== "vandaag" ? `<span class="kl-mdag">· ${esc(dt)}</span>` : "";
    let rij = "", onder = "";
    if (m.soort === "deel" && getal(m.waarde) && getal(m.van) && m.van > 0) {
      if (m.van <= 12) {
        const vol = Math.max(0, Math.min(m.van, Math.round(m.waarde)));
        const vakjes = Array.from({ length: Math.round(m.van) }, (_, i) => `<i class="${i < vol ? "kl-vol" : ""}"></i>`).join("");
        rij = `<span class="kl-vakjes" aria-hidden="true">${vakjes}</span><span><b>${esc(voluit(m.waarde))}</b>/${esc(voluit(m.van))}</span>`;
      } else {
        const pct = Math.max(0, Math.min(100, (m.waarde / m.van) * 100));
        rij = `<span><b>${esc(metEenheid(compact(m.waarde), m.eenheid))}</b> van ${esc(compact(m.van))}</span>`
          + `<span class="kl-balk" aria-hidden="true">${pct > 0 ? `<i style="width:${pct.toFixed(1)}%"></i>` : ""}</span>`;
      }
    } else if (m.soort === "getal" && getal(m.waarde)) {
      rij = `<span><b>${esc(metEenheid(voluit(m.waarde), m.eenheid))}</b> ${esc(m.label || "")}${getal(m.ook) && m.ook_label
        ? ` <span class="kl-mdag">· ${esc(voluit(m.ook))} ${esc(m.ook_label)}</span>` : ""}</span>`;
    } else {
      return "";
    }
    return `<span class="kl-meter" title="${esc(m.uitleg || m.label || "")}"><span class="kl-mrij">${rij}${dag}</span>${onder}</span>`;
  }

  function meterUitklap(m) {
    if (!m || typeof m !== "object") return "";
    let waarde = "", lijn = "";
    if (m.soort === "deel" && getal(m.waarde)) {
      waarde = metEenheid(voluit(m.waarde), m.eenheid) + (getal(m.van) && m.van > 0 ? ` van ${metEenheid(voluit(m.van), m.eenheid)}` : "");
    } else if (m.soort === "getal" && getal(m.waarde)) {
      waarde = `${metEenheid(voluit(m.waarde), m.eenheid)} ${m.label || ""}`;
    } else return "";
    if (getal(m.ook) && m.ook_label) waarde += ` · ${voluit(m.ook)} ${m.ook_label}`;
    if (m.bij) waarde += ` · ${m.bij}`;
    const extra = "";
    const reeks = lijst(m.reeks).filter((p) => p && getal(p.waarde));
    if (reeks.length >= 2) {
      const w = reeks.map((p) => p.waarde), lo = Math.min(...w), hi = Math.max(...w), span = hi - lo || 1;
      const pts = reeks.map((p, i) => `${((i / (reeks.length - 1)) * 118 + 1).toFixed(1)},${(26 - ((p.waarde - lo) / span) * 24).toFixed(1)}`).join(" ");
      const kort = (n) => (m.soort === "deel" && m.van > 12 ? metEenheid(compact(n), m.eenheid) : voluit(n));
      lijn = `<div class="kl-lijn" title="${esc(reeks.map((p) => `${dagTekst(p.dag)}: ${voluit(p.waarde)}`).join(" · "))}">`
        + `<span>${esc(kort(w[0]))}</span><svg viewBox="0 0 120 28" aria-hidden="true"><polyline points="${pts}"/></svg>`
        + `<span>${esc(kort(w[w.length - 1]))}</span></div>`;
    }
    return `<div class="kl-blok"><h5>${esc(m.label || "meter")}</h5><p>${esc(waarde)}</p>${extra}${lijn}`
      + `<p class="kl-klein">${dagTekst(m.dag) ? `gemeten ${esc(dagTekst(m.dag))}${m.uitleg ? " · " : ""}` : ""}${esc(m.uitleg || "")}</p></div>`;
  }

  // --- één rij -------------------------------------------------------------------------------------------------
  function volgendeTekst(v) {
    if (!v || !v.tekst) return "";
    return v.datum ? `${dagTekst(v.datum, false)} · ${v.tekst}` : v.tekst;
  }
  function laatsteWerkTekst(lw) {
    if (!lw || !lw.titel) return "";
    const dt = dagTekst(lw.dag);
    return dt ? `laatste werk · ${dt}: ${lw.titel}` : `laatste werk: ${lw.titel}`;
  }

  function wachtOpJou(k) {   // er ligt iets bij jou: een open vraag of iets wat alleen jij kan doen
    return (getal(k.vragen) && k.vragen > 0) || lijst(k.bij_jou).some((p) => typeof p === "string" && p.trim());
  }
  function laatstGemeld(nieuws) {   // het uur als het vandaag was, anders de dag; ouder dan een week: niets
    const n = nieuws[0];
    if (!n) return "";
    const t = Date.parse(n.ts || "");
    if (!isNaN(t)) {
      const d = new Date(t), v = verschilDagen(datum(n.ts) || d);
      if (v === 0) return `${tweecijfers(d.getHours())}:${tweecijfers(d.getMinutes())}`;
      if (v >= -6) return v === -1 ? "gisteren" : DAGEN[d.getDay()];
      return "";
    }
    return dagTekst(n.dag, true) || "";
  }

  function rijHtml(k, sinds) {
    const id = String(k.id || "");
    const nieuws = lijst(k.nieuws).filter((n) => n && n.tekst);
    const taken = lijst(k.taken).filter((t) => t && t.id);
    const bijJou = lijst(k.bij_jou).filter((p) => typeof p === "string" && p.trim());
    const vragen = getal(k.vragen) && k.vragen > 0 ? k.vragen : 0;
    const isOpen = open === id;
    const werkt = k.werkt || taken.some((t) => t.werkt);
    const stip = vragen || bijJou.length ? "kl-wacht" : werkt ? "kl-werkt" : "";
    const stipTitel = vragen ? "er wacht een vraag op jou" : bijJou.length ? "er ligt iets bij jou" : werkt ? "werkt nu" : "";

    // rechts op de regel: wat op jou wacht, anders wanneer de chat het laatst iets meldde
    const gemeld = laatstGemeld(nieuws);
    const rechts = vragen
      ? `<button type="button" class="kl-rechts kl-vraagt" data-kl-vraag title="Naar de vragen bovenaan">${vragen} ${vragen === 1 ? "vraag" : "vragen"}</button>`
      : gemeld ? `<span class="kl-rechts" title="laatst iets gemeld">${esc(gemeld)}</span>` : "";

    // een tweede regel alleen voor wat bij jou ligt of een chat die niet luistert
    let lijf = "";
    if (bijJou.length) lijf += `<div class="kl-klein kl-bijjou">bij jou: ${esc(bijJou[0])}${bijJou.length > 1 ? ` (+${bijJou.length - 1})` : ""}</div>`;
    if (k.luistert_niet) lijf += `<div class="kl-klein kl-luistert">luistert niet: ${esc(k.luistert_niet)}</div>`;

    // de uitklap: al de rest
    let uit = "";
    if (bijJou.length) uit += `<div class="kl-blok"><h5>bij jou</h5>${bijJou.map((p) => `<p>${esc(p)}</p>`).join("")}</div>`;
    if (nieuws.length) {
      uit += `<div class="kl-blok"><h5>nieuws</h5>${nieuws.slice(0, 5).map((n, i) =>
        `<p${i === 0 ? ` class="kl-eerst"` : ""}>${esc(n.tekst)}${dagTekst(n.dag || n.ts) ? ` <span class="kl-dag">· ${esc(dagTekst(n.dag || n.ts))}</span>` : ""}</p>`).join("")}</div>`;
    } else {
      const lw = laatsteWerkTekst(k.laatste_werk);
      if (lw) uit += `<div class="kl-blok"><p class="kl-klein">${esc(lw)}</p></div>`;
    }
    const vt = volgendeTekst(k.volgende);
    if (vt) uit += `<div class="kl-blok"><h5>wat volgt</h5><p>${esc(vt)}</p></div>`;
    if (taken.length) {
      uit += `<div class="kl-blok"><h5>in de maak</h5><div class="kl-chips">`
        + taken.map((t) => `<button type="button" class="kl-chip" data-kl-chat="${esc(t.id)}" title="Open ${esc(t.naam || t.id)}">`
          + `${t.werkt ? `<span class="kl-stip kl-werkt"></span>` : ""}<span>${esc(t.naam || t.id)}</span></button>`).join("")
        + `</div></div>`;
    }
    const contact = lijst(k.contact).filter((c) => c && c.naam);
    if (contact.length) {
      uit += `<div class="kl-blok"><h5>laatste contact</h5>${contact.map((c) => {
        const wie = c.richting === "uit" ? `jij → ${esc(c.naam)}` : `${esc(c.naam)} → jou`;
        const t = moment(c.ts);
        return `<p>${wie}${t ? ` <span class="kl-dag">· ${esc(t)}</span>` : ""}${c.bezet ? ` <span class="kl-dag">· loopt via een eigen bot</span>` : ""}</p>`;
      }).join("")}</div>`;
    }
    uit += meterUitklap(k.meter);
    const sys = k.systeem && typeof k.systeem === "object" ? k.systeem : {};
    const werkte = duur(sys.werktijd_s);
    const klaar = lijst(sys.klaar).filter((x) => x && x.naam).map((x) => x.naam);
    if (werkte || klaar.length) {
      uit += `<div class="kl-blok"><h5>het systeem, ${dagTekst(sinds) ? `sinds ${esc(dagTekst(sinds))}` : "de laatste 7 dagen"}</h5>`
        + (werkte ? `<p>werkte ${esc(werkte)}</p>` : "")
        + (klaar.length ? `<p>klaar: ${esc(klaar.join(", "))}</p>` : "") + `</div>`;
    }
    uit += `<button type="button" class="kl-open-chat" data-kl-chat="${esc(id)}">Open chat</button>`;

    return `<div class="kl-rij${isOpen ? " kl-open" : ""}" data-kl-id="${esc(id)}">
      <div class="kl-kop" role="button" tabindex="0" aria-expanded="${isOpen}" aria-controls="kl-uit-${esc(id)}">
        <div class="kl-r1"><span class="kl-stip ${stip}"${stipTitel ? ` title="${stipTitel}"` : ""}></span>
          <span class="kl-kopregel"><span class="kl-naam">${esc(k.naam || id)}</span>${rechts}<span class="kl-pijl" aria-hidden="true">›</span></span></div>
        ${lijf ? `<div class="kl-lijf">${lijf}</div>` : ""}
      </div>
      <div class="kl-uit" id="kl-uit-${esc(id)}"${isOpen ? "" : " hidden"}>${uit}</div>
    </div>`;
  }

  // --- tekenen -------------------------------------------------------------------------------------------------
  function sleutel(d) {   // zonder `nu` en de meettijden: die veranderen bij elke opbouw
    const k = JSON.parse(JSON.stringify(d));
    delete k.nu;
    if (k.bronnen && typeof k.bronnen === "object") for (const b of Object.values(k.bronnen)) if (b && typeof b === "object") delete b.gemeten;
    return dagSleutel(new Date()) + JSON.stringify(k);
  }

  function tekenKlanten(d) {
    const doos = el("klanten");
    if (!doos) return;
    const klanten = (d && d.ok && Array.isArray(d.klanten) ? d.klanten : []).filter((k) => k && typeof k === "object" && k.id);
    if (!klanten.length) { doos.innerHTML = ""; return; }
    if (open && !klanten.some((k) => k && String(k.id) === open)) open = null;
    const focus = document.activeElement && doos.contains(document.activeElement)
      ? (document.activeElement.closest(".kl-rij") || {}).dataset : null;
    // wat op jou wacht eerst; verder de volgorde van routing.json
    const rijen = klanten.map((k, i) => [k, i]).sort((a, b) => (wachtOpJou(b[0]) - wachtOpJou(a[0])) || (a[1] - b[1])).map(([k]) => k).map((k) => {
      try { return rijHtml(k || {}, d.sinds); } catch (e) { return ""; }   // één kapotte rij neemt de rest niet mee
    }).join("");
    let voet = "";
    const appTaken = lijst(d.app && d.app.taken).filter((t) => t && t.id);
    if (appTaken.length) {
      voet += `<div class="kl-voet"><span class="kl-lbl">De app:</span>${appTaken.map((t) =>
        `<button type="button" class="kl-link" data-kl-chat="${esc(t.id)}">${t.werkt ? `<span class="kl-stip kl-werkt"></span>` : ""}${esc(t.naam || t.id)}</button>`).join("")}</div>`;
    }
    const niet = Object.entries(d.bronnen && typeof d.bronnen === "object" ? d.bronnen : {})
      .filter(([, b]) => b && b.ok === false).map(([n]) => BRONNAAM[n] || n);
    if (niet.length) voet += `<div class="kl-niet">niet gelezen: ${esc(niet.join(", "))}</div>`;
    doos.innerHTML = `<p class="kop">Klanten</p><div class="kl-lijst">${rijen}</div>${voet}`;
    if (focus && focus.klId) {
      const kop = doos.querySelector(`.kl-rij[data-kl-id="${CSS_esc(focus.klId)}"] .kl-kop`);
      if (kop) kop.focus({ preventScroll: true });
    }
  }
  const CSS_esc = (s) => (window.CSS && window.CSS.escape ? window.CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"));

  function tekenWerktijd(d) {
    const titel = el("werkTitel"), uitleg = el("werkUitleg"), doos = el("perChat");
    if (!titel || !uitleg || !doos) return;
    const rijen = lijst(d && d.werktijd).filter((r) => r && r.id);
    titel.textContent = "Werktijd per klant";
    uitleg.textContent = "Zonder hartslagen · " + (d && d.sinds ? `sinds ${dagTekst(d.sinds)}` : "laatste 7 dagen");
    const top = Math.max(1, ...rijen.map((r) => (getal(r.s) ? r.s : 0)));
    doos.innerHTML = rijen.map((r) => {
      const s = getal(r.s) ? r.s : 0;
      return `<span class="n" data-kl-chat="${esc(r.id)}" role="button" tabindex="0">${esc(r.naam || r.id)}</span>`
        + `<span class="baan">${s > 0 ? `<i style="width:${((s / top) * 100).toFixed(1)}%"></i>` : ""}</span>`
        + `<span class="g">${esc(duur(s) || "–")}</span>`;
    }).join("");
  }

  function teken(d) {
    const k = sleutel(d);
    if (k === getekend) return;
    getekend = k;
    try { tekenKlanten(d); } catch (e) { /* het blok blijft zoals het was */ }
    try { tekenWerktijd(d); } catch (e) { /* idem */ }
  }

  // --- klikken en toetsen --------------------------------------------------------------------------------------
  function openChat(id) {
    try {
      if (typeof window.kies !== "function" || !id) return;
      // een chat die de app niet (meer) kent: niets doen, anders faalt het gesprek in de console
      if (typeof stand !== "undefined" && stand && Array.isArray(stand.chats) && stand.chats.length
        && !stand.chats.some((c) => c.id === id)) {
        id = String(id).split("--")[0];   // de taak is weg: dan de chat waaronder hij hing
        if (!stand.chats.some((c) => c.id === id)) return;
      }
      window.kies(id, true);
    } catch (e) { /* stil */ }
  }
  function naarVragen() {
    try {
      const hoe = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
      window.scrollTo({ top: 0, behavior: hoe });
      const dash = el("dash");
      if (dash && dash.scrollTop) dash.scrollTo({ top: 0, behavior: hoe });
    } catch (e) { /* stil */ }
  }
  function wissel(rij) {
    const id = rij.dataset.klId;
    open = open === id ? null : id;
    el("klanten").querySelectorAll(".kl-rij").forEach((r) => {
      const aan = r.dataset.klId === open;
      r.classList.toggle("kl-open", aan);
      r.querySelector(".kl-kop").setAttribute("aria-expanded", String(aan));
      const uit = r.querySelector(".kl-uit");
      uit.hidden = !aan;
      uit.classList.toggle("kl-komt", aan);   // alleen bij een tik: een volgende tekening speelt het niet opnieuw
    });
  }
  function koppel() {
    const doos = el("klanten");
    if (doos) {
      doos.addEventListener("click", (e) => {
        const chat = e.target.closest("[data-kl-chat]");
        if (chat) { e.stopPropagation(); return openChat(chat.dataset.klChat); }
        if (e.target.closest("[data-kl-vraag]")) { e.stopPropagation(); return naarVragen(); }
        const kop = e.target.closest(".kl-kop");
        if (kop && doos.contains(kop)) wissel(kop.closest(".kl-rij"));
      });
      doos.addEventListener("keydown", (e) => {
        if (!e.target.classList || !e.target.classList.contains("kl-kop")) return;   // chips en knoppen doen het zelf
        if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") { e.preventDefault(); wissel(e.target.closest(".kl-rij")); }
      });
    }
    const pc = el("perChat");
    if (pc && el("werkTitel")) {
      pc.addEventListener("click", (e) => { const n = e.target.closest("[data-kl-chat]"); if (n) openChat(n.dataset.klChat); });
      pc.addEventListener("keydown", (e) => {
        const n = e.target.closest && e.target.closest("[data-kl-chat]");
        if (n && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openChat(n.dataset.klChat); }
      });
    }
  }

  // --- opschriften van de vraagkaartjes: index.html tekent ze, hier komen ze binnen (klanten.py kiest ze) ---------
  let kopTeken = null, kopWekkers = [];
  function opschriften(o) {
    if (!o || typeof o !== "object" || Array.isArray(o)) return;   // een oudere klanten.py stuurt ze niet mee
    const t = JSON.stringify(o);
    if (t === kopTeken) return;
    kopTeken = t;
    window.dispatchEvent(new CustomEvent("gc-opschriften", { detail: o }));
  }
  // index.html zag een nieuwe vraag: haar opschrift volgt een paar seconden later, dus kijk een paar keer sneller
  window.addEventListener("gc-opschrift-nodig", () => {
    kopWekkers.forEach(clearTimeout);
    kopWekkers = [3000, 6500, 12000, 25000].map((ms) => setTimeout(haal, ms));
  });

  // --- ophalen -------------------------------------------------------------------------------------------------
  async function haal() {
    if (document.hidden || bezig) return;
    bezig = true;
    const rem = typeof AbortController === "function" ? new AbortController() : null;
    const wekker = rem ? setTimeout(() => rem.abort(), 15000) : 0;   // na 15 s opgeven: de volgende ronde probeert opnieuw
    try {
      const r = await fetch("/api/klanten", { cache: "no-store", signal: rem ? rem.signal : undefined });
      if (!r.ok) return;   // bv. 404 vóór de herstart: laten staan wat er stond
      const d = await r.json();
      if (d && typeof d === "object") { opschriften(d.opschriften); teken(d); }
    } catch (e) { /* geen verbinding of te traag: laten staan */ }
    finally { clearTimeout(wekker); bezig = false; }
  }

  function begin() {
    stijl();
    koppel();
    haal();
    setInterval(haal, ELKE);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) haal(); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", begin); else begin();
})();
