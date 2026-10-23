// Disposable empty workspace on :5179. Applies only fictional ledger history.
async (page) => {
  const base='http://127.0.0.1:5179';
  const csv='Entry,When,Ledger,Dr,Cr,CCY,Memo\nJ1,2024-01-01,Checking,100,0,USD,Sale\nJ1,2024-01-01,Sales,0,100,USD,Sale\nJ2,2024-01-02,Office,20,0,USD,Paper\nJ2,2024-01-02,Checking,0,20,USD,Paper\n';
  await page.goto(base+'/');
  if(!await page.getByRole('heading',{name:'Start with your files',exact:true}).count()) throw Error('Missing file-first onboarding');
  await page.getByRole('link',{name:'Move accounting history',exact:true}).click();
  await page.goto(base+'/migrate/files/quickbooks');
  await page.getByLabel('Journal export',{exact:true}).setInputFiles({name:'fictional-journal.csv',mimeType:'text/csv',buffer:Buffer.from(csv)});
  await page.getByRole('button',{name:'Inspect export',exact:true}).click();
  for(const [label,value] of [['Journal ID','Entry'],['Date','When'],['Account','Ledger'],['Debit','Dr'],['Credit','Cr'],['Currency (optional)','CCY'],['Description (optional)','Memo'],['Date format','iso'],['Currency of every amount','USD']]) await page.getByLabel(label,{exact:true}).selectOption(value);
  await page.getByLabel('Source debit total (USD)',{exact:true}).fill('120');
  await page.getByLabel('Source credit total (USD)',{exact:true}).fill('120');
  await page.getByLabel('Source report, date range, accounting basis, and currency',{exact:true}).fill('Fictional journal report, all 2024, accrual, USD');
  await page.getByRole('button',{name:'Map source accounts',exact:true}).click();
  for(const [label,value] of [['Checking','Assets:Bank:Checking'],['Sales','Income:Sales'],['Office','Expenses:Office']]) await page.getByLabel(label,{exact:true}).fill(value);
  await page.getByRole('button',{name:'Preview ledger history',exact:true}).click();
  if(!await page.getByRole('heading',{name:'Review ledger history',exact:true}).count()) throw Error('Missing verified preview');
  const previewUrl=page.url();
  const results=[];
  for(const width of [1280,390,320]) {
    await page.setViewportSize({width,height:1000});
    for(const route of ['/migrations','/migrate/files/quickbooks','/migrate/files/wave',previewUrl.replace(base,'')]) {
      const response=await page.goto(base+route);
      if(!response.ok()) throw Error(`${route} returned ${response.status()}`);
      const metrics=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,unlabelled:[...document.querySelectorAll('input:not([type=hidden]),select')].filter(e=>!e.labels?.length&&!e.getAttribute('aria-label')).length}));
      if(metrics.scroll>width||metrics.unlabelled) throw Error(`${route} invalid layout/labels at ${width}: ${JSON.stringify(metrics)}`);
      if (route.includes('/quickbooks/') && width === 320) {
        for (const cell of await page.locator('td[data-label="Credit (USD)"]').all()) {
          const box=await cell.boundingBox();
          if(!box || box.x < 0 || box.x+box.width > width) throw Error('Credit totals clipped on mobile');
        }
      }
      results.push({route,width,...metrics});
    }
  }
  await page.screenshot({path:'output/playwright/migration-preview-mobile.png',fullPage:true});
  await page.setViewportSize({width:1280,height:1000});
  await page.screenshot({path:'output/playwright/migration-preview-desktop.png'});
  await page.getByLabel('I reviewed the journals, account mapping, source totals, and coverage.',{exact:true}).check();
  await page.getByRole('button',{name:'Apply ledger history',exact:true}).click();
  if(!await page.getByRole('heading',{name:'Ledger history applied',exact:true}).count()) throw Error('Migration apply failed');
  await page.reload();
  if(await page.getByRole('button',{name:'Apply ledger history',exact:true}).count()) throw Error('Applied migration can replay');
  await page.goto(base+'/');
  if(await page.getByRole('link',{name:'Move accounting history',exact:true}).count()) throw Error('Overview offers second migration after history applied');
  return {passed:true,results,applied:true};
}
