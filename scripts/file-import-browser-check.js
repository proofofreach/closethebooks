// Run against a disposable demo on :5178. Writes repeatable JSON and screenshots.
async (page) => {
  const base = 'http://127.0.0.1:5178';
  await page.goto(base + '/import');
  await page.getByLabel('Account', {exact:true}).selectOption('business-card');
  await page.getByLabel('Statement file', {exact:true}).setInputFiles({name:'custom-bank.csv',mimeType:'text/csv',buffer:Buffer.from('When;Merchant;Value;Code\n21/10/2026;Mapping QA;12,34;map-1\n22/10/2026;Second row;2,00;map-2\n')});
  await page.getByRole('button',{name:'Upload',exact:true}).click();
  await page.getByRole('button',{name:'Prepare proposals',exact:true}).click();
  if (!await page.locator('.flash.error').filter({hasText:'Map CSV'}).count()) throw Error('Unmapped generic CSV was guessed');
  await page.locator('tr').filter({hasText:'business-card--custom-bank.csv'}).getByRole('link',{name:'Map CSV',exact:true}).click();
  await page.getByLabel('Separator',{exact:true}).selectOption(';');
  await page.getByRole('button',{name:'Read columns',exact:true}).click();
  await page.getByLabel('Date column',{exact:true}).selectOption('When');
  await page.getByLabel('Description column',{exact:true}).selectOption('Merchant');
  await page.getByLabel('Amount column',{exact:true}).selectOption('Value');
  await page.getByLabel('Reference column',{exact:true}).selectOption('Code');
  await page.getByLabel('Date order',{exact:true}).selectOption('dmy');
  await page.getByLabel('Decimal separator',{exact:true}).selectOption('comma');
  await page.getByLabel('Positive amounts mean',{exact:true}).selectOption('outflow');
  await page.getByRole('button',{name:'Preview all rows',exact:true}).click();
  if (!await page.getByRole('heading',{name:'Validated preview',exact:true}).count()) throw Error('Missing validated preview');
  if (!await page.getByText('−12.34 USD',{exact:true}).count()) throw Error('Sign/decimal mapping failed');
  await page.getByLabel('Positive amounts mean',{exact:true}).selectOption('inflow');
  await page.getByRole('button',{name:'Use this mapping',exact:true}).click();
  if (!await page.locator('.form-errors').filter({hasText:'The file or mapping changed'}).count()) throw Error('Stale preview was accepted');
  await page.getByLabel('Positive amounts mean',{exact:true}).selectOption('outflow');
  await page.getByRole('button',{name:'Preview all rows',exact:true}).click();
  await page.getByRole('button',{name:'Use this mapping',exact:true}).click();
  await page.getByRole('button',{name:'Prepare proposals',exact:true}).click();
  if (!await page.locator('.flash.success').filter({hasText:/Prepared/}).count()) throw Error('Prepare failed');
  if (!await page.getByText('Mapping QA',{exact:true}).count()) throw Error('Mapped row missing');
  const results=[];
  for (const width of [1280,390,320]) {
    await page.setViewportSize({width,height:900});
    for (const route of ['/import','/import/csv/business-card--custom-bank.csv']) {
      await page.goto(base+route);
      const scroll=await page.evaluate(()=>document.documentElement.scrollWidth);
      if(scroll>width) throw Error(`${route} overflows at ${width}: ${scroll}`);
      results.push({route,width,scroll});
    }
  }
  await page.screenshot({path:'output/playwright/csv-mapping-mobile.png',fullPage:true});
  await page.setViewportSize({width:1280,height:1000});
  await page.screenshot({path:'output/playwright/csv-mapping-desktop.png',fullPage:true});
  return {passed:true,results};
}
