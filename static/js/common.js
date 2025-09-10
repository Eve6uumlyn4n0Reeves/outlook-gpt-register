// Common helpers shared by index.html and runs.html
// Parse accounts from text with optional extension hint (csv, txt, md, json)
// Returns an array of account objects
(function(global){
  function parseTextToAccounts(rawText, extHint){
    let text = (rawText || '').trim();
    if (!text) return [];

    // JSON array
    if (text.startsWith('[')) {
      try {
        const arr = JSON.parse(text);
        return Array.isArray(arr) ? arr : [];
      } catch { /* fallthrough */ }
    }

    // JSONL / per-line JSON
    if (text.includes('{')) {
      const lines = text.split(/\r?\n/);
      const out = [];
      for (const ln of lines) {
        const s = (ln || '').trim();
        if (!s) continue;
        if (s.startsWith('{')){ try { out.push(JSON.parse(s)); } catch { /* ignore */ } }
      }
      if (out.length) return out;
    }

    // CSV or plain text fallback
    const lines = text.split(/\r?\n/).filter(x => (x || '').trim());
    if (!lines.length) return [];

    const first = lines[0];
    const hasHeader = /(^|,|\t|\s)(email)(,|\t|\s|$)/i.test(first);
    const rows = hasHeader ? lines.slice(1) : lines;
    const out = [];

    for (const row of rows) {
      const parts = row.split(/[\,\t\s]+/).map(s => s.trim()).filter(Boolean);
      if (!parts.length) continue;
      if (hasHeader){
        const hdrs = first.split(/[\,\t]+/).map(h => h.trim().toLowerCase());
        const obj = {};
        for (let i = 0; i < Math.min(hdrs.length, parts.length); i++) obj[hdrs[i]] = parts[i];
        if (obj.email) out.push(obj);
      } else {
        const obj = { email: parts[0] };
        if (parts[1]) obj.password = parts[1];
        if (parts[2]) obj.display_name = parts[2];
        if (parts[3]) obj.dob = parts[3];
        if (parts[4]) obj.proxy = parts[4];
        if (parts[5]) obj.user_agent = parts[5];
        out.push(obj);
      }
    }
    return out;
  }
  global.parseTextToAccounts = parseTextToAccounts;
})(window);

