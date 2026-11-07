// Threads-Ideas: every 30 s asks pc_helper what to do. A scan reads threads.com/saved in a background tab and posts the
// whole list; an unsave removes ONE post per turn (30-60 s apart) through the post's own ⋯ menu, then checks it is gone.
const HELPER = "http://127.0.0.1:8796";  // each computer's own helper (pc_helper on MauStation, mac_helper on MauMac)
const SAVED = "https://www.threads.com/saved";
const UNSAVE_LABELS = ["Cancelar", "Quitar de guardados", "Unsave", "Remove from saved"];

chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("tick", { periodInMinutes: 0.5 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create("tick", { periodInMinutes: 0.5 }));
chrome.alarms.onAlarm.addListener(a => a.name === "tick" && tick().catch(e => console.warn("Threads-Ideas:", e)));

async function tick() {
  const { busyUntil = 0, nextUnsave = 0, nextTry = 0, skip = [] } = await chrome.storage.session.get(["busyUntil", "nextUnsave", "nextTry", "skip"]);
  if (Date.now() < busyUntil || Date.now() < nextTry) return;  // a turn is running, or the last one failed (back off)
  const pending = await (await fetch(`${HELPER}/threads/pending`)).json();
  const unsaveCode = (pending.unsave || []).find(c => !skip.includes(c));  // posts that failed once wait for a browser restart
  if (!pending.scan && !(unsaveCode && Date.now() >= nextUnsave)) return;
  await chrome.storage.session.set({ busyUntil: Date.now() + 120000 });
  try {
    if (pending.scan) await scan();
    else {
      await chrome.storage.session.set({ nextUnsave: Date.now() + (30 + Math.random() * 30) * 1000 });
      try {
        await unsave(unsaveCode);
      } catch (e) {
        await chrome.storage.session.set({ skip: [...skip, unsaveCode] });  // skip this post, don't retry it every turn
        throw e;
      }
    }
  } catch (e) {
    await chrome.storage.session.set({ nextTry: Date.now() + 600000 });  // a failure waits 10 min, never a 30 s loop
    throw e;
  } finally {
    await chrome.storage.session.set({ busyUntil: 0 });
  }
}

async function withSavedTab(fn) {
  const tab = await chrome.tabs.create({ url: SAVED, active: false });
  try {
    await loaded(tab.id);
    return await fn(tab.id);
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

function loaded(tabId) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { chrome.tabs.onUpdated.removeListener(on); reject(new Error("threads.com did not load")); }, 45000);
    function on(id, info) {
      if (id === tabId && info.status === "complete") { clearTimeout(t); chrome.tabs.onUpdated.removeListener(on); resolve(); }
    }
    chrome.tabs.onUpdated.addListener(on);
  });
}

async function run(tabId, func, args = []) {
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return r.result;
}

// Runs inside threads.com/saved: the list is JSON embedded in the navigated document (a fetch of /saved does not carry it).
function readSaved() {
  const s = [...document.scripts].find(x => x.textContent.includes('"saved_media"'));
  if (!s) return { error: /login/.test(location.pathname) ? "not logged in to threads.com" : "saved list not found in page" };
  let found = null;
  (function walk(o) {
    if (!o || typeof o !== "object" || found) return;
    if (o.saved_media) { found = o.saved_media; return; }
    for (const k in o) walk(o[k]);
  })(JSON.parse(s.textContent));
  const clean = u => (u || "").replace(/[?#].*$/, "");
  const posts = (found.edges || []).map(e => (e.node.thread_items || [e.node])[0].post || e.node).map(p => {
    const info = p.text_post_app_info || {};
    const links = [info.link_preview_attachment && info.link_preview_attachment.url,
      ...((info.text_fragments || {}).fragments || []).map(f => f.link_fragment && f.link_fragment.uri)].filter(Boolean).map(clean);
    return { code: p.code, user: p.user.username, link: links[0] || "", caption: (p.caption && p.caption.text) || "" };
  });
  return { posts, has_next_page: !!(found.page_info && found.page_info.has_next_page) };
}

async function scan() {
  const r = await withSavedTab(tabId => run(tabId, readSaved));
  if (r.error) throw new Error(r.error);
  await fetch(`${HELPER}/threads/posts`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(r) });
}

// Runs inside threads.com/saved: opens the post's ⋯ («Más») menu and clicks its unsave item.
async function clickUnsave(code, labels) {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let link = null;
  for (let i = 0; i < 40 && !(link = document.querySelector(`a[href*="/post/${code}"]`)); i++) {  // posts render as you scroll
    window.scrollBy(0, window.innerHeight);
    await sleep(700);
  }
  if (!link) return "post not on the page";
  // the ⋯ button's icon is named by an <svg><title>Más</title> (no aria-label); climb to the post's own box
  const moreIn = el => [...el.querySelectorAll('[role="button"]')].filter(b =>
    [...b.querySelectorAll("svg")].some(s => ["Más", "More"].includes(s.getAttribute("aria-label") || (s.querySelector("title") || {}).textContent)));
  let box = link;
  while (box && !moreIn(box).length) box = box.parentElement;
  if (!box) return "menu button not found";
  const codes = new Set([...box.querySelectorAll('a[href*="/post/"]')].map(a => a.getAttribute("href").split("/post/")[1].split(/[/?]/)[0]));
  const buttons = moreIn(box);
  if (codes.size !== 1 || buttons.length !== 1) return `ambiguous post box (${codes.size} posts, ${buttons.length} menus)`;  // never risk another post
  buttons[0].click();
  for (let i = 0; i < 20; i++) {
    const item = [...document.querySelectorAll('[role="menuitem"]')].find(e => labels.includes(e.innerText.trim()));
    if (item) { item.click(); await sleep(1500); return "clicked"; }
    await sleep(250);
  }
  return "unsave item not in the menu";
}

function stillSaved(code) {
  return [...document.scripts].some(x => x.textContent.includes('"saved_media"') && x.textContent.includes(`"code":"${code}"`));
}

async function unsave(code) {
  const res = await withSavedTab(tabId => run(tabId, clickUnsave, [code, UNSAVE_LABELS]));
  if (res !== "clicked") throw new Error(`unsave ${code}: ${res}`);
  const gone = !(await withSavedTab(tabId => run(tabId, stillSaved, [code])));  // reload and check before telling pc_helper
  if (!gone) throw new Error(`unsave ${code}: still saved after clicking`);
  await fetch(`${HELPER}/threads/unsaved`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
}
