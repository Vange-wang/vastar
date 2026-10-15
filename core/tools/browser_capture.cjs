// No page evaluation supplied by callers. All script here is trusted adapter code.
const crypto = require('crypto');
const { targets, secrets, redact } = require('./browser_targets.cjs');

const pixels = page => page.screenshot({ type: 'png', animations: 'disabled', timeout: 5000,
  mask: page.frames().map(frame => frame.locator('input,textarea,[contenteditable],iframe[srcdoc]')) });

async function version(page) {
  if (page.isClosed()) throw new Error('tab_closed');
  const cdp = await page.context().newCDPSession(page);
  try {
    const dom = await cdp.send('DOMSnapshot.captureSnapshot', { computedStyles: [], includeDOMRects: true });
    const png = await pixels(page);
    return crypto.createHash('sha256').update(JSON.stringify(dom)).update(png).digest('hex');
  } finally { await cdp.detach(); }
}

async function sanitize(page) {
  for (const frame of page.frames()) {
    await frame.evaluate(values => {
      const clean = text => text
        .replace(/\b(?:Bearer\s+\S+|sk-[\w-]{8,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b/gi, '[REDACTED]')
        .replace(/\b(cookie|token|password|passwd|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s<]+/gi, '$1=[REDACTED]');
      const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
      for (let node; (node = walker.nextNode());) {
        if (['SCRIPT', 'STYLE'].includes(node.parentElement?.tagName)) continue;
        let text = clean(node.nodeValue);
        for (const value of values) if (value) text = text.split(value).join('[REDACTED]');
        if (text !== node.nodeValue) node.nodeValue = text;
      }
    }, [...secrets]);
  }
}

async function capture(page) {
  await sanitize(page);
  // Flush adapter masking mutations before measuring the snapshot generation.
  await page.evaluate(() => Promise.resolve());
  const revision = await version(page);
  const frames = [];
  for (const frame of page.frames()) {
    const data = await frame.evaluate(() => ({
      text: (document.body?.innerText || '').slice(0, 24000),
      elements: [...document.querySelectorAll('a,button,h1,h2,h3,[role]')].slice(0, 200).map(el => ({
        tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
        text: (el.innerText || '').slice(0, 256)
      })), title: document.title.slice(0, 256)
    }));
    data.elements.push(...await targets(frame));
    frames.push(redact(data));
  }
  if (frames.length > 16) throw new Error('frame_limit');
  const png = await pixels(page);
  if (revision !== await version(page)) throw new Error('page_changed_during_capture');
  if (png.length > 4 * 1024 * 1024) throw new Error('screenshot_too_large');
  return { page_revision: revision, frames, screenshot: png.toString('base64'),
    screenshot_type: 'image/png', partial: true,
    limitations: ['viewport only', 'bounded visible text and elements', 'editable fields and recognized secrets masked'] };
}

module.exports = { version, capture };
