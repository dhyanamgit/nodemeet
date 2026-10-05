/*! nodemeet booking widget (PolyForm Noncommercial 1.0.0). Requires nodemeet.js. */
(function (global) {
  'use strict';
  const NM = global.NodeMeet;
  if (!NM || !NM.Booking) { console.error('[nodemeet] load nodemeet.js before booking-widget.js'); return; }
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const CSS = '.nmb{--nmb-accent:#4f46e5;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111827;background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:20px;max-width:720px}' +
    '.nmb *{box-sizing:border-box}.nmb h3{margin:0 0 4px;font-size:18px}.nmb .nmb-sub{color:#6b7280;margin:0 0 14px}' +
    '.nmb-days{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px;margin-bottom:12px}' +
    '.nmb-day{flex:0 0 auto;border:1px solid #e5e7eb;background:#fff;border-radius:10px;padding:8px 12px;cursor:pointer;text-align:center;min-width:74px;font:inherit}' +
    '.nmb-day small{display:block;color:#6b7280}.nmb-day.nmb-sel,.nmb-time.nmb-sel{border-color:var(--nmb-accent);background:var(--nmb-accent);color:#fff}.nmb-day.nmb-sel small{color:#e0e7ff}' +
    '.nmb-times{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px}' +
    '.nmb-time{border:1px solid #c7d2fe;color:var(--nmb-accent);background:#fff;border-radius:8px;padding:9px;cursor:pointer;font:inherit;font-weight:500}' +
    '.nmb form{display:grid;gap:10px;margin-top:14px}.nmb input,.nmb textarea,.nmb select{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:9px;font:inherit}' +
    '.nmb-btn{background:var(--nmb-accent);color:#fff;border:0;border-radius:8px;padding:10px 16px;cursor:pointer;font:inherit;font-weight:500}' +
    '.nmb-btn.nmb-ghost{background:#f3f4f6;color:#111827}.nmb-btn.nmb-danger{background:#dc2626}.nmb-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}' +
    '.nmb-err{color:#b91c1c;margin:8px 0}.nmb-ok{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:14px;margin:10px 0}.nmb a{color:var(--nmb-accent)}.nmb-tz{max-width:260px}';
  function injectCss() {
    if (document.getElementById('nmb-css')) return;
    const s = document.createElement('style'); s.id = 'nmb-css'; s.textContent = CSS; document.head.append(s);
  }
  const viewerTz = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { return 'UTC'; } };
  const fmt = (iso, tz, o) => new Intl.DateTimeFormat(undefined, Object.assign({ timeZone: tz }, o)).format(new Date(iso));
  const dayKey = (iso, tz) => new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(iso));
  const addDays = (d, n) => { const x = new Date(d + 'T12:00:00Z'); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };

  class BookingWidget {
    constructor(root, opts) {
      injectCss();
      this.root = typeof root === 'string' ? document.querySelector(root) : root;
      this.o = Object.assign({ days: 14 }, opts || {});
      this.base = this.o.base != null ? this.o.base : (NM.base || '');
      this.tz = this.o.timezone || viewerTz();
      this.root.classList.add('nmb');
      if (this.o.manage) this._manage(); else this._start();
    }

    _emit(name, detail) { this.root.dispatchEvent(new CustomEvent('nodemeet:' + name, { detail, bubbles: true })); if (this.o['on' + name]) this.o['on' + name](detail); }
    _html(h) { this.root.innerHTML = h; }
    _error(e) { const d = document.createElement('div'); d.className = 'nmb-err'; d.textContent = e.message || String(e); this.root.prepend(d); }

    async _start() {
      this._html('<p class="nmb-sub">Loading availability…</p>');
      try { this.host = await NM.Booking.host(this.base, this.o.host); } catch (e) { this._html(''); return this._error(e); }
      this.from = new Intl.DateTimeFormat('en-CA', { timeZone: this.tz }).format(new Date());
      await this._loadSlots();
    }

    _tzSelect() {
      let zones = [this.tz];
      try { zones = Intl.supportedValuesOf('timeZone'); } catch (e) { /* older browsers */ }
      if (zones.indexOf(this.tz) === -1) zones.unshift(this.tz);
      return '<select class="nmb-tz" aria-label="Time zone">' + zones.map((z) => '<option' + (z === this.tz ? ' selected' : '') + '>' + esc(z) + '</option>').join('') + '</select>';
    }

    async _loadSlots(reschedule) {
      const end = addDays(this.from, this.o.days - 1);
      let data;
      try {
        data = await NM.Booking.slots(this.base, reschedule ? reschedule.host_id : this.o.host,
          { start: addDays(this.from, -1), end: addDays(end, 1), tz: this.tz });
      } catch (e) { return this._error(e); }
      const days = {};
      data.slots.forEach((s) => {
        const k = dayKey(s.start, this.tz);
        if (k >= this.from && k <= end) (days[k] = days[k] || []).push(s);
      });
      this.days = days;
      this.duration = data.duration_minutes;
      this._renderPicker(reschedule);
    }

    _renderPicker(reschedule) {
      const h = this.host || {};
      const keys = Object.keys(this.days).sort();
      const title = reschedule ? 'Pick a new time' : esc(h.title || 'Book a meeting') + (h.host_name ? ' with ' + esc(h.host_name) : '');
      let html = '<h3>' + title + '</h3><p class="nmb-sub">' + esc(this.duration) + ' min · times shown in ' + this._tzSelect() + '</p>';
      if (!keys.length) html += '<p>No open times in this range.</p>';
      html += '<div class="nmb-days">' + keys.map((k) => {
        const iso = this.days[k][0].start;
        return '<button class="nmb-day" data-day="' + k + '">' + esc(fmt(iso, this.tz, { weekday: 'short' })) + '<small>' + esc(fmt(iso, this.tz, { day: 'numeric', month: 'short' })) + '</small></button>';
      }).join('') + '</div><div class="nmb-times"></div><div class="nmb-form"></div>' +
        '<div class="nmb-row" style="margin-top:12px"><button class="nmb-btn nmb-ghost" data-nav="-1">← Earlier</button><button class="nmb-btn nmb-ghost" data-nav="1">Later →</button></div>';
      this._html(html);
      this.root.querySelector('.nmb-tz').onchange = (e) => { this.tz = e.target.value; this._loadSlots(reschedule); };
      this.root.querySelectorAll('[data-nav]').forEach((b) => {
        b.onclick = () => {
          const today = new Intl.DateTimeFormat('en-CA', { timeZone: this.tz }).format(new Date());
          const next = addDays(this.from, Number(b.dataset.nav) * this.o.days);
          this.from = next < today ? today : next;
          this._loadSlots(reschedule);
        };
      });
      this.root.querySelectorAll('[data-day]').forEach((b) => { b.onclick = () => this._pickDay(b.dataset.day, reschedule); });
      if (keys.length) this._pickDay(keys[0], reschedule);
    }

    _pickDay(k, reschedule) {
      this.root.querySelectorAll('[data-day]').forEach((b) => b.classList.toggle('nmb-sel', b.dataset.day === k));
      const box = this.root.querySelector('.nmb-times');
      box.innerHTML = this.days[k].map((s, i) => '<button class="nmb-time" data-i="' + i + '">' + esc(fmt(s.start, this.tz, { hour: 'numeric', minute: '2-digit' })) + '</button>').join('');
      box.querySelectorAll('[data-i]').forEach((b) => {
        b.onclick = () => {
          box.querySelectorAll('.nmb-time').forEach((x) => x.classList.toggle('nmb-sel', x === b));
          const slot = this.days[k][Number(b.dataset.i)];
          if (reschedule) this._confirmReschedule(slot, reschedule); else this._form(slot);
        };
      });
      this.root.querySelector('.nmb-form').innerHTML = '';
    }

    _form(slot) {
      const when = fmt(slot.start, this.tz, { dateStyle: 'full', timeStyle: 'short' });
      const box = this.root.querySelector('.nmb-form');
      box.innerHTML = '<form><strong>' + esc(when) + '</strong><input name="name" required maxlength="120" placeholder="Your name" value="' + esc(this.o.name || '') + '">' +
        '<input name="email" type="email" required placeholder="you@example.com" value="' + esc(this.o.email || '') + '">' +
        '<textarea name="notes" rows="3" maxlength="2000" placeholder="Anything to share before the meeting? (optional)"></textarea>' +
        '<div class="nmb-row"><button class="nmb-btn" type="submit">Confirm booking</button></div></form>';
      box.querySelector('form').onsubmit = async (ev) => {
        ev.preventDefault();
        const f = ev.target;
        f.querySelector('button').disabled = true;
        try {
          const b = await NM.Booking.book(this.base, { host_id: this.o.host, start: slot.start, name: f.name.value, email: f.email.value, notes: f.notes.value, timezone: this.tz });
          this._emit('booked', b);
          this._done(b, 'You\'re booked!');
        } catch (e) {
          f.querySelector('button').disabled = false;
          this._error(e);
          if (e.status === 409) this._loadSlots();
        }
      };
    }

    _done(b, heading) {
      const when = fmt(b.start, this.tz, { dateStyle: 'full', timeStyle: 'short' });
      const cal = b.calendar_links || {};
      const ics = this.base + '/api/bookings/' + encodeURIComponent(b.id) + '/invite.ics?t=' + encodeURIComponent(b.manage_token);
      this._html('<div class="nmb-ok"><h3>' + esc(heading) + '</h3><p>' + esc(b.title) + ' · ' + esc(when) + ' (' + esc(this.tz) + ')</p>' +
        '<p class="nmb-row"><a class="nmb-btn" href="' + esc(b.join_url) + '" target="_blank" rel="noopener">Join link</a>' +
        (cal.google ? '<a href="' + esc(cal.google) + '" target="_blank" rel="noopener">Google Calendar</a>' : '') +
        (cal.outlook ? '<a href="' + esc(cal.outlook) + '" target="_blank" rel="noopener">Outlook</a>' : '') +
        '<a href="' + esc(ics) + '">Download .ics</a></p>' +
        '<p class="nmb-sub">A confirmation email is on its way. <a href="' + esc(b.manage_url) + '">Reschedule or cancel</a></p></div>');
    }

    async _manage() {
      const hash = new URLSearchParams((global.location.hash || '').slice(1));
      this.token = this.o.token || hash.get('t');
      this._html('<p class="nmb-sub">Loading booking…</p>');
      let b;
      try { b = await NM.Booking.get(this.base, this.o.manage, this.token); } catch (e) { this._html(''); return this._error(e); }
      this.booking = b;
      if (b.attendee_timezone && !this.o.timezone) this.tz = b.attendee_timezone;
      if (b.status === 'cancelled') { this._html('<h3>This booking was cancelled</h3><p class="nmb-sub">' + esc(b.title) + '</p>'); return; }
      const when = fmt(b.start, this.tz, { dateStyle: 'full', timeStyle: 'short' });
      this._html('<h3>' + esc(b.title) + '</h3><p class="nmb-sub">' + esc(when) + ' (' + esc(this.tz) + ') · ' + esc(b.attendee_name) + '</p>' +
        '<div class="nmb-row"><a class="nmb-btn" href="' + esc(b.join_url) + '" target="_blank" rel="noopener">Join</a>' +
        '<button class="nmb-btn nmb-ghost" data-a="reschedule">Reschedule</button><button class="nmb-btn nmb-danger" data-a="cancel">Cancel</button></div>');
      this.root.querySelector('[data-a="cancel"]').onclick = async () => {
        const reason = global.prompt('Cancel this meeting? Optional reason:', '');
        if (reason === null) return;
        try { await NM.Booking.cancel(this.base, b.id, this.token, reason); this._emit('cancelled', b); this._html('<div class="nmb-ok"><h3>Cancelled</h3><p>We let everyone know.</p></div>'); } catch (e) { this._error(e); }
      };
      this.root.querySelector('[data-a="reschedule"]').onclick = () => {
        this.from = new Intl.DateTimeFormat('en-CA', { timeZone: this.tz }).format(new Date());
        this._loadSlots(b);
      };
    }

    _confirmReschedule(slot, b) {
      const when = fmt(slot.start, this.tz, { dateStyle: 'full', timeStyle: 'short' });
      const box = this.root.querySelector('.nmb-form');
      box.innerHTML = '<form><strong>Move to ' + esc(when) + '?</strong><div class="nmb-row"><button class="nmb-btn" type="submit">Confirm new time</button></div></form>';
      box.querySelector('form').onsubmit = async (ev) => {
        ev.preventDefault();
        try {
          const nb = await NM.Booking.reschedule(this.base, b.id, this.token, slot.start);
          this._emit('rescheduled', nb);
          this._done(nb, 'Rescheduled!');
        } catch (e) { this._error(e); if (e.status === 409) this._loadSlots(b); }
      };
    }
  }

  NM.BookingWidget = { BookingWidget, mount: (root, opts) => new BookingWidget(root, opts) };
})(typeof window !== 'undefined' ? window : globalThis);
