"""SiteBrowser: a model's hands on one duck-typed Playwright page — extraction, retries, in-memory downloads.

Fair-play: nothing here decides where to go or what to click. The model supplies every URL, action and index from
its observations; this module only reads the current page, performs the model's single action, retries flaky
navigations and recovers a dropped session with the run's own credentials. Pages are duck-typed so unit tests
never launch a browser; the injected page may be a real Playwright page, whose own page.evaluate() runs the JS.
Downloads are captured to memory bytes and the download is cancelled afterwards: nothing is left on disk.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .contracts import RuntimeBudget

TEXT_CAP = 18_000  # chars of compressed visible text per observation
GOTO_ATTEMPTS = 3  # 1 try + 2 retries against deliberately flaky error pages
MARK_ATTR = "data-atira-i"  # marks the interactive set that an observation's indices refer to
INDEX_LIMIT = 300  # a page with more interactive nodes than this is paged/split by the model, not truncated here

INTERACTIVE = "a,button,input,select,textarea,iframe,[role],[contenteditable='true']"

# Roles that actually accept text or a choice; the observation carries state only for these (never a giant
# [role] container that would swallow the 300-index budget). Field state stays model-visible; secret values never do.
FIELD_ROLES = ("textbox", "combobox", "checkbox", "radio", "spinbutton", "searchbox")

# A generic wall-clock/timer line in visible text (a clock HH:MM(:SS) with optional short label, a bare
# segmented counter like 00:03, or an elapsed "N s/min" line). The signature's text component drops these so an
# incidental tick never masks a real, same-length change. Generic formatting only — no scenario facts.
_CLOCK = r"(?:[A-Za-z][A-Za-z ().,'-]{0,24}[:\-\u2013]? ?)?\d{1,2}:\d{2}(?::\d{2})?[. ]*"
_DOTS = r"(?:[A-Za-z][A-Za-z ().,'-]{0,24}[:\-\u2013]? ?)?\d{1,2}(?::\d{2})+[. ]*"
_ELAPSED = r"(?:[A-Za-z][A-Za-z ().,'-]{0,24}[:\-\u2013]? ?)?\d{1,3}(?:[.,]\d+)? ?" \
           r"(?:s|sec|secs|second|seconds|sek|sekunde|sekunden|min|minute|minutes)[. ]*"
TIMER_LINE = rf"^(?:{_CLOCK}|{_DOTS}|{_ELAPSED})$"

# Generic UI affordance words for popup dismissal (cookie walls / modal dialogs). These are universal web-UI
# words, not scenario facts; the model still decides WHEN to call dismiss_popups.
POPUP_WORDS = ("akzeptieren", "accept", "alle akzeptieren", "accept all", "einwilligung", "zustimmung", "consent",
               "ablehnen", "reject", "nur wesentliche", "only necessary", "notwendig", "essential", "schließen",
               "close", "ok", "verstanden", "x", "datenschutz", "datenschutzhinweise", "cookie", "impressum")
LOGIN_WORDS = ("anmelden", "login", "einloggen", "log in", "sign in", "passwort", "password", "benutzer",
               "anmeldung", "benutzername", "username")


def _js() -> dict[str, str]:
    """The page-reading JS, built with json.dumps injections and braces as doubled literals (see _render_js)."""
    probe = """(() => {
  const INTERACTIVE = %r;
  const POPUP_WORDS = %r;
  const LOGIN_WORDS = %r;
  const MARK = %r;
  const INDEX_LIMIT = %r;
  const clean = (s) => (s == null ? '' : String(s)).replace(/\\s+/g, ' ').trim();
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 || rect.height > 1;
  };
  const walk = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  const lines = [];
  let node;
  while ((node = walk.nextNode())) {
    const el = node.parentElement;
    if (!el || !visible(el)) continue;
    const t = node.textContent;
    if (t && t.trim()) lines.push(t);
  }
  const parts = lines.slice();
  for (const f of document.querySelectorAll('iframe')) {
    let doc = null;
    try { doc = f.contentDocument; } catch (e) { doc = null; }
    if (doc && doc.body) parts.push(doc.body.innerText || '');
  }
  const merged = [];
  for (const raw of parts) {
    const t = clean(raw);
    if (t && merged[merged.length - 1] !== t) merged.push(t);
  }
  const roleOf = (el) => {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = ((el.getAttribute && el.getAttribute('type')) || (el.type || '')).toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || type === 'submit' || type === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'iframe') return 'iframe';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') return (type === 'checkbox' || type === 'radio') ? type : 'textbox';
    return tag;
  };
  // A field is fillable text/choice, not a submit/reset/button. Generic HTML semantics, no site facts.
  const FIELD_ROLES = %r;
  const isField = (el) => {
    const role = roleOf(el);
    const type = ((el.getAttribute && el.getAttribute('type')) || (el.type || '')).toLowerCase();
    if (['submit', 'reset', 'button', 'image', 'file'].includes(type)) return false;
    return FIELD_ROLES.includes(role);
  };
  // Accessible name: aria-labelledby wins, then the <label for> this id points at, then a wrapping <label>,
  // then aria-label / placeholder / title / own text / name / id. Mirrors how a human reads a form.
  const labelFor = (el) => {
    try {
      const lb = el.getAttribute && el.getAttribute('aria-labelledby');
      if (lb) {
        const by = document.getElementById(lb.trim());
        if (by && clean(by.innerText)) return clean(by.innerText);
      }
      const id = el.getAttribute && el.getAttribute('id');
      if (id) {
        const lab = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
        if (lab && clean(lab.innerText)) return clean(lab.innerText);
      }
      const wrap = el.closest && el.closest('label');
      if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    } catch (e) {}
    return '';
  };
  // The frame boundary: a cross/same-document iframe is reported as ONE boundary element whose inner text is
  // surfaced for reading. Its inner nodes are NOT flattened into the top-level interactive list, so the model
  // never "clicks an index" that is really inside another frame.
  const allNodes = [...document.querySelectorAll(INTERACTIVE)];
  const isFrame = (el) => el.tagName.toLowerCase() === 'iframe';
  const usable = (el) => !el.disabled && (isFrame(el) || visible(el));
  // disabled controls are OBSERVED (the model must see a locked button) but never clicked/filled blindly, so
  // they get NO index and are reported separately, not in the addressed targets.
  const disabled_elements = [];
  for (const el of allNodes) {
    if (!el.disabled || !visible(el) || isFrame(el)) continue;
    if (disabled_elements.length >= 40) break;
    const drole = roleOf(el);
    const dtype = ((el.getAttribute && el.getAttribute('type')) || (el.type || '')).toLowerCase();
    const dname = labelFor(el) || clean(el.getAttribute && el.getAttribute('aria-label')) || clean(el.innerText)
      || clean(el.value) || clean(el.getAttribute && el.getAttribute('name'));
    disabled_elements.push({role: drole, tag: el.tagName.toLowerCase(), name: dname.slice(0, 120),
      disabled: true, type: dtype});
  }
  const targets = allNodes.filter(usable);
  for (const el of allNodes) el.removeAttribute(MARK);
  targets.slice(0, 1000).forEach((el, n) => el.setAttribute(MARK, String(n)));
  const elements = [];
  for (const el of targets) {
    const role = roleOf(el);
    const attr = (k) => (el.getAttribute ? (el.getAttribute(k) || '') : '');
    const ownText = (el.innerText || '').slice(0, 120);
    const buttonValue = ['button', 'submit'].includes((el.type || '').toLowerCase()) ? (el.value || '') : '';
    const label = labelFor(el);
    const name = clean(attr('aria-label')) || label || clean(attr('placeholder')) || clean(attr('title'))
      || clean(ownText) || clean(buttonValue) || clean(attr('name')) || clean(attr('id'));
    const item = {i: elements.length, role: role, tag: el.tagName.toLowerCase(), name: name};
    if (label && label !== name) item.label = label;
    if (isField(el)) {
      const itype = ((el.getAttribute && el.getAttribute('type')) || (el.type || '')).toLowerCase();
      item.field = {type: itype, required: !!el.required, disabled: !!el.disabled,
        readonly: !!el.readOnly || el.getAttribute('readonly') !== null, hidden: !visible(el)};
      if (el.hasAttribute && el.hasAttribute('data-tan')) item.field.tan = true;
      if (itype === 'checkbox' || itype === 'radio') item.field.checked = !!el.checked;
      if (el.checkValidity) item.field.valid = !!el.checkValidity();
      const vm = (el.validationMessage || '');
      if (vm) item.field.validation_message = vm;
      if (el.willValidate && el.pattern) item.field.pattern = el.pattern;
      if (el.maxLength && el.maxLength > 0 && el.maxLength < 524288) item.field.maxlength = el.maxLength;
    }
    if (isFrame(el)) {
      let ft = null;
      try { ft = el.contentDocument && el.contentDocument.body ? (el.contentDocument.body.innerText || '') : null; }
      catch (e) { ft = null; }
      item.frame = {accessible: ft != null, text_len: ft ? clean(ft).length : 0,
        inner_interactive: ft != null && el.contentDocument ? el.contentDocument.querySelectorAll(INTERACTIVE).length : null};
    }
    if (['textbox', 'combobox', 'checkbox', 'radio', 'spinbutton', 'searchbox'].includes(role)) {
      item.value_hint = el.type === 'password' ? '********' : clean(el.value);
      if (['checkbox', 'radio'].includes(role)) item.value_hint = el.checked ? 'checked' : 'unchecked';
    }
    if (attr('href')) item.href = attr('href');
    if (el.type) item.type = el.type;
    elements.push(item);
    if (elements.length >= INDEX_LIMIT) break;
  }
  const dialogSel = '[role="dialog"],[aria-modal="true"],dialog[open],#onetrust-consent-sdk,'
    + '.cky-consent-container,.fc-consent-root';
  const dialogs = document.querySelectorAll(dialogSel).length;
  let popup = false;
  if (dialogs) {
    for (const el of document.querySelectorAll('[role="dialog"] button,[aria-modal="true"] button,'
      + 'dialog[open] button')) {
      const t = (el.innerText || '').toLowerCase();
      if (POPUP_WORDS.some((w) => t.includes(w))) { popup = true; break; }
    }
  }
  let login = false;
  for (const el of document.querySelectorAll('button,input[type="submit"],[role="button"]')) {
    const t = ((el.innerText || el.value || '') + '').toLowerCase();
    if (LOGIN_WORDS.some((w) => t.includes(w))) { login = true; break; }
  }
  let login_form = false;
  let credential_login = false, tan_form = false, has_tan = false;
  for (const f of document.querySelectorAll('form')) {
    if (f.querySelector('input[type="password"]')) login_form = true;
    const inner = clean((f.innerText || '') + ' ' + (f.getAttribute('action') || '') + ' '
      + [...f.querySelectorAll('input,button,label')].map((x) => (x.name || '') + ' ' + (x.id || '')).join(' '))
      .toLowerCase();
    const hasPw = !!f.querySelector('input[type="password"]');
    const hasUser = !!f.querySelector('input[name*="user" i],input[name*="email" i],input[id*="user" i],'
      + 'input[id*="email" i],input[type="email"]');
    if (hasPw && hasUser && !/\btan\b/.test(inner)) credential_login = true;
    if (/\btan\b|transaktionsnummer|sms-?tan|chip-?tan/.test(inner)) { tan_form = true; }
  }
  for (const el of document.querySelectorAll('input')) {
    const key = clean((el.name || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('placeholder') || '')
      + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
    if (el.hasAttribute && el.hasAttribute('data-tan')) { has_tan = true; tan_form = true; }
    if (/\btan\b|transaktionsnummer/.test(key)) { has_tan = true; tan_form = true; }
  }
  // Visible alerts: ARIA live/alert regions and open dialogs, so the model reads the page's own warnings.
  const alerts = [];
  for (const el of document.querySelectorAll('[role="alert"],[aria-live="assertive"],[aria-live="polite"]')) {
    if (!visible(el)) continue;
    const t = clean(el.innerText);
    if (t && alerts.length < 20 && !alerts.some((a) => a === t)) alerts.push(t.slice(0, 240));
  }
  // Native validation: the browser's own messages for required/pattern/range fields, per field.
  const native_validation = [];
  for (const el of document.querySelectorAll('input,select,textarea')) {
    if (native_validation.length >= 30) break;
    if (!el.willValidate) continue;
    const req = !!el.required, bad = el.validity && el.validity.badInput, tooLong = el.validity && el.validity.tooLong;
    if (!req && !bad && !tooLong && !(el.validationMessage && !el.checkValidity())) continue;
    const message = el.validationMessage || '';
    native_validation.push({name: clean(el.name || el.id || labelFor(el)).slice(0, 120),
      required: req, valid: el.checkValidity(), message: message.slice(0, 160)});
  }
  const any_invalid = native_validation.some((x) => !x.valid);
  return JSON.stringify({
    url: location.href,
    title: document.title || '',
    text: merged.join('\\n'),
    elements: elements,
    interactive_count: targets.length,
    forms: document.querySelectorAll('form').length,
    password_fields: document.querySelectorAll('input[type="password"]').length,
    dialogs: dialogs,
    popup_hint: popup,
    login_hint: login,
    login_form: login_form,
    credential_login: credential_login,
    tan_form: tan_form || has_tan,
    disabled_elements: disabled_elements,
    alerts: alerts,
    native_validation: native_validation,
    any_invalid: any_invalid
  });
})()"""

    dismiss = """(() => {
  const POPUP_WORDS = %r;
  const roots = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog[open]')];
  let n = 0;
  for (const root of roots) {
    for (const sel of ['button', '[role="button"]', 'a']) {
      for (const el of root.querySelectorAll(sel)) {
        const t = (el.innerText || '').toLowerCase();
        if (POPUP_WORDS.some((w) => t.includes(w))) {
          try { el.click(); n += 1; } catch (e) {}
          if (n >= 4) return n;
        }
      }
    }
  }
  return n;
})()"""

    receipt = """(() => {
  const patterns = %r;
  const res = patterns.map((p) => new RegExp(p, 'i'));
  const hits = [];
  const elements = document.querySelectorAll('body *');
  for (const el of elements) {
    if (hits.length >= 60) break;
    if (el.matches && el.matches('input,select,textarea')) {
      const label = [((el.closest && el.closest('label')) || {}).innerText || '', el.getAttribute('name') || '',
        el.getAttribute('placeholder') || '', el.getAttribute('aria-label') || ''].join(' ');
      if (res.some((re) => re.test(label))) {
        hits.push({i: hits.length, kind: 'field', label: label.replace(/\\s+/g, ' ').trim().slice(0, 160),
          value: el.value != null ? String(el.value).slice(0, 200) : '', tag: el.tagName.toLowerCase()});
      }
      continue;
    }
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ').trim();
    if (own && res.some((re) => re.test(own))) {
      const box = (el.closest && el.closest('tr,li,p,div,label,section,dt,dd')) || el;
      const line = ((box.innerText || own) || '').replace(/\\s+/g, ' ').trim().slice(0, 240);
      if (!hits.some((h) => h.kind === 'line' && h.text === line)) {
        hits.push({i: hits.length, kind: 'line', text: line});
      }
    }
  }
  return JSON.stringify({receipt_hits: hits.slice(0, 60)});
})()"""
    return {"probe": _render_js(probe, INTERACTIVE, list(POPUP_WORDS), list(LOGIN_WORDS), MARK_ATTR, INDEX_LIMIT,
                             list(FIELD_ROLES)),
            "dismiss": _render_js(dismiss, list(POPUP_WORDS)),
            "receipt": receipt}


def _render_js(script: str, *values: Any) -> str:
    """Fill the script's %r slots from json.dumps and un-double the remaining literal braces.

    The scripts above double every literal `{`/`}`, so the substitution can never clash with JS braces.
    """
    for value in values:
        script = script.replace("%r", json.dumps(value, ensure_ascii=False), 1)
    return script.replace("{{", "{").replace("}}", "}")


JS = _js()

INDEX_ERROR = (f"that element index no longer exists on the current page; call observe() and use the fresh "
               f"indices (the page changed, or it has more than {INDEX_LIMIT} interactive elements)")


def compress_text(raw: str, cap: int = TEXT_CAP) -> str:
    """Whitespace-normalize, drop blanks, collapse repeated consecutive lines, cap the size."""
    lines: list[str] = []
    for chunk in (raw or "").split("\n"):
        line = " ".join(chunk.split())
        if not line:
            continue
        if lines and lines[-1] == line:  # collapse a repeated run of the same line
            continue
        lines.append(line)
    out = "\n".join(lines)
    if len(out) > cap:
        out = out[:cap] + "\n[truncated: visible text continues beyond the shown cap]"
    return out


_TIMER_RE = re.compile(TIMER_LINE)


def _normalize_text(text: str) -> str:
    """Drop incidental timer/clock lines from visible text so a ticking counter never masks a real change."""
    kept = [ln for ln in (text or "").split("\n") if ln and not _TIMER_RE.match(ln)]
    return "\n".join(kept)


def _field_projection(element: dict) -> dict | None:
    """A secret-free projection of one element for the signature: field STATE, never a field VALUE.

    Passwords/TANs are masked by the probe itself; only booleans, types, the validation message and a value
    LENGTH enter here, so a filled secret changes the signature without ever being written into it."""
    role = element.get("role")
    field = element.get("field")
    if not isinstance(field, dict) and role not in FIELD_ROLES:
        return None
    field = field or {}
    hint = str(element.get("value_hint", "") or "")
    if role == "password" or field.get("type") == "password":
        value = "secret" if hint and hint != "********" and hint.lower() != "unchecked" else ""
    else:
        value = hint
    return {"i": element.get("i"), "role": role, "type": field.get("type", element.get("type", "")),
            "required": bool(field.get("required")), "disabled": bool(field.get("disabled")),
            "readonly": bool(field.get("readonly")), "checked": field.get("checked"),
            "valid": field.get("valid"), "message": str(field.get("validation_message", "") or ""),
            "name": str(element.get("name", "") or "")[:60], "value": value}


def _signature_payload(observed: dict) -> dict:
    """The normalized, secret-free content a page signature is computed over: content + fields + validation.

    Incidental timers are removed from the text; field values enter only as (masked) state; the URL and title
    anchor identity. This is deliberately NOT the old url|title|count|text-length hash: two same-length pages
    with different words, or an empty field that becomes filled, produce different payloads."""
    fields = [p for p in (_field_projection(e) for e in (observed.get("elements") or [])) if p]
    alerts = [str(a)[:240] for a in (observed.get("alerts") or [])][:20]
    native = [{"name": str(n.get("name", ""))[:60], "required": bool(n.get("required")),
               "valid": bool(n.get("valid")), "message": str(n.get("message", ""))[:160]}
              for n in (observed.get("native_validation") or [])][:30]
    hints = observed.get("hints") or {}
    disabled = [{"role": d.get("role"), "name": str(d.get("name", ""))[:120]}
                for d in (observed.get("disabled_controls") or [])]
    return {"url": str(observed.get("url", "") or ""), "title": str(observed.get("title", "") or ""),
            "text": _normalize_text(str(observed.get("text", "") or "")),
            "text_len": len(_normalize_text(str(observed.get("text", "") or ""))),
            "fields": fields, "alerts": alerts, "native_validation": native,
            "invalid": bool(hints.get("invalid")), "forms": hints.get("forms"),
            "disabled_controls": disabled}


def observation_signature(observed: Any) -> str:
    """A stable hash of what the model actually sees (content + fields + validation, timer-normalized, secret-safe).

    Exported for the runtime's progress guard: a same-length text swap, a field filled, or a validation message
    appearing all change it, while an incidental clock tick or a re-read of the same page does not. Returns ''
    for a non-dict / unreadable observation so a caller can never be poisoned into thinking nothing changed."""
    if not isinstance(observed, dict):
        return ""
    try:
        payload = json.dumps(_signature_payload(observed), sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return ""
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def safe_diagnostic_projection(observed: Any) -> dict:
    """A persistent-diagnostics view: shape, field/validation state and counts, never secret field values.

    Field values are already masked to booleans/lengths by `_field_projection`; alerts and native-validation
    messages (page warnings, not user input) are kept in bounded form so a run log shows WHY an action stalled."""
    if not isinstance(observed, dict):
        return {}
    payload = _signature_payload(observed)
    return {"url": payload["url"][:120], "title": payload["title"][:120], "text_len": payload["text_len"],
            "field_count": len(payload["fields"]), "invalid": payload["invalid"],
            "alerts": [a[:240] for a in payload["alerts"]],
            "native_validation": payload["native_validation"],
            "disabled_controls": payload["disabled_controls"], "signature": observation_signature(observed)}


class SiteBrowser:
    """One named site view over a duck-typed page. The model reads observations and asks for single actions.

    Index stability: each observation marks the interactive elements it numbered with `data-atira-i` in the DOM,
    and an action addresses `[{MARK_ATTR}="<i>"]` — never a nth-of-all-nodes count — so a hidden or disabled
    node can never shift the target. An index missing from the marks (or beyond the observation's list) is a
    stale observation: the action returns INDEX_ERROR and the model re-observes.
    """

    MAX_DOWNLOAD_BYTES = 40_000_000  # same single-document cap the evidence worker accepts

    def __init__(self, page, name: str, *, budget: RuntimeBudget, nav_timeout_ms: int = 30_000):
        self.page = page
        self.name = name
        self.budget = budget
        self.nav_timeout_ms = nav_timeout_ms
        self.last_url = ""
        self._count = 0  # interactive elements the last observation numbered
        self._creds: dict = {}
        self._recovering = False
        self._last_obs: dict | None = None  # the last observation dict, for the page_changes diff

    # ---- reading -------------------------------------------------------------

    async def _probe(self) -> dict:
        try:
            raw = await self.page.evaluate(JS["probe"])
            probe = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception as exc:
            return {"error": f"page read failed: {type(exc).__name__}: {exc}"}
        if not probe.get("url"):
            probe["url"] = self.last_url
        return probe

    def _observe(self, probe: dict, *, retried: int = 0) -> dict:
        elements = probe.get("elements", [])
        self._count = probe.get("interactive_count", len(elements))
        text = compress_text(probe.get("text", ""))
        obs = {"url": probe.get("url", ""), "title": probe.get("title", ""),
               "text": text, "elements": elements,
               "alerts": probe.get("alerts", []) or [],
               "native_validation": probe.get("native_validation", []) or [],
               "disabled_controls": probe.get("disabled_elements", []) or [],
               "hints": {"popup_hint": bool(probe.get("popup_hint")), "login_hint": bool(probe.get("login_hint")),
                         "login_form": bool(probe.get("login_form")), "forms": probe.get("forms", 0),
                         "dialogs": probe.get("dialogs", 0),
                         "credential_login": bool(probe.get("credential_login")),
                         "tan_form": bool(probe.get("tan_form")),
                         "invalid": bool(probe.get("any_invalid")),
                         "alert_count": len(probe.get("alerts", []) or []),
                         "disabled_count": len(probe.get("disabled_elements", []) or []),
                         "interactive_count": self._count}}
        if retried:
            obs["retried"] = retried
        obs["signature"] = observation_signature(obs)
        if self._last_obs is not None:
            obs["page_changes"] = self._diff(self._last_obs, obs)
        self._last_obs = obs
        return obs

    @staticmethod
    def _diff(before: dict, after: dict) -> dict:
        """What actually moved between two observations: content, fields, validation, alerts. Signature is truth."""
        changed = observation_signature(before) != observation_signature(after)
        try:
            before_by_i = {e.get("i"): e for e in (before.get("elements") or [])}
            after_by_i = {e.get("i"): e for e in (after.get("elements") or [])}
        except Exception:
            before_by_i, after_by_i = {}, {}
        filled = [i for i, e in after_by_i.items()
                  if str(e.get("value_hint", "")) and not str((before_by_i.get(i) or {}).get("value_hint", ""))
                  and e.get("role") in FIELD_ROLES]
        cleared = [i for i, e in before_by_i.items()
                   if str(e.get("value_hint", "")) and not str((after_by_i.get(i) or {}).get("value_hint", ""))
                   and e.get("role") in FIELD_ROLES]
        new_validation = len(after.get("native_validation") or []) - len(before.get("native_validation") or [])
        new_alerts = len(after.get("alerts") or []) - len(before.get("alerts") or [])
        return {"changed": changed, "url": before.get("url") != after.get("url"),
                "title": before.get("title") != after.get("title"),
                "text_len": len(str(after.get("text", ""))) - len(str(before.get("text", ""))),
                "filled_indices": filled, "cleared_indices": cleared,
                "new_validation": max(0, new_validation), "resolved_validation": max(0, -new_validation),
                "new_alerts": new_alerts, "signature": after.get("signature", "")}

    async def observe(self) -> dict:
        """Re-read the current page (pages update asynchronously after actions)."""
        probe = await self._probe()
        if "error" in probe:
            return probe
        return self._observe(probe)

    async def goto(self, url: str) -> dict:
        """Navigate with retries against error/blank pages; on persistent failure return {"error"} for the model.

        An empty read is the page not having settled yet: give it a moment, RE-READ (a settled page costs no
        new navigation), and only re-navigate when the read itself failed or the page is still empty."""
        last: dict = {"error": f"navigation to {url} did not settle"}
        for attempt in range(GOTO_ATTEMPTS):
            try:
                response = await self.page.goto(url, timeout=self._step_timeout_ms(), wait_until="domcontentloaded")
            except Exception as exc:
                last = {"error": f"navigation failed: {type(exc).__name__}: {exc}"}
                await self._settle_pause(attempt)
                continue
            self.last_url = url
            if response is not None and getattr(response, "status", 0) >= 400:
                last = {"error": f"served HTTP {response.status} for {url}"}
                await self._settle_pause(attempt)
                continue
            reads = (attempt, attempt + 1) if attempt else (0, 0, 1)  # the first nav gets two reads: settle, re-read
            for read in reads:
                probe = await self._probe()
                if "error" in probe:
                    break  # a dead read cannot settle: retry the whole navigation
                if probe.get("text") or probe.get("elements"):  # a settled page: done, without another navigation
                    observed = self._observe(probe, retried=read)
                    await self._recover_login()  # a session that dropped shows its wall on landing
                    return self._last_obs  # reflect any recovery fill the numbered-set check allowed
                last = {"error": f"empty page after navigating to {url}"}
                if read < reads[-1]:
                    await self._settle_pause(read)  # not settled yet: wait and re-read, not re-navigate
        return last

    # ---- acting --------------------------------------------------------------

    async def act(self, action: str, index: int | None = None, selector: str | None = None,
                  value: str | None = None) -> dict:
        """One model-chosen action: click, fill, press, select, scroll (dy px), back, wait (<=5000 ms),
        accept_cookies. Elements are addressed by the `i` of the last observation, or by a model-given selector.
        Returns a fresh observation (or {"ok": true} for wait/accept_cookies)."""
        action = (action or "").lower()
        if action == "wait":
            try:
                ms = min(float(value or 500), 5000)
            except (TypeError, ValueError):
                return {"error": "wait needs a millisecond value"}
            await asyncio.sleep(ms / 1000)
            return {"ok": True, "action": "wait", "ms": ms}
        if action == "back":
            await self.page.go_back(timeout=self._step_timeout_ms())
            return await self.observe()
        if action == "scroll":
            try:
                dy = int(float(value or 600))
            except (TypeError, ValueError):
                return {"error": "scroll needs a pixel amount as value"}
            await self.page.mouse.wheel(0, dy)
            return await self.observe()
        if action == "accept_cookies":
            closed = await self.dismiss_popups()
            return {"ok": True, "action": "accept_cookies", "dismissed": closed}
        if selector:
            locator = self.page.locator(selector)
        elif index is not None:
            index = int(index)
            if not 0 <= index < min(self._count, INDEX_LIMIT):  # stale observation: refuse to guess a target
                return {"error": INDEX_ERROR}
            locator = self.page.locator(f'[{MARK_ATTR}="{index}"]')
        else:
            return {"error": f"act {action!r} needs an index or a selector"}
        if action == "click":
            await locator.click(timeout=self._step_timeout_ms())
        elif action == "fill":
            if value is None:
                return {"error": "fill needs a value"}
            await locator.fill(value, timeout=self._step_timeout_ms())
        elif action == "press":
            await locator.press(value or "Enter", timeout=self._step_timeout_ms())
        elif action == "select":
            await locator.select_option(value, timeout=self._step_timeout_ms())
        else:
            return {"error": f"unknown action {action!r}; use click, fill, press, select, scroll, back, wait, "
                             "accept_cookies"}
        fill_check = await self._fill_check(action, selector, index)
        await self._recover_login()  # a session that dropped returns here; the model still steers afterwards
        observed = await self.observe()
        if isinstance(observed, dict) and fill_check is not None:
            observed["fill_verification"] = fill_check
        return observed

    async def _fill_check(self, action: str, selector: str | None, index: int | None) -> dict | None:
        """After a fill, re-read the target itself: did a value actually land? Reports presence/length only.

        Never returns the typed value, so an autofill or a password can be confirmed landed without leaking it.
        A duck-typed page whose evaluate() cannot run the inspector yields None (no verification, no false claim)."""
        if action != "fill":
            return None
        if selector:
            sel = json.dumps(selector)
        elif index is not None:
            sel = json.dumps(f'[{MARK_ATTR}="{index}"]')
        else:
            return None
        inspector = ("(sel) => { const el = document.querySelector(sel); if (!el) return JSON.stringify({found:false});"
                     " const pw = el.type === 'password'; const v = el.value || '';"
                     " return JSON.stringify({found: true, type: (el.type || '').toLowerCase(),"
                     " hasValue: pw ? v.length > 0 : !!v.trim(), length: pw ? v.length : v.trim().length,"
                     " checkbox: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : null}); }")
        try:
            raw = await self.page.evaluate(f"({inspector})({sel})")
            data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception:
            return None
        if not data.get("found"):
            return None
        return {"landed": bool(data.get("hasValue")), "length": int(data.get("length") or 0),
                "type": data.get("type", ""), "checkbox": data.get("checkbox")}

    # ---- vision, downloads, credentials ---------------------------------------

    async def screenshot_b64(self) -> str:
        """PNG base64 of the viewport (for Qwen vision)."""
        data = await self.page.screenshot(type="png")
        return base64.b64encode(data).decode()

    async def download(self, index: int | None = None, selector: str | None = None, timeout_s: float = 30) -> dict:
        """Click a model-chosen target and return the download in memory: name, raw bytes, sha256, size.

        The async Playwright `Download` object offers cancel/delete/failure/path/save_as (no read()), so this
        uses the supported route: `save_as()` to a private bounded temp file, read the bytes, delete the file
        and `cancel()` the download. Nothing survives the call and the caller's own folders are never touched.
        """
        target = None
        if selector:
            target = self.page.locator(selector)
        elif index is not None:
            if not 0 <= int(index) < min(self._count, INDEX_LIMIT):
                return {"error": INDEX_ERROR}
            target = self.page.locator(f'[{MARK_ATTR}="{int(index)}"]')
        if target is None:
            return {"error": "download needs the index of a link/button or a selector"}
        async with self.page.expect_download(timeout=int(timeout_s * 1000)) as info:
            await target.click(timeout=self._step_timeout_ms())
        download = await info.value
        scratch = tempfile.TemporaryDirectory(prefix="atira-dl-")  # private and auto-removed, never the app folder
        target_path = Path(scratch.name) / "download.bin"
        try:
            await download.save_as(str(target_path))
            data = target_path.read_bytes()[: self.MAX_DOWNLOAD_BYTES]  # bounded: the model gets what fits
        except Exception as exc:
            return {"error": f"download could not be captured: {type(exc).__name__}: {exc}"}
        finally:
            scratch.cleanup()
            try:
                await download.cancel()  # leave no platform-side artifact behind
            except Exception:
                pass
        return {"name": download.suggested_filename, "bytes": data,
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def attach_credentials(self, creds: dict) -> None:
        """Per-site login supplied by the run (never stored in evidence, never emitted)."""
        self._creds = dict(creds or {})

    async def credentials(self) -> dict:
        return dict(self._creds)

    async def dismiss_popups(self) -> int:
        """Close cookie walls / modal dialogs via generic close-accept-reject affordances. The model calls this."""
        try:
            closed = await self.page.evaluate(JS["dismiss"])
        except Exception:
            return 0
        if isinstance(closed, int) and closed:
            await asyncio.sleep(0.15)
            return closed
        return 0

    async def find_receipt(self, patterns: list[str]) -> dict:
        """The current page's fields and lines matching generic patterns the model chose (e.g. receipt words).

        A read-only helper: it reports what the page shows; whether that is the receipt is the model's decision.
        """
        wanted = [str(pattern) for pattern in patterns if str(pattern).strip()][:12]
        if not wanted:
            return {"receipt_hits": []}
        try:
            raw = await self.page.evaluate(_render_js(JS["receipt"], wanted))
            data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception as exc:
            return {"error": f"receipt read failed: {type(exc).__name__}: {exc}"}
        return {"receipt_hits": data.get("receipt_hits", [])}

    # ---- internals -------------------------------------------------------------

    def _step_timeout_ms(self) -> int:
        return max(1000, min(self.nav_timeout_ms, int(self.budget.timeout(30) * 1000)))

    async def _settle_pause(self, attempt: int) -> None:
        await asyncio.sleep(0.2 * (attempt + 1))

    @staticmethod
    def _looks_like_login(probe: dict) -> bool:
        return bool(probe.get("password_fields") or probe.get("login_form")
                    or (probe.get("login_hint") and probe.get("forms")))

    async def _recover_login(self) -> bool:
        """Re-enter the run's own USERNAME on a real credential wall (first visit or a dropped session).

        Deliberately narrow, after the run_03 lesson that every rejection was not an expired TAN: this fills ONLY
        a visible, freshly-numbered NON-secret username field, and ONLY when the page is an actual credential login
        form (username + password present, no TAN signing). It NEVER touches a password field and NEVER a TAN /
        two-factor signing form — those values belong to the model's live decision, not an automatic recovery.
        It never submits. A field that is not visible on THIS read or not in the numbered set is left alone, so
        automatic recovery can never clobber an input or a TAN code the model is handling."""
        if self._recovering or not self._creds.get("username"):
            return False
        self._recovering = True
        try:
            probe = await self._settle_probe()
            if "error" in probe:
                return False
            # Only an actual credential login form is recoverable; a TAN signing form is explicitly excluded.
            if probe.get("tan_form"):
                return False
            if not probe.get("credential_login") and not probe.get("login_form"):
                return False
            self._observe(probe)  # the marks this read numbered: indices below are addressed against them
            creds = self._creds
            filled = False
            for element in probe.get("elements", []):
                if element.get("tag") != "input":
                    continue
                fstate = element.get("field") or {}
                ftype = (element.get("type") or fstate.get("type") or "").lower()
                # never a secret control: password/TAN/hidden/disabled/readonly fields are off-limits to recovery
                if ftype == "password" or fstate.get("tan") or fstate.get("disabled") or fstate.get("readonly"):
                    continue
                if fstate.get("hidden"):
                    continue
                identity = f"{element.get('name', '')} {element.get('label', '')} {element.get('type', '')}".lower()
                if not any(word in identity for word in ("user", "email", "benutzer", "login", "kennung", "email")):
                    continue
                if not 0 <= int(element.get("i", -1)) < min(self._count, INDEX_LIMIT):
                    continue  # not freshly numbered: leave it to the model rather than type into an unseen field
                try:
                    await self.page.locator(f'[{MARK_ATTR}="{element["i"]}"]').fill(
                        str(creds["username"]), timeout=self._step_timeout_ms())
                    filled = True
                    break  # one username field only
                except Exception:
                    pass
            return filled
        finally:
            self._recovering = False

    async def _settle_probe(self) -> dict:
        """A probe that has actually settled: an empty fresh read (post-navigation/post-click async render) gets
        a moment and a re-read. This fixes the pre-fix recovery that saw one transient empty read and gave up."""
        probe = await self._probe()
        if "error" in probe or (probe.get("text") or probe.get("elements")):
            return probe
        await asyncio.sleep(0.15)
        return await self._probe()
