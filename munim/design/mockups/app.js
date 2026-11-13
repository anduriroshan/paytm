// Munim UI mockups v2 (chat-first). Draws the shell: sidebar, chat header, mockup tour bar, icons,
// and the shared step list / safety-check squares. Pages set <body data-chats data-nav data-head>.
(function () {
  const P = {
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    spark: '<path d="M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9z"/><path d="M19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
    code: '<path d="M8.5 7.5L4 12l4.5 4.5M15.5 7.5L20 12l-4.5 4.5"/>',
    shield: '<path d="M12 3l7.5 3v5.5c0 4.7-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.8-7.5-9.5V6z"/>',
    search: '<circle cx="10.5" cy="10.5" r="6"/><path d="M20 20l-5-5"/>',
    speaker: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M16 9a4.2 4.2 0 010 6M18.5 6.5a7.8 7.8 0 010 11"/>',
    users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><path d="M16 4.6a3.2 3.2 0 010 6.3M21 20c0-2.6-1.6-4.8-4-5.6"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0013 0M12 17.5V21"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    x: '<path d="M7 7l10 10M17 7L7 17"/>',
    doc: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h4"/>',
    prev: '<path d="M15 6l-6 6 6 6"/>',
    next: '<path d="M9 6l6 6-6 6"/>',
    down: '<path d="M6 9l6 6 6-6"/>',
    alert: '<path d="M12 3.5l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
    coins: '<path d="M7 5h10M7 9h10M8 5c4.5 0 6.5 1.6 6.5 4S12.5 13 8 13l7 6.5"/>',
    human: '<circle cx="12" cy="7.5" r="3.5"/><path d="M5 20.5c0-3.9 3.1-7 7-7s7 3.1 7 7"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    store: '<path d="M4 9.5l1.5-5h13L20 9.5M4 9.5h16M4 9.5c0 1.7 1.3 3 3 3s3-1.3 3-3c0 1.7 1.3 3 3 3s3-1.3 3-3c0 1.7 1.3 3 3 3M5.5 12.5V20h13v-7.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    inbox: '<path d="M4 13l2.5-8h11L20 13v6H4z"/><path d="M4 13h5l1 2.5h4l1-2.5h5"/>',
    book: '<path d="M5 4.5h9a3 3 0 013 3v12H8a3 3 0 01-3-3z"/><path d="M5 16.5a3 3 0 013-3h9"/>',
    chat: '<path d="M4 5.5h16v10H9l-5 4z"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
    refresh: '<path d="M19.5 12a7.5 7.5 0 11-2.2-5.3M19.5 4.5v4h-4"/>',
    skip: '<path d="M5 12h14" stroke-dasharray="2 3"/>',
    ledger: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9h16M9 9v11"/>',
    device: '<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 17.5h2"/>',
    loop: '<path d="M17 3l3 3-3 3"/><path d="M4 11V9a3 3 0 013-3h13M7 21l-3-3 3-3"/><path d="M20 13v2a3 3 0 01-3 3H4"/>',
    play: '<path d="M8 5.5v13l10-6.5z"/>',
    send: '<path d="M4.5 12L20 4.5 14.5 20l-3-6.5z"/><path d="M11.5 13.5L20 4.5"/>',
    globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.5 5.5 3.5 8.5s-1.1 5.9-3.5 8.5c-2.4-2.6-3.5-5.5-3.5-8.5s1.1-5.9 3.5-8.5z"/>',
    sound: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M16 9a4.2 4.2 0 010 6"/>',
  };
  const icon = (n) => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${P[n] || ""}</svg>`;
  const B = document.body.dataset;
  const here = location.pathname.split("/").pop();

  // Mockup walk-through order.
  const TOUR = [
    ["chat.html", "Merchant types the problem"],
    ["chat-working.html", "Munim does the work, live on the right"],
    ["chat-done.html", "The answer comes back, and they can keep chatting"],
    ["how-steps.html", "How Munim decided: the steps"],
    ["how-proof.html", "How Munim decided: the proof"],
    ["how-decision.html", "How Munim decided: the safety check"],
    ["needs-you.html", "What needs a human"],
  ];

  // ── sidebar ──
  const chats = {
    none: `<div class="emptynav">No chats yet</div>`,
    live: `<a class="casei on" href="chat-working.html"><i class="dot run"></i><div><b>Sharma Kirana Store</b><span>Working…</span></div></a>`,
    done: `<a class="casei ${B.nav === "chat" ? "on" : ""}" href="chat-done.html"><i class="dot ok"></i><div><b>Sharma Kirana Store</b><span>₹998 refunded</span></div></a>
           <a class="casei" href="needs-you.html"><i class="dot hum"></i><div><b>Lakshmi Tiffins</b><span>Waiting for approval</span></div></a>`,
  }[B.chats || "none"];
  const needs = B.chats === "done" ? `<span class="badge">2</span>` : `<span class="badge zero">0</span>`;
  const side = `<aside class="side">
    <div class="logo"><span class="m">M</span>Munim</div>
    <a class="newchat" href="chat.html">${icon("plus")}New chat</a>
    <div class="navsec"><h6>Chats</h6>${chats}</div>
    <div class="navsec"><a class="navi ${B.nav === "needs" ? "on" : ""}" href="needs-you.html">${icon("human")}Needs you${needs}</a></div>
    <div class="sidefoot"><span class="live"><i></i>Live · Gemini + Sarvam</span></div>
  </aside>`;

  // ── chat header ──
  const status = {
    start: "",
    run: `<span class="pill run">Working · 6.4 s</span>`,
    done: `<span class="pill ok">${icon("check")}Resolved · 9.8 s</span>`,
  }[B.head];
  const head = B.head ? `<header class="chathead">
    <span class="av">SK</span>
    <div><b>Sharma Kirana Store${B.head === "start" ? " " + icon("down") : ""}</b><small>Ramesh Sharma · Lucknow · speaks Hindi</small></div>
    <span class="spacer"></span>
    ${status}
  </header>` : "";

  // ── tour bar ──
  const i = TOUR.findIndex((t) => t[0] === here);
  const prev = TOUR[i - 1], next = TOUR[i + 1];
  const tour = `<div class="tour"><span class="tag">MOCKUP</span>
    <span><b>${i + 1}/${TOUR.length}</b> · ${TOUR[i] ? TOUR[i][1] : ""}</span><span class="spacer"></span>
    <a href="index.html">All screens</a>
    <a class="${prev ? "" : "dis"}" href="${prev ? prev[0] : "#"}">${icon("prev")}</a>
    <a class="${next ? "" : "dis"}" href="${next ? next[0] : "#"}">${icon("next")}</a></div>`;

  // ── assemble ──
  const main = document.querySelector("main");
  const shell = document.createElement("div");
  shell.className = "shell";
  shell.innerHTML = side + `<div class="page">${head}</div>`;
  shell.querySelector(".page").appendChild(main);
  document.body.prepend(shell);
  document.body.insertAdjacentHTML("beforeend", tour);

  // ── the 12 steps, grouped into 5 phases ──
  // [phase, kind, icon, name, result, time]
  const STEPS = [
    ["Understand", "ai", "spark", "Read the complaint", "Hindi · rent cut · 4 claims", "3.9 s"],
    ["", "ai", "spark", "First hunch", "Rent ran after return · 95%", "0.4 s"],
    ["Investigate", "code", "search", "Followed the money", "3 disputed ₹499 cuts", "4 ms"],
    ["", "code", "search", "Checked the claims", "2 true · 1 partly · 1 open", "3 ms"],
    ["", "code", "search", "Tested causes", "1 found · 3 ruled out", "0.8 s"],
    ["", "code", "code", "Wrote own SQL", "Skipped · not needed", ""],
    ["Decide", "code", "flag", "Verdict", "₹998 owed", "10 ms"],
    ["", "ai", "spark", "AI proposed a fix", "Refund ₹1,497 + stop rental", "0.9 s"],
    ["", "code", "shield", "Safety rules", "Corrected to ₹998", "13 ms"],
    ["Act", "code", "coins", "Carried it out", "₹998 refunded · rental stopped", "8 ms"],
    ["Reply", "ai", "spark", "Replied", "Hindi · 60 words", "1.1 s"],
    ["", "code", "users", "Checked others", "36 more merchants hit", "55 ms"],
  ];
  document.querySelectorAll("[data-steps]").forEach((el) => {
    const now = Number(el.dataset.steps) || 99; // step in progress; 99 = all done
    el.innerHTML = STEPS.map(([ph, kind, ic, name, res, t], k) => {
      const n = k + 1;
      let cls = "", r = res, tm = t;
      if (n === now) { cls = "now"; r = "Gemini · can't move money"; tm = `<span class="typing"><i></i><i></i><i></i></span>`; }
      else if (n > now) { cls = "wait"; r = ""; tm = ""; }
      else if (n === 6) cls = "skip";
      else if (n === 9) cls = "fix";
      return (ph ? `<div class="sec">${ph}</div>` : "") +
        `<div class="st ${cls}"><span class="n">${n}</span><span class="ic ${kind}">${icon(ic)}</span>
         <b>${name}</b><span class="r">${r}</span><span class="t">${tm}</span></div>`;
    }).join("") +
      `<div class="legend"><span><span class="ic ai">${icon("spark")}</span>AI · can't move money</span><span><span class="ic code">${icon("code")}</span>Plain code</span></div>`;
  });

  // ── the 11 safety checks as squares (names on hover) ──
  const CHECKS = ["Amount recomputed from the ledger", "Known action type", "Allowed fix for this verdict", "Hard-deny rules",
    "Belongs to this merchant", "Tied to the verdict's evidence", "Not already done", "Preconditions hold",
    "Within the auto-approval limit", "Prompt-injection guard", "Verdict confidence"];
  document.querySelectorAll("[data-checks]").forEach((el) => {
    el.innerHTML = CHECKS.map((c, k) => `<span class="chk ${k === 0 ? "fix" : ""}" title="${c}${k === 0 ? ": corrected" : ": passed"}">${k === 0 ? "!" : icon("check")}</span>`).join("");
  });

  // ── chat bubbles ──
  // The team reads English. Regional language appears only where the merchant used it:
  // their own words (with Munim's English reading under them), and the reply Munim sent back (on demand).
  const said = (orig, en, tm) => `<div class="row me"><div class="bub">
      <div class="orig ind"><span class="lang">HINDI</span>${orig}</div>
      <div class="tr">${icon("globe")}<span>${en}</span></div>
      <div class="tm">${tm}</div></div></div>`;
  const sent = (en, native, extra, tm) => `<div class="row mu"><span class="mav">M</span><div class="bub">
      ${en}${extra}
      <div class="tm">${tm} · sent in Hindi <span class="btn" data-native>View Hindi</span><span class="btn">${icon("sound")}Listen</span></div>
      <div class="native ind">${native}</div></div></div>`;

  const DONE_CHAT = `<div class="msgs">
    ${said("मैंने सितंबर में साउंडबॉक्स वापस कर दिया था, फिर भी हर महीने ₹499 कट रहा है। तीन महीने से पैसा कट रहा है, मेरा पैसा वापस करो।",
      "I returned the Soundbox in September, but ₹499 is still cut every month. It's been three months. Give my money back.", "10:42 · voice note")}
    ${sent("Hello Ramesh ji, your Soundbox was picked up on 12 Sep. ₹499 was wrongly cut on 1 Oct and 1 Nov, so we have refunded ₹998 to your account. Your rental is cancelled, so there will be no more charges.",
      "नमस्ते रमेश जी, आपका साउंडबॉक्स 12 Sep को पिकअप हो गया था। हमारे रिकॉर्ड के अनुसार, 01 Oct और 01 Nov को आपके ₹998 कट गए थे। हमने ये ₹998 आपके अकाउंट में रिफंड कर दिए हैं। इसके अलावा, आपका साउंडबॉक्स रेंटल सब्सक्रिप्शन भी कैंसिल कर दिया गया है, जिससे आगे कोई चार्ज नहीं लगेगा।",
      `<div class="result">
        <div class="ln"><span class="chk">${icon("check")}</span>₹998 refunded</div>
        <div class="ln"><span class="chk">${icon("check")}</span>Rental stopped</div>
        <div class="fixline">${icon("shield")}<span>Safety rules fixed the AI's amount: <s>₹1,497</s> → <b>₹998</b></span></div>
        <a class="go" href="how-steps.html">How Munim decided ${icon("arrow")}</a>
      </div>`, "10:42 · took 9.8 s")}
    ${said("1 सितंबर वाला ₹499 क्यों नहीं लौटाया?", "Why wasn't the ₹499 from 1 Sep refunded?", "10:44")}
    ${sent("On 1 Sep the Soundbox was still with you. It was picked up on 12 Sep, so that month's rent was correct.",
      "1 सितंबर को साउंडबॉक्स आपके पास था। पिकअप 12 सितंबर को हुआ, इसलिए उस महीने का किराया सही था।",
      `<div class="src"><span class="chip">Pickup 12 Sep</span><span class="chip">Charge #10 · 1 Sep</span></div>`, "10:44")}
    <div class="sys">${icon("users")}Same bug found in 36 more merchants · ₹32,029 <a class="link" href="needs-you.html">Review</a></div>
  </div>
  <div class="composer"><div class="cbox"><span class="in">Ask anything about this case…</span>
    <span class="btn icon">${icon("mic")}</span><span class="btn pri icon">${icon("send")}</span></div></div>`;
  document.querySelectorAll("[data-chat=done]").forEach((el) => (el.innerHTML = DONE_CHAT));
  document.querySelectorAll("[data-said]").forEach((el) => (el.outerHTML = said(el.dataset.orig, el.dataset.en, el.dataset.said)));

  // ── icons, "View Hindi" ──
  document.querySelectorAll("[data-i]").forEach((el) => el.insertAdjacentHTML("afterbegin", icon(el.dataset.i)));
  document.querySelectorAll("[data-native]").forEach((b) => b.addEventListener("click", () => {
    const box = b.closest(".bub").querySelector(".native");
    box.classList.toggle("open");
    b.textContent = box.classList.contains("open") ? "Hide Hindi" : "View Hindi";
  }));
})();
