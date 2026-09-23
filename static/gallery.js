/* Галерея (сетка миниатюр) и окно просмотра файлов — общий модуль для index.html и share.html.

   Gal.grid(box, items, opt)              — нарисовать сетку карточек в контейнере
   Gal.open(files, index, opt)            — открыть окно просмотра (files — только файлы,
                                            index — позиция выбранного; стрелки ←/→ листают)
   Gal.close()                            — закрыть окно
   Gal.kind(name) -> "image"|"video"|"audio"|"pdf"|"text"|null
   Gal.isOpen() -> bool

   opt: { fileUrl(rel), dlUrl(rel), textUrl(rel), thumbUrl(rel)|null,
          iconOf(name, isDir), fmtSize(n), fmtDate(ts), onOpen(item), onShare(item) }

   Рендер только через textContent/createElement (XSS-безопасно), без innerHTML. */
window.Gal = (function(){
  "use strict";

  const EX = {
    image: ["jpg","jpeg","jfif","png","gif","webp","bmp","svg","avif","heic","heif","ico","tif","tiff"],
    video: ["mp4","m4v","webm","mov","mkv","avi","ogv"],
    audio: ["mp3","wav","oga","ogg","flac","m4a","aac","opus"],
    pdf:   ["pdf"],
    text:  ["txt","text","md","markdown","log","json","jsonl","js","mjs","cjs","ts","tsx","jsx","css","scss",
            "html","htm","xml","yml","yaml","toml","ini","cfg","conf","env","properties","py","rb","php","c",
            "h","cpp","hpp","cc","cs","java","kt","go","rs","sh","bash","zsh","bat","cmd","ps1","sql","csv",
            "tsv","tex","srt","vtt","gitignore","gitattributes","dockerfile","makefile","patch","diff","pl",
            "lua","swift","r","vue","svelte","gradle","lock"]
  };

  function ext(name){
    const m = /\.([^./\\]+)$/.exec(name || "");
    return m ? m[1].toLowerCase() : "";
  }
  function kind(name){
    const e = ext(name);
    for (const k in EX) if (EX[k].indexOf(e) >= 0) return k;
    return null;
  }
  function el(tag, cls, text){
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ================= галерея ================= */

  function grid(box, items, opt){
    box.textContent = "";
    for (const it of items) box.appendChild(card(it, opt));
  }

  function card(it, opt){
    const c = el("div", "gcard");
    const thumb = el("div", "gthumb");
    if (it.type === "dir"){
      thumb.appendChild(el("span", "gico", "📁"));
    } else {
      const k = kind(it.name);
      if (k === "image"){
        const img = document.createElement("img");
        img.loading = "lazy";
        img.decoding = "async";
        img.alt = it.name;
        const t = opt.thumbUrl ? opt.thumbUrl(it.rel) : null;
        img.src = t || opt.fileUrl(it.rel);
        img.onerror = () => {                       // нет миниатюры — иконка вместо битой картинки
          img.remove();
          thumb.appendChild(el("span", "gico", opt.iconOf(it.name, false)));
        };
        thumb.appendChild(img);
      } else {
        thumb.appendChild(el("span", "gico", opt.iconOf ? opt.iconOf(it.name, false) : "📄"));
        if (k === "video") thumb.appendChild(el("span", "gbadge", "▶"));
        else if (k === "audio") thumb.appendChild(el("span", "gbadge", "♪"));
      }
    }
    c.appendChild(thumb);

    const cap = el("div", "gcap");
    const nm = el("span", "gnm", it.name);
    nm.title = it.name;
    cap.appendChild(nm);
    c.appendChild(cap);
    const sub = it.type === "dir" ? "папка"
      : [(opt.fmtSize ? opt.fmtSize(it.size) : ""), (it.mtime && opt.fmtDate ? opt.fmtDate(it.mtime) : "")]
          .filter(Boolean).join(" · ");
    const crumbs = opt.crumbsOf ? opt.crumbsOf(it) : null;
    if (crumbs && crumbs.length){
      /* результат поиска: вместо размера/даты — кликабельный путь до папки */
      const line = el("div", "gsub gsub-crumbs");
      line.appendChild(el("span", null, "…/"));
      crumbs.forEach((seg, i) => {
        const a = el("a", null, seg.label);
        a.href = seg.href || "#";
        a.title = "Открыть папку: " + seg.path + " (Ctrl/Cmd-клик — в новой вкладке)";
        a.onclick = e => {
          if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;   // пусть откроет браузер
          e.preventDefault(); e.stopPropagation();
          if (opt.onOpenPath) opt.onOpenPath(seg.path);
        };
        line.appendChild(a);
        if (i < crumbs.length - 1) line.appendChild(el("span", "sep", "›"));
      });
      c.appendChild(line);
    } else {
      c.appendChild(el("div", "gsub", sub));
    }

    c.title = it.name;
    c.tabIndex = 0;
    c.dataset.rel = String(it.rel || "").replace(/\\/g, "/");
    c.onclick = e => {
      /* папку можно открыть в новой вкладке (Ctrl/Cmd-клик) — если страница это умеет */
      if (it.type === "dir" && (e.ctrlKey || e.metaKey) && opt.onOpenWindow){ opt.onOpenWindow(it); return; }
      opt.onOpen(it);
    };
    c.onkeydown = e => { if (e.key === "Enter" || e.key === " "){ e.preventDefault(); opt.onOpen(it); } };
    return c;
  }

  /* ================= окно просмотра ================= */

  let LB = null;                      // {files, i, opt, refs}

  function isOpen(){ return !!LB; }

  function open(files, index, opt){
    close();
    if (!files || !files.length) return;
    index = Math.max(0, Math.min(index | 0, files.length - 1));
    LB = { files: files, i: index, opt: opt, refs: {} };
    build();
    document.body.classList.add("lb-open");
    document.addEventListener("keydown", onKey, true);
    show(index);
  }

  function close(){
    if (!LB) return;
    stopMedia(LB.refs.stage);
    if (LB.refs.root && LB.refs.root.parentNode) LB.refs.root.parentNode.removeChild(LB.refs.root);
    document.removeEventListener("keydown", onKey, true);
    document.body.classList.remove("lb-open");
    LB = null;
  }

  function build(){
    const opt = LB.opt, r = LB.refs;
    const root = el("div", "lb");
    r.root = root;

    const head = el("div", "lbhead");
    const txt = el("div", "lbtxt");
    r.title = el("div", "lbtitle");
    r.meta = el("div", "lbmeta");
    r.cnt = el("span", "lbcnt");
    txt.appendChild(r.title);
    txt.appendChild(r.meta);
    head.appendChild(txt);

    const btns = el("div", "lbbtns");
    r.up = el("a", "lbbtn", "⬇ Скачать");
    r.up.title = "Скачать файл на устройство";
    btns.appendChild(r.up);
    r.tab = el("a", "lbbtn", "⤢");
    r.tab.target = "_blank";
    r.tab.rel = "noopener";
    r.tab.title = "Открыть в новой вкладке";
    btns.appendChild(r.tab);
    if (opt.onShare){
      const sh = el("button", "lbbtn", "🔗 Поделиться");
      sh.type = "button";
      sh.title = "Общая ссылка — просмотр без входа";
      sh.onclick = () => opt.onShare(LB.files[LB.i]);
      btns.appendChild(sh);
    }
    const x = el("button", "lbbtn", "✕");
    x.type = "button";
    x.title = "Закрыть (Esc)";
    x.onclick = close;
    btns.appendChild(x);
    head.appendChild(btns);
    root.appendChild(head);

    const stage = el("div", "lbstage");
    r.stage = stage;
    r.prev = el("button", "lbnav prev", "‹");
    r.prev.type = "button";
    r.prev.title = "Предыдущий файл (←)";
    r.prev.onclick = () => show(LB.i - 1);
    r.next = el("button", "lbnav next", "›");
    r.next.type = "button";
    r.next.title = "Следующий файл (→)";
    r.next.onclick = () => show(LB.i + 1);
    stage.appendChild(r.prev);
    stage.appendChild(r.next);
    stage.onclick = e => { if (e.target === stage) close(); };   // клик по фону — закрыть
    root.appendChild(stage);

    let tx = 0, ty = 0, tt = 0;
    stage.addEventListener("touchstart", e => {
      const t = e.changedTouches[0]; tx = t.clientX; ty = t.clientY; tt = Date.now();
    }, { passive: true });
    stage.addEventListener("touchend", e => {
      const t = e.changedTouches[0], dx = t.clientX - tx, dy = t.clientY - ty;
      if (Date.now() - tt < 800 && Math.abs(dx) > 60 && Math.abs(dy) < 90) show(LB.i + (dx < 0 ? 1 : -1));
    }, { passive: true });

    r.hint = el("div", "lbhint");
    root.appendChild(r.hint);

    document.body.appendChild(root);
  }

  function stopMedia(stage){
    if (!stage) return;
    for (const m of stage.querySelectorAll("video,audio")){
      try { m.pause(); m.removeAttribute("src"); m.load(); } catch (e) {}
    }
  }

  function preload(i){
    const f = LB && LB.files[i];
    if (!f || kind(f.name) !== "image") return;
    const im = new Image();
    im.src = LB.opt.fileUrl(f.rel);
  }

  function show(i){
    if (!LB) return;
    const files = LB.files, opt = LB.opt, r = LB.refs;
    i = Math.max(0, Math.min(i, files.length - 1));
    LB.i = i;
    const it = files[i];

    stopMedia(r.stage);
    for (const n of Array.prototype.slice.call(r.stage.children))
      if (n !== r.prev && n !== r.next) r.stage.removeChild(n);

    r.title.textContent = it.name;
    const bits = [opt.fmtSize ? opt.fmtSize(it.size) : "", it.mtime && opt.fmtDate ? opt.fmtDate(it.mtime) : ""];
    r.meta.textContent = "";
    const info = bits.filter(Boolean).join(" · ");
    if (info) r.meta.appendChild(document.createTextNode(info + " · "));
    r.cnt.textContent = (i + 1) + " из " + files.length;
    r.meta.appendChild(r.cnt);
    r.up.href = opt.dlUrl(it.rel);
    r.up.setAttribute("download", it.name);
    r.tab.href = opt.fileUrl(it.rel);
    r.prev.disabled = i === 0;
    r.next.disabled = i === files.length - 1;
    r.hint.textContent = files.length > 1
      ? "← → перелистывание · Esc закрыть" + (kind(it.name) ? "" : " · предпросмотр недоступен, можно скачать")
      : "Esc закрыть";

    const k = kind(it.name);
    const fullUrl = opt.fileUrl(it.rel);

    if (k === "image"){
      const img = el("img", "lbimg");
      img.alt = it.name;
      const t = opt.thumbUrl ? opt.thumbUrl(it.rel) : null;
      img.src = t || fullUrl;
      if (t){
        img.classList.add("blur");
        const f = new Image();
        f.decoding = "async";
        f.onload = () => { if (LB && LB.i === i){ img.src = fullUrl; img.classList.remove("blur"); } };
        f.src = fullUrl;
      }
      r.stage.insertBefore(img, r.prev);
      preload(i + 1); preload(i - 1);

    } else if (k === "video"){
      const v = el("video", "lbvid");
      v.controls = true;
      v.playsInline = true;
      v.preload = "metadata";
      v.src = fullUrl;
      r.stage.insertBefore(v, r.prev);

    } else if (k === "audio"){
      const box = el("div", "lbaudio");
      box.appendChild(el("div", "gico2", "🎵"));
      box.appendChild(el("div", "lbl", it.name));
      const a = el("audio");
      a.controls = true;
      a.src = fullUrl;
      box.appendChild(a);
      r.stage.insertBefore(box, r.prev);

    } else if (k === "pdf"){
      const f = el("iframe", "lbpdf");
      f.src = fullUrl;                     // PDF встроенный просмотрщик браузера
      f.title = it.name;
      r.stage.insertBefore(f, r.prev);

    } else if (k === "text"){
      const pre = el("pre", "lbpre", "Загружаю…");
      r.stage.insertBefore(pre, r.prev);
      const mine = i;
      fetch(opt.textUrl(it.rel)).then(resp =>
        resp.json().then(d => ({ ok: resp.ok, d })).catch(() => ({ ok: false, d: null }))
      ).then(res => {
        if (!LB || LB.i !== mine) return;
        if (!res.ok){
          r.stage.removeChild(pre);
          r.stage.insertBefore(noPreview(it, res.d && res.d.error, opt), r.prev);
          return;
        }
        pre.textContent = res.d.text + (res.d.truncated ? "\n\n… показаны первые 2 МБ файла" : "");
      }).catch(() => { if (LB && LB.i === mine) pre.textContent = "Не удалось прочитать файл"; });

    } else {
      r.stage.insertBefore(noPreview(it, null, opt), r.prev);
    }
  }

  function noPreview(it, msg, opt){
    const box = el("div", "lbno");
    box.appendChild(el("div", "big", opt.iconOf ? opt.iconOf(it.name, false) : "📄"));
    box.appendChild(el("div", "msg", msg || "Для этого типа файла предпросмотра нет"));
    const a = el("a", "lbbtn primary", "⬇ Скачать");
    a.href = opt.dlUrl(it.rel);
    a.setAttribute("download", it.name);
    box.appendChild(a);
    return box;
  }

  function onKey(e){
    if (!LB) return;
    // открыт модальный диалог (напр. «Поделиться») — Esc и стрелки принадлежат ему
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "ArrowLeft"){ e.preventDefault(); e.stopPropagation(); show(LB.i - 1); }
    else if (e.key === "ArrowRight"){ e.preventDefault(); e.stopPropagation(); show(LB.i + 1); }
    else if (e.key === "Escape"){ e.preventDefault(); e.stopPropagation(); close(); }
  }

  return { grid: grid, open: open, close: close, kind: kind, ext: ext, isOpen: isOpen };
})();
