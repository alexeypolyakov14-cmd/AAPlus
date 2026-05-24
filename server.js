const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');
const ExcelJS = require('exceljs');

const PORT = 3000;
const MPSTATS_TOKEN = process.env.MPSTATS_TOKEN || '';
const OPENROUTER_KEY = process.env.OPENROUTER_KEY || '';
const indexHTML = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf-8');

// --- Global error handlers ---
process.on('uncaughtException', (err) => { console.error('Uncaught exception:', err.message); });
process.on('unhandledRejection', (err) => { console.error('Unhandled rejection:', err); });

// --- Banki.ru cache (5 min TTL, max 200 entries) ---
const bankiCache = new Map();
const BANKI_CACHE_MAX = 200;
const BANKI_CACHE_TTL = 5 * 60 * 1000;
function bankiCacheKey(city, currency) { return `${city}:${currency}`; }
function getBankiCache(city, currency) {
  const key = bankiCacheKey(city, currency);
  const entry = bankiCache.get(key);
  if (entry && Date.now() - entry.ts < BANKI_CACHE_TTL) return entry.data;
  return null;
}
function setBankiCache(city, currency, data) {
  if (bankiCache.size >= BANKI_CACHE_MAX) {
    const oldest = bankiCache.keys().next().value;
    bankiCache.delete(oldest);
  }
  bankiCache.set(bankiCacheKey(city, currency), { data, ts: Date.now() });
}

// --- Smart-lab cache (4h TTL) ---
const smartlabCache = new Map();
const SMARTLAB_CACHE_TTL = 4 * 60 * 60 * 1000;

// --- Real-time multiplier cache (60s TTL) ---
const realtimeCache = new Map();
const REALTIME_CACHE_TTL = 60 * 1000;

// --- News cache (15 min TTL) ---
const newsCache = new Map();
const NEWS_CACHE_TTL = 15 * 60 * 1000;

// --- Consensus cache (4h TTL) ---
const consensusCache = new Map();
const CONSENSUS_CACHE_TTL = 4 * 60 * 60 * 1000;

// --- Puppeteer browser management (lazy start, auto-close after 5 min idle) ---
let puppeteerBrowser = null;
let puppeteerLastUsed = 0;
const BROWSER_IDLE_TIMEOUT = 5 * 60 * 1000;
let puppeteerAvailable = null; // null = not checked, true/false

async function checkPuppeteer() {
  if (puppeteerAvailable !== null) return puppeteerAvailable;
  try {
    require.resolve('puppeteer-core');
    puppeteerAvailable = true;
  } catch(e) {
    puppeteerAvailable = false;
    console.log('puppeteer-core not installed — consensus endpoint disabled');
  }
  return puppeteerAvailable;
}

async function getLazyBrowser() {
  if (!(await checkPuppeteer())) return null;
  if (puppeteerBrowser && puppeteerBrowser.isConnected()) {
    puppeteerLastUsed = Date.now();
    return puppeteerBrowser;
  }
  try {
    const puppeteer = require('puppeteer-core');
    // Try common Chromium paths
    const chromePaths = [
      '/usr/bin/chromium-browser',
      '/usr/bin/chromium',
      '/usr/bin/google-chrome',
      '/usr/bin/google-chrome-stable',
      '/snap/bin/chromium'
    ];
    let execPath = null;
    for (const p of chromePaths) {
      try { if (require('fs').existsSync(p)) { execPath = p; break; } } catch(e) {}
    }
    if (!execPath) { console.error('No Chromium found'); return null; }
    puppeteerBrowser = await puppeteer.launch({
      executablePath: execPath,
      headless: 'new',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
             '--disable-gpu', '--disable-software-rasterizer', '--single-process',
             '--no-zygote', '--disable-extensions']
    });
    puppeteerLastUsed = Date.now();
    console.log('Puppeteer browser launched');
    return puppeteerBrowser;
  } catch(e) {
    console.error('Puppeteer launch error:', e.message);
    puppeteerAvailable = false;
    return null;
  }
}

// Auto-close browser after idle
setInterval(() => {
  if (puppeteerBrowser && Date.now() - puppeteerLastUsed > BROWSER_IDLE_TIMEOUT) {
    puppeteerBrowser.close().catch(() => {});
    puppeteerBrowser = null;
    console.log('Puppeteer browser closed (idle)');
  }
}, 60000);

// Ticker -> Conomy slug mapping
const CONOMY_SLUGS = {
  'SBER':'sberbank','SBERP':'sberbank','GAZP':'gazprom','LKOH':'lukoil',
  'ROSN':'rosneft','NVTK':'novatek','GMKN':'nornikel','TATN':'tatneft',
  'TATNP':'tatneft','SNGS':'surgutneftegaz','SNGSP':'surgutneftegaz',
  'VTBR':'vtb','TCSG':'tinkoff','BSPB':'bspb','CBOM':'mkb',
  'MOEX':'moex','YDEX':'yandex','OZON':'ozon','VKCO':'vk',
  'MTSS':'mts','MGNT':'magnit','X5':'x5-retail-group','FIVE':'x5-retail-group',
  'FIXR':'fix-price','PIKK':'pik','SMLT':'samolet','CHMF':'severstal',
  'NLMK':'nlmk','MAGN':'mmk','ALRS':'alrosa','PLZL':'polyus',
  'POLY':'polymetal','RUAL':'rusal','ENPG':'en-plus','AFLT':'aeroflot',
  'FLOT':'sovcomflot','FEES':'rosseti','RTKM':'rostelekom','TRNFP':'transneft',
  'SIBN':'gazpromneft','PHOR':'phosagro','AKRN':'akron',
  'HEAD':'headhunter','POSI':'group-pozitiv','DIAS':'diasoft',
  'LENT':'lenta','GCHE':'cherkizovo','AQUA':'russkaya-akvakultura',
  'RENI':'renaissance-insurance','SFIN':'sfi',
  'TRMK':'tmk','RASP':'raspadskaya','BANEP':'bashneft',
  'IRAO':'inter-rao','HYDR':'rushydro','UPRO':'unipro',
  'MSNG':'mosenergo','OGKB':'ogk-2','TGKA':'tgk-1'
};

async function fetchConsensusFromConomy(ticker) {
  const browser = await getLazyBrowser();
  if (!browser) return { error: 'puppeteer not available' };

  const slug = CONOMY_SLUGS[ticker.toUpperCase()];
  if (!slug) return { error: 'unknown ticker for Conomy: ' + ticker };

  const tickerLower = ticker.toLowerCase();
  const url = `https://conomy.ru/investments/issuers/${slug}`;
  let page;
  try {
    page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36');
    await page.setViewport({ width: 1280, height: 900 });

    // Navigate and wait for content
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 20000 });

    // Click "Оценка" tab if it exists
    try {
      await page.evaluate(() => {
        const tabs = document.querySelectorAll('a, button, [role="tab"], li');
        for (const tab of tabs) {
          const text = tab.textContent.trim().toLowerCase();
          if (text === 'оценка' || text.includes('оценка')) {
            tab.click();
            return true;
          }
        }
        return false;
      });
      // Wait for tab content to load
      await page.waitForTimeout(3000);
    } catch(e) {
      console.warn('Could not click Оценка tab:', e.message);
    }

    // Extract all available data from the page
    const data = await page.evaluate((tk) => {
      const result = {
        ticker: tk.toUpperCase(),
        company_name: '',
        target_price: null,
        potential: null,
        recommendation: null,
        current_price: null,
        valuation_metrics: {},
        analysts: [],
        raw_texts: []
      };

      // Company name
      const h1 = document.querySelector('h1');
      if (h1) result.company_name = h1.textContent.trim();

      // Get all text content from the page for extraction
      const bodyText = document.body.innerText;

      // Try to find target price patterns
      const targetPatterns = [
        /(?:целев[а-я]*\s*цен[а-я]*|target\s*price|таргет)[:\s]*([0-9.,]+)/i,
        /(?:справедлив[а-я]*\s*(?:стоимост|цен)[а-я]*)[:\s]*([0-9.,]+)/i,
        /(?:потенциал|upside)[:\s]*([+-]?[0-9.,]+)\s*%/i
      ];
      for (const p of targetPatterns) {
        const m = bodyText.match(p);
        if (m) result.raw_texts.push(m[0]);
      }

      // Look for structured data in tables
      const tables = document.querySelectorAll('table');
      for (const table of tables) {
        const rows = table.querySelectorAll('tr');
        for (const row of rows) {
          const cells = row.querySelectorAll('td, th');
          if (cells.length >= 2) {
            const label = cells[0].textContent.trim().toLowerCase();
            const value = cells[1].textContent.trim();
            if (label.includes('целев') || label.includes('target') || label.includes('таргет')) {
              result.target_price = parseFloat(value.replace(/[^\d.,]/g, '').replace(',', '.')) || null;
            }
            if (label.includes('потенциал') || label.includes('upside')) {
              result.potential = value;
            }
            if (label.includes('рекоменд') || label.includes('consensus') || label.includes('консенсус')) {
              result.recommendation = value;
            }
            if (label.includes('p/e') || label.includes('p/b') || label.includes('roe') || label.includes('p/s')) {
              result.valuation_metrics[label] = value;
            }
          }
        }
      }

      // Look for card-like elements with potential/target
      const allElements = document.querySelectorAll('[class*="potential"], [class*="target"], [class*="valuation"], [class*="estimate"], [class*="rating"], [class*="score"]');
      for (const el of allElements) {
        const text = el.textContent.trim();
        if (text.length > 3 && text.length < 200) {
          result.raw_texts.push(text);
        }
      }

      // Look for any percentage that could be potential
      const potentialEl = document.querySelector('[class*="potential"], [class*="upside"]');
      if (potentialEl) {
        result.potential = potentialEl.textContent.trim();
      }

      // Current price
      const priceEl = document.querySelector('[class*="price"], [class*="last"], [class*="quote"]');
      if (priceEl) {
        const priceText = priceEl.textContent.trim();
        const priceMatch = priceText.match(/([0-9][0-9.,]*)/);
        if (priceMatch) result.current_price = parseFloat(priceMatch[1].replace(',', '.'));
      }

      return result;
    }, tickerLower);

    puppeteerLastUsed = Date.now();
    return data;
  } catch(e) {
    console.error('Conomy fetch error:', e.message);
    return { error: e.message };
  } finally {
    if (page) {
      try { await page.close(); } catch(e) {}
    }
  }
}

function fetchSmartlabPage(pageUrl, cb) {
  const u = new URL(pageUrl);
  const opts = {
    hostname: u.hostname, path: u.pathname + u.search, method: 'GET',
    headers: {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      'Accept': 'text/html,application/xhtml+xml',
      'Accept-Language': 'ru-RU,ru;q=0.9',
      'Accept-Encoding': 'identity'
    }
  };
  https.get(opts, (resp) => {
    let data = '';
    resp.on('data', c => { data += c; });
    resp.on('end', () => cb(null, data));
  }).on('error', e => cb(e));
}

function parseSmartlabFundamentals(html) {
  const companies = [];
  const parseNum = (s) => {
    if (!s || s === '–' || s === '-' || s === '' || s === 'н/д') return null;
    const n = parseFloat(s.replace(/\s/g, '').replace(',', '.').replace('%', ''));
    return isNaN(n) ? null : n;
  };

  // Step 1: Extract ALL simple-little-table tables (page has separate tables for stocks vs banks)
  const tableRegex = /<table[^>]*class="[^"]*simple-little-table[^"]*"[^>]*>[\s\S]*?<\/table>/gi;
  const tables = html.match(tableRegex) || [];
  if (!tables.length) {
    console.warn('[SmartLab parser] No simple-little-table found');
    return companies;
  }
  console.warn('[SmartLab parser] Found', tables.length, 'table(s)');

  // Step 2: Parse each table independently (each has its own headers)
  for (let tIdx = 0; tIdx < tables.length; tIdx++) {
    const tableHtml = tables[tIdx];

    // Parse headers for THIS table
    const thRegex = /<th[^>]*>([\s\S]*?)<\/th>/gi;
    const headers = [];
    let thMatch;
    while ((thMatch = thRegex.exec(tableHtml)) !== null) {
      const txt = thMatch[1].replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
      headers.push(txt);
    }

    // Build column map for this table
    const colMap = {};
    for (let i = 0; i < headers.length; i++) {
      const h = headers[i];
      if (!h) continue;
      if (/тикер/i.test(h)) colMap.ticker = i;
      else if (/назван/i.test(h)) colMap.name = i;
      else if (/капит/i.test(h)) colMap.market_cap = i;
      else if (/ev\s*\/\s*ebitda/i.test(h)) colMap.ev_ebitda = i;
      else if (/^ev\b/i.test(h)) colMap.ev = i;
      else if (/выруч/i.test(h)) colMap.revenue = i;
      else if (/чист.*опер/i.test(h)) colMap.net_op_income = i;
      else if (/чист.*приб/i.test(h)) colMap.net_income = i;
      else if (/дд\s*ао/i.test(h)) colMap.div_yield_ao = i;
      else if (/дд\s*ап/i.test(h)) colMap.div_yield_ap = i;
      else if (/дд\s*\/\s*чп/i.test(h) || /payout/i.test(h)) colMap.div_payout = i;
      else if (/p\s*\/\s*e\b/i.test(h)) colMap.pe = i;
      else if (/p\s*\/\s*s\b/i.test(h)) colMap.ps = i;
      else if (/p\s*\/\s*b/i.test(h)) colMap.pb = i;
      else if (/рентаб.*ebitda/i.test(h) || /ebitda.*margin/i.test(h)) colMap.ebitda_margin = i;
      else if (/долг\s*\/\s*ebitda/i.test(h) || /debt.*ebitda/i.test(h)) colMap.debt_ebitda = i;
      else if (/roe/i.test(h)) colMap.roe = i;
      else if (/roa/i.test(h)) colMap.roa = i;
      else if (/чпм/i.test(h) || /nim/i.test(h)) colMap.nim = i;
      else if (/отчет/i.test(h) || /report/i.test(h)) colMap.report = i;
    }

    if (colMap.ticker === undefined || colMap.name === undefined) {
      console.warn('[SmartLab parser] Table', tIdx + 1, '- skipping: no ticker/name headers found');
      continue;
    }

    // Debug removed: Table headers logged in dev only

    // Parse data rows
    const rowRegex = /<tr[^>]*>[\s\S]*?<\/tr>/gi;
    const rows = tableHtml.match(rowRegex) || [];
    let rowCount = 0;
    for (const row of rows) {
      if (row.includes('<th')) continue;
      const cellRegex = /<td[^>]*>([\s\S]*?)<\/td>/gi;
      const cells = [];
      let m;
      while ((m = cellRegex.exec(row)) !== null) {
        const text = m[1].replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').trim();
        cells.push(text);
      }
      if (cells.length < 5) continue;

      const ticker = cells[colMap.ticker] || '';
      if (!ticker || !/^[A-Z0-9]{1,6}$/i.test(ticker)) continue;

      const g = (key) => colMap[key] !== undefined && colMap[key] < cells.length ? parseNum(cells[colMap[key]]) : null;
      const gs = (key) => colMap[key] !== undefined && colMap[key] < cells.length ? (cells[colMap[key]] || '').trim() : '';

      companies.push({
        name: cells[colMap.name] || '',
        ticker: ticker.toUpperCase(),
        market_cap: g('market_cap'),
        ev: g('ev'),
        revenue: g('revenue') || g('net_op_income'),
        net_income: g('net_income'),
        div_yield_ao: g('div_yield_ao'),
        div_yield_ap: g('div_yield_ap'),
        div_payout: g('div_payout'),
        pe: g('pe'),
        ps: g('ps'),
        pb: g('pb'),
        ev_ebitda: g('ev_ebitda'),
        ebitda_margin: g('ebitda_margin'),
        debt_ebitda: g('debt_ebitda'),
        roe: g('roe'),
        roa: g('roa'),
        nim: g('nim'),
        report: gs('report')
      });
      rowCount++;
    }
    console.warn('[SmartLab parser] Table', tIdx + 1, '- Parsed', rowCount, 'companies');
  }

  console.warn('[SmartLab parser] Total:', companies.length, 'companies');
  return companies;
}

function parseSmartlabCompany(html, ticker) {
  // Parse individual company page - extract table rows
  const result = { ticker, name: '', sector: '', metrics: {}, years: [] };
  // Extract company name from title
  const titleMatch = html.match(/<title>([^<]+)/);
  if (titleMatch) {
    const t = titleMatch[1];
    const nm = t.match(/^(.+?)\s*\(/);
    if (nm) result.name = nm[1].trim();
  }
  // Extract sector link
  const sectorMatch = html.match(/Aнализ сектора\s+([^<"]+)/i) || html.match(/анализ сектора\s+([^<"]+)/i);
  if (sectorMatch) result.sector = sectorMatch[1].trim();
  // Parse the financial table
  // Find year headers
  const yearRegex = /<th[^>]*>[\s\S]*?(\d{4})[\s\S]*?<\/th>/g;
  let ym;
  while ((ym = yearRegex.exec(html)) !== null) {
    const y = parseInt(ym[1]);
    if (y >= 2015 && y <= 2030 && !result.years.includes(y)) result.years.push(y);
  }
  result.years.sort((a, b) => a - b);
  // Parse metric rows
  // Labels use plain text matching (no regex syntax in labels)
  // For labels ending with '<', we match word boundary to avoid false matches
  const metricPatterns = [
    { key: 'revenue', label: 'Выручка' },
    { key: 'net_income', label: 'Чистая прибыль' },
    { key: 'ebitda', label: 'EBITDA' },
    { key: 'operating_income', label: 'Операционная прибыль' },
    { key: 'div_yield_ao', label: 'Див доход, ао' },
    { key: 'dividend', label: 'Дивиденд,' },
    { key: 'roe', label: 'ROE' },
    { key: 'roa', label: 'ROA' },
    { key: 'pe', label: 'P/E' },
    { key: 'pb', label: 'P/B' },
    { key: 'ps', label: 'P/S' },
    { key: 'ev_ebitda', label: 'EV/EBITDA' },
    { key: 'market_cap', label: 'Капитализация' },
    { key: 'ev', label: 'EV', exact: true },
    { key: 'debt', label: 'Долг', noPrefix: 'Чистый' },
    { key: 'net_debt', label: 'Чистый долг' },
    { key: 'ebitda_margin', label: 'Рентаб EBITDA' },
    { key: 'net_margin', label: 'Чистая рентаб' },
    { key: 'assets', label: 'Активы', noPrefix: 'банк' },
    { key: 'equity', label: 'Собственный капитал' },
    { key: 'fcf', label: 'FCF' },
    { key: 'eps', label: 'EPS' },
    { key: 'bv_per_share', label: 'BV/акцию' },
    { key: 'capex', label: 'CAPEX' },
    { key: 'amortization', label: 'Амортизация' },
    { key: 'ocf', label: 'Опер.денежный поток' },
    { key: 'debt_ebitda', label: 'Долг/EBITDA' },
    { key: 'div_payout', label: 'Дивиденды/прибыль' },
    // Bank-specific
    { key: 'net_interest_income', label: 'Чист. проц. доходы' },
    { key: 'net_commission_income', label: 'Чист. комисс. доход' },
    { key: 'bank_assets', label: 'Активы банка' },
    { key: 'capital_bank', label: 'Капитал банка' },
    { key: 'loan_portfolio', label: 'Кредитный портфель' },
    { key: 'deposits', label: 'Депозиты' },
    { key: 'net_interest_margin', label: 'Чистая процентная маржа' },
    { key: 'cost_of_risk', label: 'Стоимость риска' },
    { key: 'operating_expenses', label: 'Опер. расходы' }
  ];

  // Helper: extract text from HTML (strip tags)
  const stripHtml = (s) => s.replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();

  // For each row in the main table, try to match metric names
  const rowRegex = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
  const allRows = html.match(rowRegex) || [];
  for (const row of allRows) {
    // Extract row text once for matching
    const rowText = stripHtml(row);
    for (const mp of metricPatterns) {
      // Check if this row's text content contains the metric label
      const labelIdx = rowText.toLowerCase().indexOf(mp.label.toLowerCase());
      if (labelIdx === -1) continue;
      // For 'exact' match (e.g. "EV" must not match "EV/EBITDA")
      if (mp.exact) {
        const after = rowText[labelIdx + mp.label.length] || '';
        if (/[A-Za-zА-Яа-я0-9\/]/.test(after)) continue;
      }
      // For 'noPrefix' - skip if preceded by the excluded word
      if (mp.noPrefix) {
        const before = rowText.substring(Math.max(0, labelIdx - 20), labelIdx).toLowerCase();
        if (before.includes(mp.noPrefix.toLowerCase())) continue;
      }
      // Already have this metric? skip (prevents duplicate matches)
      if (result.metrics[mp.key]) continue;

      // Extract ALL cells (both <th> and <td>) to handle label in either tag
      const allCellRegex = /<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi;
      const allCells = [];
      let cm;
      while ((cm = allCellRegex.exec(row)) !== null) {
        allCells.push({ html: cm[1], text: stripHtml(cm[1]) });
      }
      if (allCells.length < 2) continue;

      // Find which cell contains the label, skip it, take the rest as values
      let labelCellIdx = 0;
      for (let ci = 0; ci < allCells.length; ci++) {
        if (allCells[ci].text.toLowerCase().includes(mp.label.toLowerCase())) {
          labelCellIdx = ci;
          break;
        }
      }

      const numVals = allCells.slice(labelCellIdx + 1).map(c => {
        const s = c.text;
        if (!s || s === '–' || s === '-' || s === '') return null;
        const n = parseFloat(s.replace(/\s/g, '').replace(',', '.').replace('%', ''));
        return isNaN(n) ? null : n;
      });

      // Validate: values count should roughly match years count
      if (numVals.length > 0) {
        result.metrics[mp.key] = numVals;
      }
      break;
    }
  }
  return result;
}

function parseSmartlabNews(html, ticker) {
  const news = [];
  // Smart-lab news pages have topic entries with titles, dates, links
  // Pattern: <a href="/blog/XXXXX.php">Title</a> ... date
  const topicRegex = /<div[^>]*class="[^"]*topic[^"]*"[^>]*>([\s\S]*?)<\/div>\s*<\/div>/gi;
  const items = html.match(topicRegex) || [];

  // Fallback: try to extract from simpler link patterns
  const linkRegex = /<a[^>]+href="(\/blog\/\d+\.php)"[^>]*>([^<]+)<\/a>/gi;
  let lm;
  const seen = new Set();
  while ((lm = linkRegex.exec(html)) !== null) {
    const url = lm[1];
    const title = lm[2].replace(/&[^;]+;/g, ' ').trim();
    if (seen.has(url) || !title || title.length < 10) continue;
    seen.add(url);
    // Try to find a date near this link
    const dateRegex = /(\d{1,2}[.:]\d{2}(?:\s+\d{1,2}\.\d{2}\.\d{4})?|\d{1,2}\.\d{2}\.\d{4}|\d{2}\/\d{2}\s+\d{2}:\d{2})/;
    const nearbyHtml = html.substring(Math.max(0, lm.index - 200), lm.index + lm[0].length + 200);
    const dm = nearbyHtml.match(dateRegex);
    news.push({
      title,
      url: 'https://smart-lab.ru' + url,
      date: dm ? dm[1] : '',
      source: 'Smart-lab'
    });
    if (news.length >= 15) break;
  }

  // Also try the pattern with time stamps like "15:58", "18/05"
  if (news.length === 0) {
    const altRegex = /(\d{1,2}[:/]\d{2}(?:\s+\d{1,2}\.\d{2})?)[\s\S]*?<a[^>]+href="([^"]+)"[^>]*>([^<]{10,})<\/a>/gi;
    let am;
    while ((am = altRegex.exec(html)) !== null) {
      const title = am[3].replace(/&[^;]+;/g, ' ').trim();
      const href = am[2];
      if (seen.has(href) || !title) continue;
      seen.add(href);
      news.push({
        title,
        url: href.startsWith('http') ? href : 'https://smart-lab.ru' + href,
        date: am[1],
        source: 'Smart-lab'
      });
      if (news.length >= 15) break;
    }
  }
  return news;
}

// --- Helpers ---

function proxyRequest(options, payload, res) {
  const req = https.request(options, (proxyRes) => {
    let data = '';
    proxyRes.on('data', chunk => { data += chunk; });
    proxyRes.on('end', () => {
      if (res.headersSent) return;
      res.writeHead(proxyRes.statusCode, {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*'
      });
      res.end(data);
    });
  });
  req.on('error', (e) => {
    if (res.headersSent) return;
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: e.message }));
  });
  req.setTimeout(20000, () => {
    req.destroy();
    if (res.headersSent) return;
    res.writeHead(504, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'timeout' }));
  });
  if (payload) req.write(payload);
  req.end();
}

function readBody(req, limit) {
  limit = limit || 1048576; // 1MB default
  return new Promise((resolve, reject) => {
    let body = '';
    let size = 0;
    req.on('data', chunk => {
      size += chunk.length;
      if (size > limit) { req.destroy(); reject(new Error('body too large')); return; }
      body += chunk;
    });
    req.on('end', () => resolve(body));
    req.on('error', e => reject(e));
  });
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function daysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function jsonResp(res, code, data) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify(data));
}

// --- Banki.ru HTML parser ---

function parseBankiHtml(html, city, currLabel, res) {
  try {
    const rates = [];

    // The data is embedded in an HTML-entity-encoded JSON inside a module-options attribute
    // Pattern: "name":"БАНКNAME","code":"bankcode",...,"exchange":{"buy":72.5,"sale":73.1,...}
    // HTML entities: &quot; = "

    // First decode &quot; to "
    const decoded = html.replace(/&quot;/g, '"').replace(/&amp;/g, '&');

    // Find resultList with bank entries
    const listMatch = decoded.match(/"resultList"\s*:\s*\{"list"\s*:\s*\[([\s\S]*?)\]\s*,\s*"(?:total|count|pagination)/);
    if (listMatch) {
      try {
        const listJson = JSON.parse('[' + listMatch[1] + ']');
        for (const item of listJson) {
          if (item.name && item.exchange && typeof item.exchange.buy === 'number') {
            rates.push({
              bank: item.name,
              code: item.code || '',
              buy: item.exchange.buy,
              sell: item.exchange.sale || item.exchange.sell,
              updated: item.exchange.refreshDate || null,
              newBills: item.isNewBanknotes === true
            });
          }
        }
      } catch (e) {
        // JSON parse failed on full array — try individual items
      }
    }

    // Fallback: extract individual bank entries with regex
    if (rates.length === 0) {
      const bankPattern = /"name"\s*:\s*"([^"]{2,50})"\s*,\s*"code"\s*:\s*"[^"]+"\s*,\s*"logo"[\s\S]*?"exchange"\s*:\s*\{[^}]*"buy"\s*:\s*([\d.]+)\s*,\s*"sale"\s*:\s*([\d.]+)/g;
      let m;
      while ((m = bankPattern.exec(decoded)) !== null) {
        const buy = parseFloat(m[2]);
        const sell = parseFloat(m[3]);
        if (buy > 0 && sell > 0) {
          rates.push({ bank: m[1], buy, sell });
        }
      }
    }

    // Deduplicate
    const seen = new Set();
    const unique = rates.filter(r => {
      if (seen.has(r.bank)) return false;
      seen.add(r.bank);
      return true;
    });

    const result = unique.slice(0, 15);

    const responseData = {
      rates: result,
      city: city,
      currency: currLabel,
      count: result.length
    };
    // Cache successful results
    if (result.length > 0) {
      setBankiCache(city, currLabel.toLowerCase(), responseData);
    }
    jsonResp(res, 200, responseData);
  } catch (e) {
    jsonResp(res, 500, { error: 'parse error: ' + e.message });
  }
}

// --- DCF Excel Generator ---

async function generateDCFExcel(dcf, company, ticker) {
  const wb = new ExcelJS.Workbook();
  wb.creator = 'AA+ (aaplus.pro)';
  wb.created = new Date();
  wb.calcProperties = { fullCalcOnLoad: true };

  const C = {
    navy:'1B2A4A',darkBlue:'2D4A7A',accent:'4472C4',
    lightBlue:'D6E4F0',paleBlue:'E9F0F8',
    green:'375623',greenBg:'E2EFDA',red:'C00000',redBg:'F4CCCC',
    orange:'ED7D31',orangeBg:'FCE4D6',yellowBg:'FFF2CC',
    white:'FFFFFF',offWhite:'FAFAFA',lightGray:'F2F2F2',
    midGray:'D9D9D9',darkGray:'404040',
    inputBg:'FFF8E1',inputFont:'1A237E',black:'000000'
  };
  const bThin={style:'thin',color:{argb:'BFBFBF'}};
  const bMed={style:'medium',color:{argb:C.navy}};
  const bAll={top:bThin,left:bThin,bottom:bThin,right:bThin};
  const bHeavy={top:bMed,left:bMed,bottom:bMed,right:bMed};
  const bNone={};

  const fi=c=>({type:'pattern',pattern:'solid',fgColor:{argb:c}});
  const ft=(sz,bold,color)=>({name:'Calibri',size:sz,bold:!!bold,color:{argb:color||C.black}});
  const colL=n=>{let s='';while(n>0){n--;s=String.fromCharCode(65+n%26)+s;n=Math.floor(n/26)}return s};

  function sc(ws,r,c,val,o={}){
    const cell=ws.getCell(r,c);cell.value=val;
    if(o.font)cell.font=o.font;if(o.fill)cell.fill=o.fill;
    if(o.fmt)cell.numFmt=o.fmt;if(o.align)cell.alignment=o.align;
    cell.border=o.border===false?bNone:(o.border||bAll);return cell;
  }
  function secRow(ws,r,c1,c2,t){
    ws.mergeCells(r,c1,r,c2);
    sc(ws,r,c1,t,{font:ft(10.5,true,'FFFFFF'),fill:fi(C.navy),align:{horizontal:'left',vertical:'middle',indent:1},border:bHeavy});
    ws.getRow(r).height=21;
  }
  function subH(ws,r,c1,c2,t){
    ws.mergeCells(r,c1,r,c2);
    sc(ws,r,c1,t,{font:ft(9.5,true,C.navy),fill:fi(C.lightBlue),align:{horizontal:'left',vertical:'middle',indent:1}});
    ws.getRow(r).height=19;
  }
  function lbl(ws,r,c,t,indent){
    sc(ws,r,c,t,{font:ft(9.5,false,C.darkGray),fill:fi(C.offWhite),align:{horizontal:'left',vertical:'middle',indent:indent||1,wrapText:true}});
  }
  function inp(ws,r,c,v,fmt){
    sc(ws,r,c,v,{font:ft(9.5,false,C.inputFont),fill:fi(C.inputBg),fmt:fmt||'0.0%',align:{horizontal:'right',vertical:'middle'}});
  }
  function fml(ws,r,c,formula,result,fmt,o={}){
    const cell=ws.getCell(r,c);
    cell.value={formula,result:result??0};
    cell.font=ft(o.sz||9.5,!!o.bold,o.color||C.darkGray);
    cell.fill=fi(o.bg||C.white);cell.numFmt=fmt||'#,##0.0';
    cell.alignment={horizontal:o.ha||'right',vertical:'middle'};
    cell.border=o.border||bAll;return cell;
  }
  function ref(ws,r,c,v,fmt,o={}){
    sc(ws,r,c,v,{font:ft(o.sz||9.5,!!o.bold,o.color||C.darkBlue),fill:fi(C.lightBlue),fmt:fmt||'#,##0.0',align:{horizontal:'right',vertical:'middle'}});
  }
  function val(ws,r,c,v,fmt,o={}){
    sc(ws,r,c,v,{font:ft(o.sz||9.5,!!o.bold,o.color||C.black),fill:fi(o.bg||C.white),fmt:fmt||'#,##0.0',align:{horizontal:'right',vertical:'middle'},border:o.border||bAll});
  }

  // ==================== WORKSHEET SETUP ====================
  const ws = wb.addWorksheet('DCF Model', {
    properties:{tabColor:{argb:C.accent}},
    pageSetup:{paperSize:9,orientation:'landscape',fitToPage:true,fitToWidth:1,fitToHeight:0}
  });
  ws.columns=[{width:1.5},{width:36},{width:15},{width:15},{width:15},{width:15},{width:15},{width:15},{width:15},{width:15},{width:1.5}];

  const m=company.metrics||{};const years=company.years||[];const lastIdx=years.length-1;
  const companyName=company.name||ticker;const dateStr=new Date().toLocaleDateString('ru-RU');
  const nProj=dcf.projection_years||5;

  // Pre-compute input values
  const waccPct=dcf.wacc/100, taxPct=dcf.tax_rate/100, rfPct=dcf.risk_free/100;
  const erpPct=dcf.cost_of_equity?(dcf.cost_of_equity/100-rfPct)/(dcf.beta||1):0.05;
  const betaVal=dcf.beta||1.0, kePct=dcf.cost_of_equity/100, kdPct=rfPct+0.02;
  const growPct=dcf.growth_rate/100, tgPct=dcf.terminal_growth/100;
  const ebitda=m.ebitda?(m.ebitda[lastIdx]||m.ebitda[lastIdx-1]||0):0;
  const da=dcf.da||0;
  const capex=m.capex?Math.abs(m.capex[lastIdx]||m.capex[lastIdx-1]||0):0;
  const dwc=dcf.delta_wc||0, netDebt=dcf.net_debt||0, curPrice=dcf.current_price||0;
  const mcap=dcf.enterprise_value-Math.max(0,netDebt);
  const grossDebt=netDebt>0?netDebt:0;
  const ewPct=mcap>0&&(mcap+grossDebt)>0?mcap/(mcap+grossDebt):0.7;
  const sharesM=(dcf.shares_outstanding&&dcf.shares_outstanding>0)?+(dcf.shares_outstanding/1e6).toFixed(1):(dcf.fair_price&&dcf.equity_value&&dcf.equity_value>0)?+(dcf.equity_value*1e9/dcf.fair_price/1e6).toFixed(1):null;
  if (!sharesM || sharesM <= 0) {
    console.warn('[DCF] Cannot determine shares outstanding for', ticker, 'dcf keys:', Object.keys(dcf));
    return null;
  }

  const R={}; let r=2;

  // ==================== TITLE + LEGEND ====================
  ws.mergeCells(r,2,r,10);
  sc(ws,r,2,'Discounted Cash Flow Analysis',{font:ft(15,true,'FFFFFF'),fill:fi(C.navy),align:{horizontal:'center',vertical:'middle'},border:bHeavy});
  ws.getRow(r).height=30; r++;
  ws.mergeCells(r,2,r,10);
  sc(ws,r,2,`${companyName} (${ticker})  |  FCFF-to-Firm  |  ${dateStr}  |  RUB  |  млрд руб`,{font:ft(9,false,'555555'),fill:fi(C.paleBlue),align:{horizontal:'center',vertical:'middle'}});
  ws.getRow(r).height=18; r++;

  // Legend
  sc(ws,r,2,'',{fill:fi(C.inputBg),border:bAll});
  sc(ws,r,3,'= input (можно менять)',{font:ft(8.5,false,'666666'),fill:fi(C.white),align:{horizontal:'left',indent:1},border:false});
  sc(ws,r,5,'',{fill:fi(C.lightBlue),border:bAll});
  sc(ws,r,6,'= справочные данные',{font:ft(8.5,false,'666666'),fill:fi(C.white),align:{horizontal:'left',indent:1},border:false});
  sc(ws,r,8,'f(x)',{font:ft(8.5,false,C.darkGray),fill:fi(C.white),border:bAll,align:{horizontal:'center',vertical:'middle'}});
  sc(ws,r,9,'= формула (пересчитывается)',{font:ft(8.5,false,'666666'),fill:fi(C.white),align:{horizontal:'left',indent:1},border:false});
  ws.getRow(r).height=18; r++;

  // ==================== I. ASSUMPTIONS ====================
  r++;
  secRow(ws,r,2,8,'I.  Ключевые допущения модели'); r++;
  subH(ws,r,2,3,'Стоимость капитала'); subH(ws,r,5,6,'Структура капитала'); r++;

  R.rf=r; lbl(ws,r,2,'Безрисковая ставка (Rf)'); inp(ws,r,3,rfPct,'0.00%');
  ws.getCell(r,3).note='Доходность 10-летних ОФЗ';
  lbl(ws,r,5,'E / V'); R.ew=r; inp(ws,r,6,ewPct,'0.0%'); r++;

  R.erp=r; lbl(ws,r,2,'Премия за риск (ERP)'); inp(ws,r,3,erpPct,'0.00%');
  lbl(ws,r,5,'D / V'); R.dw=r; fml(ws,r,6,`1-F${R.ew}`,1-ewPct,'0.0%'); r++;

  R.beta=r; lbl(ws,r,2,'Beta (β)'); inp(ws,r,3,betaVal,'0.00'); r++;

  R.ke=r; lbl(ws,r,2,'Стоимость собств. кап. (Ke)');
  fml(ws,r,3,`C${R.rf}+C${R.beta}*C${R.erp}`,kePct,'0.00%'); r++;

  R.kd=r; lbl(ws,r,2,'Стоимость долга (Kd, pre-tax)'); inp(ws,r,3,kdPct,'0.00%');
  ws.getCell(r,3).note='Rf + кредитный спред'; r++;

  R.tax=r; lbl(ws,r,2,'Эффективная ставка налога (t)'); inp(ws,r,3,taxPct,'0.0%'); r++;

  R.wacc=r; lbl(ws,r,2,'WACC'); ws.getCell(r,2).font=ft(9.5,true,C.navy);
  fml(ws,r,3,`F${R.ew}*C${R.ke}+F${R.dw}*C${R.kd}*(1-C${R.tax})`,waccPct,'0.00%',{bold:true}); r++;

  r++; subH(ws,r,2,3,'Темпы роста'); r++;
  R.growth=r; lbl(ws,r,2,'Темп роста FCFF (прогнозный)'); inp(ws,r,3,growPct,'0.0%'); r++;
  R.tg=r; lbl(ws,r,2,'Терминальный темп роста (g)'); inp(ws,r,3,tgPct,'0.0%'); r++;
  lbl(ws,r,2,'Горизонт прогноза, лет'); inp(ws,r,3,nProj,'0');
  sc(ws,r,4,'зафиксирован при генерации',{font:ft(8,false,'999999'),fill:fi(C.white),align:{horizontal:'left'},border:false}); r++;

  // ==================== II. BASE YEAR DATA ====================
  r++; secRow(ws,r,2,6,'II.  Данные базового года (млрд руб)'); r++;
  R.ebitda=r; lbl(ws,r,2,'EBITDA'); inp(ws,r,3,ebitda,'#,##0.0'); r++;
  R.da=r; lbl(ws,r,2,'D&A (Амортизация)'); inp(ws,r,3,da,'#,##0.0'); r++;
  R.capex=r; lbl(ws,r,2,'CapEx (положит. значение)'); inp(ws,r,3,capex,'#,##0.0'); r++;
  R.dwc=r; lbl(ws,r,2,'ΔWC (изм. оборотного капитала)'); inp(ws,r,3,dwc,'#,##0.0'); r++;
  R.nd=r; lbl(ws,r,2,'Чистый долг (отриц. = чист. ден.)'); inp(ws,r,3,netDebt,'#,##0.0');
  sc(ws,r,4,netDebt<0?'чист. ден. позиция':'чист. долг',{font:ft(8,false,'999999'),fill:fi(C.white),align:{horizontal:'left'},border:false}); r++;
  R.cp=r; lbl(ws,r,2,'Текущая цена акции, руб'); inp(ws,r,3,curPrice,'#,##0.00'); r++;
  R.sh=r; lbl(ws,r,2,'Количество акций, млн'); inp(ws,r,3,sharesM,'#,##0.0'); r++;

  // ==================== III. FCFF CALCULATION ====================
  r++; secRow(ws,r,2,6,'III.  Расчёт FCFF базового года (формулы)'); r++;
  R.ebt=r; lbl(ws,r,2,'EBITDA × (1 - t)');
  fml(ws,r,3,`C${R.ebitda}*(1-C${R.tax})`,ebitda*(1-taxPct),'#,##0.0'); r++;
  R.das=r; lbl(ws,r,2,'(+) D&A × t (налог. щит)');
  fml(ws,r,3,`C${R.da}*C${R.tax}`,da*taxPct,'#,##0.0'); r++;
  R.cxl=r; lbl(ws,r,2,'(-) Капитальные затраты');
  fml(ws,r,3,`-C${R.capex}`,-capex,'#,##0.0'); r++;
  R.dwl=r; lbl(ws,r,2,'(-) Изм. оборотного капитала');
  fml(ws,r,3,`-C${R.dwc}`,-dwc,'#,##0.0'); r++;
  r++;
  R.fcff=r; lbl(ws,r,2,'Free Cash Flow to Firm (FCFF)');
  ws.getCell(r,2).font=ft(10,true,C.navy); ws.getCell(r,2).fill=fi(C.lightBlue);
  fml(ws,r,3,`C${R.ebt}+C${R.das}+C${R.cxl}+C${R.dwl}`,dcf.fcff_base||0,'#,##0.0',{bold:true,bg:C.darkBlue,color:'FFFFFF',border:bHeavy}); r++;

  // Supporting
  r++; subH(ws,r,2,4,'Справочные данные'); r++;
  const opIncome=m.operating_income?(m.operating_income[lastIdx]||m.operating_income[lastIdx-1]||0):0;
  lbl(ws,r,2,'Операционная прибыль (EBIT)'); ref(ws,r,3,opIncome,'#,##0.0'); r++;
  if(m.ocf){const ocfV=m.ocf[lastIdx]||m.ocf[lastIdx-1]||0; lbl(ws,r,2,'Операционный ден. поток (OCF)'); ref(ws,r,3,ocfV,'#,##0.0'); r++;}
  if(m.fcf){const fcfV=m.fcf[lastIdx]||m.fcf[lastIdx-1]||0; lbl(ws,r,2,'FCF (Smart-lab)'); ref(ws,r,3,fcfV,'#,##0.0'); r++;}

  // ==================== IV. PROJECTIONS ====================
  r+=2; const tc=3+nProj;
  secRow(ws,r,2,tc,'IV.  Прогноз FCFF и дисконтирование'); r++;

  sc(ws,r,2,'',{font:ft(9,true,'FFFFFF'),fill:fi(C.accent)});
  for(let i=0;i<nProj;i++) sc(ws,r,3+i,`Год ${i+1}`,{font:ft(9,true,'FFFFFF'),fill:fi(C.accent),align:{horizontal:'center',vertical:'middle'}});
  sc(ws,r,tc,'Терминальный',{font:ft(9,true,'FFFFFF'),fill:fi(C.green),align:{horizontal:'center',vertical:'middle'}}); r++;

  // FCFF projection
  R.pf=r; lbl(ws,r,2,'FCFF');
  for(let i=0;i<nProj;i++){
    const c=3+i;
    const formula=i===0?`$C$${R.fcff}*(1+$C$${R.growth})`:`${colL(c-1)}${r}*(1+$C$${R.growth})`;
    const pf=dcf.projected_fcff&&dcf.projected_fcff[i]?dcf.projected_fcff[i].fcff:0;
    fml(ws,r,c,formula,pf,'#,##0.0');
  }
  const tfr=dcf.projected_fcff&&dcf.projected_fcff[nProj-1]?dcf.projected_fcff[nProj-1].fcff*(1+tgPct):0;
  fml(ws,r,tc,`${colL(tc-1)}${r}*(1+$C$${R.tg})`,tfr,'#,##0.0',{bg:C.greenBg}); r++;

  // YoY Growth
  lbl(ws,r,2,'  YoY рост');
  for(let i=0;i<nProj;i++){
    const c=3+i;
    const formula=i===0?`${colL(c)}${R.pf}/$C$${R.fcff}-1`:`${colL(c)}${R.pf}/${colL(c-1)}${R.pf}-1`;
    fml(ws,r,c,formula,growPct,'0.0%',{color:'666666'});
  }
  fml(ws,r,tc,`${colL(tc)}${R.pf}/${colL(tc-1)}${R.pf}-1`,tgPct,'0.0%',{color:'666666',bg:C.greenBg}); r++;

  // Discount factor
  R.df=r; lbl(ws,r,2,'Дисконт-фактор (1/(1+WACC)^n)');
  for(let i=0;i<nProj;i++) fml(ws,r,3+i,`1/(1+$C$${R.wacc})^${i+1}`,1/Math.pow(1+waccPct,i+1),'0.0000',{color:'666666'});
  fml(ws,r,tc,`1/(1+$C$${R.wacc})^${nProj}`,1/Math.pow(1+waccPct,nProj),'0.0000',{color:'666666',bg:C.greenBg}); r++;

  // PV FCFF
  R.pv=r; lbl(ws,r,2,'PV (FCFF)');
  let sumPV=0;
  for(let i=0;i<nProj;i++){
    const c=3+i;const pf=dcf.projected_fcff&&dcf.projected_fcff[i]?dcf.projected_fcff[i].fcff:0;
    const pvVal=pf/Math.pow(1+waccPct,i+1); sumPV+=pvVal;
    fml(ws,r,c,`${colL(c)}${R.pf}*${colL(c)}${R.df}`,pvVal,'#,##0.0');
  }
  r++;

  // ==================== V. VALUATION BRIDGE ====================
  r+=2; secRow(ws,r,2,6,'V.  Оценка стоимости (EV → Equity → Per Share)'); r++;

  R.spv=r; lbl(ws,r,2,'Σ PV прогнозных FCFF');
  fml(ws,r,3,`SUM(C${R.pv}:${colL(2+nProj)}${R.pv})`,sumPV,'#,##0.0'); r++;

  R.tv=r; lbl(ws,r,2,'Терминальная стоимость (TV)');
  const tvCalc=tgPct<waccPct?tfr/(waccPct-tgPct):0;
  fml(ws,r,3,`${colL(tc)}${R.pf}/($C$${R.wacc}-$C$${R.tg})`,tvCalc,'#,##0.0'); r++;

  R.pvtv=r; lbl(ws,r,2,'PV терминальной стоимости');
  fml(ws,r,3,`C${R.tv}*${colL(2+nProj)}${R.df}`,dcf.pv_terminal||0,'#,##0.0');
  const tvPct=dcf.enterprise_value>0?(dcf.pv_terminal||0)/dcf.enterprise_value:0;
  sc(ws,r,4,`${(tvPct*100).toFixed(0)}% от EV`,{font:ft(8,false,'999999'),fill:fi(C.white),align:{horizontal:'left'},border:false}); r++;

  r++;
  R.ev=r; lbl(ws,r,2,'Enterprise Value (EV)'); ws.getCell(r,2).font=ft(10,true,C.navy);
  fml(ws,r,3,`C${R.spv}+C${R.pvtv}`,dcf.enterprise_value||0,'#,##0.0',{bold:true,bg:C.lightBlue}); r++;

  R.ndl=r; lbl(ws,r,2,netDebt<0?'(+) Чистая ден. позиция':'(-) Чистый долг');
  fml(ws,r,3,`-C${R.nd}`,-netDebt,'#,##0.0'); r++;

  r++;
  R.eq=r; lbl(ws,r,2,'Equity Value'); ws.getCell(r,2).font=ft(10,true,C.navy);
  fml(ws,r,3,`C${R.ev}+C${R.ndl}`,dcf.equity_value||0,'#,##0.0',{bold:true,bg:C.lightBlue}); r++;

  R.shl=r; lbl(ws,r,2,'Количество акций, млн');
  fml(ws,r,3,`C${R.sh}`,sharesM,'#,##0.0'); r++;

  r++;
  R.fp=r; lbl(ws,r,2,'Справедливая цена за акцию, руб'); ws.getCell(r,2).font=ft(10,true,C.navy);
  fml(ws,r,3,`C${R.eq}*1000/C${R.shl}`,dcf.fair_price||0,'#,##0.00',{bold:true,bg:C.lightBlue}); r++;

  R.cpl=r; lbl(ws,r,2,'Текущая рыночная цена, руб');
  fml(ws,r,3,`C${R.cp}`,curPrice,'#,##0.00'); r++;

  const upVal=curPrice>0?(dcf.fair_price||0)/curPrice-1:0;
  lbl(ws,r,2,'Потенциал (upside / downside)');
  fml(ws,r,3,`C${R.fp}/C${R.cpl}-1`,upVal,'+0.0%;-0.0%',{bold:true,color:upVal>=0?C.green:C.red}); r++;

  // Implied multiples
  r++; subH(ws,r,2,4,'Implied-мультипликаторы при справедливой цене'); r++;
  if(ebitda>0){lbl(ws,r,2,'Implied EV/EBITDA');fml(ws,r,3,`C${R.ev}/C${R.ebitda}`,(dcf.enterprise_value||0)/ebitda,'0.0"x"');r++;}
  const netIncome=m.net_income?(m.net_income[lastIdx]||m.net_income[lastIdx-1]||0):0;
  if(netIncome>0){lbl(ws,r,2,'Implied P/E');fml(ws,r,3,`C${R.eq}/${netIncome.toFixed(1)}`,(dcf.equity_value||0)/netIncome,'0.0"x"');r++;}
  if(m.fcf){const fcfV=m.fcf[lastIdx]||m.fcf[lastIdx-1]||0;if(fcfV>0){lbl(ws,r,2,'Implied P/FCF');fml(ws,r,3,`C${R.eq}/${fcfV.toFixed(1)}`,(dcf.equity_value||0)/fcfV,'0.0"x"');r++;}}

  // ==================== VI. SENSITIVITY (formula-based) ====================
  r+=2; secRow(ws,r,2,10,'VI.  Таблица чувствительности — Справедливая цена (руб)  [формулы — обновляются при изм. входных данных]'); r++;
  subH(ws,r,2,10,'A.  WACC  vs  Терминальный темп роста (g)'); r++;

  const baseWacc=waccPct, baseTG=tgPct, baseGR=growPct;
  const waccSteps=[-0.03,-0.02,-0.01,0,0.01,0.02,0.03];
  const tgSteps=[-0.02,-0.01,-0.005,0,0.005,0.01,0.02];
  const waccVals=waccSteps.map(d=>baseWacc+d).filter(w=>w>0.04);
  const tgVals=tgSteps.map(d=>baseTG+d).filter(t=>t>=0&&t<baseWacc-0.01);

  // Build sensitivity fair-price formula
  function buildSensFP(wRef,tgRef,grRef){
    const parts=[];
    for(let i=1;i<=nProj;i++) parts.push(`$C$${R.fcff}*(1+${grRef})^${i}/(1+${wRef})^${i}`);
    parts.push(`$C$${R.fcff}*(1+${grRef})^${nProj}*(1+${tgRef})/(${wRef}-${tgRef})/(1+${wRef})^${nProj}`);
    return `IF(OR($C$${R.sh}=0,${wRef}-${tgRef}<=0),0,(${parts.join('+')}-$C$${R.nd})*1000/$C$${R.sh})`;
  }
  // JS helper for pre-computed results
  function calcFP(w,tg,gr){
    if(w<=tg||w<=0)return 0;
    let fc=dcf.fcff_base||0,pv=0;
    for(let i=1;i<=nProj;i++){fc*=(1+gr);pv+=fc/Math.pow(1+w,i);}
    const tv=fc*(1+tg)/(w-tg); pv+=tv/Math.pow(1+w,nProj);
    const eq=pv-netDebt;
    return sharesM>0?eq*1000/sharesM:0;
  }

  // Table A header
  R.sensAhdr=r;
  sc(ws,r,2,'WACC \\ g term',{font:ft(8.5,true,'FFFFFF'),fill:fi(C.navy),align:{horizontal:'center',vertical:'middle',wrapText:true}});
  ws.getRow(r).height=22;
  for(let j=0;j<tgVals.length;j++){
    const isBase=Math.abs(tgVals[j]-baseTG)<0.001;
    sc(ws,r,3+j,tgVals[j],{font:ft(9,true,isBase?'FFFFFF':C.navy),fill:fi(isBase?C.accent:C.lightBlue),fmt:'0.0%',align:{horizontal:'center',vertical:'middle'}});
  }
  r++;

  for(let i=0;i<waccVals.length;i++){
    const isBaseR=Math.abs(waccVals[i]-baseWacc)<0.001;
    sc(ws,r,2,waccVals[i],{font:ft(9,true,isBaseR?'FFFFFF':C.navy),fill:fi(isBaseR?C.accent:C.lightBlue),fmt:'0.0%',align:{horizontal:'center',vertical:'middle'}});
    for(let j=0;j<tgVals.length;j++){
      const isCenter=isBaseR&&Math.abs(tgVals[j]-baseTG)<0.001;
      const wRef=`$B${r}`, tgRef=`${colL(3+j)}$${R.sensAhdr}`, grRef=`$C$${R.growth}`;
      const formula=buildSensFP(wRef,tgRef,grRef);
      const result=calcFP(waccVals[i],tgVals[j],baseGR);
      const d=curPrice>0?result/curPrice-1:0;
      let bg=C.white;
      if(result>0&&curPrice>0){if(d>0.15)bg=C.greenBg;else if(d>0)bg=C.yellowBg;else if(d>-0.15)bg=C.orangeBg;else bg=C.redBg;}
      fml(ws,r,3+j,formula,Math.round(result),'#,##0',{sz:isCenter?10:9,bold:isCenter,bg:isCenter?'BDD7EE':bg,border:isCenter?bHeavy:bAll,color:C.black});
    }
    r++;
  }
  r++;
  ws.mergeCells(r,2,r,10);
  sc(ws,r,2,'Зелёный: upside >15% | Жёлтый: 0–15% | Оранжевый: downside 0–15% | Красный: >15% | Выделение: текущие параметры | Все значения — формулы',{font:ft(7.5,false,'999999'),align:{horizontal:'left'},border:false});

  // Table B: WACC vs FCFF Growth
  r+=2; subH(ws,r,2,10,'B.  WACC  vs  Темп роста FCFF (прогнозный период)'); r++;
  const grSteps=[-0.04,-0.02,-0.01,0,0.01,0.02,0.04];
  const grVals=grSteps.map(d=>baseGR+d).filter(g=>g>=-0.05&&g<=0.30);

  R.sensBhdr=r;
  sc(ws,r,2,'WACC \\ g FCFF',{font:ft(8.5,true,'FFFFFF'),fill:fi(C.navy),align:{horizontal:'center',vertical:'middle',wrapText:true}});
  ws.getRow(r).height=22;
  for(let j=0;j<grVals.length;j++){
    const isBase=Math.abs(grVals[j]-baseGR)<0.001;
    sc(ws,r,3+j,grVals[j],{font:ft(9,true,isBase?'FFFFFF':C.navy),fill:fi(isBase?C.accent:C.lightBlue),fmt:'0.0%',align:{horizontal:'center',vertical:'middle'}});
  }
  r++;

  const waccVals2=waccSteps.map(d=>baseWacc+d).filter(w=>w>0.04);
  for(let i=0;i<waccVals2.length;i++){
    const isBaseR=Math.abs(waccVals2[i]-baseWacc)<0.001;
    sc(ws,r,2,waccVals2[i],{font:ft(9,true,isBaseR?'FFFFFF':C.navy),fill:fi(isBaseR?C.accent:C.lightBlue),fmt:'0.0%',align:{horizontal:'center',vertical:'middle'}});
    for(let j=0;j<grVals.length;j++){
      const isCenter=isBaseR&&Math.abs(grVals[j]-baseGR)<0.001;
      const wRef=`$B${r}`, tgRef=`$C$${R.tg}`, grRef=`${colL(3+j)}$${R.sensBhdr}`;
      const formula=buildSensFP(wRef,tgRef,grRef);
      const result=calcFP(waccVals2[i],baseTG,grVals[j]);
      const d=curPrice>0?result/curPrice-1:0;
      let bg=C.white;
      if(result>0&&curPrice>0){if(d>0.15)bg=C.greenBg;else if(d>0)bg=C.yellowBg;else if(d>-0.15)bg=C.orangeBg;else bg=C.redBg;}
      fml(ws,r,3+j,formula,Math.round(result),'#,##0',{sz:isCenter?10:9,bold:isCenter,bg:isCenter?'BDD7EE':bg,border:isCenter?bHeavy:bAll,color:C.black});
    }
    r++;
  }

  // ==================== VII. HISTORICAL DATA ====================
  r+=2;
  if(years.length>0){
    secRow(ws,r,2,2+years.length,'VII.  Исторические финансовые данные (МСФО)'); r++;
    sc(ws,r,2,'Показатель',{font:ft(9,true,'FFFFFF'),fill:fi(C.accent),align:{horizontal:'left',indent:1}});
    for(let i=0;i<years.length;i++) sc(ws,r,3+i,years[i],{font:ft(9,true,'FFFFFF'),fill:fi(C.accent),align:{horizontal:'center',vertical:'middle'}});
    r++;
    const hm=[
      ['Выручка','revenue','#,##0.0',false,true],['EBITDA','ebitda','#,##0.0',false,true],
      ['  Рентаб. EBITDA','ebitda_margin','0.0"%"',true,false],['Операц. прибыль','operating_income','#,##0.0',false,false],
      ['Чистая прибыль','net_income','#,##0.0',false,true],['  Чистая рентаб.','net_margin','0.0"%"',true,false],
      ['D&A','amortization','#,##0.0',false,false],['Опер. ден. поток','ocf','#,##0.0',false,false],
      ['CapEx','capex','#,##0.0',false,false],['FCF','fcf','#,##0.0',false,false],
      ['Чистый долг','net_debt','#,##0.0',false,false],[null],
      ['ROE','roe','0.0"%"',true,false],['P/E','pe','0.0"x"',false,false],
      ['EV/EBITDA','ev_ebitda','0.0"x"',false,false],['Долг/EBITDA','debt_ebitda','0.0"x"',false,false],
    ];
    for(const item of hm){
      if(!item||item[0]===null){r++;continue;}
      const [label,key,fmt,isPct,showGr]=item;
      if(!m[key])continue;
      lbl(ws,r,2,label,label.startsWith('  ')?3:1);
      for(let i=0;i<years.length;i++){
        const v=m[key][i];
        if(v===null||v===undefined) sc(ws,r,3+i,'—',{font:ft(9,false,'CCCCCC'),fill:fi(C.lightBlue),align:{horizontal:'center'}});
        else ref(ws,r,3+i,v,fmt);
      }
      r++;
      if(showGr){
        lbl(ws,r,2,'  YoY рост',3); ws.getCell(r,2).font=ft(8.5,false,'999999');
        for(let i=0;i<years.length;i++){
          const cur=m[key][i],prev=i>0?m[key][i-1]:null;
          if(cur!==null&&prev!==null&&prev!==0) ref(ws,r,3+i,(cur/prev-1),'+0.0%;-0.0%',{color:'888888',sz:8.5});
          else sc(ws,r,3+i,'—',{font:ft(8.5,false,'CCCCCC'),fill:fi(C.lightBlue),align:{horizontal:'center'}});
        }
        r++;
      }
    }
  }

  // ==================== DISCLAIMER ====================
  r+=2; ws.mergeCells(r,2,r,10);
  sc(ws,r,2,'Disclaimer: Данная модель носит исключительно информационный характер и не является инвестиционной рекомендацией. Результаты зависят от входных допущений. Источник: Smart-lab.ru, MOEX ISS.  |  AA+ (aaplus.pro)',
    {font:ft(7.5,false,'999999'),align:{horizontal:'left',wrapText:true},border:false});

  ws.views=[{state:'frozen',ySplit:4,xSplit:2}];

  const buffer = await wb.xlsx.writeBuffer();
  return Buffer.from(buffer);
}

// --- Server ---

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  const url = new URL(req.url, `http://localhost:${PORT}`);

  // === MPStats: Check if configured ===
  if (url.pathname === '/api/mpstats/status') {
    jsonResp(res, 200, { configured: !!MPSTATS_TOKEN });
    return;
  }

  // === MPStats: Category top products ===
  // GET /api/mpstats/category?path=Электроника/Наушники&days=30&limit=20
  if (url.pathname === '/api/mpstats/category' && req.method === 'GET') {
    if (!MPSTATS_TOKEN) return jsonResp(res, 503, { error: 'MPStats API не настроен. Добавьте MPSTATS_TOKEN.' });
    const catPath = url.searchParams.get('path');
    if (!catPath) return jsonResp(res, 400, { error: 'path parameter required' });
    const days = parseInt(url.searchParams.get('days') || '30');
    const limit = Math.min(parseInt(url.searchParams.get('limit') || '20'), 50);

    const payload = JSON.stringify({
      path: catPath,
      d1: daysAgo(days),
      d2: today(),
      startRow: 0,
      endRow: limit
    });

    proxyRequest({
      hostname: 'mpstats.io',
      path: '/api/wb/get/category',
      method: 'POST',
      headers: {
        'X-Mpstats-TOKEN': MPSTATS_TOKEN,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload)
      }
    }, payload, res);
    return;
  }

  // === MPStats: Category trend ===
  // GET /api/mpstats/trend?path=Электроника/Наушники
  if (url.pathname === '/api/mpstats/trend' && req.method === 'GET') {
    if (!MPSTATS_TOKEN) return jsonResp(res, 503, { error: 'MPStats API не настроен' });
    const catPath = url.searchParams.get('path');
    if (!catPath) return jsonResp(res, 400, { error: 'path parameter required' });

    const payload = JSON.stringify({
      path: catPath,
      d1: daysAgo(365),
      d2: today()
    });

    proxyRequest({
      hostname: 'mpstats.io',
      path: '/api/wb/get/category/trend',
      method: 'POST',
      headers: {
        'X-Mpstats-TOKEN': MPSTATS_TOKEN,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload)
      }
    }, payload, res);
    return;
  }

  // === MPStats: Product sales ===
  // GET /api/mpstats/product/sales?sku=123456&days=30
  if (url.pathname === '/api/mpstats/product/sales' && req.method === 'GET') {
    if (!MPSTATS_TOKEN) return jsonResp(res, 503, { error: 'MPStats API не настроен' });
    const sku = url.searchParams.get('sku');
    if (!sku) return jsonResp(res, 400, { error: 'sku parameter required' });
    const days = parseInt(url.searchParams.get('days') || '30');

    proxyRequest({
      hostname: 'mpstats.io',
      path: `/api/wb/get/item/${sku}/sales?d1=${daysAgo(days)}&d2=${today()}`,
      method: 'GET',
      headers: { 'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json' }
    }, null, res);
    return;
  }

  // === MPStats: Product keywords ===
  // GET /api/mpstats/product/keywords?sku=123456
  if (url.pathname === '/api/mpstats/product/keywords' && req.method === 'GET') {
    if (!MPSTATS_TOKEN) return jsonResp(res, 503, { error: 'MPStats API не настроен' });
    const sku = url.searchParams.get('sku');
    if (!sku) return jsonResp(res, 400, { error: 'sku parameter required' });

    proxyRequest({
      hostname: 'mpstats.io',
      path: `/api/wb/get/item/${sku}/by_keywords`,
      method: 'GET',
      headers: { 'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json' }
    }, null, res);
    return;
  }

  // === MPStats: All niches/subjects ===
  // GET /api/mpstats/niches
  if (url.pathname === '/api/mpstats/niches' && req.method === 'GET') {
    if (!MPSTATS_TOKEN) return jsonResp(res, 503, { error: 'MPStats API не настроен' });

    proxyRequest({
      hostname: 'mpstats.io',
      path: `/api/wb/get/subject/list?dt=${today()}`,
      method: 'GET',
      headers: { 'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json' }
    }, null, res);
    return;
  }

  // === MPStats: API limits ===
  // GET /api/mpstats/limits
  if (url.pathname === '/api/mpstats/limits' && req.method === 'GET') {
    if (!MPSTATS_TOKEN) return jsonResp(res, 503, { error: 'MPStats API не настроен' });

    proxyRequest({
      hostname: 'mpstats.io',
      path: '/api/user/check/limits',
      method: 'GET',
      headers: { 'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json' }
    }, null, res);
    return;
  }

  // === CBR Precious Metals proxy ===
  if (url.pathname === '/api/cbr/metals' && req.method === 'GET') {
    const now = new Date();
    const d2 = `${String(now.getDate()).padStart(2,'0')}/${String(now.getMonth()+1).padStart(2,'0')}/${now.getFullYear()}`;
    const d1Obj = new Date(); d1Obj.setDate(d1Obj.getDate() - 7);
    const d1 = `${String(d1Obj.getDate()).padStart(2,'0')}/${String(d1Obj.getMonth()+1).padStart(2,'0')}/${d1Obj.getFullYear()}`;

    const metalUrl = `https://www.cbr.ru/scripts/xml_metall.asp?date_req1=${d1}&date_req2=${d2}`;
    https.get(metalUrl, { headers: { 'User-Agent': 'Mozilla/5.0' } }, (proxyRes) => {
      let data = '';
      proxyRes.on('data', chunk => { data += chunk; });
      proxyRes.on('end', () => {
        try {
          // Parse XML manually (simple format)
          const records = [];
          const regex = /<Record\s+Date="([^"]+)"\s+Code="(\d+)">\s*<Buy>([\d,]+)<\/Buy>\s*<Sell>([\d,]+)<\/Sell>\s*<\/Record>/g;
          let match;
          while ((match = regex.exec(data)) !== null) {
            records.push({ date: match[1], code: parseInt(match[2]), buy: parseFloat(match[3].replace(',','.')), sell: parseFloat(match[4].replace(',','.')) });
          }
          // Return latest record per metal
          const latest = {};
          records.forEach(r => { latest[r.code] = r; });
          jsonResp(res, 200, Object.values(latest));
        } catch(e) { jsonResp(res, 500, { error: 'parse error' }); }
      });
    }).on('error', (e) => { jsonResp(res, 502, { error: e.message }); });
    return;
  }

  // === CBR Key Rate proxy ===
  if (url.pathname === '/api/cbr/keyrate' && req.method === 'GET') {
    const nowDate = new Date();
    const soapBody = `<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:web="http://web.cbr.ru/">
  <soap:Body>
    <web:KeyRateXML>
      <web:fromDate>2024-01-01</web:fromDate>
      <web:ToDate>${nowDate.getFullYear()}-${String(nowDate.getMonth()+1).padStart(2,'0')}-${String(nowDate.getDate()).padStart(2,'0')}</web:ToDate>
    </web:KeyRateXML>
  </soap:Body>
</soap:Envelope>`;
    const payload = Buffer.from(soapBody, 'utf-8');
    const soapReq = https.request({
      hostname: 'www.cbr.ru',
      path: '/DailyInfoWebServ/DailyInfo.asmx',
      method: 'POST',
      headers: {
        'Content-Type': 'text/xml; charset=utf-8',
        'SOAPAction': 'http://web.cbr.ru/KeyRateXML',
        'Content-Length': payload.length
      }
    }, (proxyRes) => {
      let data = '';
      proxyRes.on('data', chunk => { data += chunk; });
      proxyRes.on('end', () => {
        try {
          const rates = [];
          const regex = /<KR>[\s\S]*?<DT>([\d\-T:+]+)<\/DT>[\s\S]*?<Rate>([\d.]+)<\/Rate>[\s\S]*?<\/KR>/g;
          let match;
          while ((match = regex.exec(data)) !== null) {
            rates.push({ date: match[1].slice(0, 10), rate: parseFloat(match[2]) });
          }
          jsonResp(res, 200, rates);
        } catch(e) { jsonResp(res, 500, { error: 'parse error' }); }
      });
    });
    soapReq.on('error', (e) => { jsonResp(res, 502, { error: e.message }); });
    soapReq.write(payload);
    soapReq.end();
    return;
  }

  // === WB Search proxy (direct, may be blocked by WB) ===
  if (url.pathname === '/api/wb' && req.method === 'GET') {
    const query = url.searchParams.get('query');
    const sort = url.searchParams.get('sort') || 'popular';
    const limit = Math.min(parseInt(url.searchParams.get('limit') || '10'), 20);
    if (!query) return jsonResp(res, 400, { error: 'query parameter required' });

    proxyRequest({
      hostname: 'search.wb.ru',
      path: `/exactmatch/ru/common/v7/search?appType=1&curr=rub&dest=-1257786&query=${encodeURIComponent(query)}&resultset=catalog&sort=${sort}&limit=${limit}&page=1`,
      method: 'GET',
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Origin': 'https://www.wildberries.ru',
        'Referer': 'https://www.wildberries.ru/'
      }
    }, null, res);
    return;
  }

  // === MOEX ISS: Historical candles ===
  // GET /api/moex/history?ticker=SBER&from=2024-01-01&to=2025-01-01
  if (url.pathname === '/api/moex/history' && req.method === 'GET') {
    const ticker = (url.searchParams.get('ticker') || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!ticker || ticker.length > 10) { jsonResp(res, 400, { error: 'invalid ticker' }); return; }
    const from = (url.searchParams.get('from') || '').replace(/[^0-9-]/g, '');
    const to = (url.searchParams.get('to') || '').replace(/[^0-9-]/g, '');
    if (!from || !to) { jsonResp(res, 400, { error: 'from and to required (YYYY-MM-DD)' }); return; }

    // Fetch all pages of candles (MOEX returns max 500 per request)
    const allCandles = [];
    let start = 0;
    const maxPages = 10;

    function fetchPage(pageNum) {
      const moexPath = `/iss/engines/stock/markets/shares/boards/TQBR/securities/${encodeURIComponent(ticker)}/candles.json?from=${from}&till=${to}&interval=24&start=${start}&iss.meta=off`;
      https.get('https://iss.moex.com' + moexPath, {
        headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' }
      }, (proxyRes) => {
        let data = '';
        proxyRes.on('data', chunk => { data += chunk; });
        proxyRes.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            const columns = parsed.candles.columns;
            const rows = parsed.candles.data;
            if (rows.length === 0) {
              // No more data, return accumulated candles
              jsonResp(res, 200, { ticker, from, to, candles: allCandles });
              return;
            }
            const oi = columns.indexOf('open');
            const ci = columns.indexOf('close');
            const hi = columns.indexOf('high');
            const li = columns.indexOf('low');
            const vi = columns.indexOf('value');
            const di = columns.indexOf('begin');
            if (oi === -1 || ci === -1 || di === -1 || hi === -1 || li === -1) {
              jsonResp(res, 500, { error: 'MOEX column structure changed', columns });
              return;
            }
            for (const row of rows) {
              allCandles.push({
                date: (row[di] || '').slice(0, 10),
                open: row[oi],
                close: row[ci],
                high: row[hi],
                low: row[li],
                value: row[vi]
              });
            }
            if (rows.length >= 500 && pageNum < maxPages) {
              start += 500;
              fetchPage(pageNum + 1);
            } else {
              jsonResp(res, 200, { ticker, from, to, candles: allCandles });
            }
          } catch(e) { jsonResp(res, 500, { error: 'parse error: ' + e.message }); }
        });
      }).on('error', (e) => { jsonResp(res, 502, { error: e.message }); });
    }
    fetchPage(0);
    return;
  }

  // === MOEX ISS: Securities list ===
  // GET /api/moex/securities
  if (url.pathname === '/api/moex/securities' && req.method === 'GET') {
    const moexPath = '/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=securities&securities.columns=SECID,SHORTNAME,PREVPRICE';
    https.get('https://iss.moex.com' + moexPath, {
      headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' }
    }, (proxyRes) => {
      let data = '';
      proxyRes.on('data', chunk => { data += chunk; });
      proxyRes.on('end', () => {
        try {
          const parsed = JSON.parse(data);
          const cols = parsed.securities.columns;
          const rows = parsed.securities.data;
          const si = cols.indexOf('SECID');
          const ni = cols.indexOf('SHORTNAME');
          const pi = cols.indexOf('PREVPRICE');
          const securities = rows
            .filter(r => r[pi] && r[pi] > 0)
            .map(r => ({ ticker: r[si], name: r[ni], price: r[pi] }));
          jsonResp(res, 200, { securities });
        } catch(e) { jsonResp(res, 500, { error: 'parse error' }); }
      });
    }).on('error', (e) => { jsonResp(res, 502, { error: e.message }); });
    return;
  }

  // === MOEX ISS: Index (IMOEX) history ===
  // GET /api/moex/index?from=2024-01-01&to=2025-01-01
  if (url.pathname === '/api/moex/index' && req.method === 'GET') {
    const from = (url.searchParams.get('from') || '').replace(/[^0-9-]/g, '');
    const to = (url.searchParams.get('to') || '').replace(/[^0-9-]/g, '');
    if (!from || !to) { jsonResp(res, 400, { error: 'from and to required' }); return; }

    const allCandles = [];
    let start = 0;

    function fetchIndexPage(pageNum) {
      const moexPath = `/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json?from=${from}&till=${to}&interval=24&start=${start}&iss.meta=off`;
      https.get('https://iss.moex.com' + moexPath, {
        headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' }
      }, (proxyRes) => {
        let data = '';
        proxyRes.on('data', chunk => { data += chunk; });
        proxyRes.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            const columns = parsed.candles.columns;
            const rows = parsed.candles.data;
            if (rows.length === 0) {
              jsonResp(res, 200, { ticker: 'IMOEX', from, to, candles: allCandles });
              return;
            }
            const ci = columns.indexOf('close');
            const di = columns.indexOf('begin');
            for (const row of rows) {
              allCandles.push({ date: (row[di] || '').slice(0, 10), close: row[ci] });
            }
            if (rows.length >= 500 && pageNum < 10) {
              start += 500;
              fetchIndexPage(pageNum + 1);
            } else {
              jsonResp(res, 200, { ticker: 'IMOEX', from, to, candles: allCandles });
            }
          } catch(e) { jsonResp(res, 500, { error: 'parse error' }); }
        });
      }).on('error', (e) => { jsonResp(res, 502, { error: e.message }); });
    }
    fetchIndexPage(0);
    return;
  }

  // === OFZ yield (risk-free rate) ===
  // GET /api/moex/ofz-yield
  if (url.pathname === '/api/moex/ofz-yield' && req.method === 'GET') {
    const ofzCacheKey = 'ofz_yield';
    const ofzCached = realtimeCache.get(ofzCacheKey);
    if (ofzCached && Date.now() - ofzCached.ts < 3600000) { // 1h cache
      jsonResp(res, 200, ofzCached.data);
      return;
    }
    // Fetch 10Y OFZ (SU26238RMFS4) and key rate proxy from MOEX
    const ofzUrl = '/iss/engines/stock/markets/bonds/securities.json?iss.meta=off&securities.columns=SECID,SHORTNAME,PREVPRICE,COUPONPERCENT,MATDATE,EFFECTIVEYIELD&marketdata.columns=SECID,YIELD&iss.only=securities,marketdata';
    const ofzReq = https.request({
      hostname: 'iss.moex.com', port: 443, path: ofzUrl, method: 'GET',
      headers: { 'Accept': 'application/json' }
    }, (ofzResp) => {
      let d = '';
      ofzResp.on('data', c => d += c);
      ofzResp.on('end', () => {
        try {
          const j = JSON.parse(d);
          const secs = j.securities;
          const md = j.marketdata;
          const secCols = secs.columns;
          const mdCols = md.columns;
          // Pre-compute column indices once (not inside loops)
          const secIdx = { SECID: secCols.indexOf('SECID'), MATDATE: secCols.indexOf('MATDATE'), EFFECTIVEYIELD: secCols.indexOf('EFFECTIVEYIELD') };
          const mdIdx = { SECID: mdCols.indexOf('SECID'), YIELD: mdCols.indexOf('YIELD') };
          if (secIdx.SECID === -1) { jsonResp(res, 500, { error: 'MOEX OFZ column structure changed' }); return; }
          // Find OFZ bonds with maturity 5-15 years for benchmark yield
          const now = new Date();
          const ofzBonds = [];
          for (let i = 0; i < secs.data.length; i++) {
            const row = secs.data[i];
            const secid = row[secIdx.SECID];
            const matDate = row[secIdx.MATDATE];
            const effYield = row[secIdx.EFFECTIVEYIELD];
            if (!secid || !secid.startsWith('SU') || !matDate) continue;
            const mat = new Date(matDate);
            const yearsToMat = (mat - now) / (365.25 * 86400000);
            if (yearsToMat >= 4 && yearsToMat <= 15 && effYield > 0) {
              // Also try to get market yield
              const mdRow = mdIdx.SECID >= 0 ? md.data.find(r => r[mdIdx.SECID] === secid) : null;
              const mktYield = mdRow && mdIdx.YIELD >= 0 ? mdRow[mdIdx.YIELD] : null;
              ofzBonds.push({
                secid, maturity: matDate,
                years_to_mat: +yearsToMat.toFixed(1),
                yield: mktYield || effYield
              });
            }
          }
          ofzBonds.sort((a, b) => a.years_to_mat - b.years_to_mat);
          // Find ~10Y bond
          const bench10y = ofzBonds.find(b => b.years_to_mat >= 8 && b.years_to_mat <= 12) || ofzBonds[Math.floor(ofzBonds.length / 2)] || null;
          const bench5y = ofzBonds.find(b => b.years_to_mat >= 4 && b.years_to_mat <= 6) || null;
          const result = {
            benchmark_10y: bench10y ? { secid: bench10y.secid, yield: bench10y.yield, maturity: bench10y.maturity } : null,
            benchmark_5y: bench5y ? { secid: bench5y.secid, yield: bench5y.yield, maturity: bench5y.maturity } : null,
            all_ofz: ofzBonds.slice(0, 15),
            risk_free_rate: bench10y ? bench10y.yield : (bench5y ? bench5y.yield : null),
            ts: new Date().toISOString()
          };
          realtimeCache.set(ofzCacheKey, { data: result, ts: Date.now() });
          jsonResp(res, 200, result);
        } catch(e) {
          console.error('OFZ parse error:', e.message);
          jsonResp(res, 500, { error: 'OFZ parse failed' });
        }
      });
    });
    ofzReq.on('error', e => { jsonResp(res, 502, { error: e.message }); });
    ofzReq.setTimeout(10000, () => { ofzReq.destroy(); });
    ofzReq.end();
    return;
  }

  // === Real-time multiplier calculation ===
  // GET /api/moex/realtime-mults?ticker=SBER
  if (url.pathname === '/api/moex/realtime-mults' && req.method === 'GET') {
    const ticker = (url.searchParams.get('ticker') || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!ticker) { jsonResp(res, 400, { error: 'ticker required' }); return; }
    const rtCacheKey = 'rt_' + ticker;
    const rtCached = realtimeCache.get(rtCacheKey);
    if (rtCached && Date.now() - rtCached.ts < REALTIME_CACHE_TTL) {
      jsonResp(res, 200, rtCached.data);
      return;
    }
    // Fetch current price from MOEX + fundamentals from Smart-lab cache
    const moexUrl = `https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/${ticker}.json?iss.meta=off&iss.only=marketdata,securities&marketdata.columns=SECID,LAST,OPEN,HIGH,LOW,VOLTODAY,VALTODAY&securities.columns=SECID,SHORTNAME,PREVPRICE,ISSUESIZE`;
    https.get(moexUrl, { headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' } }, (moexRes) => {
      let mData = '';
      moexRes.on('data', c => { mData += c; });
      moexRes.on('end', () => {
        try {
          const moex = JSON.parse(mData);
          // Extract price and shares
          const secCols = moex.securities.columns;
          const secRow = moex.securities.data[0];
          const mdCols = moex.marketdata.columns;
          const mdRow = moex.marketdata.data[0];
          if (!secCols || !mdCols || !secRow || !mdRow) { jsonResp(res, 404, { error: 'ticker not found on MOEX' }); return; }
          const price = mdRow[mdCols.indexOf('LAST')] || secRow[secCols.indexOf('PREVPRICE')];
          const issueSize = secRow[secCols.indexOf('ISSUESIZE')];
          const shortName = secRow[secCols.indexOf('SHORTNAME')];
          const open = mdRow[mdCols.indexOf('OPEN')];
          const high = mdRow[mdCols.indexOf('HIGH')];
          const low = mdRow[mdCols.indexOf('LOW')];
          const volume = mdRow[mdCols.indexOf('VOLTODAY')];
          const value = mdRow[mdCols.indexOf('VALTODAY')];
          if (!price) { jsonResp(res, 404, { error: 'no price data' }); return; }

          // Get Smart-lab fundamentals from cache
          const slFund = smartlabCache.get('sl_fund_all');
          let slCompany = null;
          if (slFund && slFund.data && slFund.data.companies) {
            slCompany = slFund.data.companies.find(c => c.ticker === ticker);
          }
          // Also check individual company cache
          const slComp = smartlabCache.get('sl_co_' + ticker);

          // Calculate live multipliers
          const liveMcap = issueSize && price ? (price * issueSize / 1e9) : null; // in billions
          const result = {
            ticker, name: shortName,
            price: { current: price, open, high, low, prev: secRow[secCols.indexOf('PREVPRICE')], change_pct: secRow[secCols.indexOf('PREVPRICE')] ? ((price / secRow[secCols.indexOf('PREVPRICE')] - 1) * 100).toFixed(2) : null },
            volume: { shares: volume, value_rub: value },
            shares_outstanding: issueSize,
            live_market_cap: liveMcap ? +liveMcap.toFixed(1) : null,
            static_mults: slCompany || null,
            live_mults: {},
            historical_pe: null,
            avg_pe_5y: null,
            ts: new Date().toISOString()
          };

          if (slCompany && liveMcap) {
            if (slCompany.net_income && slCompany.net_income > 0) result.live_mults.pe = +(liveMcap / slCompany.net_income).toFixed(1);
            if (slCompany.revenue && slCompany.revenue > 0) result.live_mults.ps = +(liveMcap / slCompany.revenue).toFixed(2);
            // P/BV: need book value. Approximate from static P/B * static mcap / live mcap
            if (slCompany.pb && slCompany.market_cap) {
              const bv = slCompany.market_cap / slCompany.pb;
              result.live_mults.pb = +(liveMcap / bv).toFixed(2);
            }
            // EV/EBITDA live: derive EBITDA and net debt from static data, recalc with live mcap
            if (slCompany.ev_ebitda && slCompany.ev && slCompany.market_cap && slCompany.ev > 0) {
              const ebitda = slCompany.ev / slCompany.ev_ebitda;
              const netDebt = slCompany.ev - slCompany.market_cap;
              const liveEV = liveMcap + netDebt;
              if (ebitda > 0 && liveEV > 0) {
                const liveRatio = +(liveEV / ebitda).toFixed(1);
                // Sanity check: if live value diverges >100% from static, likely stale data — skip
                const divergence = Math.abs(liveRatio / slCompany.ev_ebitda - 1);
                if (divergence < 1.0) {
                  result.live_mults.ev_ebitda = liveRatio;
                }
              }
            }
            if (slCompany.div_yield_ao && slCompany.market_cap && liveMcap) {
              result.live_mults.div_yield = +((slCompany.div_yield_ao * slCompany.market_cap / liveMcap)).toFixed(1);
            }
          }

          // Historical P/E from company page (if cached)
          if (slComp && slComp.data && slComp.data.metrics && slComp.data.metrics.pe) {
            const peArr = slComp.data.metrics.pe.filter(v => v !== null && v > 0 && isFinite(v));
            result.historical_pe = slComp.data.metrics.pe;
            if (peArr.length >= 2) {
              const last5 = peArr.slice(-5);
              result.avg_pe_5y = +(last5.reduce((s,v) => s + v, 0) / last5.length).toFixed(1);
            }
          }

          realtimeCache.set(rtCacheKey, { data: result, ts: Date.now() });
          if (realtimeCache.size > 100) { const k = realtimeCache.keys().next().value; realtimeCache.delete(k); }
          jsonResp(res, 200, result);
        } catch(e) {
          console.error('realtime-mults error:', e.message);
          jsonResp(res, 500, { error: 'calculation failed', detail: e.message });
        }
      });
    }).on('error', e => { jsonResp(res, 502, { error: e.message }); });
    return;
  }

  // === Consensus / Valuation proxy (Conomy.ru via Puppeteer) ===
  // GET /api/consensus/company?ticker=SBER
  if (url.pathname === '/api/consensus/company' && req.method === 'GET') {
    const ticker = (url.searchParams.get('ticker') || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!ticker) { jsonResp(res, 400, { error: 'ticker required' }); return; }

    const cCacheKey = 'consensus_' + ticker;
    const cCached = consensusCache.get(cCacheKey);
    if (cCached && Date.now() - cCached.ts < CONSENSUS_CACHE_TTL) {
      jsonResp(res, 200, cCached.data);
      return;
    }

    // Check if puppeteer is available
    checkPuppeteer().then(available => {
      if (!available) {
        jsonResp(res, 503, { error: 'puppeteer-core not installed. Run: npm install puppeteer-core' });
        return;
      }
      if (!CONOMY_SLUGS[ticker]) {
        jsonResp(res, 404, { error: 'ticker not mapped to Conomy slug: ' + ticker });
        return;
      }
      fetchConsensusFromConomy(ticker).then(data => {
        if (data.error) {
          jsonResp(res, 502, { error: data.error });
          return;
        }
        const result = { ticker, consensus: data, source: 'Conomy.ru', ts: new Date().toISOString() };
        consensusCache.set(cCacheKey, { data: result, ts: Date.now() });
        if (consensusCache.size > 100) {
          const k = consensusCache.keys().next().value;
          consensusCache.delete(k);
        }
        jsonResp(res, 200, result);
      }).catch(e => {
        console.error('consensus error:', e.message);
        jsonResp(res, 500, { error: 'consensus fetch failed: ' + e.message });
      });
    });
    return;
  }

  // === Company News proxy ===
  // GET /api/news/company?ticker=SBER
  if (url.pathname === '/api/news/company' && req.method === 'GET') {
    const ticker = (url.searchParams.get('ticker') || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!ticker) { jsonResp(res, 400, { error: 'ticker required' }); return; }
    const nCacheKey = 'news_' + ticker;
    const nCached = newsCache.get(nCacheKey);
    if (nCached && Date.now() - nCached.ts < NEWS_CACHE_TTL) {
      jsonResp(res, 200, nCached.data);
      return;
    }
    // Fetch Smart-lab news for ticker
    const slNewsUrl = 'https://smart-lab.ru/forum/news/' + ticker + '/';
    fetchSmartlabPage(slNewsUrl, (err, html) => {
      if (err) { jsonResp(res, 502, { error: 'news fetch failed' }); return; }
      try {
        const news = parseSmartlabNews(html, ticker);
        const result = { ticker, news, count: news.length, sources: ['Smart-lab'], ts: new Date().toISOString() };
        newsCache.set(nCacheKey, { data: result, ts: Date.now() });
        if (newsCache.size > 200) { const k = newsCache.keys().next().value; newsCache.delete(k); }
        jsonResp(res, 200, result);
      } catch(e) {
        console.error('news parse error:', e.message);
        jsonResp(res, 500, { error: 'parse failed' });
      }
    });
    return;
  }

  // === Smart-lab Fundamentals proxy ===
  // GET /api/smartlab/fundamentals?sector=all
  if (url.pathname === '/api/smartlab/fundamentals' && req.method === 'GET') {
    const sector = url.searchParams.get('sector') || 'all';
    const cacheKey = 'sl_fund_' + sector;
    const cached = smartlabCache.get(cacheKey);
    if (cached && Date.now() - cached.ts < SMARTLAB_CACHE_TTL) {
      jsonResp(res, 200, cached.data);
      return;
    }
    const sectorMap = {
      'нефтегаз':1,'банки':2,'финансы':3,'металлургия_черн':4,'металлургия_цвет':5,
      'металлургия_разное':6,'драг_металлы':7,'горнодобывающие':8,'химия_удобрения':9,
      'химия_разное':10,'генерация':11,'электросети':12,'энергосбыт':13,
      'ритейл':14,'потреб':15,'агропром':16,'промышленность':17,'телеком':18,
      'интернет':19,'hightech':20,'софт':21,'фармацевтика':22,'медиа':23,
      'транспорт':24,'строители':25,'машиностроение':26
    };
    let slUrl = 'https://smart-lab.ru/q/shares_fundamental2/';
    const sectorLower = sector.toLowerCase().replace(/\s+/g,'_');
    if (sectorLower !== 'all' && sectorMap[sectorLower]) {
      slUrl += '?sector_id%5B%5D=' + sectorMap[sectorLower];
    }
    fetchSmartlabPage(slUrl, (err, html) => {
      if (err) { jsonResp(res, 502, { error: 'smartlab fetch failed' }); return; }
      try {
        const companies = parseSmartlabFundamentals(html);
        const result = { companies, sector, count: companies.length, ts: new Date().toISOString() };
        smartlabCache.set(cacheKey, { data: result, ts: Date.now() });
        if (smartlabCache.size > 50) {
          const oldest = smartlabCache.keys().next().value;
          smartlabCache.delete(oldest);
        }
        jsonResp(res, 200, result);
      } catch(e) {
        console.error('smartlab parse error:', e.message);
        jsonResp(res, 500, { error: 'parse failed', detail: e.message });
      }
    });
    return;
  }

  // GET /api/smartlab/company?ticker=SBER
  if (url.pathname === '/api/smartlab/company' && req.method === 'GET') {
    const ticker = (url.searchParams.get('ticker') || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!ticker) { jsonResp(res, 400, { error: 'ticker required' }); return; }
    const cacheKey = 'sl_co_' + ticker;
    const cached = smartlabCache.get(cacheKey);
    if (cached && Date.now() - cached.ts < SMARTLAB_CACHE_TTL) {
      jsonResp(res, 200, cached.data);
      return;
    }
    const slUrl = 'https://smart-lab.ru/q/' + ticker + '/f/y/MSFO/';
    fetchSmartlabPage(slUrl, (err, html) => {
      if (err) { jsonResp(res, 502, { error: 'smartlab fetch failed' }); return; }
      try {
        const company = parseSmartlabCompany(html, ticker);
        smartlabCache.set(cacheKey, { data: company, ts: Date.now() });
        jsonResp(res, 200, company);
      } catch(e) {
        console.error('smartlab company parse error:', e.message);
        jsonResp(res, 500, { error: 'parse failed', detail: e.message });
      }
    });
    return;
  }

  // GET /api/smartlab/sectors
  if (url.pathname === '/api/smartlab/sectors' && req.method === 'GET') {
    jsonResp(res, 200, {
      sectors: [
        {id:1,name:'Нефтегаз',key:'нефтегаз'},
        {id:2,name:'Банки',key:'банки'},
        {id:3,name:'Финансы',key:'финансы'},
        {id:4,name:'Металлургия черн.',key:'металлургия_черн'},
        {id:5,name:'Металлургия цвет.',key:'металлургия_цвет'},
        {id:7,name:'Драг. металлы',key:'драг_металлы'},
        {id:8,name:'Горнодобывающие',key:'горнодобывающие'},
        {id:9,name:'Химия / удобрения',key:'химия_удобрения'},
        {id:11,name:'Э/генерация',key:'генерация'},
        {id:12,name:'Электросети',key:'электросети'},
        {id:14,name:'Ритейл',key:'ритейл'},
        {id:18,name:'Телеком',key:'телеком'},
        {id:19,name:'Интернет',key:'интернет'},
        {id:21,name:'Производство софта',key:'софт'},
        {id:24,name:'Транспорт',key:'транспорт'},
        {id:25,name:'Строители',key:'строители'},
        {id:26,name:'Машиностроение',key:'машиностроение'}
      ]
    });
    return;
  }

  // === Banki.ru Cash Exchange Rates proxy ===
  // GET /api/banki/cash?city=moskva&currency=usd
  if (url.pathname === '/api/banki/cash' && req.method === 'GET') {
    const city = (url.searchParams.get('city') || 'moskva').replace(/[^a-z0-9_-]/gi, '');
    const currency = (url.searchParams.get('currency') || 'usd').toLowerCase().replace(/[^a-z]/g, '');
    const allowedCurrencies = ['usd','eur','gbp','cny','chf','jpy','try','aed'];
    if (!allowedCurrencies.includes(currency)) { jsonResp(res, 400, { error: 'invalid currency' }); return; }
    const currencyNames = { usd: 'USD', eur: 'EUR', gbp: 'GBP', cny: 'CNY', chf: 'CHF', jpy: 'JPY', try: 'TRY', aed: 'AED' };
    const currLabel = currencyNames[currency];

    // Check cache first
    const cached = getBankiCache(city, currency);
    if (cached) {
      jsonResp(res, 200, cached);
      return;
    }

    // banki.ru URL: /products/currency/cash/{currency}/{city}/  (for non-USD)
    //              /products/currency/cash/{city}/               (for USD default)
    const bankiPath = currency === 'usd'
      ? `/products/currency/cash/${encodeURIComponent(city)}/`
      : `/products/currency/cash/${encodeURIComponent(currency)}/${encodeURIComponent(city)}/`;
    const bankiUrl = `https://www.banki.ru${bankiPath}`;
    const ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

    // Step 1: Initial request to get anti-bot cookie
    https.get(bankiUrl, {
      headers: { 'User-Agent': ua, 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'ru-RU,ru;q=0.9' }
    }, (proxyRes) => {
      // Extract Set-Cookie headers
      const setCookies = proxyRes.headers['set-cookie'] || [];
      const cookies = setCookies.map(c => c.split(';')[0]).join('; ');

      // If redirect (302) with cookies — re-request with cookies
      if (proxyRes.statusCode >= 300 && proxyRes.statusCode < 400 && cookies) {
        // Drain the response
        proxyRes.resume();

        const targetUrl = (proxyRes.headers.location && proxyRes.headers.location.startsWith('http'))
          ? proxyRes.headers.location
          : bankiUrl;

        https.get(targetUrl, {
          headers: {
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'ru-RU,ru;q=0.9',
            'Cookie': cookies
          }
        }, (res2) => {
          // Handle possible second redirect
          if (res2.statusCode >= 300 && res2.statusCode < 400) {
            res2.resume();
            const cookies2 = (res2.headers['set-cookie'] || []).map(c => c.split(';')[0]).join('; ');
            const allCookies = cookies + (cookies2 ? '; ' + cookies2 : '');
            const url3 = (res2.headers.location && res2.headers.location.startsWith('http'))
              ? res2.headers.location
              : 'https://www.banki.ru' + (res2.headers.location || `/products/currency/cash/${encodeURIComponent(city)}/`);
            https.get(url3, {
              headers: { 'User-Agent': ua, 'Accept': 'text/html', 'Cookie': allCookies }
            }, (res3) => {
              let data = '';
              res3.on('data', chunk => { data += chunk; });
              res3.on('end', () => parseBankiHtml(data, city, currLabel, res));
            }).on('error', (e) => jsonResp(res, 502, { error: e.message }));
            return;
          }
          let data = '';
          res2.on('data', chunk => { data += chunk; });
          res2.on('end', () => parseBankiHtml(data, city, currLabel, res));
        }).on('error', (e) => jsonResp(res, 502, { error: e.message }));
        return;
      }

      // No redirect — direct response
      let data = '';
      proxyRes.on('data', chunk => { data += chunk; });
      proxyRes.on('end', () => parseBankiHtml(data, city, currLabel, res));
    }).on('error', (e) => { jsonResp(res, 502, { error: e.message }); });
    return;
  }

  // === DCF Excel Export ===
  if (url.pathname === '/api/dcf/export' && req.method === 'POST') {
    readBody(req, 524288).then(async (body) => {
      let data;
      try { data = JSON.parse(body); } catch(e) { jsonResp(res, 400, { error: 'invalid json' }); return; }
      const { dcf, company, ticker } = data;
      if (!dcf || !company) { jsonResp(res, 400, { error: 'missing dcf or company data' }); return; }
      try {
        const buf = await generateDCFExcel(dcf, company, ticker || 'N/A');
        if (!buf) { jsonResp(res, 400, { error: 'Cannot generate DCF: shares outstanding data missing' }); return; }
        res.writeHead(200, {
          'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          'Content-Disposition': `attachment; filename="DCF_${ticker || 'model'}_${new Date().toISOString().slice(0,10)}.xlsx"`,
          'Content-Length': buf.length
        });
        res.end(buf);
      } catch(e) {
        console.error('DCF export error:', e);
        jsonResp(res, 500, { error: 'Excel generation failed' });
      }
    }).catch(e => { jsonResp(res, 400, { error: e.message }); });
    return;
  }

  // === OpenRouter LLM Proxy (non-streaming, kept for compatibility) ===
  if (url.pathname === '/api/llm' && req.method === 'POST') {
    if (!OPENROUTER_KEY) { jsonResp(res, 500, { error: 'OPENROUTER_KEY not configured' }); return; }
    readBody(req, 262144).then(body => {
      let parsed;
      try { parsed = JSON.parse(body); } catch(e) { jsonResp(res, 400, { error: 'invalid json' }); return; }
      const payload = JSON.stringify({
        model: parsed.model || 'google/gemini-2.5-pro-preview',
        messages: parsed.messages || [],
        max_tokens: Math.min(parsed.max_tokens || 4096, 8192),
        temperature: parsed.temperature || 0.7,
        route: 'fallback'
      });
      proxyRequest({
        hostname: 'openrouter.ai',
        port: 443,
        path: '/api/v1/chat/completions',
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + OPENROUTER_KEY,
          'Content-Type': 'application/json',
          'HTTP-Referer': 'https://aaplus.pro',
          'X-Title': 'AA+'
        }
      }, payload, res);
    }).catch(e => { jsonResp(res, 400, { error: e.message }); });
    return;
  }

  // === OpenRouter LLM Streaming Proxy (SSE) ===
  if (url.pathname === '/api/llm/stream' && req.method === 'POST') {
    if (!OPENROUTER_KEY) { jsonResp(res, 500, { error: 'OPENROUTER_KEY not configured' }); return; }
    readBody(req, 262144).then(body => {
      let parsed;
      try { parsed = JSON.parse(body); } catch(e) { jsonResp(res, 400, { error: 'invalid json' }); return; }
      const payload = JSON.stringify({
        model: parsed.model || 'google/gemini-2.5-pro-preview',
        messages: parsed.messages || [],
        max_tokens: Math.min(parsed.max_tokens || 4096, 8192),
        temperature: parsed.temperature || 0.7,
        stream: true,
        route: 'fallback'
      });
      const proxyReq = https.request({
        hostname: 'openrouter.ai',
        port: 443,
        path: '/api/v1/chat/completions',
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + OPENROUTER_KEY,
          'Content-Type': 'application/json',
          'HTTP-Referer': 'https://aaplus.pro',
          'X-Title': 'AA+'
        }
      }, (proxyRes) => {
        if (res.headersSent) return;
        // If OpenRouter returns error (non-2xx), collect and return as JSON
        if (proxyRes.statusCode >= 400) {
          let errData = '';
          proxyRes.on('data', chunk => { errData += chunk; });
          proxyRes.on('end', () => {
            if (res.headersSent) return;
            res.writeHead(proxyRes.statusCode, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
            res.end(errData);
          });
          return;
        }
        // Stream SSE to client
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'Access-Control-Allow-Origin': '*',
          'X-Accel-Buffering': 'no'
        });
        // Ensure UTF-8 integrity: buffer partial multi-byte chars
        let partialByte = Buffer.alloc(0);
        proxyRes.on('data', chunk => {
          try {
            const buf = Buffer.concat([partialByte, chunk]);
            // Check if last bytes form incomplete UTF-8 sequence
            let safeEnd = buf.length;
            for (let i = 1; i <= 4 && i <= buf.length; i++) {
              const b = buf[buf.length - i];
              if ((b & 0xC0) === 0xC0) { // start of multi-byte
                const expected = (b & 0xF0) === 0xF0 ? 4 : (b & 0xE0) === 0xE0 ? 3 : 2;
                if (i < expected) { safeEnd = buf.length - i; }
                break;
              }
              if ((b & 0x80) === 0) break; // ASCII, all good
            }
            if (safeEnd < buf.length) {
              partialByte = buf.slice(safeEnd);
              res.write(buf.slice(0, safeEnd));
            } else {
              partialByte = Buffer.alloc(0);
              res.write(buf);
            }
          } catch(e) {}
        });
        proxyRes.on('end', () => {
          try {
            if (partialByte.length > 0) res.write(partialByte);
            res.end();
          } catch(e) {}
        });
      });
      proxyReq.on('error', (e) => {
        if (res.headersSent) return;
        res.writeHead(502, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
      });
      proxyReq.setTimeout(60000, () => {
        proxyReq.destroy();
        if (res.headersSent) return;
        res.writeHead(504, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'timeout' }));
      });
      proxyReq.write(payload);
      proxyReq.end();
    }).catch(e => { jsonResp(res, 400, { error: e.message }); });
    return;
  }

  // === Serve index.html ===
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate', 'Pragma': 'no-cache', 'Expires': '0' });
  res.end(indexHTML);
});

server.listen(PORT, '0.0.0.0', () => {
  console.log('NeuroHub server running on port ' + PORT);
  console.log('MPStats API:', MPSTATS_TOKEN ? 'configured' : 'NOT configured (set MPSTATS_TOKEN env var)');

  // Preload banki.ru cache for popular cities/currencies on startup
  const preloadCities = ['moskva', 'sankt-peterburg', 'kazan', 'novosibirsk', 'ekaterinburg', 'krasnodar', 'rostov-na-donu', 'nizhniy_novgorod'];
  const preloadCurrencies = ['usd', 'eur'];
  let loaded = 0, failed = 0;
  console.log('Preloading banki.ru cache for', preloadCities.length, 'cities ×', preloadCurrencies.length, 'currencies...');
  preloadCities.forEach(city => {
    preloadCurrencies.forEach(curr => {
      const currLabel = curr.toUpperCase();
      const bankiPath = curr === 'usd'
        ? `/products/currency/cash/${encodeURIComponent(city)}/`
        : `/products/currency/cash/${curr}/${encodeURIComponent(city)}/`;
      const bankiUrl = `https://www.banki.ru${bankiPath}`;
      const ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
      https.get(bankiUrl, {
        headers: { 'User-Agent': ua, 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'ru-RU,ru;q=0.9' }
      }, (proxyRes) => {
        const setCookies = proxyRes.headers['set-cookie'] || [];
        const cookies = setCookies.map(c => c.split(';')[0]).join('; ');
        if (proxyRes.statusCode >= 300 && proxyRes.statusCode < 400 && cookies) {
          proxyRes.resume();
          const targetUrl = (proxyRes.headers.location && proxyRes.headers.location.startsWith('http')) ? proxyRes.headers.location : bankiUrl;
          https.get(targetUrl, {
            headers: { 'User-Agent': ua, 'Accept': 'text/html', 'Accept-Language': 'ru-RU,ru;q=0.9', 'Cookie': cookies }
          }, (res2) => {
            if (res2.statusCode >= 300 && res2.statusCode < 400) {
              res2.resume();
              const cookies2 = (res2.headers['set-cookie'] || []).map(c => c.split(';')[0]).join('; ');
              const allCookies = cookies + (cookies2 ? '; ' + cookies2 : '');
              const url3 = (res2.headers.location && res2.headers.location.startsWith('http')) ? res2.headers.location : 'https://www.banki.ru' + (res2.headers.location || bankiPath);
              https.get(url3, {
                headers: { 'User-Agent': ua, 'Accept': 'text/html', 'Cookie': allCookies }
              }, (res3) => {
                let data = '';
                res3.on('data', chunk => { data += chunk; });
                res3.on('end', () => { preloadParse(data, city, currLabel); });
              }).on('error', () => { failed++; checkDone(); });
              return;
            }
            let data = '';
            res2.on('data', chunk => { data += chunk; });
            res2.on('end', () => { preloadParse(data, city, currLabel); });
          }).on('error', () => { failed++; checkDone(); });
        } else {
          let data = '';
          proxyRes.on('data', chunk => { data += chunk; });
          proxyRes.on('end', () => { preloadParse(data, city, currLabel); });
        }
      }).on('error', () => { failed++; checkDone(); });
    });
  });

  function preloadParse(html, city, currLabel) {
    try {
      const decoded = html.replace(/&quot;/g, '"').replace(/&amp;/g, '&');
      const listMatch = decoded.match(/"resultList"\s*:\s*\{"list"\s*:\s*\[([\s\S]*?)\]\s*,\s*"(?:total|count|pagination)/);
      if (listMatch) {
        const listJson = JSON.parse('[' + listMatch[1] + ']');
        const rates = [];
        for (const item of listJson) {
          if (item.name && item.exchange && typeof item.exchange.buy === 'number') {
            rates.push({ bank: item.name, code: item.code || '', buy: item.exchange.buy, sell: item.exchange.sale || item.exchange.sell, updated: item.exchange.refreshDate || null, newBills: item.isNewBanknotes === true });
          }
        }
        const seen = new Set();
        const unique = rates.filter(r => { if (seen.has(r.bank)) return false; seen.add(r.bank); return true; });
        const result = unique.slice(0, 15);
        if (result.length > 0) {
          setBankiCache(city, currLabel.toLowerCase(), { rates: result, city, currency: currLabel, count: result.length });
          loaded++;
        } else { failed++; }
      } else { failed++; }
    } catch(e) { failed++; }
    checkDone();
  }

  function checkDone() {
    if (loaded + failed >= preloadCities.length * preloadCurrencies.length) {
      console.log(`Cache preloaded: ${loaded} OK, ${failed} failed (of ${preloadCities.length * preloadCurrencies.length})`);
    }
  }
});
