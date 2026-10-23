// Fictional demo on :5178; verifies dash-separated date ambiguity notice.
async (page) => {
  await page.goto('http://127.0.0.1:5178/import');
  await page.getByLabel('Account',{exact:true}).selectOption('business-card');
  await page.getByLabel('Statement file',{exact:true}).setInputFiles({name:'ambiguity-qa.csv',mimeType:'text/csv',buffer:Buffer.from('When,Merchant,Value\n03-04-2026,Ambiguity QA,-1.23\n')});
  await page.getByRole('button',{name:'Upload',exact:true}).click();
  await page.locator('tr').filter({hasText:'business-card--ambiguity-qa.csv'}).getByRole('link',{name:'Map CSV',exact:true}).click();
  for(const [label,value] of [['Date column','When'],['Description column','Merchant'],['Amount column','Value'],['Date order','dmy'],['Decimal separator','dot'],['Positive amounts mean','inflow']]) await page.getByLabel(label,{exact:true}).selectOption(value);
  await page.getByRole('button',{name:'Preview all rows',exact:true}).click();
  if(!await page.getByText('2026-04-03',{exact:true}).count()) throw Error('Dash date order failed');
  if(!await page.getByText(/can also be read in the other day\/month order/).count()) throw Error('Missing ambiguity notice');
  await page.getByRole('button',{name:'Use this mapping',exact:true}).click();
  return {passed:true,date:'2026-04-03',warning:true};
}
