// SPDX-License-Identifier: MIT
(function () {
  "use strict";

  const API_PREFIX = "/api/external-url/";
  const ROW_ID = "emby115-external-row";
  let lastItemId = "";
  let renderedFor = "";

  const players = [
    { key: "pot", label: "Pot", icon: "▶", url: (u) => "potplayer://" + u },
    { key: "vlc", label: "VLC", icon: "▰", url: (u) => "vlc://" + u },
    { key: "mpv", label: "MPV", icon: "▶", url: (u) => "mpv://play/" + encodeURIComponent(u) },
    { key: "iina", label: "IINA", icon: "▶", url: (u) => "iina://weblink?url=" + encodeURIComponent(u) },
    { key: "nplayer", label: "NPlayer", icon: "▶", url: (u) => u.replace(/^http:\/\//, "nplayer-http://").replace(/^https:\/\//, "nplayer-https://") },
    { key: "mx", label: "MX", icon: "▶", url: (u) => "intent:" + u + "#Intent;package=com.mxtech.videoplayer.ad;end" },
    { key: "infuse", label: "Infuse", icon: "▷", url: (u) => "infuse://x-callback-url/play?url=" + encodeURIComponent(u) }
  ];

  function itemIdFromText(text) {
    if (!text) return "";
    const patterns = [
      /[?&]id=(\d+)/i,
      /\/Items\/(\d+)\/PlaybackInfo/i,
      /\/Items\/(\d+)\/(?:Intros|ThemeMedia|Similar|SpecialFeatures)/i,
      /\/Users\/[^/]+\/Items\/(\d+)/i,
      /\/videos\/(\d+)\/original/i
    ];
    for (const pattern of patterns) {
      const match = pattern.exec(text);
      if (match) return match[1];
    }
    return "";
  }

  function currentItemId() {
    const fromLocation = itemIdFromText(location.href);
    return fromLocation || lastItemId;
  }

  function rememberItemIdFromUrl(url) {
    const id = itemIdFromText(String(url || ""));
    if (id) {
      lastItemId = id;
      scheduleRender();
    }
  }

  function patchNetwork() {
    if (window.__emby115NetworkPatched) return;
    window.__emby115NetworkPatched = true;

    const originalFetch = window.fetch;
    if (originalFetch) {
      window.fetch = function (input, init) {
        rememberItemIdFromUrl(typeof input === "string" ? input : input && input.url);
        return originalFetch.apply(this, arguments);
      };
    }

    const originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      rememberItemIdFromUrl(url);
      return originalOpen.apply(this, arguments);
    };
  }

  function visible(el) {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 20 && rect.height > 20;
  }

  function scoreButtonRow(el) {
    const rect = el.getBoundingClientRect();
    if (!visible(el)) return -1;
    if (rect.top < 120) return -1;
    const text = (el.textContent || "").trim();
    let score = 0;
    if (/播放|Play|Resume|从头播放|恢复播放/i.test(text)) score += 100;
    if (/预告片|Trailer|已播放|Played|收藏|Favorite/i.test(text)) score += 25;
    if (el.querySelectorAll("button, .emby-button, [role='button']").length >= 2) score += 40;
    if (rect.left > window.innerWidth * 0.18) score += 15;
    if (rect.width > 180 && rect.width < window.innerWidth * 0.88) score += 10;
    if (rect.height > 40 && rect.height < 120) score += 10;
    return score;
  }

  function findAnchor() {
    const existing = document.getElementById(ROW_ID);
    if (existing && visible(existing.parentElement)) return existing;

    const playButton = Array.from(document.querySelectorAll("button, .button-link, .emby-button, [role='button']"))
      .filter((el) => visible(el) && /播放|Play|Resume|从头播放|恢复播放/i.test((el.textContent || "").trim()))
      .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
    if (playButton) {
      let best = playButton.parentElement || playButton;
      let bestScore = scoreButtonRow(best);
      let node = playButton.parentElement;
      for (let i = 0; node && i < 5; i += 1, node = node.parentElement) {
        const score = scoreButtonRow(node);
        if (score > bestScore) {
          best = node;
          bestScore = score;
        }
      }
      if (bestScore > 0) return best;
    }

    const candidates = Array.from(document.querySelectorAll(".detailButtonContainer, .detailButtons, .mainDetailButtons, .itemDetailButtons"))
      .map((el) => ({ el, score: scoreButtonRow(el) }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score);
    return candidates[0] ? candidates[0].el : null;
  }

  function makeButton(player, data) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "emby115-ext-btn emby115-ext-" + player.key;
    button.title = player.label;
    button.innerHTML = "<span class=\"emby115-ext-icon\">" + player.icon + "</span><span>" + player.label + "</span>";
    button.addEventListener("click", function () {
      location.href = player.url(data.url);
    });
    return button;
  }

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const input = document.createElement("textarea");
    input.value = text;
    input.style.position = "fixed";
    input.style.left = "-9999px";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }

  function buildRow(itemId, data) {
    const row = document.createElement("div");
    row.id = ROW_ID;
    row.className = "emby115-ext-row";
    row.dataset.itemId = itemId;

    for (const player of players) {
      row.appendChild(makeButton(player, data));
    }

    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "emby115-ext-btn emby115-ext-copy";
    copy.innerHTML = "<span class=\"emby115-ext-icon\">⧉</span><span>复制链接</span>";
    copy.addEventListener("click", async function () {
      await copyText(data.url);
      copy.classList.add("emby115-copied");
      const old = copy.querySelector("span:last-child").textContent;
      copy.querySelector("span:last-child").textContent = "已复制";
      setTimeout(function () {
        copy.classList.remove("emby115-copied");
        copy.querySelector("span:last-child").textContent = old;
      }, 1200);
    });
    row.appendChild(copy);

    return row;
  }

  async function render() {
    const itemId = currentItemId();
    if (!itemId) return;

    const anchor = findAnchor();
    if (!anchor) return;

    const existing = document.getElementById(ROW_ID);
    if (existing && existing.dataset.itemId === itemId && renderedFor === itemId) return;
    if (existing) existing.remove();

    let data;
    try {
      const response = await fetch(API_PREFIX + encodeURIComponent(itemId), { credentials: "same-origin" });
      if (!response.ok) return;
      data = await response.json();
    } catch (_) {
      return;
    }

    const row = buildRow(itemId, data);
    anchor.insertAdjacentElement("afterend", row);
    renderedFor = itemId;
  }

  let timer = 0;
  function scheduleRender() {
    clearTimeout(timer);
    timer = setTimeout(render, 250);
  }

  function installStyle() {
    if (document.getElementById("emby115-ext-style")) return;
    const style = document.createElement("style");
    style.id = "emby115-ext-style";
    style.textContent = `
      .emby115-ext-row {
        display: flex;
        flex-wrap: wrap;
        gap: .6em;
        margin: 1em 0 1.1em;
        align-items: center;
        position: relative;
        z-index: 5;
        width: min(100%, 56em);
        min-height: 3em;
        clear: both;
      }
      .emby115-ext-btn {
        border: 0;
        border-radius: .45em;
        padding: .72em 1.05em;
        min-height: 2.8em;
        color: #fff;
        background: rgba(90, 80, 78, .78);
        display: inline-flex;
        align-items: center;
        gap: .55em;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
        box-shadow: 0 .18em .65em rgba(0, 0, 0, .18);
      }
      .emby115-ext-btn:hover {
        background: rgba(120, 104, 100, .9);
      }
      .emby115-ext-icon {
        width: 1.45em;
        height: 1.45em;
        border-radius: 50%;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background: rgba(255, 255, 255, .16);
        font-size: .95em;
      }
      .emby115-ext-pot .emby115-ext-icon { background: #f5d51d; color: #fff; }
      .emby115-ext-vlc .emby115-ext-icon { background: #f28c28; }
      .emby115-ext-mpv .emby115-ext-icon,
      .emby115-ext-iina .emby115-ext-icon,
      .emby115-ext-mx .emby115-ext-icon { background: #388ff2; }
      .emby115-copied { background: rgba(36, 132, 86, .95); }
      @media (max-width: 720px) {
        .emby115-ext-row { gap: .45em; }
        .emby115-ext-btn { padding: .64em .82em; min-height: 2.5em; }
      }
    `;
    document.head.appendChild(style);
  }

  function start() {
    patchNetwork();
    installStyle();
    scheduleRender();
    window.addEventListener("hashchange", scheduleRender);
    window.addEventListener("popstate", scheduleRender);
    new MutationObserver(scheduleRender).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
