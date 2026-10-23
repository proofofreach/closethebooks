// Run against a disposable demo only. See docs/RELEASE_AUDIT.md.
async (page) => {
  const base = 'http://127.0.0.1:5178';
  await page.goto(base + '/settings');
  await page.evaluate(() => localStorage.removeItem('books-theme'));
  await page.emulateMedia({colorScheme:'light'});
  await page.reload();
  if (await page.locator('html').getAttribute('data-theme') !== 'dark') throw new Error('Default theme must be dark on a light OS');
  await page.getByLabel('Theme', {exact:true}).selectOption('light');
  await page.reload();
  if (await page.locator('html').getAttribute('data-theme') !== 'light') throw new Error('Theme preference lost');
  await page.getByLabel('Theme', {exact:true}).selectOption('system');
  await page.emulateMedia({colorScheme:'dark'});
  await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
  await page.getByLabel('Theme', {exact:true}).selectOption('dark');
  const results = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const width of [1280, 768, 390, 320]) {
    await page.setViewportSize({width, height: 900});
    for (const route of ['/', '/import', '/accounts', '/accounts/new', '/invoices', '/invoices/new', '/ask', '/settings', '/migrate', '/migrations', '/migrate/files/quickbooks', '/migrate/files/wave', '/reports/']) {
      const response = await page.goto(base + route);
      if (!response.ok()) throw new Error(`${route}: ${response.status()}`);
      const metrics = await page.evaluate(() => ({
        width: innerWidth, scroll: document.documentElement.scrollWidth,
        unlabelled: [...document.querySelectorAll('input:not([type=hidden]),select,textarea')]
          .filter(e => !e.labels?.length && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby')).map(e => e.name),
      }));
      if (metrics.scroll > width) throw new Error(`${route} overflows at ${width}: ${metrics.scroll}`);
      if (!route.startsWith('/reports') && metrics.unlabelled.length) throw new Error(`${route}: unlabelled ${metrics.unlabelled}`);
      results.push({route, ...metrics});
    }
  }
  await page.goto(base + '/invoices/new');
  await page.getByLabel('Client', {exact:true}).fill('Release QA');
  await page.getByLabel('Description, line 1', {exact:true}).fill('Design services');
  await page.getByLabel('Amount, line 1', {exact:true}).fill('123.45');
  await page.getByRole('button', {name:'+ Add line', exact:true}).click();
  await page.getByLabel('Description, line 2', {exact:true}).fill('Review');
  await page.getByLabel('Amount, line 2', {exact:true}).fill('10.55');
  if (await page.locator('[data-invoice-total]').innerText() !== '134.00') throw new Error('Invoice total');
  await page.getByRole('button', {name:'Remove line 2', exact:true}).click();
  await page.getByLabel('Payment due in').fill('0');
  await page.getByRole('button', {name:'Create invoice', exact:true}).click();
  if (!await page.getByRole('heading', {name:/Invoice /}).count()) throw new Error('Invoice not created');
  const documentUrl = await page.getByRole('link', {name:'Open document', exact:true}).getAttribute('href');
  const invoiceHtml = await (await page.request.get(base + documentUrl)).text();
  const dates = [...invoiceHtml.matchAll(/<\/div>(\d{4}-\d{2}-\d{2})<\/div>/g)].map(match => match[1]);
  if (dates.length !== 2 || dates[0] !== dates[1]) throw new Error('Zero-day invoice terms');
  await page.goto(base + '/import');
  await page.getByLabel('Account', {exact:true}).selectOption('business-card');
  await page.getByLabel('Statement file', {exact:true}).setInputFiles({
    name:'release-qa.csv', mimeType:'text/csv',
    buffer:Buffer.from('Date,Description,Amount\n2026-09-10,Release QA,-12.34\n'),
  });
  await page.getByRole('button', {name:'Upload', exact:true}).click();
  await page.locator('tr').filter({hasText:'business-card--release-qa.csv'}).getByRole('link',{name:'Map CSV',exact:true}).click();
  for(const [label,value] of [['Date column','Date'],['Description column','Description'],['Amount column','Amount'],['Date order','iso'],['Decimal separator','dot'],['Positive amounts mean','inflow']]) await page.getByLabel(label,{exact:true}).selectOption(value);
  await page.getByRole('button',{name:'Preview all rows',exact:true}).click();
  await page.getByRole('button',{name:'Use this mapping',exact:true}).click();
  await page.getByRole('button', {name:'Prepare proposals', exact:true}).click();
  await page.waitForLoadState('networkidle');
  if (!await page.getByText('Release QA', {exact:true}).count()) throw new Error('Upload was not proposed');
  await page.getByLabel('Account', {exact:true}).selectOption('business-card');
  await page.getByLabel('Statement file', {exact:true}).setInputFiles({
    name:'release-qa.csv', mimeType:'text/csv',
    buffer:Buffer.from('Date,Description,Amount\n2026-09-10,Release QA,-12.34\n'),
  });
  await page.getByRole('button', {name:'Upload', exact:true}).click();
  if (await page.locator('td').filter({hasText:/business-card--release-qa/}).count() !== 1) throw new Error('Duplicate upload created another inbox file');
  await page.goto(base + '/accounts/new');
  const paypalKey = 'paypal-qa-' + Date.now();
  await page.getByLabel('Name', {exact:true}).fill('PayPal QA');
  await page.getByLabel('Key', {exact:false}).fill(paypalKey);
  await page.getByLabel('Track from').fill('2026-01-01');
  await page.getByLabel('Statement parser').selectOption('paypal');
  await page.getByRole('button', {name:'Add account', exact:true}).click();
  await page.goto(base + '/import');
  await page.getByLabel('Account', {exact:true}).selectOption(paypalKey);
  await page.getByLabel('Statement file', {exact:true}).setInputFiles({
    name:'paypal-qa.csv', mimeType:'text/csv',
    buffer:Buffer.from('Date,Name,Gross,Fee,Transaction ID,Status\n10/21/2026,PayPal QA client,20.00,-0.50,qa-payment,Completed\n'),
  });
  await page.getByRole('button', {name:'Upload', exact:true}).click();
  await page.getByRole('button', {name:'Prepare proposals', exact:true}).click();
  await page.waitForLoadState('networkidle');
  if (!await page.getByText('PayPal fee: PayPal QA client', {exact:true}).count()) throw new Error('PayPal fee not proposed');
  await page.goto(base + '/settings');
  await page.getByLabel('Name', {exact:true}).fill('Unsaved QA name');
  await page.getByLabel('Number prefix').fill('invalid prefix!');
  await page.getByRole('button', {name:'Save settings', exact:true}).click();
  if (await page.getByLabel('Name', {exact:true}).inputValue() !== 'Unsaved QA name') throw new Error('Settings discarded input');
  if (await page.locator('.form-errors').count() !== 1) throw new Error('Missing validation error');
  await page.goto(base + '/');
  await page.evaluate(() => document.documentElement.dataset.theme = 'light');
  await page.screenshot({path:'output/playwright/overview-mobile.png', fullPage:true});
  await page.setViewportSize({width:1280,height:900});
  await page.screenshot({path:'output/playwright/overview-desktop.png', fullPage:false});
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
  await page.screenshot({path:'output/playwright/overview-dark.png', fullPage:false});
  await page.emulateMedia({colorScheme:'light'});
  // Exercise client streaming without calling a paid AI provider.
  await page.route('**/ask', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    await new Promise(resolve => setTimeout(resolve, 500));
    return route.fulfill({contentType:'application/x-ndjson',body:
      JSON.stringify({type:'final',engine:'codex',text:'Fictional QA answer.'}) + '\n'});
  });
  await page.goto(base + '/ask');
  await page.evaluate(() => sessionStorage.setItem(`books-ask:${location.host}`, '{}'));
  await page.reload();
  await page.getByLabel('Question', {exact:true}).fill('QA streaming check');
  await page.getByRole('button', {name:'Ask', exact:true}).click();
  if (!await page.getByRole('button', {name:'New conversation', exact:true}).isDisabled()) throw new Error('Can clear an active answer');
  await page.getByText('Fictional QA answer.', {exact:true}).waitFor();
  if (await page.getByRole('button', {name:'Ask', exact:true}).isDisabled()) throw new Error('Ask stayed busy');
  for (const route of ['/', '/reports/', '/static/app.css']) {
    const denied = await page.request.get(base + route, {headers:{Host:'rebinding.example'}});
    if (denied.status() !== 403) throw new Error('Foreign Host accepted: ' + route);
  }
  if (errors.length) throw new Error(errors.join('\n'));
  return {passed:true, results, errors};
}
