/* 顶栏天气小组件 —— 云收音机 / 云电视 / 门户登录页共用
   数据源：全部走国内（中国天气网 + 太平洋IP库 / ipip.net），由 CloudAuth 的
   同源接口 /auth/weather 代理。浏览器不直连任何境外天气服务，也不受跨域限制。
   /auth/weather 不可用时显示占位，不影响任何功能。 */
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const chip = $('#weatherChip');
  if (!chip) return;

  const CACHE_KEY = 'cm_weather_v2';   // 新 key：避免复用旧版境外数据源的缓存
  const TTL = 10 * 60 * 1000;
  const iconEl = $('#wIcon'), tempEl = $('#wTemp'), cityEl = $('#wCity');

  const ICONS = {
    sun:'<circle cx="12" cy="12" r="4.2" fill="currentColor"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    cloudsun:'<circle cx="8.5" cy="8.5" r="3.2" fill="currentColor" opacity=".9"/><path d="M8.5 2v1.6M2 8.5h1.6M3.9 3.9l1.1 1.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M7 17.5h10a3.4 3.4 0 0 0 .3-6.8 5 5 0 0 0-9.5-1A3.6 3.6 0 0 0 7 17.5z" fill="currentColor" opacity=".85"/>',
    cloud:'<path d="M6 17.5h11.5a3.6 3.6 0 0 0 .3-7.2 5.3 5.3 0 0 0-10-1.1A3.8 3.8 0 0 0 6 17.5z" fill="currentColor" opacity=".85"/>',
    rain:'<path d="M6 14.5h11.5a3.6 3.6 0 0 0 .3-7.2 5.3 5.3 0 0 0-10-1.1A3.8 3.8 0 0 0 6 14.5z" fill="currentColor" opacity=".85"/><path d="M8 17.5l-1 3M12.5 17.5l-1 3M17 17.5l-1 3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    snow:'<path d="M6 14.5h11.5a3.6 3.6 0 0 0 .3-7.2 5.3 5.3 0 0 0-10-1.1A3.8 3.8 0 0 0 6 14.5z" fill="currentColor" opacity=".85"/><path d="M8 18h.01M12 19.5h.01M16 18h.01" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
    sleet:'<path d="M6 14.5h11.5a3.6 3.6 0 0 0 .3-7.2 5.3 5.3 0 0 0-10-1.1A3.8 3.8 0 0 0 6 14.5z" fill="currentColor" opacity=".85"/><path d="M8.5 18l-1 2.5M15.5 18h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    storm:'<path d="M6 14h11.5a3.6 3.6 0 0 0 .3-7.2 5.3 5.3 0 0 0-10-1.1A3.8 3.8 0 0 0 6 14z" fill="currentColor" opacity=".85"/><path d="M13 15l-3 4h3l-1.5 4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
    fog:'<path d="M6 12.5h11.5a3.6 3.6 0 0 0 .3-7.2 5.3 5.3 0 0 0-10-1.1A3.8 3.8 0 0 0 6 12.5z" fill="currentColor" opacity=".8"/><path d="M4 16.5h16M6.5 19.5h11" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" opacity=".8"/>'
  };
  function icon(name) {
    return '<svg viewBox="0 0 24 24" fill="none">' + (ICONS[name] || ICONS.cloud) + '</svg>';
  }
  function paint(w) {
    iconEl.innerHTML = icon(w.icon);
    tempEl.textContent = Math.round(w.temp) + '°';
    cityEl.textContent = w.city || '当前位置';
    const bits = [w.city, w.region, w.desc].filter(Boolean);
    if (w.feels != null) bits.push('体感 ' + Math.round(w.feels) + '°');
    if (w.max != null) bits.push('今日 ' + Math.round(w.max) + '° / ' + Math.round(w.min) + '°');
    if (w.hum != null) bits.push('湿度 ' + w.hum + '%');
    if (w.wind != null) bits.push('风速 ' + w.wind + ' km/h');
    chip.title = bits.join(' · ') + ' · ' + (w.source || '国内天气') + '（点击刷新）';
    chip.classList.remove('weather-fail');
  }
  function fail() {
    if (chip.classList.contains('weather-fail')) return;
    iconEl.innerHTML = icon('cloud');
    tempEl.textContent = '--°';
    cityEl.textContent = '天气';
    chip.title = '天气信息暂不可用，点击重试';
    chip.classList.add('weather-fail');
  }

  async function withTimeout(url, ms) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(url, { signal: ctl.signal, cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      return await r.json();
    } finally { clearTimeout(timer); }
  }
  async function fromServer(force) {
    // 绝对地址：/radio/、/tv/、/login.html 下都指向同一个门户接口
    const w = await withTimeout('/auth/weather' + (force ? '?refresh=1' : ''), 20000);
    if (!w || w.ok === false || !Number.isFinite(+w.temp)) throw new Error('bad weather');
    return w;
  }

  let busy = false;
  async function load(force) {
    if (busy) return;
    busy = true;
    try {
      if (!force) {
        const raw = localStorage.getItem(CACHE_KEY);
        if (raw) {
          const c = JSON.parse(raw);
          if (c.data && Number.isFinite(+c.data.temp) && Date.now() - c.at < TTL) { paint(c.data); return; }
        }
      }
      try {
        const w = await fromServer(force);
        paint(w);
        try { localStorage.setItem(CACHE_KEY, JSON.stringify({ at: Date.now(), data: w })); } catch {}
        return;
      } catch { /* 同源天气服务不可用时降级 */ }
      fail();
    } catch { fail(); }
    finally { busy = false; }
  }

  chip.addEventListener('click', () => load(true));
  chip.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); load(true); }
  });
  load(false);
  // 页面长期开着时每 10 分钟刷新；后台标签页不请求
  setInterval(() => { if (!document.hidden) load(true); }, TTL);
})();
