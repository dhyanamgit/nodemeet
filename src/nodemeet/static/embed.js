/*! nodemeet embed tags (PolyForm Noncommercial 1.0.0)
 *
 *   <script src="https://your-app.com/meet/static/embed.js" async></script>
 *   <nodemeet-room token="nm1...." height="640px"></nodemeet-room>
 *   <nodemeet-room token="nm1...." inline></nodemeet-room>        (no iframe)
 *   <nodemeet-booking host="host_123"></nodemeet-booking>
 *
 * `base` defaults to the server that served this script.
 */
(function (global) {
  'use strict';
  const script = document.currentScript;
  const DEFAULT_BASE = script && script.src ? script.src.replace(/\/static\/embed\.js(\?.*)?$/, '') : '';
  const loaded = {};

  function load(base, file) {
    const url = base + '/static/' + file;
    if (loaded[url]) return loaded[url];
    loaded[url] = new Promise((resolve, reject) => {
      if (file.endsWith('.css')) {
        const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = url; l.onload = resolve; l.onerror = reject;
        document.head.append(l);
      } else {
        const s = document.createElement('script'); s.src = url; s.onload = resolve; s.onerror = reject;
        document.head.append(s);
      }
    });
    return loaded[url];
  }

  function roomFromToken(token) {
    try { const p = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'); return JSON.parse(atob(p + '==='.slice((p.length + 3) % 4))).room; } catch (e) { return ''; }
  }

  class NodeMeetRoom extends HTMLElement {
    static get observedAttributes() { return ['token']; }
    connectedCallback() { this._render(); }
    attributeChangedCallback() { if (this.isConnected) this._render(); }
    disconnectedCallback() { if (this._ui) this._ui.destroy(); this._ui = null; }
    async _render() {
      const token = this.getAttribute('token');
      if (!token) return;
      const base = (this.getAttribute('base') || DEFAULT_BASE).replace(/\/$/, '');
      const room = this.getAttribute('room') || roomFromToken(token);
      const height = this.getAttribute('height') || '600px';
      this.style.display = 'block';
      this.style.height = height;
      if (this._ui) { this._ui.destroy(); this._ui = null; }
      if (this.hasAttribute('inline')) {
        await Promise.all([load(base, 'nodemeet.css'), load(base, 'nodemeet.js')]);
        await load(base, 'nodemeet-ui.js');
        this.innerHTML = '';
        const box = document.createElement('div');
        box.style.height = '100%';
        this.append(box);
        this._ui = global.NodeMeet.UI.mount(box, { base, token, room, name: this.getAttribute('name') || undefined,
          title: this.getAttribute('title') || '', onLeave: () => this.dispatchEvent(new CustomEvent('nodemeet:leave', { bubbles: true })) });
        return;
      }
      const src = base + '/r/' + encodeURIComponent(room) + '#token=' + encodeURIComponent(token);
      this.innerHTML = '';
      const f = document.createElement('iframe');
      f.src = src;
      f.allow = 'camera; microphone; display-capture; autoplay; fullscreen; clipboard-write';
      f.allowFullscreen = true;
      f.style.cssText = 'width:100%;height:100%;border:0;border-radius:' + (this.getAttribute('radius') || '12px');
      f.title = this.getAttribute('title') || 'Video meeting';
      this.append(f);
    }
  }

  class NodeMeetBooking extends HTMLElement {
    async connectedCallback() {
      const base = (this.getAttribute('base') || DEFAULT_BASE).replace(/\/$/, '');
      await load(base, 'nodemeet.js');
      await load(base, 'booking-widget.js');
      this._w = global.NodeMeet.BookingWidget.mount(this, {
        base, host: this.getAttribute('host'), manage: this.getAttribute('booking') || undefined,
        token: this.getAttribute('manage-token') || undefined, timezone: this.getAttribute('timezone') || undefined,
        name: this.getAttribute('name') || undefined, email: this.getAttribute('email') || undefined,
        days: Number(this.getAttribute('days')) || 14,
      });
    }
  }

  if (!customElements.get('nodemeet-room')) customElements.define('nodemeet-room', NodeMeetRoom);
  if (!customElements.get('nodemeet-booking')) customElements.define('nodemeet-booking', NodeMeetBooking);
})(window);
