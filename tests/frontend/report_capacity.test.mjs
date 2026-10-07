import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch, openApp, moneyStats } from './harness.mjs';

const count = 10000;
const dateOf = (days) => { const d = new Date(); d.setDate(d.getDate() - days); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
const entries = Array.from({length:count}, (_,i) => ({id:`capacity-${i}`, syncToken:'0',
 date:dateOf(i%1800), hours:1, minutes:17, description:`Synthetic capacity row ${i} `+'x'.repeat(128),
 employee:`Synthetic person ${i%32}`, employeeId:`person-${i%32}`, nameOf:'Employee',
 service:'Synthetic consulting', itemId:'service-0', customer:`Synthetic client ${i%10}`,
 customerId:`client-${i%10}`, projectId:null, billable:true, billableStatus:'Billable', hourlyRate:100}));
const data = {entries, employees:Array.from({length:32},(_,i)=>({id:`person-${i}`,name:`Synthetic person ${i}`})),
 items:[{id:'service-0',name:'Synthetic consulting'}],
 projects:{projects:[],clients:Array.from({length:10},(_,i)=>({id:`client-${i}`,name:`Synthetic client ${i}`}))}};
let browser;
before(async()=>{browser=await launch();});
after(async()=>{await browser.close();});

test('five-year report renders every one of 10000 rows and exact minutes; oversize failure clears totals', {timeout:60000}, async()=>{
 const {ctx,page,errors}=await openApp(browser,data,'report');
 try {
  await page.click('#filterToggle');
  const started=performance.now();
  await page.check('#repAllTime');
  await page.waitForFunction(()=>document.querySelector('#reportFreshness').textContent.startsWith('Updated'),null,{timeout:30000});
  assert.equal(await page.locator('#repEntries .entry').count(),count);
  assert.equal(await page.textContent('#heroTotal'),'12833:20');
  assert.equal(await page.locator('#projRows .proj-row').count(),10);
  const elapsed=performance.now()-started;
  assert.ok(elapsed<15000,`synthetic report took ${elapsed}ms`);
  await page.route('**/api/timeactivities*',r=>r.fulfill({status:413,json:{detail:{message:'Synthetic response exceeds capacity'}}}));
  await page.uncheck('#repAllTime');
  await page.waitForFunction(()=>document.querySelector('#reportFreshness').textContent.includes('could not'),null,{timeout:15000});
  assert.equal(await page.isVisible('#repMain'),false,'previous totals must disappear');
  assert.match(await page.textContent('#repDrill'),/Synthetic response exceeds capacity/);
  assert.deepEqual(errors.filter(x=>!x.includes('413')),[]);
 } finally { await ctx.close(); }
});

test('dashboard aggregates 10000-source-row two-year WIP without dropping older entries', {timeout:45000}, async()=>{
 const {ctx,page,errors}=await openApp(browser,data,'dash');
 try {
  await page.waitForFunction(()=>document.querySelector('#dashMoney')?.textContent.includes('Unbilled WIP (2y)'));
  const selected=entries.filter(x=>x.date>=dateOf(730));
  const expected=selected.length*77/60*100;
  const stats=await moneyStats(page,'Accounts receivable');
  // WIP uses compact currency formatting, so verify the displayed value with
  // the existing formatter and independently computed exact source amount.
  const displayedExpected=await page.evaluate(v=>fmtK(v),expected);
  assert.equal(stats['Unbilled WIP (2y)'],displayedExpected);
  assert.deepEqual(errors,[]);
 } finally { await ctx.close(); }
});

test('two concurrent 5000-row comparison responses stay complete; comparison failure hides old figures', {timeout:60000}, async()=>{
 const prior=new Date(); prior.setFullYear(prior.getFullYear()-1);
 const priorDate=`${prior.getFullYear()}-${String(prior.getMonth()+1).padStart(2,'0')}-${String(prior.getDate()).padStart(2,'0')}`;
 const rows=[...entries.slice(0,5000).map(x=>({...x,date:dateOf(0)})),
  ...entries.slice(0,5000).map(x=>({...x,id:'prior-'+x.id,date:priorDate}))];
 const {ctx,page,errors}=await openApp(browser,{...data,entries:rows},'report');
 try {
  await page.click('#reportView .seg button[data-unit=month]');
  await page.waitForFunction(()=>document.querySelector('#reportFreshness').textContent.startsWith('Updated'));
  assert.equal(await page.locator('#repEntries .entry').count(),5000);
  assert.equal(await page.textContent('#heroTotal'),'6416:40');
  assert.match(await page.textContent('#repYoY'),/0% vs last year/);
  await page.route('**/api/timeactivities*',r=>{
   const u=new URL(r.request().url());
   return u.searchParams.get('start')?.startsWith(String(prior.getFullYear()))
    ?r.fulfill({status:502,json:{detail:{message:'Synthetic comparison unavailable'}}})
    :r.fulfill({json:rows.filter(x=>x.date>=u.searchParams.get('start')&&x.date<=u.searchParams.get('end'))});
  });
  await page.click('#reportView .seg button[data-unit=quarter]');
  await page.waitForFunction(()=>document.querySelector('#reportFreshness').textContent.includes('could not'));
  assert.equal(await page.isVisible('#repMain'),false);
  assert.match(await page.textContent('#repDrill'),/Synthetic comparison unavailable/);
  assert.deepEqual(errors.filter(x=>!x.includes('502')),[]);
 } finally {await ctx.close();}
});
