// Semantic locators are re-resolved; no cached coordinates or backend DOM node ids.
const crypto = require('crypto');
const secrets = new Set();
const protect = text => { for (const value of secrets) if (value) text = text.split(value).join('[REDACTED]'); return text; };

function redact(value) {
  if (typeof value === 'string') return protect(value);
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, redact(v)]));
  return value;
}

async function targets(frame) {
  const entries = await frame.locator('button,input,textarea,a,select,[role="button"]').evaluateAll(elements =>
    elements.slice(0, 200).map(el => {
      const attribute = ['data-testid', 'aria-label', 'name'].find(a => el.getAttribute(a));
      const value = attribute && el.getAttribute(attribute);
      if (!value || value.length > 256) return null;
      return { tag: el.tagName.toLowerCase(), attribute, value, type: el.getAttribute('type') || '',
        href: el.getAttribute('href') || '', text: (el.innerText || '').slice(0, 256) };
    }).filter(Boolean));
  return entries.map(entry => ({ ...entry, frame_url: frame.url(), target_id: crypto.randomUUID() }));
}

function locator(frame, entry) {
  if (!['data-testid', 'aria-label', 'name'].includes(entry.attribute) || !/^[a-z]+$/.test(entry.tag)) throw new Error('target_invalid');
  const escaped = entry.value.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[\r\n]/g, '');
  return frame.locator(`${entry.tag}[${entry.attribute}="${escaped}"]`);
}

async function resolve(page, entry) {
  const frames = page.frames().filter(f => f.url() === entry.frame_url);
  if (frames.length !== 1) throw new Error('target_frame_changed');
  const match = locator(frames[0], entry);
  if (await match.count() !== 1 || !await match.isVisible() || !await match.isEnabled()) throw new Error('target_not_actionable');
  const actual = await match.evaluate(el => ({ type: el.getAttribute('type') || '', href: el.getAttribute('href') || '',
    text: (el.innerText || '').slice(0, 256) }));
  if (['type', 'href', 'text'].some(k => actual[k] !== entry[k])) throw new Error('target_changed');
  return match;
}

module.exports = { targets, resolve, secrets, redact };
